"""L5 end to end: the daemon's real `_run()` against `tools/mock_bridge.py`
over a live socket, the bridge re-asserting its state at 2 Hz and alternating
VOR, localizer and OFF (0), with a stand-in dongle that radiates a VOR on
109.400, a localizer on 109.500 and noise anywhere else, in real time.

A16 was found only this way: unit tests set the channel once, a bridge
re-asserts it twice a second. Every POST is checked with the mock bridge's
own v2.3 validator. ~25 s of wall clock.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FS = 256_000
OFF = -FS // 4
LOC_DDM = 0.05


def _loop(gen_blocks, params, seconds=4.0):
    return np.concatenate(list(gen_blocks(params)))


class AirDevice:
    """Real-time stand-in: what the antenna hears depends on the tuned frequency."""

    name = "air"
    tuner = ""

    def __init__(self):
        from tools.loc_gen import LocParams
        from tools.loc_gen import generate_blocks as loc_blocks
        from tools.vor_gen import VorParams
        from tools.vor_gen import generate_blocks as vor_blocks
        # 4 s loops hold whole cycles of 30, 90, 150 and 9960 Hz: no seam
        self.air = {
            109_400_000: _loop(vor_blocks, VorParams(radial_deg=137, fs=FS, offset_hz=OFF,
                                                     seconds=4.0, snr_db=25.0, ident="",
                                                     level=0.3)),
            109_500_000: _loop(loc_blocks, LocParams(ddm=LOC_DDM, fs=FS, offset_hz=OFF,
                                                     seconds=4.0, snr_db=25.0, ident="",
                                                     level=0.3)),
        }
        self.rng = np.random.default_rng(7)
        self.freq = 0
        self.pos = 0
        self.tunes = []
        self.closed = 0

    def set_frequency(self, hz):
        self.freq = int(hz) - OFF           # back to the navaid frequency
        self.tunes.append(self.freq)

    def read(self, n):
        time.sleep(n / FS)
        sig = self.air.get(self.freq)
        if sig is None:
            z = (self.rng.normal(0, 0.05, n) + 1j * self.rng.normal(0, 0.05, n))
            return z.astype(np.complex64)
        idx = (self.pos + np.arange(n)) % sig.size
        self.pos = (self.pos + n) % sig.size
        return sig[idx]

    def close(self):
        self.closed += 1


def test_daemon_against_the_mock_bridge_alternating_vor_loc_and_off(tmp_path, monkeypatch):
    from aiohttp import web

    from rbvor import sdr as sdr_mod
    from rbvor.__main__ import _run
    from rbvor.config import load
    from tools import mock_bridge

    device = AirDevice()
    monkeypatch.setattr(sdr_mod, "open_device", lambda cfg, order=None: device)
    mock_bridge.RECEIVED.clear()

    # VOR, then a localizer, OFF the way RB-01 sends it, the localizer again
    script = "109.4@0,109.5@9,0@17,109.5@20"

    async def scenario():
        app = mock_bridge.build(argparse.Namespace(script=script, period=0.5,
                                                   fail_rate=0.0, slow=0.0))
        runner = web.AppRunner(app, shutdown_timeout=0.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        cfg = json.loads((ROOT / "config" / "rb-vor.json").read_text())
        cfg["sdr"].update(sample_rate_hz=FS, tuner_offset_hz=OFF, block_samples=16384)
        cfg["bearing"].update(phase_sign=1, phase_offset_deg=-1.9657)
        cfg["log"]["solution_csv"] = str(tmp_path / "vor.csv")
        cfg["loc"] = {"enabled": True}
        cfg["bridge"].update(ws_url=f"ws://127.0.0.1:{port}/bridge",
                             rest_url=f"http://127.0.0.1:{port}/bridgeUpdate")
        path = tmp_path / "e2e.json"
        path.write_text(json.dumps(cfg))
        args = argparse.Namespace(dry_run=False, replay=None, freq=None, quiet=True,
                                  status_every=2.0, log_level=None, csv=None)
        daemon = asyncio.create_task(_run(load(path), args))
        await asyncio.sleep(26.0)
        assert not daemon.done(), daemon.exception()
        daemon.cancel()                       # the clean stop
        await asyncio.gather(daemon, return_exceptions=True)
        await asyncio.sleep(0.2)
        await runner.cleanup()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))

    posts = list(mock_bridge.RECEIVED)
    assert posts, "nothing reached the bridge"
    bad = [(p, mock_bridge.validate(p)) for p in posts if mock_bridge.validate(p)]
    assert not bad, bad[:3]

    # one tune per channel change, however often the bridge re-asserted (A16)
    assert device.tunes == [109_400_000, 109_500_000, 109_500_000], device.tunes

    vor_track = [p for p in posts if p["vorstatus"] == 5]
    loc_track = [p for p in posts if p["locstatus"] == 5]
    assert vor_track and all(p["locfreqactive"] == 0 for p in vor_track)
    assert abs(vor_track[-1]["vorradial"] - 137) <= 2
    assert loc_track and all(p["vorfreqactive"] == 0 for p in loc_track)
    assert all(abs(p["locdev"] - round(100 * LOC_DDM / 0.155)) <= 3 for p in loc_track)

    # OFF (vorfreq 0) released the dongle and zeroed both halves
    off = [p for p in posts if p["vorstatus"] == 0 and p["locstatus"] == 0
           and p["vorfreqactive"] == 0 and p["locfreqactive"] == 0]
    assert off and device.closed >= 1

    # the localizer was tracked again after OFF, then the clean stop idled it
    i_off = posts.index(off[0])
    assert any(p["locstatus"] == 5 for p in posts[i_off:])
    assert posts[-1]["locstatus"] == 0 and posts[-1]["locdev"] == 0

    # the localizer's solution log is its own file, next to the VOR one
    loc_csv = (tmp_path / "loc.csv").read_text().splitlines()
    assert loc_csv[0].startswith("t_utc,t_mono,channel_khz,state,locstatus,ddm")
    assert len(loc_csv) > 5
