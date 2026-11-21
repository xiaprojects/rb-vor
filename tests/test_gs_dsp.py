"""G3/G4 -- the glide-slope demodulator against the generator (docs/gs-plan.md
4.2, 4.3; CLAUDE.md A41, A42).

`GsDemod` is `LocDemod` with the glide path's nominal SDM in the derived DDMs
and a coarse carrier search before the channel filter. With the search off it
must be `LocDemod` sample for sample; with it on, a lawful station at the
0.005 % limit (16.75 kHz at 335 MHz), and that plus a 20 ppm dongle, must
decode -- and without it must not (the case that gives the test teeth).
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from rbvor.gs import (
    DDM_FULL_SCALE,
    SDM_NOMINAL,
    GsDemod,
    GsDspParams,
    GsObservation,
    ddm_to_dev_pct,
    gs_ddm_corrected,
)
from rbvor.loc import LocDemod, LocDspParams
from tools.gs_gen import GsParams, generate_blocks

FS = 256_000
OFF = -FS // 4
ACQ = 30_000.0


def decode(gen_kw=None, dsp_kw=None, seconds=6.0, skip=1.5):
    p = GsParams(fs=FS, offset_hz=OFF, seconds=seconds, **{"snr_db": None, **(gen_kw or {})})
    d = GsDemod(GsDspParams(fs=FS, offset_hz=OFF, **(dsp_kw or {})))
    obs = []
    for b in generate_blocks(p):
        obs += d.push(b)
    return [o for o in obs if o.t - 0.5 >= skip and not o.overrun], d


def test_constants_and_the_published_form():
    assert SDM_NOMINAL == 0.80 and DDM_FULL_SCALE == 0.175
    assert gs_ddm_corrected(0.1, 0.40) == pytest.approx(0.2)    # compression undone
    assert gs_ddm_corrected(0.1, 0.85) == pytest.approx(0.1)    # never scaled down
    assert ddm_to_dev_pct(0.175) == 100 and ddm_to_dev_pct(-0.0875) == -50
    assert ddm_to_dev_pct(0.5) == 100 and ddm_to_dev_pct(-0.5) == -100


def test_with_the_search_off_it_is_locdemod_sample_for_sample():
    p = GsParams(fs=FS, offset_hz=OFF, seconds=5.0, snr_db=15.0, ddm=0.05, seed=3)
    blocks = list(generate_blocks(p))
    a, b = GsDemod(GsDspParams(fs=FS, offset_hz=OFF)), LocDemod(LocDspParams(fs=FS,
                                                                           offset_hz=OFF))
    oa = [o for blk in blocks for o in a.push(blk)]
    ob = [o for blk in blocks for o in b.push(blk)]
    assert len(oa) == len(ob) > 5
    for x, y in zip(oa, ob, strict=True):
        assert isinstance(x, GsObservation)
        dx = dataclasses.asdict(x)
        dx.pop("acquired_hz")
        assert dx == dataclasses.asdict(y)


def test_the_search_within_the_afc_threshold_leaves_the_chain_untouched():
    """A41: below the AFC's own threshold no correction is set, so the chain
    stays identical to LocDemod (and costs nothing extra)."""
    kw = {"ddm": 0.05, "drift_hz": 120.0, "snr_db": 20.0, "seed": 4}
    on, d = decode(kw, {"acquire_hz": ACQ})
    off, _ = decode(kw, {})
    assert d.acquired_hz == pytest.approx(120.0, abs=10)
    assert d._fine_hz == 0.0
    assert [o.ddm for o in on] == [o.ddm for o in off]


@pytest.mark.parametrize("ddm", [-0.25, -0.175, -0.0875, 0.0, 0.03, 0.0875, 0.175, 0.25])
def test_ddm_noiseless(ddm):
    obs, _ = decode({"ddm": ddm})
    assert np.mean([o.ddm for o in obs]) == pytest.approx(ddm, abs=1e-3)
    assert np.mean([o.sdm for o in obs]) == pytest.approx(0.80, abs=2e-3)


def test_swapped_tones_negate():
    obs, _ = decode({"ddm": 0.1, "swap_tones": True})
    assert np.mean([o.ddm for o in obs]) == pytest.approx(-0.1, abs=1e-3)


@pytest.mark.parametrize("sdm", [0.75, 0.85])
@pytest.mark.parametrize("phase", [0.0, 90.0, 180.0])
def test_deep_modulation_at_the_tolerance_edges(sdm, phase):
    """Plan 4.3: SDM 0.85 with the tones in phase brings the envelope within
    15 % of zero; the depths must still read as transmitted."""
    obs, _ = decode({"ddm": 0.0875, "sdm": sdm, "phase150_deg": phase})
    assert np.mean([o.ddm for o in obs]) == pytest.approx(0.0875, abs=1.5e-3)
    assert np.mean([o.sdm for o in obs]) == pytest.approx(sdm, abs=3e-3)


@pytest.mark.parametrize("eps", [-0.025, -0.01, 0.01, 0.025])
def test_tone_search_holds_at_forty_percent_depth(eps):
    obs, _ = decode({"ddm": 0.0875, "tone_offset": eps})
    assert np.mean([o.ddm for o in obs]) == pytest.approx(0.0875, abs=1e-3)
    assert np.mean([o.tone_offset for o in obs]) == pytest.approx(eps, abs=5e-4)


@pytest.mark.parametrize("offset", [16_750.0, -16_750.0, 23_350.0, -23_350.0])
def test_a_lawful_carrier_at_the_limit_decodes_with_the_search(offset):
    """0.005 % at 335 MHz, and that plus 20 ppm at 330 MHz."""
    obs, d = decode({"ddm": 0.0875, "drift_hz": offset, "snr_db": 20.0, "seed": 1},
                    {"acquire_hz": ACQ}, seconds=8.0)
    assert d.acquired_hz == pytest.approx(offset, abs=20)
    assert np.mean([o.ddm_corrected for o in obs]) == pytest.approx(0.0875, abs=0.004)
    assert np.mean([o.snr_db for o in obs]) > 40
    assert all(o.acquired_hz == d.acquired_hz for o in obs)


def test_and_without_the_search_the_same_carrier_is_lost():
    """The case that must fail: A9 again -- a chain that works at nominal and
    collapses inside the legal tolerance. It collapses to 'no signal' (SDM and
    tone SNR near zero), not to a plausible needle."""
    obs, _ = decode({"ddm": 0.0875, "drift_hz": 16_750.0, "snr_db": 20.0, "seed": 1},
                    {}, seconds=8.0)
    assert np.mean([o.sdm for o in obs]) < 0.1
    assert np.mean([o.snr_db for o in obs]) < 15


def test_the_search_on_noise_corrects_nothing():
    rng = np.random.default_rng(5)
    d = GsDemod(GsDspParams(fs=FS, offset_hz=OFF, acquire_hz=ACQ))
    for _ in range(int(5 * FS / 32768)):
        z = 0.1 * (rng.standard_normal(32768) + 1j * rng.standard_normal(32768))
        d.push(z.astype(np.complex64))
    assert d.acquire_attempts >= 4                 # it kept trying
    assert d.acquired_hz == 0.0 and d._fine_hz == 0.0
    assert d.acquire_peak_db < GsDspParams.acquire_min_db


def test_a_glide_path_that_appears_later_is_still_found():
    """A41: coverage begins after the retune. The first search sees only
    noise; a later retry finds the carrier far off nominal."""
    rng = np.random.default_rng(6)
    d = GsDemod(GsDspParams(fs=FS, offset_hz=OFF, acquire_hz=ACQ))
    for _ in range(int(3 * FS / 32768)):
        z = 0.05 * (rng.standard_normal(32768) + 1j * rng.standard_normal(32768))
        d.push(z.astype(np.complex64))
    assert d.acquired_hz == 0.0
    p = GsParams(fs=FS, offset_hz=OFF, seconds=8.0, snr_db=20.0, ddm=0.0875,
                 drift_hz=20_000.0, level=0.3, block=32768, seed=7)
    obs = []
    for b in generate_blocks(p):
        obs += d.push(b)
    assert d.acquired_hz == pytest.approx(20_000, abs=20)
    late = [o for o in obs if o.t > 3.0 + 4.0 and not o.overrun]
    assert late and np.mean([o.ddm_corrected for o in late]) == pytest.approx(0.0875,
                                                                               abs=0.005)


def test_a_sample_gap_marks_the_window():
    p = GsParams(fs=FS, offset_hz=OFF, seconds=4.0, snr_db=None, ddm=0.05, block=32768)
    d = GsDemod(GsDspParams(fs=FS, offset_hz=OFF, acquire_hz=ACQ))
    obs = []
    for i, b in enumerate(generate_blocks(p)):
        obs += d.push(b, gap=(i == 15))
    assert any(o.overrun for o in obs) and not all(o.overrun for o in obs)


def test_reset_forgets_the_carrier_and_searches_again():
    obs, d = decode({"ddm": 0.05, "drift_hz": 18_000.0, "snr_db": 20.0},
                    {"acquire_hz": ACQ}, seconds=3.0)
    assert d.acquired_hz == pytest.approx(18_000, abs=20)
    d.reset()
    assert d.acquired_hz == 0.0 and d._fine_hz == 0.0 and d.acquire_attempts == 0


def test_state_stays_bounded_over_a_long_run():
    p = GsParams(fs=FS, offset_hz=OFF, seconds=60.0, snr_db=20.0, ddm=0.05, block=32768)
    d = GsDemod(GsDspParams(fs=FS, offset_hz=OFF, acquire_hz=ACQ))
    n = 0
    for b in generate_blocks(p):
        n += len(d.push(b))
        assert d._buf.size < 2 * d.p.window_samples
        assert d._acq_have <= d._acq_n() + 32768
    assert n >= 2 * 58                       # 0.5 s hop: every window produced


def test_params_refuse_a_search_outside_the_sampled_band():
    with pytest.raises(ValueError):
        GsDspParams(fs=FS, offset_hz=OFF, acquire_hz=FS / 2)
