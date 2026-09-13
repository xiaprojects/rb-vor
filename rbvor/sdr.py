"""Live SDR reader: device access, ring buffer, retune, overrun accounting.

Reference: rb-vor-architecture.md section 6; failure modes F7, F9;
implementation plan M5.

The reader owns a dedicated thread and does nothing but move samples. It never
demodulates, never publishes, never blocks on anything except the device. The
blocks it produces are byte-identical in shape and dtype to those from
`rbvor.record.iq_blocks`, so `rbvor.dsp.VorDemod` cannot tell a live stream from
a replayed capture -- which is what makes a field problem reproducible on a desk.

Backends are tried in order: SoapySDR (vendor-neutral, preferred), pyrtlsdr,
then a file loop for bench work without hardware.

Bounded, never queued
---------------------
The hand-off to the consumer is a fixed-size queue. When it fills, the OLDEST
block is discarded and a gap is flagged. The alternative -- growing the queue --
would trade a visible, flagged discontinuity for an invisible, growing delay,
and a bearing delivered late is worse than one never delivered (doc A 10.4).
Every discarded block raises `overrun`, and `VorDemod` discards any integration
window that touches one (F9), so a gap can degrade availability but can never
corrupt a published bearing.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable

import numpy as np

__all__ = ["SdrReader", "SdrError", "FakeDevice"]

log = logging.getLogger("rbvor.sdr")


class SdrError(RuntimeError):
    """Device could not be opened, configured or read."""


class FakeDevice:
    """Bench and CI stand-in. Emits blocks from a callable, no hardware."""

    def __init__(self, source: Callable[[int], np.ndarray], name: str = "fake"):
        self.source = source
        self.name = name
        self.freq_hz = 0
        self.closed = False
        self.gain_db: float | None = None

    def set_frequency(self, hz: int) -> None:
        self.freq_hz = int(hz)

    def read(self, n: int) -> np.ndarray:
        return self.source(n)

    def close(self) -> None:
        self.closed = True


class _SoapyDevice:
    def __init__(self, cfg: dict):
        try:
            import SoapySDR
            from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
        except ImportError as e:
            raise SdrError("SoapySDR is not installed") from e
        self._S = SoapySDR
        self._RX = SOAPY_SDR_RX
        args = cfg["device_args"] or "driver=rtlsdr"
        try:
            self.dev = SoapySDR.Device(args)
            self.dev.setSampleRate(SOAPY_SDR_RX, 0, cfg["sample_rate_hz"])
            self.dev.setFrequencyCorrection(SOAPY_SDR_RX, 0, cfg["ppm"])
            self.dev.setGainMode(SOAPY_SDR_RX, 0, cfg["gain_mode"] == "agc")
            if cfg["gain_mode"] != "agc":
                self.dev.setGain(SOAPY_SDR_RX, 0, cfg["gain_db"])
            self.stream = self.dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
            self.dev.activateStream(self.stream)
        except Exception as e:                       # noqa: BLE001
            raise SdrError(f"SoapySDR device '{args}': {e}") from e
        self.name = f"soapy:{args}"
        self.closed = False
        self._buf = np.empty(cfg["block_samples"], dtype=np.complex64)

    def set_frequency(self, hz: int) -> None:
        self.dev.setFrequency(self._RX, 0, float(hz))

    def read(self, n: int) -> np.ndarray:
        if self._buf.size < n:
            self._buf = np.empty(n, dtype=np.complex64)
        sr = self.dev.readStream(self.stream, [self._buf], n, timeoutUs=1_000_000)
        if sr.ret <= 0:
            raise SdrError(f"readStream returned {sr.ret}")
        return self._buf[:sr.ret].copy()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            with contextlib.suppress(Exception):
                self.dev.deactivateStream(self.stream)
                self.dev.closeStream(self.stream)


class _PyRtlDevice:
    def __init__(self, cfg: dict):
        try:
            from rtlsdr import RtlSdr
        except ImportError as e:
            raise SdrError("pyrtlsdr is not installed") from e
        try:
            self.dev = RtlSdr()
            self.dev.sample_rate = cfg["sample_rate_hz"]
            self.dev.gain = "auto" if cfg["gain_mode"] == "agc" else cfg["gain_db"]
            if cfg["ppm"]:
                self.dev.freq_correction = cfg["ppm"]
        except Exception as e:                       # noqa: BLE001
            raise SdrError(f"pyrtlsdr: {e}") from e
        self.name = "pyrtlsdr"
        self.closed = False

    def set_frequency(self, hz: int) -> None:
        self.dev.center_freq = int(hz)

    def read(self, n: int) -> np.ndarray:
        raw = np.frombuffer(self.dev.read_bytes(n * 2), dtype=np.uint8)
        lut = (np.arange(256, dtype=np.float32) - 127.5) / 127.5
        v = lut[raw]
        return (v[0::2] + 1j * v[1::2]).astype(np.complex64)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            with contextlib.suppress(Exception):
                self.dev.close()


def open_device(cfg: dict, order: tuple[str, ...] | None = None):
    """Open the first working backend. Raises `SdrError` with ALL failures if none work.

    The order is taken from ``cfg['driver']`` when set to a concrete backend, so
    a config that says ``"driver": "pyrtlsdr"`` does not waste time (and a
    confusing log line) trying SoapySDR first. ``"driver": "soapy"`` or an
    unset/``"file"`` driver falls back to trying both in the historical order.
    """
    if order is None:
        driver = cfg.get("driver", "soapy")
        if driver == "pyrtlsdr":
            order = ("pyrtlsdr", "soapy")
        elif driver == "soapy":
            order = ("soapy", "pyrtlsdr")
        else:                       # "file" or anything else: try both
            order = ("soapy", "pyrtlsdr")
    errors = []
    for name in order:
        try:
            if name == "soapy":
                return _SoapyDevice(cfg)
            if name == "pyrtlsdr":
                return _PyRtlDevice(cfg)
        except SdrError as e:
            errors.append(f"{name}: {e}")
            log.warning("SDR backend '%s' unavailable: %s", name, e)
    raise SdrError("no usable SDR backend tried. " + "; ".join(errors))


class SdrReader:
    """Threaded reader implementing `controller.SdrPort`.

    `set_frequency()` takes a *navaid* frequency; the configured tuner offset is
    applied here, so the rest of the system never has to remember it.
    """

    def __init__(self, cfg_sdr: dict, device=None,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg_sdr
        self.clock = clock
        self.device = device
        self.block_samples = cfg_sdr["block_samples"]
        self.offset_hz = cfg_sdr["tuner_offset_hz"]
        # Some RTL-SDR setups deliver a spectrally-mirrored band, putting the
        # signal on the wrong side of centre. Conjugating each block un-mirrors
        # it. Off by default; set sdr.swap_iq true when the survey reports a
        # MIRROR. Applied here, so it covers every backend uniformly.
        self.swap_iq = bool(cfg_sdr.get("swap_iq", False))
        self._q: queue.Queue = queue.Queue(maxsize=cfg_sdr["buffer_blocks"])
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._gap = False
        self.nav_hz: int | None = None
        self.overruns = 0
        self.blocks_read = 0
        self.read_errors = 0

    # -- lifecycle --------------------------------------------------------- #

    def open(self) -> None:
        if self.device is None:
            self.device = open_device(self.cfg)
        log.info("SDR open: %s", getattr(self.device, "name", "unknown"))

    def start(self) -> None:
        if self._thread is not None:
            return
        if self.device is None:
            self.open()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rbvor-sdr",
                                        daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.device is not None:
            self.device.close()
            self.device = None
        self._drain()

    # -- SdrPort ----------------------------------------------------------- #

    def set_frequency(self, nav_hz: int) -> None:
        """Retune, then discard everything already in flight.

        Order matters: tune first, then flush, then flag the gap. Anything still
        queued was sampled at the old frequency and must never reach the
        demodulator (F3).
        """
        self.nav_hz = int(nav_hz)
        centre = self.nav_hz + self.offset_hz
        if self.device is None:
            self.open()
        try:
            self.device.set_frequency(centre)
        except Exception as e:                       # noqa: BLE001
            raise SdrError(f"retune to {centre} Hz failed: {e}") from e
        self._drain()
        self._gap = True
        log.info("tuned %.3f MHz (centre %.3f MHz, offset %+.1f kHz)",
                 self.nav_hz / 1e6, centre / 1e6, self.offset_hz / 1e3)
        self.start()

    # -- consumer side ----------------------------------------------------- #

    def blocks(self, timeout: float = 0.0):
        """Yield `(samples, gap)` pairs currently available. Never blocks long."""
        first = True
        while True:
            try:
                item = self._q.get(timeout=timeout if first else 0.0)
            except queue.Empty:
                return
            first = False
            yield item

    def _drain(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    # -- reader thread ----------------------------------------------------- #

    def _run(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            try:
                block = self.device.read(self.block_samples)
                backoff = 0.0
            except SdrError as e:
                self.read_errors += 1
                self._gap = True
                backoff = min(max(backoff * 2, 0.1), 2.0)
                log.warning("SDR read failed (%s); retrying in %.1f s", e, backoff)
                self._stop.wait(backoff)
                continue
            except Exception as e:                   # noqa: BLE001
                self.read_errors += 1
                self._gap = True
                log.error("SDR read error: %s", e)
                self._stop.wait(0.5)
                continue

            self.blocks_read += 1
            if self.swap_iq:
                block = np.conj(block)
            gap, self._gap = self._gap, False
            try:
                self._q.put_nowait((block, gap))
            except queue.Full:
                # Drop the oldest, keep the newest, flag the discontinuity.
                self.overruns += 1
                self._gap = True
                try:
                    self._q.get_nowait()
                    self._q.put_nowait((block, True))
                except (queue.Empty, queue.Full):
                    pass
                if self.overruns in (1, 10, 100) or self.overruns % 1000 == 0:
                    log.warning("SDR overrun #%d: the consumer is not keeping up",
                                self.overruns)

    # -- diagnostics ------------------------------------------------------- #

    @property
    def stats(self) -> dict:
        return {"blocks_read": self.blocks_read, "overruns": self.overruns,
                "read_errors": self.read_errors, "queued": self._q.qsize(),
                "nav_hz": self.nav_hz}
