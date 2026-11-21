"""G2 -- the synthetic glide-slope generator (docs/gs-plan.md G2, A39).

Judged by a reference decoder written here with plain numpy (FFT brick-wall
filters, a least-squares fit at the known tones). It imports neither
`rbvor.loc` nor `rbvor.gs`, so a generator bug cannot be cancelled by the
same bug in the demodulator.

The cases that must fail: swapped tones negate the DDM; a localizer-depth
signal read as a glide path reads SDM 0.40, not 0.80.
"""

from __future__ import annotations

import numpy as np
import pytest

from rbvor.record import iq_blocks, read_sidecar
from tools.gs_gen import DDM_FULL_SCALE, SDM_NOMINAL, GsParams, generate_blocks, write_file
from tools.loc_gen import LocParams
from tools.loc_gen import generate_blocks as loc_blocks

FS = 256_000
OFF = -FS // 4


def gen(**kw) -> np.ndarray:
    kw.setdefault("fs", FS)
    kw.setdefault("offset_hz", OFF)
    kw.setdefault("seconds", 3.0)
    kw.setdefault("snr_db", None)
    return np.concatenate(list(generate_blocks(GsParams(**kw))))


def _lowpass(x: np.ndarray, fs: float, cutoff: float) -> np.ndarray:
    X = np.fft.fft(x)
    X[np.abs(np.fft.fftfreq(x.size, 1.0 / fs)) > cutoff] = 0
    return np.fft.ifft(X)


def ref(z: np.ndarray, carrier_hz: float = 0.0, eps: float = 0.0) -> tuple[float, float]:
    """(m90, m150): carrier (at -offset + carrier_hz) to DC, 6 kHz brick wall,
    envelope, 400 Hz brick wall, least squares on [1, cos, sin] per tone."""
    t = np.arange(z.size) / FS
    zb = _lowpass(z * np.exp(-2j * np.pi * (-OFF + carrier_hz) * t), FS, 6000.0)
    env = np.real(_lowpass(np.abs(zb), FS, 400.0))
    k = slice(FS // 4, -FS // 4)
    tt, e = t[k], env[k]
    cols = [np.ones_like(tt)]
    for f in (90.0 * (1 + eps), 150.0 * (1 + eps)):
        cols += [np.cos(2 * np.pi * f * tt), np.sin(2 * np.pi * f * tt)]
    c, *_ = np.linalg.lstsq(np.column_stack(cols), e, rcond=None)
    return float(np.hypot(c[1], c[2]) / c[0]), float(np.hypot(c[3], c[4]) / c[0])


def test_glide_path_constants():
    assert SDM_NOMINAL == 0.80 and DDM_FULL_SCALE == 0.175
    p = GsParams(seconds=0.1, snr_db=None)
    assert p.ident == "" and p.ident_depth == 0.0


@pytest.mark.parametrize("ddm", [-0.25, -0.175, -0.0875, 0.0, 0.04, 0.175, 0.25])
def test_reference_reads_commanded_ddm_and_sdm(ddm):
    m90, m150 = ref(gen(ddm=ddm))
    assert m150 - m90 == pytest.approx(ddm, abs=1e-3)
    assert m150 + m90 == pytest.approx(0.80, abs=1e-3)


def test_swapped_tones_negate_the_ddm():
    m90, m150 = ref(gen(ddm=0.1))
    s90, s150 = ref(gen(ddm=0.1, swap_tones=True))
    assert m150 - m90 == pytest.approx(0.1, abs=1e-3)
    assert s150 - s90 == pytest.approx(-0.1, abs=1e-3)


def test_a_localizer_depth_signal_reads_sdm_040_not_080():
    z = np.concatenate(list(loc_blocks(LocParams(fs=FS, offset_hz=OFF, seconds=3.0,
                                                 snr_db=None, ident="", ddm=0.05))))
    m90, m150 = ref(z)
    assert m90 + m150 == pytest.approx(0.40, abs=1e-3)


@pytest.mark.parametrize("sdm", [0.75, 0.85])
def test_sdm_at_both_tolerance_edges(sdm):
    m90, m150 = ref(gen(ddm=0.02, sdm=sdm))
    assert m90 + m150 == pytest.approx(sdm, abs=1e-3)


def test_carrier_offset_moves_the_carrier_by_exactly_that():
    z = gen(ddm=0.05, drift_hz=12_000.0)
    X = np.abs(np.fft.fft(z[:65536]))
    f = np.fft.fftfreq(65536, 1.0 / FS)
    assert f[np.argmax(X)] == pytest.approx(-OFF + 12_000, abs=FS / 65536)
    m90, m150 = ref(z, carrier_hz=12_000.0)
    assert m150 - m90 == pytest.approx(0.05, abs=1e-3)


def test_tone_offset_moves_both_tones():
    m90, m150 = ref(gen(ddm=0.05, tone_offset=0.025), eps=0.025)
    assert m150 - m90 == pytest.approx(0.05, abs=1e-3)


def test_write_file_records_the_glide_path_in_the_sidecar(tmp_path):
    p = GsParams(fs=FS, offset_hz=OFF, seconds=0.5, snr_db=20.0, ddm=0.03)
    path = write_file(p, tmp_path / "gs.cu8", nav_freq_hz=332_600_000)
    meta = read_sidecar(path)
    assert meta.nav_freq_hz == 332_600_000
    assert meta.synthetic["navaid"] == "gs" and meta.synthetic["carrier_at"] == "-offset"
    n = sum(b.size for b in iq_blocks(path, block_samples=4096, meta=meta))
    assert n == p.n_samples
