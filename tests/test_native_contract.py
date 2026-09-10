"""The committed native contract record must be complete and current.

This test fails when tests/native_contract.json is stale or missing a
required probe; it needs no FreeCAD.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _native_contract import CONTRACT, REQUIRED


def test_record_schema_and_freecad_version():
    assert CONTRACT["schema"] == 1
    assert str(CONTRACT["freecad"]).startswith("1.1.")


def test_every_required_probe_is_present():
    for name in REQUIRED:
        assert name in CONTRACT["probes"], f"missing probe: {name}"


def test_every_probe_reports_an_outcome():
    for name, payload in CONTRACT["probes"].items():
        assert isinstance(payload, dict), name
        assert "outcome" in payload, f"probe without outcome: {name}"
