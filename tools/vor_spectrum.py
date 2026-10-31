#!/usr/bin/env python3
"""Inspect an IQ capture: RF spectrum, AM-envelope spectrum, measured tone levels.

Reference: implementation plan M2. Used to prove a capture (synthetic or real)
contains what it should before any decoding is attempted, and to eyeball a
field recording that decoded badly.

Two outputs:
  * a text report, always -- works headless over ssh, and is what goes into an
    issue or a test report;
  * a PNG, if matplotlib is available and --png is given.

The text report is deliberately the primary output. A number in a table can be
compared between two captures; a picture cannot.

Examples
--------
    ./vor_spectrum.py captures/capture_20260724T101530Z_109400.cu8
    ./vor_spectrum.py synth_cvor_137deg.cu8 --png docs/synth_spectrum.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.record import iq_blocks, read_sidecar  # noqa: E402

SUBCARRIER_HZ = 9960.0
TONE_HZ = 30.0
IDENT_HZ = 1020.0


def welch(x: np.ndarray, fs: int, nfft: int = 8192) -> tuple[np.ndarray, np.ndarray]:
    """Averaged periodogram. Hand-rolled to keep scipy optional for this tool."""
    win = np.hanning(nfft)
    nseg = max(1, x.size // nfft)
    acc = np.zeros(nfft // 2 + 1)
    for i in range(nseg):
        seg = x[i * nfft:(i + 1) * nfft]
        if seg.size < nfft:
            break
        acc += np.abs(np.fft.rfft(np.real(seg) * win)) ** 2
    psd = acc / max(1, nseg)
    return np.fft.rfftfreq(nfft, 1.0 / fs), psd


def band_amp(audio: np.ndarray, fs: int, lo: float, hi: float) -> float:
    """Peak-equivalent amplitude of the energy between lo and hi Hz."""
    X = np.fft.rfft(audio)
    f = np.fft.rfftfreq(audio.size, 1.0 / fs)
    X[(f < lo) | (f > hi)] = 0.0
    return float(np.std(np.fft.irfft(X, n=audio.size)) * np.sqrt(2))


def analyse(path: Path, seconds: float = 4.0) -> dict:
    meta = read_sidecar(path)
    fs = meta.sample_rate_hz
    want = int(seconds * fs)
    chunks, got = [], 0
    for blk in iq_blocks(path, block_samples=1 << 16, meta=meta):
        chunks.append(blk)
        got += blk.size
        if got >= want:
            break
    if not chunks:
        raise SystemExit(f"{path}: no samples")
    z = np.concatenate(chunks)[:want]

    # Where is the carrier, really? Do not trust the sidecar offset blindly --
    # a ppm error or a wrong --offset shows up here and nowhere else. A tuner
    # parked at nav + offset puts the navaid at -offset in baseband (A22).
    nfft = min(1 << 16, 1 << int(np.floor(np.log2(z.size))))
    spec = np.fft.fftshift(np.abs(np.fft.fft(z[:nfft])))
    faxis = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs))
    f_peak = float(faxis[int(np.argmax(spec))])

    n = np.arange(z.size)
    base = z * np.exp(-2j * np.pi * f_peak * n / fs)
    env = np.abs(base).astype(np.float64)
    carrier = float(np.mean(env))
    audio = env - carrier

    d = {
        "path": str(path),
        "format": meta.format,
        "fs": fs,
        "samples": z.size,
        "seconds": z.size / fs,
        "nav_freq_mhz": meta.nav_freq_hz / 1e6,
        "sidecar_offset_hz": meta.tuner_offset_hz,
        "expected_carrier_hz": -meta.tuner_offset_hz,
        "measured_offset_hz": f_peak,
        "offset_error_hz": f_peak - (-meta.tuner_offset_hz),
        # closer to +offset (the mirror) than to -offset (where it belongs)
        "mirrored": bool(meta.tuner_offset_hz) and (
            abs(f_peak - meta.tuner_offset_hz) < abs(f_peak + meta.tuner_offset_hz)),
        "carrier_level": carrier,
        # Band measurements, not bin measurements -- see amendment A7.
        "am30_depth": band_amp(audio, fs, 20, 40) / carrier,
        "ident1020_depth": band_amp(audio, fs, 900, 1140) / carrier,
        "subcarrier_depth": band_amp(audio, fs, SUBCARRIER_HZ - 900,
                                     SUBCARRIER_HZ + 900) / carrier,
        "sub_bin_only": band_amp(audio, fs, SUBCARRIER_HZ - 20,
                                 SUBCARRIER_HZ + 20) / carrier,
        "noise_floor_depth": band_amp(audio, fs, 4000, 8000) / carrier,
        "expected_radial_deg": meta.expected_radial_deg,
        "synthetic": meta.synthetic is not None,
        "station": meta.station,
        "site": meta.site,
        "notes": meta.notes,
    }
    d["am30_snr_db"] = 20 * np.log10(
        max(d["am30_depth"], 1e-12) / max(d["noise_floor_depth"], 1e-12))
    return d, z, env, audio, meta


def report(d: dict) -> str:
    L = []
    L.append(f"file          {d['path']}")
    L.append(f"format        {d['format']}  fs {d['fs']/1e3:g} kHz  "
             f"{d['seconds']:.2f} s  ({'synthetic' if d['synthetic'] else 'live'})")
    if d["station"] or d["site"]:
        L.append(f"context       {d['station']} @ {d['site']} {d['notes']}")
    L.append(f"nav freq      {d['nav_freq_mhz']:.3f} MHz")
    L.append(f"tuner offset  sidecar {d['sidecar_offset_hz']/1e3:+.1f} kHz, so the "
             f"carrier is expected at {d['expected_carrier_hz']/1e3:+.1f} kHz; "
             f"measured {d['measured_offset_hz']/1e3:+.3f} kHz "
             f"(error {d['offset_error_hz']:+.1f} Hz)")
    if d["mirrored"]:
        L.append("    ^ the carrier is on the MIRROR side of centre -- see "
                 "vor_replay.py --survey (A22)")
    L.append("")
    L.append("  component            depth    nominal   verdict")
    for key, label, nom in (("am30_depth", "30 Hz AM", 0.30),
                            ("subcarrier_depth", "9960 Hz subcarrier", 0.30),
                            ("ident1020_depth", "1020 Hz ident", None)):
        v = d[key]
        if nom is None:
            verdict = "present" if v > 0.01 else "absent"
        else:
            verdict = "ok" if 0.5 * nom <= v <= 1.5 * nom else "OUT OF RANGE"
        nom_s = f"{nom:.2f}" if nom else "   -"
        L.append(f"  {label:<20} {v:6.3f}    {nom_s}     {verdict}")
    L.append(f"  {'noise floor 4-8 kHz':<20} {d['noise_floor_depth']:6.3f}")
    L.append(f"  {'30 Hz SNR':<20} {d['am30_snr_db']:6.1f} dB")
    L.append("")
    L.append(f"  subcarrier measured as a narrow bin at 9960 Hz: "
             f"{d['sub_bin_only']:.4f}")
    L.append("    ^ expected to be far below the band figure: with beta = 16 the")
    L.append("      component at 9960 Hz itself is only J0(16) = -0.175 of the")
    L.append("      total. Gauge the BAND, never the bin (amendment A7).")
    if d["expected_radial_deg"] is not None:
        L.append("")
        L.append(f"  expected radial {d['expected_radial_deg']:.1f} deg "
                 "(from sidecar; decoding is M3)")
    return "\n".join(L)


def make_png(path: Path, z, env, audio, meta, out: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  (matplotlib not installed; skipping PNG)", file=sys.stderr)
        return False
    fs = meta.sample_rate_hz
    fig, ax = plt.subplots(3, 1, figsize=(9, 9))

    nfft = min(1 << 15, 1 << int(np.floor(np.log2(z.size))))
    spec = 20 * np.log10(np.fft.fftshift(np.abs(np.fft.fft(z[:nfft]))) + 1e-12)
    fx = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs)) / 1e3
    ax[0].plot(fx, spec, lw=0.6)
    ax[0].set_title(f"RF spectrum — {path.name}")
    ax[0].set_xlabel("kHz from tuner centre")
    ax[0].set_ylabel("dB")
    ax[0].grid(alpha=0.3)

    f, psd = welch(audio, fs, nfft=8192)
    ax[1].semilogy(f, psd + 1e-20, lw=0.6)
    for mark, lab in ((TONE_HZ, "30"), (IDENT_HZ, "1020"), (SUBCARRIER_HZ, "9960")):
        ax[1].axvline(mark, color="r", ls="--", lw=0.7)
        ax[1].text(mark, ax[1].get_ylim()[1], lab, color="r", fontsize=7,
                   ha="center", va="top")
    ax[1].set_xlim(0, min(15000, fs / 2))
    ax[1].set_title("AM envelope spectrum")
    ax[1].set_xlabel("Hz")
    ax[1].grid(alpha=0.3)

    n = min(int(0.2 * fs), audio.size)
    ax[2].plot(np.arange(n) / fs * 1e3, env[:n], lw=0.5)
    ax[2].set_title("AM envelope, first 200 ms")
    ax[2].set_xlabel("ms")
    ax[2].grid(alpha=0.3)

    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("-t", "--seconds", type=float, default=4.0)
    ap.add_argument("--png", type=Path, default=None)
    a = ap.parse_args(argv)

    d, z, env, audio, meta = analyse(a.file, a.seconds)
    print(report(d))
    if a.png and make_png(a.file, z, env, audio, meta, a.png):
        print(f"\n  wrote {a.png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
