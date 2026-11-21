"""Glide-path pairing (docs/gs-plan.md 4.4, CLAUDE.md A39).

The table is safety data: a wrong row tunes another runway's glide path. So
it is written out here a second time, as text, in a different layout (with
the TACAN channel of each pair, as published), and compared with
`freqrules.GS_PAIRING_KHZ` -- the A5 principle of deriving the expectation
independently of the code under test.
"""

from __future__ import annotations

import pytest

from rbvor import freqrules
from rbvor.freqrules import GS_PAIRING_KHZ, gs_channel_khz, gs_channels, loc_channels

PAIRING_TEXT = """
18X 108.10 334.70   18Y 108.15 334.55   19Y 108.30 334.10   20Y 108.35 333.95
22X 108.50 329.90   22Y 108.55 329.75   24X 108.70 330.50   24Y 108.75 330.35
26X 108.90 329.30   26Y 108.95 329.15   28X 109.10 331.40   28Y 109.15 331.25
30X 109.30 332.00   30Y 109.35 331.85   32X 109.50 332.60   32Y 109.55 332.45
34X 109.70 333.20   34Y 109.75 333.05   36X 109.90 333.80   36Y 109.95 333.65
38X 110.10 334.40   38Y 110.15 334.25   40X 110.30 335.00   40Y 110.35 334.85
42X 110.50 329.60   42Y 110.55 329.45   44X 110.70 330.20   44Y 110.75 330.05
46X 110.90 330.80   46Y 110.95 330.65   48X 111.10 331.70   48Y 111.15 331.55
50X 111.30 332.30   50Y 111.35 332.15   52X 111.50 332.90   52Y 111.55 332.75
54X 111.70 333.50   54Y 111.75 333.35   56X 111.90 331.10   56Y 111.95 330.95
"""


def _parse() -> dict[int, int]:
    tok = PAIRING_TEXT.split()
    assert len(tok) % 3 == 0
    out = {}
    for i in range(0, len(tok), 3):
        loc, gs = (int(round(float(x) * 1000)) for x in tok[i + 1:i + 3])
        assert loc not in out, f"localizer {loc} listed twice"
        out[loc] = gs
    return out


def test_the_table_matches_the_independent_copy_row_by_row():
    assert _parse() == GS_PAIRING_KHZ


def test_forty_distinct_glide_paths_on_the_150_khz_grid():
    gs = list(GS_PAIRING_KHZ.values())
    assert len(gs) == 40 and len(set(gs)) == 40
    assert all((g - 329_150) % 150 == 0 for g in gs)
    assert min(gs) == 329_150 and max(gs) == 335_000
    # 40 grid slots from 329.15 to 335.00 inclusive: the map is onto the band
    assert gs_channels() == list(range(329_150, 335_001, 150))


def test_every_localizer_has_exactly_one_glide_path_and_nothing_else_does():
    assert sorted(GS_PAIRING_KHZ) == loc_channels()


def test_the_pairing_is_not_monotonic_so_an_arithmetic_rule_would_be_wrong():
    """A cheap guard against someone 'simplifying' the table into a formula:
    neighbouring localizers do not have neighbouring glide paths."""
    locs = sorted(GS_PAIRING_KHZ)
    gs = [GS_PAIRING_KHZ[k] for k in locs]
    assert gs != sorted(gs) and gs != sorted(gs, reverse=True)


@pytest.mark.parametrize("loc,gs", [(109_500, 332_600), (108_100, 334_700),
                                    (111_950, 330_950), (110_300, 335_000)])
def test_lookup(loc, gs):
    assert gs_channel_khz(loc) == gs


@pytest.mark.parametrize("ch", [None, 109_400, 112_000, 108_000, 117_950, 0, 109_501])
def test_a_channel_that_is_not_a_localizer_has_no_glide_path(ch):
    assert gs_channel_khz(ch) is None


def test_existing_classification_is_unchanged():
    """Adding the table must not move any VOR / LOC decision (A33 baseline)."""
    assert len(freqrules.vor_channels()) == 160
    assert len(loc_channels()) == 40
    assert freqrules.is_vor_frequency(109.4)[0]
    assert not freqrules.is_vor_frequency(109.5)[0]
    assert freqrules.is_loc_frequency(109.5)[0]
