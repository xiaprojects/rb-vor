"""Configuration loading and validation.

Reference: rb-vor-architecture.md section 11; implementation plan M6.

Two rules, both of which exist because a misconfigured avionics daemon that
starts happily is worse than one that refuses to start:

1. **Refuse to run on an invalid config**, reporting the offending path.
2. **No default is duplicated in code.** The shipped `config/rb-vor.json` is the
   single source of defaults; this module reads it and nothing else invents a
   value. `additionalProperties` is false throughout the schema, so a mistyped
   key is an error rather than a silently ignored setting.

Some constraints cannot be expressed in JSON Schema and are checked here:
relationships between fields, and the amendment A2 rule that the two bearing
calibration constants are either both present or both null.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rbvor.bearing import BearingCal
from rbvor.dsp import INTERMEDIATE_HZ, DspParams
from rbvor.loc import LocDspParams, LocQualityParams
from rbvor.quality import QualityParams

__all__ = ["Config", "ConfigError", "load", "DEFAULT_PATHS", "SCHEMA_PATH",
           "LocSettings", "LOC_DEFAULTS"]

log = logging.getLogger("rbvor.config")

#: `publish` keys that stopped meaning anything with RB-VOR-IF v2.0 (A20):
#: transmit-on-change is the only policy and there is no synthetic score on
#: the wire. Still accepted so a pre-v2.0 config loads; warned about so it
#: gets cleaned up.
DEPRECATED_PUBLISH_KEYS = ("mode", "period_s", "deadband_deg", "signal_scale")

_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = _ROOT / "schema" / "rb-vor.schema.json"
DEFAULT_PATHS = (
    Path("/etc/rb-vor/rb-vor.json"),
    _ROOT / "config" / "rb-vor.json",
)


#: AMENDMENT A33: the `loc` block is optional, so the defaults live here and
#: not in the shipped file -- the one deliberate exception to rule 2 above,
#: made for the same reason as the optional `dsp` keys of A25: a config on the
#: Pi's boot partition, edited by hand and never regenerated, must keep loading
#: after an upgrade (F6 shows what a config/schema mismatch does in the field).
#: Quality figures are those of `LocQualityParams`, measured in A37.
LOC_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "integration_s": 1.0,
    "hop_s": 0.5,
    "tone_search": 0.03,
    "smooth_n": 1,
}


@dataclass(frozen=True)
class LocSettings:
    """Typed view of the optional `loc` block (A33)."""

    enabled: bool
    dsp: LocDspParams
    quality: LocQualityParams
    smooth_n: int
    solution_csv: str | None


class ConfigError(ValueError):
    """Raised for any invalid configuration. Always names the offending path."""


def _fail(path: str, message: str) -> None:
    raise ConfigError(f"config{path}: {message}")


@dataclass(frozen=True)
class Config:
    """Validated configuration, with typed views onto the DSP sub-objects."""

    raw: dict[str, Any]
    source: Path | None = None
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    # -- typed views ------------------------------------------------------- #

    @property
    def dsp(self) -> DspParams:
        d = self.raw["dsp"]
        return DspParams(
            fs=self.raw["sdr"]["sample_rate_hz"],
            offset_hz=self.raw["sdr"]["tuner_offset_hz"],
            channel_lp_hz=d["channel_lp_hz"], channel_taps=d["channel_taps"],
            subcarrier_hz=d["subcarrier_hz"], sub_lp_hz=d["sub_lp_hz"],
            sub_taps=d["sub_taps"], tone_lp_hz=d["tone_lp_hz"],
            tone_taps=d["tone_taps"], tone_hz=d["tone_hz"],
            agc_tau_s=d["agc_tau_s"], integration_s=d["integration_s"],
            hop_s=d["hop_s"], noise_bins_hz=tuple(d["noise_bins_hz"]),
            # optional since A25; DspParams holds the defaults so an older
            # config (the field one, for instance) keeps loading
            **{k: d[k] for k in ("afc_threshold_hz", "afc_tau_s", "fm_estimator",
                                 "fm_beta", "sub_search_hz") if k in d},
        )

    @property
    def bearing(self) -> BearingCal:
        b = self.raw["bearing"]
        return BearingCal(phase_sign=b["phase_sign"],
                          phase_offset_deg=b["phase_offset_deg"],
                          station_cal_deg=b["station_cal_deg"],
                          smooth_n=b["smooth_n"])

    @property
    def quality(self) -> QualityParams:
        q = self.raw["quality"]
        return QualityParams(
            weights=dict(q["weights"]),
            am_nominal_depth=q["am_nominal_depth"],
            sub_nominal_depth=q["sub_nominal_depth"],
            snr_lo_db=q["snr_lo_db"], snr_hi_db=q["snr_hi_db"],
            stab_full_deg=q["stab_full_deg"],
            flag_sub_threshold=q["flag_sub_threshold"],
            flag_am_threshold=q["flag_am_threshold"],
            flag_stab_threshold=q["flag_stab_threshold"],
            history=q["history"],
        )

    # -- plain sections ---------------------------------------------------- #

    @property
    def bridge(self) -> dict:
        return self.raw["bridge"]

    @property
    def publish(self) -> dict:
        return self.raw["publish"]

    @property
    def sdr(self) -> dict:
        return self.raw["sdr"]

    @property
    def frequency(self) -> dict:
        return self.raw["frequency"]

    @property
    def record(self) -> dict:
        return self.raw["record"]

    @property
    def log(self) -> dict:
        return self.raw["log"]

    @property
    def calibrated(self) -> bool:
        return self.bearing.calibrated

    @property
    def loc(self) -> LocSettings:
        """The localizer settings; disabled defaults when the block is absent."""
        raw = self.raw.get("loc") or {}
        v = {**LOC_DEFAULTS, **{k: raw[k] for k in LOC_DEFAULTS if k in raw}}
        q = dict(raw.get("quality") or {})
        q.pop("_note", None)
        if "weights" in q:
            q["weights"] = dict(q["weights"])
        if "solution_csv" in raw:
            csv_path = raw["solution_csv"] or None
        else:
            vor_csv = (self.raw.get("log") or {}).get("solution_csv")
            csv_path = str(Path(vor_csv).with_name("loc.csv")) if vor_csv else None
        return LocSettings(
            enabled=bool(v["enabled"]),
            dsp=LocDspParams.from_dsp(self.dsp, integration_s=v["integration_s"],
                                      hop_s=v["hop_s"], tone_search=v["tone_search"]),
            quality=LocQualityParams(**q),
            smooth_n=int(v["smooth_n"]),
            solution_csv=csv_path,
        )


def _validate_schema(data: dict) -> None:
    try:
        import jsonschema
    except ImportError as e:                                    # pragma: no cover
        raise ConfigError(
            "jsonschema is required to validate the configuration; "
            "install it rather than skipping validation"
        ) from e
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        path = "".join(f"[{p!r}]" for p in e.path)
        extra = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        _fail(path, e.message + extra)


def _validate_relationships(d: dict) -> None:
    """Cross-field rules that JSON Schema cannot express."""
    sdr, dsp, freq, pub = d["sdr"], d["dsp"], d["frequency"], d["publish"]

    if sdr["sample_rate_hz"] % INTERMEDIATE_HZ:
        _fail("['sdr']['sample_rate_hz']",
              f"must be a multiple of the {INTERMEDIATE_HZ} Hz intermediate rate")
    if abs(sdr["tuner_offset_hz"]) >= sdr["sample_rate_hz"] / 2:
        _fail("['sdr']['tuner_offset_hz']",
              "must be inside +/- fs/2, otherwise the carrier falls outside the "
              "sampled band")

    if dsp["subcarrier_hz"] + dsp["sub_lp_hz"] > INTERMEDIATE_HZ / 2:
        _fail("['dsp']['sub_lp_hz']",
              "subcarrier plus filter width exceeds the intermediate Nyquist limit")
    cycles = dsp["integration_s"] * dsp["tone_hz"]
    if abs(round(cycles) - cycles) > 1e-9:
        _fail("['dsp']['integration_s']",
              f"must be a whole number of {dsp['tone_hz']} Hz cycles "
              f"(got {cycles} cycles); otherwise spectral leakage biases the phase")
    if dsp["hop_s"] > dsp["integration_s"]:
        _fail("['dsp']['hop_s']", "must not exceed integration_s, or windows are skipped")

    if freq["max_mhz"] <= freq["min_mhz"]:
        _fail("['frequency']['max_mhz']", "must exceed min_mhz")
    overlap = set(freq["allowlist_mhz"]) & set(freq["denylist_mhz"])
    if overlap:
        _fail("['frequency']['denylist_mhz']",
              f"channels appear in both allowlist and denylist: {sorted(overlap)}")

    # Doc A section 10.3 forbade on_change without a keep-alive. Since RB-VOR-IF
    # v2.0 (A20) on-change is the only policy and the spec allows keepalive_s = 0,
    # placing the staleness timeout on the consumer (spec section 4, item 2).
    stale = [k for k in DEPRECATED_PUBLISH_KEYS if k in pub]
    if stale:
        log.warning("config['publish']: %s no longer do anything since RB-VOR-IF "
                    "v2.0 and can be removed", ", ".join(stale))

    b = d["bearing"]
    have = (b["phase_sign"] is not None, b["phase_offset_deg"] is not None)
    if any(have) and not all(have):
        _fail("['bearing']",
              "phase_sign and phase_offset_deg must both be set or both be null "
              "(amendment A2); run tools/vor_calibrate.py")

    w = d["quality"]["weights"]
    total = sum(w.values())
    if abs(total - 1.0) > 1e-6:
        _fail("['quality']['weights']", f"must sum to 1.0, got {total}")
    if d["quality"]["snr_hi_db"] <= d["quality"]["snr_lo_db"]:
        _fail("['quality']['snr_hi_db']", "must exceed snr_lo_db")


def load(path: str | Path | None = None) -> Config:
    """Load, validate and return the configuration.

    Raises `ConfigError` on any problem, naming the offending path. The daemon
    must not start on a failed load.
    """
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
    else:
        p = next((c for c in DEFAULT_PATHS if c.exists()), None)
        if p is None:
            raise ConfigError(
                "no configuration found; looked in "
                + ", ".join(str(c) for c in DEFAULT_PATHS)
            )
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p}: invalid JSON at line {e.lineno}: {e.msg}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top level must be an object")

    _validate_schema(data)
    _validate_relationships(data)

    # Constructing the typed views is itself a validation step: DspParams,
    # QualityParams and BearingCal all enforce their own invariants.
    cfg = Config(raw=data, source=p)
    try:
        _ = (cfg.dsp, cfg.bearing, cfg.quality, cfg.loc)
    except ValueError as e:
        raise ConfigError(f"{p}: {e}") from e
    return cfg
