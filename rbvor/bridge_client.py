"""WebSocket consumer for `ws://localhost/bridge`.

Reference: rb-vor-architecture.md section 10.1; failure mode F8; implementation
plan M7.

RB-VOR is a read-only client here. It sends no frames at all -- not even
protocol pings, which RB-01 does not answer (CLAUDE.md A29) -- and it takes
exactly two fields from the bridge state:

===========  ==========  ==================================================
field        unit        use
===========  ==========  ==================================================
`vorfreq`    MHz         tuning command
`gpsTrack`   deg true    **diagnostics and logging only** in v1.0
===========  ==========  ==================================================

`vorradial`, `vorsnr`, `voram`, `vorstatus` and `vorsub` appearing in an
inbound frame are echoes of our own output and are ignored. Everything else is
ignored too, silently: the bridge carries traffic for the whole platform and
will grow fields that are none of our business.

The parse is pure and separately testable. The async loop around it is thin on
purpose -- it does reconnection and the state read below, and nothing else.

The socket says nothing on connect, and pushes the state only when a POST changes
a value (CLAUDE.md A31). So after every connection the state is read once with a
`GET` of `bridge.rest_url`, which is the same JSON as a frame. Without it a
restarted daemon stays deaf to the channel RB-01 already holds, and pressing that
station again changes no value, so nothing is pushed.

The link is lost only when the socket is. RB-01 has no ping/pong and does not
echo anything, close frames included, so the `websockets` keepalive (ping every
20 s, drop when no pong comes back) tore down a healthy connection every ~50 s
with `1011 keepalive ping timeout`, and every shutdown waited 10 s for a
closing handshake that never comes. Both are off. What still notices a bridge
host that vanished without closing -- power cut, WiFi drop -- is kernel TCP
keepalive: the peer's kernel answers those probes, not RB-01, so they ask
nothing of the protocol. Without it a pure reader on a dead socket waits
forever, and is still waiting after the bridge comes back.

A note on `gpsTrack`. It is tempting to use it (antenna-pattern compensation,
plausibility checks against a station database). It is not used in v1.0 and a
test asserts it never reaches `rbvor.bearing`. Anything that silently mixes an
aircraft-attitude source into a radio navigation measurement is a failure mode
waiting to be discovered in flight, and it would need its own hazard analysis
before it earns a place in the bearing path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["ParsedFrame", "parse_frame", "BridgeClient", "enable_tcp_keepalive"]

log = logging.getLogger("rbvor.bridge")

#: Fields we consume. Everything else in the bridge state is not our business.
CONSUMED = ("vorfreq", "gpsTrack")

#: How long a close waits for the peer's close frame. RB-01 never sends one, so
#: this is pure delay on every stop: the library default is 10 s, and the legacy
#: client (websockets < 14) spends up to three of them.
CLOSE_TIMEOUT_S = 0.5

#: Kernel TCP keepalive: a vanished peer is declared dead after
#: idle + interval * count = 25 s of silence, then `ws_grace_s` applies.
KEEPALIVE_IDLE_S = 10
KEEPALIVE_INTERVAL_S = 5
KEEPALIVE_COUNT = 3


@dataclass(frozen=True)
class ParsedFrame:
    """Result of parsing one bridge frame.

    `None` means "absent, no change" -- explicitly not "zero" and not "invalid".
    `error` is set for a frame that could not be parsed at all; such a frame is
    dropped without disconnecting, because one malformed message is not a reason
    to tear down a working connection.
    """

    vorfreq: float | None = None
    gps_track: float | None = None
    error: str | None = None
    raw_keys: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None


def _num(v: Any) -> float | None:
    """Accept int/float, reject bool and everything else."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def parse_frame(text: str | bytes) -> ParsedFrame:
    """Parse one bridge frame. Never raises."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return ParsedFrame(error="frame is not valid UTF-8")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        return ParsedFrame(error=f"invalid JSON: {e}")
    if not isinstance(data, dict):
        return ParsedFrame(error=f"expected an object, got {type(data).__name__}")

    track = _num(data["gpsTrack"]) if "gpsTrack" in data else None
    freq = None
    # Three distinct cases, and conflating any two of them is a bug:
    #   absent  -> no change, keep the current channel
    #   null    -> a command: no channel selected, go idle
    #   junk    -> the sender is confused; say so rather than silently keeping
    #              the old channel or silently going idle
    if "vorfreq" in data and data["vorfreq"] is not None:
        freq = _num(data["vorfreq"])
        if freq is None:
            return ParsedFrame(gps_track=track, raw_keys=tuple(data),
                               error=f"vorfreq is not a number: "
                                     f"{data['vorfreq']!r}")
    return ParsedFrame(vorfreq=freq, gps_track=track, raw_keys=tuple(data))


def enable_tcp_keepalive(sock) -> bool:
    """Turn on kernel keepalive probes. Never raises.

    Returns False when there is no socket or the kernel refuses; the link still
    works, it just cannot notice a peer that vanished without closing.
    Per-socket timing knobs are Linux names; elsewhere the system defaults
    apply, which are slower (typically two hours) but still finite.
    """
    if sock is None:
        return False
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", KEEPALIVE_IDLE_S),
                            ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL_S),
                            ("TCP_KEEPCNT", KEEPALIVE_COUNT)):
            opt = getattr(socket, name, None)
            if opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
    except OSError as e:
        log.warning("bridge socket: TCP keepalive not enabled (%s); a silently "
                    "vanished bridge will not be noticed", e)
        return False
    return True


class BridgeClient:
    """Async WebSocket client with exponential backoff.

    `on_channel` receives a frequency in MHz or None. `on_connect` and
    `on_disconnect` drive the controller's grace timer. `fetch_state` returns
    the bridge's whole state as text; None skips the read, for a dry run.
    """

    def __init__(self, cfg_bridge: dict,
                 on_channel: Callable[[float | None], None],
                 on_connect: Callable[[], None] | None = None,
                 on_disconnect: Callable[[], None] | None = None,
                 on_gps_track: Callable[[float], None] | None = None,
                 fetch_state: Callable[[], Awaitable[str]] | None = None):
        self.cfg = cfg_bridge
        self.on_channel = on_channel
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_gps_track = on_gps_track
        self.fetch_state = fetch_state
        self.connected = False
        self.frames = 0
        self.bad_frames = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def handle_text(self, text: str) -> ParsedFrame:
        """Apply one frame. Separated from the socket so tests need no server."""
        f = parse_frame(text)
        self.frames += 1
        if not f.ok:
            self.bad_frames += 1
            log.warning("dropping bridge frame: %s", f.error)
            return f
        if f.gps_track is not None and self.on_gps_track is not None:
            self.on_gps_track(f.gps_track)
        if "vorfreq" in f.raw_keys:
            self.on_channel(f.vorfreq)
        return f

    async def run(self) -> None:
        import websockets
        backoff = self.cfg["ws_reconnect_min_s"]
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.cfg["ws_url"],
                                              ping_interval=None,
                                              close_timeout=CLOSE_TIMEOUT_S) as ws:
                    enable_tcp_keepalive(ws.transport.get_extra_info("socket"))
                    self.connected = True
                    if self.on_connect:
                        self.on_connect()
                    log.info("bridge connected: %s", self.cfg["ws_url"])
                    # Subscribed first, so a change during the GET cannot be
                    # missed, and awaited before reading, so its answer cannot
                    # overwrite a newer frame. A failure is a failed connection:
                    # going on without the state is the deafness this read
                    # exists to end, so it reconnects and reads again.
                    if self.fetch_state is not None:
                        self.handle_text(await self.fetch_state())
                    backoff = self.cfg["ws_reconnect_min_s"]
                    async for message in ws:
                        if self._stop.is_set():
                            break
                        self.handle_text(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:                   # noqa: BLE001
                log.warning("bridge connection lost: %s", e)
            finally:
                if self.connected:
                    self.connected = False
                    if self.on_disconnect:
                        self.on_disconnect()
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, self.cfg["ws_reconnect_max_s"])
