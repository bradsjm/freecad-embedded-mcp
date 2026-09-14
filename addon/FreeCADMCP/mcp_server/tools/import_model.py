"""``import_model``: consent-gated STEP and STL import.

Imported files are untrusted input, so every import runs a file-target
consent preflight (purpose ``import``) before any effect. STEP objects are
created by ``Import.insert`` and discovered by document-name difference
against the pre-import name set — never Python wrapper identity, which
FreeCAD recreates for existing objects; STL is loaded into a
``Mesh::Feature``. If the native import removes or replaces a pre-existing
name, the call is refused with ``import_identity_conflict``. Everything
runs inside the shared mutation so a failure aborts the transaction and
removes only the objects this call introduced — never a pre-existing
object, even with a colliding name.

The result reports facts only: bounds, validity, solid count and a
``geometryKind`` marker. Imported geometry is never claimed to be
parametrically editable, and no units are inferred.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from typing import Any

from ..object_validation import document_bounds, mutation
from ..protocol import VALIDATION_FAILED, ToolError, check_schema
from .documents import _require_approved

_IMPORT_PREVIEW_LIMIT = 64

_IMPORT_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "path", "format"],
    "properties": {
        "document": {"type": "string", "minLength": 1},
        "path": {"type": "string", "minLength": 1},
        "format": {"type": "string", "enum": ["step", "stl"]},
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Mesh feature name for STL imports.",
        },
    },
}

_IMPORT_OBJECT_ROW = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name",
        "label",
        "typeId",
        "geometryKind",
        "bounds",
        "shapeValid",
        "meshValid",
        "solidCount",
    ],
    "properties": {
        "name": {"type": "string"},
        "label": {"type": "string"},
        "typeId": {"type": "string"},
        "geometryKind": {"type": "string", "enum": ["shape", "mesh", "none"]},
        "bounds": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 6,
            "maxItems": 6,
        },
        "shapeValid": {"type": ["boolean", "null"]},
        "meshValid": {"type": ["boolean", "null"]},
        "solidCount": {"type": ["integer", "null"], "minimum": 0},
    },
}

_IMPORT_OUTPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "document",
        "generation",
        "format",
        "path",
        "units",
        "objectCount",
        "objects",
        "objectsTruncated",
    ],
    "properties": {
        "document": {"type": "string"},
        "generation": {"type": "integer", "minimum": 0},
        "format": {"type": "string", "enum": ["step", "stl"]},
        "path": {"type": "string"},
        "units": {
            "type": "string",
            "enum": ["file_defined", "unitless_assumed_mm"],
        },
        "objects": {
            "type": "array",
            "items": _IMPORT_OBJECT_ROW,
            "maxItems": _IMPORT_PREVIEW_LIMIT,
        },
        "objectCount": {"type": "integer", "minimum": 0},
        "objectsTruncated": {"type": "boolean"},
    },
}

TOOL_DEFINITIONS = [
    {
        "name": "import_model",
        "description": (
            "Import a STEP or STL file into an existing document. Imported "
            "files are untrusted input, so the call requires file consent "
            "before any effect. STEP uses Import.insert and reports the "
            "created objects discovered by document-name difference; an "
            "import that removes or replaces an existing object is refused "
            "(import_identity_conflict). STL loads "
            "into a Mesh::Feature and fails when the file has no facets. "
            "The result reports bounds, validity, solid count and "
            "geometryKind plus a units label ('file_defined' for STEP, "
            "'unitless_assumed_mm' for STL); imported geometry is not "
            "claimed to be parametrically editable."
        ),
        "inputSchema": _IMPORT_INPUT,
        "outputSchema": _IMPORT_OUTPUT,
    }
]

HANDLERS: dict[str, Callable[[Any, dict], Any]] = {}

check_schema(_IMPORT_INPUT)
check_schema(_IMPORT_OUTPUT)


# ---------------------------------------------------------------------------
# Preflight (pure, no effects).
# ---------------------------------------------------------------------------


def preflight(ctx: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Return the import consent target for ``import_model``, else ``None``."""

    if name != "import_model":
        return None
    doc = ctx.require_document(arguments.get("document"))
    ctx.check_document_idle(doc)
    path = ctx.canonical_path(arguments.get("path"))
    if not os.path.isfile(path):
        raise ToolError(VALIDATION_FAILED, f"no such import file: '{path}'")
    return {
        "kind": "file",
        "path": path,
        "fingerprint": ctx.file_fingerprint(path),
        "purpose": "import",
        "requires_consent": True,
        "message": (
            f"Import geometry from '{path}'? The file is untrusted input and "
            "will be loaded into the current document."
        ),
    }


# ---------------------------------------------------------------------------
# Native import (lazy FreeCAD imports keep this module headless).
# ---------------------------------------------------------------------------


def _import_step(doc: Any, path: str) -> None:
    """Import a STEP file into the document via the native Import module."""
    import Import

    Import.insert(path, doc.Name)


def _import_stl(doc: Any, path: str, requested_name: str) -> Any:
    """Import an STL file as a new Mesh::Feature in the document."""
    import Mesh

    mesh_feature = doc.addObject("Mesh::Feature", requested_name or "ImportedMesh")
    mesh_feature.Mesh = Mesh.Mesh(path)
    return mesh_feature


def _mesh_valid(mesh: Any) -> bool | None:
    """Report the mesh's native validity, or None when it cannot be read."""
    checker = getattr(mesh, "isValid", None)
    if not callable(checker):
        return None
    try:
        return bool(checker())
    except Exception:
        return None


def _mesh_bounds(mesh: Any) -> list[float] | None:
    """Return the mesh's six-element bounds, or None when unavailable."""
    try:
        box = mesh.BoundBox
        values = [box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax]
        converted = [float(value) for value in values]
    except Exception:
        return None
    if not all(math.isfinite(value) for value in converted):
        return None
    return converted


def _facet_count(mesh: Any) -> int | None:
    """Count the mesh's facets, or None when they cannot be read."""
    try:
        return len(list(mesh.Facets))
    except Exception:
        return None


def _object_row(obj: Any) -> dict:
    """Project one imported object into its wire row with shape/mesh evidence."""
    shape = None
    try:
        shape = getattr(obj, "Shape", None)
    except Exception:
        shape = None
    mesh = None
    try:
        mesh = getattr(obj, "Mesh", None)
    except Exception:
        mesh = None
    if shape is not None:
        geometry_kind = "shape"
    elif mesh is not None:
        geometry_kind = "mesh"
    else:
        geometry_kind = "none"
    shape_valid = None
    solid_count = None
    if shape is not None:
        try:
            shape_valid = bool(shape.isValid())
        except Exception:
            shape_valid = None
        try:
            solid_count = len(list(shape.Solids))
        except Exception:
            solid_count = None
    return {
        "name": str(getattr(obj, "Name", "")),
        "label": str(getattr(obj, "Label", getattr(obj, "Name", ""))),
        "typeId": str(getattr(obj, "TypeId", "")),
        "geometryKind": geometry_kind,
        "bounds": (
            document_bounds(obj)
            if geometry_kind == "shape"
            else _mesh_bounds(mesh)
            if geometry_kind == "mesh"
            else None
        ),
        "shapeValid": shape_valid,
        "meshValid": _mesh_valid(mesh) if mesh is not None else None,
        "solidCount": solid_count,
    }


def _multi_solid_expectations(objects: list[Any]) -> dict[str, dict]:
    """Pin each imported multi-solid object to the count the file carries.

    The gate's default solid contract accepts one solid and refuses more, so
    a mutation cannot silently produce a multi-solid result. An imported file
    defines its own topology: a multi-body STEP carries the count it was
    authored with, and refusing it would make every such file unimportable.
    The count is read from the object the import just created and is then
    held as the contract across the gate's recompute.
    """

    expectations: dict[str, dict] = {}
    for obj in objects:
        try:
            shape = getattr(obj, "Shape", None)
            count = len(list(shape.Solids)) if shape is not None else None
        except Exception:
            continue
        if isinstance(count, int) and count > 1:
            expectations[str(getattr(obj, "Name", ""))] = {"expected_solids": count}
    return expectations


def _native_object_ids(objects: list[Any]) -> dict[str, int] | None:
    """Stable native identity per document object name.

    FreeCAD gives every ``App::DocumentObject`` a read-only, monotonically
    increasing ``ID`` at creation. Unlike Python wrapper identity or list
    position it survives wrapper recreation, so a fresh object that took a
    pre-existing name is detected even when names and order are unchanged.
    Returns ``None`` when any object lacks the evidence, telling the
    caller its replacement guard is limited.
    """

    identities: dict[str, int] = {}
    for entry in objects:
        raw = getattr(entry, "ID", None)
        if not isinstance(raw, int) or isinstance(raw, bool):
            return None
        identities[str(getattr(entry, "Name", ""))] = raw
    return identities


# ---------------------------------------------------------------------------
# Handler.
# ---------------------------------------------------------------------------


def _import_model(ctx: Any, arguments: dict) -> dict:
    """Handle import_model: consent-gated STEP/STL import inside one mutation."""
    doc = ctx.require_document(arguments["document"])
    fmt = str(arguments["format"])
    path = ctx.canonical_path(arguments["path"])
    if not os.path.isfile(path):
        raise ToolError(VALIDATION_FAILED, f"no such import file: '{path}'")
    # Recheck the approved consent target immediately before the effect.
    _require_approved(ctx, preflight(ctx, "import_model", arguments))

    requested_name = str(arguments.get("name") or "ImportedMesh")
    created: list[Any] = []

    # The gate reads ``expectations`` after the body, where the imported
    # objects and their solid counts first exist.
    expectations: dict[str, dict] = {}
    with mutation(ctx, doc, "import_model", lambda: created, expectations=expectations):
        # identity is tracked by stable document object name, never Python
        # wrapper identity: FreeCAD recreates wrappers for existing objects,
        # so an id() diff would mistake every pre-existing object for a new
        # one — and delete it on failure.
        before_objects = list(doc.Objects or ())
        before_names = {str(entry.Name) for entry in before_objects}
        before_ids = _native_object_ids(before_objects)
        try:
            if fmt == "step":
                _import_step(doc, path)
                after_objects = list(doc.Objects or ())
                after_names = {str(entry.Name) for entry in after_objects}
                after_ids = _native_object_ids(after_objects)
                prefix_matches = len(after_objects) >= len(before_objects) and all(
                    str(current.Name) == str(original.Name)
                    for current, original in zip(after_objects, before_objects, strict=False)
                )
                missing = sorted(before_names - after_names)
                # A native import can remove a pre-existing object and
                # create a new one under the same name, leaving names and
                # list order unchanged. Detect that replacement through
                # the stable native object IDs, never wrapper identity.
                replaced = sorted(
                    name
                    for name in before_names & after_names
                    if before_ids is not None
                    and after_ids is not None
                    and after_ids[name] != before_ids[name]
                )
                if missing or replaced or not prefix_matches:
                    # The native importer removed a pre-existing object (or
                    # replaced it under the same name). Refuse: the object
                    # now sitting at a pre-existing name is not attributed
                    # to this import and is never deleted by its cleanup.
                    raise ToolError(
                        VALIDATION_FAILED,
                        "native import removed or replaced existing objects; "
                        "refusing to import over them",
                        {
                            "reason": "import_identity_conflict",
                            "conflicts": (missing + replaced or sorted(before_names))[:16],
                            "conflictCount": len(missing) + len(replaced) or 1,
                            "identityEvidence": (
                                "document_object_id"
                                if before_ids is not None and after_ids is not None
                                else "unavailable"
                            ),
                        },
                    )
                if before_names and (before_ids is None or after_ids is None):
                    # Conservative fallback: no stable native identity is
                    # available to prove each pre-existing name kept its
                    # object, so refuse rather than risk a silent
                    # replacement when the import also adds new objects.
                    raise ToolError(
                        VALIDATION_FAILED,
                        "native object identity evidence is unavailable; "
                        "refusing to import over pre-existing objects",
                        {
                            "reason": "import_identity_conflict",
                            "conflicts": sorted(before_names)[:16],
                            "conflictCount": 1,
                            "identityEvidence": "unavailable",
                        },
                    )
                created.extend(
                    entry for entry in (doc.Objects or ()) if str(entry.Name) not in before_names
                )
                if not created:
                    raise ToolError(
                        VALIDATION_FAILED,
                        f"STEP import of '{path}' created no objects",
                    )
            else:
                feature = _import_stl(doc, path, requested_name)
                created.append(feature)
                if (_facet_count(getattr(feature, "Mesh", None)) or 0) <= 0:
                    raise ToolError(
                        VALIDATION_FAILED,
                        f"STL import of '{path}' produced no facets",
                    )
        except BaseException:
            # Import.insert and the mesh loader are not transaction-aware;
            # remove only objects introduced by this call — never a
            # pre-existing object: a name present before the import is
            # skipped even when the importer replaced the object under
            # it — then let the gate abort and recompute the rest.
            for entry in list(doc.Objects or ()):
                name = str(entry.Name)
                if name in before_names:
                    continue
                try:
                    doc.removeObject(name)
                except Exception:
                    continue
            created.clear()
            raise
        expectations.update(_multi_solid_expectations(created))

    rows = [_object_row(entry) for entry in created]
    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "format": fmt,
        "path": path,
        "units": "file_defined" if fmt == "step" else "unitless_assumed_mm",
        "objectCount": len(rows),
        "objects": rows[:_IMPORT_PREVIEW_LIMIT],
        "objectsTruncated": len(rows) > _IMPORT_PREVIEW_LIMIT,
    }


HANDLERS["import_model"] = _import_model
