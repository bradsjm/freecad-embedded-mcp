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
import re
from collections.abc import Mapping
from typing import Any

import FreeCAD

from .. import topology_query as tq
from ..object_validation import (
    dependent_count,
    document_bounds,
    geometry_report,
    mutation,
    shape_is_null,
)
from ..protocol import (
    DOMAIN_CURSOR,
    VALIDATION_FAILED,
    ProtocolError,
    ToolError,
    fingerprint,
    validate_schema,
)
from ..tool_contracts import (
    _BOUNDS_TOLERANCE,
    _DEFAULT_BOUNDS_TOLERANCE,
    _EXPECTED_BOUNDS,
    _EXPECTED_GENERATION,
    _EXPECTED_SOLIDS,
    _GEOMETRY_REPORT,
    require_expected_generation,
)

_MAX_LINKS = 64
_MAX_FILTER = 64
_MAX_PROPERTY_PAGE = 64
#: Wire bounds for an explicit inspect_objects selection: the handler pages
#  rows at _MAX_PROPERTY_PAGE, so the schema must advertise the same bound
#  instead of the wider whole-document listing cap.
_MAX_SELECTION = _MAX_PROPERTY_PAGE
_PROPERTY_LIST_LIMIT = 64
_MAPPING_LIMIT = 64
_JSONIFY_LEAF_BUDGET = 1024
_ENUMERATION_LIMIT = 64
_MAX_DEPENDENTS_LISTED = 64
_MAX_SPREADSHEET_CELLS = 256
_MAX_SPREADSHEET_CONTENT = 4096
_MAX_SPREADSHEET_ERROR = 1024
_SPREADSHEET_TYPE = "Spreadsheet::Sheet"
_CELL_ADDRESS = re.compile(r"^[A-Za-z]+[1-9][0-9]*$")
_SPREADSHEET_SIMPLE_UNITS = frozenset(
    {
        "a",
        "cm",
        "deg",
        "f",
        "ft",
        "g",
        "h",
        "in",
        "kg",
        "m",
        "mil",
        "min",
        "mm",
        "n",
        "nm",
        "pa",
        "rad",
        "s",
        "um",
        "v",
        "w",
        "%",
        "°",
    }
)

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
    """Return the scalar JSON-schema alternatives shared by value definitions."""
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
                {
                    "type": "object",
                    "maxProperties": _MAPPING_LIMIT,
                    "additionalProperties": previous,
                },
            ]
        }
    return defs


_INPUT_VALUE = {"$ref": "#/$defs/value4"}
_VALUE_DEFS = {**_value_defs(), **tq.QUERY_DEFS}

#: Nonrecursive link-aware property value: plain JSON values, a declarative
#: query target, or an array of shared link targets (LinkSubList entries).
#: Query definitions never reference valueN or inputPropertyValue, so the
#: existing bounded recursion depth is unchanged.
_INPUT_PROPERTY_VALUE = {
    "anyOf": [
        _INPUT_VALUE,
        {"$ref": "#/$defs/topologyQueryTarget"},
        {
            "type": "array",
            "items": {"$ref": "#/$defs/topologyTarget"},
            "maxItems": 64,
        },
    ]
}

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

#: Shared target vocabulary: whole object identity, a fresh signed
#: reference, or a declarative query. Which forms a position accepts is
#: decided by the resolver, not by this schema union.
_CANONICAL_REF = {"$ref": "#/$defs/topologyTarget"}

_PROPERTIES_MAP = {"type": "object", "additionalProperties": _INPUT_PROPERTY_VALUE}
_GENERATION = {"type": "integer", "minimum": 0}
_APPLIED = {"type": "array", "items": {"type": "string"}, "maxItems": 512}

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

_UNAVAILABLE_LINK = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object", "reason"],
    "properties": {
        "object": {"type": "string"},
        "reason": {"type": "string"},
        "nativeSubelement": {"type": "string"},
    },
}

_BOUNDED_MAPPING = {
    "type": "object",
    "maxProperties": _MAPPING_LIMIT,
    "additionalProperties": {
        "anyOf": [
            *_json_scalars(),
            {"type": "array", "maxItems": 64},
            {"type": "object", "maxProperties": _MAPPING_LIMIT},
        ]
    },
}

_WHOLE_LINK_OUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object"],
    "properties": {"object": {"type": "string", "minLength": 1}},
}

_SIGNED_LINK_OUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object", "subelement"],
    "properties": {
        "object": {"type": "string", "minLength": 1},
        "subelement": {"type": "string", "minLength": 1},
    },
}

_PROPERTY_VALUE = {
    "anyOf": [
        *_json_scalars(),
        _XYZ,
        _PLACEMENT_VALUE,
        _WHOLE_LINK_OUT,
        _SIGNED_LINK_OUT,
        _UNAVAILABLE_LINK,
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
        _BOUNDED_MAPPING,
    ]
}

#: Receipts for query-origin link/reference parameters. count equals the
#: reference count; the handler enforces that and the operation-wide budget.
_RESOLVED_SELECTIONS = {
    "type": "array",
    "items": {"$ref": "#/$defs/topologyResolvedSelection"},
    "minItems": 1,
    "maxItems": tq.MAX_QUERY_REFERENCES,
}

_SPREADSHEET_VALUE = {
    "anyOf": [
        {"type": "string", "maxLength": _MAX_SPREADSHEET_CONTENT},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
        _XYZ,
        _PLACEMENT_VALUE,
        _LINK_VALUE,
        {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string", "maxLength": _MAX_SPREADSHEET_CONTENT},
                    {"type": "number"},
                    {"type": "boolean"},
                ]
            },
            "maxItems": 64,
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["unavailable"],
            "properties": {"unavailable": {"type": "string", "maxLength": _MAX_SPREADSHEET_ERROR}},
        },
        _BOUNDED_MAPPING,
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
        "linkCount",
        "linksTruncated",
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
        "linkCount": {"type": "integer", "minimum": 0},
        "linksTruncated": {"type": "boolean"},
        "featureCount": {"type": "integer", "minimum": 0},
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
        "expression": {
            "type": ["string", "null"],
            "maxLength": _MAX_SPREADSHEET_CONTENT,
        },
        "formula": {"type": ["string", "null"], "maxLength": _MAX_SPREADSHEET_CONTENT},
        "formulaTruncated": {"type": "boolean"},
        "expressionTruncated": {"type": "boolean"},
    },
}

_SPREADSHEET_CELL = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "address",
        "alias",
        "content",
        "contentTruncated",
        "formula",
        "formulaTruncated",
        "value",
        "valueTruncated",
        "error",
    ],
    "properties": {
        "address": {"type": "string", "minLength": 1},
        "alias": {"type": ["string", "null"]},
        "content": {"type": "string", "maxLength": _MAX_SPREADSHEET_CONTENT},
        "contentTruncated": {"type": "boolean"},
        "formula": {"type": ["string", "null"], "maxLength": _MAX_SPREADSHEET_CONTENT},
        "formulaTruncated": {"type": "boolean"},
        "value": _SPREADSHEET_VALUE,
        "valueTruncated": {"type": "boolean"},
        "error": {"type": ["string", "null"], "maxLength": _MAX_SPREADSHEET_ERROR},
    },
}

_SPREADSHEET_INFO = {
    "type": "object",
    "additionalProperties": False,
    "required": ["available", "usedRange", "cells", "cellCount", "truncated"],
    "properties": {
        "available": {"type": "boolean"},
        "error": {"type": "string", "maxLength": _MAX_SPREADSHEET_ERROR},
        "usedRange": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["from", "to"],
            "properties": {
                "from": {"type": "string", "minLength": 1},
                "to": {"type": "string", "minLength": 1},
            },
        },
        "cells": {"type": "array", "items": _SPREADSHEET_CELL, "maxItems": _MAX_SPREADSHEET_CELLS},
        "cellCount": {"type": "integer", "minimum": 0},
        "truncated": {"type": "boolean"},
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
        "spreadsheet": _SPREADSHEET_INFO,
    }
)

_ROW_DEFS = {
    "placement": _PLACEMENT_ROW,
    "objectRow": _OBJECT_ROW,
    "propertyMetadata": _PROPERTY_METADATA,
    "spreadsheetCell": _SPREADSHEET_CELL,
    "spreadsheetInfo": _SPREADSHEET_INFO,
    "boundedPropertyValue": _PROPERTY_VALUE,
}

_CHANGE_PROPERTY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "after"],
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
    "required": ["properties"],
    "properties": {
        "properties": {"type": "array", "items": _CHANGE_PROPERTY},
        "geometry": _CHANGE_GEOMETRY,
        "dependentCountBefore": {"type": "integer", "minimum": 0},
        "dependentCount": {"type": "integer", "minimum": 0},
        "cellContentsPersisted": {"type": "boolean"},
    },
}

_MUTATION_OUTPUT_DEFS = {
    "geometryReport": _GEOMETRY_REPORT,
    "objectIdentity": _OBJECT_IDENTITY,
    **tq.QUERY_DEFS,
}

# ---------------------------------------------------------------------------
# Input schemas (defaults are applied by the handlers).
# ---------------------------------------------------------------------------

_DOCUMENT_FIELD = {"type": "string", "minLength": 1}
_NAME_FIELD = {"type": "string", "minLength": 1}
_RESPONSE_DETAIL = {
    "type": "string",
    "enum": ["compact", "full"],
    "default": "compact",
}
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
            "maxItems": _MAX_SELECTION,
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
            "maximum": _MAX_PROPERTY_PAGE,
            "default": 32,
        },
        "property_offset": {"type": "integer", "minimum": 0, "default": 0},
        "property_limit": {
            "type": "integer",
            "minimum": 1,
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
        "response_detail": _RESPONSE_DETAIL,
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
        "expected_generation": _EXPECTED_GENERATION,
        "expected_solids": _EXPECTED_SOLIDS,
        "expected_bounds": _EXPECTED_BOUNDS,
        "bounds_tolerance": _BOUNDS_TOLERANCE,
        "response_detail": _RESPONSE_DETAIL,
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
        "expected_generation": _EXPECTED_GENERATION,
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
        "response_detail": _RESPONSE_DETAIL,
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
        "expected_generation": _EXPECTED_GENERATION,
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
            "maxItems": 64,
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
        "resolvedSelections": _RESOLVED_SELECTIONS,
    },
    "$defs": _MUTATION_OUTPUT_DEFS,
}

_DELETE_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "generation", "removed", "rerouted", "applied"],
    "properties": {
        "document": {"type": "string"},
        "generation": _GENERATION,
        "removed": {"$ref": "#/$defs/objectIdentity"},
        "rerouted": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "feature": {"type": "string"},
                    "baseFeature": {"type": ["string", "null"]},
                },
                "required": ["feature", "baseFeature"],
            },
        },
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
        "resolvedSelections": _RESOLVED_SELECTIONS,
    },
    "$defs": _MUTATION_OUTPUT_DEFS,
}

_CREATE_OBJECTS_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "entries"],
    "properties": {
        "document": _DOCUMENT_FIELD,
        "entries": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "name"],
                "properties": {
                    "type": _NAME_FIELD,
                    "name": _NAME_FIELD,
                    "properties": _PROPERTIES_MAP,
                },
            },
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
        "response_detail": _RESPONSE_DETAIL,
    },
    "$defs": _VALUE_DEFS,
}

_CREATE_OBJECTS_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "document",
        "generation",
        "objects",
        "nameMapping",
        "changes",
        "applied",
    ],
    "properties": {
        "document": {"type": "string"},
        "generation": _GENERATION,
        "objects": {
            "type": "array",
            "items": {"$ref": "#/$defs/objectIdentity"},
            "maxItems": 32,
        },
        "nameMapping": {
            "type": "array",
            "maxItems": 32,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["requested", "actual"],
                "properties": {
                    "requested": {"type": "string"},
                    "actual": {"type": "string"},
                },
            },
        },
        "changes": {
            "type": "array",
            "items": _CHANGE,
            "maxItems": 32,
        },
        "applied": _APPLIED,
        "resolvedSelections": _RESOLVED_SELECTIONS,
    },
    "$defs": _MUTATION_OUTPUT_DEFS,
}


TOOL_DEFINITIONS = [
    {
        "name": "inspect_objects",
        "description": (
            "List a document's objects sorted by Name, or an explicit "
            "object selection resolved by name. Compact rows carry "
            "identity (name, label, TypeId), state, bounds, shape validity, "
            "solid count, Body tip and link identities; full detail adds "
            "local and global placements, property pages and property "
            "metadata with typed unavailable markers and expressions. "
            "Spreadsheet sheets also expose a bounded cell inventory with "
            "raw contents, formulas, aliases, evaluated values and errors. "
            "Cell rows mark truncated content and values explicitly. "
            "Pagination uses an opaque signed cursor bound to the document "
            "generation, the selection and the filters; a stale cursor is a "
            "restart-pagination error. limit and property_limit are "
            "requested page sizes; the server may return a smaller page — "
            "continue with nextCursor and nextPropertyOffset. Continuation "
            "calls must resubmit the same objects selection and "
            "property_filter."
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
            'response_detail: "compact" omits before-state geometry '
            "deltas, property before-values, and dependent counts from "
            "change; report always carries the post-state verdict."
        ),
        "inputSchema": _CREATE_INPUT,
        "outputSchema": _MUTATED_OUTPUT,
    },
    {
        "name": "create_objects",
        "description": (
            "Create 1-32 objects atomically in one transaction; entries may "
            "mix supported types and are applied in request order inside one "
            "transaction and one recompute, with optional per-object expectations "
            "(expected_solids, expected_bounds, bounds_tolerance) keyed by "
            "the requested name and enforced against the actual sanitized "
            "object before commit. Every type and property is prevalidated "
            "before the transaction opens. A requested name that collides "
            "with an existing object or an earlier entry dedupes natively "
            "(Box001) and nameMapping reports the actual name. A failing "
            "expectation or recompute rolls back the whole batch; the result "
            "reports actual identities, the requested-to-actual nameMapping, "
            "per-object change summaries and applied names. "
            "Sibling cross-references inside one batch are not supported "
            "because link values need actual object names; use edit_objects "
            'afterwards. response_detail: "compact" omits before-state '
            "geometry deltas, property before-values, and dependent counts "
            "from each change; per-object reports always carry the "
            "post-state verdict."
        ),
        "inputSchema": _CREATE_OBJECTS_INPUT,
        "outputSchema": _CREATE_OBJECTS_OUTPUT,
    },
    {
        "name": "edit_object",
        "description": (
            "Assign properties to an existing object. Every property is "
            "prevalidated (existence, read-only, native type, canonical "
            "links) before the transaction opens, so a later invalid "
            "property leaves earlier ones unchanged. Preserves vector, "
            "placement, color, ViewObject and link conversions; "
            "Spreadsheet::Sheet cell contents use properties.cells with "
            "address or alias keys and persist through the native sheet API; "
            "a cellContentsPersisted flag is returned after recompute readback. "
            "FuzzyTolerance is honored only when the feature actually "
            "exposes it. Optional expected_bounds (six document-space mm "
            "coordinates in xmin, ymin, zmin, xmax, ymax, zmax order plus "
            "bounds_tolerance) gate the commit. Optional expected_generation "
            "refuses the edit when the document changed since inspection. "
            "Returns "
            "before/after property values, geometry deltas, the dependent "
            "counts before and after, and post-recompute validation."
            'response_detail: "compact" omits before-state geometry '
            "deltas, property before-values, and dependent counts from "
            "change; report always carries the post-state verdict."
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
            "bounds_tolerance) checked before commit. An optional top-level "
            "expected_generation refuses the whole batch when the document "
            "changed since inspection. Duplicate object "
            "names are rejected and every property is prevalidated before "
            "the transaction opens. A failing expectation or recompute "
            "rolls back the whole batch; the result reports actual "
            "identities, per-object change summaries and applied names."
            'response_detail: "compact" omits before-state geometry '
            "deltas, property before-values, and dependent counts from "
            "each change; per-object reports always carry the post-state "
            "verdict."
        ),
        "inputSchema": _EDIT_OBJECTS_INPUT,
        "outputSchema": _EDIT_OBJECTS_OUTPUT,
    },
    {
        "name": "delete_object",
        "description": (
            "Remove one object. Objects with dependents are refused "
            "explicitly instead of being silently cascaded, except PartDesign"
            " dependents whose only link is the target's BaseFeature: the"
            " native removal clears that link so the dependent falls back to"
            " the previous solid feature, and each one is reported in"
            "'rerouted' with its resulting BaseFeature (null after a cleared"
            " link). Otherwise the removal and recompute run inside the"
            " shared mutation transaction and abort on failure."
        ),
        "inputSchema": _DELETE_INPUT,
        "outputSchema": _DELETE_OUTPUT,
    },
]


def _describe(exc: BaseException) -> str:
    """Format an exception as ``Type: message`` for bounded error details."""
    return f"{type(exc).__name__}: {exc}"


def _label(obj: Any) -> str:
    """Return an object's Label, falling back to its Name."""
    return str(getattr(obj, "Label", getattr(obj, "Name", "")))


def _states(obj: Any) -> list[str]:
    """Return an object's state flags as a list of strings, or [] when unreadable."""
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
    """Return an object's shape, or None when unavailable or null."""
    try:
        shape = obj.Shape
    except Exception:
        return None
    if shape_is_null(shape):
        return None
    return shape


def _bounds(shape: Any) -> list[float] | None:
    """Return a shape's six bound coordinates, or None when unreadable."""
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
    """Return a finite float, or None when the value is not numeric or not finite."""
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
    """Require a finite JSON number (booleans excluded) and return it as a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(VALIDATION_FAILED, f"{what} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ToolError(VALIDATION_FAILED, f"{what} must be finite")
    return number


def _is_spreadsheet(obj: Any) -> bool:
    """Return True when the object is a Spreadsheet::Sheet."""
    return str(getattr(obj, "TypeId", "")) == _SPREADSHEET_TYPE


def _spreadsheet_cell_address(sheet: Any, requested: Any) -> str:
    """Resolve a cell name or alias to a validated uppercase cell address."""
    if not isinstance(requested, str) or not requested.strip():
        raise ToolError(VALIDATION_FAILED, "spreadsheet cell names must be non-empty strings")
    key = requested.strip()
    address: str | None = None
    alias_resolver = getattr(sheet, "getCellFromAlias", None)
    if callable(alias_resolver):
        try:
            resolved = alias_resolver(key)
        except Exception:
            resolved = None
        if isinstance(resolved, str) and resolved:
            address = resolved
    if address is None:
        if not _CELL_ADDRESS.fullmatch(key):
            raise ToolError(
                VALIDATION_FAILED,
                f"spreadsheet cell '{requested}' is not a single cell address or alias",
                {"cell": requested, "nextTool": "inspect_objects"},
            )
        address = key.upper()
    if not _CELL_ADDRESS.fullmatch(address):
        raise ToolError(
            VALIDATION_FAILED,
            f"spreadsheet alias '{requested}' resolved to an invalid cell address",
            {"cell": requested},
        )
    return address


def _spreadsheet_alias(sheet: Any, address: str) -> str | None:
    """Return a cell's alias, or None when absent or unreadable."""
    getter = getattr(sheet, "getAlias", None)
    if not callable(getter):
        return None
    try:
        alias = getter(address)
    except Exception:
        return None
    return str(alias) if isinstance(alias, str) and alias else None


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    """Clip text to the limit and report whether clipping occurred."""
    return value[:limit], len(value) > limit


def _spreadsheet_error(value: Any) -> str:
    """Bound an error string to the spreadsheet error limit."""
    return _truncate_text(str(value), _MAX_SPREADSHEET_ERROR)[0]


def _spreadsheet_content_equivalent(left: str, right: str) -> bool:
    """Compare cell contents semantically rather than byte-for-byte.

    Numeric contents with a recognized simple unit compare by parsed value.
    """
    if left == right:
        return True

    def normalized(value: str) -> tuple[str, Any, str]:
        """Normalize content into a (kind, canonical text, unit) comparison triple."""
        text = value.strip()
        if text.startswith("'"):
            return "text", text[1:].strip(), ""
        match = re.fullmatch(
            r"([+-]?(?:[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+)(?:[eE][+-]?[0-9]+)?)"
            r"\s*([A-Za-zµ%°][A-Za-z0-9_µ%°]*)?",
            text,
        )
        if match is None:
            return "text", text, ""
        unit = (match.group(2) or "").casefold()
        if unit and unit not in _SPREADSHEET_SIMPLE_UNITS:
            return "text", text, ""
        number = float(match.group(1).replace(",", "."))
        if not math.isfinite(number):
            return "text", text, ""
        return "number", f"{number:.15g}", unit

    return normalized(left) == normalized(right)


def _spreadsheet_contents(sheet: Any, address: str) -> str:
    """Read a cell's raw contents through the native getContents API.

    A sheet lacking the API is a domain error, never a silent empty read.
    """
    getter = getattr(sheet, "getContents", None)
    if not callable(getter):
        raise ToolError(
            VALIDATION_FAILED,
            "the installed FreeCAD Spreadsheet::Sheet has no getContents API",
            {"reason": "native_api_unavailable"},
        )
    try:
        content = getter(address)
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"reading spreadsheet cell '{address}' failed: {_spreadsheet_error(_describe(exc))}",
            {"cell": address},
        ) from exc
    if not isinstance(content, str):
        raise ToolError(
            VALIDATION_FAILED,
            f"spreadsheet cell '{address}' returned non-string contents",
            {"cell": address},
        )
    return content


def _spreadsheet_cell_snapshot(sheet: Any, requested: Any) -> dict[str, Any]:
    """Snapshot a cell's address, alias, contents, formula and evaluated value.

    A failed value read degrades to an unavailable marker instead of failing
    the whole inspection.
    """
    address = _spreadsheet_cell_address(sheet, requested)
    alias = _spreadsheet_alias(sheet, address)
    try:
        full_content = _spreadsheet_contents(sheet, address)
    except ToolError as exc:
        return {
            "address": address,
            "alias": alias,
            "content": "",
            "contentTruncated": False,
            "formula": None,
            "formulaTruncated": False,
            "value": _unavailable("spreadsheet-read-failed"),
            "valueTruncated": False,
            "error": _truncate_text(exc.message, _MAX_SPREADSHEET_ERROR)[0],
        }
    content, content_truncated = _truncate_text(full_content, _MAX_SPREADSHEET_CONTENT)
    full_formula = full_content if full_content.startswith("=") else None
    formula = None
    formula_truncated = False
    if full_formula is not None:
        formula, formula_truncated = _truncate_text(full_formula, _MAX_SPREADSHEET_CONTENT)
    value: Any = _unavailable("spreadsheet-value-unavailable")
    value_truncated = False
    error: str | None = None
    getter = getattr(sheet, "get", None)
    if not callable(getter):
        error = "the installed FreeCAD Spreadsheet::Sheet has no get API"
    else:
        try:
            raw_value = getter(address)
            value = _jsonify(raw_value)
            value_truncated = (
                isinstance(raw_value, (list, tuple)) and len(raw_value) > _PROPERTY_LIST_LIMIT
            )
        except Exception as exc:
            error = _spreadsheet_error(_describe(exc))
    if isinstance(value, str) and len(value) > _MAX_SPREADSHEET_CONTENT:
        value, value_truncated = _truncate_text(value, _MAX_SPREADSHEET_CONTENT)
    return {
        "address": address,
        "alias": alias,
        "content": content,
        "contentTruncated": content_truncated,
        "formula": formula,
        "formulaTruncated": formula_truncated,
        "value": value,
        "valueTruncated": value_truncated,
        "error": error,
    }


def _spreadsheet_info(sheet: Any) -> dict[str, Any]:
    """Build a sheet's bounded used-range cell inventory.

    Missing native inventory APIs report ``available: false`` instead of
    raising.
    """
    used_getter = getattr(sheet, "getUsedCells", None)
    range_getter = getattr(sheet, "getUsedRange", None)
    if not callable(used_getter) or not callable(range_getter):
        missing = [
            name
            for name, getter in (("getUsedCells", used_getter), ("getUsedRange", range_getter))
            if not callable(getter)
        ]
        return {
            "available": False,
            "error": f"Spreadsheet::Sheet is missing native API: {', '.join(missing)}",
            "usedRange": None,
            "cells": [],
            "cellCount": 0,
            "truncated": False,
        }
    try:
        raw_cells = used_getter() or ()
        try:
            raw_count = len(raw_cells)
            sample = raw_cells[: _MAX_SPREADSHEET_CELLS + 1]
        except TypeError:
            raw_count = None
            sample = []
            for value in raw_cells:
                sample.append(value)
                if len(sample) > _MAX_SPREADSHEET_CELLS:
                    break
        addresses: set[str] = set()
        for value in sample:
            if not isinstance(value, str) or not value:
                continue
            if len(addresses) >= _MAX_SPREADSHEET_CELLS:
                break
            addresses.add(_spreadsheet_cell_address(sheet, value))
        raw_range = range_getter()
    except ToolError:
        raise
    except Exception as exc:
        return {
            "available": False,
            "error": "reading spreadsheet cell inventory failed: "
            f"{_spreadsheet_error(_describe(exc))}",
            "usedRange": None,
            "cells": [],
            "cellCount": 0,
            "truncated": False,
        }
    used_range = None
    if isinstance(raw_range, (list, tuple)) and len(raw_range) == 2:
        start, end = (str(value) for value in raw_range)
        if start and end:
            used_range = {"from": start, "to": end}
    cell_count = raw_count if raw_count is not None else len(addresses)
    truncated = (
        raw_count > _MAX_SPREADSHEET_CELLS
        if raw_count is not None
        else len(sample) > _MAX_SPREADSHEET_CELLS
    )
    cells = [
        _spreadsheet_cell_snapshot(sheet, address)
        for address in sorted(addresses)[:_MAX_SPREADSHEET_CELLS]
    ]
    return {
        "available": True,
        "usedRange": used_range,
        "cells": cells,
        "cellCount": cell_count,
        "truncated": truncated,
    }


def _spreadsheet_write_plan(sheet: Any, cells: Any) -> list[tuple[str, str]]:
    """Validate a cells map into unique, size-bounded (address, content) pairs."""
    if not isinstance(cells, dict):
        raise ToolError(VALIDATION_FAILED, "spreadsheet properties.cells must be an object")
    if len(cells) > _MAX_SPREADSHEET_CELLS:
        raise ToolError(
            VALIDATION_FAILED,
            f"spreadsheet properties.cells accepts at most {_MAX_SPREADSHEET_CELLS} cells",
        )
    plan: list[tuple[str, str]] = []
    seen: set[str] = set()
    for requested, content in cells.items():
        address = _spreadsheet_cell_address(sheet, requested)
        if address in seen:
            raise ToolError(
                VALIDATION_FAILED,
                f"spreadsheet cells address '{address}' is listed more than once",
            )
        if not isinstance(content, str):
            raise ToolError(
                VALIDATION_FAILED,
                f"spreadsheet cell '{requested}' contents must be a string",
            )
        if len(content) > _MAX_SPREADSHEET_CONTENT:
            raise ToolError(
                VALIDATION_FAILED,
                f"spreadsheet cell '{requested}' contents exceed "
                f"{_MAX_SPREADSHEET_CONTENT} characters",
            )
        seen.add(address)
        plan.append((address, content))
    return plan


def _spreadsheet_write_receipt(sheet: Any, cells: Any) -> list[tuple[Any, str, str, str]]:
    """Capture each planned cell's pre-write contents for post-commit verification."""
    receipt: list[tuple[Any, str, str, str]] = []
    for address, content in _spreadsheet_write_plan(sheet, cells):
        try:
            before = _spreadsheet_contents(sheet, address)
        except ToolError as exc:
            if (
                isinstance(exc.details, dict)
                and exc.details.get("reason") == "native_api_unavailable"
            ):
                raise
            before = ""
        receipt.append((sheet, address, content, before))
    return receipt


def _set_spreadsheet_cell(sheet: Any, address: str, content: str) -> None:
    """Write one cell's contents through the native set API.

    A sheet lacking the API is a domain error, never a silent no-op.
    """
    setter = getattr(sheet, "set", None)
    if not callable(setter):
        raise ToolError(
            VALIDATION_FAILED,
            "the installed FreeCAD Spreadsheet::Sheet has no set API",
            {"reason": "native_api_unavailable"},
        )
    try:
        setter(address, content)
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"writing spreadsheet cell '{address}' failed: {_spreadsheet_error(_describe(exc))}",
            {"cell": address},
        ) from exc


def _validate_spreadsheet_writes(writes: list[tuple[Any, str, str, str]]) -> None:
    """Verify every planned write persisted after recompute, refusing reversion.

    Contents that normalize to the same number and simple unit count as
    retained.
    """
    for sheet, address, expected, before in writes:
        actual = _spreadsheet_contents(sheet, address)
        if not actual and expected:
            raise ToolError(
                VALIDATION_FAILED,
                f"spreadsheet cell '{address}' did not retain native contents",
                {
                    "cell": address,
                    "expected": _truncate_text(expected, _MAX_SPREADSHEET_CONTENT)[0],
                },
            )
        if (
            actual == before
            and expected != before
            and not _spreadsheet_content_equivalent(expected, before)
        ):
            raise ToolError(
                VALIDATION_FAILED,
                f"spreadsheet cell '{address}' reverted to its pre-write contents",
                {
                    "cell": address,
                    "expected": _truncate_text(expected, _MAX_SPREADSHEET_CONTENT)[0],
                    "actual": _truncate_text(actual, _MAX_SPREADSHEET_CONTENT)[0],
                },
            )


def _vector_value(value: Any, what: str) -> Any:
    """Convert a JSON mapping or three-number array into a FreeCAD.Vector."""
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
    """Convert an RGB or RGBA number array into an (r, g, b, a) tuple."""
    if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
        raise ToolError(VALIDATION_FAILED, f"{what} must be an RGB or RGBA number array")
    parts = [_number(component, f"{what}[{index}]") for index, component in enumerate(value)]
    if len(parts) == 3:
        parts.append(1.0)
    return (parts[0], parts[1], parts[2], parts[3])


def _rotation_value(value: Any, what: str) -> Any:
    """Convert an Axis/Angle JSON mapping into a FreeCAD.Rotation.

    A missing axis defaults to +Z and a missing angle to zero degrees.
    """
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
    """Convert a JSON mapping into a FreeCAD.Placement.

    Accepts the protocol position/axis/angle_deg form or native Base and
    Rotation parts.
    """
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


def _resolve_reference(ctx: Any, doc: Any, reference: dict, parameter: str = "reference"):
    """Resolve one signed subelement reference through the geometry tool module."""
    # geometry.py is a sibling tool module; import lazily so this module
    # loads (and its tests run) regardless of registration order.
    from .geometry import resolve_reference

    return resolve_reference(ctx, doc, reference, parameter)


_TARGET_VALIDATION_SCHEMA = {"$ref": "#/$defs/topologyTarget", "$defs": tq.QUERY_DEFS}


def _validate_target(value: Any, what: str) -> None:
    """Enforce the closed shared target union before any native access.

    The generic ``value4`` alternative also admits loose dictionaries, so
    every link-bearing value is checked against the closed queryTarget /
    topologyTarget definitions here; empty subelement sentinels and numeric
    label payloads cannot slip through the permissive union branch.
    """

    try:
        validate_schema(value, _TARGET_VALIDATION_SCHEMA, _TARGET_VALIDATION_SCHEMA, what)
    except ProtocolError as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} is not a valid shared link target: {exc}",
            {"parameter": what},
        ) from exc


class _PreparedQueries:
    """Operation-local prepared-resolution context for query link values.

    Queries are resolved once per public operation, before the mutation
    opens, and the context is passed explicitly to the property and feature
    adapters; it is never module-global or persistent state. It also owns
    the two receipts budgets: at most 64 receipts and at most 64 referenced
    subshapes across the complete operation, refused with
    ``selection_limit`` before the transaction.

    Each resolution also snapshots the selected subshapes' document-space
    geometry fingerprints, so a later re-verification can prove the
    selection (object, role, index set and per-index geometry) without
    holding native subshape objects that a recompute invalidates.
    """

    def __init__(self, ctx: Any, doc: Any) -> None:
        """Bind the operation context and document, snapshotting the start generation."""
        self.ctx = ctx
        self.doc = doc
        self.generation = int(ctx.document_generation(doc))
        self.resolutions: dict[tuple[str, str], tuple[Any, str, list[int]]] = {}
        self.fingerprints: dict[tuple[str, str], dict[int, dict] | None] = {}
        self.receipts: list[dict] = []
        self.referenced_subshapes = 0

    def resolve(self, path: str, value: Mapping, *, owner: str = "") -> tuple[Any, str, list[int]]:
        """Resolve one query target once, reserving its receipt budget.

        The cache key pairs the owning target with the parameter path: a
        batch operation shares one context across several objects, so a
        same-named property on another entry must never reuse this entry's
        selection.
        """

        key = (owner, path)
        if key in self.resolutions:
            return self.resolutions[key]
        _validate_target(value, path)
        from .geometry import resolve_query

        # The operation-start resolution enforces the caller's
        # expected_generation before any native extraction; later
        # re-verifications of the same selection are guarded by the
        # index-set and subshape-identity comparison instead.
        obj, role, indices = resolve_query(self.ctx, self.doc, value, path)
        self.resolutions[key] = (obj, role, indices)
        # Snapshot the selected subshapes' document-space fingerprints so a
        # later re-verification can prove the geometry, not just the index
        # set. Both failure modes (no placed shape, unreadable subshapes)
        # record None, which re-verification refuses.
        try:
            from .geometry import placed_shape, subshape_fingerprints

            self.fingerprints[key] = subshape_fingerprints(placed_shape(obj), role, indices)
        except Exception:
            self.fingerprints[key] = None
        if indices:
            from .geometry import make_reference

            references = [make_reference(self.ctx, self.doc, obj, role, index) for index in indices]
            self.receipts.append(
                {
                    "parameter": path,
                    "document": str(getattr(self.doc, "Name", "")),
                    "generation": self.generation,
                    "references": references,
                    "count": len(references),
                }
            )
            self.referenced_subshapes += len(indices)
        if len(self.receipts) > tq.MAX_QUERY_REFERENCES:
            raise ToolError(
                VALIDATION_FAILED,
                f"the operation uses more than {tq.MAX_QUERY_REFERENCES} query "
                "selections; refuse before any mutation",
                {"reason": "selection_limit", "parameter": path},
            )
        if self.referenced_subshapes > tq.MAX_REFERENCED_SUBSHAPES:
            raise ToolError(
                VALIDATION_FAILED,
                f"the operation selects more than {tq.MAX_REFERENCED_SUBSHAPES} "
                "subshapes across query receipts",
                {"reason": "selection_limit", "parameter": path},
            )
        return obj, role, indices

    def fingerprints_for(self, path: str, *, owner: str = "") -> dict[int, dict] | None:
        """The selection-time geometry fingerprints for one resolved query."""

        return self.fingerprints.get((owner, path))

    def reverify(self, path: str, value: Mapping, *, owner: str = "") -> tuple[Any, str, list[int]]:
        """Re-run one query selection and compare it with the prepared one.

        Returns the fresh resolution; raises ``selection_changed`` when the
        matched object, role or index set differs from the selection-time
        snapshot. The per-index geometry fingerprint is compared by the
        caller, which owns the selection-time fingerprint snapshot.
        """

        from .geometry import resolve_query

        # The generation guard was enforced once at operation start; the
        # state change this re-verification follows (a base recompute)
        # legitimately advances the generation, so the probe drops the
        # expectation instead of failing against the tool's own side effect.
        probe = {key: item for key, item in value.items() if key != "expected_generation"}
        obj, role, indices = resolve_query(self.ctx, self.doc, probe, path)
        expected = self.resolutions.get((owner, path))
        if expected is not None:
            expected_obj, expected_role, expected_indices = expected
            if (
                getattr(obj, "Name", "") != getattr(expected_obj, "Name", "")
                or role != expected_role
                or list(indices) != list(expected_indices)
            ):
                raise ToolError(
                    VALIDATION_FAILED,
                    f"{path} selected a different set after a state change; "
                    "the operation was refused",
                    {"reason": "selection_changed", "parameter": path},
                )
        return obj, role, indices

    def scan_property(self, prop: str, value: Any, *, owner: str = "") -> None:
        """Pre-resolve top-level and array-entry queries in one property."""

        if isinstance(value, dict):
            if self._looks_like_query_target(value):
                self.resolve(prop, value, owner=owner)
        elif isinstance(value, list):
            for index, entry in enumerate(value):
                if isinstance(entry, dict) and self._looks_like_query_target(entry):
                    self.resolve(f"{prop}[{index}]", entry, owner=owner)

    @staticmethod
    def _looks_like_query_target(value: Mapping) -> bool:
        """True for a shared query target, never for an unrelated map.

        A shared target always names its object; requiring that string key
        keeps maps such as a spreadsheet ``cells`` alias table (whose keys
        are cell addresses or aliases) out of the scan.
        """

        return "query" in value and isinstance(value.get("object"), str)


def _resolve_one(
    ctx: Any,
    doc: Any,
    value: Any,
    what: str,
    *,
    allow_sub: bool,
    prepared: _PreparedQueries | None = None,
    owner: str = "",
) -> Any:
    """Resolve one shared link target; returns the object or (object, subs).

    Whole-object Link positions reject selected subshapes and queries with
    ``subshape_not_allowed`` instead of stripping them down. A LinkSub
    accepts exactly one query result; zero matches and ambiguity are named
    refusals with bounded candidate evidence.
    """

    if not isinstance(value, dict) or not isinstance(value.get("object"), str):
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} must be a shared link target ({'{object}'} with an "
            "optional signed subelement or declarative query)",
        )
    if not value["object"].strip():
        raise ToolError(VALIDATION_FAILED, f"{what} must name an object")
    has_query = "query" in value
    has_subelement = value.get("subelement") is not None
    if not allow_sub and (has_query or has_subelement):
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} takes a plain object link and accepts no subshape",
            {"parameter": what, "reason": "subshape_not_allowed"},
        )
    if not allow_sub:
        _validate_target(value, what)
        resolved = ctx.require_object(doc, value["object"])
        return resolved
    if has_query:
        if prepared is not None:
            obj, role, indices = prepared.resolve(what, value, owner=owner)
        else:
            _validate_target(value, what)
            from .geometry import resolve_query

            obj, role, indices = resolve_query(ctx, doc, value, what)
        if not indices or len(indices) > 1:
            from .geometry import cardinality_error

            raise cardinality_error(
                "selection_ambiguous" if len(indices) > 1 else "selection_empty",
                (
                    f"{what} matched {len(indices)} {role}s of {obj.Name}; a "
                    "single-value link needs exactly one match"
                    if indices
                    else f"{what} matched no {role} of {obj.Name}"
                ),
                what,
                ctx,
                doc,
                obj,
                role,
                indices,
            )
        native = ("Face" if role == "face" else "Edge") + str(indices[0])
        return (obj, [native])
    _validate_target(value, what)
    resolved, native = _resolve_reference(ctx, doc, value, what)
    return (resolved, [native] if native else [""])


def _resolve_link_sub_list(
    ctx: Any,
    doc: Any,
    entries: list,
    prop: str,
    *,
    prepared: _PreparedQueries | None = None,
    owner: str = "",
) -> list:
    """Expand LinkSubList entries in order into native (object, subs) pairs.

    Query entries expand to their full matched set, so a set consumer
    accepts a multi-match query within the budget. Every entry is validated
    and resolved before the first assignment and the expanded pair count is
    capped at 64; overflow refuses before any mutation.
    """

    if len(entries) > tq.MAX_QUERY_REFERENCES:
        raise ToolError(
            VALIDATION_FAILED,
            f"{prop} accepts at most {tq.MAX_QUERY_REFERENCES} entries",
            {"parameter": prop},
        )
    pairs: list = []
    for index, entry in enumerate(entries):
        what = f"{prop}[{index}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("object"), str):
            raise ToolError(
                VALIDATION_FAILED,
                f"{what} must be a shared link target",
            )
        if not entry["object"].strip():
            raise ToolError(VALIDATION_FAILED, f"{what} must name an object")
        if "query" in entry:
            if prepared is not None:
                obj, role, indices = prepared.resolve(what, entry, owner=owner)
            else:
                _validate_target(entry, what)
                from .geometry import resolve_query

                obj, role, indices = resolve_query(ctx, doc, entry, what)
            if not indices:
                from .geometry import cardinality_error

                raise cardinality_error(
                    "selection_empty",
                    f"{what} matched no {role} of {obj.Name}",
                    what,
                    ctx,
                    doc,
                    obj,
                    role,
                    indices,
                )
            for sub_index in indices:
                pairs.append(
                    (
                        obj,
                        [("Face" if role == "face" else "Edge") + str(sub_index)],
                    )
                )
        else:
            pairs.append(
                _resolve_one(ctx, doc, entry, what, allow_sub=True, prepared=prepared, owner=owner)
            )
        if len(pairs) > tq.MAX_QUERY_REFERENCES:
            raise ToolError(
                VALIDATION_FAILED,
                f"{prop} expands to more than {tq.MAX_QUERY_REFERENCES} pairs",
                {"parameter": prop, "reason": "selection_limit"},
            )
    return pairs


def _convert_value(
    ctx: Any,
    doc: Any,
    obj: Any,
    prop: str,
    value: Any,
    prepared: _PreparedQueries | None = None,
    owner: str = "",
) -> Any:
    """Convert one JSON value to the property's native FreeCAD value."""

    ptype = str(obj.getTypeIdOfProperty(prop))
    if ptype in _PLACEMENT_TYPES:
        return _placement_value(value, prop)
    if ptype in _VECTOR_TYPES:
        return _vector_value(value, prop)
    if ptype in _LINK_TYPES:
        return _resolve_one(ctx, doc, value, prop, allow_sub=False, prepared=prepared, owner=owner)
    if ptype in _LINK_SUB_TYPES:
        return _resolve_one(ctx, doc, value, prop, allow_sub=True, prepared=prepared, owner=owner)
    if ptype in _LINK_LIST_TYPES:
        return [
            _resolve_one(
                ctx,
                doc,
                entry,
                f"{prop}[{index}]",
                allow_sub=False,
                prepared=prepared,
                owner=owner,
            )
            for index, entry in enumerate(_as_array(value, prop))
        ]
    if ptype in _LINK_SUB_LIST_TYPES:
        return _resolve_link_sub_list(
            ctx, doc, _as_array(value, prop), prop, prepared=prepared, owner=owner
        )
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
    if isinstance(value, dict):
        # No native property accepts a JSON object. Passing it through made
        # FreeCAD raise a bare TypeError ("type must be 'Shape', not dict")
        # only after the object existed, with no pointer at the property.
        raise ToolError(
            VALIDATION_FAILED,
            f"property '{prop}' of type '{ptype}' does not accept a JSON "
            "object; mappable shapes are Placement, Vector, Link, LinkSub "
            "and Color values",
            {"property": prop, "propertyType": ptype},
        )
    # Unmapped property types pass through; FreeCAD rejects mismatches and
    # the mutation gate rolls the assignment back.
    return value


def _as_array(value: Any, what: str) -> list:
    """Require a JSON array and return it unchanged."""
    if not isinstance(value, list):
        raise ToolError(VALIDATION_FAILED, f"{what} must be an array")
    return value


def _string(value: Any, what: str) -> str:
    """Require a JSON string and return it unchanged."""
    if not isinstance(value, str):
        raise ToolError(VALIDATION_FAILED, f"{what} must be a string")
    return value


def _integer(value: Any, what: str) -> int:
    """Require a JSON integer (booleans excluded) and return it unchanged."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(VALIDATION_FAILED, f"{what} must be an integer")
    return value


def _enumerations(obj: Any, prop: str) -> list[str] | None:
    """Return a property's enumeration choices, or None when unavailable."""
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
                    "nextTool": "inspect_objects",
                },
            )
    return None, {}


def _call_factory(factory: Any, doc: Any, requested_name: str, kwargs: dict[str, Any]) -> Any:
    """Invoke an FEM factory, wrapping any native failure as a domain error."""
    try:
        return factory(doc, name=requested_name, **kwargs)
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"object creation failed: {_describe(exc)}",
        ) from exc


def _property_exists(obj: Any, prop: str) -> bool:
    """Return True when the holder lists the property in PropertiesList."""
    return prop in list(getattr(obj, "PropertiesList", ()) or ())


def _is_read_only(holder: Any, prop: str) -> bool:
    """Return True when the property status carries the native ReadOnly mark.

    Holders without status introspection are treated as writable.
    """
    getter = getattr(holder, "getPropertyStatus", None)
    if not callable(getter):
        return False
    try:
        status = getter(prop)
    except Exception:
        return False
    return "ReadOnly" in list(status or ())


def _viewobject(obj: Any) -> Any:
    """Return an object's ViewObject, refusing objects without one."""
    view = getattr(obj, "ViewObject", None)
    if view is None:
        raise ToolError(
            VALIDATION_FAILED,
            f"object '{getattr(obj, 'Name', '<unknown>')}' has no ViewObject",
        )
    return view


def _view_value(prop: str, value: Any) -> Any:
    """Convert a ViewObject property value, admitting only scalars and colors."""
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
    """Refuse unknown or read-only ViewObject properties before assignment."""
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
    """Refuse read-only, unknown, or cell-addressed document property writes."""
    name = str(getattr(obj, "Name", "<unknown>"))
    if _is_spreadsheet(obj):
        try:
            _spreadsheet_cell_address(obj, prop)
        except ToolError:
            pass
        else:
            raise ToolError(
                VALIDATION_FAILED,
                f"Spreadsheet cell '{prop}' must be written through properties.cells",
                {"property": prop, "nextTool": "inspect_objects"},
            )
    if not _property_exists(obj, prop):
        raise ToolError(
            VALIDATION_FAILED,
            f"object '{name}' has no property '{prop}'",
            {
                "object": name,
                "property": prop,
                "suggestions": _suggestions(prop, _all_property_names(obj)),
                "nextTool": "inspect_objects",
            },
        )
    if _is_read_only(obj, prop):
        raise ToolError(VALIDATION_FAILED, f"property '{prop}' of object '{name}' is read-only")


def _assign_converted(
    ctx: Any,
    doc: Any,
    obj: Any,
    prop: str,
    value: Any,
    queries: _PreparedQueries | None = None,
    owner: str = "",
) -> None:
    """Convert and assign one document property (live path for create)."""

    _check_document_property(obj, prop)
    setattr(
        obj,
        prop,
        _convert_value(ctx, doc, obj, prop, value, prepared=queries, owner=owner),
    )


def _apply_properties(
    ctx: Any,
    doc: Any,
    obj: Any,
    properties: dict,
    queries: _PreparedQueries | None = None,
    owner: str = "",
) -> None:
    """Assign a properties map onto an existing object (create path)."""

    for prop, value in properties.items():
        if prop == "cells" and _is_spreadsheet(obj):
            for address, content in _spreadsheet_write_plan(obj, value):
                _set_spreadsheet_cell(obj, address, content)
        elif prop == "ViewObject":
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
            _assign_converted(ctx, doc, obj, prop, value, queries=queries, owner=owner)


def _prepare_properties(
    ctx: Any,
    doc: Any,
    obj: Any,
    properties: dict,
    queries: _PreparedQueries | None = None,
    owner: str = "",
) -> list[tuple[str, str, Any]]:
    """Prevalidate and convert the whole edit map before any assignment.

    Returns ``("doc"|"view", prop, converted)`` tuples; raises before the
    transaction opens so a later invalid property leaves earlier ones
    unchanged.
    """

    prepared: list[tuple[str, str, Any]] = []
    for prop, value in properties.items():
        if prop == "cells" and _is_spreadsheet(obj):
            prepared.extend(
                ("spreadsheet", address, content)
                for address, content in _spreadsheet_write_plan(obj, value)
            )
        elif prop == "ViewObject":
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
            prepared.append(
                (
                    "doc",
                    prop,
                    _convert_value(ctx, doc, obj, prop, value, prepared=queries, owner=owner),
                )
            )
    return prepared


def _apply_prepared(obj: Any, prepared: list[tuple[str, str, Any]]) -> None:
    """Assign prevalidated rows; spreadsheet cells natively, all else via setattr."""
    for target, prop, value in prepared:
        if target == "spreadsheet":
            _set_spreadsheet_cell(obj, prop, value)
        else:
            holder = obj.ViewObject if target == "view" else obj
            setattr(holder, prop, value)


# ---------------------------------------------------------------------------
# Inspection.
# ---------------------------------------------------------------------------


def _placement_row_value(placement: Any) -> dict | None:
    """Serialize a placement to the position/axis/angle_deg row, or None on failure."""
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
    """Return an object's local placement row, or None when unavailable."""
    try:
        placement = obj.Placement
    except Exception:
        return None
    return _placement_row_value(placement)


def _solid_count(shape: Any) -> int | None:
    """Return a shape's solid count, or None when the shape is unavailable."""
    if shape is None:
        return None
    try:
        return len(shape.Solids)
    except Exception:
        return None


def _shape_valid(shape: Any) -> bool | None:
    """Return a shape's native validity, or None when the shape is unavailable."""
    if shape is None:
        return None
    try:
        return bool(shape.isValid())
    except Exception:
        return None


def _tip_name(obj: Any) -> str | None:
    """Return a PartDesign Body's tip name, or None for other objects."""
    derived = getattr(obj, "isDerivedFrom", None)
    if not callable(derived) or not derived("PartDesign::Body"):
        return None
    tip = getattr(obj, "Tip", None)
    name = str(getattr(tip, "Name", "")) if tip is not None else ""
    return name or None


def _body_history(obj: Any) -> tuple[list[dict], bool, list[dict], int]:
    """Return native Body members and its six local origin references."""

    derived = getattr(obj, "isDerivedFrom", None)
    is_body = str(getattr(obj, "TypeId", "")) == "PartDesign::Body"
    if callable(derived):
        try:
            is_body = is_body or bool(derived("PartDesign::Body"))
        except Exception:
            pass
    if not is_body:
        return [], False, [], 0

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
    return features[:256], truncated, origins, len(features)


def _link_names(obj: Any) -> tuple[list[str], int]:
    """Return sorted outbound link names plus the full count before truncation."""
    try:
        out_list = list(obj.OutList)
    except Exception:
        return [], 0
    names: list[str] = []
    for linked in out_list:
        name = str(getattr(linked, "Name", ""))
        if name and name not in names:
            names.append(name)
    return sorted(names[:_MAX_LINKS]), len(names)


def _unavailable(kind: str) -> dict:
    """Build the ``{"unavailable": kind}`` marker used for property values."""
    return {"unavailable": kind}


_LINK_LABEL = re.compile(r"(Face|Edge)([1-9][0-9]*)")


def _link_value(ctx: Any, doc: Any, raw: Any) -> Any | None:
    """Serialize a native link value with a signed subshape readback.

    Same-document whole-object links serialize as ``{object}``; supported
    subshape links as ``{object, subelement: <signed token>}``. Cross-
    document, unsupported, or stale native labels refuse through the closed
    ``unavailableLink`` branch instead of publishing raw FaceN values or
    fabricated references.
    """

    from .geometry import make_reference

    if hasattr(raw, "Name") and hasattr(raw, "TypeId"):
        if getattr(raw, "Document", None) is not doc:
            return {
                "unavailableLink": {
                    "object": str(getattr(raw, "Name", "")),
                    "reason": "cross_document",
                }
            }
        return {"object": str(raw.Name)}
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and hasattr(raw[0], "Name"):
        link, subs = raw[0], raw[1]
        if getattr(link, "Document", None) is not doc:
            return {
                "unavailableLink": {
                    "object": str(getattr(link, "Name", "")),
                    "reason": "cross_document",
                }
            }
        sub = subs
        if isinstance(subs, (list, tuple)):
            sub = subs[0] if subs else ""
        if sub in (None, ""):
            return {"object": str(link.Name)}
        match = _LINK_LABEL.fullmatch(str(sub))
        if match is None:
            return {
                "unavailableLink": {
                    "object": str(link.Name),
                    "reason": "unsupported_subelement",
                    "nativeSubelement": str(sub),
                }
            }
        role = "face" if match.group(1) == "Face" else "edge"
        index = int(match.group(2))
        try:
            shape = link.Shape
            count = len(shape.Faces) if role == "face" else len(shape.Edges)
        except Exception:
            return {
                "unavailableLink": {
                    "object": str(link.Name),
                    "reason": "unresolvable",
                    "nativeSubelement": str(sub),
                }
            }
        if index > count:
            return {
                "unavailableLink": {
                    "object": str(link.Name),
                    "reason": "stale_subelement",
                    "nativeSubelement": str(sub),
                }
            }
        return {
            "object": str(link.Name),
            "subelement": make_reference(ctx, doc, link, role, index)["subelement"],
        }
    return None


def _jsonify(value: Any, budget: list[int] | None = None) -> Any:
    """Convert one property value; document objects never stringify.

    ``budget`` carries the remaining bounded node count for one top-level
    conversion. A shared sub-mapping DAG whose repeated emission would
    expand exponentially as JSON exhausts the budget and degrades to the
    unavailable marker instead of producing an unbounded payload.
    """

    if budget is None:
        budget = [_JSONIFY_LEAF_BUDGET]
    budget[0] -= 1
    if budget[0] < 0:
        return _unavailable(type(value).__name__)
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
            # Whole-object link identity; a signed subshape readback needs
            # document context (see _link_value) and is never produced here.
            return {"object": str(value.Name)}
    except Exception:
        return _unavailable(type(value).__name__)
    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            first, second = value
            if hasattr(first, "Name") and hasattr(first, "TypeId"):
                # A (link, subelement) pair. The native label is descriptive
                # only and never an accepted selector: without document
                # context this serializer reports the link as unavailable
                # instead of publishing a reusable-looking FaceN value.
                sub = second
                if isinstance(sub, (list, tuple)):
                    sub = sub[0] if sub else ""
                if isinstance(sub, str):
                    return {
                        "unavailableLink": {
                            "object": str(first.Name),
                            "reason": "link_readback_unavailable",
                            "nativeSubelement": sub,
                        }
                    }
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
        budget[0] -= len(converted)
        if budget[0] < 0:
            return _unavailable(type(value).__name__)
        return converted
    if isinstance(value, Mapping):
        if len(value) > _MAPPING_LIMIT:
            return _unavailable(type(value).__name__)
        converted_mapping: dict[str, Any] = {}
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    return _unavailable(type(value).__name__)
                converted_item = _jsonify(item, budget)
                if (
                    isinstance(converted_item, str)
                    and len(converted_item) > _MAX_SPREADSHEET_CONTENT
                ):
                    return _unavailable(type(value).__name__)
                if isinstance(converted_item, list) and any(
                    isinstance(entry, str) and len(entry) > _MAX_SPREADSHEET_CONTENT
                    for entry in converted_item
                ):
                    return _unavailable(type(value).__name__)
                if isinstance(converted_item, Mapping) and "unavailable" in converted_item:
                    return _unavailable(type(value).__name__)
                converted_mapping[key] = converted_item
        except Exception:
            return _unavailable(type(value).__name__)
        return converted_mapping
    try:
        candidate = dict(value)
    except Exception:
        return _unavailable(type(value).__name__)
    if len(candidate) > _MAPPING_LIMIT:
        return _unavailable(type(value).__name__)
    return _jsonify(candidate, budget)


def _read_value(obj: Any, prop: str) -> Any:
    """Read one property with document-first lookup and ViewObject fallback."""
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
    metadata = {
        "type": property_type,
        "readOnly": read_only,
        "enumeration": enumeration,
        "enumerationCount": count,
        "enumerationTruncated": truncated,
        "expression": expression,
    }
    if isinstance(expression, str) and len(expression) > _MAX_SPREADSHEET_CONTENT:
        metadata["expression"], _ = _truncate_text(expression, _MAX_SPREADSHEET_CONTENT)
        metadata["expressionTruncated"] = True
    if _is_spreadsheet(holder):
        try:
            address = _spreadsheet_cell_address(holder, prop)
            content = _spreadsheet_contents(holder, address)
        except ToolError:
            pass
        else:
            formula = content if content.startswith("=") else ""
            bounded_formula, formula_truncated = _truncate_text(formula, _MAX_SPREADSHEET_CONTENT)
            metadata["formula"] = bounded_formula or None
            metadata["formulaTruncated"] = formula_truncated
    return metadata


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


def _property_page(
    obj: Any, names: list[str], *, ctx: Any = None, doc: Any = None
) -> tuple[dict, dict, list[str]]:
    """Values, metadata and over-limit list names for one property page.

    When inspection context is available, native link values serialize with
    signed subelement references instead of the context-free fallback.
    """

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
        value = _jsonify(raw)
        if ctx is not None and doc is not None:
            link_value = _link_value(ctx, doc, raw)
            if link_value is not None:
                value = link_value
        if _is_spreadsheet(holder) and isinstance(value, str):
            value, value_truncated = _truncate_text(value, _MAX_SPREADSHEET_CONTENT)
            if value_truncated:
                truncated.append(name)
        properties[name] = value
        metadata[name] = _property_metadata(holder, plain)
        if isinstance(raw, (list, tuple)) and len(raw) > _PROPERTY_LIST_LIMIT:
            truncated.append(name)
    return properties, metadata, truncated


def _row(
    obj: Any,
    detail: str,
    props: list[str],
    *,
    ctx: Any = None,
    doc: Any = None,
    property_offset: int = 0,
    property_limit: int = _MAX_PROPERTY_PAGE,
) -> dict:
    """Build one inspect_objects row with identity, geometry, links and detail pages."""
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
        properties, property_metadata, truncated = _property_page(obj, page, ctx=ctx, doc=doc)
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
    links, link_count = _link_names(obj)
    row["links"] = links
    row["linkCount"] = link_count
    row["linksTruncated"] = link_count > _MAX_LINKS
    if detail == "full":
        features, features_truncated, origins, feature_count = _body_history(obj)
        row["bodyTip"] = _tip_name(obj)
        row["features"] = features
        row["featuresTruncated"] = features_truncated
        row["featureCount"] = feature_count
        row["origins"] = origins
        row["properties"] = properties
        row["propertyMetadata"] = property_metadata
        row["propertyCount"] = property_count
        row["nextPropertyOffset"] = next_property_offset
        row["truncatedProperties"] = truncated
        if _is_spreadsheet(obj):
            row["spreadsheet"] = _spreadsheet_info(obj)
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
    """Build the signed page-state payload bound to document identity and generation."""
    return {
        "kind": "objects-page",
        "identity": str(ctx.document_identity(doc)),
        "generation": int(ctx.document_generation(doc)),
        "detail": detail,
        "limit": limit,
        "filterHash": fingerprint(sorted(str(prop) for prop in props)),
        "propertyOffset": property_offset,
        "propertyLimit": property_limit,
        "selectionHash": fingerprint(sorted(selection)) if selection else None,
        "last": last,
    }


def _stale_cursor() -> ToolError:
    """Build the documented restart-pagination refusal."""
    return ToolError(
        VALIDATION_FAILED,
        "pagination cursor is stale; restart pagination from the beginning",
        {"reason": "stale_cursor", "nextTool": "inspect_objects"},
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
    """Sign the page-state payload and return the opaque cursor string."""
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
    """Verify a cursor against the request and return its continuation key.

    A bad signature is malformed; any mismatch with the request is stale.
    """
    try:
        payload = ctx.signer.verify(DOMAIN_CURSOR, cursor)
    except ProtocolError as exc:
        # A tampered or truncated cursor is a client-side pagination
        # mistake, not an infrastructure failure: answer the documented
        # restart-pagination verdict instead of leaking a dispatch error.
        raise ToolError(
            VALIDATION_FAILED,
            "pagination cursor signature rejected; restart from the first page",
            {"reason": "malformed_cursor"},
        ) from exc
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
        "filterHash",
        "propertyOffset",
        "propertyLimit",
        "selectionHash",
    ):
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
    """List a document's objects, paging rows behind signed continuation cursors."""
    doc = ctx.require_document(args["document"])
    detail = str(args.get("detail") or "compact")
    if detail not in ("compact", "full"):
        raise ToolError(VALIDATION_FAILED, "detail must be 'compact' or 'full'")
    limit = args.get("limit")
    limit = 32 if limit is None else int(limit)
    limit = max(1, min(_MAX_PROPERTY_PAGE, limit))
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
        if not isinstance(requested, list) or len(requested) < 1:
            raise ToolError(
                VALIDATION_FAILED,
                "objects must be an array of one or more object names",
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
            ctx=ctx,
            doc=doc,
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


def _snapshot_requested(
    obj: Any, properties: dict, *, ctx: Any = None, doc: Any = None
) -> list[tuple[str, Any]]:
    """Read the requested document/ViewObject properties for a delta row.

    Values pass through ``_jsonify`` so the rows use the same bounded
    inspection encoding as ``inspect_objects``; with inspection context,
    native link values serialize through the signed ``_link_value`` path.
    """

    rows: list[tuple[str, Any]] = []
    for key, value in properties.items():
        if key == "cells" and _is_spreadsheet(obj):
            for requested in value:
                snapshot = _spreadsheet_cell_snapshot(obj, requested)
                rows.append((f"cells.{snapshot['address']}", snapshot["content"]))
        elif key == "ViewObject" and isinstance(value, dict):
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
            holder = _resolve_property_holder(obj, key)
            if holder is None:
                rows.append((key, _unavailable("no-such-property")))
                continue
            read_holder, plain = holder
            try:
                raw = getattr(read_holder, plain, None)
            except Exception:
                raw = None
            row_value = _jsonify(raw)
            if ctx is not None and doc is not None:
                link_value = _link_value(ctx, doc, raw)
                if link_value is not None:
                    row_value = link_value
            rows.append((key, row_value))
    return rows


def _change_summary(
    properties: list[dict],
    *,
    detail: str = "compact",
    solid_before: int | None,
    solid_after: int | None,
    volume_before: float | None,
    volume_after: float | None,
    bounds_before: list[float] | None,
    bounds_after: list[float] | None,
    dependents_before: int,
    dependents: int,
    cell_contents_persisted: bool = False,
) -> dict:
    """Build the compact factual ``change`` summary for one target."""

    if detail == "compact":
        summary = {
            "properties": [{"name": row["name"], "after": row["after"]} for row in properties]
        }
    else:
        summary = {
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
    if cell_contents_persisted:
        summary["cellContentsPersisted"] = True
    return summary


def _check_workload(ctx: Any, targets: list[Any]) -> None:
    """Apply the shared bounded feature checks to generic object edits.

    Delegated lazily so this module keeps importing no FreeCAD-dependent
    sibling at load time.
    """

    from .feature_contracts import check_workload

    check_workload(ctx, targets)


def create_object(ctx: Any, args: dict) -> dict:
    """Create one object through the mutation gate with prevalidated properties."""
    doc = ctx.require_document(args["document"])
    obj_type = str(args["type"])
    requested_name = str(args["name"])
    properties = dict(args.get("properties") or {})
    detail = str(args.get("response_detail") or "compact")
    expected_solids = args.get("expected_solids")
    expected_bounds = args.get("expected_bounds")
    bounds_tolerance = args.get("bounds_tolerance")
    if bounds_tolerance is None:
        bounds_tolerance = _DEFAULT_BOUNDS_TOLERANCE

    factory, factory_kwargs = _plan_create(ctx, doc, obj_type, properties)

    # Native property metadata exists only after addObject, so every
    # syntactically query-bearing property resolves and reserves its
    # receipt/reference budget before the transaction; inside the mutation
    # the conversion reuses the prepared resolution.
    queries = _PreparedQueries(ctx, doc)
    for prop, value in properties.items():
        queries.scan_property(prop, value, owner=requested_name)

    created: list[Any] = []
    spreadsheet_writes: list[tuple[Any, str, str, str]] = []
    outcome: dict = {}

    def validate_spreadsheet_writes() -> None:
        """Gate hook verifying the planned cell writes survived the recompute."""
        _validate_spreadsheet_writes(spreadsheet_writes)

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
        validate_after_recompute=validate_spreadsheet_writes,
    ) as applied:
        if factory is not None:
            created.append(_call_factory(factory, doc, requested_name, factory_kwargs))
        else:
            created.append(doc.addObject(obj_type, requested_name))
        if _is_spreadsheet(created[0]) and "cells" in properties:
            spreadsheet_writes.extend(_spreadsheet_write_receipt(created[0], properties["cells"]))
        if properties:
            _apply_properties(
                ctx, doc, created[0], properties, queries=queries, owner=requested_name
            )

    obj = created[0]
    # The gate already reported this new object after its recompute; reusing
    # that report keeps the summary factual without a second shape probe.
    report = outcome["reports"][str(obj.Name)]
    after_rows = _snapshot_requested(obj, properties, ctx=ctx, doc=doc)
    result = {
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
            detail=detail,
            solid_before=None,
            solid_after=report["solid_count"],
            volume_before=None,
            volume_after=report["volume"],
            bounds_before=None,
            bounds_after=report["bounds"],
            # A created object had no pre-mutation dependent closure.
            dependents_before=0,
            dependents=outcome["dependentCountAfter"],
            cell_contents_persisted=bool(spreadsheet_writes),
        ),
    }
    if queries.receipts:
        result["resolvedSelections"] = queries.receipts
    return result


def edit_object(ctx: Any, args: dict) -> dict:
    """Edit one object's properties inside the shared mutation transaction gate."""
    doc = ctx.require_document(args["document"])
    obj = ctx.require_object(doc, str(args["object"]))
    require_expected_generation(
        ctx,
        doc,
        args,
        message="document changed since inspection; re-run inspect_objects",
        next_tool="inspect_objects",
    )
    properties = args["properties"]
    if not isinstance(properties, dict) or not properties:
        raise ToolError(VALIDATION_FAILED, "properties must be a non-empty object")
    expected_solids = args.get("expected_solids")
    expected_bounds = args.get("expected_bounds")
    bounds_tolerance = args.get("bounds_tolerance")
    if bounds_tolerance is None:
        bounds_tolerance = _DEFAULT_BOUNDS_TOLERANCE
    detail = str(args.get("response_detail") or "compact")
    queries = _PreparedQueries(ctx, doc)
    prepared = _prepare_properties(ctx, doc, obj, properties, queries=queries, owner=obj.Name)
    spreadsheet_writes = (
        _spreadsheet_write_receipt(obj, properties["cells"])
        if _is_spreadsheet(obj) and "cells" in properties
        else []
    )

    before_rows = _snapshot_requested(obj, properties, ctx=ctx, doc=doc)
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
        validate_after_recompute=lambda: _validate_spreadsheet_writes(spreadsheet_writes),
    ) as applied:
        _apply_prepared(obj, prepared)

    after_rows = _snapshot_requested(obj, properties, ctx=ctx, doc=doc)
    # The gate's post-recompute report for this target replaces a second
    # probe of the same shape.
    report = outcome["reports"][str(obj.Name)]
    result = {
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
            detail=detail,
            solid_before=before_report["solid_count"],
            solid_after=report["solid_count"],
            volume_before=before_report["volume"],
            volume_after=report["volume"],
            bounds_before=bounds_before,
            bounds_after=report["bounds"],
            dependents_before=dependents,
            dependents=outcome["dependentCountAfter"],
            cell_contents_persisted=bool(spreadsheet_writes),
        ),
    }
    if queries.receipts:
        result["resolvedSelections"] = queries.receipts
    return result


#: Body containers hold their features in ``Group``; that membership is not
#: a data dependency, and the native removal updates both ``Group`` and
#: ``Tip`` (verified on FreeCAD 1.1.3).
_BODY_CONTAINER_TYPES = ("PartDesign::Body", "Part::BodyBase")


def _groups_the_target(dependent: Any, target: Any) -> bool:
    """True when ``dependent`` is a Body that only groups ``target``.

    A PartDesign feature sits in its Body's ``Group``, so the Body appears in
    the feature's ``InList``; treating that as a blocking dependent made every
    feature under a Body undeletable. A real data dependency (another feature
    using this one) still refuses the deletion.
    """

    if str(getattr(dependent, "TypeId", "")) not in _BODY_CONTAINER_TYPES:
        return False
    try:
        members = list(getattr(dependent, "Group", None) or ())
    except Exception:
        return False
    return any(member is target for member in members)


def _reroutes_via_base_feature(dependent: Any, target: Any) -> bool:
    """True when the native removal can reroute ``dependent`` off ``target``.

    Native ``Body::removeObject`` clears the following feature's
    ``BaseFeature`` link so that feature falls back to the previous solid
    feature in the Body's history, and it repairs ``Tip`` (verified on
    FreeCAD 1.1.3: the dependent's link reads back null and the Body still
    validates as before). The mutation gate revalidates the recomputed chain
    afterwards, so any reroute the native removal cannot complete — for
    example one that leaves the Body with a different solid count — rolls the
    deletion back. Every other dependent still blocks.
    """

    probe = getattr(dependent, "isDerivedFrom", None)
    if not callable(probe):
        return False
    try:
        if not probe("PartDesign::Feature"):
            return False
        return getattr(dependent, "BaseFeature", None) is target
    except Exception:
        return False


def delete_object(ctx: Any, args: dict) -> dict:
    """Delete one object, refusing blocking dependents and reporting BaseFeature reroutes."""
    doc = ctx.require_document(args["document"])
    obj = ctx.require_object(doc, str(args["object"]))
    require_expected_generation(
        ctx,
        doc,
        args,
        message="document changed since inspection; re-run inspect_objects",
        next_tool="inspect_objects",
    )
    name = str(getattr(obj, "Name", ""))
    blocking: list[str] = []
    reroute_candidates: list[str] = []
    try:
        for dep in list(getattr(obj, "InList", ()) or ()):
            dep_name = str(getattr(dep, "Name", ""))
            if not dep_name or _groups_the_target(dep, obj):
                continue
            if _reroutes_via_base_feature(dep, obj):
                reroute_candidates.append(dep_name)
            else:
                blocking.append(dep_name)
    except Exception as exc:  # InList must be readable to refuse safely.
        raise ToolError(
            VALIDATION_FAILED,
            f"cannot enumerate dependents of '{name}': {_describe(exc)}",
        ) from exc
    blocking = sorted(set(blocking))
    reroute_candidates = sorted(set(reroute_candidates))
    if blocking:
        raise ToolError(
            VALIDATION_FAILED,
            f"object '{name}' has dependents; refusing to delete it without "
            "them being removed first",
            {"dependents": blocking[:_MAX_DEPENDENTS_LISTED]},
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
    rerouted = []
    for dep_name in reroute_candidates:
        survivor = doc.getObject(dep_name)
        if survivor is None:
            continue
        base = getattr(survivor, "BaseFeature", None)
        rerouted.append(
            {"feature": dep_name, "baseFeature": str(getattr(base, "Name", "")) or None}
        )
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "removed": removed,
        "rerouted": rerouted,
        "applied": applied,
    }


def edit_objects(ctx: Any, args: dict) -> dict:
    """Edit 1-32 objects atomically in one transaction and a single recompute."""
    doc = ctx.require_document(args["document"])
    require_expected_generation(
        ctx,
        doc,
        args,
        message="document changed since inspection; re-run inspect_objects",
        next_tool="inspect_objects",
    )
    edits = args["edits"]
    detail = str(args.get("response_detail") or "compact")
    queries = _PreparedQueries(ctx, doc)
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
        # Entry-keyed ownership mirrors create_objects: a same-named
        # property on another edit must never reuse this entry's prepared
        # query resolution.
        prepared[obj.Name] = _prepare_properties(
            ctx, doc, obj, properties, queries=queries, owner=f"edits[{position}]"
        )
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
        before_rows[obj.Name] = _snapshot_requested(obj, edit["properties"], ctx=ctx, doc=doc)
        before_reports[obj.Name] = geometry_report(obj)
        before_bounds[obj.Name] = document_bounds(obj)
    dependent_counts = {obj.Name: dependent_count([obj]) for obj in targets}
    spreadsheet_writes: list[tuple[Any, str, str, str]] = []
    spreadsheet_writes_by_object: dict[str, bool] = {}
    for obj, edit in zip(targets, edits, strict=True):
        properties = edit["properties"]
        if _is_spreadsheet(obj) and "cells" in properties:
            writes = _spreadsheet_write_receipt(obj, properties["cells"])
            spreadsheet_writes.extend(writes)
            spreadsheet_writes_by_object[obj.Name] = bool(writes)

    outcome: dict = {}
    with mutation(
        ctx,
        doc,
        "edit_objects",
        targets,
        expectations=expectations,
        outcome=outcome,
        check_workload=_check_workload,
        validate_after_recompute=lambda: _validate_spreadsheet_writes(spreadsheet_writes),
    ) as applied:
        for obj in targets:
            _apply_prepared(obj, prepared[obj.Name])

    changes = []
    for obj, edit in zip(targets, edits, strict=True):
        after_rows = _snapshot_requested(obj, edit["properties"], ctx=ctx, doc=doc)
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
                detail=detail,
                solid_before=before_reports[obj.Name]["solid_count"],
                solid_after=report["solid_count"],
                volume_before=before_reports[obj.Name]["volume"],
                volume_after=report["volume"],
                bounds_before=before_bounds[obj.Name],
                bounds_after=report["bounds"],
                dependents_before=dependent_counts[obj.Name],
                dependents=dependent_count([obj]),
                cell_contents_persisted=spreadsheet_writes_by_object.get(obj.Name, False),
            )
        )
    result = {
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
    if queries.receipts:
        result["resolvedSelections"] = queries.receipts
    return result


def create_objects(ctx: Any, args: dict) -> dict:
    """Create 1-32 objects atomically, reporting requested-to-actual name mappings."""
    doc = ctx.require_document(args["document"])
    detail = str(args.get("response_detail") or "compact")
    queries = _PreparedQueries(ctx, doc)
    entries = args["entries"]
    if not isinstance(entries, list) or not (1 <= len(entries) <= 32):
        raise ToolError(VALIDATION_FAILED, "entries must be an array of 1 to 32 objects")
    plans: list[dict] = []
    requested_names: set[str] = set()
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ToolError(VALIDATION_FAILED, f"entries[{position}] must be an object")
        obj_type = str(entry.get("type"))
        requested_name = str(entry.get("name"))
        properties = dict(entry.get("properties") or {})
        requested_names.add(requested_name)
        factory, factory_kwargs = _plan_create(ctx, doc, obj_type, properties)
        owner = f"entries[{position}]"
        for prop, value in properties.items():
            queries.scan_property(prop, value, owner=owner)
        plans.append(
            {
                "index": position,
                "type": obj_type,
                "name": requested_name,
                "properties": properties,
                "factory": factory,
                "kwargs": factory_kwargs,
            }
        )

    expectations = args.get("expectations") or {}
    if not isinstance(expectations, dict):
        raise ToolError(VALIDATION_FAILED, "expectations must be an object")
    if len(requested_names) != len(entries) and expectations:
        raise ToolError(
            VALIDATION_FAILED,
            "duplicate requested names are ambiguous when expectations are present",
            {"reason": "duplicate_requested_name"},
        )
    unknown = sorted(set(expectations) - requested_names)
    if unknown:
        raise ToolError(
            VALIDATION_FAILED,
            f"expectations name objects that are not created: {unknown}",
        )

    created: list[Any] = []
    name_mapping: list[dict] = []
    expectations_by_actual: dict[str, dict] = {}
    spreadsheet_writes: list[tuple[Any, str, str, str]] = []
    spreadsheet_writes_by_object: dict[str, bool] = {}
    outcome: dict = {}

    def validate_spreadsheet_writes() -> None:
        """Gate hook verifying the batch's planned cell writes survived the recompute."""
        _validate_spreadsheet_writes(spreadsheet_writes)

    with mutation(
        ctx,
        doc,
        "create_objects",
        lambda: created,
        expectations=expectations_by_actual,
        outcome=outcome,
        check_workload=_check_workload,
        validate_after_recompute=validate_spreadsheet_writes,
    ) as applied:
        for plan in plans:
            if plan["factory"] is not None:
                obj = _call_factory(plan["factory"], doc, plan["name"], plan["kwargs"])
            else:
                obj = doc.addObject(plan["type"], plan["name"])
            created.append(obj)
            actual_name = str(getattr(obj, "Name", ""))
            name_mapping.append({"requested": plan["name"], "actual": actual_name})
            if plan["name"] in expectations:
                expectations_by_actual[actual_name] = expectations[plan["name"]]
            if _is_spreadsheet(obj) and "cells" in plan["properties"]:
                writes = _spreadsheet_write_receipt(obj, plan["properties"]["cells"])
                spreadsheet_writes.extend(writes)
                spreadsheet_writes_by_object[obj.Name] = bool(writes)
            if plan["properties"]:
                _apply_properties(
                    ctx,
                    doc,
                    obj,
                    plan["properties"],
                    queries=queries,
                    owner=f"entries[{plan['index']}]",
                )

    identities = []
    changes = []
    for obj, plan in zip(created, plans, strict=True):
        # The gate already reported each new object after its recompute.
        report = outcome["reports"][str(obj.Name)]
        after_rows = _snapshot_requested(obj, plan["properties"], ctx=ctx, doc=doc)
        identities.append(
            {
                "name": str(getattr(obj, "Name", "")),
                "label": _label(obj),
                "typeId": str(getattr(obj, "TypeId", "")),
            }
        )
        changes.append(
            _change_summary(
                [{"name": name, "before": None, "after": after} for name, after in after_rows],
                detail=detail,
                solid_before=None,
                solid_after=report["solid_count"],
                volume_before=None,
                volume_after=report["volume"],
                bounds_before=None,
                bounds_after=report["bounds"],
                # A created object had no pre-mutation dependent closure.
                dependents_before=0,
                # outcome["dependentCountAfter"] is the batch-wide closure
                # union, so copying it to every row would overstate each
                # target. Read each target's own post-commit closure instead:
                # a bounded, read-only InList walk, no second mutation.
                dependents=dependent_count([obj]),
                cell_contents_persisted=spreadsheet_writes_by_object.get(obj.Name, False),
            )
        )
    result = {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "objects": identities,
        "nameMapping": name_mapping,
        "changes": changes,
        "applied": applied,
    }
    if queries.receipts:
        result["resolvedSelections"] = queries.receipts
    return result


HANDLERS = {
    "inspect_objects": inspect_objects,
    "create_object": create_object,
    "create_objects": create_objects,
    "edit_object": edit_object,
    "edit_objects": edit_objects,
    "delete_object": delete_object,
}
