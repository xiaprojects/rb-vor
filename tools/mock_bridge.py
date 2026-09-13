#!/usr/bin/env python3
"""Mock RB-Avionics bridge: WebSocket state feed plus a REST sink.

Reference: implementation plan M7 (T5, T6).

Stands in for the real bridge so the daemon can be exercised end to end without
one, including the failure paths that are hard to produce on demand: dropped
connections, HTTP 500s, and a slow endpoint.

    ./mock_bridge.py --script "109.4@0,113.35@8,null@16" --port 8099
    ./mock_bridge.py --fail-rate 0.3 --slow 1.5
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import time
from pathlib import Path

from aiohttp import web

RECEIVED: list[dict] = []


def parse_script(text: str) -> list[tuple[float, float | None]]:
    """`"109.4@0,113.35@8,null@16"` -> [(0.0, 109.4), (8.0, 113.35), (16.0, None)]."""
    out = []
    for item in filter(None, (x.strip() for x in text.split(","))):
        value, _, when = item.partition("@")
        freq = None if value.lower() in ("null", "none", "") else float(value)
        out.append((float(when or 0.0), freq))
    return sorted(out)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    script = request.app["script"]
    period = request.app["period"]
    print(f"  [ws] client connected; script has {len(script)} step(s)")
    t0 = time.monotonic()
    idx = 0
    current: float | None = None
    track = 0.0
    try:
        while not ws.closed:
            now = time.monotonic() - t0
            while idx < len(script) and script[idx][0] <= now:
                current = script[idx][1]
                print(f"  [ws] t={now:5.1f}s  vorfreq -> {current}")
                idx += 1
            track = (track + 3.0) % 360.0
            state = {"vorfreq": current, "gpsTrack": round(track, 1),
                     "vorradial": 0, "vorsnr": 0, "voram": 0, "vorstatus": 0,
                     "vorsub": 0, "vorfreqactive": 0}
            await ws.send_str(json.dumps(state))
            await asyncio.sleep(period)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    print("  [ws] client gone")
    return ws


async def update_handler(request: web.Request) -> web.Response:
    app = request.app
    if app["slow"]:
        await asyncio.sleep(app["slow"])
    if app["fail_rate"] and random.random() < app["fail_rate"]:
        print("  [rest] injected failure -> 500")
        return web.Response(status=500, text="injected failure")
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="not JSON")

    problems = []
    expect = {"vorradial", "vorsnr", "voram", "vorstatus", "vorsub", "vorfreqactive"}
    if set(body) != expect:
        problems.append(f"payload keys are {sorted(body)}, expected "
                        f"{sorted(expect)} exactly")
    for k in expect:
        if k in body and not isinstance(body[k], int):
            problems.append(f"{k} is {type(body[k]).__name__}, expected int")
    if isinstance(body.get("vorradial"), int) and not 0 <= body["vorradial"] <= 359:
        problems.append(f"vorradial {body['vorradial']} out of range 0..359")
    if isinstance(body.get("vorsnr"), int) and not 0 <= body["vorsnr"] <= 120:
        problems.append(f"vorsnr {body['vorsnr']} out of range 0..120 dB")
    if isinstance(body.get("voram"), int) and not 0 <= body["voram"] <= 100:
        problems.append(f"voram {body['voram']} out of range 0..100 (AM x100)")
    if isinstance(body.get("vorstatus"), int) and body["vorstatus"] not in range(6):
        problems.append(f"vorstatus {body['vorstatus']} not in 0..5")
    if isinstance(body.get("vorsub"), int) and not 0 <= body["vorsub"] <= 100:
        problems.append(f"vorsub {body['vorsub']} out of range 0..100 (SUB x100)")
    if isinstance(body.get("vorfreqactive"), int) and body["vorfreqactive"] != 0 \
            and not 108000 <= body["vorfreqactive"] <= 118000:
        problems.append(f"vorfreqactive {body['vorfreqactive']} not 0 or in "
                        "108000..118000 kHz (VOR airband)")

    RECEIVED.append(body)
    flag = "  <-- " + "; ".join(problems) if problems else ""
    print(f"  [rest] {json.dumps(body)}{flag}")
    return web.json_response({"ok": not problems})


async def dump_handler(request: web.Request) -> web.Response:
    return web.json_response(RECEIVED)


def build(args) -> web.Application:
    app = web.Application()
    app["script"] = parse_script(args.script)
    app["period"] = args.period
    app["fail_rate"] = args.fail_rate
    app["slow"] = args.slow
    app.router.add_get("/bridge", ws_handler)
    app.router.add_post("/bridgeUpdate", update_handler)
    app.router.add_get("/received", dump_handler)
    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--script", default="109.4@0",
                    help='e.g. "109.4@0,109.5@5,113.35@10,null@20"')
    ap.add_argument("--period", type=float, default=0.5,
                    help="seconds between state frames")
    ap.add_argument("--fail-rate", type=float, default=0.0)
    ap.add_argument("--slow", type=float, default=0.0)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)

    print(f"mock bridge on http://localhost:{a.port}")
    print(f"  ws   ws://localhost:{a.port}/bridge")
    print(f"  rest http://localhost:{a.port}/bridgeUpdate")
    with contextlib.suppress(KeyboardInterrupt):
        web.run_app(build(a), port=a.port, print=None)
    if a.out:
        a.out.write_text(json.dumps(RECEIVED, indent=2) + "\n")
        print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
