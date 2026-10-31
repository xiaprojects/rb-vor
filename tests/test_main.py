"""M8 -- daemon entry point wiring.

Not the async run loop (that is exercised end to end elsewhere), just the
decisions __main__ makes about which pieces to start. The bug these pin: a
replay or bench run must not start the bridge client, or it floods the log with
connection-refused warnings against a bridge that need not exist.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args, timeout=60):
    return subprocess.run(
        [sys.executable, "-m", "rbvor", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def test_self_test_exits_zero_without_hardware():
    r = run_cli("--self-test")
    assert r.returncode == 0, r.stderr
    assert "all checks passed" in r.stdout
    assert "PASS" in r.stdout


def test_bad_config_path_is_a_clean_error_not_a_traceback():
    r = run_cli("--config", "/nonexistent/rb-vor.json", "--freq", "109.4")
    assert r.returncode == 2
    assert "configuration error" in r.stderr
    assert "Traceback" not in r.stderr


def test_dry_run_replay_does_not_contact_a_bridge(tmp_path):
    """The regression: replay + dry-run must be silent about the bridge."""
    gen = subprocess.run(
        [sys.executable, "tools/vor_gen.py", "--radial", "137", "--seconds", "6",
         "--snr-db", "25", "--fs", "256000", "--offset", "-64000",
         "--nav-freq", "109.4", "-o", str(tmp_path / "s")],
        cwd=ROOT, capture_output=True, text=True)
    assert gen.returncode == 0, gen.stderr

    cfg = _bench_config(tmp_path)
    r = run_cli("--config", str(cfg), "--replay", str(tmp_path / "s.cu8"),
                "--dry-run", "--log-level", "INFO")
    assert r.returncode == 0, r.stderr
    log = r.stdout + r.stderr
    assert "bridge connection lost" not in log
    assert "Connect call failed" not in log
    assert "TRACKING" in log
    assert "radial" in log          # dry-run echo prints radial/snr/am


def _bench_config(tmp_path: Path) -> Path:
    import json
    cfg = json.loads((ROOT / "config" / "rb-vor.json").read_text())
    cfg["sdr"]["sample_rate_hz"] = 256000
    cfg["sdr"]["tuner_offset_hz"] = -64000
    cfg["bearing"].update(phase_sign=1, phase_offset_deg=-1.9657)
    cfg["log"]["solution_csv"] = str(tmp_path / "solutions.csv")
    p = tmp_path / "bench.json"
    p.write_text(json.dumps(cfg, indent=2))
    return p


def test_unopenable_sdr_stops_the_daemon_with_exit_code_3(tmp_path, monkeypatch):
    """F3 (CLAUDE.md): a task that dies must take the daemon down.

    Seen in the field: with no openable dongle, `tick()` raised SdrError inside
    the ticker task, nobody awaited it, and the process idled for ten minutes
    printing `blocks_read: 0`. `main()`'s `except SdrError` only guarded
    `asyncio.run`, so exit code 3 was never produced. Without the fix this test
    hangs until the timeout instead of raising.
    """
    import argparse
    import asyncio

    import pytest

    from rbvor import sdr as sdr_mod
    from rbvor.__main__ import _run, main
    from rbvor.config import load
    from rbvor.sdr import SdrError

    def refuse(cfg, order=None):
        raise SdrError("no usable SDR backend (test double)")

    monkeypatch.setattr(sdr_mod, "open_device", refuse)
    cfg_path = _bench_config(tmp_path)
    args = argparse.Namespace(dry_run=True, replay=None, freq=109.4, quiet=True,
                              status_every=2.0, log_level=None, csv=None)
    with pytest.raises(SdrError):
        asyncio.run(asyncio.wait_for(_run(load(cfg_path), args), timeout=15))

    # and the CLI turns it into the documented exit code, not a hang
    rc = main(["--config", str(cfg_path), "--freq", "109.4", "--dry-run",
               "--quiet", "--log-level", "ERROR"])
    assert rc == 3


def test_self_test_passes_without_any_config_present(tmp_path):
    """A19: the self-test proves the SOFTWARE works, independent of config.

    A builder who clones the repo and runs stage 1 before installing has no
    config on disk yet (it lands in /etc at install time). The self-test must
    fall back to built-in defaults and pass with a WARN -- not hard-fail with a
    red FAIL that stops the bench before it starts. This regressed once because
    the two 'no config' error messages are phrased differently.
    """
    import shutil
    for d in ("rbvor", "tools", "schema"):
        shutil.copytree(ROOT / d, tmp_path / d)
    r = subprocess.run(
        [sys.executable, "-m", "rbvor", "--self-test"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""})
    assert r.returncode == 0, f"self-test failed with no config:\n{r.stdout}\n{r.stderr}"
    assert "all checks passed" in r.stdout
    assert "FAIL" not in r.stdout
    assert "built-in defaults" in r.stdout
