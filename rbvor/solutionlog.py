"""Per-solution CSV log and the RB-Cloud record schema.

Reference: rb-vor-architecture.md sections 6 and 13; strategic objective 7
(cloud integration for forensic analysis); implementation plan M9.

Every published solution is appended to a CSV at 1 Hz, and the same record is
the unit uploaded to RB-Cloud. Two rules from the platform's standards
discipline shape this:

* **A documented schema and a retention/privacy note** for anything uploaded to
  RB-Cloud (project instructions section 6). The schema is `SOLUTION_SCHEMA`
  below; the note is `docs/cloud-schema.md`.
* **Integrity of records.** Each row carries the fields needed to reconstruct
  and independently re-derive the bearing offline -- so a disputed reading can
  be replayed, not just believed. The IQ that produced a flagged solution is
  captured separately (`record.py`) and referenced by timestamp.

The logger never blocks the DSP: it holds an open file handle and appends a
formatted line. A failure to write is logged once and dropped, exactly like a
failed publish -- a missing log row must never stall a bearing.

Privacy note (GDPR, project instructions section 2.7). A solution row contains
no personal data on its own. `gpsTrack` is aircraft state, not a person, but a
track log plus a tail number is identifying, so the retention note treats the
uploaded stream as personal data and `docs/cloud-schema.md` states the basis and
period. `station_cal_deg` and site coordinates, if present in a sidecar, are
installation data and are handled the same way.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["SolutionRecord", "SOLUTION_SCHEMA", "SolutionLogger", "SCHEMA_VERSION",
           "LocSolutionRecord", "LOC_SOLUTION_SCHEMA", "LOC_SCHEMA_VERSION",
           "loc_schema_markdown"]

log = logging.getLogger("rbvor.solutionlog")

SCHEMA_VERSION = "rb-vor.solution/1"

#: Field name -> (unit, description). This IS the published schema; the CSV
#: header, the RB-Cloud record and docs/cloud-schema.md are all generated from
#: it, so they cannot drift apart.
SOLUTION_SCHEMA: dict[str, tuple[str, str]] = {
    "t_utc": ("ISO-8601", "wall-clock time the solution was produced"),
    "t_mono": ("s", "monotonic stream time, window centre"),
    "channel_khz": ("kHz", "tuned navaid channel, integer"),
    "state": ("enum", "controller state: TRACKING/DEGRADED/NO_SIGNAL/..."),
    "vordir": ("deg mag", "published radial FROM the station, or empty when flagged"),
    "vorsignal": ("0-100", "published quality; 0 = NAV flag, vordir invalid"),
    "flagged": ("bool", "true when the NAV flag is asserted"),
    "reason": ("text", "human-readable state/flag reason"),
    "q_am": ("0-1", "30 Hz AM depth quality term"),
    "q_sub": ("0-1", "9960 Hz subcarrier band-power quality term"),
    "q_snr": ("0-1", "30 Hz tone SNR quality term"),
    "q_stab": ("0-1", "bearing-stability quality term"),
    "amp_a": ("frac", "measured 30 Hz AM depth, nominal 0.30"),
    "sub_level": ("frac", "measured subcarrier depth (band), nominal 0.30"),
    "deviation_hz": ("Hz", "measured FM deviation, nominal 480"),
    "carrier": ("a.u.", "smoothed carrier magnitude"),
    "snr_db": ("dB", "post-integration 30 Hz tone SNR"),
    "carrier_offset_hz": ("Hz", "carrier offset from the nominal down-mix, AFC-corrected (A25); "
                                "ppm = value / channel_khz / 1000"),
    "sub_offset_hz": ("Hz", "station subcarrier offset from 9960 Hz "
                            "(harmonic estimator, D7; else 0)"),
    "sub_coh": ("frac", "coherent subcarrier amplitude x2 (harmonic estimator, D7; else 0)"),
    "gps_track": ("deg true", "aircraft track from the bridge, diagnostics only"),
    "overrun": ("bool", "window touched a sample gap (discarded if true)"),
    "iq_ref": ("filename", "IQ capture that includes this window, if any"),
}

FIELDS = tuple(SOLUTION_SCHEMA)

#: AMENDMENT A33/A37 -- the localizer log is a SEPARATE file and schema. Adding
#: columns to SOLUTION_SCHEMA would change the header, and `SolutionLogger`
#: appends to an existing file without reading its header (it writes one only
#: to a new or empty file): every row after an upgrade would sit under the old
#: header, shifted. Checked in tests/test_loc_controller.py.
LOC_SCHEMA_VERSION = "rb-vor.loc-solution/1"

LOC_SOLUTION_SCHEMA: dict[str, tuple[str, str]] = {
    "t_utc": ("ISO-8601", "wall-clock time the solution was produced"),
    "t_mono": ("s", "monotonic time of the publish"),
    "channel_khz": ("kHz", "tuned localizer channel, integer"),
    "state": ("enum", "controller state: TRACKING/DEGRADED/NO_SIGNAL/..."),
    "locstatus": ("0-5", "published status code, >= 4 usable"),
    "ddm": ("DDM", "published DDM (smoothed, compression-corrected, D9), empty when "
                   "not usable; + = 150 Hz predominates = fly left"),
    "valid": ("bool", "true when locstatus >= 4"),
    "reason": ("text", "human-readable state/flag reason"),
    "q_sdm": ("0-1", "SDM quality term (SDM / 0.40)"),
    "q_snr": ("0-1", "tone SNR quality term"),
    "q_stab": ("0-1", "DDM steadiness about its trend"),
    "value": ("0-100", "internal localizer quality score; 0 = flagged"),
    "m90": ("frac", "90 Hz depth, nominal 0.20"),
    "m150": ("frac", "150 Hz depth, nominal 0.20"),
    "ddm_raw": ("DDM", "m150 - m90 of the last window, before correction"),
    "ddm_corrected": ("DDM", "raw DDM x max(1, 0.40 / SDM), before smoothing"),
    "sdm": ("frac", "m90 + m150, nominal 0.40"),
    "snr_db": ("dB", "mean tone amplitude against the off-tone bins"),
    "tone_offset": ("frac", "found fractional tone-frequency offset (A34)"),
    "carrier": ("a.u.", "smoothed carrier magnitude"),
    "carrier_offset_hz": ("Hz", "carrier offset from the nominal down-mix (A25)"),
    "gps_track": ("deg true", "aircraft track from the bridge, diagnostics only"),
    "overrun": ("bool", "window touched a sample gap"),
    "iq_ref": ("filename", "IQ capture that includes this window, if any"),
}

LOC_FIELDS = tuple(LOC_SOLUTION_SCHEMA)


@dataclass
class SolutionRecord:
    """One row. Built by the controller at publish time."""

    t_utc: str
    t_mono: float
    channel_khz: int | None
    state: str
    vordir: int | None
    vorsignal: int
    flagged: bool
    reason: str = ""
    q_am: float = 0.0
    q_sub: float = 0.0
    q_snr: float = 0.0
    q_stab: float = 0.0
    amp_a: float = 0.0
    sub_level: float = 0.0
    deviation_hz: float = 0.0
    carrier: float = 0.0
    snr_db: float = 0.0
    carrier_offset_hz: float = 0.0
    sub_offset_hz: float = 0.0
    sub_coh: float = 0.0
    gps_track: float | None = None
    overrun: bool = False
    iq_ref: str = ""

    def as_row(self) -> dict[str, Any]:
        def fmt(v):
            if v is None:
                return ""
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, float):
                return f"{v:.5g}"
            return v
        return {k: fmt(getattr(self, k)) for k in FIELDS}

    def as_cloud_record(self) -> dict[str, Any]:
        """The RB-Cloud upload unit: the row plus schema provenance."""
        return {"schema": SCHEMA_VERSION, **{k: getattr(self, k) for k in FIELDS}}


def _fmt_cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return f"{v:.5g}"
    return v


@dataclass
class LocSolutionRecord:
    """One localizer row (A33). Built by the controller at publish time."""

    t_utc: str
    t_mono: float
    channel_khz: int | None
    state: str
    locstatus: int
    ddm: float | None
    valid: bool
    reason: str = ""
    q_sdm: float = 0.0
    q_snr: float = 0.0
    q_stab: float = 0.0
    value: int = 0
    m90: float = 0.0
    m150: float = 0.0
    ddm_raw: float = 0.0
    ddm_corrected: float = 0.0
    sdm: float = 0.0
    snr_db: float = 0.0
    tone_offset: float = 0.0
    carrier: float = 0.0
    carrier_offset_hz: float = 0.0
    gps_track: float | None = None
    overrun: bool = False
    iq_ref: str = ""

    def as_row(self) -> dict[str, Any]:
        return {k: _fmt_cell(getattr(self, k)) for k in LOC_FIELDS}

    def as_cloud_record(self) -> dict[str, Any]:
        return {"schema": LOC_SCHEMA_VERSION, **{k: getattr(self, k) for k in LOC_FIELDS}}


class SolutionLogger:
    """Append-only 1 Hz CSV writer. Never blocks the DSP; failures are dropped.

    `fields` defaults to the VOR schema; the localizer log passes LOC_FIELDS
    and its own path (A33)."""

    def __init__(self, path: str | Path | None, fields: tuple[str, ...] = FIELDS):
        self.fields = tuple(fields)
        self.path = Path(path) if path else None
        self._fh = None
        self._writer: csv.DictWriter | None = None
        self.rows_written = 0
        self.write_errors = 0
        self._error_logged = False
        if self.path:
            self._open()

    def _open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists() or self.path.stat().st_size == 0
            self._fh = self.path.open("a", newline="")
            self._writer = csv.DictWriter(self._fh, fieldnames=self.fields)
            if new:
                self._writer.writeheader()
                self._fh.flush()
        except OSError as e:
            log.warning("solution log unavailable (%s); continuing without it", e)
            self._fh = None
            self._writer = None

    def write(self, rec: SolutionRecord) -> None:
        if self._writer is None:
            return
        try:
            self._writer.writerow(rec.as_row())
            self._fh.flush()
            self.rows_written += 1
        except OSError as e:
            self.write_errors += 1
            if not self._error_logged:
                log.warning("solution log write failed (%s); dropping rows", e)
                self._error_logged = True

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
                self._writer = None


def schema_markdown() -> str:
    """Render SOLUTION_SCHEMA as a Markdown table for docs/cloud-schema.md."""
    lines = [f"Schema `{SCHEMA_VERSION}`.\n",
             "| field | unit | description |", "|---|---|---|"]
    for name, (unit, desc) in SOLUTION_SCHEMA.items():
        lines.append(f"| `{name}` | {unit} | {desc} |")
    return "\n".join(lines) + "\n"


def loc_schema_markdown() -> str:
    """Render LOC_SOLUTION_SCHEMA as a Markdown table for docs/cloud-schema.md."""
    lines = [f"Schema `{LOC_SCHEMA_VERSION}`.\n",
             "| field | unit | description |", "|---|---|---|"]
    for name, (unit, desc) in LOC_SOLUTION_SCHEMA.items():
        lines.append(f"| `{name}` | {unit} | {desc} |")
    return "\n".join(lines) + "\n"
