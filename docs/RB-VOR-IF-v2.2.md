# RB-VOR-IF v2.2 — published interface specification

**Status:** stable · **Version:** 2.2 · **Date:** 2026-07-24
**Producer:** RB-VOR software VOR receiver · **Platform:** RB-Avionics

This document is self-contained. A third party implementing a consumer needs
nothing else.

---

## 1. Overview

RB-VOR decodes a VHF Omnidirectional Range station from an RTL-SDR and publishes
the radial and a signal-quality figure into the RB-Avionics bridge.

| Direction | Transport | Endpoint | Role |
|---|---|---|---|
| consumed | WebSocket | `ws://localhost/bridge` | RB-VOR is a read-only client |
| produced | HTTP POST | `http://localhost/bridgeUpdate` | one JSON object per update |

Hosts and paths are configurable; the shapes below are not.

> **Safety.** RB-VOR is supplemental / non-required equipment. Day VFR
> situational awareness only. Not a certified NAV receiver, not for IFR
> navigation or approach guidance. Independent primary navigation is assumed
> present.

---

## 2. Consumed — WebSocket

RB-VOR connects as a client and ~~sends no frames except protocol pings~~
sends no frames at all. RB-01 has no ping/pong and answers no control frame,
so the link is considered lost only when the socket is: closed by the peer, or
declared dead by TCP keepalive on RB-VOR's side (CLAUDE.md A29). Each inbound
frame is a UTF-8 JSON object.

| Field | Type | Unit | Range | Use |
|---|---|---|---|---|
| `vorfreq` | number \| null | MHz | 108.000–117.950 | tuning command |
| `gpsTrack` | number | ° true | 0–359 | **diagnostics and logging only in v1.0** |

Every other field is ignored, including `vorradial`, `vorsnr` and `voram`,
which are echoes of RB-VOR's own output.

### 2.1 `vorfreq` — three distinct cases

| Case | Meaning |
|---|---|
| **absent** | no change; keep the current channel |
| **`null`** | no channel selected; release and go idle |
| **present, numeric** | tune this channel |
| **present, non-numeric** | frame is malformed; dropped, channel unchanged |

Conflating any two of these is a defect. A malformed frame is dropped and
logged; it does not close the connection.

### 2.2 Channel validation

A frequency is accepted only if all of the following hold.

| # | Rule |
|---|---|
| 1 | 108.000 ≤ f ≤ 117.950 MHz |
| 2 | f lands on the 50 kHz grid (±1 kHz tolerance) |
| 3 | 108.000–111.950: **VOR only if the 100 kHz digit is even.** An odd digit is an ILS localiser and is rejected. |
| 4 | 112.000–117.950: every 50 kHz channel is a VOR channel |

Examples: `109.40` ✅ · `109.50` ❌ localiser · `108.05` ✅ · `108.15` ❌ ·
`113.35` ✅ · `117.975` ❌ off-grid · `118.00` ❌ out of band.

Rejection produces one all-zero payload (`vorradial=0, vorsnr=0, voram=0`) and no retune.

### 2.3 Repetition

A bridge may re-assert its state at any rate. Repeating the **same** `vorfreq`
is a no-op at any point, including mid-retune. Producers need not deduplicate.

### 2.4 Reconnection

RB-VOR reconnects with exponential backoff (0.5 s → 30 s). After the
configured grace period without a connection it releases the SDR and goes idle
rather than continue transmitting to a bridge it can no longer hear from.

The bridge socket says nothing on connect and pushes only when a value changes. So
after every connection RB-VOR reads the state once with `GET` on `bridge.rest_url`,
and treats the answer as a frame. A restarted daemon therefore returns to the
`vorfreq` RB-01 already holds, and a bridge that drops and returns needs no new
command. If the read fails, the connection is made again and the read repeated.

---

## 3. Produced — HTTP POST

```
POST /bridgeUpdate
Content-Type: application/json

{ "vorradial": 130, "vorsnr": 42, "voram": 28, "vorstatus": 5,
  "vorsub": 29, "vorfreqactive": 109400 }
```

Encoding: UTF-8 JSON.

| Field | Type | Unit | Range | Semantics |
|---|---|---|---|---|
| `vorradial` | integer | ° magnetic | 0–359 | radial **FROM** the station |
| `vorsnr` | integer | dB | 0–120 | 30 Hz tone signal-to-noise ratio |
| `voram` | integer | — | 0–100 | 30 Hz AM depth ×100 (0.28 → 28) |
| `vorstatus` | integer | — | 0–5 | receiver status code (see §3.5) |
| `vorsub` | integer | — | 0–100 | 9960 Hz subcarrier level ×100 |
| `vorfreqactive` | integer | kHz | 0 or 108000–118000 | currently tuned VOR channel (109400 = 109.400 MHz); 0 = none |

**The payload contains exactly these six keys.** All are always present. All
are JSON integers, never strings, never floats, never null.

### 3.1 `vorradial`

Radial **from** the station, degrees magnetic, 0 = magnetic north. A consumer
needing bearing-to computes `(vorradial + 180) % 360`. RB-VOR can be configured
to publish bearing-to directly (`publish.bearing_ref: "to"`); v2.0 default is
**from**.

### 3.2 `vorsnr` and `voram`

`vorsnr` is the measured 30 Hz reference-tone SNR in whole dB; `voram` is the
measured 30 Hz AM modulation depth scaled ×100 (nominal healthy signal ≈ 30).
Together they describe signal quality: a usable bearing typically has
`vorsnr` ≳ 15 and `voram` ≳ 15. They are measurements, not a 0–100 quality
score — a consumer decides its own usability threshold.

### 3.5 `vorstatus` — receiver status code

A single integer describing what the receiver is doing, independent of whether a
usable bearing exists:

| Code | Meaning | RB-VOR state(s) |
|---|---|---|
| 0 | OFF / disabled / no frequency / idle | IDLE, INVALID_FREQ |
| 1 | antenna disconnected / no signal / very low data, or a channel is selected and the dongle cannot be had | NO_SIGNAL, NO_SDR |
| 2 | tuning | TUNING |
| 3 | acquiring | ACQUIRING |
| 4 | degraded (usable but poor) | DEGRADED |
| 5 | tracking (good) | TRACKING |

`vorstatus` is the field to drive a UI status light. A consumer wanting "do I
have a usable bearing?" checks `vorsnr > 0` (equivalently `vorstatus >= 4`, i.e. DEGRADED or TRACKING);
a consumer wanting "what is the receiver doing?" reads `vorstatus` directly.

### 3.6 `vorsub` — subcarrier level

The measured 9960 Hz FM subcarrier level, ×100 (nominal healthy ≈ 30). Unlike
the bearing trio, `vorsub` and `vorstatus` are **not** gated by bearing
validity: while ACQUIRING (`vorstatus == 3`) `vorsub` shows whether the antenna
is receiving the subcarrier at all, which is the single most useful diagnostic
for "is anything coming in?". It is 0 only when idle, tuning, or out of band
(`vorstatus` 0 or 2), where no live observation exists.

### 3.7 `vorfreqactive` — active tuned channel

The VOR channel the receiver is currently tuned to, in **kHz** (109400 =
109.400 MHz). This echoes back the commanded `vorfreq` as the receiver actually
applied it, so a consumer can confirm the tune took effect and label a displayed
bearing/distance with the station it belongs to. It is 0 when no channel is
selected (IDLE) or the commanded frequency was rejected as out of the airband
(INVALID_FREQ). Like `vorstatus` and `vorsub`, it is not gated by bearing
validity.

Note the unit: inbound `vorfreq` is in **MHz** (a float, e.g. 109.4); outbound
`vorfreqactive` is an integer in **kHz** (109400), to keep every published field
an integer. Divide by 1000 for MHz.

### 3.3 The all-zero (no-signal) payload

**Whenever there is no valid bearing, the three bearing fields are 0** (while
`vorstatus` and `vorsub` continue to describe the receiver):

```
{ "vorradial": 0, "vorsnr": 0, "voram": 0, "vorstatus": 1, "vorsub": 8, "vorfreqactive": 109400 }
```

When idle or out of band, all five are 0 except that `vorstatus` carries the
reason code:

```
{ "vorradial": 0, "vorsnr": 0, "voram": 0, "vorstatus": 0, "vorsub": 0, "vorfreqactive": 0 }
```

This is sent when the tuned frequency is **out of the airband / not a valid VOR
channel**, when the NAV flag is raised (weak or absent signal), during retune,
and when idle. A consumer treats `vorsnr == 0 && voram == 0` as **no valid
signal — ignore `vorradial`.**

### 3.4 Update rate — transmit on change

RB-VOR sends a POST **only when at least one of the six fields changes** from
the last value sent. Because `vorstatus` and `vorsub` change as the receiver
progresses (tuning → acquiring → tracking) and as signal level moves, they too
trigger sends -- a consumer therefore sees status transitions immediately. A steady signal produces no traffic. An optional keep-alive
(`publish.keepalive_s`, default 5 s) re-sends the current payload periodically so
a consumer can distinguish a steady reading from a dead daemon; set it to 0 to
disable and send purely on change.

Every state transition (retune, flag raised or cleared, going out of band)
changes the payload and is therefore sent immediately.

## 4. Consumer requirements

These are normative. A consumer that does not do these things is not conforming.

1. **Treat `vorradial` as invalid whenever `vorsnr == 0`** (equivalently
   `vorstatus != 4`). Do not display it, smooth through it, or feed it to an
   autopilot. `vorstatus` and `vorsub` remain meaningful in that case and may be
   shown as receiver status.
2. **Apply an independent staleness timeout**, 3 s recommended. RB-VOR's own
   timeout does not protect against a dead process or a broken network. This
   matters more in v2.0 than v1.0: since updates are sent only on change, a
   consumer must not confuse "no traffic" with "still valid" -- use the
   keep-alive interval as the staleness bound if `publish.keepalive_s` is set.
3. **Do not assume monotonic time between updates.** Retunes and transitions
   produce out-of-band POSTs, and a steady signal produces none at all.
4. **Handle the 359 → 0 wrap** with circular arithmetic. A linear average of
   359 and 1 gives 180, which points the aircraft the wrong way.
5. **Ignore unknown fields.** v2.x may add them.

---

## 5. Behaviour reference

| RB-VOR state | Publishes | Consumer sees |
|---|---|---|
| IDLE | all zero | no channel selected |
| INVALID_FREQ / out of airband | all zero, once | frequency rejected (requirement 4) |
| TUNING | all zero, **before the tuner moves** | previous bearing already invalidated |
| NO_SDR | `vorstatus` 1, `vorfreqactive` the selected channel, the rest zero | dongle unavailable, for example held by rb-sdr-radio; retried every `sdr.retry_s` |
| ACQUIRING | all zero, sent once | settling |
| TRACKING | bearing + value ≥ 40 | usable |
| DEGRADED | bearing + value 1–39 | usable but poor |
| DEGRADED | usable radial, `vorstatus` 4 | usable but poor |
| NO_SIGNAL | all zero | invalid; per requirement 4 |

The ordering guarantee that matters: **a flag is always published before the
tuner moves**, so no bearing from a previous station can be mistaken for the
new one.

---

## 6. Versioning

- Additive fields only within v2.x. `vorstatus` and `vorsub` were added in v2.1;
  `vorfreqactive` was added in v2.2; and in v2.2 `vorstatus` gained code 5 by
  splitting the former "tracking" 4 into 4=DEGRADED and 5=TRACKING. A consumer
  written for v2.1 that treated 4 as "tracking" now sees 5 for a good track and
  4 for degraded -- if it only checked `>= 4` for "usable" it is unaffected;
  if it checked `== 4` it must widen to `>= 4`. Reserved for future use:
  `vorident`, `vorto`, `vorcdi`, and the v2.2-planned distance fields
  (`vordist`, `vordistsrc`, `vordistkind` -- see dme-plan.md).
- Any change to the meaning, unit or range of `vorradial`, `vorsnr`, `voram`,
  `vorstatus`, `vorsub` or `vorfreqactive` is a **breaking change**, requiring
  v3.0 and a migration note.
- Consumers must ignore unknown fields rather than reject the payload.
- **v1.0 → v2.0 migration**: `vordir` renamed to `vorradial` (unchanged
  semantics: radial FROM the station). `vorsignal` (a synthetic 0–100 quality
  score) replaced by two measured quantities, `vorsnr` (dB) and `voram`
  (AM depth ×100); a v1.0 consumer computing "usable" as `vorsignal > 0` should
  now use `vorsnr > 0 || voram > 0` — equivalently, the daemon guarantees both
  are 0 together whenever there is no valid solution, so either alone suffices.
  The `signal_scale` config option (percent/boolean) no longer applies; there is
  no synthetic quality score on the wire in v2.0.

## 7. Conformance testing

`tools/mock_bridge.py` implements this specification on both sides. Point it at
a consumer, or run the daemon against it; it validates every payload and reports
any deviation from §3.

```
tools/mock_bridge.py --port 8099 --script "109.4@0,109.5@10,113.35@20,null@30"
```

The script above exercises: a valid channel, a rejected localiser, a retune,
and a release.
