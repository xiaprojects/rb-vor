#!/usr/bin/env python3
"""Measure `bearing.phase_sign` and `bearing.phase_offset_deg`.

Reference: rb-vor-architecture.md section 8; amendments A2, A6, A10; open item
O1. Implementation plan M4.

These two constants are properties of the implementation, not of the station:
which path is A and which is B, and the sign and one-sample delay of the FM
discriminator. They are measured once against the synthetic generator and
recorded in the config. Until then the daemon refuses to publish a bearing (A2).

Method
------
The model is ``radial = sign * delta_phi + offset``. For the correct sign, the
offset solved from two different radials is the *same*; for the wrong sign it
differs (by 180 deg for the 0/90 pair). So:

1. decode a noiseless CVOR at two radials 90 deg apart;
2. for each candidate sign, solve the offset at each radial and measure the
   disagreement;
3. the sign with the consistent offset wins, by a decisive margin;
4. verify on a third, unrelated radial;
5. verify that DVOR decodes identically at the same sign (amendment A6).

**This does not close open item O1.** The generator and the demodulator share
one author's sign conventions and could be consistently wrong together. Only a
capture of a real station with a known radial settles it (T8). What this tool
produces is a self-consistent calibration plus the evidence needed to check it
against reality in one step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.bearing import angular_difference, circular_mean, wrap180  # noqa: E402
from rbvor.dsp import DspParams, VorDemod  # noqa: E402
from tools.vor_gen import VorParams, generate_blocks  # noqa: E402


def decode_delta_phi(radial: float, vor_type: str = "cvor", fs: int = 256_000,
                     seconds: float = 6.0, snr_db: float | None = None,
                     skip: int = 2, deviation_hz: float = 480.0,
                     fm_estimator: str = "discriminator") -> float:
    """Mean phase difference in degrees for a commanded radial."""
    p = VorParams(radial_deg=radial, vor_type=vor_type, fs=fs, seconds=seconds,
                  offset_hz=-fs // 4, snr_db=snr_db, ident="",
                  deviation_hz=deviation_hz, block=1 << 16)
    # the harmonic estimator (D7) needs a few windows to acquire the subcarrier
    # offset; give it the same warm-up the discriminator's filters get
    skip = max(skip, 4) if fm_estimator == "harmonic" else skip
    d = VorDemod(DspParams(fs=fs, offset_hz=-fs // 4, fm_estimator=fm_estimator))
    obs = []
    for blk in generate_blocks(p):
        obs += d.push(blk)
    used = [o for o in obs[skip:] if not o.overrun]
    if len(used) < 2:
        raise SystemExit("capture too short to calibrate")
    return circular_mean([np.rad2deg(o.delta_phi) % 360.0 for o in used])


def solve(fs: int, seconds: float, verbose: bool = True,
          fm_estimator: str = "discriminator") -> dict:
    r1, r2, r3 = 0.0, 90.0, 213.0
    dec = {"fs": fs, "seconds": seconds, "fm_estimator": fm_estimator}
    d1 = decode_delta_phi(r1, **dec)
    d2 = decode_delta_phi(r2, **dec)

    candidates = {}
    for sign in (1, -1):
        o1 = (r1 - sign * d1) % 360.0
        o2 = (r2 - sign * d2) % 360.0
        candidates[sign] = {
            "offsets": (o1, o2),
            "disagreement": angular_difference(o1, o2),
            "offset": circular_mean([o1, o2]),
        }
    best = min(candidates, key=lambda s: candidates[s]["disagreement"])
    worst = -best
    margin = candidates[worst]["disagreement"] - candidates[best]["disagreement"]

    sign = best
    offset = wrap180(candidates[best]["offset"])

    # verification on an unrelated radial
    d3 = decode_delta_phi(r3, **dec)
    got3 = (sign * d3 + offset) % 360.0
    err3 = angular_difference(got3, r3)

    # amendment A6: DVOR must decode identically at the SAME sign
    d_dvor = decode_delta_phi(r3, vor_type="dvor", **dec)
    got_dvor = (sign * d_dvor + offset) % 360.0
    err_dvor = angular_difference(got_dvor, r3)

    # and the deliberately-wrong DVOR must not
    d_rev = decode_delta_phi(r3, vor_type="dvor-reversed", **dec)
    got_rev = (sign * d_rev + offset) % 360.0
    err_rev = angular_difference(got_rev, r3)

    result = {
        "phase_sign": int(sign),
        "phase_offset_deg": round(float(offset), 4),
        "sign_margin_deg": round(float(margin), 3),
        "verify_radial_deg": r3,
        "verify_error_deg": round(float(err3), 4),
        "dvor_error_deg": round(float(err_dvor), 4),
        "dvor_reversed_error_deg": round(float(err_rev), 4),
        "fs": fs,
        "seconds": seconds,
        "fm_estimator": fm_estimator,
    }

    if verbose:
        print("candidate signs")
        for s, c in candidates.items():
            mark = "  <-- chosen" if s == best else ""
            print(f"  sign {s:+d}: offsets {c['offsets'][0]:8.3f} / "
                  f"{c['offsets'][1]:8.3f}  disagreement {c['disagreement']:7.3f}{mark}")
        print(f"  decision margin {margin:.3f} deg")
        print()
        print(f"phase_sign        {sign:+d}")
        print(f"phase_offset_deg  {offset:+.4f}   (dsp.fm_estimator: {fm_estimator})")
        print()
        print(f"verify  CVOR   radial {r3:.0f} -> {got3:8.3f}  error {err3:.4f} deg")
        print(f"        DVOR   radial {r3:.0f} -> {got_dvor:8.3f}  error {err_dvor:.4f} "
              "deg   (A6: must match, same sign)")
        print(f"        DVOR-R radial {r3:.0f} -> {got_rev:8.3f}  error {err_rev:.4f} "
              "deg   (must be large: proves the test has teeth)")
        print()
        print("NOTE: this is a self-consistency calibration. The generator and the")
        print("      demodulator share sign conventions and could be wrong together.")
        print("      Open item O1 stays open until a real station confirms it (T8).")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fs", type=int, default=256_000)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--json", type=Path, default=None,
                    help="write a config fragment for bearing.*")
    ap.add_argument("--fm-estimator", choices=("discriminator", "harmonic"),
                    default="discriminator",
                    help="must match dsp.fm_estimator in the config: the two "
                         "estimators have different constants (D7)")
    a = ap.parse_args(argv)

    r = solve(a.fs, a.seconds, fm_estimator=a.fm_estimator)

    if r["sign_margin_deg"] < 90.0:
        print("\nFAIL: sign decision is not decisive", file=sys.stderr)
        return 1
    if r["verify_error_deg"] > 0.5 or r["dvor_error_deg"] > 0.5:
        print("\nFAIL: verification error too large", file=sys.stderr)
        return 1

    if a.json:
        frag = {"bearing": {"phase_sign": r["phase_sign"],
                            "phase_offset_deg": r["phase_offset_deg"],
                            "station_cal_deg": 0.0}}
        a.json.write_text(json.dumps(frag, indent=2) + "\n")
        print(f"\nwrote {a.json}")
        print("  station_cal_deg stays 0 until measured on a real station (T8)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
