# ILS localizer support for RB-VOR on RB-01: implementation plan

Brief for the implementing session (Claude Opus). Written 2026-11-10 against
`main` at `006c2f6`. Read `CLAUDE.md` first: it is the working log, and its
conventions (numbered amendments, struck-through corrections, "measured, not
assumed") apply to this work too. This plan does not repeat it.

The work has two phases in two repositories:

- **Phase A, this repo (`rb-vor`)**: the receiver decodes a localizer and
  publishes it. Milestones L0–L6.
- **Phase B, `../roastbeef` (RB-01)**: the VOR plate lets the pilot pick a
  localizer and draws the deviation. Milestones B1–B3. It starts only after
  phase A has frozen the wire format.

L7 (a real signal) closes both.

## 1. Goal

A pilot picks a localizer on the RB-01 VOR plate. RB-VOR, running on the same
Pi with the RTL-SDR, tunes it and publishes the lateral deviation on the float
bridge; the plate draws it on the deviation bar its HSI already has. With no
signal, nothing on the screen looks like a valid approach.

Today a localizer channel is rejected (`INVALID_FREQ`, doc A failure mode F5).

Phase A is done when:

- A localizer channel is tuned, decoded and published as a DDM and a validity
  status, on synthetic signals across the full legal ICAO tolerance, with an
  accuracy sweep recorded in `docs/`.
- With no signal, the receiver publishes "invalid", never "on the centreline"
  (section 4.1; this is the acceptance criterion that matters most).
- The VOR path is unchanged: every existing test passes unmodified, and
  `captures/capture_20260413T195020Z_109400.cu8` still replays to Δφ 135.467°,
  AM 0.271, SNR 38.2 dB, to the digit.
- The feature ships disabled (`loc.enabled: false`), like D7's harmonic
  estimator, until a real localizer has been decoded.

Not in scope: glide slope (329–335 MHz, a second receiver and antenna; the
HSI's GS markers stay parked), marker beacons, DME, Morse ident decoding
(`rbvor/ident.py` stays a placeholder), and autopilot coupling. This stays
supplemental equipment, not approach guidance, and the docs and the plate say
so wherever LOC appears.

## 2. The platform: what RB-01 fixes for this design

Read from `../roastbeef` on 2026-11-10 (`web/RB-01/addons/js/vor.js`,
`addons/vor.html`, `plates/js/hsi.js`, `test/integration/`). These are facts
about the consumer, and they settle most of what would otherwise be open.

| fact | consequence |
|---|---|
| The plate commands one key, `vorfreq`, from a hard-coded station list. OFF posts `vorfreq: 0` (the float bridge has no null). The Go side does not validate it. | Keep one command key. A localizer is selected by posting its frequency to `vorfreq`; `freqrules` decides the mode. No new command field. |
| There is one dongle, shared in turns with `rb-sdr-radio` (A30). | One tuned channel at a time, so one mode at a time. Only the selected demodulator runs (CPU budget of A3/O13, alongside the RB-01 render and `dump1090`). LOC inherits `NO_SDR` unchanged. |
| The plate draws the needle from `vorradial` **whatever `vorstatus` says**; the status is only a label beside it. | The `vor*` fields must not carry anything on a LOC channel that the present plate would draw as a radial. LOC gets its own fields and its own status (section 6). |
| The HSI renderer already has a lateral deviation bar: `update(outer, item, center, vertical, radial, glideSlope)`, `vertical` clamped to ±6. The VOR plate passes a constant 0. | Phase B feeds that bar; it needs no new drawing. Phase A must publish a quantity the plate can scale without knowing ICAO (section 6). |
| RB-01 rebroadcasts a bridge key only when its value changes, and has no staleness timeout. A dead daemon's last status stays on the bridge (`docs/tech-debt/radio-active-status.md`). | A stale "usable" status with a centred needle is the worst case this feature can produce. Phase A: on a clean stop the daemon publishes the LOC status as 0. A killed daemon is not covered until RB-01 has liveness; record it as an open item and state it in the spec. |
| The flight log records every bridge key (`bridgefloat` table, F6). | The `loc*` keys are logged in flight for free. That is the L7 evidence path. It also means the publish rate is a storage cost: see section 6. |
| `test/integration/dongle.py` in roastbeef imports `VorParams` and `generate_blocks` from `tools/vor_gen.py`, and `test_vor_radio.py` runs this daemon's real `main()`. | `vor_gen`'s API is a published interface: do not change it. `tools/loc_gen.py` exposes the same shape (`LocParams`, `generate_blocks`) so phase B can put a localizer on the stand-in's air. |
| Deployment: `/opt/addons/rb-vor`, config at `/boot/firmware/rb/rb-vor.json`, edited by hand on the boot partition. | An upgraded daemon must load an old config. The `loc` block is optional with defaults in code. |
| The aircraft's VOR dongle is an FC0012 that has not yet decoded anything in flight (F6, A28, O21, O22). | Phase A and B are proven on synthetic signals and the bench R820T. L7 is gated on that being fixed. |

## 3. About the reference repository

`github.com/owenson/rtlsdr-vor-loc-receiver` is C, built on Thierry Leconte's
`vortrack`. Per its README it decimates to a 50 kHz AM envelope, measures the
90 Hz and 150 Hz modulation depths, and reports `DDM = m150 − m90` against a
full scale of 0.155. It states no accuracy figures and has a synthetic test
harness for VOR only.

It is evidence that the approach is feasible, and nothing more. **Do not copy
or port its code**: it is GPL-2.0, and this repo's `LICENSE` is still a TODO.
Work from ICAO Annex 10 and from measurement. It offers no answer to the
three problems of section 4.

## 4. What makes this harder than it looks

The arithmetic is two tone amplitudes and a subtraction. These three points
are where the work is, and each is the localizer version of something
`CLAUDE.md` already learned the hard way.

### 4.1 Noise decodes as a perfect approach

With no signal, both depths read small and roughly equal, so DDM ≈ 0, which is
exactly "on the centreline". The VOR failure mode F1 (a plausible wrong
needle) is the *default* output of a naive localizer decoder, and F6 shows
the aircraft installation has already spent a whole flight hearing only
noise.

- Validity cannot be derived from DDM. It needs its own evidence: the sum of
  depths (SDM, nominal 0.40), the tone SNR against off-tone bins, and the
  carrier against the floor. An amplitude alone never proves a signal is
  present (D5).
- Required test: noise only, at several levels and seeds, through the full
  state machine, never reaches a usable status.
- The published deviation is forced to 0 only where the status already says
  unusable; the plate must gate on the status, and phase B tests that it
  does.

### 4.2 A fixed DFT bin fails inside the legal tolerance

ICAO allows the tone frequencies ±2.5 % (Cat I). At the limit, 150 Hz is off
by 3.75 Hz and 90 Hz by 2.25 Hz. Computed, not yet measured: with a 1 s
rectangular window the bin at exactly 150 Hz then reads about 6 % of the true
amplitude and the bin at 90 Hz about 10 %. The two tones lose *different*
fractions, so the DDM is grossly wrong on a lawful station. This is A7 (bin
versus band) and A9 (collapse at the legal limit) again.

The two tones come from a common source and are phase-locked, so one
fractional offset ε describes both. Candidates, to be compared by
measurement:

1. search ε to maximise `|X(90(1+ε))|² + |X(150(1+ε))|²`, then read both
   amplitudes at the found frequencies;
2. band power around each tone with the noise floor subtracted.

The test that pins the choice sweeps ε over the full ±2.5 %, edges included.

### 4.3 Low SNR pulls the needle toward the centre

A8: the envelope detector's DC output rises with noise, so measured depth
falls as SNR falls. Both tones shrink by the same factor, so `m150 − m90`
shrinks too: a weak signal under-reads the deviation, which is the unsafe
direction.

Dividing by the measured SDM and scaling by the nominal 0.40 cancels the
common factor, but assumes the transmitted SDM is 0.40, which holds near the
course line and not necessarily in the clearance sectors. Measure both forms
in the sweep, at several true DDM values and SNRs, and record the decision as
an amendment. Publish the SDM either way.

## 5. Signal facts

From memory of ICAO Annex 10 Vol I §3.1.3. **Verify each number against the
document before pinning it in a test or the spec; if the document is not
available, mark the number as unverified where it is used.**

| item | value |
|---|---|
| channels | 108.10–111.95 MHz, odd 100 kHz digit, 50 kHz grid: 40 channels |
| modulation | AM, 90 Hz and 150 Hz, 20 % each on the course line (SDM 40 %) |
| DDM | difference of the two depths; full-scale deflection at 0.155 |
| sense | 150 Hz predominates right of the front course as seen on approach (fly left); 90 Hz on the left |
| tone frequency tolerance | ±2.5 % Cat I, ±1.5 % Cat II, ±1 % Cat III |
| tone phase lock | the two tones are locked to each other (about 20° Cat I/II) |
| ident | 1020 Hz keyed, 5–15 % depth, "I" plus letters |
| polarisation | horizontal, same as VOR, so the same antenna |
| two-frequency systems | course and clearance carriers 5–14 kHz apart, straddling the channel |

What follows for this code base:

- The tones are present on the 4 kHz envelope (`a1` in `VorDemod.push`, after
  the 700 Hz filter and before the 100 Hz tone filter). The 100 Hz filter
  removes 150 Hz, so the VOR path cannot be reused past that point. The front
  end (down-mix, channel filter, AFC, envelope, carrier AGC) is shared as is.
- Any window that is a whole number of 1/30 s holds whole cycles of both
  tones when ε = 0. Choose the off-tone noise bins on purpose; the VOR bins
  (25–35 Hz) do not apply.
- The sign is less fragile than O1: "which tone is larger" cannot be flipped
  by a phase convention, and conjugating the IQ leaves the envelope untouched
  (A22). Generator and decoder still share an author, so the sense is only
  confirmed by a real station. A back course reverses it and the receiver
  cannot know; the docs say so.
- Two-frequency localizers are an unknown for the AFC (it measures mean phase
  advance and may be pulled between the two carriers) and argue against
  narrowing the channel filter for LOC, tempting as that is for SNR. Keep the
  13 kHz filter, add a synthetic two-carrier case, and measure what the AFC
  does. If it misbehaves, AFC off in LOC mode is an acceptable first answer.

## 6. Design

Decisions taken here need no re-litigation; deviate only with a measured
reason and an amendment.

**New module `rbvor/loc.py`.** `LocDemod`, `LocObservation` (`m90`, `m150`,
tone offset ε, carrier, noise estimate, overrun, `carrier_offset_hz`) and
`LocQuality`. Block functions over `np.ndarray`, state in a plain dataclass,
parameters in a frozen dataclass (D1). Reuse `PolyDecimator`, `_OnePole` and
the down-mix from `dsp.py`. Extracting a shared front-end class out of
`VorDemod` is allowed only if the VOR output stays bit-identical, proved by
the real-capture replay; otherwise compose the same pieces a second time and
leave `VorDemod` alone. Only the demodulator for the tuned mode runs.

**`freqrules.py`.** Add `is_loc_frequency()` and `loc_channels()`;
`is_vor_frequency()` keeps its exact behaviour (it is a published primitive).
One trap: the denylist currently returns `NavaidClass.LOC`. Once LOC is
decoded, a denylisted VOR channel would be decoded as a localizer. Add a
distinct class for "denied" and keep the reason string truthful.

**Controller.** `set_channel` classifies into VOR, LOC or rejected; the mode
is fixed per tuned channel. With `loc.enabled` false a LOC channel is
rejected exactly as today. States are reused. LOC does not depend on the
bearing calibration (A2 concerns a phase constant LOC does not have), so it
works with `phase_sign: null`. `_flush()` on retune covers LOC state: one
station's DDM is never attributed to the next (A26), including across a
VOR↔LOC change. A repeated frequency stays a no-op (A16). `vorfreq: 0` still
releases the dongle (RB-01's OFF). `NO_SDR` (A30) and the read on connect
(A31) need no change but each needs a LOC test.

**Quality and validity.** A separate estimator; do not bend the VOR one
(`q_sub` has no meaning here). Terms: SDM against 0.40, tone SNR, DDM
stability judged on the raw value with the smoothed value published (A15).
Thresholds and weights come from the sweep (A11's method), never from this
plan. "Warming up" stays distinct from "no signal" (A14).

**Update rate.** A localizer needle on approach wants less lag than a VOR
radial in cruise. `loc.integration_s` and `loc.hop_s` are separate from the
VOR ones; pick them from the measured noise-against-lag trade. Because
publishing is on change and every change is a POST and a flight-log row, the
published deviation needs a deliberate resolution: choose the quantum so that
a steady signal at a healthy SNR does not post on every window, and record
the measured post rate.

**Wire format, RB-VOR-IF v2.3, additive.** LOC has its own fields; on a LOC
channel the `vor*` fields read exactly as v2.2 reads a rejected frequency
(`vorstatus` 0, `vorfreqactive` 0, the rest 0). The present RB-01 plate then
shows OFF and a needle at 0, which is what it shows for a rejected frequency
today, and never a radial.

| field | type | meaning |
|---|---|---|
| `locstatus` | 0–5 | same codes as `vorstatus`; `>= 4` usable; 0 on a VOR channel, when idle, and on a clean stop |
| `locdev` | signed int, −100…+100 | deviation as a percentage of full scale (DDM 0.155 = 100), clamped; **positive = 150 Hz predominates = aircraft right of the front course = fly left**; 0 unless `locstatus >= 4` |
| `locddm` | signed int | measured DDM ×1000, not clamped, carried like the other measurements (A26) |
| `locsdm` | int | sum of depths ×100, nominal 40 |
| `locsnr` | int, dB | tone SNR |
| `locfreqactive` | int, kHz | tuned LOC channel, 0 when none |

`locdev` exists so the plate scales one number to its bar and needs no ICAO
constant; `locddm` is the raw measurement for the log. `locdev` is the one
field deliberately gated by validity, and the spec says why: unlike a radial,
its zero means something. The spec also states, in bold, that a consumer must
not draw the bar unless `locstatus >= 4`, and that a killed daemon leaves its
last values on the bridge. Write `docs/RB-VOR-IF-v2.3.md` from v2.2, bump
`IF_VERSION`, extend `tools/mock_bridge.py` to validate the new payload.
v2.2 §6 cites a `dme-plan.md` that is not in the tree; do not rely on it.

The `vor*` and `loc*` fields go out in the same POST, so a mode change is
atomic for the consumer.

**Config and schema.** A new optional `loc` block with defaults in code, so
the config on the Pi's boot partition keeps loading (the schema is
`additionalProperties: false`; F6 shows what a config/schema mismatch does in
the field). `loc.enabled` defaults to false and ships false.

**Logs.** `SOLUTION_SCHEMA` is the single source for the solution CSV and
`docs/cloud-schema.md`. Adding columns changes the header; check what
`solutionlog.py` does when appending to an existing file with the old header
before choosing between new columns and a separate LOC log. Same for
`obscsv.py`.

**Tools.** `tools/loc_gen.py`: DDM, SDM, tone offset ε, SNR, ident, optional
second carrier; honours `CARRIER_CONVENTION`, writes `carrier_at` in the
sidecar, reuses `vor_gen`'s noise and auto-level code, and exposes
`LocParams` plus `generate_blocks` like `vor_gen` does (section 2).
`vor_replay.py` selects LOC from the sidecar frequency. `vor_capture.py`
already has a flag to capture a localizer channel. A LOC sweep produces
`docs/loc-sweep.md`. The self-test gains a LOC stage, and its "109.5
localiser" line follows `loc.enabled`.

## 7. Phase A milestones (this repo)

Each ends with the full suite green (`python -m pytest`, deps from
`requirements.txt` in a venv under `~/.cache`, not `/tmp`, which has a quota;
see A32) and `ruff check .` clean. Nothing is committed or deployed by the
implementing session: Stefano reviews the working tree.

**L0. Baseline.** `git status` and `git log` first (D8). The tree has
uncommitted changes in ten files: they are Stefano's move to the RB-01 addon
layout (`/opt/addons/rb-vor`, `/boot/firmware/rb/rb-vor.json`, log paths).
Build on them, do not revert or reformat them. Run the suite and the
real-capture replay and note the figures.

**L1. Channel rules.** `freqrules` additions, the denylist fix, tests that
derive the expected classification independently (A5). No change for VOR.

**L2. Generator.** `loc_gen.py`, with an independent reference decoder in
the tests that does not import `rbvor.loc`. Include a case that must fail:
tones swapped give the negated DDM, so the tests cannot pass with a generator
that ignores its arguments (A6).

**L3. Demodulator.** `LocDemod` against the generator: DDM across −0.2…+0.2,
SDM, the ε sweep to ±2.5 % (4.2), carrier offset with and without AFC, a
sample gap, bounded state over a long run (T12's invariant), CPU cost per
mode added to the D6 table.

**L4. Sweep, quality, validity.** DDM error against SNR and true DDM for both
forms of 4.3; thresholds from that curve; the noise-only test of 4.1; the
two-carrier case. Write `docs/loc-sweep.md` and the amendments.

**L5. Controller and publisher.** Mode selection, states, flush, payload,
clean-stop publish, config, schema, mock bridge, solution log. End to end
against a live socket with the mock bridge re-asserting state at 2 Hz and
alternating VOR, LOC and 0: A16 was found only that way. Then, if Chromium
and Go are present, run `../roastbeef/test/integration/test_vor_radio.py`
unmodified: it must still pass, since it is RB-01's regression test for this
daemon. If it cannot run here, say so; do not claim it.

**L6. Docs.** IF v2.3 spec, README, RUN.md, amendments in `CLAUDE.md` (doc A
itself stays frozen), CHANGELOG, `docs/open-items.md`.

## 8. Phase B milestones (`../roastbeef`, after phase A)

Read that repo's `AGENTS.md` and `CLAUDE.md` first; its tree has unrelated
uncommitted radio work that must not be disturbed.

**B1. Plate.** `web/RB-01/addons/js/vor.js` and `vor.html`: read the `loc*`
keys; when `locfreqactive` is non-zero the plate is in LOC mode, shows the
LOC status label, and passes the deviation to the HSI's existing bar only
while `locstatus >= 4`; otherwise the bar is parked and the label says why.
Find the unit of `vertical` (±6) in `plates/js/hsi.js` before choosing the
scale from `locdev`, and check the bar's direction against the "fly left"
sense on a known input. The course pointer in LOC mode is the localizer's
front course from the station list, not `vorradial`.

**B2. Station list.** ILS entries carry type, frequency and front course.
Take them from the AIP, never from memory; if the source is not at hand, add
the mechanism with no entries and list the data as owed.

**B3. Tests.** `test/integration/dongle.py` radiates a localizer with a known
DDM using `tools/loc_gen.py`; `test_vor_radio.py` gains a round that picks
it, checks the bar and the label, then picks a channel with only noise and
checks the bar is parked. `test/display/test_vor.py` covers the new rows on
all five panels.

## 9. L7: a real signal, gated

Blocked on the receiver hearing anything in the aircraft at all (F6, O21,
O22). The cheapest first evidence is a ground capture near an ILS runway with
the bench R820T: frequency and ident from the chart (O20's lesson), GPS time
and position in the sidecar (A24), offset from the extended centreline known,
so the sign and rough size of the DDM can be checked. In flight, the
`bridgefloat` log plus the GPS track gives the same comparison for free.
Until one of them shows a sensible deviation, `loc.enabled` stays false in
the shipped config and a test pins that.

## 10. Recording the work

- Next free numbers when this was written: amendment **A33**, decision
  **D9**, open item **O23**, question **Q4**. Check before using them.
- Hypotheses are labelled as hypotheses. A figure in `CLAUDE.md` or the docs
  was measured, and the entry says on what.
- Expected open items: real-station confirmation of sense and scale; AFC on a
  two-frequency localizer; a killed daemon's stale `locstatus` on RB-01;
  the ICAO numbers of section 5 if they could not be verified.

## 11. Questions for Stefano

Work proceeds on the interim default; none of these blocks phase A.

| Q | question | interim default |
|---|---|---|
| Q4 | Are the field names and the `locdev` sense of section 6 what RB-01 should consume? | as written |
| Q5 | Which ILS stations go in the plate's list, with which front courses? | none until read from the AIP |
| Q6 | Is a ground capture near an ILS runway possible before the aircraft receiver is fixed, and where? | L7 waits |
| Q7 | Licence for this repo (the `LICENSE` TODO). | port no GPL code |
