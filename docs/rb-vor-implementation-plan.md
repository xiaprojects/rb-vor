# RB-VOR — Implementation Plan

**Companion to:** `rb-vor-architecture.md` (doc A) · **Plan version:** A (2026-07-24)
**Repo:** `xiaprojects/rb-vor` · **Language:** Python 3.11 + numpy/scipy · **Target:** Raspberry Pi 5 / Debian 12+

---

## 0. Amendments to doc A, applied by this plan

| # | Change | Reason |
|---|---|---|
| A1 | `sdr.tuner_offset_hz` default `-250000` → **`-256000`** (= −fs/4 at 1.024 Msps) | down-mix degenerates to the ±1/±j sequence — no multiplies, no trig |
| A2 | `bearing.phase_sign` and `phase_offset_deg` ship as **`null`**, not 0 | daemon must refuse to publish a bearing before calibration is recorded (F1/F4) |
| A3 | Native-port trigger table added as a gate on M8, not a design choice | measured, not assumed |

---

## 1. Milestone map

```
M0 skeleton ──┬── M1 freqrules ──────────────────┐
              └── M2 vor_gen ── M3 dsp ── M4 bearing+quality ── M5 sdr live
                                                                    │
                            M1 ─────────────────────────────────────┼── M6 controller+config
                                                                    │
                                                     M6 ── M7 bridge I/O ── M8 packaging
                                                                                  │
                                                              M9 record+cloud ────┘
                                                                                  │
                                                              M10 field campaign ─┘
```

| M | Name | Blocks on Stefano? | Artifact |
|---|---|---|---|
| M0 | Repo skeleton | no | tree + CI + `CLAUDE.md` |
| M1 | Frequency rules | no | `freqrules.py` |
| M2 | Synthetic generator | no | `tools/vor_gen.py` |
| M3 | Offline DSP | no | `dsp.py`, `tools/vor_replay.py` |
| M4 | Bearing + quality | no | `bearing.py`, `quality.py`, `tools/vor_sweep.py` |
| M5 | Live SDR capture | **hardware** | `sdr.py`, first-light log |
| M6 | Controller + config | no | `controller.py`, `config.py`, schema |
| M7 | Bridge I/O | **Q1–Q3** | `bridge_client.py`, `publisher.py` |
| M8 | Packaging | no | ZIP, `install.sh`, systemd, README |
| M9 | Recording + cloud schema | no | `record.py`, schema note |
| M10 | Calibration + field campaign | **aircraft/site** | test report, calibrated config |

**Open questions gating M7** (from doc A §17, restated):

| Q | Question | Default if unanswered |
|---|---|---|
| Q1 | `vorsignal` 0–100 or 0/1? | build 0–100 internally + `publish.signal_scale` mapping, default `percent` |
| Q2 | `vordir` = radial FROM or bearing TO? | radial FROM, magnetic |
| Q3 | Publish `periodic` @1 Hz or `on_change`+keepalive? | `periodic`, 1.0 s |

M0–M6 proceed regardless. Do not stall the plan on these.

---

## M0 — Repo skeleton

**Do**

1. Create the tree of doc A §12 with every file present as a stub carrying its docstring and its section reference into doc A.
2. `requirements.txt`: `numpy>=1.26`, `scipy>=1.11`, `SoapySDR` (system pkg, note it), `websockets>=12`, `aiohttp>=3.9`, `jsonschema>=4`.
3. `pyproject.toml`, `ruff` + `pytest` config, `pytest -q` green on zero tests.
4. GitHub Actions: `ruff check`, `pytest`, on push and PR. No SDR hardware in CI — everything must be file-driven.
5. `CLAUDE.md` with the running decision log; **first entries = amendments A1–A3 above**, plus the C/C++ decision and its rationale.
6. `docs/rb-vor-architecture.md` = doc A, committed as the baseline.

**Done when:** clean clone → `pip install -r requirements.txt && pytest` succeeds; CI green.

---

## M1 — Frequency rules

**Do**

1. `rbvor/freqrules.py`:
   ```python
   def channel_khz(mhz: float) -> int          # quantise, ±1 kHz float tolerance
   def is_vor_frequency(mhz: float) -> tuple[bool, str]   # (valid, reason)
   def classify(mhz: float) -> Literal["VOR","LOC","OFF_GRID","OUT_OF_BAND"]
   ```
   Rules R1–R5 of doc A §5. Integer kHz arithmetic only — no float comparisons anywhere.
2. Allowlist/denylist applied last, from config, passed in as an argument (module stays pure, no config import).
3. `tests/test_freqrules.py`: all 200 channels 108.000–117.950 step 50 kHz, expected class computed independently in the test from the R3/R4 rule stated as a literal table (not by calling the implementation), plus: 107.95, 118.00, 109.425, 109.4000001, NaN, None, negative, string.

**Done when:** T1 passes, 200/200. `109.4 → VOR`, `109.5 → LOC`, `108.05 → VOR`, `108.15 → LOC`.

**Note:** this module is a published platform primitive. Mirror it into RB-01/RB-02 later rather than re-deriving the rule.

---

## M2 — Synthetic VOR generator

The single most important tool in the project. Everything downstream is validated against it.

**Do**

1. `tools/vor_gen.py` — emit `.cs8` (int8 I/Q interleaved) or `.cf32`:

   | Arg | Default | Meaning |
   |---|---|---|
   | `--radial` | required | commanded radial, degrees |
   | `--type` | `cvor` | `cvor` \| `dvor` (swaps tone roles, reverses sense) |
   | `--snr-db` | 20 | AWGN in the 12 kHz channel |
   | `--seconds` | 30 | duration |
   | `--fs` | 1024000 | sample rate |
   | `--offset` | -256000 | carrier offset from centre |
   | `--am-depth` | 0.30 | 30 Hz AM depth |
   | `--sub-depth` | 0.30 | 9960 Hz subcarrier depth |
   | `--deviation` | 480 | subcarrier FM deviation, Hz |
   | `--ident` | `TST` | Morse ident, `--no-ident` to disable |
   | `--drift-hz` | 0 | carrier offset error, tests tuning tolerance |
   | `--fade` | none | `none` \| `slow` \| `flutter`, amplitude fading model |

2. Signal model, `cvor`:
   ```
   s(t) = A·[1 + m_am·cos(2π·30·t + φ_radial)
              + m_sub·cos(2π·9960·t + (Δf/30)·sin(2π·30·t))
              + m_id·ident(t)·cos(2π·1020·t)] · exp(j2π·f_off·t) + n(t)
   ```
   `dvor`: move `φ_radial` onto the subcarrier FM term, put the fixed phase on the AM term, negate the sense.
3. `tools/vor_spectrum.py` — quick PSD + baseband spectrum plot of any IQ file. Used to eyeball that 30 Hz, 1020 Hz and 9960±480 are where they should be.

**Done when:** a generated file's spectrum shows carrier, 30 Hz, 1020 Hz and the 9960 Hz subcarrier with visible ±480 Hz FM sidebands; screenshot committed to `docs/`.

**Do not proceed to M3 until M2 is trusted.** A bug here is indistinguishable from a bug in the demodulator.

---

## M3 — Offline DSP chain

**Do**

1. `rbvor/dsp.py` — block-processing class, no I/O, no config import, state in a dataclass:
   ```python
   class VorDemod:
       def __init__(self, params: DspParams) -> None
       def push(self, iq: np.ndarray) -> list[Observation]   # 0 or more per block
       def reset(self) -> None                               # on retune
   ```
   `Observation` = `(t, phi_a, phi_b, amp_a, amp_b, carrier, sub_level, noise_est, overrun)`.
2. Stages exactly as doc A §7.2, with A1 applied:
   - fs/4 down-mix via the ±1/±j sequence
   - channel FIR 12 kHz, ÷32 polyphase → 32 kHz
   - AGC by carrier magnitude, τ 0.5 s
   - AM envelope, DC split → `carrier`, `audio`
   - **Path A (corrected by A9):** LPF 700 @ 32 kHz → ÷8 → 4 kHz → LPF 100 → ÷4 → 1 kHz, so both paths traverse identical stages at identical rates and the group delays cancel
   - **Path B (corrected by A9):** mix −9960 → LPF 700 @ 32 kHz → ÷8 → 4 kHz → conjugate-product discriminator → **the identical 100 Hz LPF design as Path A** → ÷4 → 1 kHz. Demodulate at 4 kHz, *never* at 1 kHz: the subcarrier occupies ±600 Hz and doc A's ordering collapses at the legal deviation limit. The pre-mix band-pass is dropped as redundant.
   - 1000-sample (= 30 whole cycles) complex Goertzel at 30 Hz on both, hop 1 s
   - noise estimate from the 25/27/33/35 Hz bins
3. Filter designs generated once at construction and asserted symmetric/linear-phase where claimed. Unit-test that Path A and Path B share the same 100 Hz coefficient array object.
4. Discard any window whose block sequence has a gap (`overrun` flag set) — never decode across a discontinuity (F9).
5. `tools/vor_replay.py`: file → `VorDemod` → CSV of observations. Same code path the live daemon uses.

**Done when:** `vor_replay.py` on a `--radial 137 --snr-db 30` file yields a stable phase difference; run-to-run spread < 0.2°. Absolute correctness is **not** claimed at M3 — the sign and offset are M4's job.

---

## M4 — Bearing, quality, calibration

**Do**

1. `rbvor/bearing.py`:
   ```python
   def radial(obs, phase_sign: int, phase_offset_deg: float, station_cal_deg: float) -> float
   def circular_mean(bearings: Sequence[float]) -> float      # vector mean, never linear
   def circular_std(bearings: Sequence[float]) -> float
   ```
   Raise if `phase_sign` or `phase_offset_deg` is `None` (A2) — the daemon must not publish an uncalibrated bearing.
2. `rbvor/quality.py`: the four terms `q_am`, `q_sub`, `q_snr`, `q_stab` of doc A §9, each returned **individually** alongside the composite. Composite clamped 0–100, forced to 0 below `flag_threshold`.
3. `tools/vor_calibrate.py`:
   - generate `--radial 0` and `--radial 90` CVOR files at 40 dB SNR
   - decode, solve for `phase_sign` and `phase_offset_deg`
   - re-verify on `--radial 213`
   - write the two constants into a config fragment and print them
   The measured `phase_offset_deg` at this stage is **filter group delay only**. `station_cal_deg` stays 0 until M10.
4. `tools/vor_sweep.py`: 0–359° in 1° steps × SNR list → error table, RMS/peak, plus a `vorsignal` vs SNR curve. Emits Markdown + CSV into `docs/`.
5. `tests/test_dsp_synthetic.py`: T2 (≤0.5° RMS, ≤1.0° peak @30 dB), T3 (≤2° @10 dB, `vorsignal` monotonic in SNR, flag asserts below usable), T4 (DVOR identical with `phase_sign` flipped).

**Done when:** T2/T3/T4 green in CI; sweep report committed; `CLAUDE.md` records the measured `phase_sign` and `phase_offset_deg` with the date and the generator version used.

---

## M5 — Live SDR

Needs the dongle, the FM band-stop filter and an antenna. Everything before this is bench-free.

**Do**

1. `rbvor/sdr.py`:
   - SoapySDR device open from `device_args`; on failure fall back to `pyrtlsdr` behind the same interface
   - dedicated reader thread, `readStream` into a preallocated ring of `buffer_blocks` × `block_samples`
   - int8 → complex64 via a 256-entry LUT
   - overrun counter surfaced to the DSP (F9)
   - `set_frequency(hz)` applies `tuner_offset_hz`, flushes the ring, returns after `settle_ms`
   - `close()` releases the device cleanly on SIGTERM
2. `tools/vor_capture.py`: capture N seconds of live IQ to file at a given frequency. This is the field-debug tool and the bug-report attachment format.
3. First light: capture a real station, run `vor_replay.py`, compare against the GPS-derived magnetic bearing.

**Done when:** a live capture decodes through the *same* `dsp.py` path as a synthetic file, with a documented bearing error. Record the station, position, date, dongle, gain and filter configuration in `docs/first-light.md`.

**Also at M5:** run T9 (band-stop in/out) and record the desensitisation figure. Do it once, early — it determines whether item 3 of the BOM (airband LNA/filter) is needed.

---

## M6 — Controller, config, state machine

**Do**

1. `rbvor/config.py` + `schema/rb-vor.schema.json` — full schema per doc A §11 with A1/A2 applied. Refuse to start on a schema failure, with the offending path in the error. No default is duplicated in code.
2. `rbvor/controller.py` — the state machine of doc A §7.1:
   `IDLE · INVALID_FREQ · TUNING · ACQUIRING · TRACKING · DEGRADED · NO_SIGNAL`
   - 300 ms debounce on `vorfreq`, compared in integer kHz
   - retune sequence: stop → set freq → flush → settle → discard first window → resume, with `vorsignal=0` posted **before** the retune completes (F3)
   - `hold_s` staleness timer (F2)
   - sd_notify watchdog ping each loop
3. `tests/test_statemachine.py`: table-driven, injected clock, fake SDR and fake solution stream. Assert every transition of the doc A §7.1 table, and assert explicitly that no bearing from station A can ever be published after a retune to station B.

**Done when:** state table 100 % covered; retune-contamination test passes.

---

## M7 — Bridge I/O

Blocked on Q1–Q3; implement all branches and select by config.

**Do**

1. `rbvor/bridge_client.py`:
   - `websockets` client, exponential backoff 0.5 → 30 s
   - tolerant parse: unknown fields ignored, absent fields = no change, malformed frame logged and dropped without disconnecting
   - `gpsTrack` captured into the solution log only (doc A §8) — assert in a test that it does not reach `bearing.py`
   - `ws_grace_s` expiry → release SDR → `IDLE`
2. `rbvor/publisher.py`:
   - `aiohttp` POST, `http_timeout_s` 0.8
   - `periodic` and `on_change`+keepalive modes both implemented
   - `publish.signal_scale`: `percent` (0–100) or `boolean` (0/1, threshold `flag_threshold`) — internal metric stays 0–100 regardless (Q1)
   - `publish.bearing_ref`: `from` or `to` (Q2)
   - failure → one immediate retry, then backoff; **drop** stale solutions, never queue unbounded
   - out-of-band immediate POST on every state transition
3. `tools/mock_bridge.py`: WS server + REST sink, scriptable frequency sequences, fault injection (drop connection, 500 response, slow response).
4. `tests/test_interface.py` + `tests/test_bridge_io.py`: T5, T6. Assert the v1.0 payload is **exactly** `{"vordir": int, "vorsignal": int}` with no extra keys.

**Done when:** T5/T6 green against `mock_bridge.py`, including dongle-unplug and bridge-restart scenarios.

---

## M8 — Packaging

**Do**

1. `install.sh`: dependency install, `rb-vor` system user, `plugdev` membership, udev rule for the dongle, config to `/etc/rb-vor/rb-vor.json`, systemd unit enable. **No step requires root at runtime.** Detects RTL-SDR Blog V4 and installs the rtl-sdr-blog `librtlsdr` fork if needed (doc A §17.5).
2. `systemd/rb-vor.service`: `Type=notify`, `Restart=always`, `RestartSec=2`, `WatchdogSec=10`, `After=network-online.target`, hardening (`ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`).
3. `README.md` — builder-facing: BOM with alternates, antenna build (horizontal V-dipole, 670 mm legs), band-stop filter, wiring, install, first-light procedure, troubleshooting table. Hand tools only.
4. `docs/install.md`, `docs/calibration.md`, `docs/RB-VOR-IF-v1.0.md` (§10 of doc A extracted as a standalone published spec — a third party must be able to write a consumer from it alone).
5. Verify on a **clean Raspberry Pi OS image**, following only the README, no prior knowledge.
6. Run **T12** (8 h endurance) and evaluate the native-port gate:

   | Trigger | Threshold | Action if hit |
   |---|---|---|
   | CPU | > 25 % of one core sustained | port the `dsp.py` front end to C99 + liquid-dsp behind the same interface |
   | Overruns | any, in 8 h | same |
   | Dropped windows | > 0.1 % | same |
   | Contention | RB-01 render or `dump1090` degrades | same |

   If no trigger fires, the Python implementation is final for v1.0 and this is recorded in `CLAUDE.md`. The Python version remains the test oracle either way.

**Done when:** ZIP bundle installs and runs unaided on a clean image; endurance clean; port gate decision recorded.

---

## M9 — Recording and cloud

**Do**

1. `rbvor/record.py`: ring-buffered IQ, `trigger: on_flag | manual | always`, `max_seconds`, auto-prune by disk quota.
2. Solution CSV at 1 Hz: `t, channel_khz, state, vordir, vorsignal, q_am, q_sub, q_snr, q_stab, carrier, gpsTrack, overruns`.
3. RB-Cloud schema (`docs/cloud-schema.md`): versioned field table, units, retention period, GDPR note. Position and audio-adjacent data flagged explicitly. Offline buffering, upload on reconnect, integrity hash per record.

**Done when:** flag events auto-capture 30 s of IQ; a captured file replays through `vor_replay.py` and reproduces the flag.

---

## M10 — Calibration and field campaign

**Do**

| Test | Content | Acceptance |
|---|---|---|
| T7 | bench RF: replay the synthetic file through a second SDR / signal generator into the real chain | end-to-end decode |
| T8 | ground, known site (LIQQ), real station ≥ 10 min | ≤ 3° vs GPS-computed magnetic bearing |
| T10 | EMI: RB-VOR on vs off | no measurable COM/NAV degradation |
| T11 | flight: constant-radius orbit, log `gpsTrack` vs error | ≤ 5° across all headings; antenna shadowing characterised |

`station_cal_deg` is set from T8, per installation, and recorded in the installation record — never in the shipped default config.

**Done when:** test report filled from `docs/test-report-template.md`; calibrated config committed as an *example*, clearly marked site-specific.

---

## 2. Cross-cutting rules for every milestone

| Rule |
|---|
| No merge with a failing predecessor milestone. |
| No literal address, threshold, gain, endpoint or filter constant outside `config.py` / the schema. |
| `dsp.py` must always be runnable from a file. Live SDR is one more source, never a special path. |
| Every corrected hypothesis goes into `CLAUDE.md` immediately, with the superseded version struck through — no silent fixes. |
| Missing datasheet, measurement or capture → flag it in `docs/open-items.md`, do not invent a plausible value. |
| Vendor names stay out of code and docs: `SDR`, `NAVAID`, `BRIDGE`. |
| Every builder-facing document assumes hand tools, a soldering iron, a 3D printer, no lab equipment. |
| Interface changes: additive within v1.x; anything else needs v2.0 + a migration note. |

---

## 3. Delivery sequence per working session

| Session | Content |
|---|---|
| 1 | M0 + M1 — skeleton, CI, `freqrules.py`, 200-channel test |
| 2 | M2 — `vor_gen.py`, `vor_spectrum.py`, spectrum verification |
| 3 | M3 — `dsp.py`, `vor_replay.py` |
| 4 | M4 — `bearing.py`, `quality.py`, `vor_calibrate.py`, `vor_sweep.py`, T2/T3/T4 |
| 5 | M6 — `config.py`, schema, `controller.py`, state-machine tests *(runs ahead of M5; no hardware needed)* |
| 6 | M7 — `bridge_client.py`, `publisher.py`, `mock_bridge.py`, T5/T6 |
| 7 | M5 — `sdr.py`, `vor_capture.py`, first light, T9 *(hardware in hand)* |
| 8 | M8 — packaging, docs, clean-image install, T12, port gate |
| 9 | M9 — `record.py`, CSV, cloud schema |
| 10 | M10 — field campaign support, calibration, test report |

Each session ends with a ZIP of the full tree, not a diff.

---

## 4. Backlog, deferred out of v1.0

ILS localiser (90/150 Hz DDM) · glideslope · marker beacons · Morse ident decode (`vorident`, v1.1) · navaid database with TO/FROM, CDI and the GPS cross-check monitor (the real mitigation for F1) · multi-channel decode within one 2.4 MHz capture · dual-VOR position fix · **two-dongle requirement for simultaneous 1090 MHz ADS-B — document now, it constrains RB-01/RB-07 hardware**.
