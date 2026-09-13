# RB-VOR — running it on a real Raspberry Pi

This is the short path from a bare Pi to a decoded VOR bearing at **109.40 MHz**.
Every command is meant to be copied as-is. The long version, with expected
output at every step and the failure table, is `docs/bench-test.md`.

> **Supplemental / non-required equipment. Day VFR only.** Not a certified NAV
> receiver, not for IFR or approach guidance. `vordir` is meaningless whenever
> `vorsignal` is 0.

---

## Dumping raw decode data (--csv)

`--csv FILE` writes one row per demodulated window to a CSV, the same data
`vor_replay.py` produces off a capture, plus a `frequency` column (tuned channel
in kHz):

```
t,frequency,delta_phi_deg,phi_a_deg,phi_b_deg,amp_a,amp_b,sub_level,carrier,noise_est,snr_db,overrun
1.500,109400,138.79,-59.28,161.93,0.32054,479.08,0.30671,0.404583,0.002191,43.30,0
```

Every window is written, including overrun and warming ones, so the file is a
faithful record of what the receiver saw. The file is flushed per row, so a
killed run still leaves a complete file. Combine with `--dry-run` for a bench
capture-to-CSV without touching the bridge:

```
rbvor --config config/rb-vor.json --freq 109.4 --dry-run --csv drive.csv
```

## What you need

| | Item | Note |
|---|---|---|
| Host | Raspberry Pi 5, Raspberry Pi OS 64-bit (or any Debian 12+) | |
| SDR | RTL-SDR dongle | RTL-SDR Blog V4 needs its own driver — see §7 |
| Filter | FM broadcast band-stop, 88–108 MHz | **mandatory**, fits between antenna and dongle |
| Antenna | horizontal V-dipole, ~670 mm per leg | **horizontal** — VOR is horizontally polarised |

A station must actually be transmitting on your chosen frequency within range.
**109.40 MHz is a valid VOR channel, but check your aeronautical chart for a
real station on it near you.** If there is none, step 4 lets you test with a
synthetic signal instead, and you substitute your local station's frequency
everywhere below.

---

## 1. Install

```bash
git clone https://github.com/xiaprojects/rb-vor
cd rb-vor
sudo ./install.sh
```

This installs packages, blacklists the DVB-T kernel driver, creates an
unprivileged `rb-vor` user, installs udev rules so the dongle works without
root, validates the config, and enables a systemd service — **without starting
it**, because a fresh install has no calibration and would publish only flags.

If it warns about an RTL-SDR Blog V4 with the stock driver, fix that first (§7).

---

## 2. Prove the software works — no hardware needed

```bash
cd /opt/rb-vor
python3 -m rbvor --self-test
```

Every line should read `PASS`, with a single `WARN` about the missing
calibration. **If this fails, stop and fix it before touching the antenna** —
the failing line names the stage. If it passes, the software is sound and any
later problem is RF, antenna, or calibration.

---

## 3. Calibrate

```bash
python3 tools/vor_calibrate.py --json /tmp/bearing.json
```

Expect a decisive sign decision (margin near 180°) and `phase_sign +1,
phase_offset_deg -1.9657`. Merge it into the live config:

```bash
sudo python3 - <<'PY'
import json, pathlib
cfg = pathlib.Path("/etc/rb-vor/rb-vor.json")
d = json.loads(cfg.read_text())
d["bearing"].update(json.loads(pathlib.Path("/tmp/bearing.json").read_text())["bearing"])
cfg.write_text(json.dumps(d, indent=2) + "\n")
print("calibrated:", d["bearing"]["phase_sign"], d["bearing"]["phase_offset_deg"])
PY
python3 -m rbvor --self-test | grep calibration    # the WARN is now a PASS
```

> This calibration is **self-consistent only** until you confirm it against a
> real station — step 6. Until then a bearing could be mirrored by 180°.

---

## 4. Check the dongle, then decode

```bash
rtl_test -t                    # tuner named, no "usb_claim_interface error -6"
sudo -u rb-vor rtl_test -t     # the service user can reach it too
```

Live decode, no bridge needed, payloads printed instead of sent:

```bash
python3 -m rbvor --config /etc/rb-vor/rb-vor.json \
                 --freq 109.4 --dry-run --log-level INFO
```

Within a few seconds you should see the state walk
`TUNING → ACQUIRING → TRACKING` and a bearing appear:

```
INFO rbvor.sdr: tuned 109.400 MHz (centre 109.144 MHz, offset -256.0 kHz)
INFO rbvor: TRACKING: vorsignal 87
INFO rbvor: dry run: ... last {'vordir': 130, 'vorsignal': 87}
```

**No station to test against?** Decode a synthetic signal through the identical
code path instead:

```bash
python3 tools/vor_gen.py --radial 137 --seconds 30 --snr-db 25 -o /tmp/bench
python3 tools/vor_replay.py /tmp/bench.cu8       # residual should be ~ -1.97
```

**`vorsignal` stuck at 0 with `overruns` climbing** in the periodic `sdr` line
means the Pi is not keeping up: raise `sdr.block_samples` in the config, or find
what else is using the core. The bearing is never wrong in this case, only
absent — a window that spans a sample gap is discarded rather than decoded.

---

## 5. Run as a service

```bash
sudo systemctl start rb-vor
journalctl -u rb-vor -f
```

Expect `Active: active (running)`, the same state transitions as step 4, and no
watchdog restarts. The service reads `vorfreq` from the bridge WebSocket and
POSTs `{"vordir", "vorsignal"}` to the bridge on every update.

---

## 6. Confirm the calibration against a real station (important)

Pick a station whose position you know, from a spot whose position you know, so
the true magnetic radial can be read off the chart.

```bash
python3 tools/vor_capture.py -f <station MHz> -t 60 \
    --site "..." --station "..." --lat .. --lon .. \
    --expected-radial <from the chart> \
    --antenna "V-dipole horizontal 670 mm" --filter-chain "FM bandstop"
python3 tools/vor_replay.py captures/<newest>.cu8
```

The residual line is the verdict:

| Residual | Meaning | Do |
|---|---|---|
| ≈ −1.97° | calibration confirmed | leave `station_cal_deg` at 0 |
| ≈ −1.97° ± a few° | installation offset | set `station_cal_deg` to the difference |
| ≈ **+1.97°, mirrored** | sign inverted | flip `phase_sign`, re-verify |
| anything else | investigate before flying | do not calibrate it away |

---

## 7. RTL-SDR Blog V4

The V4's R828D tuner needs the rtl-sdr-blog fork of `librtlsdr`. On the stock
driver it appears to tune but produces nothing usable in the 108–118 MHz band —
total silence with no error. `install.sh` detects it and warns. Follow the
vendor's current instructions to install their `librtlsdr`, then re-run step 2.

---

## Everyday commands

| Do | Command |
|---|---|
| Prove the software | `python3 -m rbvor --self-test` |
| Decode live, no bridge | `python3 -m rbvor --config /etc/rb-vor/rb-vor.json --freq 109.4 --dry-run` |
| Inspect a capture | `python3 tools/vor_spectrum.py captures/<file>.cu8` |
| Replay a capture | `python3 tools/vor_replay.py captures/<file>.cu8` |
| Test without hardware | `python3 tools/mock_bridge.py --port 8099 --script "109.4@0" &` then run with that bridge URL |
| Start / stop service | `sudo systemctl start\|stop rb-vor` |
| Watch logs | `journalctl -u rb-vor -f` |
| Uninstall | `sudo ./install.sh --uninstall` |
| Bundle a report to send back | `python3 tools/collect_report.py --self-test -o /tmp/report.zip` |

## When something is wrong

`docs/bench-test.md` has a full troubleshooting table. The three most common:

| Symptom | Cause | Fix |
|---|---|---|
| `usb_claim_interface error -6` | kernel DVB driver holds the dongle | reboot, or `sudo rmmod dvb_usb_rtl28xxu` |
| `vorsignal` always 0, everything else fine | not calibrated | step 3 |
| tunes but decodes nothing, RTL-SDR Blog V4 | wrong `librtlsdr` | §7 |
| decodes nothing; `--survey` says **MIRROR DETECTED** | spectrum is flipped | set `"swap_iq": true` in the `sdr` config block |
