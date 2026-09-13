"""The daemon --csv dump: one row per window, exact column contract."""

from __future__ import annotations

import csv
from dataclasses import dataclass

import numpy as np

from rbvor.obscsv import FIELDS, ObsCsvSink


@dataclass
class FakeObs:
    t: float = 1.5
    phi_a: float = 0.5
    phi_b: float = -0.3
    amp_a: float = 0.30
    amp_b: float = 479.0
    carrier: float = 0.43
    sub_level: float = 0.29
    noise_est: float = 0.0006
    overrun: bool = False

    @property
    def delta_phi(self) -> float:
        return float((self.phi_a - self.phi_b + np.pi) % (2 * np.pi) - np.pi)

    @property
    def snr_db(self) -> float:
        return float(20 * np.log10(max(self.amp_a, 1e-12) / max(self.noise_est, 1e-12)))


def test_header_is_the_exact_requested_contract(tmp_path):
    p = tmp_path / "out.csv"
    sink = ObsCsvSink(p)
    sink.close()
    header = p.read_text().splitlines()[0]
    assert header == ("t,frequency,delta_phi_deg,phi_a_deg,phi_b_deg,amp_a,"
                      "amp_b,sub_level,carrier,noise_est,snr_db,overrun")
    assert FIELDS[1] == "frequency"


def test_rows_carry_frequency_and_all_fields(tmp_path):
    p = tmp_path / "out.csv"
    sink = ObsCsvSink(p)
    sink(FakeObs(), 109_400)
    sink(FakeObs(t=2.5, overrun=True), 113_350)
    sink.close()
    assert sink.rows == 2

    with p.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["frequency"] == "109400"
    assert rows[1]["frequency"] == "113350"
    assert rows[1]["overrun"] == "1"
    # delta_phi is wrapped to 0..360
    assert 0.0 <= float(rows[0]["delta_phi_deg"]) < 360.0


def test_overrun_and_warming_windows_are_included(tmp_path):
    """The dump is the raw stream: overrun windows appear too, unlike the
    published solution log which drops them."""
    p = tmp_path / "out.csv"
    sink = ObsCsvSink(p)
    sink(FakeObs(overrun=True), 109_400)
    sink.close()
    with p.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["overrun"] == "1"


def test_file_flushes_so_a_killed_run_keeps_data(tmp_path):
    p = tmp_path / "out.csv"
    sink = ObsCsvSink(p)
    sink(FakeObs(), 109_400)
    # not closed yet -- but flushed, so the row is already on disk
    text = p.read_text()
    assert "109400" in text
    sink.close()
