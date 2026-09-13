"""M6 -- state machine (T5, partially; the bridge I/O half is M7).

Every transition in the doc A section 7.1 table, as reconciled by amendment A13,
plus the one property that matters more than the table itself: **no bearing
derived from station A can ever be published after a retune to station B** (F3).

Clock is injected, so nothing here waits in real time.
"""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.config import Config  # noqa: E402
from rbvor.controller import DECODING_STATES, Controller, State  # noqa: E402
from rbvor.dsp import Observation  # noqa: E402
from tools.vor_gen import VorParams, generate_blocks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "config" / "rb-vor.json").read_text())
FS = 256_000


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@dataclass
class FakeSdr:
    tuned: list[int] = field(default_factory=list)
    closed: int = 0

    def set_frequency(self, nav_hz: int) -> None:
        self.tuned.append(nav_hz)

    def close(self) -> None:
        self.closed += 1


@dataclass
class FakePublisher:
    sent: list[tuple] = field(default_factory=list)
    #: Exactly what the controller passed, nothing masked. `sent` reports the
    #: bearing only when the controller called it valid, which is the internal
    #: question most of these tests ask; A20 made the WIRE carry the value
    #: regardless, so assertions about the payload must read `raw`.
    raw: list[dict] = field(default_factory=list)

    def publish(self, vordir, snr_db=0.0, am_depth=0.0, *, state, valid=True,
                status=0, sub_level=0.0, freq_khz=0, reason=""):
        # Store a synthetic "vorsignal" (1 if valid else 0) so the existing
        # assertions that only care about valid-vs-flag still read naturally.
        vorsignal = 1 if valid else 0
        self.sent.append((vordir if valid else None, vorsignal, state, reason,
                          snr_db, am_depth, status, sub_level, freq_khz))
        self.raw.append(dict(vordir=vordir, snr_db=snr_db, am_depth=am_depth,
                             state=state, valid=valid, status=status,
                             sub_level=sub_level, freq_khz=freq_khz,
                             reason=reason))

    @property
    def last(self):
        return self.sent[-1] if self.sent else None

    def bearings(self):
        """Every non-null vordir published as valid."""
        return [v for v, s, *_ in self.sent if v is not None and s > 0]


def make(calibrated=True, **overrides):
    raw = copy.deepcopy(BASE)
    raw["sdr"]["sample_rate_hz"] = FS
    raw["sdr"]["tuner_offset_hz"] = -FS // 4
    if calibrated:
        raw["bearing"].update(phase_sign=1, phase_offset_deg=-1.9657)
    for section, kv in overrides.items():
        raw[section].update(kv)
    cfg = Config(raw=raw)
    clock, sdr, pub = FakeClock(), FakeSdr(), FakePublisher()
    return Controller(cfg, sdr, pub, clock=clock), clock, sdr, pub


def obs(amp_a=0.30, sub=0.30, snr=60.0, dphi_deg=138.9657, overrun=False):
    return Observation(t=0.0, phi_a=np.deg2rad(dphi_deg), phi_b=0.0,
                       amp_a=amp_a, amp_b=480.0, carrier=1.0, sub_level=sub,
                       noise_est=amp_a / (10 ** (snr / 20)), overrun=overrun)


def settle(c, clock, mhz=109.4):
    """Drive the machine from IDLE to ACQUIRING on a channel."""
    c.set_channel(mhz)
    clock.advance(0.5)
    c.tick()                      # debounce expires -> retune
    clock.advance(0.5)
    c.tick()                      # settle expires -> ACQUIRING
    return c


def feed(c, n=4, **kw):
    for _ in range(n):
        c._discard_next = False
        c._handle(obs(**kw))


# --------------------------------------------------------------------------- #
# entry states
# --------------------------------------------------------------------------- #

def test_starts_idle():
    c, *_ = make()
    assert c.state is State.IDLE


def test_idle_publishes_zero_once():
    c, clock, _, pub = make()
    c.tick()
    c.tick()
    c.tick()
    assert len(pub.sent) == 1
    assert pub.sent[0][:4] == (None, 0, "IDLE", "startup")


def test_invalid_frequency_flags_once_and_names_the_reason():
    c, clock, sdr, pub = make()
    c.set_channel(109.5)                     # ILS localiser
    assert c.state is State.INVALID_FREQ
    assert pub.last[1] == 0
    assert "localiser" in pub.last[3]
    c.set_channel(109.5)
    assert len([s for s in pub.sent if s[2] == "INVALID_FREQ"]) == 1
    assert sdr.tuned == []                   # never tuned to a localiser


def test_absent_vorfreq_returns_to_idle():
    c, clock, _, _ = make()
    settle(c, clock)
    c.set_channel(None)
    assert c.state is State.IDLE
    assert c.channel_khz is None


# --------------------------------------------------------------------------- #
# debounce and retune
# --------------------------------------------------------------------------- #

def test_retune_is_debounced():
    c, clock, sdr, _ = make()
    c.set_channel(109.4)
    clock.advance(0.1)
    c.tick()
    assert sdr.tuned == []                   # 100 ms < 300 ms debounce
    clock.advance(0.25)
    c.tick()
    assert sdr.tuned == [109_400_000]


def test_spinning_the_knob_causes_one_retune_not_many():
    c, clock, sdr, _ = make()
    for mhz in (108.0, 108.2, 108.4, 109.0, 109.4):
        c.set_channel(mhz)
        clock.advance(0.05)
        c.tick()
    assert sdr.tuned == []
    clock.advance(0.35)
    c.tick()
    assert sdr.tuned == [109_400_000]


def test_retuning_to_the_same_channel_is_a_no_op():
    c, clock, sdr, _ = make()
    settle(c, clock)
    c.set_channel(109.4)
    clock.advance(1.0)
    c.tick()
    assert sdr.tuned == [109_400_000]


def test_zero_is_published_before_the_tuner_moves():
    """F3. The order matters: a consumer must see the flag before it could
    possibly see a bearing from the new station."""
    c, clock, sdr, pub = make()
    settle(c, clock)
    feed(c, 4)
    assert pub.bearings()
    n_before = len(pub.sent)

    c.set_channel(113.35)
    clock.advance(0.35)
    c.tick()
    first_new = pub.sent[n_before]
    assert first_new[1] == 0
    assert "retune" in first_new[3]
    assert sdr.tuned == [109_400_000, 113_350_000]


def test_retune_never_carries_the_previous_stations_measurements():
    """A20 + F3. The payload is no longer blanked by validity, so the retune
    POST must be honest by construction: TUNING status, and every measurement
    flushed with the station it came from. Otherwise station A's SNR/AM would
    ride out under station B's `vorfreqactive`."""
    c, clock, sdr, pub = make()
    settle(c, clock)
    feed(c, 4)
    assert pub.raw[-1]["snr_db"] > 0             # station A really was measured

    n_before = len(pub.sent)
    c.set_channel(113.35)
    clock.advance(0.35)
    c.tick()

    r = pub.raw[n_before]
    assert r["status"] == 2                      # TUNING, not TRACKING
    assert r["snr_db"] == 0 and r["am_depth"] == 0 and r["sub_level"] == 0
    assert r["vordir"] is None
    assert r["freq_khz"] == 113_350              # the new channel, not the old


def test_tuning_state_drops_samples_entirely():
    c, clock, _, pub = make()
    settle(c, clock)
    c.set_channel(113.35)
    clock.advance(0.35)
    c.tick()
    assert c.state is State.TUNING
    n = len(pub.sent)
    assert c.push(np.ones(1 << 16, np.complex64)) == 0
    assert len(pub.sent) == n


@pytest.mark.parametrize("state", [State.IDLE, State.INVALID_FREQ, State.TUNING])
def test_non_decoding_states_never_publish_a_bearing(state):
    assert state not in DECODING_STATES


def test_settle_delay_is_honoured():
    c, clock, _, _ = make(sdr={"settle_ms": 400})
    c.set_channel(109.4)
    clock.advance(0.35)
    c.tick()
    assert c.state is State.TUNING
    clock.advance(0.3)
    c.tick()
    assert c.state is State.TUNING           # 300 ms < 400 ms
    clock.advance(0.2)
    c.tick()
    assert c.state is State.ACQUIRING


# --------------------------------------------------------------------------- #
# acquisition, tracking, degradation
# --------------------------------------------------------------------------- #

def test_acquiring_needs_two_consecutive_valid_solutions():
    c, clock, _, pub = make()
    settle(c, clock)
    c._discard_next = False
    c._handle(obs())                      # warming: no dispersion estimate yet
    assert c.state is State.ACQUIRING
    assert pub.last[1] == 0
    c._handle(obs())                      # first genuinely valid solution
    assert c.state is State.ACQUIRING
    c._handle(obs())                      # second -> TRACKING
    assert c.state is State.TRACKING
    assert pub.last[0] == 137


def test_tracking_publishes_the_bearing_and_quality():
    c, clock, _, pub = make()
    settle(c, clock)
    feed(c, 5)
    vordir, valid, state, _reason, snr_db, am_depth, status, sub, freq = pub.last
    assert state == "TRACKING"
    assert vordir == 137
    assert valid == 1                        # a real, valid solution
    assert snr_db > 0 and am_depth > 0       # real measured SNR and AM flow through
    assert status == 5                       # TRACKING -> vorstatus 5
    assert freq == 109_400                   # active tuned frequency, kHz
    assert sub > 0                           # subcarrier level published


def test_degraded_is_a_real_state_with_a_nonzero_signal():
    """Amendment A13: DEGRADED publishes a usable-but-poor bearing, and is
    distinguishable on the wire from NO_SIGNAL."""
    c, clock, _, pub = make()
    settle(c, clock)
    # Deliberately marginal but not flagged: weak AM, weak subcarrier, no SNR
    # margin, and a bearing that wanders just enough to erode q_stab without
    # tripping its flag threshold.
    # The DEGRADED band is narrow -- roughly vorsignal 27..39 -- because
    # `q_stab` carries both the largest weight (0.40) and its own flag threshold
    # (0.40), so the score falls to zero soon after stability starts to go. That
    # is the conservative direction and is left alone deliberately: widening the
    # band would mean publishing more marginal bearings, and the weights came
    # from measurement (A11), not from taste.
    for i in range(8):
        c._discard_next = False
        c._handle(obs(amp_a=0.105, sub=0.05, snr=26.0,
                      dphi_deg=138.9657 + (6.0 if i % 2 else -6.0)))
    assert c.state is State.DEGRADED
    vordir, vorsignal, *_ = pub.last
    assert vordir is not None
    assert 0 < vorsignal < 40


def test_flagged_solution_gives_no_signal_and_zero():
    c, clock, _, pub = make()
    settle(c, clock)
    feed(c, 4)
    c._handle(obs(sub=0.001))
    assert c.state is State.NO_SIGNAL
    assert pub.last[1] == 0                   # internally: not a trusted bearing
    # A20: the wire still carries the last good radial and the measurements;
    # `vorstatus` 1 is what tells the consumer not to use them.
    assert pub.raw[-1]["status"] == 1
    assert pub.raw[-1]["vordir"] == 137
    assert pub.raw[-1]["snr_db"] > 0 and pub.raw[-1]["am_depth"] > 0


def test_overrun_window_is_never_published_as_valid():
    """F9."""
    c, clock, _, pub = make()
    settle(c, clock)
    feed(c, 4)
    c._handle(obs(overrun=True))
    assert pub.last[1] == 0
    assert "overrun" in pub.last[3]


def test_uncalibrated_never_publishes_a_bearing():
    """Amendment A2, enforced at the state machine as well as in bearing.py.

    A20 exempts nothing here: `vordir` is genuinely None on the wire, not
    merely masked by the fake publisher, because `vorstatus` has no code for
    "this number is off by an unknown constant".
    """
    c, clock, _, pub = make(calibrated=False)
    settle(c, clock)
    feed(c, 6)
    assert all(s[1] == 0 for s in pub.sent)
    assert all(r["vordir"] is None for r in pub.raw)
    assert "calibrate" in pub.last[3]
    # ...but the signal measurements do go out, so calibration is observable.
    assert any(r["snr_db"] > 0 and r["am_depth"] > 0 and r["sub_level"] > 0
               for r in pub.raw)


# --------------------------------------------------------------------------- #
# staleness and bridge loss
# --------------------------------------------------------------------------- #

def test_stale_signal_flushes_and_reacquires():
    c, clock, _, pub = make()
    settle(c, clock)
    feed(c, 4)
    c._handle(obs(sub=0.001))
    assert c.state is State.NO_SIGNAL
    clock.advance(BASE["publish"]["hold_s"] + 0.1)
    c.tick()
    assert c.state is State.ACQUIRING
    assert c._last_good is None              # stale bearing forgotten entirely
    assert c.tracker.n == 0                  # and stale dispersion with it


def test_bridge_loss_releases_the_sdr_after_the_grace_period():
    c, clock, sdr, pub = make()
    settle(c, clock)
    feed(c, 4)
    c.on_bridge_disconnected()
    clock.advance(1.0)
    c.tick()
    assert c.state is not State.IDLE
    clock.advance(BASE["bridge"]["ws_grace_s"])
    c.tick()
    assert c.state is State.IDLE
    assert sdr.closed == 1
    assert pub.last[1] == 0


def test_bridge_reconnect_cancels_the_grace_timer():
    c, clock, sdr, _ = make()
    settle(c, clock)
    c.on_bridge_disconnected()
    clock.advance(2.0)
    c.on_bridge_connected()
    clock.advance(10.0)
    c.tick()
    assert c.state is not State.IDLE
    assert sdr.closed == 0


def test_no_frequency_releases_the_sdr():
    """Requirement: going to IDLE (no vorfreq) releases the SDR."""
    c, clock, sdr, _ = make()
    settle(c, clock)
    feed(c, 3)
    assert sdr.closed == 0
    c.set_channel(None)                      # vorfreq went away -> IDLE
    assert c.state is State.IDLE
    assert sdr.closed == 1


def test_invalid_frequency_releases_the_sdr():
    """Requirement: an out-of-airband frequency -> INVALID_FREQ releases the SDR."""
    c, clock, sdr, _ = make()
    settle(c, clock)
    feed(c, 3)
    assert sdr.closed == 0
    c.set_channel(109.5)                     # ILS localiser, invalid VOR channel
    assert c.state is State.INVALID_FREQ
    assert sdr.closed == 1


def test_sdr_reopens_on_retune_after_idle_release():
    """After releasing on IDLE, a new frequency must reopen and tune the SDR."""
    c, clock, sdr, _ = make()
    settle(c, clock)
    c.set_channel(None)                      # release
    assert sdr.closed == 1
    tuned_before = len(sdr.tuned)
    c.set_channel(113.35)                    # a valid VOR channel
    settle(c, clock)
    assert len(sdr.tuned) > tuned_before     # reopened and retuned


# --------------------------------------------------------------------------- #
# the property that matters most
# --------------------------------------------------------------------------- #

def test_no_bearing_from_the_old_station_survives_a_retune():
    """F3, end to end with real signals.

    Station A is at radial 40, station B at radial 300. After the retune, every
    published bearing must be consistent with B. A single A-derived bearing
    leaking through is exactly failure mode F1.
    """
    c, clock, sdr, pub = make()

    def run(radial, seconds=6.0):
        p = VorParams(radial_deg=radial, fs=FS, seconds=seconds,
                      offset_hz=-FS // 4, snr_db=None, ident="", block=1 << 16)
        for blk in generate_blocks(p):
            c.push(blk)

    settle(c, clock, 109.4)
    run(40)
    first = pub.bearings()
    assert first, "station A produced no bearing at all"
    assert all(abs(((b - 40 + 180) % 360) - 180) < 3 for b in first)

    n = len(pub.sent)
    c.set_channel(113.35)
    clock.advance(0.35)
    c.tick()
    clock.advance(0.5)
    c.tick()
    assert c.state is State.ACQUIRING

    run(300)
    after = [v for v, s, *_ in pub.sent[n:] if v is not None and s > 0]
    assert after, "station B produced no bearing at all"
    assert all(abs(((b - 300 + 180) % 360) - 180) < 3 for b in after), (
        f"a bearing from the previous station leaked through: {after}")


# --------------------------------------------------------------------------- #
# amendment A16 -- found by the first live socket run, not by any unit test
# --------------------------------------------------------------------------- #

def test_repeated_identical_frequency_does_not_restart_the_retune():
    """A16. Bridges re-assert their state periodically; that is the normal
    design, and the mock bridge does it at 2 Hz.

    The original no-op check also required `state in DECODING_STATES`, so a
    repeat of the SAME frequency while still TUNING or ACQUIRING started the
    retune over -- flushing the demodulator faster than it could settle. The
    daemon would never acquire at all. Unit tests missed it because they set the
    channel exactly once.
    """
    c, clock, sdr, _ = make()
    c.set_channel(109.4)
    clock.advance(0.4)
    c.tick()
    assert sdr.tuned == [109_400_000]
    assert c.state is State.TUNING

    # bridge keeps repeating the same frequency at 2 Hz through settle...
    for _ in range(6):
        c.set_channel(109.4)
        clock.advance(0.5)
        c.tick()
    assert sdr.tuned == [109_400_000], "retune restarted by a repeated frequency"
    assert c.state is State.ACQUIRING

    # ...and keeps repeating it while we acquire and track
    for i in range(6):
        c.set_channel(109.4)
        c._discard_next = False
        c._handle(obs())
        clock.advance(0.5)
        c.tick()
        _ = i
    assert sdr.tuned == [109_400_000]
    assert c.state is State.TRACKING


def test_a_genuine_change_still_retunes_while_acquiring():
    """The A16 fix must not make the daemon deaf to a real channel change."""
    c, clock, sdr, _ = make()
    settle(c, clock, 109.4)
    assert c.state is State.ACQUIRING
    c.set_channel(113.35)
    clock.advance(0.4)
    c.tick()
    assert sdr.tuned == [109_400_000, 113_350_000]


def test_repeat_after_returning_to_idle_does_retune():
    """`channel_khz` is cleared on IDLE, so the same frequency is new again."""
    c, clock, sdr, _ = make()
    settle(c, clock, 109.4)
    c.set_channel(None)
    assert c.state is State.IDLE
    c.set_channel(109.4)
    clock.advance(0.4)
    c.tick()
    assert sdr.tuned == [109_400_000, 109_400_000]


def test_obs_sink_receives_every_window_with_frequency():
    """--csv path: the controller feeds every observation to obs_sink, with the
    tuned frequency, including overrun windows."""
    c, clock, _, _ = make()
    seen = []
    c.obs_sink = lambda obs, freq_khz: seen.append((obs.t, freq_khz, obs.overrun))
    settle(c, clock)
    feed(c, 4)
    assert seen, "obs_sink was never called"
    # frequency is the tuned channel in kHz
    assert all(fk == 109_400 for _, fk, _ in seen)
