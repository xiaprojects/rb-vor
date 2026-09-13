"""IQ recording, sidecar metadata and replay.

Reference: rb-vor-architecture.md section 6 (module `record`) and section 12.

Pulled forward from M9 into M0/M1 because live capture depends on it and the
hardware is available. Only the file-format half is implemented at this stage;
the ring-buffered flag-triggered capture (M9) builds on the same writer.

AMENDMENT A4 (2026-07-24)
------------------------
Doc A section 11 specifies `record.format: "cs8"`. That is wrong for RTL-SDR:
librtlsdr delivers **unsigned** 8-bit offset-binary samples centred on 127.5,
i.e. `cu8`, not signed `cs8`. SoapySDR can be asked for either. Mislabelling a
cu8 file as cs8 produces a 127-count DC offset -- which lands exactly on the
carrier after down-mix and quietly corrupts the AM envelope and therefore every
bearing. The format is therefore recorded per file in the sidecar and never
assumed.

Every recording is a pair:
    capture_20260724T101530Z_109400.cu8      raw interleaved samples
    capture_20260724T101530Z_109400.json     sidecar: everything needed to replay
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

__all__ = ["IQMeta", "FORMATS", "write_sidecar", "read_sidecar", "to_complex64",
           "IQWriter", "iq_blocks", "default_basename"]

#: dtype and conversion parameters per on-disk format.
FORMATS = {
    "cu8": {"dtype": np.uint8, "offset": 127.5, "scale": 127.5},   # RTL-SDR native
    "cs8": {"dtype": np.int8, "offset": 0.0, "scale": 128.0},
    "cs16": {"dtype": np.int16, "offset": 0.0, "scale": 32768.0},
    "cf32": {"dtype": np.float32, "offset": 0.0, "scale": 1.0},
}


@dataclass
class IQMeta:
    """Everything required to replay a capture without guessing.

    A capture without a sidecar is not evidence -- it is a pile of bytes.
    """

    format: str                      # key into FORMATS
    sample_rate_hz: int
    center_freq_hz: int              # what the tuner was actually told
    nav_freq_hz: int                 # the navaid channel of interest
    tuner_offset_hz: int             # center = nav + offset
    gain_db: float | None = None
    gain_mode: str = "manual"
    ppm: int = 0
    device: str = ""
    driver: str = ""
    utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    duration_s: float | None = None
    samples: int | None = None
    # Context that makes a field capture interpretable months later:
    site: str = ""
    lat: float | None = None
    lon: float | None = None
    gps_track_deg: float | None = None
    expected_radial_deg: float | None = None
    station: str = ""
    antenna: str = ""
    filter_chain: str = ""
    notes: str = ""
    #: Additive since rb-vor.iqmeta/1: parameters of a synthetic capture.
    #: Absent for real captures. Presence of this key means "not real signal".
    synthetic: dict | None = None
    tool_version: str = "rb-vor/0.9.0"
    schema: str = "rb-vor.iqmeta/1"

    def __post_init__(self) -> None:
        if self.format not in FORMATS:
            raise ValueError(f"unknown IQ format {self.format!r}; known: {sorted(FORMATS)}")


def default_basename(nav_freq_hz: int, when: datetime | None = None) -> str:
    when = when or datetime.now(UTC)
    return f"capture_{when.strftime('%Y%m%dT%H%M%SZ')}_{nav_freq_hz // 1000}"


def write_sidecar(path: os.PathLike | str, meta: IQMeta) -> Path:
    p = Path(path).with_suffix(".json")
    p.write_text(json.dumps(asdict(meta), indent=2, sort_keys=True) + "\n")
    return p


def read_sidecar(path: os.PathLike | str) -> IQMeta:
    p = Path(path)
    if p.suffix != ".json":
        p = p.with_suffix(".json")
    if not p.exists():
        raise FileNotFoundError(
            f"no sidecar for {path}: refusing to guess the sample format (see A4)"
        )
    data = json.loads(p.read_text())
    known = set(IQMeta.__dataclass_fields__)
    return IQMeta(**{k: v for k, v in data.items() if k in known})


def to_complex64(raw: np.ndarray, fmt: str) -> np.ndarray:
    """Convert interleaved on-disk samples to unit-scaled complex64.

    Uses a 256-entry lookup table for the 8-bit formats: this is the hot path at
    1.024 Msps and a LUT beats an astype/subtract/divide chain.
    """
    spec = FORMATS[fmt]
    if raw.dtype != spec["dtype"]:
        raw = raw.view(spec["dtype"])
    if fmt in ("cu8", "cs8"):
        lut = ((np.arange(256, dtype=np.float32)
                - (spec["offset"] if fmt == "cu8" else 0.0)) / spec["scale"])
        if fmt == "cs8":
            lut = (np.arange(-128, 128, dtype=np.float32) / spec["scale"])
            idx = raw.view(np.uint8).astype(np.uint8)
            vals = lut[(idx.astype(np.int16) + 128) % 256]
        else:
            vals = lut[raw]
    else:
        vals = (raw.astype(np.float32) - spec["offset"]) / spec["scale"]
    if vals.size % 2:
        vals = vals[:-1]
    return (vals[0::2] + 1j * vals[1::2]).astype(np.complex64)


class IQWriter:
    """Streaming writer. Writes the sidecar on close, with the true sample count."""

    def __init__(self, path: os.PathLike | str, meta: IQMeta):
        self.meta = meta
        self.path = Path(path).with_suffix("." + meta.format)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("wb")  # noqa: SIM115 -- streaming writer, closed in close()
        self._n = 0

    def write(self, raw: np.ndarray) -> None:
        raw.tofile(self._fh)
        self._n += raw.size // 2

    def close(self) -> Path:
        self._fh.close()
        self.meta.samples = self._n
        self.meta.duration_s = self._n / float(self.meta.sample_rate_hz)
        write_sidecar(self.path, self.meta)
        return self.path

    def __enter__(self) -> IQWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def iq_blocks(
    path: os.PathLike | str,
    block_samples: int = 32768,
    meta: IQMeta | None = None,
) -> Iterator[np.ndarray]:
    """Yield complex64 blocks from a capture file.

    This is the function `tools/vor_replay.py` and `tests/` use. The live SDR
    reader must yield blocks of exactly the same shape and dtype so that
    `dsp.VorDemod` cannot tell the two apart (doc A section 6).
    """
    p = Path(path)
    meta = meta or read_sidecar(p)
    spec = FORMATS[meta.format]
    itemsize = np.dtype(spec["dtype"]).itemsize
    count = block_samples * 2
    with open(p, "rb") as fh:
        while True:
            buf = np.fromfile(fh, dtype=spec["dtype"], count=count)
            if buf.size < 2:
                return
            yield to_complex64(buf, meta.format)
            if buf.size < count:
                return
    _ = itemsize  # kept for clarity of the block arithmetic above


# --------------------------------------------------------------------------- #
# M9: ring-buffered, flag-triggered IQ capture
# --------------------------------------------------------------------------- #

class RingRecorder:
    """Keeps the last N seconds of IQ and dumps it when triggered.

    Reference: rb-vor-architecture.md section 6 (module `record`), section 13;
    implementation plan M9; failure mode F1 (a flagged bearing is worth
    preserving so it can be re-derived offline).

    A fixed-duration ring of the most recent blocks is held in memory. On
    `trigger()` the whole ring is written to a `cu8`/`cf32` file with a full
    sidecar, so the IQ that produced a NAV flag -- or a manual mark -- can be
    replayed through `tools/vor_replay.py` and the bearing checked independently.

    Config maps directly:
      * `trigger: on_flag`  -> the controller calls `trigger()` when it flags;
      * `trigger: manual`   -> only an explicit `trigger()` writes;
      * `trigger: always`   -> continuous rolling capture (bench use; large).

    Bounded by construction: the ring is a deque with a byte budget, so a wedged
    consumer or a long flag cannot grow memory without limit.
    """

    def __init__(self, meta_template: IQMeta, out_dir: str | os.PathLike,
                 max_seconds: float, fmt: str = "cu8"):
        from collections import deque
        if fmt not in FORMATS:
            raise ValueError(f"unknown IQ format {fmt!r}")
        self.template = meta_template
        self.out_dir = Path(out_dir)
        self.max_seconds = float(max_seconds)
        self.fmt = fmt
        self.fs = meta_template.sample_rate_hz
        self._budget = int(self.max_seconds * self.fs)
        self._ring: deque = deque()
        self._held = 0
        self.captures_written = 0

    def push(self, iq: np.ndarray) -> None:
        """Add a block of complex64 IQ, evicting oldest beyond the budget."""
        self._ring.append(iq)
        self._held += iq.size
        while self._held - (self._ring[0].size if self._ring else 0) >= self._budget \
                and len(self._ring) > 1:
            self._held -= self._ring.popleft().size

    def reset(self) -> None:
        """Drop the ring. Called on retune -- old-station IQ is not evidence for
        the new one."""
        self._ring.clear()
        self._held = 0

    def trigger(self, reason: str = "", extra: dict | None = None) -> Path | None:
        """Write the current ring to a file. Returns the path, or None if empty."""
        if not self._ring:
            return None
        samples = np.concatenate(list(self._ring))
        meta = self._clone_meta(samples.size, reason, extra)
        base = self.out_dir / default_basename(self.template.nav_freq_hz)
        with IQWriter(base, meta) as w:
            w.write(encode_complex(samples, self.fmt))
        self.captures_written += 1
        return Path(str(base)).with_suffix("." + self.fmt)

    def _clone_meta(self, n: int, reason: str, extra: dict | None) -> IQMeta:
        from dataclasses import replace
        note = f"flag capture: {reason}" if reason else "flag capture"
        if extra:
            note += " | " + ", ".join(f"{k}={v}" for k, v in extra.items())
        return replace(self.template, format=self.fmt, samples=n,
                       duration_s=n / float(self.fs), notes=note, synthetic=None)


def encode_complex(z: np.ndarray, fmt: str) -> np.ndarray:
    """Quantise unit-scaled complex64 to interleaved on-disk samples.

    The inverse of `to_complex64`. Kept here rather than in tools/ so the
    recorder has no dependency on the generator; `tools/vor_gen.py` may import
    this instead of carrying its own copy.
    """
    spec = FORMATS[fmt]
    iq = np.empty(z.size * 2, dtype=np.float64)
    iq[0::2] = z.real
    iq[1::2] = z.imag
    if fmt == "cf32":
        return iq.astype(np.float32)
    scaled = iq * spec["scale"] + spec["offset"]
    lo, hi = {
        "cu8": (0, 255), "cs8": (-128, 127), "cs16": (-32768, 32767),
    }[fmt]
    return np.clip(np.round(scaled), lo, hi).astype(spec["dtype"])
