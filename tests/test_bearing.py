"""M4 -- bearing conversion and circular statistics."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.bearing import (  # noqa: E402
    BearingCal,
    BearingTracker,
    UncalibratedError,
    angular_difference,
    circular_mean,
    circular_std,
    radial,
    to_int_bearing,
    wrap180,
    wrap360,
)

# --------------------------------------------------------------------------- #
# A2: no calibration, no bearing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cal", [
    BearingCal(),
    BearingCal(phase_sign=1),
    BearingCal(phase_offset_deg=-1.97),
])
def test_uncalibrated_raises_rather_than_guessing(cal):
    """Amendment A2. A default of zero would publish a plausible-looking wrong
    bearing, which is failure mode F1."""
    assert not cal.calibrated
    with pytest.raises(UncalibratedError, match="vor_calibrate"):
        radial(0.5, cal)


def test_calibrated_pair_is_accepted():
    cal = BearingCal(phase_sign=1, phase_offset_deg=-1.9657)
    assert cal.calibrated
    assert radial(math.radians(138.9657), cal) == pytest.approx(137.0, abs=1e-6)


def test_invalid_sign_rejected():
    with pytest.raises(ValueError):
        BearingCal(phase_sign=0, phase_offset_deg=0.0)
    with pytest.raises(ValueError):
        BearingCal(phase_sign=2, phase_offset_deg=0.0)


def test_sign_and_offsets_apply_as_specified():
    a = BearingCal(phase_sign=1, phase_offset_deg=10.0, station_cal_deg=5.0)
    assert radial(math.radians(100.0), a) == pytest.approx(115.0)
    b = BearingCal(phase_sign=-1, phase_offset_deg=10.0)
    assert radial(math.radians(100.0), b) == pytest.approx(270.0)


# --------------------------------------------------------------------------- #
# wrap boundaries -- where the 359/0 bugs live
# --------------------------------------------------------------------------- #

def test_wrap360_never_returns_360():
    """`x % 360.0` on a tiny negative float returns exactly 360.0, which is
    outside the 0-359 range declared by RB-VOR-IF v1.0."""
    assert wrap360(-1e-15) == 0.0
    assert wrap360(-0.0) == 0.0
    assert wrap360(360.0) == 0.0
    assert wrap360(720.0) == 0.0
    assert 0.0 <= wrap360(359.9999999) < 360.0


@pytest.mark.parametrize("deg,want", [(0.4, 0), (359.7, 0), (-0.2, 0), (359.4, 359),
                                      (0.6, 1), (180.5, 181), (359.5, 0)])
def test_to_int_bearing_stays_in_range(deg, want):
    got = to_int_bearing(deg)
    assert got == want
    assert 0 <= got <= 359


def test_wrap180():
    assert wrap180(190.0) == pytest.approx(-170.0)
    assert wrap180(-190.0) == pytest.approx(170.0)
    assert wrap180(180.0) == pytest.approx(-180.0)


def test_angular_difference_is_symmetric_and_bounded():
    assert angular_difference(359.0, 1.0) == pytest.approx(2.0)
    assert angular_difference(1.0, 359.0) == pytest.approx(2.0)
    assert angular_difference(0.0, 180.0) == pytest.approx(180.0)
    assert all(0 <= angular_difference(a, b) <= 180
               for a in range(0, 360, 37) for b in range(0, 360, 53))


# --------------------------------------------------------------------------- #
# circular statistics
# --------------------------------------------------------------------------- #

def test_circular_mean_crosses_north():
    """The bug this exists to prevent: a linear mean gives 180, which points the
    aircraft in exactly the wrong direction."""
    assert circular_mean([359.0, 1.0]) == pytest.approx(0.0, abs=1e-6)
    assert circular_mean([350.0, 10.0]) == pytest.approx(0.0, abs=1e-6)
    assert circular_mean([358.0, 359.0, 0.0, 1.0, 2.0]) == pytest.approx(0.0, abs=1e-6)


def test_circular_mean_matches_linear_away_from_the_wrap():
    assert circular_mean([100.0, 110.0, 120.0]) == pytest.approx(110.0, abs=1e-6)


def test_circular_mean_undefined_for_opposed_bearings():
    with pytest.raises(ValueError):
        circular_mean([0.0, 180.0])


def test_circular_std_behaviour():
    assert circular_std([100.0]) == 0.0
    assert circular_std([100.0, 100.0, 100.0]) == pytest.approx(0.0, abs=1e-5)
    assert circular_std([359.0, 0.0, 1.0]) < 2.0
    assert circular_std([0.0, 90.0, 180.0, 270.0]) > 100.0


# --------------------------------------------------------------------------- #
# tracker
# --------------------------------------------------------------------------- #

def test_tracker_smooths_and_reports_dispersion():
    cal = BearingCal(phase_sign=1, phase_offset_deg=0.0, smooth_n=3)
    t = BearingTracker(cal, history=5)
    for d in (99.0, 100.0, 101.0):
        out = t.update(math.radians(d))
    assert out == pytest.approx(100.0, abs=1e-6)
    assert t.dispersion_deg < 2.0
    assert t.n == 3


def test_tracker_reset_clears_history_on_retune():
    """F3: no bearing from the previous station may survive a channel change."""
    cal = BearingCal(phase_sign=1, phase_offset_deg=0.0, smooth_n=3)
    t = BearingTracker(cal)
    for d in (10.0, 11.0, 12.0):
        t.update(math.radians(d))
    t.reset()
    assert t.n == 0
    assert t.raw is None
    assert t.update(math.radians(200.0)) == pytest.approx(200.0, abs=1e-6)


def test_tracker_smoothing_crosses_north_correctly():
    cal = BearingCal(phase_sign=1, phase_offset_deg=0.0, smooth_n=3)
    t = BearingTracker(cal)
    for d in (359.0, 0.0, 1.0):
        out = t.update(math.radians(d))
    assert angular_difference(out, 0.0) < 1e-6


def test_tracker_survives_a_window_that_cancels_out():
    """0 and 180 in a two-deep window have no circular mean. That must not
    raise out of the bearing path (it would kill the sample pump, F3); the
    newest raw value is returned and q_stab is left to flag the dispersion."""
    cal = BearingCal(phase_sign=1, phase_offset_deg=0.0, smooth_n=2)
    t = BearingTracker(cal)
    t.update(math.radians(0.0))
    out = t.update(math.radians(180.0))
    assert out == pytest.approx(180.0)
    assert t.raw == pytest.approx(180.0)
