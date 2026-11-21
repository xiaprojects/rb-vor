# RB-VOR-IF v2.4 — published interface specification

**Status:** draft for review (localizer and glide slope ship disabled) · **Version:** 2.4 ·
**Date:** 2026-11-13
**Producer:** RB-VOR software VOR / ILS receiver (localizer and glide slope) ·
**Platform:** RB-Avionics

This document is self-contained. A third party implementing a consumer needs
nothing else. It supersedes `RB-VOR-IF-v2.3.md`; what changed is listed in §6.

---

## 1. Overview

RB-VOR decodes a VHF Omnidirectional Range station, or (since v2.3, when
enabled) an ILS localizer, and (since v2.4, when enabled) the glide slope
paired with that localizer, from one or more RTL-SDRs, and publishes the
result into the RB-Avionics bridge.

| Direction | Transport | Endpoint | Role |
|---|---|---|---|
| consumed | WebSocket | `ws://localhost/bridge` | RB-VOR is a read-only client |
| consumed | HTTP GET | `http://localhost/bridgeUpdate` | the bridge state, read once per connection |
| produced | HTTP POST | `http://localhost/bridgeUpdate` | one JSON object per update |

Hosts and paths are configurable; the shapes below are not. On RB-01 they are
`/bridge/float/ws` and `/bridge/float`.

> **Safety.** RB-VOR is supplemental / non-required equipment. Day VFR
> situational awareness only. Not a certified NAV receiver, not for IFR
> navigation, and **neither the localizer nor the glide-slope output is
> approach guidance**. A vertical needle invites more trust than a lateral
> one; it has not earned it. Independent primary navigation is assumed present.

---

## 2. Consumed — WebSocket and state read

RB-VOR connects as a client and sends no frames at all. RB-01 has no ping/pong,
so the link is considered lost only when the socket is: closed by the peer, or
declared dead by TCP keepalive on RB-VOR's side. Each inbound frame is a UTF-8
JSON object.

| Field | Type | Unit | Range | Use |
|---|---|---|---|---|
| `vorfreq` | number \| null | MHz | 108.000–117.950 | tuning command, VOR **or localizer** channel |
| `gpsTrack` | number | ° true | 0–359 | diagnostics and logging only |
| `gsselect` | number | — | 0 or 1 | **v2.4**: on a shared receiver, which half of the ILS to receive: 0 localizer, 1 glide slope (§2.5) |

Every other field is ignored, including RB-VOR's own outputs echoed back.

There is **one** tuning key. A localizer is selected by posting its frequency
to `vorfreq`; the channel decides the mode (§2.2), and the glide slope is the
channel paired with it (§2.6). There is no glide-slope frequency command.

### 2.1 `vorfreq` — the cases

| Case | Meaning |
|---|---|
| **absent** | no change; keep the current channel |
| **`null`**, or **`0`** | no channel selected; release the SDR and go idle (`0` is how RB-01's OFF is sent: the float bridge has no null) |
| **present, numeric** | tune this channel, or reject it (§2.2) |
| **present, non-numeric** | frame is malformed; dropped, channel unchanged |

### 2.2 Channel validation and mode

| # | Rule |
|---|---|
| 1 | 108.000 ≤ f ≤ 117.950 MHz |
| 2 | f lands on the 50 kHz grid (±1 kHz tolerance) |
| 3 | 108.000–111.950: an **even** 100 kHz digit is a VOR channel; an **odd** digit is an ILS localizer channel (40 channels, 108.100–111.950) |
| 4 | 112.000–117.950: every 50 kHz channel is a VOR channel |
| 5 | an operator allowlist forces a channel to VOR; a denylist rejects it |

| Channel | Receiver without `loc.enabled` (the shipped default) | with `loc.enabled` |
|---|---|---|
| VOR | tuned, VOR mode | tuned, VOR mode |
| localizer | rejected, as in v2.2 | tuned, **LOC mode** |
| anything else | rejected | rejected |

Examples: `109.40` VOR · `109.50` localizer · `108.05` VOR · `108.15`
localizer · `113.35` VOR · `117.975` off-grid · `118.00` out of band.

A rejection produces one idle payload (`vorstatus` 0, every measurement 0)
and no retune.

### 2.3 Repetition

A bridge may re-assert its state at any rate. Repeating the **same** `vorfreq`
is a no-op at any point, including mid-retune. Producers need not deduplicate.

### 2.4 Reconnection and the state read

RB-VOR reconnects with exponential backoff (0.5 s → 30 s). After the configured
grace period without a connection it releases the SDR and goes idle. After
every connection it reads the state once with `GET` on the REST endpoint and
treats the answer as a frame, so a restarted daemon returns to the `vorfreq`
(and `gsselect`) the bridge already holds. If the read fails, the connection
is made again.

### 2.5 `gsselect` — the pilot's switch (v2.4)

A dongle hears about 1 MHz; a localizer (~110 MHz) and its glide slope
(~330 MHz) are 220 MHz apart. With **one** dongle only one of them can be
received, and the pilot chooses. With **two** (the receiver's own
configuration says so, §3.13) both are received and `gsselect` is ignored.
The receiver says which case applies with `gsshared` (§3.13), so a consumer
needs no knowledge of the hardware.

| Case | Meaning |
|---|---|
| absent | no change |
| `0` | receive the localizer (the default) |
| `1` | receive the glide slope |
| anything else | ignored |

- Acted on only while a localizer channel is selected **and** `gsshared` is 1.
  Read and ignored otherwise.
- **Edge-triggered.** Only a value different from the last one received
  counts, so a bridge re-asserting its whole state cannot undo the next rule.
  The first value after a (re)connection counts, so a restarted daemon
  returns to the pilot's choice.
- **A new `vorfreq` always starts on the localizer** (lateral first),
  whatever the bridge still holds for `gsselect`. A consumer that offers the
  switch should therefore post `gsselect: 0` together with every station
  pick, so that a later press of GS is a change the bridge rebroadcasts.
- Debounced like a frequency. A switch costs a retune and an acquisition:
  about 1.5–2 s with neither needle usable.

### 2.6 Glide-path pairing (v2.4)

Every localizer channel has exactly one paired glide-path channel
(Annex 10 Vol I 3.1.6.1), 40 channels from 329.150 to 335.000 MHz on a
150 kHz grid. The receiver tunes the paired channel; it cannot know whether a
glide path is actually installed there (a localizer-only approach). In that
case the glide slope reports no signal, never a deviation.

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

The table was checked against two published copies, not against Annex 10
itself (receiver open item O31).

---

## 3. Produced — HTTP POST

```
POST /bridgeUpdate
Content-Type: application/json

{ "vorradial": 130, "vorsnr": 42, "voram": 28, "vorstatus": 5,
  "vorsub": 29, "vorfreqactive": 109400 }
```

With `loc.enabled`, every payload also carries the six localizer fields:

```
{ "vorradial": 0, "vorsnr": 0, "voram": 0, "vorstatus": 0, "vorsub": 0,
  "vorfreqactive": 0,
  "locstatus": 5, "locdev": -23, "locddm": -36, "locsdm": 39, "locsnr": 37,
  "locfreqactive": 109500 }
```

With `gs.enabled` as well (two dongles, both halves received):

```
{ "vorradial": 0, "vorsnr": 0, "voram": 0, "vorstatus": 0, "vorsub": 0,
  "vorfreqactive": 0,
  "locstatus": 5, "locdev": -23, "locddm": -36, "locsdm": 39, "locsnr": 37,
  "locfreqactive": 109500,
  "gsstatus": 5, "gsdev": 50, "gsddm": 88, "gssdm": 79, "gssnr": 41,
  "gsfreqactive": 332600, "gsshared": 0 }
```

Encoding: UTF-8 JSON. Every field is a JSON integer, never a string, float or
null.

| Field | Unit | Range | Semantics |
|---|---|---|---|
| `vorradial` | ° magnetic | 0–359 | radial **FROM** the station |
| `vorsnr` | dB | 0–120 | 30 Hz tone signal-to-noise ratio |
| `voram` | — | 0–100 | 30 Hz AM depth ×100 (0.28 → 28) |
| `vorstatus` | — | 0–5 | VOR receiver status code (§3.5) |
| `vorsub` | — | 0–100 | 9960 Hz subcarrier level ×100 |
| `vorfreqactive` | kHz | 0 or 108000–118000 | tuned VOR channel; 0 = none |
| `locstatus` | — | 0–5 | localizer status code, same codes (§3.8) |
| `locdev` | % of full scale | −100…+100 | lateral deviation; **0 unless `locstatus >= 4`** (§3.9) |
| `locddm` | DDM ×1000 | signed | measured difference in depth of modulation (§3.10) |
| `locsdm` | — | 0–100 | sum of the two tone depths ×100, nominal 40 |
| `locsnr` | dB | 0–120 | localizer tone SNR |
| `locfreqactive` | kHz | 0 or a localizer channel | tuned localizer channel; 0 = none |
| `gsstatus` | — | 0–5 | glide-slope status code, same codes (§3.13) |
| `gsdev` | % of full scale | −100…+100 | vertical deviation, **+ = fly up**; **0 unless `gsstatus >= 4`** (§3.14) |
| `gsddm` | DDM ×1000 | signed | glide-slope DDM, like `locddm` (§3.15) |
| `gssdm` | — | 0–100 | sum of the two tone depths ×100, nominal 80 |
| `gssnr` | dB | 0–120 | glide-slope tone SNR |
| `gsfreqactive` | kHz | 0 or a glide-path channel | glide-path channel a receiver is tuned to; 0 = none |
| `gsshared` | — | 0 or 1 | 1 when the selected ILS's localizer and glide slope share one receiver (§3.13) |

**The payload contains exactly the six `vor*` keys, or those plus the six
`loc*` keys, or those plus the seven `gs*` keys** (6, 12 or 19). The `loc*`
keys are present on every payload of a receiver with `loc.enabled` (zeros on
a VOR channel and when idle), the `gs*` keys on every payload of one with
`gs.enabled` (which requires `loc.enabled`). A receiver without `gs.enabled`
is byte-for-byte v2.3; without `loc.enabled`, v2.2. A consumer treats absent
`loc*` and `gs*` keys as 0.

All fields of one update travel in **one POST**, also with two receivers, so
a change of mode is atomic: no payload ever has `vorfreqactive` together with
`locfreqactive` or `gsfreqactive`, and with `gsshared` 1 never
`locfreqactive` and `gsfreqactive` together.

### 3.1 `vorradial`

Radial **from** the station, degrees magnetic, 0 = magnetic north. A consumer
needing bearing-to computes `(vorradial + 180) % 360`. RB-VOR can be configured
to publish bearing-to directly (`publish.bearing_ref: "to"`); the default is
**from**.

### 3.2 `vorsnr`, `voram`, `vorsub`

Measurements, not a quality score: the 30 Hz tone SNR in whole dB, the 30 Hz
AM depth ×100 (healthy ≈ 30), the 9960 Hz subcarrier level ×100 (healthy ≈ 30).
A consumer may set its own thresholds on them; `vorstatus` is the receiver's.

### 3.3 Which fields are gated

**No `vor*` field is gated by validity.** Each carries the last measurement
the receiver made, including the radial while acquiring and in NO SIGNAL;
`vorstatus` is the single field that says whether the radial may be used. A
measurement reads 0 only when nothing has been measured since the last retune
(idle, rejected, tuning), so one station's numbers are never shown against
another's channel. One exception: an uncalibrated receiver publishes
`vorradial` 0, because the number would be off by an unknown constant.

> v2.2's §3.3 and §4.1 described the older behaviour, in which the radial,
> SNR and AM depth were forced to 0 without a valid bearing. The code has not
> done that since RB-VOR 1.0.0 (CLAUDE.md A26). v2.3 corrects the text; the
> wire behaviour is unchanged from what 1.0.0 already sent.

### 3.4 Update rate — transmit on change

RB-VOR sends a POST **only when at least one field changes** from the last
value sent. State transitions (retune, flag raised or cleared, mode change)
change the payload and are therefore sent immediately. An optional keep-alive
(`publish.keepalive_s`, default 5 s) re-sends an unchanged payload; 0 disables
it. Because every POST is also a row in RB-01's flight log, the localizer
measurement fields carry a small hysteresis (§3.11).

### 3.5 Status codes (`vorstatus`, and `locstatus`)

| Code | Meaning | RB-VOR state(s) |
|---|---|---|
| 0 | off / idle / no channel / rejected, or the other mode is tuned | IDLE, INVALID_FREQ |
| 1 | no signal, or the dongle cannot be had (held by another program) | NO_SIGNAL, NO_SDR |
| 2 | tuning | TUNING |
| 3 | acquiring: measuring, not yet judged | ACQUIRING |
| 4 | degraded: usable but poor | DEGRADED |
| 5 | tracking: good | TRACKING |

`>= 4` is usable. A consumer drives a status light from the code directly.

### 3.6 `vorfreqactive`

The VOR channel the receiver is tuned to, in kHz (109400 = 109.400 MHz); 0
when none, when the frequency was rejected, and while a localizer is tuned.

### 3.7 Units

Inbound `vorfreq` is in **MHz** (a float); outbound `vorfreqactive` and
`locfreqactive` are integers in **kHz**. Divide by 1000 for MHz.

### 3.8 On a localizer channel

While a localizer is tuned the `vor*` fields read exactly as v2.2 reads a
rejected frequency: `vorstatus` 0, `vorfreqactive` 0, every other `vor*` field
0. A consumer written for v2.2 therefore shows OFF and a needle at 0 — what it
already shows for a rejected frequency — and **never a radial**. `locstatus`
uses the codes of §3.5; it is 0 on a VOR channel, when idle, and after a clean
stop (§3.12).

### 3.9 `locdev` — lateral deviation

The deviation as a percentage of full scale, so a display can scale one number
to its bar without knowing any ICAO constant:

- `locdev = round(100 × DDM / 0.155)`, clamped to −100…+100. 0.155 is the DDM
  at the edge of the course sector (Annex 10 Vol I 3.1.1); using it as
  full-scale deflection is airborne-equipment convention.
- **Sign: positive when the 150 Hz tone predominates**, which is the aircraft
  **right of the front course** as seen on the approach (Annex 10 Vol I
  3.1.3.1.2), so the pilot must **fly left**. A display that draws the course
  bar as "where the course is" draws it to the **left** of centre for a
  positive `locdev`.
- On a **back course** the sense is reversed and the receiver cannot know.
- **`locdev` is 0 unless `locstatus >= 4`.** It is the one field in this
  interface deliberately gated by validity, because unlike a radial its zero
  means something: a centred needle says "on the centreline", and noise
  decodes as exactly that. Every other `loc*` field is carried like the `vor*`
  measurements (§3.3).

> **A consumer must not draw the deviation bar unless `locstatus >= 4`.** A
> zero `locdev` with any other status is the absence of a measurement, not a
> centred needle.

### 3.10 `locddm`, `locsdm`, `locsnr`

- `locddm`: the DDM the receiver publishes ×1000, signed like `locdev`, **not
  clamped** (DDM can exceed 0.155 outside the course sector) and not gated.
  It is the DDM after the receiver's correction for envelope compression at
  low signal levels (CLAUDE.md D9); the per-window raw value is in the
  receiver's own log.
- `locsdm`: the sum of the 90 Hz and 150 Hz depths ×100; 40 nominal (20 % each
  on the course line, Annex 10 Vol I 3.1.3.5.1). It falls at low signal
  levels; a value far from 40 on a strong signal means a non-nominal station.
- `locsnr`: the mean tone amplitude against off-tone bins of the envelope, in
  whole dB, after a 1 s integration. It is not a channel SNR: noise alone reads
  up to ~10 dB, and the receiver flags below 26 dB.

### 3.11 Resolution and hysteresis of the localizer fields

`locdev`, `locddm`, `locsdm` and `locsnr` change only when the measurement has
moved more than a deadband from the value last sent: 2, 4, 2 and 4 wire units
respectively. A shown `locdev` is therefore never more than 2 % of full scale
from the measurement. The deadbands reset on every status or channel change.
Measured: a steady localizer at 20 dB channel SNR produces 27 POSTs a minute
against 119 without them (`docs/loc-sweep.md`).

### 3.12 Stopping

On a clean stop a receiver with `loc.enabled` posts an idle payload
(`locstatus` 0, `vorstatus` 0, and with `gs.enabled` `gsstatus` 0). **A
daemon that is killed, or loses power, leaves its last values on the
bridge**, including a usable `locstatus` or `gsstatus` and whatever
`locdev` / `gsdev` it last sent. RB-01 has no staleness timeout today; see
§4 item 2.

### 3.13 `gsstatus`, `gsfreqactive`, `gsshared` (v2.4)

`gsstatus` uses the codes of §3.5 for the glide-slope receiver, which has its
own state: the localizer and the glide slope each fail alone. A tracking
localizer says nothing about the glide slope (another antenna path, perhaps
another dongle, another band), and the receiver never borrows one's validity
for the other.

| Arrangement | `gsshared` | glide slope received |
|---|---|---|
| VOR channel, idle, rejected | 0 | no; all `gs*` 0 |
| localizer, glide slope has its own dongle | 0 | **yes**, together with the localizer |
| localizer, one dongle, `gsselect` 0 | 1 | no: `gsstatus` 0, `gsfreqactive` 0 |
| localizer, one dongle, `gsselect` 1 | 1 | **yes**; the localizer is not: `locstatus` 0, `locfreqactive` 0, `locdev` 0 |

- `*freqactive` always tells the truth about a tuner. In the last row the
  consumer knows the ILS is selected from the `vorfreq` it sent and from
  `gsfreqactive`.
- A glide-slope dongle that cannot be had (held by another program, or
  unplugged) is `gsstatus` 1 with `gsfreqactive` set, retried; the localizer
  carries on. Which dongle serves what is the receiver's configuration
  (`sdr.devices`), re-read at every tune attempt, so a dongle plugged in later
  is used at the next retune or switch (and `gsshared` goes from 1 to 0).
- **On a switch the needle not received goes dark in the same POST** that
  announces the other tuning (status 2). No payload after the switch carries
  the old feature's usable status or deviation.

### 3.14 `gsdev` — vertical deviation

- `gsdev = round(100 × DDM / 0.175)`, clamped to −100…+100. 0.175 is twice
  the DDM at the edge of the half path sector (0.0875, Annex 10 Vol I
  3.1.5.6.5); using it as full-scale deflection is airborne-equipment
  convention, not stated in Annex 10. At a 3° path, full scale is roughly
  ±0.7°.
- **Sign: positive when the 150 Hz tone predominates**, which is the aircraft
  **below the glide path** (Annex 10 Vol I 3.1.5.1.4), so the pilot must
  **fly up**. This is the same rule as `locdev` (positive = 150 Hz) and a
  different direction on the display: `locdev` + is fly left, `gsdev` + is
  fly up. A display that draws the glide-slope marker as "where the path is"
  draws it **above** centre for a positive `gsdev`.
- **`gsdev` is 0 unless `gsstatus >= 4`**, for the reason `locdev` is: noise
  decodes as DDM ≈ 0, a centred marker.
- **False glide paths.** A glide path radiates false paths above the true one
  (at multiples of the path angle, some with reversed sense), and below about
  0.45 of the path angle the signal is not specified. Such a reading is
  steady, strong and **wrong**, and the receiver cannot tell. Not every
  localizer has a glide path at all (§2.6).

> **A consumer must not draw the glide-slope marker unless `gsstatus >= 4`.**
> A zero `gsdev` with any other status is the absence of a measurement, not a
> marker on the path.

### 3.15 `gsddm`, `gssdm`, `gssnr`

As `locddm`, `locsdm`, `locsnr` (§3.10), for the glide path: `gsddm` signed
like `gsdev`, not clamped, not gated, after the receiver's correction for
envelope compression (DDM × max(1, 0.80 / SDM)); `gssdm` nominal 80 (40 %
per tone, Annex 10 Vol I 3.1.5.5.1); `gssnr` after a 1 s integration, noise
alone up to ~10 dB, flagged below 26 dB. Deadbands as §3.11, in the glide
slope's own units (`gsdev` 2, `gsddm` 4, `gssdm` 2, `gssnr` 4).

---

## 4. Consumer requirements

Normative. A consumer that does not do these things is not conforming.

1. **Use `vorradial` only when `vorstatus >= 4`, `locdev` only when
   `locstatus >= 4`, and `gsdev` only when `gsstatus >= 4`.** Do not display, smooth through or couple an autopilot to
   either otherwise. The measurement fields remain meaningful as receiver
   status.
2. **Apply an independent staleness timeout**, 3 s recommended, or the
   keep-alive interval when `publish.keepalive_s` is set. RB-VOR's own state
   does not protect against a dead process (§3.12). This matters most for the
   localizer and the glide slope, where the stale value is a centred needle.
3. **Do not assume monotonic time between updates.** A steady signal produces
   no traffic, and transitions produce out-of-band POSTs.
4. **Handle the 359 → 0 wrap** of `vorradial` with circular arithmetic.
5. **Ignore unknown fields.**
6. **Treat absent `loc*` and `gs*` keys as 0** (a receiver without
   `loc.enabled` or `gs.enabled`).
7. **Decide the ILS mode from `locfreqactive > 0 || gsfreqactive > 0`**, not
   from `locfreqactive` alone, which is 0 on a shared receiver with the glide
   slope selected.
8. **Offer the LOC / GS switch only while `gsshared` is 1**, show which half is
   received from `locfreqactive` / `gsfreqactive` (not from the last press),
   and post `gsselect: 0` with every station pick (§2.5).

---

## 5. Behaviour reference

| RB-VOR state | VOR channel publishes | localizer channel publishes |
|---|---|---|
| IDLE | all zero | all zero |
| INVALID_FREQ | all zero, once | all zero, once |
| TUNING | `vorstatus` 2, measurements 0, **before the tuner moves** | `locstatus` 2, measurements 0, before the tuner moves |
| NO_SDR | `vorstatus` 1, `vorfreqactive` set | `locstatus` 1, `locfreqactive` set |
| ACQUIRING | `vorstatus` 3, measurements as measured | `locstatus` 3, `locdev` 0, measurements as measured |
| TRACKING / DEGRADED | `vorstatus` 5 / 4, usable radial | `locstatus` 5 / 4, usable `locdev` |
| NO_SIGNAL | `vorstatus` 1, last radial and measurements | `locstatus` 1, `locdev` 0, measurements as measured |

The glide slope publishes by the localizer column, in `gs*`, from its own
receiver state, on a localizer channel only (§3.13).

The ordering guarantee that matters: **the invalidation is published before
the tuner moves**, so no value from a previous station, mode or switch can be
taken for the new one.

---

## 6. Versioning

- **v2.4 (this document)**: adds `gsstatus`, `gsdev`, `gsddm`, `gssdm`,
  `gssnr`, `gsfreqactive`, `gsshared`, present only when the receiver has
  `gs.enabled`, and the consumed key `gsselect`. Additive: a receiver without
  `gs.enabled` is byte-for-byte v2.3. A v2.3 consumer of a receiver with it
  sees `locstatus` 0 while a shared receiver has the glide slope selected —
  nothing it would misread as a localizer.
- v2.3: adds `locstatus`, `locdev`, `locddm`, `locsdm`,
  `locsnr`, `locfreqactive`, present only when the receiver has
  `loc.enabled`; documents that `vorfreq` 0 (RB-01's OFF) idles the receiver,
  which it already did as an out-of-band frequency; corrects the v2.2 text of §3.3
  and §4.1 to the A26 behaviour the wire already had. Additive: a v2.2
  consumer is unaffected by a receiver without `loc.enabled`, and sees OFF on
  a localizer channel of one with it.
- v2.2: `vorfreqactive`; `vorstatus` 4 split into 4 = DEGRADED, 5 = TRACKING.
  v2.1: `vorstatus`, `vorsub`. v2.0: `vordir` → `vorradial`, `vorsignal` →
  `vorsnr` + `voram`.
- Any change to the meaning, unit or range of an existing field is a breaking
  change, requiring v3.0 and a migration note.
- Reserved for future use: `vorident`, `vorto`, `vorcdi`, `locident`, marker
  beacons (`mkr*`).

## 7. Conformance testing

`tools/mock_bridge.py` validates every payload against §3, v2.2, v2.3 and
v2.4 shapes alike, including the cross-field rules (`locdev` / `gsdev` 0
below status 4, one mode at a time, `vor*` zero on an ILS, never both halves
on a shared receiver).

```
tools/mock_bridge.py --port 8099 --script "109.4@0,109.5@10,gs=1@20,gs=0@30,0@40"
```

The script above exercises a VOR, a localizer (rejected unless the receiver
has `loc.enabled`), the pilot's switch to the glide slope and back (`gs=`
items post `gsselect`), and OFF as RB-01 sends it.
