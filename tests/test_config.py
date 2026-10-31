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


def test_shipped_config_carries_the_flight_calibration_and_a2_still_holds(tmp_path, base):
    """Amendment A2 is the *mechanism*: null calibration -> no bearing, ever.
    Since 1.0.0 the shipped file carries the flight-validated constants
    (phase_sign +1, phase_offset_deg +2.12); the mechanism is exercised here
    by nulling them, and the shipped values are pinned so a stray edit shows.
    """
    cfg = load(DEFAULT)
    assert cfg.calibrated
    assert cfg.bearing.phase_sign == 1
    assert abs(cfg.bearing.phase_offset_deg - 2.12) < 1e-9
    base["bearing"].update(phase_sign=None, phase_offset_deg=None)
    assert not load(write(tmp_path, base)).calibrated


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
    (lambda d: d["dsp"].update(hop_s=d["dsp"]["integration_s"] + 1.0), "hop_s"),
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


def test_device_selection_keys_are_accepted_and_optional(tmp_path, base):
    """A27. Optional, so a config written before the aircraft had three dongles
    still loads -- but present in the shipped file so the next builder sees them."""
    from rbvor.config import load as _load
    shipped = json.loads(DEFAULT.read_text())["sdr"]
    assert shipped["device_serial"] == "" and shipped["device_index"] == 0

    base["sdr"].update(device_serial="VOR01", device_index=2)
    cfg = load(write(tmp_path, base))
    assert cfg.sdr["device_serial"] == "VOR01" and cfg.sdr["device_index"] == 2

    base["sdr"].pop("device_serial")
    base["sdr"].pop("device_index")
    assert _load(write(tmp_path, base)).sdr.get("device_serial", "") == ""


def test_shipped_gains_are_steps_of_the_tuners_they_name():
    """A28. The first version of this test hard-coded the R820T2 ladder and
    would have passed a value the aircraft's tuner cannot produce: the VOR
    dongle is an **FC0012** (five steps, max 19.2 dB). Each profile must name a
    gain its own tuner can actually produce, and the base must be safe on the
    tuner that is fitted, since that is the fallback."""
    fc0012 = {-9.9, -4.0, 7.1, 17.9, 19.2}
    r820t2 = {0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7,
              20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2,
              42.1, 43.4, 43.9, 44.5, 48.0, 49.6}
    sdr = json.loads(DEFAULT.read_text())["sdr"]
    assert sdr["gain_db"] in fc0012
    assert sdr["tuner_profiles"]["FC0012"]["gain_db"] in fc0012
    assert sdr["tuner_profiles"]["R820T"]["gain_db"] in r820t2


def test_shipped_profiles_do_not_pre_judge_the_spectral_sense():
    """O21 is a measurement. `swap_iq` is deliberately absent from the shipped
    FC0012 profile: guessing it would send the next flight after a fault that
    may not exist (D8)."""
    prof = json.loads(DEFAULT.read_text())["sdr"]["tuner_profiles"]
    assert "swap_iq" not in prof["FC0012"]
    assert "swap_iq" not in prof["R820T"]


def test_a_profile_key_that_is_not_a_tuner_is_rejected(tmp_path, base):
    base["sdr"]["tuner_profiles"] = {"R820T3": {"gain_db": 30.0}}
    assert "tuner_profiles" in str(broken(tmp_path, base, lambda d: None))


def test_a_profile_cannot_smuggle_in_unrelated_settings(tmp_path, base):
    """The profile overlays three tuner properties; anything else belongs in
    the sdr block, where it is validated."""
    base["sdr"]["tuner_profiles"] = {"FC0012": {"sample_rate_hz": 250000}}
    assert "tuner_profiles" in str(broken(tmp_path, base, lambda d: None))


def test_keepalive_may_be_disabled_as_the_v2_spec_allows(tmp_path, base):
    """RB-VOR-IF v2.x section 3.4: keepalive_s = 0 sends purely on change; the
    staleness timeout is the consumer's job (section 4, item 2). The schema
    used to forbid 0 while the spec permitted it (A20)."""
    base["publish"]["keepalive_s"] = 0
    assert load(write(tmp_path, base)).publish["keepalive_s"] == 0


def test_shipped_default_carries_no_dead_publish_keys():
    """A20: mode / period_s / deadband_deg / signal_scale stopped meaning
    anything with v2.0 and must not be shipped as if they did."""
    from rbvor.config import DEPRECATED_PUBLISH_KEYS
    pub = json.loads(DEFAULT.read_text())["publish"]
    assert not set(DEPRECATED_PUBLISH_KEYS) & set(pub), pub


def test_pre_v2_config_with_dead_publish_keys_still_loads(tmp_path, base, caplog):
    """A config written for v1.0 keeps loading -- with a warning naming the
    keys -- so an upgrade does not refuse to start over settings that are
    merely obsolete."""
    import logging
    base["publish"].update(mode="periodic", period_s=1.0, deadband_deg=1,
                           signal_scale="percent")
    with caplog.at_level(logging.WARNING, logger="rbvor.config"):
        cfg = load(write(tmp_path, base))
    assert cfg.publish["bearing_ref"] == "from"
    assert "signal_scale" in caplog.text and "no longer do anything" in caplog.text


def test_allowlist_denylist_overlap_is_rejected(tmp_path, base):
    e = broken(tmp_path, base,
               lambda d: d["frequency"].update(allowlist_mhz=[109.5],
                                               denylist_mhz=[109.5]))
    assert "denylist" in str(e)


def test_open_item_o4_is_switchable_not_hardcoded(tmp_path, base):
    """Q2 (FROM vs TO) is still unanswered; both branches must be reachable
    from config. Q1 (signal_scale) became moot with v2.0 -- see A20."""
    base["publish"].update(bearing_ref="to")
    cfg = load(write(tmp_path, base))
    assert cfg.publish["bearing_ref"] == "to"
