"""Object tools for the embedded MCP v2 server: inspect/create/edit/delete.

Implements PLAN §5 items 7-10. All handlers run on the GUI thread, apply
every default themselves, return actual sanitized FreeCAD names, and mutate
only inside the shared ``object_validation.mutation`` transaction gate.
Property values use canonical links (``{"object": <Name>, "subelement":
<token>}`` resolved through ``geometry.resolve_reference``) and native
property-type prevalidation; unknown or read-only properties are rejected
before anything is assigned.
"""

from __future__ import annotations

import difflib
import math
from typing import Any

import FreeCAD

from ..object_validation import (
    dependent_count,
    document_bounds,
    geometry_report,
    mutation,
    shape_is_null,
)
from ..protocol import DOMAIN_CURSOR, VALIDATION_FAILED, ToolError

_MAX_LIMIT = 500
_MAX_LINKS = 64
_MAX_FILTER = 64
_MAX_PROPERTY_PAGE = 64
_PROPERTY_LIST_LIMIT = 64
_ENUMERATION_LIMIT = 64
_MAX_DEPENDENTS_LISTED = 64

# ---------------------------------------------------------------------------
# Explicit FEM factory mapping (PLAN §5 item 8: no guessed factory names).
# Maps a requested TypeId to (ObjectsFem factory, {factory kwarg: property key}).
# ---------------------------------------------------------------------------

_FEM_CONSTRAINTS = (
    "Bearing",
    "BodyHeatSource",
    "Centrif",
    "CurrentDensity",
    "Contact",
    "Displacement",
    "ElectricChargeDensity",
    "ElectrostaticPotential",
    "Fixed",
    "RigidBody",
    "FlowVelocity",
    "FluidBoundary",
    "Force",
    "Gear",
    "Heatflux",
    "InitialFlowVelocity",
    "InitialPressure",
    "InitialTemperature",
    "Magnetization",
    "PlaneRotation",
    "Pressure",
    "Pulley",
    "SelfWeight",
    "Temperature",
    "Tie",
    "Transform",
    "SectionPrint",
    "Spring",
)

_FEM_FACTORIES: dict[str, tuple[str, dict[str, str]]] = {
    "Fem::FemAnalysis": ("makeAnalysis", {}),
    # Legacy TypeId alias kept so old clients create the modern analysis.
    "Fem::AnalysisPython": ("makeAnalysis", {}),
    "Fem::SolverCalculiX": ("makeSolverCalculiX", {}),
    "Fem::MaterialCommon": ("makeMaterialSolid", {}),
    "Fem::MaterialFluid": ("makeMaterialFluid", {}),
    "Fem::MaterialReinforced": ("makeMaterialReinforced", {}),
    "Fem::MaterialMechanicalNonlinear": (
        "makeMaterialMechanicalNonlinear",
        {"base_material": "BaseMaterial"},
    ),
    "Fem::ConstantVacuumPermittivity": ("makeConstantVacuumPermittivity", {}),
    "Fem::ElementGeometry1D": ("makeElementGeometry1D", {}),
    "Fem::ElementGeometry2D": ("makeElementGeometry2D", {}),
    "Fem::ElementRotation1D": ("makeElementRotation1D", {}),
    "Fem::ElementFluid1D": ("makeElementFluid1D", {}),
}
_FEM_FACTORIES.update(
    {f"Fem::Constraint{name}": (f"makeConstraint{name}", {}) for name in _FEM_CONSTRAINTS}
)

# ---------------------------------------------------------------------------
# Property-type tables for native prevalidation.
# ---------------------------------------------------------------------------

_PLACEMENT_TYPES = {"App::PropertyPlacement"}
_VECTOR_TYPES = {
    "App::PropertyVector",
    "App::PropertyVectorDistance",
    "App::PropertyDirection",
}
_LINK_TYPES = {"App::PropertyLink", "App::PropertyXLink"}
_LINK_SUB_TYPES = {"App::PropertyLinkSub", "App::PropertyXLinkSub"}
_LINK_LIST_TYPES = {"App::PropertyLinkList", "App::PropertyXLinkList"}
_LINK_SUB_LIST_TYPES = {"App::PropertyLinkSubList", "App::PropertyXLinkSubList"}
_COLOR_TYPES = {"App::PropertyColor"}
_ENUM_TYPES = {"App::PropertyEnumeration"}
_INT_TYPES = {"App::PropertyInteger", "App::PropertyIntegerConstraint"}
_FLOAT_TYPES = {
    "App::PropertyFloat",
    "App::PropertyFloatConstraint",
    "App::PropertyQuantity",
    "App::PropertyDistance",
    "App::PropertyLength",
    "App::PropertyAngle",
    "App::PropertySpeed",
    "App::PropertyArea",
    "App::PropertyVolume",
    "App::PropertyPercent",
}
_BOOL_TYPES = {"App::PropertyBool"}
_STRING_TYPES = {
    "App::PropertyString",
    "App::PropertyFile",
    "App::PropertyFileIncluded",
    "App::PropertyPath",
    "App::PropertyDir",
    "App::PropertyUUID",
}

# ---------------------------------------------------------------------------
# Finite JSON-value schema building blocks.
# ---------------------------------------------------------------------------


def _json_scalars() -> list[dict]:
    return [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
    ]


def _value_defs() -> dict:
    """Bounded-depth recursive JSON value definitions (``value0``..``value4``)."""

    defs: dict = {"value0": {"anyOf": _json_scalars()}}
    for depth in range(1, 5):
        previous = {"$ref": f"#/$defs/value{depth - 1}"}
        defs[f"value{depth}"] = {
            "anyOf": [
                *_json_scalars(),
                {"type": "array", "items": previous, "maxItems": 64},
                {"type": "object", "additionalProperties": previous},
            ]
        }
    return defs


_INPUT_VALUE = {"$ref": "#/$defs/value4"}
_VALUE_DEFS = _value_defs()

_XYZ = {
    "type": "object",
    "additionalProperties": False,
    "required": ["x", "y", "z"],
    "properties": {
        "x": {"type": "number"},
        "y": {"type": "number"},
        "z": {"type": "number"},
    },
}

_CANONICAL_REF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object"],
    "properties": {
        "object": {"type": "string", "minLength": 1},
        "subelement": {"type": "string"},
    },
}

_DOCUMENT_FIELD = {"type": "string", "minLength": 1}
_NAME_FIELD = {"type": "string", "minLength": 1}
_EXPECTED_SOLIDS = {"type": "integer", "minimum": 0}
_PROPERTIES_MAP = {"type": "object", "additionalProperties": _INPUT_VALUE}
_GENERATION = {"type": "integer", "minimum": 0}
_APPLIED = {"type": "array", "items": {"type": "string"}, "maxItems": 512}

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
        "diagnostics": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
        "max_tolerance": {"type": ["number", "null"]},
        "ok": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
    },
}

_OBJECT_IDENTITY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "label", "typeId"],
    "properties": {
        "name": {"type": "string"},
        "label": {"type": "string"},
        "typeId": {"type": "string"},
    },
}

_PLACEMENT_ROW = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["position", "axis", "angle_deg"],
    "properties": {
        "position": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 3,
            "maxItems": 3,
        },
        "axis": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 3,
            "maxItems": 3,
        },
        "angle_deg": {"type": "number"},
    },
}

_PLACEMENT_VALUE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["position", "axis", "angle_deg"],
    "properties": {
        "position": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 3,
            "maxItems": 3,
        },
        "axis": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 3,
            "maxItems": 3,
        },
        "angle_deg": {"type": "number"},
    },
}

_LINK_VALUE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object", "subelement"],
    "properties": {
        "object": {"type": "string"},
        "subelement": {"type": "string"},
    },
}

_PROPERTY_VALUE = {
    "anyOf": [
        *_json_scalars(),
        _XYZ,
        _PLACEMENT_VALUE,
        _LINK_VALUE,
        {
            "type": "array",
            "items": {"anyOf": [{"type": "string"}, {"type": "number"}]},
            "maxItems": 64,
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["unavailable"],
            "properties": {"unavailable": {"type": "string"}},
        },
    ]
}

_OBJECT_ROW = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name",
        "label",
        "typeId",
        "state",
        "bounds",
        "shape_valid",
        "solid_count",
        "tip",
        "links",
    ],
    "properties": {
        "name": {"type": "string"},
        "label": {"type": "string"},
        "typeId": {"type": "string"},
        "state": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
        "placement": {"$ref": "#/$defs/placement"},
        "globalPlacement": {"$ref": "#/$defs/placement"},
        "boundsCoordinateSystem": {"type": "string", "enum": ["document"]},
        "geometryUnavailable": {"type": "string"},
        "bounds": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 6,
            "maxItems": 6,
        },
        "shape_valid": {"type": ["boolean", "null"]},
        "solid_count": {"type": ["integer", "null"], "minimum": 0},
        "tip": {"type": ["string", "null"]},
        "links": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
        "bodyTip": {"type": ["string", "null"]},
        "features": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "label", "typeId", "state"],
                "properties": {
                    "name": {"type": "string"},
                    "label": {"type": "string"},
                    "typeId": {"type": "string"},
                    "state": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
                },
            },
            "maxItems": 256,
        },
        "featuresTruncated": {"type": "boolean"},
        "origins": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "typeId", "role"],
                "properties": {
                    "name": {"type": "string"},
                    "typeId": {"type": "string"},
                    "role": {"type": "string", "enum": ["x", "y", "z", "xy", "xz", "yz"]},
                },
            },
            "maxItems": 6,
        },
        "properties": {"type": "object", "additionalProperties": _PROPERTY_VALUE},
    },
}

_PROPERTY_METADATA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "type",
        "readOnly",
        "enumeration",
        "enumerationCount",
        "enumerationTruncated",
    ],
    "properties": {
        "type": {"type": ["string", "null"]},
        "readOnly": {"type": ["boolean", "null"]},
        "enumeration": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "maxItems": _MAX_PROPERTY_PAGE,
        },
        "enumerationCount": {"type": "integer", "minimum": 0},
        "enumerationTruncated": {"type": "boolean"},
        "expression": {"type": ["string", "null"]},
    },
}

_OBJECT_ROW["properties"].update(
    {
        "propertyMetadata": {
            "type": "object",
            "additionalProperties": {"anyOf": [_PROPERTY_METADATA, {"type": "null"}]},
        },
        "propertyCount": {"type": "integer", "minimum": 0},
        "nextPropertyOffset": {"type": ["integer", "null"], "minimum": 0},
        "truncatedProperties": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": _MAX_PROPERTY_PAGE,
        },
    }
)

_ROW_DEFS = {
    "placement": _PLACEMENT_ROW,
    "objectRow": _OBJECT_ROW,
    "propertyMetadata": _PROPERTY_METADATA,
}

_CHANGE_PROPERTY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "before", "after"],
    "properties": {
        "name": {"type": "string"},
        "before": _PROPERTY_VALUE,
        "after": _PROPERTY_VALUE,
    },
}

_CHANGE_GEOMETRY = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "solidCountBefore",
        "solidCountAfter",
        "volumeBefore",
        "volumeAfter",
        "boundsBefore",
        "boundsAfter",
    ],
    "properties": {
        "solidCountBefore": {"type": ["integer", "null"], "minimum": 0},
        "solidCountAfter": {"type": ["integer", "null"], "minimum": 0},
        "volumeBefore": {"type": ["number", "null"]},
        "volumeAfter": {"type": ["number", "null"]},
        "boundsBefore": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 6,
            "maxItems": 6,
        },
        "boundsAfter": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 6,
            "maxItems": 6,
        },
    },
}

_CHANGE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["properties", "geometry", "dependentCountBefore", "dependentCount"],
    "properties": {
        "properties": {"type": "array", "items": _CHANGE_PROPERTY},
        "geometry": _CHANGE_GEOMETRY,
        "dependentCountBefore": {"type": "integer", "minimum": 0},
        "dependentCount": {"type": "integer", "minimum": 0},
    },
}

_CHECKPOINT_RECEIPT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "document", "generation"],
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "document": {"type": "string", "minLength": 1},
        "generation": {"type": "integer", "minimum": 0},
    },
}

_MUTATION_OUTPUT_DEFS = {
    "geometryReport": _GEOMETRY_REPORT,
    "objectIdentity": _OBJECT_IDENTITY,
}

# ---------------------------------------------------------------------------
# Input schemas (defaults are applied by the handlers).
# ---------------------------------------------------------------------------

_DOCUMENT_FIELD = {"type": "string", "minLength": 1}
_NAME_FIELD = {"type": "string", "minLength": 1}
_EXPECTED_SOLIDS = {"type": "integer", "minimum": 0}
_EXPECTED_BOUNDS = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 6,
    "maxItems": 6,
}
_BOUNDS_TOLERANCE = {
    "type": "number",
    "minimum": 0,
    "maximum": 1000000,
    "default": 0.000001,
}
_PROPERTIES_MAP = {"type": "object", "additionalProperties": _INPUT_VALUE}
_INSPECT_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document"],
    "properties": {
        "document": _DOCUMENT_FIELD,
        "objects": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "maxItems": 64,
        },
        "cursor": {"type": ["string", "null"]},
        "property_filter": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": _MAX_FILTER,
        },
        "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_LIMIT,
            "default": 32,
        },
        "property_offset": {"type": "integer", "minimum": 0, "default": 0},
        "property_limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_PROPERTY_PAGE,
            "default": 64,
        },
    },
}

_CREATE_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "type", "name"],
    "properties": {
        "document": _DOCUMENT_FIELD,
        "type": _NAME_FIELD,
        "name": _NAME_FIELD,
        "properties": _PROPERTIES_MAP,
        "expected_solids": _EXPECTED_SOLIDS,
        "expected_bounds": _EXPECTED_BOUNDS,
        "bounds_tolerance": _BOUNDS_TOLERANCE,
    },
    "$defs": _VALUE_DEFS,
}

_EDIT_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "object", "properties"],
    "properties": {
        "document": _DOCUMENT_FIELD,
        "object": _NAME_FIELD,
        "properties": _PROPERTIES_MAP,
        "expected_solids": _EXPECTED_SOLIDS,
        "expected_bounds": _EXPECTED_BOUNDS,
        "bounds_tolerance": _BOUNDS_TOLERANCE,
    },
    "$defs": _VALUE_DEFS,
}


_EDIT_OBJECTS_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "edits"],
    "properties": {
        "document": _DOCUMENT_FIELD,
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["object", "properties"],
                "properties": {
                    "object": _NAME_FIELD,
                    "properties": _PROPERTIES_MAP,
                },
            },
            "minItems": 1,
            "maxItems": 32,
        },
        "expectations": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "expected_solids": _EXPECTED_SOLIDS,
                    "expected_bounds": _EXPECTED_BOUNDS,
                    "bounds_tolerance": _BOUNDS_TOLERANCE,
                },
            },
        },
    },
    "$defs": _VALUE_DEFS,
}

_DELETE_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "object"],
    "properties": {
        "document": _DOCUMENT_FIELD,
        "object": _NAME_FIELD,
    },
}

# ---------------------------------------------------------------------------
# Output schemas.
# ---------------------------------------------------------------------------

_INSPECT_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "document",
        "generation",
        "detail",
        "total",
        "count",
        "objects",
        "nextCursor",
    ],
    "properties": {
        "document": {"type": "string"},
        "generation": _GENERATION,
        "detail": {"type": "string", "enum": ["compact", "full"]},
        "total": {"type": "integer", "minimum": 0},
        "count": {"type": "integer", "minimum": 0},
        "objects": {
            "type": "array",
            "items": {"$ref": "#/$defs/objectRow"},
            "maxItems": _MAX_LIMIT,
        },
        "nextCursor": {"type": ["string", "null"]},
    },
    "$defs": _ROW_DEFS,
}

_MUTATED_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "document",
        "generation",
        "object",
        "report",
        "applied",
        "change",
    ],
    "properties": {
        "document": {"type": "string"},
        "generation": _GENERATION,
        "object": {"$ref": "#/$defs/objectIdentity"},
        "report": {"$ref": "#/$defs/geometryReport"},
        "applied": _APPLIED,
        "change": _CHANGE,
    },
    "$defs": _MUTATION_OUTPUT_DEFS,
}

_DELETE_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "generation", "removed", "applied"],
    "properties": {
        "document": {"type": "string"},
        "generation": _GENERATION,
        "removed": {"$ref": "#/$defs/objectIdentity"},
        "applied": _APPLIED,
    },
    "$defs": _MUTATION_OUTPUT_DEFS,
}

_EDIT_OBJECTS_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "generation", "objects", "changes", "applied"],
    "properties": {
        "document": {"type": "string"},
        "generation": _GENERATION,
        "objects": {
            "type": "array",
            "items": {"$ref": "#/$defs/objectIdentity"},
            "maxItems": 32,
        },
        "changes": {
            "type": "array",
            "items": _CHANGE,
            "maxItems": 32,
        },
        "applied": _APPLIED,
    },
    "$defs": _MUTATION_OUTPUT_DEFS,
}

_MUTATED_OUTPUT["properties"]["checkpoint"] = _CHECKPOINT_RECEIPT
_DELETE_OUTPUT["properties"]["checkpoint"] = _CHECKPOINT_RECEIPT
_EDIT_OBJECTS_OUTPUT["properties"]["checkpoint"] = _CHECKPOINT_RECEIPT

TOOL_DEFINITIONS = [
    {
        "name": "inspect_objects",
        "description": (
            "List a document's objects sorted by Name, or an explicit "
            "selection of 1-64 objects resolved by name. Compact rows carry "
            "identity (name, label, TypeId), state, bounds, shape validity, "
            "solid count, Body tip and link identities; full detail adds "
            "local and global placements, property pages and property "
            "metadata with typed unavailable markers and expressions. "
            "Pagination uses an opaque signed cursor bound to the document "
            "generation, the selection and the filters; a stale cursor is a "
            "restart-pagination error."
        ),
        "inputSchema": _INSPECT_INPUT,
        "outputSchema": _INSPECT_OUTPUT,
    },
    {
        "name": "create_object",
        "description": (
            "Create an object of a supported type in a document. Generic "
            "Part/App types use doc.addObject; FEM types use an explicit "
            "factory mapping (modern analysis/solver/material plus "
            "constraints and elements). Optional expected_bounds (six "
            "document-space mm coordinates in xmin, ymin, zmin, xmax, ymax, "
            "zmax order plus bounds_tolerance) gate the "
            "commit. Returns the actual sanitized identity, post-recompute "
            "validation and a compact factual change summary."
        ),
        "inputSchema": _CREATE_INPUT,
        "outputSchema": _MUTATED_OUTPUT,
    },
    {
        "name": "edit_object",
        "description": (
            "Assign properties to an existing object. Every property is "
            "prevalidated (existence, read-only, native type, canonical "
            "links) before the transaction opens, so a later invalid "
            "property leaves earlier ones unchanged. Preserves vector, "
            "placement, color, ViewObject and link conversions; "
            "FuzzyTolerance is honored only when the feature actually "
            "exposes it. Optional expected_bounds (six document-space mm "
            "coordinates in xmin, ymin, zmin, xmax, ymax, zmax order plus "
            "bounds_tolerance) gate the commit. Returns "
            "before/after property values, geometry deltas, the dependent "
            "counts before and after, and post-recompute validation."
        ),
        "inputSchema": _EDIT_INPUT,
        "outputSchema": _MUTATED_OUTPUT,
    },
    {
        "name": "edit_objects",
        "description": (
            "Edit several existing objects atomically: 1-32 {object, "
            "properties} entries applied in request order inside one "
            "transaction and one recompute, with optional per-object "
            "expectations (expected_solids, expected_bounds, "
            "bounds_tolerance) checked before commit. Duplicate object "
            "names are rejected and every property is prevalidated before "
            "the transaction opens. A failing expectation or recompute "
            "rolls back the whole batch; the result reports actual "
            "identities, per-object change summaries and applied names."
        ),
        "inputSchema": _EDIT_OBJECTS_INPUT,
        "outputSchema": _EDIT_OBJECTS_OUTPUT,
    },
    {
        "name": "delete_object",
        "description": (
            "Remove one object. Objects with dependents are refused "
            "explicitly instead of being silently cascaded; otherwise the "
            "removal and recompute run inside the shared mutation "
            "transaction and abort on failure."
        ),
        "inputSchema": _DELETE_INPUT,
        "outputSchema": _DELETE_OUTPUT,
    },
]


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _label(obj: Any) -> str:
    return str(getattr(obj, "Label", getattr(obj, "Name", "")))


def _states(obj: Any) -> list[str]:
    try:
        raw = obj.State
    except Exception:
        return []
    if isinstance(raw, str):
        return [raw]
    try:
        return [str(item) for item in raw]
    except Exception:
        return []


def _shape(obj: Any) -> Any:
    try:
        shape = obj.Shape
    except Exception:
        return None
    if shape_is_null(shape):
        return None
    return shape


def _bounds(shape: Any) -> list[float] | None:
    if shape is None:
        return None
    try:
        box = shape.BoundBox
        coordinates = [box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax]
    except Exception:
        return None
    values = []
    for coordinate in coordinates:
        number = _number_or_none(coordinate)
        if number is None:
            return None
        values.append(number)
    return values


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _global_geometry(obj: Any) -> tuple[list[float] | None, dict | None, str | None]:
    """Document-space bounds and global placement row for one object.

    Returns ``(bounds, globalPlacement, geometryUnavailable)``. Shapeless
    objects report ``(None, None, None)`` — nothing is unavailable, there
    is simply no geometry. A shape whose global transform cannot be
    resolved reports nulls plus an explanatory ``geometryUnavailable``
    message instead of silently publishing local coordinates.
    """

    from .geometry import placed_shape

    if _shape(obj) is None:
        return None, None, None
    try:
        global_shape = placed_shape(obj)
    except Exception as exc:
        message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
        return None, None, str(message)
    return (
        _bounds(global_shape),
        _placement_row_value(getattr(global_shape, "Placement", None)),
        None,
    )


# ---------------------------------------------------------------------------
# Property conversion.
# ---------------------------------------------------------------------------


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(VALIDATION_FAILED, f"{what} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ToolError(VALIDATION_FAILED, f"{what} must be finite")
    return number


def _vector_value(value: Any, what: str) -> Any:
    if isinstance(value, dict):
        coordinates = [value.get(axis, 0) for axis in ("x", "y", "z")]
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        coordinates = list(value)
    else:
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} must be an {{x, y, z}} object or a three-number array",
        )
    numbers = [
        _number(coordinate, f"{what}.{axis}")
        for coordinate, axis in zip(coordinates, ("x", "y", "z"), strict=True)
    ]
    return FreeCAD.Vector(*numbers)


def _color_value(value: Any, what: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
        raise ToolError(VALIDATION_FAILED, f"{what} must be an RGB or RGBA number array")
    parts = [_number(component, f"{what}[{index}]") for index, component in enumerate(value)]
    if len(parts) == 3:
        parts.append(1.0)
    return (parts[0], parts[1], parts[2], parts[3])


def _rotation_value(value: Any, what: str) -> Any:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} must be an object with an Axis {{x, y, z}} and an Angle in degrees",
        )
    axis = value.get("Axis") or {}
    angle = _number(value.get("Angle", 0), f"{what}.Angle")
    return FreeCAD.Rotation(
        FreeCAD.Vector(
            _number(axis.get("x", 0), f"{what}.Axis.x"),
            _number(axis.get("y", 0), f"{what}.Axis.y"),
            _number(axis.get("z", 1), f"{what}.Axis.z"),
        ),
        angle,
    )


def _placement_value(value: Any, what: str) -> Any:
    if not isinstance(value, dict):
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} must be an object with position/axis/angle_deg or "
            "Base/Position and Rotation parts",
        )
    if any(key in value for key in ("position", "axis", "angle_deg")):
        position = value.get("position", [0, 0, 0])
        axis = value.get("axis", [0, 0, 1])
        angle = _number(value.get("angle_deg", 0), f"{what}.angle_deg")
        return FreeCAD.Placement(
            _vector_value(position, f"{what}.position"),
            FreeCAD.Rotation(_vector_value(axis, f"{what}.axis"), angle),
        )
    position = value.get("Base") or value.get("Position") or {}
    return FreeCAD.Placement(
        _vector_value(position, f"{what}.Base"),
        _rotation_value(value.get("Rotation"), f"{what}.Rotation"),
    )


def _resolve_reference(ctx: Any, doc: Any, reference: dict):
    # geometry.py is a sibling tool module; import lazily so this module
    # loads (and its tests run) regardless of registration order.
    from .geometry import resolve_reference

    return resolve_reference(ctx, doc, reference)


def _resolve_one(ctx: Any, doc: Any, value: Any, what: str, *, allow_sub: bool) -> Any:
    """Resolve one canonical link value; returns the object or (object, sub)."""

    if not isinstance(value, dict) or not isinstance(value.get("object"), str):
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} must be a canonical link object {'{object: <Name>, subelement: <token>}'}",
        )
    name = value["object"]
    if not name.strip():
        raise ToolError(VALIDATION_FAILED, f"{what} must name an object")
    subelement = value.get("subelement")
    if subelement is None:
        subelement = ""
    if not isinstance(subelement, str):
        raise ToolError(VALIDATION_FAILED, f"{what}.subelement must be a string")
    if not allow_sub and subelement:
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} takes a plain object link and accepts no subelement",
        )
    resolved, native = _resolve_reference(ctx, doc, {"object": name, "subelement": subelement})
    if allow_sub:
        return (resolved, native)
    return resolved


def _convert_value(ctx: Any, doc: Any, obj: Any, prop: str, value: Any) -> Any:
    """Convert one JSON value to the property's native FreeCAD value."""

    ptype = str(obj.getTypeIdOfProperty(prop))
    if ptype in _PLACEMENT_TYPES:
        return _placement_value(value, prop)
    if ptype in _VECTOR_TYPES:
        return _vector_value(value, prop)
    if ptype in _LINK_TYPES:
        return _resolve_one(ctx, doc, value, prop, allow_sub=False)
    if ptype in _LINK_SUB_TYPES:
        return _resolve_one(ctx, doc, value, prop, allow_sub=True)
    if ptype in _LINK_LIST_TYPES:
        return [
            _resolve_one(ctx, doc, entry, f"{prop}[{index}]", allow_sub=False)
            for index, entry in enumerate(_as_array(value, prop))
        ]
    if ptype in _LINK_SUB_LIST_TYPES:
        return [
            _resolve_one(ctx, doc, entry, f"{prop}[{index}]", allow_sub=True)
            for index, entry in enumerate(_as_array(value, prop))
        ]
    if ptype in _COLOR_TYPES:
        return _color_value(value, prop)
    if ptype in _ENUM_TYPES:
        if not isinstance(value, str):
            raise ToolError(VALIDATION_FAILED, f"{prop} must be a string")
        enumerations = _enumerations(obj, prop)
        if enumerations is not None and value not in enumerations:
            raise ToolError(
                VALIDATION_FAILED,
                f"{prop} must be one of {enumerations}",
                {"enumerations": enumerations},
            )
        return value
    if ptype in _INT_TYPES:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(VALIDATION_FAILED, f"{prop} must be an integer")
        return value
    if ptype in _FLOAT_TYPES:
        return _number(value, prop)
    if ptype in _BOOL_TYPES:
        if not isinstance(value, bool):
            raise ToolError(VALIDATION_FAILED, f"{prop} must be a boolean")
        return value
    if ptype in _STRING_TYPES:
        if not isinstance(value, str):
            raise ToolError(VALIDATION_FAILED, f"{prop} must be a string")
        return value
    if ptype == "App::PropertyStringList":
        return [
            _string(entry, f"{prop}[{index}]") for index, entry in enumerate(_as_array(value, prop))
        ]
    if ptype == "App::PropertyIntegerList":
        return [
            _integer(entry, f"{prop}[{index}]")
            for index, entry in enumerate(_as_array(value, prop))
        ]
    if ptype == "App::PropertyFloatList":
        return [
            _number(entry, f"{prop}[{index}]") for index, entry in enumerate(_as_array(value, prop))
        ]
    # Unmapped property types pass through; FreeCAD rejects mismatches and
    # the mutation gate rolls the assignment back.
    return value


def _as_array(value: Any, what: str) -> list:
    if not isinstance(value, list):
        raise ToolError(VALIDATION_FAILED, f"{what} must be an array")
    return value


def _string(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ToolError(VALIDATION_FAILED, f"{what} must be a string")
    return value


def _integer(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(VALIDATION_FAILED, f"{what} must be an integer")
    return value


def _enumerations(obj: Any, prop: str) -> list[str] | None:
    getter = getattr(obj, "getEnumerationsOfProperty", None)
    if not callable(getter):
        return None
    try:
        found = getter(prop)
    except Exception:
        return None
    return [str(entry) for entry in found] if found else None


def _suggestions(value: str, candidates: list[str]) -> list[str]:
    """Bounded did-you-mean list for a rejected name.

    The caller supplies the meaningful candidate vocabulary; ``n`` and the
    cutoff keep every error payload small and free of unrelated names.
    """

    return difflib.get_close_matches(value, candidates, n=5, cutoff=0.6)


def _plan_create(ctx: Any, doc: Any, obj_type: str, properties: dict) -> tuple[Any, dict[str, Any]]:
    """Validate the requested type before the transaction opens.

    Returns ``(factory, kwargs)``; a ``None`` factory means plain
    ``doc.addObject``. Base-link factory arguments are resolved and popped
    from ``properties`` here so missing references fail before any
    transaction exists.
    """

    if obj_type.startswith("Fem::"):
        import ObjectsFem

        spec = _FEM_FACTORIES.get(obj_type)
        if spec is None:
            message = f"FEM type '{obj_type}' has no explicit creation factory in this protocol"
            if obj_type.startswith("Fem::FemMesh"):
                message += "; FEM mesh objects are not created by create_object"
            raise ToolError(
                VALIDATION_FAILED,
                message,
                {"supportedFemTypes": sorted(_FEM_FACTORIES)},
            )
        factory_name, link_args = spec
        factory = getattr(ObjectsFem, factory_name, None)
        if not callable(factory):
            raise ToolError(
                VALIDATION_FAILED,
                f"ObjectsFem.{factory_name} is unavailable in this FreeCAD",
            )
        kwargs: dict[str, Any] = {}
        for param, key in link_args.items():
            raw = properties.pop(key, None)
            if raw is None:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"FEM type '{obj_type}' requires a canonical '{key}' link in properties",
                )
            kwargs[param] = _resolve_one(ctx, doc, raw, key, allow_sub=False)
        return factory, kwargs

    supported = getattr(doc, "supportedTypes", None)
    if callable(supported):
        types = [str(entry) for entry in (supported() or ())]
        if obj_type not in types:
            raise ToolError(
                VALIDATION_FAILED,
                f"type '{obj_type}' is not supported by document "
                f"'{getattr(doc, 'Name', '<unknown>')}'",
                {
                    "supportedTypes": types[:_MAX_FILTER],
                    "suggestions": _suggestions(obj_type, types),
                    "nextAction": "inspect_objects",
                },
            )
    return None, {}


def _call_factory(factory: Any, doc: Any, requested_name: str, kwargs: dict[str, Any]) -> Any:
    try:
        return factory(doc, name=requested_name, **kwargs)
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"object creation failed: {_describe(exc)}",
        ) from exc


def _property_exists(obj: Any, prop: str) -> bool:
    return prop in list(getattr(obj, "PropertiesList", ()) or ())


def _is_read_only(holder: Any, prop: str) -> bool:
    getter = getattr(holder, "getPropertyStatus", None)
    if not callable(getter):
        return False
    try:
        status = getter(prop)
    except Exception:
        return False
    return "ReadOnly" in list(status or ())


def _viewobject(obj: Any) -> Any:
    view = getattr(obj, "ViewObject", None)
    if view is None:
        raise ToolError(
            VALIDATION_FAILED,
            f"object '{getattr(obj, 'Name', '<unknown>')}' has no ViewObject",
        )
    return view


def _view_value(prop: str, value: Any) -> Any:
    if prop.endswith("Color"):
        return _color_value(value, prop)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _number(value, prop)
    raise ToolError(
        VALIDATION_FAILED,
        f"ViewObject property '{prop}' accepts only scalars or colors over this protocol",
    )


def _check_view_property(obj: Any, view: Any, prop: str) -> None:
    if not _property_exists(view, prop):
        raise ToolError(
            VALIDATION_FAILED,
            f"ViewObject of '{getattr(obj, 'Name', '<unknown>')}' has no property '{prop}'",
        )
    if _is_read_only(view, prop):
        raise ToolError(
            VALIDATION_FAILED,
            f"ViewObject property '{prop}' of '{getattr(obj, 'Name', '<unknown>')}' is read-only",
        )


def _check_document_property(obj: Any, prop: str) -> None:
    name = str(getattr(obj, "Name", "<unknown>"))
    if not _property_exists(obj, prop):
        raise ToolError(
            VALIDATION_FAILED,
            f"object '{name}' has no property '{prop}'",
            {
                "object": name,
                "property": prop,
                "suggestions": _suggestions(prop, _all_property_names(obj)),
                "nextAction": "inspect_objects",
            },
        )
    if _is_read_only(obj, prop):
        raise ToolError(VALIDATION_FAILED, f"property '{prop}' of object '{name}' is read-only")


def _assign_converted(ctx: Any, doc: Any, obj: Any, prop: str, value: Any) -> None:
    """Convert and assign one document property (live path for create)."""

    _check_document_property(obj, prop)
    setattr(obj, prop, _convert_value(ctx, doc, obj, prop, value))


def _apply_properties(ctx: Any, doc: Any, obj: Any, properties: dict) -> None:
    """Assign a properties map onto an existing object (create path)."""

    for prop, value in properties.items():
        if prop == "ViewObject":
            if not isinstance(value, dict):
                raise ToolError(VALIDATION_FAILED, "ViewObject must be an object")
            view = _viewobject(obj)
            for sub, sub_value in value.items():
                _check_view_property(obj, view, sub)
                setattr(view, sub, _view_value(sub, sub_value))
        elif prop == "ShapeColor":
            view = _viewobject(obj)
            _check_view_property(obj, view, "ShapeColor")
            view.ShapeColor = _color_value(value, "ShapeColor")
        else:
            _assign_converted(ctx, doc, obj, prop, value)


def _prepare_properties(
    ctx: Any, doc: Any, obj: Any, properties: dict
) -> list[tuple[str, str, Any]]:
    """Prevalidate and convert the whole edit map before any assignment.

    Returns ``("doc"|"view", prop, converted)`` tuples; raises before the
    transaction opens so a later invalid property leaves earlier ones
    unchanged.
    """

    prepared: list[tuple[str, str, Any]] = []
    for prop, value in properties.items():
        if prop == "ViewObject":
            if not isinstance(value, dict):
                raise ToolError(VALIDATION_FAILED, "ViewObject must be an object")
            view = _viewobject(obj)
            for sub, sub_value in value.items():
                _check_view_property(obj, view, sub)
                prepared.append(("view", sub, _view_value(sub, sub_value)))
        elif prop == "ShapeColor":
            view = _viewobject(obj)
            _check_view_property(obj, view, "ShapeColor")
            prepared.append(("view", "ShapeColor", _color_value(value, "ShapeColor")))
        else:
            _check_document_property(obj, prop)
            prepared.append(("doc", prop, _convert_value(ctx, doc, obj, prop, value)))
    return prepared


def _apply_prepared(obj: Any, prepared: list[tuple[str, str, Any]]) -> None:
    for target, prop, value in prepared:
        holder = obj.ViewObject if target == "view" else obj
        setattr(holder, prop, value)


# ---------------------------------------------------------------------------
# Inspection.
# ---------------------------------------------------------------------------


def _placement_row_value(placement: Any) -> dict | None:
    try:
        base = placement.Base
        rotation = placement.Rotation
        axis = rotation.Axis
        row = {
            "position": [float(base.x), float(base.y), float(base.z)],
            "axis": [float(axis.x), float(axis.y), float(axis.z)],
            "angle_deg": math.degrees(float(rotation.Angle)),
        }
    except Exception:
        return None
    if any(
        _number_or_none(component) is None
        for triple in (row["position"], row["axis"])
        for component in triple
    ):
        return None
    if _number_or_none(row["angle_deg"]) is None:
        return None
    return row


def _placement_row(obj: Any) -> dict | None:
    try:
        placement = obj.Placement
    except Exception:
        return None
    return _placement_row_value(placement)


def _solid_count(shape: Any) -> int | None:
    if shape is None:
        return None
    try:
        return len(shape.Solids)
    except Exception:
        return None


def _shape_valid(shape: Any) -> bool | None:
    if shape is None:
        return None
    try:
        return bool(shape.isValid())
    except Exception:
        return None


def _tip_name(obj: Any) -> str | None:
    derived = getattr(obj, "isDerivedFrom", None)
    if not callable(derived) or not derived("PartDesign::Body"):
        return None
    tip = getattr(obj, "Tip", None)
    name = str(getattr(tip, "Name", "")) if tip is not None else ""
    return name or None


def _body_history(obj: Any) -> tuple[list[dict], bool, list[dict]]:
    """Return native Body members and its six local origin references."""

    derived = getattr(obj, "isDerivedFrom", None)
    is_body = str(getattr(obj, "TypeId", "")) == "PartDesign::Body"
    if callable(derived):
        try:
            is_body = is_body or bool(derived("PartDesign::Body"))
        except Exception:
            pass
    if not is_body:
        return [], False, []

    members: list[Any] = []
    for attribute in ("Group", "Model"):
        candidate = getattr(obj, attribute, None)
        if candidate is None:
            continue
        try:
            members = list(candidate)
        except Exception:
            continue
        if members:
            break
    features = []
    for member in members:
        if str(getattr(member, "TypeId", "")) in ("PartDesign::Origin", "App::Origin"):
            continue
        features.append(
            {
                "name": str(getattr(member, "Name", "")),
                "label": _label(member),
                "typeId": str(getattr(member, "TypeId", "")),
                "state": _states(member),
            }
        )
    truncated = len(features) > 256

    roles = ("x", "y", "z", "xy", "xz", "yz")
    origin = getattr(obj, "Origin", None)
    candidates: list[Any] = []
    if origin is not None:
        for attribute in ("OriginFeatures", "Group", "Features"):
            value = getattr(origin, attribute, None)
            if value is None:
                continue
            try:
                candidates.extend(list(value))
            except Exception:
                continue
            if candidates:
                break
        for role in roles:
            for attribute in (
                role.upper() + "_Axis",
                role.upper() + "_Plane",
                role.capitalize() + "_Axis",
                role.capitalize() + "_Plane",
            ):
                value = getattr(origin, attribute, None)
                if value is not None and value not in candidates:
                    candidates.append(value)
    origins: list[dict] = []
    for role in roles:
        for candidate in candidates:
            # Origin datums carry a stable Role separate from their unique
            # document Name (an existing X_Axis makes the next one
            # X_Axis001 with Role still X_Axis), so roles match on Role.
            role_value = str(getattr(candidate, "Role", "") or "")
            name = str(getattr(candidate, "Name", ""))
            normalized = (role_value or name).lower().replace("_", "")
            expected = {
                "x": ("xaxis",),
                "y": ("yaxis",),
                "z": ("zaxis",),
                "xy": ("xyplane",),
                "xz": ("xzplane",),
                "yz": ("yzplane",),
            }[role]
            if normalized in expected:
                origins.append(
                    {
                        "name": name,
                        "typeId": str(getattr(candidate, "TypeId", "")),
                        "role": role,
                    }
                )
                break
    return features[:256], truncated, origins


def _link_names(obj: Any) -> list[str]:
    try:
        out_list = list(obj.OutList)
    except Exception:
        return []
    names: list[str] = []
    for linked in out_list:
        name = str(getattr(linked, "Name", ""))
        if name and name not in names:
            names.append(name)
        if len(names) >= _MAX_LINKS:
            break
    return sorted(names)


def _unavailable(kind: str) -> dict:
    return {"unavailable": kind}


def _jsonify(value: Any) -> Any:
    """Convert one property value; document objects never stringify."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _unavailable("non-finite-number")
    if isinstance(value, str):
        return value
    try:
        if hasattr(value, "Base") and hasattr(value, "Rotation"):
            base = value.Base
            axis = value.Rotation.Axis
            return {
                "position": [float(base.x), float(base.y), float(base.z)],
                "axis": [float(axis.x), float(axis.y), float(axis.z)],
                "angle_deg": math.degrees(float(value.Rotation.Angle)),
            }
        if hasattr(value, "x") and hasattr(value, "y") and hasattr(value, "z"):
            return {"x": float(value.x), "y": float(value.y), "z": float(value.z)}
        if hasattr(value, "UserString"):
            return str(value)
        if hasattr(value, "Name") and hasattr(value, "TypeId"):
            # Inspection-only descriptive identity; mutation input keeps
            # accepting opaque signed references.
            return {"object": str(value.Name), "subelement": ""}
    except Exception:
        return _unavailable(type(value).__name__)
    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            first, second = value
            if hasattr(first, "Name") and hasattr(first, "TypeId"):
                # A (link, subelement) pair: the native subelement string is
                # descriptive only and is never an accepted edit selector.
                sub = second
                if isinstance(sub, (list, tuple)):
                    sub = sub[0] if sub else ""
                if isinstance(sub, str):
                    return {"object": str(first.Name), "subelement": sub}
        converted: list[Any] = []
        for item in value[:_PROPERTY_LIST_LIMIT]:
            if isinstance(item, (bool, int, str)) and not isinstance(item, float):
                converted.append(item)
            elif isinstance(item, float):
                if not math.isfinite(item):
                    return _unavailable("non-finite-number")
                converted.append(item)
            else:
                return _unavailable(type(value).__name__)
        return converted
    return _unavailable(type(value).__name__)


def _read_value(obj: Any, prop: str) -> Any:
    if _property_exists(obj, prop):
        return _jsonify(getattr(obj, prop, None))
    view = getattr(obj, "ViewObject", None)
    if view is not None and _property_exists(view, prop):
        return _jsonify(getattr(view, prop, None))
    return _unavailable("no-such-property")


def _all_property_names(obj: Any) -> list[str]:
    """Document property names plus ViewObject names, prefixed and sorted.

    The complete name list drives unfiltered full-detail paging, so no
    property is silently omitted.
    """

    doc_props = {str(prop) for prop in (getattr(obj, "PropertiesList", ()) or ())}
    view = getattr(obj, "ViewObject", None)
    view_props = (
        {"ViewObject." + str(prop) for prop in (getattr(view, "PropertiesList", ()) or ())}
        if view is not None
        else set()
    )
    return sorted(doc_props | view_props)


def _property_metadata(holder: Any, prop: str) -> dict:
    """Metadata for one property using direct native getters.

    Failures yield null fields rather than false claims about mutability
    or available choices.
    """

    property_type = None
    type_getter = getattr(holder, "getTypeIdOfProperty", None)
    if callable(type_getter):
        try:
            raw_type = type_getter(prop)
        except Exception:
            raw_type = None
        if isinstance(raw_type, str) and raw_type:
            property_type = raw_type
    read_only = None
    status_getter = getattr(holder, "getPropertyStatus", None)
    if callable(status_getter):
        try:
            status = status_getter(prop)
        except Exception:
            status = None
        if status is not None:
            read_only = "ReadOnly" in list(status or ())
    enums = _enumerations(holder, prop)
    if enums is None:
        enumeration, count, truncated = None, 0, False
    else:
        enumeration = enums[:_ENUMERATION_LIMIT]
        count = len(enums)
        truncated = count > _ENUMERATION_LIMIT
    expression = None
    expression_getter = getattr(holder, "getExpression", None)
    if callable(expression_getter):
        try:
            raw_expression = expression_getter(prop)
        except Exception:
            raw_expression = None
        if isinstance(raw_expression, str):
            expression = raw_expression or None
        elif isinstance(raw_expression, (list, tuple)) and raw_expression:
            # FreeCAD returns a (expression string, path) tuple; keep the
            # expression-string member.
            member = raw_expression[0]
            if isinstance(member, str) and member:
                expression = member
    return {
        "type": property_type,
        "readOnly": read_only,
        "enumeration": enumeration,
        "enumerationCount": count,
        "enumerationTruncated": truncated,
        "expression": expression,
    }


def _resolve_property_holder(obj: Any, name: str) -> tuple[Any, str] | None:
    """Where one page property lives, or None when it does not exist.

    ``ViewObject.``-prefixed names resolve only against the ViewObject;
    unprefixed names keep the document-first lookup with the ViewObject
    as fallback.
    """

    if name.startswith("ViewObject."):
        plain = name[len("ViewObject.") :]
        view = getattr(obj, "ViewObject", None)
        if view is not None and _property_exists(view, plain):
            return view, plain
        return None
    if _property_exists(obj, name):
        return obj, name
    view = getattr(obj, "ViewObject", None)
    if view is not None and _property_exists(view, name):
        return view, name
    return None


def _property_page(obj: Any, names: list[str]) -> tuple[dict, dict, list[str]]:
    """Values, metadata and over-limit list names for one property page."""

    properties: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    truncated: list[str] = []
    for name in names:
        resolved = _resolve_property_holder(obj, name)
        if resolved is None:
            properties[name] = _unavailable("no-such-property")
            metadata[name] = None
            continue
        holder, plain = resolved
        try:
            raw = getattr(holder, plain, None)
        except Exception:
            raw = None
        properties[name] = _jsonify(raw)
        metadata[name] = _property_metadata(holder, plain)
        if isinstance(raw, (list, tuple)) and len(raw) > _PROPERTY_LIST_LIMIT:
            truncated.append(name)
    return properties, metadata, truncated


def _row(
    obj: Any,
    detail: str,
    props: list[str],
    *,
    property_offset: int = 0,
    property_limit: int = _MAX_PROPERTY_PAGE,
) -> dict:
    shape = _shape(obj)
    properties: dict[str, Any] = {}
    property_metadata: dict[str, Any] = {}
    property_count = 0
    next_property_offset: int | None = None
    truncated: list[str] = []
    global_bounds, global_placement, _geometry_unavailable = _global_geometry(obj)
    if detail == "full":
        if props:
            # A filter keeps its own order and de-duplication.
            names = list(dict.fromkeys(str(prop) for prop in props))
        else:
            names = _all_property_names(obj)
        property_count = len(names)
        page = names[property_offset : property_offset + property_limit]
        following = property_offset + property_limit
        if following < property_count:
            next_property_offset = following
        properties, property_metadata, truncated = _property_page(obj, page)
    row = {
        "name": str(getattr(obj, "Name", "")),
        "label": _label(obj),
        "typeId": str(getattr(obj, "TypeId", "")),
        "state": _states(obj),
    }
    if detail == "full":
        # Local placement stays the editable property value; bounds and
        # globalPlacement describe document space.
        row["placement"] = _placement_row(obj)
        row["globalPlacement"] = global_placement
        row["boundsCoordinateSystem"] = "document"
    row["bounds"] = global_bounds
    row["shape_valid"] = _shape_valid(shape)
    row["solid_count"] = _solid_count(shape)
    row["tip"] = _tip_name(obj)
    row["links"] = _link_names(obj)
    if detail == "full":
        features, features_truncated, origins = _body_history(obj)
        row["bodyTip"] = _tip_name(obj)
        row["features"] = features
        row["featuresTruncated"] = features_truncated
        row["origins"] = origins
        row["properties"] = properties
        row["propertyMetadata"] = property_metadata
        row["propertyCount"] = property_count
        row["nextPropertyOffset"] = next_property_offset
        row["truncatedProperties"] = truncated
    if _geometry_unavailable is not None:
        row["geometryUnavailable"] = _geometry_unavailable
    return row


# ---------------------------------------------------------------------------
# Signed pagination cursor.
# ---------------------------------------------------------------------------


def _cursor_payload(
    ctx: Any,
    doc: Any,
    detail: str,
    limit: int,
    props: list[str],
    last: str,
    *,
    property_offset: int = 0,
    property_limit: int = _MAX_PROPERTY_PAGE,
    selection: list[str] | None = None,
) -> dict:
    return {
        "kind": "objects-page",
        "identity": str(ctx.document_identity(doc)),
        "generation": int(ctx.document_generation(doc)),
        "detail": detail,
        "limit": limit,
        "filter": sorted(str(prop) for prop in props),
        "propertyOffset": property_offset,
        "propertyLimit": property_limit,
        "selection": sorted(selection) if selection else None,
        "last": last,
    }


def _stale_cursor() -> ToolError:
    return ToolError(
        VALIDATION_FAILED,
        "pagination cursor is stale; restart pagination from the beginning",
        {"reason": "stale_cursor"},
    )


def _make_cursor(
    ctx: Any,
    doc: Any,
    detail: str,
    limit: int,
    props: list[str],
    last: str,
    *,
    property_offset: int = 0,
    property_limit: int = _MAX_PROPERTY_PAGE,
    selection: list[str] | None = None,
) -> str:
    return ctx.signer.sign(
        DOMAIN_CURSOR,
        _cursor_payload(
            ctx,
            doc,
            detail,
            limit,
            props,
            last,
            property_offset=property_offset,
            property_limit=property_limit,
            selection=selection,
        ),
    )


def _open_cursor(
    ctx: Any,
    doc: Any,
    cursor: str,
    detail: str,
    limit: int,
    props: list[str],
    *,
    property_offset: int = 0,
    property_limit: int = _MAX_PROPERTY_PAGE,
    selection: list[str] | None = None,
) -> dict:
    payload = ctx.signer.verify(DOMAIN_CURSOR, cursor)
    expected = _cursor_payload(
        ctx,
        doc,
        detail,
        limit,
        props,
        "",
        property_offset=property_offset,
        property_limit=property_limit,
        selection=selection,
    )
    if payload.get("kind") != "objects-page":
        raise _stale_cursor()
    if payload.get("identity") != expected["identity"]:
        raise _stale_cursor()
    if payload.get("generation") != expected["generation"]:
        raise _stale_cursor()
    for key in (
        "detail",
        "limit",
        "filter",
        "propertyOffset",
        "propertyLimit",
        "selection",
    ):
        if payload.get(key) != expected[key]:
            raise _stale_cursor()
    last = payload.get("last")
    if not isinstance(last, str):
        raise _stale_cursor()
    return {"last": last, "selection": payload.get("selection")}


# ---------------------------------------------------------------------------
# Handlers.
# ---------------------------------------------------------------------------


def inspect_objects(ctx: Any, args: dict) -> dict:
    doc = ctx.require_document(args["document"])
    detail = str(args.get("detail") or "compact")
    if detail not in ("compact", "full"):
        raise ToolError(VALIDATION_FAILED, "detail must be 'compact' or 'full'")
    limit = args.get("limit")
    limit = 32 if limit is None else int(limit)
    limit = max(1, min(_MAX_LIMIT, limit))
    props = [str(prop) for prop in (args.get("property_filter") or [])]
    property_offset = args.get("property_offset")
    property_offset = 0 if property_offset is None else int(property_offset)
    property_offset = max(0, property_offset)
    property_limit = args.get("property_limit")
    property_limit = _MAX_PROPERTY_PAGE if property_limit is None else int(property_limit)
    property_limit = max(1, min(_MAX_PROPERTY_PAGE, property_limit))

    selection: list[str] | None = None
    requested = args.get("objects")
    if requested is not None:
        if not isinstance(requested, list) or not (1 <= len(requested) <= 64):
            raise ToolError(
                VALIDATION_FAILED,
                "objects must be an array of 1 to 64 object names",
            )
        names = [str(name) for name in requested]
        if any(not name for name in names):
            raise ToolError(VALIDATION_FAILED, "objects names must be non-empty")
        if len(set(names)) != len(names):
            raise ToolError(VALIDATION_FAILED, "objects names must be unique")
        # Resolve every requested name before producing rows; the cursor
        # binds the actual sanitized Names, sorted into page order.
        resolved = [ctx.require_object(doc, name) for name in names]
        selection = sorted({str(getattr(obj, "Name", "")) for obj in resolved})

    start_after: str | None = None
    cursor = args.get("cursor")
    if cursor:
        opened = _open_cursor(
            ctx,
            doc,
            str(cursor),
            detail,
            limit,
            props,
            property_offset=property_offset,
            property_limit=property_limit,
            selection=selection,
        )
        start_after = opened["last"]
        if selection is None and opened["selection"]:
            cursor_selection = [str(name) for name in opened["selection"]]
            resolved = [ctx.require_object(doc, name) for name in cursor_selection]
            selection = sorted({str(getattr(obj, "Name", "")) for obj in resolved})

    if selection is not None:
        by_name = {str(getattr(obj, "Name", "")): obj for obj in getattr(doc, "Objects", ()) or ()}
        objects = [by_name[name] for name in selection if name in by_name]
    else:
        objects = sorted(
            getattr(doc, "Objects", ()) or (),
            key=lambda obj: str(getattr(obj, "Name", "")),
        )
    total = len(objects)
    if start_after is not None:
        objects = [obj for obj in objects if str(getattr(obj, "Name", "")) > start_after]
    page = objects[:limit]
    rows = [
        _row(
            obj,
            detail,
            props,
            property_offset=property_offset,
            property_limit=property_limit,
        )
        for obj in page
    ]
    next_cursor = None
    if len(objects) > len(page) and page:
        next_cursor = _make_cursor(
            ctx,
            doc,
            detail,
            limit,
            props,
            str(getattr(page[-1], "Name", "")),
            property_offset=property_offset,
            property_limit=property_limit,
            selection=selection,
        )
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "detail": detail,
        "total": total,
        "count": len(rows),
        "objects": rows,
        "nextCursor": next_cursor,
    }


_DEFAULT_BOUNDS_TOLERANCE = 0.000001


def _snapshot_requested(obj: Any, properties: dict) -> list[tuple[str, Any]]:
    """Read the requested document/ViewObject properties for a delta row.

    Values pass through ``_jsonify`` so the rows use the same bounded
    inspection encoding as ``inspect_objects``.
    """

    rows: list[tuple[str, Any]] = []
    for key, value in properties.items():
        if key == "ViewObject" and isinstance(value, dict):
            view = getattr(obj, "ViewObject", None)
            for sub in value:
                if view is not None and _property_exists(view, sub):
                    rows.append(("ViewObject." + sub, _jsonify(getattr(view, sub, None))))
                else:
                    rows.append(("ViewObject." + sub, _unavailable("no-such-property")))
        elif key == "ShapeColor":
            view = getattr(obj, "ViewObject", None)
            if view is not None and _property_exists(view, "ShapeColor"):
                rows.append((key, _jsonify(getattr(view, "ShapeColor", None))))
            else:
                rows.append((key, _unavailable("no-such-property")))
        else:
            rows.append((key, _read_value(obj, key)))
    return rows


def _change_summary(
    properties: list[dict],
    *,
    solid_before: int | None,
    solid_after: int | None,
    volume_before: float | None,
    volume_after: float | None,
    bounds_before: list[float] | None,
    bounds_after: list[float] | None,
    dependents_before: int,
    dependents: int,
) -> dict:
    """Build the compact factual ``change`` summary for one target."""

    return {
        "properties": properties,
        "geometry": {
            "solidCountBefore": solid_before,
            "solidCountAfter": solid_after,
            "volumeBefore": volume_before,
            "volumeAfter": volume_after,
            "boundsBefore": bounds_before,
            "boundsAfter": bounds_after,
        },
        "dependentCountBefore": dependents_before,
        "dependentCount": dependents,
    }


def _check_workload(ctx: Any, targets: list[Any]) -> None:
    """Apply the shared bounded feature checks to generic object edits.

    Delegated lazily so this module keeps importing no FreeCAD-dependent
    sibling at load time.
    """

    from .feature_contracts import check_workload

    check_workload(ctx, targets)


def create_object(ctx: Any, args: dict) -> dict:
    doc = ctx.require_document(args["document"])
    obj_type = str(args["type"])
    requested_name = str(args["name"])
    properties = dict(args.get("properties") or {})
    expected_solids = args.get("expected_solids")
    expected_bounds = args.get("expected_bounds")
    bounds_tolerance = args.get("bounds_tolerance")
    if bounds_tolerance is None:
        bounds_tolerance = _DEFAULT_BOUNDS_TOLERANCE

    factory, factory_kwargs = _plan_create(ctx, doc, obj_type, properties)

    created: list[Any] = []
    outcome: dict = {}
    with mutation(
        ctx,
        doc,
        "create_object",
        lambda: created,
        expected_solids=expected_solids,
        expected_bounds=expected_bounds,
        bounds_tolerance=float(bounds_tolerance),
        outcome=outcome,
        check_workload=_check_workload,
    ) as applied:
        if factory is not None:
            created.append(_call_factory(factory, doc, requested_name, factory_kwargs))
        else:
            created.append(doc.addObject(obj_type, requested_name))
        if properties:
            _apply_properties(ctx, doc, created[0], properties)

    obj = created[0]
    # The gate already reported this new object after its recompute; reusing
    # that report keeps the summary factual without a second shape probe.
    report = outcome["reports"][str(obj.Name)]
    after_rows = _snapshot_requested(obj, properties)
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": {
            "name": str(getattr(obj, "Name", "")),
            "label": _label(obj),
            "typeId": str(getattr(obj, "TypeId", "")),
        },
        "report": report,
        "applied": applied,
        "change": _change_summary(
            [{"name": name, "before": None, "after": after} for name, after in after_rows],
            solid_before=None,
            solid_after=report["solid_count"],
            volume_before=None,
            volume_after=report["volume"],
            bounds_before=None,
            bounds_after=report["bounds"],
            # A created object had no pre-mutation dependent closure.
            dependents_before=0,
            dependents=outcome["dependentCountAfter"],
        ),
    }


def edit_object(ctx: Any, args: dict) -> dict:
    doc = ctx.require_document(args["document"])
    obj = ctx.require_object(doc, str(args["object"]))
    properties = args["properties"]
    if not isinstance(properties, dict) or not properties:
        raise ToolError(VALIDATION_FAILED, "properties must be a non-empty object")
    expected_solids = args.get("expected_solids")
    expected_bounds = args.get("expected_bounds")
    bounds_tolerance = args.get("bounds_tolerance")
    if bounds_tolerance is None:
        bounds_tolerance = _DEFAULT_BOUNDS_TOLERANCE
    prepared = _prepare_properties(ctx, doc, obj, properties)

    before_rows = _snapshot_requested(obj, properties)
    before_report = geometry_report(obj)
    bounds_before = document_bounds(obj)
    dependents = dependent_count([obj])

    outcome: dict = {}
    with mutation(
        ctx,
        doc,
        f"edit_object:{obj.Name}",
        [obj],
        expected_solids=expected_solids,
        expected_bounds=expected_bounds,
        bounds_tolerance=float(bounds_tolerance),
        outcome=outcome,
        check_workload=_check_workload,
    ) as applied:
        _apply_prepared(obj, prepared)

    after_rows = _snapshot_requested(obj, properties)
    # The gate's post-recompute report for this target replaces a second
    # probe of the same shape.
    report = outcome["reports"][str(obj.Name)]
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": {
            "name": str(getattr(obj, "Name", "")),
            "label": _label(obj),
            "typeId": str(getattr(obj, "TypeId", "")),
        },
        "report": report,
        "applied": applied,
        "change": _change_summary(
            [
                {"name": name, "before": before, "after": after}
                for (name, before), (_after, after) in zip(before_rows, after_rows, strict=True)
            ],
            solid_before=before_report["solid_count"],
            solid_after=report["solid_count"],
            volume_before=before_report["volume"],
            volume_after=report["volume"],
            bounds_before=bounds_before,
            bounds_after=report["bounds"],
            dependents_before=dependents,
            dependents=outcome["dependentCountAfter"],
        ),
    }


def delete_object(ctx: Any, args: dict) -> dict:
    doc = ctx.require_document(args["document"])
    obj = ctx.require_object(doc, str(args["object"]))
    name = str(getattr(obj, "Name", ""))
    dependents: list[str] = []
    try:
        dependents = sorted(
            {
                str(getattr(dep, "Name", ""))
                for dep in list(getattr(obj, "InList", ()) or ())
                if getattr(dep, "Name", "")
            }
        )
    except Exception as exc:  # InList must be readable to refuse safely.
        raise ToolError(
            VALIDATION_FAILED,
            f"cannot enumerate dependents of '{name}': {_describe(exc)}",
        ) from exc
    if dependents:
        raise ToolError(
            VALIDATION_FAILED,
            f"object '{name}' has dependents; refusing to delete it without "
            "them being removed first",
            {"dependents": dependents[:_MAX_DEPENDENTS_LISTED]},
        )
    removed = {
        "name": name,
        "label": _label(obj),
        "typeId": str(getattr(obj, "TypeId", "")),
    }
    with mutation(ctx, doc, f"delete_object:{name}", [obj]) as applied:
        doc.removeObject(name)
        if doc.getObject(name) is not None:
            raise ToolError(VALIDATION_FAILED, f"object '{name}' was not removed")
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "removed": removed,
        "applied": applied,
    }


def edit_objects(ctx: Any, args: dict) -> dict:
    doc = ctx.require_document(args["document"])
    edits = args["edits"]
    if not isinstance(edits, list) or not (1 <= len(edits) <= 32):
        raise ToolError(VALIDATION_FAILED, "edits must be an array of 1 to 32 entries")
    names: list[str] = []
    targets: list[Any] = []
    prepared: dict[str, list[tuple[str, str, Any]]] = {}
    for position, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ToolError(VALIDATION_FAILED, f"edits[{position}] must be an object")
        properties = edit.get("properties")
        if not isinstance(properties, dict) or not properties:
            raise ToolError(
                VALIDATION_FAILED,
                f"edits[{position}].properties must be a non-empty object",
            )
        obj = ctx.require_object(doc, str(edit.get("object")))
        if obj.Name in prepared:
            raise ToolError(
                VALIDATION_FAILED,
                f"edits list object '{obj.Name}' more than once",
            )
        prepared[obj.Name] = _prepare_properties(ctx, doc, obj, properties)
        names.append(obj.Name)
        targets.append(obj)

    expectations = args.get("expectations") or {}
    if not isinstance(expectations, dict):
        raise ToolError(VALIDATION_FAILED, "expectations must be an object")
    unknown = sorted(set(expectations) - set(prepared))
    if unknown:
        raise ToolError(
            VALIDATION_FAILED,
            f"expectations name objects that are not edited: {unknown}",
        )

    before_rows: dict[str, list[tuple[str, Any]]] = {}
    before_reports: dict[str, dict] = {}
    before_bounds: dict[str, list[float] | None] = {}
    for obj, edit in zip(targets, edits, strict=True):
        before_rows[obj.Name] = _snapshot_requested(obj, edit["properties"])
        before_reports[obj.Name] = geometry_report(obj)
        before_bounds[obj.Name] = document_bounds(obj)
    dependent_counts = {obj.Name: dependent_count([obj]) for obj in targets}

    outcome: dict = {}
    with mutation(
        ctx,
        doc,
        "edit_objects",
        targets,
        expectations=expectations,
        outcome=outcome,
        check_workload=_check_workload,
    ) as applied:
        for obj in targets:
            _apply_prepared(obj, prepared[obj.Name])

    changes = []
    for obj, edit in zip(targets, edits, strict=True):
        after_rows = _snapshot_requested(obj, edit["properties"])
        # Per-target post-recompute report from the gate's single pass.
        report = outcome["reports"][str(obj.Name)]
        changes.append(
            _change_summary(
                [
                    {"name": name, "before": before, "after": after}
                    for (name, before), (_after, after) in zip(
                        before_rows[obj.Name], after_rows, strict=True
                    )
                ],
                solid_before=before_reports[obj.Name]["solid_count"],
                solid_after=report["solid_count"],
                volume_before=before_reports[obj.Name]["volume"],
                volume_after=report["volume"],
                bounds_before=before_bounds[obj.Name],
                bounds_after=report["bounds"],
                dependents_before=dependent_counts[obj.Name],
                dependents=outcome["dependentCountAfter"],
            )
        )
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "objects": [
            {
                "name": str(getattr(obj, "Name", "")),
                "label": _label(obj),
                "typeId": str(getattr(obj, "TypeId", "")),
            }
            for obj in targets
        ],
        "changes": changes,
        "applied": applied,
    }


HANDLERS = {
    "inspect_objects": inspect_objects,
    "create_object": create_object,
    "edit_object": edit_object,
    "edit_objects": edit_objects,
    "delete_object": delete_object,
}
