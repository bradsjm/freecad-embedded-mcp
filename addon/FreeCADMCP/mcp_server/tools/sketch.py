"""``inspect_sketch`` / ``edit_sketch``: structured Sketcher access.

Both handlers run on the GUI thread. Inspection reads native geometry and
constraint rows in native index order (0-based, as FreeCAD reports them),
solves the sketch for the degree-of-freedom summary and discloses
constraint expression bindings. Editing applies one batch of operations
inside the shared ``object_validation.mutation`` gate: deletes first in
descending index order, then additions, then datum edits. Every referenced
index is prevalidated against a simulated index state before the
transaction opens, so a bad index never opens one.

FreeCAD 1.1.3 build variance: unavailable native getters yield ``null`` or
empty inspection fields rather than failing the page; a missing mutation
method rejects the operation before the transaction with
``VALIDATION_FAILED`` naming the missing native method.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from ..object_validation import mutation
from ..protocol import VALIDATION_FAILED, ToolError, check_schema

_MAX_SKETCH_ROWS = 4096
_MAX_OPERATIONS = 64
_MAX_CONSTRAINT_ARGUMENTS = 6

_SKETCH_TYPE_ID = "Sketcher::SketchObject"

#: Documented Sketcher constraint type strings this tool accepts.
_CONSTRAINT_TYPES = (
    "Block",
    "Coincident",
    "Collinear",
    "Distance",
    "DistanceX",
    "DistanceY",
    "Equal",
    "Horizontal",
    "InternalAlignment",
    "Perpendicular",
    "PointOnObject",
    "Vertical",
    "Radius",
    "Diameter",
    "Angle",
    "Symmetric",
    "Tangent",
    "SnellsLaw",
    "Weight",
)

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
        "type": {"type": "string", "enum": list(_CONSTRAINT_TYPES)},
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

_SOLVER_SUMMARY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fullyConstrained", "degreesOfFreedom", "solverMessages"],
    "properties": {
        "fullyConstrained": {"type": ["boolean", "null"]},
        "degreesOfFreedom": {"type": ["integer", "null"], "minimum": 0},
        "solverMessages": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
        },
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
        "geometry",
        "constraints",
        "expressionBindings",
    ],
    "properties": {
        "document": {"type": "string"},
        "generation": {"type": "integer", "minimum": 0},
        "object": {"type": "string"},
        "solver": _SOLVER_SUMMARY,
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
            "actual native indexes and the post-edit solver summary."
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


def _geometry_row(index: int, geo: Any) -> dict:
    construction = bool(getattr(geo, "Construction", False))
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
    first = getattr(geo, "FirstParameter", None)
    last = getattr(geo, "LastParameter", None)
    circle = getattr(geo, "Circle", None)
    if first is not None and last is not None and circle is not None:
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
        "driving": _bool_or_none(getattr(constraint, "IsDriving", None)),
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


def _solver_summary(sketch: Any) -> dict:
    degrees_of_freedom: int | None = None
    solve = getattr(sketch, "solve", None)
    if callable(solve):
        try:
            solve()
        except Exception:
            degrees_of_freedom = None
        else:
            getter = getattr(sketch, "getSolverDoF", None)
            if callable(getter):
                try:
                    raw = getter()
                except Exception:
                    raw = None
                value = _int_or_none(raw)
                if value is not None and value >= 0:
                    degrees_of_freedom = value
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
        "fullyConstrained": (degrees_of_freedom == 0 if degrees_of_freedom is not None else None),
        "degreesOfFreedom": degrees_of_freedom,
        "solverMessages": messages,
    }


def _geometry_rows(sketch: Any) -> list[dict]:
    try:
        geometry = list(getattr(sketch, "Geometry", ()) or ())
    except Exception:
        return []
    return [_geometry_row(index, geo) for index, geo in enumerate(geometry[:_MAX_SKETCH_ROWS])]


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
    if constraint_type not in _CONSTRAINT_TYPES:
        raise _fail(
            f"{what}.type must be one of the documented Sketcher constraint "
            f"types, got {constraint_type!r}"
        )
    arguments = entry.get("arguments")
    if not isinstance(arguments, list) or len(arguments) > _MAX_CONSTRAINT_ARGUMENTS:
        raise _fail(
            f"{what}.arguments must be an array of at most {_MAX_CONSTRAINT_ARGUMENTS} integers"
        )
    for value in arguments:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(f"{what}.arguments must contain integers only")
    checked: dict = {
        "type": constraint_type,
        "arguments": list(arguments),
    }
    if entry.get("datum") is not None:
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


def _native_datum(datum: str):
    """Best-effort native quantity for a constraint datum string."""

    try:
        from FreeCAD import Units

        return Units.Quantity(datum)
    except Exception:
        return datum


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
        "geometry": _geometry_rows(sketch),
        "constraints": _constraint_rows(sketch),
        "expressionBindings": _expression_bindings(sketch),
    }


def _edit_sketch(ctx: Any, arguments: dict) -> dict:
    doc = ctx.require_document(arguments["document"])
    sketch = ctx.require_object(doc, arguments["sketch"])
    _require_sketch(sketch)
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
        for entry in plan["addConstraints"]:
            import Sketcher

            if entry.get("datum") is not None:
                constraint = Sketcher.Constraint(
                    entry["type"],
                    *entry["arguments"],
                    _native_datum(entry["datum"]),
                )
            else:
                constraint = Sketcher.Constraint(entry["type"], *entry["arguments"])
            result = sketch.addConstraint(constraint)
            added_constraints.append(_added_index(result, len(added_constraints)))
        changed_datums = [
            {"index": entry["index"], "datum": entry["datum"]} for entry in plan["setDatums"]
        ]
        for entry in plan["setDatums"]:
            sketch.setDatum(entry["index"], entry["datum"])

    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": str(getattr(sketch, "Name", "")),
        "solver": _solver_summary(sketch),
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
