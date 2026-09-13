# RB-VOR-IF v1.0 — published interface specification

**Status:** superseded by [`RB-VOR-IF-v2.0.md`](RB-VOR-IF-v2.0.md) · **Version:** 1.0 · **Date:** 2026-07-24
**Producer:** RB-VOR software VOR receiver · **Platform:** RB-Avionics

> **This version is retained for historical reference only.** Deployments
> should implement v2.0, which renames `vordir`/`vorsignal` to
> `vorradial`/`vorsnr`/`voram` and sends on-change instead of periodically. See
> the migration note in v2.0 §6.

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

RB-VOR connects as a client and sends no frames except protocol pings. Each
inbound frame is a UTF-8 JSON object.

| Field | Type | Unit | Range | Use |
|---|---|---|---|---|
| `vorfreq` | number \| null | MHz | 108.000–117.950 | tuning command |
| `gpsTrack` | number | ° true | 0–359 | **diagnostics and logging only in v1.0** |

Every other field is ignored, including `vordir` and `vorsignal`, which are
echoes of RB-VOR's own output.

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

Rejection produces one `vorsignal: 0` update and no retune.

### 2.3 Repetition

A bridge may re-assert its state at any rate. Repeating the **same** `vorfreq`
is a no-op at any point, including mid-retune. Producers need not deduplicate.

### 2.4 Reconnection

RB-VOR reconnects with exponential backoff (0.5 s → 30 s). After the
configured grace period without a connection it releases the SDR and goes idle
rather than continue transmitting to a bridge it can no longer hear from.

---

## 3. Produced — HTTP POST

```
POST /bridgeUpdate
Content-Type: application/json

{ "vordir": 130, "vorsignal": 0 }
```

Encoding: UTF-8 JSON. Byte order: not applicable.

| Field | Type | Unit | Range | Semantics |
|---|---|---|---|---|
| `vordir` | integer | ° magnetic | 0–359 | radial **FROM** the station |
| `vorsignal` | integer | — | 0–100 | 0 = NAV flag; `vordir` invalid |

**The payload contains exactly these two keys.** Both are always present. Both
are JSON integers, never strings, never floats, never null.

### 3.1 `vordir`

Radial **from** the station, degrees magnetic, where 0 = magnetic north. A
consumer needing bearing-to computes `(vordir + 180) % 360`.

RB-VOR can be configured to publish bearing-to instead
(`publish.bearing_ref: "to"`). Deployments must agree on one convention; v1.0
default is **from**.

### 3.2 `vorsignal`

| Value | Band | Meaning |
|---|---|---|
| 0 | FLAG | no usable solution — **`vordir` must be ignored** |
| 1–39 | degraded | situational awareness only |
| 40–69 | acceptable | usable |
| 70–100 | good | usable |

Derived from four measured terms — 30 Hz AM depth, 9960 Hz subcarrier band
power, tone signal-to-noise, and the dispersion of recent bearings. The flag
asserts before the bearing error reaches 5°.

RB-VOR can be configured to publish 0/1 instead
(`publish.signal_scale: "boolean"`), where 1 means any usable bearing. The
internal metric remains 0–100 regardless.

### 3.3 Update rate

| Mode | Behaviour |
|---|---|
| `periodic` (default) | one POST every `period_s` (default 1.0 s) **whether or not anything changed** |
| `on_change` | POST when `vordir` moves by ≥ `deadband_deg`, or the quality band changes, plus a keep-alive every `keepalive_s` |

In both modes an **immediate out-of-band POST** accompanies every state
transition — retune, flag raised, flag cleared. A consumer never waits a full
period to learn that the bearing became invalid.

`on_change` without a keep-alive is forbidden: a consumer could not distinguish
a steady bearing from a dead daemon.

---

## 4. Consumer requirements

These are normative. A consumer that does not do these things is not conforming.

1. **Treat `vordir` as invalid whenever `vorsignal == 0`.** Do not display it,
   do not smooth through it, do not feed it to an autopilot.
2. **Apply an independent staleness timeout**, 3 s recommended. RB-VOR's own
   timeout does not protect against a dead process or a broken network.
3. **Do not assume monotonic time between updates.** Retunes and transitions
   produce out-of-band POSTs.
4. **Handle the 359 → 0 wrap** with circular arithmetic. A linear average of
   359 and 1 gives 180, which points the aircraft the wrong way.
5. **Ignore unknown fields.** v1.x may add them.

---

## 5. Behaviour reference

| RB-VOR state | Publishes | Consumer sees |
|---|---|---|
| IDLE | `vorsignal` 0 | no channel selected |
| INVALID_FREQ | `vorsignal` 0, once | frequency rejected |
| TUNING | `vorsignal` 0, **before the tuner moves** | previous bearing already invalidated |
| ACQUIRING | `vorsignal` 0 at the update rate | settling |
| TRACKING | bearing + value ≥ 40 | usable |
| DEGRADED | bearing + value 1–39 | usable but poor |
| NO_SIGNAL | last bearing + `vorsignal` 0 | invalid; ignore `vordir` |

The ordering guarantee that matters: **a flag is always published before the
tuner moves**, so no bearing from a previous station can be mistaken for the
new one.

---

## 6. Versioning

- Additive fields only within v1.x. Reserved for future use: `vorident`,
  `vorto`, `vorcdi`, `vorquality`.
- Any change to the meaning, unit or range of `vordir` or `vorsignal` is a
  **breaking change**, requiring v2.0 and a migration note.
- Consumers must ignore unknown fields rather than reject the payload.

## 7. Conformance testing

`tools/mock_bridge.py` implements this specification on both sides. Point it at
a consumer, or run the daemon against it; it validates every payload and reports
any deviation from §3.

```
tools/mock_bridge.py --port 8099 --script "109.4@0,109.5@10,113.35@20,null@30"
```

The script above exercises: a valid channel, a rejected localiser, a retune,
and a release.
