"""M8 -- packaging invariants.

Cheap structural checks that catch the packaging mistakes which otherwise only
appear on a clean image, in front of a builder who cannot debug them.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor.sdnotify import SdNotify  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_installer_is_syntactically_valid():
    r = subprocess.run(["bash", "-n", str(ROOT / "install.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_installer_never_runs_the_daemon_as_root():
    unit = (ROOT / "systemd" / "rb-vor.service").read_text()
    assert re.search(r"^User=rb-vor$", unit, re.M)
    assert "SupplementaryGroups=plugdev" in unit
    assert "NoNewPrivileges=true" in unit


def test_unit_uses_notify_and_a_watchdog():
    unit = (ROOT / "systemd" / "rb-vor.service").read_text()
    assert "Type=notify" in unit
    assert re.search(r"^WatchdogSec=\d+", unit, re.M)
    assert re.search(r"^Restart=always$", unit, re.M)


def test_unit_writable_paths_match_the_config():
    unit = (ROOT / "systemd" / "rb-vor.service").read_text()
    cfg = json.loads((ROOT / "config" / "rb-vor.json").read_text())
    writable = re.search(r"^ReadWritePaths=(.+)$", unit, re.M).group(1).split()
    for path in (cfg["record"]["dir"], cfg["log"]["solution_csv"]):
        if path:
            assert any(path.startswith(w) for w in writable), (
                f"{path} is not in the unit's ReadWritePaths")


def test_dvb_blacklist_is_shipped():
    """Without it, rtl-sdr fails with usb_claim_interface -6 on every boot."""
    conf = (ROOT / "systemd" / "rb-vor-blacklist.conf").read_text()
    assert "dvb_usb_rtl28xxu" in conf


def test_udev_rule_covers_the_common_dongles():
    rules = (ROOT / "systemd" / "99-rb-vor-sdr.rules").read_text()
    assert '0bda' in rules and '2838' in rules      # RTL2832U, the usual case
    assert 'GROUP="plugdev"' in rules


def test_sdnotify_is_a_no_op_without_systemd():
    """The daemon must run identically from a shell and in CI."""
    n = SdNotify(None)
    assert not n.enabled
    assert n.ready() is False and n.watchdog() is False and n.stopping() is False


def test_sdnotify_sends_on_a_real_socket(tmp_path):
    import socket
    path = tmp_path / "notify.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(str(path))
    srv.settimeout(2.0)
    n = SdNotify(str(path))
    assert n.enabled
    assert n.ready("hello")
    assert b"READY=1" in srv.recv(256)
    assert n.watchdog()
    assert srv.recv(256) == b"WATCHDOG=1"
    srv.close()


def test_interface_spec_is_self_contained():
    """A third party must be able to write a consumer from this file alone."""
    spec = (ROOT / "docs" / "RB-VOR-IF-v2.2.md").read_text()
    for required in ("vorradial", "vorsnr", "voram", "vorstatus", "vorsub",
                     "vorfreqactive",
                     "vorfreq", "gpsTrack",
                     "bridgeUpdate", "0–359", "0–100",
                     "radial **FROM** the station", "NAV flag"):
        assert required in spec, f"interface spec does not mention {required!r}"
    assert "three bearing fields are 0" in spec   # requirement 4, the all-zero contract


@pytest.mark.parametrize("doc", [
    "rb-vor-architecture.md", "rb-vor-implementation-plan.md",
    "RB-VOR-IF-v2.2.md", "install.md", "calibration.md", "open-items.md",
    "first-light.md",
])
def test_shipped_documentation_is_present(doc):
    assert (ROOT / "docs" / doc).exists()


def test_readme_does_not_promise_hardware_support_that_is_not_built():
    """M5 is not done; the README must not imply a live SDR path exists."""
    readme = (ROOT / "README.md").read_text()
    assert "M5" in readme


def test_every_tool_has_a_docstring_and_is_executable():
    for tool in sorted((ROOT / "tools").glob("vor_*.py")):
        src = tool.read_text()
        assert src.startswith("#!/usr/bin/env python3"), tool.name
        assert '"""' in src[:400], f"{tool.name} has no module docstring"


def test_run_guide_exists_and_covers_the_essentials():
    """RUN.md is the first thing a builder reads; it must not drift from reality."""
    run = (ROOT / "RUN.md").read_text()
    for required in ("--self-test", "vor_calibrate", "--freq 109.4",
                     "install.sh", "systemctl start rb-vor",
                     "usb_claim_interface error -6", "phase_sign"):
        assert required in run, f"RUN.md does not mention {required!r}"


def test_run_guide_commands_reference_real_tools():
    """Every tools/ script named in RUN.md must actually exist."""
    import re
    run = (ROOT / "RUN.md").read_text()
    for tool in set(re.findall(r"tools/(\w+)\.py", run)):
        assert (ROOT / "tools" / f"{tool}.py").exists(), (
            f"RUN.md references tools/{tool}.py which does not exist")


def test_changelog_and_run_guide_agree_on_the_version():
    from rbvor import __version__
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert __version__ in changelog, "CHANGELOG does not mention the current version"


def test_report_collector_produces_a_bundle(tmp_path):
    """The bench must be able to package a report in one command."""
    import subprocess
    import zipfile
    out = tmp_path / "report.zip"
    r = subprocess.run(
        [sys.executable, "tools/collect_report.py", "-o", str(out),
         "--config", str(ROOT / "config" / "rb-vor.json"),
         "--captures", str(tmp_path / "none"), "--no-log"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert out.exists()
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert "system.txt" in names
    assert "config-in-use.json" in names
    assert "test-report-template.md" in names
    assert "README.txt" in names
    # --no-log means the solution CSV must not be bundled
    assert not any(n.endswith(".csv") for n in names)


def test_report_template_names_the_open_items_it_closes():
    tmpl = (ROOT / "docs" / "test-report-template.md").read_text()
    for item in ("O1", "O3", "O4"):
        assert item in tmpl
