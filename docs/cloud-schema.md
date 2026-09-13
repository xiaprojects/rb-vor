# RB-Cloud record schema and retention

Reference: rb-vor-architecture.md section 13; project instructions objective 7
(cloud integration for forensic analysis) and section 2.7 (GDPR). Generated from
`rbvor.solutionlog.SOLUTION_SCHEMA` -- edit that, not this file, and regenerate.

## What is uploaded

One record per published solution, at the publish rate (1 Hz nominal). The same
structure is written to the local CSV (`log.solution_csv`) and uploaded to
RB-Cloud, so the local log and the cloud stream never diverge.

Each record carries enough to **re-derive the bearing offline**: the four quality
terms, the measured AM depth, subcarrier level, deviation and SNR, and -- for a
flagged solution -- the name of the IQ capture (`iq_ref`) that produced it. A
disputed reading can be replayed through `tools/vor_replay.py`, not merely
trusted.

## Record fields

Schema `rb-vor.solution/1`.

| field | unit | description |
|---|---|---|
| `t_utc` | ISO-8601 | wall-clock time the solution was produced |
| `t_mono` | s | monotonic stream time, window centre |
| `channel_khz` | kHz | tuned navaid channel, integer |
| `state` | enum | controller state: TRACKING/DEGRADED/NO_SIGNAL/... |
| `vordir` | deg mag | published radial FROM the station, or empty when flagged |
| `vorsignal` | 0-100 | published quality; 0 = NAV flag, vordir invalid |
| `flagged` | bool | true when the NAV flag is asserted |
| `reason` | text | human-readable state/flag reason |
| `q_am` | 0-1 | 30 Hz AM depth quality term |
| `q_sub` | 0-1 | 9960 Hz subcarrier band-power quality term |
| `q_snr` | 0-1 | 30 Hz tone SNR quality term |
| `q_stab` | 0-1 | bearing-stability quality term |
| `amp_a` | frac | measured 30 Hz AM depth, nominal 0.30 |
| `sub_level` | frac | measured subcarrier depth (band), nominal 0.30 |
| `deviation_hz` | Hz | measured FM deviation, nominal 480 |
| `carrier` | a.u. | smoothed carrier magnitude |
| `snr_db` | dB | post-integration 30 Hz tone SNR |
| `gps_track` | deg true | aircraft track from the bridge, diagnostics only |
| `overrun` | bool | window touched a sample gap (discarded if true) |
| `iq_ref` | filename | IQ capture that includes this window, if any |

## IQ captures

When `record.trigger` is `on_flag`, the IQ that produced a NAV flag is written
alongside the solution stream and named in that solution's `iq_ref`. These files
are large; they are uploaded on a slower path (or on request) and pruned locally
by `record.max_seconds` and disk budget. A capture carries a full sidecar
(`rbvor.record.IQMeta`), including site and antenna context when present.

## Retention and privacy (GDPR)

A single solution row contains no personal data. However:

- **`gps_track`** is aircraft state. Combined with a tail number or a
  registration, a track log is identifying, so the uploaded stream is treated as
  **personal data**.
- **Site coordinates and `station_cal_deg`**, when present in an IQ sidecar, are
  installation data and are treated the same way.

| Item | Basis | Retention |
|---|---|---|
| Solution stream (1 Hz records) | legitimate interest: flight forensics, on-condition maintenance | define per deployment; recommend <= 24 months |
| Flag IQ captures | as above | shorter; recommend <= 90 days unless attached to an incident |
| Incident-linked records | as above | for the life of the investigation |

The retention periods above are **recommendations for the deployment to set**,
not defaults baked into the software. RB-VOR itself buffers offline and uploads
on reconnect; it does not decide retention.

## Schema versioning

`rb-vor.solution/1`.
Additive fields only within a major version. Any change to the meaning, unit or
range of an existing field is a breaking change and increments the major number,
with a migration note.
