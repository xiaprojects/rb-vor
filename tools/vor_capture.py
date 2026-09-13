#!/usr/bin/env python3
"""Capture live IQ from an RTL-SDR at a VOR channel, with a full sidecar.

Reference: rb-vor-architecture.md sections 5, 7.2 and 12; implementation plan M5
(pulled forward -- hardware is on the bench).

This is the field-debug tool and the bug-report attachment format. A capture
made with this tool replays byte-identically through `tools/vor_replay.py` and
through the daemon's own DSP path, so a bearing error seen in the aircraft can
be reproduced on a desk.

Three backends are tried in order; the first that imports/executes wins:
    1. SoapySDR      -- preferred, vendor-neutral (doc A section 2)
    2. pyrtlsdr      -- fallback binding
    3. rtl_sdr(1)    -- CLI fallback, always present with the rtl-sdr package

Offset tuning is applied by default: the tuner is placed at
`nav_freq + tuner_offset` with the offset at -fs/4, which keeps the RTL DC spike
and IQ imbalance out of the 12 kHz channel AND makes the later digital down-mix
a multiply-free +/-1, +/-j sequence (amendment A1).

Examples
--------
    # 60 s of a known station, everything recorded in the sidecar
    ./vor_capture.py -f 109.4 -t 60 --site "LIQQ apron" --station PRU \\
        --lat 43.0959 --lon 12.5132 --expected-radial 137 \\
        --antenna "V-dipole horizontal 670 mm" --filter-chain "FM bandstop"

    # car bench test, ident only, short
    ./vor_capture.py -f 113.35 -t 10 --notes "car, engine off, roof mount"
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.freqrules import channel_khz, is_vor_frequency  # noqa: E402
from rbvor.record import IQMeta, IQWriter, default_basename  # noqa: E402

DEFAULT_FS = 1_024_000
DEFAULT_BLOCK = 32768


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-f", "--freq", type=float, required=True,
                   help="navaid frequency in MHz, e.g. 109.4")
    p.add_argument("-t", "--seconds", type=float, default=30.0)
    p.add_argument("-o", "--outdir", type=Path, default=Path("captures"))
    p.add_argument("--fs", type=int, default=DEFAULT_FS, help="sample rate, Hz")
    p.add_argument("--offset", type=int, default=None,
                   help="tuner offset in Hz (default: -fs/4)")
    p.add_argument("--gain", type=float, default=32.8,
                   help="tuner gain in dB; use --agc for hardware AGC")
    p.add_argument("--agc", action="store_true", help="hardware AGC instead of --gain")
    p.add_argument("--ppm", type=int, default=0)
    p.add_argument("--device", default="", help="SoapySDR device args or serial")
    p.add_argument("--backend", choices=["auto", "soapy", "pyrtlsdr", "rtl_sdr"],
                   default="auto")
    p.add_argument("--allow-non-vor", action="store_true",
                   help="capture a localiser or off-grid channel anyway")
    # sidecar context -- cheap to type, priceless three months later
    p.add_argument("--site", default="")
    p.add_argument("--station", default="")
    p.add_argument("--lat", type=float, default=None)
    p.add_argument("--lon", type=float, default=None)
    p.add_argument("--gps-track", type=float, default=None)
    p.add_argument("--expected-radial", type=float, default=None,
                   help="magnetic radial FROM the station, from map or GPS")
    p.add_argument("--antenna", default="")
    p.add_argument("--filter-chain", default="")
    p.add_argument("--notes", default="")
    return p.parse_args(argv)


def build_meta(args, fmt: str, center: int, nav: int, driver: str, device: str) -> IQMeta:
    return IQMeta(
        format=fmt,
        sample_rate_hz=args.fs,
        center_freq_hz=center,
        nav_freq_hz=nav,
        tuner_offset_hz=center - nav,
        gain_db=None if args.agc else args.gain,
        gain_mode="agc" if args.agc else "manual",
        ppm=args.ppm,
        device=device,
        driver=driver,
        site=args.site,
        lat=args.lat,
        lon=args.lon,
        gps_track_deg=args.gps_track,
        expected_radial_deg=args.expected_radial,
        station=args.station,
        antenna=args.antenna,
        filter_chain=args.filter_chain,
        notes=args.notes,
    )


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #

def capture_soapy(args, center: int, nav: int, out: Path) -> Path:
    import SoapySDR  # type: ignore
    from SoapySDR import SOAPY_SDR_CS8, SOAPY_SDR_RX  # type: ignore

    dev_args = args.device or "driver=rtlsdr"
    sdr = SoapySDR.Device(dict(kv.split("=", 1) for kv in dev_args.split(",") if "=" in kv)
                          if "=" in dev_args else dev_args)
    sdr.setSampleRate(SOAPY_SDR_RX, 0, args.fs)
    sdr.setFrequency(SOAPY_SDR_RX, 0, center)
    sdr.setFrequencyCorrection(SOAPY_SDR_RX, 0, args.ppm)
    sdr.setGainMode(SOAPY_SDR_RX, 0, bool(args.agc))
    if not args.agc:
        sdr.setGain(SOAPY_SDR_RX, 0, args.gain)

    meta = build_meta(args, "cs8", center, nav, "soapy", dev_args)
    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS8)
    sdr.activateStream(stream)
    buf = np.empty(DEFAULT_BLOCK * 2, dtype=np.int8)
    target = int(args.seconds * args.fs)
    overruns = 0
    try:
        with IQWriter(out, meta) as w:
            got = 0
            while got < target:
                sr = sdr.readStream(stream, [buf], DEFAULT_BLOCK, timeoutUs=1_000_000)
                if sr.ret <= 0:
                    overruns += 1
                    if overruns > 20:
                        raise RuntimeError(f"stream stalled, code {sr.ret}")
                    continue
                w.write(buf[: sr.ret * 2])
                got += sr.ret
                _progress(got, target)
    finally:
        sdr.deactivateStream(stream)
        sdr.closeStream(stream)
    if overruns:
        print(f"\n  warning: {overruns} stream overruns/timeouts", file=sys.stderr)
    return Path(str(out) + ".cs8")


def capture_pyrtlsdr(args, center: int, nav: int, out: Path) -> Path:
    from rtlsdr import RtlSdr  # type: ignore

    sdr = RtlSdr()
    sdr.sample_rate = args.fs
    sdr.center_freq = center
    sdr.freq_correction = args.ppm if args.ppm else 1
    sdr.gain = "auto" if args.agc else args.gain
    meta = build_meta(args, "cu8", center, nav, "pyrtlsdr", "index=0")
    target = int(args.seconds * args.fs)
    try:
        with IQWriter(out, meta) as w:
            got = 0
            while got < target:
                raw = sdr.read_bytes(DEFAULT_BLOCK * 2)
                w.write(np.frombuffer(raw, dtype=np.uint8))
                got += DEFAULT_BLOCK
                _progress(got, target)
    finally:
        sdr.close()
    return Path(str(out) + ".cu8")


def capture_rtl_sdr_cli(args, center: int, nav: int, out: Path) -> Path:
    exe = shutil.which("rtl_sdr")
    if not exe:
        raise RuntimeError("rtl_sdr not found in PATH")
    target = int(args.seconds * args.fs)
    # rtl_sdr writes unsigned 8-bit offset binary -- cu8, see amendment A4.
    path = Path(str(out) + ".cu8")
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-f", str(center), "-s", str(args.fs), "-n", str(target),
           "-p", str(args.ppm)]
    if args.agc:
        cmd += ["-g", "0"]
    else:
        cmd += ["-g", str(args.gain)]
    cmd.append(str(path))
    print("  $ " + " ".join(cmd))
    subprocess.run(cmd, check=True)
    meta = build_meta(args, "cu8", center, nav, "rtl_sdr", "index=0")
    meta.samples = path.stat().st_size // 2
    meta.duration_s = meta.samples / float(args.fs)
    from rbvor.record import write_sidecar
    write_sidecar(path, meta)
    return path


BACKENDS = {
    "soapy": capture_soapy,
    "pyrtlsdr": capture_pyrtlsdr,
    "rtl_sdr": capture_rtl_sdr_cli,
}


def _progress(got: int, target: int) -> None:
    pct = 100.0 * got / target
    print(f"\r  capturing {pct:5.1f}%", end="", flush=True)


def main(argv=None) -> int:
    args = parse_args(argv)

    ok, reason = is_vor_frequency(args.freq)
    print(f"channel check: {reason}")
    if not ok and not args.allow_non_vor:
        print("refusing to capture; pass --allow-non-vor to override", file=sys.stderr)
        return 2
    ch = channel_khz(args.freq)
    nav = (ch * 1000) if ch else int(round(args.freq * 1e6))
    offset = args.offset if args.offset is not None else -args.fs // 4
    center = nav + offset
    print(f"nav {nav/1e6:.3f} MHz  offset {offset/1e3:+.1f} kHz  "
          f"tuner {center/1e6:.3f} MHz  fs {args.fs/1e3:.0f} kHz")

    out = args.outdir / default_basename(nav)
    order = [args.backend] if args.backend != "auto" else ["soapy", "pyrtlsdr", "rtl_sdr"]
    last_err = None
    for name in order:
        try:
            print(f"backend: {name}")
            t0 = time.time()
            path = BACKENDS[name](args, center, nav, out)
            print(f"\n  wrote {path}  ({time.time()-t0:.1f} s wall)")
            print(f"  sidecar {path.with_suffix('.json')}")
            return 0
        except (ImportError, RuntimeError, FileNotFoundError, OSError) as e:
            print(f"  backend {name} unavailable: {e}", file=sys.stderr)
            last_err = e
    print(f"no working SDR backend: {last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
