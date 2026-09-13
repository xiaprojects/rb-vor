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

| Date | Site | Station | Freq | Expected radial | Decoded | Error | vorsignal | Notes |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |

## Bench notes — car test

The car is a good bench: it moves the antenna over a known ground track, which
is exactly what T11 needs in miniature. Drive a loop around the station at
roughly constant radius, log `--gps-track` per capture, and the resulting error
vs heading curve characterises antenna shadowing before anything flies.
