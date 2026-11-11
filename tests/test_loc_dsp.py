"""L3 -- the localizer demodulator, against the synthetic generator.

CLAUDE.md A33-A36. DDM across -0.2..+0.2, SDM, the tone-frequency sweep over
the whole legal tolerance with its edges (A34), carrier offset with and
without AFC, a sample gap, block-size invariance, bounded state over a long
run (T12's invariant), and noise only at the demodulator level (the state
machine half of plan 4.1 is in test_loc_controller.py).
"""

from __future__ import annotations

import numpy as np
import pytest

from rbvor.dsp import DspParams, VorDemod
from rbvor.loc import (
    DDM_FULL_SCALE,
    LocDemod,
    LocDspParams,
    LocObservation,
    ddm_to_dev_pct,
)
from tools.loc_gen import LocParams, generate_blocks

FS = 256_000
OFF = -FS // 4


def decode(seconds=6.0, dsp=None, block=None, fs=FS, **gen):
    gen.setdefault("snr_db", None)
    p = LocParams(fs=fs, offset_hz=-fs // 4, seconds=seconds, **gen)
    d = LocDemod(LocDspParams(fs=fs, offset_hz=-fs // 4, **(dsp or {})))
    obs = []
    for b in generate_blocks(p):
        if block:
            for i in range(0, b.size, block):
                obs += d.push(b[i:i + block])
        else:
            obs += d.push(b)
    return obs, d


def settled(obs, after_s=1.5):
    return [o for o in obs if o.t - 0.5 >= after_s]


@pytest.mark.parametrize("ddm", [-0.2, -0.155, -0.0775, -0.01, 0.0, 0.01, 0.0775, 0.155, 0.2])
def test_ddm_and_sdm_read_as_transmitted(ddm):
    obs, _ = decode(ddm=ddm)
    s = settled(obs)
    assert len(s) >= 5
    for o in s:
        assert o.ddm == pytest.approx(ddm, abs=2e-4)
        assert o.sdm == pytest.approx(0.40, abs=1e-3)


def test_sign_150_predominating_is_positive_and_swapped_tones_negate():
    """+ = 150 Hz predominates = right of course = fly left. The swapped case
    must fail, so a decoder that read |DDM| or ignored which tone is which
    cannot pass."""
    good = settled(decode(ddm=0.05)[0])
    bad = settled(decode(ddm=0.05, swap_tones=True)[0])
    assert all(o.m150 > o.m90 and o.ddm > 0 for o in good)
    assert all(o.ddm == pytest.approx(-0.05, abs=2e-4) for o in bad)


@pytest.mark.parametrize("eps", [-0.03, -0.025, -0.02, -0.015, -0.01, -0.005,
                                 0.005, 0.01, 0.015, 0.02, 0.025, 0.03])
def test_ddm_holds_across_the_whole_legal_tone_tolerance(eps):
    """Annex 10 Vol I 3.1.3.5.3: +/-2.5 % for Cat I, edges included, plus margin."""
    s = settled(decode(ddm=0.1, tone_offset=eps)[0])
    for o in s:
        assert o.ddm == pytest.approx(0.1, abs=5e-4)
        assert o.sdm == pytest.approx(0.40, abs=2e-3)
        assert o.tone_offset == pytest.approx(eps, abs=2e-4)


def test_a_fixed_bin_points_the_needle_wrong_on_a_lawful_station():
    """The control for the test above: with the search disabled the DDM is
    wrong on a lawful station. Measured (loc-sweep.md): at 1 % off nominal --
    inside even Cat III's tolerance -- the 150 Hz tone loses more than the
    90 Hz one and an aircraft at +0.100 DDM reads about -0.044, the WRONG
    side; at the 2.5 % Cat I limit both tones are lost and it reads about 0,
    a centred needle. If this ever passes with the search on, the sweep is
    not testing the search."""
    fixed = {"tone_search": 1e-6, "tone_step": 1e-6}
    one = settled(decode(ddm=0.1, tone_offset=0.01, dsp=fixed)[0])
    edge = settled(decode(ddm=0.1, tone_offset=0.025, dsp=fixed)[0])
    assert all(o.ddm < -0.02 for o in one)
    assert all(abs(o.ddm) < 0.01 for o in edge)


def test_tone_phase_offset_within_the_lock_tolerance_changes_nothing():
    """3.1.3.5.3.3 allows 20 deg between the tones; amplitudes do not care."""
    s = settled(decode(ddm=-0.07, phase150_deg=20.0)[0])
    assert all(o.ddm == pytest.approx(-0.07, abs=2e-4) for o in s)


@pytest.mark.parametrize("drift,afc", [(4000.0, 300.0), (4000.0, 0.0),
                                       (-5500.0, 300.0), (-5500.0, 0.0)])
def test_carrier_offset_with_and_without_afc(drift, afc):
    """Single carrier up to the 0.005 % tolerance (5.6 kHz at 111.95 MHz).
    The envelope does not care where the carrier is inside the channel, so
    the DDM is right either way; with AFC on, the offset is also measured."""
    s = settled(decode(seconds=8.0, ddm=0.0775, drift_hz=drift,
                       dsp={"afc_threshold_hz": afc})[0], after_s=3.0)
    assert all(o.ddm == pytest.approx(0.0775, abs=1e-3) for o in s)
    if afc:
        assert s[-1].carrier_offset_hz == pytest.approx(drift, abs=20)
    else:
        assert s[-1].carrier_offset_hz == 0.0


def test_afc_estimate_is_the_vor_front_ends_own():
    """The front end is VorDemod's, composed again: same AFC answer on the
    same input."""
    p = LocParams(fs=FS, offset_hz=OFF, seconds=4.0, snr_db=30.0, drift_hz=1500.0)
    blocks = list(generate_blocks(p))
    loc = LocDemod(LocDspParams(fs=FS, offset_hz=OFF))
    vor = VorDemod(DspParams(fs=FS, offset_hz=OFF))
    for b in blocks:
        loc.push(b)
        vor.push(b)
    assert loc.carrier_offset_hz == pytest.approx(vor.carrier_offset_hz, abs=5.0)
    assert LocDemod._downmix is VorDemod._downmix


def test_a_sample_gap_poisons_exactly_the_window_in_progress():
    p = LocParams(fs=FS, offset_hz=OFF, seconds=6.0, snr_db=30.0)
    d = LocDemod(LocDspParams(fs=FS, offset_hz=OFF))
    flags = []
    for i, b in enumerate(generate_blocks(p)):
        flags += [o.overrun for o in d.push(b, gap=(i == 10))]
    assert flags.count(True) == 1
    assert flags.index(True) > 0 and not flags[-1]


def test_output_does_not_depend_on_block_size():
    a = decode(seconds=4.0, ddm=0.03, snr_db=20.0)[0]
    b = decode(seconds=4.0, ddm=0.03, snr_db=20.0, block=10_000)[0]
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        assert x.ddm == pytest.approx(y.ddm, abs=1e-5)
        assert x.t == y.t


def test_reset_is_a_full_flush():
    p = LocParams(fs=FS, offset_hz=OFF, seconds=4.0, snr_db=20.0, drift_hz=2000.0)
    blocks = list(generate_blocks(p))
    d = LocDemod(LocDspParams(fs=FS, offset_hz=OFF))
    first = [o for b in blocks for o in d.push(b)]
    d.reset()
    second = [o for b in blocks for o in d.push(b)]
    assert [o.ddm for o in first] == [o.ddm for o in second]
    assert [o.carrier_offset_hz for o in first] == [o.carrier_offset_hz for o in second]


def test_first_window_is_not_a_start_up_transient():
    """A35: VorDemod-style priming made the first windows read depths up to
    13x (measured). The localizer primes after the channel filter fills."""
    obs, _ = decode(seconds=3.0, ddm=0.155)
    assert obs[0].sdm == pytest.approx(0.40, abs=0.01)
    assert obs[0].ddm == pytest.approx(0.155, abs=0.005)


def test_T12_no_state_growth_over_a_long_run():
    """Endurance invariant: buffers bounded, every expected window produced."""
    fs = 64_000
    p = LocParams(fs=fs, offset_hz=-fs // 4, seconds=10.0, snr_db=25.0, block=1 << 14)
    chunk = list(generate_blocks(p))
    d = LocDemod(LocDspParams(fs=fs, offset_hz=-fs // 4))
    windows, sizes = 0, []
    for _ in range(12):                                     # 120 s
        for b in chunk:
            windows += len(d.push(b))
        sizes.append((d._buf.size, d._buf_c.size))
    # 1 s windows at 0.5 s hop over 120 s, less the channel-filter warm-up
    assert windows == 238
    assert max(max(s) for s in sizes) < 2 * d.p.window_samples
    assert len({s[0] for s in sizes}) <= 2


def _cu8_noise(seconds, rms, seed, fs=FS):
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    z = rng.normal(0, rms, n) + 1j * rng.normal(0, rms, n)
    q = np.clip(np.round(np.stack([z.real, z.imag]) * 127.5 + 127.5), 0, 255)
    return ((q[0] - 127.5) / 127.5 + 1j * (q[1] - 127.5) / 127.5).astype(np.complex64)


@pytest.mark.parametrize("rms,seed", [(0.02, 1), (0.1, 2), (0.3, 3)])
def test_noise_reads_a_centred_needle_which_is_why_validity_is_separate(rms, seed):
    """Plan 4.1, demodulator half: noise gives a DDM near 0 -- "on the
    centreline" -- so the DDM can never be its own validity. What separates
    noise is SDM and tone SNR, far from any healthy value (loc-sweep.md)."""
    d = LocDemod(LocDspParams(fs=FS, offset_hz=OFF))
    z = _cu8_noise(12.0, rms, seed)
    obs = [o for i in range(0, z.size, 65536) for o in d.push(z[i:i + 65536])]
    s = settled(obs)
    assert abs(np.mean([o.ddm for o in s])) < 0.005
    assert max(o.sdm for o in s) < 0.08
    assert max(o.snr_db for o in s) < 13.0


def test_params_refuse_windows_that_are_not_whole_tone_cycles():
    with pytest.raises(ValueError):
        LocDspParams(integration_s=0.51)
    with pytest.raises(ValueError):
        LocDspParams(hop_s=2.0, integration_s=1.0)
    with pytest.raises(ValueError):
        LocDspParams(estimator="peak")
    with pytest.raises(ValueError):
        LocDspParams(noise_bins_hz=(150.0, 120.0))


def test_from_dsp_shares_the_vor_front_end():
    d = DspParams(fs=FS, offset_hz=OFF, afc_threshold_hz=123.0)
    p = LocDspParams.from_dsp(d, integration_s=0.5, hop_s=0.2)
    assert (p.fs, p.offset_hz, p.channel_taps, p.env_lp_hz, p.afc_threshold_hz) == \
        (FS, OFF, d.channel_taps, d.sub_lp_hz, 123.0)


def test_observation_forms():
    o = LocObservation(t=0, m90=0.15, m150=0.25, tone_offset=0, carrier=1, noise_est=0.002)
    assert o.ddm == pytest.approx(0.10) and o.sdm == pytest.approx(0.40)
    assert o.ddm_norm == pytest.approx(0.10)
    half = LocObservation(t=0, m90=0.075, m150=0.125, tone_offset=0, carrier=1,
                          noise_est=0.001)
    assert half.ddm_norm == pytest.approx(0.10)          # normalised form undoes a common factor
    assert half.snr_db == pytest.approx(20 * np.log10(0.1 / 0.001))


@pytest.mark.parametrize("ddm,pct", [
    (0.0, 0), (0.155, 100), (-0.155, -100), (0.3, 100), (-0.3, -100),
    (0.0775, 50), (-0.0775, -50), (0.000775, 1), (-0.000775, -1),
    (0.0007, 0), (-0.0007, 0),
])
def test_locdev_is_percent_of_full_scale_symmetric_and_clamped(ddm, pct):
    assert ddm_to_dev_pct(ddm) == pct
    assert DDM_FULL_SCALE == 0.155
