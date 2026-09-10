"""``inspect_sketch`` / ``edit_sketch``: structured Sketcher access.

Both handlers run on the GUI thread. Inspection reads native geometry and
constraint rows in native index order (0-based, as FreeCAD reports them),
solves the sketch for the degree-of-freedom summary and discloses
constraint expression bindings. Editing applies one batch of operations
inside the shared ``object_validation.mutation`` gate: deletes first in
descending index order, then additions, then datum edits. Every referenced
index is prevalidated against a simulated index state before the
transaction opens, so a bad index never opens one.

Both responses also carry the native object state names, the status
string, and the solve() status code. An edit batch may carry
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

import math
import re
from collections.abc import Callable
from typing import Any

from ..object_validation import mutation
from ..protocol import VALIDATION_FAILED, ToolError, check_schema

_MAX_SKETCH_ROWS = 4096
_MAX_STATE_NAMES = 32
_MAX_OPERATIONS = 64
_MAX_CONSTRAINT_ARGUMENTS = 6

_SKETCH_TYPE_ID = "Sketcher::SketchObject"

#: Constraint shapes with a recorded native acceptance: type -> the
#: len(arguments) values the native 1.1.3 constructor accepted in the
#: sweep recorded at tests/native_contract.json,
#: probes["constraint.forms"]. Collinear, InternalAlignment, SnellsLaw,
#: AngleViaPoint and Weight have no recorded form at any arity. A type
#: or arity outside this map is refused before the native constructor
#: runs, because an unrecorded Sketcher.Constraint(...) call raises
#: inside FreeCAD and terminates the process (crash report
#: freecad-2026-09-10-090417.ips: ConstraintPy::PyInit ->
#: Py::TypeError::throwFunc -> std::terminate -> abort).
_CONSTRAINT_FORM_ARITIES: dict[str, frozenset[int]] = {
    "Coincident": frozenset({4}),
    "Horizontal": frozenset({1}),
    "Vertical": frozenset({1}),
    "Block": frozenset({1}),
    "PointOnObject": frozenset({3}),
    "Parallel": frozenset({2}),
    "Perpendicular": frozenset({2}),
    "Equal": frozenset({2}),
    "Tangent": frozenset({2}),
    "Symmetric": frozenset({5, 6}),
    # The sweep rejected the documented one-edge [geo, posA, posB] form;
    # the accepted one-edge forms are [geo, pos] and [geo, pos] with a
    # datum.
    "DistanceX": frozenset({2, 4}),
    "DistanceY": frozenset({2, 4}),
    "Distance": frozenset({2, 3, 4}),
    "Radius": frozenset({1, 2}),
    "Diameter": frozenset({1, 2}),
    "Angle": frozenset({2, 4}),
}

#: Recorded argument slot roles for the arities whose slots were probed
#: in the same sweep: type -> {len(arguments): pattern}. Roles: G =
#: geometry index (>= 0), A = geometry index or axis reference (>= -2),
#: P = point position (0, 1, or 2). An arity accepted by
#: _CONSTRAINT_FORM_ARITIES with no pattern here was recorded without
#: slot roles (the two-token Radius/Diameter value form) and carries no
#: slot check.
_CONSTRAINT_ARGUMENT_ROLES: dict[str, dict[int, tuple[str, ...]]] = {
    "Coincident": {4: ("G", "P", "A", "P")},
    "Horizontal": {1: ("G",)},
    "Vertical": {1: ("G",)},
    "Block": {1: ("G",)},
    "PointOnObject": {3: ("G", "P", "G")},
    "Parallel": {2: ("G", "G")},
    "Perpendicular": {2: ("G", "G")},
    "Equal": {2: ("G", "G")},
    "Tangent": {2: ("G", "G")},
    # The sweep accepted Symmetric:[0, 1, 1, 2, -1], so the fifth slot of
    # the five-token form is an axis-or-geometry reference like the
    # second slot of Coincident.
    "Symmetric": {5: ("G", "P", "G", "P", "A"), 6: ("G", "P", "G", "P", "G", "P")},
    "DistanceX": {4: ("G", "P", "G", "P"), 2: ("G", "P")},
    "DistanceY": {4: ("G", "P", "G", "P"), 2: ("G", "P")},
    "Distance": {2: ("G", "P"), 3: ("G", "P", "P"), 4: ("G", "P", "G", "P")},
    "Radius": {1: ("G",)},
    "Diameter": {1: ("G",)},
    "Angle": {2: ("G", "G"), 4: ("G", "G", "G", "P")},
}

#: Recorded datum policy: type -> the len(arguments) values whose native
#: acceptances all carried a trailing datum quantity. Each pair is read
#: off the datum the sweep actually sent, which the recorded keys do not
#: encode: Angle:[0, 1] and Angle:[0, 1, 0, 2] were probed with "deg",
#: Distance:[2, 0], [0, 1, 2] and [0, 1, 1, 2], DistanceX:[0, 1, 1, 2],
#: DistanceY:[1, 1, 0, 2], Radius:[2] and Diameter:[2] with "mm". Every
#: arity of every type is proven in exactly one mode, so the remaining
#: recorded arities are datum-forbidden: a datum there would repeat a
#: probed-with-a-datum arity that the sweep rejected (Radius:[2, 0] with
#: a quantity, Horizontal:[0, 1]). The split matters most for DistanceX
#: and DistanceY, whose datum-free four-token forms are the recorded D1
#: abort reproductions (examples/native_contract_probe.py
#: KNOWN_BAD_FORMS), while their two-token forms were accepted only
#: datum-free.
_CONSTRAINT_DATUM_REQUIRED: dict[str, frozenset[int]] = {
    "Angle": frozenset({2, 4}),
    "Distance": frozenset({2, 3, 4}),
    "DistanceX": frozenset({4}),
    "DistanceY": frozenset({4}),
    "Radius": frozenset({1}),
    "Diameter": frozenset({1}),
}

_DATUM_PATTERN = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?(\s*\S+)?$")

_SKETCH_FIELD = {"type": "string", "minLength": 1}
_CONSTRUCTION = {"type": "boolean", "default": False}
_XY_PAIR = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 2,
    "maxItems": 2,
}
_INTEGER_ARGUMENTS = {
    "type": "array",
    "items": {"type": "integer"},
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
                "center": _XY_PAIR,
                "radius": {"type": "number", "exclusiveMinimum": 0},
                "startAngle": {"type": "number"},
                "endAngle": {"type": "number"},
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
        "solverStatus",
    ],
    "properties": {
        "fullyConstrained": {"type": ["boolean", "null"]},
        "degreesOfFreedom": {"type": ["integer", "null"], "minimum": 0},
        "solverMessages": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
        },
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
        "addedConstraints",
        "deletedGeometry",
        "deletedConstraints",
        "changedDatums",
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
        }
    },
}

TOOL_DEFINITIONS = [
    {
        "name": "inspect_sketch",
        "description": (
            "Inspect one Sketcher::SketchObject: geometry rows (point, line "
            "segment, circle, arc of circle; other kinds report an "
            "unsupported marker with the native type name) and constraint "
            "rows in native 0-based index order, the solver degree-of-"
            "freedom summary and the constraint expression bindings. "
            "Unavailable native getters report null or empty fields."
        ),
        "inputSchema": _INSPECT_SKETCH_INPUT,
        "outputSchema": _INSPECT_SKETCH_OUTPUT,
    },
    {
        "name": "edit_sketch",
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


def _fail(message: str) -> ToolError:
    return ToolError(VALIDATION_FAILED, message)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _require_sketch(obj: Any) -> None:
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
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _string_or_none(value: Any) -> str | None:
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
    value = _finite(getattr(constraint, "Value", None))
    if value is None:
        return None
    unit = "deg" if getattr(constraint, "Type", "") == "Angle" else "mm"
    return f"{value} {unit}"


def _constraint_row(index: int, constraint: Any) -> dict:
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


def _expression_bindings(sketch: Any) -> list[dict]:
    bindings: list[dict] = []
    engine = getattr(sketch, "ExpressionEngine", None)
    if not isinstance(engine, (list, tuple)):
        return bindings
    for entry in engine[:_MAX_SKETCH_ROWS]:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        path, expression = entry
        text = str(path)
        if "Constraints" not in text:
            continue
        name = text.rsplit(".", 1)[-1].strip()
        bindings.append({"constraint": name, "expression": str(expression)})
    return bindings[:_MAX_SKETCH_ROWS]


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


def _solver_summary(sketch: Any) -> dict:
    solve = getattr(sketch, "solve", None)
    solver_status: int | None = None
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
    get_messages = getattr(sketch, "getSolverMessages", None)
    if callable(get_messages):
        try:
            raw = get_messages()
        except Exception:
            raw = None
        if isinstance(raw, (list, tuple)):
            messages = [str(message) for message in raw][:16]
    return {
        "fullyConstrained": fully,
        "degreesOfFreedom": dof,
        "solverMessages": messages,
        "solverStatus": solver_status,
    }


def _geometry_rows(sketch: Any) -> list[dict]:
    try:
        geometry = list(getattr(sketch, "Geometry", ()) or ())
    except Exception:
        return []
    return [
        _geometry_row(index, geo, sketch) for index, geo in enumerate(geometry[:_MAX_SKETCH_ROWS])
    ]


def _constraint_rows(sketch: Any) -> list[dict]:
    try:
        constraints = list(getattr(sketch, "Constraints", ()) or ())
    except Exception:
        return []
    return [
        _constraint_row(index, constraint)
        for index, constraint in enumerate(constraints[:_MAX_SKETCH_ROWS])
    ]


# ---------------------------------------------------------------------------
# Edit planning (pure prevalidation, no effects).
# ---------------------------------------------------------------------------

#: operation name -> required native method, rejected before the
#: transaction when the live object cannot perform it.
_OPERATION_METHODS = {
    "addGeometry": "addGeometry",
    "addConstraints": "addConstraint",
    "setDatums": "setDatum",
    "deleteGeometry": "delGeometry",
    "deleteConstraints": "delConstraint",
}


def _check_datum(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"{what} must be a non-empty datum string")
    if not _DATUM_PATTERN.match(value.strip()):
        raise _fail(f"{what} must look like '<number>[ <unit>]'; got {value!r}")
    return value.strip()


def _validate_geometry_add(entry: Any, what: str) -> dict:
    if not isinstance(entry, dict):
        raise _fail(f"{what} must be an object")
    kind = entry.get("kind")
    if kind not in ("point", "lineSegment", "circle", "arcOfCircle"):
        raise _fail(f"{what} has unsupported kind {kind!r}")
    checked: dict = {"kind": kind, "construction": bool(entry.get("construction"))}
    for field in ("x", "y"):
        if field in entry:
            value = _finite(entry[field])
            if value is None:
                raise _fail(f"{what}.{field} must be a finite number")
            checked[field] = value
    for field in ("start", "end", "center"):
        if field in entry:
            pair = entry[field]
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise _fail(f"{what}.{field} must be an [x, y] pair")
            converted = [_finite(value) for value in pair]
            if any(value is None for value in converted):
                raise _fail(f"{what}.{field} must contain finite numbers")
            checked[field] = [float(value) for value in converted]
    radius = entry.get("radius")
    if radius is not None:
        value = _finite(radius)
        if value is None or value <= 0:
            raise _fail(f"{what}.radius must be a positive number")
        checked["radius"] = value
    for field in ("startAngle", "endAngle"):
        if field in entry:
            value = _finite(entry[field])
            if value is None:
                raise _fail(f"{what}.{field} must be a finite number")
            checked[field] = value
    return checked


def _validate_constraint_add(entry: Any, what: str) -> dict:
    if not isinstance(entry, dict):
        raise _fail(f"{what} must be an object")
    constraint_type = entry.get("type")
    arguments = entry.get("arguments")
    accepted = _CONSTRAINT_FORM_ARITIES.get(constraint_type)
    if accepted is None:
        shape = f"type {constraint_type!r}"
        if isinstance(arguments, list):
            shape = f"type {constraint_type!r} with {len(arguments)} argument(s)"
        raise ToolError(
            VALIDATION_FAILED,
            f"{what}: {shape} has no recorded native constraint form and is "
            f"not one of the accepted Sketcher constraint types",
            {
                "reason": "unrecorded_constraint_shape",
                "type": constraint_type,
                "argumentCount": len(arguments) if isinstance(arguments, list) else None,
                "acceptedArgumentCounts": None,
                "nextAction": "inspect_sketch",
            },
        )
    if not isinstance(arguments, list) or len(arguments) > _MAX_CONSTRAINT_ARGUMENTS:
        raise _fail(
            f"{what}.arguments must be an array of at most {_MAX_CONSTRAINT_ARGUMENTS} integers"
        )
    if len(arguments) not in accepted:
        raise ToolError(
            VALIDATION_FAILED,
            f"{what}.arguments has {len(arguments)} entries, but the recorded "
            f"native form of {constraint_type} accepts {sorted(accepted)}",
            {
                "reason": "unrecorded_constraint_shape",
                "type": constraint_type,
                "argumentCount": len(arguments),
                "acceptedArgumentCounts": sorted(accepted),
                "nextAction": "inspect_sketch",
            },
        )
    for value in arguments:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(f"{what}.arguments must contain integers only")
    pattern = _CONSTRAINT_ARGUMENT_ROLES.get(constraint_type, {}).get(len(arguments))
    if pattern is not None:
        for position, (role, value) in enumerate(zip(pattern, arguments, strict=True)):
            if role == "G" and value < 0:
                raise _fail(
                    f"{what}.arguments[{position}] is {value}; "
                    f"{constraint_type} expects a geometry index (>= 0) at "
                    f"slot {position}"
                )
            if role == "A" and value < -2:
                raise _fail(
                    f"{what}.arguments[{position}] is {value}; "
                    f"{constraint_type} expects a geometry index (>= 0) or an "
                    f"axis reference (-2, -1) at slot {position}"
                )
            if role == "P" and value not in (0, 1, 2):
                raise _fail(
                    f"{what}.arguments[{position}] is {value}; "
                    f"{constraint_type} expects a point position (0, 1, or 2) "
                    f"at slot {position}"
                )
    checked: dict = {
        "type": constraint_type,
        "arguments": list(arguments),
    }
    has_datum = entry.get("datum") is not None
    datum_required = len(arguments) in _CONSTRAINT_DATUM_REQUIRED.get(constraint_type, frozenset())
    if datum_required and not has_datum:
        raise ToolError(
            VALIDATION_FAILED,
            f"{what}: {constraint_type} with {len(arguments)} argument(s) has no "
            f"recorded native form without a datum",
            {
                "reason": "unrecorded_constraint_shape",
                "type": constraint_type,
                "argumentCount": len(arguments),
                "acceptedArgumentCounts": sorted(accepted),
                "datumRequired": True,
                "nextAction": "inspect_sketch",
            },
        )
    if has_datum and not datum_required:
        raise ToolError(
            VALIDATION_FAILED,
            f"{what}: {constraint_type} with {len(arguments)} argument(s) has no "
            f"recorded native form with a datum",
            {
                "reason": "unrecorded_constraint_shape",
                "type": constraint_type,
                "argumentCount": len(arguments),
                "acceptedArgumentCounts": sorted(accepted),
                "datumForbidden": True,
                "nextAction": "inspect_sketch",
            },
        )
    if has_datum:
        checked["datum"] = _check_datum(entry["datum"], f"{what}.datum")
    return checked


def _plan_sketch_edit(sketch: Any, arguments: dict) -> dict:
    """Validate every operation against a simulated index state."""

    try:
        geometry_count = len(list(getattr(sketch, "Geometry", ()) or ()))
    except Exception:
        geometry_count = 0
    try:
        constraint_count = len(list(getattr(sketch, "Constraints", ()) or ()))
    except Exception:
        constraint_count = 0

    delete_geometry = arguments.get("deleteGeometry") or []
    delete_constraints = arguments.get("deleteConstraints") or []
    add_geometry = arguments.get("addGeometry") or []
    add_constraints = arguments.get("addConstraints") or []
    set_datums = arguments.get("setDatums") or []

    if not any(
        isinstance(operation, list) and operation
        for operation in (
            delete_geometry,
            delete_constraints,
            add_geometry,
            add_constraints,
            set_datums,
        )
    ):
        raise _fail("edit_sketch requires at least one operation")

    # Reject missing native methods before any planning error so the cause
    # names the missing capability, not a simulated index.
    for operation, method in _OPERATION_METHODS.items():
        if arguments.get(operation) and not callable(getattr(sketch, method, None)):
            raise _fail(
                f"sketch '{getattr(sketch, 'Name', '<unknown>')}' is missing "
                f"the native {method} method required for {operation}"
            )

    for index in delete_geometry:
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not (0 <= index < geometry_count)
        ):
            raise _fail(
                f"deleteGeometry index {index!r} does not exist; the sketch "
                f"has {geometry_count} geometry elements"
            )
    for index in delete_constraints:
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not (0 <= index < constraint_count)
        ):
            raise _fail(
                f"deleteConstraints index {index!r} does not exist; the "
                f"sketch has {constraint_count} constraints"
            )

    checked_geometry = [
        _validate_geometry_add(entry, f"addGeometry[{position}]")
        for position, entry in enumerate(add_geometry)
    ]
    checked_constraints = [
        _validate_constraint_add(entry, f"addConstraints[{position}]")
        for position, entry in enumerate(add_constraints)
    ]

    # Datum edits apply after deletes and additions: their indexes refer to
    # the final constraint state.
    final_constraint_count = constraint_count - len(delete_constraints) + len(checked_constraints)
    checked_datums = []
    for position, entry in enumerate(set_datums):
        if not isinstance(entry, dict):
            raise _fail(f"setDatums[{position}] must be an object")
        index = entry.get("index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not (0 <= index < final_constraint_count)
        ):
            raise _fail(
                f"setDatums[{position}].index {index!r} does not exist in the "
                f"final constraint state of {final_constraint_count} constraints"
            )
        checked_datums.append(
            {
                "index": index,
                "datum": _check_datum(entry.get("datum"), f"setDatums[{position}].datum"),
            }
        )

    return {
        "deleteGeometry": sorted((int(index) for index in delete_geometry), reverse=True),
        "deleteConstraints": sorted((int(index) for index in delete_constraints), reverse=True),
        "addGeometry": checked_geometry,
        "addConstraints": checked_constraints,
        "setDatums": checked_datums,
    }


# ---------------------------------------------------------------------------
# Native construction (lazy Part/FreeCAD imports keep this module headless).
# ---------------------------------------------------------------------------


def _native_geometry(entry: dict):
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
    doc = ctx.require_document(arguments["document"])
    sketch = ctx.require_object(doc, arguments["sketch"])
    _require_sketch(sketch)
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": str(getattr(sketch, "Name", "")),
        "solver": _solver_summary(sketch),
        "state": _state_names(sketch),
        "statusText": _status_text(sketch),
        "geometry": _geometry_rows(sketch),
        "constraints": _constraint_rows(sketch),
        "expressionBindings": _expression_bindings(sketch),
    }


def _require_expected_generation(ctx: Any, doc: Any, arguments: dict) -> None:
    """Refuse a batch planned against a stale document generation."""

    expected = arguments.get("expected_generation")
    if expected is None:
        return
    actual = int(ctx.document_generation(doc))
    if expected == actual:
        return
    raise ToolError(
        VALIDATION_FAILED,
        "sketch changed since inspection; re-run inspect_sketch",
        {
            "expectedGeneration": expected,
            "actualGeneration": actual,
            "nextAction": "inspect_sketch",
        },
    )


def _edit_sketch(ctx: Any, arguments: dict) -> dict:
    doc = ctx.require_document(arguments["document"])
    sketch = ctx.require_object(doc, arguments["sketch"])
    _require_sketch(sketch)
    _require_expected_generation(ctx, doc, arguments)
    plan = _plan_sketch_edit(sketch, arguments)

    with mutation(ctx, doc, f"edit_sketch:{sketch.Name}", [sketch], expected_solids=0):
        for index in plan["deleteGeometry"]:
            sketch.delGeometry(index)
        for index in plan["deleteConstraints"]:
            sketch.delConstraint(index)
        added_geometry: list[int] = []
        for entry in plan["addGeometry"]:
            result = sketch.addGeometry(_native_geometry(entry), entry["construction"])
            added_geometry.append(_added_index(result, len(added_geometry)))
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
            added_constraints.append(_added_index(result, len(added_constraints)))
        changed_datums = [
            {"index": entry["index"], "datum": entry["datum"]} for entry in plan["setDatums"]
        ]
        for entry in plan["setDatums"]:
            sketch.setDatum(
                entry["index"],
                _native_datum(entry["datum"], f"setDatums[{entry['index']}].datum"),
            )

    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": str(getattr(sketch, "Name", "")),
        "solver": _solver_summary(sketch),
        "state": _state_names(sketch),
        "statusText": _status_text(sketch),
        "addedGeometry": added_geometry,
        "addedConstraints": added_constraints,
        "deletedGeometry": list(plan["deleteGeometry"]),
        "deletedConstraints": list(plan["deleteConstraints"]),
        "changedDatums": changed_datums,
    }


def _added_index(result: Any, appended_position: int) -> int:
    """Native add methods return the new index; trust it when usable."""

    value = _int_or_none(result)
    if value is not None and value >= 0:
        return value
    return appended_position


HANDLERS["inspect_sketch"] = _inspect_sketch
HANDLERS["edit_sketch"] = _edit_sketch
