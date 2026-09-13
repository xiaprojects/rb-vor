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

__all__ = ["SolutionRecord", "SOLUTION_SCHEMA", "SolutionLogger", "SCHEMA_VERSION"]

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
    "gps_track": ("deg true", "aircraft track from the bridge, diagnostics only"),
    "overrun": ("bool", "window touched a sample gap (discarded if true)"),
    "iq_ref": ("filename", "IQ capture that includes this window, if any"),
}

FIELDS = tuple(SOLUTION_SCHEMA)


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


class SolutionLogger:
    """Append-only 1 Hz CSV writer. Never blocks the DSP; failures are dropped."""

    def __init__(self, path: str | Path | None):
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
            self._writer = csv.DictWriter(self._fh, fieldnames=FIELDS)
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
