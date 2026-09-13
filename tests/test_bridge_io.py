"""M7 -- T5 and T6: bridge input, REST output, and the failure paths.

The interface contract is asserted literally: the v1.0 payload is exactly two
integer fields. A consumer written from `docs/RB-VOR-IF-v1.0.md` alone must not
be surprised by anything we send.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.bridge_client import BridgeClient, parse_frame  # noqa: E402
from rbvor.config import Config  # noqa: E402
from rbvor.controller import Controller, State  # noqa: E402
from rbvor.publisher import Publisher, PublishPolicy, RecordingTransport  # noqa: E402
from tools.mock_bridge import parse_script  # noqa: E402
from tools.vor_gen import VorParams, generate_blocks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "config" / "rb-vor.json").read_text())
FS = 256_000

# the all-fields-zero payload (idle / out of band), v2.2 shape
ZERO = {"vorradial": 0, "vorsnr": 0, "voram": 0, "vorstatus": 0, "vorsub": 0,
        "vorfreqactive": 0}
KEYS = set(ZERO)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeSdr:
    def __init__(self):
        self.tuned = []
        self.closed = 0

    def set_frequency(self, nav_hz):
        self.tuned.append(nav_hz)

    def close(self):
        self.closed += 1


def cfg_with(**overrides):
    raw = copy.deepcopy(BASE)
    raw["sdr"]["sample_rate_hz"] = FS
    raw["sdr"]["tuner_offset_hz"] = -FS // 4
    raw["bearing"].update(phase_sign=1, phase_offset_deg=-1.9657)
    for section, kv in overrides.items():
        raw[section].update(kv)
    return Config(raw=raw)


def policy(**kw):
    clock = FakeClock()
    p = dict(BASE["publish"])
    p.update(kw)
    return PublishPolicy(p, clock=clock), clock


# --------------------------------------------------------------------------- #
# inbound parsing -- tolerance is the requirement
# --------------------------------------------------------------------------- #

def test_the_example_payload_from_the_spec_parses():
    f = parse_frame('{"vorfreq":109.4,"gpsTrack":200,"vordir":120,"vorsignal":0}')
    assert f.ok and f.vorfreq == 109.4 and f.gps_track == 200.0


def test_unknown_fields_are_ignored_silently():
    """The bridge carries the whole platform's traffic and will grow fields."""
    f = parse_frame('{"vorfreq":109.4,"altitude":1200,"nested":{"a":[1,2]}}')
    assert f.ok and f.vorfreq == 109.4


def test_absent_null_and_junk_are_three_different_things():
    absent = parse_frame('{"gpsTrack":10}')
    assert absent.ok and "vorfreq" not in absent.raw_keys       # no change

    cleared = parse_frame('{"vorfreq":null}')
    assert cleared.ok and cleared.vorfreq is None
    assert "vorfreq" in cleared.raw_keys                        # go idle

    junk = parse_frame('{"vorfreq":"109.4"}')
    assert not junk.ok and "not a number" in junk.error          # complain


@pytest.mark.parametrize("text", [
    "", "not json", "[1,2,3]", '"string"', "42", "{", '{"vorfreq":}',
    '{"vorfreq":true}', '{"vorfreq":[109.4]}', '{"vorfreq":NaN}',
])
def test_malformed_frames_never_raise(text):
    f = parse_frame(text)
    assert not f.ok or f.vorfreq is None


def test_bad_frame_does_not_disconnect_or_change_channel():
    """One malformed message is not a reason to tear down a working connection."""
    seen = []
    c = BridgeClient(BASE["bridge"], on_channel=seen.append)
    c.handle_text('{"vorfreq":109.4}')
    c.handle_text("garbage")
    c.handle_text('{"vorfreq":"nope"}')
    c.handle_text('{"vorfreq":113.35}')
    assert seen == [109.4, 113.35]
    assert c.bad_frames == 2 and c.frames == 4


def test_gps_track_is_delivered_only_to_its_own_callback():
    """v1.0: gpsTrack is diagnostics only. It must not reach the bearing path."""
    channels, tracks = [], []
    c = BridgeClient(BASE["bridge"], on_channel=channels.append,
                     on_gps_track=tracks.append)
    c.handle_text('{"vorfreq":109.4,"gpsTrack":200}')
    assert channels == [109.4] and tracks == [200.0]


def test_gps_track_is_not_referenced_anywhere_in_the_bearing_path():
    """Structural guard: catches a future edit that quietly wires it in."""
    for mod in ("bearing.py", "dsp.py", "quality.py"):
        src = (ROOT / "rbvor" / mod).read_text()
        assert "gps" not in src.lower(), f"{mod} refers to GPS"


# outbound payload -- the v2.0 interface contract, literally
# --------------------------------------------------------------------------- #

def test_payload_has_exactly_the_three_v2_integer_fields():
    p, _ = policy()
    d = p.decide(137, 53.0, 0.30, state="TRACKING", valid=True)
    assert d.send
    assert set(d.payload) == KEYS
    assert all(isinstance(v, int) for v in d.payload.values())


def test_field_meanings_radial_snr_am():
    """vorradial=radial deg, vorsnr=SNR dB, voram=AM depth x100."""
    p, _ = policy()
    d = p.decide(137, 53.4, 0.283, state="TRACKING", valid=True, status=5,
                 sub_level=0.29, freq_khz=109400)
    assert d.payload == {"vorradial": 137, "vorsnr": 53, "voram": 28,
                         "vorstatus": 5, "vorsub": 29, "vorfreqactive": 109400}


def test_vorstatus_codes_and_vorsub():
    """vorstatus 0..5 and vorsub (SUB x100) are carried and not gated by valid."""
    p, _ = policy()
    # acquiring: no valid bearing yet, but status and sub still reported
    d = p.decide(None, 0.0, 0.0, state="ACQUIRING", valid=False, status=3,
                 sub_level=0.12)
    assert d.payload["vorstatus"] == 3
    assert d.payload["vorsub"] == 12
    assert d.payload["vorradial"] == 0 and d.payload["vorsnr"] == 0


# --------------------------------------------------------------------------- #
# A20: the payload is never zeroed by validity -- vorstatus is the only gate
# --------------------------------------------------------------------------- #

def test_values_survive_valid_false():
    """A20, the whole point: `valid=False` must NOT blank the measurements.

    Before A20 a NO_SIGNAL payload was indistinguishable from an idle one, and
    the remote had no way to see how marginal a marginal signal actually was.
    """
    p, _ = policy()
    d = p.decide(137, 40.0, 0.2, state="NO_SIGNAL", valid=False, status=1,
                 sub_level=0.11, freq_khz=109400)
    assert d.payload == {"vorradial": 137, "vorsnr": 40, "voram": 20,
                         "vorstatus": 1, "vorsub": 11, "vorfreqactive": 109400}


def test_vorstatus_is_the_only_difference_between_valid_and_not():
    """Same measurements in, same numbers out; only `vorstatus` distinguishes."""
    p, _ = policy()
    good = p.decide(137, 40.0, 0.2, state="TRACKING", valid=True, status=5,
                    sub_level=0.11, freq_khz=109400).payload
    p.reset()
    bad = p.decide(137, 40.0, 0.2, state="NO_SIGNAL", valid=False, status=1,
                   sub_level=0.11, freq_khz=109400).payload
    assert {k: v for k, v in good.items() if k != "vorstatus"} == \
           {k: v for k, v in bad.items() if k != "vorstatus"}
    assert good["vorstatus"] == 5 and bad["vorstatus"] == 1


def test_zero_payload_means_no_measurement_not_invalidity():
    """The all-zero payload is now reserved for "nothing measured yet"."""
    p, _ = policy()
    d = p.decide(None, 0.0, 0.0, state="IDLE", valid=False, status=0)
    assert d.payload == ZERO


def test_degraded_is_4_tracking_is_5():
    """DEGRADED and TRACKING are distinct status codes (4 and 5)."""
    p, _ = policy()
    d4 = p.decide(137, 30.0, 0.15, state="DEGRADED", valid=True, status=4,
                  freq_khz=109400)
    d5 = p.decide(137, 50.0, 0.30, state="TRACKING", valid=True, status=5,
                  freq_khz=109400)
    assert d4.payload["vorstatus"] == 4
    assert d5.payload["vorstatus"] == 5


def test_vorfreqactive_is_the_tuned_channel_khz():
    """vorfreqactive carries the tuned frequency in kHz, 0 when none."""
    p, _ = policy()
    d = p.decide(137, 50.0, 0.30, state="TRACKING", valid=True, status=5,
                 freq_khz=113350)
    assert d.payload["vorfreqactive"] == 113350
    idle = p.decide(None, 0.0, 0.0, state="IDLE", valid=False, status=0,
                    freq_khz=0)
    assert idle.payload["vorfreqactive"] == 0


def test_out_of_band_is_all_zero_because_nothing_is_measured():
    """Out of band there is no channel and no observation, so every field is 0.

    A20 changed the reason, not the payload: it is zero because there is
    nothing to report, not because validity blanked it.
    """
    p, _ = policy()
    d = p.decide(None, 0.0, 0.0, state="INVALID_FREQ", valid=False)
    assert d.payload == ZERO


def test_keys_are_always_present():
    """A consumer that indexes the payload must never hit a missing key."""
    p, _ = policy()
    for valid, state in [(True, "TRACKING"), (False, "NO_SIGNAL")]:
        d = p.decide(137 if valid else None, 30.0, 0.2, state=state, valid=valid)
        assert set(d.payload) == KEYS


@pytest.mark.parametrize("frm,to", [(0, 180), (137, 317), (200, 20), (359, 179)])
def test_bearing_ref_to_still_works(frm, to):
    p, _ = policy(bearing_ref="to")
    d = p.decide(frm, 40.0, 0.3, state="TRACKING", valid=True)
    assert d.payload["vorradial"] == to


def test_snr_and_am_are_clamped_non_negative():
    p, _ = policy()
    d = p.decide(137, -5.0, 0.0, state="TRACKING", valid=True)
    assert d.payload["vorsnr"] == 0 and d.payload["voram"] == 0


# --------------------------------------------------------------------------- #
# requirement 5: transmit only when a parameter changes
# --------------------------------------------------------------------------- #

def test_identical_payload_is_not_resent():
    p, clock = policy(keepalive_s=0)          # no keepalive
    assert p.decide(137, 50.0, 0.30, state="TRACKING", valid=True).send
    clock.advance(0.1)
    assert not p.decide(137, 50.0, 0.30, state="TRACKING", valid=True).send
    clock.advance(0.1)
    assert not p.decide(137, 50.4, 0.301, state="TRACKING", valid=True).send  # rounds same


def test_any_field_change_triggers_a_send():
    for changed in ("radial", "snr", "am"):
        p, clock = policy(keepalive_s=0)
        p.decide(137, 50.0, 0.30, state="TRACKING", valid=True)
        clock.advance(0.1)
        if changed == "radial":
            d = p.decide(139, 50.0, 0.30, state="TRACKING", valid=True)
        elif changed == "snr":
            d = p.decide(137, 55.0, 0.30, state="TRACKING", valid=True)
        else:
            d = p.decide(137, 50.0, 0.35, state="TRACKING", valid=True)
        assert d.send, f"{changed} change was not sent"


def test_going_invalid_is_a_change_and_sends():
    """A20: the status change alone is what makes this a change worth sending."""
    p, clock = policy(keepalive_s=0)
    p.decide(137, 50.0, 0.30, state="TRACKING", valid=True, status=5)
    clock.advance(0.1)
    d = p.decide(137, 50.0, 0.30, state="NO_SIGNAL", valid=False, status=1)
    assert d.send
    assert d.payload["vorstatus"] == 1
    assert d.payload["vorradial"] == 137     # the measurement is not blanked


def test_keepalive_resends_an_unchanged_payload():
    p, clock = policy(keepalive_s=5.0)
    p.decide(137, 50.0, 0.30, state="TRACKING", valid=True)
    for _ in range(8):
        clock.advance(1.0)
        d = p.decide(137, 50.0, 0.30, state="TRACKING", valid=True)
        if d.send:
            break
    assert d.send and d.reason == "keepalive"


# --------------------------------------------------------------------------- #
# failure paths
# --------------------------------------------------------------------------- #

def test_transport_failure_retries_once_then_backs_off():
    clock = FakeClock()
    t = RecordingTransport(fail_times=2)
    pub = Publisher(dict(BASE["publish"]), t, clock=clock)
    pub.publish(137, 50.0, 0.3, state="TRACKING", valid=True)
    assert t.attempts == 2 and t.sent == []
    assert pub.failures == 1


def test_stale_solutions_are_dropped_not_queued():
    clock = FakeClock()
    t = RecordingTransport(fail_times=10_000)
    pub = Publisher(dict(BASE["publish"]), t, clock=clock)
    pub.publish(137, 50.0, 0.3, state="TRACKING", valid=True)
    for i in range(20):
        clock.advance(1.0)
        pub.publish(137 + i, 50.0, 0.3, state="TRACKING", valid=True)
    assert pub.dropped > 5
    assert t.sent == []


def test_end_to_end_retune_sequence_through_the_publisher():
    clock = FakeClock()
    t = RecordingTransport()
    cfg = cfg_with()
    pub = Publisher(cfg.publish, t, clock=clock)
    c = Controller(cfg, FakeSdr(), pub, clock=clock)

    c.set_channel(109.4)
    clock.advance(0.4)
    c.tick()
    clock.advance(0.4)
    c.tick()
    assert c.state is State.ACQUIRING

    p = VorParams(radial_deg=137, fs=FS, seconds=8.0, offset_hz=-FS // 4,
                  snr_db=None, ident="", block=1 << 16)
    for blk in generate_blocks(p):
        clock.advance(0.1)
        c.push(blk)

    assert t.sent, "nothing was published"
    # A20: "usable" is `vorstatus >= 4`, not `vorsnr > 0` -- since the payload
    # is no longer zeroed, a non-zero SNR no longer implies a usable bearing.
    good = [s for s in t.sent if s["vorstatus"] >= 4]
    assert good and all(abs(((s["vorradial"] - 137 + 180) % 360) - 180) <= 2
                        for s in good)
    for s in t.sent:
        assert set(s) == KEYS
        assert 0 <= s["vorradial"] <= 359


def test_invalid_frequency_zeroes_all_three_and_never_tunes():
    clock = FakeClock()
    t = RecordingTransport()
    cfg = cfg_with()
    sdr = FakeSdr()
    c = Controller(cfg, sdr, Publisher(cfg.publish, t, clock=clock), clock=clock)
    c.set_channel(109.5)                      # ILS localiser, invalid
    clock.advance(1.0)
    c.tick()
    assert sdr.tuned == []
    assert t.sent
    assert t.sent[-1] == ZERO


def test_uncalibrated_daemon_never_publishes_a_bearing():
    clock = FakeClock()
    t = RecordingTransport()
    raw = copy.deepcopy(BASE)
    raw["sdr"]["sample_rate_hz"] = FS
    raw["sdr"]["tuner_offset_hz"] = -FS // 4
    cfg = Config(raw=raw)
    assert not cfg.calibrated
    c = Controller(cfg, FakeSdr(), Publisher(cfg.publish, t, clock=clock),
                   clock=clock)
    c.set_channel(109.4)
    clock.advance(0.4)
    c.tick()
    clock.advance(0.4)
    c.tick()
    p = VorParams(radial_deg=137, fs=FS, seconds=6.0, offset_hz=-FS // 4,
                  snr_db=None, ident="", block=1 << 16)
    for blk in generate_blocks(p):
        clock.advance(0.1)
        c.push(blk)
    assert t.sent
    # A2 survives A20: `vorradial` stays 0 because an uncalibrated bearing is a
    # measurement plus an unknown constant, and `vorstatus` has no code for
    # that. The signal measurements DO go out -- they are correct, and they are
    # what a builder watches while running tools/vor_calibrate.py.
    assert all(s["vorradial"] == 0 and s["vorstatus"] < 4 for s in t.sent)
    assert any(s["vorsnr"] > 0 and s["voram"] > 0 and s["vorsub"] > 0
               for s in t.sent), "signal health was suppressed while uncalibrated"


# --------------------------------------------------------------------------- #
# mock bridge helpers
# --------------------------------------------------------------------------- #

def test_mock_bridge_script_parsing():
    assert parse_script("109.4@0,113.35@8,null@16") == [
        (0.0, 109.4), (8.0, 113.35), (16.0, None)]
    assert parse_script("109.4") == [(0.0, 109.4)]


def test_controller_ignores_its_own_echo():
    """The bridge state contains vordir/vorsignal -- our own output coming back."""
    seen = []
    c = BridgeClient(BASE["bridge"], on_channel=seen.append)
    c.handle_text('{"vorfreq":109.4,"vordir":250,"vorsignal":99}')
    assert seen == [109.4]
