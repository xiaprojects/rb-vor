"""Phase difference to magnetic radial, and circular statistics.

Reference: rb-vor-architecture.md section 8; amendments A2, A6, A10.
Implementation plan M4.

The conversion itself is one line. Everything else in this module exists to
stop a wrong bearing being published as a valid one, which is failure mode F1 --
the worst outcome this project has, because a confidently wrong needle is more
dangerous than no needle at all.

Two rules follow from that:

1. **No calibration, no bearing.** `phase_sign` and `phase_offset_deg` ship as
   `null` (amendment A2) and `radial()` raises rather than guess. A default of
   zero would silently publish an uncalibrated bearing that looks plausible.
2. **Circular arithmetic everywhere.** Averaging 359 deg and 1 deg linearly
   gives 180 -- exactly the wrong answer, pointing the aircraft the other way.
   Every mean, deviation and smoothing operation here is a vector operation.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

__all__ = ["BearingCal", "UncalibratedError", "radial", "circular_mean",
           "circular_std", "circular_resultant", "wrap360", "wrap180",
           "angular_difference", "to_int_bearing", "BearingTracker"]


class UncalibratedError(RuntimeError):
    """Raised when a bearing is requested before calibration is recorded (A2)."""


def wrap360(deg: float) -> float:
    """Normalise to [0, 360). Never returns 360.0.

    `x % 360.0` on a tiny negative value returns exactly 360.0 in float, because
    360.0 - 1e-15 is not representable. That would put a bearing outside the
    0-359 range declared by RB-VOR-IF (every version) -- and `circular_mean([359, 1])`,
    which must be 0, produces exactly that tiny negative angle.
    """
    v = float(deg) % 360.0
    return 0.0 if v >= 360.0 - 1e-9 else v


def wrap180(deg: float) -> float:
    return float((deg + 180.0) % 360.0 - 180.0)


def angular_difference(a: float, b: float) -> float:
    """Smallest absolute angle between two bearings, 0..180."""
    return abs(wrap180(a - b))


def to_int_bearing(deg: float) -> int:
    """Round to the integer bearing published as `vorradial`, 0..359.

    Two traps, both caught by tests:
      * round(359.7) is 360, which is outside the declared range;
      * round() uses banker's rounding, so round(180.5) is 180 and round(181.5)
        is 182. `floor(x + 0.5)` is used instead, matching the convention
        already adopted in `freqrules.channel_khz`.
    """
    return int(math.floor(wrap360(deg) + 0.5)) % 360


@dataclass(frozen=True)
class BearingCal:
    """Installation calibration. See docs/calibration.md.

    `phase_sign` and `phase_offset_deg` are properties of the *implementation*
    (which path is A, which is B, the discriminator's sign and delay) and are
    measured once against the synthetic generator. `station_cal_deg` is a
    property of the *installation* and is measured on a real station (T8); it
    stays 0 in the shipped default and is never guessed.
    """

    phase_sign: int | None = None
    phase_offset_deg: float | None = None
    station_cal_deg: float = 0.0
    smooth_n: int = 3

    def __post_init__(self) -> None:
        if self.phase_sign not in (None, 1, -1):
            raise ValueError(f"phase_sign must be +1, -1 or None, got {self.phase_sign!r}")
        if self.smooth_n < 1:
            raise ValueError("smooth_n must be >= 1")

    @property
    def calibrated(self) -> bool:
        return self.phase_sign is not None and self.phase_offset_deg is not None

    def require(self) -> None:
        if not self.calibrated:
            raise UncalibratedError(
                "bearing.phase_sign and bearing.phase_offset_deg are not set. "
                "Run tools/vor_calibrate.py and record the result in the config "
                "(amendment A2: an uncalibrated bearing must never be published)."
            )


def radial(delta_phi_rad: float, cal: BearingCal) -> float:
    """Magnetic radial FROM the station, 0..360, from a phase difference.

    Raises `UncalibratedError` if the calibration is incomplete.
    """
    cal.require()
    deg = math.degrees(delta_phi_rad) * cal.phase_sign
    return wrap360(deg + cal.phase_offset_deg + cal.station_cal_deg)


# --------------------------------------------------------------------------- #
# circular statistics
# --------------------------------------------------------------------------- #

def circular_resultant(bearings: Iterable[float]) -> complex:
    v = np.asarray(list(bearings), dtype=float)
    if v.size == 0:
        return 0j
    return complex(np.mean(np.exp(1j * np.deg2rad(v))))


def circular_mean(bearings: Sequence[float]) -> float:
    """Vector mean. `circular_mean([359, 1]) == 0`, not 180."""
    r = circular_resultant(bearings)
    if abs(r) < 1e-12:
        raise ValueError("bearings cancel out; circular mean is undefined")
    return wrap360(math.degrees(np.angle(r)))


def circular_std(bearings: Sequence[float]) -> float:
    """Circular standard deviation in degrees, sqrt(-2 ln R).

    Unbounded above: a uniform spread tends to infinity, which is the honest
    answer. Callers clip it into a quality figure.
    """
    n = len(list(bearings))
    if n < 2:
        return 0.0
    r = abs(circular_resultant(bearings))
    return float(math.degrees(math.sqrt(-2.0 * math.log(max(r, 1e-12)))))


class BearingTracker:
    """Smoothing and dispersion over a short history of bearings."""

    def __init__(self, cal: BearingCal, history: int = 5):
        self.cal = cal
        self._smooth = deque(maxlen=max(1, cal.smooth_n))
        self._hist = deque(maxlen=max(2, history))

    def reset(self) -> None:
        """Called on retune. No bearing from the old station may survive (F3)."""
        self._smooth.clear()
        self._hist.clear()

    def update(self, delta_phi_rad: float) -> float:
        raw = radial(delta_phi_rad, self.cal)
        self._smooth.append(raw)
        self._hist.append(raw)
        try:
            return circular_mean(list(self._smooth))
        except ValueError:
            # The smoothing window cancelled out exactly (e.g. 0 and 180 with
            # smooth_n = 2). A ValueError here would propagate out of
            # Controller.push() and kill the sample pump (F3). The newest raw
            # value is the honest answer; q_stab will flag the dispersion.
            return raw

    @property
    def raw(self) -> float | None:
        return self._smooth[-1] if self._smooth else None

    @property
    def dispersion_deg(self) -> float:
        return circular_std(list(self._hist))

    @property
    def n(self) -> int:
        return len(self._hist)
