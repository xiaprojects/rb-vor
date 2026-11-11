#!/usr/bin/env python3
"""Synthetic ILS localizer signal generator.

Reference: docs/loc-plan.md section 6 (L2); CLAUDE.md A33-A36. The localizer
twin of `tools/vor_gen.py`, and the ground truth `rbvor/loc.py` is measured
against. Same public shape as vor_gen -- a params dataclass (`LocParams`) and
`generate_blocks(params)` yielding complex64 blocks -- because roastbeef's
integration stand-in (`test/integration/dongle.py`) radiates from that shape.

Signal model
------------
::

    m(t) = 1 + m90  * cos(2*pi*90*(1+eps)*t)
             + m150 * cos(2*pi*150*(1+eps)*t + phi_150)
             + m_id * key(t) * cos(2*pi*1020*t)

    m150 = (sdm + ddm) / 2         m90 = (sdm - ddm) / 2

    z(t) = level * m(t) * exp(-j*2*pi*f_offset*t) + n(t)

placed where a tuner parked at ``nav + f_offset`` puts the navaid, i.e. at
``-f_offset`` in baseband (amendment A22, `CARRIER_CONVENTION`).

Conventions, from Annex 10 Vol I (checked against the Amendment 74 text,
paragraphs as numbered there):

- DDM = depth of the larger tone minus depth of the smaller, /100 (3.1.1);
  here **signed, positive when 150 Hz predominates**. Facing the localizer
  from the approach end, 150 Hz predominates on the right (3.1.3.1.2): a
  positive DDM is an aircraft right of the front course, which must fly left.
- 20 % per tone on the course line, 18-22 % allowed (3.1.3.5.1-2); sum of
  depths 30-60 % in coverage for equipment installed after 2000 (3.1.3.5.3.6.1).
- tones 90 / 150 Hz within +/-2.5 % (Cat I), 1.5 % (II), 1 % (III)
  (3.1.3.5.3); the two tones are phase-locked, which is why ONE fractional
  offset `tone_offset` moves both (3.1.3.5.3.3: zero crossings within 20 deg
  of the 150 Hz for Cat I/II -- `phase150_deg` models that).
- ident 1020 +/- 50 Hz, depth 5-15 % (3.1.3.9.2).
- two-frequency systems: carriers 5-14 kHz apart, symmetrical about the
  assigned frequency (3.1.3.2.1). `clearance_sep_hz` puts the course carrier
  at +sep/2 and a clearance carrier at -sep/2, `clearance_db` below it.

`swap_tones` puts each tone's depth on the other tone. It exists so the
generate-and-decode tests have a case that must fail -- it must decode to the
negated DDM -- without which they would pass on a generator that ignored its
arguments (the A6 method).

Examples
--------
    ./loc_gen.py --ddm 0.0775 --snr-db 20 --seconds 10 -o loc_halfscale.cu8
    ./loc_gen.py --ddm -0.05 --tone-offset 0.025 --nav-freq 109.5
    ./loc_gen.py --ddm 0 --clearance-sep 8000 --clearance-db -10
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.record import IQMeta, IQWriter  # noqa: E402
from tools.vor_gen import (  # noqa: E402
    CARRIER_CONVENTION,
    CHANNEL_BW_HZ,
    IDENT_HZ,
    _autolevel,
    _keying,
    _phase,
    add_channel_noise,
    morse_intervals,
    to_disk,
)

TONE_90_HZ = 90.0
TONE_150_HZ = 150.0
#: Annex 10 Vol I 3.1.3.5.1: 20 % per tone on the course line.
SDM_NOMINAL = 0.40
#: The course sector half-width DDM (Annex 10 Vol I 3.1.1, "course sector").
#: Its use as indicator full-scale deflection is airborne-equipment
#: convention, not stated in Annex 10 Vol I -- unverified here.
DDM_FULL_SCALE = 0.155

__all__ = ["LocParams", "generate_blocks", "write_file", "ddm_at", "TONE_90_HZ",
           "TONE_150_HZ", "SDM_NOMINAL", "DDM_FULL_SCALE", "CARRIER_CONVENTION"]


@dataclass
class LocParams:
    ddm: float = 0.0                 # signed, + = 150 Hz predominates (fly left)
    #: DDM change per second from t = 0 (an intercept, or a needle moving);
    #: the instantaneous DDM is clipped to +/-sdm.
    ddm_rate: float = 0.0
    sdm: float = SDM_NOMINAL         # sum of depths
    tone_offset: float = 0.0         # fractional offset of BOTH tones, +/-0.025 = Cat I limit
    phase150_deg: float = 0.0        # 150 Hz phase relative to the 90 Hz lock
    swap_tones: bool = False         # deliberately wrong: decodes to -ddm
    fs: int = 1_024_000
    seconds: float = 10.0
    offset_hz: int | None = None     # default -fs/4 (amendment A1)
    drift_hz: float = 0.0            # carrier frequency error, + = higher RF
    snr_db: float | None = 20.0      # in CHANNEL_BW_HZ; None = noiseless
    ident: str = "ITST"
    ident_depth: float = 0.10
    ident_dot_s: float = 0.15
    ident_period_s: float = 10.0
    ident_edge_s: float = 0.006
    #: Two-frequency localizer: carrier separation in Hz (0 = single carrier),
    #: clearance level relative to the course carrier, and its own modulation.
    clearance_sep_hz: float = 0.0
    clearance_db: float = -10.0
    clearance_ddm: float = 0.0
    clearance_sdm: float = SDM_NOMINAL
    fade: str = "none"               # none | slow
    level: float | str = "auto"
    seed: int | None = 0
    block: int = 65536
    _intervals: list[tuple[float, float]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        for name, sdm, ddm in (("", self.sdm, self.ddm),
                               ("clearance_", self.clearance_sdm, self.clearance_ddm)):
            if sdm < 0 or abs(ddm) > sdm:
                raise ValueError(f"{name}ddm must lie within +/-{name}sdm "
                                 f"(a tone depth cannot be negative)")
            if sdm + self.ident_depth >= 1.0:
                raise ValueError(f"{name}sdm + ident_depth would over-modulate")
        if abs(self.tone_offset) > 0.1:
            raise ValueError("tone_offset is a fraction; 0.025 is the Cat I limit")
        if self.fade not in ("none", "slow"):
            raise ValueError(f"unknown fade {self.fade!r}")
        if self.offset_hz is None:
            self.offset_hz = -self.fs // 4
        if self.clearance_sep_hz < 0 or self.clearance_sep_hz > 30_000:
            raise ValueError("clearance_sep_hz must be 0..30000 (ICAO: 5-14 kHz)")
        self._intervals = (morse_intervals(self.ident, self.ident_dot_s)
                           if self.ident else [])
        if self.level == "auto":
            self.level = _autolevel(self, gen=generate_blocks)
        if not isinstance(self.level, (int, float)) or self.level <= 0:
            raise ValueError(f"level must be a positive number or 'auto', got {self.level!r}")

    @property
    def n_samples(self) -> int:
        return int(round(self.seconds * self.fs))

    def depths(self, clearance: bool = False) -> tuple[float, float]:
        """(m90, m150) as transmitted, after `swap_tones`."""
        sdm, ddm = ((self.clearance_sdm, self.clearance_ddm) if clearance
                    else (self.sdm, self.ddm))
        m90, m150 = (sdm - ddm) / 2.0, (sdm + ddm) / 2.0
        return (m150, m90) if self.swap_tones else (m90, m150)


def ddm_at(p: LocParams, t):
    """Course-carrier DDM at time(s) t, including `ddm_rate`."""
    return np.clip(p.ddm + p.ddm_rate * np.asarray(t, dtype=np.float64), -p.sdm, p.sdm)


def _modulation(p: LocParams, n0: int, n: int, t: np.ndarray, clearance: bool) -> np.ndarray:
    m90, m150 = p.depths(clearance)
    if p.ddm_rate and not clearance:
        d = ddm_at(p, t)
        m90, m150 = (p.sdm - d) / 2.0, (p.sdm + d) / 2.0
        if p.swap_tones:
            m90, m150 = m150, m90
    f90 = TONE_90_HZ * (1.0 + p.tone_offset)
    f150 = TONE_150_HZ * (1.0 + p.tone_offset)
    m = (1.0 + m90 * np.cos(_phase(f90, n0, n, p.fs))
         + m150 * np.cos(_phase(f150, n0, n, p.fs) + np.deg2rad(p.phase150_deg)))
    if p.ident_depth and p._intervals:
        m = m + p.ident_depth * _keying(t, p) * np.cos(_phase(IDENT_HZ, n0, n, p.fs))
    return m


def generate_blocks(p: LocParams) -> Iterator[np.ndarray]:
    """Yield complex64 blocks of the modelled localizer, with noise."""
    rng = np.random.default_rng(p.seed)
    f_course = -p.offset_hz + p.drift_hz          # A22: the carrier sits at -offset
    two = p.clearance_sep_hz > 0
    if two:
        f_clear = f_course - p.clearance_sep_hz / 2.0
        f_course = f_course + p.clearance_sep_hz / 2.0
        a_clear = 10 ** (p.clearance_db / 20.0)
    total = p.n_samples
    n0 = 0
    while n0 < total:
        n = min(p.block, total - n0)
        t = (n0 + np.arange(n, dtype=np.float64)) / p.fs
        m = _modulation(p, n0, n, t, clearance=False)
        if p.fade == "slow":
            m = m * 10 ** (0.15 * np.sin(2 * np.pi * 0.2 * t))       # +/-3 dB @ 0.2 Hz
        z = m * np.exp(1j * _phase(f_course, n0, n, p.fs))
        if two:
            mc = _modulation(p, n0, n, t, clearance=True)
            z = z + a_clear * mc * np.exp(1j * _phase(f_clear, n0, n, p.fs))
        z = (p.level * z).astype(np.complex128)
        if p.snr_db is not None:
            z = add_channel_noise(z, p.snr_db, p.fs, rng)
        n0 += n
        yield z.astype(np.complex64)


def write_file(p: LocParams, path: Path, fmt: str = "cu8",
               nav_freq_hz: int = 109_500_000) -> Path:
    """Generate and write an IQ file plus sidecar. Returns the IQ path."""
    m90, m150 = p.depths()
    meta = IQMeta(
        format=fmt,
        sample_rate_hz=p.fs,
        center_freq_hz=nav_freq_hz + int(p.offset_hz),
        nav_freq_hz=nav_freq_hz,
        tuner_offset_hz=int(p.offset_hz),
        gain_mode="synthetic",
        driver="loc_gen",
        device="synthetic",
        station=p.ident,
        notes=f"synthetic localizer, DDM {p.ddm:+.4f}",
        synthetic={
            "navaid": "loc",
            "ddm": p.ddm, "ddm_rate": p.ddm_rate, "sdm": p.sdm, "m90": m90, "m150": m150,
            "tone_offset": p.tone_offset, "phase150_deg": p.phase150_deg,
            "swap_tones": p.swap_tones, "snr_db": p.snr_db,
            "ident": p.ident, "ident_depth": p.ident_depth if p.ident else 0.0,
            "clearance_sep_hz": p.clearance_sep_hz, "clearance_db": p.clearance_db,
            "clearance_ddm": p.clearance_ddm, "clearance_sdm": p.clearance_sdm,
            "drift_hz": p.drift_hz, "fade": p.fade, "level": p.level, "seed": p.seed,
            "snr_bandwidth_hz": CHANNEL_BW_HZ,
            "carrier_at": CARRIER_CONVENTION,
        },
    )
    clipped = 0
    with IQWriter(path, meta) as w:
        for z in generate_blocks(p):
            raw, c = to_disk(z, fmt)
            clipped += c
            w.write(raw)
    frac = clipped / (2.0 * p.n_samples)
    if frac > 1e-4:
        print(f"  warning: {clipped} samples clipped ({frac:.3%}) -- lower --level",
              file=sys.stderr)
    return Path(str(path)).with_suffix("." + fmt)


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ddm", type=float, default=0.0,
                    help="signed DDM, + = 150 Hz predominates (right of course, fly left)")
    ap.add_argument("--ddm-rate", type=float, default=0.0, help="DDM per second")
    ap.add_argument("--sdm", type=float, default=SDM_NOMINAL)
    ap.add_argument("--tone-offset", type=float, default=0.0,
                    help="fractional tone frequency offset, 0.025 = Cat I limit")
    ap.add_argument("--phase150", type=float, default=0.0, help="degrees")
    ap.add_argument("--swap-tones", action="store_true",
                    help="deliberately wrong: decodes to the negated DDM")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--fs", type=int, default=1_024_000)
    ap.add_argument("--offset", type=int, default=None, help="Hz, default -fs/4")
    ap.add_argument("--drift-hz", type=float, default=0.0)
    ap.add_argument("--snr-db", type=float, default=20.0)
    ap.add_argument("--noiseless", action="store_true")
    ap.add_argument("--ident", default="ITST")
    ap.add_argument("--no-ident", action="store_true")
    ap.add_argument("--ident-depth", type=float, default=0.10)
    ap.add_argument("--clearance-sep", type=float, default=0.0,
                    help="two-frequency localizer: carrier separation, Hz (ICAO 5000-14000)")
    ap.add_argument("--clearance-db", type=float, default=-10.0)
    ap.add_argument("--clearance-ddm", type=float, default=0.0)
    ap.add_argument("--fade", default="none", choices=["none", "slow"])
    ap.add_argument("--level", default="auto")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--format", default="cu8", choices=["cu8", "cs8", "cs16", "cf32"])
    ap.add_argument("--nav-freq", type=float, default=109.5, help="MHz, sidecar only")
    ap.add_argument("-o", "--out", type=Path, default=None)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    p = LocParams(
        ddm=a.ddm, ddm_rate=a.ddm_rate, sdm=a.sdm, tone_offset=a.tone_offset,
        phase150_deg=a.phase150,
        swap_tones=a.swap_tones, fs=a.fs, seconds=a.seconds, offset_hz=a.offset,
        drift_hz=a.drift_hz, snr_db=None if a.noiseless else a.snr_db,
        ident="" if a.no_ident else a.ident, ident_depth=a.ident_depth,
        clearance_sep_hz=a.clearance_sep, clearance_db=a.clearance_db,
        clearance_ddm=a.clearance_ddm, fade=a.fade, seed=a.seed,
        level="auto" if a.level == "auto" else float(a.level),
    )
    out = a.out or Path(f"synth_loc_{a.ddm:+.4f}".replace(".", "p"))
    path = write_file(p, out, fmt=a.format, nav_freq_hz=int(round(a.nav_freq * 1e6)))
    print(f"wrote {path}  ({p.n_samples} samples, {p.seconds:g} s, {a.format})")
    print(f"  DDM {p.ddm:+.4f} ({100 * p.ddm / DDM_FULL_SCALE:+.0f} % of full scale), "
          f"SDM {p.sdm:.2f}, tones {100 * p.tone_offset:+.2f} %")
    print(f"  SNR {'none' if a.noiseless else f'{a.snr_db:g} dB'} in "
          f"{CHANNEL_BW_HZ/1e3:g} kHz, offset {p.offset_hz/1e3:+g} kHz, level {p.level:.4f}")
    print(f"  sidecar {path.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
