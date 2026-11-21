"""A47 -- automatic front-end gain within a configured range.

Three layers, each testable without a dongle:

* `FrontEndMeter`: what the ADC and the band look like (clipping, level,
  carrier against the floor);
* `GainController`: the policy, driven here by a scripted meter so every rule
  can be exercised in isolation, and by an analytic FM-overload receiver to
  check that the loop converges to the best gain and stays there;
* the wiring: `SdrReader` applies decisions between reads and marks the next
  block `GAIN_STEP`; the controller drops the windows that span a step without
  entering NO_SIGNAL, and sets the policy from its state.
"""
from __future__ import annotations

import copy
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.config import Config, ConfigError, _validate_relationships  # noqa: E402
from rbvor.controller import Controller, State, gain_policy_for  # noqa: E402
from rbvor.gainctl import (  # noqa: E402
    COUNTS,
    AutoGainParams,
    FrontEnd,
    FrontEndMeter,
    GainController,
)
from rbvor.record import IQMeta, RingRecorder  # noqa: E402
from rbvor.sdr import GAIN_STEP, FakeDevice, SdrReader  # noqa: E402
from tools.vor_gen import VorParams, generate_blocks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "config" / "rb-vor.json").read_text())
FS = 256_000
CARRIER = FS // 4                    # where the carrier sits (tuner offset -fs/4, A22)
R820T = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7,
         22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4,
         43.9, 44.5, 48.0, 49.6]
FC0012 = [-9.9, -4.0, 7.1, 17.9, 19.2]


# --------------------------------------------------------------------------- #
# the meter
# --------------------------------------------------------------------------- #

def _noise(n, rms_counts, seed=0):
    rng = np.random.default_rng(seed)
    s = rms_counts / COUNTS
    return ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * s).astype(np.complex64)


def _cu8_like(z):
    """Quantise and clip the way the ADC does, back to unit-scaled complex64."""
    i = np.clip(np.round(z.real * COUNTS + 127.5), 0, 255)
    q = np.clip(np.round(z.imag * COUNTS + 127.5), 0, 255)
    return (((i - 127.5) + 1j * (q - 127.5)) / COUNTS).astype(np.complex64)


def test_meter_reads_adc_level_in_counts_and_clipping_in_percent():
    m = FrontEndMeter(FS, CARRIER, 10_000, 6.0)
    m.add(_cu8_like(_noise(FS, 20.0)))
    r = m.result()
    assert r.rms_counts == pytest.approx(20.0, rel=0.03)
    assert r.clip_pct < 0.01
    m.reset()
    m.add(_cu8_like(_noise(FS, 90.0)))          # past full scale: clips heavily
    r = m.result()
    # P(|x| > 127.5) for one Gaussian component at sigma 90, either of two
    p1 = math.erfc(127.5 / 90.0 / math.sqrt(2))
    assert r.clip_pct == pytest.approx(100 * (1 - (1 - p1) ** 2), rel=0.1)


def test_meter_finds_a_carrier_and_measures_it_against_the_floor():
    n = FS
    t = np.arange(n) / FS
    z = _noise(n, 10.0) + (0.05 * np.exp(2j * np.pi * CARRIER * t)).astype(np.complex64)
    m = FrontEndMeter(FS, CARRIER, 10_000, 6.0)
    for k in range(0, n, 16384):
        m.add(z[k:k + 16384])
    r = m.result()
    # expected: carrier power over the noise power in one fs/16384 bin, less
    # the Hann window's processing loss (1.76 dB)
    noise_per_bin = 2 * (10.0 / COUNTS) ** 2 / 16384
    expect = 10 * math.log10(0.05 ** 2 / noise_per_bin) - 1.76
    assert r.cnr_db is not None
    assert r.cnr_db == pytest.approx(expect, abs=1.5)


def test_noise_alone_is_never_taken_for_a_carrier():
    """The carrier_min_db default (6 dB) against noise-only periods: the best
    noise peak in the search window must stay below it, or the controller
    would probe on noise."""
    worst = -1e9
    for seed in range(30):
        m = FrontEndMeter(FS, CARRIER, 10_000, 6.0)
        z = _noise(FS, 15.0, seed)
        for k in range(0, FS, 16384):
            m.add(z[k:k + 16384])
        r = m.result()
        assert r.cnr_db is None
        worst = max(worst, r.peak_db)
    assert worst < 4.5, worst


# --------------------------------------------------------------------------- #
# the policy, against a scripted meter
# --------------------------------------------------------------------------- #

class ScriptedMeter:
    """Stands in for FrontEndMeter: returns whatever `env(gain)` says."""

    def __init__(self, ctl, env, seconds=1.0):
        self.ctl, self.env, self.period = ctl, env, seconds
        self.seconds = 0.0

    def add(self, iq):
        self.seconds += self.period

    def reset(self):
        self.seconds = 0.0

    def result(self):
        return self.env(self.ctl.gain_db)


def fe(clip=0.0, rms=20.0, cnr=None):
    return FrontEnd(clip, rms, cnr, -1e9 if cnr is None else cnr, 1.0)


def ctl(env, ladder=R820T, start=40.2, **kw):
    p = AutoGainParams(enabled=True, **{"min_db": 0.0, "max_db": 49.6, **kw})
    c = GainController(p, ladder, start, FS, CARRIER)
    c.meter = ScriptedMeter(c, env)
    c.retuned(109_400_000)
    return c


def run(c, periods, policy="free", t0=0.0):
    """Feed `periods` evaluation periods; return the decisions."""
    out = []
    for k in range(periods):
        d = c.observe(None, t0 + k, policy)
        if d is not None:
            out.append(d)
    return out


def test_the_first_period_after_a_change_is_not_judged():
    """It straddles the step, so it describes neither gain."""
    c = ctl(lambda g: fe(clip=50.0))
    assert c.observe(None, 0.0) is None          # settling after retune
    d = c.observe(None, 1.0)
    assert d.changed and d.gain_db == 37.2       # two steps down
    assert c.observe(None, 2.0) is None          # settling after that step


def test_severe_clipping_steps_down_two_even_while_tracking():
    c = ctl(lambda g: fe(clip=8.0, rms=70))
    d = run(c, 2, policy="protect")
    assert [x.gain_db for x in d] == [37.2]
    assert "clipping" in d[0].reason


def test_moderate_clipping_is_left_alone_while_tracking():
    c = ctl(lambda g: fe(clip=2.0, rms=50, cnr=20.0))
    assert run(c, 10, policy="protect") == []
    assert c.gain_db == 40.2


def test_moderate_clipping_steps_down_one_when_free():
    c = ctl(lambda g: fe(clip=2.0, rms=50))
    d = run(c, 2)
    assert [x.gain_db for x in d] == [38.6]


def test_hold_never_moves_the_gain():
    c = ctl(lambda g: fe(clip=50.0, rms=120))
    assert run(c, 10, policy="hold") == []
    assert c.gain_db == 40.2


def test_no_carrier_and_a_full_adc_steps_down():
    c = ctl(lambda g: fe(rms=60.0))
    d = run(c, 2)
    assert d[0].gain_db == 38.6 and "no carrier" in d[0].reason


def test_no_carrier_and_an_idle_adc_steps_up():
    c = ctl(lambda g: fe(rms=1.0), start=8.7)
    d = run(c, 2)
    assert d[0].gain_db == 12.5 and "idle" in d[0].reason


def test_an_idle_adc_with_a_carrier_in_view_does_not_step_up():
    """Regression for the first closed-loop run: under FM overload the best
    gain leaves the ADC nearly idle, and the idle rule then stepped up again,
    so the gain cycled every couple of seconds. With a carrier in view the
    probe decides."""
    c = ctl(lambda g: fe(rms=1.0, cnr=30.0), start=28.0, probe_s=1e9)
    c._next_probe = 1e9
    assert run(c, 20) == []


def test_a_probe_that_helps_is_kept_and_the_next_follows_in_the_same_direction():
    # carrier/floor improves as the gain falls, down to 29.7 dB
    c = ctl(lambda g: fe(rms=20, cnr=40.0 - abs(g - 29.7)), start=36.4)
    d = run(c, 12)
    kept = [x for x in d if not x.changed]
    assert kept and all("kept" in x.reason for x in kept)
    # where the loop is anchored: the probe's origin while one is running
    assert (c._probe.from_db if c._probe else c.gain_db) == 29.7


def test_a_probe_that_does_not_help_is_undone_and_the_direction_turns():
    c = ctl(lambda g: fe(rms=20, cnr=30.0), start=28.0, probe_s=30.0)
    d = run(c, 4)
    assert [x.gain_db for x in d] == [25.4, 28.0]
    assert "undone" in d[1].reason
    assert c._dir == +1
    assert run(c, 20, t0=4) == []                 # waits probe_s before the next


def test_a_probe_that_clips_is_undone_even_if_the_carrier_rose():
    c = ctl(lambda g: fe(rms=20 if g < 30 else 60, clip=0.0 if g < 30 else 2.0,
                         cnr=20.0 if g < 30 else 25.0), start=28.0)
    c._dir = +1
    d = run(c, 4)
    assert [x.gain_db for x in d] == [29.7, 28.0]


def test_the_range_limits_every_step():
    c = ctl(lambda g: fe(clip=50.0), start=20.7, min_db=19.7, max_db=40.2)
    run(c, 20)
    assert c.gain_db == 19.7
    c = ctl(lambda g: fe(rms=1.0), start=8.7, min_db=0.0, max_db=12.5)
    run(c, 20)
    assert c.gain_db == 12.5


def test_the_range_is_snapped_inward_to_the_tuners_ladder():
    c = GainController(AutoGainParams(enabled=True, min_db=5.0, max_db=30.0), R820T,
                       19.7, FS, CARRIER)
    assert c.ladder[0] == 7.7 and c.ladder[-1] == 29.7
    # an FC0012 asked for a range above its top step gets the step nearest it
    c = GainController(AutoGainParams(enabled=True, min_db=25.0, max_db=40.0), FC0012,
                       19.2, FS, CARRIER)
    assert c.ladder == [19.2]


def test_at_the_end_of_the_range_a_probe_turns_round():
    c = ctl(lambda g: fe(rms=20, cnr=30.0), start=0.0)
    d = run(c, 2)
    assert d[0].gain_db == 0.9                    # could not go down: went up


def test_each_band_keeps_its_own_gain():
    """Nav (VHF) and glide slope (UHF) on one dongle: the glide slope is clean
    at a gain the nav band cannot take (F7)."""
    c = GainController(AutoGainParams(enabled=True), R820T, 40.2, FS, CARRIER)
    assert c.retuned(109_400_000) is None
    c.gain_db = 28.0                              # the loop settled low on VHF
    assert c.retuned(332_600_000) is None         # first UHF visit: keep what we have
    c.gain_db = 44.5                              # ... and settled high there
    assert c.retuned(110_950_000) == 28.0
    assert c.retuned(330_650_000) == 44.5


def test_applied_follows_the_devices_own_rounding():
    c = GainController(AutoGainParams(enabled=True), R820T, 40.2, FS, CARRIER)
    c.applied(32.79)
    assert c.gain_db == 32.8


# --------------------------------------------------------------------------- #
# closed loop against an analytic FM-overload receiver
# --------------------------------------------------------------------------- #

def fm_overload(g):
    """A front end whose floor, in ADC counts, is intermodulation (power rising
    3 dB per dB of gain) plus thermal noise (1 dB per dB) plus the ADC's own
    quantisation (fixed). At the flight's 40.2 dB: 75 counts RMS, clipping, the
    carrier buried. Shape from the F7 root-cause simulation."""
    s2 = 10 ** ((g - 40.2) / 10)                   # power gain re 40.2 dB
    im, th, q = 75.0 ** 2, (75.0 * 10 ** (-33 / 20)) ** 2, 1.0 / 6
    floor = im * s2 ** 3 + th * s2 + q
    floor0 = im + th + q
    cnr = 2.0 + 10 * math.log10(s2 * floor0 / floor)
    rms = math.sqrt(floor / 2)
    clip = 100 * (1 - (1 - math.erfc(127.5 / rms / math.sqrt(2))) ** 2)
    return fe(clip=clip, rms=min(rms, 127.0), cnr=cnr if cnr >= 6.0 else None)


def _cnr(g):
    return fm_overload(g).cnr_db or -99.0


def test_the_loop_finds_the_best_gain_under_fm_overload_and_stays_there():
    c = ctl(fm_overload, start=40.2, probe_s=10.0)
    assert fm_overload(40.2).clip_pct > 1 and fm_overload(40.2).cnr_db is None
    run(c, 60)
    best = max(_cnr(g) for g in R820T)
    anchor = c._probe.from_db if c._probe else c.gain_db
    assert _cnr(anchor) >= best - 1.0, (anchor, _cnr(anchor), best)
    assert fm_overload(anchor).clip_pct < 0.1
    # settled: from here on only probes, each tried and undone
    d = run(c, 200, t0=60)
    i = R820T.index(anchor)
    assert all(x.gain_db in R820T[max(0, i - 1):i + 2] for x in d), d
    assert len(d) <= 2 * (200 // 10) + 2          # at most one probe pair per probe_s
    assert (c._probe.from_db if c._probe else c.gain_db) == anchor


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

def test_the_shipped_config_carries_automatic_gain_disabled():
    """Off until flown, like the localizer and glide slope were."""
    ag = BASE["sdr"]["auto_gain"]
    assert ag["enabled"] is False
    assert ag["min_db"] <= BASE["sdr"]["gain_db"] <= ag["max_db"]


@pytest.mark.parametrize("bad, words", [
    ({"min_db": 40.0, "max_db": 20.0}, "exceeds max_db"),
    ({"clip_max_pct": 6.0, "clip_severe_pct": 5.0}, "clip_max_pct"),
    ({"rms_low_counts": 50.0, "rms_high_counts": 40.0}, "rms_low_counts"),
])
def test_inconsistent_ranges_are_rejected(bad, words):
    raw = copy.deepcopy(BASE)
    raw["sdr"]["auto_gain"] = {"enabled": True, "min_db": 0.0, "max_db": 49.6, **bad}
    with pytest.raises(ConfigError, match=words):
        _validate_relationships(raw)


def test_a_tuner_profile_may_carry_its_own_range():
    raw = copy.deepcopy(BASE)
    raw["sdr"]["tuner_profiles"]["FC0012"]["auto_gain"] = {
        "enabled": True, "min_db": 30.0, "max_db": 10.0}
    with pytest.raises(ConfigError, match="FC0012"):
        _validate_relationships(raw)


# --------------------------------------------------------------------------- #
# the reader applies decisions between reads
# --------------------------------------------------------------------------- #

def sdr_cfg(**kw):
    c = dict(BASE["sdr"])
    c.update(sample_rate_hz=FS, tuner_offset_hz=-FS // 4, block_samples=16384,
             buffer_blocks=64, gain_db=40.2)
    c.update(kw)
    return c


def clipping_source():
    rng = np.random.default_rng(1)

    def src(n):
        return _cu8_like(_noise(n, 110.0, int(rng.integers(1 << 30))))
    return src


def _drain_until(reader, cond, timeout=20.0):
    got = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got += list(reader.blocks(timeout=0.05))
        if cond(got):
            break
    return got


def test_reader_steps_the_gain_down_on_a_clipping_adc_and_marks_the_next_block():
    dev = FakeDevice(clipping_source())
    dev.gains_db = list(R820T)
    dev.gain_db = 40.2
    reader = SdrReader(sdr_cfg(auto_gain={"enabled": True, "min_db": 20.7,
                                          "max_db": 49.6, "eval_s": 0.25}), device=dev)
    reader.set_frequency(109_400_000)
    got = _drain_until(reader, lambda g: len(dev.gain_history) >= 2)
    stats = reader.stats
    reader.close()
    assert dev.gain_history[:2] == [37.2, 33.8]          # two steps at a time
    gaps = [gap for _, gap in got if gap]
    assert any(gap is GAIN_STEP for gap in gaps)
    assert stats["gain_db"] == dev.gain_history[len(dev.gain_history) - 1]
    assert stats["gain_steps"] >= 2


def test_reader_without_auto_gain_never_touches_the_gain():
    dev = FakeDevice(clipping_source())
    dev.gains_db = list(R820T)
    cfg = sdr_cfg()
    cfg.pop("auto_gain", None)
    reader = SdrReader(cfg, device=dev)
    reader.set_frequency(109_400_000)
    _drain_until(reader, lambda g: len(g) >= 40, timeout=5.0)
    reader.close()
    assert dev.gain_history == []
    assert "gain_db" not in reader.stats


def test_shipped_config_disabled_never_touches_the_gain():
    dev = FakeDevice(clipping_source())
    dev.gains_db = list(R820T)
    reader = SdrReader(sdr_cfg(), device=dev)            # auto_gain present, disabled
    reader.set_frequency(109_400_000)
    _drain_until(reader, lambda g: len(g) >= 40, timeout=5.0)
    reader.close()
    assert dev.gain_history == []


def test_reader_obeys_the_policy_it_is_given():
    dev = FakeDevice(clipping_source())
    dev.gains_db = list(R820T)
    dev.gain_db = 40.2
    reader = SdrReader(sdr_cfg(auto_gain={"enabled": True, "min_db": 0.0,
                                          "max_db": 49.6, "eval_s": 0.25}), device=dev)
    reader.gain_policy = lambda: "hold"
    reader.set_frequency(109_400_000)
    _drain_until(reader, lambda g: len(g) >= 60, timeout=5.0)
    reader.close()
    assert dev.gain_history == []


def test_reader_restores_each_bands_gain_on_retune():
    dev = FakeDevice(lambda n: _cu8_like(_noise(n, 20.0)))
    dev.gains_db = list(R820T)
    dev.gain_db = 40.2
    reader = SdrReader(sdr_cfg(auto_gain={"enabled": True, "min_db": 0.0,
                                          "max_db": 49.6}), device=dev)
    reader.gain_policy = lambda: "hold"
    reader.set_frequency(109_400_000)
    reader.agc.gain_db = 28.0                 # as if the loop had settled
    dev.gain_db = 28.0
    reader.set_frequency(330_650_000)
    reader.agc.gain_db = 44.5
    dev.gain_db = 44.5
    reader.set_frequency(110_950_000)
    reader.close()
    assert dev.gain_history[-1] == 28.0


# --------------------------------------------------------------------------- #
# the controller: policy from state, gain steps dropped silently
# --------------------------------------------------------------------------- #

def test_policy_follows_the_state():
    assert gain_policy_for(State.TRACKING) == "protect"
    for s in (State.ACQUIRING, State.NO_SIGNAL, State.DEGRADED):
        assert gain_policy_for(s) == "free"
    for s in (State.IDLE, State.TUNING, State.INVALID_FREQ, State.NO_SDR):
        assert gain_policy_for(s) == "hold"


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Sdr:
    def __init__(self):
        self.gain_policy = None

    def set_frequency(self, hz):
        pass

    def close(self):
        pass


class _Pub:
    def __init__(self):
        self.sent = []

    def publish(self, vordir, snr_db=0.0, am_depth=0.0, *, state, valid=True,
                status=0, sub_level=0.0, freq_khz=0, reason=""):
        self.sent.append((state, valid, reason))


def _controller():
    raw = copy.deepcopy(BASE)
    raw["sdr"].update(sample_rate_hz=FS, tuner_offset_hz=-FS // 4)
    raw["bearing"].update(phase_sign=1, phase_offset_deg=-1.9657)
    clock, sdr, pub = _Clock(), _Sdr(), _Pub()
    c = Controller(Config(raw=raw), sdr, pub, clock=clock)
    c.set_channel(109.4)
    for _ in range(2):
        clock.t += 0.5
        c.tick()
    return c, sdr, pub


def _vor_blocks(seconds):
    p = VorParams(radial_deg=213, fs=FS, seconds=seconds, offset_hz=-FS // 4,
                  snr_db=30.0, ident="", block=1 << 14)
    return list(generate_blocks(p))


def test_controller_hands_its_policy_to_the_reader():
    c, sdr, _ = _controller()
    assert sdr.gain_policy is not None
    assert sdr.gain_policy() == gain_policy_for(c.state)


@pytest.mark.parametrize("gap, expect_no_signal", [(GAIN_STEP, False), (True, True)])
def test_a_gain_step_drops_its_windows_without_losing_the_signal(gap, expect_no_signal):
    """A real overrun (gap True) must end in NO_SIGNAL (F9). A gain step must
    not: the samples are continuous, only their scale changed, so the windows
    that span it are dropped and the state is left alone."""
    c, _, pub = _controller()
    blocks = _vor_blocks(24.0)
    half = len(blocks) // 2
    for b in blocks[:half]:
        c.push(b)
    assert c.state in (State.TRACKING, State.DEGRADED), c.state
    states = []
    n_before = len(pub.sent)
    for k, b in enumerate(blocks[half:]):
        scale = np.float32(0.5)                 # the step: everything after is 6 dB down
        c.push(b * scale, gap=gap if k == 0 else False)
        states.append(c.state)
    if expect_no_signal:
        assert State.NO_SIGNAL in states
    else:
        assert State.NO_SIGNAL not in states
        assert c.state in (State.TRACKING, State.DEGRADED)
        assert len(pub.sent) > n_before            # publishing resumed after the step


def test_the_ring_keeps_the_samples_of_a_gain_step():
    c, _, _ = _controller()
    meta = IQMeta(format="cu8", sample_rate_hz=FS, center_freq_hz=0, nav_freq_hz=0,
                  tuner_offset_hz=-FS // 4)
    c.recorder = RingRecorder(meta, "/nonexistent", max_seconds=30)
    b = _vor_blocks(1.0)[0]
    c.push(b)
    c.push(b, gap=GAIN_STEP)
    c.push(b, gap=True)                           # a real gap is still left out
    assert c.recorder._held == 2 * b.size


def test_sidecars_record_the_gain_in_force(tmp_path):
    meta = IQMeta(format="cu8", sample_rate_hz=FS, center_freq_hz=0, nav_freq_hz=0,
                  tuner_offset_hz=-FS // 4)
    r = RingRecorder(meta, tmp_path, max_seconds=1)
    gain = {"g": 40.2}
    r.gain_source = lambda: gain["g"]
    r.push(np.zeros(1000, np.complex64))
    gain["g"] = 28.0
    assert r._clone_meta(1000, "x", None).gain_db == 28.0
    r.gain_source = lambda: None                  # unknown: the template stands
    assert r._clone_meta(1000, "x", None).gain_db is None
