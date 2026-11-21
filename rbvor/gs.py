"""ILS glide slope (glide path): constants, observation, demodulator, quality.

Reference: docs/gs-plan.md sections 4-6; CLAUDE.md A39-A44.

A glide path carries the localizer's modulation -- 90 Hz and 150 Hz AM -- on a
UHF carrier, so the demodulator *is* `rbvor.loc.LocDemod` (plan section 2).
What differs is written down here, each figure from Annex 10 Vol I as amended
to No. 74 (the edition A33 used, O26) or from measurement:

- 40 % per tone on the path, SDM 0.80 (3.1.5.5.1), not the localizer's 0.40;
- full-scale deflection at DDM 0.175 by airborne convention (twice the 0.0875
  that bounds the half sector, 3.1.5.6.5); like the localizer's 0.155 this is
  not stated in Annex 10 Vol I (O26);
- sense: 150 Hz predominates **below** the path (3.1.5.1.4), so a positive
  DDM is an aircraft below the path, which must **fly up**;
- carrier tolerance 0.005 % single frequency (3.1.5.2.1): 16.75 kHz at
  335 MHz, wider than the 13 kHz channel filter -- hence `acquire_hz`, a coarse
  carrier search before that filter (plan 4.2, A41).

Pure DSP, like `rbvor.loc`: no I/O, no config import, no logging, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from rbvor.loc import LocDemod, LocDspParams, LocObservation, LocQualityParams

__all__ = ["SDM_NOMINAL", "DDM_FULL_SCALE", "GsObservation", "GsDspParams", "GsDemod",
           "GsQualityParams", "ddm_to_dev_pct", "gs_ddm_corrected"]

#: Annex 10 Vol I 3.1.5.5.1: 40 % per tone on the glide path.
SDM_NOMINAL = 0.80
#: Indicator full scale, airborne convention (2 x 0.0875, Annex 10 Vol I
#: 3.1.5.6.5). Not in Annex 10 Vol I; same caveat as the localizer's (O26).
DDM_FULL_SCALE = 0.175


def gs_ddm_corrected(ddm: float, sdm: float) -> float:
    """The published glide-slope form: DDM x max(1, 0.80 / SDM) -- D9's
    one-sided correction at the glide path's own nominal, chosen in A42 from
    the same three-way comparison D9 made for the localizer."""
    return float(ddm * max(1.0, SDM_NOMINAL / max(sdm, 1e-9)))


def ddm_to_dev_pct(ddm: float) -> int:
    """`gsdev`: DDM as % of full scale, clamped to +/-100; + = fly up.
    Half away from zero, as `loc.ddm_to_dev_pct` (A12)."""
    x = 100.0 * ddm / DDM_FULL_SCALE
    q = int(np.floor(abs(x) + 0.5))
    return int(np.sign(x)) * min(q, 100)


@dataclass
class GsObservation(LocObservation):
    """One glide-slope window. The localizer's record with the glide path's
    nominal SDM in the two derived DDMs; `ddm` is still m150 - m90, positive
    when 150 Hz predominates, which on a glide path means below it."""

    acquired_hz: float = 0.0     # coarse carrier search result (A41), 0 if none

    @property
    def ddm_norm(self) -> float:
        return float(SDM_NOMINAL * self.ddm / max(self.sdm, 1e-9))

    @property
    def ddm_corrected(self) -> float:
        return gs_ddm_corrected(self.ddm, self.sdm)


@dataclass(frozen=True)
class GsDspParams(LocDspParams):
    """The localizer chain plus the coarse carrier acquisition of A41.

    `acquire_hz` 0 disables the search, and the chain is then `LocDemod`
    sample for sample (pinned by test)."""

    #: +/- span of the carrier search around the nominal, Hz. 0 = off.
    acquire_hz: float = 0.0
    #: Length of the block searched, seconds. 64 ms gives 15.6 Hz bins.
    acquire_s: float = 0.064
    #: Peak against the median of the searched span, dB. Noise alone reads
    #: about 11 dB over a 60 kHz span (the expected maximum of ~3800
    #: exponential bins against their median); below the threshold nothing
    #: is corrected and the AFC is left to do what it can.
    acquire_min_db: float = 20.0
    #: While no carrier has been found, search again this often, on a copy of
    #: the stream (nothing is held back after the first search). A glide path
    #: that comes into coverage after the retune is still found (A41).
    acquire_retry_s: float = 1.0

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.acquire_hz < 0 or self.acquire_hz >= self.fs / 2 - abs(self.offset_hz):
            raise ValueError("gs acquire_hz must be >= 0 and keep the search inside "
                             "the sampled band")
        if self.acquire_hz and not 0.004 <= self.acquire_s <= 1.0:
            raise ValueError("gs acquire_s must be 0.004-1.0 s")
        if self.acquire_retry_s <= 0:
            raise ValueError("gs acquire_retry_s must be > 0")


class GsDemod(LocDemod):
    """`LocDemod` emitting `GsObservation`, with an optional coarse carrier
    search on the first `acquire_s` of samples after every reset (A41).

    The search runs on the raw block, before the channel filter: a peak
    within `acquire_hz` of where the carrier belongs, refined by a parabola,
    sets the down-mix correction the AFC would otherwise have to find from
    inside a filter that may already have removed the carrier. The AFC then
    tracks from there, unchanged."""

    obs_cls = GsObservation

    def __init__(self, p: GsDspParams):
        super().__init__(p)

    def reset(self) -> None:
        super().reset()
        self._acq_done = not self.p.acquire_hz     # first search still to come
        self._acq_ok = False                       # a carrier has been found
        self._acq_buf: list[np.ndarray] = []
        self._acq_have = 0
        self._acq_wait = 0                         # samples until the next retry
        self.acquired_hz = 0.0
        self.acquire_peak_db = 0.0
        self.acquire_attempts = 0

    def _acq_n(self) -> int:
        n = int(round(self.p.acquire_s * self.p.fs))
        return 1 << max(10, (n - 1).bit_length())

    def _acquire(self, x: np.ndarray) -> None:
        p = self.p
        self.acquire_attempts += 1
        n = x.size
        X = np.fft.fft(x * np.hanning(n).astype(np.float32))
        f = np.fft.fftfreq(n, 1.0 / p.fs)
        centre = -p.offset_hz                    # where the carrier sits (A22)
        sel = np.flatnonzero(np.abs(f - centre) <= p.acquire_hz)
        if sel.size < 3:
            return
        pw = np.abs(X[sel]) ** 2
        k = int(np.argmax(pw))
        peak_db = float(10 * np.log10(pw[k] / max(float(np.median(pw)), 1e-30)))
        self.acquire_peak_db = peak_db
        if peak_db < p.acquire_min_db:
            return
        df = p.fs / n
        fk = float(f[sel[k]])
        if 0 < k < sel.size - 1:
            y0, y1, y2 = (float(np.log(max(v, 1e-30))) for v in pw[k - 1:k + 2])
            den = y0 - 2 * y1 + y2
            if den < 0:
                fk += float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5)) * df
        self.acquired_hz = fk - centre
        self._acq_ok = True
        # Within the AFC's own threshold the AFC is left to it: the chain then
        # stays sample-identical to LocDemod and costs nothing extra (a
        # down-mix correction is a complex multiply per sample, measured +0.8 %
        # of a core). Beyond it, the correction is set and the AFC restarts
        # its estimate from there.
        if p.afc_threshold_hz <= 0 or abs(self.acquired_hz) >= p.afc_threshold_hz:
            self._fine_hz = self.acquired_hz
            self._afc_est = 0.0
            self._afc_seconds = 0.0

    def push(self, iq: np.ndarray, gap: bool = False) -> list[LocObservation]:
        if self._acq_done:
            out = super().push(iq, gap=gap)
            if self.p.acquire_hz and not self._acq_ok:
                self._retry(np.asarray(iq, dtype=np.complex64))
            return out
        if gap:
            self.mark_gap()
        iq = np.asarray(iq, dtype=np.complex64)
        self._acq_buf.append(iq)
        self._acq_have += iq.size
        if self._acq_have < self._acq_n():
            return []
        buf = np.concatenate(self._acq_buf)
        self._acq_buf = []
        self._acq_have = 0
        self._acq_done = True
        self._acquire(buf[:self._acq_n()])
        self._acq_wait = int(round(self.p.acquire_retry_s * self.p.fs))
        return super().push(buf)

    def _retry(self, iq: np.ndarray) -> None:
        """No carrier found yet: every `acquire_retry_s`, search a copy of the
        next `acquire_s` of the stream, which is decoded meanwhile as usual.
        A correction found mid-stream is a step in the down-mix; the windows
        it touches were judged no-signal anyway."""
        if self._acq_wait > 0:
            self._acq_wait -= iq.size
            return
        self._acq_buf.append(iq)
        self._acq_have += iq.size
        if self._acq_have >= self._acq_n():
            buf = np.concatenate(self._acq_buf)
            self._acq_buf = []
            self._acq_have = 0
            self._acquire(buf[:self._acq_n()])
            self._acq_wait = int(round(self.p.acquire_retry_s * self.p.fs))

    def _emit(self) -> list[LocObservation]:
        out = super()._emit()
        for o in out:
            o.acquired_hz = self.acquired_hz
        return out


@dataclass(frozen=True)
class GsQualityParams(LocQualityParams):
    """`LocQualityParams` at the glide path's own scale. The figures are the
    G5 sweep's (docs/gs-sweep.md, A43), not the localizer's carried over."""

    weights: dict[str, float] = field(
        default_factory=lambda: {"sdm": 0.10, "snr": 0.70, "stab": 0.20})
    sdm_nominal: float = SDM_NOMINAL
    snr_lo_db: float = 26.0
    snr_hi_db: float = 46.0
    #: 20 % of the glide-slope full scale
    stab_full_ddm: float = 0.035
    flag_snr_db: float = 26.0
    #: Annex 10 allows 37.5-42.5 % per tone; a lawful glide path reads >= 0.60
    #: strong. Twice the localizer's figure, as its nominal is.
    flag_sdm: float = 0.40
    flag_stab_threshold: float = 0.25
    flag_eps_std: float = 0.005
    history: int = 5
