"""WebSocket consumer for `ws://localhost/bridge`.

Reference: rb-vor-architecture.md section 10.1; failure mode F8; implementation
plan M7.

RB-VOR is a read-only client here. It sends no frames except protocol pings, and
it takes exactly two fields from the bridge state:

===========  ==========  ==================================================
field        unit        use
===========  ==========  ==================================================
`vorfreq`    MHz         tuning command
`gpsTrack`   deg true    **diagnostics and logging only** in v1.0
===========  ==========  ==================================================

`vordir` and `vorsignal` appearing in an inbound frame are echoes of our own
output and are ignored. Everything else is ignored too, silently: the bridge
carries traffic for the whole platform and will grow fields that are none of our
business.

The parse is pure and separately testable. The async loop around it is thin on
purpose -- it does reconnection and nothing else.

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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["ParsedFrame", "parse_frame", "BridgeClient"]

log = logging.getLogger("rbvor.bridge")

#: Fields we consume. Everything else in the bridge state is not our business.
CONSUMED = ("vorfreq", "gpsTrack")


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


class BridgeClient:
    """Async WebSocket client with exponential backoff.

    `on_channel` receives a frequency in MHz or None. `on_connect` and
    `on_disconnect` drive the controller's grace timer.
    """

    def __init__(self, cfg_bridge: dict,
                 on_channel: Callable[[float | None], None],
                 on_connect: Callable[[], None] | None = None,
                 on_disconnect: Callable[[], None] | None = None,
                 on_gps_track: Callable[[float], None] | None = None):
        self.cfg = cfg_bridge
        self.on_channel = on_channel
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_gps_track = on_gps_track
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
                async with websockets.connect(self.cfg["ws_url"]) as ws:
                    self.connected = True
                    backoff = self.cfg["ws_reconnect_min_s"]
                    if self.on_connect:
                        self.on_connect()
                    log.info("bridge connected: %s", self.cfg["ws_url"])
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
