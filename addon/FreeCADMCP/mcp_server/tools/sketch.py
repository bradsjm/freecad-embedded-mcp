"""``inspect_sketch`` / ``edit_sketch``: structured Sketcher access.

Both handlers run on the GUI thread. Inspection is read-only: it reads
native geometry and constraint rows in native index order (0-based, as
FreeCAD reports them) plus the last solved state — it never calls
``solve()``, recomputes, or opens an edit session — and discloses
constraint expression bindings. Editing applies one batch of operations
inside the shared ``object_validation.mutation`` gate: deletes first in
descending index order, then additions, then datum edits. Every referenced
index is prevalidated against a simulated index state before the
transaction opens, so a bad index never opens one. The planning itself lives in
sketch_plan.py; this module keeps the wire schemas, native construction
and handlers.

Both responses also carry the native object state names and the status
string; only the edit response runs the solver (validating the batch)
and reports its status code, while inspection reports ``solverStatus``
null because no prior native status is exposed. An edit batch may carry
``expected_generation``; a mismatch is refused before planning, so a
stale batch never opens a transaction either.

Inspection and edit behavior mirror the native 1.1.3 contract recorded
in tests/native_contract.json: the solver summary reads the DoF and
FullyConstrained attributes, construction state comes from
sketch.getConstruction(index), and constraint driving state comes from
the Driving attribute. A missing mutation method rejects the operation
before the transaction with VALIDATION_FAILED naming the missing
native method.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import input_aliases as _aliases
from ..object_validation import mutation
from ..protocol import VALIDATION_FAILED, ToolError, check_schema
from ..tool_contracts import require_expected_generation
from .sketch_plan import (
    _CONSTRAINT_FORM_ARITIES,
    _GEOMETRY_KINDS,
    _MAX_CONSTRAINT_ARGUMENTS,
    _MAX_OPERATIONS,
    _MAX_SKETCH_ROWS,
    _fail,
    _finite,
    _plan_sketch_edit,
)

_MAX_STATE_NAMES = 32
_SOLVER_MESSAGE_LIMIT = 16

_SKETCH_TYPE_ID = "Sketcher::SketchObject"

_SKETCH_FIELD = {"type": "string", "minLength": 1}
_CONSTRUCTION = {"type": "boolean", "default": False}
_XY_PAIR = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 2,
    "maxItems": 2,
}
_LOCAL_GEOMETRY_REF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["geometry"],
    "properties": {"geometry": {"type": "string", "minLength": 1, "maxLength": 64}},
}
_INTEGER_ARGUMENTS = {
    "type": "array",
    "items": {"anyOf": [{"type": "integer"}, _LOCAL_GEOMETRY_REF]},
    "minItems": 1,
    "maxItems": _MAX_CONSTRAINT_ARGUMENTS,
}

_GEOMETRY_ADD_SCHEMA = {
    "anyOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "x", "y"],
            "properties": {
                "kind": {"const": "point"},
                "id": {"type": "string", "minLength": 1, "maxLength": 64},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "construction": _CONSTRUCTION,
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "start", "end"],
            "properties": {
                "kind": {"const": "lineSegment"},
                "id": {"type": "string", "minLength": 1, "maxLength": 64},
                "start": _XY_PAIR,
                "end": _XY_PAIR,
                "construction": _CONSTRUCTION,
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "center", "radius"],
            "properties": {
                "kind": {"const": "circle"},
                "id": {"type": "string", "minLength": 1, "maxLength": 64},
                "center": _XY_PAIR,
                "radius": {"type": "number", "exclusiveMinimum": 0},
                "construction": _CONSTRUCTION,
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "center", "radius", "startAngle", "endAngle"],
            "properties": {
                "kind": {"const": "arcOfCircle"},
                "id": {"type": "string", "minLength": 1, "maxLength": 64},
                "center": _XY_PAIR,
                "radius": {"type": "number", "exclusiveMinimum": 0},
                "startAngle": {"type": "number"},
                "endAngle": {"type": "number"},
                "construction": _CONSTRUCTION,
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "origin", "width", "height"],
            "properties": {
                "kind": {"const": "rectangle"},
                "origin": _XY_PAIR,
                "width": {"type": "number", "exclusiveMinimum": 0},
                "height": {"type": "number", "exclusiveMinimum": 0},
                "construction": _CONSTRUCTION,
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "points", "closed"],
            "properties": {
                "kind": {"const": "polyline"},
                "points": {"type": "array", "items": _XY_PAIR, "minItems": 2, "maxItems": 32},
                "closed": {"type": "boolean"},
                "construction": _CONSTRUCTION,
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "center", "radius", "sides"],
            "properties": {
                "kind": {"const": "regularPolygon"},
                "center": _XY_PAIR,
                "radius": {"type": "number", "exclusiveMinimum": 0},
                "sides": {"type": "integer", "minimum": 3, "maximum": 32},
                "rotation": {"type": "number", "default": 0},
                "construction": _CONSTRUCTION,
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "length", "diameter"],
            "properties": {
                "kind": {"const": "slot"},
                "length": {"type": "number", "exclusiveMinimum": 0},
                "diameter": {"type": "number", "exclusiveMinimum": 0},
                "center": {**_XY_PAIR, "default": [0.0, 0.0]},
                "rotation": {"type": "number", "default": 0},
                "construction": _CONSTRUCTION,
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "width", "height", "corner_radius"],
            "properties": {
                "kind": {"const": "rounded_rectangle"},
                "width": {"type": "number", "exclusiveMinimum": 0},
                "height": {"type": "number", "exclusiveMinimum": 0},
                "corner_radius": {"type": "number", "exclusiveMinimum": 0},
                "origin": {**_XY_PAIR, "default": [0.0, 0.0]},
                "construction": _CONSTRUCTION,
            },
        },
    ]
}

_CONSTRAINT_ADD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "arguments"],
    "properties": {
        "type": {"type": "string", "enum": list(_CONSTRAINT_FORM_ARITIES)},
        "arguments": _INTEGER_ARGUMENTS,
        "datum": {
            "type": "string",
            "minLength": 1,
            "description": ("Optional datum as a unit string (e.g. '10 mm', '45 deg')."),
        },
    },
}

_DATUM_SET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["index", "datum"],
    "properties": {
        "index": {"type": "integer", "minimum": 0},
        "datum": {"type": "string", "minLength": 1},
    },
}

_EXPRESSION_SET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["index", "expression"],
    "properties": {
        "index": {"type": "integer", "minimum": 0},
        "expression": {"type": ["string", "null"], "minLength": 1, "maxLength": 256},
    },
}

_STATE_NAMES = {
    "type": "array",
    "items": {"type": "string"},
    "maxItems": _MAX_STATE_NAMES,
}

_SOLVER_SUMMARY = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "fullyConstrained",
        "degreesOfFreedom",
        "solverMessages",
        "solverMessageCount",
        "solverMessagesTruncated",
        "solverStatus",
    ],
    "properties": {
        "fullyConstrained": {"type": ["boolean", "null"]},
        "degreesOfFreedom": {"type": ["integer", "null"], "minimum": 0},
        "solverMessages": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": _SOLVER_MESSAGE_LIMIT,
        },
        "solverMessageCount": {"type": "integer", "minimum": 0},
        "solverMessagesTruncated": {"type": "boolean"},
        "solverStatus": {"type": ["integer", "null"]},
    },
}

_SKETCH_GEOMETRY_ROW = {
    "anyOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["index", "kind", "x", "y", "construction"],
            "properties": {
                "index": {"type": "integer", "minimum": 0},
                "kind": {"const": "point"},
                "x": {"type": ["number", "null"]},
                "y": {"type": ["number", "null"]},
                "construction": {"type": "boolean"},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "index",
                "kind",
                "startX",
                "startY",
                "endX",
                "endY",
                "construction",
            ],
            "properties": {
                "index": {"type": "integer", "minimum": 0},
                "kind": {"const": "lineSegment"},
                "startX": {"type": ["number", "null"]},
                "startY": {"type": ["number", "null"]},
                "endX": {"type": ["number", "null"]},
                "endY": {"type": ["number", "null"]},
                "construction": {"type": "boolean"},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "index",
                "kind",
                "centerX",
                "centerY",
                "radius",
                "construction",
            ],
            "properties": {
                "index": {"type": "integer", "minimum": 0},
                "kind": {"const": "circle"},
                "centerX": {"type": ["number", "null"]},
                "centerY": {"type": ["number", "null"]},
                "radius": {"type": ["number", "null"]},
                "construction": {"type": "boolean"},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "index",
                "kind",
                "centerX",
                "centerY",
                "radius",
                "startAngle",
                "endAngle",
                "construction",
            ],
            "properties": {
                "index": {"type": "integer", "minimum": 0},
                "kind": {"const": "arcOfCircle"},
                "centerX": {"type": ["number", "null"]},
                "centerY": {"type": ["number", "null"]},
                "radius": {"type": ["number", "null"]},
                "startAngle": {"type": ["number", "null"]},
                "endAngle": {"type": ["number", "null"]},
                "construction": {"type": "boolean"},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["index", "kind", "type"],
            "properties": {
                "index": {"type": "integer", "minimum": 0},
                "kind": {"const": "unsupported"},
                "type": {"type": "string"},
            },
        },
    ]
}

_SKETCH_CONSTRAINT_ROW = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "index",
        "type",
        "first",
        "firstPos",
        "second",
        "secondPos",
        "third",
        "thirdPos",
        "datum",
        "driving",
        "active",
        "name",
    ],
    "properties": {
        "index": {"type": "integer", "minimum": 0},
        "type": {"type": ["string", "null"]},
        "first": {"type": ["integer", "null"]},
        "firstPos": {"type": ["integer", "null"]},
        "second": {"type": ["integer", "null"]},
        "secondPos": {"type": ["integer", "null"]},
        "third": {"type": ["integer", "null"]},
        "thirdPos": {"type": ["integer", "null"]},
        "datum": {"type": ["string", "null"]},
        "driving": {"type": ["boolean", "null"]},
        "active": {"type": ["boolean", "null"]},
        "name": {"type": ["string", "null"]},
    },
}

_INSPECT_SKETCH_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "sketch"],
    "properties": {
        "document": {"type": "string", "minLength": 1},
        "sketch": _SKETCH_FIELD,
    },
}

_INSPECT_SKETCH_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "document",
        "generation",
        "object",
        "solver",
        "state",
        "statusText",
        "geometry",
        "constraints",
        "expressionBindings",
        "geometryCount",
        "constraintsCount",
        "expressionBindingsCount",
        "geometryTruncated",
        "constraintsTruncated",
        "expressionBindingsTruncated",
        "geometryUnavailable",
        "constraintsUnavailable",
    ],
    "properties": {
        "document": {"type": "string"},
        "generation": {"type": "integer", "minimum": 0},
        "object": {"type": "string"},
        "solver": _SOLVER_SUMMARY,
        "state": _STATE_NAMES,
        "statusText": {"type": ["string", "null"]},
        "geometry": {
            "type": "array",
            "items": _SKETCH_GEOMETRY_ROW,
            "maxItems": _MAX_SKETCH_ROWS,
        },
        "constraints": {
            "type": "array",
            "items": _SKETCH_CONSTRAINT_ROW,
            "maxItems": _MAX_SKETCH_ROWS,
        },
        "expressionBindings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["constraint", "expression"],
                "properties": {
                    "constraint": {"type": "string"},
                    "expression": {"type": "string"},
                },
            },
            "maxItems": _MAX_SKETCH_ROWS,
        },
        "geometryCount": {"type": "integer", "minimum": 0},
        "constraintsCount": {"type": "integer", "minimum": 0},
        "expressionBindingsCount": {"type": "integer", "minimum": 0},
        "geometryTruncated": {"type": "boolean"},
        "constraintsTruncated": {"type": "boolean"},
        "expressionBindingsTruncated": {"type": "boolean"},
        "geometryUnavailable": {"type": ["string", "null"], "maxLength": 256},
        "constraintsUnavailable": {"type": ["string", "null"], "maxLength": 256},
    },
}

_EDIT_SKETCH_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "sketch"],
    "properties": {
        "document": {"type": "string", "minLength": 1},
        "sketch": _SKETCH_FIELD,
        "expected_generation": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Optional guard: refuse the batch when the document generation no longer matches."
            ),
        },
        "addGeometry": {
            "type": "array",
            "items": _GEOMETRY_ADD_SCHEMA,
            "maxItems": _MAX_OPERATIONS,
        },
        "addConstraints": {
            "type": "array",
            "items": _CONSTRAINT_ADD_SCHEMA,
            "maxItems": _MAX_OPERATIONS,
        },
        "setDatums": {
            "type": "array",
            "items": _DATUM_SET_SCHEMA,
            "maxItems": _MAX_OPERATIONS,
        },
        "setExpressions": {
            "type": "array",
            "items": _EXPRESSION_SET_SCHEMA,
            "maxItems": _MAX_OPERATIONS,
        },
        "deleteGeometry": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "maxItems": _MAX_OPERATIONS,
        },
        "deleteConstraints": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "maxItems": _MAX_OPERATIONS,
        },
    },
}

_EDIT_SKETCH_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "document",
        "generation",
        "object",
        "solver",
        "state",
        "statusText",
        "addedGeometry",
        "addedGeometryIds",
        "addedConstraints",
        "deletedGeometry",
        "deletedConstraints",
        "changedDatums",
        "changedExpressions",
        "clearedExpressions",
        "expressionBindings",
    ],
    "properties": {
        "document": {"type": "string"},
        "generation": {"type": "integer", "minimum": 0},
        "object": {"type": "string"},
        "solver": _SOLVER_SUMMARY,
        "state": _STATE_NAMES,
        "statusText": {"type": ["string", "null"]},
        "addedGeometry": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "maxItems": _MAX_OPERATIONS,
        },
        "addedGeometryIds": {
            "type": "object",
            "additionalProperties": {"type": "integer", "minimum": 0},
        },
        "addedConstraints": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "maxItems": _MAX_OPERATIONS,
        },
        "deletedGeometry": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "maxItems": _MAX_OPERATIONS,
        },
        "deletedConstraints": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "maxItems": _MAX_OPERATIONS,
        },
        "changedDatums": {
            "type": "array",
            "items": {"$ref": "#/$defs/datumRow"},
            "maxItems": _MAX_OPERATIONS,
        },
        "changedExpressions": {
            "type": "array",
            "items": {"$ref": "#/$defs/expressionRow"},
            "maxItems": _MAX_OPERATIONS,
        },
        "clearedExpressions": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "maxItems": _MAX_OPERATIONS,
        },
        "expressionBindings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["constraint", "expression"],
                "properties": {
                    "constraint": {"type": "string"},
                    "expression": {"type": "string"},
                },
            },
            "maxItems": _MAX_SKETCH_ROWS,
        },
    },
    "$defs": {
        "datumRow": {
            "type": "object",
            "additionalProperties": False,
            "required": ["index", "datum"],
            "properties": {
                "index": {"type": "integer", "minimum": 0},
                "datum": {"type": "string"},
            },
        },
        "expressionRow": {
            "type": "object",
            "additionalProperties": False,
            "required": ["index", "expression"],
            "properties": {
                "index": {"type": "integer", "minimum": 0},
                "expression": {"type": "string"},
            },
        },
    },
}

#: Liberal input (Postel): accept the synonym, emit the canonical kind.
#: ``arc``, ``rect``, and ``polygon`` name the native kinds unambiguously
#: inside this closed set; case and separator variants fold for free.
_GEOMETRY_KIND_ALIASES = {
    "arc": "arcOfCircle",
    "rect": "rectangle",
    "polygon": "regularPolygon",
}
_GEOMETRY_KIND_TABLE = _aliases.build_table(_GEOMETRY_KINDS, _GEOMETRY_KIND_ALIASES)
_CONSTRAINT_TYPE_TABLE = _aliases.build_table(_CONSTRAINT_FORM_ARITIES)
_EDIT_SKETCH_NORMALIZER_SPEC = {
    "addGeometry[].kind": _GEOMETRY_KIND_TABLE,
    "addConstraints[].type": _CONSTRAINT_TYPE_TABLE,
}


def _normalize_edit_sketch_arguments(arguments: dict) -> dict:
    """Fold sketch geometry and constraint spellings to canonical native names."""

    return _aliases.normalize_arguments(arguments, _EDIT_SKETCH_NORMALIZER_SPEC)


TOOL_DEFINITIONS = [
    {
        "name": "inspect_sketch",
        "description": (
            "Inspect one Sketcher::SketchObject: geometry rows (point, line "
            "segment, circle, arc of circle; other kinds report an "
            "unsupported marker with the native type name) and constraint "
            "rows in native 0-based index order, the solver degree-of-"
            "freedom summary and the constraint expression bindings. "
            "The inspection is read-only: it never solves, recomputes or "
            "opens an edit session, so solverStatus is null (edit_sketch "
            "reports the post-batch solve status). "
            "Unavailable native getters report null or empty fields. "
            "geometryUnavailable and constraintsUnavailable carry the "
            "reason when FreeCAD refuses to read that collection (null "
            "when readable); a marked collection's rows and count are "
            "placeholders, not verified empty data. "
            "geometry, constraints and expressionBindings cap at 4096 "
            "rows; the Count fields report full totals and the Truncated "
            "flags mark capping."
        ),
        "inputSchema": _INSPECT_SKETCH_INPUT,
        "outputSchema": _INSPECT_SKETCH_OUTPUT,
    },
    {
        "name": "edit_sketch",
        "normalize": _normalize_edit_sketch_arguments,
        "description": (
            "Apply one atomic batch of Sketcher operations: addGeometry, "
            "addConstraints, setDatums, deleteGeometry and "
            "deleteConstraints. Every referenced index is prevalidated "
            "against a simulated index state before the transaction opens; "
            "deletes run in descending index order, then additions, then "
            "datum edits, all inside one mutation that rolls back the whole "
            "batch on failure. Datums are unit strings ('10 mm', '45 deg'); "
            "angle units apply to Angle constraints. The result reports the "
            "actual native indexes and the post-edit solver summary. "
            "A one-row addGeometry entry may carry a request-local id, "
            'constraint arguments may reference it as {"geometry": "<id>"} '
            "in geometry slots, and the response maps every id to its "
            "committed index in addedGeometryIds. "
            "Constraint shapes without a recorded native acceptance are "
            "refused before execution because malformed native constructor "
            "calls can terminate FreeCAD."
        ),
        "inputSchema": _EDIT_SKETCH_INPUT,
        "outputSchema": _EDIT_SKETCH_OUTPUT,
    },
]

HANDLERS: dict[str, Callable[[Any, dict], Any]] = {}

check_schema(_INSPECT_SKETCH_INPUT)
check_schema(_INSPECT_SKETCH_OUTPUT)
check_schema(_EDIT_SKETCH_INPUT)
check_schema(_EDIT_SKETCH_OUTPUT)


# ---------------------------------------------------------------------------
# Small read helpers.
# ---------------------------------------------------------------------------


def _require_sketch(obj: Any) -> None:
    """Refuse any object that is not a Sketcher sketch."""
    derived = getattr(obj, "isDerivedFrom", None)
    if callable(derived):
        try:
            if derived(_SKETCH_TYPE_ID):
                return
        except Exception:
            pass
    if str(getattr(obj, "TypeId", "")) == _SKETCH_TYPE_ID:
        return
    raise _fail(f"object '{getattr(obj, 'Name', '<unknown>')}' is not a {_SKETCH_TYPE_ID}")


def _int_or_none(value: Any) -> int | None:
    """Return the value as an int, or None for non-ints and bools."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bool_or_none(value: Any) -> bool | None:
    """Return the value when it is a bool, else None."""
    return value if isinstance(value, bool) else None


def _string_or_none(value: Any) -> str | None:
    """Return the value when it is a non-empty string, else None."""
    if not isinstance(value, str) or not value:
        return None
    return value


# ---------------------------------------------------------------------------
# Inspection.
# ---------------------------------------------------------------------------


def _construction_flag(sketch: Any, index: int, geo: Any) -> bool:
    """Native construction state; the element attribute does not exist."""

    getter = getattr(sketch, "getConstruction", None)
    if callable(getter):
        try:
            return bool(getter(index))
        except Exception:
            pass
    return bool(getattr(geo, "Construction", False))


def _geometry_row(index: int, geo: Any, sketch: Any) -> dict:
    """Project one geometry element into its wire row by native kind."""
    construction = _construction_flag(sketch, index, geo)
    circle = getattr(geo, "Circle", None)
    first = getattr(geo, "FirstParameter", None)
    last = getattr(geo, "LastParameter", None)
    if circle is not None and first is not None and last is not None:
        # ArcOfCircle also exposes StartPoint/EndPoint, so it must be
        # classified before the line-segment test.
        center = getattr(circle, "Center", None)
        return {
            "index": index,
            "kind": "arcOfCircle",
            "centerX": _finite(getattr(center, "x", None)),
            "centerY": _finite(getattr(center, "y", None)),
            "radius": _finite(getattr(circle, "Radius", None)),
            "startAngle": _finite(first),
            "endAngle": _finite(last),
            "construction": construction,
        }
    start = getattr(geo, "StartPoint", None)
    end = getattr(geo, "EndPoint", None)
    if start is not None and end is not None:
        return {
            "index": index,
            "kind": "lineSegment",
            "startX": _finite(getattr(start, "x", None)),
            "startY": _finite(getattr(start, "y", None)),
            "endX": _finite(getattr(end, "x", None)),
            "endY": _finite(getattr(end, "y", None)),
            "construction": construction,
        }
    radius = getattr(geo, "Radius", None)
    center = getattr(geo, "Center", None)
    if radius is not None and center is not None:
        return {
            "index": index,
            "kind": "circle",
            "centerX": _finite(getattr(center, "x", None)),
            "centerY": _finite(getattr(center, "y", None)),
            "radius": _finite(radius),
            "construction": construction,
        }
    if hasattr(geo, "x") and hasattr(geo, "y"):
        return {
            "index": index,
            "kind": "point",
            "x": _finite(getattr(geo, "x", None)),
            "y": _finite(getattr(geo, "y", None)),
            "construction": construction,
        }
    return {"index": index, "kind": "unsupported", "type": type(geo).__name__}


def _datum_string(constraint: Any) -> str | None:
    """Render a constraint datum as a unit string ('10 mm', '45 deg')."""
    value = _finite(getattr(constraint, "Value", None))
    if value is None:
        return None
    unit = "deg" if getattr(constraint, "Type", "") == "Angle" else "mm"
    return f"{value} {unit}"


def _constraint_row(index: int, constraint: Any) -> dict:
    """Project one constraint into its wire row."""
    return {
        "index": index,
        "type": _string_or_none(getattr(constraint, "Type", None)),
        "first": _int_or_none(getattr(constraint, "First", None)),
        "firstPos": _int_or_none(getattr(constraint, "FirstPos", None)),
        "second": _int_or_none(getattr(constraint, "Second", None)),
        "secondPos": _int_or_none(getattr(constraint, "SecondPos", None)),
        "third": _int_or_none(getattr(constraint, "Third", None)),
        "thirdPos": _int_or_none(getattr(constraint, "ThirdPos", None)),
        "datum": _datum_string(constraint),
        "driving": _bool_or_none(getattr(constraint, "Driving", None)),
        "active": _bool_or_none(getattr(constraint, "IsActive", None)),
        "name": _string_or_none(getattr(constraint, "Name", None)),
    }


def _expression_bindings_all(sketch: Any) -> list[dict]:
    """List every constraint expression binding in native order."""
    bindings: list[dict] = []
    engine = getattr(sketch, "ExpressionEngine", None)
    if not isinstance(engine, (list, tuple)):
        return bindings
    for entry in engine:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        path, expression = entry
        text = str(path)
        if "Constraints" not in text:
            continue
        name = text.rsplit(".", 1)[-1].strip()
        bindings.append({"constraint": name, "expression": str(expression)})
    return bindings


def _expression_bindings(sketch: Any) -> list[dict]:
    """Return the constraint bindings capped at the wire row limit."""
    return _expression_bindings_all(sketch)[:_MAX_SKETCH_ROWS]


def _state_names(sketch: Any) -> list[str]:
    """Native object state names; empty when absent or not a sequence."""

    state = getattr(sketch, "State", None)
    if not isinstance(state, (list, tuple)):
        return []
    return [str(entry) for entry in state[:_MAX_STATE_NAMES]]


def _status_text(sketch: Any) -> str | None:
    """Native status string; null when the accessor is absent or fails."""

    getter = getattr(sketch, "getStatusString", None)
    if not callable(getter):
        return None
    try:
        value = getter()
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _solver_summary(sketch: Any, run_solve: bool = False) -> dict:
    """Solver summary for one sketch, read without mutating it.

    ``DoF``, ``FullyConstrained``, the solver messages and the status text
    are plain reads of the last solved state: inspection never calls
    ``solve()`` or recomputes. ``run_solve=True`` (edit_sketch's
    post-mutation validation) runs the native solver once and reports its
    status code; without it ``solverStatus`` stays null because no prior
    native status is exposed.
    """

    solver_status: int | None = None
    if run_solve:
        solve = getattr(sketch, "solve", None)
        if callable(solve):
            try:
                solver_status = _int_or_none(solve())
            except Exception:
                solver_status = None
    # solve() returns a solver status code, never a degree count:
    # a sketch with 7 remaining DoF returned 0.
    dof = _int_or_none(getattr(sketch, "DoF", None))
    if dof is None:
        getter = getattr(sketch, "getSolverDoF", None)
        if callable(getter):
            try:
                dof = _int_or_none(getter())
            except Exception:
                dof = None
    if dof is not None and dof < 0:
        dof = None
    fully = _bool_or_none(getattr(sketch, "FullyConstrained", None))
    if fully is None and dof is not None:
        fully = dof == 0
    messages: list[str] = []
    message_count = 0
    messages_truncated = False
    get_messages = getattr(sketch, "getSolverMessages", None)
    if callable(get_messages):
        try:
            raw = get_messages()
        except Exception:
            raw = None
        if isinstance(raw, (list, tuple)):
            message_count = len(raw)
            messages_truncated = message_count > _SOLVER_MESSAGE_LIMIT
            messages = [str(message) for message in raw[:_SOLVER_MESSAGE_LIMIT]]
    return {
        "fullyConstrained": fully,
        "degreesOfFreedom": dof,
        "solverMessages": messages,
        "solverMessageCount": message_count,
        "solverMessagesTruncated": messages_truncated,
        "solverStatus": solver_status,
    }


def _geometry_rows(sketch: Any) -> tuple[list[dict], int, str | None]:
    """Read geometry rows, or explain a collection FreeCAD refuses to read.

    A native read failure returns empty rows and count zero together
    with a bounded reason string, so an unreadable collection is never
    published as verified empty data; a normal empty sketch reads as
    empty with a null reason.
    """

    try:
        geometry = list(sketch.Geometry or ())
    except Exception as exc:
        return [], 0, f"reading Geometry failed: {type(exc).__name__}: {exc}"[:256]
    return (
        [
            _geometry_row(index, geo, sketch)
            for index, geo in enumerate(geometry[:_MAX_SKETCH_ROWS])
        ],
        len(geometry),
        None,
    )


def _constraint_rows(sketch: Any) -> tuple[list[dict], int, str | None]:
    """Read constraint rows, or explain a collection FreeCAD refuses to read.

    A native read failure returns empty rows and count zero together
    with a bounded reason string, so an unreadable collection is never
    published as verified empty data; a normal empty sketch reads as
    empty with a null reason.
    """

    try:
        constraints = list(sketch.Constraints or ())
    except Exception as exc:
        return [], 0, f"reading Constraints failed: {type(exc).__name__}: {exc}"[:256]
    return (
        [
            _constraint_row(index, constraint)
            for index, constraint in enumerate(constraints[:_MAX_SKETCH_ROWS])
        ],
        len(constraints),
        None,
    )


# ---------------------------------------------------------------------------
# Native construction (lazy Part/FreeCAD imports keep this module headless).
# ---------------------------------------------------------------------------


def _native_geometry(entry: dict):
    """Build the native Part geometry object for one validated entry."""
    import FreeCAD
    import Part

    kind = entry["kind"]
    if kind == "point":
        return Part.Point(FreeCAD.Vector(entry["x"], entry["y"], 0.0))
    if kind == "lineSegment":
        return Part.LineSegment(
            FreeCAD.Vector(entry["start"][0], entry["start"][1], 0.0),
            FreeCAD.Vector(entry["end"][0], entry["end"][1], 0.0),
        )
    if kind == "circle":
        return Part.Circle(
            FreeCAD.Vector(entry["center"][0], entry["center"][1], 0.0),
            FreeCAD.Vector(0.0, 0.0, 1.0),
            entry["radius"],
        )
    circle = Part.Circle(
        FreeCAD.Vector(entry["center"][0], entry["center"][1], 0.0),
        FreeCAD.Vector(0.0, 0.0, 1.0),
        entry["radius"],
    )
    return Part.ArcOfCircle(circle, entry["startAngle"], entry["endAngle"])


def _native_datum(datum: str, what: str):
    """Native quantity for a datum string; never returns a bare string."""

    from FreeCAD import Units

    try:
        return Units.Quantity(datum)
    except Exception as exc:
        raise _fail(f"{what} {datum!r} is not a valid FreeCAD quantity: {exc}") from exc


# ---------------------------------------------------------------------------
# Handlers.
# ---------------------------------------------------------------------------


def _inspect_sketch(ctx: Any, arguments: dict) -> dict:
    """Handle inspect_sketch: read-only geometry, constraint and solver rows."""
    doc = ctx.require_document(arguments["document"])
    sketch = ctx.require_object(doc, arguments["sketch"])
    _require_sketch(sketch)
    geometry, geometry_count, geometry_unavailable = _geometry_rows(sketch)
    constraints, constraints_count, constraints_unavailable = _constraint_rows(sketch)
    bindings_all = _expression_bindings_all(sketch)
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": str(getattr(sketch, "Name", "")),
        "solver": _solver_summary(sketch),
        "state": _state_names(sketch),
        "statusText": _status_text(sketch),
        "geometry": geometry,
        "constraints": constraints,
        "expressionBindings": bindings_all[:_MAX_SKETCH_ROWS],
        "geometryCount": geometry_count,
        "constraintsCount": constraints_count,
        "expressionBindingsCount": len(bindings_all),
        "geometryTruncated": geometry_count > _MAX_SKETCH_ROWS,
        "constraintsTruncated": constraints_count > _MAX_SKETCH_ROWS,
        "expressionBindingsTruncated": len(bindings_all) > _MAX_SKETCH_ROWS,
        "geometryUnavailable": geometry_unavailable,
        "constraintsUnavailable": constraints_unavailable,
    }


def _edit_sketch(ctx: Any, arguments: dict) -> dict:
    """Handle edit_sketch: apply the planned batch inside one mutation."""
    doc = ctx.require_document(arguments["document"])
    sketch = ctx.require_object(doc, arguments["sketch"])
    _require_sketch(sketch)
    require_expected_generation(
        ctx,
        doc,
        arguments,
        message="sketch changed since inspection; re-run inspect_sketch",
        next_tool="inspect_sketch",
    )
    plan = _plan_sketch_edit(sketch, arguments)

    with mutation(ctx, doc, f"edit_sketch:{sketch.Name}", [sketch], expected_solids=0):
        for index in plan["deleteGeometry"]:
            sketch.delGeometry(index)
        for index in plan["deleteConstraints"]:
            sketch.delConstraint(index)
        added_geometry: list[int] = []
        added_geometry_ids: dict[str, int] = {}
        for entry in plan["addGeometry"]:
            result = sketch.addGeometry(_native_geometry(entry), entry["construction"])
            index = _added_index(result, plan["geometryBase"] + len(added_geometry))
            added_geometry.append(index)
            entry_id = entry.get("id")
            if entry_id is not None:
                planned = plan["localGeometryIds"][entry_id]
                if index != planned:
                    raise ToolError(
                        VALIDATION_FAILED,
                        f"native addGeometry returned index {index} for geometry id "
                        f"{entry_id!r}; the batch planned index {planned}",
                        {
                            "reason": "native_index_mismatch",
                            "expectedIndex": planned,
                            "actualIndex": index,
                        },
                    )
                added_geometry_ids[entry_id] = index
        added_constraints: list[int] = []
        for position, entry in enumerate(plan["addConstraints"]):
            import Sketcher

            if entry.get("datum") is not None:
                constraint = Sketcher.Constraint(
                    entry["type"],
                    *entry["arguments"],
                    _native_datum(entry["datum"], f"addConstraints[{position}].datum"),
                )
            else:
                constraint = Sketcher.Constraint(entry["type"], *entry["arguments"])
            result = sketch.addConstraint(constraint)
            planned = plan["constraintBase"] + len(added_constraints)
            index = _added_index(result, planned)
            if index != planned:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"native addConstraint returned index {index} at position "
                    f"{position}; the batch planned index {planned}",
                    {
                        "reason": "native_index_mismatch",
                        "expectedIndex": planned,
                        "actualIndex": index,
                    },
                )
            added_constraints.append(index)
        changed_datums = [
            {"index": entry["index"], "datum": entry["datum"]} for entry in plan["setDatums"]
        ]
        for entry in plan["setDatums"]:
            sketch.setDatum(
                entry["index"],
                _native_datum(entry["datum"], f"setDatums[{entry['index']}].datum"),
            )
        for entry in plan["setExpressions"]:
            sketch.setExpression(f"Constraints[{entry['index']}]", entry["expression"])

    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": str(getattr(sketch, "Name", "")),
        "solver": _solver_summary(sketch, run_solve=True),
        "state": _state_names(sketch),
        "statusText": _status_text(sketch),
        "addedGeometry": added_geometry,
        "addedGeometryIds": added_geometry_ids,
        "addedConstraints": added_constraints,
        "deletedGeometry": list(plan["deleteGeometry"]),
        "deletedConstraints": list(plan["deleteConstraints"]),
        "changedDatums": changed_datums,
        "changedExpressions": [
            {"index": entry["index"], "expression": entry["expression"]}
            for entry in plan["setExpressions"]
            if entry["expression"] is not None
        ],
        "clearedExpressions": [
            entry["index"] for entry in plan["setExpressions"] if entry["expression"] is None
        ],
        "expressionBindings": _expression_bindings(sketch),
    }


def _added_index(result: Any, appended_position: int) -> int:
    """Native add methods return the new index; trust it when usable."""

    value = _int_or_none(result)
    if value is not None and value >= 0:
        return value
    return appended_position


HANDLERS["inspect_sketch"] = _inspect_sketch
HANDLERS["edit_sketch"] = _edit_sketch
