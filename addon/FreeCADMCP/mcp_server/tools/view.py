"""``capture_view`` tool: explicit GUI view capture as PNG.

Per plan section 5 item 15: the document, focus object and orientation are
explicit; a missing focus object is ``OBJECT_NOT_FOUND`` and never degrades
to ``fitAll``. Capture uses the five-argument
``saveImage(path, width, height, "White", "Framebuffer")`` call only —
there is deliberately no fallback to the legacy three-argument form, and
the white capture background keeps recognition deterministic regardless of
the user's viewport theme. The caller's selection (with subelements),
active document, camera and navigation-animation settings are restored in
``finally``.
"""

from __future__ import annotations

import base64
import os
import tempfile
from typing import Any

import FreeCAD

from ..gui_dispatch import _flush_gui_events
from ..protocol import ToolError

# Orientation names accepted for capture_view, mapped to the View3DInventor
# methods that set the camera. The names are exactly the plan's view enum.
_VIEW_METHODS = {
    "Isometric": "viewIsometric",
    "Front": "viewFront",
    "Top": "viewTop",
    "Right": "viewRight",
    "Back": "viewRear",  # FreeCAD names the rear orientation "rear"
    "Left": "viewLeft",
    "Bottom": "viewBottom",
    "Dimetric": "viewDimetric",
    "Trimetric": "viewTrimetric",
}


# Screenshot cost scales with pixel count. An omitted size resolves from the
# active on-screen viewport, scaled down to this ceiling; smaller viewports
# are never enlarged, and this ceiling is never a forced target.
MAX_AUTO_EDGE = 768
MAX_EXPLICIT_EDGE = 4096
# Last-resort size when no reliable viewport exists at all. A backgrounded
# MDI tab reports stale restored geometry (observed 400x300 on FreeCAD
# 1.1.3) instead of the on-screen viewport, so its report is never used.
_FALLBACK_VIEW_SIZE = (1024, 768)


def _view_size(view: Any) -> tuple[int, int]:
    """Viewport dimensions reported by ``view`` itself as ``(width, height)``."""

    size = view.getSize()
    if isinstance(size, (list, tuple)) and len(size) >= 2:
        return max(1, int(size[0])), max(1, int(size[1]))
    return max(1, int(size.width())), max(1, int(size.height()))


def _active_view_size(ctx: Any) -> tuple[int, int] | None:
    """Size of the currently raised 3D viewport, when one exists.

    The raised tab is the only trustworthy sizing reference: a backgrounded
    MDI subwindow reports stale restored geometry rather than the on-screen
    viewport. Only 3D views are considered; other active views (Start page,
    sketch editor) carry no usable viewport size for captures.
    """

    try:
        active_doc = ctx.Gui.ActiveDocument
        view = getattr(active_doc, "ActiveView", None)
        if view is None or not hasattr(view, "saveImage"):
            return None
        return _view_size(view)
    except Exception:
        return None


def _scale_to_max_edge(width: int, height: int, max_edge: int) -> tuple[int, int]:
    longest = max(width, height)
    if longest <= max_edge:
        return width, height
    scale = max_edge / longest
    return max(1, int(width * scale)), max(1, int(height * scale))


def resolve_capture_size(
    ctx: Any, view: Any, document: str, width: int | None, height: int | None
) -> tuple[int, int]:
    """Resolve the capture size.

    Both sizes omitted: the active on-screen viewport scaled proportionally
    down to a ``MAX_AUTO_EDGE`` longest edge — never upscaled, so the
    capture stays the smallest useful image the viewport supports. When the
    capture target is a backgrounded tab (its own size report is stale) or
    no viewport is available, the active viewport is the sizing reference;
    ``_FALLBACK_VIEW_SIZE`` applies only when neither exists. One size
    omitted: the other side comes from the same reference viewport clamped
    to ``MAX_EXPLICIT_EDGE`` (minimum 1) — the explicit side is never
    resized and no aspect ratio is inferred. Explicit sizes are honoured
    as given; each must be a positive integer no larger than
    ``MAX_EXPLICIT_EDGE``.
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
    reference = None
    try:
        if str(ctx.App.ActiveDocument.Name) == document:
            reference = _view_size(view)
    except Exception:
        reference = None
    if reference is None:
        reference = _active_view_size(ctx) or _FALLBACK_VIEW_SIZE
    if width is None and height is None:
        return _scale_to_max_edge(*reference, MAX_AUTO_EDGE)
    resolved_width = min(max(1, reference[0]), MAX_EXPLICIT_EDGE) if width is None else width
    resolved_height = min(max(1, reference[1]), MAX_EXPLICIT_EDGE) if height is None else height
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


def _capture_camera(view: Any) -> str | None:
    """Pre-capture camera string for ``view``; ``None`` when unavailable."""

    try:
        return str(view.getCamera())
    except Exception:
        return None


def _restore_camera(view: Any, camera: str | None) -> None:
    """Restore a pre-capture camera string (the framing moves the camera)."""

    if camera is None:
        return
    view.setCamera(camera)


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


def _validate_subelement(focus_obj: Any, name: str) -> str:
    """Validate ``name`` as a canonical subelement of ``focus_obj``.

    Only ``FaceN``/``EdgeN`` references are supported. The index is checked
    against the live shape so an out-of-range reference fails before any
    view, selection or active document change.
    """

    canonical = str(name)
    kind = canonical[:4]
    if kind not in ("Face", "Edge") or not canonical[4:].isdigit():
        raise ToolError(
            "VALIDATION_FAILED",
            f"unsupported focus_subelement '{canonical}'",
            {"supported": ["FaceN", "EdgeN"]},
        )
    try:
        shape = focus_obj.Shape
        count = len(shape.Faces) if kind == "Face" else len(shape.Edges)
    except AttributeError as exc:
        raise ToolError(
            "VALIDATION_FAILED",
            f"object '{focus_obj.Name}' has no Shape to resolve '{canonical}' against",
            {},
        ) from exc
    if not 1 <= int(canonical[4:]) <= count:
        raise ToolError(
            "VALIDATION_FAILED",
            f"{canonical} does not exist on '{focus_obj.Name}' ({count} {kind.lower()}s)",
            {},
        )
    return canonical


def capture_view(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """``capture_view`` handler (GUI thread only)."""

    document = arguments["document"]
    view_name = arguments["view_name"]
    focus_name = arguments["focus_object"]
    subelement_name = arguments.get("focus_subelement")
    width = arguments.get("width")
    height = arguments.get("height")

    doc = ctx.require_document(document)
    ctx.check_document_idle(doc)

    # Prevalidate the focus object and optional subelement before any view,
    # selection or active document change: a missing focus must fail
    # without reframing.
    focus_obj = ctx.require_object(doc, focus_name)
    if subelement_name is not None:
        subelement_name = _validate_subelement(focus_obj, subelement_name)

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

    resolved_width, resolved_height = resolve_capture_size(ctx, view, document, width, height)

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
                subelement_name,
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
        "document": str(document),
        "focus_object": str(focus_name),
        "focus_subelement": subelement_name,
        "view_name": str(view_name),
    }


def _capture_to_file(
    ctx: Any,
    view: Any,
    focus_obj: Any,
    subelement_name: str | None,
    orientation_method: str,
    document: str,
    tmp_path: str,
    width: int,
    height: int,
    previous_active: str | None,
    previous_selection: list[Any],
) -> None:
    """Apply orientation, frame on the focus target and save the PNG.

    Navigation animations are disabled around the orientation change and
    framing so ``saveImage`` never captures a mid-animation stale camera
    orientation; the preference values are restored in ``finally``. The
    caller's selection (with subelements), active document, camera and
    animation settings are restored whether or not the capture itself
    succeeded.
    """

    animation_state = _disable_navigation_animations()
    camera = _capture_camera(view)
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
        if subelement_name is None:
            ctx.Gui.Selection.addSelection(focus_obj)
        else:
            ctx.Gui.Selection.addSelection(focus_obj, subelement_name)
        ctx.Gui.SendMsgToActiveView("ViewSelection")
        _flush_gui_events()
        ctx.Gui.Selection.clearSelection()
        if subelement_name is None:
            ctx.Gui.Selection.addSelection(focus_obj)
        else:
            ctx.Gui.Selection.addSelection(focus_obj, subelement_name)
        ctx.Gui.SendMsgToActiveView("ViewSelection")
        ctx.Gui.Selection.clearSelection()

        # Five-argument saveImage only: "Framebuffer" reads the on-screen GL
        # context. This is the production capture path on FreeCAD 1.1.3;
        # behavior on other FreeCAD versions and graphics backends is not
        # guaranteed, and the event pumping and readback checks around this
        # call are what guard against stale or empty captures. The white
        # capture background keeps recognition deterministic regardless of
        # the user's viewport theme; the viewport preference is untouched.
        view.saveImage(tmp_path, width, height, "White", "Framebuffer")

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
            _restore_camera(view, camera)
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
        "focus_subelement": {
            "type": "string",
            "minLength": 5,
            "maxLength": 12,
        },
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
        "document": {"type": "string", "minLength": 1, "maxLength": 256},
        "focus_object": {"type": "string", "minLength": 1, "maxLength": 256},
        "focus_subelement": {
            "type": ["string", "null"],
            "minLength": 5,
            "maxLength": 12,
        },
        "view_name": {"type": "string", "enum": sorted(_VIEW_METHODS)},
    },
    "required": [
        "mimeType",
        "data",
        "width",
        "height",
        "document",
        "focus_object",
        "view_name",
    ],
    "additionalProperties": False,
}

TOOL_DEFINITIONS = [
    {
        "name": "capture_view",
        "description": (
            "Capture one PNG for qualitative visual inspection. The image "
            "uses a white background, preserves the current display mode, "
            "and frames either the complete focus object or one validated "
            "face or edge given as focus_subelement. Automatic captures use "
            "the active viewport scaled to at most a 768 px longest edge. "
            "The caller's camera, selection, active document, and "
            "navigation settings are restored. structuredContent reports "
            "the captured document, focus target, view name, and image "
            "dimensions."
        ),
        "inputSchema": _TOOL_INPUT_SCHEMA,
        "outputSchema": _TOOL_OUTPUT_SCHEMA,
    }
]

HANDLERS = {"capture_view": capture_view}
