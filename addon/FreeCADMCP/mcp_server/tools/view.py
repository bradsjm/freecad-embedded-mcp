"""``capture_view`` tool: intent-driven GUI view capture as PNG.

The tool is organized around inspection intent instead of caller-chosen
camera geometry. ``mode`` selects what the caller wants to see and the
tool derives the composition, so a language model never has to name a
camera orientation or self-correct between image requests:

- ``overview`` (default) captures a labeled 4x2 sheet of the seven named
  orientations plus a legend cell. Without ``focus`` it frames the whole
  visible document scene (``ViewFit``) and reports the visible root
  objects; with ``focus`` it frames that shared target exactly. This is a
  deliberate exception to the earlier "never fitAll" stance: document-scope
  overview captures must fit the visible scene, while every other mode
  still refuses a missing focus object with ``OBJECT_NOT_FOUND``.
- ``detail`` captures one enlarged view of a focus object (or one of its
  canonical ``FaceN``/``EdgeN`` subelements). Without ``view_name`` the
  orientation is derived from a planar face (camera direction opposite the
  face normal); with ``view_name`` it is the explicit single-view override.
- ``interior`` captures a labeled 2x2 sheet: three mid-plane section views
  of the focus object via the native clipping plane plus one x-ray
  Isometric panel (``ViewObject.Transparency`` 75 for that panel only).
- ``fit`` captures a labeled 1x2 sheet of two objects: an uncut Isometric
  framing both plus one section panel whose plane contains the derived
  mating axis (coaxial cylinder pair) or the explicit section override.
- ``view_name`` is accepted for ``mode: "detail"`` only: it selects the
  explicit single-view capture instead of the derived orientation. Another
  mode carrying ``view_name`` is refused with
  ``invalid_parameter_for_mode``; ``mode`` defaults to ``overview``.

Exactly one labeled PNG is returned per call; the ``views`` manifest in
structuredContent is authoritative for panel identity, camera axes and
section planes. Numeric tools (``measure``, ``inspect_topology``) remain
the authoritative evidence; section and x-ray panels are qualitative.

Capture uses the five-argument
``saveImage(path, width, height, "White", "Framebuffer")`` call only —
there is deliberately no fallback to the legacy three-argument form, and
the white capture background keeps recognition deterministic regardless of
the user's viewport theme. The caller's selection (with subelements),
active document, camera, clipping plane, focus transparency and
navigation-animation settings are restored in ``finally``; restore
failures are collected and reported as ``restoration_failed`` instead of
being silently swallowed (the image is never returned in that case).
"""

from __future__ import annotations

import base64
import math
import os
import tempfile
from collections.abc import Mapping
from typing import Any

import FreeCAD

from .. import topology_query as tq
from ..gui_dispatch import _flush_gui_events
from ..protocol import VALIDATION_FAILED, ToolError

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

# Interior sections cut at the focus bounds mid-planes; each panel uses
# the named ortho whose camera direction is parallel to the plane normal
# (Left looks along +X, Front along +Y, Bottom along +Z; signs confirmed
# against the live native contract). The x-ray panel raises the focus
# transparency for that panel only.
_SECTION_PLANES: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("SectionX", (1.0, 0.0, 0.0)),
    ("SectionY", (0.0, 1.0, 0.0)),
    ("SectionZ", (0.0, 0.0, 1.0)),
)
_AXIS_SECTION_CAMERA = ("Left", "Front", "Bottom")
_XRAY_TRANSPARENCY = 75

# Bounded scans: visible root names reported per overview capture and
# cylindrical faces inspected per fit derivation (the cap mirrors
# geometry._MAX_FACES).
_MAX_CAPTURED_OBJECTS = 16
_MAX_FIT_FACES = 64


# ---------------------------------------------------------------------------
# Small tuple-vector helpers. Direction/up/normal math stays on plain
# floats; FreeCAD.Vector is only constructed where a native API call needs
# one (camera strings, clipping placements).
# ---------------------------------------------------------------------------


def _finite_vec(values: Any) -> bool:
    """True when ``values`` is three finite floats."""

    try:
        return len(values) == 3 and all(math.isfinite(float(value)) for value in values)
    except (TypeError, ValueError):
        return False


def _vec_negate(values: tuple[float, float, float]) -> tuple[float, float, float]:
    return (-values[0], -values[1], -values[2])


def _vec_dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vec_add(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vec_sub(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vec_scale(values: tuple[float, float, float], factor: float) -> tuple[float, float, float]:
    return (values[0] * factor, values[1] * factor, values[2] * factor)


def _vec_cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _vec_norm(values: tuple[float, float, float]) -> float:
    return math.sqrt(_vec_dot(values, values))


def _vec_normalize(values: tuple[float, float, float]) -> tuple[float, float, float] | None:
    norm = _vec_norm(values)
    if not math.isfinite(norm) or norm <= 0.0:
        return None
    return (values[0] / norm, values[1] / norm, values[2] / norm)


def _unit_axis(index: int) -> tuple[float, float, float]:
    return tuple(1.0 if position == index else 0.0 for position in range(3))  # type: ignore[return-value]


# Document basis axes in tie-break order (X before Y before Z): the least
# parallel one becomes a derived screen-up or section normal.
_BASIS_AXES = (_unit_axis(0), _unit_axis(1), _unit_axis(2))
_BASIS_NAMES = ("X", "Y", "Z")


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


def _camera_keyword_values(
    camera: str, keyword: str, count: int
) -> tuple[list[float], int, int] | None:
    """Read ``count`` numbers after ``keyword`` in a camera string.

    Handles both the native ``position 1 2 3`` form and a parenthesized
    ``position (1,2,3)`` form. Returns the values plus the exact
    character span they occupy so ``_derived_camera_string`` can splice
    replacements in place — the native ``setCamera`` rejects a rewritten
    camera whose original whitespace was not preserved, so the rest of
    the string must stay byte-identical. ``None`` when the keyword is
    absent or its value tuple is malformed.
    """

    search_from = 0
    while True:
        index = camera.find(keyword, search_from)
        if index < 0:
            return None
        # The keyword must stand alone as a word.
        before = camera[index - 1] if index > 0 else " "
        after_at = index + len(keyword)
        after = camera[after_at] if after_at < len(camera) else " "
        if not (before.isspace() or before in "{,(") or not (after.isspace() or after in "},("):
            search_from = index + 1
            continue
        cursor = after_at
        values: list[float] = []
        span_start: int | None = None
        span_end = cursor
        while len(values) < count:
            # Skip separators between values; stop at the first character
            # that cannot start a number.
            while cursor < len(camera) and (camera[cursor].isspace() or camera[cursor] in "(),"):
                cursor += 1
            run_start = cursor
            while cursor < len(camera) and (camera[cursor].isdigit() or camera[cursor] in "+-.eE"):
                cursor += 1
            if cursor == run_start:
                break
            try:
                value = float(camera[run_start:cursor])
            except ValueError:
                break
            if not math.isfinite(value):
                break
            values.append(value)
            if span_start is None:
                span_start = run_start
            span_end = cursor
        if len(values) == count:
            return values, span_start or 0, span_end
        return None


def _parse_camera(camera: str) -> dict[str, tuple[list[float], int, int]] | None:
    """Parse ``position`` and ``orientation`` (and optional
    ``focalDistance``) from a camera string; ``None`` when unparseable."""

    parsed: dict[str, tuple[list[float], int, int]] = {}
    if not camera:
        return None
    for keyword, count in (("position", 3), ("orientation", 4)):
        found = _camera_keyword_values(camera, keyword, count)
        if found is None:
            return None
        parsed[keyword] = found
    focal = _camera_keyword_values(camera, "focalDistance", 1)
    if focal is not None:
        parsed["focalDistance"] = focal
    return parsed


def _rotate_axis_angle(
    axis: tuple[float, float, float], angle: float, vec: tuple[float, float, float]
) -> tuple[float, float, float] | None:
    """Rotate ``vec`` by the axis-angle pair (Rodrigues' formula).

    Inventor camera strings write ``orientation`` as an axis-angle (unit
    axis plus radians), not as a quaternion; the axis is normalized here
    so a non-unit axis still rotates correctly.
    """

    norm = _vec_norm(axis)
    if not math.isfinite(norm) or norm <= 0.0 or not math.isfinite(angle):
        return None
    unit = (axis[0] / norm, axis[1] / norm, axis[2] / norm)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    dot = _vec_dot(unit, vec)
    cross = _vec_cross(unit, vec)
    return tuple(
        vec[index] * cosine + cross[index] * sine + unit[index] * dot * (1.0 - cosine)
        for index in range(3)
    )


def _quat_to_axis_angle(
    quat: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float], float]:
    """Axis-angle (unit axis, radians in [0, pi]) from a unit quaternion."""

    qx, qy, qz, qw = quat
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if norm <= 0.0:
        return (0.0, 0.0, 1.0), 0.0
    qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm
    angle = 2.0 * math.acos(max(-1.0, min(1.0, qw)))
    sin_half = math.sqrt(max(0.0, 1.0 - qw * qw))
    if sin_half <= 0.0:
        return (0.0, 0.0, 1.0), 0.0
    axis = (qx / sin_half, qy / sin_half, qz / sin_half)
    if angle > math.pi:
        angle = 2.0 * math.pi - angle
        axis = _vec_negate(axis)
    return axis, angle


def _camera_axes(view: Any) -> tuple[list[float], list[float]] | None:
    """Live camera ``(direction, up)`` unit vectors from ``getCamera()``.

    ``direction`` is the view direction (the camera-local -Z axis in world
    coordinates) and ``up`` the camera-local +Y axis. The camera
    ``orientation`` is read as the axis-angle pair Inventor writes.
    Returns ``None`` when the camera string is unavailable or
    unparseable; panel manifests then report null camera axes instead of
    failing the capture.
    """

    try:
        camera = str(view.getCamera())
    except Exception:
        return None
    parsed = _parse_camera(camera)
    if parsed is None:
        return None
    axis_values, angle = parsed["orientation"][0][:3], parsed["orientation"][0][3]
    direction = _rotate_axis_angle(axis_values, angle, (0.0, 0.0, -1.0))
    up = _rotate_axis_angle(axis_values, angle, (0.0, 1.0, 0.0))
    if not _finite_vec(direction) or not _finite_vec(up):
        return None
    return list(direction), list(up)


def _quat_from_basis(
    x: tuple[float, float, float],
    y: tuple[float, float, float],
    z: tuple[float, float, float],
) -> tuple[float, float, float, float]:
    """Rotation quaternion ``(x, y, z, w)`` from a unit orthogonal basis.

    The basis vectors are the camera X/Y/Z axes in world coordinates
    (matrix columns); Shepperd's method picks the numerically largest
    quaternion component.
    """

    # The basis vectors are the matrix columns (camera X/Y/Z axes in
    # world coordinates), not the rows.
    m00, m10, m20 = x
    m01, m11, m21 = y
    m02, m12, m22 = z
    trace = m00 + m11 + m22
    if trace > 0.0:
        s = 2.0 * math.sqrt(trace + 1.0)
        return (
            (m21 - m12) / s,
            (m02 - m20) / s,
            (m10 - m01) / s,
            0.25 * s,
        )
    if m00 > m11 and m00 > m22:
        s = 2.0 * math.sqrt(1.0 + m00 - m11 - m22)
        return (
            0.25 * s,
            (m01 + m10) / s,
            (m02 + m20) / s,
            (m21 - m12) / s,
        )
    if m11 > m22:
        s = 2.0 * math.sqrt(1.0 + m11 - m00 - m22)
        return (
            (m01 + m10) / s,
            0.25 * s,
            (m12 + m21) / s,
            (m02 - m20) / s,
        )
    s = 2.0 * math.sqrt(1.0 + m22 - m00 - m11)
    return (
        (m02 + m20) / s,
        (m12 + m21) / s,
        0.25 * s,
        (m10 - m01) / s,
    )


def _rotation_quat(
    x: tuple[float, float, float],
    y: tuple[float, float, float],
    z: tuple[float, float, float],
) -> tuple[float, float, float, float] | None:
    """Quaternion carrying the camera basis into world axes.

    Prefers the native ``FreeCAD.Rotation`` three-vector constructor and
    falls back to the pure-Python basis conversion so the derivation stays
    deterministic where the native constructor is unavailable or returns
    an unusable quaternion. FreeCAD's ``Rotation.Q`` order is
    ``(x, y, z, w)``.
    """

    rotation = getattr(FreeCAD, "Rotation", None)
    vector = getattr(FreeCAD, "Vector", None)
    if rotation is not None and vector is not None:
        try:
            native = rotation(vector(*x), vector(*y), vector(*z))
            qx, qy, qz, qw = (float(value) for value in native.Q)
            quat = (qx, qy, qz, qw)
            norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
            if math.isfinite(norm) and norm > 0.0:
                return quat
        except Exception:
            pass
    return _quat_from_basis(x, y, z)


def _derived_camera_string(
    camera: str,
    center: tuple[float, float, float],
    direction: tuple[float, float, float],
    up: tuple[float, float, float],
    fallback_distance: float | None = None,
) -> str | None:
    """Rewrite ``position``/``orientation`` in a post-framing camera string.

    The camera is placed at ``center - direction * distance`` looking along
    ``direction`` with screen ``up``, keeping the framing distance of the
    given camera (its parsed ``focalDistance``) or ``fallback_distance``.
    The orientation is written in the axis-angle form the Inventor camera
    format uses (a quaternion is converted, never written raw).
    Returns ``None`` when the string lacks a parseable position/orientation
    or no distance is available; the caller refuses the derivation instead
    of capturing a mis-oriented panel. All other camera fields (projection,
    aspect, near/far planes) are preserved untouched.
    """

    parsed = _parse_camera(camera)
    if parsed is None:
        return None
    distance = parsed["focalDistance"][0][0] if "focalDistance" in parsed else fallback_distance
    if distance is None or not math.isfinite(distance) or distance <= 0.0:
        return None
    z_axis = _vec_negate(direction)
    y_axis = up
    x_axis = _vec_cross(y_axis, z_axis)
    x_axis = _vec_normalize(x_axis)
    y_axis = _vec_normalize(y_axis)
    z_axis = _vec_normalize(z_axis)
    if x_axis is None or y_axis is None or z_axis is None:
        return None
    quat = _rotation_quat(x_axis, y_axis, z_axis)
    if quat is None:
        return None
    position = _vec_sub(center, _vec_scale(direction, distance))
    axis, angle = _quat_to_axis_angle(quat)
    result = camera
    # Splices are applied from the highest character offset first so the
    # earlier span stays valid; everything outside the two value runs
    # stays byte-identical, as the native setCamera requires.
    splices = sorted(
        [
            (
                parsed["position"][1],
                parsed["position"][2],
                " ".join(f"{value:.9g}" for value in position),
            ),
            (
                parsed["orientation"][1],
                parsed["orientation"][2],
                " ".join(f"{value:.9g}" for value in (*axis, angle)),
            ),
        ],
        reverse=True,
    )
    for start, end, replacement in splices:
        result = result[:start] + replacement + result[end:]
    return result


class _RestoreGuard:
    """Collect session restore failures instead of swallowing them.

    Every session-scoped restore step runs through :meth:`protect`; a
    failure is recorded as an item/error pair and the remaining restores
    still run. After the last restore, ``raise_if_any`` turns collected
    failures into one ``restoration_failed`` error, so a capture whose
    view state could not be restored never returns its image.
    """

    def __init__(self) -> None:
        self.failures: list[dict[str, str]] = []

    def protect(self, item: str, restore: Any) -> None:
        try:
            restore()
        except Exception as exc:
            self.failures.append({"item": item, "error": f"{type(exc).__name__}: {exc}"})

    def raise_if_any(self) -> None:
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


def _vector3(value: Any) -> tuple[float, float, float] | None:
    """Read a Vector-like value as three finite floats."""

    try:
        coords = (value.x, value.y, value.z)
    except AttributeError:
        return None
    if not _finite_vec(coords):
        return None
    return (float(coords[0]), float(coords[1]), float(coords[2]))


def _least_parallel_basis_axis(
    direction: tuple[float, float, float],
) -> tuple[tuple[float, float, float], int]:
    """Document basis axis least parallel to ``direction`` (tie X>Y>Z).

    Used for fit section normals (the plane then contains the direction)
    and for derived detail screen-up.
    """

    best_index = 0
    best_parallel: float | None = None
    for index, basis in enumerate(_BASIS_AXES):
        parallel = abs(_vec_dot(direction, basis))
        if best_parallel is None or parallel < best_parallel:
            best_parallel = parallel
            best_index = index
    return _BASIS_AXES[best_index], best_index


def _projected_screen_up(
    direction: tuple[float, float, float],
) -> tuple[float, float, float] | None:
    """Screen-up for a derived camera: the least parallel basis axis
    projected into the plane perpendicular to ``direction``."""

    axis, _index = _least_parallel_basis_axis(direction)
    projected = _vec_sub(axis, _vec_scale(direction, _vec_dot(axis, direction)))
    return _vec_normalize(projected)


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


def capture_view(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """``capture_view`` handler (GUI thread only).

    Order: closed routing rules and per-mode input refusals, document and
    target resolution (whole/signed/query focus and fit targets resolve
    before any GUI state change), pure derivation (detail orientation,
    interior bounds, fit mating axis), the read-only clipping-plane probe,
    and only then any view mutation. Session-scoped view state is restored
    in ``finally`` through the restore guard.
    """

    from .geometry import _resolve_target

    document = arguments["document"]
    mode = arguments.get("mode")
    if mode is None:
        # mode defaults to overview regardless of view_name; a single-view
        # capture is an explicit detail request with a view_name.
        mode = "overview"
    view_name = arguments.get("view_name")
    focus_arguments = arguments.get("focus")
    width = arguments.get("width")
    height = arguments.get("height")
    a_arguments = arguments.get("a")
    b_arguments = arguments.get("b")
    section_axis = arguments.get("section_axis")
    section_point = arguments.get("section_point")

    # Closed routing rules: view_name names one explicit single-view capture
    # and refines detail only. Another mode carrying a view_name is a
    # parameter-for-mode refusal, never an implicit mode switch.
    if view_name is not None and mode != "detail":
        raise ToolError(
            VALIDATION_FAILED,
            f"view_name applies to mode 'detail' only, not {mode!r}",
            {"reason": "invalid_parameter_for_mode", "mode": str(mode)},
        )
    if view_name is not None and view_name not in _VIEW_METHODS:
        raise ToolError(
            "UNSUPPORTED_VIEW",
            f"unsupported view '{view_name}'",
            {"views": sorted(_VIEW_METHODS)},
        )

    # Per-mode input refusals before any document or view access.
    if mode in ("detail", "interior") and focus_arguments is None:
        raise ToolError(
            VALIDATION_FAILED,
            f"mode '{mode}' requires a focus target",
            {"mode": str(mode)},
        )
    if mode == "fit" and focus_arguments is not None:
        # Fit frames the a and b targets; an accepted-but-ignored focus
        # input would still be echoed into the manifest as if it had
        # framed the panels.
        raise ToolError(
            VALIDATION_FAILED,
            "mode 'fit' frames the a and b targets; focus does not apply",
            {"mode": "fit"},
        )
    if mode == "fit":
        for selector_label, selector in (("a", a_arguments), ("b", b_arguments)):
            if not isinstance(selector, dict):
                raise ToolError(
                    VALIDATION_FAILED,
                    f"mode 'fit' requires target '{selector_label}'",
                    {"selector": selector_label},
                )
        _validate_section_override(section_axis, section_point)

    doc = ctx.require_document(document)
    ctx.check_document_idle(doc)

    # Resolve every shared target before any view, selection or active
    # document change: a missing focus must fail without reframing.
    focus_obj = None
    focus_selection: Mapping | None = None
    if focus_arguments is not None:
        focus_obj, focus_selection = _resolve_target(ctx, doc, focus_arguments, "focus")
        if mode == "interior" and focus_selection is not None:
            # Interior sections the whole focus object; a selected subshape
            # input is refused rather than silently sectioning its owner.
            raise ToolError(
                VALIDATION_FAILED,
                "mode 'interior' requires a whole-object focus target",
                {"reason": "subshape_not_allowed", "mode": "interior"},
            )
    a_obj = None
    b_obj = None
    a_selection: Mapping | None = None
    b_selection: Mapping | None = None
    if mode == "fit":
        a_obj, a_selection = _resolve_target(ctx, doc, a_arguments, "a")
        b_obj, b_selection = _resolve_target(ctx, doc, b_arguments, "b")

    gui_doc = _gui_document(ctx, document)
    view = getattr(gui_doc, "ActiveView", None)
    if view is None or not hasattr(view, "saveImage"):
        raise ToolError(
            "UNSUPPORTED_VIEW",
            f"the view of document '{document}' does not support capture",
            {"document": document},
        )

    generation = int(ctx.document_generation(doc))
    focus_native = _native_subelement_label(focus_selection)
    a_native = _native_subelement_label(a_selection)
    b_native = _native_subelement_label(b_selection)

    # Sheet sizes resolve from the fixed layout totals; single-panel
    # captures keep the viewport-derived sizing. Both resolve before the
    # session opens so a size refusal never touches view state.
    size: tuple[int, ...]
    if mode == "overview":
        size = _sheet_dimensions(4, 2, width, height)
    elif mode == "interior":
        size = _sheet_dimensions(2, 2, width, height)
    elif mode == "fit":
        size = _sheet_dimensions(2, 1, width, height)
    else:
        size = resolve_capture_size(ctx, view, document, width, height)

    # Pure derivation and the read-only clipping-plane probe run before
    # the session opens, so every refusal here leaves view state —
    # animation preference, camera, selection — untouched.
    section_center: tuple[float, float, float] | None = None
    mating_axis: tuple[float, float, float] | None = None
    mating_point: tuple[float, float, float] | None = None
    if mode == "interior":
        bounds = _focus_document_bounds(focus_obj)
        view_object = getattr(focus_obj, "ViewObject", None)
        if view_object is None or not hasattr(view_object, "Transparency"):
            raise ToolError(
                VALIDATION_FAILED,
                "interior target cannot render an x-ray panel",
                {
                    "reason": "interior_target_shapeless",
                    "object": str(getattr(focus_obj, "Name", "")),
                },
            )
        section_center = (
            (bounds[0] + bounds[3]) / 2.0,
            (bounds[1] + bounds[4]) / 2.0,
            (bounds[2] + bounds[5]) / 2.0,
        )
        _require_no_clipping_plane(view)
    elif mode == "fit":
        if section_axis is not None:
            mating_axis = _vec_normalize(
                (
                    float(section_axis[0]),
                    float(section_axis[1]),
                    float(section_axis[2]),
                )
            )
            mating_point = (
                float(section_point[0]),
                float(section_point[1]),
                float(section_point[2]),
            )
        else:
            mating_axis, mating_point = _derive_mating_axis(a_obj, b_obj, a_native, b_native)
        _require_no_clipping_plane(view)

    # Session state the mode may set: the clipping plane this call
    # enabled and the focus transparency this call changed. Both are
    # restored in the session finally; their failures feed the guard.
    session: dict[str, Any] = {"clip_enabled": False, "transparency_restore": None}
    captured_objects: list[str] | None = None
    truncated = False
    apply_camera: Any = None

    if mode == "detail" and view_name is None:
        derivation = _derive_detail_orientation(focus_obj, focus_native)
        if derivation is None:
            raise ToolError(
                VALIDATION_FAILED,
                "the orientation for this detail target could not be derived; "
                "pass view_name explicitly",
                {"reason": "orientation_not_derivable", "suggestions": ["view_name"]},
            )
        direction, up, target_center, fallback_distance = derivation

        def apply_camera() -> None:
            try:
                current = str(view.getCamera())
            except Exception:
                current = None
            derived_string = (
                _derived_camera_string(current, target_center, direction, up, fallback_distance)
                if current is not None
                else None
            )
            if derived_string is None:
                raise ToolError(
                    VALIDATION_FAILED,
                    "the orientation for this detail target could not be derived; "
                    "pass view_name explicitly",
                    {"reason": "orientation_not_derivable", "suggestions": ["view_name"]},
                )
            view.setCamera(derived_string)

    previous_active: str | None = None
    active_doc = ctx.App.ActiveDocument
    if active_doc is not None:
        previous_active = str(active_doc.Name)
    previous_selection = _capture_selection_snapshot(ctx)

    # Navigation animations are disabled around the orientation changes
    # and framing so saveImage never captures a mid-animation stale camera
    # orientation; the camera is snapshotted once and restored once.
    animation_state = _disable_navigation_animations()
    camera = _capture_camera(view)

    completed = False
    try:
        try:
            if mode == "detail":
                if view_name is not None:
                    png_bytes, views_manifest, resolved_width, resolved_height = (
                        _capture_single_view(
                            ctx,
                            view,
                            document,
                            focus_obj,
                            focus_native,
                            str(view_name),
                            _VIEW_METHODS[str(view_name)],
                            False,
                            size,
                        )
                    )
                else:
                    png_bytes, views_manifest, resolved_width, resolved_height = (
                        _capture_single_view(
                            ctx,
                            view,
                            document,
                            focus_obj,
                            focus_native,
                            "Detail",
                            None,
                            True,
                            size,
                            apply_camera=apply_camera,
                        )
                    )
            elif mode == "overview":
                (
                    png_bytes,
                    views_manifest,
                    resolved_width,
                    resolved_height,
                    captured_objects,
                    truncated,
                ) = _capture_overview(
                    ctx, view, document, doc, focus_obj, focus_native, size, generation
                )
            elif mode == "interior":
                png_bytes, views_manifest, resolved_width, resolved_height = _capture_interior(
                    ctx, view, document, focus_obj, section_center, size, session
                )
            else:
                png_bytes, views_manifest, resolved_width, resolved_height = _capture_fit(
                    ctx,
                    view,
                    document,
                    a_obj,
                    b_obj,
                    mating_axis,
                    mating_point,
                    size,
                    session,
                )
            completed = True
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(
                "GUI_DISPATCH_FAILED",
                f"view capture failed: {type(exc).__name__}: {exc}",
                {"document": str(document)},
            ) from exc
    finally:
        guard = _RestoreGuard()
        guard.protect("selection", lambda: _restore_selection_snapshot(ctx, previous_selection))
        if previous_active is not None:

            def restore_active_documents() -> None:
                ctx.App.setActiveDocument(previous_active)
                ctx.Gui.setActiveDocument(previous_active)

            guard.protect("active document", restore_active_documents)
        guard.protect("camera", lambda: _restore_camera(view, camera))
        guard.protect(
            "navigation animation",
            lambda: _restore_navigation_animations(animation_state),
        )
        if session["clip_enabled"]:
            guard.protect("clipping plane", lambda: view.toggleClippingPlane(0))
        if session["transparency_restore"] is not None:
            guard.protect("transparency", session["transparency_restore"])
        if completed:
            # A successful capture whose view state could not be restored
            # never returns its image.
            guard.raise_if_any()

    result: dict[str, Any] = {
        "mimeType": "image/png",
        "data": base64.b64encode(png_bytes).decode("ascii"),
        "width": resolved_width,
        "height": resolved_height,
        "document": str(document),
        "generation": generation,
        "mode": mode,
        "focus": (
            _resolved_reference(ctx, doc, focus_obj, focus_selection)
            if focus_obj is not None
            else None
        ),
        "view_name": str(view_name) if view_name is not None else None,
        "views": views_manifest,
    }
    if mode == "fit":
        result["a"] = _resolved_reference(ctx, doc, a_obj, a_selection)
        result["b"] = _resolved_reference(ctx, doc, b_obj, b_selection)
    if mode == "overview" and focus_obj is None:
        result["captured_objects"] = captured_objects
        result["truncated"] = truncated
    return result


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
    """One single-panel capture (legacy single view or detail mode).

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


def _capture_interior(
    ctx: Any,
    view: Any,
    document: str,
    focus_obj: Any,
    section_center: tuple[float, float, float],
    size: tuple[int, int, int, int],
    session: dict[str, Any],
) -> tuple[bytes, list[dict[str, Any]], int, int]:
    """Labeled 2x2 sheet: three mid-plane sections plus one x-ray panel.

    The three section panels drive the native clipping plane (enabled per
    section, disabled once afterwards); the x-ray panel raises the focus
    transparency for that panel only. Both session flags are cleared on
    the happy path and left set for the session finally when an exception
    unwinds the capture, so restore work is never skipped. Bounds
    derivation, the x-ray capability check and the clipping-plane probe
    ran in the handler before the session opened; ``section_center`` is
    the precomputed mid-point.
    """

    sheet_width, sheet_height, cell_width, cell_height = size
    center = section_center
    cell_total_height = cell_height + _LABEL_STRIP
    cells: list[dict[str, Any]] = []
    panels: list[dict[str, Any]] = []
    temps: list[str] = []
    try:
        for index, (label, normal) in enumerate(_SECTION_PLANES):
            # The clipping plane's normal is the placement's local Z, so
            # the placement rotation carries +Z onto the section axis.
            placement = FreeCAD.Placement(
                FreeCAD.Vector(center[0], center[1], center[2]),
                FreeCAD.Rotation(
                    FreeCAD.Vector(0.0, 0.0, 1.0),
                    FreeCAD.Vector(normal[0], normal[1], normal[2]),
                ),
            )
            view.toggleClippingPlane(1, False, True, placement)
            session["clip_enabled"] = True
            panel_path = _new_capture_temp()
            temps.append(panel_path)
            axes = _capture_panel(
                ctx,
                view,
                focus_obj,
                None,
                _VIEW_METHODS[_AXIS_SECTION_CAMERA[index]],
                document,
                panel_path,
                cell_width,
                cell_height,
                camera_hook=lambda: _camera_axes(view),
            )
            direction, up = axes if axes is not None else (None, None)
            cell_x = (index % 2) * cell_width
            cell_y = (index // 2) * cell_total_height
            panels.append(
                {
                    "view": label,
                    "direction": direction,
                    "up": up,
                    "section_plane": {"normal": list(normal), "point": list(center)},
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
        view.toggleClippingPlane(0)
        session["clip_enabled"] = False
        # The x-ray capability check ran in the handler before the session
        # opened; the attribute is guaranteed present here.
        view_object = focus_obj.ViewObject
        previous_transparency = view_object.Transparency

        def restore_transparency() -> None:
            view_object.Transparency = previous_transparency

        session["transparency_restore"] = restore_transparency
        view_object.Transparency = _XRAY_TRANSPARENCY
        panel_path = _new_capture_temp()
        temps.append(panel_path)
        axes = _capture_panel(
            ctx,
            view,
            focus_obj,
            None,
            _VIEW_METHODS["Isometric"],
            document,
            panel_path,
            cell_width,
            cell_height,
            camera_hook=lambda: _camera_axes(view),
        )
        view_object.Transparency = previous_transparency
        session["transparency_restore"] = None
        direction, up = axes if axes is not None else (None, None)
        panels.append(
            {
                "view": "Xray-Isometric",
                "direction": direction,
                "up": up,
                "section_plane": None,
                "derived": False,
                "rect": {
                    "x": cell_width,
                    "y": cell_total_height,
                    "w": cell_width,
                    "h": cell_total_height,
                },
            }
        )
        cells.append(
            {
                "label": "Xray-Isometric",
                "path": panel_path,
                "x": cell_width,
                "y": cell_total_height,
                "w": cell_width,
                "h": cell_total_height,
            }
        )
        sheet_bytes, _rects = _compose_sheet(cells)
    finally:
        for path in temps:
            _unlink_quiet(path)
    return sheet_bytes, panels, sheet_width, sheet_height


def _capture_fit(
    ctx: Any,
    view: Any,
    document: str,
    a_obj: Any,
    b_obj: Any,
    mating_axis: tuple[float, float, float],
    mating_point: tuple[float, float, float],
    size: tuple[int, int, int, int],
    session: dict[str, Any],
) -> tuple[bytes, list[dict[str, Any]], int, int]:
    """Labeled 1x2 sheet: uncut Isometric plus the mating-axis section.

    The first panel frames both objects uncut; the second cuts a plane
    that contains the mating axis through the interface midpoint. The
    axis already resolved in the handler (explicit override or coaxial
    cylinder derivation) before the session opened.
    """

    normal, normal_index = _least_parallel_basis_axis(mating_axis)
    sheet_width, sheet_height, cell_width, cell_height = size
    cell_total_height = cell_height + _LABEL_STRIP
    cells: list[dict[str, Any]] = []
    panels: list[dict[str, Any]] = []
    temps: list[str] = []
    try:
        panel_path = _new_capture_temp()
        temps.append(panel_path)
        axes = _capture_panel(
            ctx,
            view,
            None,
            None,
            _VIEW_METHODS["Isometric"],
            document,
            panel_path,
            cell_width,
            cell_height,
            camera_hook=lambda: _camera_axes(view),
            framing=[(a_obj, None), (b_obj, None)],
        )
        direction, up = axes if axes is not None else (None, None)
        panels.append(
            {
                "view": "Isometric",
                "direction": direction,
                "up": up,
                "section_plane": None,
                "derived": False,
                "rect": {"x": 0, "y": 0, "w": cell_width, "h": cell_total_height},
            }
        )
        cells.append(
            {
                "label": "Isometric",
                "path": panel_path,
                "x": 0,
                "y": 0,
                "w": cell_width,
                "h": cell_total_height,
            }
        )
        # The clipping plane's normal is the placement's local Z, so the
        # placement rotation carries +Z onto the section normal; the plane
        # then contains the mating axis through the interface midpoint.
        placement = FreeCAD.Placement(
            FreeCAD.Vector(mating_point[0], mating_point[1], mating_point[2]),
            FreeCAD.Rotation(
                FreeCAD.Vector(0.0, 0.0, 1.0),
                FreeCAD.Vector(normal[0], normal[1], normal[2]),
            ),
        )
        view.toggleClippingPlane(1, False, True, placement)
        session["clip_enabled"] = True
        panel_path = _new_capture_temp()
        temps.append(panel_path)
        axes = _capture_panel(
            ctx,
            view,
            None,
            None,
            _VIEW_METHODS[_AXIS_SECTION_CAMERA[normal_index]],
            document,
            panel_path,
            cell_width,
            cell_height,
            camera_hook=lambda: _camera_axes(view),
            framing=[(a_obj, None), (b_obj, None)],
        )
        view.toggleClippingPlane(0)
        session["clip_enabled"] = False
        direction, up = axes if axes is not None else (None, None)
        panels.append(
            {
                "view": "MatingSection",
                "direction": direction,
                "up": up,
                "section_plane": {
                    "normal": list(normal),
                    "point": list(mating_point),
                },
                "derived": False,
                "rect": {
                    "x": cell_width,
                    "y": 0,
                    "w": cell_width,
                    "h": cell_total_height,
                },
            }
        )
        cells.append(
            {
                "label": "MatingSection",
                "path": panel_path,
                "x": cell_width,
                "y": 0,
                "w": cell_width,
                "h": cell_total_height,
            }
        )
        sheet_bytes, _rects = _compose_sheet(cells)
    finally:
        for path in temps:
            _unlink_quiet(path)
    return sheet_bytes, panels, sheet_width, sheet_height


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
                "enum": ["overview", "detail", "interior", "fit"],
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
                "enum": ["overview", "detail", "interior", "fit"],
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

TOOL_DEFINITIONS = [
    {
        "name": "capture_view",
        "description": (
            "Capture exactly one labeled PNG per call for qualitative "
            "visual inspection, chosen by inspection intent instead of "
            "camera names. mode 'overview' (default) captures a labeled "
            "sheet of the seven named orientations plus a legend; without "
            "focus it frames the whole visible document and reports "
            "captured_objects, with focus it frames that shared target "
            "(whole object, signed reference or a query resolved to one "
            "subshape). mode 'detail' captures one enlarged view of the "
            "focus target; without view_name the orientation derives from "
            "a planar face target, with view_name it is the explicit "
            "single-view override (view_name is accepted for detail "
            "only). mode 'interior' captures mid-plane sections through a "
            "whole-object focus plus an x-ray panel; a subshape focus is "
            "refused. mode 'fit' captures two shared targets uncut plus a "
            "section through their derived mating axis (coaxial "
            "cylindrical faces, narrowable with signed or query targets) "
            "or the explicit section_axis/section_point; fit refuses "
            "focus. Sheet panels scale to explicit width/height with the "
            "label strips constant; single-panel captures honor explicit "
            "sizes and otherwise follow the active viewport. Section and "
            "x-ray panels are qualitative: occlusion and cut-surface "
            "appearance are not guaranteed; measure and inspect_topology "
            "remain the authoritative evidence. structuredContent reports "
            "the mode, resolved focus/a/b targets, document generation, "
            "per-panel camera axes, section planes and rects, and image "
            "dimensions. The caller's camera, selection, active document, "
            "clipping plane, focus transparency and navigation-animation "
            "preference are restored; a restore failure discards the "
            "image."
        ),
        "inputSchema": _TOOL_INPUT_SCHEMA,
        "outputSchema": _TOOL_OUTPUT_SCHEMA,
    }
]

HANDLERS = {"capture_view": capture_view}
