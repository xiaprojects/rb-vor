"""VOR demodulation chain.

Reference: rb-vor-architecture.md section 7.2, as corrected by amendments
A7 and A9 (see CLAUDE.md). Implementation plan M3.

Pure DSP: no I/O, no config import, no logging, no clock. Consumes complex64
blocks and yields `Observation` records. The live SDR reader and
`rbvor.record.iq_blocks` must produce indistinguishable input, so that a field
capture replays through exactly this code (doc A section 6).

Written as block functions over ndarrays with all state in plain objects, so
that a later port of the front end to C99 + liquid-dsp stays mechanical and this
module remains the test oracle (decision D1).

Chain
-----
::

    IQ @ fs
      |  down-mix by +offset            (multiply-free when offset = -fs/4, A1)
      |     the carrier sits at -offset in baseband: a tuner parked at
      |     nav + offset puts the navaid at nav - LO = -offset (A22)
      |  channel LPF 12 kHz, decimate   -> 32 kHz complex
      |  envelope |z|, one-pole carrier estimate (tau 0.5 s)
      |  audio = (env - carrier) / carrier        normalised depth
      |
      +-- PATH A (30 Hz AM)
      |     LPF 700 Hz, /8            -> 4 kHz real
      |     TONE LPF 100 Hz, /4       -> 1 kHz real
      |
      +-- PATH B (30 Hz on the 9960 Hz FM subcarrier)
            mix by -9960 Hz
            LPF 700 Hz, /8            -> 4 kHz complex   (subcarrier baseband)
            FM demod: angle(b[n] * conj(b[n-1])) * fs/2pi
            TONE LPF 100 Hz, /4       -> 1 kHz real

    both paths: complex DFT at exactly 30 Hz over a whole number of cycles

AMENDMENT A9 -- chain ordering (2026-07-24, M3)
-----------------------------------------------
Doc A section 7.2 specifies, for path B::

    BPF 9960+/-600 -> mix -> LPF 600 -> /32 -> 1 kHz -> d/dt unwrap(angle) -> LPF 100

That decimates to 1 kHz **before** FM demodulation, and the subcarrier does not
fit: the modulation index is beta = 480/30 = 16, so it occupies roughly +/-600 Hz
(sidebands significant out to order n = 20), against a Nyquist limit of 500 Hz.

The first version of this amendment claimed the ordering "cannot work". That was
wrong and is retracted. At nominal parameters it does work: the folded orders
n = 17..20 carry about 2 % of the power, the FM discriminator tolerates the
truncation, and the resulting bearing error is a **constant** offset that
calibration removes. Measured residual spread across radials was identical for
both orderings (0.159 deg).

The real objection is margin, and it is decisive:

============================  ===========  =============
deviation (ICAO: 480 +/- 30)  A9 THD       doc A THD
============================  ===========  =============
450 Hz  (ratio 15.0)          0.26 %       0.37 %
480 Hz  (ratio 16.0)          0.29 %       0.39 %
510 Hz  (ratio 17.0)          0.32 %       **85.7 %**
525 Hz  (ratio 17.5)          0.34 %       242 %
============================  ===========  =============

ICAO Annex 10 allows a deviation ratio of 16 +/- 1. Doc A's ordering collapses
at ratio 17.0 -- inside the legal tolerance. A station transmitting perfectly
lawfully would produce nonsense. Phase stability under noise is also about six
times worse (std 0.779 deg vs 0.132 deg at 10 dB SNR, six seeds).

Corrected: **demodulate at 4 kHz, decimate to 1 kHz afterwards.** 4 kHz leaves
2 kHz of Nyquist room for a +/-600 Hz signal and degrades gracefully well beyond
the legal band.

Two further changes fall out of the same review:

* The pre-mix band-pass filter is dropped. After mixing by -9960 Hz the image
  lands at -19920 Hz and the post-mix low-pass removes it, so the band-pass was
  redundant -- and every redundant filter is another uncalibrated group delay.
* Both paths now run **the same two filter stages at the same sample rates**
  (LPF 700 @ 32 kHz then /8; TONE LPF 100 @ 4 kHz then /4). Doc A only required
  the final 100 Hz filter to match, which would have left the earlier stages
  contributing an uncancelled differential delay. With this arrangement the
  group delays cancel in the phase difference and the measured residual is a
  constant 1.96 deg across all radials, spread < 0.04 deg -- pinned in M4 as
  ``phase_offset_deg``.

Subcarrier level is reported as the RMS of the complex subcarrier baseband --
a BAND measurement, never the amplitude of a bin at 9960 Hz (amendment A7).

AMENDMENT A22 -- where the carrier is (2026-09-11)
--------------------------------------------------
The first implementation mixed by ``-offset_hz`` and `tools/vor_gen.py` placed
the carrier at ``+offset_hz``; the two agreed with each other and both were
wrong. With ``tuner_offset_hz = -256 kHz`` the tuner is parked at
nav - 256 kHz, and the navaid therefore appears at nav - LO = **+256 kHz** in
the I + jQ baseband that librtlsdr, pyrtlsdr and SoapySDR all deliver. Every
real capture showed the signal on the "wrong" side and needed ``swap_iq``.
Corrected: the mix is by ``+offset_hz`` (bringing ``-offset_hz`` to DC), the
generator places the carrier at ``-offset_hz``, and ``swap_iq`` is once again
what it was meant to be -- a per-dongle override, not a requirement.
`tests/test_dsp.py::test_carrier_where_real_hardware_puts_it_decodes` pins this
without going through the generator.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import as_strided
from scipy import signal
from scipy.special import jv

__all__ = ["DspParams", "Observation", "PolyDecimator", "VorDemod",
           "INTERMEDIATE_HZ", "TONE_RATE_HZ", "OUTPUT_RATE_HZ", "CARRIER_CONVENTION"]

#: Which side of centre the carrier is on, as a string both this module and
#: `tools/vor_gen.py` declare. They must agree (D5); the self-test compares
#: them, because a tree deployed file-by-file can hold one old and one new.
CARRIER_CONVENTION = "-offset"

INTERMEDIATE_HZ = 32_000     # after the channel filter
TONE_RATE_HZ = 4_000         # where both tone paths are filtered and matched
OUTPUT_RATE_HZ = 1_000       # where the 30 Hz DFT is taken


@dataclass(frozen=True)
class DspParams:
    """All numbers live here. Nothing else in this module hardcodes a constant."""

    fs: int = 1_024_000
    offset_hz: int = -256_000          # tuner offset, amendment A1
    #: A25: 13 kHz / 511 taps. The original 12 kHz / 255-tap design was -2.5 dB
    #: at 9960 Hz and -3.3 dB at the upper subcarrier sideband -- rolling off
    #: inside the band it was meant to pass, asymmetrically.
    channel_lp_hz: float = 13_000.0
    channel_taps: int = 511
    subcarrier_hz: float = 9_960.0
    sub_lp_hz: float = 700.0           # must clear +/-600 Hz of FM sidebands, A9
    sub_taps: int = 255                # A25: 127 taps were -1.9 dB at 540 Hz
    tone_lp_hz: float = 100.0
    tone_taps: int = 127
    tone_hz: float = 30.0
    agc_tau_s: float = 0.5
    integration_s: float = 1.0
    hop_s: float = 1.0
    #: Bins used for the noise floor estimate, Hz. Whole cycles per window.
    noise_bins_hz: tuple[float, ...] = (25.0, 27.0, 33.0, 35.0)
    #: AFC (A25). The residual carrier frequency is estimated on the 32 kHz
    #: baseband every block and smoothed over `afc_tau_s`; once it exceeds
    #: `afc_threshold_hz` the down-mix is corrected by that amount, so the
    #: subcarrier sidebands stay in the flat part of the channel filter. Below
    #: the threshold nothing is touched and the chain is bit-identical to a
    #: build without AFC. 0 disables.
    afc_threshold_hz: float = 300.0
    afc_tau_s: float = 1.0
    #: D7: how the 30 Hz reference is recovered from the FM subcarrier.
    #: "discriminator" -- conjugate-product FM demodulator, then the 30 Hz DFT
    #:   (doc A section 7.2 as corrected by A9). Collapses below ~10 dB
    #:   subcarrier SNR: the classic FM threshold.
    #: "harmonic" -- maximum-likelihood phase of the known 30 Hz modulation,
    #:   C(phi) = |sum b(t) exp(-j beta sin(w t + phi))|, evaluated through
    #:   its Jacobi-Anger expansion: Bessel-weighted DFT bins of the subcarrier
    #:   baseband at multiples of 30 Hz. No demodulation, no threshold. Both
    #:   paths then share exactly the same filters, so their delays cancel.
    fm_estimator: str = "discriminator"
    fm_beta: float = 16.0               # nominal deviation ratio 480 / 30
    sub_search_hz: float = 100.0        # ICAO: subcarrier 9960 Hz +/- 1 %

    def __post_init__(self) -> None:
        if self.fs % INTERMEDIATE_HZ:
            raise ValueError(
                f"fs={self.fs} must be an integer multiple of {INTERMEDIATE_HZ}"
            )
        if self.subcarrier_hz + self.sub_lp_hz > INTERMEDIATE_HZ / 2:
            raise ValueError("subcarrier does not fit in the intermediate rate")
        # A9: the subcarrier baseband must fit inside the Nyquist limit of the
        # rate at which it is DEMODULATED, not the rate it ends up at.
        if self.sub_lp_hz >= TONE_RATE_HZ / 2:
            raise ValueError(
                f"sub_lp_hz={self.sub_lp_hz} exceeds Nyquist at the "
                f"{TONE_RATE_HZ} Hz demodulation rate (amendment A9)"
            )
        cycles = self.integration_s * self.tone_hz
        if abs(round(cycles) - cycles) > 1e-9:
            raise ValueError(
                f"integration_s={self.integration_s} is not a whole number of "
                f"{self.tone_hz} Hz cycles -- spectral leakage would bias the phase"
            )
        if self.afc_threshold_hz < 0 or self.afc_tau_s <= 0:
            raise ValueError("afc_threshold_hz must be >= 0 and afc_tau_s > 0")
        if self.fm_estimator not in ("discriminator", "harmonic"):
            raise ValueError(f"fm_estimator must be 'discriminator' or 'harmonic', "
                             f"got {self.fm_estimator!r}")
        if not 8.0 <= self.fm_beta <= 24.0 or self.sub_search_hz < 0:
            raise ValueError("fm_beta must be within 8..24 and sub_search_hz >= 0")

    @property
    def decim_channel(self) -> int:
        return self.fs // INTERMEDIATE_HZ

    @property
    def decim_tone(self) -> int:
        return INTERMEDIATE_HZ // TONE_RATE_HZ

    @property
    def decim_out(self) -> int:
        return TONE_RATE_HZ // OUTPUT_RATE_HZ

    @property
    def window_samples(self) -> int:
        return int(round(self.integration_s * OUTPUT_RATE_HZ))

    @property
    def hop_samples(self) -> int:
        return int(round(self.hop_s * OUTPUT_RATE_HZ))


@dataclass
class Observation:
    """One integration window. Bearing and quality are computed in M4."""

    t: float                 # seconds from stream start, centre of the window
    phi_a: float             # phase of the 30 Hz AM tone, radians
    phi_b: float             # phase of the 30 Hz tone recovered from the subcarrier
    amp_a: float             # amplitude as a fraction of carrier (nominal 0.30)
    amp_b: float             # recovered reference tone amplitude, Hz deviation
    carrier: float           # smoothed |z|, arbitrary units
    sub_level: float         # subcarrier depth, BAND measurement (A7), nominal 0.30
    noise_est: float         # mean amplitude in the off-tone bins, units of amp_a
    overrun: bool = False    # window touched a sample gap; discard it (F9)
    #: A25: where the carrier actually is relative to the nominal down-mix, Hz
    #: (correction already applied + residual still measured). The tuner's ppm
    #: error and the station's own offset, together; ppm = this / nav_hz * 1e6.
    carrier_offset_hz: float = 0.0
    #: D7 (harmonic estimator only, else 0): the station's subcarrier offset
    #: from 9960 Hz, and the COHERENT subcarrier amplitude -- the correlation
    #: peak, which unlike `sub_level` (band power, noise included) is near zero
    #: when there is no subcarrier.
    sub_offset_hz: float = 0.0
    sub_coh: float = 0.0

    @property
    def delta_phi(self) -> float:
        """phi_a - phi_b wrapped to (-pi, pi]. The radial, up to sign and offset."""
        return float((self.phi_a - self.phi_b + np.pi) % (2 * np.pi) - np.pi)

    @property
    def snr_db(self) -> float:
        return float(20 * np.log10(max(self.amp_a, 1e-12) / max(self.noise_est, 1e-12)))


class PolyDecimator:
    """Streaming polyphase FIR decimator.

    Only the retained output samples are computed, so cost scales with the
    output rate rather than the input rate. Decimation phase is carried across
    blocks, so the result does not depend on how the input is chopped up -- a
    property the live path relies on and `test_dsp.py` asserts directly.

    Form (D6). With ``D`` the decimation factor and the reversed taps zero-padded
    to ``K*D``, the input from the current phase is viewed as rows of ``D``
    samples, ``B = buf[phase:].reshape(-1, D)``, and each output is a sum of
    ``K`` dot products over consecutive rows::

        y[i] = sum_k  B[i + k] . H[k]          H = taps_rev.reshape(K, D)

    All of them come out of ONE matrix product, ``P = B @ H.T`` of shape
    ``(M + K - 1, K)``, after which ``y[i]`` is the sum of the diagonal
    ``P[i, 0], P[i+1, 1], ..., P[i+K-1, K-1]`` -- read through a strided view,
    no copy. So the whole filter is one BLAS call plus one reduction,
    independent of ``K``.

    The previous form built an ``(M, L)`` sliding-window copy of the input (an
    8x expansion for the channel filter) and, because the taps were float64,
    promoted the whole thing to complex128 before the product. Measured per
    call: channel filter 1420 -> 97 us, the 32-phase tone filters 62 -> 19 us;
    output identical to float32 rounding (max 1.2e-7). The taps are held in the
    working precision -- the output was always cast to it anyway.
    """

    def __init__(self, taps: np.ndarray, decim: int, dtype=np.float32):
        self.taps = np.asarray(taps, dtype=np.float64)[::-1].copy()
        self.decim = int(decim)
        self.dtype = dtype
        self._hist = np.zeros(self.taps.size - 1, dtype=dtype)
        self._phase = 0
        #: group delay in input samples
        self.group_delay = (self.taps.size - 1) / 2.0
        # polyphase matrix in the working precision
        self._K = -(-self.taps.size // self.decim)
        padded = np.zeros(self._K * self.decim, dtype=np.float64)
        padded[:self.taps.size] = self.taps
        real_dtype = np.float32 if dtype == np.float32 else np.float64
        work = np.complex64 if dtype == np.complex64 else (
            np.complex128 if dtype == np.complex128 else real_dtype)
        #: (D, K): column k holds phase k of the reversed, padded taps
        self._HT = np.ascontiguousarray(padded.reshape(self._K, self.decim).T.astype(work))

    def reset(self) -> None:
        self._hist = np.zeros(self.taps.size - 1, dtype=self.dtype)
        self._phase = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self._hist, x.astype(self.dtype, copy=False)])
        L = self.taps.size
        D = self.decim
        n_avail = buf.size - L + 1
        if n_avail <= 0:
            self._hist = buf
            return np.zeros(0, dtype=self.dtype)
        # outputs at phase, phase + D, ... below n_avail
        m = (n_avail - 1 - self._phase) // D + 1 if self._phase < n_avail else 0
        if m > 0:
            need = self._phase + (m + self._K - 1) * D
            if need > buf.size:
                # taps padded past a multiple of D reach at most D-1 samples
                # beyond the last real one; those coefficients are zero
                src = np.concatenate([buf, np.zeros(need - buf.size, dtype=self.dtype)])
            else:
                src = buf
            rows = src[self._phase:need].reshape(m + self._K - 1, D)
            partial = rows @ self._HT                        # (m + K - 1, K)
            it = partial.itemsize
            diagonals = as_strided(partial, shape=(m, self._K),
                                   strides=(self._K * it, (self._K + 1) * it))
            y = diagonals.sum(axis=1)
            next_start = self._phase + m * D
        else:
            y = np.zeros(0, dtype=self.dtype)
            next_start = self._phase
        self._phase = next_start - n_avail
        self._hist = buf[n_avail:].copy()
        return y.astype(self.dtype, copy=False)


class _OnePole:
    """One-pole low-pass, used for the carrier / AGC estimate."""

    def __init__(self, tau_s: float, fs: float, init: float = 0.0):
        self.alpha = float(np.exp(-1.0 / (tau_s * fs)))
        self.y = float(init)

    def reset(self, init: float = 0.0) -> None:
        self.y = float(init)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        b = [1.0 - self.alpha]
        a = [1.0, -self.alpha]
        out, _ = signal.lfilter(b, a, x, zi=[self.alpha * self.y])
        if out.size:
            self.y = float(out[-1])
        return out


class VorDemod:
    """Block-processing VOR demodulator.

    Usage::

        d = VorDemod(DspParams(fs=1_024_000))
        for blk in iq_blocks(path):
            for obs in d.push(blk):
                ...
    """

    def __init__(self, p: DspParams):
        self.p = p
        self._build()
        self.reset()

    # -- construction ------------------------------------------------------ #

    def _build(self) -> None:
        p = self.p
        chan = signal.firwin(p.channel_taps, p.channel_lp_hz, fs=p.fs)
        # One filter DESIGN, instantiated twice: paths A and B must share
        # coefficients so their group delays cancel in the difference (A9).
        self._sub_taps = signal.firwin(p.sub_taps, p.sub_lp_hz, fs=INTERMEDIATE_HZ)
        self._tone_taps = signal.firwin(p.tone_taps, p.tone_lp_hz, fs=TONE_RATE_HZ)

        self.dec_chan = PolyDecimator(chan, p.decim_channel, np.complex64)
        self.dec_a1 = PolyDecimator(self._sub_taps, p.decim_tone, np.float32)
        self.dec_a2 = PolyDecimator(self._tone_taps, p.decim_out, np.float32)
        self.dec_b1 = PolyDecimator(self._sub_taps, p.decim_tone, np.complex64)
        self.dec_b2 = PolyDecimator(self._tone_taps, p.decim_out, np.float32)

        self.agc = _OnePole(p.agc_tau_s, INTERMEDIATE_HZ)

        n = p.window_samples
        k = np.arange(n)
        self._dft_tone = np.exp(-2j * np.pi * p.tone_hz * k / OUTPUT_RATE_HZ)
        self._dft_noise = [
            np.exp(-2j * np.pi * f * k / OUTPUT_RATE_HZ) for f in p.noise_bins_hz
        ]

        # Multiply-free down-mix when the offset is exactly -fs/4 (A1). The
        # carrier sits at -offset = +fs/4 (A22), so the mix is exp(-j*pi*n/2).
        self._quarter_rate = (p.offset_hz == -p.fs // 4)
        self._quarter_seq = np.array([1, -1j, -1, 1j], dtype=np.complex64)
        self._quarter_seqs = [np.roll(self._quarter_seq, -k) for k in range(4)]

        # D7: harmonic estimator tables, at TONE_RATE_HZ
        self._harm = p.fm_estimator == "harmonic"
        if self._harm:
            w4 = self._w4
            k4 = np.arange(w4)
            self._dft_tone4 = np.exp(-2j * np.pi * p.tone_hz * k4 / TONE_RATE_HZ)
            self._dft_noise4 = [np.exp(-2j * np.pi * f * k4 / TONE_RATE_HZ)
                                for f in p.noise_bins_hz]
            self._hk = np.arange(-_HARMONICS, _HARMONICS + 1)
            self._kbin = int(round(p.tone_hz * w4 / TONE_RATE_HZ))   # 30 Hz bin, integer
            self._betas = p.fm_beta + np.array([-1.0, -0.5, 0.0, 0.5, 1.0])
            self._jk = {b: jv(self._hk, b) for b in self._betas}
            # coarse search grid: one FFT bin per step, so the scan is a gather
            self._df_step = TONE_RATE_HZ / w4
            nstep = int(p.sub_search_hz / self._df_step)
            self._df_grid = np.arange(-nstep, nstep + 1) * self._df_step

    @property
    def _w4(self) -> int:
        return self.p.window_samples * self.p.decim_out

    # -- state ------------------------------------------------------------- #

    def reset(self) -> None:
        """Full flush. Called on retune: no sample may survive a channel change."""
        for d in (self.dec_chan, self.dec_a1, self.dec_a2, self.dec_b1, self.dec_b2):
            d.reset()
        self.agc.reset(0.0)
        self._mix_phase = 0
        self._sub_phase = 0
        self._fine_hz = 0.0            # AFC correction applied in the down-mix
        self._fine_phase = 0.0         # cycles, carried across blocks
        self._afc_est = 0.0            # smoothed residual, Hz
        self._afc_seconds = 0.0        # signal seen since the last correction
        # D7: subcarrier offset acquisition and tracking
        self._sub_df = 0.0             # tracked subcarrier offset, Hz
        self._df_locked = False
        self._df_acc = None            # accumulated coarse scan during acquisition
        self._df_windows = 0
        self._unlock_count = 0
        self._fm_prev = np.complex64(0)
        self._buf_a = np.zeros(0, dtype=np.float32)
        self._buf_b = np.zeros(0, dtype=np.float32)
        self._buf_c = np.zeros(0, dtype=np.float32)
        self._buf_s = np.zeros(0, dtype=np.float32)
        self._out_n = 0
        self._pending_overrun = False
        self._primed = False

    def mark_gap(self) -> None:
        """Declare a discontinuity in the input (sample overrun, F9).

        The window in progress is poisoned and reported with ``overrun=True`` so
        the caller discards it rather than decoding across a phase break.
        """
        self._pending_overrun = True

    # -- processing -------------------------------------------------------- #

    def _downmix(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        # The carrier is at -offset_hz in baseband (A22); multiplying by
        # exp(+j*2*pi*offset_hz*t) shifts it to DC.
        if self._quarter_rate:
            # exp(-j*2*pi*(fs/4)*n/fs) = exp(-j*pi*n/2) = [1, -j, -1, j]
            if n % 4 == 0:
                # whole periods: broadcast the 4-sequence over rows of 4,
                # no per-sample index gather (D6)
                y = (x.reshape(-1, 4) * self._quarter_seqs[self._mix_phase & 3]).reshape(-1)
            else:
                idx = (self._mix_phase + np.arange(n)) & 3
                y = x * self._quarter_seq[idx]
        else:
            k = self._mix_phase + np.arange(n)
            ph = 2 * np.pi * np.mod(self.p.offset_hz * k / self.p.fs, 1.0)
            y = x * np.exp(1j * ph)
        self._mix_phase += n
        if self._fine_hz:
            # AFC correction (A25): bring the carrier from +fine_hz to DC. Only
            # ever runs once a correction has been decided, so the common case
            # costs nothing.
            y = y * self._phasor(n, -self._fine_hz / self.p.fs)
        return y.astype(np.complex64, copy=False)

    def _phasor(self, n: int, cycles_per_sample: float) -> np.ndarray:
        """exp(2j*pi*(phase0 + f*k)) for k < n, without n complex exponentials.

        Built as the outer product of a 256-sample base sequence and a
        per-row rotation, so the cost is one complex multiply per sample plus
        512 exponentials. Phase continues across blocks.
        """
        a = 256
        rows = -(-n // a)
        base = np.exp(2j * np.pi * (self._fine_phase + cycles_per_sample * np.arange(a)))
        rot = np.exp(2j * np.pi * cycles_per_sample * a * np.arange(rows))
        ph = (rot[:, None] * base[None, :]).reshape(-1)[:n]
        self._fine_phase = (self._fine_phase + cycles_per_sample * n) % 1.0
        return ph.astype(np.complex64, copy=False)

    def _afc(self, z: np.ndarray) -> None:
        """Estimate the residual carrier frequency of the 32 kHz baseband and
        decide whether to move the down-mix (A25).

        The mean phase advance per sample, angle(sum(conj(z[n-1]) z[n])), is the
        power-weighted mean frequency. For a VOR that is the carrier: the 30 Hz
        and 9960 Hz sidebands are symmetric about it, and so is the ident.
        In-band noise is symmetric about *zero* instead, so at low SNR the
        estimate is pulled toward zero by the noise fraction -- which only means
        a large offset is taken up in more than one step. A correction is made
        when the smoothed estimate has exceeded the threshold for at least one
        time constant, so a noise-only input (estimate ~0) never triggers one.
        """
        if self.p.afc_threshold_hz <= 0 or z.size < 2:
            return
        dt = z.size / INTERMEDIATE_HZ
        f_res = float(np.angle(np.vdot(z[:-1], z[1:]))) * INTERMEDIATE_HZ / (2 * np.pi)
        alpha = float(np.exp(-dt / self.p.afc_tau_s))
        self._afc_est = alpha * self._afc_est + (1.0 - alpha) * f_res
        self._afc_seconds += dt
        if (abs(self._afc_est) > self.p.afc_threshold_hz
                and self._afc_seconds >= self.p.afc_tau_s):
            self._fine_hz += self._afc_est
            self._afc_est = 0.0
            self._afc_seconds = 0.0

    @property
    def carrier_offset_hz(self) -> float:
        return self._fine_hz + self._afc_est

    def push(self, iq: np.ndarray, gap: bool = False) -> list[Observation]:
        """Consume a block of complex64 IQ. Returns zero or more observations."""
        if gap:
            self.mark_gap()
        iq = np.asarray(iq, dtype=np.complex64)
        if iq.size == 0:
            return []

        z = self.dec_chan(self._downmix(iq))          # -> 32 kHz complex
        if z.size == 0:
            return []
        self._afc(z)

        env = np.abs(z).astype(np.float32)
        if not self._primed:
            self.agc.reset(float(env[0]))
            self._primed = True
        carrier = self.agc(env).astype(np.float32)
        np.maximum(carrier, 1e-9, out=carrier)
        audio = ((env - carrier) / carrier).astype(np.float32)

        # -- path A: 30 Hz AM ---------------------------------------------- #
        a1 = self.dec_a1(audio)
        a = a1 if self._harm else self.dec_a2(a1)      # D7: no tone LPF in harmonic mode

        # -- path B: 30 Hz on the 9960 Hz subcarrier ------------------------ #
        k = self._sub_phase + np.arange(audio.size)
        ph = 2 * np.pi * np.mod(self.p.subcarrier_hz * k / INTERMEDIATE_HZ, 1.0)
        self._sub_phase += audio.size
        base = self.dec_b1((audio * np.exp(-1j * ph)).astype(np.complex64))

        if base.size:
            # A cosine of depth m mixed to baseband has amplitude m/2.
            sub_level = 2.0 * np.abs(base).astype(np.float32)
            if self._harm:
                b = base                                # D7: the baseband itself
            else:
                prev = np.concatenate([[self._fm_prev], base[:-1]])
                self._fm_prev = base[-1]
                # Conjugate-product discriminator: one-sample group delay at
                # TONE_RATE_HZ, folded into phase_offset_deg in M4.
                inst = (np.angle(base * np.conj(prev)).astype(np.float32)
                        * (TONE_RATE_HZ / (2 * np.pi)))
                b = self.dec_b2(inst)
        else:
            sub_level = np.zeros(0, dtype=np.float32)
            b = np.zeros(0, dtype=np.complex64 if self._harm else np.float32)

        # -- align and window ------------------------------------------------ #
        n = min(a.size, b.size)
        if n == 0:
            return []
        self._buf_a = np.concatenate([self._buf_a, a[:n]])
        self._buf_b = np.concatenate([self._buf_b, b[:n]])
        step = self.p.decim_tone * self.p.decim_out
        self._buf_c = np.concatenate([self._buf_c, carrier[::step]])
        self._buf_s = np.concatenate([self._buf_s, sub_level[::self.p.decim_out]])

        return self._emit_harmonic() if self._harm else self._emit()

    def _emit(self) -> list[Observation]:
        p = self.p
        w, hop = p.window_samples, p.hop_samples
        out: list[Observation] = []
        while self._buf_a.size >= w and self._buf_b.size >= w:
            seg_a = self._buf_a[:w]
            seg_b = self._buf_b[:w]
            ca = (2.0 / w) * np.dot(seg_a, self._dft_tone)
            cb = (2.0 / w) * np.dot(seg_b, self._dft_tone)
            noise = float(np.mean([
                abs((2.0 / w) * np.dot(seg_a, e)) for e in self._dft_noise
            ]))
            out.append(Observation(
                t=float((self._out_n + w / 2.0) / OUTPUT_RATE_HZ),
                phi_a=float(np.angle(ca)), amp_a=float(np.abs(ca)),
                phi_b=float(np.angle(cb)), amp_b=float(np.abs(cb)),
                carrier=_mean_head(self._buf_c, w),
                sub_level=_mean_head(self._buf_s, w),
                noise_est=noise,
                overrun=self._pending_overrun,
                carrier_offset_hz=self.carrier_offset_hz,
            ))
            self._pending_overrun = False
            self._buf_a = self._buf_a[hop:]
            self._buf_b = self._buf_b[hop:]
            self._buf_c = self._buf_c[min(hop, self._buf_c.size):]
            self._buf_s = self._buf_s[min(hop, self._buf_s.size):]
            self._out_n += hop
        return out


    # -- D7: harmonic estimator ----------------------------------------------- #

    @staticmethod
    def _derotate(bw: np.ndarray, df_hz: float) -> np.ndarray:
        if not df_hz:
            return bw
        return bw * np.exp(-2j * np.pi * df_hz * np.arange(bw.size) / TONE_RATE_HZ)

    def _sweep(self, X: np.ndarray, shift_bins: int, beta: float):
        """|C(phi)| over the phi grid, from the FFT `X` of a (de-rotated) window.

        C(phi) = sum_k J_k(beta) B_k exp(-j k phi), with B_k = X at k * 30 Hz.
        A subcarrier offset of an integer number of bins is a pure index shift,
        which is what makes the coarse scan one FFT per window. Returns the
        magnitude grid and the coefficients, for `_corr_at`.
        """
        n = X.size
        kbin = int(round(self.p.tone_hz * n / TONE_RATE_HZ))
        ck = self._jk[beta] * X[(self._hk * kbin + shift_bins) % n]
        coeffs = np.zeros(_PHI_GRID, dtype=np.complex128)
        coeffs[self._hk % _PHI_GRID] = ck
        return np.abs(np.fft.fft(coeffs)), ck

    def _peak(self, X: np.ndarray):
        """(phi, sub_coh, beta, ck): best phase over the beta grid, refined by
        a parabola through the three grid points around the maximum."""
        best = None
        for beta in self._betas:
            mag, ck = self._sweep(X, 0, beta)
            m = int(np.argmax(mag))
            if best is None or mag[m] > best[0]:
                best = (mag[m], m, mag, ck, beta)
        peak, m, mag, ck, beta = best
        y0, y1, y2 = mag[(m - 1) % _PHI_GRID], mag[m], mag[(m + 1) % _PHI_GRID]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den else 0.0
        phi = 2 * np.pi * ((m + frac) % _PHI_GRID) / _PHI_GRID
        # the peak is N * amplitude; a depth-m subcarrier has amplitude m/2 at
        # baseband, so report 2 * amplitude, comparable with sub_level
        return float(phi), float(2.0 * y1 / X.size), float(beta), ck

    def _corr_at(self, ck: np.ndarray, phi: float) -> complex:
        return complex(np.sum(ck * np.exp(-1j * self._hk * phi)))

    def _refine_df(self, bwd: np.ndarray, phi: float, beta: float) -> float:
        """Residual offset, Hz, from the correlation phase slope between the two
        halves of an already de-rotated window, both evaluated at the same
        phi. The halves are whole numbers of 30 Hz cycles, so phi means the
        same thing in both. Unambiguous to +/- (fs / h) / 2, far beyond the
        0.5 Hz the coarse grid leaves."""
        h = bwd.size // 2
        _, ck1 = self._sweep(np.fft.fft(bwd[:h]), 0, beta)
        _, ck2 = self._sweep(np.fft.fft(bwd[h:]), 0, beta)
        dtheta = float(np.angle(self._corr_at(ck2, phi) * np.conj(self._corr_at(ck1, phi))))
        return dtheta / (2 * np.pi) / (h / TONE_RATE_HZ)

    def _acquire_df(self, bw: np.ndarray) -> None:
        """Accumulate the coarse offset scan over a few windows, then commit.

        A single weak window can lock onto a harmonic alias (an offset of
        +/- 30 Hz still correlates through the neighbouring Bessel orders);
        summing the scan over `_ACQ_WINDOWS` windows before choosing is what
        makes acquisition robust at low SNR.
        """
        X = np.fft.fft(bw)
        scan = np.empty(self._df_grid.size)
        for i, df in enumerate(self._df_grid):
            scan[i] = self._sweep(X, int(round(df / self._df_step)), self.p.fm_beta)[0].max()
        self._df_acc = scan if self._df_acc is None else self._df_acc + scan
        self._df_windows += 1
        self._sub_df = float(self._df_grid[int(np.argmax(self._df_acc))])
        if self._df_windows >= _ACQ_WINDOWS:
            self._df_locked = True
            self._df_acc = None
            self._unlock_count = 0

    def _emit_harmonic(self) -> list[Observation]:
        p = self.p
        w, hop = p.window_samples, p.hop_samples          # at 1 kHz, for c/s buffers
        w4, hop4 = self._w4, hop * p.decim_out
        out: list[Observation] = []
        while self._buf_a.size >= w4 and self._buf_b.size >= w4:
            seg_a = self._buf_a[:w4]
            seg_b = self._buf_b[:w4]
            ca = (2.0 / w4) * np.dot(seg_a, self._dft_tone4)
            noise = float(np.mean([
                abs((2.0 / w4) * np.dot(seg_a, e)) for e in self._dft_noise4
            ]))

            if not self._df_locked:
                self._acquire_df(seg_b)
            df = self._sub_df
            bwd = self._derotate(seg_b, df)
            phi_b, sub_coh, beta, ck = self._peak(np.fft.fft(bwd))
            if self._df_locked:
                resid = self._refine_df(bwd, phi_b, beta)
                if abs(resid) > _RELOCK_HZ:
                    self._unlock_count += 1                # do not follow a jump
                    if self._unlock_count >= _RELOCK_WINDOWS:
                        self._df_locked = False            # re-acquire from scratch
                        self._df_windows = 0
                        self._df_acc = None
                else:
                    self._unlock_count = 0
                    df = df + resid
                    self._sub_df = 0.8 * self._sub_df + 0.2 * df
                    # re-estimate with the residual removed: a theta ramp
                    # across the window costs correlation and can bias phi
                    bwd = self._derotate(seg_b, df)
                    phi_b, sub_coh, beta, ck = self._peak(np.fft.fft(bwd))

            out.append(Observation(
                t=float((self._out_n + w / 2.0) / OUTPUT_RATE_HZ),
                phi_a=float(np.angle(ca)), amp_a=float(np.abs(ca)),
                phi_b=phi_b, amp_b=float(beta * p.tone_hz),   # deviation, Hz
                carrier=_mean_head(self._buf_c, w),
                sub_level=_mean_head(self._buf_s, w),
                noise_est=noise,
                overrun=self._pending_overrun,
                carrier_offset_hz=self.carrier_offset_hz,
                sub_offset_hz=float(df),
                sub_coh=sub_coh,
            ))
            self._pending_overrun = False
            self._buf_a = self._buf_a[hop4:]
            self._buf_b = self._buf_b[hop4:]
            self._buf_c = self._buf_c[min(hop, self._buf_c.size):]
            self._buf_s = self._buf_s[min(hop, self._buf_s.size):]
            self._out_n += hop
        return out


#: D7 constants. Orders beyond ~24 carry no energy at beta = 16 (+/- 1);
#: 3600 grid points give 0.1 deg before the parabolic refinement; three
#: windows of accumulated scan acquire the subcarrier offset reliably down to
#: the noise floor the AM path itself gives up at.
_HARMONICS = 26
_PHI_GRID = 3600
_ACQ_WINDOWS = 3
_RELOCK_HZ = 2.0
_RELOCK_WINDOWS = 4


def _mean_head(buf: np.ndarray, w: int) -> float:
    if buf.size == 0:
        return 0.0
    return float(np.mean(buf[:w])) if buf.size >= w else float(np.mean(buf))
