# RB-VOR calibration

Two constants must be measured before the daemon will publish a bearing.
Amendment A2: they ship as `null` and `radial()` raises rather than guess.

| Constant | Property of | Measured against | When |
|---|---|---|---|
| `bearing.phase_sign` | the implementation | synthetic generator | once, per release |
| `bearing.phase_offset_deg` | the implementation | synthetic generator | once, per release |
| `bearing.station_cal_deg` | the installation | a real station | per aircraft (T8) |

## 1. Implementation constants

```bash
tools/vor_calibrate.py --json /etc/rb-vor/bearing.json
```

The model is `radial = sign * delta_phi + offset`. For the correct sign the
offset solved from two radials 90° apart agrees; for the wrong sign it differs
by 180°. The tool then verifies on a third unrelated radial, checks that DVOR
decodes identically at the same sign (amendment A6), and checks that a
sense-reversed DVOR does not — that last check is what stops the verification
passing vacuously.

Measured on this baseline:

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

The offset is close to the 1.35° predicted for the conjugate-product
discriminator's one-sample delay at 4 kHz; the remainder is residual
decimation-phase mismatch between the two paths.

> **This does not close open item O1.** The generator and the demodulator share
> one author's sign conventions and could be consistently wrong together. Only a
> capture of a real station with a known radial settles it.

**Two estimators, two constants (D7).** `dsp.fm_estimator` selects how the
reference tone is recovered, and the calibration must be derived with the
same setting:

| `dsp.fm_estimator` | command | result on this baseline |
|---|---|---|
| `discriminator` (shipped default) | `tools/vor_calibrate.py` | `+1`, `−1.9663` |
| `harmonic` | `tools/vor_calibrate.py --fm-estimator harmonic` | `+1`, `−0.6077` |

A config that pairs one estimator with the other's constant publishes
bearings 1.36° off. The self-test reports which estimator the config uses.

## 2. Closing O1 with one capture

```bash
tools/vor_capture.py -f <station MHz> -t 60 \
    --site "..." --lat .. --lon .. --expected-radial <from the map> \
    --antenna "..." --filter-chain "FM bandstop"
tools/vor_replay.py captures/<file>.cu8
```

`vor_replay.py` prints the residual against the sidecar's expected radial.

| Residual | Meaning |
|---|---|
| ≈ −1.97° | implementation constants confirmed; set `station_cal_deg = 0` |
| ≈ −1.97° ± small | the difference is `station_cal_deg` for this installation |
| ≈ +1.97°, mirrored | `phase_sign` is inverted — both implementations were wrong together, which is exactly what O1 warns about |
| anything else | investigate before flying; do not calibrate it away |

Record the outcome in `docs/first-light.md` and in `CLAUDE.md`.

## 3. Station constant

`station_cal_deg` absorbs installation residuals only, and is set from T8 over
at least 10 minutes on a known station. It is never shipped in the default
config, and a value larger than a few degrees means something is wrong with the
antenna or the site rather than with the software.
