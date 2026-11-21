"""Automatic front-end gain within a configured range (CLAUDE.md A47).

Why this exists: the 2026-11-14 and 11-20 flights decoded no VOR or localizer
because broadcast FM overloaded the dongle's front end (intermodulation filled
the 1 MHz passband; the ADC clipped 3-12 % of samples) while the glide slope at
330 MHz, through the same dongle at the same gain, decoded at 50 dB. The right
gain depends on the FM field, which changes with altitude and position, so a
fixed number in the config cannot be right everywhere.

What it optimises is the **carrier against the floor**, not the noise. When the
floor is intermodulation generated in the LNA, one dB of gain removed lowers
the floor by about 3 dB and the carrier by 1, so less gain is better; when the
floor is the receiver's own noise, more gain is better until the ADC fills.
Which case applies is found by trying: the controller probes one gain step and
keeps it only if the carrier-to-floor ratio improved.

Rules, evaluated once per `eval_s` of samples, in this order:

1. clipping >= `clip_severe_pct`: two steps down, in any decoding state;
2. while tracking (`policy` "protect") nothing else is done, because every step
   costs the windows that span it;
3. a probe in progress is judged: kept if the carrier-to-floor rose by at least
   `improve_db` and the ADC is not clipping, otherwise undone;
4. clipping >= `clip_max_pct`, or no carrier visible and the ADC fuller than
   `rms_high_counts`: one step down;
5. no carrier visible, ADC nearly idle (< `rms_low_counts`), not clipping:
   one step up;
6. a carrier visible and `probe_s` since the last probe: probe one step.

Pure and hardware-free: `SdrReader` feeds it blocks and applies what it
returns, so the policy is testable without a dongle.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

#: complex64 samples are unit-scaled; cu8 full scale is 127.5 counts.
COUNTS = 127.5
#: a component this close to full scale is a clipped sample (cu8 0/255 map to
#: exactly -1/+1; SoapySDR's rtlsdr driver scales slightly differently).
CLIP_LEVEL = 0.99
NFFT = 16384
#: VHF / UHF split for the per-band memory (the glide slope is 329-335 MHz).
UHF_HZ = 200_000_000

POLICIES = ("free", "protect", "hold")


@dataclass(frozen=True)
class AutoGainParams:
    enabled: bool = False
    min_db: float = 0.0
    max_db: float = 49.6
    clip_max_pct: float = 1.0
    clip_severe_pct: float = 5.0
    rms_low_counts: float = 4.0
    rms_high_counts: float = 20.0
    eval_s: float = 1.0
    probe_s: float = 30.0
    improve_db: float = 1.0
    carrier_min_db: float = 6.0
    search_hz: float = 10_000.0
    settle_s: float = 1.5

    @classmethod
    def from_cfg(cls, d: dict | None) -> AutoGainParams:
        d = {k: v for k, v in (d or {}).items() if not k.startswith("_")}
        return cls(**d)


@dataclass(frozen=True)
class FrontEnd:
    """One evaluation period's view of the ADC and the band."""
    clip_pct: float
    rms_counts: float
    cnr_db: float | None      # carrier above floor in a fs/NFFT bin; None = no carrier seen
    peak_db: float            # the best peak in the search window, carrier or not
    seconds: float


class FrontEndMeter:
    """Accumulates clipping, ADC level and an averaged spectrum."""

    def __init__(self, fs: int, carrier_hz: float, search_hz: float, carrier_min_db: float):
        self.fs = int(fs)
        self.carrier_hz = float(carrier_hz)
        self.search_hz = float(search_hz)
        self.carrier_min_db = float(carrier_min_db)
        fr = np.fft.fftshift(np.fft.fftfreq(NFFT, 1.0 / self.fs))
        d = np.abs(fr - self.carrier_hz)
        self._peak_bins = d <= self.search_hz
        self._floor_bins = (d > 20_000) & (d < 150_000)
        self._win = np.hanning(NFFT).astype(np.float32)
        self.reset()

    def reset(self) -> None:
        self.n = 0
        self.clipped = 0
        self.power = 0.0
        self.psd = np.zeros(NFFT)
        self.nfft = 0

    def add(self, iq: np.ndarray) -> None:
        iq = np.asarray(iq, dtype=np.complex64)
        if iq.size == 0:
            return
        re, im = iq.real, iq.imag
        clipped = (np.abs(re) >= CLIP_LEVEL) | (np.abs(im) >= CLIP_LEVEL)
        self.clipped += int(np.count_nonzero(clipped))
        self.power += float(np.vdot(iq, iq).real)
        self.n += iq.size
        if iq.size >= NFFT:
            seg = iq[:NFFT] * self._win
            self.psd += np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
            self.nfft += 1

    @property
    def seconds(self) -> float:
        return self.n / self.fs

    def result(self) -> FrontEnd:
        rms = math.sqrt(self.power / max(self.n, 1) / 2.0) * COUNTS
        clip = 100.0 * self.clipped / max(self.n, 1)
        cnr, peak = None, float("-inf")
        if self.nfft and self._floor_bins.any():
            floor = float(np.median(self.psd[self._floor_bins]))
            if floor > 0:
                peak = 10 * math.log10(float(self.psd[self._peak_bins].max()) / floor)
                if peak >= self.carrier_min_db:
                    cnr = peak
        return FrontEnd(clip, rms, cnr, peak, self.seconds)


@dataclass
class Decision:
    gain_db: float
    reason: str
    #: False for a decision to stay (a probe that helped): logged, not applied
    changed: bool = True


@dataclass
class _Probe:
    from_db: float
    base_cnr: float


class GainController:
    """Chooses the next gain step. `observe()` returns a Decision or None."""

    def __init__(self, params: AutoGainParams, ladder_db: Sequence[float],
                 initial_db: float, fs: int, carrier_hz: float):
        self.p = params
        full = sorted({float(g) for g in ladder_db})
        steps = [g for g in full if params.min_db - 1e-6 <= g <= params.max_db + 1e-6]
        if not steps and full:
            # the range misses every step of this tuner: take the step nearest it
            mid = (params.min_db + params.max_db) / 2
            steps = [min(full, key=lambda g: abs(g - mid))]
        if not steps:
            steps = [float(initial_db)]
        self.ladder = steps
        self.gain_db = min(steps, key=lambda g: abs(g - initial_db))
        self.meter = FrontEndMeter(fs, carrier_hz, params.search_hz, params.carrier_min_db)
        self._settling = True
        self._probe: _Probe | None = None
        self._dir = -1
        self._next_probe = 0.0
        self._band: str | None = None
        self._band_gain: dict[str, float] = {}
        self.last: FrontEnd | None = None
        self.changes = 0

    # -- helpers ----------------------------------------------------------- #

    def _step(self, k: int) -> float:
        i = self.ladder.index(self.gain_db)
        return self.ladder[max(0, min(len(self.ladder) - 1, i + k))]

    def _change(self, to_db: float, reason: str) -> Decision | None:
        if abs(to_db - self.gain_db) < 1e-6:
            return None
        self.gain_db = to_db
        self._settling = True
        self.changes += 1
        return Decision(to_db, reason)

    # -- events ------------------------------------------------------------ #

    def applied(self, gain_db: float) -> None:
        """The device reports the gain it really set (its own rounding)."""
        if self.ladder:
            self.gain_db = min(self.ladder, key=lambda g: abs(g - gain_db))

    def retuned(self, nav_hz: int) -> float | None:
        """A new channel. Returns a gain to apply when the band changed and a
        gain is remembered for the new band (VHF nav <-> UHF glide slope on a
        shared dongle), else None."""
        band = "uhf" if nav_hz >= UHF_HZ else "vhf"
        out = None
        if self._band is not None and band != self._band:
            self._band_gain[self._band] = self.gain_db
            if band in self._band_gain:
                out = self._band_gain[band]
                self.gain_db = out
        self._band = band
        self._probe = None
        self._settling = True
        self.meter.reset()
        return out

    def observe(self, iq: np.ndarray, now: float, policy: str = "free") -> Decision | None:
        self.meter.add(iq)
        if self.meter.seconds < self.p.eval_s:
            return None
        m = self.meter.result()
        self.meter.reset()
        self.last = m
        if self._settling:            # this period straddled the last change
            self._settling = False
            return None
        if policy == "hold":
            self._probe = None
            return None
        p = self.p
        if m.clip_pct >= p.clip_severe_pct:
            self._probe = None
            return self._change(self._step(-2), f"ADC clipping {m.clip_pct:.1f} %")
        if policy == "protect":
            # tracking: keep whatever gain got us here, probe included
            self._probe = None
            return None
        if self._probe is not None:
            pr, self._probe = self._probe, None
            if (m.cnr_db is not None and m.clip_pct < p.clip_max_pct
                    and m.cnr_db >= pr.base_cnr + p.improve_db):
                self._next_probe = now          # it helped: try the next step soon
                return Decision(self.gain_db, f"probe kept: carrier/floor "
                                f"{pr.base_cnr:.1f} -> {m.cnr_db:.1f} dB", changed=False)
            self._dir = -self._dir
            self._next_probe = now + p.probe_s
            got = "no carrier" if m.cnr_db is None else f"{m.cnr_db:.1f} dB"
            return self._change(pr.from_db, f"probe undone: carrier/floor "
                                f"{pr.base_cnr:.1f} -> {got}")
        if m.clip_pct >= p.clip_max_pct:
            return self._change(self._step(-1), f"ADC clipping {m.clip_pct:.1f} %")
        if m.cnr_db is None and m.rms_counts > p.rms_high_counts:
            return self._change(self._step(-1), f"ADC level {m.rms_counts:.0f} counts "
                                "with no carrier visible")
        if m.cnr_db is None and m.rms_counts < p.rms_low_counts and m.clip_pct == 0.0:
            # only with no carrier in view: with one, carrier/floor is the
            # objective and the probe finds out whether more gain helps
            return self._change(self._step(+1), f"ADC nearly idle ({m.rms_counts:.1f} counts)")
        if m.cnr_db is not None and now >= self._next_probe:
            if self._step(self._dir) == self.gain_db:
                self._dir = -self._dir            # at the end of the range: turn round
            to = self._step(self._dir)
            if to == self.gain_db:
                self._next_probe = now + p.probe_s
                return None
            self._probe = _Probe(self.gain_db, m.cnr_db)
            return self._change(to, f"probe {'down' if self._dir < 0 else 'up'} "
                                f"from carrier/floor {m.cnr_db:.1f} dB")
        return None
