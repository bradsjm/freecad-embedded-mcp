"""Opaque material-change snapshots for write tools (B2 core slice).

A write tool that wants spatial evidence without an image captures a
baseline on the GUI thread before its transaction opens, then reports the
post-commit deltas. :func:`capture_baseline` stores an independent copy of
the target's document-space shape. :func:`report_material_change` measures
``added = after.cut(before)`` and ``removed = before.cut(after)`` and
returns one schema-shaped row. :func:`report_material_changes` processes
pairs in request order under the target cap.

Nothing here raises. An unusable target, a failed snapshot, a failed
boolean or an unreadable measurement becomes a named ``reason`` token, so a
committed write is never reported as a failed write. This module defines no
tool: it has no ``TOOL_DEFINITIONS`` or ``HANDLERS`` and is imported by the
write tools that own the wire surface.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ..protocol import check_schema

# At most eight pairs are measured per operation; excess pairs report
# skipped/cost_limit without touching their objects.
MAX_MATERIAL_TARGETS = 8

# At most 256 faces are admitted per boolean operand, shared by the baseline
# and the after-shape of one pair.
MAX_OPERAND_FACES = 256

# One directional material delta: measured volume in mm3 plus document-space
# bounds, or null bounds when the boolean result is empty or unreadable.
_MATERIAL_DELTA_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["volume", "bounds"],
    "properties": {
        "volume": {"type": "number", "minimum": 0},
        "bounds": {
            "type": ["array", "null"],
            "items": {"type": "number"},
            "minItems": 6,
            "maxItems": 6,
        },
    },
}

# The ``materialChanges`` outputSchema fragment the write tools will embed.
MATERIAL_CHANGES_SCHEMA = {
    "type": "array",
    "maxItems": 32,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["object", "status", "reason", "added", "removed"],
        "properties": {
            "object": {"type": "string"},
            "status": {"type": "string", "enum": ["ok", "skipped", "unavailable"]},
            "reason": {
                "type": ["string", "null"],
                "enum": [
                    None,
                    "cost_limit",
                    "non_volumetric_target",
                    "snapshot_failed",
                    "boolean_failed",
                    "measurement_unavailable",
                ],
            },
            "added": _MATERIAL_DELTA_SCHEMA,
            "removed": _MATERIAL_DELTA_SCHEMA,
        },
    },
}

check_schema(MATERIAL_CHANGES_SCHEMA)


def _geometry_module() -> Any:
    """Import the sibling geometry module lazily to stay import-order independent."""
    from . import geometry

    return geometry


def _baseline_record(shape: Any, faces: int, reason: str | None, *, empty: bool = False) -> dict:
    """Build the opaque record returned by :func:`capture_baseline`."""
    return {"shape": shape, "faces": faces, "reason": reason, "empty": empty}


def _delta(volume: float, bounds: list[float] | None) -> dict:
    """Build one schema-shaped material delta."""
    return {"volume": volume, "bounds": bounds}


def _row(
    name: str,
    status: str,
    reason: str | None,
    added: dict | None = None,
    removed: dict | None = None,
) -> dict:
    """Build one schema-shaped ``materialChanges`` row."""
    return {
        "object": name,
        "status": status,
        "reason": reason,
        "added": added,
        "removed": removed,
    }


def _read_shape(obj: Any) -> tuple[Any, str | None]:
    """Return ``(shape, None)``, or ``(None, reason)`` for an unusable target.

    A missing or null shape and a shape without solids are
    ``non_volumetric_target``; an accessor that raises is ``snapshot_failed``.
    """
    try:
        # Read the accessor once so one failure mode is reported.
        shape = getattr(obj, "Shape", None)
        # A null shape carries no geometry, so no volume can be measured.
        null = bool(shape.isNull()) if shape is not None else False
        # A shapeless target is valid data, not a failed snapshot.
        solid_count = len(shape.Solids) if shape is not None else 0
    except Exception:
        return None, "snapshot_failed"
    if shape is None or null:
        return None, "non_volumetric_target"
    if solid_count < 1:
        return None, "non_volumetric_target"
    return shape, None


def _face_count(shape: Any) -> int | None:
    """Return the shape's face count, or None when the list is unreadable."""
    try:
        return len(shape.Faces)
    except Exception:
        return None


def _measure_whole(shape: Any) -> tuple[float, list[float] | None] | None:
    """Measure a whole placed shape, or None when the measurement is unusable.

    The create case has no before-shape to subtract, so its ``added`` delta is
    the whole new shape. The finite-volume, bounds and inverted-box rules come
    from :mod:`geometry` so a create and the ``measure`` tool agree.
    """
    geometry = _geometry_module()
    # A finite, non-negative solid volume is the only usable measurement.
    try:
        volume = geometry._finite(getattr(shape, "Volume", None))
    except Exception:
        return None
    if volume is None or volume < 0.0:
        return None
    # An inverted box is the empty-result signal and reports as null bounds.
    bounds = geometry._bbox(shape)
    return volume, None if geometry._inverted_bounds(bounds) else bounds


def _measure_cut(
    a_shape: Any, b_shape: Any
) -> tuple[tuple[float, list[float] | None] | None, str | None]:
    """Measure ``a_shape.cut(b_shape)``, mapping every failure to a reason token."""
    geometry = _geometry_module()
    # The payload carries the shared result fields from _measure_difference.
    payload: dict[str, Any] = {}
    try:
        # geometry._measure_difference owns the empty-result and inverted-box
        # rules that ``measure`` mode "difference" also reports.
        geometry._measure_difference(a_shape, b_shape, payload)
    except Exception as exc:
        # Only a measurement-tagged ToolError is a measurement failure.
        details = getattr(exc, "details", None)
        # A cut that ran but whose volume could not be read is a measurement
        # failure; every other failure is the boolean itself.
        measurement = (
            isinstance(details, Mapping) and details.get("reason") == "measurement_unavailable"
        )
        return None, "measurement_unavailable" if measurement else "boolean_failed"
    # Defense in depth: the shared helper already returns a finite volume.
    volume = payload.get("difference_volume")
    if not isinstance(volume, (int, float)) or isinstance(volume, bool):
        return None, "measurement_unavailable"
    measured = float(volume)
    # A negative or non-finite volume fails the schema's minimum.
    if not math.isfinite(measured) or measured < 0.0:
        return None, "measurement_unavailable"
    return (measured, payload.get("bounds")), None


def capture_baseline(obj: Any, *, empty: bool = False) -> dict:
    """Capture an opaque before-state record for one pair; never raises.

    ``empty=True`` records an empty baseline for a create, so the whole new
    shape becomes ``added``. Otherwise the record holds an independent copy of
    ``obj``'s shape in document space, taken before the transaction opens.
    Failures are recorded as ``reason`` tokens: ``non_volumetric_target`` for a
    shapeless or solid-free target, ``snapshot_failed`` for an unreadable shape
    or placement, and ``cost_limit`` for more than ``MAX_OPERAND_FACES`` faces.
    """
    try:
        # A create has no before-state, so its object is never inspected.
        if empty:
            return _baseline_record(None, 0, None, empty=True)
        # The before-shape must carry solid volume before any copy is taken.
        shape, reason = _read_shape(obj)
        if reason is not None:
            return _baseline_record(None, 0, reason)
        # The face count is admitted work, so it is bounded before the copy.
        faces = _face_count(shape)
        if faces is None:
            return _baseline_record(None, 0, "snapshot_failed")
        if faces > MAX_OPERAND_FACES:
            return _baseline_record(None, faces, "cost_limit")
        # placed_shape copies the shape and applies the global placement once.
        placed = _geometry_module().placed_shape(obj)
        return _baseline_record(placed, faces, None)
    except Exception:
        # Snapshot failures are data, never a failed write.
        return _baseline_record(None, 0, "snapshot_failed")


def capture_material_baselines(
    ctx: Any,
    doc: Any,
    arguments: Mapping[str, Any],
    targets: list[tuple[str, Any]],
    *,
    empty_when_solidless: bool = False,
) -> list[tuple[str, dict, Any]] | None:
    """Capture one baseline per target before the transaction opens.

    Returns ``None`` without any snapshot or boolean work unless the request
    sets ``include_material_change``. Otherwise the document is checked idle
    (a busy document refuses before any snapshot) and each target gets a
    baseline in request order. Targets past ``MAX_MATERIAL_TARGETS`` get an
    explicit ``cost_limit`` record, so the report skips them without shape
    work. ``empty_when_solidless`` gives a readable target that holds no solid
    yet (a Body's first feature) the empty baseline, so its whole new shape
    reports as added; an unreadable shape keeps the ``snapshot_failed`` record
    of :func:`capture_baseline` instead.
    """
    if arguments.get("include_material_change") is not True:
        return None
    ctx.check_document_idle(doc)
    # The (name, baseline, object) triples the report consumes in order.
    pairs: list[tuple[str, dict, Any]] = []
    for index, (name, obj) in enumerate(targets):
        if index >= MAX_MATERIAL_TARGETS:
            pairs.append((name, _baseline_record(None, 0, "cost_limit"), obj))
            continue
        # Only a readable shape without a solid has no before-state to subtract.
        empty = empty_when_solidless and _read_shape(obj)[1] == "non_volumetric_target"
        pairs.append((name, capture_baseline(obj, empty=empty), obj))
    return pairs


def _report_material_change(name: str, baseline: dict, obj: Any) -> dict:
    """Build one row, assuming :func:`report_material_change` guards the calls."""
    # A record from another source cannot be measured.
    if not isinstance(baseline, Mapping):
        return _row(name, "unavailable", "snapshot_failed")
    # A baseline that already recorded a refusal is reported as skipped.
    reason = baseline.get("reason")
    if reason is not None:
        return _row(name, "skipped", reason)
    # Empty records carry a face count; edits must also carry a shape.
    empty = baseline.get("empty") is True
    faces = baseline.get("faces")
    before = baseline.get("shape")
    if not isinstance(faces, int) or isinstance(faces, bool) or faces < 0:
        return _row(name, "unavailable", "snapshot_failed")
    if not empty and before is None:
        return _row(name, "unavailable", "snapshot_failed")
    # The after-shape must still be solid volume before it is measured.
    after_shape, after_reason = _read_shape(obj)
    if after_reason is not None:
        status = "skipped" if after_reason == "non_volumetric_target" else "unavailable"
        return _row(name, status, after_reason)
    after_faces = _face_count(after_shape)
    if after_faces is None:
        return _row(name, "unavailable", "snapshot_failed")
    # Both operands of each cut share the per-pair face cap.
    if faces + after_faces > MAX_OPERAND_FACES:
        return _row(name, "skipped", "cost_limit")
    try:
        after = _geometry_module().placed_shape(obj)
    except Exception:
        return _row(name, "unavailable", "snapshot_failed")
    # A create removed nothing, so the whole placed shape was added.
    if empty:
        measured = _measure_whole(after)
        if measured is None:
            return _row(name, "unavailable", "measurement_unavailable")
        added_volume, added_bounds = measured
        return _row(
            name,
            "ok",
            None,
            _delta(added_volume, added_bounds),
            _delta(0.0, None),
        )
    # added = after.cut(before); removed = before.cut(after).
    added, added_reason = _measure_cut(after, before)
    if added_reason is not None:
        return _row(name, "unavailable", added_reason)
    removed, removed_reason = _measure_cut(before, after)
    if removed_reason is not None:
        return _row(name, "unavailable", removed_reason)
    return _row(name, "ok", None, _delta(*added), _delta(*removed))


def report_material_change(name: str, baseline: dict, obj: Any) -> dict:
    """Report one post-commit material-change row; never raises.

    The row is ``ok`` with both deltas, ``skipped`` with the baseline's
    recorded reason or ``cost_limit``, or ``unavailable`` with
    ``snapshot_failed``, ``boolean_failed`` or ``measurement_unavailable``.
    Deltas are null on every non-``ok`` row.
    """
    try:
        return _report_material_change(name, baseline, obj)
    except Exception:
        # An unexpected failure still answers with a schema-valid row.
        return _row(name, "unavailable", "measurement_unavailable")


def report_material_changes(pairs: list[tuple[str, dict, Any]]) -> list[dict]:
    """Report rows for ``(name, baseline, obj)`` pairs in request order.

    Pairs past ``MAX_MATERIAL_TARGETS`` report ``skipped``/``cost_limit``
    without any shape work.
    """
    # Rows keep request order, including the pairs refused by the cap.
    rows: list[dict] = []
    for index, (name, baseline, obj) in enumerate(pairs):
        if index >= MAX_MATERIAL_TARGETS:
            rows.append(_row(name, "skipped", "cost_limit"))
            continue
        rows.append(report_material_change(name, baseline, obj))
    return rows
