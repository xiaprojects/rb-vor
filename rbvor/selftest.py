"""End-to-end self test. No hardware, no bridge, no network.

Reference: implementation plan M8/M9; bench procedure `docs/bench-test.md`.

Run this first on a new Raspberry Pi, before touching an antenna. It exercises
the whole chain -- channel rules, synthetic signal, demodulation, bearing,
quality, publish policy -- on a known input with a known answer, and prints a
verdict per stage.

If the self test passes and a real station does not decode, the problem is in
the RF chain or the calibration, not in the software. That is worth ten minutes
of not chasing the wrong thing.

    python3 -m rbvor --self-test
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

from rbvor import IF_VERSION, __version__

FS = 256_000
RADIAL = 137.0

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


class _Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failed = 0

    def add(self, name: str, verdict: str, detail: str = "") -> None:
        self.rows.append((name, verdict, detail))
        if verdict == FAIL:
            self.failed += 1
        colour = {"PASS": "\033[32m", "FAIL": "\033[31m", "WARN": "\033[33m"}[verdict]
        reset = "\033[0m" if sys.stdout.isatty() else ""
        head = colour if sys.stdout.isatty() else ""
        print(f"  [{head}{verdict}{reset}] {name:<34} {detail}")


def run_self_test(config_path: Path | None = None) -> int:
    from rbvor.bearing import BearingCal, BearingTracker, angular_difference
    from rbvor.dsp import DspParams, VorDemod
    from rbvor.freqrules import classify, is_vor_frequency
    from rbvor.publisher import PublishPolicy
    from rbvor.quality import QualityEstimator, QualityParams
    from tools.vor_gen import VorParams, generate_blocks

    print(f"\nRB-VOR {__version__} self test  ({IF_VERSION})\n")
    r = _Report()

    # 1 -- channel rules
    ok_vor = is_vor_frequency(109.4)[0]
    ok_loc = not is_vor_frequency(109.5)[0]
    ok_band = str(classify(118.0)) == "OUT_OF_BAND"
    r.add("channel rules (109.4 / 109.5 / 118.0)",
          PASS if (ok_vor and ok_loc and ok_band) else FAIL,
          "109.4 VOR, 109.5 localiser, 118.0 out of band")

    # 2 -- configuration
    #
    # The self-test exists to prove the SOFTWARE works, independently of any
    # config. Everything below falls back to built-in defaults when `cfg is
    # None`, so a missing or unreadable config is a WARN, not a FAIL: on a fresh
    # checkout the config lives in /etc after install, and running the self-test
    # from the source tree is exactly the case where it is absent. A genuinely
    # malformed config that the schema rejects is still a FAIL -- that is a real
    # defect a builder must fix before the bench.
    cal = BearingCal(phase_sign=1, phase_offset_deg=-1.9657)
    cfg = None
    try:
        from rbvor.config import ConfigError, load
        try:
            cfg = load(config_path)
        except ConfigError as e:
            msg = str(e)
            if "not found" in msg or "no configuration found" in msg:
                r.add("configuration", WARN,
                      "no config found -- testing with built-in defaults "
                      "(normal from a source tree; it lives in /etc after install)")
            else:
                r.add("configuration loads and validates", FAIL, msg[:80])
        if cfg is not None:
            r.add("configuration loads and validates", PASS, str(cfg.source))
            if cfg.calibrated:
                cal = cfg.bearing
                r.add("bearing calibration present", PASS,
                      f"sign {cal.phase_sign:+d}, "
                      f"offset {cal.phase_offset_deg:+.4f} deg")
            else:
                r.add("bearing calibration present", WARN,
                      "not set -- the daemon will publish only flags (A2). "
                      "Run tools/vor_calibrate.py")
    except Exception as e:                           # noqa: BLE001
        r.add("configuration", FAIL, str(e)[:80])

    # 3 -- synthetic signal, full chain
    t0 = time.perf_counter()
    p = VorParams(radial_deg=RADIAL, fs=FS, seconds=12.0, offset_hz=-FS // 4,
                  snr_db=25.0, ident="TST", block=1 << 16)
    blocks = list(generate_blocks(p))
    gen_s = time.perf_counter() - t0
    r.add("synthetic signal generated", PASS,
          f"{RADIAL:.0f} deg radial, 25 dB SNR, {gen_s:.2f} s")

    d = VorDemod(DspParams(fs=FS, offset_hz=-FS // 4))
    t0 = time.perf_counter()
    obs = []
    for b in blocks:
        obs += d.push(b)
    dsp_s = time.perf_counter() - t0
    signal_s = sum(b.size for b in blocks) / FS
    r.add("demodulator produced windows",
          PASS if len(obs) >= 10 else FAIL, f"{len(obs)} windows")

    o = obs[-1]
    r.add("30 Hz AM depth", PASS if 0.2 < o.amp_a < 0.4 else FAIL,
          f"{o.amp_a:.3f} (nominal 0.30)")
    r.add("9960 Hz subcarrier (band measure, A7)",
          PASS if 0.15 < o.sub_level < 0.4 else FAIL,
          f"{o.sub_level:.3f} (nominal 0.30)")
    r.add("FM deviation", PASS if 400 < o.amp_b < 560 else FAIL,
          f"{o.amp_b:.0f} Hz (nominal 480)")

    # 4 -- bearing
    tr = BearingTracker(cal, history=5)
    q = QualityEstimator(cfg.quality if cfg else QualityParams())
    errs, vals = [], []
    for x in obs[2:]:
        b = tr.update(x.delta_phi)
        res = q.update(x, tr.raw)
        if not res.flagged:
            errs.append(angular_difference(b, RADIAL))
            vals.append(res.value)
    if errs:
        rms = float(np.sqrt(np.mean(np.square(errs))))
        r.add("bearing accuracy", PASS if rms < 1.0 else FAIL,
              f"{rms:.3f} deg RMS over {len(errs)} solutions "
              f"(expected < 1.0)")
        r.add("vorsignal on a good signal", PASS if min(vals) >= 70 else FAIL,
              f"{min(vals)}..{max(vals)} (expected >= 70)")
    else:
        r.add("bearing accuracy", FAIL, "no unflagged solution produced")

    # 5 -- flag behaviour on a dead signal
    d2 = VorDemod(DspParams(fs=FS, offset_hz=-FS // 4))
    q2 = QualityEstimator(cfg.quality if cfg else QualityParams())
    tr2 = BearingTracker(cal, history=5)
    noise = (np.random.default_rng(0).normal(size=FS * 4)
             + 1j * np.random.default_rng(1).normal(size=FS * 4)).astype(np.complex64)
    flagged = []
    for x in d2.push(noise):
        flagged.append(q2.update(x, tr2.update(x.delta_phi)).flagged)
    r.add("NAV flag raised on noise only",
          PASS if flagged and all(flagged) else FAIL,
          f"{sum(flagged)}/{len(flagged)} windows flagged")

    # 6 -- publish payload shape
    pol = PublishPolicy(cfg.publish if cfg else
                        {"mode": "periodic", "period_s": 1.0, "deadband_deg": 1,
                         "keepalive_s": 5.0, "hold_s": 3.0,
                         "signal_scale": "percent", "bearing_ref": "from"})
    payload = pol.decide(137, 53.0, 0.30, state="TRACKING", valid=True,
                         status=5, sub_level=0.29, freq_khz=109400).payload
    shape_ok = (set(payload) == {"vorradial", "vorsnr", "voram",
                                 "vorstatus", "vorsub", "vorfreqactive"}
                and all(isinstance(v, int) for v in payload.values()))
    r.add(f"publish payload conforms to {IF_VERSION}",
          PASS if shape_ok else FAIL, str(payload))

    # 7 -- performance
    pct = 100.0 * dsp_s / signal_s * (FS / 1_024_000)
    r.add("throughput", PASS if pct < 25.0 else WARN,
          f"~{pct:.1f}% of one core at 1.024 Msps (port gate 25%)")

    print()
    if r.failed:
        print(f"  {r.failed} FAILURE(S) -- do not proceed to the bench.\n")
        return 1
    warns = sum(1 for _, v, _ in r.rows if v == WARN)
    print(f"  all checks passed{f', {warns} warning(s)' if warns else ''}.")
    print("  The software chain is sound. Any failure to decode a real station")
    print("  from here is RF, antenna, or calibration -- see docs/bench-test.md.\n")
    return 0
