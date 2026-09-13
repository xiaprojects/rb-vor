"""M6 -- configuration loading and validation.

The daemon must refuse to start on a bad config, and must name the offending
path when it does. A config that loads with a silently-ignored typo is worse
than one that fails loudly.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.config import Config, ConfigError, load  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "config" / "rb-vor.json"


@pytest.fixture
def base() -> dict:
    return json.loads(DEFAULT.read_text())


def write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "rb-vor.json"
    p.write_text(json.dumps(data))
    return p


def broken(tmp_path, base, mutate) -> ConfigError:
    d = copy.deepcopy(base)
    mutate(d)
    with pytest.raises(ConfigError) as e:
        load(write(tmp_path, d))
    return e.value


# --------------------------------------------------------------------------- #
# the shipped default must be valid
# --------------------------------------------------------------------------- #

def test_shipped_default_config_loads():
    cfg = load(DEFAULT)
    assert isinstance(cfg, Config)
    assert cfg.dsp.fs == 1_024_000
    assert cfg.dsp.offset_hz == -256_000            # amendment A1
    assert cfg.record["format"] == "cu8"            # amendment A4
    assert cfg.dsp.sub_lp_hz < 2000                 # amendment A9


def test_shipped_default_is_uncalibrated():
    """Amendment A2: the shipped config must not contain a usable calibration."""
    cfg = load(DEFAULT)
    assert not cfg.calibrated
    assert cfg.bearing.phase_sign is None
    assert cfg.bearing.phase_offset_deg is None


def test_quality_defaults_match_the_a11_derivation():
    q = load(DEFAULT).quality
    assert q.weights == {"am": 0.25, "sub": 0.10, "snr": 0.25, "stab": 0.40}
    assert (q.snr_lo_db, q.snr_hi_db) == (26.0, 46.0)
    assert q.flag_stab_threshold == 0.40


# --------------------------------------------------------------------------- #
# failure modes
# --------------------------------------------------------------------------- #

def test_missing_file_is_reported():
    with pytest.raises(ConfigError, match="not found"):
        load("/nonexistent/rb-vor.json")


def test_malformed_json_reports_the_line(tmp_path):
    p = tmp_path / "rb-vor.json"
    p.write_text('{"version": "1.0",,}')
    with pytest.raises(ConfigError, match="line"):
        load(p)


def test_unknown_key_is_rejected_not_ignored(tmp_path, base):
    """A mistyped key that is silently accepted means the operator believes they
    set something they did not."""
    e = broken(tmp_path, base, lambda d: d["sdr"].update(gain_dB=40))
    assert "sdr" in str(e)


def test_missing_section_is_rejected(tmp_path, base):
    e = broken(tmp_path, base, lambda d: d.pop("quality"))
    assert "quality" in str(e)


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["sdr"].update(sample_rate_hz=250_000), "sample_rate_hz"),
    (lambda d: d["sdr"].update(tuner_offset_hz=900_000), "tuner_offset_hz"),
    (lambda d: d["dsp"].update(sub_lp_hz=2500), "sub_lp_hz"),
    (lambda d: d["dsp"].update(integration_s=1.017), "integration_s"),
    (lambda d: d["dsp"].update(hop_s=2.0), "hop_s"),
    (lambda d: d["frequency"].update(max_mhz=107.0), "max_mhz"),
    (lambda d: d["quality"]["weights"].update(am=0.9), "weights"),
    (lambda d: d["quality"].update(snr_hi_db=10.0), "snr_hi_db"),
])
def test_cross_field_rules_name_the_offending_path(tmp_path, base, mutate, expect):
    assert expect in str(broken(tmp_path, base, mutate))


def test_a9_guard_is_enforced_by_config_not_only_by_dsp(tmp_path, base):
    """sub_lp_hz above the 4 kHz demodulation Nyquist is doc A's original,
    broken ordering. It must be impossible to configure."""
    e = broken(tmp_path, base, lambda d: d["dsp"].update(sub_lp_hz=1999.5))
    assert "sub_lp_hz" in str(e)


def test_half_a_calibration_is_rejected(tmp_path, base):
    """Amendment A2: both constants or neither. One of each is how a wrong
    bearing gets published."""
    e = broken(tmp_path, base,
               lambda d: d["bearing"].update(phase_sign=1, phase_offset_deg=None))
    assert "phase_sign" in str(e) and "both" in str(e)


def test_a_complete_calibration_is_accepted(tmp_path, base):
    base["bearing"].update(phase_sign=1, phase_offset_deg=-1.9657)
    cfg = load(write(tmp_path, base))
    assert cfg.calibrated
    assert cfg.bearing.phase_sign == 1


def test_on_change_without_keepalive_is_rejected(tmp_path, base):
    """Doc A 10.3: a consumer could not distinguish 'steady' from 'dead'."""
    e = broken(tmp_path, base,
               lambda d: d["publish"].update(mode="on_change", keepalive_s=0.5))
    assert "keepalive_s" in str(e)


def test_allowlist_denylist_overlap_is_rejected(tmp_path, base):
    e = broken(tmp_path, base,
               lambda d: d["frequency"].update(allowlist_mhz=[109.5],
                                               denylist_mhz=[109.5]))
    assert "denylist" in str(e)


def test_open_items_o3_o4_are_switchable_not_hardcoded(tmp_path, base):
    """Q1/Q2 are unanswered; both branches must be reachable from config."""
    base["publish"].update(signal_scale="boolean", bearing_ref="to")
    cfg = load(write(tmp_path, base))
    assert cfg.publish["signal_scale"] == "boolean"
    assert cfg.publish["bearing_ref"] == "to"
