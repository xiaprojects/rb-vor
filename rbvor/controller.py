"""State machine: channel changes, retune sequencing, staleness, publishing.

Reference: rb-vor-architecture.md sections 7.1 and 10.4; failure modes F1-F3,
F8, F9; implementation plan M6.

Deliberately synchronous and clock-injected. Every input is an explicit event
and every timer reads an injected clock, so the whole state table is testable
without asyncio, without hardware and without waiting in real time. The asyncio
wiring lives in M7, and does nothing except translate I/O into these calls.

AMENDMENT A13 -- reconciling doc A section 7.1 with amendment A11
-----------------------------------------------------------------
Doc A defines DEGRADED as "quality below threshold but > 0", publishing the last
good bearing with a "true low vorsignal". A11 then made the NAV flag force
`vorsignal` to 0 outright. Read together the two states became identical on the
wire, which makes DEGRADED meaningless as a published distinction.

Reconciled as follows, and this is now normative:

===========  ==================================  =============================
state        entered when                        publishes
===========  ==================================  =============================
TRACKING     solution not flagged, value >= 40   current bearing, real value
DEGRADED     solution not flagged, value < 40    current bearing, real value
NO_SIGNAL    solution flagged                    last good bearing, value 0
===========  ==================================  =============================

So DEGRADED is a genuine "usable but poor" state with a non-zero `vorsignal`,
which is what doc A's wording describes, and NO_SIGNAL owns the flag. `hold_s`
then governs how long NO_SIGNAL keeps repeating the last known bearing before
the tracker and quality history are flushed and the machine returns to
ACQUIRING -- stale dispersion from a signal that has gone must not linger and
make the next acquisition look good.

AMENDMENT A33 -- localizer mode
-------------------------------
With `loc.enabled`, a localizer channel is tuned and decoded by
`rbvor.loc.LocDemod` instead of being rejected. The mode is fixed per tuned
channel by `freqrules`, so one demodulator runs at a time and every state of
the table above is reused. On a localizer channel the vor* fields read as a
rejected frequency does in v2.2 (all 0), the loc* fields carry the localizer,
and both go out in one POST. With `loc.enabled` false nothing here runs and the
payload is v2.2's six keys, byte for byte.

Internally `vorsignal == 0` still means "this bearing is not to be trusted", in
every state, always. It is not on the wire: since AMENDMENT A20 the payload is
never zeroed, and `vorstatus` (>= 4 == usable) is the only validity signal a
consumer reads.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC
from enum import StrEnum
from typing import Protocol

import numpy as np

from rbvor import freqrules
from rbvor.bearing import BearingTracker, to_int_bearing
from rbvor.dsp import VorDemod
from rbvor.loc import LocDemod, LocQuality
from rbvor.quality import ACCEPTABLE, QualityEstimator
from rbvor.sdr import SdrError

__all__ = ["State", "Controller", "SdrPort", "PublisherPort"]

log = logging.getLogger("rbvor.controller")

#: A38: hysteresis on the localizer's TRACKING/DEGRADED split, score points.
LOC_SPLIT_HYSTERESIS = 5


class State(StrEnum):
    IDLE = "IDLE"
    INVALID_FREQ = "INVALID_FREQ"
    TUNING = "TUNING"
    NO_SDR = "NO_SDR"
    ACQUIRING = "ACQUIRING"
    TRACKING = "TRACKING"
    DEGRADED = "DEGRADED"
    NO_SIGNAL = "NO_SIGNAL"


#: States in which incoming samples may be decoded. Anything else drops them --
#: this set is the mechanism that makes retune contamination (F3) impossible.
DECODING_STATES = frozenset({State.ACQUIRING, State.TRACKING,
                             State.DEGRADED, State.NO_SIGNAL})


class SdrPort(Protocol):
    def set_frequency(self, nav_hz: int) -> None:
        """Tune to a navaid frequency. The port applies the configured offset."""

    def close(self) -> None:
        ...


class PublisherPort(Protocol):
    def publish(self, vordir: int | None, snr_db: float = 0.0,
                am_depth: float = 0.0, *, state: str, valid: bool = True,
                status: int = 0, sub_level: float = 0.0, freq_khz: int = 0,
                reason: str = "") -> None:
        ...


class Controller:
    """Owns the demodulator, the bearing tracker and the quality estimator."""

    def __init__(self, cfg, sdr: SdrPort, publisher: PublisherPort,
                 clock: Callable[[], float] = time.monotonic,
                 notify: Callable[[], None] | None = None,
                 recorder=None, solution_sink=None, obs_sink=None,
                 sdr_retry_s: float | None = None,
                 loc_solution_sink=None, loc_obs_sink=None):
        self.cfg = cfg
        self.sdr = sdr
        self.publisher = publisher
        self.clock = clock
        self.notify = notify
        # Optional, wired by the daemon (M9). The controller works identically
        # with both None -- every existing test constructs it that way -- so
        # forensic logging cannot change the bearing path.
        #   recorder:      rbvor.record.RingRecorder, fed raw IQ, dumped on flag
        #   solution_sink: callable(SolutionRecord); the 1 Hz CSV + cloud row
        #   obs_sink:      callable(Observation, freq_khz); the raw per-window
        #                  decode dump (--csv), one row per demodulated window
        #                  including overrun/warming windows -- same data
        #                  vor_replay.py writes, plus the tuned frequency.
        self.recorder = recorder
        self.solution_sink = solution_sink
        self.obs_sink = obs_sink
        #: A30: seconds between attempts on a dongle that cannot be had, or None
        #: to let the SdrError through. The daemon sets it when the channel
        #: comes from the bridge; in bench mode nothing will free the dongle.
        self.sdr_retry_s = sdr_retry_s

        self.demod = VorDemod(cfg.dsp)
        self.tracker = BearingTracker(cfg.bearing, history=cfg.quality.history)
        self.quality = QualityEstimator(cfg.quality)

        # A33: the localizer. Built only when enabled, so a disabled feature
        # costs nothing and changes nothing. `mode` is fixed per tuned channel.
        self.loc_cfg = cfg.loc if hasattr(cfg, "loc") else None
        self.loc_enabled = bool(self.loc_cfg and self.loc_cfg.enabled)
        self.loc_solution_sink = loc_solution_sink
        self.loc_obs_sink = loc_obs_sink
        if self.loc_enabled:
            self.loc_demod = LocDemod(self.loc_cfg.dsp)
            self.loc_quality = LocQuality(self.loc_cfg.quality)
        self.mode: str | None = None          # "vor" | "loc" | None
        self._loc_hist: list[float] = []      # raw published-form DDMs, for smoothing
        self._loc_ddm: float | None = None    # smoothed, what goes on the wire
        self._last_loc_obs = None
        self._last_loc_q = None

        self.state = State.IDLE
        self.channel_khz: int | None = None
        self.last_reason = "startup"

        self._pending: tuple[int, float] | None = None
        self._settle_at = 0.0
        self._sdr_retry_at = 0.0
        self._discard_next = False
        self._consecutive_valid = 0
        self._last_good: int | None = None
        self._last_good_at: float | None = None
        self._bridge_lost_at: float | None = None
        self._published_idle = False
        self._last_iq_ref = ""
        self._last_q = None
        self._last_obs = None
        self._gps_track: float | None = None

    # -- helpers ----------------------------------------------------------- #

    @property
    def _freq_kwargs(self) -> dict:
        f = self.cfg.frequency
        return {
            "allowlist_mhz": f["allowlist_mhz"],
            "denylist_mhz": f["denylist_mhz"],
            "band_min_khz": int(round(f["min_mhz"] * 1000)),
            "band_max_khz": int(round(f["max_mhz"] * 1000)),
            "grid_khz": f["grid_khz"],
            "tolerance_khz": f["snap_tolerance_khz"],
        }

    def _enter(self, state: State, reason: str) -> None:
        if state != self.state:
            self.state = state
            self.last_reason = reason
            # Requirement: OFF/Disabled/No-frequency/IDLE release the SDR so the
            # dongle is free (and cool) when no VOR is selected. INVALID_FREQ is
            # the "no valid frequency" case and releases too. The reader reopens
            # automatically on the next set_frequency (retune), so this is safe
            # to do on every entry; SdrReader.close() is idempotent. NO_SDR
            # releases because a retune can fail with the device already open
            # (A30), and waiting must not hold what the other program wants.
            if state in (State.IDLE, State.INVALID_FREQ, State.NO_SDR):
                self._release_sdr()

    def _release_sdr(self) -> None:
        """Free the SDR device. Safe to call when already released."""
        try:
            self.sdr.close()
        except Exception as e:                       # noqa: BLE001
            log.debug("SDR release: %s", e)

    #: map the internal state machine to the published vorstatus code.
    _STATUS_CODE = {
        State.IDLE: 0,           # OFF / Disabled / No frequency / IDLE
        State.INVALID_FREQ: 0,   # no valid frequency selected
        State.NO_SIGNAL: 1,      # antenna disconnected / no signal / very low data
        State.NO_SDR: 1,         # a channel is selected, no dongle to hear it on (A30)
        State.TUNING: 2,
        State.ACQUIRING: 3,
        State.DEGRADED: 4,       # usable but poor -- a tracked bearing, low quality
        State.TRACKING: 5,       # tracking, good quality
    }

    def _loc_wire(self):
        """The loc* half of the payload (A33). Zeros unless on a LOC channel."""
        from rbvor.publisher import LocWire
        if self.mode != "loc":
            return LocWire()
        o = self._last_loc_obs
        status = self._STATUS_CODE.get(self.state, 0)
        return LocWire(status=status,
                       ddm=self._loc_ddm if o is not None else None,
                       sdm=o.sdm if o is not None else 0.0,
                       snr_db=o.snr_db if o is not None else 0.0,
                       freq_khz=self.channel_khz or 0,
                       usable=status >= 4 and self._loc_ddm is not None)

    def _publish(self, vordir: int | None, vorsignal: int, reason: str = "") -> None:
        if self.loc_enabled:
            self._publish_with_loc(vordir, vorsignal, reason)
            return
        # `vorsignal` (0-100 quality) is retained internally as the validity
        # gate for the solution log: >0 means a real bearing, 0 means
        # flag/idle/out-of-band. The wire format has not carried it since v2.0.
        #
        # AMENDMENT A20 -- the payload is never zeroed. Every field carries the
        # last measurement the receiver actually made, and `vorstatus` is the
        # single field that tells the consumer whether the bearing may be used.
        # Suppressing the measurements here threw away exactly the data a
        # remote needs to judge a marginal signal for itself, and duplicated a
        # decision `vorstatus` already expresses.
        #
        # The measurements read 0 only when there is genuinely nothing to
        # report: no observation yet, or none since the last retune -- `_flush`
        # drops the previous station's, so a stale reading can never be
        # attributed to a new channel (F3).
        valid = vorsignal > 0 and vordir is not None
        o = self._last_obs
        snr_db = o.snr_db if o else 0.0
        am_depth = o.amp_a if o else 0.0
        sub_level = o.sub_level if o else 0.0
        status = self._STATUS_CODE.get(self.state, 0)
        # vorfreqactive: the currently tuned VOR channel in kHz (e.g. 109400).
        # 0 when no channel is selected (IDLE) or the frequency was rejected.
        freq_khz = self.channel_khz if self.channel_khz else 0
        self.publisher.publish(vordir, snr_db, am_depth, state=str(self.state),
                               valid=valid, status=status, sub_level=sub_level,
                               freq_khz=freq_khz,
                               reason=reason or self.last_reason)
        if self.solution_sink is not None:
            self._emit_solution(vordir, vorsignal, reason or self.last_reason)

    def _publish_with_loc(self, vordir, vorsignal: int, reason: str) -> None:
        """`_publish` when the localizer is enabled: the v2.3 payload, in which
        a LOC channel reads as a rejected frequency in the vor* fields."""
        loc = self._loc_wire()
        why = reason or self.last_reason
        if self.mode == "loc":
            self.publisher.publish(None, 0.0, 0.0, state=str(self.state), valid=False,
                                   status=0, sub_level=0.0, freq_khz=0,
                                   reason=why, loc=loc)
            if self.loc_solution_sink is not None:
                self._emit_loc_solution(why)
            return
        valid = vorsignal > 0 and vordir is not None
        o = self._last_obs
        self.publisher.publish(vordir, o.snr_db if o else 0.0, o.amp_a if o else 0.0,
                               state=str(self.state), valid=valid,
                               status=self._STATUS_CODE.get(self.state, 0),
                               sub_level=o.sub_level if o else 0.0,
                               freq_khz=self.channel_khz if self.channel_khz else 0,
                               reason=why, loc=loc)
        if self.solution_sink is not None:
            self._emit_solution(vordir, vorsignal, why)

    def _emit_loc_solution(self, reason: str) -> None:
        import contextlib
        from datetime import datetime

        from rbvor.solutionlog import LocSolutionRecord
        q = self._last_loc_q
        o = self._last_loc_obs
        wire = self._loc_wire()
        rec = LocSolutionRecord(
            t_utc=datetime.now(tz=UTC).isoformat(),
            t_mono=self.clock(),
            channel_khz=self.channel_khz,
            state=str(self.state),
            locstatus=wire.status,
            ddm=self._loc_ddm if wire.usable else None,
            valid=wire.usable,
            reason=reason,
            q_sdm=q.q_sdm if q else 0.0,
            q_snr=q.q_snr if q else 0.0,
            q_stab=q.q_stab if q else 0.0,
            value=q.value if q else 0,
            m90=o.m90 if o else 0.0,
            m150=o.m150 if o else 0.0,
            ddm_raw=o.ddm if o else 0.0,
            ddm_corrected=o.ddm_corrected if o else 0.0,
            sdm=o.sdm if o else 0.0,
            snr_db=o.snr_db if o else 0.0,
            tone_offset=o.tone_offset if o else 0.0,
            carrier=o.carrier if o else 0.0,
            carrier_offset_hz=o.carrier_offset_hz if o else 0.0,
            gps_track=self._gps_track,
            overrun=o.overrun if o else False,
            iq_ref=self._last_iq_ref,
        )
        with contextlib.suppress(Exception):         # a log failure never stalls a solution
            self.loc_solution_sink(rec)

    def _emit_solution(self, vordir, vorsignal, reason: str) -> None:
        import contextlib
        from datetime import datetime

        from rbvor.solutionlog import SolutionRecord
        q = self._last_q
        o = self._last_obs
        rec = SolutionRecord(
            t_utc=datetime.now(tz=UTC).isoformat(),
            t_mono=self.clock(),
            channel_khz=self.channel_khz,
            state=str(self.state),
            vordir=vordir,
            vorsignal=vorsignal,
            flagged=(vorsignal == 0),
            reason=reason,
            q_am=q.q_am if q else 0.0,
            q_sub=q.q_sub if q else 0.0,
            q_snr=q.q_snr if q else 0.0,
            q_stab=q.q_stab if q else 0.0,
            amp_a=o.amp_a if o else 0.0,
            sub_level=o.sub_level if o else 0.0,
            deviation_hz=o.amp_b if o else 0.0,
            carrier=o.carrier if o else 0.0,
            snr_db=o.snr_db if o else 0.0,
            carrier_offset_hz=getattr(o, "carrier_offset_hz", 0.0) if o else 0.0,
            sub_offset_hz=getattr(o, "sub_offset_hz", 0.0) if o else 0.0,
            sub_coh=getattr(o, "sub_coh", 0.0) if o else 0.0,
            gps_track=self._gps_track,
            overrun=o.overrun if o else False,
            iq_ref=self._last_iq_ref,
        )
        with contextlib.suppress(Exception):         # a log failure never stalls a bearing
            self.solution_sink(rec)

    def _flush(self) -> None:
        """Drop everything derived from the previous signal."""
        self.demod.reset()
        self.tracker.reset()
        self.quality.reset()
        if self.recorder is not None:
            self.recorder.reset()      # old-station IQ is not evidence for the new one
        # A20: the payload is no longer zeroed by validity, so the last
        # observation is what the wire reports until a new one arrives. It must
        # therefore die with the signal it came from -- otherwise station A's
        # SNR/AM/SUB would be published against station B's channel (F3).
        self._last_obs = None
        self._last_q = None
        if self.loc_enabled:
            # A26 for the localizer: one station's DDM is never attributed to
            # the next, including across a VOR <-> LOC change.
            self.loc_demod.reset()
            self.loc_quality.reset()
        self._loc_hist = []
        self._loc_ddm = None
        self._last_loc_obs = None
        self._last_loc_q = None
        self._consecutive_valid = 0
        self._last_good = None
        self._last_good_at = None
        self._discard_next = True

    # -- events from the bridge -------------------------------------------- #

    def set_channel(self, mhz: float | None) -> None:
        """A `vorfreq` value arrived (or went away). Debounced, never immediate."""
        if mhz is None:
            self._pending = None
            if self.state is not State.IDLE:
                self.channel_khz = None
                self.mode = None
                self._flush()
                self._enter(State.IDLE, "no vorfreq in bridge state")
                self._published_idle = False
            return

        ok, reason = freqrules.is_vor_frequency(mhz, **self._freq_kwargs)
        if not ok and self.loc_enabled:
            # A33: a localizer channel is a valid channel in its own right
            ok = freqrules.is_loc_frequency(mhz, **self._freq_kwargs)[0]
        if not ok:
            self._pending = None
            self.channel_khz = None
            self._flush()
            if self.state is not State.INVALID_FREQ:
                self._enter(State.INVALID_FREQ, reason)
                self.mode = None
                self._publish(None, 0, reason)
            return

        ch = freqrules.channel_khz(mhz, grid_khz=self.cfg.frequency["grid_khz"],
                                   tolerance_khz=self.cfg.frequency["snap_tolerance_khz"])
        if ch == self.channel_khz:
            # AMENDMENT A16: already tuned here, or on the way. Do NOT restart
            # the retune. `channel_khz` is cleared whenever we drop to IDLE or
            # INVALID_FREQ, so this comparison is sufficient on its own.
            #
            # The original condition also required `state in DECODING_STATES`,
            # which meant that a repeat of the SAME frequency while still TUNING
            # or ACQUIRING started the retune over. A bridge that re-asserts its
            # state periodically -- which is the normal design, and what the
            # mock bridge does at 2 Hz -- would then reset the demodulator
            # faster than it could ever settle, and the daemon would never
            # acquire at all. Found in the first end-to-end run against a live
            # socket; no unit test would have caught it, because unit tests set
            # the channel once.
            self._pending = None
            return
        # Debounce in integer kHz. A user spinning a tuning knob must not cause
        # a retune per detent.
        if self._pending is None or self._pending[0] != ch:
            self._pending = (ch, self.clock())

    def on_bridge_connected(self) -> None:
        self._bridge_lost_at = None

    def on_bridge_disconnected(self) -> None:
        if self._bridge_lost_at is None:
            self._bridge_lost_at = self.clock()

    # -- events from the SDR ----------------------------------------------- #

    def push(self, iq: np.ndarray, gap: bool = False) -> int:
        """Feed samples. Returns the number of observations published.

        Samples arriving in a non-decoding state are dropped on the floor. This
        is what guarantees that no bearing derived from station A can ever be
        published after a retune to station B (F3).
        """
        if self.state not in DECODING_STATES:
            return 0
        if self.recorder is not None and not gap:
            self.recorder.push(iq)
        published = 0
        if self.mode == "loc":
            # A33: only the demodulator of the tuned mode runs
            for obs in self.loc_demod.push(iq, gap=gap):
                if self._discard_next:
                    self._discard_next = False
                    continue
                self._handle_loc(obs)
                published += 1
            return published
        for obs in self.demod.push(iq, gap=gap):
            if self._discard_next:
                self._discard_next = False
                continue
            self._handle(obs)
            published += 1
        return published

    def _handle(self, obs) -> None:
        now = self.clock()

        # Raw per-window dump (--csv): every demodulated window, before any
        # gating, so the file mirrors what vor_replay.py produces off a capture.
        # Carries the tuned frequency alongside the observation.
        if self.obs_sink is not None:
            self.obs_sink(obs, self.channel_khz or 0)

        if obs.overrun:
            # F9: never decode across a sample gap.
            self._enter(State.NO_SIGNAL, "sample overrun in window")
            self._publish(self._last_good, 0, "sample overrun in window")
            return

        if not self.cfg.bearing.calibrated:
            # A2: an uncalibrated BEARING must never reach the bridge -- it is a
            # measurement plus an unknown constant, which is not a bearing. This
            # is the one field A20 still forces to 0, deliberately: `vorstatus`
            # cannot express "the number would be wrong by an unknown offset".
            # The signal measurements are published normally, because they are
            # correct and they are exactly what a builder needs while running
            # tools/vor_calibrate.py.
            self._last_q = self.quality.update(obs, None)
            self._last_obs = obs
            self._enter(State.NO_SIGNAL, "uncalibrated: run tools/vor_calibrate.py")
            self._publish(None, 0, self.last_reason)
            return

        bearing = self.tracker.update(obs.delta_phi)
        # AMENDMENT A15: `q_stab` must see the RAW bearing, not the smoothed one.
        # Smoothing is precisely what hides the jitter that q_stab exists to
        # detect: with smooth_n = 3 a +/-6 deg alternation arrives as +/-2 deg and
        # scores as stable. The smoothed value is what gets published; the raw
        # value is what gets judged.
        q = self.quality.update(obs, self.tracker.raw, tracking=True)
        self._last_q = q
        self._last_obs = obs

        if q.warming:
            # A14: not a signal failure, just not enough history yet. Staying in
            # ACQUIRING keeps `hold_s` from running against a healthy signal.
            # A20: the bearing goes out even so -- it exists, it is calibrated,
            # and `vorstatus` 3 says the receiver has not yet judged it.
            self._enter(State.ACQUIRING, q.reason)
            self._publish(to_int_bearing(bearing), 0, q.reason)
            return

        if q.flagged:
            self._consecutive_valid = 0
            # Capture the IQ that produced the flag, so the bearing can be
            # re-derived offline (F1). on_flag only; manual/always handled by
            # the daemon and the trigger config.
            self._maybe_capture(q.reason)
            self._enter(State.NO_SIGNAL, q.reason)
            self._publish(self._last_good, 0, q.reason)
            return

        self._consecutive_valid += 1
        self._last_good = to_int_bearing(bearing)
        self._last_good_at = now

        if self.state is State.ACQUIRING and self._consecutive_valid < 2:
            # Doc A: leave ACQUIRING only after two consecutive valid solutions.
            # A20: publish the bearing anyway; `vorstatus` 3 is what tells the
            # consumer it is not yet confirmed.
            self._publish(self._last_good, 0, "acquiring")
            return

        self._enter(State.TRACKING if q.value >= ACCEPTABLE else State.DEGRADED,
                    f"vorsignal {q.value}")
        self._publish(self._last_good, q.value, self.last_reason)

    def _handle_loc(self, obs) -> None:
        """One localizer window (A33). Mirrors `_handle`, with three
        differences: no calibration is involved (A2 concerns a phase constant
        a localizer does not have); validity comes from `LocQuality`, never
        from the DDM, because noise decodes as DDM ~ 0 (plan 4.1); and nothing
        is held in NO_SIGNAL -- `locdev` is 0 there, since a held deviation
        would be a needle that looks valid."""
        now = self.clock()
        if self.loc_obs_sink is not None:
            self.loc_obs_sink(obs, self.channel_khz or 0)
        self._last_loc_obs = obs
        if obs.overrun:
            self._loc_ddm = None
            self._enter(State.NO_SIGNAL, "sample overrun in window")
            self._publish(None, 0, "sample overrun in window")
            return
        raw = obs.ddm_corrected                 # D9: the published form
        self._loc_hist = (self._loc_hist + [raw])[-self.loc_cfg.smooth_n:]
        self._loc_ddm = float(np.mean(self._loc_hist))
        # A15: the raw value is judged, the smoothed one published.
        q = self.loc_quality.update(obs, raw)
        self._last_loc_q = q
        if q.warming:
            self._enter(State.ACQUIRING, q.reason)
            self._publish(None, 0, q.reason)
            return
        if q.flagged:
            self._consecutive_valid = 0
            self._maybe_capture(q.reason)
            self._enter(State.NO_SIGNAL, q.reason)
            self._publish(None, 0, q.reason)
            return
        self._consecutive_valid += 1
        self._last_good_at = now
        if self.state is State.ACQUIRING and self._consecutive_valid < 2:
            self._publish(None, 0, "acquiring")
            return
        # A38: +/-5 of hysteresis on the TRACKING/DEGRADED split. Without it
        # a steady 10 dB localizer, whose score sits on the split, flapped
        # between status 5 and 4 23 times a minute (measured), each a POST and
        # a label change on the plate.
        hold = LOC_SPLIT_HYSTERESIS if self.state in (State.TRACKING, State.DEGRADED) else 0
        tracking = (q.value >= ACCEPTABLE - hold if self.state is State.TRACKING
                    else q.value >= ACCEPTABLE + hold)
        self._enter(State.TRACKING if tracking else State.DEGRADED, f"locsignal {q.value}")
        self._publish(None, q.value, self.last_reason)

    def shutdown(self) -> None:
        """Clean stop (A33). With the localizer enabled, publish an idle
        payload -- locstatus 0 -- so the last thing on the bridge is not a
        usable status with a centred needle: RB-01 has no staleness timeout
        and rebroadcasts only changes. Disabled, nothing is published, as
        before. A killed daemon is not covered (open item O25)."""
        if not self.loc_enabled:
            return
        self._pending = None
        self.channel_khz = None
        self.mode = None
        self._flush()
        self._enter(State.IDLE, "daemon stopping")
        self._publish(None, 0, self.last_reason)

    def _maybe_capture(self, reason: str) -> None:
        if self.recorder is None or self.cfg.record["trigger"] != "on_flag":
            return
        import contextlib
        with contextlib.suppress(Exception):         # capture never stalls decoding
            path = self.recorder.trigger(reason=reason,
                                         extra={"channel_khz": self.channel_khz})
            if path is not None:
                self._last_iq_ref = path.name

    # -- timers ------------------------------------------------------------ #

    def tick(self) -> None:
        """Periodic housekeeping: debounce, settle, staleness, watchdog."""
        now = self.clock()
        if self.notify is not None:
            self.notify()

        # bridge loss -> release the SDR rather than keep transmitting to a
        # bridge we can no longer hear from (doc A section 10.4)
        if (self._bridge_lost_at is not None
                and now - self._bridge_lost_at >= self.cfg.bridge["ws_grace_s"]):
            if self.state is not State.IDLE:
                self.channel_khz = None
                self.mode = None
                self._pending = None
                self._flush()
                self._enter(State.IDLE, "bridge unreachable")  # releases the SDR
                self._publish(None, 0, self.last_reason)
            return

        if self._pending is not None:
            ch, at = self._pending
            if (now - at) * 1000.0 >= self.cfg.frequency["debounce_ms"]:
                self._pending = None
                self._retune(ch)
                return

        if self.state is State.NO_SDR and now >= self._sdr_retry_at:
            # Published only if it worked: a refused attempt changes nothing,
            # so the plate must not flash TUNING every retry_s.
            self._enter(State.TUNING, f"tuning {self.channel_khz / 1000:.3f} MHz")
            self._tune()
            if self.state is State.TUNING:
                self._publish(None, 0, "SDR available")
            return

        if self.state is State.TUNING and now >= self._settle_at:
            self._enter(State.ACQUIRING, "settled")
            self._publish(None, 0, "acquiring")
            return

        if self.state is State.IDLE and not self._published_idle:
            self._published_idle = True
            self._publish(None, 0, self.last_reason)
            return

        # staleness: hold the last bearing for hold_s, then forget it entirely
        if (self.state is State.NO_SIGNAL and self._last_good_at is not None
                and now - self._last_good_at >= self.cfg.publish["hold_s"]):
            self._flush()
            self._enter(State.ACQUIRING, "signal lost; re-acquiring")
            self._publish(None, 0, self.last_reason)

    def _retune(self, channel_khz: int) -> None:
        """Retune sequence. The invalidation goes out BEFORE the tuner moves.

        A20 made this ordering matter more, not less. Publishing first and
        entering TUNING afterwards -- the original order -- put the *old*
        station's SNR/AM/SUB on the wire behind `vorstatus` 5 with a radial of
        0: a usable status attached to a bearing that does not exist, pointing
        at magnetic north. Now TUNING (status 2) is entered and the previous
        station's measurements are flushed *before* the payload is built, so
        the retune POST says honestly "tuning, nothing measured yet" (F3).
        """
        self._enter(State.TUNING, f"tuning {channel_khz / 1000:.3f} MHz")
        self._flush()
        self.channel_khz = channel_khz
        # A33: the mode follows the channel, decided once per retune
        self.mode = ("loc" if self.loc_enabled and freqrules.is_loc_frequency(
            channel_khz / 1000.0, **self._freq_kwargs)[0] else "vor")
        if self.recorder is not None:
            self.recorder.set_channel(channel_khz * 1000)   # so a flag capture names its channel
        self._publish(None, 0, f"retune to {channel_khz / 1000:.3f} MHz")
        self._tune()

    def _tune(self) -> None:
        """Move the tuner, which claims the dongle if the daemon does not hold it.

        AMENDMENT A30: a dongle that cannot be had is a state, not a crash. One
        USB dongle is shared with rb-sdr-radio, so being refused is normal when
        the pilot selects a VOR with the radio on. The channel stays selected,
        `vorstatus` says there is nothing to use, and the dongle is tried again
        every `sdr.retry_s` until it is free or the channel is dropped.
        """
        try:
            self.sdr.set_frequency(self.channel_khz * 1000)
        except SdrError as e:
            if self.sdr_retry_s is None:
                raise
            self._sdr_retry_at = self.clock() + self.sdr_retry_s
            self._enter(State.NO_SDR, f"SDR unavailable: {e}")
            self._publish(None, 0, self.last_reason)
            return
        self._settle_at = self.clock() + self.cfg.sdr["settle_ms"] / 1000.0
