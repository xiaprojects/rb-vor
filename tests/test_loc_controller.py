"""L4/L5 -- the localizer through the state machine and onto the wire.

CLAUDE.md A33, A37, A38, D9; RB-VOR-IF v2.3. Driven with a real `Publisher`
and `RecordingTransport`, so every assertion is about the payload a consumer
would receive, and every payload is checked against `tools/mock_bridge.py`'s
validator.

The property that matters most (plan 4.1): noise only, through the full
state machine, at several levels and seeds, never reaches a usable
`locstatus`, and `locdev` is 0 throughout.
"""

from __future__ import annotations

import copy
import csv
import json
from pathlib import Path

import numpy as np
import pytest

from rbvor.config import Config, load
from rbvor.controller import Controller, State
from rbvor.loc import ddm_to_dev_pct
from rbvor.publisher import (
    LOC_KEYS,
    LocWire,
    Publisher,
    PublishPolicy,
    RecordingTransport,
)
from rbvor.sdr import SdrError
from rbvor.solutionlog import LOC_FIELDS, SolutionLogger
from tools.loc_gen import LocParams
from tools.loc_gen import generate_blocks as loc_blocks
from tools.mock_bridge import VOR_KEYS, validate
from tools.vor_gen import VorParams
from tools.vor_gen import generate_blocks as vor_blocks

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "config" / "rb-vor.json").read_text())
FS = 256_000


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeSdr:
    def __init__(self):
        self.tuned, self.closed = [], 0

    def set_frequency(self, nav_hz):
        self.tuned.append(nav_hz)

    def close(self):
        self.closed += 1


def make(loc=True, calibrated=True, loc_block=None, freq=None, **ctl):
    raw = copy.deepcopy(BASE)
    raw["sdr"]["sample_rate_hz"] = FS
    raw["sdr"]["tuner_offset_hz"] = -FS // 4
    raw["publish"]["keepalive_s"] = 0
    if not calibrated:
        raw["bearing"].update(phase_sign=None, phase_offset_deg=None)
    if loc_block is not None:
        raw["loc"] = loc_block
    elif loc:
        raw["loc"] = {"enabled": True}
    else:
        raw.pop("loc", None)
    if freq:
        raw["frequency"].update(freq)
    cfg = Config(raw=raw)
    clock, sdr, tr = FakeClock(), FakeSdr(), RecordingTransport()
    pub = Publisher(cfg.publish, tr, clock=clock)
    return Controller(cfg, sdr, pub, clock=clock, **ctl), clock, sdr, tr


def tune(c, clock, mhz):
    c.set_channel(mhz)
    clock.advance(0.5)
    c.tick()                       # debounce -> retune (TUNING)
    clock.advance(0.5)
    c.tick()                       # settle -> ACQUIRING


def run(c, clock, blocks, block_s):
    for b in blocks:
        c.push(b)
        clock.advance(block_s)
        c.tick()


def loc_signal(seconds=8.0, **kw):
    kw.setdefault("snr_db", 25.0)
    p = LocParams(fs=FS, offset_hz=-FS // 4, seconds=seconds, block=1 << 15, **kw)
    return list(loc_blocks(p)), p.block / FS


def all_valid(tr):
    bad = [(i, p, validate(p)) for i, p in enumerate(tr.sent) if validate(p)]
    assert not bad, bad[:3]


# --------------------------------------------------------------------------- #
# disabled: exactly as before
# --------------------------------------------------------------------------- #

def test_shipped_config_has_the_localizer_disabled():
    """Plan L7: off until a real localizer has been decoded."""
    cfg = load(ROOT / "config" / "rb-vor.json")
    assert cfg.loc.enabled is False
    assert BASE["loc"]["enabled"] is False


def test_a_config_without_a_loc_block_loads_with_the_localizer_off(tmp_path):
    raw = copy.deepcopy(BASE)
    raw.pop("loc")
    p = tmp_path / "old.json"
    p.write_text(json.dumps(raw))
    cfg = load(p)
    assert cfg.loc.enabled is False
    assert cfg.loc.solution_csv == str(Path(raw["log"]["solution_csv"]).with_name("loc.csv"))


@pytest.mark.parametrize("block", [None, {"enabled": False}])
def test_disabled_localizer_is_rejected_exactly_as_before_and_the_wire_is_v22(block):
    c, clock, sdr, tr = make(loc=False, loc_block=block)
    c.set_channel(109.5)
    clock.advance(1.0)
    c.tick()
    assert c.state is State.INVALID_FREQ and "localiser" in c.last_reason
    assert sdr.tuned == []
    assert all(set(p) == VOR_KEYS for p in tr.sent)
    c.shutdown()                                   # disabled: publishes nothing new
    assert all(set(p) == VOR_KEYS for p in tr.sent)


def test_loc_block_keys_are_validated(tmp_path):
    raw = copy.deepcopy(BASE)
    raw["loc"] = {"enabled": True, "integraton_s": 1.0}
    p = tmp_path / "typo.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(Exception, match="integraton_s"):
        load(p)
    raw["loc"] = {"enabled": True, "integration_s": 0.51}
    p.write_text(json.dumps(raw))
    with pytest.raises(Exception, match="1/30"):
        load(p)


# --------------------------------------------------------------------------- #
# enabled: tuning, tracking, the payload
# --------------------------------------------------------------------------- #

def test_localizer_channel_is_tuned_and_vor_fields_read_as_rejected():
    c, clock, sdr, tr = make()
    c.set_channel(109.5)
    clock.advance(0.5)
    c.tick()
    assert c.state is State.TUNING and c.mode == "loc"
    assert sdr.tuned == [109_500_000]
    p = tr.sent[-1]
    assert p["locstatus"] == 2 and p["locfreqactive"] == 109_500
    assert all(p[k] == 0 for k in VOR_KEYS)
    all_valid(tr)


@pytest.mark.parametrize("ddm", [0.0775, -0.04, 0.0])
def test_a_localizer_is_tracked_and_published(ddm):
    c, clock, _, tr = make()
    tune(c, clock, 109.5)
    blocks, bs = loc_signal(ddm=ddm)
    run(c, clock, blocks, bs)
    assert c.state is State.TRACKING
    p = tr.sent[-1]
    assert p["locstatus"] == 5
    assert abs(p["locdev"] - ddm_to_dev_pct(ddm)) <= 2
    assert abs(p["locddm"] - 1000 * ddm) <= 4
    assert 37 <= p["locsdm"] <= 41
    assert p["locsnr"] >= 30
    assert all(p[k] == 0 for k in VOR_KEYS)
    all_valid(tr)


def test_the_localizer_needs_no_bearing_calibration():
    """A2 concerns a phase constant a localizer does not have."""
    c, clock, _, tr = make(calibrated=False)
    tune(c, clock, 110.3)
    blocks, bs = loc_signal(ddm=0.05)
    run(c, clock, blocks, bs)
    assert c.state is State.TRACKING
    assert abs(tr.sent[-1]["locdev"] - ddm_to_dev_pct(0.05)) <= 2      # A38 deadband


def test_warming_up_is_acquiring_not_no_signal():
    """A14: the first windows cannot be judged; that is not a dead signal."""
    c, clock, _, tr = make()
    tune(c, clock, 109.5)
    blocks, bs = loc_signal(seconds=1.6)
    states = []
    for b in blocks:
        c.push(b)
        states.append(c.state)
    assert State.NO_SIGNAL not in states
    assert all(p["locdev"] == 0 for p in tr.sent)


def test_a_weak_localizer_is_flagged_and_never_shows_a_deviation():
    c, clock, _, tr = make()
    tune(c, clock, 109.5)
    blocks, bs = loc_signal(seconds=10.0, ddm=0.1, snr_db=-3.0)
    run(c, clock, blocks, bs)
    assert all(p["locstatus"] < 4 for p in tr.sent)
    assert all(p["locdev"] == 0 for p in tr.sent)
    assert c.state is State.NO_SIGNAL
    all_valid(tr)


def _noise(seconds, rms, seed, block=1 << 15):
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    for i in range(0, n, block):
        m = min(block, n - i)
        z = rng.normal(0, rms, m) + 1j * rng.normal(0, rms, m)
        q = np.clip(np.round(np.stack([z.real, z.imag]) * 127.5 + 127.5), 0, 255)
        yield ((q[0] - 127.5) / 127.5 + 1j * (q[1] - 127.5) / 127.5).astype(np.complex64)


@pytest.mark.parametrize("rms,seed", [(0.02, 11), (0.05, 12), (0.1, 13), (0.2, 14),
                                      (0.3, 15), (0.1, 16)])
def test_noise_only_never_reaches_a_usable_status(rms, seed):
    """THE acceptance test of the feature (plan 4.1). Noise decodes as DDM ~ 0
    -- a perfect approach -- so a decoder that derived validity from the DDM
    would publish 'on the centreline' here. Full state machine, cu8-quantised
    noise like a dongle, 20 s per case."""
    c, clock, _, tr = make()
    tune(c, clock, 109.5)
    bs = (1 << 15) / FS
    run(c, clock, _noise(20.0, rms, seed), bs)
    assert c._last_loc_obs is not None                 # it really was decoding
    assert all(p["locstatus"] < 4 for p in tr.sent)
    assert all(p["locdev"] == 0 for p in tr.sent)
    assert c.state in (State.NO_SIGNAL, State.ACQUIRING)
    all_valid(tr)


def test_signal_lost_drops_the_deviation_at_once_never_holds_it():
    c, clock, _, tr = make()
    tune(c, clock, 109.5)
    blocks, bs = loc_signal(ddm=0.1)
    run(c, clock, blocks, bs)
    assert tr.sent[-1]["locdev"] != 0
    n = len(tr.sent)
    run(c, clock, _noise(6.0, 0.1, 3), (1 << 15) / FS)
    after = tr.sent[n:]
    assert after and all(p["locdev"] == 0 for p in after if p["locstatus"] < 4)
    assert tr.sent[-1]["locstatus"] < 4


def test_overrun_window_is_not_used():
    c, clock, _, tr = make()
    tune(c, clock, 109.5)
    blocks, bs = loc_signal(ddm=0.1)
    run(c, clock, blocks, bs)
    c.push(blocks[0], gap=True)
    for b in blocks[:20]:
        c.push(b)
        if c.state is State.NO_SIGNAL:
            break
    assert c.state is State.NO_SIGNAL and "overrun" in c.last_reason
    assert tr.sent[-1]["locdev"] == 0


# --------------------------------------------------------------------------- #
# retune, flush, repeat, release, NO_SDR
# --------------------------------------------------------------------------- #

def test_switching_vor_and_loc_is_atomic_and_flushes_everything():
    """A26 across modes: one station's numbers are never attributed to the
    next, and no payload ever claims both modes."""
    c, clock, _, tr = make()
    tune(c, clock, 109.5)
    blocks, bs = loc_signal(ddm=0.1)
    run(c, clock, blocks, bs)
    assert tr.sent[-1]["locstatus"] == 5
    n = len(tr.sent)

    c.set_channel(109.4)                              # to a VOR
    clock.advance(0.5)
    c.tick()
    first = tr.sent[n]
    assert c.mode == "vor" and first["vorstatus"] == 2 and first["vorfreqactive"] == 109_400
    assert all(first[k] == 0 for k in LOC_KEYS)
    vp = VorParams(radial_deg=137, fs=FS, offset_hz=-FS // 4, seconds=6.0, snr_db=25.0,
                   block=1 << 15)
    run(c, clock, list(vor_blocks(vp)), (1 << 15) / FS)
    assert c.state is State.TRACKING
    assert all(p[k] == 0 for p in tr.sent[n:] for k in LOC_KEYS)
    m = len(tr.sent)

    c.set_channel(111.95)                             # back to a localizer
    clock.advance(0.5)
    c.tick()
    assert tr.sent[m]["locstatus"] == 2 and tr.sent[m]["locsdm"] == 0
    assert all(tr.sent[m][k] == 0 for k in VOR_KEYS)
    for p in tr.sent:
        assert not (p["vorfreqactive"] and p["locfreqactive"])
    all_valid(tr)


def test_a_repeated_localizer_frequency_is_a_no_op_at_every_stage():
    """A16, which only a bridge re-asserting its state at 2 Hz revealed."""
    c, clock, sdr, _ = make()
    c.set_channel(109.5)
    clock.advance(0.5)
    c.tick()
    for _ in range(6):                                # TUNING, then ACQUIRING
        c.set_channel(109.5)
        clock.advance(0.25)
        c.tick()
    assert sdr.tuned == [109_500_000]
    assert c.state is State.ACQUIRING


def test_vorfreq_zero_releases_the_dongle_from_a_localizer():
    """RB-01's OFF posts vorfreq 0 (the float bridge has no null)."""
    c, clock, sdr, tr = make()
    tune(c, clock, 109.5)
    closed = sdr.closed
    c.set_channel(0.0)
    assert c.state is State.INVALID_FREQ and sdr.closed > closed
    p = tr.sent[-1]
    assert p["locstatus"] == 0 and p["locfreqactive"] == 0 and p["vorstatus"] == 0
    all_valid(tr)


def test_denied_and_allowlisted_channels_with_the_localizer_enabled():
    c, clock, sdr, _ = make(freq={"denylist_mhz": [109.5]})
    c.set_channel(109.5)
    assert c.state is State.INVALID_FREQ and "denylist" in c.last_reason
    c, clock, sdr, _ = make(freq={"allowlist_mhz": [109.5]})
    c.set_channel(109.5)
    clock.advance(0.5)
    c.tick()
    assert c.mode == "vor"                    # the operator forced it to be a VOR


class RefusingSdr(FakeSdr):
    def __init__(self, refusals):
        super().__init__()
        self.refusals, self.attempts = refusals, 0

    def set_frequency(self, nav_hz):
        self.attempts += 1
        if self.attempts <= self.refusals:
            raise SdrError("usb_claim_interface error -6")
        super().set_frequency(nav_hz)


def test_no_sdr_on_a_localizer_channel():
    """A30 unchanged: the channel stays selected, status 1, retried."""
    c, clock, _, tr = make(sdr_retry_s=5.0)
    c.sdr = RefusingSdr(2)
    c.set_channel(109.5)
    clock.advance(0.5)
    c.tick()
    assert c.state is State.NO_SDR
    p = tr.sent[-1]
    assert p["locstatus"] == 1 and p["locfreqactive"] == 109_500 and p["locdev"] == 0
    for _ in range(3):
        clock.advance(5.0)
        c.tick()
    assert c.sdr.tuned == [109_500_000] and c.mode == "loc"
    all_valid(tr)


def test_the_read_on_connect_brings_back_a_localizer():
    """A31 unchanged: the state read is handled as a frame, LOC included."""
    import asyncio

    from test_bridge_io import Rb01Standin

    from rbvor.bridge_client import BridgeClient

    c, clock, sdr, _ = make()

    async def scenario():
        async with Rb01Standin(payload=None) as bridge:
            async def fetch():
                return '{"vorfreq":109.5}'
            client = BridgeClient(dict(BASE["bridge"], ws_url=bridge.url,
                                       ws_reconnect_min_s=0.01, ws_reconnect_max_s=0.02),
                                  on_channel=c.set_channel, fetch_state=fetch)
            task = asyncio.create_task(client.run())
            for _ in range(300):
                if c._pending:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    clock.advance(0.5)
    c.tick()
    assert c.mode == "loc" and sdr.tuned == [109_500_000]


def test_clean_stop_publishes_locstatus_zero():
    c, clock, _, tr = make()
    tune(c, clock, 109.5)
    blocks, bs = loc_signal(ddm=0.0)
    run(c, clock, blocks, bs)
    assert tr.sent[-1]["locstatus"] == 5 and abs(tr.sent[-1]["locdev"]) <= 2   # centred
    c.shutdown()
    assert tr.sent[-1]["locstatus"] == 0 and tr.sent[-1]["locfreqactive"] == 0
    all_valid(tr)


# --------------------------------------------------------------------------- #
# logs
# --------------------------------------------------------------------------- #

def test_localizer_solutions_go_to_their_own_log(tmp_path):
    """A separate file, because SolutionLogger appends under whatever header
    an existing file has (next test)."""
    vor_rows, loc_rows = [], []
    c, clock, _, _ = make(solution_sink=vor_rows.append, loc_solution_sink=loc_rows.append)
    tune(c, clock, 109.5)
    blocks, bs = loc_signal(seconds=4.0, ddm=0.05)
    run(c, clock, blocks, bs)
    assert loc_rows and not vor_rows
    lg = SolutionLogger(tmp_path / "loc.csv", fields=LOC_FIELDS)
    for r in loc_rows:
        lg.write(r)
    lg.close()
    rows = list(csv.DictReader((tmp_path / "loc.csv").open()))
    assert tuple(rows[0]) == LOC_FIELDS
    assert rows[-1]["locstatus"] == "5" and float(rows[-1]["ddm"]) == pytest.approx(0.05, abs=3e-3)


def test_solution_logger_never_rewrites_an_existing_header(tmp_path):
    """Why the localizer has its own file (A37): appending to a file with a
    different header writes rows that do not match it, silently."""
    p = tmp_path / "vor.csv"
    p.write_text("a,b,c\n")
    lg = SolutionLogger(p, fields=LOC_FIELDS)
    assert p.read_text().splitlines()[0] == "a,b,c"
    lg.close()


# --------------------------------------------------------------------------- #
# publisher details
# --------------------------------------------------------------------------- #

def policy():
    return PublishPolicy({"keepalive_s": 0, "hold_s": 3.0, "bearing_ref": "from"},
                         clock=FakeClock())


def test_locdev_is_gated_by_validity_and_the_rest_is_not():
    p = policy()
    d = p.decide(None, state="NO_SIGNAL", status=0,
                 loc=LocWire(status=1, ddm=0.05, sdm=0.39, snr_db=33.2, freq_khz=109500))
    assert d.payload["locdev"] == 0
    assert d.payload["locddm"] == 50 and d.payload["locsdm"] == 39 and d.payload["locsnr"] == 33


@pytest.mark.parametrize("seed", range(5))
def test_hysteresis_never_holds_the_needle_more_than_two_percent_off(seed):
    rng = np.random.default_rng(seed)
    p = policy()
    walk = np.cumsum(rng.normal(0, 0.002, 400)) + 0.03
    for x in walk:
        p.decide(None, state="TRACKING", status=0,
                 loc=LocWire(status=5, ddm=float(x), sdm=0.4, snr_db=40, freq_khz=109500,
                             usable=True))
        dev = p._last_payload["locdev"]            # what the consumer now holds
        if abs(x) <= 0.155:
            assert abs(dev - 100 * x / 0.155) <= 2.0 + 1e-9
        else:
            assert abs(dev) >= 99


def test_hysteresis_stops_a_steady_value_from_posting_on_every_window():
    p = policy()
    sent = 0
    for i in range(100):
        x = 0.0775 + (0.0008 if i % 2 else -0.0008)  # flips the last digit of locdev
        d = p.decide(None, state="TRACKING", status=0,
                     loc=LocWire(status=5, ddm=x, sdm=0.4, snr_db=40, freq_khz=109500,
                                 usable=True))
        sent += d.send
    # the first payload, then at most one settling move into the deadband
    assert sent <= 2


def test_mock_bridge_rejects_what_v23_forbids():
    good = {**dict.fromkeys(VOR_KEYS, 0), "locstatus": 5, "locdev": 12, "locddm": 19,
            "locsdm": 40, "locsnr": 35, "locfreqactive": 109500}
    assert validate(good) == []
    assert validate({**good, "locstatus": 3})                     # deviation not usable
    assert validate({**good, "vorfreqactive": 109400})            # two modes
    assert validate({**good, "locfreqactive": 109400})            # not a LOC channel
    assert validate({**good, "locdev": 101})
    del good["locsnr"]
    assert validate(good)                                         # partial set
