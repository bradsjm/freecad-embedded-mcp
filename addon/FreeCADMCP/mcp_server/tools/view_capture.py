"""Support machinery for the ``capture_view`` tool.

Orientation vocabulary, capture sizing, session and target helpers,
geometry derivation for the intent modes, framing and panel/sheet capture
pipelines, and the ``capture_view`` wire schemas. Nothing here imports
FreeCAD at module level: the session pieces that construct native
placements and drive the animation preference stay in ``view.py``, which
owns the module-level FreeCAD import for the view tool domain.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Mapping
from typing import Any

from .. import topology_query as tq
from ..gui_dispatch import _flush_gui_events
from ..protocol import ToolError
from .view_camera import (
    _camera_axes,
    _finite_vec,
    _projected_screen_up,
    _vec_add,
    _vec_dot,
    _vec_negate,
    _vec_norm,
    _vec_normalize,
    _vec_scale,
    _vec_sub,
    _vector3,
)

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

#: Capture modes, the closed set both capture schemas serve.
_CAPTURE_MODES = ("overview", "detail", "interior", "fit")


# Screenshot cost scales with pixel count. An omitted size resolves from the
# active on-screen viewport, scaled down to this ceiling; smaller viewports
# are never enlarged, and this ceiling is never a forced target.
MAX_AUTO_EDGE = 768
MAX_EXPLICIT_EDGE = 4096
# Last-resort size when no reliable viewport exists at all. A backgrounded
# MDI tab reports stale restored geometry (observed 400x300 on FreeCAD
# 1.1.3) instead of the on-screen viewport, so its report is never used.
_FALLBACK_VIEW_SIZE = (1024, 768)

# Sheet layout: every intent-mode panel is a square image with a constant
# label strip along its bottom edge. The strip is layout-constant under
# explicit width/height scaling; the manifest rect of each panel is the
# full cell (image area plus strip).
_PANEL_EDGE = 512
_LABEL_STRIP = 40
_LABEL_STRIP_COLOR = (240, 240, 240)

# Overview sheet: seven named orientation panels plus one legend cell on a
# 4x2 grid; the legend carries document, generation and the axis
# convention so the sheet is self-describing.
_OVERVIEW_VIEWS: tuple[tuple[str, str], ...] = (
    ("Isometric", "viewIsometric"),
    ("Front", "viewFront"),
    ("Back", "viewRear"),
    ("Left", "viewLeft"),
    ("Right", "viewRight"),
    ("Top", "viewTop"),
    ("Bottom", "viewBottom"),
)

# Bounded scans: visible root names reported per overview capture and
# cylindrical faces inspected per fit derivation (the cap mirrors
# geometry._MAX_FACES).
_MAX_CAPTURED_OBJECTS = 16
_MAX_FIT_FACES = 64


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
    """Scale a width/height pair proportionally under a longest-edge ceiling.

    Sizes already within the ceiling are returned unchanged, never upscaled.
    """
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

    _validate_size_argument("width", width)
    _validate_size_argument("height", height)
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


def _validate_size_argument(name: str, value: Any) -> None:
    """Validate one explicit capture size against the schema bounds.

    Shared by ``resolve_capture_size`` (single-panel captures) and
    ``_sheet_dimensions`` (intent-mode sheets); the messages are the
    schema's and are raised before any view state changes.
    """

    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError("VALIDATION_FAILED", f"{name} must be an integer", {})
    if not 1 <= value <= MAX_EXPLICIT_EDGE:
        raise ToolError(
            "VALIDATION_FAILED",
            f"{name} must be between 1 and {MAX_EXPLICIT_EDGE}",
            {},
        )


def _sheet_dimensions(
    columns: int, rows: int, width: int | None, height: int | None
) -> tuple[int, int, int, int]:
    """Resolve sheet and cell dimensions for an intent-mode sheet.

    Defaults are the fixed design totals: ``columns`` by ``rows`` cells of
    ``_PANEL_EDGE`` square images plus one ``_LABEL_STRIP`` row each.
    Explicit width/height replace the sheet size — cells flex, the label
    strips stay constant, and the sheet is the exact cell tiling. Returns
    ``(sheet_width, sheet_height, cell_width, cell_height)``.
    """

    _validate_size_argument("width", width)
    _validate_size_argument("height", height)
    if width is None and height is None:
        default_width = columns * _PANEL_EDGE
        default_height = rows * (_PANEL_EDGE + _LABEL_STRIP)
        return default_width, default_height, _PANEL_EDGE, _PANEL_EDGE
    if width is None:
        width = columns * _PANEL_EDGE
    if height is None:
        height = rows * (_PANEL_EDGE + _LABEL_STRIP)
    minimum_height = rows * (_LABEL_STRIP + 1)
    if height < minimum_height:
        raise ToolError(
            "VALIDATION_FAILED",
            f"height must leave at least {_LABEL_STRIP + 1}px per panel row ({rows} rows)",
            {"height": height, "rows": rows},
        )
    cell_width = max(1, width // columns)
    cell_height = max(1, (height - rows * _LABEL_STRIP) // rows)
    return cell_width * columns, (cell_height + _LABEL_STRIP) * rows, cell_width, cell_height


def _compose_sheet(cells: list[dict[str, Any]]) -> tuple[bytes, list[dict[str, int]]]:
    """Composite captured panels into one labeled sheet PNG.

    Each cell is ``{"label", "path", "x", "y", "w", "h", "legend"}``. A
    panel cell draws the captured image across the cell minus the constant
    label strip along its bottom edge; a legend cell draws its text lines
    over the strip background instead. ``PySide.QtGui`` is imported lazily
    so the module keeps loading headless. Returns the sheet PNG bytes and
    the per-cell rects, which the handler reports verbatim as the
    ``views`` manifest ``rect`` values.
    """

    from PySide import QtGui

    width = max(cell["x"] + cell["w"] for cell in cells)
    height = max(cell["y"] + cell["h"] for cell in cells)
    fd, sheet_path = tempfile.mkstemp(suffix=".png", prefix="mcp-capture-")
    os.close(fd)
    try:
        canvas = QtGui.QImage(width, height, QtGui.QImage.Format_RGB32)
        painter = QtGui.QPainter(canvas)
        try:
            painter.fillRect(0, 0, width, height, QtGui.QColor(255, 255, 255))
            strip = QtGui.QColor(*_LABEL_STRIP_COLOR)
            for cell in cells:
                x, y, w, h = cell["x"], cell["y"], cell["w"], cell["h"]
                if cell.get("legend"):
                    painter.fillRect(x, y, w, h, strip)
                    for offset, line in enumerate(cell["legend"]):
                        painter.drawText(x + 12, y + 20 + 16 * offset, line)
                    continue
                strip_y = y + h - _LABEL_STRIP
                image = QtGui.QImage(cell["path"])
                if image.isNull():
                    raise ToolError(
                        "GUI_DISPATCH_FAILED",
                        "panel image could not be decoded for the sheet",
                        {"label": str(cell["label"])},
                    )
                painter.drawImage(x, y, image)
                painter.fillRect(x, strip_y, w, _LABEL_STRIP, strip)
                painter.drawText(x + 12, strip_y + 26, str(cell["label"]))
        finally:
            painter.end()
        if not canvas.save(sheet_path):
            raise ToolError(
                "GUI_DISPATCH_FAILED",
                "sheet composition failed to save the PNG",
                {},
            )
        with open(sheet_path, "rb") as handle:
            sheet_bytes = handle.read()
        return sheet_bytes, [
            {"x": cell["x"], "y": cell["y"], "w": cell["w"], "h": cell["h"]} for cell in cells
        ]
    finally:
        if os.path.exists(sheet_path):
            try:
                os.unlink(sheet_path)
            except OSError:
                pass


class _RestoreGuard:
    """Collect session restore failures instead of swallowing them.

    Every session-scoped restore step runs through :meth:`protect`; a
    failure is recorded as an item/error pair and the remaining restores
    still run. After the last restore, ``raise_if_any`` turns collected
    failures into one ``restoration_failed`` error, so a capture whose
    view state could not be restored never returns its image.
    """

    def __init__(self) -> None:
        """Start with no collected restore failures."""
        self.failures: list[dict[str, str]] = []

    def protect(self, item: str, restore: Any) -> None:
        """Run one restore step, recording a failure instead of raising."""
        try:
            restore()
        except Exception as exc:
            self.failures.append({"item": item, "error": f"{type(exc).__name__}: {exc}"})

    def raise_if_any(self) -> None:
        """Raise one restoration_failed error when failures were collected."""
        if not self.failures:
            return
        raise ToolError(
            "GUI_DISPATCH_FAILED",
            "view capture succeeded but restoring the captured view state failed",
            {"reason": "restoration_failed", "restoration": self.failures},
        )


def _require_no_clipping_plane(view: Any) -> None:
    """Refuse section modes while a clipping plane is already active.

    Interior and fit sections drive the view's clipping plane themselves;
    starting from a user-enabled plane would restore the wrong state and
    cut the uncut panels, so the preexisting plane is refused instead.
    """

    try:
        active = bool(view.hasClippingPlane())
    except Exception as exc:
        raise ToolError(
            "GUI_DISPATCH_FAILED",
            "clipping plane state could not be read",
            {"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if active:
        raise ToolError(
            "VALIDATION_FAILED",
            "a clipping plane is already active; section captures need a clean view",
            {
                "reason": "clipping_plane_active",
                "nextAction": "toggle the clipping plane off in the GUI and retry",
            },
        )


def _focus_document_bounds(focus_obj: Any) -> list[float]:
    """Document-space bounds of the focus object's placed shape.

    Interior sections cut through the focus geometry, so a target without
    a usable placed shape is refused before any view change instead of
    producing an empty sheet. Geometry helpers are imported lazily so this
    module loads (and its tests run) regardless of registration order.
    """

    from .geometry import _bbox, placed_shape

    object_name = str(getattr(focus_obj, "Name", ""))
    try:
        shape = placed_shape(focus_obj)
    except ToolError as exc:
        raise ToolError(
            "VALIDATION_FAILED",
            "interior target has no shape to section",
            {"reason": "interior_target_shapeless", "object": object_name},
        ) from exc
    bounds = _bbox(shape)
    if bounds is None:
        raise ToolError(
            "VALIDATION_FAILED",
            "interior target has no shape to section",
            {"reason": "interior_target_shapeless", "object": object_name},
        )
    return bounds


def _visible_root_objects(doc: Any) -> tuple[list[str], bool]:
    """Names of the visible root objects of ``doc``, capped at 16.

    Root objects carry no parent geo feature group; visibility follows the
    App-level ``Visibility`` property when present. Beyond the cap the
    list reports ``truncated`` instead of growing unbounded.
    """

    names: list[str] = []
    truncated = False
    for obj in list(getattr(doc, "Objects", None) or []):
        try:
            if obj.getParentGeoFeatureGroup() is not None:
                continue
        except Exception:
            continue
        if not getattr(obj, "Visibility", True):
            continue
        if len(names) >= _MAX_CAPTURED_OBJECTS:
            truncated = True
            break
        names.append(str(getattr(obj, "Name", "")))
    return names, truncated


def _cylinders_of(
    obj: Any,
) -> list[tuple[tuple[float, float, float], tuple[float, float, float], float]]:
    """Cylindrical faces of the object's placed shape, capped at 64.

    Each entry is ``(unit_axis, center, radius)``. An object without a
    usable placed shape contributes no candidates; the fit derivation
    reports that as "no mating axis found" rather than failing.
    """

    from .geometry import _type_name, placed_shape

    try:
        faces = list(placed_shape(obj).Faces)[:_MAX_FIT_FACES]
    except Exception:
        return []
    found: list[tuple[tuple[float, float, float], tuple[float, float, float], float]] = []
    for face in faces:
        surface = getattr(face, "Surface", None)
        if _type_name(surface) != "Cylinder":
            continue
        axis = _vector3(getattr(surface, "Axis", None))
        center = _vector3(getattr(surface, "Center", None))
        radius = getattr(surface, "Radius", None)
        if axis is None or center is None or not isinstance(radius, (int, float)):
            continue
        unit = _vec_normalize(axis)
        if unit is None:
            continue
        found.append((unit, center, float(radius)))
    return found


def _explicit_cylinder(
    obj: Any, subelement: str | None
) -> list[tuple[tuple[float, float, float], tuple[float, float, float], float]]:
    """The one cylindrical face named by a canonical ``FaceN`` selector.

    An empty list means the selector does not name a usable cylinder; the
    fit derivation then reports "no mating axis found" instead of widening
    the search to the object's other faces.
    """

    from .geometry import _type_name, placed_shape

    if subelement is None or not subelement.startswith("Face"):
        return []
    try:
        face = placed_shape(obj).Faces[int(subelement[4:]) - 1]
        surface = face.Surface
    except Exception:
        return []
    if _type_name(surface) != "Cylinder":
        return []
    axis = _vector3(getattr(surface, "Axis", None))
    center = _vector3(getattr(surface, "Center", None))
    radius = getattr(surface, "Radius", None)
    unit = _vec_normalize(axis) if axis is not None else None
    if unit is None or center is None or not isinstance(radius, (int, float)):
        return []
    return [(unit, center, float(radius))]


def _derive_mating_axis(
    a_obj: Any,
    b_obj: Any,
    a_subelement: str | None,
    b_subelement: str | None,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Derive the mating axis of two objects from one coaxial cylinder pair.

    A canonical ``FaceN`` subelement on a selector narrows that object's
    candidates to the named face — when that face is not a cylinder the
    object contributes no candidates and the derivation reports
    ``no_mating_axis_found``. Without a ``FaceN`` subelement every
    cylindrical face of the placed shapes (capped at 64 per object) is
    considered. Exactly one
    qualifying pair is required — axes parallel within 1 degree and center
    distance within the larger radius. Zero pairs refuse with
    ``no_mating_axis_found`` (numeric tools are the next step); two or
    more refuse as ``ambiguous_mating_axis``. The axis sign is
    canonicalized (first nonzero component positive) so the derived
    section is deterministic. Returns ``(unit_axis, midpoint)``.
    """

    candidates_a = (
        _explicit_cylinder(a_obj, a_subelement)
        if a_subelement is not None and str(a_subelement).startswith("Face")
        else _cylinders_of(a_obj)
    )
    candidates_b = (
        _explicit_cylinder(b_obj, b_subelement)
        if b_subelement is not None and str(b_subelement).startswith("Face")
        else _cylinders_of(b_obj)
    )
    cos_limit = math.cos(math.radians(1.0))
    matches: list[
        tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
    ] = []
    for axis_a, center_a, radius_a in candidates_a:
        for axis_b, center_b, radius_b in candidates_b:
            if abs(_vec_dot(axis_a, axis_b)) < cos_limit:
                continue
            if _vec_norm(_vec_sub(center_a, center_b)) > max(radius_a, radius_b):
                continue
            matches.append((axis_a, center_a, center_b))
    if not matches:
        raise ToolError(
            "VALIDATION_FAILED",
            "no coaxial cylindrical mating pair found between the two objects",
            {
                "reason": "no_mating_axis_found",
                "nextTool": "inspect_topology",
                "suggestions": ["section_axis", "section_point"],
            },
        )
    if len(matches) > 1:
        raise ToolError(
            "VALIDATION_FAILED",
            "multiple coaxial cylindrical pairs match; the mating axis is ambiguous",
            {
                "reason": "ambiguous_mating_axis",
                "pairs": len(matches),
                "suggestions": ["section_axis", "section_point"],
            },
        )
    axis, center_a, center_b = matches[0]
    for component in axis:
        if component > 0.0:
            break
        if component < 0.0:
            axis = _vec_negate(axis)
            break
    midpoint = _vec_scale(_vec_add(center_a, center_b), 0.5)
    return axis, midpoint


def _derive_detail_orientation(
    focus_obj: Any, subelement: str | None
) -> (
    tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
        float | None,
    ]
    | None
):
    """Derive a detail camera from a planar ``FaceN`` target.

    ``direction`` is the inward view direction (opposite the face normal)
    and ``up`` the least parallel document basis axis projected into the
    screen plane. Also returns the face center (the derived camera target)
    and a fallback framing distance from the focus bounds. ``None`` when
    the target is not a derivable planar face; the caller refuses with
    ``orientation_not_derivable`` instead of capturing a guess.
    """

    from .geometry import _bbox, _face_summary, _type_name, placed_shape

    if subelement is None or not subelement.startswith("Face"):
        return None
    try:
        shape = placed_shape(focus_obj)
        face = shape.Faces[int(subelement[4:]) - 1]
    except Exception:
        return None
    if _type_name(getattr(face, "Surface", None)) != "Plane":
        return None
    summary = _face_summary(face)
    normal = summary.get("normal")
    center = summary.get("center")
    if normal is None or center is None:
        return None
    direction = _vec_normalize(_vec_negate((normal[0], normal[1], normal[2])))
    if direction is None:
        return None
    up = _projected_screen_up(direction)
    if up is None:
        return None
    fallback_distance: float | None = None
    bounds = _bbox(shape)
    if bounds is not None:
        diagonal = _vec_norm(
            (
                bounds[3] - bounds[0],
                bounds[4] - bounds[1],
                bounds[5] - bounds[2],
            )
        )
        if math.isfinite(diagonal) and diagonal > 0.0:
            fallback_distance = 1.5 * diagonal
    return direction, up, (center[0], center[1], center[2]), fallback_distance


def _new_capture_temp() -> str:
    """One temp PNG path using the module's established mkstemp pattern."""

    fd, path = tempfile.mkstemp(suffix=".png", prefix="mcp-capture-")
    os.close(fd)
    return path


def _unlink_quiet(path: str) -> None:
    """Delete a temp capture file, ignoring absence and OS errors."""
    if os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


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


def _native_subelement_label(selection: Mapping | None) -> str | None:
    """The native FaceN/EdgeN label for a resolved subshape, or None.

    Validated tokens and query results are mapped to native labels only
    here, inside the private framing path; they are never accepted as
    durable input.
    """

    if selection is None:
        return None
    return ("Face" if selection["role"] == "face" else "Edge") + str(selection["index"])


def _resolved_reference(ctx: Any, doc: Any, obj: Any, selection: Mapping | None) -> dict:
    """The canonical whole/signed target describing a resolution outcome."""

    from .geometry import make_reference, whole_reference

    if selection is None:
        return whole_reference(obj)
    return make_reference(ctx, doc, obj, selection["role"], selection["index"])


def _frame_targets(ctx: Any, targets: list[tuple[Any, str | None]]) -> None:
    """Frame the given (object, subelement) targets in the active view.

    The framing is issued twice: the first pass pumps stale frames out of
    the compositor (macOS occluded-window blank captures, Linux stale
    frames), the second pass runs synchronously right before ``saveImage``
    so the frame matches the requested framing.
    """

    for _pass in (0, 1):
        ctx.Gui.Selection.clearSelection()
        for target_obj, target_sub in targets:
            if target_sub is None:
                ctx.Gui.Selection.addSelection(target_obj)
            else:
                ctx.Gui.Selection.addSelection(target_obj, target_sub)
        ctx.Gui.SendMsgToActiveView("ViewSelection")
        if _pass == 0:
            _flush_gui_events()
    ctx.Gui.Selection.clearSelection()


def _frame_scene(ctx: Any) -> None:
    """Fit the whole visible scene (document-scope overview framing).

    Mirrors the double framing pass of ``_frame_targets``: the first
    ``ViewFit`` pumps stale frames out of the compositor, the second runs
    synchronously right before the panel capture.
    """

    ctx.Gui.SendMsgToActiveView("ViewFit")
    _flush_gui_events()
    ctx.Gui.SendMsgToActiveView("ViewFit")


def _capture_panel(
    ctx: Any,
    view: Any,
    focus_obj: Any,
    subelement: str | None,
    orientation_method: str,
    document: str,
    tmp_path: str,
    width: int,
    height: int,
    camera_hook: Any = None,
    framing: list[tuple[Any, str | None]] | None = None,
    apply_camera: Any = None,
) -> Any:
    """Capture one oriented, framed panel image to ``tmp_path``.

    Performs the active-document switch, the optional orientation change,
    the double selection-framing pass on the focus target and the
    five-argument Framebuffer ``saveImage`` with an empty-image check.
    ``orientation_method`` may be ``None`` for a panel whose camera is set
    by the caller instead of a named orientation. ``camera_hook`` runs
    after the capture (inside the panel's view state) and its return value
    is passed through, so callers can read the live camera for the panel
    manifest. ``framing`` selects the framing pass: the default frames the
    focus target through selection framing, an explicit empty list fits
    the whole visible scene instead, and a non-empty list frames exactly
    those (object, subelement) targets. ``apply_camera`` runs between
    framing and ``saveImage`` for panels whose camera is rewritten from
    the post-framing state. Session-scoped restore work (selection, active
    document, camera, animation preference) stays with the caller.
    """

    ctx.App.setActiveDocument(document)
    ctx.Gui.setActiveDocument(document)
    _flush_gui_events()

    if orientation_method is not None:
        getattr(view, orientation_method)()
        _flush_gui_events()

    if framing is None:
        framing = [(focus_obj, subelement)]
    if framing:
        _frame_targets(ctx, framing)
    else:
        _frame_scene(ctx)
    if apply_camera is not None:
        apply_camera()

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
    return camera_hook() if camera_hook is not None else None


def _capture_single_view(
    ctx: Any,
    view: Any,
    document: str,
    focus_obj: Any,
    subelement: str | None,
    view_label: str,
    orientation_method: str | None,
    derived: bool,
    size: tuple[int, int],
    apply_camera: Any = None,
) -> tuple[bytes, list[dict[str, Any]], int, int]:
    """Capture one detail panel with the resolved camera and target.

    ``size`` was resolved by the caller before the session opened, so a
    size refusal never touches view state. The manifest reports one entry
    spanning the whole image.
    """

    resolved_width, resolved_height = size
    panel_path = _new_capture_temp()
    try:
        axes = _capture_panel(
            ctx,
            view,
            focus_obj,
            subelement,
            orientation_method,
            document,
            panel_path,
            resolved_width,
            resolved_height,
            camera_hook=lambda: _camera_axes(view),
            apply_camera=apply_camera,
        )
        with open(panel_path, "rb") as handle:
            png_bytes = handle.read()
    finally:
        _unlink_quiet(panel_path)
    direction, up = axes if axes is not None else (None, None)
    fragment = {
        "view": str(view_label),
        "direction": direction,
        "up": up,
        "section_plane": None,
        "rect": {"x": 0, "y": 0, "w": resolved_width, "h": resolved_height},
        "derived": bool(derived),
    }
    return png_bytes, [fragment], resolved_width, resolved_height


def _capture_overview(
    ctx: Any,
    view: Any,
    document: str,
    doc: Any,
    focus_obj: Any,
    subelement: str | None,
    size: tuple[int, int, int, int],
    generation: int,
) -> tuple[bytes, list[dict[str, Any]], int, int, list[str] | None, bool]:
    """Labeled 4x2 sheet: the seven named orientations plus a legend cell.

    With a focus object every panel frames it through selection framing;
    without one every panel fits the visible document scene instead and
    the capture reports the visible root objects (capped at 16). Returns
    the sheet bytes, the manifest fragments (legend included), the sheet
    size and the captured-object report.
    """

    sheet_width, sheet_height, cell_width, cell_height = size
    captured_objects: list[str] | None = None
    truncated = False
    if focus_obj is None:
        captured_objects, truncated = _visible_root_objects(doc)
    framing: list[tuple[Any, str | None]] | None = None if focus_obj is not None else []
    cell_total_height = cell_height + _LABEL_STRIP
    cells: list[dict[str, Any]] = []
    panels: list[dict[str, Any]] = []
    temps: list[str] = []
    try:
        for index, (label, method) in enumerate(_OVERVIEW_VIEWS):
            cell_x = (index % 4) * cell_width
            cell_y = (index // 4) * cell_total_height
            panel_path = _new_capture_temp()
            temps.append(panel_path)
            axes = _capture_panel(
                ctx,
                view,
                focus_obj,
                subelement,
                method,
                document,
                panel_path,
                cell_width,
                cell_height,
                camera_hook=lambda: _camera_axes(view),
                framing=framing,
            )
            direction, up = axes if axes is not None else (None, None)
            panels.append(
                {
                    "view": label,
                    "direction": direction,
                    "up": up,
                    "section_plane": None,
                    "derived": False,
                    "rect": {
                        "x": cell_x,
                        "y": cell_y,
                        "w": cell_width,
                        "h": cell_total_height,
                    },
                }
            )
            cells.append(
                {
                    "label": label,
                    "path": panel_path,
                    "x": cell_x,
                    "y": cell_y,
                    "w": cell_width,
                    "h": cell_total_height,
                }
            )
        legend_x = 3 * cell_width
        legend_y = cell_total_height
        cells.append(
            {
                "label": "Legend",
                "x": legend_x,
                "y": legend_y,
                "w": cell_width,
                "h": cell_total_height,
                "legend": [
                    f"document: {document}",
                    f"generation: {generation}",
                    "axes: X/Y/Z are document axes",
                ],
            }
        )
        panels.append(
            {
                "view": "Legend",
                "direction": None,
                "up": None,
                "section_plane": None,
                "derived": False,
                "rect": {
                    "x": legend_x,
                    "y": legend_y,
                    "w": cell_width,
                    "h": cell_total_height,
                },
            }
        )
        sheet_bytes, _rects = _compose_sheet(cells)
    finally:
        for path in temps:
            _unlink_quiet(path)
    return sheet_bytes, panels, sheet_width, sheet_height, captured_objects, truncated


def _validate_section_override(section_axis: Any, section_point: Any) -> None:
    """Validate the explicit fit section override.

    Both values must be given together; the axis must be finite and
    non-zero, the point finite. Absent entirely is valid (derivation
    runs).
    """

    if section_axis is None and section_point is None:
        return
    if section_axis is None or section_point is None:
        raise ToolError(
            "VALIDATION_FAILED",
            "section_axis and section_point must be provided together",
            {"reason": "section_override_invalid"},
        )
    if not _finite_vec(section_axis) or not _finite_vec(section_point):
        raise ToolError(
            "VALIDATION_FAILED",
            "section_axis and section_point must be three finite numbers",
            {"reason": "section_override_invalid"},
        )
    if _vec_norm((float(section_axis[0]), float(section_axis[1]), float(section_axis[2]))) <= 0.0:
        raise ToolError(
            "VALIDATION_FAILED",
            "section_axis must be non-zero",
            {"reason": "section_override_invalid"},
        )


#: Three finite numbers; used for the explicit fit section override.
_VECTOR3_SCHEMA = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 3,
    "maxItems": 3,
}

#: One ``views`` manifest entry: panel identity, live camera axes, the
#: section plane when the panel is cut, the panel rect on the sheet and
#: whether its orientation was derived rather than named.
_VIEW_MANIFEST_ITEM = {
    "type": "object",
    "properties": {
        "view": {"type": "string", "minLength": 1, "maxLength": 64},
        "direction": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 3,
            "maxItems": 3,
        },
        "up": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 3,
            "maxItems": 3,
        },
        "section_plane": {
            "type": ["object", "null"],
            "properties": {
                "normal": _VECTOR3_SCHEMA,
                "point": _VECTOR3_SCHEMA,
            },
            "required": ["normal", "point"],
            "additionalProperties": False,
        },
        "rect": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "w": {"type": "integer", "minimum": 1},
                "h": {"type": "integer", "minimum": 1},
            },
            "required": ["x", "y", "w", "h"],
            "additionalProperties": False,
        },
        "derived": {"type": "boolean"},
    },
    "required": ["view", "direction", "up", "section_plane", "rect", "derived"],
    "additionalProperties": False,
}

#: Capture targets are the shared vocabulary: whole-object identity, a
#: fresh signed reference, or a declarative query resolved to exactly one
#: subshape before any GUI state changes.
_TOOL_INPUT_SCHEMA = tq.merge_query_defs(
    {
        "type": "object",
        "properties": {
            "document": {"type": "string", "minLength": 1, "maxLength": 256},
            "mode": {
                "type": "string",
                "enum": list(_CAPTURE_MODES),
                "default": "overview",
            },
            "focus": {"$ref": "#/$defs/topologyTarget"},
            "view_name": {
                "type": "string",
                "enum": sorted(_VIEW_METHODS),
            },
            "a": {"$ref": "#/$defs/topologyTarget"},
            "b": {"$ref": "#/$defs/topologyTarget"},
            "section_axis": _VECTOR3_SCHEMA,
            "section_point": _VECTOR3_SCHEMA,
            "width": {"type": "integer", "minimum": 1, "maximum": MAX_EXPLICIT_EDGE},
            "height": {"type": "integer", "minimum": 1, "maximum": MAX_EXPLICIT_EDGE},
        },
        "required": ["document"],
        "additionalProperties": False,
    }
)

#: One resolved whole/signed target, or null when the mode frames none.
_TARGET_OUT = {
    "anyOf": [
        {"type": "null"},
        {"$ref": "#/$defs/topologyWholeTarget"},
        {"$ref": "#/$defs/topologyReferenceTarget"},
    ]
}

_TOOL_OUTPUT_SCHEMA = tq.merge_query_defs(
    {
        "type": "object",
        "properties": {
            "mimeType": {"type": "string", "const": "image/png"},
            "data": {"type": "string", "minLength": 1},
            "width": {"type": "integer", "minimum": 1, "maximum": MAX_EXPLICIT_EDGE},
            "height": {"type": "integer", "minimum": 1, "maximum": MAX_EXPLICIT_EDGE},
            "document": {"type": "string", "minLength": 1, "maxLength": 256},
            "generation": {"type": "integer", "minimum": 0},
            "mode": {
                "type": "string",
                "enum": list(_CAPTURE_MODES),
            },
            "focus": _TARGET_OUT,
            "a": _TARGET_OUT,
            "b": _TARGET_OUT,
            "view_name": {
                "type": ["string", "null"],
                "minLength": 3,
                "maxLength": 12,
            },
            "views": {
                "type": "array",
                "items": _VIEW_MANIFEST_ITEM,
                "minItems": 1,
                "maxItems": 8,
            },
            "captured_objects": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 256},
                "maxItems": 16,
            },
            "truncated": {"type": "boolean"},
        },
        "required": [
            "mimeType",
            "data",
            "width",
            "height",
            "document",
            "generation",
            "mode",
            "focus",
        ],
        "additionalProperties": False,
    }
)
