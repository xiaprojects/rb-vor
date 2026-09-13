"""M4 -- quality metric (unit) and T2/T3/T4 accuracy acceptance (integration).

T2  <= 0.5 deg RMS, <= 1.0 deg peak at 30 dB SNR
T3  <= 2 deg at 10 dB; `vorsignal` monotonic in SNR; flag asserts below usable
T4  CVOR and DVOR decode identically at the SAME phase_sign (amendment A6);
    a sense-reversed DVOR decodes to the negation

The quality thresholds and weights under test are the ones derived from the
sweep in amendment A11, not doc A's assumed values.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.bearing import BearingCal, angular_difference  # noqa: E402
from rbvor.quality import QualityEstimator, QualityParams  # noqa: E402
from tools.vor_sweep import run_one  # noqa: E402

CAL = BearingCal(phase_sign=1, phase_offset_deg=-1.9657)
QP = QualityParams()
RADIALS = (0, 45, 137, 213, 300)


@dataclass
class FakeObs:
    amp_a: float = 0.30
    sub_level: float = 0.30
    snr_db: float = 50.0
    overrun: bool = False


def sweep(snr, radials=RADIALS, seconds=12.0, vor_type="cvor"):
    out = [run_one(r, snr, CAL, QP, seconds=seconds, vor_type=vor_type)
           for r in radials]
    return [r for r in out if r]


def rms(rows):
    return float(np.sqrt(np.mean([r["err_mean"] ** 2 for r in rows])))


# --------------------------------------------------------------------------- #
# quality -- unit
# --------------------------------------------------------------------------- #

def test_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1"):
        QualityParams(weights={"am": 0.5, "sub": 0.5, "snr": 0.5, "stab": 0.5})


def test_missing_weight_rejected():
    with pytest.raises(ValueError, match="missing"):
        QualityParams(weights={"am": 0.5, "sub": 0.5})


def test_snr_mapping_covers_the_measured_range_not_the_channel_range():
    """Amendment A11: doc A's 0-20 dB mapping saturated across the whole useful
    range because the DSP reports post-integration tone SNR (17-68 dB)."""
    p = QualityParams()
    assert p.snr_lo_db >= 20.0
    assert p.snr_hi_db > p.snr_lo_db
    q = QualityEstimator(p)
    lo = q.update(FakeObs(snr_db=p.snr_lo_db - 5), 100.0).q_snr
    mid = q.update(FakeObs(snr_db=(p.snr_lo_db + p.snr_hi_db) / 2), 100.0).q_snr
    hi = q.update(FakeObs(snr_db=p.snr_hi_db + 5), 100.0).q_snr
    assert lo == 0.0 and hi == 1.0
    assert 0.3 < mid < 0.7


def test_perfect_signal_scores_full_marks():
    q = QualityEstimator()
    for _ in range(6):
        r = q.update(FakeObs(), 100.0)
    assert r.value == 100
    assert not r.flagged
    assert r.band == "good"


def test_flag_on_missing_subcarrier():
    q = QualityEstimator()
    for _ in range(4):
        r = q.update(FakeObs(sub_level=0.01), 100.0)
    assert r.flagged and r.value == 0 and "subcarrier" in r.reason


def test_flag_on_weak_am():
    q = QualityEstimator()
    for _ in range(4):
        r = q.update(FakeObs(amp_a=0.05), 100.0)
    assert r.flagged and r.value == 0 and "30 Hz AM" in r.reason


def test_flag_on_unstable_bearing():
    """The term that actually predicts error, per the A11 sweep."""
    q = QualityEstimator()
    for b in (0.0, 90.0, 200.0, 300.0, 40.0):
        r = q.update(FakeObs(), b)
    assert r.flagged and r.value == 0 and "unstable" in r.reason


def test_flag_while_acquiring():
    """No bearing history means no dispersion estimate, so no confidence."""
    q = QualityEstimator()
    r = q.update(FakeObs(), 100.0)
    assert r.flagged and r.value == 0 and "acquiring" in r.reason


def test_flag_on_overrun_window():
    """F9: a window spanning a sample gap must never be published."""
    q = QualityEstimator()
    for _ in range(4):
        q.update(FakeObs(), 100.0)
    r = q.update(FakeObs(overrun=True), 100.0)
    assert r.flagged and r.value == 0 and "overrun" in r.reason


def test_not_tracking_is_flagged():
    q = QualityEstimator()
    for _ in range(4):
        q.update(FakeObs(), 100.0)
    r = q.update(FakeObs(), 100.0, tracking=False)
    assert r.flagged and r.value == 0


def test_reset_clears_bearing_history():
    q = QualityEstimator()
    for _ in range(5):
        q.update(FakeObs(), 100.0)
    q.reset()
    assert q.update(FakeObs(), 100.0).flagged      # back to acquiring


def test_terms_are_reported_individually():
    """A single scalar is useless for diagnosing an installation (doc A 9)."""
    q = QualityEstimator()
    for _ in range(4):
        r = q.update(FakeObs(), 100.0)
    d = r.as_dict()
    assert {"q_am", "q_sub", "q_snr", "q_stab", "vorsignal"} <= set(d)


def test_published_value_stays_in_range():
    q = QualityEstimator()
    for obs in (FakeObs(amp_a=9.0, sub_level=9.0, snr_db=999.0), FakeObs()):
        for _ in range(4):
            r = q.update(obs, 100.0)
        assert 0 <= r.value <= 100


# --------------------------------------------------------------------------- #
# T2 / T3 -- accuracy acceptance
# --------------------------------------------------------------------------- #

def test_T2_accuracy_at_30_db():
    rows = sweep(30)
    assert rms(rows) <= 0.5
    assert max(r["err_max"] for r in rows) <= 1.0


def test_T3_accuracy_at_10_db():
    rows = sweep(10)
    assert rms(rows) <= 2.0


def test_T3_vorsignal_is_monotonic_in_snr():
    vals = [np.mean([r["vorsignal"] for r in sweep(s, radials=(0, 137, 250))])
            for s in (30, 20, 10, 6, 3)]
    assert all(a >= b - 2.0 for a, b in zip(vals, vals[1:], strict=False))
    assert vals[0] > vals[-1] + 20.0


def test_T3_flag_asserts_before_the_error_becomes_misleading():
    """F1 is the whole point of `vorsignal`. At the SNR where the bearing error
    passes the 5 deg T11 limit, the flag must already be up."""
    bad = sweep(-3, radials=(0, 137, 250))
    assert rms(bad) > 5.0                       # genuinely misleading
    assert np.mean([r["flagged"] for r in bad]) > 0.9

    good = sweep(10, radials=(0, 137, 250))
    assert rms(good) < 2.0
    assert np.mean([r["flagged"] for r in good]) == 0.0


def test_no_flag_raised_on_a_healthy_signal():
    assert all(r["flagged"] == 0.0 for r in sweep(30))


# --------------------------------------------------------------------------- #
# T4 -- CVOR / DVOR equivalence through the full bearing path (A6)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("radial", [0, 137, 300])
def test_T4_dvor_decodes_identically_at_the_same_sign(radial):
    c = run_one(radial, 30, CAL, QP, seconds=12.0, vor_type="cvor")
    d = run_one(radial, 30, CAL, QP, seconds=12.0, vor_type="dvor")
    assert c["err_mean"] < 0.5
    assert d["err_mean"] < 0.5


def test_T4_reversed_dvor_is_wrong_by_the_expected_amount():
    radial = 137
    r = run_one(radial, 30, CAL, QP, seconds=12.0, vor_type="dvor-reversed")
    # decodes to -radial, i.e. wrong by 2*radial modulo 360
    assert r["err_mean"] > 10.0
    assert angular_difference(r["err_mean"], angular_difference(-radial, radial)) < 2.0
