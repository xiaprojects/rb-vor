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


#: 256-entry lookup tables for the 8-bit formats, built once. Indexed by the
#: raw byte value: for cs8 the byte is reinterpreted as uint8, so the table is
#: laid out in two's-complement order (0..127 then -128..-1).
_LUT8 = {
    "cu8": ((np.arange(256, dtype=np.float32) - 127.5) / 127.5),
    "cs8": (np.concatenate([np.arange(0, 128), np.arange(-128, 0)])
            .astype(np.float32) / 128.0),
}


def to_complex64(raw: np.ndarray, fmt: str) -> np.ndarray:
    """Convert interleaved on-disk samples to unit-scaled complex64.

    This is the hot path at 1.024 Msps, for replay and for the pyrtlsdr
    backend alike. Two things keep it cheap (D6): a 256-entry lookup table
    for the 8-bit formats instead of an astype/subtract/divide chain, and the
    fact that interleaved I, Q float32 pairs *are* the memory layout of
    complex64 -- so the result is a zero-copy `view`, not a strided
    ``re + 1j * im`` that materialises three temporaries.
    """
    spec = FORMATS[fmt]
    if raw.dtype != spec["dtype"]:
        raw = raw.view(spec["dtype"])
    if fmt in ("cu8", "cs8"):
        vals = _LUT8[fmt][raw.view(np.uint8)]
    else:
        vals = ((raw.astype(np.float32) - spec["offset"]) / spec["scale"])
    if vals.size % 2:
        vals = vals[:-1]
    return np.ascontiguousarray(vals, dtype=np.float32).view(np.complex64)


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

    Bounded by construction: the ring is a deque with a sample budget, so a
    wedged consumer or a long flag cannot grow memory without limit.

    Two things about cost, measured (D6). The ring is held **already
    quantised** to the on-disk format -- 2 bytes per cu8 sample instead of 8
    for complex64, so 30 s at 1.024 Msps is 61 MB, not 246 MB -- because
    `encode_complex` is cheap enough to run per block. And the file is written
    by a **background thread**: `trigger()` hands the ring's chunks over and
    returns at once. Before that, a trigger concatenated, quantised and wrote
    the whole ring inside `Controller.push()`, i.e. inside the sample pump:
    2.1 s on a desktop, longer on a Pi writing to SD, against an SDR queue that
    holds about 1 s. A flag capture would therefore cause an overrun and cost
    the next window (F9) -- the forensic record damaging the thing it records.
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
        self._ring: deque = deque()        # encoded chunks, 2 values per sample
        self._held = 0                     # samples in the ring
        self._writer = None                # threading.Thread while a write is in flight
        self.captures_written = 0
        self.captures_skipped = 0

    def push(self, iq: np.ndarray) -> None:
        """Add a block of complex64 IQ, evicting oldest beyond the budget."""
        self._ring.append(encode_complex(iq, self.fmt))
        self._held += iq.size
        while self._held - (self._ring[0].size // 2 if self._ring else 0) >= self._budget \
                and len(self._ring) > 1:
            self._held -= self._ring.popleft().size // 2

    def reset(self) -> None:
        """Drop the ring. Called on retune -- old-station IQ is not evidence for
        the new one."""
        self._ring.clear()
        self._held = 0

    def set_channel(self, nav_hz: int) -> None:
        """Record which navaid the ring now holds. Called on retune.

        Without this every flag capture was written with `nav_freq_hz: 0` and
        `center_freq_hz: 0` (the daemon builds the template before it knows the
        channel), so its file name was `capture_..._0` and `vor_replay.py`
        could not place the carrier -- a capture that cannot be replayed is not
        evidence (A4). The file name and the sidecar now carry the channel.
        """
        from dataclasses import replace
        nav = int(nav_hz)
        self.template = replace(self.template, nav_freq_hz=nav,
                                center_freq_hz=nav + int(self.template.tuner_offset_hz))

    def trigger(self, reason: str = "", extra: dict | None = None) -> Path | None:
        """Start writing the current ring to a file. Returns the path at once.

        Returns None when the ring is empty, or when the previous capture is
        still being written -- consecutive flags a second apart would otherwise
        queue up copies of largely the same samples; the one in flight already
        holds the evidence. Call `flush()` to wait for the write.
        """
        if not self._ring:
            return None
        if self._writer is not None and self._writer.is_alive():
            self.captures_skipped += 1
            return None
        import threading
        chunks = list(self._ring)          # references; chunks are never mutated
        n = self._held
        meta = self._clone_meta(n, reason, extra)
        base = self.out_dir / default_basename(self.template.nav_freq_hz)
        writer = IQWriter(base, meta)      # opens the file now, so the name exists

        def _write() -> None:
            with writer:
                for c in chunks:
                    writer.write(c)

        self._writer = threading.Thread(target=_write, name="rbvor-iqwrite", daemon=True)
        self._writer.start()
        self.captures_written += 1
        return writer.path

    def flush(self, timeout: float | None = None) -> None:
        """Wait for a capture in progress to finish writing (sidecar included)."""
        if self._writer is not None:
            self._writer.join(timeout)

    def _clone_meta(self, n: int, reason: str, extra: dict | None) -> IQMeta:
        from dataclasses import replace
        note = f"flag capture: {reason}" if reason else "flag capture"
        if extra:
            note += " | " + ", ".join(f"{k}={v}" for k, v in extra.items())
        return replace(self.template, format=self.fmt, samples=n,
                       duration_s=n / float(self.fs), notes=note, synthetic=None)


_INT_RANGE = {"cu8": (0.0, 255.0), "cs8": (-128.0, 127.0), "cs16": (-32768.0, 32767.0)}


def encode_complex(z: np.ndarray, fmt: str) -> np.ndarray:
    """Quantise unit-scaled complex64 to interleaved on-disk samples.

    The inverse of `to_complex64`, and like it built on the fact that complex64
    already *is* interleaved float32 I, Q: the input is viewed, not unpacked,
    and the arithmetic stays in float32 (D6). Rounding is floor(x + 0.5), which
    maps a sample that came off the cu8 lookup table back to its original byte
    exactly. Fast enough to run on every block, which is what lets
    `RingRecorder` hold its ring already quantised.

    Kept here rather than in tools/ so the recorder has no dependency on the
    generator; `tools/vor_gen.py` may import this instead of carrying its own.
    """
    spec = FORMATS[fmt]
    v = np.ascontiguousarray(z, dtype=np.complex64).view(np.float32)
    if fmt == "cf32":
        return v.copy()
    q = v * np.float32(spec["scale"])
    q += np.float32(spec["offset"] + 0.5)
    np.floor(q, out=q)
    lo, hi = _INT_RANGE[fmt]
    np.clip(q, lo, hi, out=q)
    return q.astype(spec["dtype"])
