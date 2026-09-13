"""M9 -- solution logging, RB-Cloud schema, and flag-triggered IQ capture."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.config import Config  # noqa: E402
from rbvor.controller import Controller  # noqa: E402
from rbvor.publisher import Publisher, RecordingTransport  # noqa: E402
from rbvor.record import (  # noqa: E402
    IQMeta,
    RingRecorder,
    encode_complex,
    read_sidecar,
    to_complex64,
)
from rbvor.solutionlog import (  # noqa: E402
    SOLUTION_SCHEMA,
    SolutionLogger,
    SolutionRecord,
    schema_markdown,
)
from tools.vor_gen import VorParams, generate_blocks  # noqa: E402

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
        self.tuned = []

    def set_frequency(self, nav_hz):
        self.tuned.append(nav_hz)

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

def test_schema_has_the_fields_needed_to_re_derive_a_bearing():
    """A disputed reading must be reconstructable, not just believed."""
    for f in ("t_utc", "channel_khz", "vordir", "vorsignal", "flagged",
              "q_am", "q_sub", "q_snr", "q_stab", "amp_a", "sub_level",
              "deviation_hz", "snr_db", "iq_ref"):
        assert f in SOLUTION_SCHEMA


def test_schema_markdown_renders_every_field():
    md = schema_markdown()
    for name in SOLUTION_SCHEMA:
        assert f"`{name}`" in md


def test_cloud_record_carries_schema_provenance():
    rec = SolutionRecord(t_utc="2026-07-24T00:00:00Z", t_mono=1.0,
                         channel_khz=109_400, state="TRACKING", vordir=137,
                         vorsignal=99, flagged=False)
    d = rec.as_cloud_record()
    assert d["schema"].startswith("rb-vor.solution/")
    assert d["vordir"] == 137


# --------------------------------------------------------------------------- #
# CSV logger
# --------------------------------------------------------------------------- #

def test_csv_writes_header_once_and_appends(tmp_path):
    p = tmp_path / "sol.csv"
    lg = SolutionLogger(p)
    lg.write(SolutionRecord(t_utc="t1", t_mono=1.0, channel_khz=109_400,
                            state="TRACKING", vordir=137, vorsignal=99, flagged=False))
    lg.close()
    lg2 = SolutionLogger(p)                          # reopen: no second header
    lg2.write(SolutionRecord(t_utc="t2", t_mono=2.0, channel_khz=109_400,
                             state="NO_SIGNAL", vordir=None, vorsignal=0, flagged=True))
    lg2.close()
    lines = p.read_text().strip().splitlines()
    assert lines[0].startswith("t_utc,")
    assert len(lines) == 3
    assert lines.count(lines[0]) == 1


def test_flagged_row_has_empty_vordir(tmp_path):
    p = tmp_path / "sol.csv"
    lg = SolutionLogger(p)
    lg.write(SolutionRecord(t_utc="t", t_mono=1.0, channel_khz=109_400,
                            state="NO_SIGNAL", vordir=None, vorsignal=0, flagged=True))
    lg.close()
    row = p.read_text().strip().splitlines()[1].split(",")
    header = p.read_text().strip().splitlines()[0].split(",")
    assert row[header.index("vordir")] == ""
    assert row[header.index("vorsignal")] == "0"


def test_logger_without_a_path_is_a_no_op():
    lg = SolutionLogger(None)
    lg.write(SolutionRecord(t_utc="t", t_mono=1.0, channel_khz=None,
                            state="IDLE", vordir=None, vorsignal=0, flagged=True))
    lg.close()
    assert lg.rows_written == 0


def test_write_failure_is_dropped_not_raised(tmp_path, monkeypatch):
    lg = SolutionLogger(tmp_path / "sol.csv")

    def boom(_):
        raise OSError("disk full")
    monkeypatch.setattr(lg._writer, "writerow", boom)
    lg.write(SolutionRecord(t_utc="t", t_mono=1.0, channel_khz=1, state="X",
                            vordir=1, vorsignal=1, flagged=False))   # must not raise
    assert lg.write_errors == 1


# --------------------------------------------------------------------------- #
# encode round-trip
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fmt", ["cu8", "cs8", "cs16", "cf32"])
def test_encode_decode_round_trip(fmt):
    z = (np.random.default_rng(0).uniform(-0.6, 0.6, 4000)
         + 1j * np.random.default_rng(1).uniform(-0.6, 0.6, 4000)).astype(np.complex64)
    back = to_complex64(encode_complex(z, fmt), fmt)
    tol = 1e-6 if fmt == "cf32" else 1.0 / 100
    assert np.max(np.abs(z - back)) < tol


# --------------------------------------------------------------------------- #
# ring recorder
# --------------------------------------------------------------------------- #

def _meta():
    return IQMeta(format="cu8", sample_rate_hz=FS, center_freq_hz=109_336_000,
                  nav_freq_hz=109_400_000, tuner_offset_hz=-FS // 4, site="bench")


def test_ring_is_bounded_to_max_seconds(tmp_path):
    r = RingRecorder(_meta(), tmp_path, max_seconds=1.0, fmt="cu8")
    for _ in range(30):
        r.push(np.zeros(FS // 10, dtype=np.complex64))   # 3 s pushed
    assert r._held / FS <= 1.3                            # ~1 s held, not 3


def test_trigger_writes_ring_with_a_reason(tmp_path):
    r = RingRecorder(_meta(), tmp_path, max_seconds=2.0, fmt="cu8")
    for _ in range(10):
        r.push(np.full(FS // 10, 0.2, dtype=np.complex64))
    path = r.trigger(reason="no subcarrier", extra={"channel_khz": 109_400})
    assert path is not None and path.exists()
    m = read_sidecar(path)
    assert "no subcarrier" in m.notes
    assert m.synthetic is None
    assert r.captures_written == 1


def test_trigger_on_empty_ring_returns_none(tmp_path):
    r = RingRecorder(_meta(), tmp_path, max_seconds=2.0, fmt="cu8")
    assert r.trigger("x") is None


def test_reset_drops_the_ring(tmp_path):
    r = RingRecorder(_meta(), tmp_path, max_seconds=2.0, fmt="cu8")
    r.push(np.zeros(FS, dtype=np.complex64))
    r.reset()
    assert r.trigger("after reset") is None


# --------------------------------------------------------------------------- #
# controller integration -- the point of M9
# --------------------------------------------------------------------------- #

def _cfg(tmp_path, trigger="on_flag"):
    raw = copy.deepcopy(BASE)
    raw["sdr"].update(sample_rate_hz=FS, tuner_offset_hz=-FS // 4,
                      block_samples=1 << 15)
    raw["bearing"].update(phase_sign=1, phase_offset_deg=-1.9657)
    raw["record"].update(enabled=True, trigger=trigger, dir=str(tmp_path / "iq"))
    return Config(raw=raw)


def _drive(controller, clock, radial, seconds=8.0, snr=None):
    controller.set_channel(109.4)
    clock.advance(0.4)
    controller.tick()
    clock.advance(0.4)
    controller.tick()
    p = VorParams(radial_deg=radial, fs=FS, seconds=seconds, offset_hz=-FS // 4,
                  snr_db=snr, ident="", block=1 << 15)
    for blk in generate_blocks(p):
        clock.advance(0.1)
        controller.push(blk)


def test_solution_sink_receives_a_row_per_publish(tmp_path):
    cfg = _cfg(tmp_path)
    clock = FakeClock()
    rows = []
    c = Controller(cfg, FakeSdr(), Publisher(cfg.publish, RecordingTransport(),
                                             clock=clock),
                   clock=clock, solution_sink=rows.append)
    _drive(c, clock, 137)
    assert rows, "no solution rows emitted"
    good = [r for r in rows if not r.flagged]
    assert good and all(0 <= r.vordir <= 359 for r in good)
    # the pre-tune flush row legitimately has channel_khz None (F3: the flag
    # goes out before channel_khz is set); assert on the decoding rows
    assert all(r.channel_khz == 109_400 for r in rows if r.state == "TRACKING")
    assert any(r.q_sub > 0 for r in good)            # quality terms populated


def test_flag_triggers_an_iq_capture_referenced_in_the_row(tmp_path):
    """The forensic loop end to end: a NAV flag writes the IQ that caused it,
    and the solution row names that file so they can be matched later."""
    cfg = _cfg(tmp_path, trigger="on_flag")
    clock = FakeClock()
    rows = []
    meta = IQMeta(format="cu8", sample_rate_hz=FS, center_freq_hz=109_336_000,
                  nav_freq_hz=109_400_000, tuner_offset_hz=-FS // 4)
    recorder = RingRecorder(meta, tmp_path / "iq", max_seconds=3.0, fmt="cu8")
    c = Controller(cfg, FakeSdr(), Publisher(cfg.publish, RecordingTransport(),
                                             clock=clock),
                   clock=clock, recorder=recorder, solution_sink=rows.append)

    # good signal, then drive it into a flag with noise-only IQ
    _drive(c, clock, 137, seconds=6.0)
    noise = (np.random.default_rng(0).normal(size=FS * 3)
             + 1j * np.random.default_rng(1).normal(size=FS * 3)).astype(np.complex64)
    for i in range(0, noise.size, 1 << 15):
        clock.advance(0.1)
        c.push(noise[i:i + (1 << 15)])

    assert recorder.captures_written >= 1, "flag did not trigger a capture"
    captures = list((tmp_path / "iq").glob("*.cu8"))
    assert captures, "no IQ file written"
    flagged_rows = [r for r in rows if r.flagged and r.iq_ref]
    assert flagged_rows, "no flagged row referenced its capture"
    assert any(r.iq_ref == captures[0].name for r in flagged_rows)


def test_no_recorder_and_no_sink_is_the_default_and_changes_nothing(tmp_path):
    """Every pre-M9 test constructs the controller without these; the bearing
    path must be identical."""
    raw = copy.deepcopy(BASE)
    raw["sdr"].update(sample_rate_hz=FS, tuner_offset_hz=-FS // 4,
                      block_samples=1 << 15)
    raw["bearing"].update(phase_sign=1, phase_offset_deg=-1.9657)
    cfg = Config(raw=raw)
    clock = FakeClock()
    t = RecordingTransport()
    c = Controller(cfg, FakeSdr(), Publisher(cfg.publish, t, clock=clock),
                   clock=clock)
    _drive(c, clock, 213)
    good = [s for s in t.sent if s["vorstatus"] >= 4]     # A20: status is the gate
    assert good and all(abs(((s["vorradial"] - 213 + 180) % 360) - 180) <= 3
                        for s in good)
