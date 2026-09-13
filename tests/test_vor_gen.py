"""M2 -- synthetic VOR generator.

Acceptance (plan A, M2): the generated signal must contain the carrier, the
30 Hz tone, the 1020 Hz ident and the 9960 Hz subcarrier with +/-480 Hz FM
sidebands, at the commanded depths, encoding the commanded radial.

The verification here uses a deliberately naive reference decoder written
inside the test file. It is NOT `rbvor.dsp` (which does not exist yet) and it is
not written the same way: it uses full-rate FFT-based filtering rather than the
polyphase/Goertzel chain of doc A section 7.2. Two independent implementations
agreeing on the radial is the evidence; one implementation agreeing with itself
is not.

Amendment A6 is pinned here: CVOR and DVOR must decode IDENTICALLY, and the
deliberately-wrong `dvor-reversed` must decode to the negation. Without that
third case the equivalence test would pass even if both models were trivial.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.record import iq_blocks, read_sidecar  # noqa: E402
from tools.vor_gen import (  # noqa: E402
    IDENT_HZ,
    VorParams,
    generate_blocks,
    morse_intervals,
    write_file,
)

# Small, fast test rate. Must clear 9960 + 3*480 Hz, and 64 kHz divides neatly
# by the 30 Hz integration window (64000/30 is not an integer, but the WINDOW is
# defined in whole seconds, which is what matters -- 1 s contains exactly 30
# cycles at any sample rate).
FS = 64_000
OFFSET = -FS // 4


def gen(radial, vor_type="cvor", seconds=3.0, snr_db=None, **kw) -> np.ndarray:
    p = VorParams(radial_deg=radial, vor_type=vor_type, fs=FS, seconds=seconds,
                  offset_hz=OFFSET, snr_db=snr_db, block=1 << 16, **kw)
    return np.concatenate(list(generate_blocks(p)))


# --------------------------------------------------------------------------- #
# independent reference decoder -- FFT-based, unlike rbvor.dsp will be
# --------------------------------------------------------------------------- #

def _bandpass_fft(x: np.ndarray, fs: int, lo: float, hi: float) -> np.ndarray:
    """Zero-phase brick-wall filter via FFT. Zero group delay by construction,
    which is exactly why this cannot be reused as the production filter -- but
    it removes group delay as a confounder when validating the generator."""
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(x.size, 1.0 / fs)
    X[(f < lo) | (f > hi)] = 0.0
    return np.fft.irfft(X, n=x.size)


def _tone_phase(x: np.ndarray, fs: int, f0: float, seconds: float) -> tuple[float, float]:
    """Complex DFT at exactly f0 over a whole number of cycles -> (phase, amp)."""
    n = int(round(seconds * fs))
    seg = x[:n]
    t = np.arange(n) / fs
    c = np.sum(seg * np.exp(-2j * np.pi * f0 * t)) * 2.0 / n
    return float(np.angle(c)), float(np.abs(c))


def reference_decode(z: np.ndarray, fs: int = FS, offset_hz: int = OFFSET,
                     seconds: float = 2.0) -> dict:
    """Naive, slow, correct-by-inspection VOR decoder used only for testing."""
    n = np.arange(z.size)
    base = z * np.exp(-2j * np.pi * offset_hz * n / fs)     # down-mix to DC
    env = np.abs(base).astype(np.float64)
    carrier = float(np.mean(env))
    audio = env - carrier

    # skip the first 0.25 s: FFT filtering has no transient, but the ident
    # keying edge at t=0 does perturb the very start
    skip = int(0.25 * fs)
    audio = audio[skip:]

    am = _bandpass_fft(audio, fs, 15.0, 45.0)
    phi_am, amp_am = _tone_phase(am, fs, 30.0, seconds)

    sub = _bandpass_fft(audio, fs, 9960 - 1500, 9960 + 1500)
    analytic = sub + 1j * np.imag(np.fft.ifft(
        np.fft.fft(sub) * _hilbert_mask(sub.size)))
    inst = np.diff(np.unwrap(np.angle(analytic))) * fs / (2 * np.pi)
    fm = _bandpass_fft(inst - np.mean(inst), fs, 15.0, 45.0)
    phi_fm, amp_fm = _tone_phase(fm, fs, 30.0, seconds)

    # Subcarrier level must be measured as BAND POWER, never as the amplitude of
    # the bin at exactly 9960 Hz: with beta = 480/30 = 16 the residual carrier
    # component is J0(16) = -0.175, so a narrow bin sees only ~3 % of the power
    # and reads "no subcarrier". See amendment A7.
    sub_band = _bandpass_fft(audio, fs, 9960 - 900, 9960 + 900)
    amp_sub = float(np.std(sub_band) * np.sqrt(2))
    _, amp_id = _tone_phase(_bandpass_fft(audio, fs, 900, 1140), fs, IDENT_HZ, seconds)

    radial = np.rad2deg(phi_am - phi_fm) % 360.0
    return {
        "radial": radial, "carrier": carrier,
        "am_depth": amp_am / carrier, "sub_depth": amp_sub / carrier,
        "ident_depth": amp_id / carrier,
        "fm_dev_hz": float(np.std(fm) * np.sqrt(2)),
        "amp_fm": amp_fm,
    }


def _hilbert_mask(n: int) -> np.ndarray:
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1
        h[1:n // 2] = 2
    else:
        h[0] = 1
        h[1:(n + 1) // 2] = 2
    return h


def angdiff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


# --------------------------------------------------------------------------- #
# generator structure
# --------------------------------------------------------------------------- #

def test_morse_intervals_are_ordered_and_non_overlapping():
    iv = morse_intervals("TST", 0.15)
    assert iv == sorted(iv)
    assert all(b > a for a, b in iv)
    assert all(iv[i][1] <= iv[i + 1][0] for i in range(len(iv) - 1))
    # T = dash, S = 3 dots, T = dash  ->  5 key-on intervals
    assert len(iv) == 5


def test_unknown_morse_character_is_rejected():
    with pytest.raises(ValueError):
        morse_intervals("TS!", 0.15)


def test_rejects_sample_rate_too_low_for_the_subcarrier():
    with pytest.raises(ValueError):
        VorParams(radial_deg=0, fs=16_000)


def test_rejects_unknown_type_and_fade():
    with pytest.raises(ValueError):
        VorParams(radial_deg=0, vor_type="tvor")
    with pytest.raises(ValueError):
        VorParams(radial_deg=0, fade="wobble")


def test_default_offset_is_minus_fs_over_four():
    """Amendment A1: -fs/4 makes the down-mix multiply-free."""
    assert VorParams(radial_deg=0, fs=1_024_000).offset_hz == -256_000


def test_sample_count_and_dtype():
    z = gen(0, seconds=1.0)
    assert z.size == FS
    assert z.dtype == np.complex64


# --------------------------------------------------------------------------- #
# spectral content
# --------------------------------------------------------------------------- #

def test_carrier_sits_at_the_tuner_offset():
    z = gen(0, seconds=1.0)
    spec = np.abs(np.fft.fftshift(np.fft.fft(z)))
    f = np.fft.fftshift(np.fft.fftfreq(z.size, 1.0 / FS))
    assert abs(f[int(np.argmax(spec))] - OFFSET) < 2.0


def test_modulation_depths_match_the_commanded_values():
    z = gen(137, seconds=3.0)
    d = reference_decode(z)
    assert d["am_depth"] == pytest.approx(0.30, abs=0.02)
    assert d["sub_depth"] == pytest.approx(0.30, abs=0.03)


def test_subcarrier_bin_at_9960_is_almost_empty():
    """Amendment A7, pinned as a test so nobody re-introduces a bin measurement.

    The FM modulation index is beta = deviation/tone = 480/30 = 16. The residual
    component at the subcarrier frequency itself is J0(16) = -0.175, i.e. about
    3 % of the subcarrier power. Gauging subcarrier presence with a narrowband
    detector at 9960 Hz therefore reads near-zero on a perfectly good station.
    `quality.q_sub` (M4) must integrate the band, not sample the bin.
    """
    z = gen(0, seconds=3.0, ident="")
    n = np.arange(z.size)
    env = np.abs(z * np.exp(-2j * np.pi * OFFSET * n / FS)).astype(np.float64)
    audio = env - np.mean(env)
    _, bin_amp = _tone_phase(_bandpass_fft(audio, FS, 9900, 10020), FS, 9960.0, 2.0)
    band_amp = np.std(_bandpass_fft(audio, FS, 9060, 10860)) * np.sqrt(2)
    assert bin_amp / band_amp < 0.30          # bin is a small fraction of the band
    assert band_amp / np.mean(env) == pytest.approx(0.30, abs=0.03)


def test_fm_deviation_is_480_hz():
    z = gen(0, seconds=3.0, ident="")
    d = reference_decode(z)
    assert d["fm_dev_hz"] == pytest.approx(480.0, rel=0.10)


def test_ident_tone_present_and_removable():
    with_id = reference_decode(gen(0, seconds=3.0, ident="TST"))
    without = reference_decode(gen(0, seconds=3.0, ident=""))
    assert with_id["ident_depth"] > 0.01
    assert without["ident_depth"] < 0.002


# --------------------------------------------------------------------------- #
# radial encoding -- the point of the whole tool
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("radial", [0, 1, 45, 90, 137, 180, 213, 270, 359])
def test_commanded_radial_is_recovered(radial):
    d = reference_decode(gen(radial, seconds=3.0, ident=""))
    assert angdiff(d["radial"], radial) < 1.0


def test_cvor_and_dvor_decode_identically(  # amendment A6
):
    """A properly-formed DVOR is transparent to the receiver. That is its whole
    purpose, and it is why plan A's original T4 ('DVOR needs phase_sign
    flipped') was wrong."""
    for radial in (0, 73, 200, 315):
        c = reference_decode(gen(radial, "cvor", seconds=3.0, ident=""))["radial"]
        d = reference_decode(gen(radial, "dvor", seconds=3.0, ident=""))["radial"]
        assert angdiff(c, d) < 1.0
        assert angdiff(c, radial) < 1.0


@pytest.mark.parametrize("radial", [40, 137, 250])
def test_reversed_dvor_decodes_to_the_negation(radial):
    """Gives the equivalence test above its teeth: if the generator ignored the
    type argument entirely, this test would fail."""
    r = reference_decode(gen(radial, "dvor-reversed", seconds=3.0, ident=""))["radial"]
    assert angdiff(r, -radial % 360) < 1.0
    assert angdiff(r, radial) > 5.0 or radial % 180 == 0


def test_ident_keying_does_not_shift_the_radial():
    """Soft keying exists so the 1020 Hz ident does not splatter into 30 Hz."""
    a = reference_decode(gen(137, seconds=3.0, ident=""))["radial"]
    b = reference_decode(gen(137, seconds=3.0, ident="TST"))["radial"]
    assert angdiff(a, b) < 1.0


def test_hard_keying_would_be_worse():
    """Documents WHY ident_edge_s exists, rather than leaving it as a magic
    constant nobody dares change."""
    soft = reference_decode(gen(137, seconds=3.0, ident="TST", ident_edge_s=0.006))
    hard = reference_decode(gen(137, seconds=3.0, ident="TST", ident_edge_s=1e-6))
    assert angdiff(soft["radial"], 137) <= angdiff(hard["radial"], 137) + 0.5


# --------------------------------------------------------------------------- #
# impairments
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("snr_db,tol", [(40, 1.0), (20, 2.0), (10, 4.0)])
def test_radial_survives_noise(snr_db, tol):
    d = reference_decode(gen(137, seconds=3.0, snr_db=snr_db, ident=""))
    assert angdiff(d["radial"], 137) < tol


@pytest.mark.parametrize("fade", ["slow", "flutter"])
def test_amplitude_fading_does_not_bias_the_radial(fade):
    """AM fading is common-mode: it must not rotate the bearing."""
    d = reference_decode(gen(137, seconds=3.0, fade=fade, ident=""))
    assert angdiff(d["radial"], 137) < 2.0


def test_tuner_drift_does_not_bias_the_radial():
    """A ppm error moves the carrier; the AM envelope must not care."""
    d = reference_decode(gen(137, seconds=3.0, drift_hz=300.0, ident=""))
    assert angdiff(d["radial"], 137) < 2.0


def test_reproducible_for_a_fixed_seed():
    assert np.array_equal(gen(90, seconds=0.5, snr_db=10),
                          gen(90, seconds=0.5, snr_db=10))


# --------------------------------------------------------------------------- #
# file output
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fmt", ["cu8", "cs8", "cf32"])
def test_file_round_trip_preserves_the_radial(tmp_path, fmt):
    p = VorParams(radial_deg=213, fs=FS, seconds=3.0, offset_hz=OFFSET,
                  snr_db=None, ident="")
    path = write_file(p, tmp_path / "synth", fmt=fmt)
    meta = read_sidecar(path)
    assert meta.expected_radial_deg == 213.0
    assert meta.synthetic["vor_type"] == "cvor"
    assert meta.format == fmt
    z = np.concatenate(list(iq_blocks(path, block_samples=1 << 15)))
    assert angdiff(reference_decode(z)["radial"], 213) < 1.5


def test_default_level_does_not_clip_cu8(tmp_path, capsys):
    p = VorParams(radial_deg=0, fs=FS, seconds=1.0, offset_hz=OFFSET, snr_db=20)
    write_file(p, tmp_path / "s", fmt="cu8")
    assert "clipped" not in capsys.readouterr().err
