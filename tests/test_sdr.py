"""M5 -- live SDR reader.

Runs against `FakeDevice`, so the whole live path -- reader thread, bounded
queue, retune flush, overrun flagging, controller integration -- is exercised in
CI with no radio attached. The only thing not covered here is the vendor driver
itself, which is what the bench test is for.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.config import Config  # noqa: E402
from rbvor.controller import Controller, State  # noqa: E402
from rbvor.publisher import Publisher, RecordingTransport  # noqa: E402
from rbvor.sdr import FakeDevice, SdrError, SdrReader, open_device  # noqa: E402
from tools.vor_gen import VorParams, generate_blocks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "config" / "rb-vor.json").read_text())
FS = 256_000


def sdr_cfg(**kw):
    c = dict(BASE["sdr"])
    c.update(sample_rate_hz=FS, tuner_offset_hz=-FS // 4, block_samples=8192,
             buffer_blocks=4)
    c.update(kw)
    return c


def const_source(value=0.1 + 0.0j):
    def src(n):
        return np.full(n, value, dtype=np.complex64)
    return src


# --------------------------------------------------------------------------- #
# device layer
# --------------------------------------------------------------------------- #

def test_open_device_reports_every_backend_it_tried():
    with pytest.raises(SdrError) as e:
        open_device(sdr_cfg(), order=("soapy", "pyrtlsdr"))
    msg = str(e.value)
    assert "soapy" in msg and "pyrtlsdr" in msg


def test_tuner_offset_is_applied_by_the_reader_not_by_callers():
    """The rest of the system passes navaid frequencies and never has to
    remember the offset."""
    dev = FakeDevice(const_source())
    r = SdrReader(sdr_cfg(), device=dev)
    r.set_frequency(109_400_000)
    assert dev.freq_hz == 109_400_000 - FS // 4
    assert r.nav_hz == 109_400_000
    r.close()


def test_reader_delivers_blocks_of_the_configured_size():
    r = SdrReader(sdr_cfg(), device=FakeDevice(const_source()))
    r.set_frequency(109_400_000)
    time.sleep(0.2)
    got = list(r.blocks())
    r.close()
    assert got
    block, gap = got[0]
    assert block.dtype == np.complex64
    assert block.size == 8192
    assert gap is True                # first block after a retune is a gap


def test_queue_is_bounded_and_drops_the_oldest():
    """Doc A 10.4: bounded, never queued. A growing queue would trade a visible
    flagged gap for an invisible growing delay."""
    r = SdrReader(sdr_cfg(buffer_blocks=3), device=FakeDevice(const_source()))
    r.set_frequency(109_400_000)
    time.sleep(0.4)                   # nobody is consuming
    assert r._q.qsize() <= 3
    assert r.overruns > 0
    r.close()


def test_overrun_sets_the_gap_flag_on_the_surviving_block():
    r = SdrReader(sdr_cfg(buffer_blocks=2), device=FakeDevice(const_source()))
    r.set_frequency(109_400_000)
    time.sleep(0.3)
    blocks = list(r.blocks())
    r.close()
    assert r.overruns > 0
    assert any(gap for _, gap in blocks)


def test_retune_discards_everything_already_in_flight():
    """F3: queued samples were taken at the old frequency."""
    r = SdrReader(sdr_cfg(), device=FakeDevice(const_source()))
    r.set_frequency(109_400_000)
    time.sleep(0.2)
    assert r._q.qsize() > 0
    r.set_frequency(113_350_000)
    assert r._q.qsize() == 0
    r.close()


def test_read_errors_are_retried_and_flagged_not_fatal():
    """F7: a USB reset must not kill the daemon."""
    state = {"n": 0}

    def flaky(n):
        state["n"] += 1
        if state["n"] % 3 == 0:
            raise SdrError("simulated USB glitch")
        return np.zeros(n, dtype=np.complex64)

    r = SdrReader(sdr_cfg(), device=FakeDevice(flaky))
    r.set_frequency(109_400_000)
    time.sleep(0.5)
    r.close()
    assert r.read_errors > 0
    assert r.blocks_read > 0          # kept going


def test_close_is_idempotent_and_stops_the_thread():
    dev = FakeDevice(const_source())
    r = SdrReader(sdr_cfg(), device=dev)
    r.set_frequency(109_400_000)
    time.sleep(0.1)
    r.close()
    r.close()
    assert dev.closed
    assert not any(t.name == "rbvor-sdr" for t in threading.enumerate())


def test_stats_are_reported_for_diagnostics():
    r = SdrReader(sdr_cfg(), device=FakeDevice(const_source()))
    r.set_frequency(109_400_000)
    time.sleep(0.15)
    s = r.stats
    r.close()
    assert set(s) == {"blocks_read", "overruns", "read_errors", "queued", "nav_hz"}
    assert s["nav_hz"] == 109_400_000


# --------------------------------------------------------------------------- #
# the live path, end to end
# --------------------------------------------------------------------------- #

def test_live_path_decodes_a_real_bearing():
    """Reader thread -> queue -> controller -> publisher, with no file anywhere.

    This is the code path the bench test exercises; the only thing the bench
    adds is the vendor driver and an antenna.
    """
    raw = copy.deepcopy(BASE)
    raw["sdr"].update(sample_rate_hz=FS, tuner_offset_hz=-FS // 4,
                      block_samples=1 << 15, buffer_blocks=64)
    raw["bearing"].update(phase_sign=1, phase_offset_deg=-1.9657)
    cfg = Config(raw=raw)

    p = VorParams(radial_deg=213, fs=FS, seconds=14.0, offset_hz=-FS // 4,
                  snr_db=25.0, ident="TST", block=1 << 15)
    pool = list(generate_blocks(p))
    idx = {"i": 0}
    # Pace the fake device. A real SDR delivers samples at the sample rate; an
    # unpaced fake outruns the consumer by ~100x, every window touches a gap,
    # and nothing is ever published. That is correct behaviour (F9), and it is
    # asserted separately in `test_reader_outrunning_the_consumer_publishes_
    # nothing` -- but it makes a useless integration test. 4x realtime keeps the
    # threading honest while staying fast enough for CI.
    block_period = (1 << 15) / FS / 4.0

    def source(n):
        b = pool[idx["i"] % len(pool)]
        idx["i"] += 1
        time.sleep(block_period)
        return b

    reader = SdrReader(cfg.sdr, device=FakeDevice(source))
    t = RecordingTransport()
    controller = Controller(cfg, reader, Publisher(cfg.publish, t))

    controller.set_channel(109.4)
    # Deadline is generous on purpose: this test shares a machine with the rest
    # of the suite in CI, and the reader thread competes for the GIL. The code
    # under test is not slow -- a single good solution is enough to prove the
    # live path carries a real bearing end to end.
    deadline = time.monotonic() + 40.0
    while time.monotonic() < deadline:
        controller.tick()
        for block, gap in reader.blocks():
            controller.push(block, gap=gap)
        if len([s for s in t.sent if s["vorstatus"] >= 4]) >= 1:
            break
        time.sleep(0.005)
    reader.close()

    # A20: usability is `vorstatus >= 4`. The payload is no longer zeroed, so
    # `vorsnr > 0` is true of acquiring and flagged windows too.
    good = [s for s in t.sent if s["vorstatus"] >= 4]
    assert good, f"no bearing published; state={controller.state} sent={t.sent[:3]}"
    assert controller.state in (State.TRACKING, State.DEGRADED)
    for s in good:
        assert abs(((s["vorradial"] - 213 + 180) % 360) - 180) <= 3, s
        assert set(s) == {"vorradial", "vorsnr", "voram", "vorstatus", "vorsub", "vorfreqactive"}


def test_reader_outrunning_the_consumer_publishes_nothing_rather_than_garbage():
    """The safe failure, pinned.

    If the reader produces faster than the DSP consumes -- a Pi under load, a
    block size that is too small, another process stealing the core -- the queue
    overruns, every integration window touches a gap, and F9 discards all of
    them. The daemon then publishes `vorsignal` 0 forever.

    That is the right outcome: availability is lost, correctness is not. It is
    also an unhelpful symptom in the field ("vorsignal always 0"), which is why
    `SdrReader.stats` is logged every 30 s and why the troubleshooting table in
    docs/install.md names it.
    """
    raw = copy.deepcopy(BASE)
    raw["sdr"].update(sample_rate_hz=FS, tuner_offset_hz=-FS // 4,
                      block_samples=1 << 15, buffer_blocks=4)
    raw["bearing"].update(phase_sign=1, phase_offset_deg=-1.9657)
    cfg = Config(raw=raw)

    p = VorParams(radial_deg=213, fs=FS, seconds=6.0, offset_hz=-FS // 4,
                  snr_db=25.0, ident="", block=1 << 15)
    pool = list(generate_blocks(p))
    idx = {"i": 0}

    def source(n):
        # Deliberately far faster than realtime (a block is 128 ms of signal),
        # but not a busy-spin: without the yield the reader thread starves the
        # consumer through the GIL and the test measures the wrong thing.
        b = pool[idx["i"] % len(pool)]
        idx["i"] += 1
        time.sleep(0.0005)
        return b

    reader = SdrReader(cfg.sdr, device=FakeDevice(source))
    t = RecordingTransport()
    controller = Controller(cfg, reader, Publisher(cfg.publish, t))
    controller.set_channel(109.4)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        controller.tick()
        for block, gap in reader.blocks():
            controller.push(block, gap=gap)
        time.sleep(0.005)
    reader.close()

    assert reader.overruns > 0, "the fake device did not outrun the consumer"
    assert all(s["vorstatus"] < 4 for s in t.sent), (
        "a bearing was published as usable from gapped data")


def test_swap_iq_conjugates_every_block():
    """A mirrored capture (some RTL-SDR setups flip the spectrum) is un-mirrored
    when sdr.swap_iq is set. Applied in the reader so it covers all backends."""
    seen = []

    def source(n):
        return np.full(n, 0.1 + 0.2j, dtype=np.complex64)

    cfg = sdr_cfg(swap_iq=True)
    r = SdrReader(cfg, device=FakeDevice(source))
    r.set_frequency(109_400_000)
    import time as _t
    _t.sleep(0.15)
    for block, _gap in r.blocks():
        seen.append(block)
    r.close()
    assert seen, "no blocks produced"
    # 0.1 + 0.2j conjugated is 0.1 - 0.2j
    assert np.allclose(seen[0][0], 0.1 - 0.2j), "swap_iq did not conjugate the block"


def test_swap_iq_defaults_off():
    cfg = sdr_cfg()
    r = SdrReader(cfg, device=FakeDevice(lambda n: np.zeros(n, np.complex64)))
    assert r.swap_iq is False
    r.close()
