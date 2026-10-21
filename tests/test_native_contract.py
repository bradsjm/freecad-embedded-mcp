"""The committed native contract record must be complete and passing.

This test fails when tests/native_contract.json is stale, misses a required
probe, or records a probe that failed on the live FreeCAD; it needs no FreeCAD.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _native_contract import CONTRACT, REQUIRED

# Probe outcomes that mean the live run failed rather than recorded behavior.
# ``rejected`` is a recorded native refusal and is a valid outcome.
_FAILED_OUTCOMES = {"mismatch", "crash", "abort", "error", "script_error", "create_error"}


def _supported_version(version: str) -> bool:
    parts = tuple(int(part) for part in version.split(".")[:3])
    return (1, 1, 3) <= parts < (1, 2, 0)


def test_record_schema_and_freecad_version():
    assert CONTRACT["schema"] == 1
    assert _supported_version(str(CONTRACT["freecad"])), CONTRACT["freecad"]


def test_every_required_probe_is_present():
    missing = [name for name in REQUIRED if name not in CONTRACT["probes"]]
    assert not missing, f"missing probes: {missing}"


def test_no_probe_records_a_failed_run():
    failed = {
        name: payload.get("outcome") if isinstance(payload, dict) else payload
        for name, payload in CONTRACT["probes"].items()
        if not isinstance(payload, dict)
        or not isinstance(payload.get("outcome"), str)
        or payload["outcome"] in _FAILED_OUTCOMES
    }
    assert not failed, f"probes without a passing outcome: {failed}"
