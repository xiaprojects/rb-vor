# Full ILS for RB-VOR on RB-01: glide slope and multiple SDRs, implementation plan

Brief for the implementing session (Claude Opus). Written 2026-11-11 against
`main` at `006c2f6` **plus the uncommitted localizer work** (A33–A38, D9,
`docs/loc-plan.md` phase A and B). Read `CLAUDE.md` first: it is the working
log, and its conventions (numbered amendments, struck-through corrections,
"measured, not assumed") apply here. Read `docs/loc-plan.md` and
`docs/RB-VOR-IF-v2.3.md` second: this plan builds on both and does not repeat
them.

Two things are added, and the second is what makes the first possible:

1. **Glide slope (GS)**: the vertical half of an ILS, 329–335 MHz.
2. **More than one SDR**: the config lists the dongles; the receiver uses
   every one that is present. With two, localizer and glide slope are received
   together. With one, it is shared and **the pilot switches** which of the two
   is received.

The work has two phases in two repositories, as for the localizer:

- **Phase A, this repo (`rb-vor`)**: milestones G0–G7.
- **Phase B, `../roastbeef` (RB-01)**: milestones H1–H4, after phase A has
  frozen the wire format (RB-VOR-IF v2.4).

G8 (a real signal) closes both.

## 1. Goal

A pilot picks an ILS on the RB-01 VOR plate, exactly as today (one frequency,
the localizer's). RB-VOR works out the paired glide-slope channel and:

- **two receivers available**: tunes the localizer on one and the glide slope
  on the other, and publishes both deviations; the plate draws the lateral bar
  and the GS markers its HSI already has;
- **one receiver available**: receives the localizer, and the plate offers a
  LOC / GS switch. The feature not selected shows *nothing*, never its last
  value.

Phase A is done when:

- A glide-slope channel is tuned, decoded and published as a DDM and a
  validity status, on synthetic signals across the full legal ICAO tolerance,
  with a sweep recorded in `docs/gs-sweep.md`.
- With no signal the receiver publishes "invalid", never "on the glide path"
  (section 4.1). Noise on both receivers at once must not look like a
  perfect ILS: that is the acceptance criterion that matters most.
- One and two receivers are both proven through the real `main()` against the
  mock bridge, including a dongle that appears, disappears or is busy.
- VOR and LOC are unchanged: every existing test passes unmodified, and the
  G0 fingerprints (section 7) are identical at the end.
- The feature ships disabled (`gs.enabled: false`) until a real glide slope
  has been decoded (G8), and a test pins that.

Not in scope: marker beacons, DME, Morse ident, autopilot coupling, automatic
time-multiplexing of one dongle between LOC and GS (the pilot switches; see
section 6.2), and any change to how `rb-sdr-radio` shares a dongle (A30
stands). This stays supplemental equipment, **not approach guidance**, and the
docs and the plate say so wherever GS appears. A vertical needle invites more
trust than a lateral one; the wording is not optional.

## 2. The platform: what is fixed before the design starts

Read on 2026-11-11 from this tree and `../roastbeef`. Facts, with their
consequences.

| fact | consequence |
|---|---|
| A GS channel is paired one-to-one with a localizer channel (section 5). | No new frequency command. `vorfreq` on a localizer channel selects the ILS; `freqrules` gives the GS channel. |
| One dongle sees 1.024 MHz. Localizer (~110 MHz) and glide slope (~330 MHz) are 220 MHz apart. | LOC and GS at the same time needs two dongles. There is no DSP way round it. |
| `Controller` owns one `SdrPort`, one `mode` (`vor` / `loc`), one state. `__main__` builds one `SdrReader` and one pump. Every existing test builds `Controller(cfg, sdr, publisher)`. | The second receiver is an optional hook with default `None`, like `recorder=` (M9). The constructor's existing arguments keep their meaning. |
| `sdr.resolve_device()` picks **one** dongle from `device_serial` / `device_index`; an absent serial is a loud error (A27). `tuner_profiles` override `swap_iq`, `gain_db`, `ppm` per tuner type (A28). | The list form is new and its "absent" rule differs (section 6.1); the single form keeps A27 exactly. Profiles keep working per opened device. |
| A localizer and a glide slope carry the **same modulation**: 90 Hz and 150 Hz AM. `LocDemod` (search estimator A34, priming A35) is parametric. | Reuse `LocDemod`; do not write a second demodulator. What differs is the nominal SDM, the full scale, the sense, the carrier tolerance and the thresholds, and each of those is measured again (section 4). |
| The HSI renderer has GS markers: `update(outer, item, center, vertical, radial, glideSlope)`; `glideSlope` is clamped to ±15 and moves both markers by `-glideSlope * 25 / 3` px, so positive is **up** and 3 units are one dot (dots 25 px apart, drawn at −4…+4). The VOR plate passes `$scope.items.slope`. | Phase B feeds that argument. Find what `items.slope` is today and where "parked" is before choosing the scale (H1). |
| The plate is in LOC mode while `locfreqactive > 0`, and draws the bar only while `locstatus >= 4` (`addons/js/vor.js`). | The same rule, per axis, for GS. In single-receiver GS mode `locfreqactive` is 0, so the plate's mode test must change (H1). |
| RB-01 rebroadcasts a key only when it changes and has no staleness timeout (O25, roastbeef `docs/tech-debt/radio-active-status.md`). | A stale usable `gsstatus` beside a centred marker is the worst case here too. A clean stop posts `gsstatus` 0; a killed daemon is not covered and the spec says so. |
| RB-01's `main/sdr.go` `configDevices()` hands every **untagged** dongle to any enabled band (1090 / 978 / 868 / AIS) that still lacks one, and caps the count at 3. | A NAV or GS dongle with no `stratux:` tag can be taken by Stratux. Today that is avoided by luck of configuration. With more dongles it needs a rule (H3). |
| `rb-sdr-radio` picks its dongle the same way (`device_serial` / `device_index`) and yields per A30. | Any listed dongle may be found busy at any retune. `NO_SDR` becomes per receiver. |
| `test/integration/dongle.py` in roastbeef stands in for **one** USB device and imports `VorParams` / `LocParams` and `generate_blocks` from this repo's tools. | Those APIs stay as they are. A two-receiver test needs a second stand-in device (H4). |
| The aircraft has an FC0012 on the VOR antenna (covers 22–948 MHz, so it reaches 330) and an R820T2 on 1090 (A28). Neither has decoded a navaid in flight (F6, O21, O22). | Hardware reaches the band. Whether the antenna and the FM band-stop filter pass 330 MHz is unknown (O6, section 9). Everything here is proven on synthetic signals and the bench R820T first. |
| `config/rb-vor.json` in the working tree has `loc.enabled: true`, while `tests/test_loc_controller.py::test_shipped_config_has_the_localizer_disabled` pins `false`. | Probably Stefano's bench edit. G0 reports it; do not revert it and do not edit the test. Ask (Q9). |

## 3. Sources

Work from ICAO Annex 10 Vol I §3.1.5 (glide path) and §3.1.6 (pairing) and
from measurement. A33 found a scan of Vol I amended to No. 74; use the same
text and say which edition each figure came from. As in `loc-plan.md` §3: the
`owenson/rtlsdr-vor-loc-receiver` repository is GPL-2.0 and is **not** to be
copied or ported; it has no glide slope anyway.

## 4. What makes this harder than it looks

The demodulator exists. These five points are where the work is.

### 4.1 Noise on two receivers is a perfect ILS

A33/A37 established that noise decodes as DDM ≈ 0. With a glide slope the
failure is complete: lateral centred, vertical centred, both "valid". And F6
shows the aircraft installation has spent a whole flight on noise.

- GS validity is judged on its own evidence (tone SNR, SDM, steadiness of ε,
  detrended residual), never on the DDM value, and **never borrowed from the
  localizer**: a tracking localizer says nothing about the other antenna
  path, the other dongle, or the other frequency.
- Required test: noise only on both receivers, several levels and seeds,
  through the full state machine, `gsstatus` never reaches 4 and `gsdev`
  stays 0. Then: a good localizer with noise on the GS receiver, and the
  reverse. Each axis must fail alone.
- `gsdev` is gated by validity exactly like `locdev`, for the same reason.

### 4.2 The legal carrier tolerance is wider than the channel filter

Computed, not yet measured. Annex 10 allows a single-frequency glide path
carrier ±0.005 %. At 335 MHz that is **±16.75 kHz**. The channel filter is a
13 kHz low-pass (A25), and the AFC measures the residual on the 32 kHz
baseband *after* that filter. So a lawful station at the limit is outside the
filter before the AFC can see it. The dongle adds its own error, and at
330 MHz every ppm is three times what it is at 110 MHz: 20 ppm is 6.6 kHz.

This is A9 again: a chain that works at nominal and collapses inside the
legal tolerance. Real stations are usually within a few ppm, which is exactly
why it would not be noticed on the bench.

Measure first: DDM error and validity against carrier offset from −25 to
+25 kHz, AFC on and off, at two SNRs. Then choose, by measurement, among at
least:

1. a coarse carrier acquisition before the channel filter (an FFT peak search
   on the 1.024 Msps block over about ±30 kHz at retune, then the existing AFC
   tracks), applied to GS mode only;
2. a wider channel filter for GS with the AFC as it is (costs SNR; measure how
   much).

Whatever is chosen, a carrier found far off centre must be **reported**
(`carrier_offset_hz` in the log), and an offset that cannot be acquired must
end in NO_SIGNAL, not in a DDM. Do not change the VOR or LOC front end for
this; if the acquisition stage is shared code, it is off in those modes and
the G0 fingerprints prove it.

Two-frequency (capture-effect) glide paths have two carriers 4–32 kHz apart
(to verify). A36 showed the AFC settles on the power-weighted mean; an
acquisition stage that picks "the strongest peak" may hop between them. Add
the synthetic two-carrier case to the sweep and record what happens.

### 4.3 SDM is 0.80, not 0.40: D9 does not transfer by assumption

A glide path modulates 40 % per tone (SDM 0.80, tolerance 37.5–42.5 % per
tone, to verify). Three things follow, each to be measured, none to be
assumed from the localizer sweep:

- **Envelope compression** (A8) at SDM 0.80 is not the same curve. D9's
  correction `DDM × max(1, 0.40 / SDM)` becomes a candidate
  `DDM × max(1, 0.80 / SDM)`; repeat the three-way comparison of D9 (raw,
  normalised, one-sided) at several true DDMs and SNRs, and at lawful SDMs
  other than 0.80.
- **Deep modulation.** With SDM 0.85 and both tones in phase the envelope
  comes within 15 % of zero. The envelope detector, the one-pole carrier
  estimate and the cu8 quantisation at low level all behave differently
  there. Include SDM at both tolerance edges with the tone phase swept.
- **Validity thresholds** (A37): `flag_sdm`, `flag_snr_db`, the weights and
  the TRACKING/DEGRADED split are re-derived from a GS sweep. They live in
  their own config block; the localizer's numbers are not reused silently.

### 4.4 The pairing table is safety data

A wrong row tunes another facility's glide path: a valid-looking vertical
needle that belongs to a different runway. The 40 pairs in section 5 are
written from memory. Before any code depends on them:

- verify every row against Annex 10 Vol I §3.1.6.1 and record the edition;
- in the test, write the table out as text a second time, independently of
  `freqrules.py` (the A5 principle), and check the two properties that catch
  transcription errors: 40 distinct GS channels, all on the 150 kHz grid
  from 329.15 to 335.00 MHz;
- if the document cannot be reached, the table ships marked unverified, an
  open item says so, and `gs.enabled` cannot be true in the shipped config.

### 4.5 One dongle, two needles: the unselected one must go dark

With one receiver the pilot switches between LOC and GS. The trap is the
needle that is *not* being received. Its last value is the most plausible
thing on the screen and it is wrong within seconds.

- On every switch: enter TUNING and flush before building the payload (the
  A26 retune order), so the first POST after a switch carries status 2 for
  the new feature and **status 0, deviation 0, `*freqactive` 0** for the old.
- A repeated switch command to the feature already selected is a no-op at any
  point, including mid-retune (A16: the bridge re-asserts state).
- Required test: LOC tracking, switch to GS, and assert that no payload after
  the switch command carries `locstatus >= 4` or a non-zero `locdev`; and the
  reverse.
- A new `vorfreq` always starts on LOC (lateral first). The switch does not
  survive a channel change.

## 5. Signal facts

**From memory of ICAO Annex 10 Vol I §3.1.5 and §3.1.6. Verify each number
against the document before pinning it in a test or the spec; where the
document is not available, mark the number unverified where it is used.**

| item | value |
|---|---|
| band | 328.6–335.4 MHz; 40 channels, 329.15–335.00 MHz, 150 kHz apart |
| carrier tolerance | ±0.005 % single-frequency; ±0.002 % each carrier of a two-frequency system, carriers 4–32 kHz apart |
| modulation | AM, 90 Hz and 150 Hz, 40 % each on the path (SDM 80 %), 37.5–42.5 % |
| tone tolerance, phase lock | as the localizer: ±2.5 / 1.5 / 1 % Cat I / II / III, tones locked |
| sense | **150 Hz predominates below the path (fly up)**, 90 Hz above (fly down) |
| DDM | 0.0875 at the edge of the half path sector; full-scale deflection at **0.175** by airborne convention (not in Annex 10; same caveat as O26) |
| sector | DDM 0.0875 at about ±0.12 θ from the path, θ the glide angle (2–4°, usually 3°); so full scale is roughly ±0.7° at 3° |
| ident | none on the glide path |
| polarisation | horizontal |
| false paths | above the true path at multiples of θ, some with reversed sense; below about 0.45 θ the signal is not specified. The receiver cannot know. |

Pairing, localizer MHz → glide path MHz (§3.1.6.1, **to verify row by row**):

| LOC | GS | LOC | GS | LOC | GS | LOC | GS |
|---|---|---|---|---|---|---|---|
| 108.10 | 334.70 | 109.10 | 331.40 | 110.10 | 334.40 | 111.10 | 331.70 |
| 108.15 | 334.55 | 109.15 | 331.25 | 110.15 | 334.25 | 111.15 | 331.55 |
| 108.30 | 334.10 | 109.30 | 332.00 | 110.30 | 335.00 | 111.30 | 332.30 |
| 108.35 | 333.95 | 109.35 | 331.85 | 110.35 | 334.85 | 111.35 | 332.15 |
| 108.50 | 329.90 | 109.50 | 332.60 | 110.50 | 329.60 | 111.50 | 332.90 |
| 108.55 | 329.75 | 109.55 | 332.45 | 110.55 | 329.45 | 111.55 | 332.75 |
| 108.70 | 330.50 | 109.70 | 333.20 | 110.70 | 330.20 | 111.70 | 333.50 |
| 108.75 | 330.35 | 109.75 | 333.05 | 110.75 | 330.05 | 111.75 | 333.35 |
| 108.90 | 329.30 | 109.90 | 333.80 | 110.90 | 330.80 | 111.90 | 331.10 |
| 108.95 | 329.15 | 109.95 | 333.65 | 110.95 | 330.65 | 111.95 | 330.95 |

What follows for this code base:

- Not every localizer has a glide path (a LOC-only approach, an offset
  localizer). The receiver cannot know; it tunes the paired channel and
  reports NO_SIGNAL. The docs say so.
- The sign is as robust as the localizer's (which tone is larger; conjugating
  IQ leaves the envelope untouched, A22), and as unconfirmed: generator and
  decoder share an author (O23's lesson). Only a real station settles it.
- The false paths are the glide slope's back course: a valid, steady,
  wrong needle. Not solvable in the receiver. State it in the spec and on the
  plate's help text.

## 6. Design

Decisions taken here need no re-litigation; deviate only with a measured
reason and an amendment.

### 6.1 Settings: a list of SDRs

A new optional key `sdr.devices`, an ordered list. Absent, the receiver
behaves exactly as today (one dongle from `sdr.device_serial` /
`sdr.device_index`, A27 rules, pinned by a test). The schema is
`additionalProperties: false` and the config on the Pi's boot partition is
edited by hand: an old config must keep loading (A33's rule, F6's lesson).

```json
"sdr": {
  "devices": [
    { "device_serial": "rbnav1", "use": ["nav", "gs"] },
    { "device_serial": "rbgs1",  "use": ["gs"], "gain_db": 32.8, "ppm": 2 }
  ]
}
```

- Each entry selects one dongle by `device_serial` (preferred) or
  `device_index` (allowed, with the A27 warning).
- `use` says what that dongle **may** serve: `nav` (VOR and localizer), `gs`,
  or both; default both. It exists because a dongle is wired to an antenna,
  and an antenna or a filter may pass only one of the two bands. The config
  is where that fact about the installation is written down.
- Optional per-entry overrides: `gain_db`, `ppm`, `swap_iq`, `bias_tee`.
  Precedence: device entry, then `tuner_profiles`, then the `sdr` block. Log
  the effective values at open (A28).
- Everything else (`sample_rate_hz`, `tuner_offset_hz`, block sizes,
  `retry_s`) stays common in the `sdr` block.

**Allocation is one pure function**, in the style of `resolve_device()`, so
the whole policy is testable with no radio:

```
allocate(devices_cfg, serials_present, want) -> {"nav": DeviceChoice | None,
                                                 "gs":  DeviceChoice | None,
                                                 "shared": bool, "notes": [...]}
```

Rules, in order:

1. Consider only listed entries whose dongle is present in the enumeration.
2. `nav` takes the first present entry that allows `nav`.
3. `gs` takes the first *other* present entry that allows `gs`.
4. If there is none, and the `nav` dongle also allows `gs`, the two share it:
   `shared` is true and the pilot's switch decides (section 6.2).
5. A feature with no dongle at all is `None`: that feature is NO_SDR.
6. Two entries resolving to the same dongle, or two dongles sharing a listed
   serial, is a configuration error at load or at first enumeration.

**"Use all of them" means all of the listed ones.** The receiver never opens
a dongle that is not in its list, because the others belong to Stratux
(1090 / 978 / 868 / AIS) and to `rb-sdr-radio`.

**Absent is not an error in the list form.** A27 made a missing pinned serial
a loud error, because with one dongle the alternative was silently decoding
another antenna. A list exists precisely so that the receiver works with
whatever is plugged in. So: a listed dongle that is absent is a warning naming
every serial that *is* present; allocation proceeds with the rest; no dongle
at all is NO_SDR (A30), retried every `sdr.retry_s`. This relaxes A27 for the
list form only; record it as an amendment. The guarantee A27 was written for
still holds: nothing is ever opened that the config did not name.

**Allocation is re-evaluated at every tune attempt** (a new channel, a
switch, an NO_SDR retry), not once at startup: dongles are hot-plugged, and
the radio takes and returns them. Enumeration costs a USB query; do it on
those events only, never per block. A dongle pulled *during* reception is
A30's known gap; it stays open, but it now has a per-receiver form, so say
in the amendment what happens on each receiver.

Busy is per receiver. If the GS dongle is held by `rb-sdr-radio`, the glide
slope is NO_SDR and the localizer carries on. The receiver does **not** fall
back to sharing the NAV dongle in that case: allocation by presence is
predictable, allocation by who-got-there-first is not, and a mode that
changes with the radio's on/off switch would be untestable from the plate.
Measure nothing here; it is a decision. If Stefano wants the fallback, it is
Q10.

`python3 -m rbvor --devices` prints index, serial, tuner and the allocation
the current config would produce, like `rb-sdr-radio devices`. The startup
banner prints the same allocation.

### 6.2 The pilot's switch (one receiver)

One new consumed key on the float bridge, **`gsselect`**: 0 = localizer
(default), 1 = glide slope.

- It is acted on only while a localizer channel with a paired glide path is
  selected **and** allocation says `shared`. Otherwise it is read and
  ignored: with two receivers both run; on a VOR channel there is no glide
  path.
- Absent means no change, as for `vorfreq`. It is read on connect (A31), so a
  restarted daemon returns to what the pilot chose.
- A change of `vorfreq` resets the receiver's selection to LOC, whatever the
  bridge still holds for `gsselect`; the plate posts `gsselect: 0` together
  with every station pick so the bridge agrees (H1). The receiver must not
  depend on the plate doing so.
- It is debounced like a frequency (`frequency.debounce_ms`).

The receiver tells the plate whether the switch is needed with one published
field, `gsshared` (section 6.4), so the plate needs no knowledge of the
hardware.

Automatic alternation (LOC for N seconds, GS for N seconds) is rejected for
this version: each needle would be stale half the time, each retune costs
`settle_ms` plus an acquisition, and 4.5 would have to hold on every hop.
If it is ever wanted it is a separate plan with its own measurement.

### 6.3 Receiver structure

- **`rbvor/freqrules.py`**: `gs_channel_khz(loc_khz) -> int | None` and
  `gs_channels()`, from the pairing table. `classify()`,
  `classify_navaid()`, `is_vor_frequency()` and `is_loc_frequency()` keep
  their exact behaviour.
- **`rbvor/gs.py`**: small. `GsDspParams` built from `LocDspParams` (so the
  front end is the same code), the GS constants (`SDM_NOMINAL` 0.80,
  `DDM_FULL_SCALE` 0.175), `ddm_to_dev_pct` for GS, `GsQualityParams`, and
  whatever 4.2 adds. The demodulator is `LocDemod`. If `LocDemod` needs a
  parameter it does not have (nominal SDM for the correction, the
  acquisition stage), add it with a default that leaves the localizer
  bit-identical, proved by the G0 localizer fingerprint.
- **A GS chain object** holding demodulator, quality estimator, last
  observation and its own state (the existing `State` values; NO_SDR, TUNING,
  ACQUIRING, TRACKING, DEGRADED, NO_SIGNAL all apply). It is fed blocks from
  either source:
  - *dedicated*: its own `SdrReader`, passed to `Controller` as a new
    optional argument (default `None`);
  - *shared*: the controller's existing reader, with `mode` taking a third
    value `"gs"`. Only the selected demodulator runs.
- **`Controller`**: on a retune to a localizer channel with `gs.enabled`, it
  asks the allocation what GS gets and starts the chain accordingly. `_flush()`
  covers the GS chain; a VOR channel, `vorfreq` 0, IDLE and INVALID_FREQ
  release **both** dongles (A21). The NAV side must not wait for the GS side:
  a GS dongle that will not open never delays or fails the localizer.
- **`__main__`**: builds the readers from the allocation; the pump drains
  every reader; F5's bound (at most the queued blocks per call) holds per
  reader so neither can starve the other or the event loop. F3's
  done-callback covers the new tasks. Bench mode (`--freq`) with a localizer
  frequency and `gs.enabled` also tunes the glide path when a second dongle is
  listed, and keeps F3's exit 3 for the NAV dongle only.
- **`sdr.py`**: `allocate()`; `open_device()` takes a resolved choice; one
  enumeration helper shared with `--devices`. `vor_capture.py` uses the same
  code, so a capture and a live decode cannot disagree about which antenna
  they heard (A27).
- `gs.enabled` requires `loc.enabled`; say so in config validation with a
  message that names both keys.

Two receivers run two DSP chains. CPU measured on the desktop is about 1 % of
a core each (O28); the Pi figure with RB-01 and `dump1090` running is O13 and
is now more pressing. Also unmeasured: USB throughput with three or four
dongles on one Pi (overruns, F9). Add both to the sweep document for the
desktop and to the open items for the Pi.

### 6.4 Wire format, RB-VOR-IF v2.4, additive

Consumed, new: `gsselect` (section 6.2).

Produced, new, **only when `gs.enabled`** (then on every payload, zeros when
not applicable). The payload is exactly the 6 `vor*` keys, or those plus the
6 `loc*` keys, or those plus the 7 `gs*` keys. A receiver without
`gs.enabled` is byte-for-byte v2.3.

| field | type | meaning |
|---|---|---|
| `gsstatus` | 0–5 | same codes as `vorstatus`; `>= 4` usable; 0 when idle, on a VOR channel, when the glide path is not the selected feature of a shared receiver, and on a clean stop |
| `gsdev` | signed int, −100…+100 | deviation in % of full scale (DDM 0.175 = 100), clamped; **positive = 150 Hz predominates = aircraft below the path = fly up**; 0 unless `gsstatus >= 4` |
| `gsddm` | signed int | published DDM ×1000, not clamped, not gated (A26) |
| `gssdm` | int | sum of depths ×100, nominal 80 |
| `gssnr` | int, dB | tone SNR |
| `gsfreqactive` | int, kHz | glide-path channel a receiver is tuned to (332600), 0 when none |
| `gsshared` | 0 / 1 | 1 when the localizer and the glide path of the selected ILS have to share one receiver, so only one is received and `gsselect` chooses; 0 otherwise |

Rules the spec states and `tools/mock_bridge.py` checks:

- `*freqactive` always tells the truth about a tuner. In shared GS mode
  `locfreqactive` is 0 and `locstatus` is 0: the localizer is not being
  received. The consumer knows the ILS is selected from the `vorfreq` it
  sent and from `gsfreqactive`.
- Never `vorfreqactive` together with `locfreqactive` or `gsfreqactive`.
  With `gsshared` 1, never `locfreqactive` and `gsfreqactive` together.
- `gsdev` 0 below status 4; `locdev` likewise (unchanged).
- NO_SDR on the GS receiver is `gsstatus` 1 with `gsfreqactive` set, as the
  localizer does (v2.3 §5).
- **A consumer must not draw the glide-slope marker unless `gsstatus >= 4`,**
  in bold, with the killed-daemon caveat of v2.3 §3.12, and a paragraph on
  false paths and on localizers without a glide path.
- Sign: both `locdev` and `gsdev` are positive when the 150 Hz tone
  predominates. That is one rule for the receiver and two different
  directions on the display (fly left, fly up); the spec spells out both.

All fields of one update travel in **one POST**, with two receivers too: the
publisher merges the latest state of both chains. The hysteresis of A38
applies to the `gs*` measurement fields with deadbands chosen by the same
measurement; with two chains posting, the rate per minute is measured again
and recorded, because every POST is a flight-log row.

Write `docs/RB-VOR-IF-v2.4.md` from v2.3, bump `IF_VERSION`, extend the mock
bridge (payload shapes, cross-field rules, a `--script` syntax that can also
send `gsselect`, and the GET of A31 returning it).

### 6.5 Config, logs, tools

- **Config**: optional `gs` block, defaults in code (`GS_DEFAULTS`, the A33
  exception). `gs.enabled` defaults to false and ships false.
- **Logs**: a separate schema and file for the glide slope (`gs.solution_csv`,
  default `gs.csv` beside the others), for A33's reason: `SolutionLogger`
  never rewrites an existing header. `--csv` gains a `<name>-gs.csv` twin.
  `docs/cloud-schema.md` is generated; regenerate it.
- **IQ recording** stays on the NAV receiver only. Say so in the docs; a
  second ring is 61 MB more (D6) and O18 is still open.
- **Generator**: glide-slope signals come from `tools/loc_gen.py` (same
  modulation). Add what is missing as optional parameters with defaults that
  leave today's output unchanged (fingerprint), and a thin `gs` preset or
  `tools/gs_gen.py` exposing the same shape (`GsParams`, `generate_blocks`)
  for roastbeef's stand-in. Sidecars carry the real RF frequency and
  `carrier_at`.
- **`vor_capture.py`**: accepts a glide-path frequency (329.15–335.00, on the
  pairing table) and `--device` by serial; `vor_replay.py` selects GS from the
  sidecar frequency; `--survey` works at 330 MHz and reports the offset in ppm
  (three times as sensitive as at 110: useful for measuring a dongle's ppm).
- **Sweep**: `tools/gs_sweep.py` or a mode of `loc_sweep.py`, producing
  `docs/gs-sweep.md`.
- **Self-test**: a GS stage, and an allocation line.

## 7. Phase A milestones (this repo)

Each ends with the full suite green (`python -m pytest`, deps from
`requirements.txt` in a venv under `~/.cache`, not `/tmp`; A32) and
`ruff check .` clean. Nothing is committed or deployed by the implementing
session: Stefano reviews the working tree.

**G0. Baseline.** `git status` and `git log` first (D8). The tree holds the
uncommitted localizer work and Stefano's addon-layout changes: build on them,
do not revert or reformat. Run the suite and note the count and any failure
(see the `loc.enabled` row of section 2). Take fingerprints before any edit,
A33's method: a SHA-256 over every field of every observation for (a) the
real capture `captures/capture_20260413T195020Z_109400.cu8` under the VOR
settings A33 used, (b) a synthetic VOR run, (c) a synthetic localizer run
through `LocDemod` and through the controller's published payloads. They
must be identical at the end of G7.

**G1. Pairing and settings.** Verify the table (4.4). `freqrules` additions.
`sdr.devices` in schema and config, `allocate()` with tests that enumerate
the cases as a table written in the test (0, 1, 2, 3 dongles; `use`
restrictions; absent, duplicate and unlisted serials; index entries), and
`--devices`. An old config loads and allocates as before.

**G2. Generator.** GS signals; an independent reference decoder in the tests
that imports neither `rbvor.loc` nor `rbvor.gs`; the case that must fail
(tones swapped negate the DDM; a localizer-depth signal read as a glide path
reads SDM 40, not 80).

**G3. Demodulator.** `LocDemod` under GS parameters against the generator:
DDM across −0.25…+0.25, SDM at 0.75 / 0.80 / 0.85 with tone phase swept
(4.3), the ε sweep to ±2.5 % (A34 must still hold at 40 % depth), a sample
gap, bounded state over a long run, CPU.

**G4. Carrier acquisition.** The measurement of 4.2 first, written into
`docs/gs-sweep.md` before any fix. Then the chosen fix, the same table
again, the two-carrier case, and the test that pins the behaviour at the
legal limit (and at limit plus a 20 ppm dongle).

**G5. Sweep, quality, validity.** DDM error against SNR and true DDM for the
candidate forms (4.3); thresholds and weights from that curve (A37's
method, and its honesty about what is judgement); the noise-only tests of
4.1; integration time against lag (a glide-slope marker close in moves
faster than a localizer bar: choose `gs.integration_s` / `hop_s` from the
measured trade, not by copying the localizer's).

**G6. Controller, allocation, publisher.** The GS chain in both feeds; the
switch (4.5) with its tests; per-receiver NO_SDR and retry; release on idle;
payload, hysteresis, publish rate; clean-stop publish; config; mock bridge;
solution log. End to end through the real `main()` against a live socket
with the mock bridge re-asserting state at 2 Hz (A16 was found only that
way), in these arrangements, each with fake devices injected the way the
existing tests do it:

| arrangement | must show |
|---|---|
| two receivers, ILS picked | both axes reach status ≥ 4, `gsshared` 0, `gsselect` ignored |
| one receiver, ILS picked | LOC tracks, `gsshared` 1, `gsstatus` 0; `gsselect` 1 → GS tracks, LOC dark (4.5); back again |
| one receiver, `gsselect` 1 held on the bridge, new ILS picked | starts on LOC |
| two receivers, GS dongle refuses to open | LOC tracks, `gsstatus` 1, retried; opens later → GS tracks, no restart |
| second dongle plugged in after start | next tune attempt uses it; `gsshared` goes 1 → 0 |
| VOR picked after an ILS | both `loc*` and `gs*` zero in the retune POST, GS dongle released |
| `vorfreq` 0 | both dongles released |
| noise on both | neither status reaches 4 |
| `gs.enabled` false | payload and behaviour byte-for-byte v2.3 |

Then, if Chromium and Go are present, run
`../roastbeef/test/integration/test_vor_radio.py` unmodified. A33 recorded 59
of 60 passing with one failure that predates this work; compare against
that, and if it cannot run here, say so rather than claim it.

**G7. Docs.** IF v2.4, README, RUN.md (including how to give dongles serials
with `rtl_eeprom` and how to read `--devices`), `docs/install.md`,
amendments in `CLAUDE.md`, CHANGELOG, `docs/open-items.md`. Re-check the G0
fingerprints.

## 8. Phase B milestones (`../roastbeef`, after phase A)

Read that repo's `AGENTS.md` and `CLAUDE.md` first. Its tree has uncommitted
work (the localizer plate, radio, alerts): build on the plate changes, do not
disturb the rest.

**H1. Plate.** `web/RB-01/addons/js/vor.js`, `vor.html`, `vor.css`:

- Read the `gs*` keys (absent = 0).
- ILS mode is `locfreqactive > 0 || gsfreqactive > 0` (today it is
  `locfreqactive > 0` alone, which would drop to VOR mode in shared GS mode).
- Pass the glide slope to the HSI's `glideSlope` argument only while
  `gsstatus >= 4`; otherwise the markers are parked where they cannot be read
  as "on path", and a label says why (the status, or "LOC ONLY" when
  `gsshared` is 1 and LOC is selected). Positive `gsdev` is fly up; the
  renderer's positive is up. Check the direction on a known input, and choose
  the scale so full scale lands on a dot that exists (section 2).
- While `gsshared` is 1, show a LOC / GS switch that posts `gsselect`. It
  shows which one is being received from `locfreqactive` / `gsfreqactive`,
  not from the last press (the lesson of `radio-active-status.md`).
- Every station pick posts `gsselect: 0` in the same POST as `vorfreq`.
- With one receiver the course pointer in GS mode still comes from the
  station's front course; the lateral bar is hidden, not centred (Q8's rule).

**H2. Text.** Wherever GS appears: supplemental, not approach guidance;
false paths exist; a localizer may have no glide path.

**H3. Dongle ownership.** `main/sdr.go` `configDevices()` gives untagged
dongles to any Stratux band that lacks one. Decide with Stefano (Q11) how
the NAV, GS and radio dongles are kept out of that: the proposal is a
reserved serial prefix that pass 2 skips, written by the same `rtl_eeprom`
step that names them for `sdr.devices`. Also note the cap of 3 in
`sdrWatcher` when more dongles are fitted. Any change there touches traffic
reception: it needs that repo's own tests and review.

**H4. Tests.** `test/integration/dongle.py` gains a second stand-in device
so two receivers can be on the air; `test_vor_radio.py` gains rounds for:
two receivers (bar and markers move, labels), one receiver (switch shown,
pressing it darkens the other axis), noise only (both parked), GS dongle
taken by the radio (LOC continues, GS says no SDR). `test/display/test_vor.py`
covers the new rows and the switch on all five panels, clear of the FAB
zones.

Whether the RB-01 settings page should edit `sdr.devices` is Q12; phase B
does not build it unless answered.

## 9. G8: a real signal, gated

Blocked on the receiver hearing a navaid in the aircraft at all (F6, O21,
O22) and on the localizer's own L7 (O23). In order of cost:

1. **Does the installation pass 330 MHz?** A 10 s capture on a glide-path
   channel with each candidate dongle and antenna, `vor_replay.py --survey`:
   carrier above the floor or not. Unknowns to write down as found: the VOR
   antenna's response at three times its design frequency (certified
   installations commonly feed the glide-slope receiver from the VOR/LOC
   antenna through a diplexer, which suggests it can work; this is a
   hypothesis here, not a measurement), the FM band-stop filter's passband
   (A27), and any airband filter or LNA (O8), which would block it.
2. **Ground, near an ILS runway, bench R820T.** On the ground one is far
   below the path, so the expected reading is strongly fly-up (150 Hz
   predominant) or nothing at all outside the azimuth coverage. Fly-up is a
   real check of the sense; no carrier is not evidence of anything.
   Frequency from the chart (O20's lesson), GPS time and position in the
   sidecar (A24).
3. **In flight.** The `bridgefloat` log plus GPS position and altitude gives
   the elevation angle from the glide-path antenna, to compare with the
   published angle: sense and scale.

Until one of these shows a sensible deviation, `gs.enabled` stays false in
the shipped config and a test pins it.

## 10. Recording the work

- Next free numbers when this was written: amendment **A39**, decision
  **D10**, open item **O31**, question **Q9**. Check before using them.
- Hypotheses are labelled as hypotheses. A figure in `CLAUDE.md` or the docs
  was measured, and the entry says on what.
- Expected amendments: the list form relaxing A27; the carrier acquisition
  of 4.2 with its table; the GS form of D9; GS validity thresholds; the
  switch and its darkening rule; publish rate with two chains.
- Expected open items: real-station confirmation of GS sense and scale; the
  pairing table and §3.1.5 figures if unverified, and 0.175 as full scale;
  whether the aircraft installation passes 330 MHz; CPU and USB throughput on
  the Pi with two chains; a dongle pulled during reception, per receiver;
  false glide paths (stated, not solved); the killed daemon's stale
  `gsstatus` (extends O25); Stratux taking an untagged dongle.

## 11. Questions for Stefano

Work proceeds on the interim default; none of these blocks phase A.

| Q | question | interim default |
|---|---|---|
| Q9 | `config/rb-vor.json` has `loc.enabled: true` in the working tree against the test that pins false. Deliberate? | left as found, reported in G0 |
| Q10 | With two dongles listed and the GS one busy (radio), should the glide slope fall back to sharing the NAV dongle? | no: GS shows NO SDR, LOC continues |
| Q11 | How are NAV / GS / radio dongles kept away from Stratux's automatic assignment: a reserved serial prefix skipped by `configDevices()`? Which prefix? | plain serials in `sdr.devices`; H3 waits |
| Q12 | "Settings" is read here as `rb-vor.json` (`sdr.devices`). Should the RB-01 settings page edit it too? | file only |
| Q13 | "The pilot switches which feature" is read as LOC ↔ GS on the plate. Should the same switch also arbitrate VOR/ILS against the SDR radio, replacing A30's first-come rule? | LOC ↔ GS only; A30 unchanged |
| Q14 | Field names and senses of section 6.4 (`gsdev` positive = fly up, `gsselect`, `gsshared`) as RB-01 should consume them? | as written |
| Q15 | Which dongle and antenna will serve the glide slope in the aircraft? A third dongle on the VOR antenna through a splitter, or its own antenna? | none assumed; section 9 step 1 decides |
