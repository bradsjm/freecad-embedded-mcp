"""The ``inspect_user_context`` tool.

Read-only observation of the user's context: active document, active and
edit objects, workbench, the foreground view (type, camera, viewport), the
bounded GUI selection as explicit targets, and optionally one unchanged PNG
of the active 3D viewport. Failed native reads become ``unavailable``
markers while verified facts stay usable; nothing is framed, activated, or
reconfigured. The module imports no FreeCAD at module level and raises no
``ToolError``: every native read is guarded and reported.
"""

from __future__ import annotations

import base64
import math
import re
from typing import Any

from .. import topology_query as tq
from ..protocol import check_schema
from .view_camera import (
    _camera_axes,
    _camera_keyword_values,
    _finite_vec,
    _parse_camera,
)
from .view_capture import (
    MAX_AUTO_EDGE,
    _new_capture_temp,
    _scale_to_max_edge,
    _unlink_quiet,
    _view_size,
)

# Context snapshot bounds: at most 64 flattened selection rows are
# returned; the full flattened count and ``truncated`` report the rest.
_MAX_CONTEXT_SELECTIONS = 64
_CONTEXT_CANONICAL_SUBELEMENT = re.compile(r"(?:Face|Edge)[1-9][0-9]*")
_UNREADABLE_SUBELEMENTS = object()

# Canonical emission order of the closed ``unavailable`` literal set.
_CONTEXT_UNAVAILABLE_ORDER = (
    "activeDocument",
    "activeObject",
    "editObject",
    "workbench",
    "activeView",
    "camera",
    "viewport",
    "selection",
    "image",
)


def _context_viewport_size(view: Any) -> tuple[int, int] | None:
    """Native viewport dimensions for the context snapshot.

    Unlike the capture sizing path this never clamps or substitutes:
    the native report is confirmed positive first, then ``_view_size``
    supplies the shared conversion; a failed, non-numeric, or
    nonpositive native report is an unavailable viewport, never a
    usable size.
    """

    try:
        size = view.getSize()
        if isinstance(size, (list, tuple)) and len(size) >= 2:
            native_width, native_height = size[0], size[1]
        else:
            native_width, native_height = size.width(), size.height()
        positive = all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
            for value in (native_width, native_height)
        )
        if not positive:
            return None
        width, height = _view_size(view)
    except Exception:
        return None
    return width, height


def _context_camera(view: Any) -> tuple[dict[str, Any], bool]:
    """Camera position, direction, up and projection for the snapshot.

    The normal path reuses ``_camera_axes`` (direction/up) and
    ``_parse_camera`` (position); ``_camera_keyword_values`` is only the
    partial-position fallback for a camera string whose orientation is
    unreadable. Each field is read independently: a readable field is
    reported even when a sibling field cannot be read, and any
    unreadable or non-finite field marks the camera unavailable.
    """

    camera: dict[str, Any] = {
        "position": None,
        "direction": None,
        "up": None,
        "projection": None,
    }
    ok = True
    try:
        camera_type = str(view.getCameraType())
    except Exception:
        camera_type = ""
    projection = {"Orthographic": "orthographic", "Perspective": "perspective"}.get(camera_type)
    if projection is None:
        ok = False
    else:
        camera["projection"] = projection
    try:
        camera_string = str(view.getCamera())
    except Exception:
        camera_string = None
    axes = _camera_axes(view) if camera_string is not None else None
    if axes is not None:
        camera["direction"], camera["up"] = axes
    else:
        ok = False
    parsed = _parse_camera(camera_string) if camera_string else None
    if parsed is not None and _finite_vec(parsed["position"][0]):
        camera["position"] = list(parsed["position"][0])
    elif parsed is not None:
        ok = False
    else:
        # Partial-position fallback: a valid position survives an
        # unreadable orientation.
        partial = _camera_keyword_values(camera_string, "position", 3) if camera_string else None
        if partial is not None and _finite_vec(partial[0]):
            camera["position"] = list(partial[0])
        else:
            ok = False
    return camera, ok


def _context_unwrap(native: Any) -> Any:
    """Unwrap a GUI-side holder to its App object (``.Object`` when present).

    A native App object without an ``Object`` attribute is returned
    itself; other getter failures propagate to the guarded caller and
    are never converted into a fallback.
    """

    try:
        obj = native.Object
    except AttributeError:
        return native
    return native if obj is None else obj


def _context_object_identity(obj: Any) -> dict[str, Any] | None:
    """The object's actual ``Document.Name``/``Name`` identity.

    Never falls back to another document or stringifies a missing name:
    an unreadable identity is None and marks the fact unavailable.
    """

    try:
        name = obj.Name
        document = obj.Document.Name
    except Exception:
        return None
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(document, str) or not document:
        return None
    return {"document": document, "object": name}


def _context_generation(value: Any) -> int | None:
    """A usable nonnegative generation counter, or None when malformed."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _context_row(
    document: str | None,
    generation: int | None,
    object_name: str | None,
    label: str | None,
    type_id: str | None,
    subelement: str | None,
    target: Any,
    reason: str | None,
) -> dict[str, Any]:
    """One closed selection observation row."""

    return {
        "document": document,
        "generation": generation,
        "object": object_name,
        "label": label,
        "type": type_id,
        "subelement": subelement,
        "target": target,
        "reason": reason,
    }


def _context_selection_row(ctx: Any, so: Any, subelement: Any) -> dict[str, Any]:
    """One flattened selection observation, converted to a safe target.

    Classification order: live object identity, whole-object case,
    linked-instance subelement, sketch subelement, canonical ``FaceN``/
    ``EdgeN`` name, shape/index validation. Per-row failures stay local:
    a diagnostic row with a null target is kept and neighboring rows are
    unaffected. Diagnostic subelement names are preserved unchanged.
    """

    # Independent fact reads: each readable identity fact survives a
    # failed sibling getter and stays in the diagnostic row.
    document: str | None = None
    object_name: str | None = None
    label: str | None = None
    type_id: str | None = None
    native: Any = None
    try:
        raw_document = so.DocumentName
        if isinstance(raw_document, str) and raw_document:
            document = raw_document
    except Exception:
        document = None
    try:
        raw_object = so.ObjectName
        if isinstance(raw_object, str) and raw_object:
            object_name = raw_object
    except Exception:
        object_name = None
    try:
        native = so.Object
    except Exception:
        native = None
    for attribute, slot in (("Label", "label"), ("TypeId", "type_id")):
        try:
            value = getattr(native, attribute)
            if value is not None:
                if slot == "label":
                    label = str(value)
                else:
                    type_id = str(value)
        except Exception:
            pass
    row = _context_row(
        document,
        None,
        object_name,
        label,
        type_id,
        subelement if isinstance(subelement, str) else None,
        None,
        None,
    )
    try:
        doc = ctx.require_document(document)
        live = ctx.require_object(doc, object_name)
    except Exception:
        row["reason"] = "selection_object_unavailable"
        return row
    try:
        row["generation"] = _context_generation(ctx.document_generation(doc))
    except Exception:
        row["generation"] = None
    if subelement is _UNREADABLE_SUBELEMENTS:
        # The subelement list is unknown; readable facts and the
        # generation stay in the diagnostic row.
        row["reason"] = "selection_object_unavailable"
        return row
    if native is None or live is not native or row["generation"] is None:
        # A missing selected object, a resolution to another instance,
        # or a missing generation prevents target minting.
        row["reason"] = "selection_object_unavailable"
        return row
    if subelement is None:
        from .geometry import whole_reference

        # Whole-object case, including shapeless objects and whole
        # linked instances: the target names the selected instance.
        row["target"] = whole_reference(native)
        return row
    if type_id is None:
        row["reason"] = "selection_object_unavailable"
        return row
    if type_id.startswith("App::Link"):
        # Instance topology correspondence is not proven in this slice;
        # classified before the sketch probe.
        row["reason"] = "unsupported_instance"
        return row
    try:
        derived = getattr(native, "isDerivedFrom", None)
        is_sketch = (
            derived("Sketcher::SketchObject")
            if callable(derived)
            else type_id == "Sketcher::SketchObject"
        )
    except Exception:
        # A failed type probe is an unavailable object row, never
        # evidence of a nonsketch type.
        row["reason"] = "selection_object_unavailable"
        return row
    if is_sketch:
        # Displayed sketch edge labels do not map to editable geometry
        # or constraint identifiers.
        row["reason"] = "sketch_subelement"
        return row
    match = _CONTEXT_CANONICAL_SUBELEMENT.fullmatch(subelement)
    if match is None:
        row["reason"] = "unsupported_subelement"
        return row
    role = "face" if subelement.startswith("Face") else "edge"
    try:
        index = int(subelement[4:])  # len("Face") == len("Edge") == 4
        shape = native.Shape
        subshapes = list(shape.Faces if role == "face" else shape.Edges)
    except Exception:
        row["reason"] = "selection_geometry_unavailable"
        return row
    if index < 1 or index > len(subshapes):
        row["reason"] = "selection_geometry_unavailable"
        return row
    from .geometry import make_reference

    try:
        row["target"] = make_reference(ctx, doc, native, role, index)
    except Exception:
        row["reason"] = "selection_geometry_unavailable"
    return row


def _context_selection(ctx: Any) -> tuple[dict[str, Any], set[str]]:
    """Bounded GUI selection snapshot: flattened observation rows.

    ``getSelectionEx("*", 0)`` disables native subobject resolution so a
    selected instance is never replaced by its source. Each
    SelectionObject contributes one row per subelement member (an empty
    list or an empty-string subelement is one whole-object observation);
    native order is preserved but is not click chronology. An unreadable
    subelement list keeps one diagnostic row and makes the count
    unknown, which marks the selection unavailable.
    """

    try:
        selection_objects = list(ctx.Gui.Selection.getSelectionEx("*", 0))
    except Exception:
        return (
            {"status": "unavailable", "count": None, "entries": [], "truncated": False},
            {"selection"},
        )
    # Ordered rows carry a known-cardinality flag: an unreadable
    # subelement list keeps one diagnostic placeholder row, but its
    # unknown cardinality never counts toward the truncation threshold
    # and forces the count to null.
    rows: list[tuple[Any, Any, bool]] = []
    known_count = 0
    for so in selection_objects:
        try:
            subelements = list(so.SubElementNames)
        except Exception:
            rows.append((so, _UNREADABLE_SUBELEMENTS, False))
            continue
        if not subelements:
            rows.append((so, None, True))
            known_count += 1
            continue
        for subelement in subelements:
            rows.append((so, None if subelement == "" else str(subelement), True))
            known_count += 1
    count = known_count if not any(not known for _so, _sub, known in rows) else None
    truncated = known_count > _MAX_CONTEXT_SELECTIONS
    entries = [
        _context_selection_row(ctx, so, subelement)
        for so, subelement, _known in rows[:_MAX_CONTEXT_SELECTIONS]
    ]
    return (
        {
            "status": "available" if count is not None else "unavailable",
            "count": count,
            "entries": entries,
            "truncated": truncated,
        },
        set() if count is not None else {"selection"},
    )


def _context_capture_image(view: Any, width: int, height: int) -> tuple[bytes, int, int] | None:
    """One unchanged PNG of the active 3D viewport, or None on failure.

    Uses only the four-argument ``saveImage(path, width, height,
    "Current")`` form: no framing, orientation, event pumping,
    selection clearing, or preference setters are called, so the view's
    own state is the capture input. Whether the backend renders
    selection highlights into that output is not asserted here. The
    file is decoded locally with PySide to confirm it is decodable and
    matches the requested dimensions; any failure — including temp
    creation or the Qt import — is None, never a partial image or an
    escaped exception.
    """

    path: str | None = None
    try:
        from PySide import QtGui

        path = _new_capture_temp()
        try:
            view.saveImage(path, width, height, "Current")
            with open(path, "rb") as handle:
                png_bytes = handle.read()
        except Exception:
            return None
        if not png_bytes:
            return None
        try:
            image = QtGui.QImage(path)
            if image.isNull() or int(image.width()) != width or int(image.height()) != height:
                return None
        except Exception:
            return None
        return png_bytes, width, height
    except Exception:
        return None
    finally:
        if path is not None:
            _unlink_quiet(path)


def inspect_user_context(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """``inspect_user_context`` handler (GUI thread only).

    Reads the current user context: active document, active and edit
    objects, workbench, foreground view (type, camera, viewport), the
    GUI selection as bounded explicit targets, and optionally one
    unchanged PNG of the active 3D viewport. Failed native reads become
    ``unavailable`` markers while the verified facts stay usable; the
    handler never activates a view, changes selection, or substitutes a
    fallback. The result describes state at execution time, not at
    user-message submission time.
    """

    unavailable: set[str] = set()

    active_document: dict[str, Any] | None = None
    active_doc = None
    active_doc_name: str | None = None
    try:
        active_doc = ctx.App.ActiveDocument
    except Exception:
        unavailable.add("activeDocument")
    if active_doc is not None:
        try:
            active_doc_name = str(active_doc.Name)
            generation = _context_generation(ctx.document_generation(active_doc))
            if generation is None:
                raise ValueError("malformed active document facts")
            active_document = {
                "name": active_doc_name,
                "label": str(active_doc.Label),
                "generation": generation,
            }
        except Exception:
            active_document = None
            active_doc_name = None
            unavailable.add("activeDocument")

    active_object: dict[str, Any] | None = None
    edit_object: dict[str, Any] | None = None
    if active_doc is not None:
        try:
            gui_doc = ctx.Gui.getDocument(active_doc_name)
        except Exception:
            gui_doc = None
            unavailable.update(("activeObject", "editObject"))
        if gui_doc is None:
            unavailable.update(("activeObject", "editObject"))
        else:
            try:
                active_holder = gui_doc.ActiveObject
            except Exception:
                unavailable.add("activeObject")
                active_holder = None
            if active_holder is not None:
                try:
                    unwrapped = _context_unwrap(active_holder)
                    active_object = _context_object_identity(unwrapped)
                except Exception:
                    active_object = None
                if active_object is None:
                    unavailable.add("activeObject")
            try:
                edit_holder = gui_doc.getInEdit()
            except Exception:
                unavailable.add("editObject")
                edit_holder = None
            if edit_holder is not None:
                try:
                    unwrapped = _context_unwrap(edit_holder)
                    edit_object = _context_object_identity(unwrapped)
                except Exception:
                    edit_object = None
                if edit_object is None:
                    unavailable.add("editObject")

    try:
        workbench = str(ctx.Gui.activeWorkbench().name())
    except Exception:
        workbench = None
        unavailable.add("workbench")

    view: Any = None
    try:
        view = ctx.Gui.getMainWindow().getActiveWindow()
    except Exception:
        unavailable.add("activeView")
    active_view: dict[str, Any] | None = None
    if view is not None:
        try:
            # Descriptive for every returned view type; only views
            # exposing the 3D camera/size/capture surface are inspected
            # further.
            description = repr(view)
            eligible = all(
                callable(getattr(view, attribute, None))
                for attribute in ("getCamera", "getSize", "saveImage")
            )
        except Exception:
            unavailable.add("activeView")
            description = None
            eligible = False
        if description is not None:
            active_view = {"type": description, "camera": None, "viewport": None}
            if eligible:
                camera, camera_ok = _context_camera(view)
                active_view["camera"] = camera
                if not camera_ok:
                    unavailable.add("camera")
                viewport = _context_viewport_size(view)
                if viewport is None:
                    unavailable.add("viewport")
                else:
                    active_view["viewport"] = {"width": viewport[0], "height": viewport[1]}
            else:
                unavailable.update(("camera", "viewport"))
    # A successful None foreground window is empty state: activeView
    # stays null without an unavailable marker; a failed getter left the
    # activeView marker above.

    selection, selection_unavailable = _context_selection(ctx)
    unavailable.update(selection_unavailable)

    result: dict[str, Any] = {
        "activeDocument": active_document,
        "activeObject": active_object,
        "editObject": edit_object,
        "workbench": workbench,
        "activeView": active_view,
        "selection": selection,
    }

    if arguments.get("include_image", False):
        try:
            eligible = (
                active_view is not None
                and view is not None
                and all(
                    callable(getattr(view, attribute, None))
                    for attribute in ("getCamera", "getSize", "saveImage")
                )
                and active_view["viewport"] is not None
            )
        except Exception:
            eligible = False
        if not eligible:
            unavailable.add("image")
        else:
            width, height = _scale_to_max_edge(
                int(active_view["viewport"]["width"]),
                int(active_view["viewport"]["height"]),
                MAX_AUTO_EDGE,
            )
            captured = _context_capture_image(view, width, height)
            if captured is None:
                unavailable.add("image")
            else:
                png_bytes, image_width, image_height = captured
                result["mimeType"] = "image/png"
                result["data"] = base64.b64encode(png_bytes).decode("ascii")
                result["width"] = image_width
                result["height"] = image_height

    result["unavailable"] = [
        marker for marker in _CONTEXT_UNAVAILABLE_ORDER if marker in unavailable
    ]
    return result


# ---------------------------------------------------------------------------
# inspect_user_context contract.
# ---------------------------------------------------------------------------

#: Closed input: no required properties, only the optional image flag.
_CONTEXT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "include_image": {"type": "boolean", "default": False},
    },
    "additionalProperties": False,
}

#: Nullable three-number vector: an unavailable camera field is null,
#: never a substituted value.
_CONTEXT_VECTOR3 = {
    "type": ["array", "null"],
    "items": {"type": "number"},
    "minItems": 3,
    "maxItems": 3,
}

#: Selective nullability: a present name or document is a real
#: 1-256-character string; selection rows keep nullable identity fields
#: because a diagnostic row reports only what was readable.
_CONTEXT_NAME = {"type": ["string", "null"], "minLength": 1, "maxLength": 256}

_CONTEXT_REQUIRED_NAME = {"type": "string", "minLength": 1, "maxLength": 256}

#: Whole identity or nothing: an active or edit object is reported with
#: its actual document/name strings, or the whole fact is null.
_CONTEXT_OBJECT_IDENTITY = {
    "type": ["object", "null"],
    "properties": {
        "document": _CONTEXT_REQUIRED_NAME,
        "object": _CONTEXT_REQUIRED_NAME,
    },
    "required": ["document", "object"],
    "additionalProperties": False,
}

#: Camera fields or null: each vector is nullable, projection is the
#: closed orthographic/perspective vocabulary or null.
_CONTEXT_CAMERA = {
    "type": ["object", "null"],
    "properties": {
        "position": _CONTEXT_VECTOR3,
        "direction": _CONTEXT_VECTOR3,
        "up": _CONTEXT_VECTOR3,
        "projection": {"enum": ["orthographic", "perspective", None]},
    },
    "required": ["position", "direction", "up", "projection"],
    "additionalProperties": False,
}

#: Positive native viewport dimensions, or null when unavailable.
_CONTEXT_VIEWPORT = {
    "type": ["object", "null"],
    "properties": {
        "width": {"type": "integer", "minimum": 1},
        "height": {"type": "integer", "minimum": 1},
    },
    "required": ["width", "height"],
    "additionalProperties": False,
}

#: One flattened selection observation: readable identity facts, the
#: native diagnostic subelement name, a safe explicit target or null
#: with a closed diagnostic reason.
_CONTEXT_SELECTION_ROW = {
    "type": "object",
    "properties": {
        "document": _CONTEXT_NAME,
        "generation": {"type": ["integer", "null"], "minimum": 0},
        "object": _CONTEXT_NAME,
        "label": {"type": ["string", "null"]},
        "type": {"type": ["string", "null"]},
        "subelement": {"type": ["string", "null"]},
        "target": {
            "anyOf": [
                {"type": "null"},
                {"$ref": "#/$defs/topologyWholeTarget"},
                {"$ref": "#/$defs/topologyReferenceTarget"},
            ]
        },
        "reason": {
            "enum": [
                None,
                "unsupported_subelement",
                "unsupported_instance",
                "sketch_subelement",
                "selection_object_unavailable",
                "selection_geometry_unavailable",
            ]
        },
    },
    "required": [
        "document",
        "generation",
        "object",
        "label",
        "type",
        "subelement",
        "target",
        "reason",
    ],
    "additionalProperties": False,
}

#: Bounded selection report: flattened count (null when a subelement
#: list was unreadable), at most 64 rows, and an explicit truncation
#: flag.
_CONTEXT_SELECTION = {
    "type": "object",
    "properties": {
        "status": {"enum": ["available", "unavailable"]},
        "count": {"type": ["integer", "null"], "minimum": 0},
        "entries": {
            "type": "array",
            "items": _CONTEXT_SELECTION_ROW,
            "maxItems": _MAX_CONTEXT_SELECTIONS,
        },
        "truncated": {"type": "boolean"},
    },
    "required": ["status", "count", "entries", "truncated"],
    "additionalProperties": False,
}

#: Raw handler schema: all four image fields are optional because a
#: JSON-only result and an unavailable image are both valid; the
#: handler emits either all four or none. The server strips ``data``
#: for the published schema (mimeType/width/height survive as optional
#: metadata).
_CONTEXT_OUTPUT_SCHEMA = tq.merge_query_defs(
    {
        "type": "object",
        "properties": {
            "activeDocument": {
                "type": ["object", "null"],
                "properties": {
                    "name": _CONTEXT_REQUIRED_NAME,
                    "label": {"type": "string"},
                    "generation": {"type": "integer", "minimum": 0},
                },
                "required": ["name", "label", "generation"],
                "additionalProperties": False,
            },
            "activeObject": _CONTEXT_OBJECT_IDENTITY,
            "editObject": _CONTEXT_OBJECT_IDENTITY,
            "workbench": {"type": ["string", "null"]},
            "activeView": {
                "type": ["object", "null"],
                "properties": {
                    "type": {"type": "string"},
                    "camera": _CONTEXT_CAMERA,
                    "viewport": _CONTEXT_VIEWPORT,
                },
                "required": ["type", "camera", "viewport"],
                "additionalProperties": False,
            },
            "selection": _CONTEXT_SELECTION,
            "mimeType": {"type": "string", "const": "image/png"},
            "data": {"type": "string", "minLength": 1},
            "width": {"type": "integer", "minimum": 1},
            "height": {"type": "integer", "minimum": 1},
            "unavailable": {
                "type": "array",
                "items": {"enum": list(_CONTEXT_UNAVAILABLE_ORDER)},
                "maxItems": len(_CONTEXT_UNAVAILABLE_ORDER),
            },
        },
        "required": [
            "activeDocument",
            "activeObject",
            "editObject",
            "workbench",
            "activeView",
            "selection",
            "unavailable",
        ],
        "additionalProperties": False,
    }
)

check_schema(_CONTEXT_INPUT_SCHEMA)
check_schema(_CONTEXT_OUTPUT_SCHEMA)
