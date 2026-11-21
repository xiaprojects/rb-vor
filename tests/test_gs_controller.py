"""G6 -- the glide slope through the state machine and onto the wire.

docs/gs-plan.md 4.1, 4.5, 6.1-6.4; CLAUDE.md A40, A44; RB-VOR-IF v2.4. Driven
with a real `Publisher`, and every payload checked against
`tools/mock_bridge.py`'s validator. A `Sky` radiates per frequency, so each
fake dongle hears what it is tuned to -- which is what makes the switch and
the two-receiver cases testable without hardware.

The properties that matter most:
- noise on both receivers never makes a usable ILS (plan 4.1);
- each axis fails alone;
- on a shared receiver, the needle not received goes dark in the same POST
  that announces the switch (plan 4.5);
- with `gs.enabled` false, nothing changes on the wire (v2.3 byte for byte).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from rbvor.config import Config, ConfigError, load
from rbvor.controller import Controller, State
from rbvor.publisher import Publisher, RecordingTransport
from rbvor.sdr import SdrError
from rbvor.solutionlog import GS_FIELDS, LOC_FIELDS, SolutionLogger
from tools.gs_gen import GsParams
from tools.loc_gen import LocParams, generate_blocks
from tools.mock_bridge import GS_KEYS, LOC_KEYS, VOR_KEYS, validate
from tools.vor_gen import VorParams
from tools.vor_gen import generate_blocks as vor_blocks

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "config" / "rb-vor.json").read_text())
FS = 256_000
BLK = 1 << 15
BLK_S = BLK / FS
LOC_HZ, GS_HZ = 109_500_000, 332_600_000        # a pair from the table (A39)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeSdr:
    """Records tunes and closes; `refuse` makes the next opens fail (busy)."""

    def __init__(self, name="nav"):
        self.name = name
        self.tuned: list[int] = []
        self.closed = 0
        self.refuse = 0
        self.open = False

    def set_frequency(self, nav_hz):
        if self.refuse:
            self.refuse -= 1
            raise SdrError(f"{self.name} dongle busy")
        self.tuned.append(nav_hz)
        self.open = True

    def close(self):
        self.closed += 1
        self.open = False

    @property
    def freq(self):
        return self.tuned[-1] if self.tuned and self.open else None


def _noise(seed=0, level=0.1):
    rng = np.random.default_rng(seed)
    while True:
        z = level * (rng.standard_normal(BLK) + 1j * rng.standard_normal(BLK)) / np.sqrt(2)
        q = np.clip(np.round(z.real * 127.5 + 127.5), 0, 255)
        r = np.clip(np.round(z.imag * 127.5 + 127.5), 0, 255)
        yield ((q - 127.5) / 127.5 + 1j * (r - 127.5) / 127.5).astype(np.complex64)


def _cycle(make):
    while True:
        yield from make()


class Sky:
    """What a dongle hears at each frequency. Anything not radiated is noise."""

    def __init__(self, seed=0):
        self.src: dict[int, object] = {}
        self.noise = _noise(seed)

    def loc(self, ddm=0.05, snr_db=25.0, seed=1, hz=LOC_HZ):
        p = LocParams(fs=FS, offset_hz=-FS // 4, seconds=30.0, block=BLK, snr_db=snr_db,
                      ddm=ddm, seed=seed)
        self.src[hz] = _cycle(lambda: generate_blocks(p))
        return self

    def gs(self, ddm=0.0875, snr_db=25.0, seed=2, hz=GS_HZ, **kw):
        p = GsParams(fs=FS, offset_hz=-FS // 4, seconds=30.0, block=BLK, snr_db=snr_db,
                     ddm=ddm, seed=seed, **kw)
        self.src[hz] = _cycle(lambda: generate_blocks(p))
        return self

    def vor(self, hz=109_400_000):
        p = VorParams(radial_deg=137, fs=FS, offset_hz=-FS // 4, seconds=30.0, block=BLK,
                      snr_db=25.0, seed=3)
        self.src[hz] = _cycle(lambda: vor_blocks(p))
        return self

    def noise_at(self, hz, seed=9, level=0.1):
        self.src[hz] = _noise(seed, level)
        return self

    def block(self, hz):
        return next(self.src.get(hz, self.noise))


def make(gs=True, loc=True, two=False, gs_block=None, csv_dir=None, allocate=None, **ctl):
    raw = copy.deepcopy(BASE)
    raw["sdr"]["sample_rate_hz"] = FS
    raw["sdr"]["tuner_offset_hz"] = -FS // 4
    raw["publish"]["keepalive_s"] = 0
    raw["loc"] = {"enabled": loc}
    if gs:
        raw["gs"] = {"enabled": True, **(gs_block or {})}
    cfg = Config(raw=raw)
    clock, sdr, tr = FakeClock(), FakeSdr("nav"), RecordingTransport()
    gs_sdr = FakeSdr("gs") if two else None
    pub = Publisher(cfg.publish, tr, clock=clock)
    c = Controller(cfg, sdr, pub, clock=clock, sdr_retry_s=5.0, gs_sdr=gs_sdr,
                   allocate=allocate, **ctl)
    return c, clock, sdr, gs_sdr, tr


def select(c, clock, mhz):
    c.set_channel(mhz)
    clock.advance(0.5)
    c.tick()


def run(c, clock, sky, seconds, sdr, gs_sdr=None):
    for _ in range(int(round(seconds / BLK_S))):
        if sdr.freq is not None:
            c.push(sky.block(sdr.freq))
        if gs_sdr is not None and gs_sdr.freq is not None:
            c.push_gs(sky.block(gs_sdr.freq))
        clock.advance(BLK_S)
        c.tick()


def last(tr):
    return tr.sent[-1]


def all_valid(tr):
    bad = [(i, p, validate(p)) for i, p in enumerate(tr.sent) if validate(p)]
    assert not bad, bad[:3]


# -- configuration -------------------------------------------------------------- #

def test_shipped_config_has_the_glide_slope_disabled():
    """G8: off until a real glide slope has been decoded."""
    cfg = load(ROOT / "config" / "rb-vor.json")
    assert cfg.gs.enabled is False
    assert BASE["gs"]["enabled"] is False


def test_gs_enabled_requires_loc_enabled(tmp_path):
    raw = copy.deepcopy(BASE)
    raw["loc"] = {"enabled": False}
    raw["gs"] = {"enabled": True}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match=r"gs.*requires.*loc\.enabled"):
        load(p)


def test_a_config_without_a_gs_block_loads_with_the_glide_slope_off(tmp_path):
    raw = copy.deepcopy(BASE)
    raw.pop("gs")
    p = tmp_path / "old.json"
    p.write_text(json.dumps(raw))
    assert load(p).gs.enabled is False


def test_shipped_acquisition_default_covers_the_legal_tolerance_plus_a_dongle():
    """A41: 0.005 % at 335 MHz is 16.75 kHz; 20 ppm at 330 MHz adds 6.6 kHz."""
    assert load(ROOT / "config" / "rb-vor.json").gs.dsp.acquire_hz >= 16_750 + 6_600


# -- disabled: v2.3 exactly ------------------------------------------------------- #

def test_disabled_glide_slope_leaves_the_wire_exactly_as_v23():
    seqs = []
    for gs_block in ("absent", {"enabled": False}):
        raw = copy.deepcopy(BASE)
        raw["sdr"].update(sample_rate_hz=FS, tuner_offset_hz=-FS // 4)
        raw["publish"]["keepalive_s"] = 0
        raw["loc"] = {"enabled": True}
        raw.pop("gs")
        if gs_block != "absent":
            raw["gs"] = gs_block
        cfg = Config(raw=raw)
        clock, sdr, tr = FakeClock(), FakeSdr(), RecordingTransport()
        c = Controller(cfg, sdr, Publisher(cfg.publish, tr, clock=clock), clock=clock)
        sky = Sky().loc()
        select(c, clock, 109.5)
        c.set_gs_select(1)
        run(c, clock, sky, 6.0, sdr)
        assert all(set(p) == VOR_KEYS | LOC_KEYS for p in tr.sent)
        assert sdr.tuned == [LOC_HZ]
        seqs.append(tr.sent)
    assert seqs[0] == seqs[1]


# -- two receivers ----------------------------------------------------------------- #

def test_two_receivers_decode_both_axes_together():
    c, clock, sdr, gsd, tr = make(two=True)
    sky = Sky().loc(ddm=0.05).gs(ddm=0.0875)
    select(c, clock, 109.5)
    run(c, clock, sky, 8.0, sdr, gsd)
    p = last(tr)
    assert p["locstatus"] >= 4 and p["gsstatus"] >= 4
    assert p["locfreqactive"] == 109_500 and p["gsfreqactive"] == 332_600
    assert p["gsshared"] == 0
    assert p["gsdev"] == pytest.approx(50, abs=6)        # +0.0875 / 0.175 = 50 %, fly up
    assert p["locdev"] == pytest.approx(32, abs=6)       # +0.05 / 0.155
    assert sdr.tuned == [LOC_HZ] and gsd.tuned == [GS_HZ]
    all_valid(tr)


def test_two_receivers_ignore_the_switch():
    c, clock, sdr, gsd, tr = make(two=True)
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 6.0, sdr, gsd)
    c.set_gs_select(1)
    run(c, clock, sky, 3.0, sdr, gsd)
    assert sdr.tuned == [LOC_HZ] and gsd.tuned == [GS_HZ]
    assert last(tr)["locstatus"] >= 4 and last(tr)["gsstatus"] >= 4


def test_gs_sense_negative_ddm_is_fly_down():
    c, clock, sdr, gsd, tr = make(two=True)
    sky = Sky().loc().gs(ddm=-0.0875)
    select(c, clock, 109.5)
    run(c, clock, sky, 8.0, sdr, gsd)
    assert last(tr)["gsdev"] == pytest.approx(-50, abs=6)


def test_each_axis_fails_alone_good_loc_noise_gs():
    c, clock, sdr, gsd, tr = make(two=True)
    sky = Sky().loc().noise_at(GS_HZ)
    select(c, clock, 109.5)
    run(c, clock, sky, 12.0, sdr, gsd)
    assert last(tr)["locstatus"] >= 4
    assert all(p["gsstatus"] < 4 and p["gsdev"] == 0 for p in tr.sent)
    all_valid(tr)


def test_each_axis_fails_alone_noise_loc_good_gs():
    c, clock, sdr, gsd, tr = make(two=True)
    sky = Sky().noise_at(LOC_HZ).gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 12.0, sdr, gsd)
    assert last(tr)["gsstatus"] >= 4
    assert all(p["locstatus"] < 4 and p["locdev"] == 0 for p in tr.sent)


@pytest.mark.parametrize("level,seed", [(0.02, 0), (0.1, 1), (0.3, 2), (0.1, 3)])
def test_noise_on_both_receivers_is_never_a_usable_ils(level, seed):
    """Plan 4.1: lateral centred, vertical centred, both 'valid' would be the
    worst output this receiver could produce."""
    c, clock, sdr, gsd, tr = make(two=True)
    sky = Sky().noise_at(LOC_HZ, seed=seed, level=level).noise_at(GS_HZ, seed=seed + 50,
                                                                  level=level)
    select(c, clock, 109.5)
    run(c, clock, sky, 20.0, sdr, gsd)
    assert all(p["gsstatus"] < 4 and p["locstatus"] < 4 for p in tr.sent)
    assert all(p["gsdev"] == 0 and p["locdev"] == 0 for p in tr.sent)


def test_gs_dongle_busy_leaves_the_localizer_alone_and_is_retried():
    c, clock, sdr, gsd, tr = make(two=True)
    gsd.refuse = 2                       # held by the radio for two attempts
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 6.0, sdr, gsd)
    p = last(tr)
    assert p["locstatus"] >= 4
    assert p["gsstatus"] == 1 and p["gsfreqactive"] == 332_600   # NO_SDR, v2.3 5
    assert c.gs_rx.state is State.NO_SDR
    run(c, clock, sky, 12.0, sdr, gsd)   # retried every retry_s; opens on the third
    assert last(tr)["gsstatus"] >= 4 and gsd.tuned == [GS_HZ]
    assert sdr.tuned == [LOC_HZ]         # the localizer never retuned
    all_valid(tr)


def test_nav_dongle_busy_does_not_stop_the_glide_slope():
    c, clock, sdr, gsd, tr = make(two=True)
    sdr.refuse = 99
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 8.0, sdr, gsd)
    assert last(tr)["locstatus"] == 1 and last(tr)["gsstatus"] >= 4


def test_vor_after_an_ils_zeroes_both_halves_and_frees_the_gs_dongle():
    c, clock, sdr, gsd, tr = make(two=True)
    sky = Sky().loc().gs().vor()
    select(c, clock, 109.5)
    run(c, clock, sky, 6.0, sdr, gsd)
    n = len(tr.sent)
    select(c, clock, 109.4)
    retune = tr.sent[n]
    assert retune["vorstatus"] == 2 and retune["vorfreqactive"] == 109_400
    assert all(retune[k] == 0 for k in LOC_KEYS | GS_KEYS)
    assert gsd.closed >= 1 and not gsd.open
    run(c, clock, sky, 6.0, sdr, gsd)
    assert last(tr)["vorstatus"] >= 4 and last(tr)["gsfreqactive"] == 0
    all_valid(tr)


def test_no_frequency_releases_both_dongles():
    c, clock, sdr, gsd, tr = make(two=True)
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 4.0, sdr, gsd)
    c.set_channel(None)
    clock.advance(0.2)
    c.tick()
    assert not sdr.open and not gsd.open
    assert all(last(tr)[k] == 0 for k in LOC_KEYS | GS_KEYS)


def test_a_localizer_without_a_paired_glide_path_cannot_exist_but_vor_has_none():
    """Every localizer channel has a pair (A39); a VOR channel has none, and the
    gs* half stays zero there with gsshared 0."""
    c, clock, sdr, gsd, tr = make(two=True)
    sky = Sky().vor()
    select(c, clock, 109.4)
    run(c, clock, sky, 4.0, sdr, gsd)
    assert gsd.tuned == []
    assert all(p["gsfreqactive"] == 0 and p["gsshared"] == 0 for p in tr.sent)


def test_clean_stop_publishes_the_glide_slope_idle():
    c, clock, sdr, gsd, tr = make(two=True)
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 8.0, sdr, gsd)
    assert last(tr)["gsstatus"] >= 4
    c.shutdown()
    assert last(tr)["gsstatus"] == 0 and last(tr)["gsdev"] == 0
    assert not gsd.open


# -- one receiver: the pilot's switch (plan 4.5) ------------------------------------ #

def test_one_receiver_starts_on_the_localizer_and_says_it_is_shared():
    c, clock, sdr, _, tr = make()
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 6.0, sdr)
    p = last(tr)
    assert p["locstatus"] >= 4 and p["gsstatus"] == 0 and p["gsfreqactive"] == 0
    assert p["gsshared"] == 1 and sdr.tuned == [LOC_HZ]
    all_valid(tr)


def test_switch_to_gs_darkens_the_localizer_in_the_same_post():
    c, clock, sdr, _, tr = make()
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 6.0, sdr)
    assert last(tr)["locstatus"] >= 4 and last(tr)["locdev"] != 0
    n = len(tr.sent)
    c.set_gs_select(1)
    run(c, clock, sky, 8.0, sdr)
    after = tr.sent[n:]
    first = after[0]
    assert first["locstatus"] == 0 and first["locdev"] == 0 and first["locfreqactive"] == 0
    assert first["gsstatus"] == 2 and first["gsfreqactive"] == 332_600
    assert all(p["locstatus"] < 4 and p["locdev"] == 0 for p in after)
    assert last(tr)["gsstatus"] >= 4 and last(tr)["gsshared"] == 1
    assert sdr.tuned == [LOC_HZ, GS_HZ]
    all_valid(tr)


def test_switch_back_to_loc_darkens_the_glide_slope_in_the_same_post():
    c, clock, sdr, _, tr = make()
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 6.0, sdr)
    c.set_gs_select(1)
    run(c, clock, sky, 8.0, sdr)
    assert last(tr)["gsstatus"] >= 4 and last(tr)["gsdev"] != 0
    n = len(tr.sent)
    c.set_gs_select(0)
    run(c, clock, sky, 8.0, sdr)
    after = tr.sent[n:]
    assert after[0]["gsstatus"] == 0 and after[0]["gsfreqactive"] == 0
    assert after[0]["locstatus"] == 2
    assert all(p["gsstatus"] < 4 and p["gsdev"] == 0 for p in after)
    assert last(tr)["locstatus"] >= 4
    assert sdr.tuned == [LOC_HZ, GS_HZ, LOC_HZ]
    assert sdr.closed == 0              # the dongle was handed over, never released
    all_valid(tr)


def test_a_repeated_switch_is_a_no_op_even_mid_retune():
    """A16 for gsselect: the bridge re-asserts its state."""
    c, clock, sdr, _, tr = make()
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 4.0, sdr)
    c.set_gs_select(1)
    for _ in range(20):
        run(c, clock, sky, 0.25, sdr)
        c.set_gs_select(1)
    assert sdr.tuned == [LOC_HZ, GS_HZ]
    assert last(tr)["gsstatus"] >= 4


def test_a_new_station_starts_on_the_localizer_even_if_the_bridge_still_says_gs():
    c, clock, sdr, _, tr = make()
    sky = Sky().loc().gs().loc(hz=110_100_000).gs(hz=334_400_000)
    select(c, clock, 109.5)
    run(c, clock, sky, 3.0, sdr)
    c.set_gs_select(1)
    run(c, clock, sky, 6.0, sdr)
    assert sdr.tuned[-1] == GS_HZ
    select(c, clock, 110.1)
    for _ in range(12):                  # the bridge keeps re-asserting gsselect 1
        c.set_gs_select(1)
        run(c, clock, sky, 0.5, sdr)
    assert sdr.tuned[-1] == 110_100_000
    assert last(tr)["locstatus"] >= 4 and last(tr)["gsstatus"] == 0
    assert last(tr)["gsshared"] == 1


def test_a_restarted_daemon_returns_to_the_pilots_choice():
    """The read on connect (A31) delivers vorfreq and gsselect in one frame;
    vorfreq is applied first and resets, gsselect then restores."""
    c, clock, sdr, _, tr = make()
    sky = Sky().loc().gs()
    c.set_channel(109.5)
    c.set_gs_select(1)
    clock.advance(0.5)
    c.tick()
    run(c, clock, sky, 8.0, sdr)
    assert sdr.tuned == [GS_HZ]
    assert last(tr)["gsstatus"] >= 4 and last(tr)["locstatus"] == 0


def test_the_switch_is_ignored_on_a_vor_channel():
    c, clock, sdr, _, tr = make()
    sky = Sky().vor()
    select(c, clock, 109.4)
    run(c, clock, sky, 3.0, sdr)
    c.set_gs_select(1)
    run(c, clock, sky, 3.0, sdr)
    assert sdr.tuned == [109_400_000] and last(tr)["vorstatus"] >= 4


@pytest.mark.parametrize("junk", [2, -1, "x", 0.5, None])
def test_junk_gsselect_changes_nothing(junk):
    c, clock, sdr, _, tr = make()
    sky = Sky().loc()
    select(c, clock, 109.5)
    run(c, clock, sky, 3.0, sdr)
    c.set_gs_select(junk)
    run(c, clock, sky, 2.0, sdr)
    assert sdr.tuned == [LOC_HZ]


def test_shared_gs_dongle_refused_is_no_sdr_and_retried():
    c, clock, sdr, _, tr = make()
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 4.0, sdr)
    sdr.refuse = 1
    c.set_gs_select(1)
    run(c, clock, sky, 1.0, sdr)
    p = last(tr)
    assert p["gsstatus"] == 1 and p["gsfreqactive"] == 332_600 and p["locstatus"] == 0
    assert c.state is State.NO_SDR
    run(c, clock, sky, 10.0, sdr)
    assert last(tr)["gsstatus"] >= 4
    all_valid(tr)


def test_noise_on_the_shared_receiver_never_makes_a_usable_glide_slope():
    c, clock, sdr, _, tr = make()
    sky = Sky().loc().noise_at(GS_HZ)
    select(c, clock, 109.5)
    run(c, clock, sky, 3.0, sdr)
    c.set_gs_select(1)
    run(c, clock, sky, 20.0, sdr)
    assert all(p["gsstatus"] < 4 and p["gsdev"] == 0 for p in tr.sent)


# -- the arrangement follows the allocation ---------------------------------------- #

def test_a_second_dongle_plugged_in_is_used_at_the_next_tune_attempt():
    plugged = {"gs": False}
    c, clock, sdr, gsd, tr = make(two=True, allocate=lambda: plugged["gs"])
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 4.0, sdr, gsd)
    assert last(tr)["gsshared"] == 1 and gsd.tuned == []
    plugged["gs"] = True
    c.set_gs_select(1)                   # the pilot presses GS: a tune attempt
    run(c, clock, sky, 8.0, sdr, gsd)
    p = last(tr)
    assert p["gsshared"] == 0 and p["gsstatus"] >= 4 and p["locstatus"] >= 4
    assert gsd.tuned == [GS_HZ] and sdr.tuned == [LOC_HZ]
    all_valid(tr)


def test_a_dedicated_dongle_that_goes_away_turns_the_receiver_shared():
    plugged = {"gs": True}
    c, clock, sdr, gsd, tr = make(two=True, allocate=lambda: plugged["gs"])
    gsd.refuse = 99
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 2.0, sdr, gsd)
    assert last(tr)["gsstatus"] == 1
    plugged["gs"] = False
    run(c, clock, sky, 8.0, sdr, gsd)
    p = last(tr)
    assert p["gsshared"] == 1 and p["gsstatus"] == 0 and p["locstatus"] >= 4
    all_valid(tr)


# -- logs ---------------------------------------------------------------------------- #

def test_glide_slope_has_its_own_solution_log(tmp_path):
    loc_log = SolutionLogger(tmp_path / "loc.csv", fields=LOC_FIELDS)
    gs_log = SolutionLogger(tmp_path / "gs.csv", fields=GS_FIELDS)
    rows = []
    c, clock, sdr, gsd, tr = make(two=True, loc_solution_sink=loc_log.write,
                                  gs_solution_sink=lambda r: (rows.append(r),
                                                              gs_log.write(r)))
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 6.0, sdr, gsd)
    loc_log.close()
    gs_log.close()
    assert rows and rows[-1].channel_khz == 332_600 and rows[-1].loc_channel_khz == 109_500
    assert rows[-1].valid and rows[-1].ddm == pytest.approx(0.0875, abs=0.01)
    head = (tmp_path / "gs.csv").read_text().splitlines()[0].split(",")
    assert head == list(GS_FIELDS)
    # glide-slope events are not localizer rows
    n_loc = len((tmp_path / "loc.csv").read_text().splitlines()) - 1
    n_gs = len((tmp_path / "gs.csv").read_text().splitlines()) - 1
    assert n_loc < len(tr.sent) and n_gs < len(tr.sent)


# -- IQ recording (A46) ---------------------------------------------------------------- #

def _recorders(tmp_path):
    from rbvor.record import IQMeta, RingRecorder
    meta = IQMeta(format="cu8", sample_rate_hz=FS, center_freq_hz=0, nav_freq_hz=0,
                  tuner_offset_hz=-FS // 4)
    nav = RingRecorder(meta, tmp_path / "iq", max_seconds=3.0, fmt="cu8")
    gs = RingRecorder(meta, tmp_path / "iq", max_seconds=3.0, fmt="cu8")
    return nav, gs


def _gs_captures(tmp_path):
    return sorted((tmp_path / "iq").glob("capture_*_332600.cu8"))


def test_a_glide_slope_flag_writes_glide_path_iq_on_its_own_dongle(tmp_path):
    """Two receivers: a glide slope that loses its signal writes the IQ of the
    glide-path dongle, named by the glide-path channel, and gs.csv names it.
    The localizer, still good, writes nothing."""
    from rbvor.record import iq_blocks, read_sidecar
    nav, gsr = _recorders(tmp_path)
    rows = []
    c, clock, sdr, gsd, tr = make(two=True, recorder=nav, gs_recorder=gsr,
                                  gs_solution_sink=rows.append)
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 6.0, sdr, gsd)
    assert last(tr)["gsstatus"] >= 4 and gsr.captures_written == 0
    sky.noise_at(GS_HZ, level=0.1)
    run(c, clock, sky, 4.0, sdr, gsd)
    gsr.flush()
    assert gsr.captures_written >= 1 and nav.captures_written == 0
    caps = _gs_captures(tmp_path)
    assert caps, sorted(p.name for p in (tmp_path / "iq").iterdir())
    meta = read_sidecar(caps[0])
    assert meta.nav_freq_hz == GS_HZ and meta.center_freq_hz == GS_HZ - FS // 4
    assert "glide slope" in meta.notes
    assert sum(b.size for b in iq_blocks(caps[0])) == meta.samples > 0
    assert any(r.iq_ref == caps[0].name for r in rows if r.gsstatus < 4)


def test_a_glide_slope_flag_on_a_shared_dongle_writes_glide_path_iq(tmp_path):
    """One receiver, GS selected: the lent nav dongle's IQ goes to the glide
    slope's ring, not the nav ring."""
    nav, gsr = _recorders(tmp_path)
    c, clock, sdr, _, tr = make(recorder=nav, gs_recorder=gsr)
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 4.0, sdr)
    c.set_gs_select(1)
    run(c, clock, sky, 6.0, sdr)
    assert last(tr)["gsstatus"] >= 4
    nav_held = nav._held
    sky.noise_at(GS_HZ, level=0.1)
    run(c, clock, sky, 4.0, sdr)
    gsr.flush()
    assert gsr.captures_written >= 1 and nav.captures_written == 0
    assert nav._held == nav_held          # GS samples never entered the nav ring
    assert _gs_captures(tmp_path)


def test_a_good_glide_slope_writes_nothing_and_a_retune_empties_its_ring(tmp_path):
    nav, gsr = _recorders(tmp_path)
    c, clock, sdr, gsd, tr = make(two=True, recorder=nav, gs_recorder=gsr)
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 6.0, sdr, gsd)
    assert gsr._held > 0 and gsr.captures_written == 0
    c.set_channel(0)                      # deselect: the glide slope stops
    clock.advance(0.5)
    c.tick()
    select(c, clock, 109.5)               # retune: the ring starts empty
    assert gsr._held == 0
    assert gsr.captures_written == 0 and not list(tmp_path.rglob("*.cu8"))


def test_the_switch_is_debounced_like_a_frequency():
    """A pilot tapping LOC/GS/LOC within the debounce window costs no retune."""
    c, clock, sdr, _, tr = make()
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    run(c, clock, sky, 4.0, sdr)
    c.set_gs_select(1)
    clock.advance(0.05)
    c.tick()
    c.set_gs_select(0)
    run(c, clock, sky, 2.0, sdr)
    assert sdr.tuned == [LOC_HZ]
    assert last(tr)["locstatus"] >= 4


@pytest.mark.parametrize("two", [True, False])
def test_a_glide_slope_dongle_that_goes_silent_never_leaves_a_usable_marker(two):
    """A45: a dongle pulled mid-reception delivers nothing; without a check the
    last usable gsdev would stay on the bridge (the A30 gap, per receiver)."""
    c, clock, sdr, gsd, tr = make(two=two)
    sky = Sky().loc().gs()
    select(c, clock, 109.5)
    if not two:
        run(c, clock, sky, 3.0, sdr)
        c.set_gs_select(1)
    run(c, clock, sky, 8.0, sdr, gsd)
    assert last(tr)["gsstatus"] >= 4
    dead = gsd if two else sdr
    dead.open = False                    # pulled: no more blocks
    run(c, clock, sky, 4.0, sdr, gsd)
    assert last(tr)["gsstatus"] == 1 and last(tr)["gsdev"] == 0
    assert "no samples" in c.gs_rx.reason
