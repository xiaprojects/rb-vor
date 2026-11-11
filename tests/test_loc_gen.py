"""L2 -- the synthetic localizer generator (CLAUDE.md A33).

Judged by an independent reference decoder written here with plain numpy:
FFT brick-wall filters and a least-squares fit at the known tone frequencies.
It shares no code with `rbvor/loc.py` (the plan's L2 requirement), so a
generator bug cannot be cancelled by the same bug in the demodulator.

`swap_tones` is the case that must fail (the A6 method): with it the DDM must
come back negated, so these tests cannot pass on a generator that ignores
its arguments.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from rbvor.record import iq_blocks, read_sidecar
from tools import vor_gen
from tools.loc_gen import (
    CARRIER_CONVENTION,
    DDM_FULL_SCALE,
    LocParams,
    generate_blocks,
    write_file,
)

FS = 256_000
OFF = -FS // 4


def gen(**kw) -> np.ndarray:
    kw.setdefault("fs", FS)
    kw.setdefault("offset_hz", OFF)
    kw.setdefault("seconds", 3.0)
    kw.setdefault("snr_db", None)
    kw.setdefault("ident", "")
    return np.concatenate(list(generate_blocks(LocParams(**kw))))


def _brickwall(x: np.ndarray, fs: float, cutoff: float) -> np.ndarray:
    X = np.fft.fft(x)
    f = np.fft.fftfreq(x.size, 1.0 / fs)
    X[np.abs(f) > cutoff] = 0
    return np.fft.ifft(X)


def ref_depths(z: np.ndarray, eps: float = 0.0, fs: int = FS,
               offset: int = OFF) -> tuple[float, float]:
    """(m90, m150) by an independent route: mix the carrier (at -offset, A22)
    to DC, brick-wall to 6 kHz, envelope, brick-wall to 400 Hz, then least
    squares on [1, cos, sin] at 90(1+eps) and 150(1+eps)."""
    t = np.arange(z.size) / fs
    zb = _brickwall(z * np.exp(2j * np.pi * offset * t), fs, 6000.0)
    env = np.abs(zb)
    env = np.real(_brickwall(env, fs, 400.0))
    k = slice(fs // 4, -fs // 4)                     # drop filter edge effects
    tt, e = t[k], env[k]
    cols = [np.ones_like(tt)]
    for f in (90.0 * (1 + eps), 150.0 * (1 + eps)):
        cols += [np.cos(2 * np.pi * f * tt), np.sin(2 * np.pi * f * tt)]
    c, *_ = np.linalg.lstsq(np.column_stack(cols), e, rcond=None)
    dc = c[0]
    return float(np.hypot(c[1], c[2]) / dc), float(np.hypot(c[3], c[4]) / dc)


@pytest.mark.parametrize("ddm", [-0.2, -0.155, -0.0775, 0.0, 0.03, 0.155, 0.2])
def test_reference_decoder_reads_the_commanded_ddm_and_sdm(ddm):
    m90, m150 = ref_depths(gen(ddm=ddm))
    assert m150 - m90 == pytest.approx(ddm, abs=1e-3)
    assert m150 + m90 == pytest.approx(0.40, abs=1e-3)


def test_swapped_tones_decode_to_the_negated_ddm():
    """The case that must fail. Without it a generator that ignored `ddm` or
    put both depths on the same tone could still pass the test above at 0."""
    good = ref_depths(gen(ddm=0.1))
    bad = ref_depths(gen(ddm=0.1, swap_tones=True))
    assert good[1] - good[0] == pytest.approx(0.1, abs=1e-3)
    assert bad[1] - bad[0] == pytest.approx(-0.1, abs=1e-3)


def test_sdm_is_honoured_independently_of_ddm():
    m90, m150 = ref_depths(gen(ddm=0.05, sdm=0.30))
    assert m90 + m150 == pytest.approx(0.30, abs=1e-3)
    assert m150 - m90 == pytest.approx(0.05, abs=1e-3)


@pytest.mark.parametrize("eps", [-0.025, 0.025])
def test_tone_offset_moves_both_tones_together(eps):
    """Read at the offset frequencies, the depths are right; read at the
    nominal ones over 3 s, they are not -- so the offset is really applied."""
    z = gen(ddm=0.1, tone_offset=eps)
    at_eps = ref_depths(z, eps=eps)
    at_nominal = ref_depths(z, eps=0.0)
    assert at_eps[1] - at_eps[0] == pytest.approx(0.1, abs=1e-3)
    assert at_nominal[0] < 0.05 and at_nominal[1] < 0.05


def test_carrier_sits_where_real_hardware_puts_it():
    """A22/D5: at -offset in baseband. The sidecar says so."""
    z = gen(seconds=1.0)
    f = np.fft.fftfreq(z.size, 1.0 / FS)
    assert f[np.argmax(np.abs(np.fft.fft(z)))] == pytest.approx(-OFF, abs=2.0)
    assert CARRIER_CONVENTION == vor_gen.CARRIER_CONVENTION == "-offset"


def test_two_frequency_localizer_puts_two_carriers_symmetrically():
    """Annex 10 Vol I 3.1.3.2.1: 5-14 kHz apart, symmetric about the channel."""
    z = gen(seconds=1.0, clearance_sep_hz=8000.0, clearance_db=-10.0)
    S = np.abs(np.fft.fft(z))
    f = np.fft.fftfreq(z.size, 1.0 / FS)

    def peak(fc):
        b = np.abs(f - fc) < 50
        return S[b].max()
    hi, lo = peak(-OFF + 4000), peak(-OFF - 4000)
    assert 20 * np.log10(hi / lo) == pytest.approx(10.0, abs=0.2)
    assert peak(-OFF) < 0.01 * hi                    # nothing at the channel centre


def test_ident_is_on_1020_hz():
    z = gen(seconds=4.0, ident="I", ident_depth=0.10, ident_dot_s=0.5)
    t = np.arange(z.size) / FS
    env = np.abs(_brickwall(z * np.exp(2j * np.pi * OFF * t), FS, 6000.0))
    E = np.abs(np.fft.rfft(env - env.mean()))
    f = np.fft.rfftfreq(env.size, 1.0 / FS)
    band = (f > 900) & (f < 1200)
    assert f[band][np.argmax(E[band])] == pytest.approx(1020.0, abs=1.0)


def test_noise_follows_the_vor_generator_definition():
    """Same SNR definition as vor_gen (12 kHz channel), via the shared helper."""
    clean = gen(seconds=1.0, seed=1, level=0.3)       # fixed: auto-level sees the noise
    noisy = gen(seconds=1.0, snr_db=10.0, seed=1, level=0.3)
    n = noisy - clean
    p_n_in_channel = np.mean(np.abs(n) ** 2) * vor_gen.CHANNEL_BW_HZ / FS
    snr = 10 * np.log10(np.mean(np.abs(clean) ** 2) / p_n_in_channel)
    assert snr == pytest.approx(10.0, abs=0.2)


def test_auto_level_fits_the_container():
    z = gen(seconds=1.0, snr_db=6.0, level="auto")
    peak = np.percentile(np.maximum(np.abs(z.real), np.abs(z.imag)), 99.99)
    assert 0.5 < peak <= 1.0


def test_same_public_shape_as_vor_gen():
    """roastbeef's integration stand-in drives generators through this shape."""
    p = LocParams(fs=FS, offset_hz=OFF, seconds=0.5, block=32768)
    blocks = list(generate_blocks(p))
    assert all(b.dtype == np.complex64 for b in blocks)
    assert sum(b.size for b in blocks) == p.n_samples
    assert blocks[0].size == 32768


@pytest.mark.parametrize("kw", [
    {"ddm": 0.5}, {"ddm": -0.41}, {"sdm": 0.95}, {"tone_offset": 0.2},
    {"fade": "flutter"}, {"clearance_sep_hz": -1.0}, {"level": -1.0},
])
def test_impossible_parameters_are_refused(kw):
    with pytest.raises(ValueError):
        LocParams(fs=FS, seconds=0.1, snr_db=None, **kw)


def test_file_and_sidecar_round_trip(tmp_path):
    p = LocParams(ddm=-0.05, fs=FS, offset_hz=OFF, seconds=2.0, snr_db=None,
                  ident="", level=0.5)
    path = write_file(p, tmp_path / "loc", fmt="cf32", nav_freq_hz=110_300_000)
    meta = read_sidecar(path)
    side = json.loads(path.with_suffix(".json").read_text())
    assert meta.nav_freq_hz == 110_300_000
    assert meta.synthetic["navaid"] == "loc"
    assert meta.synthetic["carrier_at"] == "-offset"
    assert side["synthetic"]["m150"] == pytest.approx(0.175)
    z = np.concatenate(list(iq_blocks(path, meta=meta)))
    m90, m150 = ref_depths(z)
    assert m150 - m90 == pytest.approx(-0.05, abs=1e-3)


def test_full_scale_constant_is_the_course_sector_ddm():
    assert DDM_FULL_SCALE == 0.155
