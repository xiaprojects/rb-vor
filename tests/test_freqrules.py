"""T1 -- frequency rules.

Acceptance: all 200 channels from 108.000 to 117.950 MHz classified correctly,
plus out-of-band, off-grid and malformed-input cases.

The expected classification is derived here independently of the implementation
(a literal odd-digit set rather than a modulo test) so that a single wrong
operator in freqrules.py cannot be mirrored into the test and pass.
"""

from __future__ import annotations

import math

import pytest

from rbvor.freqrules import (
    NavaidClass,
    channel_khz,
    classify,
    is_vor_frequency,
    vor_channels,
)

ODD_HUNDREDS = {1, 3, 5, 7, 9}


def expected_class(channel_khz_value: int) -> NavaidClass:
    """Independent restatement of R3/R4 for the 108-118 MHz band."""
    if channel_khz_value >= 112_000:
        return NavaidClass.VOR
    hundreds = int(str(channel_khz_value).zfill(6)[3])
    return NavaidClass.LOC if hundreds in ODD_HUNDREDS else NavaidClass.VOR


ALL_CHANNELS = list(range(108_000, 117_951, 50))


def test_band_has_200_channels():
    assert len(ALL_CHANNELS) == 200


@pytest.mark.parametrize("ch", ALL_CHANNELS, ids=lambda c: f"{c/1000:.3f}")
def test_every_channel_classified(ch: int):
    mhz = ch / 1000.0
    assert classify(mhz) is expected_class(ch)
    assert channel_khz(mhz) == ch


def test_vor_loc_split_counts():
    """Sanity on the shape of the band, not just per-channel agreement."""
    vor = [c for c in ALL_CHANNELS if classify(c / 1000.0) is NavaidClass.VOR]
    loc = [c for c in ALL_CHANNELS if classify(c / 1000.0) is NavaidClass.LOC]
    assert len(vor) + len(loc) == 200
    # 108.000-111.950 is 80 channels, split evenly between even and odd
    # 100 kHz digits; 112.000-117.950 is 120 channels, all VOR.
    assert len(loc) == 40
    assert len(vor) == 160
    assert vor_channels() == vor


@pytest.mark.parametrize(
    "mhz,expect",
    [
        (109.40, NavaidClass.VOR),   # the worked example from the bridge payload
        (109.50, NavaidClass.LOC),
        (108.00, NavaidClass.VOR),
        (108.05, NavaidClass.VOR),
        (108.10, NavaidClass.LOC),
        (108.15, NavaidClass.LOC),
        (108.20, NavaidClass.VOR),
        (111.80, NavaidClass.VOR),
        (111.90, NavaidClass.LOC),
        (111.95, NavaidClass.LOC),
        (112.00, NavaidClass.VOR),
        (112.10, NavaidClass.VOR),   # above the split, odd digit is still VOR
        (113.35, NavaidClass.VOR),
        (117.95, NavaidClass.VOR),
    ],
)
def test_spot_checks(mhz: float, expect: NavaidClass):
    assert classify(mhz) is expect


@pytest.mark.parametrize("mhz", [107.95, 107.90, 118.00, 118.05, 0.0, 1090.0, -109.4])
def test_out_of_band(mhz: float):
    assert classify(mhz) is NavaidClass.OUT_OF_BAND
    assert is_vor_frequency(mhz)[0] is False


@pytest.mark.parametrize(
    "mhz,expect_channel",
    [(107.999, 108_000), (108.0009, 108_000), (117.9491, 117_950)],
)
def test_band_edge_within_tolerance_snaps_inward(mhz: float, expect_channel: int):
    """Specification, not an accident: snap first, then band-check the channel.

    A value one tolerance step outside the band edge is the edge channel. A
    value that snaps to a channel outside the band (118.000) is out of band.
    """
    assert channel_khz(mhz) == expect_channel
    assert classify(mhz) is NavaidClass.VOR


@pytest.mark.parametrize("mhz", [109.425, 109.43, 110.005, 117.975, 112.02])
def test_off_grid(mhz: float):
    assert classify(mhz) is NavaidClass.OFF_GRID
    assert channel_khz(mhz) is None


@pytest.mark.parametrize("mhz", [109.4000001, 109.3999999, 109.4009, 109.3991])
def test_float_tolerance_snaps_to_channel(mhz: float):
    """JSON round-tripping must not turn a valid channel into an off-grid one."""
    assert channel_khz(mhz) == 109_400
    assert classify(mhz) is NavaidClass.VOR


@pytest.mark.parametrize("mhz", [None, "109.4", "", [], {}, True, False, float("nan"),
                                 float("inf"), float("-inf")])
def test_malformed_input_is_invalid_not_an_exception(mhz):
    """The bridge is external input. Bad values are classified, never raised."""
    assert classify(mhz) in (NavaidClass.INVALID, NavaidClass.OUT_OF_BAND)
    ok, reason = is_vor_frequency(mhz)
    assert ok is False
    assert reason


def test_denylist_overrides_a_valid_vor_channel():
    assert classify(109.4, denylist_mhz=[109.4]) is NavaidClass.LOC
    assert is_vor_frequency(109.4, denylist_mhz=[109.4])[0] is False


def test_allowlist_overrides_a_localiser_channel():
    assert classify(109.5, allowlist_mhz=[109.5]) is NavaidClass.VOR


def test_denylist_wins_over_allowlist():
    assert classify(109.4, allowlist_mhz=[109.4], denylist_mhz=[109.4]) is NavaidClass.LOC


def test_reason_strings_are_populated_and_specific():
    ok, reason = is_vor_frequency(109.4)
    assert ok and "109.400" in reason
    ok, reason = is_vor_frequency(109.5)
    assert not ok and "localiser" in reason
    ok, reason = is_vor_frequency(120.0)
    assert not ok and "outside" in reason
    ok, reason = is_vor_frequency(109.425)
    assert not ok and "grid" in reason


def test_channel_identity_is_integer_khz():
    """Guard against anyone reintroducing float equality on frequencies."""
    a = channel_khz(109.4)
    b = channel_khz(109.40000000001)
    assert isinstance(a, int) and a == b
    assert not math.isclose(109.4, 109.40000000001, rel_tol=0, abs_tol=0)
