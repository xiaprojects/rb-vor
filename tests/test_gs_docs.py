"""RB-VOR-IF v2.4 is self-contained, and its pairing table -- a third copy of
the safety data -- agrees with the code (CLAUDE.md A39, A44)."""

from __future__ import annotations

import re
from pathlib import Path

from rbvor import IF_VERSION
from rbvor.freqrules import GS_PAIRING_KHZ

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "docs" / "RB-VOR-IF-v2.4.md").read_text()


def test_the_receiver_claims_v24():
    assert IF_VERSION == "RB-VOR-IF v2.4"


def test_spec_names_every_field_and_the_switch():
    for k in ("gsstatus", "gsdev", "gsddm", "gssdm", "gssnr", "gsfreqactive", "gsshared",
              "gsselect", "locdev", "vorradial", "vorfreq"):
        assert f"`{k}`" in SPEC, k
    assert "POST /bridgeUpdate" in SPEC


def test_spec_carries_the_safety_statements():
    for s in ("**neither the localizer nor the glide-slope output is", "fly up",
              "False glide paths",
              "must not draw the glide-slope marker unless `gsstatus >= 4`",
              "same POST", "byte-for-byte v2.3"):
        assert s in SPEC, s


def test_the_spec_pairing_table_agrees_with_the_code():
    rows = re.findall(r"^\| (1\d\d\.\d\d) \| (3\d\d\.\d\d) \| (1\d\d\.\d\d) \| (3\d\d\.\d\d) "
                      r"\| (1\d\d\.\d\d) \| (3\d\d\.\d\d) \| (1\d\d\.\d\d) \| (3\d\d\.\d\d) \|$",
                      SPEC, flags=re.M)
    pairs = {}
    for r in rows:
        for i in range(0, 8, 2):
            pairs[int(round(float(r[i]) * 1000))] = int(round(float(r[i + 1]) * 1000))
    assert pairs == GS_PAIRING_KHZ
