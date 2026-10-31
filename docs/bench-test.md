# RB-VOR bench test — worked example at 109.40 MHz

Target: Raspberry Pi 5, Raspberry Pi OS 64-bit, RTL-SDR, FM band-stop filter,
horizontal V-dipole. Allow about an hour.

Work through the stages in order. Each one either passes or tells you exactly
where to look, and each is cheap enough that skipping it costs more than running
it.

> **109.40 MHz is used throughout as the worked example.** It is a valid VOR
> channel (100 kHz digit is 4, even — see §2.2 of the interface spec).
> **Check your chart for a station actually transmitting on 109.40 within
> range.** If there is none, stages 1–4 still work unchanged, and stage 5 gives
> you a way to test without one. Substitute your local station's frequency in
> stage 6 onward.

---

## Stage 0 — install

```bash
git clone https://github.com/xiaprojects/rb-vor && cd rb-vor
sudo ./install.sh
```

Expect: package install, DVB-T blacklist, `rb-vor` user created, udev rules,
config validated, unit enabled but **not started**. A warning that the bearing
calibration is unset is expected and correct.

If the installer warns about an RTL-SDR Blog V4 with the stock driver, stop and
fix that first (`docs/install.md` §4.1). On the stock driver the V4 appears to
tune and produces nothing usable in our band — total failure, no error message.

---

## Stage 1 — software self test, no hardware

```bash
cd /opt/rb-vor
python3 -m rbvor --self-test
```

Expect every line `PASS`, with one `WARN` for the missing calibration:

```
  [PASS] channel rules (109.4 / 109.5 / 118.0)  109.4 VOR, 109.5 localiser, ...
  [WARN] bearing calibration present            not set -- the daemon will publish only flags
  [PASS] 30 Hz AM depth                         0.299 (nominal 0.30)
  [PASS] 9960 Hz subcarrier (band measure, A7)  0.289 (nominal 0.30)
  [PASS] FM deviation                           479 Hz (nominal 480)
  [PASS] bearing accuracy                       0.078 deg RMS over 9 solutions
  [PASS] vorsignal on a good signal             99..99
  [PASS] NAV flag raised on noise only          4/4 windows flagged
  [PASS] throughput                             ~1.6% of one core at 1.024 Msps
```

On a fresh checkout, before install, you will see one extra line:

```
  [WARN] configuration    no config found -- testing with built-in defaults
```

That is expected and harmless — the config lives in `/etc/rb-vor` only after
`install.sh` runs, and the self-test uses built-in defaults for everything it
needs. The run still ends "all checks passed".

**If any line says FAIL, stop.** Nothing downstream can work, and the failing
line names the stage. If it passes, the software chain is sound and every later
problem is RF, antenna, or calibration.

Record the throughput figure — it is your Pi's real number, against the 25 %
port gate.

---

## Stage 2 — calibrate

```bash
python3 tools/vor_calibrate.py --json /tmp/bearing.json
```

Expect a decisive sign choice (margin near 180°) and a small offset:

```
  sign +1: offsets  358.032 /  358.036  disagreement   0.004  <-- chosen
  sign -1: offsets    1.968 /  181.964  disagreement 179.996
  decision margin 179.992 deg

phase_sign        +1
phase_offset_deg  -1.9657

verify  CVOR   radial 213 ->  212.983  error 0.0166 deg
        DVOR   radial 213 ->  213.002  error 0.0022 deg
        DVOR-R radial 213 ->  147.002  error 65.9978 deg
```

Merge the `bearing` block into `/etc/rb-vor/rb-vor.json`:

```bash
sudo python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/etc/rb-vor/rb-vor.json")
cfg = json.loads(p.read_text())
cfg["bearing"].update(json.loads(pathlib.Path("/tmp/bearing.json").read_text())["bearing"])
p.write_text(json.dumps(cfg, indent=2) + "\n")
print("phase_sign", cfg["bearing"]["phase_sign"],
      "offset", cfg["bearing"]["phase_offset_deg"])
PY
python3 -m rbvor --self-test | grep calibration
```

The WARN should now be a PASS.

> This calibration is **self-consistent only**. The generator and the
> demodulator share sign conventions and could be wrong together. Stage 7 is
> what settles it (open item O1).

---

## Stage 3 — the dongle

```bash
rtl_test -t
```

Expect the tuner named, and no `usb_claim_interface error -6`. If you see that
error the DVB-T driver still holds the device — reboot, or
`sudo rmmod dvb_usb_rtl28xxu`.

Confirm the unprivileged user can reach it:

```bash
sudo -u rb-vor rtl_test -t
```

If this fails but the root version worked, the udev rule has not been applied:
`sudo udevadm control --reload-rules && sudo udevadm trigger`, then replug.

---

## Stage 4 — capture, with and without the filter (test T9)

Fit the FM band-stop filter between antenna and dongle. Antenna **horizontal**.

```bash
python3 tools/vor_capture.py -f 109.4 -t 60 \
    --site "bench" --station "???" \
    --antenna "V-dipole horizontal 670 mm" \
    --filter-chain "FM bandstop" --notes "bench, filter IN"

python3 tools/vor_spectrum.py captures/<newest>.cu8
```

Expect the tuner offset to match, and — if a station is on the air — the three
components present:

```
tuner offset  sidecar -256.0 kHz, measured -256.000 kHz (error +0.0 Hz)

  component            depth    nominal   verdict
  30 Hz AM              0.245    0.30     ok
  9960 Hz subcarrier    0.247    0.30     ok
  1020 Hz ident         0.047       -     present
  30 Hz SNR              15.5 dB
```

Now remove the band-stop filter and repeat. **Record both `30 Hz SNR` figures**
— the difference is your T9 desensitisation number, and it decides whether BOM
item 3 (the airband LNA) is needed at your site.

Note that depths read low on a noisy capture. That is the envelope detector's DC
rising with noise (amendment A8), not a fault.

---

## Stage 5 — bench without a station

If nothing transmits on 109.40 near you, or you want a known answer before
trusting an unknown one, inject a synthetic signal through the real code path:

```bash
python3 tools/vor_gen.py --radial 137 --seconds 30 --snr-db 25 -o /tmp/bench
python3 tools/vor_replay.py /tmp/bench.cu8
```

Expect `residual ≈ -1.97 deg`, matching your calibration.

You can also drive the daemon end to end with no bridge and no radio:

```bash
python3 tools/mock_bridge.py --port 8099 --script "109.4@0" &
python3 -m rbvor --config /etc/rb-vor/rb-vor.json --replay /tmp/bench.cu8
```

The mock bridge prints and validates every payload:

```
  [rest] {"vorradial": 137, "vorsnr": 41, "voram": 29, "vorstatus": 5, "vorsub": 25, "vorfreqactive": 109400}
```

Any deviation from the interface spec is flagged inline with the reason.

---

## Stage 6 — live decode at 109.40 MHz

Bench mode: fixed frequency, no bridge, payloads logged instead of POSTed.

```bash
python3 -m rbvor --config /etc/rb-vor/rb-vor.json \
                 --freq 109.4 --dry-run --log-level INFO
```

Expect, within a few seconds:

```
INFO rbvor.sdr: SDR open: soapy:driver=rtlsdr
INFO rbvor.sdr: tuned 109.400 MHz (centre 109.144 MHz, offset -256.0 kHz)
INFO rbvor: TUNING: tuning 109.400 MHz
INFO rbvor: ACQUIRING: settled
INFO rbvor: TRACKING: vorsignal 87
INFO rbvor: sdr {'blocks_read': 921, 'overruns': 0, 'read_errors': 0, ...}
```

Let it run 10 minutes and check the periodic `sdr` line:

| Field | Expect | If not |
|---|---|---|
| `overruns` | 0 | the Pi is not keeping up — see below |
| `read_errors` | 0 | USB or power; try a powered hub, shorter lead |
| `queued` | small and steady | fine |

**`sig 0` on the status line (`vorsnr`/`voram` 0 on the wire) with rising
`overruns`** is the one failure with a
misleading symptom. The reader is outrunning the demodulator, so every
integration window touches a sample gap and is discarded — correctly, since a
window spanning a discontinuity would give a wrong bearing. Availability is
lost, correctness is not. Raise `sdr.block_samples`, or find what else is using
the core.

---

## Stage 7 — settle the calibration against reality (closes O1)

Pick a station whose position you know and a spot whose position you know, so
the true magnetic radial can be computed from the chart.

```bash
python3 tools/vor_capture.py -f <station MHz> -t 60 \
    --site "..." --station "..." --lat .. --lon .. \
    --expected-radial <from the chart> \
    --antenna "V-dipole horizontal 670 mm" --filter-chain "FM bandstop"

python3 tools/vor_replay.py captures/<newest>.cu8
```

The residual line is the answer:

| Residual | Meaning | Action |
|---|---|---|
| ≈ −1.97° | implementation constants confirmed | `station_cal_deg = 0`, **O1 closed** |
| ≈ −1.97° ± a few ° | installation residual | set `station_cal_deg` to the difference |
| ≈ **+1.97°**, mirrored | `phase_sign` is inverted — both implementations were wrong together | flip `phase_sign`, re-verify, and record it |
| anything else | do not calibrate it away | investigate before flying |

Record the result in `docs/first-light.md` and in `CLAUDE.md`.

---

## Stage 8 — the car (a small T11)

The car is a better bench than it looks: it moves the antenna over a known
ground track around a known point, which is what the flight test does at scale.

Drive a roughly constant-radius loop around the station and capture once per
quadrant, noting your track each time:

```bash
python3 tools/vor_capture.py -f <station MHz> -t 60 \
    --gps-track <heading> --lat .. --lon .. --expected-radial <computed> \
    --notes "car, quadrant N, engine off"
```

Replay all four and tabulate error against heading. That is your antenna
shadowing curve, obtained before anything flies. Engine off for at least one
capture — alternator and ignition noise is worth quantifying separately.

---

## Stage 9 — the service

```bash
sudo systemctl start rb-vor
systemctl status rb-vor
journalctl -u rb-vor -f
```

Expect `Active: active (running)`, no watchdog restarts, and the same state
transitions as stage 6. If it restarts every 10 seconds the watchdog is not
being fed — capture `journalctl -u rb-vor -n 200` and an IQ file.

---

## What to send back

| Item | Why |
|---|---|
| Stage 1 self-test output | your Pi's real throughput number |
| Stage 2 calibration output | the implementation constants |
| Both stage 4 `30 Hz SNR` figures | the T9 filter benefit at your site |
| Stage 7 residual + the `.json` sidecar | closes O1 |
| Stage 6 `sdr` stats after 10 minutes | overrun and error behaviour on real hardware |
| Any `.cu8` that decoded badly | reproducible on a desk via `vor_replay.py` |

Sidecars are small and safe to commit. IQ files are large — attach them to an
issue rather than committing them.
