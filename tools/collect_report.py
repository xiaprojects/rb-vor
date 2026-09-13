#!/usr/bin/env python3
"""Bundle everything the bench should send back into one zip.

Reference: docs/test-report-template.md; implementation plan M10.

After a bench session, run this. It gathers the pieces that let the maintainer
diagnose remotely -- the self-test result, the calibration, the config actually
in use, recent solution log, capture sidecars, and system facts -- into a single
archive, without the large IQ payloads (those go to an issue on request).

    python3 tools/collect_report.py -o /tmp/rb-vor-report.zip

Nothing here is sensitive by default, but the solution log may contain a
gps_track series (see docs/cloud-schema.md); --no-log omits it.
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbvor import __version__  # noqa: E402


def _run(cmd: list[str], timeout: float = 15.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:                           # noqa: BLE001
        return f"({' '.join(cmd)} failed: {e})"


def system_facts() -> str:
    lines = [f"rb-vor {__version__}",
             f"collected {datetime.now(tz=UTC).isoformat()}",
             f"python {platform.python_version()}",
             f"platform {platform.platform()}",
             f"machine {platform.machine()}", ""]
    try:
        lines.append("os-release:")
        lines.append(Path("/etc/os-release").read_text())
    except OSError:
        pass
    lines += ["", "lsusb:", _run(["lsusb"]),
              "", "rtl_test -t:", _run(["rtl_test", "-t"], timeout=6),
              "", "systemctl status rb-vor:",
              _run(["systemctl", "status", "rb-vor", "--no-pager"]),
              "", "journal (last 50):",
              _run(["journalctl", "-u", "rb-vor", "-n", "50", "--no-pager"])]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", type=Path,
                    default=Path("/tmp/rb-vor-report.zip"))
    ap.add_argument("--config", type=Path, default=Path("/etc/rb-vor/rb-vor.json"))
    ap.add_argument("--captures", type=Path, default=Path("captures"))
    ap.add_argument("--no-log", action="store_true",
                    help="omit the solution CSV (may contain a gps_track series)")
    ap.add_argument("--self-test", action="store_true",
                    help="run the self test and include its output")
    a = ap.parse_args(argv)

    manifest = []
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("system.txt", system_facts())
        manifest.append("system.txt")

        # config in use, with the calibration visible (not secret)
        if a.config.exists():
            z.write(a.config, "config-in-use.json")
            manifest.append("config-in-use.json")
            cfg = json.loads(a.config.read_text())
            csv_path = cfg.get("log", {}).get("solution_csv")
            if csv_path and not a.no_log and Path(csv_path).exists():
                # last 2000 lines is plenty for diagnosis
                data = Path(csv_path).read_text().splitlines()
                tail = "\n".join(data[:1] + data[-2000:])
                z.writestr("solutions-tail.csv", tail)
                manifest.append("solutions-tail.csv")

        # every capture SIDECAR (small); never the IQ itself
        if a.captures.exists():
            for j in sorted(a.captures.glob("*.json")):
                z.write(j, f"captures/{j.name}")
                manifest.append(f"captures/{j.name}")

        if a.self_test:
            out = _run([sys.executable, "-m", "rbvor", "--self-test"], timeout=60)
            z.writestr("self-test.txt", out)
            manifest.append("self-test.txt")

        # a blank report template to fill in
        tmpl = Path(__file__).resolve().parents[1] / "docs" / "test-report-template.md"
        if tmpl.exists():
            z.write(tmpl, "test-report-template.md")
            manifest.append("test-report-template.md")

        readme = io.StringIO()
        readme.write("RB-VOR bench report bundle\n")
        readme.write("=" * 30 + "\n\n")
        readme.write("Contents:\n")
        for m in manifest:
            readme.write(f"  - {m}\n")
        readme.write("\nIQ files are NOT included (too large). If a capture\n")
        readme.write("decoded badly, attach its .cu8 to a GitHub issue; its\n")
        readme.write("sidecar (.json) is here and identifies it.\n")
        readme.write("\nFill in test-report-template.md and include it.\n")
        z.writestr("README.txt", readme.getvalue())

    print(f"wrote {a.out}  ({len(manifest) + 1} items)")
    for m in manifest:
        print(f"  {m}")
    print("\n  IQ payloads omitted by design; attach any bad capture to an issue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
