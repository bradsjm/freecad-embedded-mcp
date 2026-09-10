"""Shared loader for the recorded native FreeCAD contract.

AGENTS.md deviation note: this non-collected helper module exists because
many test files must load the same record; a shared import-time binding
is the documented allowed exception to the per-file convention.
"""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "native_contract.json"
CONTRACT: dict = json.loads(_PATH.read_text(encoding="utf-8"))

#: Every probe name the verification run must capture (Phase 3 list).
REQUIRED: tuple[str, ...] = (
    "server.version",
    "shape.null_attributes",
    "attachment.properties",
    "geometry.attributes",
    "geometry.getConstruction",
    "constraint.attributes",
    "constraint.forms",
    "solver.attributes",
    "setDatum.string",
    "setDatum.quantity",
    "object.property_status",
    "mesh.pipeline",
    "part.read",
    "gui.selection",
    "qt.signals",
    "fem.objects",
    "doc.lifecycle",
)


def probe(name: str) -> dict:
    """Return the raw recorded probe payload; KeyError when absent."""

    return CONTRACT["probes"][name]
