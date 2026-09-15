"""``create_feature`` / ``edit_feature``: Body-aware PartDesign features.

Creation accepts either the original raw ``properties`` map (the five
original kinds) or typed semantic ``parameters`` mapped onto native
properties by :mod:`mcp_server.tools.feature_contracts`; the two are never
combined. Every feature is created through ``body.newObject`` so Body
membership and Tip stay native, and the whole operation runs inside one
shared mutation so a property, profile, support, Tip, bounds or geometry
failure removes the created feature through transaction abort.

``edit_feature`` edits the parameters of one existing feature that must
already belong to the named Body, recomputes, and reports the Body's final
geometry. When recovery is enabled, an expensive feature definition creates
one verified recovery copy before the transaction opens; a failed copy
refuses the mutation.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

from .. import input_aliases as _aliases
from .. import topology_query as tq
from ..object_validation import document_bounds, geometry_report, mutation
from ..protocol import (
    VALIDATION_FAILED,
    ProtocolError,
    ToolError,
    check_schema,
    stale_generation_details,
    validate_schema,
)
from . import feature_contracts as _contracts
from . import objects as _objects

#: Kinds whose original raw-``properties`` contract stays available. Every
#: other kind accepts typed semantic parameters only.
_KIND_TYPES = {
    "sketch": "Sketcher::SketchObject",
    "pad": "PartDesign::Pad",
    "pocket": "PartDesign::Pocket",
    "hole": "PartDesign::Hole",
    "datum_plane": "PartDesign::Plane",
}

#: Ordered datum candidates; only the core ``PartDesign::Plane`` is accepted.
_DATUM_TYPE_CANDIDATES = ("PartDesign::Plane", "PartDesign::FeaturePython")

_PROFILE_KINDS = (
    "pad",
    "pocket",
    "hole",
    "revolve",
    "groove",
    "loft",
    "pipe",
    "helix",
)
_GEAR_PROFILE_KIND = "gear_profile"
#: Feature TypeIds edit_feature accepts, mapped to their semantic kind.
#: Dress-ups (fillet, chamfer) and patterns (linear, polar) and revolutions
#: joined the original pad/pocket/hole/gear set so semantic edits cover the
#: common PartDesign sweep.
_EDITABLE_FEATURE_KINDS = {
    "PartDesign::Pad": "pad",
    "PartDesign::Pocket": "pocket",
    "PartDesign::Hole": "hole",
    "Part::Part2DObjectPython": _GEAR_PROFILE_KIND,
    "PartDesign::Fillet": "fillet",
    "PartDesign::Chamfer": "chamfer",
    "PartDesign::LinearPattern": "linear_pattern",
    "PartDesign::PolarPattern": "polar_pattern",
    "PartDesign::Revolution": "revolve",
}
_EDITABLE_KIND_NAMES = tuple(_EDITABLE_FEATURE_KINDS.values())
_SUPPORT_KINDS = ("sketch", "datum_plane", "datum_point", "primitive")
_ORIGIN_PLANE_ROLES = {"xy": "XY_Plane", "xz": "XZ_Plane", "yz": "YZ_Plane"}

#: Closed wire vocabularies for the liberal-input tables below. The request
#: schemas and the alias tables both build from these tuples, so a synonym
#: can never name a value the schema would refuse.
_CREATE_KINDS = (
    "datum_plane",
    "datum_line",
    "sketch",
    "pad",
    "pocket",
    "hole",
    "revolve",
    "groove",
    "fillet",
    "chamfer",
    "thickness",
    "draft",
    "linear_pattern",
    "polar_pattern",
    "mirrored",
    "loft",
    "pipe",
    "gear_profile",
    "helix",
    "primitive",
    "subshape_binder",
    "multi_transform",
    "scaled",
    "datum_point",
)
_PAD_EXTENTS = ("distance", "up_to_face")
_POCKET_EXTENTS = ("distance", "through_all", "up_to_face")
_HOLE_DEPTH_TYPES = ("dimension", "through_all")
_HOLE_CUTS = ("none", "counterbore", "countersink", "counterdrill")
_HOLE_THREADS = (
    "none",
    "iso_metric",
    "iso_metric_fine",
    "unc",
    "unf",
    "unef",
    "npt",
    "bsp",
    "bsw",
    "bsf",
)
_PRIMITIVE_SHAPES = (
    "box",
    "cylinder",
    "cone",
    "sphere",
    "prism",
    "torus",
    "ellipsoid",
    "wedge",
)
_HELIX_MODES = ("pitch_height", "pitch_turns", "height_turns", "height_growth")
_FEATURE_MODES = ("additive", "subtractive")
_TRANSFORMATION_KINDS = ("mirrored", "linear", "polar")
_DATUM_PLANES = ("xy", "xz", "yz")
_DATUM_AXES = ("x", "y", "z")
_SKETCH_AXES = ("H_Axis", "V_Axis", "N_Axis")
_MIRROR_SKETCH_AXES = ("H_Axis", "V_Axis")

#: Kinds that produce a solid Body result and must therefore become the Body
#: Tip. Datums, sketches and the gear wire profile are deliberately excluded:
#: a profile must never replace the solid Tip.
_TIP_KINDS = frozenset(
    {
        "pad",
        "pocket",
        "hole",
        "revolve",
        "groove",
        "fillet",
        "chamfer",
        "thickness",
        "draft",
        "linear_pattern",
        "polar_pattern",
        "mirrored",
        "loft",
        "pipe",
        "helix",
        "primitive",
        "multi_transform",
        "scaled",
    }
)

_PROFILE_REF = {
    "anyOf": [
        {"type": "string", "minLength": 1},
        _objects._CANONICAL_REF,
    ]
}

#: An axis is either a whole-object canonical reference or the closed
#: sketch-axis form; the two are resolved by different native paths.
_AXIS_REF = {
    "anyOf": [
        _objects._CANONICAL_REF,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["object", "sketchAxis"],
            "properties": {
                "object": _objects._NAME_FIELD,
                "sketchAxis": {"type": "string", "enum": list(_SKETCH_AXES)},
            },
        },
    ]
}

#: Draft's pull direction is a whole-object datum line only.
_LINE_REF = _objects._CANONICAL_REF

#: Mirror planes accept a canonical plane/planar-face reference or a sketch
#: H_Axis/V_Axis (an N_Axis mirror plane is not proven by the native tests).
_MIRROR_PLANE_REF = {
    "anyOf": [
        _objects._CANONICAL_REF,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["object", "sketchAxis"],
            "properties": {
                "object": _objects._NAME_FIELD,
                "sketchAxis": {"type": "string", "enum": list(_MIRROR_SKETCH_AXES)},
            },
        },
    ]
}

_SUBELEMENT_LIST = {
    "type": "array",
    "items": _objects._CANONICAL_REF,
    "minItems": 1,
    "maxItems": _contracts.MAX_SUBELEMENTS,
}

_NAME_LIST = {
    "type": "array",
    "items": _objects._NAME_FIELD,
    "minItems": 1,
    "maxItems": _contracts.MAX_PATTERN_ORIGINALS_SEMANTIC,
}

_SECTION_LIST = {
    "type": "array",
    "items": _objects._NAME_FIELD,
    "minItems": 1,
    "maxItems": _contracts.MAX_LOFT_SECTIONS,
}

_SCALAR_PARAM = {
    "anyOf": [
        {"type": "number"},
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["expression"],
            "properties": {"expression": {"type": "string", "minLength": 1, "maxLength": 256}},
        },
    ]
}

#: Semantic parameter contract per kind. Each branch is closed and states the
#: fields it requires, so an incomplete request fails wire validation rather
#: than opening a transaction.
_SEMANTIC_PARAM_SCHEMAS = {
    "sketch": {
        "type": "object",
        "additionalProperties": False,
        "required": ["plane"],
        "properties": {
            "plane": {"type": "string", "enum": list(_DATUM_PLANES)},
            "offset": _SCALAR_PARAM,
        },
    },
    "datum_plane": {
        "type": "object",
        "additionalProperties": False,
        "required": ["plane"],
        "properties": {
            "plane": {"type": "string", "enum": list(_DATUM_PLANES)},
            "offset": _SCALAR_PARAM,
        },
    },
    "pad": {
        "type": "object",
        "additionalProperties": False,
        "required": ["extent"],
        "properties": {
            "extent": {"type": "string", "enum": list(_PAD_EXTENTS)},
            "length": _SCALAR_PARAM,
            "face": _objects._CANONICAL_REF,
            "symmetric": {"type": "boolean"},
            "reversed": {"type": "boolean"},
        },
    },
    "pocket": {
        "type": "object",
        "additionalProperties": False,
        "required": ["extent"],
        "properties": {
            "extent": {"type": "string", "enum": list(_POCKET_EXTENTS)},
            "length": _SCALAR_PARAM,
            "face": _objects._CANONICAL_REF,
            "symmetric": {"type": "boolean"},
            "reversed": {"type": "boolean"},
        },
    },
    "hole": {
        "type": "object",
        "additionalProperties": False,
        "required": ["diameter"],
        "properties": {
            "diameter": _SCALAR_PARAM,
            "depth": _SCALAR_PARAM,
            "depth_type": {"type": "string", "enum": list(_HOLE_DEPTH_TYPES)},
            "cut": {
                "type": "string",
                "enum": list(_HOLE_CUTS),
            },
            "counterbore_diameter": _SCALAR_PARAM,
            "counterbore_depth": _SCALAR_PARAM,
            "countersink_diameter": _SCALAR_PARAM,
            "countersink_angle": _SCALAR_PARAM,
            "thread": {
                "type": "string",
                "enum": list(_HOLE_THREADS),
            },
            "thread_size": {"type": "string", "minLength": 1, "maxLength": 16},
        },
    },
    "gear_profile": {
        "type": "object",
        "additionalProperties": False,
        "required": ["teeth", "module"],
        "properties": {
            "teeth": {
                "type": "integer",
                "minimum": _contracts.MIN_GEAR_TEETH,
                "maximum": _contracts.MAX_GEAR_TEETH,
            },
            "module": {
                "type": "number",
                "minimum": _contracts.MIN_GEAR_MODULE_MM,
                "maximum": _contracts.MAX_GEAR_MODULE_MM,
            },
            "pressure_angle": {"type": "number", "minimum": 14.5, "maximum": 25},
        },
    },
    "datum_line": {
        "type": "object",
        "additionalProperties": False,
        "required": ["axis"],
        "properties": {"axis": {"type": "string", "enum": list(_DATUM_AXES)}},
    },
    "revolve": {
        "type": "object",
        "additionalProperties": False,
        "required": ["axis", "angle"],
        "properties": {
            "axis": _AXIS_REF,
            "angle": _SCALAR_PARAM,
            "reversed": {"type": "boolean"},
        },
    },
    "groove": {
        "type": "object",
        "additionalProperties": False,
        "required": ["axis", "angle"],
        "properties": {
            "axis": _AXIS_REF,
            "angle": _SCALAR_PARAM,
            "reversed": {"type": "boolean"},
        },
    },
    "fillet": {
        "type": "object",
        "additionalProperties": False,
        "required": ["base", "subelements", "radius"],
        "properties": {
            "base": _objects._CANONICAL_REF,
            "subelements": _SUBELEMENT_LIST,
            "radius": _SCALAR_PARAM,
        },
    },
    "chamfer": {
        "type": "object",
        "additionalProperties": False,
        "required": ["base", "subelements", "size"],
        "properties": {
            "base": _objects._CANONICAL_REF,
            "subelements": _SUBELEMENT_LIST,
            "size": _SCALAR_PARAM,
        },
    },
    "thickness": {
        "type": "object",
        "additionalProperties": False,
        "required": ["base", "subelements", "thickness"],
        "properties": {
            "base": _objects._CANONICAL_REF,
            "subelements": _SUBELEMENT_LIST,
            "thickness": _SCALAR_PARAM,
            "inward": {"type": "boolean"},
        },
    },
    "draft": {
        "type": "object",
        "additionalProperties": False,
        "required": ["base", "subelements", "neutral_plane", "pull_direction", "angle"],
        "properties": {
            "base": _objects._CANONICAL_REF,
            "subelements": _SUBELEMENT_LIST,
            "neutral_plane": _objects._CANONICAL_REF,
            "pull_direction": _LINE_REF,
            "angle": _SCALAR_PARAM,
            "reversed": {"type": "boolean"},
        },
    },
    "linear_pattern": {
        "type": "object",
        "additionalProperties": False,
        "required": ["originals", "axis", "count", "length"],
        "properties": {
            "originals": _NAME_LIST,
            "axis": _AXIS_REF,
            "count": {"type": "integer", "minimum": 2, "maximum": 32},
            "length": _SCALAR_PARAM,
        },
    },
    "polar_pattern": {
        "type": "object",
        "additionalProperties": False,
        "required": ["originals", "axis", "count"],
        "properties": {
            "originals": _NAME_LIST,
            "axis": _AXIS_REF,
            "count": {"type": "integer", "minimum": 2, "maximum": 32},
            "angle": _SCALAR_PARAM,
        },
    },
    "mirrored": {
        "type": "object",
        "additionalProperties": False,
        "required": ["originals", "plane"],
        "properties": {
            "originals": _NAME_LIST,
            "plane": _MIRROR_PLANE_REF,
        },
    },
    "loft": {
        "type": "object",
        "additionalProperties": False,
        "required": ["sections", "mode"],
        "properties": {
            "sections": _SECTION_LIST,
            "mode": {"type": "string", "enum": list(_FEATURE_MODES)},
            "ruled": {"type": "boolean"},
        },
    },
    "pipe": {
        "type": "object",
        "additionalProperties": False,
        "required": ["spine", "mode"],
        "properties": {
            "spine": _objects._NAME_FIELD,
            "mode": {"type": "string", "enum": list(_FEATURE_MODES)},
        },
    },
    "helix": {
        "type": "object",
        "additionalProperties": False,
        "required": ["axis", "helix_mode", "mode"],
        "properties": {
            "axis": _AXIS_REF,
            "helix_mode": {
                "type": "string",
                "enum": list(_HELIX_MODES),
            },
            "mode": {"type": "string", "enum": list(_FEATURE_MODES)},
            "pitch": _SCALAR_PARAM,
            "height": _SCALAR_PARAM,
            "turns": _SCALAR_PARAM,
            "angle": _SCALAR_PARAM,
            "growth": _SCALAR_PARAM,
            "left_handed": {"type": "boolean"},
            "reversed": {"type": "boolean"},
        },
    },
    "primitive": {
        "type": "object",
        "additionalProperties": False,
        "required": ["shape", "mode"],
        "properties": {
            "shape": {
                "type": "string",
                "enum": list(_PRIMITIVE_SHAPES),
            },
            "mode": {"type": "string", "enum": list(_FEATURE_MODES)},
            "length": _SCALAR_PARAM,
            "width": _SCALAR_PARAM,
            "height": _SCALAR_PARAM,
            "radius": _SCALAR_PARAM,
            "radius1": _SCALAR_PARAM,
            "radius2": _SCALAR_PARAM,
            "radius3": _SCALAR_PARAM,
            "circumradius": _SCALAR_PARAM,
            "polygon": {
                "type": "integer",
                "minimum": _contracts.MIN_PRISM_POLYGON,
                "maximum": _contracts.MAX_PRISM_POLYGON,
            },
            "x_min": _SCALAR_PARAM,
            "x_max": _SCALAR_PARAM,
            "y_min": _SCALAR_PARAM,
            "y_max": _SCALAR_PARAM,
            "z_min": _SCALAR_PARAM,
            "z_max": _SCALAR_PARAM,
            "x2_min": _SCALAR_PARAM,
            "x2_max": _SCALAR_PARAM,
            "z2_min": _SCALAR_PARAM,
            "z2_max": _SCALAR_PARAM,
        },
    },
    "subshape_binder": {
        "type": "object",
        "additionalProperties": False,
        "required": ["references"],
        "properties": {
            "references": {
                "type": "array",
                "minItems": 1,
                "maxItems": _contracts.MAX_BINDER_REFERENCES,
                "items": _objects._CANONICAL_REF,
            },
            "make_face": {"type": "boolean"},
        },
    },
    "multi_transform": {
        "type": "object",
        "additionalProperties": False,
        "required": ["originals", "transformations"],
        "properties": {
            "originals": _NAME_LIST,
            "transformations": {
                "type": "array",
                "minItems": 1,
                "maxItems": _contracts.MAX_TRANSFORM_STEPS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind"],
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": list(_TRANSFORMATION_KINDS),
                        },
                        "plane": _MIRROR_PLANE_REF,
                        "axis": _AXIS_REF,
                        "length": _SCALAR_PARAM,
                        "count": {"type": "integer", "minimum": 2, "maximum": 32},
                        "angle": _SCALAR_PARAM,
                    },
                },
            },
        },
    },
    "scaled": {
        "type": "object",
        "additionalProperties": False,
        "required": ["originals", "factor", "count"],
        "properties": {
            "originals": _NAME_LIST,
            "factor": _SCALAR_PARAM,
            "count": {"type": "integer", "minimum": 2, "maximum": 32},
        },
    },
    "datum_point": {  # identical contract to datum_plane
        "type": "object",
        "additionalProperties": False,
        "required": ["plane"],
        "properties": {
            "plane": {"type": "string", "enum": list(_DATUM_PLANES)},
            "offset": _SCALAR_PARAM,
        },
    },
}

# Named root definitions keep the semantic union inspectable and let handler
# validation select the same closed branch that registration exposes.
_SEMANTIC_PARAM_DEFS = {
    f"{kind}Parameters": schema for kind, schema in _SEMANTIC_PARAM_SCHEMAS.items()
}


def _semantic_ref(kind: str) -> dict[str, str]:
    """Return the named schema reference for one semantic feature kind."""

    return {"$ref": f"#/$defs/{kind}Parameters"}


_CREATE_FEATURE_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "body", "kind", "name"],
    "properties": {
        "document": _objects._DOCUMENT_FIELD,
        "body": _objects._NAME_FIELD,
        "kind": {
            "type": "string",
            "enum": list(_CREATE_KINDS),
        },
        "name": _objects._NAME_FIELD,
        "properties": _objects._PROPERTIES_MAP,
        "parameters": {
            "anyOf": [_semantic_ref(kind) for kind in _SEMANTIC_PARAM_SCHEMAS],
        },
        "profile": _PROFILE_REF,
        "support": _objects._CANONICAL_REF,
        "expected_solids": _objects._EXPECTED_SOLIDS,
        "expected_bounds": _objects._EXPECTED_BOUNDS,
        "bounds_tolerance": _objects._BOUNDS_TOLERANCE,
        "response_detail": _objects._RESPONSE_DETAIL,
    },
    "$defs": {**_objects._VALUE_DEFS, **_SEMANTIC_PARAM_DEFS},
}

_CHECKPOINT_PROPERTY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "document", "generation"],
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "document": {"type": "string", "minLength": 1},
        "generation": {"type": "integer", "minimum": 0},
    },
}

_CREATE_FEATURE_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "document",
        "generation",
        "object",
        "body",
        "bodyTip",
        "bodyReport",
        "change",
        "applied",
    ],
    "properties": {
        "document": {"type": "string"},
        "generation": _objects._GENERATION,
        "object": {"$ref": "#/$defs/objectIdentity"},
        "body": {"$ref": "#/$defs/objectIdentity"},
        "bodyTip": {"type": ["string", "null"]},
        "bodyReport": {"$ref": "#/$defs/geometryReport"},
        "change": _objects._CHANGE,
        "applied": _objects._APPLIED,
        "checkpoint": _CHECKPOINT_PROPERTY,
        "resolvedSelections": _objects._RESOLVED_SELECTIONS,
    },
    "$defs": {
        "geometryReport": _objects._GEOMETRY_REPORT,
        "objectIdentity": _objects._OBJECT_IDENTITY,
        "topologyResolvedSelection": tq.QUERY_DEFS["topologyResolvedSelection"],
    },
}

_EDIT_FEATURE_INPUT = tq.merge_query_defs(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["document", "body", "object", "parameters"],
        "properties": {
            "document": _objects._DOCUMENT_FIELD,
            "body": _objects._NAME_FIELD,
            "object": _objects._NAME_FIELD,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "radius": _SCALAR_PARAM,
                    "size": _SCALAR_PARAM,
                    "angle": _SCALAR_PARAM,
                    "count": {"type": "integer", "minimum": 2, "maximum": 32},
                    "extent": {"type": "string", "enum": list(_POCKET_EXTENTS)},
                    "length": _SCALAR_PARAM,
                    "face": _objects._CANONICAL_REF,
                    "diameter": _SCALAR_PARAM,
                    "depth": _SCALAR_PARAM,
                    "depth_type": {"type": "string", "enum": list(_HOLE_DEPTH_TYPES)},
                    "cut": {
                        "type": "string",
                        "enum": list(_HOLE_CUTS),
                    },
                    "counterbore_diameter": _SCALAR_PARAM,
                    "counterbore_depth": _SCALAR_PARAM,
                    "countersink_diameter": _SCALAR_PARAM,
                    "countersink_angle": _SCALAR_PARAM,
                    "thread": {
                        "type": "string",
                        "enum": list(_HOLE_THREADS),
                    },
                    "thread_size": {"type": "string", "minLength": 1, "maxLength": 16},
                    "symmetric": {"type": "boolean"},
                    "reversed": {"type": "boolean"},
                    "teeth": {
                        "type": "integer",
                        "minimum": _contracts.MIN_GEAR_TEETH,
                        "maximum": _contracts.MAX_GEAR_TEETH,
                    },
                    "module": {
                        "type": "number",
                        "minimum": _contracts.MIN_GEAR_MODULE_MM,
                        "maximum": _contracts.MAX_GEAR_MODULE_MM,
                    },
                    "pressure_angle": {"type": "number", "minimum": 14.5, "maximum": 25},
                },
            },
            "expected_generation": {"type": ["integer", "null"], "minimum": 0},
            "expected_solids": _objects._EXPECTED_SOLIDS,
            "expected_bounds": _objects._EXPECTED_BOUNDS,
            "bounds_tolerance": _objects._BOUNDS_TOLERANCE,
            "response_detail": _objects._RESPONSE_DETAIL,
        },
    }
)

#: One persisted parameter state: the actual native value plus the live
#: expression binding (null when the value is written directly).
_PARAMETER_STATE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "expression"],
    "properties": {
        "value": {"type": ["number", "string", "boolean", "null"]},
        "expression": {"type": ["string", "null"]},
    },
}

#: One requested parameter's uniform readback row. ``before`` appears only
#: for response_detail full; the row shape is identical for numeric,
#: boolean, enum and expression-driven parameters.
_PARAMETER_VALUES_ROW = {
    "type": "object",
    "additionalProperties": False,
    "required": ["parameter", "property", "after"],
    "properties": {
        "parameter": {"type": "string", "minLength": 1},
        "property": {"type": "string", "minLength": 1},
        "after": _PARAMETER_STATE,
        "before": _PARAMETER_STATE,
    },
}

#: edit_feature reports uniform parameter rows and Body evidence; the
#: creation report (change/applied) stays create_feature's own shape.
_EDIT_FEATURE_OUTPUT = tq.merge_query_defs(
    {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "document",
            "generation",
            "object",
            "body",
            "bodyTip",
            "bodyReport",
            "parameterValues",
        ],
        "properties": {
            "document": {"type": "string"},
            "generation": _objects._GENERATION,
            "object": {"$ref": "#/$defs/objectIdentity"},
            "body": {"$ref": "#/$defs/objectIdentity"},
            "bodyTip": {"type": ["string", "null"]},
            "bodyReport": {"$ref": "#/$defs/geometryReport"},
            "parameterValues": {
                "type": "array",
                "items": _PARAMETER_VALUES_ROW,
                "minItems": 1,
                "maxItems": 32,
            },
            "checkpoint": _CHECKPOINT_PROPERTY,
            "geometryChange": _objects._CHANGE_GEOMETRY,
            "resolvedSelections": _objects._RESOLVED_SELECTIONS,
        },
        "$defs": {
            "geometryReport": _objects._GEOMETRY_REPORT,
            "objectIdentity": _objects._OBJECT_IDENTITY,
            "topologyResolvedSelection": tq.QUERY_DEFS["topologyResolvedSelection"],
        },
    }
)

# ---------------------------------------------------------------------------
# Liberal input (Postel): accept the synonym, emit the canonical kind.
# ---------------------------------------------------------------------------

#: What other modelers call the operation, mapped to the PartDesign kind
#: that performs it. Each entry is unambiguous inside the closed kind set:
#: no alias folds onto two members, and every target is a schema member by
#: construction, because the tables build from the same tuples as the
#: schemas. Unknown values pass through untouched and are refused by the
#: input schema under the caller's own spelling.
_KIND_ALIASES = {
    "extrude": "pad",
    "extrusion": "pad",
    "extruded boss": "pad",
    "cut": "pocket",
    "extruded cut": "pocket",
    "cut extrude": "pocket",
    "sweep": "pipe",
    "mirror": "mirrored",
    "circular pattern": "polar_pattern",
    "rectangular pattern": "linear_pattern",
    "gear": "gear_profile",
    "involute gear": "gear_profile",
    "shell": "thickness",
    "scale": "scaled",
    "plane": "datum_plane",
    "line": "datum_line",
    "point": "datum_point",
    "revolved cut": "groove",
    "revolve cut": "groove",
}
_EXTENT_ALIASES = {
    "length": "distance",
    "blind": "distance",
    "through": "through_all",
    "thru": "through_all",
    "through all": "through_all",
    "thru all": "through_all",
    "all": "through_all",
    "to face": "up_to_face",
    "to object": "up_to_face",
}
_DEPTH_TYPE_ALIASES = {
    "distance": "dimension",
    "blind": "dimension",
    "length": "dimension",
    "through": "through_all",
    "thru": "through_all",
    "through all": "through_all",
    "thru all": "through_all",
    "all": "through_all",
}
_HOLE_CUT_ALIASES = {
    "plain": "none",
    "simple": "none",
    "drill": "none",
    "c'bore": "counterbore",
    "csk": "countersink",
    "c'sink": "countersink",
}
_THREAD_ALIASES = {
    "metric": "iso_metric",
    "metric fine": "iso_metric_fine",
}
_PRIMITIVE_SHAPE_ALIASES = {
    "cuboid": "box",
    "cube": "box",
    "block": "box",
}
_FEATURE_MODE_ALIASES = {
    "add": "additive",
    "subtract": "subtractive",
    "remove": "subtractive",
}
_TRANSFORMATION_KIND_ALIASES = {
    "mirror": "mirrored",
    "circular": "polar",
    "rectangular": "linear",
}
_SKETCH_AXIS_ALIASES = {
    "horizontal": "H_Axis",
    "vertical": "V_Axis",
    "normal": "N_Axis",
    "h": "H_Axis",
    "v": "V_Axis",
    "n": "N_Axis",
}
_MIRROR_SKETCH_AXIS_ALIASES = {
    "horizontal": "H_Axis",
    "vertical": "V_Axis",
    "h": "H_Axis",
    "v": "V_Axis",
}

#: Extents and modes fold against the widest schema that carries the field;
#: a kind whose own enum is narrower (a pad has no through_all) refuses the
#: canonical value itself, which keeps the refusal anchored to the caller's
#: unambiguous intent.
_KIND_TABLE = _aliases.build_table(_CREATE_KINDS, _KIND_ALIASES)
_EXTENT_TABLE = _aliases.build_table(_POCKET_EXTENTS, _EXTENT_ALIASES)
_DEPTH_TYPE_TABLE = _aliases.build_table(_HOLE_DEPTH_TYPES, _DEPTH_TYPE_ALIASES)
_HOLE_CUT_TABLE = _aliases.build_table(_HOLE_CUTS, _HOLE_CUT_ALIASES)
_THREAD_TABLE = _aliases.build_table(_HOLE_THREADS, _THREAD_ALIASES)
_PRIMITIVE_SHAPE_TABLE = _aliases.build_table(_PRIMITIVE_SHAPES, _PRIMITIVE_SHAPE_ALIASES)
_HELIX_MODE_TABLE = _aliases.build_table(_HELIX_MODES)
_FEATURE_MODE_TABLE = _aliases.build_table(_FEATURE_MODES, _FEATURE_MODE_ALIASES)
_TRANSFORMATION_KIND_TABLE = _aliases.build_table(
    _TRANSFORMATION_KINDS, _TRANSFORMATION_KIND_ALIASES
)
_DATUM_PLANE_TABLE = _aliases.build_table(_DATUM_PLANES)
_DATUM_AXIS_TABLE = _aliases.build_table(_DATUM_AXES)
_SKETCH_AXIS_TABLE = _aliases.build_table(_SKETCH_AXES, _SKETCH_AXIS_ALIASES)
_MIRROR_SKETCH_AXIS_TABLE = _aliases.build_table(_MIRROR_SKETCH_AXES, _MIRROR_SKETCH_AXIS_ALIASES)

#: Request paths each table governs. ``parameters`` values normalize inside
#: the semantic parameter map; ``parameters.transformations[].kind``
#: descends into every multi_transform step. The mirror-plane sketch axis
#: lives at ``parameters.transformations[].plane.sketchAxis`` and the
#: profile's at ``profile.sketchAxis``; non-string leaves and absent paths
#: pass through.
_CREATE_NORMALIZER_SPEC = {
    "kind": _KIND_TABLE,
    "parameters.extent": _EXTENT_TABLE,
    "parameters.depth_type": _DEPTH_TYPE_TABLE,
    "parameters.cut": _HOLE_CUT_TABLE,
    "parameters.thread": _THREAD_TABLE,
    "parameters.shape": _PRIMITIVE_SHAPE_TABLE,
    "parameters.helix_mode": _HELIX_MODE_TABLE,
    "parameters.mode": _FEATURE_MODE_TABLE,
    "parameters.plane": _DATUM_PLANE_TABLE,
    "parameters.axis": _DATUM_AXIS_TABLE,
    "parameters.axis.sketchAxis": _SKETCH_AXIS_TABLE,
    "parameters.transformations[].kind": _TRANSFORMATION_KIND_TABLE,
    "parameters.transformations[].plane.sketchAxis": _MIRROR_SKETCH_AXIS_TABLE,
    "parameters.transformations[].axis.sketchAxis": _SKETCH_AXIS_TABLE,
    "profile.sketchAxis": _SKETCH_AXIS_TABLE,
}
_EDIT_FEATURE_NORMALIZER_SPEC = {
    "parameters.extent": _EXTENT_TABLE,
    "parameters.depth_type": _DEPTH_TYPE_TABLE,
    "parameters.cut": _HOLE_CUT_TABLE,
    "parameters.thread": _THREAD_TABLE,
}


def _normalize_create_arguments(arguments: dict) -> dict:
    """Fold create_feature synonyms and case/separator variants to canonical values."""

    return _aliases.normalize_arguments(arguments, _CREATE_NORMALIZER_SPEC)


def _normalize_edit_feature_arguments(arguments: dict) -> dict:
    """Fold edit_feature parameter synonyms and spelling variants to canonical values."""

    return _aliases.normalize_arguments(arguments, _EDIT_FEATURE_NORMALIZER_SPEC)


TOOL_DEFINITIONS = [
    {
        "name": "create_feature",
        "normalize": _normalize_create_arguments,
        "description": (
            "Create one PartDesign feature inside an existing Body: "
            "datum_plane (PartDesign::Plane), datum_line (PartDesign::Line), "
            "sketch (Sketcher::SketchObject), pad, pocket, hole, revolve "
            "(Revolution), groove, fillet, chamfer, thickness, draft, "
            "linear_pattern, polar_pattern, mirrored, loft, pipe (each "
            "additive or subtractive where the kind offers both), or the "
            "involute gear_profile wire. Also creates helix (additive or "
            "subtractive, pitch/height/turns driven), the eight primitive "
            "shapes (additive or subtractive), a same-document "
            "subshape_binder reference holder, an atomic multi_transform "
            "composite, scaled features, and datum points. The feature is "
            "created through "
            "body.newObject so Body membership and Tip are native; solid "
            "kinds require a profile, and dress-ups, patterns and lofts/pipes "
            "take shared-target subelement, originals, axis, plane or spine "
            "references: a signed reference or a declarative query that "
            "expands to the matched set within each list's budget. Semantic "
            "parameters map onto native properties (lengths in mm, angles "
            "in degrees). Scalar parameters accept either a number or an "
            "{expression} binding; integer-only fields such as teeth, "
            "polygon, and pattern count, enumerations, and flags take "
            "direct values only. Everything runs in one transaction; a "
            "property, profile, reference, Tip, bounds or geometry failure "
            'aborts and removes the feature. response_detail: "compact" '
            "omits before-state geometry deltas, property before-values, "
            "and dependent counts from change; bodyReport always carries "
            "the post-state verdict. Common synonyms from other modelers "
            "(extrude, cut, sweep, circular pattern, shell) and case or "
            "spacing variants are accepted and normalized; results always "
            "report the canonical kind."
        ),
        "inputSchema": _CREATE_FEATURE_INPUT,
        "outputSchema": _CREATE_FEATURE_OUTPUT,
    },
    {
        "name": "edit_feature",
        "normalize": _normalize_edit_feature_arguments,
        "description": (
            "Edit one existing PartDesign feature that belongs to the named "
            "Body: pad/pocket extent and length, hole diameter/depth and "
            "its cut, depth-type and thread parameters, gear "
            "teeth/module/pressure angle, fillet radius, chamfer size, "
            "linear/polar pattern count, length and angle, and revolve "
            "angle/reversed. Parameters are validated against the feature's "
            "kind (a wrong-kind parameter names the kind and its supported "
            "parameters) before the transaction opens. Other feature types "
            "are refused with the supported kinds and an edit_object "
            "fallback. Scalar values are written natively and {expression} "
            "objects bind a native expression on scalar parameters only; "
            "integer-only fields, enumerations, and flags reject "
            "expressions, and a resolved expression that lands outside the "
            "parameter's range refuses and rolls back. "
            "The feature and its Body are recomputed and validated in one "
            "mutation, and parameterValues reports every requested "
            "parameter's persisted expression and actual native value; "
            "bodyReport always carries the post-state verdict. "
            "Extent, depth-type, cut, and thread values accept common "
            "synonyms and case or spacing variants (thru, blind, c'bore, "
            "metric) and normalize to the canonical enum; results always "
            "report the canonical value. "
            'response_detail "full" adds each row before-state and the '
            "geometryChange deltas; the default compact omits them."
        ),
        "inputSchema": _EDIT_FEATURE_INPUT,
        "outputSchema": _EDIT_FEATURE_OUTPUT,
    },
]

HANDLERS: dict[str, Callable[[Any, dict], Any]] = {}

check_schema(_CREATE_FEATURE_INPUT)
check_schema(_CREATE_FEATURE_OUTPUT)
check_schema(_EDIT_FEATURE_INPUT)
check_schema(_EDIT_FEATURE_OUTPUT)


# ---------------------------------------------------------------------------
# Prevalidation (before the transaction opens).
# ---------------------------------------------------------------------------


def _fail(message: str, details: dict | None = None) -> ToolError:
    """Build a VALIDATION_FAILED tool error with the given message."""
    return ToolError(VALIDATION_FAILED, message, details)


def _require_body(body: Any) -> None:
    """Refuse any object that is not a PartDesign Body."""
    derived = getattr(body, "isDerivedFrom", None)
    if callable(derived):
        try:
            if derived("PartDesign::Body"):
                return
        except Exception:
            pass
    if str(getattr(body, "TypeId", "")) == "PartDesign::Body":
        return
    raise _fail(f"object '{getattr(body, 'Name', '<unknown>')}' is not a PartDesign::Body")


def _body_members(body: Any) -> list[Any] | None:
    """Return the Body's member list, or None when neither Group nor Model exists."""
    for attribute in ("Group", "Model"):
        members = getattr(body, attribute, None)
        if members is not None:
            try:
                return list(members)
            except Exception:
                continue
    return None


def _require_member(body: Any, obj: Any) -> None:
    """Require ``obj`` to be a current member of ``body``."""

    members = _body_members(body)
    if members is None:
        raise _fail(
            f"cannot verify Body membership for '{getattr(obj, 'Name', '')}': "
            "the body exposes neither Group nor Model"
        )
    if obj not in members:
        raise _fail(
            f"object '{getattr(obj, 'Name', '')}' does not belong to body "
            f"'{getattr(body, 'Name', '')}'"
        )


def _datum_type_id(doc: Any) -> str | None:
    """The core datum-plane TypeId when the document supports it."""

    try:
        supported = {str(entry) for entry in (doc.supportedTypes() or ())}
    except Exception:
        return None
    for candidate in _DATUM_TYPE_CANDIDATES:
        if candidate in supported:
            return candidate if candidate == "PartDesign::Plane" else None
    return None


def _profile_object(ctx: Any, doc: Any, body: Any, profile: Any) -> Any:
    """Resolve the profile reference to a Body member, refusing subshapes."""
    if isinstance(profile, str):
        profile_obj = ctx.require_object(doc, profile)
    else:
        reference = dict(profile)
        if "query" in reference or reference.get("subelement"):
            raise ToolError(
                VALIDATION_FAILED,
                "profile must be a whole-object reference; selected subshapes are not accepted",
                {"reason": "subshape_not_allowed"},
            )
        profile_obj = ctx.require_object(doc, str(reference.get("object")))
    _require_member(body, profile_obj)
    return profile_obj


def _native_support(
    ctx: Any,
    doc: Any,
    support: Mapping,
    *,
    path: str = "support",
    queries: _objects._PreparedQueries | None = None,
) -> tuple[Any, str]:
    """Resolve a whole/signed/query reference through the shared resolver.

    A query-bearing value is resolved once through the operation's prepared
    context (which caches the resolution and reserves its receipt), so the
    appliers consume the selection-time result instead of re-running the
    selector after state changes.
    """

    from . import geometry

    if queries is not None and isinstance(support, Mapping) and "query" in support:
        obj, role, indices = queries.resolve(path, support)
        if not indices or len(indices) > 1:
            raise _fail(f"{path} must resolve to exactly one subshape for this position")
        native = ("Face" if role == "face" else "Edge") + str(indices[0])
        return obj, native
    return geometry.resolve_reference(ctx, doc, support, path)


def _origin_members(body: Any) -> list[Any] | None:
    """Members of this Body's Origin container, or ``None`` when absent."""

    origin = getattr(body, "Origin", None)
    if origin is None:
        return None
    for attribute in ("OriginFeatures", "Group", "Features"):
        members = getattr(origin, attribute, None)
        if members is None:
            continue
        try:
            listed = list(members)
        except Exception:
            continue
        if listed:
            return listed
    return []


def _origin_plane(body: Any, role: str) -> Any:
    """Resolve one Body-local origin plane; never another Body's origin.

    The planes are separate ``App::Plane`` objects held by the Body's own
    Origin container, so they are matched by name inside that container
    rather than by attribute access or by any other Body's origin.
    """

    if role not in _ORIGIN_PLANE_ROLES:
        raise _fail(f"plane {role!r} must be one of xy, xz, yz")
    expected = _ORIGIN_PLANE_ROLES[role]
    members = _origin_members(body)
    if members is None:
        raise _fail(f"body '{getattr(body, 'Name', '')}' exposes no Origin container")
    for member in members:
        if (
            str(getattr(member, "Name", "")) == expected
            or str(getattr(member, "Role", "")) == expected
        ):
            return member
    raise _fail(
        f"body '{getattr(body, 'Name', '')}' has no local origin plane "
        f"'{expected}'; semantic attachment is unavailable"
    )


def _resolve_axis(
    ctx: Any,
    doc: Any,
    reference: Any,
    *,
    path: str = "axis",
    queries: _objects._PreparedQueries | None = None,
) -> tuple[Any, list[str]]:
    """Resolve an axis reference to a native ``LinkSub`` value.

    A canonical whole-object reference must name a native origin axis or a
    datum line; the closed ``{object, sketchAxis}`` form names a sketch and
    one of its axis literals. The two forms never mix, and selected
    subshapes are refused here (axes are whole-object positions).
    """

    if not isinstance(reference, Mapping):
        raise _fail("axis must be a canonical reference or a sketch-axis object")
    if "query" in reference:
        raise _fail("an axis reference must be a whole object, not a subshape")
    if "sketchAxis" in reference:
        name = reference.get("object")
        axis = reference.get("sketchAxis")
        if not isinstance(name, str) or not name:
            raise _fail("sketch-axis reference requires an object name")
        if axis not in ("H_Axis", "V_Axis", "N_Axis"):
            raise _fail("sketchAxis must be H_Axis, V_Axis or N_Axis")
        sketch = ctx.require_object(doc, name)
        if str(getattr(sketch, "TypeId", "")) != "Sketcher::SketchObject":
            raise _fail(f"axis sketch '{name}' is not a Sketcher::SketchObject")
        return sketch, [str(axis)]
    obj, native = _native_support(ctx, doc, reference, path=path, queries=queries)
    if native:
        raise _fail("an axis reference must be a whole object, not a subelement")
    if str(getattr(obj, "TypeId", "")) not in ("App::Line", "PartDesign::Line"):
        raise _fail(f"axis '{getattr(obj, 'Name', '')}' is not a native origin axis or datum line")
    return obj, [""]


#: Kinds whose ``ReferenceAxis`` is verified only with a whole-object native
#: origin axis or datum line. The native 1.1.3 tests set
#: ``Revolution.ReferenceAxis = (Doc.Y_Axis, [""])``; a sketch axis literal
#: recomputes to an invalid shape there, so it is refused rather than guessed.
_AXIS_REQUIRES_OBJECT = ("revolve", "groove")


def _check_axis_form(kind: str, value: Any) -> None:
    """Refuse an axis form this kind has no recorded native acceptance for."""

    if kind in _AXIS_REQUIRES_OBJECT and isinstance(value, Mapping) and "sketchAxis" in value:
        raise _fail(
            f"kind '{kind}' requires a whole-object native origin axis or "
            "datum line; a sketch-axis reference is not verified for this "
            "feature"
        )


def _resolve_plane(
    ctx: Any,
    doc: Any,
    reference: Any,
    *,
    allow_sketch_axes: bool = False,
    path: str = "plane",
    queries: _objects._PreparedQueries | None = None,
) -> tuple[Any, list[str]]:
    """Resolve a plane reference to a native ``LinkSub`` value.

    A query-bearing reference resolves to exactly one compatible face
    through the prepared context; whole objects must be native planes.
    """

    if isinstance(reference, Mapping) and "sketchAxis" in reference:
        if not allow_sketch_axes:
            raise _fail("this plane does not accept a sketch-axis reference")
        name = reference.get("object")
        axis = reference.get("sketchAxis")
        if not isinstance(name, str) or not name:
            raise _fail("sketch-axis reference requires an object name")
        if axis not in ("H_Axis", "V_Axis"):
            raise _fail("a mirror plane accepts H_Axis or V_Axis only")
        sketch = ctx.require_object(doc, name)
        if str(getattr(sketch, "TypeId", "")) != "Sketcher::SketchObject":
            raise _fail(f"mirror plane sketch '{name}' is not a Sketcher::SketchObject")
        return sketch, [str(axis)]
    obj, native = _native_support(ctx, doc, reference, path=path, queries=queries)
    if native and not native.startswith("Face"):
        raise _fail(f"plane reference must be a plane or a signed face token, got {native}")
    if not native and str(getattr(obj, "TypeId", "")) not in (
        "App::Plane",
        "PartDesign::Plane",
    ):
        raise _fail(
            f"plane '{getattr(obj, 'Name', '')}' is not a native origin plane or datum plane"
        )
    return obj, [native]


def _resolve_line(
    ctx: Any,
    doc: Any,
    reference: Any,
    *,
    path: str = "line",
    queries: _objects._PreparedQueries | None = None,
) -> tuple[Any, list[str]]:
    """Resolve a pull direction: a whole ``PartDesign::Line`` datum only."""

    if isinstance(reference, Mapping) and "query" in reference:
        raise _fail("a pull direction must be a whole datum line, not a subshape")
    obj, native = _native_support(ctx, doc, reference, path=path, queries=queries)
    if native:
        raise _fail("a pull direction must be a whole datum line, not a subelement")
    if str(getattr(obj, "TypeId", "")) != "PartDesign::Line":
        raise _fail(f"pull direction '{getattr(obj, 'Name', '')}' is not a PartDesign::Line datum")
    return obj, [""]


def _resolve_originals(ctx: Any, doc: Any, body: Any, names: Any, limit: int) -> list[Any]:
    """Resolve same-Body source features, rejecting duplicates and patterns."""

    if not isinstance(names, (list, tuple)) or not names:
        raise _fail("originals must be a nonempty list of feature names")
    if len(names) > limit:
        raise _fail(f"at most {limit} originals are accepted")
    originals: list[Any] = []
    seen: set[str] = set()
    pattern_types = (
        "PartDesign::LinearPattern",
        "PartDesign::PolarPattern",
        "PartDesign::Mirrored",
        "PartDesign::MultiTransform",
    )
    for position, name in enumerate(names):
        if not isinstance(name, str) or not name:
            raise _fail(f"originals[{position}] must be a feature name")
        obj = ctx.require_object(doc, name)
        _require_member(body, obj)
        if name in seen:
            raise _fail(f"originals[{position}] duplicates '{name}'")
        if str(getattr(obj, "TypeId", "")) in pattern_types:
            raise _fail(f"originals[{position}] '{name}' is already a pattern or mirror")
        seen.add(name)
        originals.append(obj)
    return originals


def _resolve_sections(ctx: Any, doc: Any, body: Any, profile: Any, sections: Any) -> list[Any]:
    """Resolve ordered loft sections; each must be another same-Body profile."""

    if not isinstance(sections, (list, tuple)) or not sections:
        raise _fail("sections must be a nonempty list of profile names")
    if len(sections) > _contracts.MAX_LOFT_SECTIONS:
        raise _fail(f"at most {_contracts.MAX_LOFT_SECTIONS} sections are accepted")
    ordered: list[Any] = [profile]
    for position, name in enumerate(sections):
        if not isinstance(name, str) or not name:
            raise _fail(f"sections[{position}] must be a profile name")
        obj = _profile_object(ctx, doc, body, name)
        if obj is profile or obj in ordered:
            raise _fail(f"sections[{position}] '{name}' is a duplicate profile")
        ordered.append(obj)
    return ordered


def _resolve_binder_references(
    ctx: Any,
    doc: Any,
    references: Any,
    *,
    queries: _objects._PreparedQueries | None = None,
) -> list[tuple[Any, list[str]]]:
    """Resolve same-document binder references onto native link pairs.

    Query entries expand to their matched set and the expanded pair count
    is capped at the binder budget. Binder references may name objects
    outside the target Body — that is the feature's purpose — so no
    Body-membership check is applied here.
    """

    from . import geometry

    if not isinstance(references, (list, tuple)) or not references:
        raise _fail("references must be a nonempty list of canonical references")
    if len(references) > _contracts.MAX_BINDER_REFERENCES:
        raise _fail(f"at most {_contracts.MAX_BINDER_REFERENCES} references are accepted")
    links: list[tuple[Any, list[str]]] = []
    for position, entry in enumerate(references):
        path = f"references[{position}]"
        if isinstance(entry, Mapping) and "query" in entry:
            if queries is None:
                raise _fail(f"{path} requires prepared query resolution")
            obj, role, indices = queries.resolve(path, entry)
            if not indices:
                from . import geometry

                raise geometry.cardinality_error(
                    "selection_empty",
                    f"{path} matched no {role} of {obj.Name}",
                    path,
                    ctx,
                    doc,
                    obj,
                    role,
                    indices,
                )
            for index in indices:
                native = ("Face" if role == "face" else "Edge") + str(index)
                links.append((obj, [native]))
                if len(links) > _contracts.MAX_BINDER_REFERENCES:
                    raise _fail(
                        f"references expand past the {_contracts.MAX_BINDER_REFERENCES} pair budget"
                    )
        else:
            obj, native = geometry.resolve_reference(ctx, doc, entry, path)
            links.append((obj, [str(native)]))
        if getattr(obj, "Document", None) is not doc:
            raise _fail("subshape_binder accepts same-document references only")
    return links


def _expand_base_subelements(
    ctx: Any,
    doc: Any,
    base_obj: Any,
    role: str,
    references: Any,
    *,
    path_prefix: str,
    queries: _objects._PreparedQueries | None = None,
) -> list[str]:
    """Resolve and expand one dress-up subelement list onto native labels.

    Query entries expand to their full matched set (1..limit same-base final
    references); every entry is validated before the first assignment and
    the expanded label count is capped. Resolution runs through the prepared
    context so the selection happens once per operation.
    """

    from . import geometry

    if not isinstance(references, (list, tuple)) or not references:
        raise _fail(f"subelements must be a nonempty list of signed {role} references")
    limit = _contracts.MAX_BASE_FACES if role == "face" else _contracts.MAX_BASE_EDGES
    limit = min(limit, _contracts.MAX_SUBELEMENTS)
    labels: list[str] = []
    seen: set[str] = set()
    for position, entry in enumerate(references):
        path = f"{path_prefix}[{position}]"
        if isinstance(entry, Mapping) and "query" in entry:
            if queries is None:
                raise _fail(f"{path} requires prepared query resolution")
            obj, resolved_role, indices = queries.resolve(path, entry)
            if obj is not base_obj:
                raise _fail(f"{path} targets '{obj.Name}' but the base is '{base_obj.Name}'")
            if resolved_role != role:
                raise _fail(f"{path} must select {role} entries, got {resolved_role}")
            if not indices:
                from . import geometry

                raise geometry.cardinality_error(
                    "selection_empty",
                    f"{path} matched no {role} of {obj.Name}",
                    path,
                    ctx,
                    doc,
                    obj,
                    role,
                    indices,
                )
            for index in indices:
                labels.append(("Face" if role == "face" else "Edge") + str(index))
        else:
            obj, native = geometry.resolve_reference(ctx, doc, entry, path)
            if obj is not base_obj:
                raise _fail(f"{path} targets '{obj.Name}' but the base is '{base_obj.Name}'")
            if not native.startswith("Face" if role == "face" else "Edge"):
                raise _fail(f"{path} must be a signed {role} token")
            labels.append(native)
        if labels[-1] in seen:
            raise _fail(f"{path} duplicates {labels[-1]}")
        seen.add(labels[-1])
        if len(labels) > limit:
            raise _fail(f"subelements expand past the {limit} {role} reference limit")
    return labels


def _reverify_query_selections(
    doc: Any,
    queries: _objects._PreparedQueries,
    paths: list[tuple[str, Mapping]],
) -> None:
    """Re-run the named queries and demand identical selections.

    After ``_apply_base_list`` deliberately touches and recomputes the base
    feature, a query-origin selection must still name the same native
    subshapes: the object, role and index set are re-resolved, and the
    per-index document-space fingerprints are compared against the
    selection-time snapshot. Matching indices alone are insufficient; the
    recompute legitimately regenerates equivalent subshapes as new native
    objects, so identity is proven from geometry (which compares equal)
    rather than from the ``isSame`` object handle. Any changed or
    unreadable geometry refuses with ``selection_changed`` inside the
    mutation gate instead of binding a newly selected edge.
    """

    from . import geometry

    for path, value in paths:
        obj, role, indices = queries.reverify(path, value)
        if not indices:
            raise ToolError(
                VALIDATION_FAILED,
                f"{path} no longer matches any {role} after base normalization",
                {"reason": "selection_changed", "parameter": path},
            )
        snapshot = queries.fingerprints_for(path)
        try:
            fresh = geometry.subshape_fingerprints(geometry.placed_shape(obj), role, indices)
        except Exception as exc:
            raise ToolError(
                VALIDATION_FAILED,
                f"subshape fingerprint failed while re-verifying {path}: {exc}",
                {"reason": "selection_changed", "parameter": path},
            ) from exc
        for index in indices:
            original = snapshot.get(index) if snapshot else None
            candidate = fresh.get(index) if fresh else None
            if original is None or candidate is None:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"{path} selected a different {role} {index} after base "
                    "normalization; its geometry is unreadable, so the "
                    "mutation was refused",
                    {"reason": "selection_changed", "parameter": path},
                )
            if not geometry.fingerprints_match(original, candidate):
                raise ToolError(
                    VALIDATION_FAILED,
                    f"{path} selected a different {role} {index} after base "
                    "normalization; the dress-up reference would bind new "
                    "geometry, so the mutation was refused",
                    {"reason": "selection_changed", "parameter": path},
                )


def _apply_base_list(
    ctx: Any,
    doc: Any,
    feature: Any,
    kind: str,
    parameters: Mapping[str, Any],
    *,
    queries: _objects._PreparedQueries | None = None,
) -> list[str]:
    """Apply a bounded shared-target subelement list onto a dress-up's Base."""

    role = _contracts.BASE_KINDS[kind]
    base = parameters.get("base")
    references = parameters.get("subelements")
    if not isinstance(base, Mapping) or not base:
        raise _fail("base must be a whole-object canonical reference")
    if base.get("subelement") or "query" in base:
        raise ToolError(
            VALIDATION_FAILED,
            "base must be a whole-object canonical reference; a selected subshape is not accepted",
            {"reason": "subshape_not_allowed"},
        )
    if not isinstance(references, (list, tuple)) or not references:
        raise _fail(f"subelements must be a nonempty list of signed {role} references")
    from . import geometry

    base_obj, _base_role, base_native = geometry._resolve_reference_parts(ctx, doc, base, "base")
    if base_native:
        raise _fail(
            "base must be a whole-object canonical reference; a signed subelement is not accepted"
        )
    labels = _expand_base_subelements(
        ctx, doc, base_obj, role, references, path_prefix="subelements", queries=queries
    )
    query_paths = [
        (f"subelements[{position}]", entry)
        for position, entry in enumerate(references)
        if isinstance(entry, Mapping) and "query" in entry
    ]
    touch = getattr(base_obj, "touch", None)
    if callable(touch):
        # A base feature that has executed only once (its creation) carries
        # an element map that does not survive its next re-execution: a
        # dress-up bound to that map fails with "Invalid edge link" the
        # first time the base is edited, whatever tool performs the edit.
        # Force one re-execution now so the reference binds to a map that
        # has already been regenerated.
        touch()
        doc.recompute()
    if query_paths and queries is not None:
        _reverify_query_selections(doc, queries, query_paths)
    if not _objects._property_exists(feature, "Base"):
        raise _fail(f"feature '{getattr(feature, 'Name', '')}' exposes no Base property")
    feature.Base = (base_obj, labels)
    return [f"base->Base({len(labels)} {role})"]


def _apply_semantic_references(
    ctx: Any,
    doc: Any,
    body: Any,
    feature: Any,
    kind: str,
    parameters: Mapping[str, Any],
    *,
    queries: _objects._PreparedQueries | None = None,
) -> list[str]:
    """Apply the kind's reference parameters as native link values.

    Every shared-target parameter resolves through the operation's prepared
    context: preflight resolved each query once and this applier consumes
    the cached selection-time result.
    """

    if kind in _contracts.BASE_KINDS and ("base" in parameters or "subelements" in parameters):
        applied = _apply_base_list(ctx, doc, feature, kind, parameters, queries=queries)
    else:
        applied = []
    if kind in ("pad", "pocket") and "face" in parameters:
        value = parameters["face"]
        face_obj, face_native = _native_support(ctx, doc, value, path="face", queries=queries)
        if face_native and not face_native.startswith("Face"):
            raise _fail(f"the up_to_face reference must be a signed face token, got {face_native}")
        if not face_native and str(getattr(face_obj, "TypeId", "")) not in (
            "App::Plane",
            "PartDesign::Plane",
        ):
            raise _fail(f"up_to_face '{face_obj.Name}' is neither a plane nor a signed face")
        if not _objects._property_exists(feature, "UpToFace"):
            raise _fail(f"feature '{getattr(feature, 'Name', '')}' exposes no UpToFace property")
        feature.UpToFace = (face_obj, [face_native] if face_native else [])
        applied.append("face->UpToFace")
    for name, (prop, form) in _contracts.REFERENCE_PROPERTIES.get(kind, {}).items():
        if name not in parameters:
            continue
        value = parameters[name]
        if form == "originals":
            originals = _resolve_originals(
                ctx, doc, body, value, _contracts.MAX_PATTERN_ORIGINALS_SEMANTIC
            )
            setattr(
                feature,
                prop,
                originals,
            )
            suffix = f"({len(originals)})" if kind == "multi_transform" else ""
            applied.append(f"{name}->{prop}{suffix}")
            continue
        if form == "sections":
            ordered = _resolve_sections(ctx, doc, body, getattr(feature, "Profile", None), value)
            setattr(feature, prop, ordered[1:])
            applied.append(f"{name}->{prop}")
            continue
        if form == "spine":
            setattr(feature, prop, _profile_object(ctx, doc, body, value))
            applied.append(f"{name}->{prop}")
            continue
        if form == "binder_support":
            links = _resolve_binder_references(ctx, doc, value, queries=queries)
            if not _objects._property_exists(feature, prop):
                raise _fail(f"feature '{feature.Name}' exposes no {prop} property")
            feature.Support = links
            applied.append(f"{name}->{prop}({len(links)})")
            continue
        if form == "origin_axis":
            applied.extend(_apply_datum_line_axis(feature, body, value))
            continue
        if form == "axis":
            _check_axis_form(kind, value)
            obj, subs = _resolve_axis(ctx, doc, value, path=name, queries=queries)
        elif form == "mirror_plane":
            obj, subs = _resolve_plane(
                ctx, doc, value, allow_sketch_axes=True, path=name, queries=queries
            )
        elif form == "plane":
            obj, subs = _resolve_plane(ctx, doc, value, path=name, queries=queries)
        elif form == "line":
            obj, subs = _resolve_line(ctx, doc, value, path=name, queries=queries)
        else:
            raise _fail(f"kind '{kind}' has an unsupported reference form '{form}'")
        if not _objects._property_exists(feature, prop):
            raise _fail(f"feature '{feature.Name}' exposes no {prop} property")
        setattr(feature, prop, (obj, subs))
        applied.append(f"{name}->{prop}")
    return applied


def _preflight_semantic_references(
    ctx: Any,
    doc: Any,
    body: Any,
    kind: str,
    parameters: Mapping[str, Any],
    profile_obj: Any = None,
    *,
    queries: _objects._PreparedQueries | None = None,
) -> None:
    """Resolve every reference parameter before the transaction opens.

    Query-bearing values resolve exactly once here, through the prepared
    context (cached selections plus reserved receipts); the appliers inside
    the mutation consume the cached results. The resolution is deterministic
    and read-only, so a bad reference, a foreign Body member or an
    over-limit list never opens a transaction.
    """

    if not parameters:
        return
    if kind in _contracts.BASE_KINDS and ("base" in parameters or "subelements" in parameters):
        role = _contracts.BASE_KINDS[kind]
        base = parameters.get("base")
        value = parameters.get("subelements")
        if not isinstance(base, Mapping) or not base:
            raise _fail("base must be a whole-object canonical reference")
        if base.get("subelement") or "query" in base:
            raise ToolError(
                VALIDATION_FAILED,
                "base must be a whole-object canonical reference; a selected "
                "subshape is not accepted",
                {"reason": "subshape_not_allowed"},
            )
        if not isinstance(value, (list, tuple)) or not value:
            raise _fail(f"subelements must be a nonempty list of signed {role} references")
        from . import geometry

        base_obj, _base_role, base_native = geometry._resolve_reference_parts(
            ctx, doc, base, "base"
        )
        if base_native:
            raise _fail(
                "base must be a whole-object canonical reference; a signed "
                "subelement is not accepted"
            )
        _expand_base_subelements(
            ctx, doc, base_obj, role, value, path_prefix="subelements", queries=queries
        )
    elif kind in ("pad", "pocket") and "face" in parameters:
        face_obj, face_native = _native_support(
            ctx, doc, parameters["face"], path="face", queries=queries
        )
        if face_native and not face_native.startswith("Face"):
            raise _fail(f"the up_to_face reference must be a signed face token, got {face_native}")
        if not face_native and str(getattr(face_obj, "TypeId", "")) not in (
            "App::Plane",
            "PartDesign::Plane",
        ):
            raise _fail(f"up_to_face '{face_obj.Name}' is neither a plane nor a signed face")
    if kind == "multi_transform":
        for position, entry in enumerate(parameters.get("transformations") or ()):
            if not isinstance(entry, Mapping):
                raise _fail(f"transformations[{position}] must be an object")
            entry_kind = str(entry.get("kind", ""))
            if entry_kind == "mirrored":
                _resolve_plane(
                    ctx,
                    doc,
                    entry.get("plane"),
                    allow_sketch_axes=True,
                    path=f"transformations[{position}].plane",
                    queries=queries,
                )
            elif entry_kind in ("linear", "polar"):
                _resolve_axis(
                    ctx,
                    doc,
                    entry.get("axis"),
                    path=f"transformations[{position}].axis",
                    queries=queries,
                )
    for name, (_prop, form) in _contracts.REFERENCE_PROPERTIES.get(kind, {}).items():
        if name not in parameters:
            continue
        value = parameters[name]
        if form == "originals":
            _resolve_originals(ctx, doc, body, value, _contracts.MAX_PATTERN_ORIGINALS_SEMANTIC)
        elif form == "sections":
            _resolve_sections(ctx, doc, body, profile_obj, value)
        elif form == "spine":
            _profile_object(ctx, doc, body, value)
        elif form == "binder_support":
            _resolve_binder_references(ctx, doc, value, queries=queries)
        elif form == "origin_axis":
            _origin_axis(body, value)
        elif form == "axis":
            _check_axis_form(kind, value)
            _resolve_axis(ctx, doc, value, path=name, queries=queries)
        elif form == "mirror_plane":
            _resolve_plane(ctx, doc, value, allow_sketch_axes=True, path=name, queries=queries)
        elif form == "plane":
            _resolve_plane(ctx, doc, value, path=name, queries=queries)
        elif form == "line":
            _resolve_line(ctx, doc, value, path=name, queries=queries)


def _origin_axis(body: Any, value: Any) -> Any:
    """Return the Body's own origin axis named by ``x``/``y``/``z``."""

    expected = _contracts.ORIGIN_AXIS_NAMES.get(str(value))
    if expected is None:
        raise _fail("datum_line axis must be x, y or z")
    members = _origin_members(body)
    if members is None:
        raise _fail("datum_line requires a Body Origin container")
    axis = next((m for m in members if str(getattr(m, "Name", "")) == expected), None)
    if axis is None:
        raise _fail(f"body '{getattr(body, 'Name', '')}' has no local origin axis '{expected}'")
    return axis


def _apply_datum_line_axis(feature: Any, body: Any, value: Any) -> list[str]:
    """Attach a datum line to the Body's own origin axis."""

    axis = _origin_axis(body, value)
    expected = str(getattr(axis, "Name", ""))
    feature.AttachmentSupport = [(axis, "")]
    feature.MapMode = "TwoPointLine"
    return [f"axis->AttachmentSupport({expected})"]


def _apply_profile(feature: Any, profile_obj: Any, kind: str) -> None:
    """Assign the profile object to the feature's native Profile property."""
    if not _objects._property_exists(feature, "Profile"):
        raise _fail(
            f"feature '{feature.Name}' exposes no Profile property"
            + (
                " and no documented equivalent; refusing to guess a profile target"
                if kind == "hole"
                else ""
            )
        )
    feature.Profile = profile_obj


def _apply_attachment(feature: Any, support_obj: Any, native: str, map_mode: str) -> None:
    """Attach the support reference and map mode to the feature."""
    for prop in ("Support", "AttachmentSupport"):
        if _objects._property_exists(feature, prop):
            setattr(feature, prop, [(support_obj, native)])
            break
    else:
        raise _fail(f"feature '{feature.Name}' exposes neither Support nor AttachmentSupport")
    if not _objects._property_exists(feature, "MapMode"):
        raise _fail(f"feature '{feature.Name}' exposes no MapMode property")
    feature.MapMode = map_mode


#: Sub-parameters each hole cut form requires on the wire.
_HOLE_CUT_REQUIREMENTS = {
    "counterbore": ("counterbore_diameter", "counterbore_depth"),
    "countersink": ("countersink_diameter", "countersink_angle"),
    "counterdrill": ("countersink_diameter", "counterbore_depth", "countersink_angle"),
}

_HOLE_CUT_FIELDS = frozenset(
    {"counterbore_diameter", "counterbore_depth", "countersink_diameter", "countersink_angle"}
)

_MODE_FIELDS = {
    "primitive": {
        "box": {"length", "width", "height", "mode", "shape"},
        "cylinder": {"radius", "height", "mode", "shape"},
        "cone": {"radius1", "radius2", "height", "mode", "shape"},
        "sphere": {"radius", "mode", "shape"},
        "prism": {"polygon", "circumradius", "height", "mode", "shape"},
        "torus": {"radius1", "radius2", "mode", "shape"},
        "ellipsoid": {"radius1", "radius2", "radius3", "mode", "shape"},
        "wedge": {
            "x_min",
            "x_max",
            "y_min",
            "y_max",
            "z_min",
            "z_max",
            "x2_min",
            "x2_max",
            "z2_min",
            "z2_max",
            "mode",
            "shape",
        },
    },
    "helix": {
        "pitch_height": {
            "axis",
            "helix_mode",
            "mode",
            "pitch",
            "height",
            "angle",
            "left_handed",
            "reversed",
        },
        "pitch_turns": {
            "axis",
            "helix_mode",
            "mode",
            "pitch",
            "turns",
            "angle",
            "left_handed",
            "reversed",
        },
        "height_turns": {
            "axis",
            "helix_mode",
            "mode",
            "height",
            "turns",
            "angle",
            "left_handed",
            "reversed",
        },
        "height_growth": {
            "axis",
            "helix_mode",
            "mode",
            "height",
            "growth",
            "angle",
            "left_handed",
            "reversed",
        },
    },
}

#: Helix driver pairs: one dimension and one extent driver per mode.
_HELIX_DRIVERS = {
    "pitch_height": ("pitch", "height"),
    "pitch_turns": ("pitch", "turns"),
    "height_turns": ("height", "turns"),
    "height_growth": ("height", "growth"),
}

#: Required scalar parameters per primitive shape; the remaining native
#: properties keep their recorded defaults.
_PRIMITIVE_REQUIRED = {
    "box": ("length", "width", "height"),
    "cylinder": ("radius", "height"),
    "cone": ("radius1", "radius2", "height"),
    "sphere": ("radius",),
    "prism": ("polygon", "circumradius", "height"),
    "torus": ("radius1", "radius2"),
    "ellipsoid": ("radius1", "radius2", "radius3"),
    "wedge": ("x2_min", "x2_max", "z2_min", "z2_max"),
}


def _check_hole_cut(cut: str, parameters: Mapping[str, Any]) -> None:
    """Require the cut form's sub-parameters and bound the countersink angle."""

    required = _HOLE_CUT_REQUIREMENTS.get(cut, ())
    applicable = {
        "none": set(),
        "counterbore": {"counterbore_diameter", "counterbore_depth"},
        "countersink": {"countersink_diameter", "countersink_angle"},
        "counterdrill": {"countersink_diameter", "counterbore_depth", "countersink_angle"},
    }.get(cut, set())
    ignored = sorted(_HOLE_CUT_FIELDS & set(parameters) - applicable)
    if ignored:
        raise _fail(
            f"parameter '{ignored[0]}' is not applicable to hole cut '{cut}'",
            {"reason": "inapplicable_parameter", "parameter": ignored[0]},
        )
    missing = [name for name in required if name not in parameters]
    if missing:
        raise _fail(f"hole cut {cut!r} requires {', '.join(missing)}")
    angle = parameters.get("countersink_angle")
    if "countersink_angle" in required and not isinstance(angle, Mapping):
        number = _contracts.finite_number(parameters.get("countersink_angle"))
        if number is None or number <= 0 or number > 180.0:
            raise _fail("countersink_angle must be a finite angle in (0, 180] degrees")


def _check_semantic_parameters(
    kind: str, parameters: Mapping[str, Any], *, partial: bool = False
) -> None:
    """Handler-side kind checks that outlive the wire schema."""

    schema = _SEMANTIC_PARAM_SCHEMAS.get(kind)
    if schema is None:
        if parameters:
            raise _fail(f"kind '{kind}' accepts no semantic parameters")
        return
    # Semantic-only kinds have no raw-property fallback, so their required
    # fields are enforced even when the request carried no parameters at all;
    # a missing mapping must never open a transaction on native defaults.
    if kind not in _KIND_TYPES and not parameters and not partial:
        raise _fail(
            f"kind '{kind}' requires parameters: {', '.join(sorted(schema.get('required', ())))}"
        )
    if not parameters:
        # The original five kinds keep their raw ``properties`` contract: a
        # request without semantic parameters is legitimate for them.
        return
    allowed = set(schema["properties"])
    if partial and kind == "hole":
        allowed.add("thread_size")
    unknown = sorted(set(parameters) - allowed)
    if unknown:
        raise _fail(f"kind '{kind}' does not accept parameters: {', '.join(unknown)}")
    missing = sorted(set(schema.get("required", ())) - set(parameters)) if not partial else []
    if missing:
        if kind == "helix" and "mode" in missing:
            raise _fail("mode 'additive' or 'subtractive' is required")
        raise _fail(f"kind '{kind}' requires parameters: {', '.join(missing)}")

    if kind in ("pad", "pocket") and "extent" in parameters:
        extent = parameters["extent"]
        if extent == "through_all" and kind == "pad":
            raise _fail("through_all is available for pocket only")
        if extent == "distance" and "length" not in parameters and not partial:
            raise _fail("distance extent requires a length")
        if extent != "distance" and "length" in parameters:
            raise _fail(f"{extent} extent does not accept a length")
        if extent != "distance" and "symmetric" in parameters:
            raise _fail("symmetric is available for the distance extent only")
        if extent == "up_to_face" and "face" not in parameters:
            raise _fail("up_to_face extent requires a face reference")
        if extent != "up_to_face" and "face" in parameters:
            raise _fail(f"{extent} extent does not accept a face reference")
        length = parameters.get("length")
        if length is not None and not isinstance(length, Mapping):
            number = _contracts.finite_number(length)
            if number is None or number <= 0:
                raise _fail("length must be a positive finite number or an expression")
    if kind in ("loft", "pipe"):
        mode = parameters.get("mode")
        if mode is not None and mode not in ("additive", "subtractive"):
            raise _fail("mode 'additive' or 'subtractive' is required")
    if kind in _contracts.BASE_KINDS and "subelements" in parameters:
        subelements = parameters["subelements"]
        if not isinstance(subelements, (list, tuple)) or not subelements:
            raise _fail("subelements must be a nonempty list")
    if kind == "loft" and "sections" in parameters:
        sections = parameters["sections"]
        if isinstance(sections, (list, tuple)) and len(sections) > _contracts.MAX_LOFT_SECTIONS:
            raise _fail(f"sections must contain at most {_contracts.MAX_LOFT_SECTIONS} entries")
    if kind == "helix" and "mode" not in parameters and not partial:
        raise _fail("mode 'additive' or 'subtractive' is required")
    if kind == "hole":
        for name in ("diameter", "depth"):
            raw = parameters.get(name)
            if raw is None:
                continue
            if isinstance(raw, Mapping):
                continue
            number = _contracts.finite_number(raw)
            if number is None or number <= 0:
                raise _fail(f"hole {name} must be a positive finite number or an expression")
        depth_type = parameters.get("depth_type", "dimension")
        if depth_type == "dimension" and "depth" not in parameters and not partial:
            raise _fail("hole requires depth for a dimension extent")
        if depth_type == "through_all" and "depth" in parameters:
            raise _fail("through_all ignores depth; remove the depth parameter")
        cut = parameters.get("cut", "none")
        if "cut" in parameters or _HOLE_CUT_FIELDS.intersection(parameters):
            _check_hole_cut(cut, parameters)
        thread = parameters.get("thread")
        if thread is not None and thread != "none" and "thread_size" not in parameters:
            raise _fail(f"hole thread {thread!r} requires thread_size")
        if (
            "thread_size" in parameters
            and not (partial and thread is None)
            and (thread is None or thread == "none")
        ):
            raise _fail("thread_size requires a thread")
    if kind == "helix":
        if "helix_mode" not in parameters:
            if partial:
                return
            raise _fail("helix requires helix_mode")
        drivers = _HELIX_DRIVERS[parameters["helix_mode"]]
        missing = [name for name in drivers if name not in parameters]
        if missing:
            raise _fail(f"helix_mode {parameters['helix_mode']!r} requires {', '.join(missing)}")
        for name in ("pitch", "height", "turns", "growth"):
            if name in parameters and name not in drivers:
                raise _fail(f"helix_mode {parameters['helix_mode']!r} does not accept {name!r}")
        _check_positive_scalar("pitch", parameters)
        _check_positive_scalar("height", parameters)
        raw_turns = parameters.get("turns")
        if raw_turns is not None and not isinstance(raw_turns, Mapping):
            number = _contracts.finite_number(raw_turns)
            if number is None or number <= 0 or number > _contracts.MAX_HELIX_TURNS:
                raise _fail(f"turns must be a finite number in (0, {_contracts.MAX_HELIX_TURNS:g}]")
        raw_angle = parameters.get("angle")
        if raw_angle is not None and not isinstance(raw_angle, Mapping):
            number = _contracts.finite_number(raw_angle)
            if number is None or number < -80.0 or number > 80.0:
                raise _fail("angle must be a finite angle in [-80, 80] degrees")
        raw_growth = parameters.get("growth")
        if raw_growth is not None and not isinstance(raw_growth, Mapping):
            number = _contracts.finite_number(raw_growth)
            if number is None or number < 0:
                raise _fail("growth must be a non-negative finite number")
    if kind == "primitive":
        shape = parameters.get("shape")
        missing = [name for name in _PRIMITIVE_REQUIRED.get(shape, ()) if name not in parameters]
        if missing:
            raise _fail(f"primitive shape {shape!r} requires {', '.join(missing)}")
        for name in ("length", "width", "height", "radius", "radius2", "radius3", "circumradius"):
            _check_positive_scalar(name, parameters)
        if shape != "cone":
            _check_positive_scalar("radius1", parameters)
        if shape == "wedge":
            x2_min = _contracts.finite_number(parameters.get("x2_min"))
            x2_max = _contracts.finite_number(parameters.get("x2_max"))
            if x2_min is not None and x2_max is not None and x2_min > x2_max:
                raise _fail("wedge requires x2_min <= x2_max")
            z2_min = _contracts.finite_number(parameters.get("z2_min"))
            z2_max = _contracts.finite_number(parameters.get("z2_max"))
            if z2_min is not None and z2_max is not None and z2_min > z2_max:
                raise _fail("wedge requires z2_min <= z2_max")
    if kind == "multi_transform":
        for position, entry in enumerate(parameters.get("transformations") or ()):
            if not isinstance(entry, Mapping):
                raise _fail(f"transformations[{position}] must be an object")
            entry_kind = str(entry.get("kind", ""))
            required = {
                "mirrored": ("plane",),
                "linear": ("axis", "length", "count"),
                "polar": ("axis", "count"),
            }.get(entry_kind)
            if required is None:
                raise _fail(
                    f"transformations[{position}] kind {entry_kind!r} requires "
                    "one of 'mirrored', 'linear', 'polar'"
                )
            missing = [name for name in required if name not in entry]
            if missing:
                raise _fail(
                    f"transformations[{position}] kind {entry_kind!r} requires {', '.join(missing)}"
                )
            for name in ("length", "angle"):
                if isinstance(entry.get(name), Mapping):
                    raise _fail("transformation scalars accept numbers only")
            if entry_kind == "linear" and not isinstance(entry.get("length"), Mapping):
                number = _contracts.finite_number(entry.get("length"))
                if number is None or number <= 0:
                    raise _fail(
                        f"transformations[{position}] length must be a positive finite number"
                    )
            if entry_kind == "polar":
                raw_angle = entry.get("angle")
                if raw_angle is not None and not isinstance(raw_angle, Mapping):
                    number = _contracts.finite_number(raw_angle)
                    if number is None or number <= 0 or number > 360.0:
                        raise _fail(
                            f"transformations[{position}] angle must be a finite "
                            "angle in (0, 360] degrees"
                        )
    if kind == "scaled":
        _check_positive_scalar("factor", parameters)
    if kind == "fillet":
        _check_positive_scalar("radius", parameters)
    if kind == "chamfer":
        _check_positive_scalar("size", parameters)
    if kind == "thickness":
        _check_positive_scalar("thickness", parameters)
    if kind in ("revolve", "groove"):
        _check_angle("angle", parameters, 360.0)
    if kind == "draft":
        _check_angle("angle", parameters, 45.0)
    if kind == "linear_pattern":
        _check_positive_scalar("length", parameters)
    if kind == "polar_pattern":
        _check_angle("angle", parameters, 360.0)
    if kind == _GEAR_PROFILE_KIND and (not partial or {"teeth", "module"}.issubset(parameters)):
        _contracts.check_gear_bounds(parameters["teeth"], parameters["module"])


def _validate_semantic_branch(
    kind: str, parameters: Mapping[str, Any], *, partial: bool = False
) -> None:
    """Validate one selected semantic branch and return bounded reasons."""

    schema = _SEMANTIC_PARAM_SCHEMAS.get(kind)
    if schema is None:
        if parameters:
            raise _fail(
                f"kind '{kind}' accepts no semantic parameters",
                {"reason": "invalid_feature_parameters", "kind": kind, "errors": []},
            )
        return
    if not parameters and kind in _KIND_TYPES:
        return
    selected = copy.deepcopy(schema)
    if partial:
        selected.pop("required", None)
        if kind == "hole":
            selected.setdefault("properties", {})["thread_size"] = {
                "type": "string",
                "minLength": 1,
                "maxLength": 16,
            }
    # The branch is validated outside a full root schema, so resolve the
    # topology references from the shared local definitions explicitly.
    selected["$defs"] = {**_objects._VALUE_DEFS}
    try:
        _check_semantic_parameters(kind, parameters, partial=partial)
        validate_schema(dict(parameters), selected)
    except ProtocolError as exc:
        message = exc.message
        raise _fail(
            message,
            {
                "reason": "invalid_feature_parameters",
                "kind": kind,
                "errors": [message[:256]],
            },
        ) from None
    except ToolError as exc:
        details = dict(exc.details) if isinstance(exc.details, Mapping) else {}
        details.update({"reason": "invalid_feature_parameters", "kind": kind})
        details.setdefault("errors", [exc.message[:256]])
        raise _fail(exc.message, details) from None


def _reject_mode_inapplicable(kind: str, parameters: Mapping[str, Any]) -> None:
    """Reject semantic fields that the selected mode cannot consume."""

    selected: set[str] | None = None
    if kind == "primitive":
        selected = _MODE_FIELDS["primitive"].get(str(parameters.get("shape")))
    elif kind == "helix":
        selected = _MODE_FIELDS["helix"].get(str(parameters.get("helix_mode")))
    if selected is not None:
        for name in sorted(set(parameters) - selected):
            raise _fail(
                f"parameter '{name}' is not applicable to {kind} mode",
                {"reason": "inapplicable_parameter", "parameter": name},
            )


def _check_positive_scalar(name: str, parameters: Mapping[str, Any]) -> None:
    """Reject a non-positive numeric value; expressions are checked later."""

    raw = parameters.get(name)
    if raw is None or isinstance(raw, Mapping):
        return
    number = _contracts.finite_number(raw)
    if number is None or number <= 0:
        raise _fail(f"{name} must be a positive finite number or an expression")


def _check_angle(name: str, parameters: Mapping[str, Any], upper: float) -> None:
    """Reject an angle outside ``(0, upper]``; expressions checked later."""

    raw = parameters.get(name)
    if raw is None or isinstance(raw, Mapping):
        return
    number = _contracts.finite_number(raw)
    if number is None or number <= 0 or number > upper:
        raise _fail(f"{name} must be a finite angle in (0, {upper:g}] degrees")


def _has_expression(feature: Any, prop: str) -> bool:
    """True when ``prop`` currently carries a native expression binding.

    Reads the object's ``ExpressionEngine`` so the numeric write below only
    clears a binding that actually exists.
    """

    engine = getattr(feature, "ExpressionEngine", None)
    if not engine or isinstance(engine, (str, bytes)):
        return False
    try:
        entries = list(engine)
    except TypeError:
        return False
    for entry in entries:
        if isinstance(entry, (tuple, list)):
            path = entry[0] if entry else None
        else:
            path = getattr(entry, "path", None)
        if path in (prop, f".{prop}"):
            return True
    return False


def _apply_semantic_parameters(
    ctx: Any,
    doc: Any,
    body: Any,
    feature: Any,
    kind: str,
    parameters: Mapping[str, Any],
    *,
    queries: _objects._PreparedQueries | None = None,
) -> list[str]:
    """Write semantic parameters onto their native properties.

    A number clears any prior expression and writes the numeric value; an
    ``{expression}`` object binds a native expression instead. Returns the
    applied ``"<semantic>=<native>"`` labels for the change summary.
    """

    applied: list[str] = []
    # Only the kind's scalar/flag/expression parameters are handled here;
    # reference parameters and the creation-only ``mode`` selector are
    # applied by their own resolvers.
    scalar_names = _contracts.SEMANTIC_PROPERTIES.get(kind, {})
    for name, value in parameters.items():
        if name not in scalar_names:
            continue
        if name == "symmetric":
            if not isinstance(value, bool):
                raise _fail("symmetric must be a boolean")
            _apply_side_type(feature, value)
            applied.append("symmetric->SideType")
            continue
        if name == "face":
            # ``face`` is a topology reference and is applied by
            # ``_apply_semantic_references`` so edit and create share the
            # prepared-query receipt path.
            continue
        if isinstance(value, Mapping):
            spec = _contracts.SEMANTIC_PROPERTIES.get(kind, {}).get(name)
            if spec is None:
                raise _fail(f"kind '{kind}' has no native mapping for '{name}'")
            prop = spec[0]
            if not _objects._property_exists(feature, prop):
                raise _fail(
                    f"feature '{getattr(feature, 'Name', '')}' exposes no native "
                    f"property '{prop}' for '{name}'"
                )
            expression = str(value.get("expression") or "")
            if not expression.strip():
                raise _fail(f"{name} expression must be a non-empty string")
            feature.setExpression(prop, expression)
            applied.append(f"{name}->expression:{prop}")
            continue
        mapping = _contracts.native_value(kind, name, value)
        if mapping is None:
            raise _fail(f"kind '{kind}' has no native mapping for '{name}'")
        prop, native = mapping
        if not _objects._property_exists(feature, prop):
            raise _fail(
                f"feature '{getattr(feature, 'Name', '')}' exposes no native "
                f"property '{prop}' for '{name}'"
            )
        if isinstance(value, list):
            raise _fail(f"{name} must be a scalar or an expression object")
        if _has_expression(feature, prop):
            # Clear only a real binding. Calling setExpression(prop, None)
            # on an unbound property touches the feature's expression state,
            # and the next recompute then regenerates the feature's element
            # map: a downstream dress-up edge reference fails to resolve
            # ("Invalid edge link") and the whole mutation rolls back even
            # though the property value itself is harmless.
            feature.setExpression(prop, None)
        setattr(feature, prop, native)
        applied.append(f"{name}->{prop}={native}")
    return applied


def _apply_side_type(feature: Any, symmetric: bool) -> None:
    """Set the native ``SideType`` from its live enumeration.

    The native 1.1.3 tests record only the one-sided form, so the symmetric
    entry is matched by its exact enumeration string. When the live
    enumeration does not offer it the request is refused rather than
    guessed by integer position.
    """

    if not _objects._property_exists(feature, "SideType"):
        if symmetric:
            raise _fail(
                f"feature '{getattr(feature, 'Name', '')}' exposes no SideType "
                "property; symmetric extents are unavailable"
            )
        return
    if not symmetric:
        feature.SideType = _contracts.SIDE_TYPE_ONE_SIDE
        return
    getter = getattr(feature, "getEnumerationsOfProperty", None)
    entries: list[str] = []
    if callable(getter):
        try:
            entries = [str(entry) for entry in (getter("SideType") or ())]
        except Exception:
            entries = []
    for candidate in (_contracts.SIDE_TYPE_SYMMETRIC, _contracts.SIDE_TYPE_TWO_SIDES):
        if candidate in entries:
            feature.SideType = candidate
            return
    raise _fail(
        "symmetric extents are unverified on this build: the live SideType "
        f"enumeration {entries} exposes no recorded symmetric entry"
    )


def _apply_semantic_attachment(
    ctx: Any, doc: Any, body: Any, feature: Any, kind: str, parameters: Mapping[str, Any]
) -> None:
    """Attach a semantic sketch or datum plane to a Body-local origin plane."""

    role = str(parameters.get("plane"))
    plane = _origin_plane(body, role)
    map_mode = "ObjectOrigin" if kind == "datum_point" else "FlatFace"
    _apply_attachment(feature, plane, "", map_mode)
    offset = parameters.get("offset")
    if offset is None:
        return
    if isinstance(offset, Mapping):
        expression = str(offset.get("expression") or "")
        if not expression.strip():
            raise _fail("offset expression must be a non-empty string")
        # AttachmentOffset is a placement: an expression binds its Z
        # component, and no numeric value is guessed for the rest.
        feature.setExpression("AttachmentOffset.Base.z", expression)
        return
    number = _contracts.finite_number(offset)
    if number is None:
        raise _fail("offset must be a finite number or an expression")
    import FreeCAD

    placement = FreeCAD.Placement()
    placement.Base = FreeCAD.Vector(0.0, 0.0, number)
    if _has_expression(feature, "AttachmentOffset.Base.z"):
        # Same rule as the scalar writes: only clear a binding that exists.
        feature.setExpression("AttachmentOffset.Base.z", None)
    feature.AttachmentOffset = placement


def _apply_hole_parameters(
    feature: Any, parameters: Mapping[str, Any], *, pin: bool = True
) -> list[str]:
    """Pin the hole contract and apply the exposed cut/depth/thread forms.

    The tool does not expose every native hole field, so unexposed fields
    are pinned to the recorded defaults. ``ThreadType``, ``HoleCutType``
    and ``DepthType`` are pinned only when the caller did not supply the
    corresponding semantic parameter, so a caller value always wins.
    ``ThreadSize`` is verified against the live enumeration after
    ``ThreadType`` is set, because the native build populates the size
    list only then.
    """

    cut = parameters.get("cut")
    depth_type = parameters.get("depth_type")
    thread = parameters.get("thread")
    if pin:
        # Creation-only: pinning on an edit would reset values the request
        # never mentioned, including an existing thread definition.
        pinned: list[tuple[str, Any]] = [
            ("DrillPoint", "Flat"),
            ("Tapered", False),
            ("ModelThread", False),
            ("DrillForDepth", False),
            ("UseCustomThreadClearance", False),
            ("ThreadDirection", "Right"),
            ("Threaded", False),
        ]
        if cut is None:
            pinned.append(("HoleCutType", 0))
        if depth_type is None:
            pinned.append(("DepthType", 0))
        if thread is None:
            pinned.append(("ThreadType", 0))
        for prop, value in pinned:
            if _objects._property_exists(feature, prop):
                setattr(feature, prop, value)
    for name, semantic in (("cut", cut), ("depth_type", depth_type), ("thread", thread)):
        if semantic is None:
            continue
        prop, native = _contracts.native_value("hole", name, semantic)
        if _objects._property_exists(feature, prop):
            setattr(feature, prop, native)
    thread_active = thread is not None and thread != "none"
    if not pin and thread is None and "thread_size" in parameters:
        current_thread = getattr(feature, "ThreadType", None)
        thread_active = current_thread not in (None, "", 0, "0", "None")
    if not thread_active:
        return []
    if thread is not None and _objects._property_exists(feature, "Threaded"):
        feature.Threaded = True
    thread_size = parameters.get("thread_size")
    if thread_size is None:
        return []
    getter = getattr(feature, "getEnumerationsOfProperty", None)
    entries: list[str] = []
    if callable(getter):
        try:
            entries = [str(entry) for entry in (getter("ThreadSize") or ())]
        except Exception:
            entries = []
    if not entries or entries == ["---"]:
        raise _fail("thread sizes cannot be verified on this build")
    if thread_size not in entries:
        raise _fail(
            f"thread size {thread_size!r} is not in the live ThreadSize enumeration",
            {"valid": entries[:10], "validCount": len(entries)},
        )
    if _objects._property_exists(feature, "ThreadSize"):
        feature.ThreadSize = thread_size
        return [f"thread_size->ThreadSize={thread_size}"]
    return []


def _apply_gear_flags(feature: Any) -> None:
    """Pin the external, standard-precision gear contract.

    Both flags are native properties the tool does not expose: the native
    proxy defaults to an internal, high-precision gear, so every created
    profile states the supported combination explicitly. A proxy without
    the flags cannot prove this contract and is refused.
    """

    for prop, expected in (("ExternalGear", True), ("HighPrecision", False)):
        if not _objects._property_exists(feature, prop):
            raise _fail(
                f"gear '{getattr(feature, 'Name', '')}' exposes no {prop} "
                "property; the installed InvoluteGearFeature is unsupported"
            )
        setattr(feature, prop, expected)


def _create_gear_profile(ctx: Any, doc: Any, body: Any, requested_name: str) -> Any:
    """Create the native involute gear proxy through its public factory."""

    try:
        from InvoluteGearFeature import makeInvoluteGear
    except Exception as exc:
        raise _fail(
            "native_extension_unavailable: the installed InvoluteGearFeature "
            f"module cannot be imported: {exc}"
        ) from exc

    # The factory attaches through the GUI active context
    # (``ActiveView.getActiveObject("pdbody")`` and ``("part")``), so the
    # target document, the target Body and the part context are activated
    # first and restored after — including a prior value of None.
    previous_document = ctx.App.ActiveDocument
    previous_gui_document = None
    previous_body = None
    previous_part = None
    try:
        previous_gui_document = ctx.Gui.ActiveDocument
        view = previous_gui_document.ActiveView if previous_gui_document else None
        previous_body = view.getActiveObject("pdbody") if view is not None else None
        previous_part = view.getActiveObject("part") if view is not None else None
    except Exception:
        previous_gui_document = None
        previous_body = None
        previous_part = None

    ctx.App.setActiveDocument(doc.Name)
    try:
        try:
            ctx.Gui.setActiveDocument(doc.Name)
            gui_document = ctx.Gui.getDocument(doc.Name)
            gui_document.ActiveView.setActiveObject("pdbody", body)
            gui_document.ActiveView.setActiveObject("part", None)
        except Exception as exc:
            raise _fail(
                "native_extension_unavailable: the target Body could not be "
                f"made active for the gear factory: {exc}"
            ) from exc
        gear = makeInvoluteGear(requested_name)
    except Exception as exc:
        raise _fail(
            f"native_extension_unavailable: the installed InvoluteGearFeature factory failed: {exc}"
        ) from exc
    finally:
        if previous_gui_document is not None:
            try:
                ctx.Gui.setActiveDocument(previous_gui_document.Name)
                gui_document = ctx.Gui.getDocument(previous_gui_document.Name)
                # Restore None too: leaving the target Body active would let a
                # later factory attach to the wrong container.
                gui_document.ActiveView.setActiveObject("pdbody", previous_body)
                gui_document.ActiveView.setActiveObject("part", previous_part)
            except Exception:
                pass
        if previous_document is not None:
            ctx.App.setActiveDocument(previous_document.Name)

    if getattr(gear, "Document", None) is not doc:
        raise _fail(
            "native_extension_unavailable: the gear factory created the "
            "profile in a different document"
        )
    members = _body_members(body)
    if members is not None and gear not in members:
        for other in doc.Objects:
            if other is body:
                continue
            other_members = _body_members(other)
            if other_members is not None and gear in other_members:
                raise _fail(
                    "native_extension_unavailable: the gear factory attached "
                    f"the profile to body '{getattr(other, 'Name', '')}'"
                )
        body.addObject(gear)
    return gear


# ---------------------------------------------------------------------------
# Handlers.
# ---------------------------------------------------------------------------


def _expected_body_expectation(arguments: Mapping[str, Any], tolerance: float) -> dict[str, Any]:
    """Collect the caller's expected_solids/expected_bounds into a gate expectation."""
    expectation: dict[str, Any] = {}
    if arguments.get("expected_solids") is not None:
        expectation["expected_solids"] = arguments["expected_solids"]
    if arguments.get("expected_bounds") is not None:
        expectation["expected_bounds"] = arguments["expected_bounds"]
        expectation["bounds_tolerance"] = tolerance
    return expectation


def _mutation_label(action: str, body: Any, feature: Any = None) -> str:
    """Build the '<action>:<body>[:<feature>]' mutation label."""
    suffix = f":{getattr(feature, 'Name', '')}" if feature is not None else ""
    return f"{action}:{getattr(body, 'Name', '')}{suffix}"


def _checkpoint_if_expensive(ctx: Any, doc: Any, label: str, expensive: bool) -> None:
    """Create one verified recovery copy when recovery is enabled.

    Runs before the transaction opens: a failed copy refuses the mutation
    instead of proceeding unverified. The document is checked idle first so
    a busy document (for example one with a running FEM solve) is rejected
    before an unstable copy is captured.
    """

    if not expensive:
        return
    settings = getattr(ctx, "settings", None) or {}
    if not bool(settings.get("recovery_enabled", False)):
        return
    ctx.check_document_idle(doc)
    ctx.checkpoint_before_mutation(doc, label)


def _create_multi_transform(
    ctx: Any,
    doc: Any,
    body: Any,
    requested_name: str,
    parameters: Mapping[str, Any],
    created: list[Any],
    expectations: dict[str, dict],
    *,
    queries: _objects._PreparedQueries | None = None,
) -> Any:
    """Create the parent composite and its child transformations in order.

    Children carry no Originals — the parent drives them, as recorded by
    the native MultiTransform test. The parent is appended to ``created``
    first, so the Tip assertion and the returned object identity name the
    composite rather than a child.
    """

    transformation_types = {
        "mirrored": "PartDesign::Mirrored",
        "linear": "PartDesign::LinearPattern",
        "polar": "PartDesign::PolarPattern",
    }
    children: list[Any] = []
    for index, entry in enumerate(parameters["transformations"]):
        entry_kind = str(entry["kind"])
        child = body.newObject(transformation_types[entry_kind], f"{requested_name}Tf{index}")
        if entry_kind == "mirrored":
            obj, subs = _resolve_plane(
                ctx,
                doc,
                entry["plane"],
                allow_sketch_axes=True,
                path=f"transformations[{index}].plane",
                queries=queries,
            )
            child.MirrorPlane = (obj, subs)
        elif entry_kind == "linear":
            obj, subs = _resolve_axis(
                ctx, doc, entry["axis"], path=f"transformations[{index}].axis", queries=queries
            )
            child.Direction = (obj, subs)
            child.Length = float(entry["length"])
            child.Occurrences = int(entry["count"])
        else:
            obj, subs = _resolve_axis(
                ctx, doc, entry["axis"], path=f"transformations[{index}].axis", queries=queries
            )
            child.Axis = (obj, subs)
            child.Occurrences = int(entry["count"])
            child.Angle = float(entry.get("angle", 360.0))
        children.append(child)
    feature = body.newObject("PartDesign::MultiTransform", requested_name)
    feature.Transformations = children
    created.append(feature)
    created.extend(children)
    expectations.setdefault(str(feature.Name), {})
    return feature


def _create_feature(ctx: Any, arguments: dict) -> dict:
    """Handle create_feature: validated PartDesign feature creation in one mutation."""
    doc = ctx.require_document(arguments["document"])
    body = ctx.require_object(doc, str(arguments["body"]))
    _require_body(body)
    kind = str(arguments["kind"])
    requested_name = str(arguments["name"])
    properties = dict(arguments.get("properties") or {})
    parameters = dict(arguments.get("parameters") or {})
    if properties and parameters:
        raise _fail("create_feature accepts either properties or parameters, not both")
    if kind == _GEAR_PROFILE_KIND and properties:
        raise _fail(
            "gear_profile accepts typed parameters only; raw properties are "
            "not available for this kind"
        )
    if properties and kind not in _KIND_TYPES:
        raise _fail(
            f"kind '{kind}' accepts typed parameters only; raw properties are "
            "not available for this kind"
        )
    _validate_semantic_branch(kind, parameters)
    _reject_mode_inapplicable(kind, parameters)
    if arguments.get("profile") is not None and kind not in _PROFILE_KINDS:
        raise _fail(
            "profile is not applicable to this feature kind",
            {"reason": "inapplicable_parameter", "parameter": "profile"},
        )
    bounds_tolerance = arguments.get("bounds_tolerance")
    if bounds_tolerance is None:
        bounds_tolerance = _objects._DEFAULT_BOUNDS_TOLERANCE
    detail = str(arguments.get("response_detail") or "compact")
    # Prepared query context: every shared-target reference in parameters
    # resolves once before the checkpoint and the transaction.
    queries = _objects._PreparedQueries(ctx, doc)

    if kind == "datum_plane":
        type_id = _datum_type_id(doc)
        if type_id is None:
            raise _fail(
                "core datum-plane creation is unavailable: the document does "
                "not support PartDesign::Plane; no generic substitute is "
                "created"
            )
    elif kind == "primitive":
        shape = str(parameters.get("shape", ""))
        mode = str(parameters.get("mode", ""))
        type_id = _contracts.PRIMITIVE_TYPES.get((shape, mode))
        if type_id is None:
            raise _fail(
                f"primitive requires a known shape/mode pair; got {shape!r}/{mode!r}",
                {"knownShapes": list(_contracts.PRIMITIVE_SHAPES)},
            )
    elif kind in _contracts.MODE_KIND_TYPES:
        mode = str(parameters.get("mode", ""))
        type_id = _contracts.MODE_KIND_TYPES[kind].get(mode)
        if type_id is None:
            raise _fail(f"kind '{kind}' requires mode 'additive' or 'subtractive'")
    else:
        type_id = _contracts.KIND_TYPES[kind]

    profile_obj = None
    if kind in _PROFILE_KINDS:
        if arguments.get("profile") is None:
            raise _fail(f"kind '{kind}' requires a profile object")
        profile_obj = _profile_object(ctx, doc, body, arguments["profile"])

    support_obj = None
    native_support = ""
    if arguments.get("support") is not None:
        if "MapMode" not in properties:
            raise _fail(
                "a support attachment requires an explicit properties.MapMode; "
                "this tool never invents an attachment mode"
            )
        if kind in _SUPPORT_KINDS and "plane" in parameters:
            raise _fail(
                "a signed support reference and a semantic plane are "
                "mutually exclusive attachment targets"
            )
        support_obj, native_support = _native_support(
            ctx, doc, arguments["support"], path="support", queries=queries
        )
    elif "MapMode" in properties:
        raise _fail("properties.MapMode requires a support reference")

    expectations: dict[str, dict] = {}
    body_expectation = _expected_body_expectation(arguments, float(bounds_tolerance))
    if body_expectation:
        expectations[body.Name] = body_expectation

    created: list[Any] = []
    outcome: dict[str, Any] = {}
    applied_semantic: list[str] = []

    def _validate_tip() -> None:
        """Assert the Body's post-recompute Tip names the created feature."""
        if not created:
            return
        tip = getattr(body, "Tip", None)
        tip_name = str(getattr(tip, "Name", "")) or None
        if kind == _GEAR_PROFILE_KIND:
            if tip_name == str(created[0].Name):
                raise _fail(
                    f"body '{body.Name}' Tip must not become the gear profile",
                    {"reason": "profile_became_tip", "bodyTip": tip_name},
                )
            return
        if kind not in _TIP_KINDS:
            return
        if tip_name != str(created[0].Name):
            raise _fail(
                f"body '{body.Name}' Tip is {tip_name!r} after recompute, "
                f"but the created {kind} is '{created[0].Name}'",
                {"reason": "tip_mismatch", "bodyTip": tip_name},
            )

    # Raw link properties carrying a query resolve before the transaction
    # (property metadata exists only after newObject, but the SELECTION does
    # not depend on the created feature), so a stated generation is checked
    # once at operation start and the receipt budget is reserved up front.
    for prop, value in properties.items():
        queries.scan_property(prop, value)

    label = _mutation_label("create_feature", body)
    # Resolve every reference before the checkpoint and the transaction, so
    # a stale token, a foreign Body member or an over-limit list never opens
    # one and never costs a recovery copy.
    _preflight_semantic_references(ctx, doc, body, kind, parameters, profile_obj, queries=queries)
    # Ordinary pads, pockets, holes and gear profiles are not checkpointed;
    # the expensive dress-up/pattern/loft/pipe kinds are.
    _checkpoint_if_expensive(ctx, doc, label, kind in _contracts.EXPENSIVE_KINDS)

    with mutation(
        ctx,
        doc,
        label,
        lambda: [body, *created],
        expectations=expectations,
        outcome=outcome,
        check_workload=_contracts.check_workload,
        validate_after_recompute=_validate_tip,
    ) as applied:
        if kind == _GEAR_PROFILE_KIND:
            feature = _create_gear_profile(ctx, doc, body, requested_name)
            created.append(feature)
            expectations.setdefault(str(feature.Name), {})
        elif kind == "multi_transform":
            feature = _create_multi_transform(
                ctx,
                doc,
                body,
                requested_name,
                parameters,
                created,
                expectations,
                queries=queries,
            )
            applied_semantic.extend(
                f"transformations[{index}]->{child.Name}" for index, child in enumerate(created[1:])
            )
        else:
            feature = body.newObject(type_id, requested_name)
            created.append(feature)
            expectations.setdefault(str(feature.Name), {})

        if profile_obj is not None:
            _apply_profile(feature, profile_obj, kind)
        remaining = dict(properties)
        if support_obj is not None:
            _apply_attachment(feature, support_obj, native_support, properties["MapMode"])
            remaining.pop("MapMode", None)
        if remaining:
            prepared = _objects._prepare_properties(ctx, doc, feature, remaining, queries=queries)
            _objects._apply_prepared(feature, prepared)
        if parameters:
            # datum_point states its plane on the wire; the primitive plane
            # is optional, so its absence skips the attachment entirely.
            if kind in _SUPPORT_KINDS and "plane" in parameters:
                _apply_semantic_attachment(ctx, doc, body, feature, kind, parameters)
                applied_semantic.append("plane->AttachmentSupport")
                offset = parameters.get("offset")
                if offset is not None:
                    applied_semantic.append("offset->AttachmentOffset")
            if kind not in ("sketch", "datum_plane", "datum_point"):
                if kind == "hole":
                    applied_semantic.extend(_apply_hole_parameters(feature, parameters))
                applied_semantic.extend(
                    _apply_semantic_references(
                        ctx, doc, body, feature, kind, parameters, queries=queries
                    )
                )
                applied_semantic.extend(
                    _apply_semantic_parameters(
                        ctx, doc, body, feature, kind, parameters, queries=queries
                    )
                )
                if kind == _GEAR_PROFILE_KIND:
                    # The native proxy defaults to internal, high-precision
                    # gears; this contract is external profiles at the
                    # standard precision, and neither flag is exposed.
                    _apply_gear_flags(feature)
        elif kind == "hole":
            _apply_hole_parameters(feature, {})
        elif kind == _GEAR_PROFILE_KIND:
            _apply_gear_flags(feature)
        if kind in _TIP_KINDS:
            # FreeCAD does not always advance the Tip to a newly added
            # feature (observed for patterns), so the solid contract is
            # stated explicitly: the created feature is the Body result.
            tip = getattr(body, "Tip", None)
            if str(getattr(tip, "Name", "")) != str(feature.Name):
                body.Tip = feature

    feature = created[0]
    report = outcome["reports"][str(feature.Name)]
    body_report = outcome["reports"][str(body.Name)]
    after_rows = _objects._snapshot_requested(feature, properties, ctx=ctx, doc=doc)
    result: dict[str, Any] = {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": {
            "name": str(getattr(feature, "Name", "")),
            "label": _objects._label(feature),
            "typeId": str(getattr(feature, "TypeId", "")),
        },
        "body": {
            "name": str(getattr(body, "Name", "")),
            "label": _objects._label(body),
            "typeId": str(getattr(body, "TypeId", "")),
        },
        "bodyTip": str(getattr(getattr(body, "Tip", None), "Name", "")) or None,
        "bodyReport": body_report,
        "change": _objects._change_summary(
            [
                {"name": name, "before": None, "after": _objects._jsonify(value)}
                for name, value in after_rows
            ],
            detail=detail,
            solid_before=None,
            solid_after=report["solid_count"],
            volume_before=None,
            volume_after=report["volume"],
            bounds_before=None,
            bounds_after=document_bounds(feature),
            dependents_before=outcome.get("dependentCountBefore", 0),
            dependents=outcome.get("dependentCountAfter", 0),
        ),
        "applied": [*applied, *applied_semantic],
    }
    if queries.receipts:
        result["resolvedSelections"] = queries.receipts
    return result


def _editable_kind(feature: Any) -> str:
    """Map a feature's TypeId to its editable kind, refusing unsupported types."""
    type_id = str(getattr(feature, "TypeId", ""))
    if (
        type_id == "Part::Part2DObjectPython"
        and str(getattr(getattr(feature, "Proxy", None), "Type", "")) != "InvoluteGear"
    ):
        raise _fail(
            f"object '{feature.Name}' is not a trusted native gear feature; "
            "use edit_object for its native properties",
            {
                "typeId": type_id,
                "supportedKinds": list(_EDITABLE_KIND_NAMES),
                "nextTool": "edit_object",
            },
        )
    kind = _EDITABLE_FEATURE_KINDS.get(type_id)
    if kind is None:
        supported = ", ".join(_EDITABLE_KIND_NAMES)
        raise _fail(
            f"object '{feature.Name}' of type '{type_id}' is not editable by "
            f"edit_feature; supported kinds are {supported}; use edit_object "
            "for native properties",
            {
                "typeId": type_id,
                "supportedKinds": list(_EDITABLE_KIND_NAMES),
                "nextTool": "edit_object",
            },
        )
    return kind


def _check_edit_gear_bounds(feature: Any, parameters: Mapping[str, Any]) -> None:
    """Bound the gear against the values the edit actually produces."""

    raw_teeth = parameters.get("teeth", getattr(feature, "NumberOfTeeth", None))
    if isinstance(raw_teeth, Mapping):
        raw_teeth = getattr(feature, "NumberOfTeeth", None)
    raw_module = parameters.get("module", None)
    if raw_module is None:
        raw_module = _contracts.quantity_mm(getattr(feature, "Modules", None))
    _contracts.check_gear_bounds(raw_teeth, raw_module)


#: Native scalar parameters that must stay strictly positive, per kind,
#: checked against the value the edit actually produces.
_POSITIVE_EDIT_PARAMS = {
    "pad": ("length",),
    "pocket": ("length",),
    "hole": ("diameter", "depth"),
    "fillet": ("radius",),
    "chamfer": ("size",),
    "linear_pattern": ("length",),
}

#: Edited kinds whose requested angle resolves in degrees inside (0, 360].
_ANGLE_EDIT_PARAMS = {
    "revolve": ("angle",),
    "polar_pattern": ("angle",),
}

#: Edited pattern kinds whose count must resolve to an integer in 2..32.
_COUNT_EDIT_PARAMS = {
    "linear_pattern": ("count",),
    "polar_pattern": ("count",),
}


def _check_edit_hole_parameters(feature: Any, parameters: Mapping[str, Any]) -> None:
    """Hole combination rules an edit request must satisfy by itself.

    Unlike a creation, an edit starts from the feature's current values, so
    the dimension-extent depth rule is create-only. What an edit must
    still state coherently is every form it changes: a cut form carries
    its sub-parameters, a thread change carries its size, and a size
    without a thread companion is refused when the feature does not
    thread yet.
    """

    if parameters.get("depth_type") == "through_all" and "depth" in parameters:
        raise _fail("through_all ignores depth; remove the depth parameter")
    cut = parameters.get("cut")
    if cut is not None:
        _check_hole_cut(cut, parameters)
    thread = parameters.get("thread")
    if thread is not None and thread != "none" and "thread_size" not in parameters:
        raise _fail(f"hole thread {thread!r} requires thread_size")
    if "thread_size" in parameters and thread == "none":
        raise _fail("thread_size requires a thread")
    if "thread_size" in parameters and thread is None:
        current = str(getattr(feature, "ThreadType", "None") or "None")
        if current in ("", "None"):
            raise _fail("thread_size requires a thread")


#: Per-kind semantic parameter names an edit may address, plus the
#: hole-only thread_size wire special case. Validating against this set
#: (not the schema union) is what turns a wrong-kind parameter into an
#: actionable refusal naming the kind and its supported parameters.
_EDIT_KIND_PARAMETERS = {
    kind: sorted(names) for kind, names in _contracts.SEMANTIC_PROPERTIES.items()
}
_EDIT_KIND_PARAMETERS["hole"] = sorted({*_contracts.SEMANTIC_PROPERTIES["hole"], "thread_size"})


def _check_edit_parameter_names(kind: str, parameters: Mapping[str, Any]) -> None:
    """Refuse parameters the selected kind does not support."""

    supported = _EDIT_KIND_PARAMETERS.get(kind)
    if supported is None:
        return
    for name in sorted(parameters):
        if name not in supported:
            raise ToolError(
                VALIDATION_FAILED,
                f"parameter '{name}' is not supported by kind '{kind}'",
                {
                    "kind": kind,
                    "parameter": name,
                    "supportedParameters": supported,
                    "nextTool": "inspect_objects",
                },
            )


def _check_edit_scalar_ranges(kind: str, feature: Any, parameters: Mapping[str, Any]) -> None:
    """Reject a non-positive final scalar before the transaction opens."""

    for name in _POSITIVE_EDIT_PARAMS.get(kind, ()):
        raw = parameters.get(name)
        if raw is None:
            continue
        if isinstance(raw, Mapping):
            continue
        number = _contracts.finite_number(raw)
        if number is None or number <= 0:
            raise _fail(f"{name} must be a positive finite number or an expression")
    for name in _ANGLE_EDIT_PARAMS.get(kind, ()):
        _check_angle(name, parameters, 360.0)
    for name in _COUNT_EDIT_PARAMS.get(kind, ()):
        raw = parameters.get(name)
        if raw is None or isinstance(raw, Mapping):
            continue
        if isinstance(raw, bool) or not isinstance(raw, int) or not 2 <= raw <= 32:
            raise _fail("count must be an integer from 2 through 32")


def _read_native_expression(feature: Any, prop: str) -> str | None:
    """The persisted expression binding for ``prop``, or None.

    ``getExpression`` may answer a bare string or an ``(expression, path)``
    tuple depending on the binding; the ExpressionEngine scan is the
    fallback. A missing readback is never reported as a successful binding.
    """

    getter = getattr(feature, "getExpression", None)
    if callable(getter):
        try:
            raw = getter(prop)
        except Exception:
            raw = None
        if isinstance(raw, str) and raw:
            return raw
        if isinstance(raw, (list, tuple)) and raw and isinstance(raw[0], str) and raw[0]:
            return raw[0]
    engine = getattr(feature, "ExpressionEngine", None)
    if engine and not isinstance(engine, (str, bytes)):
        try:
            entries = list(engine)
        except TypeError:
            entries = []
        for entry in entries:
            if isinstance(entry, (tuple, list)):
                path, expression = entry[0], entry[1] if len(entry) > 1 else None
            else:
                path = getattr(entry, "path", None)
                expression = getattr(entry, "expression", None)
            if path in (prop, f".{prop}") and isinstance(expression, str) and expression:
                return expression
    return None


def _parameter_state(feature: Any, prop: str) -> dict:
    """One closed parameter state row: actual value plus live binding."""

    return {
        "value": _objects._jsonify(getattr(feature, prop, None)),
        "expression": _read_native_expression(feature, prop),
    }


def _restore_parameter_expressions(
    doc: Any,
    feature: Any,
    requested: list[tuple[str, str]],
    before_states: Mapping[str, Mapping[str, Any]],
) -> None:
    """Restore bindings that FreeCAD transaction abort does not preserve."""

    for name, prop in requested:
        expected = before_states[name]["expression"]
        if _read_native_expression(feature, prop) != expected:
            feature.setExpression(prop, expected)
    doc.recompute()


def _angle_degrees(feature: Any, prop: str) -> float | None:
    """Resolved angle in degrees from the native property.

    An Angle quantity answers ``getValueAs("deg").Value``; a bare quantity
    falls back to its ``.Value`` and a plain number passes through. Unit
    strings are never parsed.
    """

    raw = getattr(feature, prop, None)
    if raw is None:
        return None
    getter = getattr(raw, "getValueAs", None)
    if callable(getter):
        try:
            return _contracts.finite_number(getter("deg").Value)
        except Exception:
            pass
    value = getattr(raw, "Value", None)
    if value is not None:
        return _contracts.finite_number(value)
    return _contracts.finite_number(raw)


def _edit_feature(ctx: Any, arguments: dict) -> dict:
    """Handle edit_feature: validated semantic parameter edits in one mutation."""
    doc = ctx.require_document(arguments["document"])
    body = ctx.require_object(doc, str(arguments["body"]))
    _require_body(body)
    feature = ctx.require_object(doc, str(arguments["object"]))
    kind = _editable_kind(feature)
    _require_member(body, feature)
    expected_generation = arguments.get("expected_generation")
    if expected_generation is not None:
        actual_generation = int(ctx.document_generation(doc))
        if expected_generation != actual_generation:
            raise _fail(
                "feature changed since inspection; re-run inspect_objects",
                stale_generation_details(expected_generation, actual_generation, "inspect_objects"),
            )

    parameters = dict(arguments.get("parameters") or {})
    detail = str(arguments.get("response_detail") or "compact")
    if not parameters:
        raise _fail("parameters must contain at least one semantic value")
    unknown = sorted(
        set(parameters) - set(_EDIT_FEATURE_INPUT["properties"]["parameters"]["properties"])
    )
    if unknown:
        raise _fail(f"edit_feature does not accept parameters: {', '.join(unknown)}")
    _check_edit_parameter_names(kind, parameters)
    _validate_semantic_branch(kind, parameters, partial=True)
    _reject_mode_inapplicable(kind, parameters)
    if kind == _GEAR_PROFILE_KIND:
        _check_edit_gear_bounds(feature, parameters)
    if kind == "hole":
        _check_edit_hole_parameters(feature, parameters)
    _check_edit_scalar_ranges(kind, feature, parameters)

    # Resolve the native property of every requested parameter up front and
    # capture its state (actual value plus live expression binding) before
    # the mutation, so the post-state rows are complete readbacks.
    requested: list[tuple[str, str]] = []
    numeric: list[tuple[str, Any]] = []
    for name, raw in parameters.items():
        if kind == "hole" and name == "thread_size":
            prop = "ThreadSize"
        elif name == "face":
            prop = "UpToFace"
        else:
            probe = raw if not isinstance(raw, Mapping) else 0
            mapping = _contracts.native_value(kind, name, probe)
            if mapping is None:
                raise _fail(f"feature '{feature.Name}' has no native mapping for '{name}'")
            prop = mapping[0]
        if not _objects._property_exists(feature, prop):
            raise _fail(f"feature '{feature.Name}' exposes no '{prop}' parameter")
        requested.append((name, prop))
        if not isinstance(raw, Mapping):
            # The hole thread_size wire special case has no native_value
            # mapping; the raw string is a valid sole numeric write.
            numeric.append((name, raw))
    if not numeric and not any(isinstance(v, Mapping) for v in parameters.values()):
        raise _fail("at least one numeric parameter value is required")
    before_states = {name: _parameter_state(feature, prop) for name, prop in requested}
    before_report = geometry_report(feature)
    queries = _objects._PreparedQueries(ctx, doc)
    _preflight_semantic_references(ctx, doc, body, kind, parameters, queries=queries)

    expectations: dict[str, dict] = {}
    body_expectation = _expected_body_expectation(
        arguments, float(arguments.get("bounds_tolerance") or _objects._DEFAULT_BOUNDS_TOLERANCE)
    )
    if body_expectation:
        expectations[body.Name] = body_expectation
    outcome: dict[str, Any] = {}

    label = _mutation_label("edit_feature", body, feature)
    _checkpoint_if_expensive(ctx, doc, label, _contracts.expensive_feature_present([body, feature]))

    def _check_final_values() -> None:
        """Expressions can resolve anywhere; the final native value cannot."""

        for name in _POSITIVE_EDIT_PARAMS.get(kind, ()):
            if name not in parameters:
                continue
            prop = _contracts.SEMANTIC_PROPERTIES[kind][name][0]
            # Every name here is a length: a native length property reads
            # back as ``Base.Quantity``, not as a plain number, so the
            # magnitude needs the quantity reader.
            number = _contracts.quantity_mm(getattr(feature, prop, None))
            if number is None or number <= 0:
                raise _fail(f"{name} resolved to a non-positive value; the edit was refused")
        for name in _ANGLE_EDIT_PARAMS.get(kind, ()):
            if name not in parameters:
                continue
            prop = _contracts.SEMANTIC_PROPERTIES[kind][name][0]
            degrees = _angle_degrees(feature, prop)
            if degrees is None or not 0 < degrees <= 360.0:
                raise _fail(
                    f"{name} resolved to {degrees!r} degrees; the edit requires "
                    "a finite angle in (0, 360] degrees"
                )
        for name in _COUNT_EDIT_PARAMS.get(kind, ()):
            if name not in parameters:
                continue
            prop = _contracts.SEMANTIC_PROPERTIES[kind][name][0]
            count = getattr(feature, prop, None)
            if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 32:
                raise _fail(f"{name} resolved to {count!r}; the edit requires an integer in 2..32")

    try:
        with mutation(
            ctx,
            doc,
            label,
            [body, feature],
            expectations=expectations,
            outcome=outcome,
            check_workload=_contracts.check_workload,
            validate_after_recompute=_check_final_values,
        ) as applied:
            if kind == "hole":
                applied.extend(_apply_hole_parameters(feature, parameters, pin=False))
            _apply_semantic_references(ctx, doc, body, feature, kind, parameters, queries=queries)
            _apply_semantic_parameters(ctx, doc, body, feature, kind, parameters, queries=queries)
    except ToolError as exc:
        if (exc.details or {}).get("operationState") != "rolled_back":
            raise
        try:
            _restore_parameter_expressions(doc, feature, requested, before_states)
        except Exception as restore_exc:
            raise ToolError(
                VALIDATION_FAILED,
                f"{exc.message}; restoring the prior expression bindings also failed: "
                f"{type(restore_exc).__name__}: {restore_exc}",
                {
                    "operationState": "rollback_failed",
                    "rollbackFailed": True,
                    "rollbackStage": "restore_expressions",
                    "originalError": f"{type(exc).__name__}: {exc.message}",
                    "originalDetails": exc.details,
                },
            ) from restore_exc
        raise

    report = outcome["reports"][str(feature.Name)]
    body_report = outcome["reports"][str(body.Name)]
    rows = []
    for name, prop in requested:
        row: dict[str, Any] = {
            "parameter": name,
            "property": prop,
            "after": _parameter_state(feature, prop),
        }
        if detail == "full":
            row["before"] = before_states[name]
        rows.append(row)
    result: dict[str, Any] = {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": {
            "name": str(getattr(feature, "Name", "")),
            "label": _objects._label(feature),
            "typeId": str(getattr(feature, "TypeId", "")),
        },
        "body": {
            "name": str(getattr(body, "Name", "")),
            "label": _objects._label(body),
            "typeId": str(getattr(body, "TypeId", "")),
        },
        "bodyTip": str(getattr(getattr(body, "Tip", None), "Name", "")) or None,
        "bodyReport": body_report,
        "parameterValues": rows,
    }
    if detail == "full":
        result["geometryChange"] = {
            "solidCountBefore": before_report["solid_count"],
            "solidCountAfter": report["solid_count"],
            "volumeBefore": before_report["volume"],
            "volumeAfter": report["volume"],
            "boundsBefore": before_report["bounds"],
            "boundsAfter": document_bounds(feature),
        }
    if queries.receipts:
        result["resolvedSelections"] = queries.receipts
    return result


HANDLERS["create_feature"] = _create_feature
HANDLERS["edit_feature"] = _edit_feature
