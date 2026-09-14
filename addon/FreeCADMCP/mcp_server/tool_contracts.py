"""Shared FreeCAD-free schema fragments and freshness checks for tools."""

from __future__ import annotations

from typing import Any

from .protocol import VALIDATION_FAILED, ToolError, stale_generation_details

# The optional document-generation guard protects plans made from inspection.
_EXPECTED_GENERATION = {
    "type": ["integer", "null"],
    "minimum": 0,
    "description": (
        "Optional guard: refuse when the document generation no longer matches the inspected value."
    ),
}

# The expected solid count is an explicit post-mutation geometry contract.
_EXPECTED_SOLIDS = {"type": "integer", "minimum": 0}

# Bounds use document-space minimum and maximum coordinates.
_EXPECTED_BOUNDS = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 6,
    "maxItems": 6,
}

# Tolerance is bounded to keep geometry comparisons predictable.
_BOUNDS_TOLERANCE = {
    "type": "number",
    "minimum": 0,
    "maximum": 1000000,
    "default": 0.000001,
}

# This value matches the handler default when no tolerance is supplied.
_DEFAULT_BOUNDS_TOLERANCE = 0.000001

# Mutation results share one bounded geometry evidence shape.
_GEOMETRY_REPORT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name",
        "state",
        "object_valid",
        "shape_valid",
        "solid_count",
        "volume",
        "bounds",
        "diagnostics",
        "max_tolerance",
        "ok",
        "error",
    ],
    "properties": {
        "name": {"type": "string"},
        "state": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
        "object_valid": {"type": "boolean"},
        "shape_valid": {"type": ["boolean", "null"]},
        "solid_count": {"type": ["integer", "null"], "minimum": 0},
        "volume": {"type": ["number", "null"]},
        "bounds": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 6,
            "maxItems": 6,
        },
        "geometryUnavailable": {"type": "string", "minLength": 1},
        "diagnostics": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
        "max_tolerance": {"type": ["number", "null"]},
        "ok": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
    },
}


def require_expected_generation(
    ctx: Any,
    doc: Any,
    arguments: dict[str, Any],
    *,
    message: str,
    next_tool: str,
) -> None:
    """Refuse a mutation plan when the document generation is stale."""

    expected = arguments.get("expected_generation")
    if expected is None:
        return
    actual = int(ctx.document_generation(doc))
    if expected == actual:
        return
    raise ToolError(
        VALIDATION_FAILED,
        message,
        stale_generation_details(expected, actual, next_tool),
    )
