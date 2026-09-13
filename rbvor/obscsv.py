"""Raw per-window observation dump for the daemon's ``--csv`` option.

One row per demodulated window -- the same data ``tools/vor_replay.py`` writes
off a capture, with a ``frequency`` column added so a live multi-channel session
stays interpretable. Written for every window, including overrun and warming
ones, so the file is a faithful record of what the receiver saw, not just what it
published.

The column order is fixed and matches the request exactly:

    t,frequency,delta_phi_deg,phi_a_deg,phi_b_deg,amp_a,amp_b,sub_level,
    carrier,noise_est,snr_db,overrun

`frequency` is the tuned VOR channel in kHz (109400 = 109.400 MHz), 0 when none.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

FIELDS = [
    "t", "frequency", "delta_phi_deg", "phi_a_deg", "phi_b_deg",
    "amp_a", "amp_b", "sub_level", "carrier", "noise_est", "snr_db", "overrun",
]


class ObsCsvSink:
    """Append-only CSV writer. Use as ``controller.obs_sink`` and ``close()`` it.

    The file is opened once, the header written once, and each call appends a
    row and flushes, so a long run that is killed still leaves a complete file
    up to the last window.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fh = self.path.open("w", newline="")
        self._w = csv.writer(self._fh)
        self._w.writerow(FIELDS)
        self._fh.flush()
        self.rows = 0

    def __call__(self, obs, freq_khz: int) -> None:
        self._w.writerow([
            f"{obs.t:.3f}",
            int(freq_khz),
            f"{np.rad2deg(obs.delta_phi) % 360:.4f}",
            f"{np.rad2deg(obs.phi_a):.4f}",
            f"{np.rad2deg(obs.phi_b):.4f}",
            f"{obs.amp_a:.5f}",
            f"{obs.amp_b:.2f}",
            f"{obs.sub_level:.5f}",
            f"{obs.carrier:.6g}",
            f"{obs.noise_est:.6f}",
            f"{obs.snr_db:.2f}",
            int(obs.overrun),
        ])
        self._fh.flush()
        self.rows += 1

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
