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
      |  down-mix by -offset            (multiply-free when offset = -fs/4, A1)
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
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import signal

__all__ = ["DspParams", "Observation", "PolyDecimator", "VorDemod",
           "INTERMEDIATE_HZ", "TONE_RATE_HZ", "OUTPUT_RATE_HZ"]

INTERMEDIATE_HZ = 32_000     # after the channel filter
TONE_RATE_HZ = 4_000         # where both tone paths are filtered and matched
OUTPUT_RATE_HZ = 1_000       # where the 30 Hz DFT is taken


@dataclass(frozen=True)
class DspParams:
    """All numbers live here. Nothing else in this module hardcodes a constant."""

    fs: int = 1_024_000
    offset_hz: int = -256_000          # tuner offset, amendment A1
    channel_lp_hz: float = 12_000.0
    channel_taps: int = 255
    subcarrier_hz: float = 9_960.0
    sub_lp_hz: float = 700.0           # must clear +/-600 Hz of FM sidebands, A9
    sub_taps: int = 127
    tone_lp_hz: float = 100.0
    tone_taps: int = 127
    tone_hz: float = 30.0
    agc_tau_s: float = 0.5
    integration_s: float = 1.0
    hop_s: float = 1.0
    #: Bins used for the noise floor estimate, Hz. Whole cycles per window.
    noise_bins_hz: tuple[float, ...] = (25.0, 27.0, 33.0, 35.0)

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
    """

    def __init__(self, taps: np.ndarray, decim: int, dtype=np.float32):
        self.taps = np.asarray(taps, dtype=np.float64)[::-1].copy()
        self.decim = int(decim)
        self.dtype = dtype
        self._hist = np.zeros(self.taps.size - 1, dtype=dtype)
        self._phase = 0
        #: group delay in input samples
        self.group_delay = (self.taps.size - 1) / 2.0

    def reset(self) -> None:
        self._hist = np.zeros(self.taps.size - 1, dtype=self.dtype)
        self._phase = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self._hist, x.astype(self.dtype, copy=False)])
        L = self.taps.size
        n_avail = buf.size - L + 1
        if n_avail <= 0:
            self._hist = buf
            return np.zeros(0, dtype=self.dtype)
        starts = np.arange(self._phase, n_avail, self.decim)
        if starts.size:
            wins = sliding_window_view(buf, L)[starts]
            y = wins @ self.taps
            next_start = int(starts[-1]) + self.decim
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

        # Multiply-free down-mix when the offset is exactly -fs/4 (A1).
        self._quarter_rate = (p.offset_hz == -p.fs // 4)
        self._quarter_seq = np.array([1, 1j, -1, -1j], dtype=np.complex64)

    # -- state ------------------------------------------------------------- #

    def reset(self) -> None:
        """Full flush. Called on retune: no sample may survive a channel change."""
        for d in (self.dec_chan, self.dec_a1, self.dec_a2, self.dec_b1, self.dec_b2):
            d.reset()
        self.agc.reset(0.0)
        self._mix_phase = 0
        self._sub_phase = 0
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
        if self._quarter_rate:
            # exp(+j*2*pi*(fs/4)*n/fs) = exp(j*pi*n/2) = [1, j, -1, -j]
            idx = (self._mix_phase + np.arange(n)) & 3
            y = x * self._quarter_seq[idx]
        else:
            k = self._mix_phase + np.arange(n)
            ph = 2 * np.pi * np.mod(-self.p.offset_hz * k / self.p.fs, 1.0)
            y = x * np.exp(1j * ph)
        self._mix_phase += n
        return y.astype(np.complex64, copy=False)

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

        env = np.abs(z).astype(np.float32)
        if not self._primed:
            self.agc.reset(float(env[0]))
            self._primed = True
        carrier = self.agc(env).astype(np.float32)
        np.maximum(carrier, 1e-9, out=carrier)
        audio = ((env - carrier) / carrier).astype(np.float32)

        # -- path A: 30 Hz AM ---------------------------------------------- #
        a = self.dec_a2(self.dec_a1(audio))

        # -- path B: 30 Hz on the 9960 Hz subcarrier ------------------------ #
        k = self._sub_phase + np.arange(audio.size)
        ph = 2 * np.pi * np.mod(self.p.subcarrier_hz * k / INTERMEDIATE_HZ, 1.0)
        self._sub_phase += audio.size
        base = self.dec_b1((audio * np.exp(-1j * ph)).astype(np.complex64))

        if base.size:
            # A cosine of depth m mixed to baseband has amplitude m/2.
            sub_level = 2.0 * np.abs(base).astype(np.float32)
            prev = np.concatenate([[self._fm_prev], base[:-1]])
            self._fm_prev = base[-1]
            # Conjugate-product discriminator: one-sample group delay at
            # TONE_RATE_HZ, folded into phase_offset_deg in M4.
            inst = (np.angle(base * np.conj(prev)).astype(np.float32)
                    * (TONE_RATE_HZ / (2 * np.pi)))
            b = self.dec_b2(inst)
        else:
            sub_level = np.zeros(0, dtype=np.float32)
            b = np.zeros(0, dtype=np.float32)

        # -- align and window ------------------------------------------------ #
        n = min(a.size, b.size)
        if n == 0:
            return []
        self._buf_a = np.concatenate([self._buf_a, a[:n]])
        self._buf_b = np.concatenate([self._buf_b, b[:n]])
        step = self.p.decim_tone * self.p.decim_out
        self._buf_c = np.concatenate([self._buf_c, carrier[::step]])
        self._buf_s = np.concatenate([self._buf_s, sub_level[::self.p.decim_out]])

        return self._emit()

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
            ))
            self._pending_overrun = False
            self._buf_a = self._buf_a[hop:]
            self._buf_b = self._buf_b[hop:]
            self._buf_c = self._buf_c[min(hop, self._buf_c.size):]
            self._buf_s = self._buf_s[min(hop, self._buf_s.size):]
            self._out_n += hop
        return out


def _mean_head(buf: np.ndarray, w: int) -> float:
    if buf.size == 0:
        return 0.0
    return float(np.mean(buf[:w])) if buf.size >= w else float(np.mean(buf))
