#!/usr/bin/env python3
"""Accuracy and quality sweep: bearing error vs radial and SNR.

Reference: implementation plan M4 (T2, T3); open item O10.

Produces the evidence for three things:
  * T2/T3 acceptance -- error against radial at a range of SNRs;
  * the `vorsignal` curve -- does the published quality figure actually track
    the error it is supposed to predict?
  * the O10 weight derivation -- which of the four quality terms carry
    information, measured rather than assumed.

Outputs Markdown and CSV. Numbers, not pictures: a table can be diffed between
two runs, a plot cannot.

Examples
--------
    ./vor_sweep.py --step 15 --snr 40 20 10 6
    ./vor_sweep.py --step 1 --snr 30 --md docs/sweep.md --csv docs/sweep.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.bearing import BearingCal, BearingTracker, angular_difference  # noqa: E402
from rbvor.dsp import DspParams, VorDemod  # noqa: E402
from rbvor.quality import QualityEstimator, QualityParams  # noqa: E402
from tools.vor_gen import VorParams, generate_blocks  # noqa: E402


def run_one(radial, snr_db, cal, qp, fs=256_000, seconds=10.0, skip=2,
            vor_type="cvor", seed=0, ident="", warm=4):
    """One sweep point.

    Warm-up is handled in two stages and both matter:
      * `skip` discards the DSP filter transient;
      * `warm` then discards the results produced while the bearing tracker's
        smoothing deque and the quality estimator's history are still filling.
    Feeding the tracker with transient windows and then averaging the result was
    the bug that produced a 5 deg RMS error at 40 dB SNR in the first sweep.
    """
    p = VorParams(radial_deg=radial, vor_type=vor_type, fs=fs, seconds=seconds,
                  offset_hz=-fs // 4, snr_db=snr_db, ident=ident,
                  seed=seed, block=1 << 16)
    d = VorDemod(DspParams(fs=fs, offset_hz=-fs // 4))
    all_obs = []
    for blk in generate_blocks(p):
        all_obs += d.push(blk)
    usable = [o for o in all_obs[skip:] if not o.overrun]

    tracker = BearingTracker(cal, history=qp.history)
    q = QualityEstimator(qp)
    rows = []
    for o in usable:
        b = tracker.update(o.delta_phi)
        rows.append((o, b, q.update(o, b)))
    rows = rows[warm:]
    if not rows:
        return None
    errs = [angular_difference(b, radial) for _, b, _ in rows]
    return {
        "radial": radial, "snr_db": snr_db,
        "err_mean": float(np.mean(errs)), "err_max": float(np.max(errs)),
        "q_am": float(np.mean([r.q_am for _, _, r in rows])),
        "q_sub": float(np.mean([r.q_sub for _, _, r in rows])),
        "q_snr": float(np.mean([r.q_snr for _, _, r in rows])),
        "q_stab": float(np.mean([r.q_stab for _, _, r in rows])),
        "vorsignal": float(np.mean([r.value for _, _, r in rows])),
        "flagged": float(np.mean([r.flagged for _, _, r in rows])),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--step", type=int, default=15, help="radial step, degrees")
    ap.add_argument("--snr", type=float, nargs="+",
                    default=[40, 30, 20, 15, 10, 6, 3])
    ap.add_argument("--fs", type=int, default=256_000)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--sign", type=int, default=None)
    ap.add_argument("--offset", type=float, default=None)
    ap.add_argument("--md", type=Path, default=None)
    ap.add_argument("--csv", type=Path, default=None)
    a = ap.parse_args(argv)

    if a.sign is None or a.offset is None:
        from tools.vor_calibrate import solve
        print("no calibration supplied; measuring it first\n")
        cal_result = solve(a.fs, a.seconds, verbose=False)
        sign = a.sign if a.sign is not None else cal_result["phase_sign"]
        offset = a.offset if a.offset is not None else cal_result["phase_offset_deg"]
        print(f"using phase_sign {sign:+d}, phase_offset_deg {offset:+.4f}\n")
    else:
        sign, offset = a.sign, a.offset

    cal = BearingCal(phase_sign=sign, phase_offset_deg=offset)
    qp = QualityParams()
    radials = list(range(0, 360, a.step))

    rows = []
    total = len(radials) * len(a.snr)
    for i, snr in enumerate(a.snr):
        for j, r in enumerate(radials):
            res = run_one(r, snr, cal, qp, fs=a.fs, seconds=a.seconds)
            if res:
                rows.append(res)
            n = i * len(radials) + j + 1
            print(f"\r  sweeping {n}/{total}", end="", flush=True)
    print()

    if a.csv:
        a.csv.parent.mkdir(parents=True, exist_ok=True)
        with a.csv.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    lines = []
    lines.append("# RB-VOR accuracy sweep\n")
    lines.append(f"{len(radials)} radials (step {a.step} deg) x {len(a.snr)} SNRs, "
                 f"fs {a.fs/1000:g} kHz, {a.seconds:g} s per point.\n")
    lines.append(f"Calibration: `phase_sign` {sign:+d}, "
                 f"`phase_offset_deg` {offset:+.4f}.\n")
    lines.append("| SNR dB | RMS err | max err | vorsignal | q_am | q_sub | q_snr "
                 "| q_stab | flagged |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for snr in a.snr:
        g = [r for r in rows if r["snr_db"] == snr]
        if not g:
            continue
        rms = float(np.sqrt(np.mean([r["err_mean"] ** 2 for r in g])))
        lines.append(
            f"| {snr:g} | {rms:.3f} | {max(r['err_max'] for r in g):.3f} | "
            f"{np.mean([r['vorsignal'] for r in g]):.0f} | "
            f"{np.mean([r['q_am'] for r in g]):.3f} | "
            f"{np.mean([r['q_sub'] for r in g]):.3f} | "
            f"{np.mean([r['q_snr'] for r in g]):.3f} | "
            f"{np.mean([r['q_stab'] for r in g]):.3f} | "
            f"{100*np.mean([r['flagged'] for r in g]):.0f}% |")

    # O10: which terms actually predict the error?
    lines.append("\n## Open item O10 — do the quality terms predict the error?\n")
    err = np.array([r["err_mean"] for r in rows])
    lerr = np.log10(np.maximum(err, 1e-3))
    lines.append("Spearman rank correlation of each term against bearing error "
                 "(negative is good: higher quality, lower error).\n")
    lines.append("| term | rank corr vs error | spread across the sweep |")
    lines.append("|---|---|---|")
    from scipy import stats
    for k in ("q_am", "q_sub", "q_snr", "q_stab", "vorsignal"):
        v = np.array([r[k] for r in rows])
        rho = stats.spearmanr(v, lerr).statistic if v.std() > 0 else float("nan")
        lines.append(f"| `{k}` | {rho:+.3f} | {v.min():.3f} – {v.max():.3f} |")

    text = "\n".join(lines) + "\n"
    print()
    print(text)
    if a.md:
        a.md.parent.mkdir(parents=True, exist_ok=True)
        a.md.write_text(text)
        print(f"wrote {a.md}")
    if a.csv:
        print(f"wrote {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
