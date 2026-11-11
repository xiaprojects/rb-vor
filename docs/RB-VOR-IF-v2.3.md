# RB-VOR-IF v2.3 — published interface specification

**Status:** draft for review (localizer support ships disabled) · **Version:** 2.3 ·
**Date:** 2026-11-10
**Producer:** RB-VOR software VOR / ILS-localizer receiver · **Platform:** RB-Avionics

This document is self-contained. A third party implementing a consumer needs
nothing else. It supersedes `RB-VOR-IF-v2.2.md`; what changed is listed in §6.

---

## 1. Overview

RB-VOR decodes a VHF Omnidirectional Range station, or (since v2.3, when
enabled) an ILS localizer, from an RTL-SDR and publishes the result into the
RB-Avionics bridge.

| Direction | Transport | Endpoint | Role |
|---|---|---|---|
| consumed | WebSocket | `ws://localhost/bridge` | RB-VOR is a read-only client |
| consumed | HTTP GET | `http://localhost/bridgeUpdate` | the bridge state, read once per connection |
| produced | HTTP POST | `http://localhost/bridgeUpdate` | one JSON object per update |

Hosts and paths are configurable; the shapes below are not. On RB-01 they are
`/bridge/float/ws` and `/bridge/float`.

> **Safety.** RB-VOR is supplemental / non-required equipment. Day VFR
> situational awareness only. Not a certified NAV receiver, not for IFR
> navigation, and **the localizer output is not approach guidance**.
> Independent primary navigation is assumed present.

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

Every other field is ignored, including RB-VOR's own outputs echoed back.

There is **one** command key. A localizer is selected by posting its frequency
to `vorfreq`; the channel decides the mode (§2.2).

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
the bridge already holds. If the read fails, the connection is made again.

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

**The payload contains exactly the six `vor*` keys, or exactly all twelve.**
The `loc*` keys are present on every payload of a receiver with `loc.enabled`
(zeros on a VOR channel and when idle) and on none of a receiver without it,
whose wire is byte-for-byte v2.2. A consumer treats absent `loc*` keys as 0.

The `vor*` and `loc*` fields of one update travel in **one POST**, so a change
of mode is atomic: no payload ever has both `vorfreqactive` and
`locfreqactive` non-zero.

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
(`locstatus` 0, `vorstatus` 0). **A daemon that is killed, or loses power,
leaves its last values on the bridge**, including a usable `locstatus` and
whatever `locdev` it last sent. RB-01 has no staleness timeout today; see
§4 item 2.

---

## 4. Consumer requirements

Normative. A consumer that does not do these things is not conforming.

1. **Use `vorradial` only when `vorstatus >= 4`, and `locdev` only when
   `locstatus >= 4`.** Do not display, smooth through or couple an autopilot to
   either otherwise. The measurement fields remain meaningful as receiver
   status.
2. **Apply an independent staleness timeout**, 3 s recommended, or the
   keep-alive interval when `publish.keepalive_s` is set. RB-VOR's own state
   does not protect against a dead process (§3.12). This matters most for the
   localizer, where the stale value is a centred needle.
3. **Do not assume monotonic time between updates.** A steady signal produces
   no traffic, and transitions produce out-of-band POSTs.
4. **Handle the 359 → 0 wrap** of `vorradial` with circular arithmetic.
5. **Ignore unknown fields.**
6. **Treat absent `loc*` keys as 0** (a receiver without `loc.enabled`).

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

The ordering guarantee that matters: **the invalidation is published before
the tuner moves**, so no value from a previous station or mode can be taken
for the new one.

---

## 6. Versioning

- **v2.3 (this document)**: adds `locstatus`, `locdev`, `locddm`, `locsdm`,
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
- Reserved for future use: `vorident`, `vorto`, `vorcdi`, `locident`, and
  glide-slope fields (`gs*`).

## 7. Conformance testing

`tools/mock_bridge.py` validates every payload against §3, v2.2 and v2.3
shapes alike, including the cross-field rules (`locdev` 0 below status 4, one
mode at a time, `vor*` zero on a localizer channel).

```
tools/mock_bridge.py --port 8099 --script "109.4@0,109.5@10,0@20,113.35@25"
```

The script above exercises a VOR, a localizer (rejected unless the receiver
has `loc.enabled`), OFF as RB-01 sends it, and a retune.
