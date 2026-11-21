#!/usr/bin/env python3
"""Glide-slope measurements behind every default of rbvor/gs.py (G3-G6).

Writes docs/gs-sweep.md. Each section is a decision recorded in CLAUDE.md:

- accuracy and DDM form (A42)           - carrier offset and acquisition (A41)
- deep modulation, tone tolerance (G3)  - two-frequency glide paths (A41)
- noise only, validity thresholds (A43) - window against lag (A43)
- publish rate with two chains (A44)    - CPU

    python3 tools/gs_sweep.py              # full, a few minutes
    python3 tools/gs_sweep.py --quick      # fewer seeds, for a smoke run

Synthetic only: generator and decoder share an author (O23's lesson). The
sense and scale are confirmed by a real glide path (G8) and by nothing here.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor import _threadcaps  # noqa: E402,F401
from rbvor.gs import (  # noqa: E402
    DDM_FULL_SCALE,
    SDM_NOMINAL,
    GsDemod,
    GsDspParams,
    GsQualityParams,
)
from rbvor.loc import LocQuality  # noqa: E402
from tools.gs_gen import GsParams, generate_blocks  # noqa: E402

FS = 256_000
SKIP_S = 1.5
ACQ = 30_000.0


def observe(gen_kw: dict, dsp_kw: dict | None = None, fs: int = FS,
            seconds: float = 10.0, skip: float = SKIP_S) -> tuple[list, GsDemod]:
    p = GsParams(fs=fs, offset_hz=-fs // 4, seconds=seconds, **gen_kw)
    dk = {"acquire_hz": ACQ, **(dsp_kw or {})}
    d = GsDemod(GsDspParams(fs=fs, offset_hz=-fs // 4, **dk))
    obs = []
    for b in generate_blocks(p):
        obs += d.push(b)
    return [o for o in obs if o.t - d.p.integration_s / 2 >= skip and not o.overrun], d


def rms(x) -> float:
    x = np.asarray(x, dtype=float)
    return float(np.sqrt(np.mean(x ** 2))) if x.size else float("nan")


def pct(x: float) -> str:
    return f"{100 * x / DDM_FULL_SCALE:.1f}"


# --------------------------------------------------------------------------- #

def sec_accuracy(out, seeds):
    """A42: raw, normalised and one-sided DDM forms against SNR and DDM."""
    ddms = (-0.175, -0.0875, 0.0, 0.0875, 0.175)
    out += ["## Accuracy and DDM form (A42)", "",
            "DDM error, RMS in % of full scale (0.175), over DDMs "
            f"{list(ddms)}, {seeds} seeds, 10 s each. `raw` = m150 - m90; `norm` = "
            "0.80 x DDM / SDM; `published` = DDM x max(1, 0.80 / SDM). Also the "
            "mean measured SDM (nominal 0.80) and the raw form's scale "
            "(mean raw / true over the non-zero DDMs).", "",
            "| channel SNR dB | raw | norm | published | mean SDM | raw scale |",
            "|---|---|---|---|---|---|"]
    for snr in (None, 20, 10, 6, 3, 0):
        er, en, ep, sd, sc = [], [], [], [], []
        for ddm in ddms:
            for s in range(seeds if snr is not None else 1):
                obs, _ = observe({"ddm": ddm, "snr_db": snr, "seed": s})
                er += [o.ddm - ddm for o in obs]
                en += [o.ddm_norm - ddm for o in obs]
                ep += [o.ddm_corrected - ddm for o in obs]
                sd += [o.sdm for o in obs]
                if ddm:
                    sc += [o.ddm / ddm for o in obs]
        lab = "noiseless" if snr is None else str(snr)
        out.append(f"| {lab} | {pct(rms(er))} | {pct(rms(en))} | {pct(rms(ep))} | "
                   f"{np.mean(sd):.3f} | {np.mean(sc):.3f} |")
    out += ["", "Scale error at lawful SDMs other than 0.80, noiseless, DDM = full "
            "scale (0.175). Error in % of full scale; + = over-reads.", "",
            "| SDM | raw | norm | published |", "|---|---|---|---|"]
    for sdm in (0.75, 0.78, 0.80, 0.82, 0.85):
        obs, _ = observe({"ddm": 0.175, "sdm": sdm, "snr_db": None})
        out.append(f"| {sdm:.2f} | {pct(np.mean([o.ddm for o in obs]) - 0.175)} | "
                   f"{pct(np.mean([o.ddm_norm for o in obs]) - 0.175)} | "
                   f"{pct(np.mean([o.ddm_corrected for o in obs]) - 0.175)} |")
    out.append("")


def sec_deep(out):
    """G3/4.3: SDM at the tolerance edges with the 150 Hz phase swept."""
    out += ["## Deep modulation (plan 4.3)", "",
            "SDM 0.75 / 0.80 / 0.85 (37.5-42.5 % per tone), 150 Hz phase relative "
            "to 90 Hz swept (in phase = deepest trough), DDM +0.0875, published "
            "form. Max |error| in % of full scale over the phases, and the "
            "envelope's minimum (1 - SDM at worst).", "",
            "| SDM | noiseless max err | 10 dB RMS err | 10 dB mean SDM read |",
            "|---|---|---|---|"]
    for sdm in (0.75, 0.80, 0.85):
        errs, e10, s10 = [], [], []
        for ph in (0.0, 45.0, 90.0, 135.0, 180.0):
            obs, _ = observe({"ddm": 0.0875, "sdm": sdm, "snr_db": None,
                              "phase150_deg": ph}, seconds=6)
            errs += [abs(o.ddm_corrected - 0.0875) for o in obs]
            obs, _ = observe({"ddm": 0.0875, "sdm": sdm, "snr_db": 10.0,
                              "phase150_deg": ph, "seed": int(ph)}, seconds=6)
            e10 += [o.ddm_corrected - 0.0875 for o in obs]
            s10 += [o.sdm for o in obs]
        out.append(f"| {sdm:.2f} | {pct(max(errs))} | {pct(rms(e10))} | "
                   f"{np.mean(s10):.3f} |")
    out.append("")


def sec_tolerance(out):
    """G3: A34's tone search at 40 % depth."""
    out += ["## Tone-frequency tolerance (A34 at 40 % depth)", "",
            "Both tones offset by eps (Cat I allows 2.5 %), DDM +0.0875. Error in % "
            "of full scale. `fixed bin` reads 90 and 150 Hz exactly, for comparison.",
            "", "| eps | search noiseless | search 10 dB RMS | fixed bin noiseless |",
            "|---|---|---|---|"]
    for eps in (-0.03, -0.025, -0.01, 0.0, 0.01, 0.025, 0.03):
        a, _ = observe({"ddm": 0.0875, "snr_db": None, "tone_offset": eps}, seconds=6)
        b, _ = observe({"ddm": 0.0875, "snr_db": 10.0, "tone_offset": eps, "seed": 2},
                       seconds=8)
        c, _ = observe({"ddm": 0.0875, "snr_db": None, "tone_offset": eps}, seconds=6,
                       dsp_kw={"tone_search": 0.0025, "tone_step": 0.0025})
        out.append(f"| {100 * eps:+.1f} % | "
                   f"{pct(np.mean([o.ddm_corrected for o in a]) - 0.0875)} | "
                   f"{pct(rms([o.ddm_corrected - 0.0875 for o in b]))} | "
                   f"{pct(np.mean([o.ddm_corrected for o in c]) - 0.0875)} |")
    out += ["", "(The `fixed bin` column searches only +/-0.25 %, the narrowest grid "
            "the parameters allow; at eps 0 it is the search itself.)", ""]


def sec_carrier(out):
    """A41: DDM and validity against carrier offset, AFC only vs acquisition."""
    out += ["## Carrier offset and coarse acquisition (A41, plan 4.2)", "",
            "Annex 10 allows 0.005 %: **16.75 kHz at 335 MHz**. A 20 ppm dongle adds "
            "6.6 kHz at 330 MHz. DDM +0.0875, 8 s. `AFC only` is the localizer "
            "front end (13 kHz channel filter, AFC after it); `acquire` adds the "
            f"coarse search before the filter (+/-{ACQ / 1000:g} kHz). Each cell: RMS "
            "error % FS / mean SDM / mean tone SNR dB.", "",
            "| offset kHz | SNR | AFC only | acquire | found kHz |", "|---|---|---|---|---|"]
    for snr in (20.0, 6.0):
        for off in (0, 8000, 12000, 14000, 15000, 16000, 16750, 20000, 23350, 28000,
                    -16750, -23350):
            cells = []
            found = 0.0
            for acq in (0.0, ACQ):
                obs, d = observe({"ddm": 0.0875, "snr_db": snr, "drift_hz": float(off),
                                  "seed": 1}, dsp_kw={"acquire_hz": acq}, seconds=8)
                cells.append(f"{pct(rms([o.ddm_corrected - 0.0875 for o in obs]))} / "
                             f"{np.mean([o.sdm for o in obs]):.3f} / "
                             f"{np.mean([o.snr_db for o in obs]):.1f}")
                if acq:
                    found = d.acquired_hz
            out.append(f"| {off / 1000:+.2f} | {snr:g} | {cells[0]} | {cells[1]} | "
                       f"{found / 1000:+.3f} |")
    out.append("")


def sec_two_carrier(out):
    """A41: two-frequency glide paths."""
    out += ["## Two-frequency glide paths (A41)", "",
            "Course carrier at +sep/2 with DDM +0.0875, clearance carrier at -sep/2 "
            "with DDM 0 (its own SDM 0.80), 20 dB. Separation 4-32 kHz is from "
            "memory of Annex 10 3.1.5.2.1 (O31). Cell: published DDM / where the "
            "acquisition and the AFC settled (kHz from the assigned frequency) / "
            "share of windows the validity check passed.",
            "", "| sep kHz | clearance dB | AFC only | acquire |", "|---|---|---|---|"]
    for sep in (4000, 8000, 16000, 32000):
        for cdb in (-10.0, -6.0, 0.0):
            cells = []
            for acq in (0.0, ACQ):
                obs, d = observe({"ddm": 0.0875, "snr_db": 20.0, "clearance_sep_hz": sep,
                                  "clearance_db": cdb, "seed": 4},
                                 dsp_kw={"acquire_hz": acq}, seconds=8)
                dd = np.mean([o.ddm_corrected for o in obs]) if obs else float("nan")
                q = LocQuality(GsQualityParams())
                ok = [not q.update(o, o.ddm_corrected).flagged for o in obs][3:]
                cells.append(f"{dd:+.4f} / {d.carrier_offset_hz / 1000:+.2f} / "
                             f"{100 * np.mean(ok):.0f} %")
            out.append(f"| {sep / 1000:g} | {cdb:g} | {cells[0]} | {cells[1]} |")
    out += ["", "The course DDM is +0.0875; the clearance carrier's DDM is 0. A reading "
            "between the two is envelope capture, as for the localizer (A36).", ""]


def _cu8(z: np.ndarray) -> np.ndarray:
    q = np.clip(np.round(z.real * 127.5 + 127.5), 0, 255)
    r = np.clip(np.round(z.imag * 127.5 + 127.5), 0, 255)
    return ((q - 127.5) / 127.5 + 1j * (r - 127.5) / 127.5).astype(np.complex64)


def noise_obs(level: float, seed: int, seconds: float):
    rng = np.random.default_rng(1000 + seed)
    d = GsDemod(GsDspParams(fs=FS, offset_hz=-FS // 4, acquire_hz=ACQ))
    obs = []
    n = int(seconds * FS)
    for i in range(0, n, 32768):
        m = min(32768, n - i)
        z = level * (rng.standard_normal(m) + 1j * rng.standard_normal(m)) / np.sqrt(2)
        obs += d.push(_cu8(z))
    return [o for o in obs if o.t >= SKIP_S], d


def sec_noise(out, seeds):
    """A43 / plan 4.1: what noise alone decodes as."""
    out += ["## Noise only (plan 4.1)", "",
            "cu8-quantised complex Gaussian noise, 5 levels x "
            f"{seeds} seeds x 30 s, through `GsDemod` with acquisition on.", ""]
    snr, sdm, ddm, eps_sd, peaks, judged = [], [], [], [], [], []
    for lvl in (0.02, 0.05, 0.1, 0.2, 0.4):
        for s in range(seeds):
            obs, d = noise_obs(lvl, s, 30.0)
            peaks.append(d.acquire_peak_db)
            snr += [o.snr_db for o in obs]
            sdm += [o.sdm for o in obs]
            ddm += [o.ddm_corrected for o in obs]
            e = [o.tone_offset for o in obs]
            eps_sd += [np.std(e[i:i + 5]) for i in range(len(e) - 4)]
            q = LocQuality(GsQualityParams())
            judged += [q.update(o, o.ddm_corrected) for o in obs]
    passed = sum(1 for r in judged if not r.flagged)
    out += [f"- windows: {len(snr)}", f"- tone SNR: max {max(snr):.1f} dB, mean "
            f"{np.mean(snr):.1f} dB", f"- SDM: max {max(sdm):.3f}",
            f"- published DDM: mean {np.mean(ddm):+.4f}, std {np.std(ddm):.4f}",
            f"- eps std over 5 windows: mean {100 * np.mean(eps_sd):.2f} %, "
            f"min {100 * np.min(eps_sd):.2f} %",
            f"- acquisition peak against median: max {max(peaks):.1f} dB "
            f"(threshold {GsDspParams.acquire_min_db:g} dB)",
            f"- windows passing every flag: **{passed}**", ""]


def sec_validity(out, seeds):
    """A43: published-DDM error binned by the tone SNR the receiver measures."""
    bins: dict[int, list[float]] = {}
    for snr in range(-1, 13):
        for ddm in (-0.175, -0.0875, 0.0, 0.0875):
            for s in range(seeds):
                obs, _ = observe({"ddm": ddm, "snr_db": float(snr), "seed": 50 + s},
                                 seconds=8)
                for o in obs:
                    b = int(np.floor(o.snr_db / 2) * 2)
                    bins.setdefault(b, []).append(o.ddm_corrected - ddm)
    out += ["## Validity: error against measured tone SNR (A43)", "",
            f"Channel SNR -1..12 dB x 4 DDMs x {seeds} seeds; every window binned by "
            "the tone SNR the receiver measured. % of full scale.", "",
            "| tone SNR dB | windows | RMS | max |", "|---|---|---|---|"]
    for b in sorted(bins):
        if 14 <= b <= 40:
            e = np.asarray(bins[b])
            out.append(f"| {b}-{b + 2} | {e.size} | {pct(rms(e))} | "
                       f"{pct(float(np.max(np.abs(e))))} |")
    out += ["", "Flag rate with the shipped thresholds (`GsQualityParams`), by channel "
            "SNR, DDM +0.0875:", "", "| channel SNR dB | flagged | TRACKING-grade "
            "(value >= 40) of the rest |", "|---|---|---|"]
    for snr in (12, 9, 6, 4, 3, 1, 0):
        r = []
        for s in range(seeds):
            obs, _ = observe({"ddm": 0.0875, "snr_db": float(snr), "seed": 80 + s},
                             seconds=12)
            q = LocQuality(GsQualityParams())
            r += [q.update(o, o.ddm_corrected) for o in obs][3:]
        ok = [x for x in r if not x.flagged]
        trk = sum(1 for x in ok if x.value >= 40)
        out.append(f"| {snr} | {100 * (1 - len(ok) / len(r)):.0f} % | "
                   f"{100 * trk / max(1, len(ok)):.0f} % |")
    out.append("")


def sec_passed(out, seeds):
    """A43: the error of the windows that pass every flag, against the SNR flag."""
    import dataclasses
    out += ["## Error of the windows that pass every flag (A43)", "",
            f"5 DDMs (0, +/-half, +/-full scale) x {seeds + 1} seeds x 10 s per row; only "
            "windows `LocQuality(GsQualityParams)` passes. % of full scale. The question: "
            "does a higher tone-SNR flag buy accuracy, or only cost availability?", "",
            "| channel SNR dB | flag_snr_db | valid | RMS | p99 | max |",
            "|---|---|---|---|---|---|"]
    for snr in (3.5, 4.0, 5.0, 6.0, 8.0):
        for flag in (26.0, 28.0, 30.0):
            errs, n, ok = [], 0, 0
            for ddm in (-0.175, -0.0875, 0.0, 0.0875, 0.175):
                for s in range(seeds + 1):
                    obs, _ = observe({"ddm": ddm, "snr_db": snr, "seed": 300 + s},
                                     seconds=10)
                    q = LocQuality(dataclasses.replace(GsQualityParams(), flag_snr_db=flag))
                    for o in obs:
                        n += 1
                        if not q.update(o, o.ddm_corrected).flagged:
                            ok += 1
                            errs.append(o.ddm_corrected - ddm)
            e = 100 * np.abs(np.asarray(errs or [0.0])) / DDM_FULL_SCALE
            out.append(f"| {snr:g} | {flag:g} | {100 * ok / n:.0f} % | "
                       f"{np.sqrt(np.mean(e ** 2)):.1f} | {np.percentile(e, 99):.1f} | "
                       f"{e.max():.1f} |")
    out += ["", "Valid tops out near 87 %: the first windows of every run are warming "
            "(fewer than three DDMs to judge stability), as for the localizer.", ""]


def sec_window(out, seeds):
    """A43: DDM noise against window length (lag = half a window)."""
    out += ["## Window against lag (A43)", "",
            f"Published-DDM std at 10 and 6 dB, DDM +0.0875, {seeds} seeds; a window "
            "lags by half its length. % of full scale.", "",
            "| window s | hop s | 10 dB | 6 dB | lag s |", "|---|---|---|---|---|"]
    for win, hop in ((0.5, 0.2), (1.0, 0.5), (1.5, 0.5), (2.0, 0.5)):
        cells = []
        for snr in (10.0, 6.0):
            e = []
            for s in range(seeds):
                obs, _ = observe({"ddm": 0.0875, "snr_db": snr, "seed": 20 + s},
                                 dsp_kw={"integration_s": win, "hop_s": hop}, seconds=12)
                e += [o.ddm_corrected - 0.0875 for o in obs]
            cells.append(pct(float(np.std(e))))
        out.append(f"| {win:g} | {hop:g} | {cells[0]} | {cells[1]} | {win / 2:g} |")
    out.append("")


def sec_cpu(out):
    from rbvor.loc import LocDemod, LocDspParams
    fs, secs = 1_024_000, 20.0
    blocks = list(generate_blocks(GsParams(fs=fs, seconds=secs, snr_db=20.0, block=65536,
                                           level=0.5, ddm=0.05)))
    far = list(generate_blocks(GsParams(fs=fs, seconds=secs, snr_db=20.0, block=65536,
                                        level=0.5, ddm=0.05, drift_hz=16_750.0)))
    rows = []
    for name, demod, blks in (
            ("LOC (LocDemod)", LocDemod(LocDspParams(fs=fs)), blocks),
            ("GS, acquisition off", GsDemod(GsDspParams(fs=fs)), blocks),
            ("GS, acquisition on, carrier on nominal", GsDemod(GsDspParams(
                fs=fs, acquire_hz=ACQ)), blocks),
            ("GS, acquisition on, carrier +16.75 kHz (correction active)",
             GsDemod(GsDspParams(fs=fs, acquire_hz=ACQ)), far)):
        best = []
        for _ in range(3):
            demod.reset()
            t0 = time.perf_counter()
            for b in blks:
                demod.push(b)
            best.append(time.perf_counter() - t0)
        rows.append((name, 100 * min(best) / secs))
    out += ["## CPU", "", f"Demodulator only, {secs:g} s of 1.024 Msps, 65536-sample "
            "blocks, best of 3, this machine, BLAS single-threaded. Two receivers run "
            "two chains: LOC + GS.", "", "| demodulator | % of one core |", "|---|---|"]
    out += [f"| {n} | {p:.2f} |" for n, p in rows] + [""]


SECTIONS = ("accuracy", "deep", "tolerance", "carrier", "two", "noise", "validity",
            "passed", "window", "publish", "cpu")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", nargs="*", choices=SECTIONS)
    ap.add_argument("-o", "--output", type=Path,
                    default=Path(__file__).resolve().parents[1] / "docs" / "gs-sweep.md")
    a = ap.parse_args(argv)
    seeds = 1 if a.quick else 3
    want = set(a.only or SECTIONS)
    out = ["# Glide-slope sweep", "",
           "Generated by `tools/gs_sweep.py`" + (" --quick" if a.quick else "") + ". "
           "Synthetic signals only (`tools/gs_gen.py`), so the sense and scale are "
           "confirmed against our own generator and nothing else (O23's lesson, G8). "
           f"fs {FS} Hz unless stated; SDM nominal {SDM_NOMINAL}, full scale DDM "
           f"{DDM_FULL_SCALE}. Each section backs one decision in CLAUDE.md.", ""]
    t0 = time.time()
    for name, fn in (("accuracy", lambda: sec_accuracy(out, seeds)),
                     ("deep", lambda: sec_deep(out)),
                     ("tolerance", lambda: sec_tolerance(out)),
                     ("carrier", lambda: sec_carrier(out)),
                     ("two", lambda: sec_two_carrier(out)),
                     ("noise", lambda: sec_noise(out, seeds + 1)),
                     ("validity", lambda: sec_validity(out, seeds)),
                     ("passed", lambda: sec_passed(out, seeds)),
                     ("window", lambda: sec_window(out, seeds)),
                     ("publish", lambda: sec_publish(out)),
                     ("cpu", lambda: sec_cpu(out))):
        if name in want:
            fn()
            print(f"  {name} done, {time.time() - t0:.0f} s", flush=True)
    a.output.write_text("\n".join(out) + "\n")
    print(a.output)
    return 0


class _Sdr:
    def __init__(self):
        self.freq = None

    def set_frequency(self, hz):
        self.freq = hz

    def close(self):
        pass


def posts_per_minute(snr_db: float, arrangement: str, seconds: float = 60.0) -> float:
    """POSTs per minute of a steady ILS through the real Controller and
    Publisher (shipped keep-alive 5 s). `arrangement`: "loc" (gs disabled,
    v2.3), "two" (dedicated GS dongle), "shared-gs" (one dongle, GS selected)."""
    import copy
    import json

    from rbvor.config import Config
    from rbvor.controller import Controller
    from rbvor.publisher import Publisher, RecordingTransport
    from tools.loc_gen import LocParams

    root = Path(__file__).resolve().parents[1]
    raw = copy.deepcopy(json.loads((root / "config" / "rb-vor.json").read_text()))
    raw["sdr"].update(sample_rate_hz=FS, tuner_offset_hz=-FS // 4)
    raw["loc"] = {"enabled": True}
    raw["gs"] = {"enabled": arrangement != "loc"}
    cfg = Config(raw=raw)
    t = [0.0]
    clock = lambda: t[0]                                          # noqa: E731
    tr = RecordingTransport()
    nav, gsd = _Sdr(), (_Sdr() if arrangement == "two" else None)
    c = Controller(cfg, nav, Publisher(cfg.publish, tr, clock=clock), clock=clock,
                   gs_sdr=gsd)
    blk = 1 << 15
    n = int((seconds + 6) * FS / blk) + 1
    air = {
        109_500_000: generate_blocks(LocParams(fs=FS, offset_hz=-FS // 4, ddm=0.0775,
                                               snr_db=snr_db, seconds=seconds + 8,
                                               block=blk, seed=1)),
        332_600_000: generate_blocks(GsParams(fs=FS, offset_hz=-FS // 4, ddm=0.0875,
                                              snr_db=snr_db, seconds=seconds + 8,
                                              block=blk, seed=2)),
    }
    c.set_channel(109.5)
    if arrangement == "shared-gs":
        c.set_gs_select(1)
    n0 = None
    for _ in range(n):
        t[0] += blk / FS
        c.tick()
        if nav.freq in air:
            c.push(next(air[nav.freq]))
        if gsd is not None and gsd.freq in air:
            c.push_gs(next(air[gsd.freq]))
        if n0 is None and t[0] >= 6.0:
            n0, t0 = len(tr.sent), t[0]
    return 60.0 * (len(tr.sent) - n0) / (t[0] - t0)


def sec_publish(out):
    """A44: POSTs per minute, through the real Controller and Publisher."""
    out += ["## Publish rate (A44)", "",
            "POSTs per minute of a steady ILS (LOC DDM +0.0775, GS DDM +0.0875), real "
            "Controller and Publisher, keep-alive 5 s, after 6 s settling, 60 s. Every "
            "POST is a flight-log row. The localizer alone is the A38 figure for "
            "comparison.", "",
            "| channel SNR dB | LOC only (v2.3) | LOC + GS, two receivers | GS selected, "
            "one receiver |", "|---|---|---|---|"]
    for snr in (30, 20, 10):
        out.append(f"| {snr} | {posts_per_minute(snr, 'loc'):.0f} | "
                   f"{posts_per_minute(snr, 'two'):.0f} | "
                   f"{posts_per_minute(snr, 'shared-gs'):.0f} |")
    out.append("")


if __name__ == "__main__":
    raise SystemExit(main())
