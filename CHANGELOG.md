## Unreleased — ILS localizer, phase A, shipped disabled (CLAUDE.md A33–A38, D9) — 2026-11-10

- **ILS localizer decoding**, off by default (`"loc": {"enabled": false}`).
  Enabled, a localizer channel posted to `vorfreq` is tuned and decoded instead
  of being rejected, and the DDM is published. Measured on synthetic signals
  only (`docs/loc-sweep.md`); no real localizer has been decoded, so it stays
  off until one has (plan L7, open item O23). Supplemental, not approach
  guidance; a back course reads reversed.
- **RB-VOR-IF v2.3** (`docs/RB-VOR-IF-v2.3.md`): six `loc*` fields —
  `locstatus`, `locdev` (% of full scale, + = fly left, **0 unless
  `locstatus >= 4`**), `locddm`, `locsdm`, `locsnr`, `locfreqactive` — on
  every payload of a receiver with `loc.enabled`, and on none without it, whose
  wire stays byte-for-byte v2.2. On a localizer channel every `vor*` field is 0,
  so a v2.2 plate shows OFF, never a radial. The v2.3 text also corrects v2.2's
  description of zeroed bearing fields, which the code stopped doing in 1.0.0.
- **Noise never reads as an approach.** Noise decodes as DDM ≈ 0, a centred
  needle, so validity comes from tone SNR, sum of depths and a steady tone
  frequency, never from the DDM. 0 valid windows in 1100 noise-only windows
  across five levels and four seeds (`docs/loc-sweep.md`), and no usable status
  through the full state machine in six 20 s noise runs (`tests/test_loc_controller.py`).
- **Tone frequencies anywhere in the legal ±2.5 % decode**: a search, not a
  fixed bin. A fixed bin 1 % off nominal points the needle the wrong way.
- **Weak signals no longer under-read the deviation**: the DDM is corrected
  for the measured envelope compression, but never scaled down (D9).
- New: `rbvor/loc.py`, `tools/loc_gen.py` (same `LocParams` /
  `generate_blocks` shape as `vor_gen`), `tools/loc_sweep.py`;
  `vor_replay.py` decodes a localizer capture from its sidecar frequency;
  `vor_capture.py` accepts localizer channels; the self-test has a localizer
  stage; `mock_bridge.py` validates v2.3.
- `freqrules`: `is_loc_frequency()`, `loc_channels()`, and `classify_navaid()`
  with a `DENIED` class, so a denylisted VOR channel can never be tuned as a
  localizer. `is_vor_frequency()` and `classify()` behave exactly as before;
  the reason string for a denied channel no longer claims an odd digit.
- With the localizer enabled, a clean stop posts an idle payload, so
  `locstatus` 0 is the last thing on the bridge. A killed daemon still leaves
  its last values (O25).
- Localizer solutions go to their own CSV (`loc.csv` beside the VOR one):
  the solution logger appends under whatever header an existing file has.
- VOR path unchanged: every existing test passes unmodified, and the real
  capture replays identically (bit-level fingerprint of every observation).

## Unreleased — a shared dongle and a restart are no longer failures (CLAUDE.md A30, A31) — 2026-11-06

- **A dongle that cannot be had no longer stops the daemon.** With the channel
  from the bridge, being refused the SDR (`usb_claim_interface error -6`, usually
  because rb-sdr-radio holds it) used to exit 3, and under `Restart=always` the
  daemon crash-looped for as long as the radio held the dongle, with the plate
  stuck on TUNING. It now stays up in the new `NO_SDR` state, with the channel
  still selected and `vorstatus` 1, tries again every `sdr.retry_s` and takes the
  dongle by itself when it comes free. A dongle missing when the channel is
  selected behaves the same. One pulled during reception is not covered.
- **New optional `sdr.retry_s`**, default 5. A config without it still loads.
- **Bench mode is unchanged:** with `--freq`, nothing will free the dongle, so
  it is still exit 3 (F3).
- **The daemon reads the bridge on connect.** The socket says nothing until a value
  changes, so a restarted daemon could sit OFF with `vorfreq` on the bridge, and
  pressing that station again changed nothing. It now does a `GET` of
  `bridge.rest_url` after every connection and returns to that channel. This
  also ends the case of a bridge lost for longer than `ws_grace_s`.
- `tools/mock_bridge.py` answers the read.

## Unreleased — bridge link no longer pings RB-01 (CLAUDE.md A29) — 2026-10-31

- **The WebSocket keepalive ping is off.** RB-01 has no ping/pong, so the
  library's default keepalive dropped a healthy bridge link every ~50 s
  (`sent 1011 (internal error) keepalive ping timeout; no close frame
  received`) and delivered no `vorfreq` for the last 10 s of each cycle. The
  link is now lost only when the socket is: closed by the peer, or declared
  dead by kernel TCP keepalive (~25 s), which RB-01's kernel answers without
  RB-01 being involved.
- Stopping the daemon no longer waits 10 s for a close frame RB-01 never
  sends (0.5 s now).

## Unreleased — first flight log and what it found (CLAUDE.md F6, A27) — 2026-09-29

- **`sdr.device_serial` / `sdr.device_index`.** rb-vor always opened RTL-SDR
  index 0 and had no way to choose; `device_args` was read only by the
  SoapySDR backend, which is not installed on the Pi. With ADS-B in the same
  box this means the receiver can end up on the 1090 MHz antenna. Both
  backends and `tools/vor_capture.py` (`--device-serial`, `--device-index`)
  now honour the selection, an absent serial is a startup error naming the
  serials present, and several dongles with none pinned logs a warning. The
  capture sidecar records the device instead of hard-coding `index=0`.
- **Gain is snapped to the fitted tuner's ladder** and logged when it has to
  be (`snap_gain`), and the tuner type is logged at open. The aircraft's VOR
  dongle is an **FC0012**: five steps, max 19.2 dB — so the 14.4 that flew was
  silently rounded to 17.9, and a naive "raise it to 32.8" would have been
  clamped to 19.2. `sdr.gain_db` now ships **19.2**, a real step of the tuner
  actually fitted (A28). Starting point, not a measurement (O19).
- **`sdr.tuner_profiles`**: per-tuner overrides of `swap_iq`, `gain_db` and
  `ppm`, applied when the device reports its tuner, with the `sdr` block as
  fallback. One config now serves an FC0012 and an R820T without edits. Ships
  with gain profiles for both; `swap_iq` is deliberately left unset (O21).
- **`vor_replay.py --auto-swap`**: decodes a capture both spectral senses in
  one pass and prints which one works, so "does this dongle need `swap_iq`" is
  a measurement. Declines to choose when neither sense decodes.
- **Note for anyone on an FC0012**: every spectral convention in this project
  (A22, D5, `swap_iq: false`) was measured on an R820T, which librtlsdr runs
  in low-IF *with spectrum inversion*; the FC0012 is zero-IF. If your survey
  says `MIRROR DETECTED`, set `swap_iq: true` for that dongle (O21).
- A librtlsdr/pyrtlsdr version mismatch now raises a clean `SdrError` pointing
  at RUN.md §7 instead of an unhandled `AttributeError` from ctypes: `rtlsdr`
  binds the library at import time, so the mismatch is not an `ImportError`
  and escaped the handler (O7).
- `docs/first-light.md` has its first entry; `docs/open-items.md` gains
  O18–O20.

## Unreleased — reconciliation with 1.0.0 (CLAUDE.md A26, D8) — 2026-09-13

- The 2026-09-11/12 work below had been done on a stale 0.9.0 / v2.1 copy;
  the 1.0.0 (v2.2) publish-path features are now merged back (A26): payload
  never zeroed, radial published while acquiring, `vorstatus` 4 = degraded /
  5 = tracking, `vorfreqactive`, `--csv` per-window dump, retune ordering.
- Shipped config = the committed flight config (calibration +1 / +2.12, bridge
  192.168.10.1, gain 14.4) with `swap_iq: false` (D5) and the A25/D6 DSP
  settings. **A 1.0.0 config with `swap_iq: true` must be changed to `false`
  for this build**; the daemon warns at startup.
- The self-test judges its synthetic signal against the synthetic constant and
  reports the flight constant's excess as the station offset.

## Unreleased — fixes from the 2026-09-11 review (CLAUDE.md A20–A24, F1–F4)

- **Offset convention corrected (A22).** `dsp.py` mixed by `-tuner_offset_hz`
  and `vor_gen.py` placed the carrier at `+tuner_offset_hz`; both agreed with
  each other and disagreed with every real capture, which is why `swap_iq` was
  needed on a standard RTL-SDR. The carrier now sits at `-tuner_offset_hz`
  (+256 kHz for the default −256 kHz offset), where a tuner parked at
  `nav + offset` actually puts it. **Migration:** a field config that used
  `swap_iq: true` with `tuner_offset_hz: -256000` must set `swap_iq: false`;
  synthetic files generated before this change lack `synthetic.carrier_at` in
  their sidecar, are reported by `vor_replay.py`, and decode with `--swap-iq`.
  `vor_replay.py --survey` now expects the carrier at `-tuner_offset_hz` and
  reports the `sdr.ppm` value with the correct sign.
- **Daemon no longer idles headless when the SDR cannot be opened (F3).** An
  exception in any daemon task stops the loop and is re-raised; `SdrError`
  yields exit code 3 as documented. Previously the ticker task died silently
  and the process ran for ever reporting `blocks_read: 0`.
- **Sample pump can no longer starve the event loop under overload (F5).**
  `SdrReader.blocks()` yields at most what was queued when called; a reader
  outrunning the demodulator used to trap `pump()` in its loop, stopping the
  ticker, the state machine and the watchdog feed. Startup also warns when
  `swap_iq` is set (a standard RTL-SDR must not need it after A22).
- **Dead `publish` keys retired (A20).** `mode`, `period_s`, `deadband_deg`
  and `signal_scale` have done nothing since interface v2.0; they are no longer
  required or shipped, are accepted with a warning, and `keepalive_s` may now be
  0 as the v2.x spec says.
- Flag-triggered IQ captures now carry the tuned channel in their file name and
  sidecar instead of `nav_freq_hz: 0`.
- `BearingTracker` no longer raises when the smoothing window cancels exactly.
- Startup warns when `tuner_offset_hz` is 0 (carrier on the DC spur, F2).
- Docstrings and user docs (README, RUN.md, bench-test, install, template)
  moved from v1.0 to v2.1 vocabulary; `.vscode/sftp.json` is git-ignored.
- **Hot path 3.5× cheaper (D6).** The polyphase decimator no longer promotes
  its window matrix to complex128 or gathers an 8× copy of the input: one
  matrix product in working precision plus a diagonal sum. cu8 → complex64 is
  a zero-copy view over a LUT built once, shared by replay and the pyrtlsdr
  backend. Output identical to float32 rounding. `sdr.block_samples` default
  32768 → 65536 (measured: no gain beyond). Full chain measured at 1.6 % of
  one desktop core, 62× realtime.
- **Signal chain (A25).** Channel filter 12 kHz/255 taps → 13 kHz/511 taps
  (was −2.5 dB at the 9960 Hz subcarrier, asymmetric across its sidebands);
  sub LPF 127 → 255 taps; `dsp.integration_s` 1 → 2 s at 1 s hop; AFC
  (`dsp.afc_threshold_hz`, default 300 Hz, 0 disables) keeps an off-frequency
  carrier inside the passband and reports `carrier_offset_hz`. Bearing RMS
  error roughly halved at every SNR (`docs/sweep.md`); NAV flag thresholds
  unchanged. Calibration constant unchanged (−1.9663 vs −1.9657).
- **Harmonic reference-tone estimator, opt-in (D7).** `dsp.fm_estimator:
  "harmonic"` replaces the FM discriminator with a maximum-likelihood phase
  estimate of the known 30 Hz modulation (Bessel-weighted DFT bins). No FM
  threshold: bearing RMS 1.63° → 0.78° at 3 dB, 9.8° → 2.3° at −3 dB; real
  captures 0.8–0.9° → 0.4°. Acquires and reports the station's subcarrier
  offset (`sub_offset_hz`) and a coherent subcarrier level (`sub_coh`).
  Different calibration constant: run `vor_calibrate.py --fm-estimator
  harmonic`. Default remains `"discriminator"` pending a live-dongle run.
- **Flag-triggered IQ capture no longer stalls the receiver (D6).** The ring is
  held quantised (61 MB for 30 s instead of 246 MB) and written by a background
  thread; `trigger()` returns in under a millisecond where it used to block
  the sample pump for seconds and cause the overrun it was recording.
  `RingRecorder.flush()` waits for a write; a trigger during a write is
  skipped and counted in `captures_skipped`.

## Unreleased — interface v2.1

- **SDR release**: the dongle is now released when the receiver goes to IDLE / INVALID_FREQ (off, no frequency, or out-of-band), and reopens on the next retune.
- **New payload field `vorstatus`** (0=idle/off, 1=no-signal, 2=tuning, 3=acquiring, 4=tracking).
- **New payload field `vorsub`** (9960 Hz subcarrier level x100).
- Both new fields are additive; a v2.0 consumer is unaffected. See `docs/RB-VOR-IF-v2.1.md`.

## Unreleased — interface v2.0

**Breaking change** to the published bridge interface. See `docs/RB-VOR-IF-v2.0.md`.

- `vordir` renamed to `vorradial`.
- `vorsignal` (synthetic 0-100 quality) replaced by `vorsnr` (measured SNR, dB) and `voram` (measured AM depth x100).
- Out-of-airband or invalid solution now publishes all three fields as 0.
- Publish policy is now always transmit-on-change (was periodic/on_change, config-selectable); an optional keep-alive still applies.
- Migration note in `docs/RB-VOR-IF-v2.0.md` section 6.

# RB-VOR changelog

## 0.9.0 — 2026-07-24 — bench release

First release intended to be run on hardware. Everything but the field campaign
is implemented; the interface is frozen at RB-VOR-IF v1.0.

**Start here:** `docs/bench-test.md` — a worked bench procedure at 109.40 MHz.

### Implemented

| Milestone | Content |
|---|---|
| M0–M1 | repo, CI, channel rules (`freqrules`), 200-channel test |
| M2 | synthetic VOR generator and spectrum tool |
| M3 | production DSP chain, replay tool |
| M4 | bearing, quality metric, calibration and sweep tools |
| M5 | live SDR reader (SoapySDR / pyrtlsdr), threaded, bounded queue |
| M6 | configuration + JSON Schema, state machine |
| M7 | WebSocket consumer, REST producer, mock bridge |
| M8 | installer, systemd unit with watchdog, udev, published interface spec |
| M9 | per-solution CSV log, RB-Cloud record schema, flag-triggered IQ capture |

525 tests, all file-driven or synthetic; CI needs no radio.

### Measured

| Metric | Result |
|---|---|
| Bearing accuracy, 30 dB SNR | 0.04° RMS |
| Bearing accuracy, 10 dB SNR | 0.47° RMS |
| NAV flag asserts | before error reaches 5° |
| CPU, 1.024 Msps, full chain | 3.6 % of one core |
| Dropped windows, 300 s | 0 |

### Corrections to the design documents

Seventeen amendments are recorded in `CLAUDE.md`. The ones that changed
behaviour rather than wording:

- **A4** RTL-SDR delivers `cu8`, not `cs8`; the wrong label injects a DC offset
  onto the carrier and corrupts every bearing.
- **A7** subcarrier level must be measured as band power. With β = 16 the bin at
  9960 Hz holds ~3 % of the power, so a narrowband detector reads "no
  subcarrier" on a healthy station.
- **A9** the FM subcarrier must be demodulated at 4 kHz, not 1 kHz. The original
  ordering collapses at a deviation ratio of 17.0 — inside the legal ICAO
  tolerance.
- **A11** the quality metric was rebuilt from measurement. The original SNR
  mapping saturated across the entire useful range, and the flag thresholds let
  a 17.6° error through unflagged.
- **A15** `q_stab` was judging the smoothed bearing, hiding the jitter it exists
  to detect.
- **A16** a repeated identical frequency restarted the retune, so a bridge that
  re-asserts its state — the normal design — would prevent acquisition forever.

### Known limitations

- **Bearing calibration is not shipped.** `phase_sign` and `phase_offset_deg`
  are `null` and the daemon publishes only flags until they are measured. This
  is deliberate (A2): an uncalibrated bearing looks plausible and is wrong.
- **Open item O1**: the calibration is self-consistent but not yet confirmed
  against a real station. `docs/bench-test.md` stage 7 closes it.
- **Open items O3/O4**: the `vorsignal` scale (0–100 vs 0/1) and `vordir` sense
  (from vs to) expected by the existing bridge are unconfirmed. Both branches
  are implemented and selected by configuration.
- **Open item O12**: the installer is verified by construction and by tests, but
  has not yet been run on a clean Raspberry Pi OS image.
- No ILS localiser, glideslope, marker beacon or Morse ident decode (backlog).
- One dongle decodes one channel. Simultaneous 1090 MHz ADS-B needs a second.

### Safety

Supplemental / non-required equipment. Day VFR situational awareness only. Not a
certified NAV receiver, not for IFR navigation or approach guidance. Independent
primary navigation is assumed present. `vordir` is invalid whenever
`vorsignal` is 0, in every state, always.
