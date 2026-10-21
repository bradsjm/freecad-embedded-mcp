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

# A nonnegative finite measurement or endpoint shared by the range fragments.
_RANGE_NUMBER = {"type": "number", "minimum": 0}

# A topology count endpoint, bounded by the query candidate cap.
_RANGE_COUNT = {"type": "integer", "minimum": 0, "maximum": 4096}

# Each target accepts at most this many topology count checks.
MAX_COUNT_CHECKS_PER_TARGET = 8

# One operation evaluates at most this many topology count queries in total.
MAX_COUNT_QUERIES_PER_OPERATION = 32

# The expected volume is an inclusive range in cubic millimetres.
_EXPECTED_VOLUME = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"min": _RANGE_NUMBER, "max": _RANGE_NUMBER},
    "description": (
        "Post-write volume contract in mm3: inclusive min and/or max (at least one). "
        "The target must be a valid solid with positive volume. A miss or an "
        "unreadable volume refuses the write and rolls it back."
    ),
}

# Each count check states how many subshapes a declarative query must match.
_EXPECTED_TOPOLOGY_COUNT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {"$ref": "#/$defs/topologyQuery"},
        "min": _RANGE_COUNT,
        "max": _RANGE_COUNT,
    },
}

# Count checks evaluate set queries in document space; zero matches count as zero.
_EXPECTED_TOPOLOGY_COUNTS = {
    "type": "array",
    "minItems": 1,
    "maxItems": MAX_COUNT_CHECKS_PER_TARGET,
    "items": _EXPECTED_TOPOLOGY_COUNT,
    "description": (
        "Post-write topology contracts: each item counts the faces or edges a "
        "declarative query matches (zero matches count as zero) and needs min "
        "and/or max. For exactly four cylindrical faces use "
        'query [{"role": "face", "selector": "%CYLINDER"}], min 4, max 4.'
    ),
}

# Both write-time geometry expectations share one property pair.
EXPECTATION_PROPERTIES = {
    "expected_volume": _EXPECTED_VOLUME,
    "expected_topology_counts": _EXPECTED_TOPOLOGY_COUNTS,
}

# One reported count check echoes its request plus the measured match count.
_EXPECTATION_COUNT_RESULT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query", "measured"],
    "properties": {
        "query": {"$ref": "#/$defs/topologyQuery"},
        "min": _RANGE_COUNT,
        "max": _RANGE_COUNT,
        "measured": _RANGE_COUNT,
    },
}

# Passed expectation rows appear per target and only for the requested checks.
_EXPECTATION_RESULTS = {
    "type": "array",
    "maxItems": 32,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["object"],
        "properties": {
            "object": {"type": "string"},
            "volume": {
                "type": "object",
                "additionalProperties": False,
                "required": ["measured"],
                "properties": {
                    "min": _RANGE_NUMBER,
                    "max": _RANGE_NUMBER,
                    "measured": _RANGE_NUMBER,
                },
            },
            "topologyCounts": {
                "type": "array",
                "maxItems": MAX_COUNT_CHECKS_PER_TARGET,
                "items": _EXPECTATION_COUNT_RESULT,
            },
        },
    },
    "description": (
        "Only requested write-time expectations, reported after commit; a passed "
        "check appears with its measurement and a failed check refuses the write."
    ),
}

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
