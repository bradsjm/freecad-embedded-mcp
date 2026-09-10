"""``create_feature``: Body-aware PartDesign feature construction.

One semantic operation creates a datum plane, sketch, pad, pocket or hole
inside an existing ``PartDesign::Body``, wires the profile and attachment
support, applies properties and validates the Body's final geometry — all
inside one shared mutation so any failure removes the created feature
through transaction abort. Body membership and Tip are established by
FreeCAD itself; this tool never guesses factory names or attachment modes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..object_validation import document_bounds, geometry_report, mutation
from ..protocol import VALIDATION_FAILED, ToolError, check_schema
from . import objects as _objects

#: Exact TypeIds per requested kind (FreeCAD 1.1 registered types only).
_KIND_TYPES = {
    "sketch": "Sketcher::SketchObject",
    "pad": "PartDesign::Pad",
    "pocket": "PartDesign::Pocket",
    "hole": "PartDesign::Hole",
    "datum_plane": "PartDesign::Plane",
}

#: Ordered datum candidates; only the core ``PartDesign::Plane`` is accepted.
_DATUM_TYPE_CANDIDATES = ("PartDesign::Plane", "PartDesign::FeaturePython")

_PROFILE_KINDS = ("pad", "pocket", "hole")

_PROFILE_REF = {
    "anyOf": [
        {"type": "string", "minLength": 1},
        _objects._CANONICAL_REF,
    ]
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
            "enum": ["datum_plane", "sketch", "pad", "pocket", "hole"],
        },
        "name": _objects._NAME_FIELD,
        "properties": _objects._PROPERTIES_MAP,
        "profile": _PROFILE_REF,
        "support": _objects._CANONICAL_REF,
        "expected_solids": _objects._EXPECTED_SOLIDS,
        "expected_bounds": _objects._EXPECTED_BOUNDS,
        "bounds_tolerance": _objects._BOUNDS_TOLERANCE,
    },
    "$defs": _objects._VALUE_DEFS,
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
    },
    "$defs": {
        "geometryReport": _objects._GEOMETRY_REPORT,
        "objectIdentity": _objects._OBJECT_IDENTITY,
    },
}

TOOL_DEFINITIONS = [
    {
        "name": "create_feature",
        "description": (
            "Create one PartDesign feature inside an existing Body: "
            "datum_plane (PartDesign::Plane), sketch "
            "(Sketcher::SketchObject), pad, pocket or hole. The feature is "
            "created through body.newObject so Body membership is native; "
            "pad/pocket/hole require a profile object from the same Body, "
            "and an optional signed support reference attaches through "
            "Support plus an explicitly requested MapMode. Properties are "
            "validated against the created feature and everything runs in "
            "one transaction; a property, profile, support, Tip or geometry "
            "failure aborts and removes the feature. Expectations are "
            "checked against the Body's final geometry."
        ),
        "inputSchema": _CREATE_FEATURE_INPUT,
        "outputSchema": _CREATE_FEATURE_OUTPUT,
    }
]

HANDLERS: dict[str, Callable[[Any, dict], Any]] = {}

check_schema(_CREATE_FEATURE_INPUT)
check_schema(_CREATE_FEATURE_OUTPUT)


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


def _body_members(body: Any) -> list[Any] | None:
    for attribute in ("Group", "Model"):
        members = getattr(body, attribute, None)
        if members is not None:
            try:
                return list(members)
            except Exception:
                continue
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
    members = _body_members(body)
    if members is None:
        raise _fail(
            f"cannot verify Body membership for '{profile_obj.Name}': the body "
            "exposes neither Group nor Model"
        )
    if profile_obj not in members:
        raise _fail(f"profile '{profile_obj.Name}' does not belong to body '{body.Name}'")
    return profile_obj


def _native_support(ctx: Any, doc: Any, support: Mapping) -> tuple[Any, str]:
    from .geometry import resolve_reference

    return resolve_reference(ctx, doc, support)


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


def _apply_support(feature: Any, support_obj: Any, native: str, map_mode: str) -> None:
    for prop in ("Support", "AttachmentSupport"):
        if _objects._property_exists(feature, prop):
            setattr(feature, prop, [(support_obj, native)])
            break
    else:
        raise _fail(f"feature '{feature.Name}' exposes neither Support nor AttachmentSupport")
    if not _objects._property_exists(feature, "MapMode"):
        raise _fail(f"feature '{feature.Name}' exposes no MapMode property")
    feature.MapMode = map_mode


# ---------------------------------------------------------------------------
# Handler.
# ---------------------------------------------------------------------------


def _create_feature(ctx: Any, arguments: dict) -> dict:
    doc = ctx.require_document(arguments["document"])
    body = ctx.require_object(doc, str(arguments["body"]))
    _require_body(body)
    kind = str(arguments["kind"])
    requested_name = str(arguments["name"])
    properties = dict(arguments.get("properties") or {})
    expected_solids = arguments.get("expected_solids")
    expected_bounds = arguments.get("expected_bounds")
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
    else:
        type_id = _KIND_TYPES[kind]

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
        support_obj, native_support = _native_support(ctx, doc, arguments["support"])
    elif "MapMode" in properties:
        raise _fail("properties.MapMode requires a support reference")

    # Per-target expectations: the Body carries the requested contract, the
    # created feature keeps the default one. Filled inside the transaction
    # once the actual feature Name exists; the gate reads it after recompute.
    expectations: dict[str, dict] = {}
    body_expectation: dict[str, Any] = {}
    if expected_solids is not None:
        body_expectation["expected_solids"] = expected_solids
    if expected_bounds is not None:
        body_expectation["expected_bounds"] = expected_bounds
        body_expectation["bounds_tolerance"] = float(bounds_tolerance)
    if body_expectation:
        expectations[body.Name] = body_expectation

    created: list[Any] = []

    with mutation(
        ctx,
        doc,
        f"create_feature:{body.Name}",
        lambda: [body, *created],
        expectations=expectations,
    ) as applied:
        # body.newObject owns every accepted feature type, so Body
        # membership and Tip are native. The rejected datum path never
        # reaches here: a non-Plane datum TypeId fails prevalidation.
        feature = body.newObject(type_id, requested_name)
        created.append(feature)
        expectations.setdefault(str(getattr(feature, "Name", "")), {})

        if profile_obj is not None:
            _apply_profile(feature, profile_obj, kind)
        if support_obj is not None:
            _apply_support(feature, support_obj, native_support, properties["MapMode"])
            remaining = {k: v for k, v in properties.items() if k != "MapMode"}
        else:
            remaining = properties
        if remaining:
            prepared = _objects._prepare_properties(ctx, doc, feature, remaining)
            _objects._apply_prepared(feature, prepared)

        # Tip is set by FreeCAD during recompute; recompute now so a Tip
        # contract failure aborts inside this transaction instead of
        # surfacing after the commit.
        doc.recompute()
        tip = getattr(body, "Tip", None)
        tip_name = str(getattr(tip, "Name", "")) or None
        if kind in _PROFILE_KINDS and tip_name != str(feature.Name):
            raise _fail(
                f"body '{body.Name}' Tip is {tip_name!r} after recompute, "
                f"but the created {kind} is '{feature.Name}'",
                {"reason": "tip_mismatch", "bodyTip": tip_name},
            )

    feature = created[0]
    report = geometry_report(feature)
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
        "bodyReport": geometry_report(body, expected_solids),
        "change": _objects._change_summary(
            [{"name": name, "before": None, "after": after} for name, after in after_rows],
            solid_before=None,
            solid_after=report["solid_count"],
            volume_before=None,
            volume_after=report["volume"],
            bounds_before=None,
            bounds_after=document_bounds(feature),
            dependents=0,
        ),
        "applied": applied,
    }


HANDLERS["create_feature"] = _create_feature
