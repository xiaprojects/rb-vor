# RB-VOR accuracy sweep

24 radials (step 15 deg) x 7 SNRs, fs 256 kHz, 12 s per point, 2 s windows at 1 s hop, `fm_estimator` harmonic.

Calibration: `phase_sign` +1, `phase_offset_deg` -0.6078.

| SNR dB | RMS err | max err | vorsignal | q_am | q_sub | q_snr | q_stab | flagged |
|---|---|---|---|---|---|---|---|---|
| 30 | 0.020 | 0.041 | 100 | 0.999 | 1.000 | 1.000 | 0.997 | 0% |
| 10 | 0.241 | 0.501 | 91 | 0.855 | 0.919 | 0.857 | 0.968 | 0% |
| 6 | 0.457 | 0.919 | 79 | 0.673 | 0.823 | 0.654 | 0.947 | 0% |
| 3 | 0.781 | 1.539 | 68 | 0.489 | 0.744 | 0.475 | 0.921 | 0% |
| 0 | 1.307 | 2.790 | 0 | 0.313 | 0.685 | 0.254 | 0.876 | 100% |
| -3 | 2.282 | 5.085 | 0 | 0.182 | 0.648 | 0.045 | 0.785 | 100% |
| -6 | 4.245 | 9.419 | 0 | 0.099 | 0.628 | 0.003 | 0.598 | 100% |

## Open item O10 — do the quality terms predict the error?

Spearman rank correlation of each term against bearing error (negative is good: higher quality, lower error).

| term | rank corr vs error | spread across the sweep |
|---|---|---|
| `q_am` | -0.949 | 0.091 – 0.999 |
| `q_sub` | -0.957 | 0.627 – 1.000 |
| `q_snr` | -0.952 | 0.000 – 1.000 |
| `q_stab` | -0.910 | 0.495 – 0.998 |
| `vorsignal` | -0.901 | 0.000 – 100.000 |
