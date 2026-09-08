"""``capture_view`` tool: explicit GUI view capture as PNG.

Per plan section 5 item 15: the document, focus object and orientation are
explicit; a missing focus object is ``OBJECT_NOT_FOUND`` and never degrades
to ``fitAll``. Capture uses the five-argument
``saveImage(path, width, height, "Current", "Framebuffer")`` call only —
there is deliberately no fallback to the legacy three-argument form. The
caller's selection and active document are restored in ``finally``.
"""

from __future__ import annotations

import base64
import os
import tempfile
from typing import Any

import FreeCAD
import FreeCADGui

from ..gui_dispatch import _flush_gui_events
from ..protocol import ToolError

# Orientation names accepted for capture_view, mapped to the View3DInventor
# methods that set the camera. The names are exactly the plan's view enum.
_VIEW_METHODS = {
    "Isometric": "viewIsometric",
    "Front": "viewFront",
    "Top": "viewTop",
    "Right": "viewRight",
    "Back": "viewBack",
    "Left": "viewLeft",
    "Bottom": "viewBottom",
    "Dimetric": "viewDimetric",
    "Trimetric": "viewTrimetric",
}


# Screenshot cost scales with pixel count, so an omitted size is clamped to a
# bounded longest edge. Explicit sizes are honoured up to MAX_EXPLICIT_EDGE.
MAX_AUTO_EDGE = 1024
MAX_EXPLICIT_EDGE = 4096


def _view_size(view: Any) -> tuple[int, int]:
    """Current viewport dimensions as ``(width, height)`` (the one shared
    dimension helper for every size path; no per-call fallback variants)."""

    try:
        size = view.getSize()
        if isinstance(size, (list, tuple)) and len(size) >= 2:
            return max(1, int(size[0])), max(1, int(size[1]))
        return max(1, int(size.width())), max(1, int(size.height()))
    except Exception:
        return 1024, 768


def _scale_to_max_edge(width: int, height: int, max_edge: int) -> tuple[int, int]:
    longest = max(width, height)
    if longest <= max_edge:
        return width, height
    scale = max_edge / longest
    return max(1, int(width * scale)), max(1, int(height * scale))


def resolve_capture_size(
    view: Any, width: int | None, height: int | None
) -> tuple[int, int]:
    """Resolve the capture size.

    Both sizes omitted: current viewport, longest edge clamped to
    ``MAX_AUTO_EDGE``. One size omitted: the other side comes from the
    current viewport unclamped. Explicit sizes are honoured as given; each
    must be a positive integer no larger than ``MAX_EXPLICIT_EDGE``.
    """

    for name, value in (("width", width), ("height", height)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError("VALIDATION_FAILED", f"{name} must be an integer", {})
        if not 1 <= value <= MAX_EXPLICIT_EDGE:
            raise ToolError(
                "VALIDATION_FAILED",
                f"{name} must be between 1 and {MAX_EXPLICIT_EDGE}",
                {},
            )
    view_width, view_height = _view_size(view)
    if width is None and height is None:
        return _scale_to_max_edge(view_width, view_height, MAX_AUTO_EDGE)
    resolved_width = view_width if width is None else width
    resolved_height = view_height if height is None else height
    return resolved_width, resolved_height


# Navigation camera animation: with ``UseNavigationAnimations`` enabled
# (observed default on live FreeCAD 1.1.3, AnimationDuration 500) an
# orientation change animates and ``saveImage`` captures a stale mid-flight
# orientation (live smoke: Isometric captured as front view). The
# orientation and framing are applied with the animation preference
# temporarily disabled and the setting is restored afterwards.
_VIEW_PARAM_PATH = "User parameter:BaseApp/Preferences/View"
_DEFAULT_ANIMATION_DURATION = 500


def _disable_navigation_animations() -> dict[str, Any]:
    params = FreeCAD.ParamGet(_VIEW_PARAM_PATH)
    state = {
        "params": params,
        "use_animations": params.GetBool("UseNavigationAnimations", True),
        "duration": params.GetInt("AnimationDuration", _DEFAULT_ANIMATION_DURATION),
    }
    params.SetBool("UseNavigationAnimations", False)
    params.SetInt("AnimationDuration", 0)
    return state


def _restore_navigation_animations(state: dict[str, Any]) -> None:
    params = state["params"]
    params.SetBool("UseNavigationAnimations", state["use_animations"])
    params.SetInt("AnimationDuration", state["duration"])


def _capture_selection_snapshot(ctx: Any) -> list[dict[str, Any]]:
    """Subelement-preserving selection snapshot across all documents.

    ``clearSelection`` wipes the selection of every document, so the
    caller's selection — including face/edge subelement selections that
    plain object lists lose — is captured as per-document
    ``SelectionObject`` snapshots and restored exactly.
    """

    snapshots: list[dict[str, Any]] = []
    try:
        documents = list(ctx.App.listDocuments().values())
    except Exception:
        documents = []
    for doc in documents:
        try:
            selection_ex = ctx.Gui.Selection.getSelectionEx(str(doc.Name))
        except Exception:
            continue
        for selected in selection_ex:
            obj = getattr(selected, "Object", None)
            if obj is None:
                continue
            subelements = [str(sub) for sub in (selected.SubElementNames or [])]
            snapshots.append({"object": obj, "subelements": subelements})
    return snapshots


def _restore_selection_snapshot(ctx: Any, snapshots: list[dict[str, Any]]) -> None:
    ctx.Gui.Selection.clearSelection()
    for snapshot in snapshots:
        obj = snapshot["object"]
        subelements = snapshot["subelements"]
        if subelements:
            for subelement in subelements:
                ctx.Gui.Selection.addSelection(obj, subelement)
        else:
            ctx.Gui.Selection.addSelection(obj)


def _gui_document(ctx: Any, document: str) -> Any:
    """Resolve the GUI document for ``document``; no implicit fallback."""

    try:
        gui_doc = ctx.Gui.getDocument(document)
    except Exception as exc:
        raise ToolError(
            "GUI_DISPATCH_FAILED",
            f"document '{document}' has no GUI view to capture",
            {"document": document, "reason": str(exc)},
        ) from exc
    if gui_doc is None:
        raise ToolError(
            "GUI_DISPATCH_FAILED",
            f"document '{document}' has no GUI view to capture",
            {"document": document},
        )
    return gui_doc


def capture_view(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """``capture_view`` handler (GUI thread only)."""

    document = arguments["document"]
    view_name = arguments["view_name"]
    focus_name = arguments["focus_object"]
    width = arguments.get("width")
    height = arguments.get("height")

    doc = ctx.require_document(document)
    ctx.check_document_idle(doc)

    # Prevalidate the focus object before any view, selection or active
    # document change: a missing focus must fail without reframing.
    focus_obj = ctx.require_object(doc, focus_name)

    if view_name not in _VIEW_METHODS:
        raise ToolError(
            "UNSUPPORTED_VIEW",
            f"unsupported view '{view_name}'",
            {"views": sorted(_VIEW_METHODS)},
        )

    gui_doc = _gui_document(ctx, document)
    view = getattr(gui_doc, "ActiveView", None)
    if view is None or not hasattr(view, "saveImage"):
        raise ToolError(
            "UNSUPPORTED_VIEW",
            f"the view of document '{document}' does not support capture",
            {"document": document},
        )

    resolved_width, resolved_height = resolve_capture_size(view, width, height)

    previous_active: str | None = None
    active_doc = ctx.App.ActiveDocument
    if active_doc is not None:
        previous_active = str(active_doc.Name)
    previous_selection = _capture_selection_snapshot(ctx)

    fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="mcp-capture-")
    os.close(fd)
    try:
        try:
            _capture_to_file(
                ctx,
                view,
                focus_obj,
                _VIEW_METHODS[view_name],
                str(document),
                tmp_path,
                resolved_width,
                resolved_height,
                previous_active,
                previous_selection,
            )
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(
                "GUI_DISPATCH_FAILED",
                f"view capture failed: {type(exc).__name__}: {exc}",
                {"document": str(document)},
            ) from exc
        try:
            with open(tmp_path, "rb") as handle:
                png_bytes = handle.read()
        except OSError as exc:
            raise ToolError(
                "GUI_DISPATCH_FAILED",
                "view capture could not be read back",
                {"reason": str(exc)},
            ) from exc
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return {
        "mimeType": "image/png",
        "data": base64.b64encode(png_bytes).decode("ascii"),
        "width": resolved_width,
        "height": resolved_height,
    }


def _capture_to_file(
    ctx: Any,
    view: Any,
    focus_obj: Any,
    orientation_method: str,
    document: str,
    tmp_path: str,
    width: int,
    height: int,
    previous_active: str | None,
    previous_selection: list[Any],
) -> None:
    """Apply orientation, frame on the focus object and save the PNG.

    Navigation animations are disabled around the orientation change and
    framing so ``saveImage`` never captures a mid-animation stale camera
    orientation; the preference values are restored in ``finally``. The
    caller's selection (with subelements) and active document are restored
    whether or not the capture itself succeeded.
    """

    animation_state = _disable_navigation_animations()
    try:
        ctx.App.setActiveDocument(document)
        ctx.Gui.setActiveDocument(document)
        _flush_gui_events()

        getattr(view, orientation_method)()
        _flush_gui_events()

        # Frame on the prevalidated focus through selection framing. The
        # framing is issued twice: the first pass pumps stale frames out of
        # the compositor (macOS occluded-window blank captures, Linux stale
        # frames), the second pass runs synchronously right before
        # saveImage so the frame matches the requested framing.
        ctx.Gui.Selection.clearSelection()
        ctx.Gui.Selection.addSelection(focus_obj)
        ctx.Gui.SendMsgToActiveView("ViewSelection")
        _flush_gui_events()
        ctx.Gui.Selection.clearSelection()
        ctx.Gui.Selection.addSelection(focus_obj)
        ctx.Gui.SendMsgToActiveView("ViewSelection")
        ctx.Gui.Selection.clearSelection()

        # Five-argument saveImage only: "Framebuffer" reads the on-screen GL
        # context and captures correctly on Wayland/X11/Windows/macOS.
        view.saveImage(tmp_path, width, height, "Current", "Framebuffer")

        if os.path.getsize(tmp_path) <= 0:
            raise ToolError(
                "GUI_DISPATCH_FAILED",
                "view capture produced an empty image",
                {"document": document},
            )
    finally:
        try:
            _restore_selection_snapshot(ctx, previous_selection)
        except Exception:
            pass
        if previous_active is not None:
            try:
                ctx.App.setActiveDocument(previous_active)
                ctx.Gui.setActiveDocument(previous_active)
            except Exception:
                pass
        try:
            _restore_navigation_animations(animation_state)
        except Exception:
            pass


_TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "document": {"type": "string", "minLength": 1, "maxLength": 256},
        "focus_object": {"type": "string", "minLength": 1, "maxLength": 256},
        "view_name": {
            "type": "string",
            "enum": sorted(_VIEW_METHODS),
        },
        "width": {"type": "integer", "minimum": 1, "maximum": MAX_EXPLICIT_EDGE},
        "height": {"type": "integer", "minimum": 1, "maximum": MAX_EXPLICIT_EDGE},
    },
    "required": ["document", "focus_object", "view_name"],
    "additionalProperties": False,
}

_TOOL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "mimeType": {"type": "string", "const": "image/png"},
        "data": {"type": "string", "minLength": 1},
        "width": {"type": "integer", "minimum": 1, "maximum": MAX_EXPLICIT_EDGE},
        "height": {"type": "integer", "minimum": 1, "maximum": MAX_EXPLICIT_EDGE},
    },
    "required": ["mimeType", "data", "width", "height"],
    "additionalProperties": False,
}

TOOL_DEFINITIONS = [
    {
        "name": "capture_view",
        "description": (
            "Capture a PNG of the document's 3D view with an explicit "
            "orientation, framed on one existing object. Returns the PNG as "
            "base64 image content. The caller's selection and active "
            "document are preserved."
        ),
        "inputSchema": _TOOL_INPUT_SCHEMA,
        "outputSchema": _TOOL_OUTPUT_SCHEMA,
    }
]

HANDLERS = {"capture_view": capture_view}
