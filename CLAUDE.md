# RB-VOR — working log

Normative documents: `docs/rb-vor-architecture.md` (doc A),
`docs/rb-vor-implementation-plan.md` (plan A). For the wire format,
`docs/RB-VOR-IF-v2.1.md` supersedes doc A §10 (A20, A21). This file records decisions,
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
residual decimation-phase mismatch between the paths. ~~`phase_sign` is **+1** for
this implementation~~ — but O1 stays open until a real station confirms it, since
the synthetic generator and the demodulator share my sign conventions and could
be consistently wrong together. **They were.** The config that decoded a real
station carries `phase_sign −1, phase_offset_deg +2.12` — see F1 in the field
section below. The synthetic +1 / −1.9657 pair remains what `vor_calibrate.py`
produces and is now known to be the *mirrored* row of the `docs/calibration.md`
table.

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

### A20 — RB-VOR-IF v2.0: `vordir` / `vorsignal` are no longer on the wire (recorded 2026-09-11)
Undated work, reconstructed from `CHANGELOG.md`, `docs/RB-VOR-IF-v2.0.md`,
`publisher.py` and `tests/test_bridge_io.py`. Breaking change to the published
interface, and the reason the "frozen at v1.0" statements in `README.md` and
the 0.9.0 changelog entry are stale:

| v1.0 | v2.0 | why |
|---|---|---|
| `vordir` | `vorradial` | same semantics (radial FROM), clearer name |
| `vorsignal` 0–100 | `vorsnr` (dB) + `voram` (depth ×100) | measurements, not a synthetic score; the consumer sets its own threshold |
| `publish.mode` periodic / on_change | always transmit-on-change + `keepalive_s` | one policy, no configuration branch |

Consequences that the code carries but nothing had written down:
- `vorsignal` survives **internally** as the validity gate and as the
  TRACKING / DEGRADED split of A13. On the wire both states are `vorstatus 4`
  (A21), so the A13 distinction is now visible only in the solution log.
- **Q1 and Q3 of the open-questions table are closed by this change**, not by
  an answer: Q1 (`vorsignal` 0–100 vs 0/1) is moot, Q3 (periodic vs on-change)
  is decided. Q2 (FROM vs TO) is still open and still `bearing_ref: "from"`.
- `publish.mode`, `period_s`, `deadband_deg` and `signal_scale` are **dead**:
  no code reads them (`publisher.py` reads only `bearing_ref` and
  `keepalive_s`; `controller.py` reads `hold_s`). ~~Yet the schema still lists
  all four as `required` and `config.py` still validates the `on_change` /
  `keepalive_s` relationship … the schema wins today.~~ **Cleaned up
  2026-09-11:** the four keys are optional and marked deprecated in the schema,
  gone from the shipped config, accepted with a warning naming them
  (`config.DEPRECATED_PUBLISH_KEYS`), the `on_change` rule is removed, and
  `keepalive_s` may be 0 as the spec says (the staleness timeout is the
  consumer's, spec §4 item 2).

### A21 — RB-VOR-IF v2.1: `vorstatus`, `vorsub`, SDR released when idle (recorded 2026-09-11)
Additive. `vorstatus` (0 idle/off/invalid, 1 no-signal, 2 tuning, 3 acquiring,
4 tracking/degraded) and `vorsub` (9960 Hz level ×100) are **not** gated by
bearing validity — while ACQUIRING or NO_SIGNAL, `vorsub` is the one number
that says whether the antenna hears anything. `rbvor.__init__.IF_VERSION` is
`"RB-VOR-IF v2.1"`; `tools/mock_bridge.py` validates the five-key payload.

Same change: entering IDLE or INVALID_FREQ now closes the SDR
(`Controller._enter` → `_release_sdr`) so the dongle is free and cool when no
VOR is selected; `SdrReader.set_frequency()` reopens it. Tested in
`tests/test_statemachine.py::test_no_frequency_releases_the_sdr` and
`::test_invalid_frequency_releases_the_sdr`.

### A22 — the "spectral mirror" is our sign convention, not the dongle's (2026-09-11)
Every real capture in the tree that contains a signal (85 of 89 in
`new_Antenna_test.txt`, taken with `tuner_offset_hz = −256 kHz`; the four
exceptions have SNR ≤ 4 dB, i.e. nothing on either side) reports
`MIRROR DETECTED`: 18–43 dB more energy at **+256 kHz** than at the −256 kHz
the decoder expects. `sdr.swap_iq` and
`vor_replay.py --swap-iq` (conjugate every block) were added as the fix, with
the comment ~~"some RTL-SDR setups deliver a spectrally-mirrored band"~~.

That hypothesis is wrong, and the code shows why. `vor_gen.py` places the
carrier at `exp(+j·2π·offset_hz·t)`, i.e. at **−256 kHz** in baseband, and
`dsp.py` down-mixes by `−offset_hz` (the `[1, j, −1, −j]` sequence shifts the
spectrum *up* by fs/4) to bring exactly that back to DC. Generator and
demodulator agree with each other. But a tuner parked at `nav + offset`
= nav − 256 kHz delivers the carrier at nav − LO = **+256 kHz**, under the
I + jQ convention that librtlsdr, pyrtlsdr and SoapySDR all use. So the
synthetic signal sits on the wrong side of centre, the DSP was built to match
it, and the hardware is the one telling the truth. No RTL-SDR "mirrors".

`swap_iq` is nevertheless a *correct* workaround: conjugation moves +256 kHz to
−256 kHz, and because `dsp.py` takes `np.abs(z)` before either path, the
envelope — and with it the AM depth, subcarrier, SNR and bearing — is
unchanged. It cannot explain the `phase_sign` flip of F1.

~~**Pending decision, not taken here:** flip the sign … or keep the
workaround.~~ **Decided and done — see D5.** The mix in `dsp.py`, the
placement in `vor_gen.py`, `band_survey()`'s `expected_hz`, `vor_spectrum.py`
and the two reference decoders in the tests were flipped together; `swap_iq`
is back to being a per-dongle override. A correct field config is now
`tuner_offset_hz: −256000` **and `swap_iq: false`** — see F2 for what happens
when the offset is set to 0 instead.

### A23 — D4's CPU figure did not transfer to the Pi (2026-07-25, recorded 2026-09-11)
Recorded only in the docstring of `rbvor/_threadcaps.py`. On the Pi the daemon
pegged **100 % of all four cores** while doing ~4 % of real work: the polyphase
FIR (`wins @ taps`) and the scipy filters hand off to OpenBLAS, which spawns a
worker per core and **busy-waits** between calls. The A3 trigger table
(>25 % sustained, contention with RB-01 / `dump1090`) therefore fired on the
first Pi run — for a reason unrelated to the arithmetic that D3/D4 measured.

Fix: `_threadcaps.py` pins `OPENBLAS_NUM_THREADS` and five siblings to 1
**before numpy is imported** (`__main__.py` imports it first, `isort: off`).
The startup banner prints `threads BLAS/OpenMP capped to 1` when it acted.
D4 stands, with the caveat that the "container is not the aircraft" warning
was justified within a day. **The post-fix figure on the Pi is not recorded
anywhere** — open item O13.

### A24 — Pi timestamps are not evidence (recorded 2026-09-11)
`stderr.txt` holds three separate daemon runs; all three start at
`2026-04-13 20:38:18–19`. The capture sidecars carry `utc` ≈ 2026-04-13 for
software that `_threadcaps.py` dates 2026-07-25. The Pi has no RTC and no
network in the car, so its clock restarts from the same fake-hwclock value on
every boot. Consequences: capture file names (`capture_20260413T…`) do not
order events across boots, the field work in this file is *undated*, and any
future capture must carry GPS time — `vor_capture.sh` already folds `GPSTime`
into `--site` for exactly this reason. Date the work by GPS, never by the Pi.

### A25 — where the signal quality was being lost, and three fixes that measure (2026-09-12)
Asked "how can the software improve signal quality", the answer came from
measuring, not from the textbook. Split of the bearing noise by path
(synthetic, 24 s; and the six real captures with the car's motion removed by
a linear detrend):

| | AM path | FM path |
|---|---|---|
| synthetic, 10 dB channel SNR | 0.51° | 0.39° |
| synthetic, 3 dB | 1.40° | **3.90°**, deviation reads 194 Hz |
| real, strong captures | 0.46–0.59° | **1.2–1.6°** |
| real, weak captures | 1.3–3.0° | **7–18°** |

The AM path sits at its Cramér-Rao bound. **The FM subcarrier path is the
weak link** — 3× noisier on every real capture, and its recovered deviation
collapses below ~400 Hz in 35 % of the orbit captures that had a healthy tone
SNR: the conjugate-product discriminator is at or below its threshold in the
car. That is where the next real gain is (see the D7 experiment).

Three things done now, each measured:

1. **The "12 kHz" channel filter was −2.45 dB at 9960 Hz and −3.3 dB at the
   upper sideband** (255 taps at 1.024 Msps: a 13 kHz-wide transition band),
   i.e. rolling off inside the band it existed to pass, asymmetrically —
   FM-to-AM conversion, and the reason every real capture read `subcarrier
   0.17–0.21` against 0.30 (and the A7 synthetic 0.247). Now 511 taps at
   13 kHz: −0.1 / −0.3 dB across 9960 ± 600, −40 dB at the 32 kHz Nyquist.
   The sub LPF goes 127 → 255 taps (was −1.9 dB at 540 Hz). Real capture:
   subcarrier 0.171 → **0.223**, deviation 313 → 333 Hz; weak captures' FM
   path 7.3° → 4.7°, 18° → 11°. Calibration constant moved by 0.0006°.
   Decimator cost doubles — from 97 µs, after D6 irrelevant.
2. **`integration_s` 1 → 2 s, `hop_s` 1 s.** Bearing noise 0.74 → 0.56° at
   10 dB, 4.1 → 2.7° at 3 dB; output stays 1 Hz; the constant is independent
   of the window (0.001° across 1–3 s). Costs 0.5 s of lag: under 0.5° at the
   0.3–0.8°/s a radial changes in cruise.
3. **AFC.** The residual carrier frequency is measured on the 32 kHz baseband
   every block (mean phase advance per sample — unbiased for a VOR, whose
   sidebands are all symmetric about the carrier), smoothed over 1 s, and
   once it exceeds 300 Hz the down-mix is corrected by that amount before the
   channel filter. Below the threshold the chain is **bit-identical** to a
   build without AFC (pinned by test), so the common case costs nothing. At
   −4 kHz (a 40 ppm dongle, or a station at its ±0.002 % tolerance limit)
   the subcarrier level fell 0.275 → 0.152 and the bearing noise doubled;
   with AFC both recover fully and the offset is reported as
   `carrier_offset_hz` (observation, solution log, status line, replay
   summary with ppm). Noise alone never triggers it (symmetric about zero).

The accuracy sweep re-run with the new chain (`docs/sweep.md`; the sweep tool
also had the A15 bug — it judged the smoothed bearing — now fixed):

| channel SNR | A11 RMS | **now RMS** | max | flagged |
|---|---|---|---|---|
| 30 dB | 0.041° | **0.019°** | 0.04° | 0 % |
| 10 dB | 0.474° | **0.261°** | 0.59° | 0 % |
| 3 dB | 2.33° | **1.63°** | 4.0° | 0 % |
| 0 dB | 6.54° | **3.46°** | 9.5° | 100 % |
| −3 dB | 17.4° | **9.8°** | 33° | 100 % |

Error roughly halved at every SNR; the flag still asserts exactly where the
max error crosses the 5° T11 limit, with more margin than before. The A11
thresholds and weights stand unchanged; O10 stays closed.

Not done, and why: a PLL on the 30 Hz tones (the whole-cycle DFT already is
the matched filter, and the AM path is at its bound); more taps on the tone
LPF; a shorter `agc_tau_s` (O11 is about amplitude, the bearing is phase).

### A27 — the receiver could not choose its dongle; gain raised (2026-09-29)
Found by F6. `rbvor/sdr.py` opened `RtlSdr()` with no arguments, i.e. **device
index 0, always**; `sdr.device_args` was read only by the SoapySDR backend,
which is not installed on the Pi (F4). `tools/vor_capture.py` had the same
hole in all three of its backends and hard-coded `device: "index=0"` into
every sidecar it wrote. With one dongle that is invisible. The aircraft has at
least three (RB-01's own status row reports `Devices: 2`, plus the VOR
dongle), and index order follows USB enumeration, which changes between boots.

Added `sdr.device_serial` (preferred) and `sdr.device_index`, honoured by both
backends and by the capture tool. The policy is one pure function,
`sdr.resolve_device(cfg, serials)`, so it is testable on a machine where
`rtlsdr` cannot even be imported (see below) — and so a capture and a live
decode can never disagree about which antenna they heard. Rules:

- a serial that is not present is an **error naming every serial that is**,
  never a silent fall back to index 0. The failure it guards against —
  decoding the 1090 MHz antenna — is invisible in the output, so it has to be
  loud at startup;
- two dongles sharing a serial is an error (`rtl_eeprom -s` fixes it);
- more than one dongle with no serial pinned logs a warning naming the index
  and serial actually opened, so the next log says what was listened to.

`gain_db` 14.4 → **32.8**, the value the config shipped before the field
edit and a real R820T2 step (the tuner quantises to its own ladder; a value
off it is silently rounded, which a test now pins). 14.4 was enough in the car
at short range; an aircraft antenna plus the mandatory FM band-stop is a
different budget. **This is a starting point, not a measurement** — the right
value comes from a gain sweep judged on carrier-to-floor in
`vor_replay.py --survey`, which is O19.

Fixed on the way, same code path: `import rtlsdr` binds librtlsdr at **import**
time, so a version mismatch raises `AttributeError` (`undefined symbol:
rtlsdr_set_dithering` on a stock librtlsdr where pyrtlsdr wants the blog fork,
O7) — not `ImportError`. It escaped the handler and took the daemon down with
a raw ctypes message naming neither cause nor fix. Now a clean `SdrError`
pointing at RUN.md §7, so the backend falls through and F3 turns it into exit
3. It surfaced only because this session installed pyrtlsdr on the dev machine
to check its API; on a Pi with the stock driver it would have been a crash in
the aircraft.

### A28 — the aircraft's VOR dongle is an FC0012, and every convention here was measured on an R820T (2026-09-29)
Stefano, after F6: **index 0 = FC0012 = VOR antenna; index 1 = R820T2 =
1090 MHz**. That single fact overturns two conclusions and explains why the
allocation is the way it is — the FC0012 tops out at 948.6 MHz and physically
cannot do 1090, so the only 1090-capable tuner went to ADS-B and the VOR
antenna got the weaker one.

**Retraction 1.** ~~A27's leading hypothesis, "the wrong dongle": rb-vor opened
index 0, which may have been the ADS-B antenna.~~ Wrong — index 0 *is* the VOR
antenna. rb-vor was listening to the right coax all along. A27's device
selection is still worth having (enumeration order is not stable, and the
capture tool was writing `index=0` into sidecars it had never checked), but it
is **not** the explanation for F6.

**Retraction 2.** ~~A27 raised `gain_db` to 32.8, "a real R820T2 step".~~ The
fitted tuner is not an R820T2. The **FC0012 has five gain steps: −9.9, −4.0,
7.1, 17.9, 19.2 dB**. So 32.8 would have been silently clamped to 19.2, and the
14.4 that flew was silently rounded to 17.9 — the config described neither the
request nor the hardware. pyrtlsdr documents this rounding; nothing surfaced
it. Now `snap_gain()` snaps to the device's own ladder and **logs when it had
to**, the tuner type is logged at open, and the shipped `gain_db` is **19.2**,
a real FC0012 step at the top of its range (a starting point; O19 still wants
a measured sweep). The test I wrote a day earlier pinning "a real R820T2 step"
was itself the bug — it would have passed a value this aircraft cannot produce.

**The new leading hypothesis for F6: spectral sense is tuner-dependent.**
librtlsdr runs the R820T/R828D in low-IF with spectrum inversion enabled in
the demodulator; the FC0012 is zero-IF and gets no such inversion. Every
convention in this project — A22, D5, `swap_iq: false` — was measured against
captures from an **R820T** (`stderr.txt`: "Found Rafael Micro R820T tuner").
If the two families deliver opposite senses, then `swap_iq: false` is correct
for the bench dongle and **wrong for the aircraft's FC0012**, and the decoder
spent the whole flight looking at the mirror of the signal: AM ≈ 0, subcarrier
at the noise floor, flat with geometry. That is exactly F6.

This is precisely the case D5 kept `swap_iq` for — "a per-dongle override".
**It is a hypothesis, not a measurement, and it is not written into the
config** (D8) — `swap_iq` is deliberately absent from the shipped FC0012
profile, and a test pins that absence.

**The settings that make it testable** (`sdr.tuner_profiles`): per-tuner
overrides of `swap_iq`, `gain_db` and `ppm`, applied once the device reports
its tuner, with the `sdr` block as the fallback. Three properties of the
*tuner* rather than of the installation, so one config serves both dongles and
swapping hardware needs no edit. Gain is resolved inside the device (it needs
the handle); `swap_iq` is consumed by `SdrReader`, so the overlay is applied
again after `open()` and logged, because it changes how the band is read.
A config with no profiles behaves exactly as before, pinned by a test.

**And the measurement itself**: `vor_replay.py --auto-swap` decodes a capture
**both ways in one pass** and prints a verdict, so the answer is a number
rather than a judgement. Verified in both directions against real data — the
tree's own R820T capture, and a deliberately conjugated copy of it:

```
    normal       AM depth 0.260   30 Hz SNR   38.1 dB
    swapped      AM depth 0.021   30 Hz SNR   11.6 dB
    -> NORMAL sense decodes: leave "swap_iq" false for this tuner
```

Note the losing sense reads **AM 0.021 / SNR 11.6 dB** — the flight read
0.005 / 2.6 dB. That is the shape of F6, which is what makes the hypothesis
worth testing first. It refuses to choose when both senses read ~0 (that is
F6's own data, and picking from noise would send the next flight after a fault
that may not exist), and flags an implausible tie.

Third candidate, worth knowing before buying a flight: the FC0012 is a poor
VHF receiver — far worse noise figure than the R820T2, and some dongles switch
their VHF/UHF front end on a GPIO that not every board wires. The project's
own BOM asks for an R820T2/V4. **Replacing the FC0012 with a second R820T2
eliminates this entire class of unknown**, including the sense question, and
puts the aircraft on the hardware every measurement in this file was taken
with.

### A26 — the payload is never zeroed; `vorstatus` 4/5; `vorfreqactive`; `--csv` (1.0.0 lineage, ported 2026-09-13)
The 1.0.0 commit (`becaf78`) carries a set of changes to the publish path that
my working copy never had — see the incident note under D8 for how that
happened. They are the RB-VOR-IF **v2.2** semantics and are now the code:

- **No field is gated by validity.** Every field carries the last measurement
  the receiver made; `vorstatus` is the single validity signal (`>= 4`
  usable). A field is 0 only when nothing has been measured since the last
  retune — `_flush()` drops `_last_obs`, so station A's numbers can never be
  attributed to station B (F3). The 1.0.0 lineage recorded this in code as
  "AMENDMENT A20"; that number was already taken here (IF v2.0), so it is A26.
- **The radial is published while ACQUIRING** (status 3: "not yet judged")
  and while warming — it exists and it is calibrated. Uncalibrated is the one
  case that still forces the radial to 0, because `vorstatus` has no code for
  "off by an unknown constant" (A2); the measurements go out regardless, which
  is what a builder needs while calibrating.
- **`vorstatus` 4 = DEGRADED, 5 = TRACKING** (was 4 for both). A consumer that
  tested `== 4` for "tracking" must test `>= 4`.
- **`vorfreqactive`**, the tuned channel in kHz (109400), sixth field; 0 when
  idle or rejected.
- **Retune order**: enter TUNING and flush *before* building the payload, so
  the retune POST says "tuning, nothing measured" rather than carrying the old
  station's SNR behind a usable status.
- **`--csv FILE`**: one row per demodulated window (`rbvor/obscsv.py`), before
  any gating, with the tuned channel — the live twin of `vor_replay.py`'s CSV.

Ported by taking 1.0.0's `controller.py`, `publisher.py`, `obscsv.py`,
`mock_bridge.py`, the v2.2 spec and its seven new tests wholesale, then
re-applying the two D6/A25 lines the controller had gained (recorder channel,
solution-log fields) and `--csv` into the F3-hardened `__main__`. Three of my
tests that pinned the old zeroing are gone; theirs replace them. A2's tests
now build the uncalibrated case explicitly, because since 1.0.0 the shipped
config carries the flight calibration (see D8).

### A29 — the bridge link is lost only when the socket is; no WebSocket ping (2026-10-31)
Seen on the console against the real RB-01:
`bridge connection lost: sent 1011 (internal error) keepalive ping timeout; no
close frame received`. Stefano: **RB-01 has no ping/pong and echoes nothing.**
`BridgeClient` called `websockets.connect(url)` with the library defaults, i.e.
a ping every 20 s and a 20 s pong deadline — so a healthy link was torn down on
a timer. ~~Doc A §10.1 / IF v2.2 §2: "sends no WS frames except protocol
pings".~~ It now sends none.

Reproduced against a stand-in that, like RB-01, never reads after the
handshake (a `websockets` server cannot play that part: it always answers).
The cycle is ~50 s, not 40: ping at 20 s, no pong by 40 s, then 10 s waiting
for a close frame that never comes — and **no `vorfreq` is delivered during
those last 10 s**, so a channel change in that window waited for the
reconnect. The drop itself stayed under `ws_grace_s`, which is why it showed as
a warning and not as IDLE. The same wait cost every daemon stop 10 s (up to
30 s on the legacy client, which spends up to three close timeouts —
inferred from 3 × 1 s measured, not run at 10 s).

Changed, `rbvor/bridge_client.py`:
- `ping_interval=None`. RB-01 cannot answer, so the ping can only ever lie.
- `close_timeout` 10 → 0.5 s. Stop measured 10.0 → 0.5 s (websockets 17),
  1.5 s on websockets 12, the floor `pyproject.toml` allows.
- **Kernel TCP keepalive on the socket** (idle 10 s, 5 s × 3 probes: dead
  after ~25 s of silence, then `ws_grace_s`). Not asked for, and the one
  judgement call here: the ping was also, incidentally, the only thing that
  noticed a peer vanishing *without* closing — a power cut or WiFi drop on a
  bridge at `192.168.10.1`. A client that never writes never gets an RST, so
  without a replacement it would sit on the dead socket for ever and still be
  deaf to `vorfreq` after the bridge came back. Keepalive probes are answered
  by the peer's kernel, not by RB-01, so they ask nothing of its protocol;
  "timeout only on disconnection of the socket" is exactly what they detect.
  Harmless when the bridge is on the same host (a dead process closes its
  sockets).

Regressions, `tests/test_bridge_io.py`, all against the never-reads stand-in
and each confirmed failing on the old client: the link survives the library's
keepalive compressed 100× (old: re-made, 2 connections), stop does not wait
for a close frame (old: 10.0 s), the live socket carries `SO_KEEPALIVE` with
the configured idle (old: never set).

### A30 — a dongle that cannot be had is a state, not a crash (2026-11-06)
Found by `test/integration/test_vor_radio.py` in roastbeef, which alternates this
daemon and rb-sdr-radio on one dongle from the real RB-01 screen. A VOR picked
while the radio held the dongle made `SdrReader.open()` raise in `_retune`, which
F3 had made fatal: exit 3, restart after 2 s, exit 3 again, for as long as the
radio held it, with the plate stuck on TUNING.

F3 stays right about what it fixed, a task that dies silently. What it assumed
no longer holds: that an unopenable SDR is a broken installation. With one dongle
shared between two receivers it is normal use, and a pilot action can end it.

Changed: `Controller._tune` catches `SdrError` when `sdr_retry_s` is set and
enters `State.NO_SDR`. The channel stays selected, `vorstatus` is 1, and the
dongle is tried again every `sdr.retry_s`, optional, default 5, the same as
rb-sdr-radio. `_enter` releases the SDR on NO_SDR, because a retune can fail with
the device already open and waiting must not hold what the other program wants.

Three judgement calls:
- **`vorstatus` 1, not 2.** TUNING promises it is on the way, and this can last
  as long as the radio is on. The wire format is unchanged: a consumer reading
  `vorstatus` needs nothing new. The reason is in the journal and the solution log.
- **A refused attempt publishes nothing.** Going through TUNING on the wire
  would flash the plate every 5 s.
- **Every `SdrError` is retried, not only "busy".** Telling them apart would mean
  parsing libusb strings through two backends. A dongle missing when a channel
  is selected recovers the same way once it is plugged in. The cost is that a
  broken installation now waits at NO_SDR with its reason in the journal, where
  it used to crash-loop.

**Not covered:** a dongle pulled during reception. `SdrReader._run` still loops on
read errors with the dead handle, and only a retune reaches `_tune`. Unchanged by
this amendment and not tested.

**Bench mode is deliberately untouched**: with `--freq` the daemon passes no
`sdr_retry_s`, the error propagates and F3's exit 3 stands, because nothing will
free the dongle. So does its regression test.

Verified on a real Nano 3 (R820T, x86), refused by a real rb-sdr-radio. Not run
on a Pi or an FC0012.

### A31 — the bridge is read on connect (2026-11-06)
Same test. `/bridge/float/ws` sends nothing on connect, and RB-01 rebroadcasts
only when a value changes. So a restarted daemon heard `vorfreq` only if something
on the bridge changed after it connected, and its own startup publish won that
race by about 2 ms (seen in 1 of 16 runs). A station picked while it was down was
lost every time. The plate said OFF with `vorfreq` still on the bridge, and pressing
the same station did nothing, because an unchanged value reaches nobody. rb-sdr-radio
already had this read, and its tests name this gap.

Changed: after every connection, `BridgeClient` calls `fetch_state`, a `GET` of
`bridge.rest_url` (`HttpTransport.fetch`), and handles the answer as a frame.
It is subscribed first, so a change during the GET cannot be missed, and it
consumes frames only after, so the answer cannot overwrite a newer frame.

**Differs from rb-sdr-radio on purpose:** there a failed read logs a warning and
keeps the link. Here it is a failed connection, so it reconnects with backoff and
reads again, because carrying on without the state is the deafness this read
exists to end. Hence `tools/mock_bridge.py` gained a `GET`.

"Absent" stays "no change", as in a frame: `vorfreq` missing from the answer
does not idle the daemon. A side effect worth having: after a bridge outage longer
than `ws_grace_s` the daemon used to stay IDLE until the next change, and now
returns to the channel.

Known, not addressed: a dead daemon's last `vorstatus` stays on the bridge. RB-01
has no timeout (section 4 item 2 of IF v2.2), and cannot have one yet, because it
rebroadcasts only changes, so the keep-alive reaches no client. Tracked in
roastbeef `docs/tech-debt/radio-active-status.md`.

### A32 — CI had never run a test (2026-11-06)
All five runs since the first push failed at `ruff check .` on the loose
`test_dongle.py`, so the `pytest` step was skipped every time. The ruff failure
was hiding two more, each of which would have failed the step on its own:

- Bare `pytest` could not import `rbvor` from `tests/conftest.py`. `python -m
  pytest` puts the working directory on the path and bare `pytest` does not, so
  this never showed by hand. `pyproject.toml` now sets `pythonpath = ["."]`.
  `pip install -e .` would not do: the tests also import `tools.*`, which is not
  packaged.
- `ci.yml` kept its own package list, without `websockets` or `aiohttp`. It now
  installs `requirements.txt`, so the two cannot drift.

`test_dongle.py`'s late `import numpy` keeps a `noqa: E402` instead of moving to
the top, so the script still reports on the dongle where numpy is missing.

Verified on a replica, not yet on a GitHub runner: Python 3.11.18, a fresh venv
from `requirements.txt`, a `git archive` of HEAD plus these files, and CI's exact
commands. Ruff clean, 604 passed, and 604 again pinned to two CPUs.

### A33 — ILS localizer, phase A: channel rules, mode, wire v2.3, VOR untouched (2026-11-10)
Brief: `docs/loc-plan.md` (L0–L6). Localizer decoding is in, **shipped
disabled** (`loc.enabled: false`, pinned by
`tests/test_loc_controller.py::test_shipped_config_has_the_localizer_disabled`)
until a real localizer has been decoded (L7, O23).

**L0 baseline — the plan's figures were stale.** The plan (and D5/D6) quote
the real capture `capture_20260413T195020Z_109400.cu8` as Δφ 135.467°, AM
0.271, SNR 38.2 dB. Run before any change, `vor_replay.py` gives **135.220°,
0.260, 38.1 dB**. The plan's figures reproduce to the digit only with the
pre-A25 filters (`channel_lp_hz` 12000, `channel_taps` 255, `sub_taps` 127),
so they date from before A25 and nobody updated them. The baseline used
instead: a SHA-256 over every field of every observation of the real capture
under four DSP settings (default, pre-A25, 2 s windows, harmonic) plus a
synthetic run, taken before any edit and again at the end — **identical**;
the `vor_replay.py` text output is also byte-identical. Suite at L0: 604
collected, 604 passed; ruff clean.

**Channel rules (L1).** `is_loc_frequency()` and `loc_channels()` (40
channels, 108.10–111.95, checked against a list written out as text in
`tests/test_loc_freqrules.py`). The denylist trap the plan named was real:
`classify()` returned `LOC` for a denylisted VOR channel, so once LOC is
decodable a denied 109.4 would have been tuned as a localizer. New
`NavaidClass.DENIED`, returned by a new `classify_navaid()`. **Deviation from
the plan:** `classify()` itself still returns `LOC` for a denied channel,
because `tests/test_freqrules.py` pins that and the brief requires existing
tests unmodified; it is now a thin wrapper that folds DENIED into LOC, and
everything that may tune a localizer uses `classify_navaid()`.
`is_vor_frequency()` returns the same booleans as before; its reason for a
denied channel no longer claims "odd 100 kHz digit".

**Mode.** The mode is fixed per tuned channel at retune; only the tuned
mode's demodulator runs. States are reused; A16 (repeat = no-op), `_flush()`
across a VOR↔LOC change, `vorfreq` 0 releasing the dongle, NO_SDR (A30) and
the read on connect (A31) each have a LOC test. LOC needs no bearing
calibration.

**Wire (RB-VOR-IF v2.3, `docs/RB-VOR-IF-v2.3.md`).** Six `loc*` fields as
plan §6. **Deviation from the plan:** they are on the wire **only when
`loc.enabled`** — then on every payload, zeros on a VOR channel. Without it
the payload is v2.2's six keys exactly. Reason: `tests/test_bridge_io.py`
asserts the payload is exactly the six v2.2 keys, and a disabled feature
should not change the wire. The `vor*` and `loc*` halves go in one POST.

**Clean stop.** With `loc.enabled`, `Controller.shutdown()` publishes an idle
payload and `HttpTransport.drain()` waits for it before the session closes.
Disabled, nothing new is published (VOR behaviour unchanged). A killed daemon
is not covered (O25).

**Found writing the spec:** v2.2 §3.3 and §4.1 still describe the radial, SNR
and AM depth being forced to 0 without a valid bearing. The code has not done
that since 1.0.0 (A26). v2.3 states the A26 behaviour; v2.2 is left as
written because `tests/test_packaging.py` pins its text.

**Logs.** `SolutionLogger` appends to an existing file without reading its
header (it writes one only to a new or empty file — pinned by
`test_solution_logger_never_rewrites_an_existing_header`), so new columns in
`SOLUTION_SCHEMA` would have misaligned every row of an existing `vor.csv`.
The localizer has its own schema (`LOC_SOLUTION_SCHEMA`) and file
(`loc.solution_csv`, default `loc.csv` beside `log.solution_csv`); `--csv`
gains a `<name>-loc.csv` twin.

**Config.** Optional `loc` block, defaults in `config.LOC_DEFAULTS` (the
A25-style exception to "no default in code", for the boot-partition config).
**ICAO figures.** Plan §5 was written from memory. Checked against the only
text reachable, a scan of Annex 10 Vol I as amended to No. 74 (pages dated
4/11/99): band 108–111.975 MHz (3.1.3.2.1), 20 % per tone, 18–22 % (3.1.3.5.1–2),
tones ±2.5 / 1.5 / 1 % for Cat I / II / III (3.1.3.5.3), phase lock 20° Cat
I/II (3.1.3.5.3.3), 150 Hz predominating on the right facing the localizer
from the approach end (3.1.3.1.2), ident 1020 ± 50 Hz at 5–15 % (3.1.3.9.2),
two carriers 5–14 kHz apart and symmetric (3.1.3.2.1), course sector bounded
by DDM 0.155 (3.1.1) — all as the plan had them. Two things the plan did not
have: **SDM may legally be 30–60 % in coverage** (3.1.3.5.3.6.1), which
decided D9, and a single carrier may be 0.005 % off (5.6 kHz at 111.95 MHz).
Not verified: the odd-tenth channel plan (Vol V) and 0.155 as indicator full
scale (airborne convention). Later amendments not checked. O26.

**Roastbeef.** `../roastbeef/test/integration/test_vor_radio.py`, run
unmodified against this tree: 60 checks, 59 pass. The failure, "and played"
(rb-sdr-radio's playback file `heard.raw`), fails identically with
`RB_VOR` pointed at a `git archive` of HEAD, so it predates this work and is
not on rb-vor's side.

`tools/vor_gen.py` gained `add_channel_noise()` and a `gen=` argument to
`_autolevel()` so `loc_gen.py` reuses them; arithmetic and rng order
unchanged (synthetic fingerprint identical), `VorParams`/`generate_blocks`
unchanged.

### A34 — the localizer tones are found by search, not read from a bin (2026-11-10, L3)
Plan 4.2 measured (`docs/loc-sweep.md`, "Tone-frequency tolerance"; 1 s Hann
window, true DDM +0.100). A **fixed** bin at 90/150 Hz reads, noiseless:

| tone offset | fixed-bin DDM read | search | band power |
|---|---|---|---|
| ±1 % (inside even Cat III) | **−0.044** (wrong side) | +0.1001 | +0.1001 |
| ±2 % | −0.007 | +0.1001 | +0.1001 |
| ±2.5 % (Cat I limit) | −0.003 (a centred needle) | +0.1001 | +0.1001 |

The plan expected a "grossly wrong" DDM; the measurement is worse than that:
at 1 % the 150 Hz tone loses more than the 90 Hz one and the needle points
the **wrong way**. (`test_a_fixed_bin_points_the_needle_wrong_on_a_lawful_station`;
its first version asserted "near 0" and failed, which is how the sign was
found.) The two candidates of the plan both hold across ±3 %: noiseless
error ≤ 1e-4; at 10 dB, RMS 0.0120–0.0136 (search) vs 0.0124–0.0142 (band);
at 3 dB 0.0475–0.0505 vs 0.0476–0.0510. **Search chosen:** marginally better
at every point measured, and it reports the offset ε, which A37 uses as a
validity cue. Grid ±3 % in 0.25 % steps, parabola, amplitudes read at the
refined ε; `band` stays as `estimator: "band"` for comparison.

### A35 — the localizer primes its carrier estimate after the channel filter fills (2026-11-10, L3)
First smoke run: SDM read 0.402 for 0.400. Traced to a start-up transient,
not a scale error: `VorDemod` primes the one-pole carrier estimate with the
first output of a channel filter that starts from zeros, i.e. ~0, so the
first windows read depths **×1.83** (tau 0.5 s; ×13 at tau 5 s), decaying
over seconds. The VOR copes (the controller discards a window, quality warms
up) and is not touched. `LocDemod` drops the first ⌈taps/decim⌉ channel
samples and primes on the mean of the next 1/30 s: the first window then
read SDM 0.4006 (measured before the gain correction below). Residual steady scale ×1.0010/×1.0009 = the 700 Hz / 255-tap
envelope filter's gain at 90/150 Hz, divided out.

### A36 — AFC and two-frequency localizers (2026-11-10, L4)
Synthetic course + clearance carriers (Annex 10: 5–14 kHz apart, symmetric),
DDM +0.0775, 20 dB (`docs/loc-sweep.md`, "Carrier offset"). The AFC settles
on the **power-weighted mean** frequency, as its docstring predicts: clearance
−10 dB at 8 kHz → +3492 Hz (course carrier at +4000). At 14 kHz it centres the
course carrier exactly (+6999 Hz) and so pushes the clearance carrier out of
the 13 kHz channel, which *reduced* the DDM error (−0.0055 → −0.0020). In
every other case the DDM was identical with AFC on and off. The residual error
is envelope capture between carriers, not AFC: −0.005 at clearance −10 dB,
−0.019 at −3 dB, −0.041 at 0 dB; a clearance carrier 6 dB *stronger* with DDM
0.30 dominates (reads +0.27). Single carrier at +4 / −5.5 kHz (the 0.005 %
tolerance at 111.95 MHz is 5.6 kHz): DDM unchanged with AFC on or off. **AFC
stays on in LOC mode; the 13 kHz channel filter is kept.** Real two-frequency
behaviour is O24.

### A37 — localizer validity: thresholds from the sweep, never from the DDM (2026-11-10, L4)
Noise decodes as DDM ≈ 0 — a centred needle (plan 4.1). Measured on 1100
cu8-quantised noise windows (5 levels × 4 seeds × 30 s): DDM +0.0016 mean,
0.0075 std; SDM ≤ 0.034; tone SNR ≤ 11.2 dB; ε std 2 %.

`LocQuality` judges tone SNR, SDM, steadiness of ε and the DDM's residual
about a straight line, never the DDM value. Thresholds read off the error of
the published DDM binned by the tone SNR the receiver measures (channel SNR
−1…12 dB × 4 DDMs × 3 seeds):

| tone SNR | pub RMS / max, % of full scale |
|---|---|
| 24–26 dB | 9.8 / 28.8 |
| 26–28 dB | 7.8 / 21.2 |
| 28–30 dB | 6.8 / 16.2 |
| 30–32 dB | 4.6 / 11.2 |

- `flag_snr_db` **26** (≈ 5 dB channel SNR): the worst valid window about one
  dot (20 % FS) out. Noise maximum 11.2 dB: 15 dB margin.
- `flag_sdm` **0.20**: a lawful station reads ≥ 0.30 when strong.
- `flag_eps_std` **0.5 %**: a real signal's ε std over 5 windows is 0.014 %
  mean / 0.021 % max at the SNR threshold. **Not a guarantee on its own:**
  4 of 1100 noise windows passed it alone. Tone SNR and SDM each rejected all
  1100 on their own.
- Stability is the **detrended** residual: a plain std (the plan's wording)
  would flag an intercept. Measured: DDM ramp −0.2 → +0.2 at 0.02/s, 33
  windows judged, 0 flagged, no lag bias at the window centre (−0.0002).
- Weights sdm 0.10 / snr 0.70 / stab 0.20, snr mapped 26–46 dB: put the
  TRACKING/DEGRADED split at ≈ 29.5 dB tone SNR, where the RMS is ≈ 5 % FS.
  **This is judgement on a binned table, not A11's correlation method**; the
  terms are all driven by SNR on synthetic noise and a correlation would say
  nothing more. O27.

Result: 0 % flagged at ≥ 10 dB channel SNR, 28 % at 6 dB, 89 % at 3 dB,
100 % at ≤ 0 dB. The state-machine test (six noise runs, 20 s each) never
reaches `locstatus` ≥ 4 and `locdev` stays 0.

### A38 — publish rate: hysteresis on the localizer fields (2026-11-10, L5)
Every POST is also a flight-log row. Measured through the real Controller and
Publisher, steady DDM +0.0775, keep-alive 5 s, POSTs per minute
(`docs/loc-sweep.md`, "Publish rate"):

| channel SNR | plain rounding | shipped | VOR for comparison |
|---|---|---|---|
| 30 dB | 112 | 15 | 55 |
| 20 dB | 119 | 27 | 57 |
| 10 dB | 119 | 81 | 57 |

Shipped: `locdev` / `locddm` / `locsdm` / `locsnr` move only beyond a deadband
of 2 / 4 / 2 / 4 wire units of the value last sent, reset on any status or
channel change, so a shown `locdev` is at most 2 % FS (0.0031 DDM) from the
measurement. Two defects found on the way, both by test: moving to `round(x)`
when leaving the deadband overshot and **ping-ponged** between two values on
a flicker just over half the deadband (fixed: move the least distance back
into the deadband); and at 10 dB the TRACKING/DEGRADED split, where the score
sits, flipped `locstatus` **23 times in ~64 s** — ±5 points of hysteresis on
the split in LOC mode. At 10 dB 81/min remain: the 1 s DDM really moves by
3 % FS there; more would need smoothing, which costs lag.

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
**Amended by A23**: the first Pi run pegged four cores through OpenBLAS
busy-waiting, not DSP load; fixed by `_threadcaps.py`, post-fix figure not yet
recorded (O13).

### D5 — the offset convention follows the hardware; `swap_iq` is an override again (2026-09-11)
A22 left the choice open: flip generator and demodulator together, or keep
conjugating every block. Flipped. The argument that settled it: a decoder that
needs `swap_iq: true` on every standard RTL-SDR has the default wrong, and a
default that is wrong in the field is exactly the kind of thing this log
exists to prevent. Changed together, so the generate-and-decode tests could
not pass by accident: `dsp.py` mixes by `+offset_hz` (quarter-rate sequence
`[1, −j, −1, +j]`), `vor_gen.py` places the carrier at `−offset_hz` and tags
the sidecar with `synthetic.carrier_at = "-offset"`, `band_survey()` expects
`−tuner_offset_hz` and reports `sdr.ppm` with the physically correct sign,
`vor_spectrum.py` likewise, and the two independent reference decoders in
`tests/` flipped their own mixes.

Pinned without going through the generator:
`tests/test_dsp.py::test_carrier_where_real_hardware_puts_it_decodes` builds a
bare AM carrier with numpy at `−offset` and decodes it; its counterpart at
`+offset` must *not* decode against a noise floor; and `np.conj` moves it
back. A noiseless counterpart was tried first and passed vacuously — the
normalised depth `(env − carrier)/carrier` is scale-invariant, so a carrier
100 dB down in the stopband still reads 0.30. Worth knowing: **`amp_a` alone
never proves a signal is present**; `carrier` and `snr_db` do.

**Confirmed on real data:** `captures/capture_20260413T195020Z_109400.cu8`
now surveys as `carrier found +256.1 kHz … on target` and decodes *without*
`--swap-iq` to Δφ 135.467°, AM 0.271, SNR 38.2 dB — the same figures the
swap-iq decode in `new_Antenna_test.txt` produced. Files generated by the old
`vor_gen.py` lack `carrier_at`; `vor_replay.py` names them and they decode
with `--swap-iq`.

A `test_T12_no_state_growth_over_a_long_run` guard was added to CI. It cannot
run for eight hours, so it asserts the invariant that makes the eight-hour run
pass instead: internal buffers stay bounded and every expected window is
produced.

### D6 — hot path optimised from the profile, not from taste (2026-09-12)
`cProfile` over 20 s of 1.024 Msps signal, cu8 bytes in, observations out, on
the dev desktop (single-threaded BLAS per A23):

| stage | before | after | change |
|---|---|---|---|
| cu8 → complex64 | 0.275 s | 0.183 s | LUT built once; interleaved float32 pairs *are* complex64, so `.view()` replaces `re + 1j·im` and its three temporaries; `_PyRtlDevice.read` now shares the same function instead of rebuilding a LUT per read |
| `VorDemod.push` | 1.461 s | 0.324 s | see below |
| **total, 32768-sample blocks** | **8.7 %** of one core | **2.5 %** | 3.5× |
| total, 65536-sample blocks | — | 1.8 % | new shipped default |
| full chain through the controller, 65536 | — | **1.6 %** (62× realtime) | bearing, quality, publish policy included |

80 % of the DSP time was `PolyDecimator.__call__`, for two reasons that had
nothing to do with arithmetic. The taps were `float64`, so `wins @ taps`
promoted the entire `(M, 255)` sliding-window matrix of `complex64` to
`complex128` before the product — and then cast the result back. And the
sliding-window gather itself was an 8× expansion of the input. Replaced by a
true polyphase form: the input from the current phase viewed as `(M+K−1, D)`
rows, **one** product `rows @ H.T` giving every partial sum, and the output
read off the K diagonals through a strided view. One BLAS call plus one
reduction, independent of K, working precision throughout. Per call: channel
filter 1420 → 97 µs; the 32-phase tone filters 62 → 19 µs. Output identical
to float32 rounding (max |Δ| 1.2e-7 on noise), every DSP test unchanged, and
`capture_20260413T195020Z` replays to the same Δφ 135.467° / AM 0.271 /
SNR 38.2 dB to the digit. `scipy.signal.upfirdn` was tried and rejected:
slower (1.15 ms) and no streaming state.

Two things worth knowing that the profile taught:
- What is left is numpy dispatch on small arrays, not flops — so the strongest
  remaining lever is block size, which is configuration. `sdr.block_samples`
  goes 32768 → **65536** in the shipped config (1.62 % → 1.04 % for the DSP);
  131072 gains nothing more. 64 ms of latency is invisible at 1 Hz, the queue
  still holds 1 s, and an overrun still costs one window either way (F9).
- The quarter-rate mix is now a broadcast over rows of four (no per-sample
  index gather). Folding it into the channel taps would remove it entirely but
  couples the mixer's phase to the decimator's; not worth the coupling for
  ~0.3 % of a core, and D1 wants the port to stay mechanical.

The A3 gate now has 10× margin on the desktop. **O13 still stands**: this is
the container, not the aircraft; the Pi 5 figure with RB-01 and `dump1090`
running is the one that closes it.

**The ring recorder was a stall waiting to happen.** Measured with
`record.enabled`: the ring held 30 s of `complex64` (**246 MB** on a Pi), and
a flag `trigger()` concatenated, quantised and wrote all of it *inside*
`Controller.push()` — 2.14 s on the desktop, longer on a Pi writing to SD —
against an SDR queue holding ~1 s. Every flag capture would therefore have
caused an overrun and discarded the next window (F9): the forensic record
damaging the thing it records. Now the ring is held already quantised
(`encode_complex` rewritten on the float32 view, 2.7 ms → 0.26 ms per block,
so it can run per block; **61 MB**), and `trigger()` hands the chunks to a
writer thread and returns in 0.5 ms. A trigger arriving while a write is in
flight is skipped and counted, not queued — consecutive flags a second apart
would only copy the same samples. `flush()` joins the writer; the daemon calls
it at shutdown. Bytes on disk are identical to what the synchronous path wrote
(`test_ring_is_held_quantised_and_trigger_does_not_block_the_caller`).

Noticed on the way, not fixed: `record.trigger` accepts `manual` and `always`
in the schema, but nothing implements either — the controller handles
`on_flag` only and `__main__` never reads the key. Drift, listed below.

Side effect: the test suite went 1 min 51 s → 1 min 21 s (539 tests).

### D7 — the FM reference is now estimated, not demodulated: `fm_estimator: "harmonic"` (2026-09-12, gated)
A25 showed the FM subcarrier path to be the weak link — 3× noisier than the
AM path on every real capture, and below its threshold in 35 % of the orbit
windows that had a healthy tone SNR. The discriminator (doc A §7.2, A9)
differentiates a noisy phase; below ~10 dB subcarrier SNR it produces clicks
and the recovered deviation collapses (194 Hz at 3 dB channel SNR).

**The modulation is known.** The subcarrier baseband is
`b(t) = A·exp(j(β·sin(ωt + φ) + θ))` with ω = 2π·30 Hz and β ≈ 16; φ is
the reference phase we want, θ and A are nuisances. Its maximum-likelihood
estimate is the φ that maximises `|Σ b(t)·exp(−jβ sin(ωt+φ))|`, which never
differentiates anything and therefore has no threshold. By Jacobi–Anger,
`exp(−jβ sin x) = Σ_k J_k(β) e^{−jkx}`, so the correlation is a Fourier
series in φ, `C(φ) = Σ_k J_k(β)·B_k·e^{−jkφ}`, whose coefficients are the
DFT bins `B_k` of the window at k·30 Hz — one FFT of the window, one
3600-point inverse FFT to sweep φ, a parabola through the peak. Two
nuisances are handled explicitly: the station's subcarrier offset (ICAO
allows 9960 Hz ± 1 %; **the real station sits at +40.0 Hz**) is acquired by
a coarse scan that is a pure bin shift of the same FFT, accumulated over
three windows so a weak window cannot lock a harmonic alias (a ±30 Hz error
still correlates through neighbouring orders — the first prototype did
exactly that on the weakest capture), then tracked per window from the
correlation phase slope between the two half-windows; and β is taken from a
five-point grid, which makes the recovered deviation a by-product (the real
station: ~465 Hz, ratio 15.5). In this mode path A drops its tone LPF and
takes the 30 Hz DFT at 4 kHz, so **both paths now share exactly the same
filters** — A9's matched-delay principle without the exception it had to
make for the discriminator.

Measured (2 s windows; real captures with the car's motion removed):

| | discriminator | **harmonic** |
|---|---|---|
| FM-path noise, synthetic 10 / 3 / 0 dB | 0.25° / 2.45° / 9.1° | **0.04° / 0.12° / 0.19°** |
| bearing, synthetic 3 / 0 / −3 dB | 3.3° / 9.8° / 24.6° | **1.26° / 2.2° / 4.0°** |
| bearing, real strong captures ×3 | 0.79–0.88° | **0.36–0.44°** (now AM-limited) |
| bearing, real weak `195044` | 7.38° | **1.10°** |
| `sub_coh` no-signal vs signal | — | 0.010 vs 0.11–0.24 (`sub_level`: 0.164 vs 0.17–0.22) |
| calibration: radial disagreement, verify error | 0.004°, 0.017° | **0.000°, 0.0006°** |
| sweep RMS at 10 / 3 / 0 / −3 dB | 0.26 / 1.63 / 3.46 / 9.8° | **0.24 / 0.78 / 1.31 / 2.28°** |
| CPU | — | +0.4 % of a core (per window, independent of fs) |

`docs/sweep-harmonic.md` has the full sweep. A6 holds (CVOR = DVOR, reversed
DVOR fails). The constant differs from the discriminator's
(**+1 / −0.6077°** vs +1 / −1.9663°), so `vor_calibrate.py --fm-estimator`
must match `dsp.fm_estimator`; `docs/calibration.md` says so.

**Gated, not default.** The shipped config stays `"discriminator"` until a
live dongle has run it through the state machine (acquisition, retune,
relock). Everything else is in place: `vor_replay.py --fm-estimator
harmonic` on a real capture gives Δφ 134.19° with spread 0.65° (vs 0.93°),
and the daemon replays the same capture to TRACKING with the harmonic
constant. Flip = one config key plus a re-run of the calibration tool.

Two consequences to act on later:
- With the FM path no longer the limit, the A11 flag thresholds are
  conservative by ~3 dB: at 0 dB the harmonic sweep shows 1.3° RMS / 2.8°
  max, yet `q_am` (0.313 < 0.35) flags 100 %. Re-derive after field
  confirmation — **O17**. The AM path is the limit now (4° at −3 dB), so the
  5° line moves to about −4 dB, not further.
- `sub_coh` is a far better subcarrier-presence measure than `sub_level`
  and should replace it in `q_sub` once the estimator is default (O17).

### D8 — the stale-snapshot incident, and git as the only source of truth (2026-09-13)
**What happened.** The tree I worked on from 2026-09-11 to 09-13 (D5, D6, D7,
A20–A25, F3, F5) was a copy of 0.9.0 at interface v2.1. The real code had
moved on to 1.0.0 / v2.2 (A26) on the Pi and on GitHub; that copy was not in
front of me, and the directory was not a git repository until today. When the
whole tree was synced to the Pi for the gate, it overwrote 1.0.0, and the
flight that followed showed every A26 feature as a regression: radial 0 until
TRACKING instead of during discovery, measurements zeroed on every flag,
`vorstatus` never 5, no `vorfreqactive`, no `--csv` — and, because my config
shipped `null` per A2 while 1.0.0 shipped the flight calibration, possibly no
bearing at all.

**Reconciliation.** `git diff HEAD` showed the 1.0.0 lineage confined to the
publish path (`controller.py` +46, `publisher.py` +11, `__main__.py` +14,
`obscsv.py`, `mock_bridge.py`, two test files, the v2.2 spec); every other
file differed from HEAD only by my own edits, line counts identical. Their
side was ported onto mine (A26). Resulting config: the committed flight values
(`phase_sign +1`, `phase_offset_deg +2.12`, bridge at `192.168.10.1`,
`gain_db 14.4`) **plus** `swap_iq: false` — the committed `true` was correct
for the 1.0.0 chain and is wrong under D5 (it mirrors the band; the daemon
warns) — and the measured A25/D6 DSP settings. 560 tests pass; the real
capture replays through the daemon to radial 137–138° with all six fields.

**Corrections this forces in this file.**
- F1's premise is withdrawn: the committed 1.0.0 config says `phase_sign +1`,
  not −1. The −1 came from the older `rb-vor - Copy.json`; the sign the
  flight confirmed agrees with the synthetic calibration. The +4.09° between
  the flight constant (+2.12) and the synthetic one (−1.97) is the
  station/installation offset (T8), which the self-test now prints. O14
  (commit the orbit evidence) still stands; O1's *sign* question is answered.
- A2's rule "ship `null`" is superseded by 1.0.0's decision to ship the
  flight calibration in `config/rb-vor.json`; the mechanism (null → no
  bearing) is unchanged and its tests now construct that case explicitly.
- The "Copy" config row in the field table: its `phase_sign −1` is the older
  value; the flown one is +1.

**Rule from now on.** The repository is the source of truth. A session starts
with `git status` and `git log`; nothing is edited on a tree whose relation to
HEAD is unknown; the Pi is deployed from a commit, not from saved files. A
tree that shows files *deleted* relative to HEAD is not "clean", it is stale.

### D9 — localizer estimator, DDM form and window (2026-11-10, L4)
**Estimator: tone search** (A34). **Window 1 s, hop 0.5 s, no smoothing**
(`smooth_n` 1): DDM std at 10 dB is 4.4 / 3.2 / 2.7 / 2.4 % FS for windows of
0.5 / 1 / 1.5 / 2 s; a window lags by half its length, and 0.5 s of lag was
judged worth more on an approach than the 0.8 % FS a 2 s window saves.
Overlapped windows make further smoothing cheap in noise and dear in lag.

**DDM form (plan 4.3), measured, three candidates** (`docs/loc-sweep.md`):

| channel SNR | raw m150−m90 RMS %FS | normalised 0.40·DDM/SDM | published |
|---|---|---|---|
| 20 dB | 1.5 | 1.2 | 1.2 |
| 10 dB | 10.8 | 4.0 | 4.0 |
| 6 dB | 25.2 | 6.7 | 6.7 |
| 3 dB | 41.0 | 10.4 | 10.4 |

The raw form under-reads by exactly the SDM compression (A8): at 10 dB it
reads 87.5 % of the deviation, at 3 dB 51 % — the unsafe direction, as the
plan feared. The normalised form removes it at every SNR, **but** on a
lawful station whose SDM is not 0.40 it carries a scale error: the plan
assumed 0.40; Annex 10 Vol I allows 18–22 % per tone on course (3.1.3.5.2)
and an SDM of 30–60 % in coverage (3.1.3.5.3.6.1). Measured at full scale:
SDM 0.36 → +10.7 % FS, 0.44 → −9.4 %, 0.60 → −33.5 %, 0.30 → +32.8 %.
**Chosen: `DDM × max(1, 0.40 / SDM)`** — correct compression, never scale
down. Identical to the normalised form whenever the measured SDM is below
0.40 (every row above), raw when above: SDM 0.44 → −1.5 %, 0.60 → −1.6 %.
The cost: a station transmitting less than 0.40 over-reads (0.36 → +10.7 %
at full scale), i.e. errs towards *more* deviation. The SDM is published
(`locsdm`) and the raw DDM logged, so either can be re-derived.

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

## Field campaign (M10) — state of the evidence (2026-09-11)

`README.md` says "LIRZ orbit confirmed the bearing to 1.4°". Nothing in the
tree supports that number: `docs/first-light.md` has an empty log table and no
sidecar in `captures/` carries a position, a track or an expected radial. What
the tree *does* contain, none of it previously recorded here:

| artefact | what it is |
|---|---|
| `new_Antenna_test.txt` | `vor_replay.py --survey` output for **89 consecutive 10 s captures** at 109.400 MHz (`rtl_sdr` backend, offset −256 kHz, gain 14.4, ppm 2). Raw `delta_phi` sweeps smoothly 135° → 360° → 42° over ~12 min at ~22°/min — an orbit — then the signal collapses (AM 0.005, SNR −5 dB). Only 6 of the 89 `.cu8` files are in `captures/`. |
| `stdout.txt`, `stderr.txt` | three daemon runs in `--freq 109.4 --dry-run` bench mode with the field config below |
| ~~`config/rb-vor - Copy.json`~~ | the config those runs used — the only record of the field calibration. **Deleted 2026-09-13** when its SDR block was merged into `config/rb-vor.json`; the table below is now the only record. The merge brought `tuner_offset_hz: 0` and `swap_iq: true` back into the shipped config, which broke the synthetic replay test (a mirrored file, D5) — corrected the same morning to `−256000` / `false`; `driver: pyrtlsdr` and `ppm: 2` stay, being hardware facts. |

Field config against the shipped defaults:

| key | shipped | field | note |
|---|---|---|---|
| `bearing.phase_sign` | null (synthetic: +1) | ~~**−1**~~ **+1** in the 1.0.0 commit | F1 as first written was wrong — D8 |
| `bearing.phase_offset_deg` | null (synthetic: −1.9657) | **+2.12** | +4.09° beyond the synthetic constant = station/installation offset (T8) |
| `sdr.tuner_offset_hz` | −256000 | **0** | F2 |
| `sdr.swap_iq` | false | true | A22 |
| `sdr.driver` / `gain_db` / `ppm` | soapy / 32.8 / 0 | pyrtlsdr / 14.4 / 2 | SoapySDR is not installed on the Pi (`stderr.txt`) |
| `bridge.ws_url` / `rest_url` | `ws://localhost/bridge`, `…/bridgeUpdate` | `ws://localhost/bridge/float/ws`, `http://localhost/bridge/float` | the real bridge; partially answers O5 (no token seen) |
| `quality.snr_lo/hi_db` | 26 / 46 | 20 / 40 | F2 — compensating, not measured |
| `quality.flag_am_threshold` | 0.35 | 0.20 | F2 — compensating |
| `quality.flag_stab_threshold` | 0.40 | 0.30 | F2 — compensating |

### F1 — ~~the sign was wrong, as O1 warned~~; the proof is still not in the tree
~~A real station decodes with `phase_sign −1`, the opposite of the synthetic
calibration.~~ **Withdrawn 2026-09-13 (D8):** the 1.0.0 commit — the flown
config — carries `phase_sign +1`; the −1 came from an older file. The sign
agrees with the synthetic calibration; what the real station adds is a
+4.09° offset (T8). The paragraph below is kept as written for the record.
A real station was first thought to decode with `phase_sign −1`, the opposite
of the synthetic calibration. This is precisely the O1 scenario: `vor_gen.py` and `dsp.py`
share one author's CVOR sense and were consistently wrong together. A22 does
**not** explain it (conjugation leaves the envelope untouched). The orbit in
`new_Antenna_test.txt` cannot settle the sign on its own — a sweep direction
means nothing without the GPS track that says which way the car went — and the
comparison that produced −1 and the "1.4°" figure was done outside the repo.
**O1 stays open** until a dated orbit with GPS track, expected radials and the
decoded radials is committed (O14). Until then −1 / +2.12 is the best available
value and is *not* to be copied into `config/rb-vor.json`, which ships null by
A2.

### F2 — `tuner_offset_hz: 0` re-creates the A4 failure through the tuner's DC spur (hypothesis)
Measured, same station, same ~135° radial:

| | captures, offset −256 kHz + swap | live run, offset 0 |
|---|---|---|
| 30 Hz AM depth | 0.27–0.33 | ≈ 0.06–0.16 (`q_am` 0.20–0.55) |
| subcarrier level | 0.17–0.24 | ≈ 0.09–0.10 |
| 30 Hz SNR | 22–44 dB | < 20 dB (`q_snr` = 0.00 in 434 of 473 dashboard lines) |
| deviation | 400–470 Hz when strong | not shown |
| state | — | NO_SIGNAL 328, DEGRADED 79, TRACKING 21, ACQUIRING 42 lines |

The offset was presumably zeroed because, before `swap_iq` existed, −256 kHz
decoded nothing (A22) and 0 works regardless of which side the signal is on.
But at 0 the carrier sits on the RTL2832U's DC/LO-leakage spur, which inflates
the `carrier` estimate and dilutes `(env − carrier)/carrier` — the A4 mechanism
by a different route — and the 30 Hz tone drops out of the A11 SNR window.
The relaxed field thresholds then let the diluted signal through instead of
fixing the cause. The causal part is a hypothesis; the numbers are not.
**Next step, before any threshold is touched:** restore `−256000` (with
`swap_iq: false` since D5), re-run the same bench, and expect the A11 defaults
to hold. The field thresholds must not become the shipped defaults on the
strength of this run. The daemon now warns at startup when `tuner_offset_hz`
is 0.

### F3 — an `SdrError` inside the tick task is swallowed; the daemon idles headless (defect, **fixed 2026-09-11**)
Third run in `stderr.txt`: both backends fail to open
(`LIBUSB_ERROR_IO`, `SoapySDR is not installed`), then ten minutes of
`sdr {'blocks_read': 0, …}` and nothing else. Trace: `ticker()` →
`Controller.tick()` → `_retune()` → `SdrReader.set_frequency()` → `open()`
raises `SdrError`; the exception kills the ticker task and is never awaited,
`main()`'s `except SdrError` only guards `asyncio.run`, and `pump()` keeps
reporting an empty reader. `stdout.txt` confirms it: one `[IDLE]` dashboard
line after the banner, then silence. Under systemd the watchdog would restart
the unit (the notify lives in the dead task), which hides the bug rather than
fixing it; in bench mode nothing recovers. ~~Needs: the tick task's exception to
stop the daemon with exit code 3, plus a regression test with a device that
fails to open. Not fixed in this pass.~~ **Fixed:** every task in `_run()` has
a done-callback; the first failure sets `stop`, is re-raised after cleanup, and
`main()` maps `SdrError` to exit code 3. Regression:
`tests/test_main.py::test_unopenable_sdr_stops_the_daemon_with_exit_code_3`,
which hangs to its timeout without the fix. The same review found a second
way for the pump to die silently — `circular_mean` raising on a smoothing
window that cancels exactly — closed in `BearingTracker.update`.
**Narrowed by A30 (2026-11-06):** exit 3 now applies in bench mode only. Under a
bridge the same error is the `NO_SDR` state, with the dongle retried. The
done-callback on every task stands: any other task failure still stops the daemon.

### F5 — the sample pump could starve the event loop under overload (defect, **fixed 2026-09-11**)
Found by the D5 change, indirectly: after the convention flip the full suite
hung in `test_reader_outrunning_the_consumer_publishes_nothing_rather_than_garbage`,
which had passed 527/527 an hour earlier with no change to the code it
exercises. The test loops `for block, gap in reader.blocks(): controller.push(…)`
and checks a wall-clock deadline *between* calls — but `SdrReader.blocks()`
was an unbounded generator that kept yielding as long as the queue had an
item, and a fake device producing faster than the consumer kept it non-empty
for ever. Timing had merely tipped the other way.

`__main__.pump()` has the identical loop. On a Pi whose reader outruns the
demodulator — the overload case F9 exists for — the pump would never return
to the event loop: no ticks, no state machine, no watchdog feed, until systemd
killed it. So the "flaky test" was a daemon defect with a test attached.
Fixed in `SdrReader.blocks()`: it yields at most the number of blocks queued
at the moment of the call, so every caller gets control back at least once
per `buffer_blocks`. Regression:
`tests/test_sdr.py::test_blocks_returns_control_even_when_the_reader_outruns_the_caller`.
Also: `tests/conftest.py` now applies `_threadcaps` before numpy, because the
suite itself was running at ~380 % CPU on four cores (A23 in miniature).

### F4 — the Pi deployment is not what `install.sh` builds
`stderr.txt` shows `/opt/addons/rb-vor/lib/python3.13/site-packages/rtlsdr/` and
`vor_capture.sh` sources `/opt/addons/rb-vor/bin/activate`: on the Pi, `/opt/addons/rb-vor`
is a **venv** holding pyrtlsdr, and SoapySDR is absent. `install.sh` installs
the source tree there and `python3-soapysdr` from apt, and
`systemd/rb-vor.service` runs `/usr/bin/python3 -m rbvor`, which would not see
that venv. O12 (clean-image install) is therefore not just unverified — the
working installation was made by hand and differs from the installer. Also
of note: `vor_capture.sh` reads GPS from `http://localhost/getSituation`
(the bridge exposes a Stratux-style situation API) and cycles five channels
(114.4, 109.4, 111.8, 114.7, 115.2) at 3 s each.

### F6 — first flight log, LIRZ, 2026-09-27: the receiver was correct and heard nothing
`/home/stefano/roastbeef/test-data/flightlog-20260927175640.0159.sqlite`,
table `bridgefloat` (18,649 rows, 14 min 20 s). **The first real flight
evidence in the project.** The daemon tuned 109.400 at t=34 s, walked
TUNING → ACQUIRING in 0.3 s, dropped to NO_SIGNAL at t=38.7 s and stayed
there. `vorradial` never appears in the log at all — it never changed from 0,
so no bearing was ever published.

| | this flight | real station (car, 109.400) | nominal |
|---|---|---|---|
| `voram` (30 Hz AM ×100) | **0.5** mean, max 2 | 27 | 30 |
| `vorsnr` | **2.6 dB** mean, max 13 | 38–55 dB | — |
| `vorsub` | 17.7 ± 0.7 | 17–22 | 30 |

The AM depth — the field that carries the bearing — is 60× below nominal.
`vorsub` is *not* evidence of a subcarrier: 0.164 is what that band measure
reads on noise alone (D7), which is why `sub_coh` exists.

**It is noise, not a weak signal.** The readings are flat against everything
that would modulate a real station: ground (parked 5½ min) vs airborne is
2.50 vs 2.70 dB, 0.50 vs 0.55, 17.95 vs 17.72; range held 9.3–11.6 nm,
altitude 724–2493 ft, the radial swung 241°→273° true, banks reached 51°.
Nothing responds. The 26 above-median samples are scattered in time, not
clustered at closest range. Geometry was good and the signal should have been
easy: radio horizon ~94 nm against a 10 nm range. (Station position assumed
from the README example, `PRU` 43.0959/12.5132 — the charted frequency is
still unverified, O20.)

**The platform was healthy**: the ADS-B dongle logged 33,686 ES messages and
4,644 traffic targets in the same window, CPU 32–38 °C.

**The safety property held.** This is failure mode F1 *not* happening: given
nothing, the receiver published nothing rather than a plausible wrong needle.
A2/A11/A26 did their job in the air, which is the one thing that had never
been tested where it matters.

Causes, after the config was confirmed:
- ~~`swap_iq`/offset mismatch (the D5/D8 trap)~~ **eliminated.** The config
  carries `swap_iq: false` with `tuner_offset_hz: −256000`, the correct D5
  pairing — and it proves which code flew: `dsp` holds `afc_threshold_hz`,
  `afc_tau_s` and `fm_estimator`, three keys the 1.0.0 schema rejects
  (`additionalProperties: false`). On 1.0.0 that config fails to load and the
  daemon exits 2, publishing nothing; the log shows 1,038 publishes. Code and
  config were the matched merged pair, and the calibration was live
  (`phase_sign +1`, `phase_offset_deg +2.12`), so A2 was not suppressing the
  radial either.
- ~~**Leading hypothesis: the wrong dongle.**~~ **Eliminated by A28**: index 0
  is the VOR antenna. rb-vor was on the right coax. The leading hypothesis is
  now tuner-dependent spectral sense — the dongle is an **FC0012** and every
  convention here was measured on an **R820T**.
- Still open: `gain_db` was 14.4 (A27 raises it), and whether 109.400 carries
  a station there at all (O20).
- **No IQ exists from this flight**: `record.enabled` was false. Note before
  enabling it — with `trigger: on_flag` and a flag raised continuously, the
  recorder writes a 61 MB capture as fast as the card accepts one, ≈12 GB over
  a flight this length against the 17 GB free. D6 bounds the rate, not the
  total (O18).

---

## Open questions to Stefano ~~(block M7 only)~~

| Q | Question | Interim default in code |
|---|---|---|
| ~~Q1~~ | ~~`vorsignal` 0–100 or 0/1?~~ | ~~0–100 internally, `publish.signal_scale: "percent"`~~ **Moot since A20**: `vorsignal` is not on the wire; `signal_scale` is a dead key. |
| Q2 | `vorradial` = radial FROM or bearing TO? | radial FROM, magnetic, `publish.bearing_ref: "from"` — still open (O4) |
| ~~Q3~~ | ~~Publish periodic @1 Hz or on-change + keepalive?~~ | ~~`periodic`, 1.0 s~~ **Decided by A20**: always on-change, `keepalive_s` 5 s. |
| Q4 | Are the `loc*` field names and the `locdev` sense of RB-VOR-IF v2.3 (+ = 150 Hz = fly left, % of full scale) what RB-01 should consume? | as written in v2.3 (A33) |
| Q5 | Which ILS stations go in the plate's list, with which front courses? | none until read from the AIP |
| Q6 | Is a ground capture near an ILS runway possible before the aircraft receiver is fixed, and where? | L7 waits (O23) |
| Q7 | Licence for this repo (the `LICENSE` TODO)? | no GPL code ported; the localizer was written from Annex 10 and measurement |
| Q8 | Phase B hides the VOR plate's deviation bar in VOR mode too: it was always drawn centred there (a constant 0, never a deviation), which looks like "on course". Keep it hidden? | hidden; the bar is drawn only for a localizer with `locstatus >= 4` |

## Open technical items

See `docs/open-items.md`. Nothing there is to be filled with a plausible value.

## Known drift (2026-09-11) — verified against the tree, not yet corrected

Listed so the next session does not rediscover it. None of these change a
bearing; all of them mislead a reader.

- ~~`README.md`: "frozen at v1.0", v1.0 payload, `vordir`/`vorsignal`
  wording; `RUN.md`, `docs/bench-test.md`, `docs/install.md`,
  `docs/test-report-template.md` on v1.0 vocabulary; docstrings in
  `quality.py`, `publisher.py`, `bearing.py`, `sdr.py`, `bridge_client.py`
  predating their amendments; dead `publish` keys required by the schema.~~
  All corrected 2026-09-11. (`docs/RB-VOR-IF-v1.0.md` *did* already carry a
  superseded banner — the first draft of this list was wrong about that.)
  Test count at the time of the first review: 527 collected, 527 passed
  (Python 3.14 / numpy 2.5 / scipy 1.18 in a scratch venv; the dev machine has
  no project deps installed).
- Still on v1.0 vocabulary, deliberately left: doc A §10 and its header "not
  yet implemented" (doc A is the frozen design baseline; amendments live
  here), `docs/cloud-schema.md` (the *solution log* legitimately keeps
  `vordir`/`vorsignal` columns — they are the internal record, not the wire),
  the `TRACKING: vorsignal 87` log line (internal score, still true).
- `record.trigger: "manual"` / `"always"` are valid in the schema and in doc
  A §11 but implemented nowhere (D6). Either implement `always` as a rolling
  capture or drop the two values from the schema; do not leave a config that
  claims to record continuously and does not.
- **Deployment is file-by-file** (`.vscode/sftp.json`, `uploadOnSave`): only
  files saved in the editor reach the Pi. The first self-test on the Pi after
  the 2026-09-11/12 work (run 2026-09-13) showed a mixed tree — a new
  `config/rb-vor.json` against an old schema (`Additional properties are not
  allowed ('afc_tau_s' …)`), and a new generator against an old demodulator
  (synthetic AM depth 0.001, deviation 50 Hz: the two disagreed on the carrier
  side, D5). Not a code defect; every script-made edit had simply never been
  uploaded. The self-test now checks `CARRIER_CONVENTION` in `vor_gen` and
  `dsp` against each other and names a mixed tree explicitly, and a schema
  "additional properties" rejection says the same. **Deploy with the whole
  tree** (SFTP: Sync Local → Remote, or rsync), never by saving files.
- Loose files in the root that are neither code nor docs: `stdout.txt`,
  `stderr.txt`, `new_Antenna_test.txt`, `test_dongle.py`, `vor_capture.sh`,
  `vor_capture_filename.sh`, `config/rb-vor - Copy.json`, and 118 MB of
  `captures/*.cu8`. They are the only field evidence there is (see the M10
  section), so they are not to be deleted — but they need a home
  (`field/<gps-date>/…`) and a sidecar each.
- `.vscode/sftp.json` holds the Pi's SSH password in clear text. The tree is
  not a git repository today; before it becomes one, that file goes in
  `.gitignore` and the password changes.
- `tests/`: 233 test functions across 13 files, **539 after parametrisation,
  539 passed in 1 min 27 s** with `tests/conftest.py` capping BLAS threads —
  the same suite took ~10 min without the cap (A23 on the dev machine).
  `docs/open-items.md` gained O13–O16; O16 is closed (F3).
