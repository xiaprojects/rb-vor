# RB-VOR

Software VOR receiver for RB-Avionics. An RTL-SDR dongle and a Raspberry Pi
decode a VHF Omnidirectional Range station and publish the radial into the
RB-Avionics bridge.

> **Supplemental / non-required equipment.** Day VFR situational awareness only.
> Not a certified NAV receiver. Not for IFR navigation or approach guidance.
> Independent primary navigation is assumed present. `vordir` is meaningless
> whenever `vorsignal` is 0 — consumers must honour that.

Part of the RB-Avionics platform · `xiaprojects/rb-vor` · https://www.rbavionics.com

---

## Quick start on a Raspberry Pi

```bash
git clone https://github.com/xiaprojects/rb-vor && cd rb-vor
sudo ./install.sh
cd /opt/rb-vor
python3 -m rbvor --self-test                      # no hardware needed
python3 tools/vor_calibrate.py --json /tmp/bearing.json
# merge the "bearing" block into /etc/rb-vor/rb-vor.json
python3 -m rbvor --config /etc/rb-vor/rb-vor.json --freq 109.4 --dry-run
```

**Running on a real Pi: [`RUN.md`](RUN.md)** — copy-paste path from a bare
image to a decoded 109.40 MHz bearing.
**Full bench procedure with expected output at every step:
[`docs/bench-test.md`](docs/bench-test.md).**

## Status — v0.9.0 bench release

| Milestone | State |
|---|---|
| M0 repo skeleton, CI | done |
| M1 `freqrules.py` + 200-channel test | done, 260 tests green |
| M2 `vor_gen.py` + `vor_spectrum.py` | done, 297 tests green |
| M3 `dsp.py` + `vor_replay.py` | done, 335 tests green |
| M4 bearing, quality, calibration | done, 380 tests green |
| M5 `sdr.py` live reader + self-test + bench guide | done, 501 tests green |
| M6 `config.py` + schema + `controller.py` | done, 424 tests green |
| M7 `bridge_client.py` + `publisher.py` + `mock_bridge.py` | done, 468 tests green |
| M8 packaging, systemd, installer, IF spec | done |
| M9 recording + cloud schema | done |
| Interface v2.0 (`vorradial`/`vorsnr`/`voram`, transmit-on-change) | done, 523 tests green (510 + 13 SDR) |
| M10 field campaign | **in progress — LIRZ orbit confirmed the bearing to 1.4°** |

Everything except the field campaign is implemented. The interface is frozen at
RB-VOR-IF v1.0. See [`CHANGELOG.md`](CHANGELOG.md) for measured performance and
known limitations.

> **A fresh install publishes no bearing at all**, only `vorsignal: 0`, until the
> calibration is measured. That is deliberate — an uncalibrated bearing looks
> plausible and is wrong, which is the worst failure this system has.

---

## What you need

| # | Item | Primary | Alternate | Note |
|---|---|---|---|---|
| 1 | SDR dongle | RTL-SDR Blog V4 (1 ppm TCXO) | Nooelec NESDR SMArTee v2; generic RTL2832U + R820T2 | V4 needs the rtl-sdr-blog `librtlsdr` fork |
| 2 | FM broadcast band-stop, 88–108 MHz | RTL-SDR Blog broadcast-FM block | Nooelec Flamingo FM+ | **mandatory** — 108 MHz sits right above the FM band |
| 3 | Antenna | horizontal V-dipole, 670 mm per leg | telescopic dipole kit | **horizontal** — VOR is horizontally polarised |
| 4 | Coax | RG-316 SMA–SMA 1 m | RG-58 + BNC adapter | keep it short |
| 5 | Ferrites | clip-on 5 mm × 4 | — | one per USB and coax end |
| 6 | USB isolator | USB 2.0 galvanic isolator | ferrite on the USB lead | optional, against Pi switching noise |
| 7 | Host | Raspberry Pi 5, Raspberry Pi OS 64-bit | any Debian 12+ x86_64 | |

Hand tools, a soldering iron and a 3D printer. No lab equipment.

---

## Install

```bash
git clone https://github.com/xiaprojects/rb-vor && cd rb-vor
sudo ./install.sh
```

Creates an unprivileged `rb-vor` user, installs udev rules so the dongle works
without root, blacklists the DVB-T kernel driver, validates the config, and
enables a `Type=notify` systemd unit with a watchdog. It does **not** start the
service, because a fresh install is uncalibrated and would publish only flags —
see `docs/install.md` §3 and `docs/calibration.md`.

Development, without installing:

```bash
pip install -r requirements.txt && pytest
```

---

## Make a capture

```bash
tools/vor_capture.py -f 109.4 -t 60 \
    --site "LIQQ apron" --station PRU --lat 43.0959 --lon 12.5132 \
    --expected-radial 137 --antenna "V-dipole horizontal 670 mm" \
    --filter-chain "FM bandstop" --notes "car roof, engine off"
```

Writes `captures/capture_<utc>_<channel>.cu8` plus a `.json` sidecar holding
sample rate, tuner offset, gain, position, expected radial and antenna — every
field needed to replay it. **Capture files are not committed; sidecars are.**

Backends are tried in order: SoapySDR → pyrtlsdr → `rtl_sdr(1)`. The tool
refuses non-VOR channels unless you pass `--allow-non-vor`.

---

## Frequency rules

`rbvor/freqrules.py` is a published platform primitive — reuse it in RB-01/RB-02
rather than re-deriving the rule.

| Band | Rule |
|---|---|
| 108.000–111.950 | VOR only if the 100 kHz digit is **even**. Odd = ILS localiser, rejected. |
| 112.000–117.950 | every 50 kHz channel is VOR |

`109.40 → VOR` · `109.50 → localiser, rejected` · `108.05 → VOR` ·
`108.15 → rejected` · `117.975 → off-grid` · `118.00 → out of band`

All comparisons are in integer kHz. Never compare navaid frequencies as floats.

---

## Interface — RB-VOR-IF v1.0

Consumes, over `ws://localhost/bridge`:

| Field | Unit | Use |
|---|---|---|
| `vorfreq` | MHz | tuning command |
| `gpsTrack` | ° true | diagnostics/log only in v1.0 |

Produces, `POST http://localhost/bridgeUpdate`, 1 Hz:

```json
{ "vordir": 130, "vorsignal": 0 }
```

| Field | Unit | Range | Meaning |
|---|---|---|---|
| `vordir` | ° magnetic | 0–359 | radial **FROM** the station |
| `vorsignal` | — | 0–100 | 0 = NAV flag, `vordir` invalid |

Full spec: `docs/rb-vor-architecture.md` §10. Additive fields only within v1.x.

---

## Layout

```
rbvor/        package — freqrules and record implemented, rest stubbed by milestone
tools/        vor_capture (M5, ready) · vor_gen, vor_replay, vor_sweep, vor_calibrate (M2–M4)
tests/        file-driven only; CI has no SDR hardware
docs/         architecture (doc A), implementation plan, open items, first-light log
config/       default rb-vor.json
CLAUDE.md     decisions, amendments, corrected hypotheses
```

## Contributing

Corrections are propagated immediately into every affected artifact and recorded
in `CLAUDE.md` with the superseded version struck through. Missing datasheet,
measurement or capture → an entry in `docs/open-items.md`, never a plausible
invented value.
