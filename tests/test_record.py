"""IQ file I/O round-trip, including the cu8/cs8 distinction of amendment A4."""

from __future__ import annotations

import numpy as np
import pytest

from rbvor.record import (
    FORMATS,
    IQMeta,
    IQWriter,
    iq_blocks,
    read_sidecar,
    to_complex64,
    write_sidecar,
)


def _meta(fmt="cu8"):
    return IQMeta(format=fmt, sample_rate_hz=1_024_000,
                  center_freq_hz=109_144_000, nav_freq_hz=109_400_000,
                  tuner_offset_hz=-256_000)


def test_unknown_format_rejected():
    with pytest.raises(ValueError):
        IQMeta(format="cs4", sample_rate_hz=1, center_freq_hz=1,
               nav_freq_hz=1, tuner_offset_hz=0)


def test_cu8_is_centred_not_offset():
    """A4: cu8 midscale is 127.5. Mislabelling it as cs8 injects a DC carrier."""
    raw = np.full(64, 127, dtype=np.uint8)
    z = to_complex64(raw, "cu8")
    assert np.max(np.abs(z)) < 0.01
    z_wrong = to_complex64(raw.view(np.int8), "cs8")
    assert np.abs(z_wrong).mean() > 0.9   # this is the bug A4 exists to prevent


def test_cs8_round_trip():
    raw = np.array([0, 64, -64, 127, -128, 0], dtype=np.int8)
    z = to_complex64(raw, "cs8")
    assert z.shape == (3,)
    assert np.isclose(z[0].real, 0.0)
    assert np.isclose(z[0].imag, 0.5)
    assert np.isclose(z[1].real, -0.5)


def test_odd_sample_count_is_truncated_not_misaligned():
    z = to_complex64(np.arange(7, dtype=np.uint8), "cu8")
    assert z.shape == (3,)


def test_sidecar_round_trip(tmp_path):
    m = _meta()
    m.site = "LIQQ"
    m.expected_radial_deg = 137.0
    p = write_sidecar(tmp_path / "x.cu8", m)
    back = read_sidecar(p)
    assert back.nav_freq_hz == 109_400_000
    assert back.expected_radial_deg == 137.0
    assert back.format == "cu8"


def test_missing_sidecar_refuses_to_guess(tmp_path):
    (tmp_path / "orphan.cu8").write_bytes(b"\x7f" * 16)
    with pytest.raises(FileNotFoundError):
        read_sidecar(tmp_path / "orphan.cu8")


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_writer_then_blocks(tmp_path, fmt):
    n = 5000
    spec = FORMATS[fmt]
    if fmt == "cu8":
        raw = np.random.randint(0, 256, n * 2).astype(spec["dtype"])
    elif fmt == "cf32":
        raw = np.random.uniform(-1, 1, n * 2).astype(spec["dtype"])
    else:
        info = np.iinfo(spec["dtype"])
        raw = np.random.randint(info.min, info.max, n * 2).astype(spec["dtype"])
    with IQWriter(tmp_path / "cap", _meta(fmt)) as w:
        w.write(raw)
    got = np.concatenate(list(iq_blocks(tmp_path / f"cap.{fmt}", block_samples=1024)))
    assert got.shape == (n,)
    assert got.dtype == np.complex64
    assert read_sidecar(tmp_path / "cap.json").samples == n
