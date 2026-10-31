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
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from rbvor.record import to_complex64

__all__ = ["SdrReader", "SdrError", "FakeDevice", "resolve_device",
           "DeviceChoice", "rtl_serials", "snap_gain", "TUNER_NAMES",
           "apply_tuner_profile", "TUNER_PROFILE_KEYS"]

log = logging.getLogger("rbvor.sdr")


class SdrError(RuntimeError):
    """Device could not be opened, configured or read."""


@dataclass(frozen=True)
class DeviceChoice:
    """Which dongle to open, and how it was decided."""

    index: int
    serial: str = ""
    detail: str = ""
    warning: str = ""


def resolve_device(cfg_sdr: dict, serials: Sequence[str]) -> DeviceChoice:
    """Pick the dongle from `sdr.device_serial` / `sdr.device_index` (A27).

    `serials` is the librtlsdr enumeration, position == device index; an entry
    may be "" when the USB strings cannot be read. Pure, so the whole policy is
    testable without a radio -- which matters here, because `rtlsdr` cannot even
    be imported on a machine whose librtlsdr lacks the blog fork's symbols.

    Rules, in order:

    1. `device_serial` set -> that dongle, or an error naming what IS present.
       A wrong serial must never silently fall back to index 0: pinning the
       dongle is the entire point, and the failure it exists to prevent
       (decoding a 1090 MHz antenna, F6) is invisible in the output.
    2. otherwise `device_index`, bounds-checked against the enumeration.
    3. more than one dongle and no serial given -> a warning, because index
       order follows USB enumeration and changes between boots.
    """
    want = str(cfg_sdr.get("device_serial", "") or "").strip()
    n = len(serials)
    if want:
        matches = [i for i, s in enumerate(serials) if s == want]
        if not matches:
            have = ", ".join(f"{i}:{s or '<no serial>'}" for i, s in enumerate(serials))
            raise SdrError(
                f"no RTL-SDR with serial {want!r} (sdr.device_serial); "
                f"{n} device(s) present: {have or 'none'}. "
                "Set serials once with `rtl_eeprom -d <n> -s <name>`.")
        if len(matches) > 1:
            raise SdrError(f"{len(matches)} RTL-SDRs share serial {want!r}: "
                           "give them distinct serials with `rtl_eeprom -s`")
        i = matches[0]
        return DeviceChoice(i, want, f"serial {want!r} (index {i})")

    idx = int(cfg_sdr.get("device_index", 0) or 0)
    if n and not 0 <= idx < n:
        raise SdrError(f"sdr.device_index {idx} but {n} RTL-SDR device(s) present "
                       f"(valid 0..{n - 1})")
    serial = serials[idx] if idx < n else ""
    warning = ""
    if n > 1:
        warning = (f"{n} RTL-SDR devices present and sdr.device_serial is unset: "
                   f"using index {idx} (serial {serial or '<no serial>'}). USB "
                   "enumeration order is not stable -- pin the VOR dongle by "
                   "serial, or a reboot can point the receiver at the ADS-B "
                   "antenna (CLAUDE.md F6).")
    return DeviceChoice(idx, serial, f"index {idx}"
                        + (f" (serial {serial})" if serial else ""), warning)


#: librtlsdr's `enum rtlsdr_tuner`. Worth logging: the tuner decides the gain
#: ladder AND the spectral sense (A28), and this project's conventions were
#: all measured on an R820T.
TUNER_NAMES = {0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013",
               4: "FC2580", 5: "R820T", 6: "R828D"}


#: Settings a `sdr.tuner_profiles` entry may override once the tuner is known.
#: Deliberately short: these are the three that are properties of the *tuner*
#: rather than of the installation (A28).
TUNER_PROFILE_KEYS = ("swap_iq", "gain_db", "ppm")


def apply_tuner_profile(cfg_sdr: dict, tuner: str) -> tuple[dict, list[str]]:
    """Overlay `sdr.tuner_profiles[<tuner>]` on the sdr config (A28).

    Returns `(effective, notes)`. The tuner is only known after the device is
    open, so this runs late and the base `sdr` values stay the fallback -- a
    config with no profiles behaves exactly as before.

    It exists because the two tuners in this aircraft disagree about things the
    decoder must get right and cannot guess: the FC0012 has five gain steps to
    19.2 dB where an R820T has 29 to 49.6, and the two families may deliver
    opposite spectral senses (librtlsdr runs the R820T in low-IF with spectrum
    inversion; the FC0012 is zero-IF -- O21). One config file then serves
    either dongle, and swapping hardware needs no edit.
    """
    prof = (cfg_sdr.get("tuner_profiles") or {}).get(tuner or "", {}) or {}
    effective = dict(cfg_sdr)
    notes: list[str] = []
    for k in TUNER_PROFILE_KEYS:
        if k in prof and prof[k] != cfg_sdr.get(k):
            notes.append(f"{k}: {cfg_sdr.get(k)!r} -> {prof[k]!r}")
            effective[k] = prof[k]
    return effective, notes


def snap_gain(requested_db: float, supported_db: Sequence[float]) -> tuple[float, str]:
    """Snap a requested gain to the tuner's own ladder (A28).

    Returns `(chosen, note)`; `note` is "" when the request was already a step.

    Every tuner quantises to a different set: the R820T2 has 29 steps to
    49.6 dB, the **FC0012 has five, topping out at 19.2 dB**. librtlsdr rounds
    silently, so a config asking for 32.8 dB on an FC0012 gets 19.2 and says
    nothing -- the number in the file then describes neither the request nor
    the hardware. We snap explicitly and say so.
    """
    if not supported_db:
        return float(requested_db), ""
    chosen = min(supported_db, key=lambda g: (abs(g - requested_db), g))
    if abs(chosen - requested_db) < 0.05:
        return float(chosen), ""
    lo, hi = min(supported_db), max(supported_db)
    if requested_db > hi:
        note = (f"gain {requested_db:g} dB is above this tuner's maximum; "
                f"using {chosen:g} dB (ladder {lo:g}..{hi:g} dB)")
    elif requested_db < lo:
        note = (f"gain {requested_db:g} dB is below this tuner's minimum; "
                f"using {chosen:g} dB (ladder {lo:g}..{hi:g} dB)")
    else:
        note = (f"gain {requested_db:g} dB is not a step of this tuner; "
                f"using {chosen:g} dB")
    return float(chosen), note


def rtl_serials() -> list[str]:
    """Serial of every RTL-SDR present, position == device index. [] if unknown."""
    try:
        from rtlsdr import RtlSdr
        return [s if isinstance(s, str) else s.decode("ascii", "replace")
                for s in RtlSdr.get_device_serial_addresses()]
    except Exception as e:                           # noqa: BLE001
        log.debug("cannot enumerate RTL-SDR devices: %s", e)
        return []


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
        # A27: pin the dongle. SoapySDR's rtlsdr driver takes `serial=` or
        # `rtl=<index>`; only add them when the operator has not already put an
        # explicit selector in device_args.
        sel = str(cfg.get("device_serial", "") or "").strip()
        if sel and "serial=" not in args:
            args = f"{args},serial={sel}"
        elif not sel and "rtl=" not in args and "serial=" not in args:
            args = f"{args},rtl={int(cfg.get('device_index', 0) or 0)}"
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
        except Exception as e:                       # noqa: BLE001
            # `rtlsdr` binds librtlsdr at IMPORT time, so a version mismatch
            # raises AttributeError (e.g. `undefined symbol:
            # rtlsdr_set_dithering` against a stock librtlsdr where pyrtlsdr
            # expects the rtl-sdr-blog fork, O7). That is not an ImportError,
            # so it used to escape as a raw ctypes error and take the daemon
            # down with a message naming neither the cause nor the fix.
            raise SdrError(
                f"pyrtlsdr cannot bind librtlsdr ({e}). The installed "
                "librtlsdr does not match pyrtlsdr's expectations -- see "
                "RUN.md section 7 (RTL-SDR Blog fork)."
            ) from e
        choice = resolve_device(cfg, rtl_serials())
        if choice.warning:
            log.warning("%s", choice.warning)
        tuner = ""
        try:
            self.dev = RtlSdr(device_index=choice.index)
            self.dev.sample_rate = cfg["sample_rate_hz"]
            with contextlib.suppress(Exception):
                tuner = TUNER_NAMES.get(self.dev.get_tuner_type(), "unknown")
            eff, notes = apply_tuner_profile(cfg, tuner)
            for n in notes:
                log.info("tuner profile %s: %s", tuner, n)
            if eff["gain_mode"] == "agc":
                self.dev.gain = "auto"
            else:
                # A28: snap to the tuner's ladder and say so. The aircraft's VOR
                # dongle is an FC0012 (five steps, max 19.2 dB), not the R820T2
                # the config's numbers came from.
                gain, note = snap_gain(eff["gain_db"],
                                       getattr(self.dev, "valid_gains_db", []) or [])
                if note:
                    log.warning("%s: %s", tuner or "tuner", note)
                self.dev.gain = gain
            if eff["ppm"]:
                self.dev.freq_correction = eff["ppm"]
        except SdrError:
            raise
        except Exception as e:                       # noqa: BLE001
            raise SdrError(f"pyrtlsdr {choice.detail}: {e}") from e
        self.tuner = tuner
        self.name = f"pyrtlsdr {choice.detail}" + (f", {tuner} tuner" if tuner else "")
        self.closed = False

    def set_frequency(self, hz: int) -> None:
        self.dev.center_freq = int(hz)

    def read(self, n: int) -> np.ndarray:
        # librtlsdr delivers cu8 (A4); one conversion path shared with replay,
        # so the live stream and a capture file decode byte-identically.
        raw = np.frombuffer(self.dev.read_bytes(n * 2), dtype=np.uint8)
        return to_complex64(raw, "cu8")

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
        # Per-dongle override: conjugate each block to un-mirror a genuinely
        # inverted device or an I/Q-swapped capture path. A standard RTL-SDR
        # does NOT need it (amendment A22 -- the "mirror" every real capture
        # showed was our own sign convention, since corrected). Off by
        # default; set it only when `vor_replay.py --survey` reports a MIRROR
        # on a capture from THIS dongle. Applied here so it covers every
        # backend uniformly.
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
        # A28: `swap_iq` is consumed here rather than in the device, and the
        # tuner is only known once the device is open -- so the profile is
        # applied last, and loudly, because it changes how the band is read.
        tuner = getattr(self.device, "tuner", "")
        if tuner:
            eff, _ = apply_tuner_profile(self.cfg, tuner)
            want = bool(eff.get("swap_iq", False))
            if want != self.swap_iq:
                log.info("tuner profile %s: swap_iq %s -> %s",
                         tuner, self.swap_iq, want)
                self.swap_iq = want

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
        """Yield the `(samples, gap)` pairs queued at the moment of the call.

        Bounded on purpose. A caller loops `for block in reader.blocks()`, and
        if the reader thread refills the queue faster than the caller drains
        it, an unbounded generator never returns: the daemon's pump would sit
        in that loop for ever, never yielding to the event loop, so the ticker
        stops, the state machine stops, and the watchdog stops being fed --
        precisely when the machine is overloaded (F5). Yielding at most what
        was queued on entry guarantees the caller gets control back at least
        once per `buffer_blocks`; anything that arrives meanwhile is picked up
        by the next call, and anything that overflows is counted and flagged
        (F9) rather than silently delayed.
        """
        limit = max(1, self._q.qsize())
        for i in range(limit):
            try:
                item = self._q.get(timeout=timeout if i == 0 else 0.0)
            except queue.Empty:
                return
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
