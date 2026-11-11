#!/usr/bin/env python3
"""Synthetic VOR signal generator.

Reference: rb-vor-architecture.md sections 4 and 12.1; implementation plan M2.

Emits an IQ file plus sidecar containing a VOR signal at a commanded radial.
This is the ground truth for every downstream test: the DSP chain (M3), the
bearing sign and filter group delay (M4), the quality metric curve (M4) and the
CI suite all measure themselves against files produced here.

Signal model
------------
Baseband modulating function, CVOR::

    m(t) = 1 + m_am  * cos(2*pi*30*t + phi_am)
             + m_sub * cos(2*pi*9960*t + beta*sin(2*pi*30*t + phi_fm))
             + m_id  * key(t) * cos(2*pi*1020*t)

    beta = deviation / 30 = 480 / 30 = 16      (FM modulation index)

then placed where a tuner parked at ``nav + f_offset`` puts the navaid, i.e.
at ``nav - LO = -f_offset`` in baseband (amendment A22)::

    z(t) = level * m(t) * exp(-j*2*pi*f_offset*t) + n(t)

With the default ``f_offset = -fs/4`` the carrier is at **+fs/4**. Files
written before A22 placed it at ``+f_offset`` and carry no
``synthetic.carrier_at`` key in their sidecar; `tools/vor_replay.py` warns
about them and they decode with ``--swap-iq``.

Radial encoding, and amendment A6
---------------------------------
- ``cvor``  : AM 30 Hz is the VARIABLE, FM subcarrier carries the REFERENCE.
              phi_am = radial, phi_fm = 0   ->  phi_am - phi_fm = +radial
- ``dvor``  : roles swap AND rotation sense reverses -- by design, so that the
              receiver output is unchanged.
              phi_am = 0, phi_fm = -radial  ->  phi_am - phi_fm = +radial
- ``dvor-reversed`` : a deliberately WRONG DVOR (sense not reversed), which
              decodes to -radial. It exists so the CVOR/DVOR equivalence test
              can fail; without it that test would pass trivially.

**AMENDMENT A6.** Plan A, M4/T4 stated that DVOR should decode correctly only
after flipping ``bearing.phase_sign``. That is wrong. Making the receiver
output identical is the entire purpose of the DVOR arrangement -- a real
receiver cannot tell the two apart, and neither should ours. T4 is corrected to:
*CVOR and DVOR must decode to the same bearing with the same ``phase_sign``,
and ``dvor-reversed`` must decode to its negation.*
``phase_sign`` remains a calibration constant, but for a different and real
reason: our own choice of which path is A and which is B, and the sign
convention of the FM demodulator, are implementation freedoms that must be
pinned by measurement (O1).

Examples
--------
    ./vor_gen.py --radial 137 --snr-db 30 --seconds 30 -o t137.cu8
    ./vor_gen.py --radial 0 --snr-db 40 --seconds 5 --no-ident --format cf32
    ./vor_gen.py --radial 213 --type dvor --fade slow --snr-db 6
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.record import IQMeta, IQWriter  # noqa: E402

TONE_HZ = 30.0
SUBCARRIER_HZ = 9960.0
#: Must equal `rbvor.dsp.CARRIER_CONVENTION` (D5). The self-test checks it.
CARRIER_CONVENTION = "-offset"

IDENT_HZ = 1020.0
DEVIATION_HZ = 480.0
CHANNEL_BW_HZ = 12000.0  # SNR is specified in this bandwidth

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..", "0": "-----", "1": ".----", "2": "..---",
    "3": "...--", "4": "....-", "5": ".....", "6": "-....", "7": "--...",
    "8": "---..", "9": "----.",
}


@dataclass
class VorParams:
    radial_deg: float
    vor_type: str = "cvor"          # cvor | dvor | dvor-reversed
    fs: int = 1_024_000
    seconds: float = 30.0
    offset_hz: int | None = None    # default -fs/4 (amendment A1)
    drift_hz: float = 0.0
    snr_db: float | None = 20.0     # None = noiseless
    am_depth: float = 0.30
    sub_depth: float = 0.30
    ident_depth: float = 0.10
    deviation_hz: float = DEVIATION_HZ
    tone_hz: float = TONE_HZ
    subcarrier_hz: float = SUBCARRIER_HZ
    ident: str = "TST"
    ident_dot_s: float = 0.15       # ~8 wpm
    ident_period_s: float = 10.0
    ident_edge_s: float = 0.006     # soft keying: hard edges splatter into 30 Hz
    fade: str = "none"              # none | slow | flutter
    #: Digital level. "auto" (default) measures the actual peak of signal PLUS
    #: noise on a short probe and backs off to 92 % of full scale. A fixed level
    #: cannot work across the SNR range: at 10 dB SNR in a 12 kHz channel the
    #: noise spread over 1.024 MHz carries several times the signal power, so
    #: the peak is set by the noise, not by the modulation depth. A real
    #: receiver's AGC does the same thing for the same reason.
    level: float | str = "auto"
    seed: int | None = 0
    block: int = 65536
    _intervals: list[tuple[float, float]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.vor_type not in ("cvor", "dvor", "dvor-reversed"):
            raise ValueError(f"unknown vor_type {self.vor_type!r}")
        if self.fade not in ("none", "slow", "flutter"):
            raise ValueError(f"unknown fade {self.fade!r}")
        if self.offset_hz is None:
            self.offset_hz = -self.fs // 4
        if self.subcarrier_hz + 3 * self.deviation_hz > self.fs / 2:
            raise ValueError(
                f"fs={self.fs} too low for a {self.subcarrier_hz} Hz subcarrier"
            )
        self._intervals = morse_intervals(
            self.ident, self.ident_dot_s
        ) if self.ident else []
        if self.level == "auto":
            self.level = _autolevel(self)
        if not isinstance(self.level, (int, float)) or self.level <= 0:
            raise ValueError(f"level must be a positive number or 'auto', got {self.level!r}")

    @property
    def n_samples(self) -> int:
        return int(round(self.seconds * self.fs))

    def phases(self) -> tuple[float, float]:
        """Return (phi_am, phi_fm) in radians for the commanded radial."""
        r = np.deg2rad(self.radial_deg)
        if self.vor_type == "cvor":
            return r, 0.0
        if self.vor_type == "dvor":
            return 0.0, -r
        return 0.0, +r          # dvor-reversed: decodes to -radial, on purpose


def _autolevel(p, probe_s: float = 0.25, headroom: float = 0.92, gen=None) -> float:
    """Choose a digital level so that signal+noise fits the container.

    Measured, not estimated: a short probe is generated at unit level with the
    same seed and impairments, and the level is set from its 99.995th percentile
    peak. Returns a plain float, so a run is still fully reproducible.

    `gen` is the block generator to probe, `generate_blocks` by default;
    `tools/loc_gen.py` passes its own (A33). Any params object with `level`,
    `seconds`, `fs`, `block` and `_intervals` works.
    """
    import copy
    probe = copy.copy(p)
    probe.level = 1.0
    probe.seconds = min(probe_s, p.seconds)
    probe.block = int(probe.seconds * p.fs) + 1
    probe._intervals = p._intervals
    z = next((gen or generate_blocks)(probe))
    peak = float(np.percentile(np.maximum(np.abs(z.real), np.abs(z.imag)), 99.995))
    return headroom / max(peak, 1e-9)


def morse_intervals(text: str, dot_s: float) -> list[tuple[float, float]]:
    """Key-on intervals, in seconds from the start of the ident, for `text`."""
    out: list[tuple[float, float]] = []
    t = 0.0
    for i, ch in enumerate(text.upper()):
        code = MORSE.get(ch)
        if code is None:
            raise ValueError(f"character {ch!r} has no Morse representation")
        if i:
            t += 3 * dot_s                      # inter-character gap
        for j, sym in enumerate(code):
            if j:
                t += dot_s                      # inter-symbol gap
            dur = dot_s * (3 if sym == "-" else 1)
            out.append((t, t + dur))
            t += dur
    return out


def _keying(t: np.ndarray, p: VorParams) -> np.ndarray:
    """Raised-cosine keying envelope, repeating every `ident_period_s`."""
    if not p._intervals:
        return np.zeros_like(t)
    tm = np.mod(t, p.ident_period_s)
    env = np.zeros_like(t)
    e = p.ident_edge_s
    for start, end in p._intervals:
        rise = np.clip((tm - start) / e, 0.0, 1.0)
        fall = np.clip((end - tm) / e, 0.0, 1.0)
        env += 0.5 * (1 - np.cos(np.pi * rise)) * 0.5 * (1 - np.cos(np.pi * fall))
    return np.clip(env, 0.0, 1.0)


def _phase(f: float, n0: int, n: int, fs: int) -> np.ndarray:
    """2*pi*f*t with t = (n0..n0+n)/fs, kept accurate over long files.

    The fractional part is taken before scaling by 2*pi so that a 30-minute
    capture does not lose phase resolution to float64 exponent growth.
    """
    idx = n0 + np.arange(n, dtype=np.float64)
    return 2.0 * np.pi * np.mod(f * idx / fs, 1.0)


def add_channel_noise(z: np.ndarray, snr_db: float, fs: int,
                      rng: np.random.Generator) -> np.ndarray:
    """Add white complex Gaussian noise for `snr_db` in the CHANNEL_BW_HZ channel.

    SNR is specified in the CHANNEL_BW_HZ channel, but the noise is spread over
    the whole sample rate, so scale accordingly. Signal power is measured on
    the block itself. Shared with `tools/loc_gen.py` (A33).
    """
    n = z.size
    p_sig = float(np.mean(np.abs(z) ** 2))
    p_noise = p_sig / (10 ** (snr_db / 10.0)) * (fs / CHANNEL_BW_HZ)
    sigma = np.sqrt(p_noise / 2.0)
    return z + rng.normal(0, sigma, n) + 1j * rng.normal(0, sigma, n)


def generate_blocks(p: VorParams) -> Iterator[np.ndarray]:
    """Yield complex64 blocks of the modelled signal, with noise and fading."""
    rng = np.random.default_rng(p.seed)
    phi_am, phi_fm = p.phases()
    beta = p.deviation_hz / p.tone_hz
    # A22: the carrier sits at -offset_hz. `drift_hz` is the signal's own
    # frequency error, positive = higher RF, and adds directly.
    f_off = -p.offset_hz + p.drift_hz
    total = p.n_samples
    n0 = 0
    while n0 < total:
        n = min(p.block, total - n0)
        t = (n0 + np.arange(n, dtype=np.float64)) / p.fs

        am = p.am_depth * np.cos(_phase(p.tone_hz, n0, n, p.fs) + phi_am)
        fm_arg = _phase(p.subcarrier_hz, n0, n, p.fs) + beta * np.sin(
            _phase(p.tone_hz, n0, n, p.fs) + phi_fm
        )
        sub = p.sub_depth * np.cos(fm_arg)
        idt = p.ident_depth * _keying(t, p) * np.cos(_phase(IDENT_HZ, n0, n, p.fs))

        m = 1.0 + am + sub + idt

        if p.fade == "slow":
            m *= 10 ** (0.15 * np.sin(2 * np.pi * 0.2 * t))      # +/-3 dB @ 0.2 Hz
        elif p.fade == "flutter":
            m *= 10 ** (0.10 * np.sin(2 * np.pi * 10.0 * t))     # +/-2 dB @ 10 Hz

        carrier_phase = 2 * np.pi * np.mod(f_off * (n0 + np.arange(n)) / p.fs, 1.0)
        z = (p.level * m * np.exp(1j * carrier_phase)).astype(np.complex128)

        if p.snr_db is not None:
            z = add_channel_noise(z, p.snr_db, p.fs, rng)

        n0 += n
        yield z.astype(np.complex64)


def to_disk(z: np.ndarray, fmt: str) -> tuple[np.ndarray, int]:
    """Quantise complex64 to the on-disk interleaved format. Returns (raw, clipped)."""
    iq = np.empty(z.size * 2, dtype=np.float64)
    iq[0::2] = z.real
    iq[1::2] = z.imag
    if fmt == "cf32":
        return iq.astype(np.float32), int(np.sum(np.abs(iq) > 1.0))
    if fmt == "cu8":
        v = iq * 127.5 + 127.5
        clipped = int(np.sum((v < 0) | (v > 255)))
        return np.clip(np.round(v), 0, 255).astype(np.uint8), clipped
    if fmt == "cs8":
        v = iq * 128.0
        clipped = int(np.sum((v < -128) | (v > 127)))
        return np.clip(np.round(v), -128, 127).astype(np.int8), clipped
    if fmt == "cs16":
        v = iq * 32768.0
        clipped = int(np.sum((v < -32768) | (v > 32767)))
        return np.clip(np.round(v), -32768, 32767).astype(np.int16), clipped
    raise ValueError(f"unsupported output format {fmt!r}")


def write_file(p: VorParams, path: Path, fmt: str = "cu8",
               nav_freq_hz: int = 109_400_000) -> Path:
    """Generate and write an IQ file plus sidecar. Returns the IQ path."""
    meta = IQMeta(
        format=fmt,
        sample_rate_hz=p.fs,
        center_freq_hz=nav_freq_hz + int(p.offset_hz),
        nav_freq_hz=nav_freq_hz,
        tuner_offset_hz=int(p.offset_hz),
        gain_mode="synthetic",
        driver="vor_gen",
        device="synthetic",
        station=p.ident,
        expected_radial_deg=float(p.radial_deg),
        notes=f"synthetic {p.vor_type}",
        synthetic={
            "vor_type": p.vor_type,
            "radial_deg": p.radial_deg,
            "snr_db": p.snr_db,
            "am_depth": p.am_depth,
            "sub_depth": p.sub_depth,
            "ident_depth": p.ident_depth if p.ident else 0.0,
            "deviation_hz": p.deviation_hz,
            "tone_hz": p.tone_hz,
            "subcarrier_hz": p.subcarrier_hz,
            "ident": p.ident,
            "fade": p.fade,
            "drift_hz": p.drift_hz,
            "level": p.level,
            "seed": p.seed,
            "snr_bandwidth_hz": CHANNEL_BW_HZ,
            # A22: which side of centre the carrier was placed on. Absent in
            # files generated before the convention was corrected.
            "carrier_at": CARRIER_CONVENTION,
        },
    )
    clipped = 0
    with IQWriter(path, meta) as w:
        for z in generate_blocks(p):
            raw, c = to_disk(z, fmt)
            clipped += c
            w.write(raw)
    frac = clipped / (2.0 * p.n_samples)
    # A handful of clipped samples are Gaussian tails and are harmless; a real
    # dongle clips them too. Only warn when it becomes a distortion mechanism.
    if frac > 1e-4:
        print(f"  warning: {clipped} samples clipped ({frac:.3%}) -- lower --level",
              file=sys.stderr)
    return Path(str(path)).with_suffix("." + fmt)


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--radial", type=float, required=True, help="degrees, 0-359")
    ap.add_argument("--type", dest="vor_type", default="cvor",
                    choices=["cvor", "dvor", "dvor-reversed"])
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--fs", type=int, default=1_024_000)
    ap.add_argument("--offset", type=int, default=None, help="Hz, default -fs/4")
    ap.add_argument("--drift-hz", type=float, default=0.0)
    ap.add_argument("--snr-db", type=float, default=20.0)
    ap.add_argument("--noiseless", action="store_true")
    ap.add_argument("--am-depth", type=float, default=0.30)
    ap.add_argument("--sub-depth", type=float, default=0.30)
    ap.add_argument("--deviation", type=float, default=DEVIATION_HZ)
    ap.add_argument("--ident", default="TST")
    ap.add_argument("--no-ident", action="store_true")
    ap.add_argument("--fade", default="none", choices=["none", "slow", "flutter"])
    ap.add_argument("--level", default="auto",
                    help="digital level, or 'auto' (default) to fit signal+noise")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--format", default="cu8", choices=["cu8", "cs8", "cs16", "cf32"])
    ap.add_argument("--nav-freq", type=float, default=109.4, help="MHz, sidecar only")
    ap.add_argument("-o", "--out", type=Path, default=None)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    p = VorParams(
        radial_deg=a.radial, vor_type=a.vor_type, fs=a.fs, seconds=a.seconds,
        offset_hz=a.offset, drift_hz=a.drift_hz,
        snr_db=None if a.noiseless else a.snr_db,
        am_depth=a.am_depth, sub_depth=a.sub_depth, deviation_hz=a.deviation,
        ident="" if a.no_ident else a.ident, fade=a.fade, seed=a.seed,
        level="auto" if a.level == "auto" else float(a.level),
    )
    out = a.out or Path(f"synth_{a.vor_type}_{int(a.radial):03d}deg")
    path = write_file(p, out, fmt=a.format, nav_freq_hz=int(round(a.nav_freq * 1e6)))
    print(f"wrote {path}  ({p.n_samples} samples, {p.seconds:g} s, {a.format})")
    print(f"  level {p.level:.4f} "
          f"({'auto' if a.level == 'auto' else 'fixed'})")
    print(f"  {a.vor_type}, radial {a.radial:g} deg, "
          f"SNR {'none' if a.noiseless else f'{a.snr_db:g} dB'} in "
          f"{CHANNEL_BW_HZ/1e3:g} kHz, offset {p.offset_hz/1e3:+g} kHz")
    print(f"  sidecar {path.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
