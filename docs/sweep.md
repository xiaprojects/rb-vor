# RB-VOR accuracy sweep

12 radials (step 30 deg) x 8 SNRs, fs 256 kHz, 12 s per point.

Calibration: `phase_sign` +1, `phase_offset_deg` -1.9657.

| SNR dB | RMS err | max err | vorsignal | q_am | q_sub | q_snr | q_stab | flagged |
|---|---|---|---|---|---|---|---|---|
| 40 | 0.015 | 0.035 | 100 | 0.999 | 0.963 | 1.000 | 0.999 | 0% |
| 30 | 0.041 | 0.092 | 99 | 0.998 | 0.963 | 1.000 | 0.998 | 0% |
| 20 | 0.132 | 0.270 | 99 | 0.986 | 0.958 | 0.994 | 0.992 | 0% |
| 10 | 0.474 | 1.156 | 85 | 0.867 | 0.900 | 0.618 | 0.968 | 0% |
| 6 | 1.169 | 3.113 | 72 | 0.695 | 0.816 | 0.395 | 0.917 | 0% |
| 3 | 2.327 | 6.675 | 55 | 0.514 | 0.737 | 0.199 | 0.750 | 0% |
| 0 | 6.544 | 18.733 | 6 | 0.336 | 0.667 | 0.017 | 0.369 | 83% |
| -3 | 17.447 | 52.789 | 0 | 0.199 | 0.617 | 0.000 | 0.002 | 100% |

## Open item O10 — do the quality terms predict the error?

Spearman rank correlation of each term against bearing error (negative is good: higher quality, lower error).

| term | rank corr vs error | spread across the sweep |
|---|---|---|
| `q_am` | -0.982 | 0.181 – 0.999 |
| `q_sub` | -0.983 | 0.615 – 0.963 |
| `q_snr` | -0.982 | 0.000 – 1.000 |
| `q_stab` | -0.989 | 0.000 – 1.000 |
| `vorsignal` | -0.986 | 0.000 – 100.000 |
