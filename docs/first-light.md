# First light — record here, M5

Fill one block per capture. A capture without this context is not evidence.

## Procedure

1. Band-stop filter fitted between antenna and dongle. Verify with T9 (capture
   the same station with and without it, compare `q_snr`).
2. Antenna **horizontal** — VOR is horizontally polarised; a vertical whip
   costs roughly 20 dB.
3. Pick a station whose position is known, and note your own position.
4. Capture 60 s:

   ```
   tools/vor_capture.py -f 109.4 -t 60 \
       --site "..." --station "..." --lat .. --lon .. \
       --expected-radial .. --antenna "..." --filter-chain "..." \
       --notes "car roof, engine off"
   ```

5. Commit the `.json` sidecar (not the IQ) and attach the IQ to the issue.

## Log

| Date | Site | Station | Freq | Expected radial | Decoded | Error | vorstatus | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-09-27 | airborne nr LIRZ, 9.3–11.6 nm, 724–2493 ft | assumed PRU (43.0959/12.5132, unverified) | 109.400 | 241°→273° true (≈237°–269° mag) | **none** | — | 1 (NO_SIGNAL) for 14 min | First flight log: `flightlog-20260927175640.0159.sqlite`. AM depth 0.005 vs 0.30 nominal, SNR 2.6 dB, flat on the ground and airborne → noise floor, no signal. Receiver behaved correctly (no bearing published). See CLAUDE.md F6. |

## Bench notes — car test

The car is a good bench: it moves the antenna over a known ground track, which
is exactly what T11 needs in miniature. Drive a loop around the station at
roughly constant radius, log `--gps-track` per capture, and the resulting error
vs heading curve characterises antenna shadowing before anything flies.
