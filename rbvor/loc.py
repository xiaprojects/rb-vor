"""ILS localizer demodulator and quality estimator.

Reference: docs/loc-plan.md sections 4-6; CLAUDE.md A33-A37, D9.

Pure DSP, like `rbvor.dsp`: no I/O, no config import, no logging, no clock.
Consumes complex64 blocks and yields `LocObservation` records. Block functions
over ndarrays, state in plain attributes, parameters in a frozen dataclass
(decision D1), so the port stays mechanical.

Chain
-----
::

    IQ @ fs
      |  down-mix, channel LPF 13 kHz / 511 taps, decimate -> 32 kHz complex
      |  AFC (A25), envelope |z|, one-pole carrier estimate (tau 0.5 s)
      |  audio = (env - carrier) / carrier        normalised depth
      |  LPF 700 Hz / 255 taps, /8                -> 4 kHz real
      |
      +-- window of integration_s, Hann, hop hop_s
            tone search: the eps maximising |X(90(1+eps))|^2 + |X(150(1+eps))|^2
            m90, m150 read at the found frequencies; noise from off-tone bins

The front end is the VOR front end, *composed a second time* from the same
pieces rather than extracted out of `VorDemod`: `PolyDecimator`, `_OnePole`,
the same filter designs, and `VorDemod`'s own down-mix / AFC methods bound to
this class. `VorDemod` is not touched, so the VOR path is bit-identical by
construction (and the real-capture fingerprint proves it, CLAUDE.md A33).

Why a search and not a bin (A34)
--------------------------------
Annex 10 Vol I 3.1.3.5.3 allows the tones +/-2.5 % (Cat I). The two tones are
phase-locked (3.1.3.5.3.3), so ONE fractional offset `eps` moves both. A DFT
fixed at 90 and 150 Hz loses a different fraction of each tone off-nominal,
and the DDM is wrong on a lawful station -- A7/A9 again. The search costs two
dot products per grid point per window.

Sign
----
``ddm = m150 - m90``, positive when 150 Hz predominates: right of the front
course as seen on approach, so **fly left** (Annex 10 Vol I 3.1.3.1.2). A back
course reverses this and the receiver cannot know.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal

from rbvor.dsp import (
    INTERMEDIATE_HZ,
    TONE_RATE_HZ,
    DspParams,
    PolyDecimator,
    VorDemod,
    _OnePole,
)

__all__ = ["LocDspParams", "LocObservation", "LocDemod", "LocQualityParams",
           "LocQualityResult", "LocQuality", "TONE_90_HZ", "TONE_150_HZ",
           "SDM_NOMINAL", "DDM_FULL_SCALE", "ddm_to_dev_pct"]

TONE_90_HZ = 90.0
TONE_150_HZ = 150.0
#: Annex 10 Vol I 3.1.3.5.1: 20 % per tone on the course line.
SDM_NOMINAL = 0.40
#: Course sector half-width (Annex 10 Vol I 3.1.1). Used as indicator full
#: scale by airborne convention; that convention is not in Annex 10 Vol I.
DDM_FULL_SCALE = 0.155


def ddm_to_dev_pct(ddm: float) -> int:
    """`locdev`: DDM as a percentage of full scale, clamped to +/-100.

    Rounded half away from zero with floor(x + 0.5) on the magnitude, so the
    two sides of the course quantise symmetrically (banker's rounding would
    not, A12).
    """
    x = 100.0 * ddm / DDM_FULL_SCALE
    q = int(np.floor(abs(x) + 0.5))
    return int(np.sign(x)) * min(q, 100)


@dataclass(frozen=True)
class LocDspParams:
    """All numbers of the localizer chain. The front-end ones are taken from
    the VOR `DspParams` (`from_dsp`), so both modes see the same front end."""

    fs: int = 1_024_000
    offset_hz: int = -256_000
    channel_lp_hz: float = 13_000.0
    channel_taps: int = 511
    env_lp_hz: float = 700.0            # same design as dsp.sub_lp_hz
    env_taps: int = 255                 # same design as dsp.sub_taps
    agc_tau_s: float = 0.5
    afc_threshold_hz: float = 300.0
    afc_tau_s: float = 1.0
    #: Window and hop. Must be whole 1/30 s so both tones fit whole cycles at
    #: eps = 0. Chosen from the noise-against-lag measurement (A36).
    integration_s: float = 1.0
    hop_s: float = 0.5
    #: +/- fractional range of the tone search. Cat I allows 0.025 (A34).
    tone_search: float = 0.03
    tone_step: float = 0.0025
    #: "search" (A34, default) or "band" (band power minus floor), kept for
    #: the comparison in tools/loc_sweep.py.
    estimator: str = "search"
    #: Off-tone bins for the noise floor, Hz. Clear of the tone search ranges
    #: (87.3-92.7, 145.5-154.5 Hz), of multiples of 30 Hz (where every
    #: intermodulation product of 90 and 150 Hz lands) and of 50 Hz mains
    #: harmonics.
    noise_bins_hz: tuple[float, ...] = (105.0, 115.0, 125.0, 135.0, 165.0, 195.0)

    def __post_init__(self) -> None:
        if self.fs % INTERMEDIATE_HZ:
            raise ValueError(f"fs={self.fs} must be a multiple of {INTERMEDIATE_HZ}")
        for name, v in (("integration_s", self.integration_s), ("hop_s", self.hop_s)):
            cycles = v * 30.0
            if v <= 0 or abs(round(cycles) - cycles) > 1e-9:
                raise ValueError(f"loc {name}={v} must be a positive whole number "
                                 "of 1/30 s, so both tones fit whole cycles")
        if self.hop_s > self.integration_s:
            raise ValueError("loc hop_s must not exceed integration_s")
        if not 0 < self.tone_search <= 0.05 or not 0 < self.tone_step <= self.tone_search:
            raise ValueError("tone_search must be in (0, 0.05] and tone_step in (0, tone_search]")
        if self.estimator not in ("search", "band"):
            raise ValueError(f"loc estimator must be 'search' or 'band', got {self.estimator!r}")
        if self.env_lp_hz >= TONE_RATE_HZ / 2:
            raise ValueError("env_lp_hz must stay below the 4 kHz Nyquist (A9)")
        lo = TONE_90_HZ * (1 - self.tone_search) - 4 / self.integration_s
        hi = TONE_150_HZ * (1 + self.tone_search) + 4 / self.integration_s
        for f in self.noise_bins_hz:
            if any(abs(f - c * (1 + s * self.tone_search)) < 4 / self.integration_s
                   for c in (TONE_90_HZ, TONE_150_HZ) for s in (-1, 0, 1)):
                raise ValueError(f"noise bin {f} Hz overlaps a tone search range")
            if not 0 < f < self.env_lp_hz:
                raise ValueError(f"noise bin {f} Hz outside the envelope band")
        del lo, hi

    @classmethod
    def from_dsp(cls, d: DspParams, **loc) -> LocDspParams:
        return cls(fs=d.fs, offset_hz=d.offset_hz, channel_lp_hz=d.channel_lp_hz,
                   channel_taps=d.channel_taps, env_lp_hz=d.sub_lp_hz,
                   env_taps=d.sub_taps, agc_tau_s=d.agc_tau_s,
                   afc_threshold_hz=d.afc_threshold_hz, afc_tau_s=d.afc_tau_s,
                   **loc)

    @property
    def window_samples(self) -> int:
        return int(round(self.integration_s * TONE_RATE_HZ))

    @property
    def hop_samples(self) -> int:
        return int(round(self.hop_s * TONE_RATE_HZ))


@dataclass
class LocObservation:
    """One localizer integration window."""

    t: float                 # seconds from stream start, centre of the window
    m90: float               # 90 Hz depth, fraction of carrier (nominal 0.20)
    m150: float              # 150 Hz depth
    tone_offset: float       # found fractional tone offset eps
    carrier: float           # smoothed |z|, arbitrary units
    noise_est: float         # RMS amplitude in the off-tone bins, units of m90
    overrun: bool = False    # window touched a sample gap; discard it (F9)
    carrier_offset_hz: float = 0.0

    @property
    def ddm(self) -> float:
        """m150 - m90; + = 150 Hz predominates = right of course = fly left."""
        return float(self.m150 - self.m90)

    @property
    def sdm(self) -> float:
        return float(self.m150 + self.m90)

    @property
    def ddm_norm(self) -> float:
        """The 4.3 alternative: DDM scaled by nominal SDM / measured SDM."""
        return float(SDM_NOMINAL * self.ddm / max(self.sdm, 1e-9))

    @property
    def ddm_corrected(self) -> float:
        """The published form (D9): DDM x max(1, 0.40 / SDM).

        Undoes the envelope compression of A8 -- both depths shrink by the same
        factor as SNR falls, so the raw DDM under-reads, the unsafe direction
        -- but never scales a DDM DOWN. A station transmitting more than 40 %
        SDM (legal: 18-22 % per tone on course, Annex 10 Vol I 3.1.3.5.2) is
        therefore read raw, and one transmitting less over-reads by at most
        0.40 / 0.36 = 1.11 on course: wrong, if at all, towards more
        deviation. Measured in docs/loc-sweep.md against both pure forms.
        """
        return float(self.ddm * max(1.0, SDM_NOMINAL / max(self.sdm, 1e-9)))

    @property
    def snr_db(self) -> float:
        """Mean tone amplitude against the off-tone bins."""
        a = 0.5 * (self.m90 + self.m150)
        return float(20 * np.log10(max(a, 1e-12) / max(self.noise_est, 1e-12)))


class LocDemod:
    """Block-processing localizer demodulator. Same calling shape as VorDemod."""

    # The VOR front end, reused by reference rather than copied (plan section
    # 6). These read only p.fs, p.offset_hz, p.afc_threshold_hz, p.afc_tau_s
    # and the mixer/AFC attributes initialised in `reset()`.
    _downmix = VorDemod._downmix
    _phasor = VorDemod._phasor
    _afc = VorDemod._afc
    carrier_offset_hz = VorDemod.carrier_offset_hz

    def __init__(self, p: LocDspParams):
        self.p = p
        self._build()
        self.reset()

    def _build(self) -> None:
        p = self.p
        chan = signal.firwin(p.channel_taps, p.channel_lp_hz, fs=p.fs)
        env_taps = signal.firwin(p.env_taps, p.env_lp_hz, fs=INTERMEDIATE_HZ)
        self.dec_chan = PolyDecimator(chan, p.fs // INTERMEDIATE_HZ, np.complex64)
        self.dec_env = PolyDecimator(env_taps, INTERMEDIATE_HZ // TONE_RATE_HZ, np.float32)
        self.agc = _OnePole(p.agc_tau_s, INTERMEDIATE_HZ)
        # Passband gain of the envelope filter at each tone (1.0010 at 90 Hz,
        # 1.0009 at 150 Hz for the 700 Hz / 255-tap design), divided out so a
        # depth reads as transmitted. Taken at the nominal tone: across the
        # +/-3 % search it changes by less than 1e-5.
        _, h = signal.freqz(env_taps, worN=[TONE_90_HZ, TONE_150_HZ], fs=INTERMEDIATE_HZ)
        self._g90, self._g150 = (float(g) for g in np.abs(h))

        self._quarter_rate = (p.offset_hz == -p.fs // 4)
        self._quarter_seq = np.array([1, -1j, -1, 1j], dtype=np.complex64)
        self._quarter_seqs = [np.roll(self._quarter_seq, -k) for k in range(4)]

        w = p.window_samples
        self._n = np.arange(w)
        self._win = np.hanning(w + 2)[1:-1]              # strictly positive taps
        self._wsum = float(self._win.sum())
        nstep = int(round(p.tone_search / p.tone_step))
        self._eps = np.arange(-nstep, nstep + 1) * p.tone_step
        # (2*G, w) windowed analysis rows: 90 Hz rows then 150 Hz rows
        f = np.concatenate([TONE_90_HZ * (1 + self._eps), TONE_150_HZ * (1 + self._eps)])
        self._rows = (np.exp(-2j * np.pi * np.outer(f, self._n) / TONE_RATE_HZ)
                      * self._win).astype(np.complex64)
        self._noise_rows = (np.exp(-2j * np.pi * np.outer(np.array(p.noise_bins_hz),
                                                          self._n) / TONE_RATE_HZ)
                            * self._win).astype(np.complex64)

    def reset(self) -> None:
        """Full flush. Called on retune: no sample may survive a channel change."""
        self.dec_chan.reset()
        self.dec_env.reset()
        self.agc.reset(0.0)
        self._mix_phase = 0
        self._fine_hz = 0.0
        self._fine_phase = 0.0
        self._afc_est = 0.0
        self._afc_seconds = 0.0
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_c = np.zeros(0, dtype=np.float32)
        self._out_n = 0
        self._pending_overrun = False
        self._primed = False
        # A35: channel-filter output samples still affected by its zero history
        self._warm = -(-self.p.channel_taps // (self.p.fs // INTERMEDIATE_HZ))

    def mark_gap(self) -> None:
        """Declare a discontinuity (sample overrun, F9): the window in progress
        is reported with ``overrun=True`` so the caller discards it."""
        self._pending_overrun = True

    def push(self, iq: np.ndarray, gap: bool = False) -> list[LocObservation]:
        if gap:
            self.mark_gap()
        iq = np.asarray(iq, dtype=np.complex64)
        if iq.size == 0:
            return []
        z = self.dec_chan(self._downmix(iq))
        if z.size == 0:
            return []
        if self._warm:
            # A35: VorDemod primes its carrier estimate on the first output of
            # a channel filter that starts from zeros, i.e. near 0, and the
            # first windows then read depths up to 13x too large while it
            # decays (measured; the VOR controller discards a window and
            # warms up its history over it). Here the warm-up samples are
            # dropped and the estimate primed on the mean of what follows.
            drop = min(self._warm, z.size)
            self._warm -= drop
            z = z[drop:]
            if z.size == 0:
                return []
        self._afc(z)
        env = np.abs(z).astype(np.float32)
        if not self._primed:
            self.agc.reset(float(np.mean(env[:INTERMEDIATE_HZ // 30])))
            self._primed = True
        carrier = self.agc(env).astype(np.float32)
        np.maximum(carrier, 1e-9, out=carrier)
        audio = ((env - carrier) / carrier).astype(np.float32)
        a = self.dec_env(audio)
        if a.size == 0:
            return []
        self._buf = np.concatenate([self._buf, a])
        self._buf_c = np.concatenate(
            [self._buf_c, carrier[::INTERMEDIATE_HZ // TONE_RATE_HZ][:a.size]])
        return self._emit()

    # -- estimation -------------------------------------------------------- #

    def _amp(self, x: np.ndarray, f_hz: float) -> float:
        ph = np.exp(-2j * np.pi * f_hz * self._n / TONE_RATE_HZ)
        return float(2.0 * abs(np.dot(x * self._win, ph)) / self._wsum)

    def _search(self, seg: np.ndarray) -> tuple[float, float, float]:
        """(m90, m150, eps): grid over eps, parabola through the peak of the
        summed power, then both amplitudes read exactly at the refined eps."""
        X = self._rows @ seg.astype(np.complex64)
        g = self._eps.size
        pw = np.abs(X[:g]) ** 2 + np.abs(X[g:]) ** 2
        k = int(np.argmax(pw))
        eps = float(self._eps[k])
        if 0 < k < g - 1:
            y0, y1, y2 = pw[k - 1], pw[k], pw[k + 1]
            den = y0 - 2 * y1 + y2
            if den < 0:
                eps += float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5)) * self.p.tone_step
        return (self._amp(seg, TONE_90_HZ * (1 + eps)) / self._g90,
                self._amp(seg, TONE_150_HZ * (1 + eps)) / self._g150, eps)

    def _band(self, seg: np.ndarray, noise_pow: float) -> tuple[float, float, float]:
        """(m90, m150, 0): power in each tone's tolerance band minus the floor.
        The comparison candidate of plan section 4.2 (A34)."""
        w = seg.size
        X = np.fft.rfft(seg * self._win)
        f = np.fft.rfftfreq(w, 1.0 / TONE_RATE_HZ)
        norm = 4.0 / (w * float(np.sum(self._win ** 2)))
        out = []
        for c in (TONE_90_HZ, TONE_150_HZ):
            half = c * self.p.tone_search + 2.0 / self.p.integration_s
            b = np.abs(f - c) <= half
            e = float(np.sum(np.abs(X[b]) ** 2)) - b.sum() * noise_pow
            out.append(float(np.sqrt(max(e, 0.0) * norm)))
        return out[0] / self._g90, out[1] / self._g150, 0.0

    def _emit(self) -> list[LocObservation]:
        p = self.p
        w, hop = p.window_samples, p.hop_samples
        out: list[LocObservation] = []
        while self._buf.size >= w:
            seg = self._buf[:w].astype(np.float64)
            nb = self._noise_rows @ seg.astype(np.complex64)
            noise_amp = 2.0 * np.abs(nb) / self._wsum
            noise = float(np.sqrt(np.mean(noise_amp ** 2)))
            if p.estimator == "search":
                m90, m150, eps = self._search(seg)
            else:
                # mean |X|^2 per bin in the noise bins, unscaled
                m90, m150, eps = self._band(seg, float(np.mean(np.abs(nb) ** 2)))
            out.append(LocObservation(
                t=float((self._out_n + w / 2.0) / TONE_RATE_HZ),
                m90=m90, m150=m150, tone_offset=eps,
                carrier=float(np.mean(self._buf_c[:w])) if self._buf_c.size else 0.0,
                noise_est=noise, overrun=self._pending_overrun,
                carrier_offset_hz=self.carrier_offset_hz,
            ))
            self._pending_overrun = False
            self._buf = self._buf[hop:]
            self._buf_c = self._buf_c[hop:]
            self._out_n += hop
        return out


# --------------------------------------------------------------------------- #
# quality and validity
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LocQualityParams:
    """Thresholds and weights from the L4 sweep (A37), not from the plan.

    Chosen on the error of `ddm_corrected` binned by measured tone SNR
    (docs/loc-sweep.md, "validity"): at 26 dB the worst window is about one
    dot (20 % of full scale) out and the RMS 7-8 %; at the TRACKING/DEGRADED
    split (~29.5 dB) the RMS is about 5 %. Noise only reads at most ~10 dB.
    """

    weights: dict[str, float] = field(
        default_factory=lambda: {"sdm": 0.10, "snr": 0.70, "stab": 0.20})
    sdm_nominal: float = SDM_NOMINAL
    snr_lo_db: float = 26.0
    snr_hi_db: float = 46.0
    #: Residual DDM spread about a straight line over `history` raw values at
    #: which q_stab reaches 0 (20 % of full scale). Detrended so that a steady
    #: intercept -- DDM ramping through the course -- is not "unstable".
    stab_full_ddm: float = 0.031
    flag_snr_db: float = 26.0
    flag_sdm: float = 0.20               # Annex 10 allows >= 0.30 in coverage
    flag_stab_threshold: float = 0.25    # residual spread above 15 % of full scale
    #: The tones come from the station's own oscillator, so the found offset
    #: is steady on a real signal; on noise it wanders by ~2 % (std).
    flag_eps_std: float = 0.005
    history: int = 5

    def __post_init__(self) -> None:
        if set(self.weights) != {"sdm", "snr", "stab"}:
            raise ValueError("loc quality weights must be exactly sdm, snr, stab")
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise ValueError("loc quality weights must sum to 1.0")
        if self.snr_hi_db <= self.snr_lo_db:
            raise ValueError("loc snr_hi_db must exceed snr_lo_db")
        if self.history < 3:
            raise ValueError("loc history must be >= 3 (a line through fewer "
                             "points has no residual)")


@dataclass
class LocQualityResult:
    q_sdm: float
    q_snr: float
    q_stab: float
    value: int
    flagged: bool
    warming: bool = False
    reason: str = ""


def _clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


def detrended_std(y: list[float]) -> float:
    """Std of the residual about a least-squares line (0 for < 3 points)."""
    n = len(y)
    if n < 3:
        return 0.0
    x = np.arange(n, dtype=np.float64)
    a, b = np.polyfit(x, np.asarray(y, dtype=np.float64), 1)
    return float(np.std(np.asarray(y) - (a * x + b)))


class LocQuality:
    """Validity of a localizer solution. Never derived from the DDM value:
    noise decodes as DDM ~ 0, which is a perfect approach (plan 4.1). The
    evidence is the tone SNR, the SDM, the steadiness of the tone frequency
    and of the DDM about its trend."""

    def __init__(self, p: LocQualityParams | None = None):
        self.p = p or LocQualityParams()
        self._ddm: list[float] = []
        self._eps: list[float] = []

    def reset(self) -> None:
        self._ddm = []
        self._eps = []

    def update(self, obs: LocObservation, raw_ddm: float) -> LocQualityResult:
        """Judge one window. `raw_ddm` is the unsmoothed DDM (A15)."""
        p = self.p
        q_sdm = _clip01(obs.sdm / p.sdm_nominal)
        snr = obs.snr_db
        q_snr = _clip01((snr - p.snr_lo_db) / (p.snr_hi_db - p.snr_lo_db))
        self._ddm = (self._ddm + [float(raw_ddm)])[-p.history:]
        self._eps = (self._eps + [float(obs.tone_offset)])[-p.history:]
        n = len(self._ddm)
        judged = n >= 3
        q_stab = (_clip01(1.0 - detrended_std(self._ddm) / p.stab_full_ddm)
                  if judged else 0.0)
        w = p.weights
        value = int(round(100 * _clip01(w["sdm"] * q_sdm + w["snr"] * q_snr
                                        + w["stab"] * q_stab)))
        flagged, warming, reason = False, False, ""
        if obs.overrun:
            flagged, reason = True, "sample overrun in window"
        elif snr < p.flag_snr_db:
            flagged, reason = True, f"tone SNR below threshold ({snr:.1f} dB)"
        elif obs.sdm < p.flag_sdm:
            flagged, reason = True, f"SDM below threshold ({obs.sdm:.3f})"
        elif judged and float(np.std(self._eps)) > p.flag_eps_std:
            flagged, reason = True, (f"tone frequency unsteady "
                                     f"({100 * float(np.std(self._eps)):.2f} %)")
        elif judged and q_stab < p.flag_stab_threshold:
            flagged, reason = True, f"DDM unstable ({q_stab:.2f})"
        elif not judged:
            flagged, warming, reason = True, True, "acquiring: insufficient DDM history"
        if flagged:
            value = 0
        return LocQualityResult(q_sdm=q_sdm, q_snr=q_snr, q_stab=q_stab, value=value,
                                flagged=flagged, warming=warming, reason=reason)
