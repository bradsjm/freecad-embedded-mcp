"""``export`` tool: STL, STEP, 3MF and native FCStd serialization.

Per plan section 5 item 14 and section 6: mesh/STEP exports serialize placed
shape copies of the selected objects (one collective bed translation, never
per-object alignment), while ``fcstd`` exports the entire native document
through ``saveCopy`` and rejects meshing/bed options. Every format is written
to a sibling temporary file with the intended extension, verified by
readback, and only then published: exclusive no-clobber ``os.link`` for a
new destination, fingerprint-rechecked ``os.replace`` for an approved
overwrite. A failed readback never touches the destination.
"""

from __future__ import annotations

import errno
import math
import os
import tempfile
from typing import Any

import FreeCAD
import Mesh
import MeshPart
import Part

from .. import protocol
from ..object_validation import geometry_report, shape_is_null
from ..protocol import ToolError

FORMATS = ("stl", "step", "3mf", "fcstd")
_EXTENSIONS = {"stl": ".stl", "step": ".step", "3mf": ".3mf", "fcstd": ".FCStd"}

DEFAULT_LINEAR_DEFLECTION = 0.03
DEFAULT_ANGULAR_DEFLECTION = 0.12
MAX_LINEAR_DEFLECTION = 1000.0
MAX_ANGULAR_DEFLECTION = 3.14159


# ---------------------------------------------------------------------------
# Input helpers.
# ---------------------------------------------------------------------------


def _deflection(arguments: dict[str, Any], key: str, default: float) -> float:
    """Schema-validated deflection or its default.

    Type/range checks live in the registered input schema (number,
    exclusiveMinimum 0, bounded maximum) and run at the dispatch
    boundary; the handler only applies the default when the key is
    absent.
    """

    value = arguments.get(key)
    return default if value is None else float(value)


def _bed_align(arguments: dict[str, Any]) -> bool:
    """Schema-validated boolean or its default (absent/None means off)."""

    value = arguments.get("bed_align")
    return False if value is None else bool(value)


# ---------------------------------------------------------------------------
# Consent target (preflight; pure, no effects).
# ---------------------------------------------------------------------------


def preflight(ctx: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Return the overwrite-consent target for ``export``, else ``None``.

    A nonexistent destination needs no consent: publication is exclusive
    no-clobber. An existing destination returns its current fingerprint so
    the server can bind an MRTR consent challenge to it; ``requires_consent``
    and ``message`` mark the target for the server's consent choreography
    and are excluded from the consent-binding identity, so overwriting an
    existing file can never silently bypass MRTR.
    """

    if name != "export":
        return None
    doc = ctx.require_document(arguments.get("document"))
    ctx.check_document_idle(doc)
    path = ctx.canonical_path(arguments.get("path"))
    fingerprint = ctx.file_fingerprint(path)
    if fingerprint is None:
        return None
    return {
        "kind": "file",
        "path": path,
        "fingerprint": fingerprint,
        "purpose": "overwrite",
        "requires_consent": True,
        "message": (
            f"Exporting will overwrite the existing file '{path}'. "
            "Its current content will be replaced."
        ),
    }


# ---------------------------------------------------------------------------
# Staged publication.
# ---------------------------------------------------------------------------


def _staged_path(destination: str, extension: str) -> str:
    """A sibling temporary file in the destination directory, same ext."""

    parent = os.path.dirname(destination) or "."
    fd, staged = tempfile.mkstemp(prefix=".mcp-export-", suffix=extension, dir=parent)
    os.close(fd)
    return staged


def _remove_own_temp(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _recheck_destination_parent(ctx: Any, destination: str) -> None:
    """Recheck the canonical parent immediately before publishing."""

    parent = os.path.dirname(destination) or "."
    if not os.path.isdir(parent):
        raise ToolError(
            "VALIDATION_FAILED",
            f"destination directory '{parent}' does not exist",
            {"path": destination},
        )
    canonical_parent = ctx.canonical_path(parent)
    if canonical_parent != parent:
        raise ToolError(
            "PATH_NOT_ALLOWED",
            "destination directory changed on disk; refusing to publish",
            {"path": destination},
        )


def _target_fingerprint(ctx: Any, destination: str) -> dict[str, Any] | None:
    """The consented target fingerprint for this operation, if any."""

    approved = ctx.approved_target
    if not isinstance(approved, dict) or approved.get("path") != destination:
        return None
    fingerprint = approved.get("fingerprint")
    if not isinstance(fingerprint, dict):
        return None
    return fingerprint


def _fingerprint_of(value: Any) -> str:
    return protocol.fingerprint(value)


def publish(ctx: Any, staged: str, destination: str) -> None:
    """Publish the verified staged file; never clobber without consent.

    New destination: exclusive ``os.link`` — a file appearing meanwhile fails
    with a fresh-consent-required error instead of overwriting it. Approved
    replacement: the consented fingerprint is rechecked, then ``os.replace``
    publishes. Either way the staged name is removed afterwards.
    """

    _recheck_destination_parent(ctx, destination)
    approved_fingerprint = _target_fingerprint(ctx, destination)

    if approved_fingerprint is None:
        current = ctx.file_fingerprint(destination)
        if current is not None:
            raise ToolError(
                "CONSENT_DENIED",
                "destination appeared while exporting; request fresh consent before overwriting it",
                {"reason": "target_changed", "path": destination},
            )
        try:
            os.link(staged, destination)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR):
                raise ToolError(
                    "CONSENT_DENIED",
                    "destination appeared while exporting; request fresh "
                    "consent before overwriting it",
                    {"reason": "target_changed", "path": destination},
                ) from exc
            raise ToolError(
                "VALIDATION_FAILED",
                "this filesystem does not support exclusive publication with "
                f"hard links ({os.strerror(exc.errno) if exc.errno else exc}); "
                "the destination was not written",
                {"path": destination},
            ) from exc
        _remove_own_temp(staged)
        return

    current = ctx.file_fingerprint(destination)
    if current is None or _fingerprint_of(current) != _fingerprint_of(approved_fingerprint):
        raise ToolError(
            "CONSENT_DENIED",
            "target changed after consent; retry the original export operation",
            {"reason": "target_changed", "path": destination},
        )
    os.replace(staged, destination)


# ---------------------------------------------------------------------------
# Geometry preparation (mesh/STEP).
# ---------------------------------------------------------------------------


def _require_export_solid(ctx: Any, doc: Any, name: str) -> Any:
    """Resolve one selected object and require a solid with volume."""
    obj = ctx.require_object(doc, name)
    report = geometry_report(obj, expected_solids=1)
    if not report.get("ok"):
        error = report.get("error") or "geometry validation failed"
        raise ToolError(
            "VALIDATION_FAILED",
            f"object '{name}' cannot be exported: {error}",
            {"object": str(report.get("name") or getattr(obj, "Name", name))},
        )
    volume = report.get("volume")
    if (
        not isinstance(volume, (int, float))
        or isinstance(volume, bool)
        or not math.isfinite(volume)
        or volume <= 0
    ):
        raise ToolError(
            "VALIDATION_FAILED",
            f"object '{name}' has no solid with positive volume to export",
            {
                "object": str(report.get("name") or getattr(obj, "Name", name)),
                "volume": volume,
            },
        )
    return obj


def _placed_shape_copies(ctx: Any, doc: Any, object_names: list[str]) -> list[tuple[str, Any]]:
    """Document-space shape copies of the selected objects, selection order.

    ``_require_export_solid`` is the one geometry validation per selected
    object; the global-coordinate copy comes from the shared
    ``geometry.placed_shape`` helper (native ``getGlobalPlacement`` applied
    exactly once), so nested/rotated ancestors export where they sit in
    the document, not where their local placement says.
    """

    from .geometry import placed_shape

    copies: list[tuple[str, Any]] = []
    for name in object_names:
        obj = _require_export_solid(ctx, doc, name)
        copies.append((str(obj.Name), placed_shape(obj)))
    return copies


def _collective_bed_alignment(copies: list[tuple[str, Any]]) -> None:
    """One collective translation of minimum Z to zero for all copies.

    The assembly is never disturbed: every copy receives the same vector, so
    relative placement is untouched.
    """

    min_z = min(shape.BoundBox.ZMin for _, shape in copies)
    if not math.isfinite(min_z) or min_z == 0:
        return
    translation = FreeCAD.Vector(0.0, 0.0, -min_z)
    for _, shape in copies:
        shape.translate(translation)


def _bounds(box: Any) -> list[float]:
    values = [
        box.XMin,
        box.YMin,
        box.ZMin,
        box.XMax,
        box.YMax,
        box.ZMax,
    ]
    if not all(math.isfinite(float(value)) for value in values):
        raise ToolError("VALIDATION_FAILED", "export produced non-finite bounds", {})
    return [float(value) for value in values]


# ---------------------------------------------------------------------------
# Mesh export (stl / 3mf).
# ---------------------------------------------------------------------------


def _mesh_readback(path: str, fmt: str) -> dict[str, Any]:
    try:
        mesh = Mesh.Mesh(path)
        is_solid = bool(mesh.isSolid())
        count_facets = int(mesh.CountFacets)
        bounds = _bounds(mesh.BoundBox)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            "VALIDATION_FAILED",
            f"{fmt} export readback failed; the destination file was not modified",
            {"reason": f"{type(exc).__name__}: {exc}"},
        ) from exc
    return {
        "isSolid": is_solid,
        "countFacets": count_facets,
        "bounds": bounds,
    }


def _export_mesh(
    ctx: Any,
    doc: Any,
    object_names: list[str],
    fmt: str,
    destination: str,
    linear_deflection: float,
    angular_deflection: float,
    bed_align: bool,
) -> dict[str, Any]:
    copies = _placed_shape_copies(ctx, doc, object_names)
    if bed_align:
        _collective_bed_alignment(copies)

    combined = Mesh.Mesh()
    for _, shape in copies:
        mesh = MeshPart.meshFromShape(
            Shape=shape,
            LinearDeflection=linear_deflection,
            AngularDeflection=angular_deflection,
            Relative=False,
        )
        combined.addMesh(mesh)

    staged = _staged_path(destination, _EXTENSIONS[fmt])
    try:
        try:
            if fmt == "3mf":
                combined.write(staged, Format="3MF")
            else:
                combined.write(staged)
        except Exception as exc:
            raise ToolError(
                "VALIDATION_FAILED",
                f"{fmt} mesh export failed: {type(exc).__name__}: {exc}",
                {"path": destination},
            ) from exc
        readback = _mesh_readback(staged, fmt)
        publish(ctx, staged, destination)
    finally:
        _remove_own_temp(staged)

    return {
        "format": fmt,
        "path": destination,
        "objects": [name for name, _ in copies],
        "mesh": readback,
    }


# ---------------------------------------------------------------------------
# STEP export.
# ---------------------------------------------------------------------------


def _step_readback(path: str) -> dict[str, Any]:
    try:
        shape = Part.read(path)
        if shape_is_null(shape):
            raise ToolError(
                "VALIDATION_FAILED",
                f"STEP readback of '{path}' produced no geometry",
                {"path": path},
            )
        solids = list(shape.Solids)
        volume = float(sum(solid.Volume for solid in solids))
        is_valid = bool(shape.isValid())
        bounds = _bounds(shape.BoundBox)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            "VALIDATION_FAILED",
            "STEP export readback failed; the destination file was not modified",
            {"reason": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not solids or not math.isfinite(volume):
        raise ToolError(
            "VALIDATION_FAILED",
            "STEP readback contains no usable solids; the destination file was not modified",
            {},
        )
    return {
        "isValid": is_valid,
        "solidCount": len(solids),
        "volume": volume,
        "bounds": bounds,
    }


def _export_step(
    ctx: Any,
    doc: Any,
    object_names: list[str],
    destination: str,
    bed_align: bool,
) -> dict[str, Any]:
    copies = _placed_shape_copies(ctx, doc, object_names)
    if bed_align:
        _collective_bed_alignment(copies)

    compound = Part.Compound([shape for _, shape in copies])
    staged = _staged_path(destination, _EXTENSIONS["step"])
    try:
        try:
            compound.exportStep(staged)
        except Exception as exc:
            raise ToolError(
                "VALIDATION_FAILED",
                f"STEP export failed: {type(exc).__name__}: {exc}",
                {"path": destination},
            ) from exc
        readback = _step_readback(staged)
        publish(ctx, staged, destination)
    finally:
        _remove_own_temp(staged)

    return {
        "format": "step",
        "path": destination,
        "objects": [name for name, _ in copies],
        "step": readback,
    }


# ---------------------------------------------------------------------------
# Native FCStd export.
# ---------------------------------------------------------------------------


def _placement_equal(a: Any, b: Any) -> bool:
    """Tolerant placement comparison for the FCStd readback.

    FreeCAD 1.1 can flip one quaternion rounding bit when re-reading a
    freshly created origin plane, so exact equality false-rejects faithful
    copies. Translation must agree to a micrometre and rotation to well
    under a microradian; a real placement change is orders of magnitude
    larger.
    """

    placement_a = getattr(a, "Placement", None)
    placement_b = getattr(b, "Placement", None)
    if (placement_a is None) != (placement_b is None):
        return False
    if placement_a is None:
        return True
    try:
        if (placement_a.Base - placement_b.Base).Length > 1e-6:
            return False
        relative = placement_a.Rotation.inverted() * placement_b.Rotation
        return abs(relative.Angle) < 1e-9
    except Exception:
        return placement_a == placement_b


def _verify_reopened_copy(doc: Any, reopened: Any) -> None:
    """Compare object identities, count and placements after reopening."""

    originals = list(doc.Objects)
    if len(reopened.Objects) != len(originals):
        raise ToolError(
            "VALIDATION_FAILED",
            "FCStd readback failed: reopened copy has a different object "
            "count; the destination file was not modified",
            {"originalCount": len(originals), "reopenedCount": len(reopened.Objects)},
        )
    by_name = {str(obj.Name): obj for obj in reopened.Objects}
    for obj in originals:
        name = str(obj.Name)
        twin = by_name.get(name)
        if twin is None:
            raise ToolError(
                "VALIDATION_FAILED",
                "FCStd readback failed: reopened copy is missing object "
                f"'{name}'; the destination file was not modified",
                {"object": name},
            )
        if not _placement_equal(obj, twin):
            raise ToolError(
                "VALIDATION_FAILED",
                f"FCStd readback failed: placement of object '{name}' "
                "changed in the saved copy; the destination file was not "
                "modified",
                {"object": name},
            )


def _export_fcstd(ctx: Any, doc: Any, destination: str) -> dict[str, Any]:
    original_file_name = str(doc.FileName)
    original_modified = _modified_flag(doc)
    active_document_name = None
    selection_captured = False
    selected_names: list[str] = []
    try:
        import FreeCADGui

        if FreeCAD.ActiveDocument is not None:
            active_document_name = str(FreeCAD.ActiveDocument.Name)
        selected_names = [
            str(getattr(obj, "Name", "")) for obj in (FreeCADGui.Selection.getSelection() or ())
        ]
        selection_captured = True
    except Exception:
        selection_captured = False

    staged = _staged_path(destination, _EXTENSIONS["fcstd"])
    reopened = None
    try:
        try:
            doc.saveCopy(staged)
        except Exception as exc:
            raise ToolError(
                "VALIDATION_FAILED",
                f"FCStd export failed: {type(exc).__name__}: {exc}",
                {"path": destination},
            ) from exc
        # Both the save identity and the unsaved-state flag must survive the
        # copy: either change means the source document was disturbed.
        if str(doc.FileName) != original_file_name or _modified_flag(doc) != original_modified:
            raise ToolError(
                "VALIDATION_FAILED",
                "FCStd export changed the source document's save identity or "
                "unsaved state; refusing to publish",
                {"path": destination},
            )
        try:
            reopened = FreeCAD.openDocument(staged, True)
        except Exception as exc:
            raise ToolError(
                "VALIDATION_FAILED",
                "FCStd export readback failed: the saved copy could not be "
                "reopened; the destination file was not modified",
                {"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc
        try:
            _verify_reopened_copy(doc, reopened)
            publish(ctx, staged, destination)
        finally:
            if reopened is not None:
                try:
                    FreeCAD.closeDocument(str(reopened.Name))
                except Exception as close_exc:
                    raise ToolError(
                        "VALIDATION_FAILED",
                        "FCStd export verification copy could not be closed; "
                        "it remains open in the document tree",
                        {"path": staged},
                    ) from close_exc
    finally:
        _remove_own_temp(staged)
        _restore_presentation(active_document_name, selected_names if selection_captured else None)

    return {
        "format": "fcstd",
        "path": destination,
        "objects": [],
        "objectCount": len(doc.Objects),
    }


def _restore_presentation(
    active_document_name: str | None, selected_names: list[str] | None
) -> None:
    """Restore the caller's active document and selection; never raise.

    ``selected_names`` is ``None`` when the selection could not be captured,
    in which case it is left untouched; an empty list restores an empty
    selection.
    """

    try:
        import FreeCADGui

        if (
            active_document_name is not None
            and getattr(FreeCAD.ActiveDocument, "Name", None) != active_document_name
            and active_document_name in FreeCAD.listDocuments()
        ):
            FreeCAD.setActiveDocument(active_document_name)
        gui_document = (
            FreeCADGui.getDocument(active_document_name) if active_document_name else None
        )
        if gui_document is None or selected_names is None:
            return
        if selected_names:
            FreeCADGui.Selection.setSelection(
                [
                    gui_document.getObject(name)
                    for name in selected_names
                    if gui_document.getObject(name)
                ]
            )
        else:
            FreeCADGui.Selection.clearSelection()
    except Exception:
        pass


def _modified_flag(doc: Any) -> bool | None:
    """Conservative ``doc.Modified`` read; ``None`` when unavailable."""

    value = getattr(doc, "Modified", None)
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# Handler.
# ---------------------------------------------------------------------------


def export(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """``export`` handler (GUI thread only)."""

    fmt = str(arguments["format"])  # schema enum guarantees membership
    doc = ctx.require_document(arguments.get("document"))
    ctx.check_document_idle(doc)
    destination = ctx.canonical_path(arguments.get("path"))
    object_names = [str(name) for name in (arguments.get("objects") or [])]

    if fmt == "fcstd":
        if object_names:
            raise ToolError(
                "VALIDATION_FAILED",
                "fcstd export serializes the entire native document; pass an empty object list",
                {"format": "fcstd"},
            )
        for key in ("linear_deflection", "angular_deflection", "bed_align"):
            if key in arguments:
                raise ToolError(
                    "VALIDATION_FAILED",
                    f"{key} is not accepted for fcstd export",
                    {"format": "fcstd", "option": key},
                )
    elif not object_names:
        raise ToolError(
            "VALIDATION_FAILED",
            f"{fmt} export requires a nonempty object list",
            {"format": fmt},
        )

    try:
        if fmt == "fcstd":
            return _export_fcstd(ctx, doc, destination)
        if fmt == "step":
            return _export_step(
                ctx,
                doc,
                object_names,
                destination,
                _bed_align(arguments),
            )
        return _export_mesh(
            ctx,
            doc,
            object_names,
            fmt,
            destination,
            _deflection(arguments, "linear_deflection", DEFAULT_LINEAR_DEFLECTION),
            _deflection(arguments, "angular_deflection", DEFAULT_ANGULAR_DEFLECTION),
            _bed_align(arguments),
        )
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            "VALIDATION_FAILED",
            f"export failed: {type(exc).__name__}: {exc}",
            {"format": fmt, "path": destination},
        ) from exc


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


_BOUNDS_SCHEMA = {
    "type": "array",
    "minItems": 6,
    "maxItems": 6,
    "items": {"type": "number"},
}

_MESH_READBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "isSolid": {"type": "boolean"},
        "countFacets": {"type": "integer", "minimum": 0},
        "bounds": _BOUNDS_SCHEMA,
    },
    "required": ["isSolid", "countFacets", "bounds"],
    "additionalProperties": False,
}

_STEP_READBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "isValid": {"type": "boolean"},
        "solidCount": {"type": "integer", "minimum": 0},
        "volume": {"type": "number"},
        "bounds": _BOUNDS_SCHEMA,
    },
    "required": ["isValid", "solidCount", "volume", "bounds"],
    "additionalProperties": False,
}

_TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "document": {"type": "string", "minLength": 1, "maxLength": 256},
        "objects": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 256},
            "maxItems": 1024,
        },
        "format": {"type": "string", "enum": list(FORMATS)},
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "linear_deflection": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": MAX_LINEAR_DEFLECTION,
        },
        "angular_deflection": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": MAX_ANGULAR_DEFLECTION,
        },
        "bed_align": {"type": "boolean"},
    },
    "required": ["document", "objects", "format", "path"],
    "additionalProperties": False,
}

_TOOL_OUTPUT_SCHEMA = {
    "type": "object",
    "$defs": {
        "bounds": _BOUNDS_SCHEMA,
        "meshReadback": _MESH_READBACK_SCHEMA,
        "stepReadback": _STEP_READBACK_SCHEMA,
    },
    "properties": {
        "format": {"type": "string", "enum": list(FORMATS)},
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "objects": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 256},
            "maxItems": 1024,
        },
        "mesh": {"$ref": "#/$defs/meshReadback"},
        "step": {"$ref": "#/$defs/stepReadback"},
        "objectCount": {"type": "integer", "minimum": 0},
    },
    "required": ["format", "path", "objects"],
    "additionalProperties": False,
}

TOOL_DEFINITIONS = [
    {
        "name": "export",
        "description": (
            "Export objects to STL, STEP or 3MF, or the entire document to a "
            "native FCStd copy. Writes to a temporary sibling file, verifies "
            "the result by readback, then publishes; overwriting an existing "
            "destination requires consent."
        ),
        "inputSchema": _TOOL_INPUT_SCHEMA,
        "outputSchema": _TOOL_OUTPUT_SCHEMA,
    }
]

HANDLERS = {"export": export}
