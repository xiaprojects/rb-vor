#!/usr/bin/env python3
"""Replay an IQ capture through the production DSP chain.

Reference: implementation plan M3. This is the offline half of the daemon: the
same `rbvor.dsp.VorDemod` the live service uses, fed from a file instead of the
SDR. A bearing problem seen in the aircraft is reproduced on a desk with this.

Outputs a per-window CSV and a summary. Bearing conversion (sign, offset) is
M4; until then the raw phase difference is reported, which is the radial up to
one constant.

Examples
--------
    ./vor_replay.py captures/capture_20260724T101530Z_109400.cu8
    ./vor_replay.py synth_cvor_137deg.cu8 --csv /tmp/obs.csv --integration 3
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.dsp import DspParams, VorDemod  # noqa: E402
from rbvor.record import iq_blocks, read_sidecar  # noqa: E402

FIELDS = ["t", "delta_phi_deg", "phi_a_deg", "phi_b_deg", "amp_a", "amp_b",
          "sub_level", "carrier", "noise_est", "snr_db", "overrun"]


def band_survey(file, meta, block=1 << 17, nfft=1 << 14, search_khz=None):
    """Power spectrum of the whole recorded band, and where the carrier really is.

    The decoder assumes the VOR sits at `-tuner_offset_hz` in baseband (i.e. the
    carrier the tuner was steered to). If the real signal is shifted -- ppm
    error, a wrong offset in the sidecar, or simply a different station in the
    passband -- the decoder looks in the wrong place and recovers nothing. This
    finds the actual peak and reports the discrepancy.

    Returns a dict of findings; prints nothing.
    """
    fs = meta.sample_rate_hz
    # Welch-style average of |FFT|^2 over the whole file, no extra deps.
    acc = np.zeros(nfft, dtype=np.float64)
    win = np.hanning(nfft).astype(np.float32)
    n_seg = 0
    for blk in iq_blocks(file, block_samples=block, meta=meta):
        # chop the block into nfft segments
        m = (blk.size // nfft) * nfft
        if m == 0:
            continue
        segs = blk[:m].reshape(-1, nfft)
        for s in segs:
            acc += np.abs(np.fft.fft(s * win)) ** 2
            n_seg += 1
    if n_seg == 0:
        return {"ok": False}
    psd = np.fft.fftshift(acc / n_seg)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs))   # baseband Hz
    psd_db = 10 * np.log10(psd / psd.max() + 1e-20)

    # where the decoder EXPECTS the carrier in baseband: the generator places it
    # at exp(j*2*pi*offset_hz*t), i.e. AT tuner_offset_hz (negative = below
    # centre), and the DSP down-mixes by -offset_hz to bring it to zero. So the
    # carrier sits at offset_hz, not its negation.
    expected_hz = meta.tuner_offset_hz

    # search for the real peak. by default within +/- 100 kHz of expected, so a
    # strong adjacent FM broadcast image does not win.
    span = (search_khz or 100) * 1000
    mask = np.abs(freqs - expected_hz) <= span
    if not mask.any():
        mask = np.ones_like(freqs, dtype=bool)
    idx_local = np.argmax(np.where(mask, psd, -np.inf))
    peak_hz = float(freqs[idx_local])
    peak_db = float(psd_db[idx_local])

    # noise floor = median of the whole band; carrier-to-floor in dB
    floor_db = float(np.median(psd_db))
    peak_snr = peak_db - floor_db

    shift_hz = peak_hz - expected_hz
    # translate the shift into a ppm error at the tuned RF frequency
    centre_rf = meta.nav_freq_hz + meta.tuner_offset_hz
    ppm = (shift_hz / centre_rf) * 1e6 if centre_rf else float("nan")

    # mirror check: is there strong energy at the MIRROR of the expected
    # position (i.e. at -expected_hz)? A spectrally-flipped capture -- common on
    # some RTL-SDR setups -- puts the real signal on the wrong side of centre.
    mirror_hz = -expected_hz
    # compare a small band around each (avoid the DC spike at centre)
    def band_peak_db(centre):
        b = np.abs(freqs - centre) <= 3000
        return float(psd_db[b].max()) if b.any() else -200.0
    exp_db = band_peak_db(expected_hz)
    mir_db = band_peak_db(mirror_hz)
    mirrored = mir_db > exp_db + 3.0        # mirror clearly stronger

    # If mirrored, the real signal is at mirror_hz; measure its precise offset
    # from the ideal mirror position so we can report a real ppm (not the
    # peak-finder's artifact on the wrong side).
    mirror_shift_hz = 0.0
    mirror_ppm = float("nan")
    if mirrored:
        b = np.abs(freqs - mirror_hz) <= 5000
        if b.any():
            real_peak = float(freqs[b][np.argmax(psd[b])])
            mirror_shift_hz = real_peak - mirror_hz
            if centre_rf:
                mirror_ppm = (mirror_shift_hz / centre_rf) * 1e6

    return {
        "ok": True, "freqs": freqs, "psd_db": psd_db,
        "expected_hz": expected_hz, "peak_hz": peak_hz, "peak_db": peak_db,
        "floor_db": floor_db, "peak_snr": peak_snr, "shift_hz": shift_hz,
        "ppm": ppm, "n_seg": n_seg, "nfft": nfft, "fs": fs,
        "mirror_hz": mirror_hz, "exp_db": exp_db, "mir_db": mir_db,
        "mirrored": mirrored, "mirror_shift_hz": mirror_shift_hz,
        "mirror_ppm": mirror_ppm,
    }


def print_survey(s, meta):
    if not s.get("ok"):
        print("  band survey: capture too short for the FFT size")
        return
    fs = s["fs"]
    print("  band survey (whole recorded passband)")
    print(f"    resolution     {fs / s['nfft']:.0f} Hz/bin over "
          f"{fs / 1e3:g} kHz, {s['n_seg']} segments")
    print(f"    carrier found  {s['peak_hz'] / 1e3:+.1f} kHz from centre, "
          f"{s['peak_snr']:.1f} dB above floor")
    print(f"    decoder expects{s['expected_hz'] / 1e3:+.1f} kHz "
          f"(= tuner_offset)")
    if s.get("mirrored"):
        print(f"    MIRROR DETECTED: energy at {s['mirror_hz']/1e3:+.1f} kHz is "
              f"{s['mir_db']-s['exp_db']:.1f} dB stronger than at the")
        print("                   expected position. The spectrum is FLIPPED --")
        print("                   the real signal is on the wrong side of centre.")
        print("                   Fix: decode with --swap-iq, or set "
              '"swap_iq": true in config.')
    if s.get("mirrored"):
        # When the band is mirrored, the peak-finder locks onto the real signal's
        # image and any "shift"/"ppm" it reports is an artifact of that, not a
        # tuning error. Measure the true offset on the MIRROR side (where the
        # real signal is) and only warn if THAT is genuinely displaced.
        mir_shift = s.get("mirror_shift_hz", 0.0)
        if abs(mir_shift) < fs / s["nfft"] * 2:
            print("    shift          none once un-mirrored -- the carrier is on "
                  "frequency")
            print("                   (ignore any ppm hint: the apparent shift is "
                  "the mirror)")
        else:
            mir_ppm = s.get("mirror_ppm", float("nan"))
            print(f"    shift          {mir_shift:+.0f} Hz ({mir_ppm:+.1f} ppm) "
                  "on the mirror side")
            print("                   real residual offset after un-mirroring; "
                  "ppm may be worth setting")
    else:
        shift = s["shift_hz"]
        if abs(shift) < fs / s["nfft"]:
            print(f"    shift          {shift:+.0f} Hz -- on target, "
                  "decoder is looking in the right place")
        else:
            rf_hint = (meta.nav_freq_hz + shift) / 1e6
            print(f"    shift          {shift:+.0f} Hz  ({s['ppm']:+.1f} ppm)  <-- "
                  "SIGNAL IS DISPLACED")
            print(f"                   the real peak is at ~{rf_hint:.4f} MHz RF")
            if abs(s["ppm"]) > 1:
                print(f"                   set sdr.ppm to about {s['ppm']:+.0f}, "
                      "or the station is off-frequency")

    # a compact ASCII spectrum so a displaced or absent carrier is visible in
    # a terminal, no plotting library needed.
    _ascii_spectrum(s)


def _ascii_spectrum(s, width=64, rows=12):
    freqs, psd_db = s["freqs"], s["psd_db"]

    # downsample to `width` columns by taking the max in each bucket (peaks show)
    n = len(psd_db)
    bucket = n // width
    if bucket < 1:
        return
    cols = np.array([psd_db[i * bucket:(i + 1) * bucket].max() for i in range(width)])
    fcols = np.array([freqs[i * bucket + bucket // 2] for i in range(width)])
    lo, hi = float(cols.min()), float(cols.max())
    rng = max(hi - lo, 1.0)
    print("    spectrum (dB, peak-per-column):")
    for r in range(rows):
        level = hi - (r / rows) * rng
        line = "".join("#" if c >= level else " " for c in cols)
        print(f"      {level:6.0f} |{line}")
    # frequency axis: mark centre and edges
    axis = [" "] * width
    for target, ch in ((freqs.min(), "["), (0.0, "|"), (freqs.max(), "]")):
        j = int(np.argmin(np.abs(fcols - target)))
        axis[min(j, width - 1)] = ch
    # mark the expected and found carrier positions
    je = int(np.argmin(np.abs(fcols - s["expected_hz"])))
    jf = int(np.argmin(np.abs(fcols - s["peak_hz"])))
    axis[min(je, width - 1)] = "E"
    axis[min(jf, width - 1)] = "^"
    print(f"             {''.join(axis)}")
    print(f"             {freqs.min() / 1e3:+.0f} kHz{' ' * (width - 16)}"
          f"centre{' ' * (width - 20)}{freqs.max() / 1e3:+.0f} kHz")
    print("             E = decoder expects carrier,  ^ = carrier found,  "
          "| = centre")


def circ_mean_deg(x: np.ndarray) -> float:
    return float(np.rad2deg(np.angle(np.mean(np.exp(1j * np.deg2rad(x))))) % 360.0)


def circ_std_deg(x: np.ndarray) -> float:
    r = float(np.abs(np.mean(np.exp(1j * np.deg2rad(x)))))
    return float(np.rad2deg(np.sqrt(-2.0 * np.log(max(r, 1e-12)))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--csv", type=Path, default=None)
    ap.add_argument("--integration", type=float, default=1.0,
                    help="seconds; must be a whole number of 30 Hz cycles")
    ap.add_argument("--hop", type=float, default=1.0)
    ap.add_argument("--skip", type=int, default=2,
                    help="leading windows to discard (filter transient)")
    ap.add_argument("--block", type=int, default=1 << 17)
    ap.add_argument("--survey", action="store_true",
                    help="spectral survey of the whole band; finds a shifted or "
                         "absent carrier before decoding")
    ap.add_argument("--survey-nfft", type=int, default=1 << 14,
                    help="FFT size for --survey (default 16384)")
    ap.add_argument("--survey-span-khz", type=float, default=100,
                    help="+/- kHz around the expected carrier to search for the peak")
    ap.add_argument("--swap-iq", action="store_true",
                    help="conjugate IQ to un-mirror a spectrally-flipped capture "
                         "(some RTL-SDR setups mirror the band; the survey shows "
                         "the carrier on the wrong side of centre)")
    ap.add_argument("--list", action="store_true",
                    help="print every window's radial, not just the mean -- shows "
                         "how the bearing moves within one capture")
    ap.add_argument("--phase-sign", type=int, choices=(1, -1), default=None,
                    help="apply this phase_sign so --list prints true radials, "
                         "not raw delta_phi")
    ap.add_argument("--phase-offset", type=float, default=0.0,
                    help="phase_offset_deg + station_cal_deg to add after phase_sign")
    a = ap.parse_args(argv)

    meta = read_sidecar(a.file)

    if a.survey:
        s = band_survey(a.file, meta, block=a.block, nfft=a.survey_nfft,
                        search_khz=a.survey_span_khz)
        print(f"file            {a.file}")
        print(f"fs {meta.sample_rate_hz/1e3:g} kHz  "
              f"offset {meta.tuner_offset_hz/1e3:+g} kHz  "
              f"nav {meta.nav_freq_hz/1e6:.3f} MHz")
        print()
        print_survey(s, meta)
        print()

    # The 30 Hz tone integration must span a whole number of cycles, or spectral
    # leakage biases the phase and the demodulator produces nothing. Catch the
    # common mistake (e.g. --integration 0.25 = 7.5 cycles) with a clear message
    # instead of silent empty output.
    cycles = a.integration * 30.0
    if abs(round(cycles) - cycles) > 1e-6:
        nearest = [n / 30.0 for n in (round(cycles) - 1, round(cycles),
                                      round(cycles) + 1) if n > 0]
        print(f"{a.file}: --integration {a.integration:g}s is {cycles:.2f} cycles "
              "of the 30 Hz tone, which must be a whole number.", file=sys.stderr)
        print("  Valid nearby values: "
              + ", ".join(f"{v:.4g}s ({v*30:.0f} cyc)" for v in nearest),
              file=sys.stderr)
        print("  Common choices: 0.1 (3), 0.2 (6), 0.5 (15), 1.0 (30 cycles).",
              file=sys.stderr)
        return 2

    p = DspParams(fs=meta.sample_rate_hz, offset_hz=meta.tuner_offset_hz,
                  integration_s=a.integration, hop_s=a.hop)
    d = VorDemod(p)

    obs = []
    for blk in iq_blocks(a.file, block_samples=a.block, meta=meta):
        obs += d.push(np.conj(blk) if a.swap_iq else blk)

    if len(obs) <= a.skip:
        print(f"{a.file}: only {len(obs)} windows -- capture too short", file=sys.stderr)
        return 1
    kept = [o for o in obs[a.skip:] if not o.overrun]
    dropped = len(obs) - a.skip - len(kept)

    if a.csv:
        a.csv.parent.mkdir(parents=True, exist_ok=True)
        with a.csv.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(FIELDS)
            for o in obs:
                w.writerow([f"{o.t:.3f}", f"{np.rad2deg(o.delta_phi) % 360:.4f}",
                            f"{np.rad2deg(o.phi_a):.4f}", f"{np.rad2deg(o.phi_b):.4f}",
                            f"{o.amp_a:.5f}", f"{o.amp_b:.2f}", f"{o.sub_level:.5f}",
                            f"{o.carrier:.6g}", f"{o.noise_est:.6f}",
                            f"{o.snr_db:.2f}", int(o.overrun)])

    dphi = np.array([np.rad2deg(o.delta_phi) % 360.0 for o in kept])
    mean = circ_mean_deg(dphi)

    print(f"file            {a.file}")
    print(f"source          {'synthetic' if meta.synthetic else 'live'}  "
          f"{meta.station} {meta.site} {meta.notes}".rstrip())
    print(f"fs {meta.sample_rate_hz/1e3:g} kHz  offset {meta.tuner_offset_hz/1e3:+g} kHz  "
          f"nav {meta.nav_freq_hz/1e6:.3f} MHz")
    print(f"windows         {len(obs)} total, {a.skip} skipped, "
          f"{dropped} overrun, {len(kept)} used  "
          f"({a.integration:g} s integration, {a.hop:g} s hop)")
    print()
    print(f"  delta_phi       {mean:8.3f} deg   circular std {circ_std_deg(dphi):.3f}")
    print(f"  30 Hz AM depth  {np.mean([o.amp_a for o in kept]):8.3f}   nominal 0.30")
    print(f"  subcarrier      {np.mean([o.sub_level for o in kept]):8.3f}   "
          "nominal 0.30 (band measurement, A7)")
    print(f"  deviation       {np.mean([o.amp_b for o in kept]):8.1f} Hz  nominal 480")
    print(f"  30 Hz SNR       {np.mean([o.snr_db for o in kept]):8.1f} dB")

    if a.list:
        # per-window radial, so a moving receiver (a car orbit, a flight leg)
        # shows the bearing evolving rather than collapsing to one mean.
        def to_radial(dp_deg):
            if a.phase_sign is None:
                return dp_deg                      # raw delta_phi
            return (a.phase_sign * dp_deg + a.phase_offset) % 360.0
        label = "radial" if a.phase_sign is not None else "delta_phi"
        print()
        print(f"  per-window {label} ({a.integration:g}s integration, "
              f"{a.hop:g}s hop):")
        print(f"    {'t (s)':>7}  {label:>8}  {'AM':>5}  {'SNR':>5}  {'lock'}")
        # per-window circular quality: compare each raw window to the running mean
        for o in kept:
            dp = np.rad2deg(o.delta_phi) % 360.0
            rad = to_radial(dp)
            lock = "ok" if o.amp_a > 0.15 and o.snr_db > 15 else "weak"
            print(f"    {o.t:7.1f}  {rad:8.1f}  {o.amp_a:5.3f}  "
                  f"{o.snr_db:5.1f}  {lock}")

    if meta.expected_radial_deg is not None:
        resid = (mean - meta.expected_radial_deg) % 360.0
        print()
        print(f"  expected radial {meta.expected_radial_deg:8.1f} deg")
        print(f"  residual        {resid:8.3f} deg  <-- constant to be measured "
              "as phase_offset_deg in M4")
    else:
        print()
        print("  no expected radial in the sidecar; delta_phi is the radial up to")
        print("  one constant. Bearing conversion is M4.")
    if a.csv:
        print(f"\n  wrote {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
