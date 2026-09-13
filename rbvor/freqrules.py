"""VHF navaid channel rules.

Reference: rb-vor-architecture.md section 5 (Frequency validation).

This module is a published RB-Avionics platform primitive: RB-01, RB-02 and any
third-party bridge consumer are expected to reuse it rather than re-deriving the
even/odd 100 kHz rule. It is deliberately pure -- no config import, no logging,
no I/O -- so it can be lifted into other products unchanged.

All arithmetic is done in integer kHz. Never compare navaid frequencies as
floats: 109.4 is not exactly representable in binary and 109.4 == 109.4 is not
a safe equality test after a JSON round trip.

Rules
-----
R1  108.000 MHz <= f <= 117.950 MHz
R2  f lands on the 50 kHz channel grid (snapped with a +/-1 kHz tolerance)
R3  108.000-111.950: VOR only if the 100 kHz digit is EVEN.
    An odd 100 kHz digit is an ILS localiser and must be rejected.
R4  112.000-117.950: every 50 kHz channel is a VOR channel
R5  optional allowlist / denylist for regional exceptions and test channels

Note on R3/R4: a handful of channels (e.g. 108.000-108.050) are reserved or
unassigned in some regions despite satisfying the even-digit rule. That is a
regional assignment matter, not a channelling rule -- handle it with the R5
denylist, not by special-casing this module.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from enum import StrEnum

__all__ = [
    "NavaidClass",
    "BAND_MIN_KHZ",
    "BAND_MAX_KHZ",
    "GRID_KHZ",
    "SNAP_TOLERANCE_KHZ",
    "LOC_SPLIT_KHZ",
    "channel_khz",
    "classify",
    "is_vor_frequency",
    "vor_channels",
]

BAND_MIN_KHZ: int = 108_000
BAND_MAX_KHZ: int = 117_950
GRID_KHZ: int = 50
SNAP_TOLERANCE_KHZ: int = 1
#: Below this channel the even/odd 100 kHz digit decides VOR vs ILS localiser.
#: At or above it, every channel on the grid is a VOR channel.
LOC_SPLIT_KHZ: int = 112_000


class NavaidClass(StrEnum):
    """Classification of a tuned channel."""

    VOR = "VOR"
    LOC = "LOC"
    OFF_GRID = "OFF_GRID"
    OUT_OF_BAND = "OUT_OF_BAND"
    INVALID = "INVALID"


def _to_khz(mhz: object) -> int | None:
    """Convert a caller-supplied MHz value to integer kHz, or None if unusable.

    Accepts int/float. Rejects None, bool, strings, NaN and infinities: the
    bridge payload is external input and must not be trusted to be a number.
    """
    if isinstance(mhz, bool) or not isinstance(mhz, (int, float)):
        return None
    value = float(mhz)
    if not math.isfinite(value):
        return None
    # floor(x + 0.5) rather than round(): round() uses banker's rounding, which
    # would make the .5 kHz boundary behave asymmetrically between channels.
    return int(math.floor(value * 1000.0 + 0.5))


def channel_khz(
    mhz: object,
    *,
    grid_khz: int = GRID_KHZ,
    tolerance_khz: int = SNAP_TOLERANCE_KHZ,
) -> int | None:
    """Quantise a frequency in MHz onto the navaid channel grid.

    Returns the channel in integer kHz, or None if the value is not a number or
    does not sit on the grid within ``tolerance_khz``.

    The returned integer is the canonical channel identity. Compare channels
    with this value, never with the original float.

    >>> channel_khz(109.4)
    109400
    >>> channel_khz(109.4000001)
    109400
    >>> channel_khz(109.425) is None
    True
    """
    raw = _to_khz(mhz)
    if raw is None:
        return None
    snapped = int(math.floor(raw / grid_khz + 0.5)) * grid_khz
    if abs(raw - snapped) > tolerance_khz:
        return None
    return snapped


def _normalise_list(values: Iterable[object] | None) -> frozenset:
    """Convert an allow/deny list of MHz floats into a set of channel kHz."""
    if not values:
        return frozenset()
    out = set()
    for v in values:
        ch = channel_khz(v)
        if ch is not None:
            out.add(ch)
    return frozenset(out)


def classify(
    mhz: object,
    *,
    allowlist_mhz: Sequence[object] | None = None,
    denylist_mhz: Sequence[object] | None = None,
    band_min_khz: int = BAND_MIN_KHZ,
    band_max_khz: int = BAND_MAX_KHZ,
    grid_khz: int = GRID_KHZ,
    tolerance_khz: int = SNAP_TOLERANCE_KHZ,
) -> NavaidClass:
    """Classify a frequency in MHz. See module docstring for rules R1-R5."""
    raw = _to_khz(mhz)
    if raw is None:
        return NavaidClass.INVALID

    # Ordering matters, and this ordering is the specification:
    #   1. coarse band reject, so a wildly wrong value (0, 1090 MHz) is reported
    #      as out-of-band rather than as a grid problem;
    #   2. snap to the grid;
    #   3. band-check the SNAPPED channel, not the raw value.
    # Consequence, deliberate: a value within the snap tolerance of a band-edge
    # channel is that channel. 107.999 -> 108.000 (VOR). 118.000 stays out of
    # band because it snaps to itself, and 117.975 is off-grid, not out-of-band.
    if raw < band_min_khz - grid_khz or raw > band_max_khz + grid_khz:
        return NavaidClass.OUT_OF_BAND

    # R2
    channel = channel_khz(mhz, grid_khz=grid_khz, tolerance_khz=tolerance_khz)
    if channel is None:
        return NavaidClass.OFF_GRID

    # R1, applied to the snapped channel
    if channel < band_min_khz or channel > band_max_khz:
        return NavaidClass.OUT_OF_BAND

    # R5 -- applied last, so an operator can force a channel either way.
    if channel in _normalise_list(denylist_mhz):
        return NavaidClass.LOC
    if channel in _normalise_list(allowlist_mhz):
        return NavaidClass.VOR

    # R4
    if channel >= LOC_SPLIT_KHZ:
        return NavaidClass.VOR

    # R3
    hundreds_digit = (channel % 1000) // 100
    return NavaidClass.VOR if hundreds_digit % 2 == 0 else NavaidClass.LOC


def is_vor_frequency(mhz: object, **kwargs) -> tuple[bool, str]:
    """Return ``(valid, reason)`` for a candidate ``vorfreq`` value.

    ``reason`` is a short, log-safe explanation and is always populated, both
    for accepted and rejected values.

    >>> is_vor_frequency(109.4)
    (True, 'VOR channel 109.400 MHz')
    >>> is_vor_frequency(109.5)[0]
    False
    """
    cls = classify(mhz, **kwargs)
    if cls is NavaidClass.VOR:
        return True, f"VOR channel {channel_khz(mhz) / 1000.0:.3f} MHz"
    if cls is NavaidClass.LOC:
        return False, (
            f"{channel_khz(mhz) / 1000.0:.3f} MHz is an ILS localiser channel "
            "(odd 100 kHz digit below 112 MHz), not a VOR"
        )
    if cls is NavaidClass.OFF_GRID:
        return False, f"{mhz} MHz is not on the {GRID_KHZ} kHz channel grid"
    if cls is NavaidClass.OUT_OF_BAND:
        return False, (
            f"{mhz} MHz is outside the VOR band "
            f"{BAND_MIN_KHZ / 1000.0:.3f}-{BAND_MAX_KHZ / 1000.0:.3f} MHz"
        )
    return False, f"{mhz!r} is not a usable frequency value"


def vor_channels(**kwargs) -> list[int]:
    """All VOR channels in the band, in kHz. Used by tests and by the scanner."""
    return [
        ch
        for ch in range(BAND_MIN_KHZ, BAND_MAX_KHZ + 1, GRID_KHZ)
        if classify(ch / 1000.0, **kwargs) is NavaidClass.VOR
    ]
