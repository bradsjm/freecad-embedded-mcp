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
from collections.abc import Mapping
from typing import Any

import FreeCAD

from .. import input_aliases as _aliases
from ..gui_state import capture_selection_snapshot, restore_selection_snapshot
from ..protocol import VALIDATION_FAILED, ToolError
from .view_camera import (
    _camera_axes,
    _derived_camera_string,
    _least_parallel_basis_axis,
    _vec_normalize,
)
from .view_camera import (
    _parse_camera as _parse_camera,
)
from .view_capture import (
    _CAPTURE_MODES,
    _LABEL_STRIP,
    _TOOL_INPUT_SCHEMA,
    _TOOL_OUTPUT_SCHEMA,
    _VIEW_METHODS,
    _capture_camera,
    _capture_overview,
    _capture_panel,
    _capture_single_view,
    _compose_sheet,
    _derive_detail_orientation,
    _derive_mating_axis,
    _focus_document_bounds,
    _gui_document,
    _native_subelement_label,
    _new_capture_temp,
    _require_no_clipping_plane,
    _resolved_reference,
    _restore_camera,
    _RestoreGuard,
    _sheet_dimensions,
    _unlink_quiet,
    _validate_section_override,
    resolve_capture_size,
)
from .view_context import (
    _CONTEXT_INPUT_SCHEMA,
    _CONTEXT_OUTPUT_SCHEMA,
    inspect_user_context,
)

#: Liberal input (Postel): a model reaches for the FreeCAD camera name
#: (``rear``), the standard abbreviation (``iso``), or SolidWorks' fit
#: spelling (``zoom``); all fold onto the canonical vocabulary. The tables
#: build from the same sources as the schema enums, so an alias can never
#: normalize to a refused value. The selector grammar keeps CadQuery's own
#: case rules; liberal folding applies to parameter values only.
_MODE_ALIASES = {"zoom": "fit"}
_VIEW_NAME_ALIASES = {"iso": "Isometric", "rear": "Back"}
_MODE_TABLE = _aliases.build_table(_CAPTURE_MODES, _MODE_ALIASES)
_VIEW_NAME_TABLE = _aliases.build_table(_VIEW_METHODS, _VIEW_NAME_ALIASES)
_CAPTURE_NORMALIZER_SPEC = {"mode": _MODE_TABLE, "view_name": _VIEW_NAME_TABLE}


def _normalize_capture_arguments(arguments: dict) -> dict:
    """Fold capture mode and orientation spellings to canonical values."""

    return _aliases.normalize_arguments(arguments, _CAPTURE_NORMALIZER_SPEC)


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


# Navigation camera animation: with ``UseNavigationAnimations`` enabled
# (observed default on live FreeCAD 1.1.3, AnimationDuration 500) an
# orientation change animates and ``saveImage`` captures a stale mid-flight
# orientation (live smoke: Isometric captured as front view). The
# orientation and framing are applied with the animation preference
# temporarily disabled and the setting is restored afterwards.
_VIEW_PARAM_PATH = "User parameter:BaseApp/Preferences/View"
_DEFAULT_ANIMATION_DURATION = 500


def _disable_navigation_animations() -> dict[str, Any]:
    """Disable navigation animations and return the prior preference state."""
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
    """Restore navigation-animation preferences from a captured state."""
    params = state["params"]
    params.SetBool("UseNavigationAnimations", state["use_animations"])
    params.SetInt("AnimationDuration", state["duration"])


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
            """Rewrite the live camera to the derived detail orientation."""
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
    previous_selection = capture_selection_snapshot(ctx)

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
        guard.protect("selection", lambda: restore_selection_snapshot(ctx, previous_selection))
        if previous_active is not None:

            def restore_active_documents() -> None:
                """Re-activate the captured App and GUI active document."""
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
            """Restore the focus object's pre-x-ray transparency."""
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


TOOL_DEFINITIONS = [
    {
        "name": "capture_view",
        "normalize": _normalize_capture_arguments,
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
    },
    {
        "name": "inspect_user_context",
        "description": (
            "Observe the current user context without changing anything: "
            "active document, active and edit objects, workbench, the "
            "foreground view (type, camera, viewport) and the GUI "
            "selection as bounded explicit targets (at most 64 rows) "
            "that existing tools accept directly — whole-object targets "
            "or signed face/edge references; unsupported observations "
            "carry a diagnostic reason instead of a guessed target. "
            "include_image (default false) optionally returns one PNG of "
            "the unchanged current active 3D viewport using the "
            "four-argument saveImage form; nothing is framed, activated, "
            "or reconfigured. Unlike capture_view the scene is never "
            "reframed and no other view is activated. The tool does not "
            "infer user intent or click history, does not change "
            "selection, and selection is not authorization. Native "
            "FaceN/EdgeN names in the result are diagnostics, not "
            "durable targets. A failed native read is reported in "
            "unavailable while the verified facts stay usable."
        ),
        "inputSchema": _CONTEXT_INPUT_SCHEMA,
        "outputSchema": _CONTEXT_OUTPUT_SCHEMA,
    },
]

HANDLERS = {"capture_view": capture_view, "inspect_user_context": inspect_user_context}
