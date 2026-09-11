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

from collections.abc import Callable, Mapping
from typing import Any

from ..object_validation import document_bounds, geometry_report, mutation
from ..protocol import VALIDATION_FAILED, ToolError, check_schema
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
)
_GEAR_PROFILE_KIND = "gear_profile"
_SUPPORT_KINDS = ("sketch", "datum_plane")
_ORIGIN_PLANE_ROLES = {"xy": "XY_Plane", "xz": "XZ_Plane", "yz": "YZ_Plane"}

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
                "sketchAxis": {"type": "string", "enum": ["H_Axis", "V_Axis", "N_Axis"]},
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
                "sketchAxis": {"type": "string", "enum": ["H_Axis", "V_Axis"]},
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
            "plane": {"type": "string", "enum": ["xy", "xz", "yz"]},
            "offset": _SCALAR_PARAM,
        },
    },
    "datum_plane": {
        "type": "object",
        "additionalProperties": False,
        "required": ["plane"],
        "properties": {
            "plane": {"type": "string", "enum": ["xy", "xz", "yz"]},
            "offset": _SCALAR_PARAM,
        },
    },
    "pad": {
        "type": "object",
        "additionalProperties": False,
        "required": ["extent"],
        "properties": {
            "extent": {"type": "string", "enum": ["distance", "up_to_face"]},
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
            "extent": {"type": "string", "enum": ["distance", "through_all", "up_to_face"]},
            "length": _SCALAR_PARAM,
            "face": _objects._CANONICAL_REF,
            "symmetric": {"type": "boolean"},
            "reversed": {"type": "boolean"},
        },
    },
    "hole": {
        "type": "object",
        "additionalProperties": False,
        "required": ["diameter", "depth"],
        "properties": {
            "diameter": _SCALAR_PARAM,
            "depth": _SCALAR_PARAM,
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
        "properties": {"axis": {"type": "string", "enum": ["x", "y", "z"]}},
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
            "mode": {"type": "string", "enum": ["additive", "subtractive"]},
            "ruled": {"type": "boolean"},
        },
    },
    "pipe": {
        "type": "object",
        "additionalProperties": False,
        "required": ["spine", "mode"],
        "properties": {
            "spine": _objects._NAME_FIELD,
            "mode": {"type": "string", "enum": ["additive", "subtractive"]},
        },
    },
}

_CREATE_FEATURE_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "body", "kind", "name"],
    "properties": {
        "document": _objects._DOCUMENT_FIELD,
        "body": _objects._NAME_FIELD,
        "kind": {
            "type": "string",
            "enum": [
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
            ],
        },
        "name": _objects._NAME_FIELD,
        "properties": _objects._PROPERTIES_MAP,
        "parameters": {"anyOf": list(_SEMANTIC_PARAM_SCHEMAS.values())},
        "profile": _PROFILE_REF,
        "support": _objects._CANONICAL_REF,
        "expected_solids": _objects._EXPECTED_SOLIDS,
        "expected_bounds": _objects._EXPECTED_BOUNDS,
        "bounds_tolerance": _objects._BOUNDS_TOLERANCE,
    },
    "$defs": _objects._VALUE_DEFS,
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
    },
    "$defs": {
        "geometryReport": _objects._GEOMETRY_REPORT,
        "objectIdentity": _objects._OBJECT_IDENTITY,
    },
}

_EDIT_FEATURE_INPUT = {
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
                "extent": {"type": "string", "enum": ["distance", "through_all", "up_to_face"]},
                "length": _SCALAR_PARAM,
                "diameter": _SCALAR_PARAM,
                "depth": _SCALAR_PARAM,
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
    },
}

_EDIT_FEATURE_OUTPUT = _CREATE_FEATURE_OUTPUT

TOOL_DEFINITIONS = [
    {
        "name": "create_feature",
        "description": (
            "Create one PartDesign feature inside an existing Body: "
            "datum_plane (PartDesign::Plane), datum_line (PartDesign::Line), "
            "sketch (Sketcher::SketchObject), pad, pocket, hole, revolve "
            "(Revolution), groove, fillet, chamfer, thickness, draft, "
            "linear_pattern, polar_pattern, mirrored, loft, pipe (each "
            "additive or subtractive where the kind offers both), or the "
            "involute gear_profile wire. The feature is created through "
            "body.newObject so Body membership and Tip are native; solid "
            "kinds require a profile, and dress-ups, patterns and lofts/pipes "
            "take signed subelement, originals, axis, plane or spine "
            "references. Semantic parameters map onto native properties "
            "(lengths in mm, angles in degrees) and accept either a number or "
            "an {expression} binding. Everything runs in one transaction; a "
            "property, profile, reference, Tip, bounds or geometry failure "
            "aborts and removes the feature."
        ),
        "inputSchema": _CREATE_FEATURE_INPUT,
        "outputSchema": _CREATE_FEATURE_OUTPUT,
    },
    {
        "name": "edit_feature",
        "description": (
            "Edit one existing scalar PartDesign feature that belongs to the "
            "named Body: pad/pocket extent and length, hole diameter/depth, "
            "or gear teeth/module/pressure angle. Parameters are validated "
            "against the feature before the transaction opens; numeric values "
            "are written natively and {expression} objects bind a native "
            "expression. The feature and its Body are recomputed and "
            "validated in one mutation, and the result reports the feature's "
            "actual before/after values and the Body's final geometry."
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
    return ToolError(VALIDATION_FAILED, message, details)


def _require_body(body: Any) -> None:
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
    if isinstance(profile, str):
        profile_obj = ctx.require_object(doc, profile)
    else:
        reference = dict(profile)
        subelement = reference.get("subelement") or ""
        if subelement:
            raise _fail(
                "profile must be a whole-object reference; signed subelement "
                "references are not accepted"
            )
        profile_obj = ctx.require_object(doc, str(reference.get("object")))
    _require_member(body, profile_obj)
    return profile_obj


def _native_support(ctx: Any, doc: Any, support: Mapping) -> tuple[Any, str]:
    """Resolve a signed support reference through the shared resolver."""

    from . import geometry

    return geometry.resolve_reference(ctx, doc, support)


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
        if str(getattr(member, "Name", "")) == expected:
            return member
    raise _fail(
        f"body '{getattr(body, 'Name', '')}' has no local origin plane "
        f"'{expected}'; semantic attachment is unavailable"
    )


def _resolve_axis(ctx: Any, doc: Any, reference: Any) -> tuple[Any, list[str]]:
    """Resolve an axis reference to a native ``LinkSub`` value.

    A canonical whole-object reference must name a native origin axis or a
    datum line; the closed ``{object, sketchAxis}`` form names a sketch and
    one of its axis literals. The two forms never mix.
    """

    if not isinstance(reference, Mapping):
        raise _fail("axis must be a canonical reference or a sketch-axis object")
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
    obj, native = _native_support(ctx, doc, reference)
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
    ctx: Any, doc: Any, reference: Any, *, allow_sketch_axes: bool = False
) -> tuple[Any, list[str]]:
    """Resolve a plane reference to a native ``LinkSub`` value."""

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
    obj, native = _native_support(ctx, doc, reference)
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


def _resolve_line(ctx: Any, doc: Any, reference: Any) -> tuple[Any, list[str]]:
    """Resolve a pull direction: a whole ``PartDesign::Line`` datum only."""

    obj, native = _native_support(ctx, doc, reference)
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


def _apply_base_list(
    ctx: Any, doc: Any, feature: Any, kind: str, parameters: Mapping[str, Any]
) -> list[str]:
    """Apply a bounded signed subelement list onto a dress-up's Base link."""

    role = _contracts.BASE_KINDS[kind]
    base = parameters.get("base")
    references = parameters.get("subelements")
    if not isinstance(base, Mapping) or not base:
        raise _fail("base must be a whole-object canonical reference")
    if base.get("subelement"):
        raise _fail(
            "base must be a whole-object canonical reference; a signed subelement is not accepted"
        )
    if not isinstance(references, (list, tuple)) or not references:
        raise _fail(f"subelements must be a nonempty list of signed {role} references")
    limit = _contracts.MAX_BASE_FACES if role == "face" else _contracts.MAX_BASE_EDGES
    if len(references) > limit:
        raise _fail(f"at most {limit} {role} references are accepted")
    from .geometry import resolve_reference_list

    base_obj, labels = resolve_reference_list(ctx, doc, base, list(references), role)
    if not _objects._property_exists(feature, "Base"):
        raise _fail(f"feature '{getattr(feature, 'Name', '')}' exposes no Base property")
    feature.Base = (base_obj, labels)
    return [f"base->Base({len(labels)} {role})"]


def _apply_semantic_references(
    ctx: Any, doc: Any, body: Any, feature: Any, kind: str, parameters: Mapping[str, Any]
) -> list[str]:
    """Apply the kind's reference parameters as native link values."""

    if kind in _contracts.BASE_KINDS:
        applied = _apply_base_list(ctx, doc, feature, kind, parameters)
    else:
        applied = []
    for name, (prop, form) in _contracts.REFERENCE_PROPERTIES.get(kind, {}).items():
        if name not in parameters:
            continue
        value = parameters[name]
        if form == "originals":
            setattr(
                feature,
                prop,
                _resolve_originals(
                    ctx, doc, body, value, _contracts.MAX_PATTERN_ORIGINALS_SEMANTIC
                ),
            )
            applied.append(f"{name}->{prop}")
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
        if form == "origin_axis":
            applied.extend(_apply_datum_line_axis(feature, body, value))
            continue
        if form == "axis":
            _check_axis_form(kind, value)
            obj, subs = _resolve_axis(ctx, doc, value)
        elif form == "mirror_plane":
            obj, subs = _resolve_plane(ctx, doc, value, allow_sketch_axes=True)
        elif form == "plane":
            obj, subs = _resolve_plane(ctx, doc, value)
        elif form == "line":
            obj, subs = _resolve_line(ctx, doc, value)
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
) -> None:
    """Resolve every reference parameter before the transaction opens.

    The resolution is deterministic and read-only, so doing it twice (here
    and again inside the mutation) costs nothing while ensuring a bad
    reference, a foreign Body member or an over-limit list never opens a
    transaction.
    """

    if not parameters:
        return
    if kind in _contracts.BASE_KINDS:
        role = _contracts.BASE_KINDS[kind]
        base = parameters.get("base")
        value = parameters.get("subelements")
        if not isinstance(base, Mapping) or not base:
            raise _fail("base must be a whole-object canonical reference")
        if base.get("subelement"):
            raise _fail(
                "base must be a whole-object canonical reference; a signed "
                "subelement is not accepted"
            )
        if not isinstance(value, (list, tuple)) or not value:
            raise _fail(f"subelements must be a nonempty list of signed {role} references")
        limit = _contracts.MAX_BASE_FACES if role == "face" else _contracts.MAX_BASE_EDGES
        if len(value) > limit:
            raise _fail(f"at most {limit} {role} references are accepted")
        from .geometry import resolve_reference_list

        resolve_reference_list(ctx, doc, base, list(value), role)
    elif kind in ("pad", "pocket") and "face" in parameters:
        face_obj, face_native = _native_support(ctx, doc, parameters["face"])
        if face_native and not face_native.startswith("Face"):
            raise _fail(f"the up_to_face reference must be a signed face token, got {face_native}")
        if not face_native and str(getattr(face_obj, "TypeId", "")) not in (
            "App::Plane",
            "PartDesign::Plane",
        ):
            raise _fail(f"up_to_face '{face_obj.Name}' is neither a plane nor a signed face")
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
        elif form == "origin_axis":
            _origin_axis(body, value)
        elif form == "axis":
            _check_axis_form(kind, value)
            _resolve_axis(ctx, doc, value)
        elif form == "mirror_plane":
            _resolve_plane(ctx, doc, value, allow_sketch_axes=True)
        elif form == "plane":
            _resolve_plane(ctx, doc, value)
        elif form == "line":
            _resolve_line(ctx, doc, value)


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
    for prop in ("Support", "AttachmentSupport"):
        if _objects._property_exists(feature, prop):
            setattr(feature, prop, [(support_obj, native)])
            break
    else:
        raise _fail(f"feature '{feature.Name}' exposes neither Support nor AttachmentSupport")
    if not _objects._property_exists(feature, "MapMode"):
        raise _fail(f"feature '{feature.Name}' exposes no MapMode property")
    feature.MapMode = map_mode


def _check_semantic_parameters(kind: str, parameters: Mapping[str, Any]) -> None:
    """Handler-side kind checks that outlive the wire schema."""

    schema = _SEMANTIC_PARAM_SCHEMAS.get(kind)
    if schema is None:
        if parameters:
            raise _fail(f"kind '{kind}' accepts no semantic parameters")
        return
    # Semantic-only kinds have no raw-property fallback, so their required
    # fields are enforced even when the request carried no parameters at all;
    # a missing mapping must never open a transaction on native defaults.
    if kind not in _KIND_TYPES and not parameters:
        raise _fail(
            f"kind '{kind}' requires parameters: {', '.join(sorted(schema.get('required', ())))}"
        )
    if not parameters:
        # The original five kinds keep their raw ``properties`` contract: a
        # request without semantic parameters is legitimate for them.
        return
    unknown = sorted(set(parameters) - set(schema["properties"]))
    if unknown:
        raise _fail(f"kind '{kind}' does not accept parameters: {', '.join(unknown)}")
    missing = sorted(set(schema.get("required", ())) - set(parameters))
    if missing:
        raise _fail(f"kind '{kind}' requires parameters: {', '.join(missing)}")

    if kind in ("pad", "pocket"):
        extent = parameters["extent"]
        if extent == "through_all" and kind == "pad":
            raise _fail("through_all is available for pocket only")
        if extent == "distance" and "length" not in parameters:
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
    if kind == "hole":
        for name in ("diameter", "depth"):
            raw = parameters[name]
            if isinstance(raw, Mapping):
                continue
            number = _contracts.finite_number(raw)
            if number is None or number <= 0:
                raise _fail(f"hole {name} must be a positive finite number or an expression")
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
    if kind == _GEAR_PROFILE_KIND:
        _contracts.check_gear_bounds(parameters["teeth"], parameters["module"])


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


def _apply_semantic_parameters(
    ctx: Any,
    doc: Any,
    body: Any,
    feature: Any,
    kind: str,
    parameters: Mapping[str, Any],
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
            if not isinstance(value, Mapping) or "object" not in value:
                raise _fail("face must be a canonical reference object")
            face_obj, face_native = _native_support(ctx, doc, value)
            _require_member(body, face_obj)
            if face_native and not face_native.startswith("Face"):
                raise _fail(
                    f"the up_to_face reference must be a signed face token, got {face_native}"
                )
            if not _objects._property_exists(feature, "UpToFace"):
                raise _fail(
                    f"feature '{getattr(feature, 'Name', '')}' exposes no UpToFace property"
                )
            feature.UpToFace = (face_obj, [face_native])
            applied.append(f"{name}->UpToFace")
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
    _apply_attachment(feature, plane, "", "FlatFace")
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
    feature.setExpression("AttachmentOffset.Base.z", None)
    feature.AttachmentOffset = placement


def _apply_hole_defaults(feature: Any) -> None:
    """Pin the plain cylindrical finite-depth hole contract."""

    for prop, value in (
        ("ThreadType", 0),
        ("HoleCutType", 0),
        ("DepthType", 0),
        ("DrillPoint", 0),
        ("Tapered", False),
    ):
        if _objects._property_exists(feature, prop):
            setattr(feature, prop, value)


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
    expectation: dict[str, Any] = {}
    if arguments.get("expected_solids") is not None:
        expectation["expected_solids"] = arguments["expected_solids"]
    if arguments.get("expected_bounds") is not None:
        expectation["expected_bounds"] = arguments["expected_bounds"]
        expectation["bounds_tolerance"] = tolerance
    return expectation


def _mutation_label(action: str, body: Any, feature: Any = None) -> str:
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


def _create_feature(ctx: Any, arguments: dict) -> dict:
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
    bounds_tolerance = arguments.get("bounds_tolerance")
    if bounds_tolerance is None:
        bounds_tolerance = _objects._DEFAULT_BOUNDS_TOLERANCE

    if kind == "datum_plane":
        type_id = _datum_type_id(doc)
        if type_id is None:
            raise _fail(
                "core datum-plane creation is unavailable: the document does "
                "not support PartDesign::Plane; no generic substitute is "
                "created"
            )
    elif kind in _contracts.MODE_KIND_TYPES:
        mode = str(parameters.get("mode", ""))
        type_id = _contracts.MODE_KIND_TYPES[kind].get(mode)
        if type_id is None:
            raise _fail(f"kind '{kind}' requires mode 'additive' or 'subtractive'")
    else:
        type_id = _contracts.KIND_TYPES[kind]

    _check_semantic_parameters(kind, parameters)

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
        support_obj, native_support = _native_support(ctx, doc, arguments["support"])
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

    label = _mutation_label("create_feature", body)
    # Resolve every reference before the checkpoint and the transaction, so
    # a stale token, a foreign Body member or an over-limit list never opens
    # one and never costs a recovery copy.
    _preflight_semantic_references(ctx, doc, body, kind, parameters, profile_obj)
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
            prepared = _objects._prepare_properties(ctx, doc, feature, remaining)
            _objects._apply_prepared(feature, prepared)
        if parameters:
            if kind in _SUPPORT_KINDS:
                _apply_semantic_attachment(ctx, doc, body, feature, kind, parameters)
                offset = parameters.get("offset")
                if offset is not None:
                    applied_semantic.append("offset->AttachmentOffset")
            else:
                if kind == "hole":
                    _apply_hole_defaults(feature)
                applied_semantic.extend(
                    _apply_semantic_references(ctx, doc, body, feature, kind, parameters)
                )
                applied_semantic.extend(
                    _apply_semantic_parameters(ctx, doc, body, feature, kind, parameters)
                )
                if kind == _GEAR_PROFILE_KIND:
                    # The native proxy defaults to internal, high-precision
                    # gears; this contract is external profiles at the
                    # standard precision, and neither flag is exposed.
                    _apply_gear_flags(feature)
        elif kind == "hole":
            _apply_hole_defaults(feature)
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
    after_rows = _objects._snapshot_requested(feature, properties)
    return {
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


def _editable_type_id(feature: Any) -> str:
    type_id = str(getattr(feature, "TypeId", ""))
    if type_id == "Part::Part2DObjectPython":
        if str(getattr(getattr(feature, "Proxy", None), "Type", "")) != "InvoluteGear":
            raise _fail(f"object '{feature.Name}' is not a trusted native gear feature")
        return type_id
    if type_id not in ("PartDesign::Pad", "PartDesign::Pocket", "PartDesign::Hole"):
        raise _fail(f"object '{feature.Name}' is not an editable scalar feature")
    return type_id


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
}


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


def _edit_feature(ctx: Any, arguments: dict) -> dict:
    doc = ctx.require_document(arguments["document"])
    body = ctx.require_object(doc, str(arguments["body"]))
    _require_body(body)
    feature = ctx.require_object(doc, str(arguments["object"]))
    _editable_type_id(feature)
    _require_member(body, feature)
    expected_generation = arguments.get("expected_generation")
    if expected_generation is not None and expected_generation != int(ctx.document_generation(doc)):
        raise _fail("feature changed since inspection; re-run inspect_objects")

    parameters = dict(arguments.get("parameters") or {})
    if not parameters:
        raise _fail("parameters must contain at least one semantic value")
    kind = _semantic_kind(feature)
    unknown = sorted(
        set(parameters) - set(_EDIT_FEATURE_INPUT["properties"]["parameters"]["properties"])
    )
    if unknown:
        raise _fail(f"edit_feature does not accept parameters: {', '.join(unknown)}")
    if kind == _GEAR_PROFILE_KIND:
        _check_edit_gear_bounds(feature, parameters)
    _check_edit_scalar_ranges(kind, feature, parameters)

    numeric: list[tuple[str, Any]] = []
    for name, raw in parameters.items():
        mapping = _contracts.native_value(kind, name, raw if not isinstance(raw, Mapping) else 0)
        if mapping is None:
            raise _fail(f"feature '{feature.Name}' has no native mapping for '{name}'")
        prop, native = mapping
        if not _objects._property_exists(feature, prop):
            raise _fail(f"feature '{feature.Name}' exposes no '{prop}' parameter")
        if isinstance(raw, Mapping):
            continue
        numeric.append((name, native))
    if not numeric and not any(isinstance(v, Mapping) for v in parameters.values()):
        raise _fail("at least one numeric parameter value is required")
    before = {
        name: getattr(feature, _contracts.SEMANTIC_PROPERTIES[kind][name][0], None)
        for name, _ in numeric
    }
    before_report = geometry_report(feature)

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
            number = _contracts.finite_number(getattr(feature, prop, None))
            if number is None or number <= 0:
                raise _fail(f"{name} resolved to a non-positive value; the edit was refused")

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
        _apply_semantic_parameters(ctx, doc, body, feature, kind, parameters)

    report = outcome["reports"][str(feature.Name)]
    rows = []
    for name, _ in numeric:
        prop = _contracts.SEMANTIC_PROPERTIES[kind][name][0]
        before_value = _objects._jsonify(before[name])
        after_value = _objects._jsonify(getattr(feature, prop, None))
        if before_value != after_value:
            rows.append({"name": name, "before": before_value, "after": after_value})
    return {
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
        "bodyReport": outcome["reports"][str(body.Name)],
        "change": _objects._change_summary(
            rows,
            solid_before=before_report["solid_count"],
            solid_after=report["solid_count"],
            volume_before=before_report["volume"],
            volume_after=report["volume"],
            bounds_before=before_report["bounds"],
            bounds_after=document_bounds(feature),
            dependents_before=outcome.get("dependentCountBefore", 0),
            dependents=outcome.get("dependentCountAfter", 0),
        ),
        "applied": applied,
    }


def _semantic_kind(feature: Any) -> str:
    """Map a feature TypeId back onto the semantic contract it follows."""

    type_id = str(getattr(feature, "TypeId", ""))
    if type_id == "Part::Part2DObjectPython":
        return _GEAR_PROFILE_KIND
    return {
        "PartDesign::Pad": "pad",
        "PartDesign::Pocket": "pocket",
        "PartDesign::Hole": "hole",
    }[type_id]


HANDLERS["create_feature"] = _create_feature
HANDLERS["edit_feature"] = _edit_feature
