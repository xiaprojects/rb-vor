"""RB-VOR daemon entry point.

Wires the pieces together and does nothing else: all behaviour lives in
`controller`, `publisher`, `bridge_client` and `sdr`, so the parts that matter
stay testable without a network or a radio.

    python3 -m rbvor --config /etc/rb-vor/rb-vor.json
    python3 -m rbvor --config ... --replay capture.cu8 --dry-run
    python3 -m rbvor --config ... --freq 109.4 --dry-run   # no bridge needed
    python3 -m rbvor --self-test                           # no hardware needed
"""

from __future__ import annotations

# isort: off
# MUST be first: caps BLAS/OpenMP thread pools before numpy is imported, or the
# DSP pegs every core busy-waiting. See rbvor/_threadcaps.py.
from rbvor import _threadcaps  # noqa: E402,F401

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import numpy as np
# isort: on

from rbvor import __version__
from rbvor.bridge_client import BridgeClient
from rbvor.config import ConfigError, load
from rbvor.controller import Controller
from rbvor.publisher import HttpTransport, Publisher, RecordingTransport
from rbvor.record import IQMeta, RingRecorder
from rbvor.sdnotify import SdNotify
from rbvor.sdr import SdrError, SdrReader
from rbvor.solutionlog import SolutionLogger

log = logging.getLogger("rbvor")


class _ReplaySdr:
    """Stand-in for --replay, where the file supplies the samples."""

    def __init__(self) -> None:
        self.tuned: list[int] = []

    def set_frequency(self, nav_hz: int) -> None:
        self.tuned.append(nav_hz)
        log.info("tune %.3f MHz (replay)", nav_hz / 1e6)

    def close(self) -> None:
        pass


async def _run(cfg, args) -> int:
    transport = RecordingTransport(echo=True) if args.dry_run else HttpTransport(
        cfg.bridge["rest_url"], cfg.bridge["http_timeout_s"])
    if isinstance(transport, HttpTransport):
        await transport.start()

    publisher = Publisher(cfg.publish, transport)
    sd = SdNotify()
    live = args.replay is None
    sdr = SdrReader(cfg.sdr) if live else _ReplaySdr()

    # M9: forensic logging. Both optional; the controller is identical without
    # them. The solution CSV path comes from config; the IQ recorder is built
    # only when record.enabled is set.
    sol_logger = SolutionLogger(cfg.log["solution_csv"])
    recorder = None
    if cfg.record["enabled"]:
        iq_meta = IQMeta(format=cfg.record["format"],
                         sample_rate_hz=cfg.sdr["sample_rate_hz"],
                         center_freq_hz=0, nav_freq_hz=0,
                         tuner_offset_hz=cfg.sdr["tuner_offset_hz"])
        recorder = RingRecorder(iq_meta, cfg.record["dir"],
                                max_seconds=cfg.record["max_seconds"],
                                fmt=cfg.record["format"])

    # 1.0.0: raw per-window decode dump (--csv), the live counterpart of what
    # vor_replay.py writes off a capture, with the tuned channel per row.
    obs_sink = None
    if args.csv:
        from rbvor.obscsv import ObsCsvSink
        obs_sink = ObsCsvSink(args.csv)
        print(f"  dumping per-window decode to {args.csv}", flush=True)

    # The bridge client is only started when the channel actually comes from the
    # bridge. Bench mode (--freq) and replay both supply the channel themselves,
    # so starting the client would only flood the log with connection-refused
    # warnings against a bridge that need not exist. The same test decides what a
    # dongle that cannot be had means: under a bridge another program may hold it
    # and the pilot may free it (A30); on a bench nothing will, so it is exit 3 (F3).
    channel_from_bridge = args.freq is None and args.replay is None

    controller = Controller(cfg, sdr, publisher, notify=sd.watchdog,
                            recorder=recorder,
                            solution_sink=sol_logger.write,
                            obs_sink=obs_sink,
                            sdr_retry_s=(cfg.sdr.get("retry_s", 5.0)
                                         if channel_from_bridge else None))

    if not cfg.calibrated:
        log.warning("bearing calibration is not set: NO bearing will be published. "
                    "Run tools/vor_calibrate.py (amendment A2, open item O1).")
    if cfg.sdr["tuner_offset_hz"] == 0:
        log.warning("sdr.tuner_offset_hz is 0: the carrier sits on the tuner's DC "
                    "spur. Expect diluted AM depth and low SNR (CLAUDE.md F2); "
                    "use -fs/4 (amendment A1).")
    if cfg.sdr.get("swap_iq"):
        log.warning("sdr.swap_iq is set: a standard RTL-SDR does not need it since "
                    "amendment A22 (the old 'mirror' was our own sign convention). "
                    "Confirm with `vor_replay.py --survey` on a capture from this "
                    "dongle, or the band will now be mirrored the other way.")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # A task that dies takes the daemon down with it. Found in the field (F3):
    # with no openable SDR, `tick()` raised SdrError inside the ticker task, the
    # exception was never awaited, and the process idled headless for ten
    # minutes reporting `blocks_read: 0`. Under systemd the watchdog would have
    # masked it; on a bench nothing recovers. So every task is watched, the
    # first failure stops the loop, and it is re-raised after cleanup so
    # `main()` can turn it into an exit code.
    tasks: list[asyncio.Task] = []
    failures: list[BaseException] = []

    def _watch(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        e = t.exception()
        if e is not None:
            log.error("%s task failed: %s", t.get_name(), e)
            failures.append(e)
            stop.set()

    def spawn(coro, name: str) -> None:
        t = asyncio.create_task(coro, name=name)
        t.add_done_callback(_watch)
        tasks.append(t)
    if args.freq is not None:
        log.info("bench mode: fixed frequency %.3f MHz, bridge input ignored",
                 args.freq)
        controller.set_channel(args.freq)
    elif channel_from_bridge:
        def _set_track(track):
            controller._gps_track = track      # logged only; never in the bearing path
        client = BridgeClient(cfg.bridge, controller.set_channel,
                              on_connect=controller.on_bridge_connected,
                              on_disconnect=controller.on_bridge_disconnected,
                              on_gps_track=_set_track,
                              fetch_state=(transport.fetch if isinstance(
                                  transport, HttpTransport) else None))
        spawn(client.run(), "bridge")

    sd.ready(f"config {cfg.source}")

    # Startup banner: the facts that determine whether anything can work.
    print("-" * 64, flush=True)
    print(f"  rb-vor {__version__}", flush=True)
    print(f"  config      {cfg.source}", flush=True)
    print(f"  sample rate {cfg.sdr['sample_rate_hz']} Hz, "
          f"offset {cfg.sdr['tuner_offset_hz']:+d} Hz", flush=True)
    print(f"  calibrated  {'YES' if cfg.calibrated else 'NO -- will publish flags only'}",
          flush=True)
    print(f"  mode        {'replay' if args.replay else ('bench' if args.freq else 'bridge')}"
          f"{'  (dry-run)' if args.dry_run else ''}", flush=True)
    if _threadcaps._CHANGED:
        print(f"  threads     BLAS/OpenMP capped to 1 "
              f"({len(_threadcaps._CHANGED)} vars)", flush=True)
    print("-" * 64, flush=True)

    def _status_line() -> str:
        c = controller
        q = c._last_q
        bearing = ("---" if c._last_good is None or c.state in
                   ("IDLE", "TUNING", "INVALID_FREQ") else f"{c._last_good:3d}\u00b0")
        qterms = (f"am {q.q_am:.2f} sub {q.q_sub:.2f} snr {q.q_snr:.2f} "
                  f"stab {q.q_stab:.2f}" if q else "am -- sub -- snr -- stab --")
        vs = q.value if q else 0
        ch = f"{c.channel_khz/1000:.3f}" if c.channel_khz else "-----"
        sdr_bit = ""
        if live:
            s = sdr.stats
            sdr_bit = (f" | sdr blk {s['blocks_read']} ovr {s['overruns']} "
                       f"err {s['read_errors']} q {s['queued']}")
        o = getattr(c, "_last_obs", None)
        afc = (f"  afc {o.carrier_offset_hz:+.0f} Hz"
               if o is not None and abs(o.carrier_offset_hz) >= 50 else "")
        return (f"[{c.state:11s}] {ch} MHz  brg {bearing}  sig {vs:3d}  "
                f"{qterms}{afc}{sdr_bit}")

    async def ticker() -> None:
        last = ""
        last_status_print = 0.0
        while not stop.is_set():
            controller.tick()
            status = f"{controller.state}: {controller.last_reason}"
            if status != last:
                log.info("%s", status)
                sd.status(status)
                last = status
            # periodic one-line dashboard, on by default; --quiet turns it off
            now = loop.time()
            if not args.quiet and now - last_status_print >= args.status_every:
                print(_status_line(), flush=True)
                last_status_print = now
            await asyncio.sleep(0.1)

    spawn(ticker(), "ticker")

    if live:
        async def pump() -> None:
            last_report = loop.time()
            while not stop.is_set():
                got = False
                for block, gap in sdr.blocks():
                    controller.push(block, gap=gap)
                    got = True
                if loop.time() - last_report >= 30.0:
                    log.info("sdr %s", sdr.stats)
                    last_report = loop.time()
                await asyncio.sleep(0.0 if got else 0.02)
        spawn(pump(), "pump")
    else:
        async def replay() -> None:
            from rbvor.record import iq_blocks, read_sidecar
            meta = read_sidecar(args.replay)
            swap = bool(cfg.sdr.get("swap_iq", False))
            if (meta.synthetic is not None and "carrier_at" not in meta.synthetic
                    and not swap):
                log.warning("%s was generated before amendment A22 and has the "
                            "carrier on the wrong side of centre; set sdr.swap_iq "
                            "for this file or regenerate it", args.replay)
            if args.freq is None:
                controller.set_channel(meta.nav_freq_hz / 1e6)
            for _ in range(12):
                controller.tick()
                await asyncio.sleep(0.1)
            for blk in iq_blocks(args.replay,
                                 block_samples=cfg.sdr["block_samples"], meta=meta):
                controller.push(np.conj(blk) if swap else blk)
                await asyncio.sleep(0)
            await asyncio.sleep(0.5)
            stop.set()
        spawn(replay(), "replay")

    try:
        await stop.wait()
    finally:
        sd.stopping()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if live:
            sdr.close()
            log.info("sdr final %s", sdr.stats)
        sol_logger.close()
        if obs_sink is not None:
            obs_sink.close()
            log.info("wrote %d rows to %s", obs_sink.rows, args.csv)
        if recorder is not None:
            recorder.flush(timeout=10.0)     # let a capture in flight finish its sidecar
        if isinstance(transport, HttpTransport):
            await transport.close()
        if isinstance(transport, RecordingTransport):
            log.info("dry run: %d payloads, last %s",
                     len(transport.sent), transport.sent[-1] if transport.sent else None)
    if failures:
        raise failures[0]
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rbvor", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--replay", type=Path, default=None,
                    help="decode a capture file instead of the SDR")
    ap.add_argument("--freq", type=float, default=None,
                    help="fix the channel in MHz and ignore the bridge (bench mode)")
    ap.add_argument("--dry-run", action="store_true",
                    help="log payloads instead of POSTing them")
    ap.add_argument("--self-test", action="store_true",
                    help="run the full chain on a synthetic signal and exit")
    ap.add_argument("--csv", type=Path, default=None,
                    help="dump every decoded window to this CSV file "
                         "(t,frequency,delta_phi_deg,phi_a_deg,phi_b_deg,amp_a,"
                         "amp_b,sub_level,carrier,noise_est,snr_db,overrun)")
    ap.add_argument("--log-level", default=None)
    ap.add_argument("--status-every", type=float, default=2.0,
                    help="seconds between the one-line status dashboard (default 2)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the periodic status dashboard")
    ap.add_argument("--version", action="version", version=f"rb-vor {__version__}")
    args = ap.parse_args(argv)

    if args.self_test:
        from rbvor.selftest import run_self_test
        logging.basicConfig(level=args.log_level or "WARNING",
                            format="%(levelname)-7s %(name)s: %(message)s")
        return run_self_test(args.config)

    try:
        cfg = load(args.config)
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=args.log_level or cfg.log["level"],
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log.info("rb-vor %s, config %s", __version__, cfg.source)

    try:
        return asyncio.run(_run(cfg, args))
    except SdrError as e:
        log.error("%s", e)
        return 3
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
