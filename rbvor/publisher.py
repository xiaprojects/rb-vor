"""REST producer for `POST /bridgeUpdate`.

Reference: rb-vor-architecture.md sections 10.2, 10.3, 10.4; failure mode F8;
implementation plan M7.

Split deliberately in two:

* `PublishPolicy` -- pure decision logic. Given a solution and a clock, decide
  whether to send and what the payload is. No I/O, no async, no network. This is
  where the interface contract lives, so it is where the tests live.
* `Publisher` -- glues the policy to a transport. The transport is injectable,
  so T5/T6 run without a network.

Wire format: RB-VOR-IF v2.2 (`docs/RB-VOR-IF-v2.2.md`), six integer fields --
`vorradial`, `vorsnr`, `voram`, `vorstatus`, `vorsub`, `vorfreqactive`. The
v1.0 pair `vordir`/`vorsignal` is gone from the wire (CLAUDE.md A20);
`vorsignal` lives on internally as the validity gate and in the solution log.
Since A26 (the 1.0.0 lineage's "payload is never zeroed") no field is gated by
validity: `vorstatus` (>= 4 usable) is the one thing a consumer reads for that.

Two rules that shape the design:

* **Drop, never queue** (doc A section 10.4). A late bearing is worse than no
  bearing: it describes where the aircraft was, presented as where it is. On
  failure the publisher retries once immediately and then backs off, discarding
  anything stale in the meantime. There is no unbounded queue anywhere in this
  module.
* **Transmit on change, plus an optional keep-alive** (IF v2.x section 3.4).
  A POST goes out only when a field changes; `publish.keepalive_s` re-sends an
  unchanged payload so a consumer can tell "steady" from "dead", and 0 disables
  it -- the spec then places the staleness timeout on the consumer (section 4,
  item 2). Doc A's periodic / on_change choice no longer exists.

Open item O4 (Q2) is unanswered, so both branches exist and are selected by
configuration: `publish.bearing_ref` is `from` or `to`. Q1 (`signal_scale`)
became moot with v2.0.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from rbvor.bearing import to_int_bearing

__all__ = ["PublishPolicy", "Publisher", "Transport", "RecordingTransport",
           "HttpTransport", "Decision"]

log = logging.getLogger("rbvor.publisher")


@dataclass(frozen=True)
class Decision:
    send: bool
    payload: dict[str, int] | None = None
    reason: str = ""


class Transport(Protocol):
    def send(self, payload: dict[str, int]) -> None:
        ...


class PublishPolicy:
    """Decides what goes on the wire, and when. Pure and clock-injected."""

    def __init__(self, cfg_publish: dict,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg_publish
        self.clock = clock
        self._last_sent_at: float | None = None
        self._last_payload: dict[str, int] | None = None
        self._last_state: str | None = None

    def reset(self) -> None:
        self._last_sent_at = None
        self._last_payload = None
        self._last_state = None

    # -- payload shaping --------------------------------------------------- #

    def _shape(self, vordir: int | None, snr_db: float, am_depth: float,
               valid: bool, status: int = 0, sub_level: float = 0.0,
               freq_khz: int = 0) -> dict[str, int]:
        """Build the v2.2 payload: six integer fields.

        vorradial : radial in degrees (0-359).
        vorsnr    : 30 Hz tone SNR in whole dB.
        voram     : 30 Hz AM depth x100 (0.283 -> 28).
        vorstatus : 0 idle/off/no-freq, 1 no-signal, 2 tuning, 3 acquiring,
                    4 degraded, 5 tracking. Reflects the state machine.
        vorsub    : 9960 Hz subcarrier level x100 (like voram).
        vorfreqactive : currently tuned VOR channel in kHz (109400 = 109.400 MHz);
                    0 when no channel is selected or the frequency was rejected.

        A26 (the 1.0.0 lineage's "A20") -- no field is gated by validity any
        more. Every field carries the last measurement the receiver actually
        made, and `vorstatus` is the single field that says whether the bearing
        may be used. Zeroing the trio on the producer side destroyed
        information the consumer needs (a NO_SIGNAL payload could not be
        distinguished from an idle one without reading `vorstatus` anyway) and
        gave a second, weaker validity channel that consumers were free to
        disagree with.

        A field reads 0 now only when there is genuinely nothing to report --
        no observation yet, or none since the last retune. `valid` is still
        accepted because the controller and the solution log use it internally,
        but it no longer shapes the wire format.
        """
        radial = 0 if vordir is None else to_int_bearing(
            vordir + (180 if self.cfg["bearing_ref"] == "to" else 0))
        return {
            "vorradial": int(radial),
            "vorsnr": max(0, int(round(snr_db))),
            "voram": max(0, int(round(am_depth * 100))),
            "vorstatus": int(status),
            "vorsub": max(0, int(round(sub_level * 100))),
            "vorfreqactive": int(freq_khz),
        }

    # -- decision ---------------------------------------------------------- #

    def decide(self, vordir: int | None, snr_db: float = 0.0,
               am_depth: float = 0.0, *, state: str, valid: bool = True,
               status: int = 0, sub_level: float = 0.0, freq_khz: int = 0,
               transition: bool = False) -> Decision:
        now = self.clock()
        payload = self._shape(vordir, snr_db, am_depth, valid, status, sub_level,
                              freq_khz)

        # Requirement 5: transmit only when a parameter changes. A state
        # transition (retune, flag raised/cleared) always sends, since the
        # payload it produces differs from the previous one anyway; sending it
        # explicitly guarantees the consumer sees the change immediately rather
        # than at the next natural difference.
        if transition or (self._last_state is not None and state != self._last_state):
            self._last_state = state
            if payload != self._last_payload:
                self._commit(now, payload, state)
                return Decision(True, payload, "state transition")
            # transition but identical payload (e.g. flag->flag): keep-alive only
        self._last_state = state

        if self._last_payload is None:
            self._commit(now, payload, state)
            return Decision(True, payload, "first solution")

        if payload != self._last_payload:
            self._commit(now, payload, state)
            return Decision(True, payload, "changed")

        # Nothing changed. Optional keep-alive so a consumer can distinguish a
        # steady reading from a dead daemon.
        keepalive = self.cfg.get("keepalive_s", 0)
        if keepalive and now - (self._last_sent_at or 0.0) >= keepalive - 1e-9:
            self._commit(now, payload, state)
            return Decision(True, payload, "keepalive")
        return Decision(False, None, "unchanged")

    def _commit(self, now: float, payload: dict, state: str) -> None:
        self._last_sent_at = now
        self._last_payload = payload
        self._last_state = state


class RecordingTransport:
    """Test double. Also useful for `--dry-run` on a bench.

    `echo=True` prints each payload as it is sent, so `--dry-run` shows every
    bearing live instead of only a summary at shutdown.
    """

    def __init__(self, fail_times: int = 0, echo: bool = False):
        self.sent: list[dict] = []
        self.attempts = 0
        self.fail_times = fail_times
        self.echo = echo

    def send(self, payload: dict[str, int]) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise ConnectionError("simulated bridge failure")
        self.sent.append(dict(payload))
        if self.echo:
            radial = payload.get("vorradial", 0)
            snr = payload.get("vorsnr", 0)
            am = payload.get("voram", 0)
            st = payload.get("vorstatus", 0)
            sub = payload.get("vorsub", 0)
            fk = payload.get("vorfreqactive", 0)
            names = {0: "idle", 1: "no-sig", 2: "tuning", 3: "acquir",
                     4: "degrad", 5: "track"}
            stn = names.get(st, str(st))
            fq = f"{fk/1000:.3f}" if fk else "  ---  "
            # A26: every field is always populated, so the line always shows
            # the measurements. `vorstatus` is what says whether the radial is
            # usable -- flagged with a trailing '?' so the bench operator reads
            # the same gate the consumer applies.
            usable = "" if st >= 4 else " ?"
            print(f"  [{stn:6}] {fq} MHz  radial {radial:3d}\u00b0{usable}  "
                  f"snr {snr:2d} dB  am {am/100:.2f}  sub {sub/100:.2f}",
                  flush=True)


class Publisher:
    """Implements `controller.PublisherPort`."""

    def __init__(self, cfg_publish: dict, transport: Transport,
                 clock: Callable[[], float] = time.monotonic):
        self.policy = PublishPolicy(cfg_publish, clock=clock)
        self.transport = transport
        self.clock = clock
        self.dropped = 0
        self.failures = 0
        self._backoff_until = 0.0
        self._backoff = 0.0

    def reset(self) -> None:
        self.policy.reset()

    def publish(self, vordir: int | None, snr_db: float = 0.0,
                am_depth: float = 0.0, *, state: str, valid: bool = True,
                status: int = 0, sub_level: float = 0.0, freq_khz: int = 0,
                reason: str = "") -> None:
        d = self.policy.decide(vordir, snr_db, am_depth, state=state, valid=valid,
                               status=status, sub_level=sub_level,
                               freq_khz=freq_khz)
        if not d.send:
            return
        now = self.clock()
        if now < self._backoff_until:
            # F8: drop rather than queue. A bearing delivered late is worse than
            # one never delivered.
            self.dropped += 1
            return
        try:
            self.transport.send(d.payload)
            self._backoff = 0.0
            self._backoff_until = 0.0
        except Exception as e:                       # noqa: BLE001 - transport agnostic
            try:
                self.transport.send(d.payload)       # one immediate retry
                self._backoff = 0.0
                self._backoff_until = 0.0
                return
            except Exception:                        # noqa: BLE001
                pass
            self.failures += 1
            self._backoff = min(max(self._backoff * 2.0, 0.5), 30.0)
            self._backoff_until = now + self._backoff
            log.warning("bridge POST failed (%s); backing off %.1f s",
                        e, self._backoff)


class HttpTransport:
    """aiohttp POST, driven from a background event loop.

    `send()` is synchronous by contract because the controller is synchronous.
    The coroutine is handed to the running loop and not awaited: the DSP thread
    must never block on the network (doc A section 6).
    """

    def __init__(self, url: str, timeout_s: float,
                 loop: asyncio.AbstractEventLoop | None = None):
        self.url = url
        self.timeout_s = timeout_s
        self.loop = loop
        self._session: Any = None
        self._inflight = 0
        self.max_inflight = 2

    async def start(self) -> None:
        import aiohttp
        self.loop = self.loop or asyncio.get_running_loop()
        self._session = aiohttp.ClientSession(
            timeout=__import__("aiohttp").ClientTimeout(total=self.timeout_s))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def send(self, payload: dict[str, int]) -> None:
        if self._session is None or self.loop is None:
            raise ConnectionError("HttpTransport not started")
        if self._inflight >= self.max_inflight:
            # Bounded in-flight requests: the alternative is an unbounded queue
            # of stale bearings, which doc A 10.4 forbids.
            raise ConnectionError("bridge is not keeping up")
        self._inflight += 1
        asyncio.run_coroutine_threadsafe(self._post(payload), self.loop)

    async def _post(self, payload: dict[str, int]) -> None:
        try:
            async with self._session.post(self.url, json=payload) as r:
                if r.status >= 400:
                    log.warning("bridge returned HTTP %d", r.status)
        except Exception as e:                       # noqa: BLE001
            log.warning("bridge POST error: %s", e)
        finally:
            self._inflight -= 1
