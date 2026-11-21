"""G6 end to end: the daemon's real `_run()` against `tools/mock_bridge.py`
over a live socket, the bridge re-asserting its state at 2 Hz (A16 was found
only this way), stand-in dongles radiating a localizer on 109.500 and its
glide path on 332.600 in real time. Every POST is checked with the mock
bridge's own v2.4 validator.

Two arrangements (docs/gs-plan.md 6.1-6.2):
- one dongle: the pilot switches GS and back through `gsselect`;
- two dongles listed in `sdr.devices` by serial: both axes together, the
  allocation choosing which dongle is which.

~45 s of wall clock.
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
LOC_HZ, GS_HZ = 109_500_000, 332_600_000
LOC_DDM, GS_DDM = 0.05, 0.0875


def _air():
    from tools.gs_gen import GsParams
    from tools.loc_gen import LocParams, generate_blocks
    # 4 s loops hold whole cycles of 90 and 150 Hz: no seam
    return {
        LOC_HZ: np.concatenate(list(generate_blocks(LocParams(
            ddm=LOC_DDM, fs=FS, offset_hz=OFF, seconds=4.0, snr_db=25.0, ident="",
            level=0.3)))),
        GS_HZ: np.concatenate(list(generate_blocks(GsParams(
            ddm=GS_DDM, fs=FS, offset_hz=OFF, seconds=4.0, snr_db=25.0, level=0.3)))),
    }


class AirDevice:
    """Real time: what this dongle hears depends on where it is tuned."""

    tuner = ""

    def __init__(self, air, name="air", seed=7):
        self.air, self.name = air, name
        self.rng = np.random.default_rng(seed)
        self.freq = 0
        self.pos = 0
        self.tunes: list[int] = []
        self.closed = 0

    def set_frequency(self, hz):
        self.freq = int(hz) - OFF
        self.tunes.append(self.freq)

    def read(self, n):
        time.sleep(n / FS)
        sig = self.air.get(self.freq)
        if sig is None:
            return (self.rng.normal(0, 0.05, n)
                    + 1j * self.rng.normal(0, 0.05, n)).astype(np.complex64)
        idx = (self.pos + np.arange(n)) % sig.size
        self.pos = (self.pos + n) % sig.size
        return sig[idx]

    def close(self):
        self.closed += 1


def _scenario(tmp_path, script, seconds, cfg_edit):
    from aiohttp import web

    from rbvor.__main__ import _run
    from rbvor.config import load
    from tools import mock_bridge

    mock_bridge.RECEIVED.clear()

    async def go():
        app = mock_bridge.build(argparse.Namespace(script=script, period=0.5,
                                                   fail_rate=0.0, slow=0.0))
        runner = web.AppRunner(app, shutdown_timeout=0.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        cfg = json.loads((ROOT / "config" / "rb-vor.json").read_text())
        cfg["sdr"].update(sample_rate_hz=FS, tuner_offset_hz=OFF, block_samples=16384)
        cfg["log"]["solution_csv"] = str(tmp_path / "vor.csv")
        cfg["loc"] = {"enabled": True}
        cfg["gs"] = {"enabled": True}
        cfg["bridge"].update(ws_url=f"ws://127.0.0.1:{port}/bridge",
                             rest_url=f"http://127.0.0.1:{port}/bridgeUpdate")
        cfg_edit(cfg)
        path = tmp_path / "e2e.json"
        path.write_text(json.dumps(cfg))
        args = argparse.Namespace(dry_run=False, replay=None, freq=None, quiet=True,
                                  status_every=2.0, log_level=None, csv=None)
        daemon = asyncio.create_task(_run(load(path), args))
        await asyncio.sleep(seconds)
        assert not daemon.done(), daemon.exception()
        daemon.cancel()
        await asyncio.gather(daemon, return_exceptions=True)
        await asyncio.sleep(0.2)
        await runner.cleanup()

    asyncio.run(asyncio.wait_for(go(), timeout=seconds + 30))
    posts = list(mock_bridge.RECEIVED)
    assert posts, "nothing reached the bridge"
    bad = [(p, mock_bridge.validate(p)) for p in posts if mock_bridge.validate(p)]
    assert not bad, bad[:3]
    return posts


def test_one_dongle_pilot_switches_to_the_glide_slope_and_back(tmp_path, monkeypatch):
    from rbvor import sdr as sdr_mod
    device = AirDevice(_air())
    monkeypatch.setattr(sdr_mod, "open_device", lambda cfg, order=None: device)
    posts = _scenario(tmp_path, "109.5@0,gs=1@8,gs=0@17", 25.0, lambda cfg: None)

    # one tune per switch, however often the bridge re-asserted
    assert device.tunes == [LOC_HZ, GS_HZ, LOC_HZ], device.tunes
    assert device.closed == 0 or device.tunes[-1] == LOC_HZ

    loc_trk = [i for i, p in enumerate(posts) if p["locstatus"] == 5]
    gs_trk = [i for i, p in enumerate(posts) if p["gsstatus"] == 5]
    assert loc_trk and gs_trk
    assert all(posts[i]["gsshared"] == 1 for i in loc_trk + gs_trk)
    assert all(posts[i]["locfreqactive"] == 0 and posts[i]["locdev"] == 0 for i in gs_trk)
    assert all(abs(posts[i]["gsdev"] - round(100 * GS_DDM / 0.175)) <= 4 for i in gs_trk)
    # never both needles usable at once on one dongle
    assert not any(p["locstatus"] >= 4 and p["gsstatus"] >= 4 for p in posts)
    # back on the localizer at the end, then the clean stop idled everything
    assert loc_trk[-1] > gs_trk[-1]
    assert posts[-1]["gsstatus"] == 0 and posts[-1]["locstatus"] == 0
    gs_csv = (tmp_path / "gs.csv").read_text().splitlines()
    assert gs_csv[0].startswith("t_utc,t_mono,channel_khz,loc_channel_khz,state,gsstatus")


def test_two_listed_dongles_receive_both_axes_together(tmp_path, monkeypatch):
    from rbvor import __main__ as main_mod
    from rbvor import sdr as sdr_mod
    air = _air()
    devices = {"rbnav1": AirDevice(air, "rbnav1"), "rbgs1": AirDevice(air, "rbgs1", 8)}
    # USB order deliberately the reverse of the list: allocation follows the list
    monkeypatch.setattr(main_mod, "rtl_serials", lambda: ["stratux:1090", "rbgs1", "rbnav1"])
    monkeypatch.setattr(sdr_mod, "open_device",
                        lambda cfg, order=None: devices[cfg["device_serial"]])

    def edit(cfg):
        cfg["sdr"]["devices"] = [{"device_serial": "rbnav1", "use": ["nav", "gs"]},
                                 {"device_serial": "rbgs1", "use": ["gs"]}]

    posts = _scenario(tmp_path, "109.5@0,gs=1@8", 16.0, edit)
    assert devices["rbnav1"].tunes == [LOC_HZ]
    assert devices["rbgs1"].tunes == [GS_HZ]
    both = [p for p in posts if p["locstatus"] == 5 and p["gsstatus"] == 5]
    assert both, "never both axes at once"
    assert all(p["gsshared"] == 0 for p in both)
    assert all(p["gsfreqactive"] == 332_600 and p["locfreqactive"] == 109_500 for p in both)
    assert posts[-1]["gsstatus"] == 0
    assert devices["rbgs1"].closed >= 1             # released at the clean stop
