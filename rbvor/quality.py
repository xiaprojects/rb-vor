"""Signal quality: the internal `vorsignal` figure and the NAV flag.

Reference: rb-vor-architecture.md section 9; amendments A7, A8, A11 (which
closed open item O10). Implementation plan M4.

`vorsignal` exists for one reason: failure mode F1, a wrong bearing published as
a valid one. Every design choice here is biased towards being pessimistic, since
an over-optimistic quality figure is the mechanism by which F1 reaches the pilot.

Since RB-VOR-IF v2.0 (CLAUDE.md A20) the 0-100 score is no longer on the wire;
the bridge receives the measured `vorsnr` / `voram` / `vorsub` instead. The
score still decides validity (0 = NAV flag, the bearing trio goes to zero), the
TRACKING / DEGRADED split (A13) and the solution log.

Scale (internal semantics, doc A section 9)::

    0        NAV flag -- `vordir` is invalid and must be ignored
    1-39     degraded -- situational awareness only
    40-69    acceptable
    70-100   good

Four terms
----------
``q_am``    30 Hz AM depth against the 0.30 nominal
``q_sub``   9960 Hz subcarrier depth, measured as BAND power (A7), against 0.30
``q_snr``   30 Hz tone against the off-tone bins, mapped over `snr_lo/hi_db`
``q_stab``  1 - (circular std-dev of recent bearings / `stab_full_deg`)

Amendment A8 established that the first three are correlated, not independent:
an envelope detector's DC output rises with noise, so measured depth falls as
SNR falls even at constant transmitted depth. The weights, the SNR mapping and
the per-term flag thresholds were therefore re-derived from the
`tools/vor_sweep.py` curve rather than asserted (A11, `docs/sweep.md`).
Every term is reported individually as well as combined -- a single scalar is
useless for diagnosing an installation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

__all__ = ["QualityParams", "QualityResult", "QualityEstimator",
           "GOOD", "ACCEPTABLE", "DEGRADED"]

GOOD = 70
ACCEPTABLE = 40
DEGRADED = 1


def _clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


@dataclass(frozen=True)
class QualityParams:
    #: Derived from the M4 sweep, not assumed -- see amendment A11 and O10.
    #: `q_stab` carries the most information about the actual bearing error;
    #: `q_sub` carries the least (its range is compressed) and serves mainly as
    #: a subcarrier-presence check.
    weights: dict[str, float] = field(
        default_factory=lambda: {"am": 0.25, "sub": 0.10, "snr": 0.25, "stab": 0.40}
    )
    am_nominal_depth: float = 0.30
    sub_nominal_depth: float = 0.30
    #: Doc A section 9 specified 0-20 dB. That is a CHANNEL SNR; what the DSP
    #: measures is the post-integration 30 Hz tone against the off-tone bins,
    #: which carries roughly 28 dB of processing gain from the 1 s window and
    #: spans 17-68 dB in practice. The original mapping saturated at 1.0 across
    #: the entire useful range, contributing a constant to every score and
    #: inflating `vorsignal` exactly where it mattered. Amendment A11.
    snr_lo_db: float = 26.0
    snr_hi_db: float = 46.0
    stab_full_deg: float = 10.0
    #: NAV flag thresholds, per term. Set from the measured error curve so that
    #: a flag is raised before the bearing error exceeds the 5 deg T11 limit.
    #: `q_sub` is a presence check (its range is compressed), the other two are
    #: quality checks.
    flag_sub_threshold: float = 0.15
    flag_am_threshold: float = 0.35
    flag_stab_threshold: float = 0.40
    #: Windows of bearing history used by `q_stab`.
    history: int = 5

    def __post_init__(self) -> None:
        missing = {"am", "sub", "snr", "stab"} - set(self.weights)
        if missing:
            raise ValueError(f"quality weights missing: {sorted(missing)}")
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"quality weights must sum to 1.0, got {total}")
        if self.snr_hi_db <= self.snr_lo_db:
            raise ValueError("snr_hi_db must exceed snr_lo_db")


@dataclass
class QualityResult:
    q_am: float
    q_sub: float
    q_snr: float
    q_stab: float
    value: int              # 0-100, the published vorsignal
    flagged: bool
    #: True when the flag is raised only because there is not yet enough bearing
    #: history to judge stability. Amendment A14: a warming-up estimator and a
    #: dead signal both produce vorsignal 0, but they are not the same
    #: condition, and the state machine must not report a healthy acquisition
    #: as a signal failure.
    warming: bool = False
    reason: str = ""

    @property
    def band(self) -> str:
        if self.value == 0:
            return "FLAG"
        if self.value >= GOOD:
            return "good"
        if self.value >= ACCEPTABLE:
            return "acceptable"
        return "degraded"

    def as_dict(self) -> dict:
        return {"q_am": self.q_am, "q_sub": self.q_sub, "q_snr": self.q_snr,
                "q_stab": self.q_stab, "vorsignal": self.value,
                "flagged": self.flagged, "warming": self.warming,
                "reason": self.reason}


class QualityEstimator:
    """Stateful because `q_stab` needs a short history of bearings."""

    def __init__(self, p: QualityParams | None = None):
        self.p = p or QualityParams()
        self._bearings: list[float] = []

    def reset(self) -> None:
        """Called on retune: dispersion from the old station means nothing here."""
        self._bearings = []

    def update(self, obs, bearing_deg: float | None = None,
               tracking: bool = True) -> QualityResult:
        """Evaluate one observation.

        `obs` is an `rbvor.dsp.Observation`. `bearing_deg` may be None before
        calibration, in which case `q_stab` is held at 0 and cannot inflate the
        result -- pessimistic by design.
        """
        p = self.p
        q_am = _clip01(obs.amp_a / p.am_nominal_depth)
        q_sub = _clip01(obs.sub_level / p.sub_nominal_depth)
        q_snr = _clip01((obs.snr_db - p.snr_lo_db) / (p.snr_hi_db - p.snr_lo_db))

        if bearing_deg is not None:
            self._bearings.append(float(bearing_deg))
            if len(self._bearings) > p.history:
                self._bearings = self._bearings[-p.history:]
        if len(self._bearings) >= 2:
            from rbvor.bearing import circular_std
            q_stab = _clip01(1.0 - circular_std(self._bearings) / p.stab_full_deg)
        else:
            q_stab = 0.0

        w = p.weights
        composite = (w["am"] * q_am + w["sub"] * q_sub
                     + w["snr"] * q_snr + w["stab"] * q_stab)
        value = int(round(100.0 * _clip01(composite)))

        reason = ""
        flagged = False
        warming = False
        if not tracking:
            flagged, reason = True, "not tracking"
        elif obs.overrun:
            flagged, reason = True, "sample overrun in window"
        elif q_sub < p.flag_sub_threshold:
            flagged, reason = True, f"no usable subcarrier ({q_sub:.2f})"
        elif q_am < p.flag_am_threshold:
            flagged, reason = True, f"30 Hz AM below threshold ({q_am:.2f})"
        elif len(self._bearings) >= 2 and q_stab < p.flag_stab_threshold:
            flagged, reason = True, f"bearing unstable ({q_stab:.2f})"
        elif len(self._bearings) < 2:
            flagged, warming = True, True
            reason = "acquiring: insufficient bearing history"
        if flagged:
            value = 0

        return QualityResult(q_am=q_am, q_sub=q_sub, q_snr=q_snr, q_stab=q_stab,
                             value=value, flagged=flagged, warming=warming,
                             reason=reason)


def warmup_windows(p: QualityParams) -> int:
    """Windows needed before `q_stab` is meaningful. Callers should not treat
    the first result as representative."""
    return 2 + int(np.ceil(p.history / 2))
