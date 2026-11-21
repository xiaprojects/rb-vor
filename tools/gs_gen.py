#!/usr/bin/env python3
"""Synthetic ILS glide-slope (glide path) signal generator.

Reference: docs/gs-plan.md G2; CLAUDE.md A39. A glide path carries exactly
the localizer's modulation -- 90 Hz and 150 Hz AM -- so this is
`tools/loc_gen.py` with the glide path's numbers, not a second generator.
Same public shape (`GsParams`, `generate_blocks`) for roastbeef's stand-in.

What differs from a localizer (Annex 10 Vol I as amended to No. 74):

- 40 % per tone on the path, SDM 0.80, 37.5-42.5 % per tone (3.1.5.5.1);
- **150 Hz predominates below the path** (3.1.5.1.4): a positive `ddm` is an
  aircraft below the path, which must fly up;
- no ident (3.1.5 has none); horizontal polarisation is irrelevant here;
- carrier tolerance 0.005 % (3.1.5.2.1): `drift_hz` up to 16.75 kHz at
  335 MHz; two-frequency glide paths have carriers 4-32 kHz apart
  (`clearance_sep_hz`; the separation is from memory, to verify -- O31).

The carrier is placed at ``-offset_hz`` in baseband (A22), as for VOR and LOC.

Examples
--------
    ./gs_gen.py --ddm 0.0875 --snr-db 20 --seconds 10 -o gs_halfscale.cu8
    ./gs_gen.py --ddm -0.05 --drift-hz 16750 --nav-freq 335.0
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.record import IQMeta, IQWriter  # noqa: E402
from tools.loc_gen import LocParams, ddm_at, generate_blocks  # noqa: E402
from tools.vor_gen import CARRIER_CONVENTION, CHANNEL_BW_HZ, to_disk  # noqa: E402

#: Annex 10 Vol I 3.1.5.5.1
SDM_NOMINAL = 0.80
#: Airborne full-scale convention, 2 x 0.0875 (see rbvor/gs.py)
DDM_FULL_SCALE = 0.175

__all__ = ["GsParams", "generate_blocks", "write_file", "ddm_at", "SDM_NOMINAL",
           "DDM_FULL_SCALE", "CARRIER_CONVENTION"]


@dataclass
class GsParams(LocParams):
    """`LocParams` with glide-path defaults. `ddm` + = 150 Hz = below = fly up."""

    sdm: float = SDM_NOMINAL
    ident: str = ""
    ident_depth: float = 0.0
    clearance_sdm: float = SDM_NOMINAL


def write_file(p: GsParams, path: Path, fmt: str = "cu8",
               nav_freq_hz: int = 332_600_000) -> Path:
    """Generate and write an IQ file plus sidecar. Returns the IQ path.
    `nav_freq_hz` is the glide-path RF channel (332.6 MHz pairs with 109.5)."""
    m90, m150 = p.depths()
    meta = IQMeta(
        format=fmt, sample_rate_hz=p.fs,
        center_freq_hz=nav_freq_hz + int(p.offset_hz), nav_freq_hz=nav_freq_hz,
        tuner_offset_hz=int(p.offset_hz), gain_mode="synthetic", driver="gs_gen",
        device="synthetic", station="", notes=f"synthetic glide slope, DDM {p.ddm:+.4f}",
        synthetic={
            "navaid": "gs",
            "ddm": p.ddm, "ddm_rate": p.ddm_rate, "sdm": p.sdm, "m90": m90, "m150": m150,
            "tone_offset": p.tone_offset, "phase150_deg": p.phase150_deg,
            "swap_tones": p.swap_tones, "snr_db": p.snr_db,
            "clearance_sep_hz": p.clearance_sep_hz, "clearance_db": p.clearance_db,
            "clearance_ddm": p.clearance_ddm, "clearance_sdm": p.clearance_sdm,
            "drift_hz": p.drift_hz, "fade": p.fade, "level": p.level, "seed": p.seed,
            "snr_bandwidth_hz": CHANNEL_BW_HZ, "carrier_at": CARRIER_CONVENTION,
        },
    )
    with IQWriter(path, meta) as w:
        for z in generate_blocks(p):
            raw, _ = to_disk(z, fmt)
            w.write(raw)
    return Path(str(path)).with_suffix("." + fmt)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ddm", type=float, default=0.0, help="+ = 150 Hz = below = fly up")
    ap.add_argument("--sdm", type=float, default=SDM_NOMINAL)
    ap.add_argument("--tone-offset", type=float, default=0.0)
    ap.add_argument("--drift-hz", type=float, default=0.0)
    ap.add_argument("--snr-db", type=float, default=20.0)
    ap.add_argument("--clearance-sep", type=float, default=0.0)
    ap.add_argument("--clearance-db", type=float, default=-10.0)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--fs", type=int, default=1_024_000)
    ap.add_argument("--nav-freq", type=float, default=332.6, help="glide-path MHz")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--format", default="cu8")
    ap.add_argument("-o", "--output", type=Path, default=Path("gs_synthetic.cu8"))
    a = ap.parse_args(argv)
    p = GsParams(ddm=a.ddm, sdm=a.sdm, tone_offset=a.tone_offset, drift_hz=a.drift_hz,
                 snr_db=a.snr_db, clearance_sep_hz=a.clearance_sep,
                 clearance_db=a.clearance_db, seconds=a.seconds, fs=a.fs, seed=a.seed)
    out = write_file(p, a.output, a.format, int(round(a.nav_freq * 1e6)))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
