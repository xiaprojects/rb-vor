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
#: Constants `tools/vor_calibrate.py` derives for the synthetic generator, per
#: estimator (docs/calibration.md). The synthetic stages below are judged
#: against these, never against the config's constant: a flight calibration
#: includes the station and installation offset (T8), which the generator
#: does not have. The difference is reported, not failed.
SYNTHETIC_CAL = {"discriminator": -1.9663, "harmonic": -0.6077}

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
    from rbvor import dsp as dsp_mod
    from rbvor.bearing import BearingCal, BearingTracker, angular_difference
    from rbvor.dsp import DspParams, VorDemod
    from rbvor.freqrules import classify, is_loc_frequency, is_vor_frequency
    from rbvor.publisher import PublishPolicy
    from rbvor.quality import QualityEstimator, QualityParams
    from tools import vor_gen as gen_mod
    from tools.vor_gen import VorParams, generate_blocks

    print(f"\nRB-VOR {__version__} self test  ({IF_VERSION})\n")
    r = _Report()

    # 0 -- is this one tree, or two? A file-by-file deployment (the sftp
    # upload-on-save workflow) can leave the generator and the demodulator
    # from different versions. They must agree on where the carrier sits
    # (D5); if they do not, every synthetic stage below fails for a reason
    # that has nothing to do with the software being tested.
    gen_conv = getattr(gen_mod, "CARRIER_CONVENTION", "<pre-D5>")
    dsp_conv = getattr(dsp_mod, "CARRIER_CONVENTION", "<pre-D5>")
    r.add("generator and demodulator agree (D5)",
          PASS if gen_conv == dsp_conv else FAIL,
          f"vor_gen {gen_conv}, dsp {dsp_conv}" if gen_conv == dsp_conv else
          f"vor_gen {gen_conv} vs dsp {dsp_conv} -- MIXED VERSIONS on disk: "
          "sync the whole tree, not single files")

    # 1 -- channel rules. 109.5 is a localizer channel either way; whether the
    # daemon decodes it or rejects it follows loc.enabled (A33), reported once
    # the config is read, below.
    ok_vor = is_vor_frequency(109.4)[0]
    ok_loc = not is_vor_frequency(109.5)[0] and is_loc_frequency(109.5)[0]
    ok_band = str(classify(118.0)) == "OUT_OF_BAND"
    r.add("channel rules (109.4 / 109.5 / 118.0)",
          PASS if (ok_vor and ok_loc and ok_band) else FAIL,
          "109.4 VOR, 109.5 localizer, 118.0 out of band")

    # 2 -- configuration
    #
    # The self-test exists to prove the SOFTWARE works, independently of any
    # config. Everything below falls back to built-in defaults when `cfg is
    # None`, so a missing or unreadable config is a WARN, not a FAIL: on a fresh
    # checkout the config lives in /etc after install, and running the self-test
    # from the source tree is exactly the case where it is absent. A genuinely
    # malformed config that the schema rejects is still a FAIL -- that is a real
    # defect a builder must fix before the bench.
    cfg = None
    estimator = "discriminator"          # D7: the config may select "harmonic"
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
            elif "Additional properties are not allowed" in msg:
                r.add("configuration loads and validates", FAIL,
                      msg[:60] + " -- the config is newer than schema/"
                      "rb-vor.schema.json: MIXED VERSIONS on disk, sync the whole tree")
            else:
                r.add("configuration loads and validates", FAIL, msg[:80])
        if cfg is not None:
            r.add("configuration loads and validates", PASS, str(cfg.source))
            estimator = cfg.dsp.fm_estimator
            if cfg.calibrated:
                station = cfg.bearing.phase_offset_deg - SYNTHETIC_CAL[estimator]
                r.add("bearing calibration present", PASS,
                      f"sign {cfg.bearing.phase_sign:+d}, "
                      f"offset {cfg.bearing.phase_offset_deg:+.4f} deg "
                      f"(fm_estimator {estimator}; {station:+.2f} deg beyond "
                      f"the synthetic constant = station/installation offset)")
                if cfg.bearing.phase_sign != 1:
                    r.add("calibration sign", WARN,
                          "phase_sign is -1: the synthetic chain calibrates to +1, "
                          "so a real station confirmed the opposite -- see O1")
            else:
                r.add("bearing calibration present", WARN,
                      "not set -- the daemon will publish only flags (A2). "
                      "Run tools/vor_calibrate.py")
    except Exception as e:                           # noqa: BLE001
        r.add("configuration", FAIL, str(e)[:80])
    loc_on = bool(cfg is not None and cfg.loc.enabled)
    r.add("localizer channels (A33)", PASS,
          "109.5 will be DECODED (loc.enabled true; supplemental, not approach "
          "guidance)" if loc_on else
          "109.5 will be rejected as INVALID_FREQ (loc.enabled false, the shipped default)")

    # 3 -- synthetic signal, full chain
    t0 = time.perf_counter()
    p = VorParams(radial_deg=RADIAL, fs=FS, seconds=12.0, offset_hz=-FS // 4,
                  snr_db=25.0, ident="TST", block=1 << 16)
    blocks = list(generate_blocks(p))
    gen_s = time.perf_counter() - t0
    r.add("synthetic signal generated", PASS,
          f"{RADIAL:.0f} deg radial, 25 dB SNR, {gen_s:.2f} s")

    d = VorDemod(DspParams(fs=FS, offset_hz=-FS // 4, fm_estimator=estimator))
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

    # 4 -- bearing, against the synthetic constant for this estimator
    cal = BearingCal(phase_sign=1, phase_offset_deg=SYNTHETIC_CAL[estimator])
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
    d2 = VorDemod(DspParams(fs=FS, offset_hz=-FS // 4, fm_estimator=estimator))
    q2 = QualityEstimator(cfg.quality if cfg else QualityParams())
    tr2 = BearingTracker(cal, history=5)
    noise = (np.random.default_rng(0).normal(size=FS * 4)
             + 1j * np.random.default_rng(1).normal(size=FS * 4)).astype(np.complex64)
    flagged = []
    for x in d2.push(noise):
        tr2.update(x.delta_phi)
        flagged.append(q2.update(x, tr2.raw).flagged)      # raw, per A15
    r.add("NAV flag raised on noise only",
          PASS if flagged and all(flagged) else FAIL,
          f"{sum(flagged)}/{len(flagged)} windows flagged")

    # 6 -- publish payload shape
    pol = PublishPolicy(cfg.publish if cfg else
                        {"keepalive_s": 5.0, "hold_s": 3.0, "bearing_ref": "from"})
    payload = pol.decide(137, 53.0, 0.30, state="TRACKING", valid=True,
                         status=5, sub_level=0.29, freq_khz=109_400).payload
    # v2.3 adds the loc* keys only with loc.enabled; without it the payload
    # is exactly the v2.2 six
    shape_ok = (set(payload) == {"vorradial", "vorsnr", "voram",
                                 "vorstatus", "vorsub", "vorfreqactive"}
                and all(isinstance(v, int) for v in payload.values())
                and payload["vorstatus"] == 5 and payload["vorfreqactive"] == 109_400)
    from rbvor.publisher import LOC_KEYS, LocWire
    pol2 = PublishPolicy({"keepalive_s": 0, "hold_s": 3.0, "bearing_ref": "from"})
    p23 = pol2.decide(None, state="TRACKING", status=0,
                      loc=LocWire(status=5, ddm=0.0775, sdm=0.40, snr_db=40.0,
                                  freq_khz=109_500, usable=True)).payload
    shape_ok = shape_ok and (set(p23) == set(payload) | set(LOC_KEYS)
                             and p23["locdev"] == 50 and p23["vorstatus"] == 0)
    r.add(f"publish payload conforms to {IF_VERSION}",
          PASS if shape_ok else FAIL,
          f"{payload}; with loc.enabled +{len(LOC_KEYS)} loc* keys")

    # 6b -- localizer chain (A33), run whether or not it is enabled: it proves
    # the software, like every other stage here.
    from rbvor.loc import LocDemod, LocDspParams, LocQuality
    from tools.loc_gen import LocParams
    from tools.loc_gen import generate_blocks as loc_blocks
    lp = LocParams(ddm=0.0775, tone_offset=0.02, fs=FS, offset_hz=-FS // 4,
                   seconds=6.0, snr_db=25.0, block=1 << 16)
    ld = LocDemod(LocDspParams(fs=FS, offset_hz=-FS // 4))
    lobs = [x for b in loc_blocks(lp) for x in ld.push(b)][3:]
    lq = LocQuality()
    lres = [lq.update(x, x.ddm_corrected) for x in lobs]
    lerr = max((abs(x.ddm_corrected - 0.0775) for x in lobs), default=1.0)
    r.add("localizer DDM (half scale, tones +2 %)",
          PASS if lobs and lerr < 0.01 and not lres[-1].flagged else FAIL,
          f"max error {lerr:.4f} DDM over {len(lobs)} windows (expected < 0.01)")
    ld2 = LocDemod(LocDspParams(fs=FS, offset_hz=-FS // 4))
    lq2 = LocQuality()
    lnoise = [lq2.update(x, x.ddm_corrected) for x in ld2.push(noise)]
    r.add("localizer invalid on noise only (plan 4.1)",
          PASS if lnoise and all(v.flagged for v in lnoise) else FAIL,
          f"{sum(v.flagged for v in lnoise)}/{len(lnoise)} windows flagged")

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
