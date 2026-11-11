#!/usr/bin/env python3
"""Localizer accuracy and validity sweep.

Reference: docs/loc-plan.md L3/L4 (sections 4.1-4.3); CLAUDE.md A34-A37, D9.
The localizer twin of `tools/vor_sweep.py`. Every number in docs/loc-sweep.md
and in the localizer amendments comes from a run of this script.

Sections
--------
  accuracy   DDM error against channel SNR and true DDM, for the raw form
             (m150 - m90) and the SDM-normalised form (0.40 * DDM / SDM), plus
             the validity verdict of `LocQuality` -- plan 4.3 and 4.1
  tolerance  DDM error across the legal tone-frequency tolerance, for the tone
             search, the band-power estimator and a fixed bin -- plan 4.2
  sdm        DDM error when the transmitted SDM is not 0.40 (Annex 10 Vol I
             allows 0.30-0.60), for both forms
  window     DDM noise against integration time (the lag trade)
  validity   error binned by measured tone SNR (where the thresholds came
             from), an intercept ramp, tone-offset steadiness
  noise      noise only, quantised to cu8 like a dongle, several levels and
             seeds: what a localizer decoder reads when there is no localizer
  carrier    carrier offset with and without AFC, and a two-frequency
             localizer (course + clearance carriers 5-14 kHz apart)
  publish    POSTs per minute of a steady signal, with and without the
             hysteresis of the loc* fields (A38)
  cpu        demodulator cost per second of signal at 1.024 Msps, LOC vs VOR

    ./loc_sweep.py --md docs/loc-sweep.md
    ./loc_sweep.py --sections tolerance noise --quick
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.dsp import DspParams, VorDemod  # noqa: E402
from rbvor.loc import (  # noqa: E402
    DDM_FULL_SCALE,
    LocDemod,
    LocDspParams,
    LocQuality,
    LocQualityParams,
)
from tools.loc_gen import LocParams, generate_blocks  # noqa: E402
from tools.vor_gen import VorParams  # noqa: E402
from tools.vor_gen import generate_blocks as vor_blocks  # noqa: E402

FS = 256_000
SKIP_S = 1.5          # filter + AGC settling (tau 0.5 s) before a window counts


def observe(gen_kw: dict, dsp_kw: dict | None = None, fs: int = FS,
            seconds: float = 12.0) -> list:
    gen_kw = dict(gen_kw)
    gen_kw.setdefault("ident", "ITST")
    p = LocParams(fs=fs, offset_hz=-fs // 4, seconds=seconds, **gen_kw)
    d = LocDemod(LocDspParams(fs=fs, offset_hz=-fs // 4, **(dsp_kw or {})))
    obs = []
    for b in generate_blocks(p):
        obs += d.push(b)
    return [o for o in obs if o.t - d.p.integration_s / 2 >= SKIP_S and not o.overrun]


def judge(obs: list, qp: LocQualityParams | None = None) -> list:
    q = LocQuality(qp)
    return [q.update(o, o.ddm) for o in obs]


def _fmt(x: float, nd: int = 4) -> str:
    return f"{x:+.{nd}f}"


def sec_accuracy(out: list[str], seeds: int, snrs, ddms, seconds: float) -> None:
    out += ["## Accuracy against SNR and true DDM (plan 4.3)", "",
            f"fs {FS} Hz, {seconds:g} s per point, {seeds} seed(s), default window "
            "(1 s at 0.5 s hop), SDM 0.40, ident on. Error = measured - true. "
            "`raw` is m150 - m90; `norm` is 0.40 x DDM / SDM; `pub` is the published "
            "form, DDM x max(1, 0.40 / SDM) (D9). Flagged: windows "
            "`LocQuality` (shipped defaults) rejects.", "",
            "| SNR dB | true DDM | raw bias | raw RMS | norm bias | norm RMS | "
            "pub bias | pub RMS | SDM read | tone SNR dB | flagged |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    summary = []
    for snr in snrs:
        all_raw, all_norm, all_pub, all_flag = [], [], [], []
        for ddm in ddms:
            raw, norm, pub, sdm, tsnr, flag = [], [], [], [], [], []
            for s in range(seeds):
                obs = observe({"ddm": ddm, "snr_db": snr, "seed": s}, seconds=seconds)
                res = judge(obs)
                raw += [o.ddm - ddm for o in obs]
                norm += [o.ddm_norm - ddm for o in obs]
                pub += [o.ddm_corrected - ddm for o in obs]
                sdm += [o.sdm for o in obs]
                tsnr += [o.snr_db for o in obs]
                flag += [r.flagged and not r.warming for r in res[1:]]
            all_raw += raw
            all_norm += norm
            all_pub += pub
            all_flag += flag
            out.append(f"| {snr:g} | {ddm:+.4f} | {_fmt(np.mean(raw))} | "
                       f"{np.sqrt(np.mean(np.square(raw))):.4f} | {_fmt(np.mean(norm))} | "
                       f"{np.sqrt(np.mean(np.square(norm))):.4f} | {_fmt(np.mean(pub))} | "
                       f"{np.sqrt(np.mean(np.square(pub))):.4f} | {np.mean(sdm):.3f} | "
                       f"{np.mean(tsnr):.1f} | {100 * np.mean(flag):.0f}% |")
        summary.append((snr, np.sqrt(np.mean(np.square(all_raw))),
                        np.sqrt(np.mean(np.square(all_norm))),
                        np.max(np.abs(all_raw)), 100 * np.mean(all_flag),
                        np.sqrt(np.mean(np.square(all_pub))), np.max(np.abs(all_pub))))
    out += ["", "Summary over all true DDMs (RMS in DDM and in % of full scale 0.155):", "",
            "| SNR dB | raw RMS %FS | raw max %FS | norm RMS %FS | pub RMS %FS | "
            "pub max %FS | flagged |",
            "|---|---|---|---|---|---|---|"]
    for snr, r, n, mx, fl, pb, pmx in summary:
        out.append(f"| {snr:g} | {100 * r / DDM_FULL_SCALE:.1f} | "
                   f"{100 * mx / DDM_FULL_SCALE:.1f} | {100 * n / DDM_FULL_SCALE:.1f} | "
                   f"{100 * pb / DDM_FULL_SCALE:.1f} | {100 * pmx / DDM_FULL_SCALE:.1f} | "
                   f"{fl:.0f}% |")
    out.append("")


def sec_tolerance(out: list[str], snrs, seconds: float, seeds: int) -> None:
    eps_list = [-0.03, -0.025, -0.02, -0.01, 0.0, 0.01, 0.02, 0.025, 0.03]
    est = {"search": {}, "band": {"estimator": "band"},
           "fixed bin": {"tone_search": 1e-6, "tone_step": 1e-6}}
    for snr in snrs:
        out += [f"## Tone-frequency tolerance, {'noiseless' if snr is None else f'{snr:g} dB'} "
                "(plan 4.2)", "",
                "True DDM +0.1000. Cat I allows +/-2.5 %, the search covers +/-3 %. "
                f"Cell: mean DDM error / RMS over {seeds} seed(s)." if snr is not None else
                "True DDM +0.1000. Cat I allows +/-2.5 %, the search covers +/-3 %. "
                "Cell: mean DDM error.", "",
                "| tone offset | " + " | ".join(est) + " | eps found |",
                "|---|" + "---|" * (len(est) + 1)]
        for eps in eps_list:
            cells, found = [], 0.0
            for name, kw in est.items():
                errs = []
                for s in range(seeds if snr is not None else 1):
                    obs = observe({"ddm": 0.1, "tone_offset": eps, "snr_db": snr, "seed": s},
                                  kw, seconds=seconds)
                    errs += [o.ddm - 0.1 for o in obs]
                    if name == "search":
                        found = float(np.mean([o.tone_offset for o in obs]))
                rms = np.sqrt(np.mean(np.square(errs)))
                cells.append(_fmt(np.mean(errs)) if snr is None
                             else f"{_fmt(np.mean(errs))} / {rms:.4f}")
            out.append(f"| {100 * eps:+.1f} % | " + " | ".join(cells)
                       + f" | {100 * found:+.2f} % |")
        out.append("")


def sec_sdm(out: list[str], seconds: float) -> None:
    out += ["## Transmitted SDM other than 0.40", "",
            "Annex 10 Vol I 3.1.3.5.2 allows each tone 18-22 % on the course line, and "
            "3.1.3.5.3.6.1 an SDM of 30-60 % in coverage. 20 dB channel SNR, seed 0.", "",
            "| true SDM | true DDM | raw error | norm error | norm error %FS | "
            "pub error | pub error %FS |",
            "|---|---|---|---|---|---|---|"]
    for sdm in (0.30, 0.36, 0.40, 0.44, 0.60):
        for ddm in (0.0775, 0.155):
            obs = observe({"ddm": ddm, "sdm": sdm, "snr_db": 20.0}, seconds=seconds)
            r = np.mean([o.ddm - ddm for o in obs])
            n = np.mean([o.ddm_norm - ddm for o in obs])
            c = np.mean([o.ddm_corrected - ddm for o in obs])
            out.append(f"| {sdm:.2f} | {ddm:+.4f} | {_fmt(r)} | {_fmt(n)} | "
                       f"{100 * n / DDM_FULL_SCALE:+.1f} | {_fmt(c)} | "
                       f"{100 * c / DDM_FULL_SCALE:+.1f} |")
    out.append("")


def sec_window(out: list[str], snrs, seconds: float, seeds: int) -> None:
    out += ["## Integration time against DDM noise (the lag trade)", "",
            "Std of the raw DDM over windows, true DDM +0.0775, hop = half the window "
            f"(1/3 for 1.5 s). {seeds} seed(s). Lag of a window = half its length.", "",
            "| window s | hop s | " + " | ".join(f"std @ {s:g} dB (%FS)" for s in snrs) + " |",
            "|---|---|" + "---|" * len(snrs)]
    for w, h in ((0.5, 0.2), (1.0, 0.5), (1.5, 0.5), (2.0, 1.0)):
        cells = []
        for snr in snrs:
            sd = []
            for s in range(seeds):
                obs = observe({"ddm": 0.0775, "snr_db": snr, "seed": s},
                              {"integration_s": w, "hop_s": h}, seconds=seconds)
                sd.append(np.std([o.ddm for o in obs]))
            v = float(np.mean(sd))
            cells.append(f"{v:.4f} ({100 * v / DDM_FULL_SCALE:.1f})")
        out.append(f"| {w:g} | {h:g} | " + " | ".join(cells) + " |")
    out.append("")


def _quantise_cu8(z: np.ndarray) -> np.ndarray:
    """What a dongle delivers: 8-bit offset binary, decoded the way record.py does."""
    q = np.clip(np.round(np.stack([z.real, z.imag]) * 127.5 + 127.5), 0, 255)
    return ((q[0] - 127.5) / 127.5 + 1j * (q[1] - 127.5) / 127.5).astype(np.complex64)


def noise_windows(rms: float, seed: int, seconds: float, fs: int = FS,
                  dsp_kw: dict | None = None) -> list:
    rng = np.random.default_rng(seed)
    d = LocDemod(LocDspParams(fs=fs, offset_hz=-fs // 4, **(dsp_kw or {})))
    obs, n, blk = [], int(seconds * fs), 1 << 16
    for i in range(0, n, blk):
        m = min(blk, n - i)
        z = (rng.normal(0, rms, m) + 1j * rng.normal(0, rms, m))
        obs += d.push(_quantise_cu8(z))
    return [o for o in obs if o.t - d.p.integration_s / 2 >= SKIP_S]


def sec_noise(out: list[str], seeds: int, seconds: float) -> None:
    out += ["## Noise only (plan 4.1)", "",
            "Complex Gaussian noise, quantised to cu8 like a dongle, at several RMS "
            "levels in full-scale units (0.02 is ~2.5 counts, quantisation-dominated; "
            "0.3 approaches clipping). Shipped defaults. No window may be valid.", "",
            "| noise RMS | windows | DDM mean | DDM std | |DDM| max | SDM mean / max | "
            "tone SNR mean / max dB | eps std | valid windows |",
            "|---|---|---|---|---|---|---|---|---|"]
    qp = LocQualityParams()
    total, valid_total = 0, 0
    passes = {"tone SNR": 0, "SDM": 0, "tone offset steady": 0}
    for rms in (0.02, 0.05, 0.1, 0.2, 0.3):
        obs = []
        valid = 0
        for s in range(seeds):
            o = noise_windows(rms, 100 + s, seconds)
            res = judge(o)
            valid += sum(not r.flagged for r in res)
            passes["tone SNR"] += sum(x.snr_db >= qp.flag_snr_db for x in o)
            passes["SDM"] += sum(x.sdm >= qp.flag_sdm for x in o)
            passes["tone offset steady"] += sum(
                np.std([x.tone_offset for x in o[i - qp.history + 1:i + 1]]) <= qp.flag_eps_std
                for i in range(qp.history - 1, len(o)))
            obs += o
        total += len(obs)
        valid_total += valid
        ddm = np.array([o.ddm for o in obs])
        out.append(f"| {rms:g} | {len(obs)} | {_fmt(ddm.mean())} | {ddm.std():.4f} | "
                   f"{np.abs(ddm).max():.4f} | {np.mean([o.sdm for o in obs]):.3f} / "
                   f"{np.max([o.sdm for o in obs]):.3f} | "
                   f"{np.mean([o.snr_db for o in obs]):.1f} / "
                   f"{np.max([o.snr_db for o in obs]):.1f} | "
                   f"{np.std([o.tone_offset for o in obs]) * 100:.2f} % | {valid} |")
    out += ["", f"Total: {valid_total} valid of {total} noise windows. Windows that "
            "would pass each criterion on its own (any one of them suffices to reject "
            "noise): " + ", ".join(f"{k} {v}" for k, v in passes.items()) + ".", ""]


def sec_carrier(out: list[str], seconds: float) -> None:
    out += ["## Carrier offset, AFC, and two-frequency localizers", "",
            "True DDM +0.0775, 20 dB channel SNR, seed 0, 16 s. `afc off` sets "
            "`afc_threshold_hz` 0. Annex 10 Vol I 3.1.3.2.1: single-carrier tolerance "
            "0.005 % (5.6 kHz at 111.95 MHz); two carriers 5-14 kHz apart, symmetric "
            "about the channel. Clearance carrier with DDM 0 unless stated.", "",
            "| case | AFC | DDM error | SDM | tone SNR dB | carrier_offset_hz (last) |",
            "|---|---|---|---|---|---|"]
    cases = [("single carrier, on frequency", {}),
             ("single carrier, +4 kHz", {"drift_hz": 4000.0}),
             ("single carrier, -5.5 kHz", {"drift_hz": -5500.0}),
             ("two carriers 5 kHz, clearance -10 dB", {"clearance_sep_hz": 5000.0}),
             ("two carriers 8 kHz, clearance -10 dB", {"clearance_sep_hz": 8000.0}),
             ("two carriers 14 kHz, clearance -10 dB", {"clearance_sep_hz": 14000.0}),
             ("two carriers 8 kHz, clearance -3 dB",
              {"clearance_sep_hz": 8000.0, "clearance_db": -3.0}),
             ("two carriers 8 kHz, clearance 0 dB",
              {"clearance_sep_hz": 8000.0, "clearance_db": 0.0}),
             ("two carriers 8 kHz, clearance +6 dB, DDM 0.30",
              {"clearance_sep_hz": 8000.0, "clearance_db": 6.0, "clearance_ddm": 0.30}),
             ("two carriers 14 kHz, clearance -10 dB, +2 kHz",
              {"clearance_sep_hz": 14000.0, "drift_hz": 2000.0})]
    for name, kw in cases:
        for afc in ("on", "off"):
            g = {"ddm": 0.0775, "snr_db": 20.0, **kw}
            obs = observe(g, {} if afc == "on" else {"afc_threshold_hz": 0.0},
                          seconds=seconds)
            out.append(f"| {name} | {afc} | {_fmt(np.mean([o.ddm for o in obs]) - 0.0775)} | "
                       f"{np.mean([o.sdm for o in obs]):.3f} | "
                       f"{np.mean([o.snr_db for o in obs]):.1f} | "
                       f"{obs[-1].carrier_offset_hz:+.0f} |")
    out.append("")


def sec_validity(out: list[str], seeds: int, seconds: float) -> None:
    """Where the thresholds of LocQualityParams came from (A37)."""
    rows = []
    for snr in (12, 10, 8, 6, 5, 4, 3, 2, 1, 0, -1):
        for ddm in (-0.155, 0.0, 0.0775, 0.155):
            for s in range(seeds):
                for o in observe({"ddm": ddm, "snr_db": snr, "seed": s}, seconds=seconds):
                    rows.append((snr, o.snr_db, o.sdm, o.ddm_corrected - ddm, o.ddm - ddm))
    r = np.array(rows)
    fs = 100.0 / DDM_FULL_SCALE
    out += ["## Validity: error against measured tone SNR (thresholds, A37)", "",
            f"Channel SNR -1..12 dB x true DDM (-0.155, 0, +0.0775, +0.155) x {seeds} "
            f"seed(s), {seconds:g} s each, every settled window, binned by the tone SNR "
            "the receiver itself measures. Errors in % of full scale.", "",
            "| tone SNR dB | windows | pub RMS | pub max | raw RMS | SDM mean |",
            "|---|---|---|---|---|---|"]
    for lo in range(14, 40, 2):
        m = (r[:, 1] >= lo) & (r[:, 1] < lo + 2)
        if m.sum():
            out.append(f"| {lo}-{lo + 2} | {m.sum()} | {fs * np.sqrt(np.mean(r[m, 3] ** 2)):.1f} | "
                       f"{fs * np.abs(r[m, 3]).max():.1f} | "
                       f"{fs * np.sqrt(np.mean(r[m, 4] ** 2)):.1f} | {r[m, 2].mean():.3f} |")
    out += ["", "| channel SNR dB | tone SNR mean (std) | SDM mean | pub RMS |",
            "|---|---|---|---|"]
    for snr in np.unique(r[:, 0])[::-1]:
        m = r[:, 0] == snr
        out.append(f"| {snr:g} | {r[m, 1].mean():.1f} ({r[m, 1].std():.1f}) | "
                   f"{r[m, 2].mean():.3f} | {fs * np.sqrt(np.mean(r[m, 3] ** 2)):.1f} |")
    out.append("")

    # intercept: the DDM ramps through the course; must not be "unstable"
    from tools.loc_gen import ddm_at
    out += ["### A localizer intercept", "",
            "DDM ramping from -0.2 to +0.2 over 20 s (0.02 DDM/s, full scale in under "
            "8 s), 20 dB channel SNR. The stability term is judged about a straight "
            "line, so a ramp is not instability.", "",
            "| quantity | value |", "|---|---|"]
    p = LocParams(fs=FS, offset_hz=-FS // 4, seconds=20.0, ddm=-0.2, ddm_rate=0.02,
                  snr_db=20.0, ident="ITST")
    d = LocDemod(LocDspParams(fs=FS, offset_hz=-FS // 4))
    obs = [o for b in generate_blocks(p) for o in d.push(b)]
    obs = [o for o in obs if o.t >= 2.0]
    res = judge(obs)
    lag_err = [o.ddm_corrected - float(ddm_at(p, o.t)) for o in obs]
    out += [f"| windows judged | {len(res) - 2} |",
            f"| flagged (not warming) | {sum(r.flagged and not r.warming for r in res)} |",
            f"| mean error vs DDM at window centre | {_fmt(np.mean(lag_err))} |",
            f"| RMS error vs DDM at window centre | {np.sqrt(np.mean(np.square(lag_err))):.4f} |",
            ""]

    # tone-offset steadiness at the threshold
    eps_sd = []
    for s in range(seeds):
        obs = observe({"ddm": 0.0775, "snr_db": 5.0, "seed": s, "tone_offset": 0.02},
                      seconds=seconds)
        eps_sd += [np.std([o.tone_offset for o in obs[i:i + 5]])
                   for i in range(len(obs) - 4)]
    out += [f"Tone-offset steadiness at 5 dB channel SNR (tone SNR ~26 dB, the flag "
            f"threshold): std over 5 windows mean {100 * np.mean(eps_sd):.3f} %, max "
            f"{100 * np.max(eps_sd):.3f} %; flag at 0.5 %. Noise: see below.", ""]


def posts_per_minute(snr_db: float, navaid: str, deadband: dict | None,
                     seconds: float = 60.0, ddm: float = 0.0775) -> tuple[float, int]:
    """POSTs per minute of steady signal through the real controller and
    publisher (shipped keep-alive 5 s), and the windows decoded."""
    import copy
    import json

    from rbvor import publisher as pubmod
    from rbvor.config import Config
    from rbvor.controller import Controller
    from rbvor.publisher import Publisher, RecordingTransport

    raw = json.loads((Path(__file__).resolve().parents[1] / "config" / "rb-vor.json").read_text())
    raw = copy.deepcopy(raw)
    raw["sdr"].update(sample_rate_hz=FS, tuner_offset_hz=-FS // 4)
    raw["loc"] = {"enabled": True}
    saved = dict(pubmod.LOC_DEADBAND)
    if deadband is not None:
        pubmod.LOC_DEADBAND.update(deadband)
    try:
        cfg = Config(raw=raw)
        t = [0.0]
        clock = lambda: t[0]                                      # noqa: E731
        tr = RecordingTransport()
        c = Controller(cfg, _NullSdr(), Publisher(cfg.publish, tr, clock=clock), clock=clock)
        c.set_channel(109.5 if navaid == "loc" else 109.4)
        for _ in range(10):
            t[0] += 0.1
            c.tick()
        blk = 1 << 15
        if navaid == "loc":
            gen = generate_blocks(LocParams(fs=FS, offset_hz=-FS // 4, seconds=seconds + 4,
                                            snr_db=snr_db, ddm=ddm, block=blk))
        else:
            gen = vor_blocks(VorParams(radial_deg=137, fs=FS, offset_hz=-FS // 4,
                                       seconds=seconds + 4, snr_db=snr_db, block=blk))
        n0, windows = None, 0
        for b in gen:
            windows += c.push(b)
            t[0] += blk / FS
            c.tick()
            if n0 is None and t[0] >= 5.0:
                n0 = len(tr.sent)
        return 60.0 * (len(tr.sent) - n0) / (t[0] - 5.0), windows
    finally:
        pubmod.LOC_DEADBAND.clear()
        pubmod.LOC_DEADBAND.update(saved)


class _NullSdr:
    def set_frequency(self, nav_hz):
        pass

    def close(self):
        pass


def sec_publish(out: list[str]) -> None:
    from rbvor.publisher import LOC_DEADBAND
    plain = dict.fromkeys(LOC_DEADBAND, 0.5)
    out += ["## Publish rate (A38)", "",
            "POSTs per minute of a steady signal (DDM +0.0775, or radial 137), measured "
            "through the real Controller and Publisher, shipped keep-alive 5 s, after a "
            "5 s settling period, 60 s. The localizer decodes 2 windows/s (1 s at 0.5 s "
            "hop), so 120/min is a POST on every window. `plain` rounds every field; "
            f"`shipped` applies the deadbands {LOC_DEADBAND} (wire units).", "",
            "| channel SNR dB | LOC plain | LOC shipped | VOR (for comparison) |",
            "|---|---|---|---|"]
    for snr in (30, 20, 10, 6):
        a, _ = posts_per_minute(snr, "loc", plain)
        b, _ = posts_per_minute(snr, "loc", None)
        v, _ = posts_per_minute(snr, "vor", None)
        out.append(f"| {snr} | {a:.0f} | {b:.0f} | {v:.0f} |")
    out.append("")


def sec_cpu(out: list[str]) -> None:
    fs, secs = 1_024_000, 20.0
    lp = LocParams(fs=fs, seconds=secs, snr_db=20.0, block=65536, level=0.5)
    vp = VorParams(radial_deg=137, fs=fs, seconds=secs, snr_db=20.0, block=65536, level=0.5)
    lb, vb = list(generate_blocks(lp)), list(vor_blocks(vp))
    rows = []
    for name, demod, blocks in (
            ("VOR (discriminator)", VorDemod(DspParams(fs=fs, integration_s=2.0)), vb),
            ("VOR (harmonic)", VorDemod(DspParams(fs=fs, integration_s=2.0,
                                                  fm_estimator="harmonic")), vb),
            ("LOC (search, 1 s / 0.5 s)", LocDemod(LocDspParams(fs=fs)), lb),
            ("LOC (search, 0.5 s / 0.2 s)",
             LocDemod(LocDspParams(fs=fs, integration_s=0.5, hop_s=0.2)), lb)):
        best = []
        for _ in range(3):
            demod.reset()
            t0 = time.perf_counter()
            for b in blocks:
                demod.push(b)
            best.append(time.perf_counter() - t0)
        rows.append((name, 100 * min(best) / secs))
    out += ["## CPU", "",
            f"Demodulator only, {secs:g} s of 1.024 Msps in 65536-sample blocks, best of 3, "
            "this machine, BLAS single-threaded (conftest/_threadcaps not applied: run "
            "with OPENBLAS_NUM_THREADS=1).", "",
            "| demodulator | % of one core |", "|---|---|"]
    out += [f"| {n} | {p:.2f} |" for n, p in rows] + [""]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sections", nargs="+",
                    default=["accuracy", "tolerance", "sdm", "window", "validity",
                             "noise", "carrier", "publish", "cpu"])
    ap.add_argument("--quick", action="store_true", help="fewer seeds and points")
    ap.add_argument("--md", type=Path, default=None)
    a = ap.parse_args(argv)
    seeds = 1 if a.quick else 3
    secs = 8.0 if a.quick else 12.0
    out = ["# RB-VOR localizer sweep", "",
           "Generated by `tools/loc_sweep.py` against `tools/loc_gen.py` (synthetic "
           "signals only; no real localizer has been decoded). Channel SNR is in a "
           "12 kHz bandwidth, as for the VOR sweeps. DDM is signed, + = 150 Hz "
           "predominates.", ""]
    snrs = [30, 20, 15, 10, 6, 3, 0, -3]
    ddms = [-0.155, -0.0775, 0.0, 0.0775, 0.155, 0.2]
    for s in a.sections:
        t0 = time.perf_counter()
        if s == "accuracy":
            sec_accuracy(out, seeds, snrs, ddms, secs)
        elif s == "tolerance":
            sec_tolerance(out, [None, 10.0, 3.0], secs, seeds)
        elif s == "sdm":
            sec_sdm(out, secs)
        elif s == "window":
            sec_window(out, [20, 10, 3], 16.0, seeds)
        elif s == "validity":
            sec_validity(out, seeds, 10.0)
        elif s == "noise":
            sec_noise(out, 2 if a.quick else 4, 20.0 if a.quick else 30.0)
        elif s == "carrier":
            sec_carrier(out, 16.0)
        elif s == "publish":
            sec_publish(out)
        elif s == "cpu":
            sec_cpu(out)
        else:
            ap.error(f"unknown section {s}")
        print(f"  {s} done in {time.perf_counter() - t0:.0f} s", file=sys.stderr)
    text = "\n".join(out) + "\n"
    if a.md:
        a.md.write_text(text)
        print(f"wrote {a.md}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
