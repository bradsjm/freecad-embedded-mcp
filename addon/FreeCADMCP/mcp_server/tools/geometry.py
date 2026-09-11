"""Deterministic geometry tools: validate_geometry, measure, signed topology refs.

``validate_geometry`` reports per-object state, shape validity, solid count,
volume, bounds, ``shape.check()`` diagnostics and ``shape.getTolerance(1)``
without repairing anything; expected solids and expected bounds produce
explicit verdicts. ``measure`` supports the distance/interference/section/faces
modes over whole objects or subshapes selected by bounding box. Subshape
results carry a canonical ``{object, subelement}`` reference whose subelement
is an opaque HMAC-signed topology token (document identity, generation,
object name, role, index) produced with the shared server signing key via
``ctx.signer.sign("topology", payload)``; empty subelement denotes the whole
object. Numeric ``Face7``/``Edge7`` selectors are never accepted as durable
references.

``make_reference``/``resolve_reference`` are consumed by other tool modules
(e.g. ``objects``) to hand out and re-open subshape identities.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

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
    stale_generation_details,
)

# Subshape bounding boxes must match each requested coordinate within 1 mm
# *1e-6 per the plan.
_BOX_TOLERANCE_MM = 1e-6
_DEFAULT_BOUNDS_TOLERANCE = 0.000001
_MAX_CANDIDATES = 16
_MAX_CURVES = 32
_MAX_FACES = 64
_MAX_TOPOLOGY_PAGE = 100
_DEFAULT_TOPOLOGY_PAGE = 50

_NUMERIC_SUBELEMENT = re.compile(r"(?:Face|Edge|Vertex|Wire)\d+")
_SUBELEMENT_INDEX = re.compile(r"(Face|Edge)([1-9][0-9]*)")
_MAX_SUBELEMENT_LIST = 32


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
    """Canonical reference for a whole object; empty subelement denotes it."""

    return {"object": obj.Name, "subelement": ""}


def _reference_for(ctx: Any, doc: Any, obj: Any, selection: Mapping | None) -> dict:
    if selection is None:
        return whole_reference(obj)
    return make_reference(ctx, doc, obj, selection["role"], selection["index"])


def resolve_reference(ctx: Any, doc: Any, reference: Any) -> tuple[Any, str]:
    """Resolve a canonical reference to ``(object, native_subelement)``.

    Verifies the signed token against the current document identity and
    generation; stale or tampered references are rejected with
    ``VALIDATION_FAILED``. Caller-supplied numeric ``Face7``/``Edge7``
    selectors are explicitly rejected. An empty subelement resolves to the
    whole object (native subelement ``""``).
    """

    if not isinstance(reference, Mapping):
        raise ToolError(VALIDATION_FAILED, "topology reference must be an object mapping")
    name = reference.get("object")
    subelement = reference.get("subelement", "")
    if subelement is None:
        subelement = ""
    if not isinstance(name, str) or not name:
        raise ToolError(VALIDATION_FAILED, "topology reference is missing an object name")
    if not isinstance(subelement, str):
        raise ToolError(VALIDATION_FAILED, "topology reference subelement must be a string")
    obj = ctx.require_object(doc, name)
    if subelement == "":
        return obj, ""
    if "." not in subelement:
        if _NUMERIC_SUBELEMENT.fullmatch(subelement):
            raise ToolError(
                VALIDATION_FAILED,
                "numeric subelement selectors (e.g. Face7) are not durable; use the"
                " signed reference returned by measure",
            )
        raise ToolError(VALIDATION_FAILED, "topology reference subelement is not a signed token")
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
    shape = _shape_of(obj)
    if shape is None:
        raise ToolError(
            VALIDATION_FAILED,
            f"object {obj.Name} has no shape to resolve the reference against",
            {"reason": "missing_subelement"},
        )
    try:
        subshapes = list(shape.Faces if role == "face" else shape.Edges)
    except Exception as exc:
        raise ToolError(VALIDATION_FAILED, f"subshape access failed: {exc}") from exc
    if index > len(subshapes):
        raise ToolError(
            VALIDATION_FAILED,
            f"{role} {index} no longer exists on {obj.Name}",
            {"reason": "missing_subelement"},
        )
    return obj, _subelement_label(role, index)


def resolve_reference_list(
    ctx: Any, doc: Any, base: Any, references: list[Mapping], role: str
) -> tuple[Any, list[str]]:
    """Resolve 1..N signed references onto one base object.

    Every reference must name the base object and carry a signed token of
    the requested role; duplicates are rejected. Returns the base object
    and the native subelement labels in request order.
    """

    if not references:
        raise ToolError(VALIDATION_FAILED, "at least one signed reference is required")
    if len(references) > _MAX_SUBELEMENT_LIST:
        raise ToolError(
            VALIDATION_FAILED,
            f"at most {_MAX_SUBELEMENT_LIST} signed references are accepted",
        )
    base_obj, _native_base = resolve_reference(ctx, doc, base)
    seen: set[str] = set()
    labels: list[str] = []
    for position, reference in enumerate(references):
        obj, native = resolve_reference(ctx, doc, reference)
        if obj is not base_obj:
            raise ToolError(
                VALIDATION_FAILED,
                f"reference {position} targets '{obj.Name}' but the base is '{base_obj.Name}'",
                {"position": position},
            )
        if not native:
            raise ToolError(
                VALIDATION_FAILED,
                f"reference {position} must carry a signed subelement token",
                {"position": position},
            )
        if not native.startswith(("Face", "Edge")):
            raise ToolError(
                VALIDATION_FAILED,
                f"reference {position} must be a signed {role} token",
                {"position": position, "native": native},
            )
        if role == "face" and not native.startswith("Face"):
            raise ToolError(
                VALIDATION_FAILED,
                f"reference {position} must be a signed face token",
                {"position": position, "native": native},
            )
        if role == "edge" and not native.startswith("Edge"):
            raise ToolError(
                VALIDATION_FAILED,
                f"reference {position} must be a signed edge token",
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
# Subshape selection by bounding box.
# ---------------------------------------------------------------------------


def _resolve_reference_selection(
    ctx: Any, doc: Any, selector: Mapping
) -> tuple[Any, Mapping | None]:
    """Resolve a canonical signed ``{object, subelement}`` reference.

    An empty subelement selects the whole object; a signed Face/Edge token
    selects the placed (document-space) subshape at the signed index.
    """

    obj, native = resolve_reference(ctx, doc, selector)
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
    try:
        subshapes = list(shape.Faces if role == "face" else shape.Edges)
    except Exception as exc:
        raise ToolError(VALIDATION_FAILED, f"subshape access failed: {exc}") from exc
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


def _resolve_target(ctx: Any, doc: Any, selector: Any) -> tuple[Any, Mapping | None]:
    """Resolve an object name, signed reference, or ``{object, role, box}``.

    Returns ``(object, selection)`` where ``selection`` is None for a whole
    object, otherwise ``{role, index, shape, bounds}`` for the selected
    subshape. Zero matches and ambiguity are explicit errors; ambiguity lists
    the candidate subelements.
    """

    if isinstance(selector, str):
        return ctx.require_object(doc, selector), None
    if "subelement" in selector:
        return _resolve_reference_selection(ctx, doc, selector)
    obj = ctx.require_object(doc, selector["object"])
    role = selector["role"]
    box = [float(value) for value in selector["box"]]
    # Selectors and the returned subshape live in document space: match
    # against the global-coordinate copy (index order is unchanged).
    shape = placed_shape(obj)
    try:
        subshapes = list(shape.Faces if role == "face" else shape.Edges)
    except Exception as exc:
        raise ToolError(VALIDATION_FAILED, f"subshape access failed: {exc}") from exc
    matches: list[tuple[int, Any, list[float]]] = []
    for index, subshape in enumerate(subshapes, 1):
        bounds = _bbox(subshape)
        if bounds is None:
            continue
        # Deliberate contract: the selector box must equal the subelement's
        # document-space bounds (within tolerance), which selects exactly
        # one face or edge. Overlap matching would turn any enclosing box
        # into an ambiguous multi-match.
        if all(abs(bounds[i] - box[i]) <= _BOX_TOLERANCE_MM for i in range(6)):
            matches.append((index, subshape, bounds))
    if not matches:
        raise ToolError(
            VALIDATION_FAILED,
            f"no {role} of {obj.Name} has bounds equal to the requested "
            f"bounding box (within {_BOX_TOLERANCE_MM:g} mm); give the exact "
            f"bounds of one {role}, or select it with a signed reference "
            "from inspect_topology or a measure faces pass",
            {"role": role, "box": box},
        )
    if len(matches) > 1:
        details = {
            "role": role,
            "candidates": [
                {
                    "object": obj.Name,
                    "subelement": _subelement_label(role, index),
                    "bounds": bounds,
                }
                for index, _subshape, bounds in matches[:_MAX_CANDIDATES]
            ],
        }
        raise ToolError(
            VALIDATION_FAILED,
            f"{len(matches)} {role}s of {obj.Name} match the requested bounding box",
            details,
        )
    index, subshape, bounds = matches[0]
    return obj, {"role": role, "index": index, "shape": subshape, "bounds": bounds}


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
        bool(report["object_valid"])
        and report["shape_valid"] is not False
        and solids_verdict != "mismatch"
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
        "verdicts": {
            "solids": solids_verdict,
            "volume": volume_verdict,
            **({"bounds": bounds_verdict} if bounds_verdict is not None else {}),
        },
        "valid": valid,
    }


def _handle_validate_geometry(ctx: Any, arguments: Mapping[str, Any]) -> dict:
    doc = ctx.require_document(arguments["document"])
    expected_solids = arguments.get("expected_solids")
    expected_bounds = arguments.get("expected_bounds") or {}
    tolerance = arguments.get("bounds_tolerance", _DEFAULT_BOUNDS_TOLERANCE)
    entries = []
    for requested in arguments["objects"]:
        obj = ctx.require_object(doc, requested)
        report = geometry_report(obj, expected_solids)
        # Reported bounds are document-space: measured against the global
        # copied shape, never the local serialized placement. Shapeless
        # objects keep null bounds instead of failing.
        global_report = dict(report)
        if _shape_of(obj) is not None:
            global_report["bounds"] = _bbox(placed_shape(obj))
        entries.append(_geometry_entry(global_report, expected_solids, expected_bounds, tolerance))
    return {
        "document": {
            "name": doc.Name,
            "generation": int(ctx.document_generation(doc)),
        },
        "units": {"length": "mm", "volume": "mm3", "tolerance": "mm"},
        "all_valid": all(entry["valid"] for entry in entries),
        "objects": entries,
    }


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
    volume = _finite(getattr(common, "Volume", None)) or 0.0
    payload["common_volume"] = volume
    payload["overlaps"] = volume > 0.0
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
    a_obj, a_sel = _resolve_target(ctx, doc, arguments["a"])
    payload: dict[str, Any] = {
        "mode": mode,
        "units": {"length": "mm", "area": "mm2", "volume": "mm3"},
    }
    if mode == "faces":
        if a_sel is not None and a_sel["role"] != "face":
            raise ToolError(
                VALIDATION_FAILED,
                "mode 'faces' requires a whole-object or face selector",
            )
        return _measure_faces(ctx, doc, a_obj, a_sel, payload)
    if mode not in ("distance", "interference", "section"):
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
        raise ToolError(VALIDATION_FAILED, f"mode {mode!r} requires a 'b' selector")
    b_obj, b_sel = _resolve_target(ctx, doc, arguments["b"])
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
    return _measure_interference(a_shape, b_shape, payload)


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
) -> dict:
    return {
        "kind": "topology-page",
        "identity": str(ctx.document_identity(doc)),
        "generation": int(ctx.document_generation(doc)),
        "object": object_name,
        "role": role,
        "limit": limit,
        "last": last,
    }


def _open_topology_cursor(
    ctx: Any,
    doc: Any,
    cursor: str,
    object_name: str,
    role: str,
    limit: int,
) -> int:
    """Return the page start index, rejecting stale or changed cursors."""

    try:
        payload = ctx.signer.verify(DOMAIN_CURSOR, cursor)
    except ProtocolError as exc:
        raise ToolError(
            VALIDATION_FAILED,
            "topology cursor signature rejected; restart from the first page",
            {"reason": "malformed_cursor", "nextTool": "inspect_topology"},
        ) from exc
    expected = _topology_cursor_payload(ctx, doc, object_name, role, limit, 0)
    if payload.get("kind") != "topology-page":
        raise _stale_topology_cursor()
    for key in ("identity", "generation", "object", "role", "limit"):
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
    role = str(arguments["role"])
    if role not in ("face", "edge"):
        raise ToolError(VALIDATION_FAILED, "role must be 'face' or 'edge'")
    indices = arguments.get("indices")
    detail = str(arguments.get("detail") or "full")
    limit = arguments.get("limit")
    limit = _DEFAULT_TOPOLOGY_PAGE if limit is None else int(limit)
    limit = max(1, min(_MAX_TOPOLOGY_PAGE, limit))
    obj = ctx.require_object(doc, str(arguments["object"]))

    if indices and arguments.get("cursor"):
        raise ToolError(
            VALIDATION_FAILED,
            "indices cannot be combined with a pagination cursor",
            {"reason": "indices_with_cursor"},
        )
    if indices:
        seen: set[int] = set()
        for value in indices:
            if value in seen:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"indices repeats index {value}",
                    {"reason": "duplicate_index", "index": value},
                )
            seen.add(value)

    start_after = 0
    cursor = arguments.get("cursor")
    if cursor:
        start_after = _open_topology_cursor(ctx, doc, str(cursor), obj.Name, role, limit)

    shape = placed_shape(obj)
    try:
        subshapes = list(shape.Faces if role == "face" else shape.Edges)
    except Exception as exc:
        raise ToolError(VALIDATION_FAILED, f"subshape access failed: {exc}") from exc
    total = len(subshapes)

    build = _topology_face_item if role == "face" else _topology_edge_item
    if indices:
        ordered = sorted(int(value) for value in indices)
        for value in ordered:
            if value > total:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"index {value} is out of range; the object has {total} {role}s",
                    {"reason": "index_out_of_range", "index": value, "total": total},
                )
        items = [build(ctx, doc, obj, value, subshapes[value - 1], detail) for value in ordered]
        next_cursor = None
    else:
        items = [
            build(ctx, doc, obj, index, subshape, detail)
            for index, subshape in enumerate(
                subshapes[start_after : start_after + limit], start_after + 1
            )
        ]
        next_cursor = None
        if start_after + limit < total:
            next_cursor = ctx.signer.sign(
                DOMAIN_CURSOR,
                _topology_cursor_payload(ctx, doc, obj.Name, role, limit, start_after + limit),
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

_REFERENCE_DEF = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "object": {"type": "string", "minLength": 1},
        "subelement": {"type": "string"},
    },
    "required": ["object", "subelement"],
}

_SELECTOR_DEF = {
    "anyOf": [
        {"type": "string", "minLength": 1},
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "object": {"type": "string", "minLength": 1},
                "role": {"enum": ["face", "edge"]},
                "box": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 6,
                    "maxItems": 6,
                },
            },
            "required": ["object", "role", "box"],
        },
        _REFERENCE_DEF,
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
        "reference": {"$ref": "#/$defs/reference"},
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
        "reference": {"$ref": "#/$defs/reference"},
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

_INSPECT_TOPOLOGY_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "object", "role"],
    "properties": {
        "document": {"type": "string", "minLength": 1},
        "object": {"type": "string", "minLength": 1},
        "role": {"enum": ["face", "edge"]},
        "cursor": {"type": ["string", "null"]},
        "indices": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
            "minItems": 1,
            "maxItems": _MAX_TOPOLOGY_PAGE,
        },
        "detail": {"type": "string", "enum": ["compact", "full"], "default": "full"},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": _DEFAULT_TOPOLOGY_PAGE,
        },
    },
}

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
        "reference": _REFERENCE_DEF,
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


_VALIDATE_GEOMETRY_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document": {"type": "string", "minLength": 1},
        "objects": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "maxItems": 100,
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
    "required": ["document", "objects"],
}

_VALIDATE_GEOMETRY_OUTPUT = {
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
                "solids": {"enum": ["match", "mismatch", "unspecified"]},
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
                "verdicts",
                "valid",
            ],
        },
    },
}

_MEASURE_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document": {"type": "string", "minLength": 1},
        "a": _SELECTOR_DEF,
        "mode": {"enum": ["distance", "interference", "section", "faces"]},
        "b": _SELECTOR_DEF,
        "plane": _PLANE_DEF,
    },
    "required": ["document", "a", "mode"],
}

_MEASURE_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mode": {"enum": ["distance", "interference", "section", "faces"]},
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
    "required": ["mode", "units"],
    "$defs": {
        "point": _POINT_DEF,
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
                "reference": {"$ref": "#/$defs/reference"},
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
            " produce explicit verdicts; nothing is repaired."
        ),
        "inputSchema": _VALIDATE_GEOMETRY_INPUT,
        "outputSchema": _VALIDATE_GEOMETRY_OUTPUT,
    },
    {
        "name": "measure",
        "description": (
            "Measure geometry in document (global) coordinates between whole"
            " objects or bbox-selected faces/edges (selector boxes are"
            " document-space mm; a box selects the one face or edge whose"
            " bounds equal it within tolerance, so take exact bounds from"
            " inspect_topology): distance (distToShape), interference (common"
            " volume), planar section curves (z plane or normal+point in"
            " document space) or face areas with sampled normals. Semantics:"
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
            "Page through an object's faces or edges in native index order "
            "with document-space bounds, sampled centers/normals, surface "
            "and curve type names and optional radius/axis data. Each item "
            "carries a signed topology reference usable as a measure "
            "selector; pagination uses a signed cursor bound to the document "
            "generation, object, role and page size. Pass indices (1-based) "
            "to fetch exactly those rows in ascending order instead of a "
            "page (nextCursor is then null, total still reports the full "
            'count), and detail: "compact" for rows carrying only index, '
            "reference, bounds and surfaceType/curveType."
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
