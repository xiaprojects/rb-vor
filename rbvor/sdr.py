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

from rbvor.gainctl import AutoGainParams, GainController
from rbvor.record import to_complex64

__all__ = ["SdrReader", "SdrError", "FakeDevice", "resolve_device",
           "DeviceChoice", "rtl_serials", "snap_gain", "TUNER_NAMES",
           "apply_tuner_profile", "TUNER_PROFILE_KEYS", "Allocation", "allocate",
           "device_cfg", "format_allocation", "DEVICE_OVERRIDE_KEYS", "GAIN_STEP"]

log = logging.getLogger("rbvor.sdr")


class _GainStepGap:
    """The `gap` value of the first block after an automatic gain step (A47).

    Truthy, so every consumer that tests `if gap:` treats it exactly as a
    sample gap -- the demodulator poisons the window, nothing decodes across
    the step. The controller recognises it by identity and drops the windows
    that span the step silently, instead of entering NO_SIGNAL as it must for a
    real overrun (F9): the samples are continuous, only their scale changed.
    """

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return "GAIN_STEP"


GAIN_STEP = _GainStepGap()


class SdrError(RuntimeError):
    """Device could not be opened, configured or read."""


@dataclass(frozen=True)
class DeviceChoice:
    """Which dongle to open, and how it was decided."""

    index: int
    serial: str = ""
    detail: str = ""
    warning: str = ""
    #: A40, list form only: per-entry overrides as (key, value) pairs, what
    #: the entry may serve, and whether it was selected by serial.
    overrides: tuple = ()
    uses: tuple[str, ...] = ("nav", "gs")
    by_serial: bool = True


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


#: Per-entry overrides a `sdr.devices` entry may carry (A40). Precedence when
#: the device is opened: the entry, then `tuner_profiles`, then the sdr block.
DEVICE_OVERRIDE_KEYS = ("gain_db", "ppm", "swap_iq", "bias_tee")
DEVICE_USES = ("nav", "gs")


@dataclass(frozen=True)
class Allocation:
    """Which dongle serves which feature (A40). `nav` is VOR and localizer.

    `shared` is True when the glide slope has no dongle of its own and may use
    the nav one: then only one of localizer and glide slope is received, and
    the pilot's `gsselect` chooses. A feature that is None has no dongle at
    all and is NO_SDR."""

    nav: DeviceChoice | None
    gs: DeviceChoice | None
    shared: bool
    notes: tuple[str, ...] = ()
    listed: bool = False          # True for the `sdr.devices` form


def _entry_label(e: dict) -> str:
    if str(e.get("device_serial", "") or "").strip():
        return f"serial {e['device_serial']!r}"
    return f"index {int(e.get('device_index', 0) or 0)}"


def allocate(cfg_sdr: dict, serials: Sequence[str]) -> Allocation:
    """Decide which dongle serves the localizer/VOR and which the glide slope.

    Pure, like `resolve_device`, so the whole policy is testable without a
    radio. `serials` is the librtlsdr enumeration, position == index.

    Without `sdr.devices` this is A27 exactly: one dongle from
    `device_serial` / `device_index` (a missing serial still raises), used for
    everything, so the glide slope shares it.

    With `sdr.devices` (an ordered list), rules in order (docs/gs-plan.md 6.1):

    1. only listed entries whose dongle is present count;
    2. `nav` takes the first present entry whose `use` allows `nav`;
    3. `gs` takes the first *other* present entry that allows `gs`;
    4. failing that, if the nav dongle allows `gs`, the two share it;
    5. a feature with no dongle is None;
    6. two entries resolving to one dongle, or two dongles sharing a listed
       serial, is an error.

    Absent is **not** an error in the list form -- relaxing A27 for this form
    only: a list exists so the receiver works with whatever is plugged in. An
    absent entry is a note naming every serial that is present. The guarantee
    A27 was written for still holds: nothing is chosen that the list does not
    name, because the other dongles belong to Stratux and rb-sdr-radio.
    """
    devices = cfg_sdr.get("devices")
    if not devices:
        nav = resolve_device(cfg_sdr, serials)
        notes = (nav.warning,) if nav.warning else ()
        return Allocation(nav=nav, gs=None, shared=True, notes=notes)

    n = len(serials)
    have = ", ".join(f"{i}:{s or '<no serial>'}" for i, s in enumerate(serials)) or "none"
    notes: list[str] = []
    present: list[tuple[dict, DeviceChoice]] = []
    taken: dict[int, str] = {}
    for k, e in enumerate(devices):
        uses = tuple(e.get("use") or DEVICE_USES)
        serial = str(e.get("device_serial", "") or "").strip()
        overrides = tuple((key, e[key]) for key in DEVICE_OVERRIDE_KEYS if key in e)
        if serial:
            matches = [i for i, s in enumerate(serials) if s == serial]
            if len(matches) > 1:
                raise SdrError(f"{len(matches)} RTL-SDRs share serial {serial!r} "
                               f"(sdr.devices[{k}]): give them distinct serials "
                               "with `rtl_eeprom -s`")
            if not matches:
                notes.append(f"sdr.devices[{k}] (serial {serial!r}, use "
                             f"{'/'.join(uses)}) is not present; present: {have}")
                continue
            idx = matches[0]
            choice = DeviceChoice(idx, serial, f"serial {serial!r} (index {idx})",
                                  overrides=overrides, uses=uses)
        else:
            idx = int(e.get("device_index", 0) or 0)
            if idx >= n:
                notes.append(f"sdr.devices[{k}] (index {idx}, use {'/'.join(uses)}) "
                             f"is not present; present: {have}")
                continue
            s = serials[idx]
            warning = ""
            if n > 1:
                warning = (f"sdr.devices[{k}] selects index {idx} (serial "
                           f"{s or '<no serial>'}) by position; USB enumeration "
                           "order is not stable -- pin it by serial (A27)")
                notes.append(warning)
            choice = DeviceChoice(idx, s, f"index {idx}" + (f" (serial {s})" if s else ""),
                                  warning, overrides=overrides, uses=uses,
                                  by_serial=False)
        if idx in taken:
            raise SdrError(f"sdr.devices[{k}] ({_entry_label(e)}) and {taken[idx]} "
                           f"both resolve to the dongle at index {idx}")
        taken[idx] = f"sdr.devices entry {_entry_label(e)}"
        present.append((e, choice))

    nav = next((c for _, c in present if "nav" in c.uses), None)
    gs = next((c for _, c in present if "gs" in c.uses and c is not nav), None)
    shared = gs is None and nav is not None and "gs" in nav.uses
    if nav is None:
        notes.append("no listed dongle present may serve nav (VOR / localizer)")
    if gs is None and not shared:
        notes.append("no listed dongle present may serve the glide slope")
    return Allocation(nav=nav, gs=gs, shared=shared, notes=tuple(notes), listed=True)


def device_cfg(cfg_sdr: dict, choice: DeviceChoice) -> dict:
    """The sdr config that opens exactly `choice` with its own overrides (A40).

    The entry's overrides travel under `_device_overrides` so that
    `apply_tuner_profile` -- which runs only once the tuner is known -- can
    let them win over the profile, as the precedence rule says."""
    eff = {k: v for k, v in cfg_sdr.items() if k != "devices"}
    eff["device_serial"] = choice.serial if choice.by_serial else ""
    eff["device_index"] = choice.index
    ov = dict(choice.overrides)
    eff.update(ov)
    eff["_device_overrides"] = ov
    return eff


def format_allocation(a: Allocation, serials: Sequence[str]) -> list[str]:
    """Human-readable lines for `--devices` and the startup banner."""
    lines = [f"{len(serials)} RTL-SDR device(s) present"
             + (": " + ", ".join(f"{i}:{s or '<no serial>'}" for i, s in enumerate(serials))
                if serials else "")]
    form = "sdr.devices list" if a.listed else "single dongle (no sdr.devices)"
    lines.append(f"allocation ({form}):")
    lines.append(f"  nav (VOR / localizer): {a.nav.detail if a.nav else 'NONE -> NO_SDR'}")
    if a.gs is not None:
        lines.append(f"  glide slope:           {a.gs.detail} (dedicated)")
    elif a.shared:
        lines.append("  glide slope:           shares the nav dongle (pilot selects LOC / GS)")
    else:
        lines.append("  glide slope:           NONE -> NO_SDR")
    lines += [f"  note: {n}" for n in a.notes]
    return lines


#: librtlsdr's `enum rtlsdr_tuner`. Worth logging: the tuner decides the gain
#: ladder AND the spectral sense (A28), and this project's conventions were
#: all measured on an R820T.
TUNER_NAMES = {0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013",
               4: "FC2580", 5: "R820T", 6: "R828D"}


#: Settings a `sdr.tuner_profiles` entry may override once the tuner is known.
#: Deliberately short: these are the three that are properties of the *tuner*
#: rather than of the installation (A28).
TUNER_PROFILE_KEYS = ("swap_iq", "gain_db", "ppm", "auto_gain")


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
    # A40: a `sdr.devices` entry's own value wins over the tuner profile.
    pinned = cfg_sdr.get("_device_overrides") or {}
    for k in TUNER_PROFILE_KEYS:
        if k in pinned:
            continue
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
        #: the tuner's gain ladder; tests set it to exercise automatic gain
        self.gains_db: list[float] = []
        self.gain_history: list[float] = []

    def set_frequency(self, hz: int) -> None:
        self.freq_hz = int(hz)

    def set_gain(self, db: float) -> float:
        g, _ = snap_gain(db, self.gains_db)
        self.gain_db = g
        self.gain_history.append(g)
        return g

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
        self.gain_db = None if cfg["gain_mode"] == "agc" else float(cfg["gain_db"])
        self.gains_db: list[float] = []
        with contextlib.suppress(Exception):
            r = self.dev.getGainRange(SOAPY_SDR_RX, 0)
            step = r.step() if r.step() > 0 else 1.0
            self.gains_db = [round(float(g), 2)
                             for g in np.arange(r.minimum(), r.maximum() + step / 2, step)]

    def set_frequency(self, hz: int) -> None:
        self.dev.setFrequency(self._RX, 0, float(hz))

    def set_gain(self, db: float) -> float:
        g, _ = snap_gain(db, self.gains_db)
        try:
            self.dev.setGain(self._RX, 0, float(g))
        except Exception as e:                       # noqa: BLE001
            raise SdrError(f"setGain {g} dB failed: {e}") from e
        self.gain_db = g
        return g

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
        self.gain_db: float | None = None
        self.gains_db: list[float] = []
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
                self.gains_db = [float(g) for g in
                                 (getattr(self.dev, "valid_gains_db", []) or [])]
                gain, note = snap_gain(eff["gain_db"], self.gains_db)
                if note:
                    log.warning("%s: %s", tuner or "tuner", note)
                self.dev.gain = gain
                self.gain_db = gain
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

    def set_gain(self, db: float) -> float:
        g, _ = snap_gain(db, self.gains_db)
        try:
            self.dev.gain = g
        except Exception as e:                       # noqa: BLE001
            raise SdrError(f"set gain {g} dB failed: {e}") from e
        self.gain_db = g
        return g

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
                 clock: Callable[[], float] = time.monotonic, name: str = "rbvor-sdr",
                 before_open: Callable[[], object] | None = None):
        self.name = name
        #: A40: called before every open, i.e. at every tune attempt from a
        #: released dongle. The daemon re-reads the allocation there (it
        #: rebinds this reader), so a hot-plugged dongle is found.
        self.before_open = before_open
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

        self._unavailable = ""
        #: A47: automatic gain. Built at open() (the ladder and the tuner
        #: profile are only known then); None means fixed gain, as before.
        self.agc: GainController | None = None
        #: "free" | "protect" | "hold", asked once per evaluation; the
        #: controller sets it from its state. Without one the gain is free.
        self.gain_policy: Callable[[], str] | None = None
        self.gain_steps = 0
        self._agc_device = None      # the device the controller was built for

    # -- lifecycle --------------------------------------------------------- #

    def bind(self, cfg_sdr: dict | None, reason: str = "") -> None:
        """A40: point this reader at another dongle, or at none.

        `cfg_sdr` is a `device_cfg()` result; None means the allocation gave
        this feature no dongle, and the next open raises `SdrError(reason)`,
        which the controller turns into NO_SDR and retries (A30). A device
        open on a *different* dongle is closed first; rebinding to the same
        one leaves it running."""
        def key(c):
            if c is None:
                return None
            return (c.get("device_serial"), c.get("device_index"),
                    tuple(sorted((c.get("_device_overrides") or {}).items())))
        if (key(cfg_sdr) != key(self.cfg if not self._unavailable else None)
                and self.device is not None):
            self.close()
        if cfg_sdr is None:
            self._unavailable = reason or "no dongle allocated"
            return
        self._unavailable = ""
        self.cfg = cfg_sdr
        self.offset_hz = cfg_sdr["tuner_offset_hz"]
        if self.device is None:
            self.swap_iq = bool(cfg_sdr.get("swap_iq", False))

    def open(self) -> None:
        if self.device is None and self.before_open is not None:
            self.before_open()
        if self.device is None:
            if self._unavailable:
                raise SdrError(self._unavailable)
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

    def _build_agc(self, tuner: str) -> None:
        """A47: automatic gain within `sdr.auto_gain` (tuner profile first)."""
        eff = apply_tuner_profile(self.cfg, tuner)[0] if tuner else self.cfg
        params = AutoGainParams.from_cfg(eff.get("auto_gain"))
        if not params.enabled or not hasattr(self.device, "set_gain"):
            self.agc = None
            return
        if eff.get("gain_mode") == "agc":
            log.warning("sdr.auto_gain is ignored: gain_mode is \"agc\" (the tuner's own)")
            self.agc = None
            return
        ladder = list(getattr(self.device, "gains_db", []) or [])
        start = getattr(self.device, "gain_db", None)
        start = float(eff["gain_db"]) if start is None else float(start)
        self.agc = GainController(params, ladder or [start], start,
                                  int(eff["sample_rate_hz"]), -float(eff["tuner_offset_hz"]))
        if abs(self.agc.gain_db - start) > 1e-6:
            self.agc.applied(self.device.set_gain(self.agc.gain_db))
        log.info("automatic gain %g..%g dB (%d steps), starting at %g dB",
                 self.agc.ladder[0], self.agc.ladder[-1], len(self.agc.ladder),
                 self.agc.gain_db)

    @property
    def gain_db(self) -> float | None:
        """The gain the tuner was last set to, or None when unknown."""
        return getattr(self.device, "gain_db", None) if self.device is not None else None

    def start(self) -> None:
        if self._thread is not None:
            return
        if self.device is None:
            self.open()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name,
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
        if self._agc_device is not self.device:
            # built here, not in open(): a reader handed a device never opens
            # one, and the ladder and tuner profile belong to THIS device
            self._agc_device = self.device
            self._build_agc(getattr(self.device, "tuner", "") or "")
        if self.agc is not None:
            # A47: VHF and UHF (nav vs glide slope on one dongle) keep their
            # own gains; the remembered one is restored on a band change.
            g = self.agc.retuned(self.nav_hz)
            if g is not None:
                self.agc.applied(self.device.set_gain(g))
                log.info("automatic gain: %g dB restored for %s", self.agc.gain_db,
                         "UHF" if self.nav_hz >= 200_000_000 else "VHF")
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
            if self.agc is not None:
                self._auto_gain(block)
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

    def _auto_gain(self, block: np.ndarray) -> None:
        """A47: measure this block; apply a step when the controller asks.

        Runs in the reader thread, between two reads, so the device is never
        touched from two threads. The block just read was sampled at the old
        gain and goes out unchanged; the NEXT block carries `GAIN_STEP`.
        """
        policy = self.gain_policy() if self.gain_policy is not None else "free"
        d = self.agc.observe(block, self.clock(), policy)
        if d is None:
            return
        m = self.agc.last
        level = (f"clip {m.clip_pct:.2f} %, ADC {m.rms_counts:.1f} counts, carrier/floor "
                 + ("--" if m.cnr_db is None else f"{m.cnr_db:.1f} dB")) if m else ""
        if not d.changed:
            log.info("automatic gain: stays %g dB, %s (%s)", d.gain_db, d.reason, level)
            return
        old = self.gain_db
        try:
            self.agc.applied(self.device.set_gain(d.gain_db))
        except Exception as e:                       # noqa: BLE001
            log.warning("automatic gain: step to %g dB failed (%s)", d.gain_db, e)
            if old is not None:
                self.agc.applied(old)
            return
        self.gain_steps += 1
        if not self._gap:
            self._gap = GAIN_STEP
        log.info("automatic gain: %s -> %g dB, %s (%s)",
                 "?" if old is None else f"{old:g}", self.agc.gain_db, d.reason, level)

    # -- diagnostics ------------------------------------------------------- #

    @property
    def stats(self) -> dict:
        out = {"blocks_read": self.blocks_read, "overruns": self.overruns,
               "read_errors": self.read_errors, "queued": self._q.qsize(),
               "nav_hz": self.nav_hz}
        if self.agc is not None:
            out["gain_db"] = self.gain_db
            out["gain_steps"] = self.gain_steps
        return out
