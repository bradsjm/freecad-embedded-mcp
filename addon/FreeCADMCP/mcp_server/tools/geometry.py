"""Deterministic geometry tools: validate_geometry, measure, signed topology refs.

``validate_geometry`` reports per-object state, shape validity, solid count,
volume, bounds, ``shape.check()`` diagnostics and ``shape.getTolerance(1)``
without repairing anything; expected solids and expected bounds produce
explicit verdicts and shared query checks. ``measure`` supports the
distance/interference/section/difference/faces modes over shared topology
targets: a whole object (``{"object": <Name>}``), a signed subshape reference
(``{"object": <Name>, "subelement": <token>}``) or a declarative query
(``{"object": <Name>, "query": [...]}``). Signed subelements are opaque
HMAC tokens (document identity, generation, object name, role, index)
produced with the shared server signing key via
``ctx.signer.sign("topology", payload)``. Numeric ``Face7``/``Edge7``
selectors and the retired exact-box selectors are never accepted as durable
references.

``make_reference``/``resolve_reference``/``resolve_query`` are consumed by
other tool modules (e.g. ``objects``) to hand out and re-open subshape
identities; the selector grammar itself lives in
:mod:`mcp_server.topology_query`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .. import topology_query as tq
from ..object_validation import (
    compare_expected_bounds,
    geometry_report,
    shape_is_null,
)
from ..protocol import (
    DOMAIN_CURSOR,
    VALIDATION_FAILED,
    ProtocolError,
    ToolError,
    check_schema,
    fingerprint,
    stale_generation_details,
)

_DEFAULT_BOUNDS_TOLERANCE = 0.000001
_MAX_CARDINALITY_CANDIDATES = 16
_MAX_CURVES = 32
_MAX_FACES = 64
_MAX_TOPOLOGY_PAGE = 100
_DEFAULT_TOPOLOGY_PAGE = 50
_MAX_CHECKS = 16
_MAX_CHECK_TARGETS = 100

_NUMERIC_SUBELEMENT = re.compile(r"(?:Face|Edge|Vertex|Wire)\d+")
_SUBELEMENT_INDEX = re.compile(r"(Face|Edge)([1-9][0-9]*)")
_MAX_SUBELEMENT_LIST = 32

# Exact FreeCAD surface/curve class names mapped to the uppercase analytic
# selector types. A missing Surface/Curve or an unmapped class is
# "unavailable" for typed selectors, never silently OTHER.
_FACE_TYPE_NAMES = {
    "Plane": "PLANE",
    "Cylinder": "CYLINDER",
    "Cone": "CONE",
    "Sphere": "SPHERE",
    "Toroid": "TORUS",
    "BezierSurface": "BEZIER",
    "BSplineSurface": "BSPLINE",
    "SurfaceOfRevolution": "REVOLUTION",
    "SurfaceOfExtrusion": "EXTRUSION",
    "OffsetSurface": "OFFSET",
}
_EDGE_TYPE_NAMES = {
    "Line": "LINE",
    "Circle": "CIRCLE",
    "ArcOfCircle": "CIRCLE",
    "Ellipse": "ELLIPSE",
    "ArcOfEllipse": "ELLIPSE",
    "Hyperbola": "HYPERBOLA",
    "ArcOfHyperbola": "HYPERBOLA",
    "Parabola": "PARABOLA",
    "ArcOfParabola": "PARABOLA",
    "BezierCurve": "BEZIER",
    "BSplineCurve": "BSPLINE",
    "OffsetCurve": "OFFSET",
}


# ---------------------------------------------------------------------------
# Small numeric helpers. Every float leaving this module passes through
# ``_finite`` so structured output never contains NaN or infinity.
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _point(vector: Any) -> list[float] | None:
    """Convert a FreeCAD ``Base.Vector``-like value to ``[x, y, z]`` or None."""

    if vector is None:
        return None
    try:
        coords = (vector.x, vector.y, vector.z)
    except AttributeError:
        return None
    converted = [_finite(coord) for coord in coords]
    if any(coord is None for coord in converted):
        return None
    return converted


def _shape_of(obj: Any) -> Any:
    """Return the object's shape, or None for shapeless/invalid accessors."""

    try:
        shape = getattr(obj, "Shape", None)
    except Exception:
        return None
    if shape_is_null(shape):
        return None
    return shape or None


def _unresolvable_geometry(obj: Any) -> ToolError:
    return ToolError(
        VALIDATION_FAILED,
        "Cannot resolve document-space geometry",
        {"object": str(getattr(obj, "Name", ""))},
    )


def placed_shape(obj: Any) -> Any:
    """Return a copy of ``obj``'s shape in document (global) coordinates.

    The copy's placement is set to ``obj.getGlobalPlacement()`` exactly
    once: the native getter already accumulates the object's own local
    placement with every ancestor transform, so multiplying the existing
    shape placement would double the object's own transform.

    Fail-closed: a null/missing shape or an unavailable/failed
    global-placement access raises ``VALIDATION_FAILED`` instead of
    silently returning local coordinates. An ``App::Link`` with a
    non-identity scale (or an unresolved instance path) is rejected for
    the same reason until a native transform can represent it; ordinary
    unscaled links resolve through the native global-placement API.
    """

    shape = _shape_of(obj)
    if shape is None:
        raise _unresolvable_geometry(obj)
    type_id = str(getattr(obj, "TypeId", "") or "")
    if type_id.startswith("App::Link"):
        scale = getattr(obj, "Scale", None)
        if isinstance(scale, (int, float)) and not isinstance(scale, bool):
            # App::Link exposes a uniform scalar Scale.
            components = (float(scale),) * 3
        else:
            try:
                components = (float(scale.x), float(scale.y), float(scale.z))
            except Exception:
                raise _unresolvable_geometry(obj) from None
        if any(abs(component - 1.0) > 1e-9 for component in components):
            raise _unresolvable_geometry(obj)
    try:
        placement = obj.getGlobalPlacement()
    except Exception:
        raise _unresolvable_geometry(obj) from None
    if placement is None:
        raise _unresolvable_geometry(obj)
    try:
        global_shape = shape.copy()
        global_shape.Placement = placement
    except Exception:
        raise _unresolvable_geometry(obj) from None
    return global_shape


def _bbox(shape: Any) -> list[float] | None:
    """Return ``[xmin, ymin, zmin, xmax, ymax, zmax]`` finite floats or None."""

    try:
        box = shape.BoundBox
        values = [box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax]
    except Exception:
        return None
    converted = [_finite(value) for value in values]
    if any(value is None for value in converted):
        return None
    return converted


def _solid_count(shape: Any) -> int | None:
    """Number of native solids in ``shape``; None when Solids is unreadable."""

    try:
        return len(shape.Solids)
    except Exception:
        return None


def _inverted_bounds(bounds: list[float] | None) -> bool:
    """True when a readable bounding box has no extent (``min`` past ``max``).

    OCC reports the fully consumed ``shape.cut()`` result as a non-null shape
    whose box is inverted (±DBL_MAX per axis), so an inverted box — not
    ``isNull()`` — is the emptiness signal; an unreadable box (None) is not.
    """

    if bounds is None:
        return False
    return any(bounds[index] > bounds[index + 3] for index in range(3))


def _subelement_label(role: str, index: int) -> str:
    return ("Face" if role == "face" else "Edge") + str(index)


# ---------------------------------------------------------------------------
# Topology references.
# ---------------------------------------------------------------------------


def make_reference(ctx: Any, doc: Any, obj: Any, role: str, index: int) -> dict:
    """Build a canonical ``{object, subelement}`` reference for a subshape.

    The subelement is an opaque HMAC token signed under the shared ``topology``
    domain binding document identity, generation, object name, role and index.
    """

    payload = {
        "document": ctx.document_identity(doc),
        "generation": int(ctx.document_generation(doc)),
        "object": obj.Name,
        "role": role,
        "index": int(index),
    }
    return {"object": obj.Name, "subelement": ctx.signer.sign("topology", payload)}


def whole_reference(obj: Any) -> dict:
    """Canonical whole-object reference: identity without a subelement."""

    return {"object": obj.Name}


def _reference_for(ctx: Any, doc: Any, obj: Any, selection: Mapping | None) -> dict:
    """The canonical whole/signed target describing a resolution outcome."""

    if selection is None:
        return whole_reference(obj)
    return make_reference(ctx, doc, obj, selection["role"], selection["index"])


def _cardinality_error(
    reason: str,
    message: str,
    parameter: str,
    ctx: Any,
    doc: Any,
    obj: Any,
    role: str,
    indices: list[int],
) -> ToolError:
    """Build the shared zero-match/ambiguity refusal with bounded evidence."""

    return ToolError(
        VALIDATION_FAILED,
        message,
        {
            "reason": reason,
            "parameter": parameter,
            "object": obj.Name,
            "role": role,
            "matchCount": len(indices),
            "candidates": [
                make_reference(ctx, doc, obj, role, index)
                for index in indices[:_MAX_CARDINALITY_CANDIDATES]
            ],
            "candidatesTruncated": len(indices) > _MAX_CARDINALITY_CANDIDATES,
            "nextTool": "inspect_topology",
        },
    )


def _root_subshapes(shape: Any, role: str) -> list[Any]:
    """The complete root Faces/Edges array, refusing unreadable access."""

    try:
        return list(shape.Faces if role == "face" else shape.Edges)
    except Exception as exc:
        raise ToolError(VALIDATION_FAILED, f"subshape access failed: {exc}") from exc


def _normalize_unit(vector: Any) -> tuple[float, float, float] | None:
    point = _point(vector)
    if point is None:
        return None
    norm = math.sqrt(sum(value * value for value in point))
    if norm <= 0.0:
        return None
    return (point[0] / norm, point[1] / norm, point[2] / norm)


def _query_record(role: str, index: int, subshape: Any) -> dict:
    """One plain evaluation record for :mod:`mcp_server.topology_query`.

    Every read is guarded: an unreadable field is recorded as unavailable so
    the evaluator can refuse a selector that actually needs it instead of
    silently dropping an eligible candidate. Classification uses the exact
    native class name; an unmapped analytic class is recorded as ``unmapped``
    and stays usable for centers but never pretends to be another type.
    """

    record: dict[str, Any] = {"index": index, "role": role}
    surface_attribute = "Surface" if role == "face" else "Curve"
    table = _FACE_TYPE_NAMES if role == "face" else _EDGE_TYPE_NAMES
    try:
        raw_type = getattr(subshape, surface_attribute, None)
    except Exception:
        raw_type = None
    type_name = _type_name(raw_type)
    if type_name is None:
        record["type"], record["typeStatus"] = None, "unreadable"
    else:
        mapped = table.get(type_name)
        if mapped is None:
            record["type"], record["typeStatus"] = None, "unmapped"
        else:
            record["type"], record["typeStatus"] = mapped, "ok"
    try:
        record["center"] = _point(getattr(subshape, "CenterOfMass", None))
    except Exception:
        record["center"] = None
    record["direction"] = None
    if record.get("type") == "PLANE":
        try:
            u1, u2, v1, v2 = subshape.ParameterRange
            record["direction"] = _normalize_unit(
                subshape.normalAt((u1 + u2) / 2.0, (v1 + v2) / 2.0)
            )
        except Exception:
            record["direction"] = None
    elif record.get("type") == "LINE":
        try:
            record["direction"] = _normalize_unit(subshape.tangentAt(subshape.FirstParameter))
        except Exception:
            record["direction"] = None
    record["radius"] = None
    record["axis"] = None
    if record.get("typeStatus") == "ok":
        try:
            analytic = getattr(subshape, surface_attribute, None)
            if record["type"] in ("CYLINDER", "SPHERE", "CIRCLE"):
                record["radius"] = _finite(getattr(analytic, "Radius", None))
            if record["type"] in ("CYLINDER", "CIRCLE", "CONE", "TORUS"):
                record["axis"] = _normalize_unit(getattr(analytic, "Axis", None))
        except Exception:
            pass
    return record


def resolve_query(
    ctx: Any, doc: Any, target: Mapping, parameter: str = "query"
) -> tuple[Any, str, list[int]]:
    """Resolve a shared query target to ``(object, final role, native indices)``.

    ``expected_generation`` is checked before any native extraction. The
    first stage enumerates root faces or edges; later same-role stages
    filter the current set and a face-to-edge stage expands the selected
    faces' edges, deduplicated with native ``isSame`` and mapped back to
    root indices. Every stage sorts ascending; no face or edge is ever
    renumbered, so signed tokens keep binding root indices.
    """

    obj = ctx.require_object(doc, str(target.get("object")))
    expected_generation = target.get("expected_generation")
    if expected_generation is not None:
        actual = int(ctx.document_generation(doc))
        if expected_generation != actual:
            raise ToolError(
                VALIDATION_FAILED,
                "query target is stale for the current document generation",
                stale_generation_details(int(expected_generation), actual, "inspect_topology"),
            )
    steps = tq.normalize_query(target.get("query"), parameter)
    shape = placed_shape(obj)
    role: str | None = None
    selected: list[int] = []
    records: list[dict] = []
    for step in steps:
        if role is None:
            role = step["role"]
            subshapes = _root_subshapes(shape, role)
            if len(subshapes) > tq.MAX_QUERY_CANDIDATES:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"{obj.Name} has {len(subshapes)} {role}s; queries are "
                    f"capped at {tq.MAX_QUERY_CANDIDATES} candidates",
                    {"reason": "query_too_large", "nextTool": "inspect_topology"},
                )
            records = [
                _query_record(role, index, subshape) for index, subshape in enumerate(subshapes, 1)
            ]
            current = records
        elif step["role"] == role:
            by_index = {record["index"]: record for record in records}
            current = [by_index[index] for index in selected]
        else:
            # face -> edge expansion; edge -> face was refused by the
            # normalizer (no implicit ancestor query).
            current = _expand_face_edges(shape, selected)
            role = step["role"]
            records = current
        if step["selector"] is not None:
            indices = tq.evaluate_selector(tq.parse_selector(step["selector"]), current)
        else:
            indices = [record["index"] for record in current]
        by_index = {record["index"]: record for record in current}
        indices = [
            index
            for index in indices
            if tq.radius_predicate_matches(step, by_index[index])
            and tq.axis_predicate_matches(step, by_index[index])
        ]
        selected = sorted(indices)
    assert role is not None
    return obj, role, selected


def _expand_face_edges(shape: Any, selected: list[int]) -> list[dict]:
    """Expand selected faces to their deduplicated root edge records.

    Correspondence is proven with native ``isSame`` against the retained
    root edge array; a face edge with no root correspondence, or an
    unavailable ``isSame`` API, refuses the query instead of guessing an
    index. The expansion is capped before any unbounded report is built.
    """

    root_faces = _root_subshapes(shape, "face")
    root_edges = _root_subshapes(shape, "edge")
    if len(root_edges) > tq.MAX_QUERY_CANDIDATES:
        raise ToolError(
            VALIDATION_FAILED,
            f"the expansion would examine {len(root_edges)} edges; queries "
            f"are capped at {tq.MAX_QUERY_CANDIDATES} candidates",
            {"reason": "query_too_large", "nextTool": "inspect_topology"},
        )
    expanded: set[int] = set()
    for face_index in sorted(selected):
        face = root_faces[face_index - 1]
        try:
            face_edges = list(face.Edges)
        except Exception as exc:
            raise ToolError(
                VALIDATION_FAILED,
                f"subshape access failed: {exc}",
                {"reason": "selector_geometry_unavailable", "nextTool": "inspect_topology"},
            ) from exc
        for edge in face_edges:
            root_index = None
            for candidate_index, root_edge in enumerate(root_edges, 1):
                is_same = getattr(edge, "isSame", None)
                if not callable(is_same):
                    raise ToolError(
                        VALIDATION_FAILED,
                        "the native subshape isSame API is unavailable; "
                        "face-to-edge expansion cannot be proven",
                        {
                            "reason": "selector_geometry_unavailable",
                            "nextTool": "inspect_topology",
                        },
                    )
                try:
                    if is_same(root_edge):
                        root_index = candidate_index
                        break
                except Exception as exc:
                    raise ToolError(
                        VALIDATION_FAILED,
                        f"subshape correspondence failed: {exc}",
                        {"reason": "selector_geometry_unavailable", "nextTool": "inspect_topology"},
                    ) from exc
            if root_index is None:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"an edge of face {face_index} has no root correspondence; "
                    "the query cannot be resolved against root indices",
                    {"reason": "selector_geometry_unavailable", "nextTool": "inspect_topology"},
                )
            expanded.add(root_index)
    if len(expanded) > tq.MAX_QUERY_CANDIDATES:
        raise ToolError(
            VALIDATION_FAILED,
            f"the expansion selected {len(expanded)} edges; queries are "
            f"capped at {tq.MAX_QUERY_CANDIDATES} candidates",
            {"reason": "query_too_large", "nextTool": "inspect_topology"},
        )
    return [_query_record("edge", index, root_edges[index - 1]) for index in sorted(expanded)]


def _resolve_reference_parts(
    ctx: Any, doc: Any, reference: Mapping, parameter: str
) -> tuple[Any, str | None, str]:
    """Resolve one shared target to ``(object, role, native label)``.

    ``role`` is None for a whole object. Query targets must resolve to
    exactly one subshape; zero matches and ambiguity are named refusals
    carrying bounded signed candidate evidence.
    """

    if "query" in reference:
        obj, role, indices = resolve_query(ctx, doc, reference, parameter)
        if not indices:
            raise _cardinality_error(
                "selection_empty",
                f"{parameter} matched no {role} of {obj.Name}",
                parameter,
                ctx,
                doc,
                obj,
                role,
                indices,
            )
        if len(indices) > 1:
            raise _cardinality_error(
                "selection_ambiguous",
                f"{parameter} matched {len(indices)} {role}s of {obj.Name}; "
                "narrow the query or use a set consumer",
                parameter,
                ctx,
                doc,
                obj,
                role,
                indices,
            )
        return obj, role, _subelement_label(role, indices[0])
    if "subelement" not in reference or reference.get("subelement") is None:
        name = reference.get("object")
        if not isinstance(name, str) or not name:
            raise ToolError(VALIDATION_FAILED, f"{parameter} is missing an object name")
        return ctx.require_object(doc, name), None, ""
    subelement = reference.get("subelement")
    if not isinstance(subelement, str):
        raise ToolError(VALIDATION_FAILED, f"{parameter}.subelement must be a string")
    if not subelement:
        raise ToolError(
            VALIDATION_FAILED,
            f"{parameter}.subelement is empty; omit it for a whole object or "
            "pass a signed reference",
            {"parameter": parameter, "reason": "empty_subelement"},
        )
    obj = ctx.require_object(doc, str(reference.get("object")))
    if "." not in subelement:
        if _NUMERIC_SUBELEMENT.fullmatch(subelement):
            raise ToolError(
                VALIDATION_FAILED,
                "numeric subelement selectors (e.g. Face7) are not durable; use the"
                " signed reference returned by inspect_topology or measure",
            )
        raise ToolError(VALIDATION_FAILED, f"{parameter}.subelement is not a signed token")
    try:
        payload = ctx.signer.verify("topology", subelement)
    except ProtocolError as exc:
        data = exc.data if isinstance(exc.data, Mapping) else {}
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference signature rejected",
            {"reason": data.get("reason", "invalid")},
        ) from exc
    if payload.get("document") != ctx.document_identity(doc):
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference belongs to a different document",
            {"reason": "document_mismatch"},
        )
    if payload.get("generation") != ctx.document_generation(doc):
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference is stale for the current document generation",
            stale_generation_details(
                int(payload["generation"]),
                int(ctx.document_generation(doc)),
                "inspect_topology",
            ),
        )
    if payload.get("object") != obj.Name:
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference does not match the referenced object",
            {"reason": "object_mismatch"},
        )
    role = payload.get("role")
    index = payload.get("index")
    if role not in ("face", "edge") or isinstance(index, bool) or not isinstance(index, int):
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference role or index is invalid",
            {"reason": "malformed"},
        )
    if index < 1:
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference index must be positive",
            {"reason": "malformed"},
        )
    # A token binds the document generation, so an index past the current
    # subshape count means the shape changed under an unchanged generation
    # (or the token was crafted); refuse instead of handing out a label the
    # native accessor would mis-read.
    shape = _shape_of(obj)
    if shape is None:
        raise ToolError(
            VALIDATION_FAILED,
            f"object {obj.Name} has no shape to resolve the reference against",
            {"reason": "missing_subelement"},
        )
    subshapes = _root_subshapes(shape, role)
    if index > len(subshapes):
        raise ToolError(
            VALIDATION_FAILED,
            f"{role} {index} no longer exists on {obj.Name}",
            {"reason": "missing_subelement"},
        )
    return obj, role, _subelement_label(role, index)


def resolve_reference(
    ctx: Any, doc: Any, reference: Any, parameter: str = "reference"
) -> tuple[Any, str]:
    """Resolve a shared whole/signed/query target to ``(object, native)``.

    A target without a ``subelement`` key names the whole object; an empty
    subelement string is the retired empty sentinel and is refused, as are
    raw numeric ``Face7``/``Edge7`` labels. Stale or tampered signed tokens
    are rejected with ``VALIDATION_FAILED``.
    """

    if not isinstance(reference, Mapping):
        raise ToolError(VALIDATION_FAILED, f"{parameter} must be a target object mapping")
    obj, _role, native = _resolve_reference_parts(ctx, doc, reference, parameter)
    return obj, native


def resolve_reference_list(
    ctx: Any, doc: Any, base: Any, references: list[Mapping], role: str
) -> tuple[Any, list[str]]:
    """Resolve 1..N shared targets onto one base object.

    Every entry must name the base object and resolve to a signed subshape
    of the requested role; query targets expand first and whole-object
    entries are refused. Duplicates are rejected. Returns the base object
    and the native subelement labels in request order.
    """

    if not references:
        raise ToolError(VALIDATION_FAILED, "at least one signed reference is required")
    if len(references) > _MAX_SUBELEMENT_LIST:
        raise ToolError(
            VALIDATION_FAILED,
            f"at most {_MAX_SUBELEMENT_LIST} signed references are accepted",
        )
    base_obj, _base_role, base_native = _resolve_reference_parts(ctx, doc, base, "base")
    if base_native:
        raise ToolError(
            VALIDATION_FAILED,
            "base must be a whole object, not a subshape",
            {"reason": "subshape_not_allowed"},
        )
    seen: set[str] = set()
    labels: list[str] = []
    for position, reference in enumerate(references):
        parameter = f"references[{position}]"
        if not isinstance(reference, Mapping):
            raise ToolError(VALIDATION_FAILED, f"{parameter} must be a target object mapping")
        obj, resolved_role, native = _resolve_reference_parts(ctx, doc, reference, parameter)
        if obj is not base_obj:
            raise ToolError(
                VALIDATION_FAILED,
                f"reference {position} targets '{obj.Name}' but the base is '{base_obj.Name}'",
                {"position": position},
            )
        if resolved_role is None:
            raise ToolError(
                VALIDATION_FAILED,
                f"reference {position} must carry a signed subelement token",
                {"position": position, "reason": "subshape_not_allowed"},
            )
        prefix = "Face" if role == "face" else "Edge"
        if not native.startswith(prefix):
            raise ToolError(
                VALIDATION_FAILED,
                f"reference {position} must be a signed {role} token",
                {"position": position, "native": native},
            )
        if native in seen:
            raise ToolError(
                VALIDATION_FAILED,
                f"reference {position} duplicates {native}",
                {"position": position},
            )
        seen.add(native)
        labels.append(native)
    return base_obj, labels


# ---------------------------------------------------------------------------
# Shared target resolution (whole object, signed reference, or query).
# ---------------------------------------------------------------------------


def _resolve_reference_selection(
    ctx: Any, doc: Any, selector: Mapping, parameter: str
) -> tuple[Any, Mapping | None]:
    """Resolve a whole/signed/query target to ``(object, selection)``.

    A whole object selects nothing (``selection is None``); a signed Face/Edge
    token selects the placed (document-space) subshape at the signed index.
    """

    obj, native = resolve_reference(ctx, doc, selector, parameter)
    if native == "":
        return obj, None
    match = _SUBELEMENT_INDEX.fullmatch(native)
    if match is None:
        raise ToolError(
            VALIDATION_FAILED,
            f"unsupported native subelement {native!r} on {obj.Name}",
        )
    role = "face" if match.group(1) == "Face" else "edge"
    index = int(match.group(2))
    shape = placed_shape(obj)
    subshapes = _root_subshapes(shape, role)
    if index > len(subshapes):
        raise ToolError(
            VALIDATION_FAILED,
            f"{role} {index} no longer exists on {obj.Name}",
            {"reason": "missing_subelement"},
        )
    subshape = subshapes[index - 1]
    return obj, {
        "role": role,
        "index": index,
        "shape": subshape,
        "bounds": _bbox(subshape),
    }


def _resolve_single_query(
    ctx: Any, doc: Any, selector: Mapping, parameter: str
) -> tuple[Any, Mapping]:
    """Resolve a query target to exactly one subshape selection."""

    obj, role, indices = resolve_query(ctx, doc, selector, parameter)
    if not indices:
        raise _cardinality_error(
            "selection_empty",
            f"{parameter} matched no {role} of {obj.Name}",
            parameter,
            ctx,
            doc,
            obj,
            role,
            indices,
        )
    if len(indices) > 1:
        raise _cardinality_error(
            "selection_ambiguous",
            f"{parameter} matched {len(indices)} {role}s of {obj.Name}; "
            "narrow the query or use a set consumer",
            parameter,
            ctx,
            doc,
            obj,
            role,
            indices,
        )
    index = indices[0]
    shape = placed_shape(obj)
    subshapes = _root_subshapes(shape, role)
    subshape = subshapes[index - 1]
    return obj, {
        "role": role,
        "index": index,
        "shape": subshape,
        "bounds": _bbox(subshape),
    }


def _resolve_target(
    ctx: Any, doc: Any, selector: Any, parameter: str
) -> tuple[Any, Mapping | None]:
    """Resolve a shared whole/signed/query target to ``(object, selection)``.

    The retired bare-name string, ``{object, role, box}`` and mixed
    query+subelement forms are refused before any native access.
    """

    if not isinstance(selector, Mapping):
        raise ToolError(
            VALIDATION_FAILED,
            f"{parameter} must be a shared target object ({'{object}'}), a "
            "signed reference, or a query target",
        )
    if "query" in selector:
        return _resolve_single_query(ctx, doc, selector, parameter)
    return _resolve_reference_selection(ctx, doc, selector, parameter)


def _target_shape(obj: Any, selection: Mapping | None) -> Any:
    """The measurement target in document coordinates.

    Whole objects resolve through :func:`placed_shape` (global placement
    applied exactly once); a selected subshape is already a document-space
    face/edge of the global copy (see ``_resolve_target``).
    """

    if selection is not None:
        return selection["shape"]
    return placed_shape(obj)


# ---------------------------------------------------------------------------
# validate_geometry.
# ---------------------------------------------------------------------------


def _geometry_entry(
    report: Mapping[str, Any],
    expected_solids: int | None,
    expected_bounds: Mapping[str, Any],
    tolerance: float,
) -> dict:
    name = report["name"]
    solid_count = report["solid_count"]
    volume = report["volume"]
    if expected_solids is None:
        solids_verdict = "unspecified"
    elif solid_count is None:
        solids_verdict = "unavailable"
    elif solid_count == expected_solids:
        solids_verdict = "match"
    else:
        solids_verdict = "mismatch"
    if not solid_count:
        # Valid non-solids (groups, sketches, empty bodies) are never volume
        # failures; only objects with solids require positive volume.
        volume_verdict = "not_applicable"
    elif volume is not None and volume > 0.0:
        volume_verdict = "positive"
    else:
        volume_verdict = "nonpositive"
    expected = expected_bounds.get(name) if isinstance(expected_bounds, Mapping) else None
    measured = report["bounds"]
    bounds_verdict = None
    if expected is not None:
        verdict, deviations = compare_expected_bounds(measured, expected, tolerance)
        bounds_verdict = {
            "verdict": verdict,
            "expected": [float(value) for value in expected],
            "deviations": deviations,
        }
    valid = (
        bool(report.get("ok", True))
        and bool(report["object_valid"])
        and report["shape_valid"] is not False
        and solids_verdict not in ("mismatch", "unavailable")
        and volume_verdict in ("positive", "not_applicable")
        and (bounds_verdict is None or bounds_verdict["verdict"] == "match")
    )
    return {
        "name": name,
        "state": list(report["state"]),
        "object_valid": bool(report["object_valid"]),
        "shape_valid": report["shape_valid"],
        "solid_count": solid_count,
        "volume": volume,
        "bounds": measured,
        "max_tolerance": report["max_tolerance"],
        "diagnostics": list(report["diagnostics"]),
        "error": report.get("error"),
        "verdicts": {
            "solids": solids_verdict,
            "volume": volume_verdict,
            **({"bounds": bounds_verdict} if bounds_verdict is not None else {}),
        },
        "valid": valid,
    }


#: Bounded diagnostics strings for indeterminate check rows.
_MAX_CHECK_DIAGNOSTICS = 4
_MAX_CHECK_DIAGNOSTIC_LENGTH = 256


def _indeterminate_check(check: Mapping[str, Any], reason: str, diagnostics: list[str]) -> dict:
    """One closed indeterminate result row with bounded evidence."""

    return {
        "id": check["id"],
        "kind": check["kind"],
        "status": "indeterminate",
        "reason": reason,
        "diagnostics": [
            diagnostic[:_MAX_CHECK_DIAGNOSTIC_LENGTH]
            for diagnostic in diagnostics[:_MAX_CHECK_DIAGNOSTICS]
        ],
    }


def _normalize_geometry_checks(checks: Any) -> list[dict]:
    """Stage 1: normalize and prevalidate every check before resolution.

    IDs default to the stable one-based input position; duplicate effective
    IDs and structurally invalid ranges fail the call here, before any
    native access.
    """

    if not isinstance(checks, list) or not 1 <= len(checks) <= _MAX_CHECKS:
        raise ToolError(
            VALIDATION_FAILED,
            f"checks must be an array of 1 to {_MAX_CHECKS} check objects",
        )
    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for position, raw in enumerate(checks, 1):
        if not isinstance(raw, Mapping) or raw.get("kind") not in (
            "volume_range",
            "clearance_min",
            "interference_max",
        ):
            raise ToolError(
                VALIDATION_FAILED,
                f"checks[{position - 1}] must carry kind volume_range, "
                "clearance_min or interference_max",
            )
        kind = raw["kind"]
        check_id = raw.get("id")
        if check_id is None:
            check_id = f"check-{position}"
        if not isinstance(check_id, str) or not 1 <= len(check_id) <= 64:
            raise ToolError(
                VALIDATION_FAILED,
                f"checks[{position - 1}].id must be a string of 1 to 64 characters",
            )
        if check_id in seen_ids:
            raise ToolError(
                VALIDATION_FAILED,
                f"checks repeats the effective id {check_id!r}",
                {"reason": "duplicate_check_id", "id": check_id},
            )
        seen_ids.add(check_id)
        check: dict[str, Any] = {"id": check_id, "kind": kind}
        if kind == "volume_range":
            if raw.get("object") is None:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"check {check_id!r} requires an object target",
                )
            minimum = raw.get("min")
            maximum = raw.get("max")
            if minimum is None and maximum is None:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"volume_range check {check_id!r} requires min or max",
                    {"reason": "empty_volume_range", "id": check_id},
                )
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"volume_range check {check_id!r} has min greater than max",
                    {"reason": "invalid_volume_range", "id": check_id},
                )
            check["object"] = raw["object"]
            if minimum is not None:
                check["min"] = minimum
            if maximum is not None:
                check["max"] = maximum
        else:
            for side in ("a", "b"):
                if raw.get(side) is None:
                    raise ToolError(
                        VALIDATION_FAILED,
                        f"{kind} check {check_id!r} requires targets a and b",
                    )
                check[side] = raw[side]
            if kind == "clearance_min":
                minimum = raw.get("min")
                if minimum is None:
                    raise ToolError(
                        VALIDATION_FAILED,
                        f"clearance_min check {check_id!r} requires min",
                    )
                if not isinstance(minimum, (int, float)) or isinstance(minimum, bool):
                    raise ToolError(
                        VALIDATION_FAILED,
                        f"clearance_min check {check_id!r} min must be a number",
                    )
                if not math.isfinite(float(minimum)) or minimum <= 0:
                    raise ToolError(
                        VALIDATION_FAILED,
                        f"clearance_min check {check_id!r} min must be finite "
                        "and strictly greater than zero; use interference_max "
                        "to permit touching",
                        {"id": check_id},
                    )
                check["min"] = minimum
            else:
                maximum = raw.get("max")
                if maximum is not None:
                    if (
                        isinstance(maximum, bool)
                        or not isinstance(maximum, (int, float))
                        or not math.isfinite(float(maximum))
                        or maximum < 0
                    ):
                        raise ToolError(
                            VALIDATION_FAILED,
                            f"interference_max check {check_id!r} max must be a "
                            "nonnegative finite volume",
                        )
                    check["max"] = maximum
        normalized.append(check)
    return normalized


def _prepare_geometry_checks(
    ctx: Any, doc: Any, checks: list[dict]
) -> tuple[list[dict], list[dict], list[str]]:
    """Stage 2: resolve every identity/query/reference target.

    Invalid, stale, missing or ambiguous targets fail the call here, before
    any expensive distance or common calculation. Returns the checks with
    resolved targets attached, the bounded resolvedSelection receipt list
    for query-origin targets, and the first-use-ordered owner-object names.
    """

    receipts: list[dict] = []
    owners: list[str] = []
    generation = int(ctx.document_generation(doc))

    def _resolve_side(check: dict, side: str) -> dict:
        target = check[side]
        if "query" in target:
            obj, role, indices = resolve_query(ctx, doc, target, side)
            if not indices or len(indices) > 1:
                raise _cardinality_error(
                    "selection_ambiguous" if len(indices) > 1 else "selection_empty",
                    f"{side} of check {check['id']!r} must resolve to exactly "
                    f"one {role} of {obj.Name}",
                    side,
                    ctx,
                    doc,
                    obj,
                    role,
                    indices,
                )
            if len(receipts) < tq.MAX_QUERY_REFERENCES:
                references = [make_reference(ctx, doc, obj, role, index) for index in indices]
                receipts.append(
                    {
                        "parameter": f"checks.{check['id']}.{side}",
                        "document": str(getattr(doc, "Name", "")),
                        "generation": generation,
                        "references": references,
                        "count": len(references),
                    }
                )
            selection = {
                "role": role,
                "index": indices[0],
                "shape": None,
                "reference": make_reference(ctx, doc, obj, role, indices[0]),
            }
        else:
            obj, whole_or_selection = _resolve_target(ctx, doc, target, side)
            selection = None
            if whole_or_selection is not None:
                selection = {
                    "role": whole_or_selection["role"],
                    "index": whole_or_selection["index"],
                    "shape": None,
                    "reference": make_reference(
                        ctx, doc, obj, whole_or_selection["role"], whole_or_selection["index"]
                    ),
                }
        name = str(getattr(obj, "Name", ""))
        if name not in owners:
            owners.append(name)
        return {
            "object": obj,
            "selection": selection,
            "reference": (selection["reference"] if selection is not None else {"object": name}),
        }

    prepared: list[dict] = []
    for check in checks:
        entry = dict(check)
        if check["kind"] == "volume_range":
            volume_request = {**check, "kind": "volume_range", "a": check["object"]}
            entry["target"] = _resolve_side(volume_request, "a")
        else:
            entry["target_a"] = _resolve_side(check, "a")
            entry["target_b"] = _resolve_side(check, "b")
        prepared.append(entry)
    return prepared, receipts, owners


def _check_target_shape(target: Mapping[str, Any]) -> Any:
    """The prepared placed shape for one resolved check target."""

    obj = target["object"]
    selection = target["selection"]
    if selection is None:
        return placed_shape(obj)
    subshapes = _root_subshapes(placed_shape(obj), selection["role"])
    return subshapes[selection["index"] - 1]


def _volumetric_gate(check: Mapping[str, Any], targets: list[Mapping[str, Any]]) -> dict | None:
    """Require valid, non-null volumetric targets before acceptance math.

    Returns an indeterminate row when any target has no shape, an invalid
    shape, or no positive finite solid volume; None means every target may
    proceed.
    """

    for side, target in zip(("a", "b", "object"), targets, strict=False):
        if target is None:
            continue
        label = f"target {side!r}" if side != "object" else "target"
        try:
            shape = _check_target_shape(target)
        except ToolError as exc:
            return _indeterminate_check(
                check, "target_shapeless", [str(exc.message)[:_MAX_CHECK_DIAGNOSTIC_LENGTH]]
            )
        if shape is None:
            return _indeterminate_check(
                check,
                "target_shapeless",
                [f"{label} has no shape"],
            )
        try:
            valid = bool(shape.isValid())
        except Exception:
            valid = None
        if valid is False:
            return _indeterminate_check(
                check,
                "target_invalid",
                [f"{label} failed shape.isValid()"],
            )
        solids = _solid_count(shape)
        volume = _finite(getattr(shape, "Volume", None))
        if not solids or volume is None or volume <= 0:
            return _indeterminate_check(
                check,
                "non_volumetric_target",
                [
                    (
                        f"{label} reports solid_count={solids!r}, volume={volume!r}; "
                        "fit checks need a solid with positive volume"
                    )
                ],
            )
    return None


def _evaluate_geometry_checks(ctx: Any, doc: Any, checks: list[dict]) -> list[dict]:
    """Stage 3: evaluate the prepared checks and build closed result rows."""

    results: list[dict] = []
    for check in checks:
        kind = check["kind"]
        if kind == "volume_range":
            target = check["target"]
            indeterminate = _volumetric_gate(check, [target])
            if indeterminate is not None:
                results.append(indeterminate)
                continue
            try:
                shape = _check_target_shape(target)
                measured = _finite(getattr(shape, "Volume", None))
            except ToolError as exc:
                results.append(
                    _indeterminate_check(
                        check,
                        "measurement_unavailable",
                        [str(exc.message)[:_MAX_CHECK_DIAGNOSTIC_LENGTH]],
                    )
                )
                continue
            minimum = check.get("min")
            maximum = check.get("max")
            if measured is None:
                results.append(
                    _indeterminate_check(
                        check,
                        "measurement_unavailable",
                        ["volume readback was not a finite number"],
                    )
                )
                continue
            passed = (minimum is None or measured >= minimum) and (
                maximum is None or measured <= maximum
            )
            row = {
                "id": check["id"],
                "kind": kind,
                "status": "pass" if passed else "fail",
                "object": target["reference"],
                "measured": measured,
            }
            if minimum is not None:
                row["min"] = minimum
            if maximum is not None:
                row["max"] = maximum
            results.append(row)
            continue
        target_a = check["target_a"]
        target_b = check["target_b"]
        indeterminate = _volumetric_gate(check, [target_a, target_b])
        if indeterminate is not None:
            results.append(indeterminate)
            continue
        try:
            a_shape = _check_target_shape(target_a)
            b_shape = _check_target_shape(target_b)
            distance_payload: dict[str, Any] = {}
            _measure_distance(a_shape, b_shape, distance_payload)
            common_payload: dict[str, Any] = {}
            _measure_interference(a_shape, b_shape, common_payload)
        except ToolError as exc:
            results.append(
                _indeterminate_check(
                    check,
                    "measurement_unavailable",
                    [str(exc.message)[:_MAX_CHECK_DIAGNOSTIC_LENGTH]],
                )
            )
            continue
        distance = distance_payload.get("distance")
        common_volume = common_payload.get("common_volume")
        if kind == "clearance_min":
            minimum = check["min"]
            # _finite was applied by the measurement helpers; a None here is
            # a nonfinite or missing measurement, never a pass.
            if distance is None or common_volume is None:
                results.append(
                    _indeterminate_check(
                        check,
                        "measurement_unavailable",
                        [f"distance={distance!r}, common_volume={common_volume!r}"],
                    )
                )
                continue
            passed = distance >= minimum and common_volume <= 0
            results.append(
                {
                    "id": check["id"],
                    "kind": kind,
                    "status": "pass" if passed else "fail",
                    "a": target_a["reference"],
                    "b": target_b["reference"],
                    "distance": distance,
                    "common_volume": common_volume,
                    "min": minimum,
                    "max_interference": 0,
                }
            )
        else:
            maximum = check.get("max", 0)
            if common_volume is None:
                results.append(
                    _indeterminate_check(
                        check,
                        "measurement_unavailable",
                        [f"common_volume={common_volume!r}"],
                    )
                )
                continue
            passed = common_volume <= maximum
            results.append(
                {
                    "id": check["id"],
                    "kind": kind,
                    "status": "pass" if passed else "fail",
                    "a": target_a["reference"],
                    "b": target_b["reference"],
                    "common_volume": common_volume,
                    "max": maximum,
                }
            )
    return results


def _handle_validate_geometry(ctx: Any, arguments: Mapping[str, Any]) -> dict:
    doc = ctx.require_document(arguments["document"])
    expected_solids = arguments.get("expected_solids")
    expected_bounds = arguments.get("expected_bounds") or {}
    tolerance = arguments.get("bounds_tolerance")
    if tolerance is None:
        tolerance = _DEFAULT_BOUNDS_TOLERANCE
    checks_input = arguments.get("checks")
    objects_requested = arguments.get("objects")
    if objects_requested is None and checks_input is None:
        raise ToolError(
            VALIDATION_FAILED,
            "validate_geometry requires objects or checks; it never defaults "
            "to every document object",
        )
    # Stage 1: normalize and prevalidate checks before any native access.
    checks = _normalize_geometry_checks(checks_input) if checks_input is not None else []
    # Stage 2: resolve every target; refusals here precede any expensive
    # measurement, and query targets record their bounded receipts.
    prepared_checks, receipts, owners = _prepare_geometry_checks(ctx, doc, checks)
    requested = owners[:_MAX_CHECK_TARGETS] if objects_requested is None else objects_requested
    entries = []
    for name in requested:
        obj = ctx.require_object(doc, name)
        report = geometry_report(obj, expected_solids)
        # Reported bounds are document-space: measured against the global
        # copied shape, never the local serialized placement. Shapeless
        # objects keep null bounds instead of failing.
        global_report = dict(report)
        if _shape_of(obj) is not None:
            global_report["bounds"] = _bbox(placed_shape(obj))
        entries.append(_geometry_entry(global_report, expected_solids, expected_bounds, tolerance))
    # Stage 3: evaluate the checks.
    results = _evaluate_geometry_checks(ctx, doc, prepared_checks)
    all_valid = all(entry["valid"] for entry in entries)
    result: dict[str, Any] = {
        "document": {
            "name": doc.Name,
            "generation": int(ctx.document_generation(doc)),
        },
        "units": {"length": "mm", "volume": "mm3", "tolerance": "mm"},
        "all_valid": all_valid,
        "objects": entries,
    }
    if checks_input is not None:
        checks_passed = all(row["status"] == "pass" for row in results)
        result["checks"] = results
        result["checksPassed"] = checks_passed
        # accepted is the single overall summary and never a substitute
        # for the raw evidence beside it.
        result["accepted"] = bool(all_valid and checks_passed)
        if receipts:
            result["resolvedSelections"] = receipts
    return result


# ---------------------------------------------------------------------------
# measure.
# ---------------------------------------------------------------------------


def _measure_distance(a_shape: Any, b_shape: Any, payload: dict) -> dict:
    try:
        distance, points, _info = a_shape.distToShape(b_shape)
    except Exception as exc:
        raise ToolError(VALIDATION_FAILED, f"distance computation failed: {exc}") from exc
    payload["distance"] = _finite(distance)
    if points:
        first = points[0]
        point_a = _point(first[0])
        point_b = _point(first[1])
        if point_a is not None and point_b is not None:
            payload["points"] = {"a": point_a, "b": point_b}
    return payload


def _measure_interference(a_shape: Any, b_shape: Any, payload: dict) -> dict:
    try:
        common = a_shape.common(b_shape)
    except Exception as exc:
        raise ToolError(VALIDATION_FAILED, f"common computation failed: {exc}") from exc
    try:
        raw_volume = getattr(common, "Volume", None)
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"common-volume computation returned no readable volume: {exc}",
            {"reason": "measurement_unavailable", "measurement": "common_volume"},
        ) from exc
    volume = _finite(raw_volume)
    if volume is None:
        raise ToolError(
            VALIDATION_FAILED,
            "common-volume computation returned no finite volume",
            {"reason": "measurement_unavailable", "measurement": "common_volume"},
        )
    payload["common_volume"] = volume
    payload["overlaps"] = volume > 0.0
    return payload


def _measure_difference(a_shape: Any, b_shape: Any, payload: dict) -> dict:
    """Report the volume of ``a`` not covered by ``b`` (``a.cut(b)``).

    An empty result means ``a`` is fully consumed by ``b``; it reports
    ``difference_volume`` 0 with null bounds and no solids rather than a
    missing volume, and OCC keeps such a shape non-null with an inverted
    bounding box (observed live on 1.1.3), so that box is reported as null
    instead of as its ±DBL_MAX coordinates. Volume, bounds and shape facts
    are mode-conditional fields of the ``measure`` output, like
    ``common_volume``.
    """

    try:
        result = a_shape.cut(b_shape)
    except Exception as exc:
        raise ToolError(VALIDATION_FAILED, f"cut computation failed: {exc}") from exc
    if shape_is_null(result):
        payload["difference_volume"] = 0.0
        payload["solid_count"] = 0
        payload["bounds"] = None
        payload["shape_valid"] = True
        return payload
    try:
        raw_volume = getattr(result, "Volume", None)
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"difference-volume computation returned no readable volume: {exc}",
            {"reason": "measurement_unavailable", "measurement": "difference_volume"},
        ) from exc
    volume = _finite(raw_volume)
    if volume is None:
        raise ToolError(
            VALIDATION_FAILED,
            "difference-volume computation returned no finite volume",
            {"reason": "measurement_unavailable", "measurement": "difference_volume"},
        )
    bounds = _bbox(result)
    payload["difference_volume"] = volume
    payload["bounds"] = None if _inverted_bounds(bounds) else bounds
    payload["solid_count"] = _solid_count(result)
    validity = getattr(result, "isValid", None)
    valid = True
    if callable(validity):
        try:
            valid = bool(validity())
        except Exception:
            valid = True
    payload["shape_valid"] = valid
    return payload


def _plane_vectors(
    plane: Mapping[str, Any],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return the normalized ``(normal, point)`` for a plane argument."""

    if "z" in plane:
        return (0.0, 0.0, 1.0), (0.0, 0.0, float(plane["z"]))
    normal = tuple(float(value) for value in plane["normal"])
    length = math.sqrt(sum(value * value for value in normal))
    if length < 1e-12:
        raise ToolError(
            VALIDATION_FAILED,
            "plane normal must be nonzero",
            {"parameter": "normal"},
        )
    point = tuple(float(value) for value in plane["point"])
    normalized = (normal[0] / length, normal[1] / length, normal[2] / length)
    return normalized, point


def _section_face(
    normal: tuple[float, float, float], point: tuple[float, float, float], shape: Any
) -> Any:
    """Build a finite planar face covering the shape's projection on the plane.

    The face lies in the plane through ``point`` with the given normal and is
    centered on the projection of the shape's bounding-box center, sized to
    twice the box diagonal so the section never clips. Built from an explicit
    polygon so the in-plane axes are deterministic.
    """

    import FreeCAD as App
    import Part

    bounds = _bbox(shape)
    if bounds is None:
        center = point
        diagonal = 100.0
    else:
        center = tuple((bounds[i] + bounds[i + 3]) / 2.0 for i in range(3))
        diagonal = math.dist(center, (bounds[3], bounds[4], bounds[5]))
    nx, ny, nz = normal
    px, py, pz = point
    offset = (center[0] - px) * nx + (center[1] - py) * ny + (center[2] - pz) * nz
    cx, cy, cz = (
        center[0] - nx * offset,
        center[1] - ny * offset,
        center[2] - nz * offset,
    )
    if abs(nz) > 0.9:
        ux, uy, uz = 1.0, 0.0, 0.0
    else:
        length = math.hypot(nx, ny)
        ux, uy, uz = -ny / length, nx / length, 0.0
    vx = ny * uz - nz * uy
    vy = nz * ux - nx * uz
    vz = nx * uy - ny * ux
    half = (2.0 * diagonal + 1.0) / 2.0
    corners = [
        (
            cx - ux * half - vx * half,
            cy - uy * half - vy * half,
            cz - uz * half - vz * half,
        ),
        (
            cx + ux * half - vx * half,
            cy + uy * half - vy * half,
            cz + uz * half - vz * half,
        ),
        (
            cx + ux * half + vx * half,
            cy + uy * half + vy * half,
            cz + uz * half + vz * half,
        ),
        (
            cx - ux * half + vx * half,
            cy - uy * half + vy * half,
            cz - uz * half + vz * half,
        ),
        (
            cx - ux * half - vx * half,
            cy - uy * half - vy * half,
            cz - uz * half - vz * half,
        ),
    ]
    wire = Part.makePolygon([App.Vector(*corner) for corner in corners])
    return Part.Face(wire)


def _measure_section(a_shape: Any, plane: Mapping[str, Any], payload: dict) -> dict:
    normal, point = _plane_vectors(plane)
    payload["plane"] = {"normal": list(normal), "point": list(point)}
    face = _section_face(normal, point, a_shape)
    try:
        compound = a_shape.section(face)
    except Exception as exc:
        raise ToolError(VALIDATION_FAILED, f"section computation failed: {exc}") from exc
    try:
        edges = list(getattr(compound, "Edges", ()) or ())
        wires = list(getattr(compound, "Wires", ()) or ())
    except Exception as exc:
        raise ToolError(VALIDATION_FAILED, f"section result access failed: {exc}") from exc
    curves = [_summarize_edge(edge) for edge in edges[:_MAX_CURVES]]
    total = 0.0
    for edge in edges:
        length = _finite(getattr(edge, "Length", None))
        if length is not None:
            total += length
    payload["curves"] = curves
    payload["edge_count"] = len(edges)
    payload["wire_count"] = len(wires)
    payload["total_length"] = total
    payload["truncated"] = len(edges) > _MAX_CURVES
    return payload


def _summarize_edge(edge: Any) -> dict:
    import Part

    length = _finite(getattr(edge, "Length", None))
    curve = getattr(edge, "Curve", None)
    circle = getattr(Part, "Circle", None)
    line = getattr(Part, "Line", None)
    if circle is not None and isinstance(curve, circle):
        return {
            "kind": "circle",
            "radius": _finite(getattr(curve, "Radius", None)),
            "center": _point(getattr(curve, "Center", None)),
            "closed": bool(edge.isClosed()),
            "length": length,
        }
    if line is not None and isinstance(curve, line):
        try:
            vertexes = list(getattr(edge, "Vertexes", ()) or ())
        except Exception:
            vertexes = []
        entry: dict[str, Any] = {"kind": "line", "length": length}
        if vertexes:
            start = _point(vertexes[0].Point)
            if start is not None:
                entry["start"] = start
        if len(vertexes) > 1:
            end = _point(vertexes[-1].Point)
            if end is not None:
                entry["end"] = end
        return entry
    return {"kind": "other", "length": length}


def _face_summary(face: Any) -> dict:
    summary: dict[str, Any] = {"area": _finite(getattr(face, "Area", None))}
    try:
        u1, u2, v1, v2 = face.ParameterRange
        mid_u = (u1 + u2) / 2.0
        mid_v = (v1 + v2) / 2.0
        normal = _point(face.normalAt(mid_u, mid_v))
        center = _point(face.valueAt(mid_u, mid_v))
    except Exception:
        return summary
    if normal is not None:
        summary["normal"] = normal
    if center is not None:
        summary["center"] = center
    return summary


def _measure_faces(ctx: Any, doc: Any, obj: Any, selection: Mapping | None, payload: dict) -> dict:
    shape = _target_shape(obj, selection)
    if shape is None:
        raise ToolError(VALIDATION_FAILED, f"object {obj.Name} has no shape")
    payload["a"] = _reference_for(ctx, doc, obj, selection)
    face_count: int
    if selection is not None:
        faces = [(selection["index"], selection["shape"])]
        truncated = False
        face_count = 1
    else:
        try:
            all_faces = list(shape.Faces)
        except Exception:
            all_faces = []
        truncated = len(all_faces) > _MAX_FACES
        face_count = len(all_faces)
        faces = list(enumerate(all_faces[:_MAX_FACES], 1))
    entries = []
    for index, face in faces:
        entry = {
            "index": index,
            "reference": make_reference(ctx, doc, obj, "face", index),
        }
        entry.update(_face_summary(face))
        entries.append(entry)
    payload["faces"] = entries
    payload["face_count"] = face_count
    payload["truncated"] = truncated
    return payload


def _handle_measure(ctx: Any, arguments: Mapping[str, Any]) -> dict:
    doc = ctx.require_document(arguments["document"])
    mode = arguments["mode"]
    a_obj, a_sel = _resolve_target(ctx, doc, arguments["a"], "a")
    payload: dict[str, Any] = {
        "mode": mode,
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "units": {"length": "mm", "area": "mm2", "volume": "mm3"},
    }
    if mode == "faces":
        if a_sel is not None and a_sel["role"] != "face":
            raise ToolError(
                VALIDATION_FAILED,
                "mode 'faces' requires a whole-object or face target",
            )
        return _measure_faces(ctx, doc, a_obj, a_sel, payload)
    if mode not in ("distance", "interference", "section", "difference"):
        raise ToolError(VALIDATION_FAILED, f"unsupported measure mode {mode!r}")
    if mode == "section":
        if arguments.get("plane") is None:
            raise ToolError(VALIDATION_FAILED, "mode 'section' requires a 'plane'")
        if arguments.get("b") is not None:
            raise ToolError(VALIDATION_FAILED, "mode 'section' takes only 'a' and 'plane'")
        payload["a"] = _reference_for(ctx, doc, a_obj, a_sel)
        a_shape = _target_shape(a_obj, a_sel)
        if a_shape is None:
            raise ToolError(VALIDATION_FAILED, f"object {a_obj.Name} has no shape")
        return _measure_section(a_shape, arguments["plane"], payload)
    if arguments.get("b") is None:
        raise ToolError(VALIDATION_FAILED, f"mode {mode!r} requires a 'b' target")
    b_obj, b_sel = _resolve_target(ctx, doc, arguments["b"], "b")
    payload["a"] = _reference_for(ctx, doc, a_obj, a_sel)
    payload["b"] = _reference_for(ctx, doc, b_obj, b_sel)
    a_shape = _target_shape(a_obj, a_sel)
    b_shape = _target_shape(b_obj, b_sel)
    if a_shape is None:
        raise ToolError(VALIDATION_FAILED, f"object {a_obj.Name} has no shape")
    if b_shape is None:
        raise ToolError(VALIDATION_FAILED, f"object {b_obj.Name} has no shape")
    if mode == "distance":
        return _measure_distance(a_shape, b_shape, payload)
    if mode == "interference":
        return _measure_interference(a_shape, b_shape, payload)
    return _measure_difference(a_shape, b_shape, payload)


# ---------------------------------------------------------------------------
# inspect_topology.
# ---------------------------------------------------------------------------


def _type_name(value: Any) -> str | None:
    """Native geometry type name, or None when unavailable."""

    if value is None:
        return None
    return type(value).__name__


def _first_vertex_point(edge: Any, last: bool) -> list[float] | None:
    try:
        vertexes = list(getattr(edge, "Vertexes", ()) or ())
    except Exception:
        return None
    if not vertexes:
        return None
    target = vertexes[-1] if last else vertexes[0]
    return _point(getattr(target, "Point", None))


def _topology_face_item(
    ctx: Any, doc: Any, obj: Any, index: int, face: Any, detail: str = "full"
) -> dict:
    surface = getattr(face, "Surface", None)
    item = {
        "index": index,
        "reference": make_reference(ctx, doc, obj, "face", index),
        "bounds": _bbox(face),
        "surfaceType": _type_name(surface),
    }
    if detail == "compact":
        return item
    summary = _face_summary(face)
    item.update(
        {
            "area": summary.get("area"),
            "center": summary.get("center"),
            "normal": summary.get("normal"),
            "radius": _finite(getattr(surface, "Radius", None)),
            "axis": _point(getattr(surface, "Axis", None)),
        }
    )
    return item


def _topology_edge_item(
    ctx: Any, doc: Any, obj: Any, index: int, edge: Any, detail: str = "full"
) -> dict:
    curve = getattr(edge, "Curve", None)
    item = {
        "index": index,
        "reference": make_reference(ctx, doc, obj, "edge", index),
        "bounds": _bbox(edge),
        "curveType": _type_name(curve),
    }
    if detail == "compact":
        return item
    try:
        closed = bool(edge.isClosed())
    except Exception:
        closed = None
    item.update(
        {
            "length": _finite(getattr(edge, "Length", None)),
            "closed": closed,
            "start": _first_vertex_point(edge, last=False),
            "end": _first_vertex_point(edge, last=True),
            "center": _point(getattr(curve, "Center", None)),
            "radius": _finite(getattr(curve, "Radius", None)),
            "axis": _point(getattr(curve, "Axis", None)),
        }
    )
    return item


def _topology_cursor_payload(
    ctx: Any,
    doc: Any,
    object_name: str,
    role: str,
    limit: int,
    last: int,
    query_hash: str,
) -> dict:
    return {
        "kind": "topology-page",
        "identity": str(ctx.document_identity(doc)),
        "generation": int(ctx.document_generation(doc)),
        "object": object_name,
        "role": role,
        "limit": limit,
        "queryHash": query_hash,
        "last": last,
    }


def _open_topology_cursor(
    ctx: Any,
    doc: Any,
    cursor: str,
    object_name: str,
    role: str,
    limit: int,
    query_hash: str,
) -> int:
    """Return the matched-list position, rejecting stale or changed cursors."""

    try:
        payload = ctx.signer.verify(DOMAIN_CURSOR, cursor)
    except ProtocolError as exc:
        raise ToolError(
            VALIDATION_FAILED,
            "topology cursor signature rejected; restart from the first page",
            {"reason": "malformed_cursor", "nextTool": "inspect_topology"},
        ) from exc
    expected = _topology_cursor_payload(ctx, doc, object_name, role, limit, 0, query_hash)
    if payload.get("kind") != "topology-page":
        raise _stale_topology_cursor()
    for key in ("identity", "generation", "object", "role", "limit", "queryHash"):
        if payload.get(key) != expected[key]:
            raise _stale_topology_cursor()
    last = payload.get("last")
    if isinstance(last, bool) or not isinstance(last, int) or last < 1:
        raise ToolError(
            VALIDATION_FAILED,
            "topology cursor carries a malformed page index",
            {"reason": "malformed_cursor", "nextTool": "inspect_topology"},
        )
    return last


def _stale_topology_cursor() -> ToolError:
    return ToolError(
        VALIDATION_FAILED,
        "topology cursor is stale or was built for a different request; "
        "restart from the first page",
        {"reason": "stale_cursor", "nextTool": "inspect_topology"},
    )


def _handle_inspect_topology(ctx: Any, arguments: Mapping[str, Any]) -> dict:
    doc = ctx.require_document(arguments["document"])
    detail = str(arguments.get("detail") or "compact")
    limit = arguments.get("limit")
    limit = _DEFAULT_TOPOLOGY_PAGE if limit is None else int(limit)
    limit = max(1, min(_MAX_TOPOLOGY_PAGE, limit))
    target = arguments["target"]
    if not isinstance(target, Mapping):
        raise ToolError(VALIDATION_FAILED, "target must be a shared target object")

    if "query" in target:
        obj, role, indices = resolve_query(ctx, doc, target, "target")
        query_hash = fingerprint(target)
    else:
        obj, role, native = _resolve_reference_parts(ctx, doc, target, "target")
        query_hash = fingerprint({"object": obj.Name} if native == "" else dict(target))
        if native == "":
            # A whole object enumerates its faces by default; a caller that
            # wants edges passes an explicit one-step edge query.
            role = "face"
            indices = list(range(1, len(_root_subshapes(placed_shape(obj), role)) + 1))
        else:
            indices = [int(native[4:])]
        if native:
            role = "face" if native.startswith("Face") else "edge"

    start_after = 0
    cursor = arguments.get("cursor")
    if cursor:
        start_after = _open_topology_cursor(
            ctx, doc, str(cursor), obj.Name, role, limit, query_hash
        )

    subshapes = _root_subshapes(placed_shape(obj), role)
    total = len(indices)
    build = _topology_face_item if role == "face" else _topology_edge_item
    page = indices[start_after : start_after + limit]
    items = [build(ctx, doc, obj, index, subshapes[index - 1], detail) for index in page]
    next_cursor = None
    if start_after + limit < total:
        next_cursor = ctx.signer.sign(
            DOMAIN_CURSOR,
            _topology_cursor_payload(
                ctx, doc, obj.Name, role, limit, start_after + limit, query_hash
            ),
        )
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "object": str(getattr(obj, "Name", "")),
        "role": role,
        "total": total,
        "count": len(items),
        "items": items,
        "nextCursor": next_cursor,
    }


# ---------------------------------------------------------------------------
# Subshape fingerprints.
#
# A post-normalization re-verification must prove that a query still names
# the same geometry, not merely the same index: a base recompute legitimately
# regenerates equivalent subshapes as brand-new native objects (which native
# ``isSame`` rejects), while a genuine geometry change must still fail closed.
# The fingerprint is the plain-data, document-space evidence for that
# comparison; every read is guarded and an unreadable field stays ``None`` so
# the comparator refuses instead of matching on absence.
# ---------------------------------------------------------------------------

#: Mapped type names whose analytic object carries a readable ``Radius``.
_RADIUS_TYPES = ("CIRCLE", "CYLINDER", "SPHERE")
#: Mapped type names whose analytic object carries a readable ``Axis``.
_AXIS_TYPES = ("CIRCLE", "CYLINDER", "CONE", "TORUS")

#: Fields every fingerprint must carry for a comparison to mean anything.
_FINGERPRINT_REQUIRED_FIELDS = ("type", "bounds", "measure", "center", "point")

#: Geometric fields, compared within ``_DEFAULT_BOUNDS_TOLERANCE``; ``role``,
#: ``type`` and ``closed`` are discrete and compared exactly.
_FINGERPRINT_GEOMETRY_FIELDS = (
    "bounds",
    "measure",
    "center",
    "normal",
    "point",
    "start",
    "end",
    "radius",
    "axis",
)


def _guarded_attribute(owner: Any, name: str) -> Any:
    """Read ``owner.name``, returning None when the read itself fails."""

    try:
        return getattr(owner, name, None)
    except Exception:
        return None


def _guarded_call(owner: Any, name: str, *arguments: Any) -> Any:
    """Call a native method under guard, returning None when absent or failed."""

    method = _guarded_attribute(owner, name)
    if not callable(method):
        return None
    try:
        return method(*arguments)
    except Exception:
        return None


def _mapped_type(role: str, analytic: Any) -> str | None:
    """The mapped analytic type of one Surface/Curve, or None when unproven.

    Classification reuses the exact native ``Surface``/``Curve`` class name
    and the shared type tables, so an unreadable or unmapped geometry class
    stays None and can never masquerade as another type in a fingerprint.
    """

    table = _FACE_TYPE_NAMES if role == "face" else _EDGE_TYPE_NAMES
    return table.get(_type_name(analytic) or "")


def _parameter_midpoints(subshape: Any, count: int) -> tuple[float, ...] | None:
    """The midpoint of a ``count``-dimensional parameter range, or None."""

    try:
        bounds = [float(value) for value in subshape.ParameterRange]
    except Exception:
        return None
    if len(bounds) != 2 * count:
        return None
    midpoints = tuple((bounds[index] + bounds[index + count]) / 2.0 for index in range(count))
    return midpoints if all(math.isfinite(value) for value in midpoints) else None


def _normalized_direction(vector: Any) -> tuple[float, float, float] | None:
    """A unit normal/tangent/axis as a frozen tuple, or None when unusable."""

    direction = _normalize_unit(vector)
    return None if direction is None else tuple(direction)


def _frozen(values: Any) -> tuple[float, ...] | None:
    """Freeze one coordinate list into an immutable tuple, or None."""

    return None if values is None else tuple(values)


def subshape_fingerprint(role: str, subshape: Any) -> dict:
    """Document-space evidence identifying one face or edge.

    Every field is read under its own guard and stays ``None`` when the read
    fails or does not apply; every sequence is a frozen tuple of finite
    floats, so the result is deep-immutable plain data holding no reference
    to the shape it was taken from and stays valid after a recompute
    regenerates that shape. The evidence deliberately covers more than
    bounds, center and length: the mapped analytic type and closed status
    are discrete, and the parameter-midpoint point value, plane/line
    direction, endpoint vertices and analytic radius/axis are what separate
    geometry whose coarse facts agree.
    """

    analytic = _guarded_attribute(subshape, "Surface" if role == "face" else "Curve")
    mapped = _mapped_type(role, analytic)
    fingerprint: dict[str, Any] = {
        "role": role,
        "type": mapped,
        "closed": None,
        "bounds": _frozen(_bbox(subshape)),
        "measure": _finite(_guarded_attribute(subshape, "Area" if role == "face" else "Length")),
        "center": _frozen(_point(_guarded_attribute(subshape, "CenterOfMass"))),
        "normal": None,
        "point": None,
        "start": None,
        "end": None,
        "radius": None,
        "axis": None,
    }
    if role == "face":
        midpoints = _parameter_midpoints(subshape, 2)
        if midpoints is not None:
            side_u, side_v = midpoints
            point = _guarded_call(subshape, "valueAt", side_u, side_v)
            fingerprint["point"] = _frozen(_point(point))
            if mapped == "PLANE":
                # Only the plane normal is required evidence: the midpoint
                # normal of a closed curved surface can land on a seam or pole.
                fingerprint["normal"] = _normalized_direction(
                    _guarded_call(subshape, "normalAt", side_u, side_v)
                )
    else:
        closed = _guarded_call(subshape, "isClosed")
        fingerprint["closed"] = None if closed is None else bool(closed)
        fingerprint["start"] = _frozen(_first_vertex_point(subshape, last=False))
        fingerprint["end"] = _frozen(_first_vertex_point(subshape, last=True))
        midpoint = _parameter_midpoints(subshape, 1)
        if midpoint is not None:
            fingerprint["point"] = _frozen(_point(_guarded_call(subshape, "valueAt", midpoint[0])))
            if mapped == "LINE":
                fingerprint["normal"] = _normalized_direction(
                    _guarded_call(subshape, "tangentAt", midpoint[0])
                )
    if mapped in _RADIUS_TYPES:
        fingerprint["radius"] = _finite(_guarded_attribute(analytic, "Radius"))
    if mapped in _AXIS_TYPES:
        fingerprint["axis"] = _normalized_direction(_guarded_attribute(analytic, "Axis"))
    return fingerprint


def _fingerprint_gaps(fingerprint: Mapping) -> list[str]:
    """The fields one fingerprint cannot prove; empty when it is complete.

    Beyond what every subshape proves (mapped type, document-space bounds,
    area/length, center of mass and parameter-midpoint point), each role and
    mapped type adds the evidence that distinguishes it: an edge its closed
    status and, when open, both endpoint vertices; a plane face its midpoint
    normal and a line edge its tangent; a radius/axis analytic type its
    radius/axis. A field that is missing or unreadable is a gap, so an
    unsupported read can never compare equal.
    """

    required = list(_FINGERPRINT_REQUIRED_FIELDS)
    if fingerprint.get("role") == "edge":
        required.append("closed")
        if fingerprint.get("closed") is not True:
            # A closed edge has no meaningful endpoints; an open one does.
            required.extend(("start", "end"))
    if fingerprint.get("type") in ("PLANE", "LINE"):
        required.append("normal")
    if fingerprint.get("type") in _RADIUS_TYPES:
        required.append("radius")
    if fingerprint.get("type") in _AXIS_TYPES:
        required.append("axis")
    return [name for name in required if fingerprint.get(name) is None]


def _within_tolerance(left: Any, right: Any) -> bool:
    """Tolerance-equality for one fingerprint field, recursing over sequences."""

    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
            return False
        if len(left) != len(right):
            return False
        return all(_within_tolerance(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    try:
        return abs(float(left) - float(right)) <= _DEFAULT_BOUNDS_TOLERANCE
    except (TypeError, ValueError):
        return False


def fingerprints_match(expected: Any, fresh: Any) -> bool:
    """Strict same-index correspondence between two subshape fingerprints.

    True only when both fingerprints are complete (no
    :func:`_fingerprint_gaps`) and agree exactly on the discrete fields
    (role, mapped type, closed status) and within ``_DEFAULT_BOUNDS_TOLERANCE``
    on every geometric field. A geometric field readable in only one of the
    two is a mismatch, while fields neither one carries (an inapplicable
    radius, a face's closed status) are ignored. Regenerated but
    geometrically equivalent subshapes compare equal; anything else fails
    closed.
    """

    if not isinstance(expected, Mapping) or not isinstance(fresh, Mapping):
        return False
    if _fingerprint_gaps(expected) or _fingerprint_gaps(fresh):
        return False
    if expected.get("role") != fresh.get("role") or expected.get("type") != fresh.get("type"):
        return False
    if expected.get("closed") != fresh.get("closed"):
        return False
    for name in _FINGERPRINT_GEOMETRY_FIELDS:
        left, right = expected.get(name), fresh.get(name)
        if left is None or right is None:
            if (left is None) != (right is None):
                return False
            continue
        if not _within_tolerance(left, right):
            return False
    return True


def subshape_fingerprints(shape: Any, role: str, indices: Iterable[int]) -> dict[int, dict] | None:
    """Fingerprints for the named 1-based ``role`` subshapes of ``shape``.

    Returns the complete requested index set, or ``None`` when the subshape
    array is unreadable or an index falls outside it: absent evidence is
    reported by the caller as ``selection_changed``, never as a match.
    """

    try:
        subshapes = _root_subshapes(shape, role)
    except ToolError:
        return None
    fingerprints: dict[int, dict] = {}
    for index in list(indices):
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if not 1 <= index <= len(subshapes):
            return None
        fingerprints[index] = subshape_fingerprint(role, subshapes[index - 1])
    return fingerprints


# ---------------------------------------------------------------------------
# Schemas.
# ---------------------------------------------------------------------------


_BOUNDS_ARRAY_DEF = {
    "type": ["array", "null"],
    "items": {"type": "number"},
    "minItems": 6,
    "maxItems": 6,
}

_POINT_DEF = {
    "type": ["array", "null"],
    "items": {"type": "number"},
    "minItems": 3,
    "maxItems": 3,
}

# Signed subshape reference (output): the subelement is always a nonempty
# opaque token; whole objects serialize as {"object"} with no subelement.
_REFERENCE_DEF = {
    "anyOf": [
        {"$ref": "#/$defs/topologyWholeTarget"},
        {"$ref": "#/$defs/topologyReferenceTarget"},
    ]
}

_TOPOLOGY_FACE_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "index",
        "reference",
        "bounds",
        "surfaceType",
    ],
    "properties": {
        "index": {"type": "integer", "minimum": 1},
        "reference": {"$ref": "#/$defs/topologyReferenceTarget"},
        "bounds": _BOUNDS_ARRAY_DEF,
        "area": {"type": ["number", "null"]},
        "center": {"$ref": "#/$defs/point"},
        "normal": {"$ref": "#/$defs/point"},
        "surfaceType": {"type": ["string", "null"]},
        "radius": {"type": ["number", "null"]},
        "axis": {"$ref": "#/$defs/point"},
    },
}

_TOPOLOGY_EDGE_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "index",
        "reference",
        "bounds",
        "curveType",
    ],
    "properties": {
        "index": {"type": "integer", "minimum": 1},
        "reference": {"$ref": "#/$defs/topologyReferenceTarget"},
        "bounds": _BOUNDS_ARRAY_DEF,
        "length": {"type": ["number", "null"]},
        "curveType": {"type": ["string", "null"]},
        "closed": {"type": ["boolean", "null"]},
        "start": {"$ref": "#/$defs/point"},
        "end": {"$ref": "#/$defs/point"},
        "center": {"$ref": "#/$defs/point"},
        "radius": {"type": ["number", "null"]},
        "axis": {"$ref": "#/$defs/point"},
    },
}

_INSPECT_TOPOLOGY_INPUT = tq.merge_query_defs(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["document", "target"],
        "properties": {
            "document": {"type": "string", "minLength": 1},
            "target": {"$ref": "#/$defs/topologyTarget"},
            "cursor": {"type": ["string", "null"]},
            "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_TOPOLOGY_PAGE,
                "default": _DEFAULT_TOPOLOGY_PAGE,
            },
        },
    }
)

_INSPECT_TOPOLOGY_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "document",
        "generation",
        "object",
        "role",
        "total",
        "count",
        "items",
        "nextCursor",
    ],
    "properties": {
        "document": {"type": "string"},
        "generation": {"type": "integer", "minimum": 0},
        "object": {"type": "string"},
        "role": {"enum": ["face", "edge"]},
        "total": {"type": "integer", "minimum": 0},
        "count": {"type": "integer", "minimum": 0},
        "items": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"$ref": "#/$defs/faceItem"},
                    {"$ref": "#/$defs/edgeItem"},
                ]
            },
            "maxItems": _MAX_TOPOLOGY_PAGE,
        },
        "nextCursor": {"type": ["string", "null"]},
    },
    "$defs": {
        "point": _POINT_DEF,
        "bounds": _BOUNDS_ARRAY_DEF,
        "topologyWholeTarget": tq.QUERY_DEFS["topologyWholeTarget"],
        "topologyReferenceTarget": tq.QUERY_DEFS["topologyReferenceTarget"],
        "faceItem": _TOPOLOGY_FACE_ITEM,
        "edgeItem": _TOPOLOGY_EDGE_ITEM,
    },
}

_PLANE_DEF = {
    "anyOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"z": {"type": "number"}},
            "required": ["z"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "normal": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                },
                "point": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                },
            },
            "required": ["normal", "point"],
        },
    ]
}


_CHECK_TARGET_DEF = {"$ref": "#/$defs/topologyTarget"}

_CHECK_VOLUME_RANGE_DEF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "object"],
    "properties": {
        "kind": {"const": "volume_range"},
        "id": {"type": "string", "minLength": 1, "maxLength": 64},
        "object": _CHECK_TARGET_DEF,
        "min": {"type": "number", "minimum": 0},
        "max": {"type": "number", "minimum": 0},
    },
}

_CHECK_CLEARANCE_DEF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "a", "b", "min"],
    "properties": {
        "kind": {"const": "clearance_min"},
        "id": {"type": "string", "minLength": 1, "maxLength": 64},
        "a": _CHECK_TARGET_DEF,
        "b": _CHECK_TARGET_DEF,
        "min": {"type": "number", "exclusiveMinimum": 0},
    },
}

_CHECK_INTERFERENCE_DEF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "a", "b"],
    "properties": {
        "kind": {"const": "interference_max"},
        "id": {"type": "string", "minLength": 1, "maxLength": 64},
        "a": _CHECK_TARGET_DEF,
        "b": _CHECK_TARGET_DEF,
        "max": {"type": "number", "minimum": 0},
    },
}

_VALIDATE_GEOMETRY_INPUT = tq.merge_query_defs(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "document": {"type": "string", "minLength": 1},
            "objects": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": _MAX_CHECK_TARGETS,
            },
            "checks": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"$ref": "#/$defs/checkVolumeRange"},
                        {"$ref": "#/$defs/checkClearanceMin"},
                        {"$ref": "#/$defs/checkInterferenceMax"},
                    ]
                },
                "minItems": 1,
                "maxItems": _MAX_CHECKS,
            },
            "expected_solids": {"type": "integer", "minimum": 0, "maximum": 100000},
            "expected_bounds": {
                "type": "object",
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 6,
                    "maxItems": 6,
                },
            },
            "bounds_tolerance": {
                "type": "number",
                "minimum": 0,
                "maximum": 1000000,
                "default": _DEFAULT_BOUNDS_TOLERANCE,
            },
        },
        "required": ["document"],
        "$defs": {
            "checkVolumeRange": _CHECK_VOLUME_RANGE_DEF,
            "checkClearanceMin": _CHECK_CLEARANCE_DEF,
            "checkInterferenceMax": _CHECK_INTERFERENCE_DEF,
        },
    }
)

_CHECK_RESULT_COMMON = {
    "id": {"type": "string", "minLength": 1},
    "kind": {"enum": ["volume_range", "clearance_min", "interference_max"]},
    "status": {"enum": ["pass", "fail"]},
}

_CHECK_RESULT_VOLUME = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "status", "object", "measured"],
    "properties": {
        **_CHECK_RESULT_COMMON,
        "object": {
            "anyOf": [
                {"$ref": "#/$defs/topologyWholeTarget"},
                {"$ref": "#/$defs/topologyReferenceTarget"},
            ]
        },
        "measured": {"type": ["number", "null"]},
        "min": {"type": "number", "minimum": 0},
        "max": {"type": "number", "minimum": 0},
    },
}

_CHECK_TARGET_OUT = {
    "anyOf": [
        {"$ref": "#/$defs/topologyWholeTarget"},
        {"$ref": "#/$defs/topologyReferenceTarget"},
    ]
}

_CHECK_RESULT_CLEARANCE = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "kind",
        "status",
        "a",
        "b",
        "distance",
        "common_volume",
        "min",
        "max_interference",
    ],
    "properties": {
        **_CHECK_RESULT_COMMON,
        "a": _CHECK_TARGET_OUT,
        "b": _CHECK_TARGET_OUT,
        "distance": {"type": ["number", "null"]},
        "common_volume": {"type": ["number", "null"]},
        "min": {"type": "number"},
        "max_interference": {"const": 0},
    },
}

_CHECK_RESULT_INTERFERENCE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "status", "a", "b", "common_volume", "max"],
    "properties": {
        **_CHECK_RESULT_COMMON,
        "a": _CHECK_TARGET_OUT,
        "b": _CHECK_TARGET_OUT,
        "common_volume": {"type": ["number", "null"]},
        "max": {"type": "number", "minimum": 0},
    },
}

_CHECK_RESULT_INDETERMINATE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "status", "reason"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "kind": {"enum": ["volume_range", "clearance_min", "interference_max"]},
        "status": {"const": "indeterminate"},
        "reason": {
            "enum": [
                "measurement_unavailable",
                "non_volumetric_target",
                "target_invalid",
                "target_shapeless",
            ]
        },
        "diagnostics": {
            "type": "array",
            "items": {"type": "string", "maxLength": _MAX_CHECK_DIAGNOSTIC_LENGTH},
            "maxItems": _MAX_CHECK_DIAGNOSTICS,
        },
    },
}

_RESOLVED_SELECTIONS_DEF = {
    "type": "array",
    "items": {"$ref": "#/$defs/topologyResolvedSelection"},
    "minItems": 1,
    "maxItems": tq.MAX_QUERY_REFERENCES,
}

_VALIDATE_GEOMETRY_OUTPUT = tq.merge_query_defs(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "document": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "generation": {"type": "integer", "minimum": 0},
                },
                "required": ["name", "generation"],
            },
            "units": {"type": "object", "additionalProperties": {"type": "string"}},
            "all_valid": {"type": "boolean"},
            "objects": {
                "type": "array",
                "items": {"$ref": "#/$defs/objectReport"},
                "maxItems": 100,
            },
            "checks": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"$ref": "#/$defs/checkResultVolume"},
                        {"$ref": "#/$defs/checkResultClearance"},
                        {"$ref": "#/$defs/checkResultInterference"},
                        {"$ref": "#/$defs/checkResultIndeterminate"},
                    ]
                },
                "maxItems": _MAX_CHECKS,
            },
            "checksPassed": {"type": "boolean"},
            "accepted": {"type": "boolean"},
            "resolvedSelections": _RESOLVED_SELECTIONS_DEF,
        },
        "required": ["document", "units", "all_valid", "objects"],
        "$defs": {
            "bounds": _BOUNDS_ARRAY_DEF,
            "boundsVerdict": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "verdict": {"enum": ["match", "mismatch", "unavailable"]},
                    "expected": {"$ref": "#/$defs/bounds"},
                    "deviations": {
                        "type": ["array", "null"],
                        "items": {"type": "number"},
                        "minItems": 6,
                        "maxItems": 6,
                    },
                },
                "required": ["verdict"],
            },
            "verdicts": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "solids": {"enum": ["match", "mismatch", "unavailable", "unspecified"]},
                    "volume": {"enum": ["positive", "nonpositive", "not_applicable"]},
                    "bounds": {"$ref": "#/$defs/boundsVerdict"},
                },
                "required": ["solids", "volume"],
            },
            "objectReport": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "state": {"type": "array", "items": {"type": "string"}},
                    "object_valid": {"type": "boolean"},
                    "shape_valid": {"type": ["boolean", "null"]},
                    "solid_count": {"type": ["integer", "null"], "minimum": 0},
                    "volume": {"type": ["number", "null"]},
                    "bounds": {"$ref": "#/$defs/bounds"},
                    "max_tolerance": {"type": ["number", "null"]},
                    "diagnostics": {"type": "array", "items": {"type": "string"}},
                    "error": {"type": ["string", "null"]},
                    "verdicts": {"$ref": "#/$defs/verdicts"},
                    "valid": {"type": "boolean"},
                },
                "required": [
                    "name",
                    "state",
                    "object_valid",
                    "shape_valid",
                    "solid_count",
                    "volume",
                    "bounds",
                    "max_tolerance",
                    "diagnostics",
                    "error",
                    "verdicts",
                    "valid",
                ],
            },
            "checkResultVolume": _CHECK_RESULT_VOLUME,
            "checkResultClearance": _CHECK_RESULT_CLEARANCE,
            "checkResultInterference": _CHECK_RESULT_INTERFERENCE,
            "checkResultIndeterminate": _CHECK_RESULT_INDETERMINATE,
        },
    }
)

_MEASURE_INPUT = tq.merge_query_defs(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "document": {"type": "string", "minLength": 1},
            "a": {"$ref": "#/$defs/topologyTarget"},
            "mode": {"enum": ["distance", "interference", "section", "difference", "faces"]},
            "b": {"$ref": "#/$defs/topologyTarget"},
            "plane": _PLANE_DEF,
        },
        "required": ["document", "a", "mode"],
    }
)

_MEASURE_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mode": {"enum": ["distance", "interference", "section", "difference", "faces"]},
        "document": {"type": "string", "minLength": 1},
        "generation": {"type": "integer", "minimum": 0},
        "units": {"type": "object", "additionalProperties": {"type": "string"}},
        "a": {"$ref": "#/$defs/reference"},
        "b": {"$ref": "#/$defs/reference"},
        "distance": {"type": ["number", "null"]},
        "points": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "a": {"$ref": "#/$defs/point"},
                "b": {"$ref": "#/$defs/point"},
            },
            "required": ["a", "b"],
        },
        "common_volume": {"type": "number"},
        "overlaps": {"type": "boolean"},
        "difference_volume": {"type": "number"},
        "solid_count": {"type": ["integer", "null"], "minimum": 0},
        "bounds": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 6,
            "maxItems": 6,
        },
        "shape_valid": {"type": "boolean"},
        "plane": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "normal": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                },
                "point": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                },
            },
            "required": ["normal", "point"],
        },
        "curves": {
            "type": "array",
            "items": {"$ref": "#/$defs/curve"},
            "maxItems": _MAX_CURVES,
        },
        "edge_count": {"type": "integer", "minimum": 0},
        "wire_count": {"type": "integer", "minimum": 0},
        "face_count": {"type": "integer", "minimum": 0},
        "total_length": {"type": "number"},
        "truncated": {"type": "boolean"},
        "faces": {
            "type": "array",
            "items": {"$ref": "#/$defs/faceReport"},
            "maxItems": _MAX_FACES,
        },
    },
    "required": ["mode", "units", "document", "generation"],
    "$defs": {
        "point": _POINT_DEF,
        "topologyWholeTarget": tq.QUERY_DEFS["topologyWholeTarget"],
        "topologyReferenceTarget": tq.QUERY_DEFS["topologyReferenceTarget"],
        "reference": _REFERENCE_DEF,
        "curve": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"enum": ["line", "circle", "other"]},
                "length": {"type": ["number", "null"]},
                "radius": {"type": ["number", "null"]},
                "center": {"$ref": "#/$defs/point"},
                "closed": {"type": "boolean"},
                "start": {"$ref": "#/$defs/point"},
                "end": {"$ref": "#/$defs/point"},
            },
            "required": ["kind"],
        },
        "faceReport": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "index": {"type": "integer", "minimum": 1},
                "area": {"type": ["number", "null"]},
                "normal": {"$ref": "#/$defs/point"},
                "center": {"$ref": "#/$defs/point"},
                "reference": {"$ref": "#/$defs/topologyReferenceTarget"},
            },
            "required": ["index", "reference"],
        },
    },
}

TOOL_DEFINITIONS = [
    {
        "name": "validate_geometry",
        "description": (
            "Validate object geometry in document (global) coordinates: state,"
            " shape validity, solid count, volume, bounds, shape.check"
            " diagnostics and maximum tolerance. Optional expected_solids and"
            " per-object expected_bounds (six document-space mm coordinates)"
            " produce explicit verdicts; nothing is repaired. Optional"
            " declarative checks add compact named acceptance evidence:"
            " volume_range gates a solid's volume between inclusive mm3"
            " bounds, clearance_min passes only when minimum distance >= min"
            " AND common volume is zero (positive clearance evidence), and"
            " interference_max passes when common volume <= max (touching is"
            " permitted, not clearance). Check targets use the shared"
            " whole/signed/query vocabulary; every fit target must be a"
            " valid solid with positive volume, otherwise the row reports"
            " indeterminate instead of passing. checksPassed is true only"
            " when every check passes; accepted combines it with the object"
            " reports and never replaces the raw evidence. objects may be"
            " omitted when checks are supplied: the owner objects of the"
            " check targets are then reported in first-use order; with no"
            " checks, objects is required and check-only fields are absent."
        ),
        "inputSchema": _VALIDATE_GEOMETRY_INPUT,
        "outputSchema": _VALIDATE_GEOMETRY_OUTPUT,
    },
    {
        "name": "measure",
        "description": (
            "Measure geometry in document (global) coordinates between"
            " shared targets: a whole object ({object}), a signed reference"
            " ({object, subelement}) or a declarative query ({object,"
            " query}) that must resolve to exactly one subshape (zero"
            " matches are selection_empty, several are"
            " selection_ambiguous). Modes: distance (distToShape),"
            " interference (common volume), difference (a.cut(b) volume —"
            " the material of a that b does not cover; an empty result"
            " reports difference_volume 0 with null bounds), planar section"
            " curves (z plane or normal+point in document space) or face"
            " areas with sampled normals. Query selectors use the shared"
            ' CadQuery-style grammar (e.g. {"role":"face","selector":">Z"}'
            ' chained to {"role":"edge","selector":"%CIRCLE"}). Semantics:'
            " distance is the raw distToShape value and a positive result"
            " does not prove separation (OCC can report a positive distance"
            " for intersecting shapes; FreeCAD issue #25158); interference"
            " detects positive common volume only, so tangential or"
            " surface/edge-only contact reports common_volume 0 and overlaps"
            " false. Clearance-critical decisions must combine both modes"
            " and apply the application tolerance. Subshape results return"
            " signed topology references; units are mm, mm2 and mm3."
        ),
        "inputSchema": _MEASURE_INPUT,
        "outputSchema": _MEASURE_OUTPUT,
    },
    {
        "name": "inspect_topology",
        "description": (
            "Inspect topology through one shared target. A whole object"
            " ({object}) enumerates its faces in native index order; a"
            " query target ({object, query}) evaluates the shared"
            " CadQuery-style selector chain (e.g. faces >Z then edges"
            " %CIRCLE) and reports the final-stage set; a signed reference"
            " returns its one referenced subshape. Items carry document-"
            "space bounds, type names, and (full detail) centers, normals,"
            " radii, axes and lengths, each with a signed reference usable"
            " as a measure/fillet/focus target. Pagination uses a signed"
            " cursor bound to the document generation, object, role, page"
            " size and canonical target; a selector or generation change"
            " refuses the cursor as stale. detail defaults to compact:"
            " index, reference, bounds and surfaceType/curveType only."
        ),
        "inputSchema": _INSPECT_TOPOLOGY_INPUT,
        "outputSchema": _INSPECT_TOPOLOGY_OUTPUT,
    },
]

HANDLERS = {
    "validate_geometry": _handle_validate_geometry,
    "measure": _handle_measure,
    "inspect_topology": _handle_inspect_topology,
}

check_schema(_VALIDATE_GEOMETRY_INPUT)
check_schema(_VALIDATE_GEOMETRY_OUTPUT)
check_schema(_MEASURE_INPUT)
check_schema(_MEASURE_OUTPUT)
check_schema(_INSPECT_TOPOLOGY_INPUT)
check_schema(_INSPECT_TOPOLOGY_OUTPUT)
