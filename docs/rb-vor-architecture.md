# RB-VOR — Architecture & Implementation Instructions

**Repo:** `xiaprojects/rb-vor` · **Interface:** RB-VOR-IF v1.0 · **Status:** design baseline, not yet implemented
**Doc version:** A (2026-07-24)

---

## 1. Scope

Software VOR receiver: RTL-SDR + Linux host decodes a VHF Omnidirectional Range station and publishes the radial and a signal-quality figure into the RB-Avionics bridge.

**Technology matrix cell**

| Axis | Value |
|---|---|
| OS | Linux (Raspberry Pi OS 64-bit / Debian 12+) |
| Compute | Raspberry Pi 5 (RB-03 / RB-01 Linux branch) |
| Sensor bus | none — USB SDR |
| Display interface | none — headless daemon |
| Form factor | remote box |
| RF | RTL-SDR (RTL2832U) |

**Other branch:** the ESP32/ESP32-S3 branch (RB-02, RB-05, RB-07) **cannot** host this function — no VHF front end, no FPU headroom for the 9960 Hz FM path. Those products consume `vordir`/`vorsignal` as bridge clients only. This asymmetry is intentional and must be stated in the RB-02/RB-05 feature tree as *"VOR: consumer only"*.

**Safety framing:** supplemental / non-required equipment. Not a certified NAV receiver, VFR day use, not for IFR navigation, not for approach guidance. Independent primary navigation is assumed present. Never publish a bearing without an accompanying quality figure, and never hold a stale bearing silently (§9, §10.4).

---

## 2. Prior art and tool survey

Do not start from zero. Evaluate and credit before writing DSP.

| Item | Class | Use |
|---|---|---|
| `librtlsdr` / `rtl-sdr-blog` fork | driver | primary device access |
| `SoapySDR` + `SoapyRTLSDR` | HAL | **preferred** — abstracts RTL / Airspy / SDRplay, satisfies objective 5 (replaceable components) |
| `pyrtlsdr` | binding | acceptable fallback if SoapySDR packaging is a burden |
| `rtl_fm`, `rtl_sdr`, `rtl_tcp` | CLI | bench/bring-up only, **not** the production data path (no phase-coherent control, no clean retune) |
| `rtl_433` | reference | study its input abstraction (RTL-SDR / SoapySDR / `rtl_tcp` / file loader) and its dumper/replay model — copy that structure |
| `liquid-dsp` | C DSP lib | if the Python path misses the CPU budget |
| `numpy` / `scipy.signal` | DSP | reference implementation |
| `martinber/vor-python-decoder` | prior art | <cite index="2-1">works from a demodulated AM WAV recording: low-pass to recover one 30 Hz tone, band-pass at 9.96 kHz and mix against a complex exponential to centre the FM subcarrier at 0 Hz, FM-demodulate to recover the second 30 Hz tone, then take the phase difference to get the bearing</cite>. Reported <cite index="6-1">accuracy of about ±3° across three test locations</cite>. Offline, CVOR-oriented, WAV-in. **Take the algorithm, replace the I/O.** |

Note a naming trap: published descriptions disagree on which 30 Hz tone is "reference" and which is "variable" (the roles swap between CVOR and DVOR, and the referenced README uses the opposite convention to the classic CVOR one). The *magnitude* of the phase difference is unaffected; only the **sign** is. Do not hardcode a sign — see §8.

---

## 3. RF chain and BOM

```
[VOR antenna] -> [FM broadcast band-stop] -> [optional airband LNA] -> [RTL-SDR] -> USB -> [Pi 5]
```

The FM broadcast band-stop filter is **not optional**. 108.00 MHz sits immediately above the 88–108 MHz broadcast band; without it the RTL front end will desensitise or produce images on any site within range of an FM transmitter.

| # | Item | Class | Primary | Alternate ≥1 | Note |
|---|---|---|---|---|---|
| 1 | SDR dongle | distributor/Amazon | RTL-SDR Blog V4 (R828D, 1 ppm TCXO) | Nooelec NESDR SMArTee v2 (TCXO); generic RTL2832U + R820T2 (AliExpress) | V4 needs current `librtlsdr` (rtl-sdr-blog fork). Record the exact driver requirement in the installer. |
| 2 | FM band-stop | distributor/Amazon | RTL-SDR Blog broadcast-FM block (88–108 MHz) | Nooelec Flamingo FM+ | mandatory |
| 3 | Airband filter/LNA (optional) | distributor | 108–137 MHz SAW/cavity + LNA | none needed if signal margin is adequate | only if bench test shows deficit |
| 4 | Antenna, ground test | build | horizontal V-dipole, each leg 640 mm (λ/4 @ 117 MHz ≈ 640 mm, @ 108 MHz ≈ 694 mm — cut for 112 MHz ≈ 670 mm) | telescopic dipole kit shipped with dongle | **VOR is horizontally polarised** — a vertical whip loses ~20 dB |
| 5 | Antenna, aircraft | aviation supply | existing VOR cat's-whisker / blade, 50 Ω | dedicated second antenna | if splitting from an installed NAV antenna, use a proper splitter and treat as an aircraft modification |
| 6 | Coax | Amazon | RG-316 SMA-SMA 1 m | RG-58 BNC + adapter | keep short |
| 7 | USB isolator (optional) | Amazon | USB 2.0 galvanic isolator | ferrite clamp on USB lead | Pi 5 switching noise ingress |
| 8 | Ferrites | Amazon | clip-on 5 mm × 4 | — | one per USB and coax end |

**EMI:** the dongle and the Pi both radiate in-band. Shield the dongle, keep the USB lead short and ferrited, and bench-verify that RB-VOR running does not raise the noise floor of the aircraft COM/NAV receivers before any airborne test.

**Assumption to confirm:** aircraft antenna sharing arrangement at LIQQ. Flagged, not invented.

---

## 4. Signal structure (what we must extract)

| Component | Modulation | Nominal | Purpose |
|---|---|---|---|
| Carrier | AM | 108.000–117.950 MHz | RF |
| 30 Hz tone A | AM on carrier, ~30 % depth | 30 Hz | phase datum #1 |
| 9960 Hz subcarrier | AM on carrier, ~30 % depth | 9960 Hz | carries phase datum #2 |
| 30 Hz tone B | FM on the 9960 Hz subcarrier, ±480 Hz deviation | 30 Hz | phase datum #2 |
| Ident | AM, keyed | 1020 Hz | 2–3 letter Morse, every ~10 s |
| Voice (optional) | AM | 300–3000 Hz | ATIS/AWOS on some stations |

Radial = phase difference between tone A and tone B, referenced so that magnetic north = 0°.
Occupied baseband: subcarrier + Carson bandwidth ≈ 9960 + (480 + 30) ≈ 10.5 kHz → channel filter ±12 kHz.

CVOR and DVOR swap which tone is reference and which is variable, and DVOR reverses rotation sense so a normal receiver output is identical. Our decoder therefore needs one configurable sign, not two decoders (§8).

---

## 5. Frequency validation (requirement 1)

`vorfreq` is in MHz, float.

| Rule | Test |
|---|---|
| R1 | 108.000 ≤ f ≤ 117.950 |
| R2 | f lands on the 50 kHz grid: `round(f*1000)` ≡ 0 mod 50, tolerance ±1 kHz on the float |
| R3 | 108.000–111.950: **VOR only if the 100 kHz digit is even**. Odd 100 kHz digit = ILS localiser → not a VOR. `d = (round(f*1000) % 1000) // 100; valid ⇔ d % 2 == 0` |
| R4 | 112.000–117.950: all 50 kHz channels are VOR |
| R5 | optional `denylist` / `allowlist` in config for regional exceptions and test frequencies |

Examples: 109.40 ✅ (d=4) · 109.50 ❌ (d=5, localiser) · 108.05 ✅ (d=0) · 108.15 ❌ · 113.35 ✅ · 117.975 ❌ (off grid).

Implement as a pure function `is_vor_frequency(mhz) -> (bool, reason)` with a table-driven unit test covering every 50 kHz channel from 108.000 to 117.950 (200 cases) plus out-of-band and off-grid cases. This function is a published part of the platform — expose it, version it, reuse it in RB-01/RB-02.

**Change detection:** act only on a *change* of the validated, quantised channel (compare in integer kHz, never float equality). Debounce 300 ms so that a user spinning a tuning knob does not trigger a retune storm.

---

## 6. Software architecture

Single daemon, `rb-vord`, Python 3.11 + asyncio, one process, three concurrency domains.

```
                    ws://localhost/bridge
                             |
                    +--------v---------+
                    |  bridge_client   |  asyncio, autoreconnect+backoff
                    |  (WS consumer)   |  parses vorfreq, gpsTrack
                    +--------+---------+
                             | channel change (int kHz)
                    +--------v---------+
                    |    controller    |  state machine (§7), debounce,
                    |                  |  validation (§5), retune orders
                    +----+--------+----+
             retune  |        |  solutions (1 Hz)
                +----v----+   |   +----v-------------+
                | sdr_rx  |   |   |    publisher     |
                | thread  |   |   | POST /bridgeUpdate|
                +----+----+   |   +------------------+
                     | IQ ring buffer (lock-free, np arrays)
                +----v----+
                | dsp     |  thread: decimate -> AM env -> 2x 30 Hz -> phase
                +---------+
```

| Module | File | Responsibility |
|---|---|---|
| `bridge_client` | `rbvor/bridge_client.py` | WS connect, reconnect with exponential backoff (0.5→30 s), tolerant JSON parse (fields may be absent), emits `ChannelRequest` |
| `controller` | `rbvor/controller.py` | validation, debounce, state machine, retune sequencing, hold/staleness timers |
| `sdr_rx` | `rbvor/sdr.py` | SoapySDR stream in a dedicated thread, int8→complex64 via 256-entry LUT, writes to ring buffer, never blocks |
| `dsp` | `rbvor/dsp.py` | the pipeline of §7 — pure, testable, accepts a file source instead of the SDR |
| `bearing` | `rbvor/bearing.py` | phase→radial, calibration, sign, circular smoothing |
| `quality` | `rbvor/quality.py` | `vorsignal` computation (§9) |
| `ident` | `rbvor/ident.py` | 1020 Hz Morse decode (v1.1, build the hook now) |
| `publisher` | `rbvor/publisher.py` | REST POST, retry/backoff, never blocks DSP, drops rather than queues unbounded |
| `freqrules` | `rbvor/freqrules.py` | §5, published/reusable |
| `config` | `rbvor/config.py` | JSON load + schema validation, no hardcoded values anywhere else |
| `record` | `rbvor/record.py` | IQ dump/replay (à la `rtl_433` dumpers) — mandatory for field debugging and cloud forensics |

**Rules**
- Nothing except `config.py` reads a literal address, threshold, gain or endpoint.
- `dsp.py` must run identically from a `.cs8`/`.cf32` file — this is the test harness *and* the RB-Cloud replay path.
- The DSP thread never performs I/O. The publisher never performs DSP. A stalled bridge must not stall demodulation.
- Vendor names stay out of the code: `SDR`, `NAVAID`, `BRIDGE`. Station names come from data files.

---

## 7. State machine and DSP pipeline

### 7.1 States

| State | Entry | Publishes | Exit |
|---|---|---|---|
| `IDLE` | start, or `vorfreq` absent | `vorsignal=0` once | valid channel received |
| `INVALID_FREQ` | `vorfreq` fails §5 | `vorsignal=0` once, log reason | valid channel received |
| `TUNING` | channel change | `vorsignal=0` immediately | retune complete + settle |
| `ACQUIRING` | after settle | `vorsignal=0` @1 Hz | ≥2 consecutive valid solutions |
| `TRACKING` | valid solutions | `vordir`,`vorsignal` @1 Hz | quality < flag threshold |
| `DEGRADED` | quality below threshold but > 0 | `vordir` (last good) + true low `vorsignal`, ≤ `hold_s` | recovery → TRACKING; timeout → `NO_SIGNAL` |
| `NO_SIGNAL` | hold timeout, or no subcarrier | `vorsignal=0` @1 Hz, `vordir` unchanged/last | quality recovers |

Retune sequence: stop stream → set centre frequency → clear ring buffer → wait `settle_ms` (default 250) → discard first full integration window → resume. Publish `vorsignal=0` at the instant of retune, before the SDR settles — never let a bearing from the old station survive a channel change.

### 7.2 Chain (defaults, all in config)

| Stage | Parameter |
|---|---|
| Sample rate | 1.024 Msps complex, int8 |
| Tuning | centre = f_VOR − 250 kHz (**offset tuning**, keeps the RTL DC spike and IQ imbalance out of the channel) |
| Digital mix | ×exp(+j2π·250 kHz·t) to bring the carrier to 0 Hz |
| Channel filter | FIR low-pass, cutoff 12 kHz, transition 4 kHz, ~60 dB stopband |
| Decimation 1 | ÷32 → **32 kHz** complex (polyphase, `resample_poly` or hand FIR) |
| AGC | normalise by carrier magnitude (moving mean of \|z\|, τ = 0.5 s) — keeps modulation depth measurable |
| AM demod | `env = abs(z)`; carrier level `C = mean(env)`; `audio = env − C` |
| **Path A (AM 30 Hz)** | LPF 100 Hz (FIR, linear phase) → ÷32 → 1 kHz |
| **Path B (FM 30 Hz)** | BPF 9960 ± 600 Hz on `audio` → mix ×exp(−j2π·9960·t) → LPF 600 Hz → ÷32 → 1 kHz → `d/dt unwrap(angle)` → **the same LPF 100 Hz design as Path A** → 1 kHz |
| Integration | complex Goertzel/DFT at exactly 30 Hz, window = 1000 samples = 1.000 s = **30 whole cycles** (zero spectral leakage), hop 1 s. Low-signal option: 3 s window, 1 s hop (`integration_s`) |
| Output | `φ_A`, `φ_B`, `|A|`, `|B|`, `C`, `subcarrier_level`, noise estimate from bins 25/35 Hz |

**Critical:** Path A and Path B must terminate in the *same* 100 Hz filter design so their group delays cancel at 30 Hz. Residual delay from the band-pass and FM demod in Path B is a fixed constant — measure it once with the synthetic generator (§12.1) and store it as `phase_offset_deg`. Do not guess it.

---

## 8. Bearing computation

```
Δφ  = wrap180(φ_A − φ_B)
raw = wrap360(sign * degrees(Δφ) + phase_offset_deg + station_cal_deg)
vordir = round(raw) % 360
```

| Constant | Config key | How obtained |
|---|---|---|
| `sign` | `bearing.phase_sign` | +1 or −1. **Determined empirically once**, against a station of known position with a known GPS-derived magnetic bearing. Never inferred from a textbook, because the CVOR/DVOR naming is inconsistent across sources (§2). |
| `phase_offset_deg` | `bearing.phase_offset_deg` | measured against the synthetic generator at a commanded radial (§12.1) — this is pure filter group delay |
| `station_cal_deg` | `bearing.station_cal_deg` | site/installation residual, default 0 |

**Definition of `vordir` (v1.0):** the **radial FROM the station**, in **degrees magnetic**, integer 0–359, where 0 = magnetic north. If the consuming display needs bearing-TO, it computes `(vordir + 180) % 360`. This must be stated in the interface spec, not left to the reader.

Smoothing: circular (vector) mean over the last `smooth_n` solutions, default 3. Never smooth linearly across the 359/0 wrap.

`gpsTrack` in v1.0 is **consumed for diagnostics only** — logged alongside each solution so that ground and flight test can correlate bearing error with aircraft heading (antenna pattern / airframe shadowing). It is not used in the bearing computation. Do not silently start using it.

---

## 9. `vorsignal` definition

Integer **0–100**. This is a published semantic, so pin it down:

| Value | Meaning |
|---|---|
| 0 | **NAV flag** — no usable solution. `vordir` must be treated as invalid by every consumer. |
| 1–39 | degraded — usable for situational awareness only |
| 40–69 | acceptable |
| 70–100 | good |

Composition (weights in config, defaults shown):

| Term | Measure | Weight |
|---|---|---|
| `q_am` | 30 Hz AM depth vs 30 % nominal, saturating | 0.25 |
| `q_sub` | 9960 Hz subcarrier depth vs 30 % nominal | 0.25 |
| `q_snr` | 30 Hz bin power / mean of adjacent bins (25, 27, 33, 35 Hz), mapped through 0–20 dB | 0.30 |
| `q_stab` | 1 − (circular std-dev of last 5 bearings / 10°), clipped | 0.20 |

`vorsignal = clip(round(100 · Σ wᵢqᵢ), 0, 100)`, forced to 0 when `q_sub` or `q_am` falls below `flag_threshold` (default 0.15) or when the state is not `TRACKING`/`DEGRADED`.

Each `qᵢ` is logged separately at 1 Hz — a single scalar is useless for diagnosing an installation.

---

## 10. Interface specification — RB-VOR-IF v1.0

### 10.1 Consumed — WebSocket
- **Endpoint:** `ws://localhost/bridge` (host/path in config)
- **Direction:** RB-VOR is a *client*, read-only. It sends no WS frames except protocol pings.
- **Encoding:** UTF-8 JSON object per frame. Unknown fields ignored. Absent fields = no change.

| Field | Type | Unit | Range | Use |
|---|---|---|---|---|
| `vorfreq` | number | MHz | 108.000–117.950 | tuning command, §5 |
| `gpsTrack` | number | ° true | 0–359 | diagnostics/log only in v1.0 |
| `vordir` | number | ° mag | 0–359 | **ignored on input** (echo of our own output) |
| `vorsignal` | number | — | 0–100 | ignored on input |

### 10.2 Produced — REST
- **Endpoint:** `POST http://localhost/bridgeUpdate`, `Content-Type: application/json`
- **Rate:** 1 Hz nominal. Additionally an immediate out-of-band POST on every state transition (retune, flag set, flag clear).
- **Body (v1.0, exactly two fields):**
```json
{ "vordir": 130, "vorsignal": 0 }
```

| Field | Type | Unit | Range | Semantics |
|---|---|---|---|---|
| `vordir` | integer | ° magnetic | 0–359 | radial FROM the station |
| `vorsignal` | integer | — | 0–100 | 0 = NAV flag, `vordir` invalid |

Byte order: N/A (JSON text). Numbers are JSON numbers, no strings, no units in the value.

### 10.3 Publish policy
Both behaviours are supported, selected by config, because "on change" and "every second" are different contracts:

| `publish.mode` | Behaviour |
|---|---|
| `periodic` (default) | POST every `publish.period_s` (1.0) regardless of change — a consumer can then detect our death by silence |
| `on_change` | POST only when `vordir` moves by ≥ `publish.deadband_deg` (default 1) **or** `vorsignal` crosses a band boundary, plus a keep-alive POST every `publish.keepalive_s` (default 5) |

`on_change` without a keep-alive is forbidden — a consumer could not distinguish "steady" from "dead".

### 10.4 Failure and staleness semantics
- No valid solution for `hold_s` (default 3 s) → `vorsignal = 0`.
- WS disconnected → after `ws_grace_s` (default 5 s) the SDR is released and the daemon returns to `IDLE`. RB-VOR does not keep transmitting a bearing to a bridge it can no longer hear from.
- REST POST failure → retry once immediately, then back off; **drop** stale solutions rather than queue them. A late bearing is worse than no bearing.
- Consumers must treat `vordir` as invalid whenever `vorsignal == 0`, and must apply their own staleness timeout (recommended 3 s) on top of ours.

### 10.5 Versioning
Additive fields only within v1.x (`vorident`, `vorto`, `vorcdi`, `vorquality`). Any change to the meaning, unit or range of `vordir` or `vorsignal` is a **breaking change** requiring v2.0 and a migration note. Reserved names must be claimed in this document before use.

---

## 11. Configuration — `rb-vor.json`

Nothing is hardcoded. Ship a documented default plus a JSON Schema (`schema/rb-vor.schema.json`) and validate at start-up; refuse to run on a config that fails schema validation.

```json
{
  "$schema": "./schema/rb-vor.schema.json",
  "version": "1.0",
  "bridge": {
    "ws_url": "ws://localhost/bridge",
    "rest_url": "http://localhost/bridgeUpdate",
    "ws_reconnect_min_s": 0.5,
    "ws_reconnect_max_s": 30.0,
    "ws_grace_s": 5.0,
    "http_timeout_s": 0.8
  },
  "publish": {
    "mode": "periodic",
    "period_s": 1.0,
    "deadband_deg": 1,
    "keepalive_s": 5.0,
    "hold_s": 3.0
  },
  "sdr": {
    "driver": "soapy",
    "device_args": "driver=rtlsdr",
    "sample_rate_hz": 1024000,
    "tuner_offset_hz": -250000,
    "gain_mode": "manual",
    "gain_db": 32.8,
    "ppm": 0,
    "bias_tee": false,
    "settle_ms": 250,
    "buffer_blocks": 16,
    "block_samples": 32768
  },
  "dsp": {
    "channel_lp_hz": 12000,
    "decim_1": 32,
    "subcarrier_hz": 9960,
    "subcarrier_bp_hz": 600,
    "tone_lp_hz": 100,
    "decim_2": 32,
    "tone_hz": 30,
    "integration_s": 1.0,
    "hop_s": 1.0,
    "agc_tau_s": 0.5
  },
  "bearing": {
    "phase_sign": 1,
    "phase_offset_deg": 0.0,
    "station_cal_deg": 0.0,
    "smooth_n": 3
  },
  "quality": {
    "weights": { "am": 0.25, "sub": 0.25, "snr": 0.30, "stab": 0.20 },
    "am_nominal_depth": 0.30,
    "sub_nominal_depth": 0.30,
    "snr_lo_db": 0.0,
    "snr_hi_db": 20.0,
    "flag_threshold": 0.15
  },
  "frequency": {
    "min_mhz": 108.0,
    "max_mhz": 117.95,
    "grid_khz": 50,
    "debounce_ms": 300,
    "allowlist_mhz": [],
    "denylist_mhz": []
  },
  "record": {
    "enabled": false,
    "dir": "/var/lib/rb-vor/iq",
    "format": "cs8",
    "max_seconds": 30,
    "trigger": "on_flag"
  },
  "log": { "level": "INFO", "solution_csv": "/var/log/rb-vor/solutions.csv" }
}
```

---

## 12. Repository layout and packaging

```
rb-vor/
├── README.md                  builder-facing: hand tools, soldering iron, 3D printer, no lab
├── CLAUDE.md                  working notes, protocol/decision log, corrections history
├── LICENSE                    (match rb-esp / rb-ems-go)
├── docs/
│   ├── rb-vor-architecture.md this document
│   ├── RB-VOR-IF-v1.0.md      §10 extracted as a standalone published interface spec
│   ├── install.md             RF chain, antenna build, filter, first-light procedure
│   ├── calibration.md         phase_sign and phase_offset_deg procedure
│   └── test-report-template.md
├── rbvor/                     package (§6 module table)
├── schema/rb-vor.schema.json
├── config/rb-vor.json         default config
├── tools/
│   ├── vor_gen.py             synthetic VOR IQ generator (§12.1)
│   ├── vor_replay.py          file → dsp, offline decode
│   └── vor_sweep.py           0–359° sweep accuracy report
├── tests/
│   ├── test_freqrules.py      all 200 channels + edge cases
│   ├── test_dsp_synthetic.py  ±0.5° at high SNR, ±2° at 6 dB SNR
│   ├── test_statemachine.py
│   └── test_interface.py      WS parse tolerance, REST payload shape
├── systemd/rb-vor.service
├── install.sh
└── requirements.txt
```

`systemd/rb-vor.service`: `Type=simple`, `Restart=always`, `RestartSec=2`, `After=network-online.target`, dedicated `rb-vor` user in the `plugdev` group, udev rule for the dongle, `WatchdogSec=10` with sd_notify pings from the controller loop. Ship the udev rule in `install.sh` — do not tell builders to run as root.

Deliverable format: ZIP bundle containing the tree above, ready to `./install.sh` on a clean Raspberry Pi OS image.

### 12.1 Synthetic generator (build this first)

`tools/vor_gen.py` produces an IQ file for a commanded radial, SNR and VOR type:

```
vor_gen.py --radial 137 --type cvor --snr-db 20 --seconds 30 --fs 1024000 --offset -250000 -o t137.cs8
```

Model: carrier, 30 % AM at 30 Hz with phase per the commanded radial, 30 % AM at 9960 Hz FM-modulated ±480 Hz by 30 Hz, 1020 Hz keyed Morse ident, AWGN. A `--type dvor` switch swaps the roles and reverses rotation sense.

This file is the entire basis for CI, for the `phase_offset_deg` measurement, and for proving the CVOR/DVOR sign handling without an airplane. **No DSP code is merged without a passing sweep against it.**

---

## 13. Test and verification plan

| # | Level | Method | Acceptance |
|---|---|---|---|
| T1 | unit | `test_freqrules` | 200/200 channels classified correctly, 109.4 valid, 109.5 rejected |
| T2 | unit | synthetic, 0–359° in 1° steps, SNR 30 dB | error ≤ 0.5° RMS, ≤ 1.0° peak |
| T3 | unit | synthetic, SNR sweep 30→0 dB | error ≤ 2° at 10 dB; `vorsignal` decreases monotonically; flag asserts below the usable point |
| T4 | unit | synthetic DVOR | same accuracy with `phase_sign` flipped; documented in `calibration.md` |
| T5 | integration | mock bridge (WS server + REST sink) | retune within 1.5 s of `vorfreq` change; `vorsignal=0` posted before any new bearing; invalid frequency → flag, no retune |
| T6 | integration | kill/restart the mock bridge, unplug the dongle | no crash, correct backoff, correct return to `IDLE`, watchdog does not trip |
| T7 | bench RF | signal generator or a second SDR transmitting the synthetic file | end-to-end through the real RF chain |
| T8 | ground | real station, known site, LIQQ | error ≤ 3° vs GPS-computed magnetic bearing to the station, over ≥ 10 min |
| T9 | ground | FM band-stop in/out comparison | quantified desensitisation figure recorded in the test report |
| T10 | EMI | RB-VOR running vs off | no measurable degradation of aircraft COM/NAV reception |
| T11 | flight | orbit at constant radius, log `gpsTrack` vs error | error ≤ 5° over the full 360° of heading; antenna shadowing characterised |
| T12 | endurance | 8 h continuous | no memory growth, no sample overrun, no unhandled reconnect |

Every test emits a machine-readable log; T8/T11 logs go to RB-Cloud under a documented schema with a retention note.

**Performance budget (Pi 5):** 1.024 Msps int8 → complex64, one polyphase ÷32, one BPF, one FM demod, two ÷32 stages, two 30 Hz DFTs. Target < 25 % of one core with numpy/scipy in 32 k blocks. Measure in T12; if exceeded, move `dsp.py`'s front end to `liquid-dsp` via a thin C extension, keeping the Python reference as the test oracle.

---

## 14. Failure modes (input to the RB-VOR FMEA)

| ID | Failure | Effect | Detection | Mitigation |
|---|---|---|---|---|
| F1 | Wrong bearing published as valid | misleading navigation information — **worst case** | `q_stab`, cross-check against GPS-derived bearing when station position is known | quality gate, flag threshold, supplemental-only framing, cross-check monitor in v1.1 |
| F2 | Stale bearing after station loss | as F1 | `hold_s` timer | force `vorsignal=0` |
| F3 | Stale bearing after retune | as F1 | state machine | `vorsignal=0` at the instant of retune, buffer flush |
| F4 | Sign inversion (CVOR/DVOR) | 180°-symmetric error | calibration procedure, T4/T8 | one-time calibration, recorded per installation |
| F5 | ILS localiser tuned as VOR | nonsense bearing | R3 | reject at validation |
| F6 | FM broadcast desensitisation | low quality, bearing noise | `q_snr` | mandatory band-stop filter, T9 |
| F7 | SDR unplugged / USB reset | no output | Soapy error, watchdog | flag, restart, systemd `Restart=always` |
| F8 | Bridge unreachable | no output | POST failure | backoff, drop stale, log |
| F9 | Sample overrun under CPU load | phase error, discontinuity | overrun counter from Soapy | reject the affected window outright, never decode a gapped buffer |
| F10 | Host clock/PPM drift | tuning offset | — | TCXO dongle, `ppm` in config |

F1 is the reason `vorsignal` exists and the reason it must never be optimistic.

---

## 15. Implementation order

Each step ends with a runnable artifact and a passing test. No step is skipped, and no step is merged with a failing predecessor.

| Step | Deliverable | Acceptance |
|---|---|---|
| 1 | `freqrules.py` + `test_freqrules.py` | T1 |
| 2 | `tools/vor_gen.py` | produces a file; spectrum inspected and documented |
| 3 | `dsp.py` file-in reference decode + `tools/vor_replay.py` | T2 |
| 4 | `bearing.py`, `quality.py`, calibration procedure + `docs/calibration.md` | T3, T4 |
| 5 | `sdr.py` live capture to file; confirm live IQ decodes through the same path | first light on a real station |
| 6 | `controller.py` state machine + `config.py` + schema | T5 |
| 7 | `bridge_client.py`, `publisher.py` | T5, T6 |
| 8 | packaging: `install.sh`, systemd unit, udev rule, README, CLAUDE.md | clean-image install works unaided |
| 9 | `record.py`, solution CSV, RB-Cloud schema note | T12 |
| 10 | `docs/RB-VOR-IF-v1.0.md` published as a standalone spec | third party can implement a consumer from it alone |

---

## 16. Backlog (objective 6 — parity, not scope reduction)

| Item | Note |
|---|---|
| ILS localiser | 90/150 Hz DDM on the odd-decimal 108–112 channels — same front end, different back end, `vorcdi` field |
| Glideslope | 329–335 MHz, second dongle |
| Marker beacons | 75 MHz, 400/1300/3000 Hz |
| Morse ident decode | `vorident` string, v1.1 — real safeguard against tuning the wrong station |
| Station database | position + magnetic variation → TO/FROM, CDI, cross-check monitor against GPS (mitigates F1) |
| Multi-channel | one 2.4 Msps capture covers a 2.4 MHz slice → several navaids decoded simultaneously from one dongle |
| Dual VOR / position fix | two radials → fix, cross-check against GPS |
| ADS-B coexistence | RB-01/RB-07 already want the SDR at 1090 MHz — **one dongle cannot do both**; document the two-dongle requirement now |
| Cloud | radial + quality + IQ snippets on flag events, documented schema, retention and privacy note |

---

## 17. Open items — flagged, not invented

1. `phase_sign` and `phase_offset_deg` — must be measured (§12.1, T4, T8). No plausible value is to be written into the shipped config; ship `null` and refuse to publish a bearing until calibration is recorded.
2. Whether the bridge's `vorsignal` is expected by existing RB consumers as 0/1 boolean rather than 0–100. **This must be confirmed against the current bridge implementation before v1.0 freezes** — the example payload only ever shows `0`. If boolean is required, keep 0–100 internally and add a `publish.signal_scale: "percent" | "boolean"` mapping rather than degrading the metric.
3. Aircraft antenna arrangement and whether the installed NAV antenna may be shared.
4. Bridge authentication — none shown in the example; if the bridge later requires a token, it goes in `config.bridge`.
5. RTL-SDR Blog V4 driver availability in the target Raspberry Pi OS image (the V4 needs the rtl-sdr-blog fork of `librtlsdr`); `install.sh` must detect and handle this.
