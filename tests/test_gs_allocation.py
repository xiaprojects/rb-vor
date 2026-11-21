"""Dongle allocation for localizer and glide slope (docs/gs-plan.md 6.1, A40).

The cases are written out as a table, as the plan asks: 0-3 dongles, `use`
restrictions, absent / duplicate / unlisted serials, index entries. The
single form (no `sdr.devices`) must behave exactly as A27.
"""

from __future__ import annotations

import pytest

from rbvor.sdr import (
    FakeDevice,
    SdrError,
    SdrReader,
    allocate,
    apply_tuner_profile,
    device_cfg,
    format_allocation,
    resolve_device,
)

BASE = {"sample_rate_hz": 1_024_000, "tuner_offset_hz": -256_000, "block_samples": 4096,
        "buffer_blocks": 4, "gain_db": 19.2, "ppm": 0, "swap_iq": False}


def dev(serial=None, index=None, use=None, **kw):
    e = {}
    if serial is not None:
        e["device_serial"] = serial
    if index is not None:
        e["device_index"] = index
    if use is not None:
        e["use"] = use
    e.update(kw)
    return e


NAV, GS, BOTH = ["nav"], ["gs"], ["nav", "gs"]

# (case, devices list, present serials, expected nav index, gs index, shared)
CASES = [
    ("one listed, present, default use", [dev("A")], ["A"], 0, None, True),
    ("two listed, both present", [dev("A"), dev("B")], ["A", "B"], 0, 1, False),
    ("order follows the list, not USB", [dev("B"), dev("A")], ["A", "B"], 1, 0, False),
    ("second absent -> shared", [dev("A"), dev("B")], ["A"], 0, None, True),
    ("first absent -> second does both", [dev("A"), dev("B")], ["B"], 0, None, True),
    ("none present", [dev("A"), dev("B")], [], None, None, False),
    ("unlisted dongles are never used", [dev("A")], ["X", "A", "Y"], 1, None, True),
    ("only unlisted present", [dev("A")], ["X", "Y"], None, None, False),
    ("nav-only dongle, no gs -> no glide slope", [dev("A", use=NAV)], ["A"], 0, None, False),
    ("gs-only dongle alone -> no nav", [dev("B", use=GS)], ["B"], None, 0, False),
    ("nav-only + gs-only", [dev("A", use=NAV), dev("B", use=GS)], ["A", "B"], 0, 1, False),
    ("gs-only listed first", [dev("B", use=GS), dev("A", use=NAV)], ["A", "B"], 0, 1, False),
    ("three listed, three present: third unused",
     [dev("A"), dev("B"), dev("C")], ["C", "B", "A"], 2, 1, False),
    ("both dongles gs-capable, first nav-only", [dev("A", use=NAV), dev("B")], ["A", "B"],
     0, 1, False),
    ("index entries", [dev(index=0), dev(index=1)], ["", ""], 0, 1, False),
    ("index entry beyond enumeration", [dev(index=0), dev(index=3)], ["A"], 0, None, True),
]


@pytest.mark.parametrize("name,devices,serials,nav,gs,shared", CASES,
                         ids=[c[0] for c in CASES])
def test_allocation_table(name, devices, serials, nav, gs, shared):
    a = allocate({**BASE, "devices": devices}, serials)
    assert (a.nav.index if a.nav else None) == nav
    assert (a.gs.index if a.gs else None) == gs
    assert a.shared is shared
    assert a.listed


def test_absent_entry_is_a_note_naming_what_is_present_not_an_error():
    a = allocate({**BASE, "devices": [dev("rbnav1"), dev("rbgs1")]}, ["rbnav1", "stratux:1090"])
    assert a.shared
    assert any("rbgs1" in n and "stratux:1090" in n for n in a.notes)


def test_no_dongle_at_all_says_so_for_both_features():
    a = allocate({**BASE, "devices": [dev("A")]}, [])
    assert a.nav is None and a.gs is None and not a.shared
    assert any("nav" in n for n in a.notes) and any("glide slope" in n for n in a.notes)


def test_two_dongles_sharing_a_listed_serial_is_an_error():
    with pytest.raises(SdrError, match="share serial"):
        allocate({**BASE, "devices": [dev("A")]}, ["A", "A"])


def test_two_entries_resolving_to_one_dongle_is_an_error():
    with pytest.raises(SdrError, match="both resolve"):
        allocate({**BASE, "devices": [dev("A"), dev(index=0)]}, ["A", "B"])


def test_index_entry_with_several_dongles_warns_like_a27():
    a = allocate({**BASE, "devices": [dev(index=1)]}, ["A", "B"])
    assert a.nav.index == 1
    assert any("not stable" in n for n in a.notes)


# -- the single form is A27, unchanged ------------------------------------------

def test_without_a_list_one_dongle_serves_everything_and_gs_shares_it():
    a = allocate({**BASE, "device_serial": "B"}, ["A", "B"])
    assert a.nav == resolve_device({**BASE, "device_serial": "B"}, ["A", "B"])
    assert a.gs is None and a.shared and not a.listed


def test_without_a_list_a_missing_serial_is_still_a_loud_error():
    with pytest.raises(SdrError, match="no RTL-SDR with serial"):
        allocate({**BASE, "device_serial": "Z"}, ["A", "B"])


def test_without_a_list_the_a27_warning_is_carried():
    a = allocate(dict(BASE), ["A", "B"])
    assert a.nav.index == 0 and any("not stable" in n for n in a.notes)


# -- overrides and precedence ------------------------------------------------------

def test_device_cfg_opens_exactly_the_chosen_dongle_with_its_overrides():
    a = allocate({**BASE, "devices": [dev("A"), dev("B", gain_db=32.8, ppm=2)]}, ["B", "A"])
    c = device_cfg({**BASE, "devices": []}, a.gs)
    assert "devices" not in c
    assert c["device_serial"] == "B" and c["device_index"] == 0
    assert c["gain_db"] == 32.8 and c["ppm"] == 2
    assert resolve_device(c, ["B", "A"]).index == 0


def test_entry_override_wins_over_the_tuner_profile_which_wins_over_the_block():
    cfg = {**BASE, "tuner_profiles": {"R820T": {"gain_db": 40.2, "ppm": 5}},
           "devices": [dev("A", gain_db=32.8)]}
    c = device_cfg(cfg, allocate(cfg, ["A"]).nav)
    eff, _ = apply_tuner_profile(c, "R820T")
    assert eff["gain_db"] == 32.8          # entry
    assert eff["ppm"] == 5                 # profile, the entry is silent on it


def test_profile_application_without_entries_is_unchanged():
    cfg = {**BASE, "tuner_profiles": {"R820T": {"gain_db": 40.2}}}
    eff, notes = apply_tuner_profile(cfg, "R820T")
    assert eff["gain_db"] == 40.2 and notes == ["gain_db: 19.2 -> 40.2"]


def test_format_allocation_names_every_case():
    a = allocate({**BASE, "devices": [dev("A")]}, ["A"])
    txt = "\n".join(format_allocation(a, ["A"]))
    assert "shares the nav dongle" in txt and "serial 'A'" in txt
    a = allocate({**BASE, "devices": [dev("A"), dev("B")]}, ["A", "B"])
    assert "(dedicated)" in "\n".join(format_allocation(a, ["A", "B"]))


# -- the reader follows the allocation ------------------------------------------

def test_a_reader_bound_to_no_dongle_refuses_to_open_with_the_reason():
    r = SdrReader(dict(BASE))
    r.bind(None, "no listed dongle present may serve the glide slope")
    with pytest.raises(SdrError, match="glide slope"):
        r.set_frequency(332_600_000)


def test_rebinding_to_another_dongle_closes_the_open_one():
    import numpy as np
    r = SdrReader(dict(BASE), device=FakeDevice(lambda n: np.zeros(n, np.complex64)))
    r.cfg = {**BASE, "device_serial": "A", "device_index": 0}
    first = r.device
    r.bind({**BASE, "device_serial": "A", "device_index": 0})
    assert r.device is first                    # same dongle: untouched
    r.bind({**BASE, "device_serial": "B", "device_index": 1})
    assert first.closed and r.device is None
    r.close()
