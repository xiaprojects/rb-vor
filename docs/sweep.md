# RB-VOR accuracy sweep

24 radials (step 15 deg) x 9 SNRs, fs 256 kHz, 12 s per point, 2 s windows at 1 s hop.

Calibration: `phase_sign` +1, `phase_offset_deg` -1.9613.

| SNR dB | RMS err | max err | vorsignal | q_am | q_sub | q_snr | q_stab | flagged |
|---|---|---|---|---|---|---|---|---|
| 40 | 0.007 | 0.016 | 100 | 0.999 | 1.000 | 1.000 | 0.999 | 0% |
| 30 | 0.019 | 0.042 | 100 | 0.998 | 1.000 | 1.000 | 0.997 | 0% |
| 20 | 0.060 | 0.133 | 99 | 0.985 | 0.995 | 1.000 | 0.991 | 0% |
| 15 | 0.113 | 0.247 | 98 | 0.954 | 0.978 | 1.000 | 0.983 | 0% |
| 10 | 0.261 | 0.593 | 90 | 0.854 | 0.919 | 0.849 | 0.965 | 0% |
| 6 | 0.509 | 1.891 | 78 | 0.672 | 0.823 | 0.644 | 0.926 | 0% |
| 3 | 1.632 | 4.044 | 61 | 0.488 | 0.744 | 0.471 | 0.746 | 0% |
| 0 | 3.461 | 9.500 | 0 | 0.313 | 0.685 | 0.255 | 0.534 | 100% |
| -3 | 9.830 | 33.211 | 0 | 0.182 | 0.648 | 0.049 | 0.000 | 100% |

## Open item O10 — do the quality terms predict the error?

Spearman rank correlation of each term against bearing error (negative is good: higher quality, lower error).

| term | rank corr vs error | spread across the sweep |
|---|---|---|
| `q_am` | -0.977 | 0.174 – 0.999 |
| `q_sub` | -0.975 | 0.647 – 1.000 |
| `q_snr` | -0.941 | 0.009 – 1.000 |
| `q_stab` | -0.970 | 0.000 – 0.999 |
| `vorsignal` | -0.973 | 0.000 – 100.000 |
