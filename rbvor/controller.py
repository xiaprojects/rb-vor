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
import math
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
from rbvor.sdr import GAIN_STEP, SdrError

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


def gain_policy_for(state: State) -> str:
    """A47: what automatic gain may do in this state.

    TRACKING: "protect" -- only a severely clipping ADC is stepped down, since
    every step costs the windows that span it. Looking for a signal (ACQUIRING,
    NO_SIGNAL) or holding a weak one (DEGRADED): "free". Not decoding: "hold".
    """
    if state is State.TRACKING:
        return "protect"
    if state in (State.ACQUIRING, State.NO_SIGNAL, State.DEGRADED):
        return "free"
    return "hold"


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


class GsReceiver:
    """The glide slope's own receiver (A40, docs/gs-plan.md 6.3).

    One glide-path channel on one port, with its own state, demodulator and
    quality, so the localizer and the glide slope can each fail alone (plan
    4.1). The same object serves both arrangements:

    * *dedicated*: the port is the glide-slope dongle and the localizer runs
      beside it on the nav dongle;
    * *shared*: the port is the nav dongle itself, lent while the pilot has
      the glide slope selected; the controller's own state mirrors this one.

    It mirrors `Controller._handle_loc` window by window -- validity from the
    signal, never from the DDM (noise decodes as DDM ~ 0) -- and the
    controller's TUNING / NO_SDR / hold timers. Every event calls `on_event`,
    and the controller publishes one payload carrying all the fields.
    """

    def __init__(self, gs_cfg, clock: Callable[[], float], settle_s: float,
                 hold_s: float, on_event: Callable[[str], None],
                 obs_sink=None, before_retry: Callable[[], bool] | None = None,
                 recorder=None):
        from rbvor.gs import GsDemod
        self.cfg = gs_cfg
        self.clock = clock
        self.settle_s = settle_s
        self.hold_s = hold_s
        self.on_event = on_event
        self.obs_sink = obs_sink
        #: Called before each NO_SDR retry; False abandons the attempt (the
        #: controller has changed the arrangement, A40).
        self.before_retry = before_retry
        #: A46: the glide slope's own RingRecorder (or None), fed this
        #: receiver's IQ and dumped on a glide-slope flag.
        self.recorder = recorder
        self.last_iq_ref = ""
        self.demod = GsDemod(gs_cfg.dsp)
        self.quality = LocQuality(gs_cfg.quality)
        self.port: SdrPort | None = None
        self.release = True
        self.state = State.IDLE
        self.reason = "idle"
        self.channel_khz: int | None = None
        self._settle_at = 0.0
        self._retry_at = 0.0
        self._last_window_at = 0.0
        self._flush()

    #: A45: seconds without a single window, while decoding, before the
    #: glide slope declares that it hears nothing. Six hops at the shipped
    #: 0.5 s; well clear of the 1 s window plus the filter fill after a retune.
    STARVED_S = 3.0

    @property
    def active(self) -> bool:
        return self.state is not State.IDLE

    def _flush(self) -> None:
        self.demod.reset()
        self.quality.reset()
        self.hist: list[float] = []
        self.ddm: float | None = None
        self.last_obs = None
        self.last_q = None
        self._consecutive_valid = 0
        self._last_good_at: float | None = None
        self._discard_next = True

    def _enter(self, state: State, reason: str) -> None:
        if state != self.state:
            self.state = state
            self.reason = reason
            if state in (State.IDLE, State.NO_SDR) and self.release and self.port is not None:
                try:
                    self.port.close()
                except Exception as e:               # noqa: BLE001
                    log.debug("GS SDR release: %s", e)

    def start(self, gs_khz: int, port: SdrPort, release: bool) -> None:
        """Retune to a glide-path channel: TUNING and flush before the payload
        is built (A26's order), then move the tuner."""
        self.port, self.release = port, release
        self.state = State.TUNING                    # no release on the way through
        self.reason = f"tuning glide path {gs_khz / 1000:.2f} MHz"
        self._flush()
        self.channel_khz = gs_khz
        if self.recorder is not None:
            self.recorder.reset()            # the old channel's IQ is not evidence here
            self.recorder.set_channel(gs_khz * 1000)
        self.on_event(self.reason)
        self._tune()

    def stop(self, release: bool | None = None) -> None:
        """Back to IDLE. `release` False keeps a shared dongle open for the
        localizer that is about to retune it. Publishes nothing: the caller
        publishes the payload that results."""
        if release is not None:
            self.release = release
        self.channel_khz = None
        self._flush()
        self._enter(State.IDLE, "glide slope not selected")
        self.release = True

    def _tune(self) -> None:
        try:
            self.port.set_frequency(self.channel_khz * 1000)
        except SdrError as e:
            self._retry_at = self.clock() + self.cfg.retry_s
            self._enter(State.NO_SDR, f"glide-slope SDR unavailable: {e}")
            self.on_event(self.reason)
            return
        self._settle_at = self.clock() + self.settle_s

    def tick(self) -> None:
        now = self.clock()
        if self.state is State.NO_SDR and now >= self._retry_at:
            if self.before_retry is not None and not self.before_retry():
                return
            self.state = State.TUNING
            self.reason = f"tuning glide path {self.channel_khz / 1000:.2f} MHz"
            self._tune()
            if self.state is State.TUNING:
                self.on_event("glide-slope SDR available")
            return
        if self.state is State.TUNING and now >= self._settle_at:
            self._enter(State.ACQUIRING, "settled")
            self._last_window_at = now
            self.on_event("acquiring")
            return
        if (self.state in (State.ACQUIRING, State.TRACKING, State.DEGRADED)
                and now - self._last_window_at >= self.STARVED_S):
            # A45: no samples -- a dongle pulled or wedged mid-reception (the
            # A30 gap). Without this the last usable gsdev would stay on the
            # bridge as a valid marker for as long as the silence lasted.
            self.ddm = None
            self._consecutive_valid = 0
            self._enter(State.NO_SIGNAL, "no samples from the glide-slope dongle")
            self._last_good_at = now
            self.on_event(self.reason)
            return
        if (self.state is State.NO_SIGNAL and self._last_good_at is not None
                and now - self._last_good_at >= self.hold_s):
            self._flush()
            self._enter(State.ACQUIRING, "signal lost; re-acquiring")
            self.on_event(self.reason)

    def push(self, iq: np.ndarray, gap: bool = False) -> int:
        if self.state not in DECODING_STATES:
            return 0
        if self.recorder is not None and not gap:
            self.recorder.push(iq)
        n = 0
        for obs in self.demod.push(iq, gap=gap):
            self._last_window_at = self.clock()
            if self._discard_next:
                self._discard_next = False
                continue
            self._handle(obs)
            n += 1
        return n

    def _handle(self, obs) -> None:
        now = self.clock()
        if self.obs_sink is not None:
            self.obs_sink(obs, self.channel_khz or 0)
        self.last_obs = obs
        if obs.overrun:
            self.ddm = None
            self._enter(State.NO_SIGNAL, "sample overrun in window")
            self.on_event(self.reason)
            return
        raw = obs.ddm_corrected                       # A42: the published form
        self.hist = (self.hist + [raw])[-self.cfg.smooth_n:]
        self.ddm = float(np.mean(self.hist))
        q = self.quality.update(obs, raw)             # A15: raw judged
        self.last_q = q
        if q.warming:
            self._enter(State.ACQUIRING, q.reason)
            self.on_event(q.reason)
            return
        if q.flagged:
            self._consecutive_valid = 0
            self._maybe_capture(q.reason)
            self._enter(State.NO_SIGNAL, q.reason)
            self.on_event(q.reason)
            return
        self._consecutive_valid += 1
        self._last_good_at = now
        if self.state is State.ACQUIRING and self._consecutive_valid < 2:
            self.on_event("acquiring")
            return
        hold = LOC_SPLIT_HYSTERESIS if self.state in (State.TRACKING, State.DEGRADED) else 0
        tracking = (q.value >= ACCEPTABLE - hold if self.state is State.TRACKING
                    else q.value >= ACCEPTABLE + hold)
        self._enter(State.TRACKING if tracking else State.DEGRADED, f"gssignal {q.value}")
        self.on_event(self.reason)

    def _maybe_capture(self, reason: str) -> None:
        if self.recorder is None or self.cfg_trigger != "on_flag":
            return
        import contextlib
        with contextlib.suppress(Exception):         # capture never stalls decoding
            path = self.recorder.trigger(reason=f"glide slope: {reason}",
                                         extra={"channel_khz": self.channel_khz})
            if path is not None:
                self.last_iq_ref = path.name

    #: record.trigger, set by the controller; only on_flag is implemented (D6).
    cfg_trigger = "on_flag"

    def wire(self, shared: bool):
        from rbvor.publisher import GsWire
        if self.channel_khz is None:
            return GsWire(shared=shared)
        o = self.last_obs
        status = Controller._STATUS_CODE.get(self.state, 0)
        return GsWire(status=status,
                      ddm=self.ddm if o is not None else None,
                      sdm=o.sdm if o is not None else 0.0,
                      snr_db=o.snr_db if o is not None else 0.0,
                      freq_khz=self.channel_khz,
                      usable=status >= 4 and self.ddm is not None,
                      shared=shared)


class Controller:
    """Owns the demodulator, the bearing tracker and the quality estimator."""

    def __init__(self, cfg, sdr: SdrPort, publisher: PublisherPort,
                 clock: Callable[[], float] = time.monotonic,
                 notify: Callable[[], None] | None = None,
                 recorder=None, solution_sink=None, obs_sink=None,
                 sdr_retry_s: float | None = None,
                 loc_solution_sink=None, loc_obs_sink=None,
                 gs_sdr: SdrPort | None = None,
                 allocate: Callable[[], bool] | None = None,
                 gs_solution_sink=None, gs_obs_sink=None, gs_recorder=None):
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

        # A39-A44: the glide slope. Built only when enabled (it requires the
        # localizer, config validation says so). `gs_sdr` is a second dongle;
        # `allocate()` answers, at every tune attempt, whether the glide slope
        # has a dongle of its own (True) or shares the nav one (False), and
        # rebinds the readers as a side effect (__main__ does that from
        # `sdr.allocate`). Without it: dedicated exactly when gs_sdr is given.
        gs_cfg = cfg.gs if hasattr(cfg, "gs") else None
        self.gs_enabled = bool(self.loc_enabled and gs_cfg and gs_cfg.enabled)
        self.gs_sdr = gs_sdr
        self.allocate = allocate
        self.gs_solution_sink = gs_solution_sink
        self.gs_rx: GsReceiver | None = None
        if self.gs_enabled:
            self.gs_rx = GsReceiver(gs_cfg, clock, cfg.sdr["settle_ms"] / 1000.0,
                                    cfg.publish["hold_s"], self._on_gs_event,
                                    obs_sink=gs_obs_sink,
                                    before_retry=self._gs_before_retry,
                                    recorder=gs_recorder)
            self.gs_rx.cfg_trigger = cfg.record["trigger"]
        self._gs_dedicated = False   # arrangement decided at the last tune attempt
        self._gs_want = 0            # pilot's selection on a shared receiver
        self._gs_select_last: int | None = None
        self._gs_select_at: float | None = None

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

        # A47: windows still to drop silently after an automatic gain step.
        self._gain_discard = 0
        if hasattr(sdr, "gain_policy"):
            sdr.gain_policy = self._gain_policy
        if gs_sdr is not None and hasattr(gs_sdr, "gain_policy") and self.gs_rx is not None:
            gs_sdr.gain_policy = lambda: gain_policy_for(self.gs_rx.state)

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

    def _publish_with_loc(self, vordir, vorsignal: int, reason: str,
                          source: str = "main") -> None:
        """`_publish` when the localizer is enabled: the v2.3 payload, in which
        a LOC channel reads as a rejected frequency in the vor* fields. With
        the glide slope enabled (A44) the gs* half rides in the same POST, and
        a shared receiver with the glide slope selected (mode "gs") reads as
        a LOC channel does. `source` "gs" is a glide-slope event: it goes on
        the wire with everything else, but into the glide-slope log only."""
        loc = self._loc_wire()
        extra = {"gs": self._gs_wire()} if self.gs_enabled else {}
        why = reason or self.last_reason
        if self.mode in ("loc", "gs"):
            self.publisher.publish(None, 0.0, 0.0, state=str(self.state), valid=False,
                                   status=0, sub_level=0.0, freq_khz=0,
                                   reason=why, loc=loc, **extra)
            if source == "main" and self.mode == "loc" and self.loc_solution_sink is not None:
                self._emit_loc_solution(why)
            if source == "gs" and self.gs_solution_sink is not None:
                self._emit_gs_solution(why)
            return
        valid = vorsignal > 0 and vordir is not None
        o = self._last_obs
        self.publisher.publish(vordir, o.snr_db if o else 0.0, o.amp_a if o else 0.0,
                               state=str(self.state), valid=valid,
                               status=self._STATUS_CODE.get(self.state, 0),
                               sub_level=o.sub_level if o else 0.0,
                               freq_khz=self.channel_khz if self.channel_khz else 0,
                               reason=why, loc=loc, **extra)
        if self.solution_sink is not None:
            self._emit_solution(vordir, vorsignal, why)

    # -- glide slope (A39-A44) --------------------------------------------- #

    def _gs_paired_khz(self) -> int | None:
        """The glide path of the selected channel, if it is a localizer."""
        if not self.gs_enabled or self.mode not in ("loc", "gs"):
            return None
        return freqrules.gs_channel_khz(self.channel_khz)

    def _gs_shared(self) -> bool:
        """`gsshared`: the selected ILS's LOC and GS must share one receiver."""
        return self._gs_paired_khz() is not None and not self._gs_dedicated

    def _gs_wire(self):
        from rbvor.publisher import GsWire
        shared = self._gs_shared()
        rx = self.gs_rx
        if rx is not None and rx.channel_khz is not None and (
                self.mode == "gs" or (self.mode == "loc" and self._gs_dedicated)):
            return rx.wire(shared)
        return GsWire(shared=shared)

    def _gs_plan(self) -> bool:
        """Ask the allocation whether the glide slope has its own dongle.
        Re-evaluated at every tune attempt: dongles are hot-plugged and taken
        by the radio (plan 6.1). A failure keeps the last answer."""
        if self.allocate is None:
            return self.gs_sdr is not None
        try:
            return bool(self.allocate())
        except SdrError as e:
            log.warning("dongle allocation failed: %s", e)
            return self._gs_dedicated

    def _gs_port(self) -> SdrPort:
        return self.gs_sdr if self._gs_dedicated else self.sdr

    def _on_gs_event(self, reason: str) -> None:
        if self.mode == "gs":
            # shared receiver, glide slope selected: the controller's own state
            # is the glide slope's, for the journal and the dashboard
            self.state, self.last_reason = self.gs_rx.state, self.gs_rx.reason
        self._publish_with_loc(None, 0, reason, source="gs")

    def _gs_before_retry(self) -> bool:
        """Before a glide-slope NO_SDR retry: is the arrangement unchanged?
        A dedicated GS dongle that has gone away turns the receiver shared."""
        if self.mode != "loc" or not self._gs_dedicated:
            return True
        if self._gs_plan():
            return True
        self._gs_dedicated = False
        self.gs_rx.stop(release=True)
        self._publish_with_loc(None, 0, "glide slope now shares the nav dongle", source="gs")
        return False

    def _gs_stop(self) -> None:
        """The selected channel is no longer this ILS: GS off, its dongle freed."""
        if self.gs_rx is not None and self.gs_rx.active:
            self.gs_rx.stop(release=self._gs_dedicated)

    def _gs_start_dedicated(self) -> None:
        gs_khz = self._gs_paired_khz()
        if gs_khz is not None and self._gs_dedicated:
            self.gs_rx.start(gs_khz, self.gs_sdr, release=True)

    def set_gs_select(self, value) -> None:
        """A `gsselect` value arrived: 0 localizer, 1 glide slope (plan 6.2).

        Edge-triggered: only a value different from the last one received
        counts, so a bridge re-asserting its whole state (A16) cannot undo the
        reset to LOC that a new `vorfreq` makes. The first value after start
        is an edge, so a restarted daemon returns to the pilot's choice (A31).
        Debounced like a frequency; acted on only for a shared receiver on an
        ILS with a glide path, read and ignored otherwise."""
        if value is None:
            return
        try:
            v = int(value)
        except (TypeError, ValueError):
            log.warning("gsselect is not 0 or 1: %r", value)
            return
        if v not in (0, 1):
            log.warning("gsselect is not 0 or 1: %r", value)
            return
        if v == self._gs_select_last:
            return
        self._gs_select_last = v
        self._gs_want = v
        self._gs_select_at = self.clock()

    def _apply_gs_select(self) -> None:
        self._gs_select_at = None
        if self._pending is not None or self._gs_paired_khz() is None:
            return                     # a retune will read _gs_want itself
        want = "gs" if self._gs_want else "loc"
        if want == self.mode:
            return                     # A16: already there, or on the way
        if self._gs_dedicated:
            return                     # both run; nothing to switch
        self._gs_dedicated = self._gs_plan()
        if self._gs_dedicated:
            # A second dongle has appeared since the last tune (plan 6.3 table):
            # no switch is needed any more; the glide slope gets its own.
            if self.mode == "gs":
                self._switch_to_loc("glide slope moved to its own dongle")
            self._gs_start_dedicated()
            return
        if want == "gs":
            self._switch_to_gs()
        else:
            self._switch_to_loc("localizer selected")

    def _switch_to_gs(self) -> None:
        """Shared receiver: localizer -> glide slope. The localizer goes dark
        in the same POST that says the glide slope is tuning (plan 4.5)."""
        self.state, self.last_reason = State.TUNING, "switching to the glide slope"
        self._flush()                  # the localizer's DDM dies here
        self.mode = "gs"
        self.gs_rx.start(self._gs_paired_khz(), self.sdr, release=True)

    def _switch_to_loc(self, reason: str) -> None:
        """Shared receiver: glide slope -> localizer, by the same rule."""
        self.gs_rx.stop(release=False)  # the localizer retunes this dongle now
        self.state, self.last_reason = State.TUNING, reason
        self._flush()
        self.mode = "loc"
        self._publish(None, 0, f"retune to {self.channel_khz / 1000:.3f} MHz")
        self._tune()

    def push_gs(self, iq: np.ndarray, gap: bool = False) -> int:
        """Samples from the dedicated glide-slope dongle."""
        if self.gs_rx is None or self.mode != "loc" or not self._gs_dedicated:
            return 0
        return self.gs_rx.push(iq, gap=gap)

    def _emit_gs_solution(self, reason: str) -> None:
        import contextlib
        from datetime import datetime

        from rbvor.solutionlog import GsSolutionRecord
        rx = self.gs_rx
        q, o = rx.last_q, rx.last_obs
        wire = self._gs_wire()
        rec = GsSolutionRecord(
            t_utc=datetime.now(tz=UTC).isoformat(), t_mono=self.clock(),
            channel_khz=rx.channel_khz, loc_channel_khz=self.channel_khz,
            state=str(rx.state), gsstatus=wire.status,
            ddm=rx.ddm if wire.usable else None, valid=wire.usable, shared=wire.shared,
            reason=reason,
            q_sdm=q.q_sdm if q else 0.0, q_snr=q.q_snr if q else 0.0,
            q_stab=q.q_stab if q else 0.0, value=q.value if q else 0,
            m90=o.m90 if o else 0.0, m150=o.m150 if o else 0.0,
            ddm_raw=o.ddm if o else 0.0, ddm_corrected=o.ddm_corrected if o else 0.0,
            sdm=o.sdm if o else 0.0, snr_db=o.snr_db if o else 0.0,
            tone_offset=o.tone_offset if o else 0.0, carrier=o.carrier if o else 0.0,
            carrier_offset_hz=o.carrier_offset_hz if o else 0.0,
            acquired_hz=getattr(o, "acquired_hz", 0.0) if o else 0.0,
            gps_track=self._gps_track, overrun=o.overrun if o else False,
            iq_ref=rx.last_iq_ref,
        )
        with contextlib.suppress(Exception):         # a log failure never stalls a solution
            self.gs_solution_sink(rec)

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

    def _gain_policy(self) -> str:
        if self.mode == "gs" and self.gs_rx is not None:
            return gain_policy_for(self.gs_rx.state)
        return gain_policy_for(self.state)

    def _gain_step_windows(self) -> int:
        """A47: windows that may contain samples from both sides of a gain step,
        or from the carrier estimate settling after it."""
        dsp = self.loc_cfg.dsp if self.mode == "loc" else self.cfg.dsp
        settle = float((self.cfg.sdr.get("auto_gain") or {}).get("settle_s", 1.5))
        return math.ceil((dsp.integration_s + settle) / dsp.hop_s)

    def _flush(self) -> None:
        """Drop everything derived from the previous signal."""
        self._gain_discard = 0
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
            self._gs_want = 0
            if self.state is not State.IDLE:
                self._gs_stop()
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
            self._gs_stop()
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
            # plan 6.2: a new station always starts on the localizer, whatever
            # the bridge still holds for gsselect (edge-triggered, see there)
            self._gs_want = 0

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
        if self.mode == "gs":
            # A40: shared receiver, glide slope selected. The glide slope
            # records into its own ring (A46), never into the nav one.
            return self.gs_rx.push(iq, gap=gap)
        if self.state not in DECODING_STATES:
            return 0
        if gap is GAIN_STEP:
            # A47: an automatic gain step. The samples are continuous, so the
            # ring keeps them, and the windows spanning the step are dropped
            # without the NO_SIGNAL a real overrun means (F9).
            self._gain_discard = self._gain_step_windows()
        if self.recorder is not None and (not gap or gap is GAIN_STEP):
            self.recorder.push(iq)
        published = 0
        if self.mode == "loc":
            # A33: only the demodulator of the tuned mode runs
            for obs in self.loc_demod.push(iq, gap=gap):
                if self._discard_next:
                    self._discard_next = False
                    continue
                if self._gain_discard:
                    self._gain_discard -= 1
                    continue
                self._handle_loc(obs)
                published += 1
            return published
        for obs in self.demod.push(iq, gap=gap):
            if self._discard_next:
                self._discard_next = False
                continue
            if self._gain_discard:
                self._gain_discard -= 1
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
        self._gs_stop()
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
        if self.gs_rx is not None and self.gs_rx.active:
            self.gs_rx.tick()

        # bridge loss -> release the SDR rather than keep transmitting to a
        # bridge we can no longer hear from (doc A section 10.4)
        if (self._bridge_lost_at is not None
                and now - self._bridge_lost_at >= self.cfg.bridge["ws_grace_s"]):
            if self.state is not State.IDLE:
                self._gs_stop()
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

        if (self._gs_select_at is not None
                and (now - self._gs_select_at) * 1000.0 >= self.cfg.frequency["debounce_ms"]):
            self._apply_gs_select()
            return

        if self.mode == "gs":
            return            # the glide-slope receiver runs its own timers

        if self.state is State.NO_SDR and now >= self._sdr_retry_at:
            if self._gs_paired_khz() is not None and not self.gs_rx.active:
                # A40: a tune attempt re-reads the allocation (and rebinds the
                # nav reader as a side effect)
                self._gs_dedicated = self._gs_plan()
            # Published only if it worked: a refused attempt changes nothing,
            # so the plate must not flash TUNING every retry_s.
            self._enter(State.TUNING, f"tuning {self.channel_khz / 1000:.3f} MHz")
            self._tune()
            if self.state is State.TUNING:
                self._publish(None, 0, "SDR available")
                if self._gs_dedicated and not self.gs_rx.active:
                    self._gs_start_dedicated()
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
        self._gs_stop()
        self._enter(State.TUNING, f"tuning {channel_khz / 1000:.3f} MHz")
        self._flush()
        self.channel_khz = channel_khz
        # A33: the mode follows the channel, decided once per retune
        self.mode = ("loc" if self.loc_enabled and freqrules.is_loc_frequency(
            channel_khz / 1000.0, **self._freq_kwargs)[0] else "vor")
        if self.recorder is not None:
            self.recorder.set_channel(channel_khz * 1000)   # so a flag capture names its channel
        if self._gs_paired_khz() is not None:
            # A40: which arrangement, decided at every tune attempt. A shared
            # receiver starts on the localizer unless the pilot's selection
            # says otherwise (a restarted daemon reading it back, A31).
            self._gs_dedicated = self._gs_plan()
            if not self._gs_dedicated and self._gs_want:
                self.mode = "gs"
                self.state, self.last_reason = State.TUNING, "tuning the glide slope"
                self.gs_rx.start(self._gs_paired_khz(), self.sdr, release=True)
                return
        self._publish(None, 0, f"retune to {channel_khz / 1000:.3f} MHz")
        self._tune()
        if self._gs_dedicated:
            # each axis fails alone: a nav dongle that will not open does not
            # stop the glide slope, nor the reverse (plan 6.3)
            self._gs_start_dedicated()

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
