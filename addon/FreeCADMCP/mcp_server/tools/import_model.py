"""``import_model``: consent-gated STEP and STL import.

Imported files are untrusted input, so every import runs a file-target
consent preflight (purpose ``import``) before any effect. STEP objects are
created by ``Import.insert`` and discovered by identity difference; STL is
loaded into a ``Mesh::Feature``. Everything runs inside the shared mutation
so a failure aborts the transaction and removes only the objects this call
introduced — never a pre-existing object, even with a colliding name.

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

_MAX_IMPORT_OBJECTS = 4096

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
        "objects",
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
            "maxItems": _MAX_IMPORT_OBJECTS,
        },
    },
}

TOOL_DEFINITIONS = [
    {
        "name": "import_model",
        "description": (
            "Import a STEP or STL file into an existing document. Imported "
            "files are untrusted input, so the call requires file consent "
            "before any effect. STEP uses Import.insert and reports the "
            "created objects discovered by identity difference; STL loads "
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
    import Import

    Import.insert(path, doc.Name)


def _import_stl(doc: Any, path: str, requested_name: str) -> Any:
    import Mesh

    mesh_feature = doc.addObject("Mesh::Feature", requested_name or "ImportedMesh")
    mesh_feature.Mesh = Mesh.Mesh(path)
    return mesh_feature


def _mesh_valid(mesh: Any) -> bool | None:
    checker = getattr(mesh, "isValid", None)
    if not callable(checker):
        return None
    try:
        return bool(checker())
    except Exception:
        return None


def _mesh_bounds(mesh: Any) -> list[float] | None:
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
    try:
        return len(list(mesh.Facets))
    except Exception:
        return None


def _object_row(obj: Any) -> dict:
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


# ---------------------------------------------------------------------------
# Handler.
# ---------------------------------------------------------------------------


def _import_model(ctx: Any, arguments: dict) -> dict:
    doc = ctx.require_document(arguments["document"])
    fmt = str(arguments["format"])
    path = ctx.canonical_path(arguments["path"])
    if not os.path.isfile(path):
        raise ToolError(VALIDATION_FAILED, f"no such import file: '{path}'")
    # Recheck the approved consent target immediately before the effect.
    _require_approved(ctx, preflight(ctx, "import_model", arguments))

    requested_name = str(arguments.get("name") or "ImportedMesh")
    created: list[Any] = []

    with mutation(ctx, doc, "import_model", lambda: created):
        before_ids = {id(entry) for entry in (doc.Objects or ())}
        try:
            if fmt == "step":
                _import_step(doc, path)
                created.extend(
                    entry for entry in (doc.Objects or ()) if id(entry) not in before_ids
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
            # pre-existing object, even with a colliding name — then let
            # the gate abort and recompute the rest.
            for entry in list(doc.Objects or ()):
                if id(entry) in before_ids:
                    continue
                try:
                    doc.removeObject(str(entry.Name))
                except Exception:
                    continue
            created.clear()
            raise

    return {
        "document": str(getattr(doc, "Name", "")),
        "generation": int(ctx.document_generation(doc)),
        "format": fmt,
        "path": path,
        "units": "file_defined" if fmt == "step" else "unitless_assumed_mm",
        "objects": [_object_row(entry) for entry in created],
    }


HANDLERS["import_model"] = _import_model
