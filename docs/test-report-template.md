# RB-VOR test report — <site>, <date>

Fill this in during the bench and field campaign (M10). It is the record that
turns "it seemed to work" into evidence, and it is what closes the remaining
open items. One report per site/aircraft.

## Setup

| | |
|---|---|
| Operator | |
| Date / time (UTC) | |
| Aircraft / registration | |
| Site | |
| RB-VOR version | `python3 -m rbvor --version` |
| Host | Pi model, OS version |
| SDR | model, serial, driver (stock / rtl-sdr-blog) |
| Antenna | type, orientation, leg length |
| RF chain | band-stop model, LNA if any |

## Stage 1 — self test (software proof)

```
python3 -m rbvor --self-test
```

| Line | Result | Notes |
|---|---|---|
| all PASS? | | |
| throughput (% of one core @ 1.024 Msps) | | **your Pi's real number vs the 25% gate** |

## Stage 2 — calibration (implementation constants)

```
python3 tools/vor_calibrate.py --json /tmp/bearing.json
```

| | Value |
|---|---|
| `phase_sign` | |
| `phase_offset_deg` | |
| sign decision margin (deg) | should be near 180 |
| verify error (deg) | should be < 0.1 |

## Stage 3 — dongle

| Check | Result |
|---|---|
| `rtl_test -t` names the tuner | |
| no `usb_claim_interface error -6` | |
| `sudo -u rb-vor rtl_test -t` works | |

## Stage 4 — T9, band-stop filter benefit

Same station, same gain, filter in then out.

| Config | `30 Hz SNR` (dB) from `vor_spectrum.py` |
|---|---|
| band-stop IN | |
| band-stop OUT | |
| **difference (the filter benefit)** | |

Decision: is BOM item 3 (airband LNA) needed here? __________

## Stage 6 — live decode

10 minutes at the chosen frequency.

| Field (from the periodic `sdr` line) | Value |
|---|---|
| overruns | should be 0 |
| read_errors | should be 0 |
| queued (steady) | |
| typical `vorsnr` / `voram` / `vorsub` while tracking | |

## Stage 7 — O1, calibration vs a real station

```
python3 tools/vor_capture.py -f <MHz> -t 60 --lat .. --lon .. \
    --expected-radial <from chart> ...
python3 tools/vor_replay.py captures/<file>.cu8
```

| | Value |
|---|---|
| station / frequency | |
| expected radial (from chart, deg mag) | |
| decoded radial (deg mag) | |
| residual (deg) | |
| verdict (per calibration.md table) | confirmed / station offset / **mirrored** / investigate |
| `station_cal_deg` set to | |

**O1 closed?** ______  Attach the `.json` sidecar.

## Stage 8 — T11, car orbit (antenna shadowing)

| Quadrant / track (deg) | Expected radial | Decoded | Error (deg) |
|---|---|---|---|
| N | | | |
| E | | | |
| S | | | |
| W | | | |

| | |
|---|---|
| max error across headings | should be ≤ 5° |
| engine-off vs engine-on noise floor delta | |

## Stage 9 — service

| Check | Result |
|---|---|
| `systemctl status rb-vor` active | |
| no watchdog restarts over the run | |
| bridge round-trip (if a real bridge is present) | |

## Interface confirmation (O3, O4)

| | Answer |
|---|---|
| bridge expects `vorradial` as | from / to (O4) |
| O3 (`vorsignal` scale) | moot since RB-VOR-IF v2.0: no synthetic score on the wire |
| `mock_bridge.py` reported any payload deviation? | |

## Attachments

- [ ] `bearing.json` (calibration)
- [ ] stage-7 `.cu8` sidecar (`.json`)
- [ ] any capture that decoded badly (IQ to an issue, sidecar committed)
- [ ] `solutions.csv` excerpt if logging was enabled

## Summary

| Open item | Status after this report |
|---|---|
| O1 (calibration vs reality) | |
| O3/O4 (bridge conventions) | |
| O8 (FM filter benefit) | |
| O12 (clean-image install) | |
