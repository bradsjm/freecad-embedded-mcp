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

import math
from typing import Any

import FreeCAD

from ..object_validation import geometry_report, mutation
from ..protocol import DOMAIN_CURSOR, VALIDATION_FAILED, ToolError

_MAX_LIMIT = 500
_MAX_LINKS = 64
_MAX_FILTER = 64
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
    {
        f"Fem::Constraint{name}": (f"makeConstraint{name}", {})
        for name in _FEM_CONSTRAINTS
    }
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
            "anyOf": _json_scalars()
            + [
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

_PROPERTY_VALUE = {
    "anyOf": _json_scalars()
    + [
        _XYZ,
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
        "placement",
        "bounds",
        "shape_valid",
        "solid_count",
        "tip",
        "links",
        "properties",
    ],
    "properties": {
        "name": {"type": "string"},
        "label": {"type": "string"},
        "typeId": {"type": "string"},
        "state": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
        "placement": {"$ref": "#/$defs/placement"},
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
        "properties": {"type": "object", "additionalProperties": _PROPERTY_VALUE},
    },
}

_ROW_DEFS = {
    "placement": _PLACEMENT_ROW,
    "objectRow": _OBJECT_ROW,
}

_MUTATION_OUTPUT_DEFS = {
    "geometryReport": _GEOMETRY_REPORT,
    "objectIdentity": _OBJECT_IDENTITY,
}

# ---------------------------------------------------------------------------
# Input schemas (defaults are applied by the handlers).
# ---------------------------------------------------------------------------

_INSPECT_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document"],
    "properties": {
        "document": _DOCUMENT_FIELD,
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
            "default": 100,
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
    "required": ["document", "generation", "object", "report", "applied"],
    "properties": {
        "document": {"type": "string"},
        "generation": _GENERATION,
        "object": {"$ref": "#/$defs/objectIdentity"},
        "report": {"$ref": "#/$defs/geometryReport"},
        "applied": _APPLIED,
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

TOOL_DEFINITIONS = [
    {
        "name": "inspect_objects",
        "description": (
            "List a document's objects sorted by Name. Compact rows carry "
            "label, TypeId, state, placement, bounds, shape validity, solid "
            "count, Body tip and link identities; full detail adds requested "
            "properties with typed unavailable markers. Pagination uses an "
            "opaque signed cursor bound to the document generation and "
            "filters; a stale cursor is a restart-pagination error."
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
            "constraints and elements). Returns the actual sanitized "
            "identity and post-recompute validation."
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
            "exposes it."
        ),
        "inputSchema": _EDIT_INPUT,
        "outputSchema": _MUTATED_OUTPUT,
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
        return obj.Shape
    except Exception:
        return None


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
        for coordinate, axis in zip(coordinates, ("x", "y", "z"))
    ]
    return FreeCAD.Vector(*numbers)


def _color_value(value: Any, what: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
        raise ToolError(
            VALIDATION_FAILED, f"{what} must be an RGB or RGBA number array"
        )
    parts = [
        _number(component, f"{what}[{index}]") for index, component in enumerate(value)
    ]
    if len(parts) == 3:
        parts.append(1.0)
    return (parts[0], parts[1], parts[2], parts[3])


def _rotation_value(value: Any, what: str) -> Any:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} must be an object with an Axis {{x, y, z}} and an Angle "
            "in degrees",
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
            f"{what} must be an object with Base/Position and Rotation parts",
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
    resolved, native = _resolve_reference(
        ctx, doc, {"object": name, "subelement": subelement}
    )
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
            _string(entry, f"{prop}[{index}]")
            for index, entry in enumerate(_as_array(value, prop))
        ]
    if ptype == "App::PropertyIntegerList":
        return [
            _integer(entry, f"{prop}[{index}]")
            for index, entry in enumerate(_as_array(value, prop))
        ]
    if ptype == "App::PropertyFloatList":
        return [
            _number(entry, f"{prop}[{index}]")
            for index, entry in enumerate(_as_array(value, prop))
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


def _plan_create(
    ctx: Any, doc: Any, obj_type: str, properties: dict
) -> tuple[Any, dict[str, Any]]:
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
            message = (
                f"FEM type '{obj_type}' has no explicit creation factory in "
                "this protocol"
            )
            if obj_type.startswith("Fem::FemMesh"):
                message += (
                    "; FEM mesh objects are not created by create_object, "
                    "use run_script for meshing workflows"
                )
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
                    f"FEM type '{obj_type}' requires a canonical '{key}' "
                    "link in properties",
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
                {"supportedTypes": types[:_MAX_FILTER]},
            )
    return None, {}


def _call_factory(
    factory: Any, doc: Any, requested_name: str, kwargs: dict[str, Any]
) -> Any:
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
        f"ViewObject property '{prop}' accepts only scalars or colors over "
        "this protocol",
    )


def _check_view_property(obj: Any, view: Any, prop: str) -> None:
    if not _property_exists(view, prop):
        raise ToolError(
            VALIDATION_FAILED,
            f"ViewObject of '{getattr(obj, 'Name', '<unknown>')}' has no "
            f"property '{prop}'",
        )
    if _is_read_only(view, prop):
        raise ToolError(
            VALIDATION_FAILED,
            f"ViewObject property '{prop}' of "
            f"'{getattr(obj, 'Name', '<unknown>')}' is read-only",
        )


def _check_document_property(obj: Any, prop: str) -> None:
    name = str(getattr(obj, "Name", "<unknown>"))
    if not _property_exists(obj, prop):
        raise ToolError(VALIDATION_FAILED, f"object '{name}' has no property '{prop}'")
    if _is_read_only(obj, prop):
        raise ToolError(
            VALIDATION_FAILED, f"property '{prop}' of object '{name}' is read-only"
        )


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
            setattr(view, "ShapeColor", _color_value(value, "ShapeColor"))
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


def _placement_row(obj: Any) -> dict | None:
    try:
        placement = obj.Placement
        base = placement.Base
        rotation = placement.Rotation
        axis = rotation.Axis
        row = {
            "position": [float(base.x), float(base.y), float(base.z)],
            "axis": [float(axis.x), float(axis.y), float(axis.z)],
            "angle_deg": float(rotation.Angle),
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
                "angle_deg": float(value.Rotation.Angle),
            }
        if hasattr(value, "x") and hasattr(value, "y") and hasattr(value, "z"):
            return {"x": float(value.x), "y": float(value.y), "z": float(value.z)}
        if hasattr(value, "UserString"):
            return str(value)
        if hasattr(value, "Name") and hasattr(value, "TypeId"):
            return str(value.Name)
    except Exception:
        return _unavailable(type(value).__name__)
    if isinstance(value, (list, tuple)):
        converted: list[Any] = []
        for item in value:
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


def _row(obj: Any, detail: str, props: list[str]) -> dict:
    shape = _shape(obj)
    properties: dict[str, Any] = {}
    if detail == "full":
        seen: list[str] = []
        for prop in props:
            if prop not in seen:
                seen.append(prop)
        properties = {prop: _read_value(obj, prop) for prop in seen}
    return {
        "name": str(getattr(obj, "Name", "")),
        "label": _label(obj),
        "typeId": str(getattr(obj, "TypeId", "")),
        "state": _states(obj),
        "placement": _placement_row(obj),
        "bounds": _bounds(shape),
        "shape_valid": _shape_valid(shape),
        "solid_count": _solid_count(shape),
        "tip": _tip_name(obj),
        "links": _link_names(obj),
        "properties": properties,
    }


# ---------------------------------------------------------------------------
# Signed pagination cursor.
# ---------------------------------------------------------------------------


def _cursor_payload(
    ctx: Any, doc: Any, detail: str, limit: int, props: list[str], last: str
) -> dict:
    return {
        "kind": "objects-page",
        "identity": str(ctx.document_identity(doc)),
        "generation": int(ctx.document_generation(doc)),
        "detail": detail,
        "limit": limit,
        "filter": sorted(str(prop) for prop in props),
        "last": last,
    }


def _stale_cursor() -> ToolError:
    return ToolError(
        VALIDATION_FAILED,
        "pagination cursor is stale; restart pagination from the beginning",
        {"reason": "stale_cursor"},
    )


def _make_cursor(
    ctx: Any, doc: Any, detail: str, limit: int, props: list[str], last: str
) -> str:
    return ctx.signer.sign(
        DOMAIN_CURSOR, _cursor_payload(ctx, doc, detail, limit, props, last)
    )


def _open_cursor(
    ctx: Any,
    doc: Any,
    cursor: str,
    detail: str,
    limit: int,
    props: list[str],
) -> dict:
    payload = ctx.signer.verify(DOMAIN_CURSOR, cursor)
    expected = _cursor_payload(ctx, doc, detail, limit, props, "")
    if payload.get("kind") != "objects-page":
        raise _stale_cursor()
    if payload.get("identity") != expected["identity"]:
        raise _stale_cursor()
    if payload.get("generation") != expected["generation"]:
        raise _stale_cursor()
    for key in ("detail", "limit", "filter"):
        if payload.get(key) != expected[key]:
            raise _stale_cursor()
    last = payload.get("last")
    if not isinstance(last, str):
        raise _stale_cursor()
    return {"last": last}


# ---------------------------------------------------------------------------
# Handlers.
# ---------------------------------------------------------------------------


def inspect_objects(ctx: Any, args: dict) -> dict:
    doc = ctx.require_document(args["document"])
    detail = str(args.get("detail") or "compact")
    if detail not in ("compact", "full"):
        raise ToolError(VALIDATION_FAILED, "detail must be 'compact' or 'full'")
    limit = args.get("limit")
    limit = 100 if limit is None else int(limit)
    limit = max(1, min(_MAX_LIMIT, limit))
    props = [str(prop) for prop in (args.get("property_filter") or [])]

    start_after: str | None = None
    cursor = args.get("cursor")
    if cursor:
        start_after = _open_cursor(ctx, doc, str(cursor), detail, limit, props)["last"]

    objects = sorted(
        list(getattr(doc, "Objects", ()) or ()),
        key=lambda obj: str(getattr(obj, "Name", "")),
    )
    total = len(objects)
    if start_after is not None:
        objects = [
            obj for obj in objects if str(getattr(obj, "Name", "")) > start_after
        ]
    page = objects[:limit]
    rows = [_row(obj, detail, props) for obj in page]
    next_cursor = None
    if len(objects) > len(page) and page:
        next_cursor = _make_cursor(
            ctx, doc, detail, limit, props, str(getattr(page[-1], "Name", ""))
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


def create_object(ctx: Any, args: dict) -> dict:
    doc = ctx.require_document(args["document"])
    obj_type = str(args["type"])
    requested_name = str(args["name"])
    properties = dict(args.get("properties") or {})
    expected_solids = args.get("expected_solids")

    factory, factory_kwargs = _plan_create(ctx, doc, obj_type, properties)

    created: list[Any] = []
    with mutation(
        ctx, doc, "create_object", lambda: created, expected_solids=expected_solids
    ) as applied:
        if factory is not None:
            created.append(_call_factory(factory, doc, requested_name, factory_kwargs))
        else:
            created.append(doc.addObject(obj_type, requested_name))
        if properties:
            _apply_properties(ctx, doc, created[0], properties)

    obj = created[0]
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": {
            "name": str(getattr(obj, "Name", "")),
            "label": _label(obj),
            "typeId": str(getattr(obj, "TypeId", "")),
        },
        "report": geometry_report(obj, expected_solids),
        "applied": applied,
    }


def edit_object(ctx: Any, args: dict) -> dict:
    doc = ctx.require_document(args["document"])
    obj = ctx.require_object(doc, str(args["object"]))
    properties = args["properties"]
    if not isinstance(properties, dict) or not properties:
        raise ToolError(VALIDATION_FAILED, "properties must be a non-empty object")
    expected_solids = args.get("expected_solids")
    prepared = _prepare_properties(ctx, doc, obj, properties)

    with mutation(
        ctx, doc, f"edit_object:{obj.Name}", [obj], expected_solids=expected_solids
    ) as applied:
        _apply_prepared(obj, prepared)

    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": {
            "name": str(getattr(obj, "Name", "")),
            "label": _label(obj),
            "typeId": str(getattr(obj, "TypeId", "")),
        },
        "report": geometry_report(obj, expected_solids),
        "applied": applied,
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


HANDLERS = {
    "inspect_objects": inspect_objects,
    "create_object": create_object,
    "edit_object": edit_object,
    "delete_object": delete_object,
}
