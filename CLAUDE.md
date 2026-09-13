# RB-VOR — working log

Normative documents: `docs/rb-vor-architecture.md` (doc A),
`docs/rb-vor-implementation-plan.md` (plan A). This file records decisions,
amendments and **corrected hypotheses**. Superseded statements are struck
through, never deleted — the correction history is part of the deliverable.

---

## Decisions

### D1 — Python, not C/C++ (2026-07-24)
Front-end load is ~35 Mflop/s against a Pi 5 core in the ~10 Gflop/s class:
roughly 5–10 % of one core in numpy with 32 k blocks, dominated by interpreter
overhead rather than arithmetic. The project's unknowns (CVOR/DVOR sign, filter
group delay, quality thresholds) are all discovered by experiment, and C makes
that loop 5–10× slower for no measurable gain at 1 Hz output.
There is no library called `libSDR`; the real candidates are `librtlsdr`
(device only), **SoapySDR** (multi-vendor HAL) and **liquid-dsp** (C DSP).
Native port is a *gated* decision, not a design choice — see plan A, M8.
`dsp.py` is therefore written as block functions over `np.ndarray`, no Python
objects in the inner loop, all state in a plain dataclass, so the port stays
mechanical and the Python version remains the test oracle.

---

## Amendments to doc A

### A1 — tuner offset −250 kHz → **−256 kHz** (2026-07-24)
−fs/4 at 1.024 Msps. The digital down-mix degenerates to the ±1/±j sequence:
no multiplies, no trig. Free in either language. Applied to `config/rb-vor.json`.

### A2 — `phase_sign` and `phase_offset_deg` ship as `null` (2026-07-24)
Not `0`. The daemon must refuse to publish a bearing before calibration is
recorded. Mitigates F1 (wrong bearing published as valid) and F4 (sign
inversion). Applied to `config/rb-vor.json`.

### A3 — native-port trigger table gates M8 (2026-07-24)
CPU >25 % of one core sustained, any overrun in 8 h, >0.1 % dropped windows, or
measurable contention with RB-01 render / `dump1090`. Measured, not assumed.

### A4 — record format `cs8` → **`cu8`** (2026-07-24)
~~Doc A §11 specifies `record.format: "cs8"`.~~ Wrong for RTL-SDR: librtlsdr
delivers **unsigned** 8-bit offset-binary samples centred on 127.5 (`cu8`), not
signed `cs8`. Mislabelling cu8 as cs8 injects a 127-count DC offset that lands
exactly on the carrier after down-mix and silently corrupts the AM envelope, and
therefore every bearing. Format is now recorded per file in the sidecar and
never assumed; `read_sidecar()` raises rather than guess. Regression test:
`tests/test_record.py::test_cu8_is_centred_not_offset`.
SoapySDR can be asked for `CS8` directly and the capture tool does so — hence
both formats are supported, and the sidecar is what distinguishes them.

### A5 — rule ordering in `freqrules.classify()` (2026-07-24)
First implementation band-checked the **raw** value before snapping to the grid,
which classified 107.999 as out-of-band and 117.975 as out-of-band rather than
off-grid. Corrected order is now normative:
1. coarse band reject (±one grid step), so 0 MHz / 1090 MHz report as
   out-of-band rather than as a grid problem;
2. snap to the 50 kHz grid;
3. band-check the **snapped** channel.
Consequence, deliberate and tested: a value within the snap tolerance of a
band-edge channel *is* that channel (107.999 → 108.000, VOR); 118.000 snaps to
itself and is out of band; 117.975 is off-grid.
Found by `tests/test_freqrules.py`, not by review — which is the point of
deriving the expected classification independently in the test.

### A6 — DVOR does NOT need a flipped `phase_sign` (2026-07-24, M2)
~~Plan A, M4/T4: "DVOR identical with `phase_sign` flipped".~~ Wrong. Making the
receiver output indistinguishable is the entire *purpose* of the DVOR
arrangement: the reference/variable roles swap **and** the rotation sense
reverses, and the two inversions cancel. A real receiver cannot tell CVOR from
DVOR, and neither should ours.

**T4 is corrected to:** CVOR and DVOR must decode to the *same* bearing with the
*same* `phase_sign`; and a deliberately sense-reversed DVOR must decode to the
negation. `tools/vor_gen.py --type dvor-reversed` exists solely to give that
equivalence test teeth — without a case that must fail, the test would pass even
if the generator ignored `--type` entirely.

`phase_sign` remains a calibration constant (O1), but for a real and different
reason: our own choice of which path is A and which is B, and the sign
convention of the FM demodulator, are implementation freedoms that must be
pinned by measurement. Not because of CVOR/DVOR.

### A7 — subcarrier level is a BAND measurement, never a bin (2026-07-24, M2)
Found by a failing test, not by review. The FM modulation index is
`beta = deviation / tone = 480 / 30 = 16`. The residual component at the
subcarrier frequency itself is `J0(16) = -0.175`, i.e. **about 3 % of the
subcarrier power**; the rest is in the sidebands out to roughly ±510 Hz.

A narrowband detector sitting on 9960 Hz therefore reads near-zero on a
perfectly healthy station. Measured on a 0.30-depth synthetic signal: band
figure 0.247, bin figure 0.043.

Consequence for M4: `quality.q_sub` **must** integrate the 9960 ± 900 Hz band.
Doc A §9 says "subcarrier depth vs 30 % nominal" without specifying how it is
measured — that ambiguity is now closed. Pinned by
`tests/test_vor_gen.py::test_subcarrier_bin_at_9960_is_almost_empty`.

### A8 — `q_am` and `q_snr` are correlated, not independent (2026-07-24, M2)
An envelope detector's DC output rises with noise (Rician bias): `mean|z+n|` is
greater than `|z|`. Since modulation depth is measured *against* that DC
estimate, the apparent 30 Hz depth falls as SNR falls even though the
transmitted depth is constant. Measured: 0.30 nominal reads 0.245 at 25 dB SNR
in a 12 kHz channel.

This is correct receiver behaviour and is fine for a quality metric — it
degrades in the right direction. But doc A §9 presents `q_am`, `q_sub`, `q_snr`
and `q_stab` as four independent terms with fixed weights, and three of them
move together. M4 must therefore (a) not treat the 0.30 nominal as achievable at
low SNR, and (b) re-derive the weights empirically from the
`tools/vor_sweep.py` SNR curve rather than keeping the assumed 0.25/0.25/0.30/
0.20 split. The assumed weights stay in the default config as a starting point
and are flagged in `docs/open-items.md` as O10.

### A9 — path B chain ordering: demodulate at 4 kHz, not 1 kHz (2026-07-24, M3)
Doc A §7.2 for path B reads
`BPF 9960±600 → mix → LPF 600 → /32 → 1 kHz → d/dt unwrap(angle) → LPF 100`,
i.e. it decimates to 1 kHz **before** FM demodulation. With β = 16 the
subcarrier occupies ≈ ±600 Hz against a Nyquist limit of 500 Hz, so orders
n = 17…20 fold back.

**Retraction.** The first draft of this amendment stated the ordering "cannot
work". ~~That claim was wrong and is withdrawn.~~ Measured: at nominal
parameters it works. The folded orders carry ~2 % of the power, the
discriminator tolerates the truncation, and the resulting error is a *constant*
offset that calibration removes — the residual spread across radials was
identical for both orderings (0.159°).

**The real objection is margin.** ICAO Annex 10 allows a deviation ratio of
16 ± 1, i.e. 450–510 Hz. Measured THD of the recovered reference tone:

| deviation | ratio | A9 ordering | doc A ordering |
|---|---|---|---|
| 450 Hz | 15.0 | 0.26 % | 0.37 % |
| 480 Hz | 16.0 | 0.29 % | 0.39 % |
| **510 Hz** | **17.0** | **0.32 %** | **85.7 %** |
| 525 Hz | 17.5 | 0.34 % | 242 % |

Doc A's ordering collapses at ratio 17.0 — the upper limit of the *legal*
tolerance, not an abuse case. A station transmitting lawfully would produce
nonsense. Phase stability under noise is also ~6× worse (std 0.779° vs 0.132°
at 10 dB SNR over six seeds).

Corrected chain: demodulate at 4 kHz, decimate to 1 kHz afterwards. Two changes
follow from the same review:
- the pre-mix band-pass is dropped — after mixing by −9960 Hz the image lands at
  −19920 Hz and the post-mix low-pass removes it, so the band-pass was redundant,
  and every redundant filter is another uncalibrated group delay;
- **both paths now share the same two filter stages at the same rates** (LPF 700
  @ 32 kHz then /8; TONE LPF 100 @ 4 kHz then /4). Doc A only required the final
  100 Hz filter to match, leaving the earlier stages contributing an uncancelled
  differential delay.

`DspParams.__post_init__` now refuses any configuration with
`sub_lp_hz >= TONE_RATE_HZ/2` — the guard that would have caught this before it
shipped. Evidence lives in `tests/test_dsp.py::test_docA_ordering_fails_inside_icao_tolerance`.

### A10 — measured residual, ready for M4 (2026-07-24, M3)
The corrected chain yields a residual of **+1.96°**, constant across radials
0–359 (spread < 0.04° noiseless, < 0.2° at 25 dB SNR). This is the value M4 will
formalise as `bearing.phase_offset_deg`. It is close to the 1.35° predicted for
the one-sample conjugate-product discriminator delay at 4 kHz; the remainder is
residual decimation-phase mismatch between the paths. `phase_sign` is **+1** for
this implementation — but O1 stays open until a real station confirms it, since
the synthetic generator and the demodulator share my sign conventions and could
be consistently wrong together.

### A11 — quality metric re-derived from measurement, closing O10 (2026-07-24, M4)
Doc A §9 specified four terms with weights 0.25/0.25/0.30/0.20 and an SNR
mapping of 0–20 dB. The first sweep exposed three defects.

**1. The SNR mapping was against the wrong quantity.** Doc A's 0–20 dB is a
*channel* SNR. What the DSP reports is the post-integration 30 Hz tone against
the off-tone bins, which carries ~28 dB of processing gain from the 1 s window
and spans **17–68 dB** in practice. `q_snr` therefore read 1.000 at every SNR
from +40 down to −6 dB — a constant 0.30 added to every score, inflating
`vorsignal` precisely where it needed to fall. Remapped to 26–46 dB.

**2. The flag thresholds let F1 through.** With a single 0.15 threshold on
`q_am`/`q_sub`, a signal at −3 dB channel SNR — **17.6° RMS bearing error** —
was published unflagged, because `q_am` was 0.199. Thresholds are now per term
and set from the measured error curve: `q_sub` 0.15 (presence check only, its
range is compressed to 0.62–0.96), `q_am` 0.35, and a new flag on `q_stab` 0.40.
Acquisition with fewer than two bearings is also flagged.

**3. Weights.** With the SNR term fixed, all four terms correlate with error at
ρ ≈ −0.98, confirming A8's point that they are not independent. `q_stab` has the
widest usable range and collapses exactly where the error explodes, so it takes
the largest weight. Final: **am 0.25, sub 0.10, snr 0.25, stab 0.40.**

Resulting behaviour (12 radials × 8 SNRs, `docs/sweep.md`):

| channel SNR | RMS err | vorsignal | flagged |
|---|---|---|---|
| 30 dB | 0.041° | 99 | 0 % |
| 10 dB | 0.474° | 85 | 0 % |
| 3 dB | 2.33° | 55 | 0 % |
| 0 dB | 6.54° | 6 | 83 % |
| −3 dB | 17.4° | 0 | 100 % |

The flag now asserts as the error crosses the 5° T11 limit, which is what
`vorsignal` exists to do. **O10 is closed.**

### A12 — `wrap360` could return 360.0 (2026-07-24, M4)
`x % 360.0` on a tiny negative float returns exactly 360.0, because
360.0 − 1e-15 is not representable. `circular_mean([359, 1])` produces exactly
such a value, so the correct answer 0° was published as 360° — outside the 0–359
range declared by RB-VOR-IF v1.0. Also `round()` uses banker's rounding, so
`round(180.5)` is 180. Both fixed; `to_int_bearing()` now uses `floor(x + 0.5)`,
matching the convention already adopted in `freqrules.channel_khz`.

### A13 — DEGRADED reconciled with the A11 flag (2026-07-24, M6)
Doc A §7.1 defines DEGRADED as "quality below threshold but > 0", publishing the
last good bearing with a "true low vorsignal". A11 then made the NAV flag force
`vorsignal` to 0. Read together, DEGRADED and NO_SIGNAL became identical on the
wire, leaving DEGRADED meaningless as a published distinction.

Normative reconciliation:

| state | entered when | publishes |
|---|---|---|
| TRACKING | not flagged, value ≥ 40 | current bearing, real value |
| DEGRADED | not flagged, value < 40 | current bearing, real value |
| NO_SIGNAL | flagged | last good bearing, value 0 |

`hold_s` now governs how long NO_SIGNAL repeats the last bearing before the
tracker and quality history are flushed and the machine returns to ACQUIRING —
stale dispersion from a departed signal must not make the next acquisition look
healthy.

Observed: the DEGRADED band is narrow (roughly vorsignal 27–39), because
`q_stab` carries both the largest weight and its own flag threshold at the same
value. Left alone deliberately — widening it would mean publishing more marginal
bearings, and the weights came from measurement, not taste.

### A14 — "warming up" is not "no signal" (2026-07-24, M6)
The quality estimator flags when it has fewer than two bearings, because it
cannot judge stability. The controller initially treated that flag like any
other and entered NO_SIGNAL — so a perfectly healthy acquisition was reported as
a signal failure, and `hold_s` began running against a good station.
`QualityResult` now carries `warming` separately from `flagged`; the controller
stays in ACQUIRING for it. Both still publish `vorsignal` 0, so consumers are
unaffected — this is about not lying in the state and the reason string.

### A15 — `q_stab` was judging the smoothed bearing (2026-07-24, M6)
Found because a test that should have produced DEGRADED produced TRACKING.
The controller passed `tracker.update(...)` — the **smoothed** bearing — to the
quality estimator, so `q_stab` measured the dispersion of an already-smoothed
series. With `smooth_n = 3` a ±6° alternation arrives as ±2° and scores as
stable: smoothing hides precisely the jitter `q_stab` exists to detect, and the
term with the largest weight in the whole metric was being fed a laundered
input.

Corrected: the **raw** bearing is what gets judged, the **smoothed** bearing is
what gets published. This matters most in the case the metric is for — a marginal
signal that wanders — which is exactly where it was weakest.

### A16 — a repeated frequency restarted the retune, blocking acquisition forever (2026-07-24, M7)
**Found by the first end-to-end run against a live socket. No unit test caught
it, and none would have.**

`set_channel()` treated a repeat of the *same* frequency as a no-op only when
already in a decoding state:

```python
if ch == self.channel_khz and self.state in DECODING_STATES:   # wrong
```

So a repeat arriving during TUNING or ACQUIRING fell through to the debounce
path and started the retune over — flushing the demodulator, the tracker and the
quality history.

Bridges re-assert their whole state periodically. That is the normal design, and
the mock bridge does it at 2 Hz. Against such a bridge the daemon would reset
itself faster than it could settle and **would never acquire at all**. In the
live run this showed up as `tune 109.400 MHz` twice and nothing but
`{"vordir": 0, "vorsignal": 0}` on the wire.

Corrected to `if ch == self.channel_khz: return`. `channel_khz` is cleared
whenever the machine drops to IDLE or INVALID_FREQ, so the comparison is
sufficient on its own and a genuinely new channel still retunes.

Unit tests missed this because they set the channel once, the way a person
would. The bridge does not behave like a person. Three regression tests added:
repeat-during-tuning, a genuine change while acquiring, and repeat after IDLE.

### A19 — self-test hard-failed when no config was present (2026-07-24, bench)
Found by walking the bench procedure on a clean checkout. `python3 -m rbvor
--self-test` from a bare source tree -- exactly stage 1, run before install
places the config in /etc -- reported a red **FAIL** on "configuration loads and
validates" and told the builder "do not proceed to the bench".

That is backwards. The self-test's entire purpose is to prove the *software*
works independently of config; every stage below the config check already falls
back to built-in defaults when `cfg is None`. A missing config on a fresh
checkout is the normal case, not a defect. It is now a WARN, and the run passes.

A genuinely *malformed* config (schema rejection) is still a FAIL -- that is a
real problem a builder must fix. The distinction is the error message; matching
only "not found" missed the "no configuration found" phrasing from the
search-both-paths branch, which is why the first fix did not take. Regression:
`tests/test_main.py::test_self_test_passes_without_any_config_present`, which
runs the self-test in an isolated directory with no config on disk.

### A18 — replay and bench mode must not start the bridge client (2026-07-24, M5/bench)
Found in the release smoke test. `python3 -m rbvor --replay ... --dry-run`
started the WebSocket client anyway and flooded the log with
`bridge connection lost` / `Connect call failed` against a bridge that has no
reason to exist when the channel comes from a file. The client is now started
only when the channel actually comes from the bridge — i.e. neither `--freq`
nor `--replay` is given. Regression: `tests/test_main.py`.

### A17 — replay must yield to the event loop (2026-07-24, M7)
Same live run: `__main__` pushed replay blocks in a tight loop inside a
coroutine, so the tick task never ran, the state machine's timers never fired,
and replay sat in TUNING indefinitely. Fixed with `await asyncio.sleep(0)` per
block. Note that replay compresses time — the publish cadence follows wall clock,
not signal time — so a 10 s capture replayed in 0.5 s yields ~1 periodic POST,
not 10. That is correct, not a defect.

---

## Decisions (continued)

### M9 — forensic logging added without touching the bearing path (2026-07-25)
Solution CSV + RB-Cloud schema (`rbvor/solutionlog.py`) and flag-triggered IQ
capture (`RingRecorder` in `record.py`). Both are wired into the controller as
**optional hooks** (`recorder=`, `solution_sink=`), default `None`. Every
pre-M9 test constructs the controller without them and still passes unchanged,
which is the guarantee that logging cannot alter a bearing.

The solution schema is the single source: the CSV header, the cloud record and
`docs/cloud-schema.md` are all generated from `SOLUTION_SCHEMA`, so they cannot
drift. Each row carries the quality terms, measured depths, deviation and SNR,
plus `iq_ref` naming the capture that produced a flag — enough to re-derive a
disputed bearing offline via `vor_replay.py` rather than trust it.

`gps_track` reaches the log via a dedicated callback and a structural test
(`test_gps_track_is_not_referenced_anywhere_in_the_bearing_path`) still asserts
it never enters `bearing.py`, `dsp.py` or `quality.py`.

### D4 — port gate closed: Python is final for v1.0 (2026-07-24, M8)
T12, 300 s of 1.024 Msps signal through the full chain plus bearing and quality:

| metric | measured | A3 threshold |
|---|---|---|
| sustained CPU | **3.6 %** of one core | 25 % |
| RSS growth | flat after warm-up | none |
| dropped windows | **0 of 300** | < 0.1 % |
| mean bearing error @ 25 dB | 0.072° | — |

No trigger fires. Decision D1 stands and the C99 + liquid-dsp port is **not**
undertaken for v1.0. Re-measure on the target Pi 5 with RB-01 and `dump1090`
running before treating this as settled — the container is not the aircraft.

A `test_T12_no_state_growth_over_a_long_run` guard was added to CI. It cannot
run for eight hours, so it asserts the invariant that makes the eight-hour run
pass instead: internal buffers stay bounded and every expected window is
produced.

### D3 — Python confirmed against the A3 port gate (2026-07-24, M3)
Full chain at 1.024 Msps measured at **6.5 % of one core** in this container
(15.5× realtime), against the A3 threshold of 25 %. The gate stays open;
decision D1 stands. Re-measure on the Pi 5 under T12 with RB-01 and dump1090
running before this is treated as settled.

### D2 — synthetic level is auto-scaled, not fixed (2026-07-24, M2)
A fixed digital level cannot span the SNR range. SNR is specified in the 12 kHz
channel, but the noise occupies the whole 1.024 MHz sample rate, so at 6 dB SNR
the wideband noise carries several times the signal power and the container peak
is set by noise, not by modulation depth. `--level auto` (default) generates a
0.25 s probe at unit level with the same seed and impairments, then backs off to
92 % of full scale from the measured 99.995th percentile. Deterministic, and it
is what a real AGC does for the same reason. A few clipped Gaussian tails
(< 0.01 %) are harmless and no longer warned about — a real dongle clips them
too.

---

## Open questions to Stefano (block M7 only)

| Q | Question | Interim default in code |
|---|---|---|
| Q1 | `vorsignal` 0–100 or 0/1? | 0–100 internally, `publish.signal_scale: "percent"` |
| Q2 | `vordir` = radial FROM or bearing TO? | radial FROM, magnetic, `publish.bearing_ref: "from"` |
| Q3 | Publish periodic @1 Hz or on-change + keepalive? | `periodic`, 1.0 s |

## Open technical items

See `docs/open-items.md`. Nothing there is to be filled with a plausible value.
