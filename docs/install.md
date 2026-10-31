# RB-VOR installation

Assumes a clean Raspberry Pi OS 64-bit (or Debian 12+) image, hand tools, a
soldering iron, a 3D printer, and no lab equipment.

## 1. RF chain

```
[antenna] -> [FM broadcast band-stop] -> [optional airband LNA] -> [RTL-SDR] -> USB -> [Pi 5]
```

| # | Item | Primary | Alternate | Note |
|---|---|---|---|---|
| 1 | SDR dongle | RTL-SDR Blog V4 (1 ppm TCXO) | Nooelec NESDR SMArTee v2; generic RTL2832U + R820T2 | V4 needs the rtl-sdr-blog driver — see §4 |
| 2 | FM band-stop 88–108 MHz | RTL-SDR Blog broadcast-FM block | Nooelec Flamingo FM+ | **mandatory** |
| 3 | Airband LNA/filter | 108–137 MHz SAW + LNA | — | only if T9 shows a deficit |
| 4 | Antenna | horizontal V-dipole, 670 mm legs | telescopic dipole kit | **horizontal** — VOR is horizontally polarised |
| 5 | Coax | RG-316 SMA–SMA 1 m | RG-58 + BNC adapter | keep short |
| 6 | Ferrites | clip-on 5 mm × 4 | — | one per USB and coax end |
| 7 | USB isolator | USB 2.0 galvanic isolator | ferrite on the USB lead | optional |

The band-stop filter is not optional. 108.000 MHz sits directly above the FM
broadcast band; without it the RTL front end desensitises or produces images on
any site within range of an FM transmitter. Quantify it once with T9 — capture
the same station with and without the filter and compare the `30 Hz SNR` line
from `tools/vor_spectrum.py`.

Antenna orientation matters more than antenna quality: VOR is horizontally
polarised, and a vertical whip costs roughly 20 dB.

## 2. Install

```bash
git clone https://github.com/xiaprojects/rb-vor && cd rb-vor
sudo ./install.sh
```

The installer creates an unprivileged `rb-vor` user, installs udev rules so the
dongle is reachable without root, blacklists the DVB-T kernel driver, installs
to `/opt/rb-vor` with config in `/etc/rb-vor`, validates that config, and enables
a `Type=notify` systemd unit with a 10 s watchdog.

It deliberately does **not** start the service, because a fresh install is
uncalibrated (§3).

```bash
sudo ./install.sh --uninstall     # keeps config, captures and logs
sudo ./install.sh --no-service    # files only
```

## 3. Calibrate before starting

A fresh config has `bearing.phase_sign: null`, so the daemon publishes only
flags and never a bearing. This is deliberate — an uncalibrated bearing looks
plausible and is wrong, which is the worst failure this system has.

```bash
cd /opt/rb-vor
python3 tools/vor_calibrate.py --json /tmp/bearing.json
# merge the "bearing" block into /etc/rb-vor/rb-vor.json
sudo systemctl start rb-vor
journalctl -u rb-vor -f
```

Then confirm against a real station — see `calibration.md`. Until that is done
the calibration is only self-consistent (open item O1).

## 4. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `usb_claim_interface error -6` | the kernel DVB-T driver claimed the dongle | the installer blacklists it; reboot, or `sudo rmmod dvb_usb_rtl28xxu` |
| Device not found as `rb-vor` user | udev rule not applied | `sudo udevadm control --reload-rules && sudo udevadm trigger`, replug |
| Tunes but nothing decodes, RTL-SDR Blog V4 | stock `librtlsdr` does not drive the V4's R828D tuner | install the rtl-sdr-blog fork (§4.1) |
| `vorstatus` stuck at 1, `vorsub` near zero | no subcarrier — wrong frequency, or a localiser | check the channel against §2.2 of the interface spec |
| `vorstatus` never reaches 4, radial 0, measurements healthy, status line says `uncalibrated` | uncalibrated | §3 |
| Bearing is stable but wrong by a constant | `station_cal_deg` or an inverted `phase_sign` | `calibration.md`, table in §2 |
| Bearing wanders, `q_stab` low | weak signal, FM breakthrough, or a vertical antenna | fit the band-stop, turn the antenna horizontal |
| Nothing decodes; `vor_replay.py --survey` reports **MIRROR DETECTED** | the RTL-SDR is delivering a spectrally-flipped band; the signal is on the wrong side of centre | set `"swap_iq": true` in the `sdr` block, or decode captures with `--swap-iq` |
| Service restarts every 10 s | watchdog not being fed — the event loop is wedged | `journalctl -u rb-vor`, and report with an IQ capture |

### 4.1 RTL-SDR Blog V4

The V4 uses an R828D tuner and needs the rtl-sdr-blog fork of `librtlsdr`. On
the stock driver it appears to tune but produces nothing usable above roughly
100 MHz — which is our entire band, so this is total failure with no error
message rather than a subtle degradation. `install.sh` detects the combination
and warns. Follow the vendor's current instructions to replace `librtlsdr`;
they change often enough that copying them here would age badly.

## 5. Verify

```bash
# offline, no hardware: decode a synthetic signal end to end
cd /opt/rb-vor
python3 tools/vor_gen.py --radial 137 --seconds 10 --snr-db 25 -o /tmp/t
python3 tools/vor_replay.py /tmp/t.cu8

# with hardware: capture a real station and inspect it
python3 tools/vor_capture.py -f 109.4 -t 60 --site "..." --expected-radial ...
python3 tools/vor_spectrum.py captures/<file>.cu8
python3 tools/vor_replay.py captures/<file>.cu8

# without a bridge: run both ends locally
python3 tools/mock_bridge.py --port 8099 --script "109.4@0,113.35@20" &
python3 -m rbvor --config /etc/rb-vor/rb-vor.json
```

## 6. Measured performance

T12, 300 s of 1.024 Msps signal on the development machine:

| Metric | Result | Limit |
|---|---|---|
| Sustained CPU | 3.6 % of one core | 25 % (A3 port gate) |
| RSS growth | flat after warm-up | no growth |
| Dropped windows | 0 of 300 | < 0.1 % |
| Mean bearing error at 25 dB SNR | 0.072° | — |

Re-measure on the target Pi 5 with RB-01 and `dump1090` running before treating
the port gate as settled.
