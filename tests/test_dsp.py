"""M3 -- production DSP chain.

Acceptance (plan A, M3): a stable phase difference with run-to-run spread
< 0.2 deg. Absolute correctness is deliberately NOT claimed here -- the sign and
the constant offset are M4's job. What M3 must prove is that the residual is
*constant*: same across radials, across block sizes, across CVOR/DVOR.

Amendment A9 is pinned by `test_docA_ordering_fails_inside_icao_tolerance`,
which is the evidence for the correction rather than an assertion of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.dsp import (  # noqa: E402
    OUTPUT_RATE_HZ,
    TONE_RATE_HZ,
    DspParams,
    PolyDecimator,
    VorDemod,
)
from tools.vor_gen import VorParams, generate_blocks  # noqa: E402

FS = 256_000
OFFSET = -FS // 4


def params(**kw) -> DspParams:
    return DspParams(fs=FS, offset_hz=OFFSET, **kw)


def run(radial, vor_type="cvor", snr=None, seconds=5.0, block=1 << 16,
        ident="", deviation_hz=480.0, seed=0, dsp=None, drop_first=2):
    p = VorParams(radial_deg=radial, vor_type=vor_type, fs=FS, seconds=seconds,
                  offset_hz=OFFSET, snr_db=snr, ident=ident, block=block,
                  deviation_hz=deviation_hz, seed=seed)
    d = dsp or VorDemod(params())
    obs = []
    for blk in generate_blocks(p):
        obs += d.push(blk)
    return obs[drop_first:]


def dphi_deg(obs) -> np.ndarray:
    return np.array([np.rad2deg(o.delta_phi) % 360.0 for o in obs])


def circ_mean(deg: np.ndarray) -> float:
    return float(np.rad2deg(np.angle(np.mean(np.exp(1j * np.deg2rad(deg))))) % 360.0)


def circ_spread(deg: np.ndarray) -> float:
    m = circ_mean(deg)
    return float(np.ptp(((deg - m + 180.0) % 360.0) - 180.0))


def angdiff(a, b) -> float:
    return float(abs(((a - b + 180.0) % 360.0) - 180.0))


# --------------------------------------------------------------------------- #
# PolyDecimator
# --------------------------------------------------------------------------- #

def test_decimator_matches_a_reference_implementation():
    taps = signal.firwin(31, 0.2)
    x = np.random.default_rng(0).normal(size=4096).astype(np.float32)
    got = PolyDecimator(taps, 4, np.float32)(x)
    want = signal.lfilter(taps, 1, x)[::4]
    assert np.allclose(got, want[:got.size], atol=1e-5)


@pytest.mark.parametrize("blocks", [[4096], [1000, 3096], [1, 4095], [333] * 12])
def test_decimator_is_independent_of_block_boundaries(blocks):
    """The live path chops the stream arbitrarily; the result must not care."""
    taps = signal.firwin(31, 0.2)
    x = np.random.default_rng(1).normal(size=sum(blocks)).astype(np.float32)
    ref = PolyDecimator(taps, 4, np.float32)(x)
    d = PolyDecimator(taps, 4, np.float32)
    out, i = [], 0
    for n in blocks:
        out.append(d(x[i:i + n]))
        i += n
    assert np.allclose(np.concatenate(out), ref, atol=1e-6)


def test_decimator_reset_restores_initial_state():
    taps = signal.firwin(31, 0.2)
    x = np.random.default_rng(2).normal(size=1024).astype(np.float32)
    d = PolyDecimator(taps, 4, np.float32)
    a = d(x)
    d.reset()
    assert np.allclose(d(x), a)


def test_decimator_handles_a_block_shorter_than_the_filter():
    """A short first block yields a short output, not an error.

    Zero-primed history means output appears immediately; those first samples
    are the filter transient. `VorDemod` discards the first windows for exactly
    this reason, and the callers of `run()` in this file drop them too.
    """
    taps = signal.firwin(63, 0.2)
    d = PolyDecimator(taps, 4, np.float32)
    first = d(np.ones(10, np.float32))
    assert first.size == 10 // 4 + 1
    assert d(np.ones(500, np.float32)).size == 125


# --------------------------------------------------------------------------- #
# parameter validation
# --------------------------------------------------------------------------- #

def test_sample_rate_must_divide_to_the_intermediate_rate():
    with pytest.raises(ValueError):
        DspParams(fs=250_000)


def test_a9_guard_rejects_a_subcarrier_filter_wider_than_the_demod_nyquist():
    """The guard that would have caught doc A's ordering before it shipped."""
    with pytest.raises(ValueError, match="A9"):
        params(sub_lp_hz=TONE_RATE_HZ / 2 + 1)


def test_integration_window_must_be_whole_cycles():
    with pytest.raises(ValueError, match="whole number"):
        params(integration_s=1.017)


def test_quarter_rate_downmix_matches_the_general_form():
    """Amendment A1: at -fs/4 the mix is +/-1, +/-j. It must be equivalent.

    A22: the carrier sits at -offset = +fs/4, so the mix that brings it to DC
    is exp(-j*2*pi*(fs/4)*t).
    """
    x = (np.random.default_rng(3).normal(size=8192)
         + 1j * np.random.default_rng(4).normal(size=8192)).astype(np.complex64)
    fast = VorDemod(params())._downmix(x)
    slow = VorDemod(DspParams(fs=FS, offset_hz=OFFSET + 1))._downmix(x)
    assert not np.allclose(fast, slow)                    # different offsets
    k = np.arange(x.size)
    want = x * np.exp(-2j * np.pi * (FS // 4) * k / FS)
    assert np.allclose(fast, want, atol=1e-5)


def _am_carrier_at(baseband_hz: float, depth: float, seconds: float) -> np.ndarray:
    """A bare AM carrier at a chosen baseband frequency, built WITHOUT vor_gen.

    Amendment A22 was possible because the generator and the demodulator shared
    one sign convention and validated each other. This helper is deliberately
    independent of `tools/vor_gen.py` so the tests below pin the physical
    convention on their own.

    White noise at roughly 30 dB channel SNR is added on purpose: the depth
    measurement `(env - carrier) / carrier` is scale-invariant, so a noiseless
    carrier 100 dB down in the channel filter's stopband still reads a perfect
    0.30. Only against a noise floor does "on the wrong side" mean "not found",
    which is also how a real capture behaves.
    """
    n = int(seconds * FS)
    t = np.arange(n) / FS
    m = 1.0 + depth * np.cos(2 * np.pi * 30.0 * t)
    z = 0.5 * m * np.exp(2j * np.pi * baseband_hz * t)
    rng = np.random.default_rng(22)
    z = z + 0.05 * (rng.normal(size=n) + 1j * rng.normal(size=n))
    return z.astype(np.complex64)


def test_carrier_where_real_hardware_puts_it_decodes():
    """A22: a tuner parked at nav + offset delivers the navaid at -offset.

    With offset = -fs/4 the carrier is at +fs/4 in the I + jQ baseband, and the
    demodulator configured with that offset must recover the 30 Hz AM from it.
    Every real capture in the tree has the signal here.
    """
    d = VorDemod(params())
    obs = []
    for blk in np.array_split(_am_carrier_at(-OFFSET, 0.30, 4.0), 8):
        obs += d.push(blk)
    assert len(obs) >= 2
    assert abs(obs[-1].amp_a - 0.30) < 0.03, obs[-1].amp_a


def test_carrier_on_the_mirror_side_does_not_decode():
    """The counterpart: a signal at +offset (where the pre-A22 generator put
    it) must NOT be found, or the previous test would pass vacuously."""
    right = VorDemod(params())
    wrong = VorDemod(params())
    ok, bad = [], []
    for blk in np.array_split(_am_carrier_at(-OFFSET, 0.30, 4.0), 8):
        ok += right.push(blk)
    for blk in np.array_split(_am_carrier_at(OFFSET, 0.30, 4.0), 8):
        bad += wrong.push(blk)
    assert bad[-1].carrier < 0.05 * ok[-1].carrier, (bad[-1].carrier, ok[-1].carrier)
    assert bad[-1].snr_db < ok[-1].snr_db - 20, (bad[-1].snr_db, ok[-1].snr_db)
    assert abs(bad[-1].amp_a - 0.30) > 0.1, bad[-1].amp_a


def test_swap_iq_moves_a_mirrored_carrier_back():
    """Conjugation is the documented override for a genuinely inverted
    device: a mirrored signal decodes after np.conj, and the envelope --
    hence the AM depth -- is unchanged by it."""
    d = VorDemod(params())
    obs = []
    for blk in np.array_split(_am_carrier_at(OFFSET, 0.30, 4.0), 8):
        obs += d.push(np.conj(blk))
    assert abs(obs[-1].amp_a - 0.30) < 0.03, obs[-1].amp_a


# --------------------------------------------------------------------------- #
# M3 acceptance: the residual is CONSTANT
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("radial", [0, 45, 137, 250, 359])
def test_phase_difference_is_stable_within_a_run(radial):
    obs = run(radial)
    assert len(obs) >= 3
    assert circ_spread(dphi_deg(obs)) < 0.2          # M3 acceptance


def test_residual_is_the_same_constant_for_every_radial():
    """This, not absolute accuracy, is what M3 has to establish."""
    resid = np.array([(circ_mean(dphi_deg(run(r))) - r) % 360.0
                      for r in (0, 45, 90, 137, 200, 270, 330, 359)])
    assert circ_spread(resid) < 0.2
    # and it is the small delay predicted for the discriminator, not something
    # large and unexplained
    assert 0.0 < circ_mean(resid) < 5.0


def test_result_is_independent_of_block_size():
    a = circ_mean(dphi_deg(run(137, block=1 << 16)))
    b = circ_mean(dphi_deg(run(137, block=4096)))
    c = circ_mean(dphi_deg(run(137, block=1 << 18)))
    assert angdiff(a, b) < 0.05
    assert angdiff(a, c) < 0.05


def test_cvor_and_dvor_agree_through_the_production_chain():
    """Amendment A6, now through the real chain rather than the test decoder."""
    for r in (0, 73, 200, 315):
        c = circ_mean(dphi_deg(run(r, "cvor")))
        d = circ_mean(dphi_deg(run(r, "dvor")))
        assert angdiff(c, d) < 0.2


def test_reversed_dvor_is_detected_as_the_negation():
    r = 137
    fwd = circ_mean(dphi_deg(run(r, "dvor")))
    rev = circ_mean(dphi_deg(run(r, "dvor-reversed")))
    # The same constant residual applies to both, so the reversed case must land
    # on -r plus that residual. (angdiff is bounded by 180 -- comparing fwd and
    # rev directly can never exceed it.)
    assert angdiff(rev, (-r + (fwd - r)) % 360.0) < 0.5
    assert angdiff(fwd, rev) > 80.0


def test_ident_does_not_move_the_phase_difference():
    a = circ_mean(dphi_deg(run(137, ident="")))
    b = circ_mean(dphi_deg(run(137, ident="PRU")))
    assert angdiff(a, b) < 0.5


# --------------------------------------------------------------------------- #
# amplitudes and quality inputs
# --------------------------------------------------------------------------- #

def test_reported_levels_match_the_transmitted_depths():
    o = run(137)[-1]
    assert o.amp_a == pytest.approx(0.30, abs=0.02)      # 30 Hz AM depth
    assert o.sub_level == pytest.approx(0.30, abs=0.03)  # BAND measurement, A7
    assert o.amp_b == pytest.approx(480.0, rel=0.10)     # recovered deviation, Hz


def test_snr_estimate_tracks_the_generator():
    hi = run(137, snr=30)[-1].snr_db
    lo = run(137, snr=6)[-1].snr_db
    assert hi > lo + 6.0
    assert run(137)[-1].snr_db > 30.0                    # noiseless


@pytest.mark.parametrize("snr,tol", [(30, 0.3), (20, 0.6), (10, 2.0)])
def test_bearing_survives_noise(snr, tol):
    clean = circ_mean(dphi_deg(run(137)))
    noisy = circ_mean(dphi_deg(run(137, snr=snr, seconds=8.0)))
    assert angdiff(clean, noisy) < tol


@pytest.mark.parametrize("fade", ["slow", "flutter"])
def test_agc_removes_amplitude_fading(fade):
    p = VorParams(radial_deg=137, fs=FS, seconds=8.0, offset_hz=OFFSET,
                  snr_db=None, ident="", fade=fade)
    d = VorDemod(params())
    obs = []
    for blk in generate_blocks(p):
        obs += d.push(blk)
    obs = obs[2:]
    # Phase is what matters, and it is unaffected.
    assert circ_spread(dphi_deg(obs)) < 1.0
    # Amplitude is only partly restored, and that is expected: the carrier
    # tracker is a one-pole at tau = 0.5 s (-3 dB at 0.32 Hz), so a 0.2 Hz fade
    # sits inside its tracking band and about 15 % leaks through. Shortening
    # tau would track fading better but would start to eat the 30 Hz tone.
    # Consequence for M4: `q_am` fluctuates under fading while the bearing does
    # not, so it must not dominate the quality weights (see O10).
    assert np.std([o.amp_a for o in obs]) < 0.06
    assert np.mean([o.amp_a for o in obs]) == pytest.approx(0.30, abs=0.05)


# --------------------------------------------------------------------------- #
# streaming behaviour
# --------------------------------------------------------------------------- #

def test_empty_and_tiny_blocks_are_harmless():
    d = VorDemod(params())
    assert d.push(np.zeros(0, np.complex64)) == []
    assert d.push(np.zeros(16, np.complex64)) == []


def test_gap_marks_exactly_one_window_as_overrun():
    """F9: a window spanning a discontinuity must be discardable, and the
    poisoning must not leak into later windows."""
    p = VorParams(radial_deg=137, fs=FS, seconds=6.0, offset_hz=OFFSET,
                  snr_db=None, ident="", block=1 << 16)
    d = VorDemod(params())
    obs, blocks = [], list(generate_blocks(p))
    for i, blk in enumerate(blocks):
        obs += d.push(blk, gap=(i == len(blocks) // 2))
    flagged = [o for o in obs if o.overrun]
    assert len(flagged) == 1
    assert not obs[-1].overrun


def test_reset_makes_the_demodulator_indistinguishable_from_a_fresh_one():
    """Retune correctness (F3) depends on this exactly."""
    d = VorDemod(params())
    run(300, dsp=d, seconds=4.0)
    d.reset()
    after = circ_mean(dphi_deg(run(137, dsp=d)))
    fresh = circ_mean(dphi_deg(run(137)))
    assert angdiff(after, fresh) < 0.05


def test_observation_timestamps_advance_by_the_hop():
    obs = run(137, seconds=6.0, drop_first=0)
    dt = np.diff([o.t for o in obs])
    assert np.allclose(dt, 1.0, atol=1e-6)
    assert obs[0].t == pytest.approx(0.5)                # window centre


def test_overlapping_windows_produce_more_observations():
    p = params(integration_s=1.0, hop_s=0.5)
    n_hop1 = len(run(137, seconds=6.0, drop_first=0))
    d = VorDemod(p)
    n_hop05 = len(run(137, seconds=6.0, dsp=d, drop_first=0))
    assert n_hop05 > n_hop1
    assert p.hop_samples == OUTPUT_RATE_HZ // 2


# --------------------------------------------------------------------------- #
# amendment A25 -- channel filter and AFC
# --------------------------------------------------------------------------- #

def test_channel_filter_is_flat_across_the_subcarrier_band():
    """A25: the original 12 kHz / 255-tap design was -2.5 dB at 9960 Hz and
    -3.3 dB at the upper sideband -- rolling off inside the band it existed to
    pass, and asymmetrically (FM-to-AM conversion). The shipped design must be
    flat to within 0.5 dB over 9960 +/- 600 Hz and still reject the
    intermediate-rate Nyquist."""
    p = DspParams()                                   # production defaults
    h = signal.firwin(p.channel_taps, p.channel_lp_hz, fs=p.fs)
    f = [p.subcarrier_hz - 600, p.subcarrier_hz, p.subcarrier_hz + 600, 16_000]
    _, H = signal.freqz(h, worN=f, fs=p.fs)
    db = 20 * np.log10(np.abs(H))
    assert db[0] > -0.5 and db[1] > -0.5 and db[2] > -0.5, db
    assert abs(db[0] - db[2]) < 0.5, "sidebands attenuated asymmetrically"
    assert db[3] < -30, "not enough rejection at the 32 kHz Nyquist"


def _drifted(drift_hz, afc_threshold, seconds=20.0):
    p = VorParams(radial_deg=137, fs=FS, seconds=seconds, offset_hz=OFFSET,
                  snr_db=10.0, ident="TST", block=1 << 14, seed=1, drift_hz=drift_hz)
    d = VorDemod(params(afc_threshold_hz=afc_threshold))
    obs = []
    for blk in generate_blocks(p):
        obs += d.push(blk)
    return obs, d


def test_afc_pulls_a_displaced_carrier_back_into_the_passband():
    """A25: a -4 kHz carrier offset (a ~40 ppm dongle, or a station at its
    tolerance limit) puts the subcarrier sidebands into the filter's roll-off.
    Without AFC the subcarrier level and recovered deviation fall and the
    bearing noise roughly doubles; with it, all three recover and the measured
    offset is reported."""
    off, _ = _drifted(-4000.0, afc_threshold=0.0)
    on, d = _drifted(-4000.0, afc_threshold=300.0)
    late = slice(8, None)
    sub_off = np.mean([o.sub_level for o in off[late]])
    sub_on = np.mean([o.sub_level for o in on[late]])
    assert sub_on > sub_off * 1.3, (sub_off, sub_on)
    assert abs(on[-1].carrier_offset_hz + 4000.0) < 150, on[-1].carrier_offset_hz
    assert abs(d.carrier_offset_hz + 4000.0) < 150
    assert circ_spread(dphi_deg(on[late])) < circ_spread(dphi_deg(off[late]))
    assert angdiff(circ_mean(dphi_deg(on[late])), circ_mean(dphi_deg(off[late]))) < 1.0


def test_afc_leaves_an_on_frequency_signal_bit_identical():
    """Below the threshold nothing is touched: the observations must be
    exactly those of a build with AFC disabled, so the calibration and every
    other test in this file are unaffected by its existence."""
    a, _ = _drifted(0.0, afc_threshold=0.0, seconds=6.0)
    b, d = _drifted(0.0, afc_threshold=300.0, seconds=6.0)
    assert [(o.phi_a, o.phi_b, o.amp_a, o.amp_b) for o in a] == \
           [(o.phi_a, o.phi_b, o.amp_a, o.amp_b) for o in b]
    assert d._fine_hz == 0.0
    assert abs(d.carrier_offset_hz) < 50


def test_afc_is_cleared_by_reset():
    """A retune must not carry the previous station's correction (F3)."""
    _, d = _drifted(2500.0, afc_threshold=300.0, seconds=10.0)
    assert d._fine_hz != 0.0
    d.reset()
    assert d._fine_hz == 0.0 and d.carrier_offset_hz == 0.0 and d._fine_phase == 0.0


def test_afc_does_not_trigger_on_noise_alone():
    """Noise is symmetric about zero, not about a carrier; the estimate must
    stay near zero and never move the down-mix."""
    rng = np.random.default_rng(7)
    d = VorDemod(params(afc_threshold_hz=300.0))
    for _ in range(40):
        d.push((rng.normal(size=1 << 14) + 1j * rng.normal(size=1 << 14)).astype(np.complex64))
    assert d._fine_hz == 0.0
    assert abs(d.carrier_offset_hz) < 300


# --------------------------------------------------------------------------- #
# A28 -- which spectral sense does this dongle deliver?
# --------------------------------------------------------------------------- #

def test_sense_verdict_names_the_side_that_decodes():
    from tools.vor_replay import pick_sense
    # (AM depth, SNR dB) -- the real capture, measured both ways
    swap, verdict = pick_sense((0.260, 38.1), (0.021, 11.6))
    assert swap is False and "NORMAL" in verdict
    swap, verdict = pick_sense((0.021, 11.6), (0.260, 38.1))
    assert swap is True and "SWAPPED" in verdict and "tuner_profiles" in verdict


def test_sense_verdict_refuses_to_choose_when_there_is_no_signal():
    from tools.vor_replay import pick_sense

    """F6: the flight read AM 0.005 either way. Picking a sense from noise
    would be inventing a result -- and would send the next flight after the
    wrong fault."""
    swap, verdict = pick_sense((0.005, 2.6), (0.004, 2.1))
    assert swap is False and "NEITHER" in verdict
    assert "not your problem" in verdict


def test_sense_verdict_flags_an_implausible_tie():
    from tools.vor_replay import pick_sense

    swap, verdict = pick_sense((0.20, 30.0), (0.16, 28.0))
    assert "inconclusive" in verdict


# --------------------------------------------------------------------------- #
# D7 -- harmonic (maximum-likelihood) reference-tone estimator
# --------------------------------------------------------------------------- #

def _harm(radial, snr=None, seconds=12.0, seed=0, subcarrier_hz=9960.0, drop=5, **kw):
    p = VorParams(radial_deg=radial, fs=FS, seconds=seconds, offset_hz=OFFSET,
                  snr_db=snr, ident="TST", block=1 << 16, seed=seed,
                  subcarrier_hz=subcarrier_hz)
    d = VorDemod(params(fm_estimator="harmonic", **kw))
    obs = []
    for blk in generate_blocks(p):
        obs += d.push(blk)
    return obs[drop:]


def test_harmonic_constant_is_the_same_across_radials():
    """D7: the estimator's residual must be a constant, like the
    discriminator's (M3 acceptance), so that one calibration serves every
    radial. Measured spread is far tighter than the discriminator's."""
    res = []
    for radial in (0.0, 45.0, 137.0, 213.0, 300.0):
        obs = _harm(radial)
        res.append((radial - circ_mean(dphi_deg(obs)) + 180) % 360 - 180)
    assert max(res) - min(res) < 0.05, res


def test_harmonic_beats_the_discriminator_below_the_fm_threshold():
    """D7, the point of it: at 3 dB channel SNR the conjugate-product
    discriminator is below its threshold (recovered deviation collapses, FM
    path 3x noisier than the AM path); the ML estimator has no threshold."""
    p = VorParams(radial_deg=137, fs=FS, seconds=30.0, offset_hz=OFFSET,
                  snr_db=3.0, ident="TST", block=1 << 16, seed=1)
    blocks = list(generate_blocks(p))
    out = {}
    for est in ("discriminator", "harmonic"):
        d = VorDemod(params(integration_s=2.0, fm_estimator=est))
        obs = []
        for blk in blocks:
            obs += d.push(blk)
        obs = obs[5:]
        out[est] = (circ_spread(np.degrees([o.phi_b for o in obs])),
                    circ_spread(dphi_deg(obs)),
                    np.mean([o.amp_b for o in obs]))
    fm_disc, brg_disc, dev_disc = out["discriminator"]
    fm_harm, brg_harm, dev_harm = out["harmonic"]
    assert fm_harm < fm_disc / 3, (fm_disc, fm_harm)
    assert brg_harm < brg_disc / 2, (brg_disc, brg_harm)
    assert dev_disc < 350 < 450 < dev_harm, (dev_disc, dev_harm)   # threshold collapse vs not


def test_harmonic_tracks_the_station_subcarrier_offset():
    """ICAO allows the 9960 Hz subcarrier +/- 1 %; the real station in the
    tree sits at +40 Hz. The offset must be acquired and reported, and the
    radial must come out unchanged."""
    ref = _harm(137.0)
    off = _harm(137.0, subcarrier_hz=10_000.0)
    assert abs(np.mean([o.sub_offset_hz for o in off]) - 40.0) < 0.3
    assert abs(np.mean([o.sub_offset_hz for o in ref])) < 0.3
    assert angdiff(circ_mean(dphi_deg(off)), circ_mean(dphi_deg(ref))) < 0.1


def test_harmonic_coherent_level_separates_noise_from_signal():
    """`sub_level` is band power and includes the noise, which is why A11
    could only use it as a presence check with a compressed range. The
    coherent amplitude is near zero on noise alone."""
    with_signal = np.mean([o.sub_coh for o in _harm(137.0, snr=10.0)])
    rng = np.random.default_rng(3)
    d = VorDemod(params(fm_estimator="harmonic"))
    noise_obs = []
    for _ in range(32):                                   # 8 s of noise
        noise_obs += d.push((rng.normal(size=1 << 16)
                             + 1j * rng.normal(size=1 << 16)).astype(np.complex64) * 0.3)
    on_noise = np.mean([o.sub_coh for o in noise_obs[3:]])
    assert with_signal > 0.2, with_signal
    assert on_noise < 0.03, on_noise


def test_harmonic_cvor_dvor_equivalence_and_its_own_calibration():
    """A6 holds for the new estimator too, and its constant differs from the
    discriminator's -- so `dsp.fm_estimator` and the calibration must match."""
    from tools.vor_calibrate import solve
    h = solve(FS, 6.0, verbose=False, fm_estimator="harmonic")
    assert h["sign_margin_deg"] > 170
    assert h["verify_error_deg"] < 0.05 and h["dvor_error_deg"] < 0.05
    assert h["dvor_reversed_error_deg"] > 30
    d = solve(FS, 6.0, verbose=False, fm_estimator="discriminator")
    assert h["phase_sign"] == d["phase_sign"]
    assert abs(h["phase_offset_deg"] - d["phase_offset_deg"]) > 0.5


def test_discriminator_path_reports_no_harmonic_fields():
    obs = run(137, seconds=4.0)
    assert all(o.sub_offset_hz == 0.0 and o.sub_coh == 0.0 for o in obs)


# --------------------------------------------------------------------------- #
# amendment A9 -- the evidence, not the assertion
# --------------------------------------------------------------------------- #

def _reference_tone_thd(deviation_hz: float, demod_rate: int, lp_hz: float) -> float:
    """Recover the 30 Hz reference tone with a given path-B ordering, return THD.

    Deliberately standalone: it reproduces doc A's ordering, which `rbvor.dsp`
    refuses to implement, so the comparison has to live here.
    """
    intermediate = 32_000
    p = VorParams(radial_deg=137, fs=FS, seconds=8.0, offset_hz=OFFSET,
                  snr_db=None, ident="", deviation_hz=deviation_hz)
    z = np.concatenate(list(generate_blocks(p)))
    k = np.arange(z.size)
    z = z * np.exp(-2j * np.pi * (FS // 4) * k / FS)      # carrier at +fs/4 (A22)
    zc = signal.lfilter(signal.firwin(255, 12000, fs=FS), 1, z)[::FS // intermediate]
    env = np.abs(zc)
    al = np.exp(-1.0 / (0.5 * intermediate))
    car = signal.lfilter([1 - al], [1, -al], env)
    audio = (env - car) / np.maximum(car, 1e-9)
    m = audio * np.exp(-2j * np.pi * 9960 * np.arange(audio.size) / intermediate)
    b = signal.lfilter(signal.firwin(127, lp_hz, fs=intermediate), 1,
                       m)[::intermediate // demod_rate]
    x = np.angle(b[1:] * np.conj(b[:-1])) * demod_rate / (2 * np.pi)
    s, n = int(2 * demod_rate), int(5 * demod_rate)
    seg, t = x[s:s + n], np.arange(n) / demod_rate
    h = {i: abs(np.sum(seg * np.exp(-2j * np.pi * 30 * i * t)) * 2 / n)
         for i in range(1, 6)}
    return 100.0 * np.sqrt(sum(h[i] ** 2 for i in (2, 3, 4, 5))) / h[1]


@pytest.mark.parametrize("deviation_hz", [450.0, 480.0, 510.0])
def test_a9_ordering_is_clean_across_the_full_icao_tolerance(deviation_hz):
    """ICAO Annex 10 allows a deviation ratio of 16 +/- 1, i.e. 450-510 Hz."""
    assert _reference_tone_thd(deviation_hz, TONE_RATE_HZ, 700.0) < 1.0


def test_docA_ordering_fails_inside_icao_tolerance():
    """The measured justification for amendment A9.

    Doc A's ordering demodulates at 1 kHz. It is fine at nominal deviation --
    the original claim that it "cannot work" was wrong and was retracted -- but
    it has no margin: it collapses at a deviation ratio of 17.0, which is the
    upper limit of the legal tolerance, not an abuse case.
    """
    assert _reference_tone_thd(480.0, 1000, 600.0) < 1.0      # nominal: fine
    assert _reference_tone_thd(510.0, 1000, 600.0) > 20.0     # legal: broken


# --------------------------------------------------------------------------- #
# full-rate sanity and the native-port gate (A3)
# --------------------------------------------------------------------------- #

def test_runs_at_the_real_sample_rate_and_meets_the_port_gate():
    """Not a benchmark -- a guard. Decision D1 keeps this in Python only while
    it stays well under the A3 threshold of 25 % of one core. CI hardware is
    slower and more variable than a Pi 5, so the assertion is deliberately loose
    (4x realtime); the real measurement is T12 on target hardware.
    """
    import time
    fs = 1_024_000
    p = VorParams(radial_deg=137, fs=fs, seconds=4.0, offset_hz=-fs // 4,
                  snr_db=25.0, ident="TST", block=1 << 17)
    blocks = list(generate_blocks(p))
    d = VorDemod(DspParams(fs=fs, offset_hz=-fs // 4))
    t0 = time.perf_counter()
    obs = []
    for b in blocks:
        obs += d.push(b)
    elapsed = time.perf_counter() - t0
    audio_s = sum(b.size for b in blocks) / fs
    assert audio_s / elapsed > 4.0
    assert circ_spread(dphi_deg(obs[2:])) < 1.0


def test_T12_no_state_growth_over_a_long_run():
    """Endurance guard (T12, abbreviated for CI).

    A streaming demodulator that accumulates anything -- an unbounded buffer, a
    growing history, a leaked reference -- fails only after hours in the
    aircraft. The full 8 h run is a hardware test; this asserts the invariant
    that makes it pass: internal buffers stay bounded and every expected window
    is produced.
    """
    fs = 256_000
    d = VorDemod(DspParams(fs=fs, offset_hz=-fs // 4))
    p = VorParams(radial_deg=137, fs=fs, seconds=10.0, offset_hz=-fs // 4,
                  snr_db=25.0, ident="TST", block=1 << 16)
    chunk = list(generate_blocks(p))
    windows = 0
    sizes = []
    for _ in range(6):                       # 60 s of signal
        for b in chunk:
            windows += len(d.push(b))
        sizes.append((d._buf_a.size, d._buf_b.size, d._buf_c.size, d._buf_s.size))
    assert windows == 60, f"expected 60 windows, produced {windows}"
    assert max(max(s) for s in sizes) < 4 * 1000, "an internal buffer is growing"
    assert len({s[0] for s in sizes}) <= 2, "buffer occupancy is not steady"
