"""L1 -- ILS localizer channel rules (CLAUDE.md A33).

The expected localizer set is written out here from the channel plan as text
(odd tenths 1/3/5/7/9 of 108-111 MHz, x.x0 and x.x5), not computed with the
modulo arithmetic freqrules uses, so one wrong operator there cannot be
mirrored into this file and pass (the A5 method).

Source: Annex 10 Vol I 3.1.3.2.1 (band 108-111.975 MHz), read in the
Amendment 74 text. The odd-tenth pairing plan is Annex 10 Vol V and was NOT
checked against that document: it is the rule this module already implemented.
"""

from __future__ import annotations

import pytest

from rbvor.freqrules import (
    NavaidClass,
    classify,
    classify_navaid,
    is_loc_frequency,
    is_vor_frequency,
    loc_channels,
    vor_channels,
)

EXPECTED_LOC_MHZ = [f"{mhz}.{tenth}{hund}" for mhz in ("108", "109", "110", "111")
                    for tenth in "13579" for hund in "05"]
EXPECTED_LOC_KHZ = sorted(int(s.replace(".", "")) * 10 for s in EXPECTED_LOC_MHZ)
ALL_CHANNELS = list(range(108_000, 117_951, 50))


def test_forty_localizer_channels_exactly_as_the_plan_lists_them():
    assert len(EXPECTED_LOC_KHZ) == 40
    assert EXPECTED_LOC_KHZ[0] == 108_100 and EXPECTED_LOC_KHZ[-1] == 111_950
    assert loc_channels() == EXPECTED_LOC_KHZ


@pytest.mark.parametrize("ch", ALL_CHANNELS, ids=lambda c: f"{c/1000:.3f}")
def test_every_channel_is_exactly_one_of_vor_or_loc(ch):
    mhz = ch / 1000.0
    is_loc = is_loc_frequency(mhz)[0]
    is_vor = is_vor_frequency(mhz)[0]
    assert is_loc != is_vor
    assert is_loc == (ch in EXPECTED_LOC_KHZ)


def test_vor_set_is_unchanged_by_adding_localizers():
    """is_vor_frequency is a published primitive: same 160 channels."""
    assert len(vor_channels()) == 160
    assert set(vor_channels()).isdisjoint(loc_channels())


@pytest.mark.parametrize("mhz", [109.5, 108.1, 108.15, 111.95, 110.3, 109.4999])
def test_localizer_accepted_with_a_truthful_reason(mhz):
    ok, reason = is_loc_frequency(mhz)
    assert ok and "localizer" in reason


@pytest.mark.parametrize("mhz,word", [
    (109.4, "VOR"), (112.1, "VOR"), (113.35, "VOR"),
    (118.0, "outside"), (107.9, "outside"), (109.525, "grid"),
    (None, "usable"), ("109.5", "usable"), (float("nan"), "usable"),
])
def test_non_localizers_rejected_with_a_reason(mhz, word):
    ok, reason = is_loc_frequency(mhz)
    assert not ok and word in reason


def test_denied_vor_channel_is_not_decoded_as_a_localizer():
    """The trap the plan named: the denylist used to return LOC, and once LOC
    is decodable that would tune a denied VOR channel as a localizer."""
    deny = {"denylist_mhz": [109.4]}
    assert classify_navaid(109.4, **deny) is NavaidClass.DENIED
    assert is_loc_frequency(109.4, **deny)[0] is False
    assert is_vor_frequency(109.4, **deny)[0] is False


def test_denied_reason_no_longer_claims_an_odd_digit():
    ok, reason = is_vor_frequency(109.4, denylist_mhz=[109.4])
    assert not ok and "denylist" in reason and "odd" not in reason


def test_a_denied_localizer_is_denied_too():
    assert is_loc_frequency(109.5, denylist_mhz=[109.5])[0] is False
    assert 109_500 not in loc_channels(denylist_mhz=[109.5])
    assert len(loc_channels(denylist_mhz=[109.5])) == 39


def test_published_classify_still_reports_denied_as_loc():
    """Kept for consumers of the published primitive (and its own tests)."""
    assert classify(109.4, denylist_mhz=[109.4]) is NavaidClass.LOC


def test_allowlisted_localizer_channel_is_a_vor_not_a_localizer():
    allow = {"allowlist_mhz": [109.5]}
    assert is_vor_frequency(109.5, **allow)[0] is True
    assert is_loc_frequency(109.5, **allow)[0] is False


def test_classify_navaid_agrees_with_classify_everywhere_without_lists():
    for ch in ALL_CHANNELS:
        assert classify_navaid(ch / 1000.0) is classify(ch / 1000.0)
