"""Mutation gate and post-recompute validity checks for MCP v2 tools.

The validity logic is carried over verbatim from
``rpc_server/object_validation.py`` (issue #110 semantics: Invalid/Error/
Touched states are rejected even when ``isValid()`` reports true, and
shapeless objects stay valid). On top of it this module provides:

- ``geometry_report``: a finite, JSON-safe shape report with the shared
  one-solid and positive-volume contract.
- ``mutation``: the transaction contextmanager every mutating tool enters.
  It never nests inside a user transaction, opens its own transaction,
  recomputes, validates the affected objects *and* their recomputed
  dependents — dependents already valid before the mutation keep their
  observed solid count as the solid contract, new or previously
  invalid/shapeless dependents use the default one-solid contract — and
  commits on success, aborting with an explicit rollback diagnostic on
  any failure.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from typing import Any, Callable, Iterable

from .protocol import VALIDATION_FAILED, ToolError

_FAILED_STATES = {"invalid", "error", "touched"}

# Bounds that keep the gate cheap and its diagnostics JSON-safe.
_MAX_DEPENDENTS = 256
_MAX_DIAGNOSTICS = 16


# ---------------------------------------------------------------------------
# Validity (legacy logic, unchanged).
# ---------------------------------------------------------------------------


def _object_states(obj: Any) -> list[str]:
    """Return FreeCAD's state labels without assuming a concrete container."""

    try:
        raw_state = obj.State
    except Exception:
        return []

    if isinstance(raw_state, str):
        return [raw_state]

    try:
        return [str(item) for item in raw_state]
    except Exception:
        return []


def object_validity_error(obj: Any) -> str | None:
    """Return a diagnostic when ``obj`` is invalid, otherwise ``None``.

    Shape presence is deliberately not used as the discriminator. Containers,
    groups, spreadsheets, and empty sketches can all be valid without a shape.
    """

    name = str(getattr(obj, "Name", "<unknown>"))
    states = _object_states(obj)
    failed_states = [
        state for state in states if state.strip().casefold() in _FAILED_STATES
    ]
    is_valid = getattr(obj, "isValid", None)

    if callable(is_valid):
        try:
            is_valid_result = bool(is_valid())
        except Exception as exc:
            return (
                f"Object '{name}' exists, but its validity could not be checked after "
                f"recompute: {type(exc).__name__}: {exc}. Fix or remove the object "
                "before building on it."
            )
    else:
        is_valid_result = True

    # FreeCAD's isValid() only reflects the Error bit. A feature can still be
    # incomplete after recompute while its State reports Touched, so retain the
    # explicit state check requested by issue #110.
    if is_valid_result and not failed_states:
        return None

    get_status = getattr(obj, "getStatusString", None)
    try:
        reason = str(get_status()).strip() if callable(get_status) else ""
    except Exception:
        reason = ""

    state = ", ".join(states) if states else "unknown"

    detail = f": {reason}" if reason else ""
    return (
        f"Object '{name}' exists but failed to compute{detail} "
        f"(State: {state}). Fix the cause or remove the object before building "
        "on it."
    )


# ---------------------------------------------------------------------------
# Shape reporting.
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _shape_bounds(shape: Any) -> list[float] | None:
    try:
        box = shape.BoundBox
        coordinates = [
            box.XMin,
            box.YMin,
            box.ZMin,
            box.XMax,
            box.YMax,
            box.ZMax,
        ]
    except Exception:
        return None
    finite = [_finite(coordinate) for coordinate in coordinates]
    if any(coordinate is None for coordinate in finite):
        return None
    return [float(coordinate) for coordinate in finite]  # type: ignore[arg-type]


def _shape_diagnostics(shape: Any) -> list[str]:
    try:
        found = shape.check()
    except Exception:
        return []
    if not found:
        return []
    if isinstance(found, str):
        found = [found]
    return [str(item) for item in found][:_MAX_DIAGNOSTICS]


def _solid_count(shape: Any) -> int | None:
    try:
        solids = shape.Solids
    except Exception:
        return None
    try:
        return len(solids)
    except Exception:
        return None


def geometry_report(obj: Any, expected_solids: int | None = None) -> dict:
    """Return a JSON-safe shape report for ``obj``; this never raises.

    The report carries the object-level validity verdict plus, when a Shape
    is present, its validity, solid count, volume, bounds, ``check()``
    diagnostics and maximum tolerance. Unavailable or non-finite values are
    reported as ``None`` instead of failing the report.

    The solid contract: an explicit ``expected_solids`` requires the shape to
    have exactly that many solids. The default wants exactly one solid *only*
    for shapes that actually contain solids; zero-solid shapes (wires, empty
    bodies) and shapeless objects (groups, sketches, spreadsheets, FEM
    containers) are valid without one. A shape that contains solids must
    also enclose a positive volume: zero, negative, or non-finite volume is
    degenerate and fails the report.
    """

    name = str(getattr(obj, "Name", "<unknown>"))
    validity = object_validity_error(obj)
    report: dict = {
        "name": name,
        "state": _object_states(obj),
        "object_valid": validity is None,
        "shape_valid": None,
        "solid_count": None,
        "volume": None,
        "bounds": None,
        "diagnostics": [],
        "max_tolerance": None,
        "ok": False,
        "error": validity,
    }
    if validity is not None:
        return report

    try:
        shape = obj.Shape
    except Exception:
        shape = None
    if shape is None:
        # Shapeless objects are valid without a solid contract; an explicit
        # nonzero expected_solids cannot be satisfied without a shape.
        if expected_solids is not None and expected_solids > 0:
            report["error"] = (
                f"Object '{name}' has no shape; expected_solids="
                f"{expected_solids} cannot be satisfied."
            )
            return report
        report["ok"] = True
        report["error"] = None
        return report

    try:
        report["shape_valid"] = bool(shape.isValid())
    except Exception:
        report["shape_valid"] = None
    report["solid_count"] = _solid_count(shape)
    report["volume"] = _finite(getattr(shape, "Volume", None))
    report["bounds"] = _shape_bounds(shape)
    report["diagnostics"] = _shape_diagnostics(shape)
    try:
        report["max_tolerance"] = _finite(shape.getTolerance(1))
    except Exception:
        report["max_tolerance"] = None

    error: str | None = None
    if report["shape_valid"] is False:
        error = f"Object '{name}' has an invalid shape."
    elif report["solid_count"] is not None and report["solid_count"] > 0:
        # Solid-bearing shapes must enclose positive volume; zero, negative,
        # or non-finite volume is degenerate. Zero-solid and shapeless
        # objects stay valid without a volume.
        volume = report["volume"]
        if volume is None or volume <= 0:
            shown = "no finite" if volume is None else repr(volume)
            error = (
                f"Object '{name}' produces {report['solid_count']} solids "
                f"but reports {shown} volume; solid geometry must enclose "
                "positive volume."
            )
    if error is None:
        if expected_solids is not None:
            count = report["solid_count"]
            if count is None:
                error = (
                    f"Object '{name}' did not report a solid count; "
                    f"expected_solids={expected_solids} cannot be verified."
                )
            elif count != expected_solids:
                error = (
                    f"Object '{name}' produces {count} solids, but "
                    f"expected_solids={expected_solids}."
                )
        elif report["solid_count"] is not None and report["solid_count"] > 1:
            error = (
                f"Object '{name}' produces {report['solid_count']} solids; pass "
                "expected_solids explicitly for a multisolid contract."
            )
    report["ok"] = error is None
    report["error"] = error
    return report


# ---------------------------------------------------------------------------
# Mutation gate.
# ---------------------------------------------------------------------------


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _rollback_failure_details(stage: str, exc: BaseException) -> dict:
    """Explicit ``rollbackFailed`` payload preserving the original error.

    A rollback that failed halfway must never read as a valid rollback: the
    stage names which step (``abort`` or ``recompute``) broke, and the
    original mutation failure — with its diagnostics for tool errors — is
    carried in the details and the exception chain.
    """

    details: dict = {
        "rollbackFailed": True,
        "rollbackStage": stage,
        "originalError": _describe(exc),
    }
    if isinstance(exc, ToolError) and exc.details is not None:
        details["originalDetails"] = exc.details
    return details


def _reject_user_transaction(ctx: Any, doc: Any) -> None:
    """Refuse to nest MCP mutations inside a user transaction."""

    app = getattr(ctx, "App", None)
    if app is not None:
        getter = getattr(app, "getActiveTransaction", None)
        if callable(getter):
            try:
                active = getter()
            except Exception as exc:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"cannot check for an active user transaction: {_describe(exc)}",
                ) from exc
            if active:
                raise ToolError(
                    VALIDATION_FAILED,
                    "a user transaction is already active in FreeCAD; refusing "
                    "to nest an MCP mutation in it",
                    {"reason": "user_transaction_active"},
                )
    if bool(getattr(doc, "HasPendingTransaction", False)):
        raise ToolError(
            VALIDATION_FAILED,
            f"document '{getattr(doc, 'Name', '<unknown>')}' has a pending user "
            "transaction; refusing to nest an MCP mutation in it",
            {"reason": "pending_transaction"},
        )


def _dependents_of(targets: list[Any]) -> tuple[list[Any], bool]:
    """Transitive InList closure of ``targets`` and whether it was truncated.

    The closure walk stops at ``_MAX_DEPENDENTS``; the truncated flag lets
    callers refuse the complexity instead of silently skipping dependents.
    """

    seen = {str(getattr(obj, "Name", "")) for obj in targets}
    dependents: list[Any] = []
    truncated = False
    queue = list(targets)
    while queue:
        obj = queue.pop(0)
        try:
            links = list(obj.InList)
        except Exception:
            continue
        for dep in links:
            dep_name = str(getattr(dep, "Name", ""))
            if not dep_name or dep_name in seen:
                continue
            if len(dependents) >= _MAX_DEPENDENTS:
                truncated = True
                break
            seen.add(dep_name)
            dependents.append(dep)
            queue.append(dep)
        if truncated:
            break
    return sorted(
        dependents, key=lambda item: str(getattr(item, "Name", ""))
    ), truncated


def _baseline_solid_counts(dependents: list[Any]) -> dict[str, int]:
    """Capture the observed solid counts of currently valid dependents.

    Taken before the mutation body runs, this baseline becomes the explicit
    solid contract each already-valid dependent is revalidated against after
    recompute, so a preexisting multisolid dependent (e.g. a compound created
    with ``expected_solids=2``) stays valid when an input object is edited.
    Only a fully valid geometry report may grandfather its count — object
    validity, a valid shape, the exact observed count, and a positive
    volume. Invalid-shape, non-positive-volume, or otherwise failing
    dependents stay under the default contract, shapeless dependents are
    valid without one anyway, and zero-solid shapes carry no count worth
    preserving.
    """

    baseline: dict[str, int] = {}
    for dep in dependents:
        name = str(getattr(dep, "Name", ""))
        if not name or name in baseline:
            continue
        if object_validity_error(dep) is not None:
            continue
        try:
            shape = dep.Shape
        except Exception:
            continue
        if shape is None:
            continue
        count = _solid_count(shape)
        if count is None or count <= 0:
            continue
        # Grandfather only a fully valid report: geometry_report enforces
        # shape validity and positive volume for solid-bearing shapes, so
        # an invalid-shape, non-positive-volume, or otherwise failing
        # dependent can never have its count captured as a contract.
        if not geometry_report(dep, count)["ok"]:
            continue
        baseline[name] = count
    return baseline


@contextmanager
def mutation(
    ctx: Any,
    doc: Any,
    label: str,
    objects: Iterable[Any] | Callable[[], Iterable[Any]],
    expected_solids: int | None = None,
) -> Any:
    """Run one tool mutation inside its own FreeCAD transaction.

    Yields ``applied``: the actual sanitized Names of the mutated objects,
    filled only after a validated commit and left ``[]`` whenever anything
    rolled back. ``objects`` is an iterable of the affected objects or a
    zero-argument callable returning them; the callable is evaluated inside
    the transaction just before recompute, so create flows can hand back the
    object they only create inside the ``with`` body.

    Order of operations: reject busy/FEM-locked documents (via
    ``ctx.check_document_idle``) and preexisting user transactions
    (``App.getActiveTransaction`` plus ``doc.HasPendingTransaction``);
    preserve UndoMode and enable undo only for this operation; open the
    transaction; run the body; recompute; validate the affected objects with
    ``object_validity_error`` plus ``geometry_report(expected_solids)`` *and*
    every recomputed dependent with ``object_validity_error`` plus a solid
    contract: dependents already valid before the mutation are held to their
    captured pre-mutation solid count (preserving explicit multisolid
    contracts the API cannot restate per dependent), while new or previously
    invalid/shapeless dependents use the default ``geometry_report`` solid
    contract. A dependent closure larger
    than 256 is refused before any effects for direct targets (and rolled
    back for create flows) instead of being silently skipped; commit on
    success, abort on any assignment, recompute, validation or commit
    failure. FreeCAD's abort undoes recorded changes without recomputing,
    so every rollback recomputes to clear the stale Touched/Invalid cached
    state and restore usable pre-mutation geometry. Abort or rollback
    recompute failures surface explicitly as ``rollbackFailed`` tool errors
    naming the failing stage, with the original error and diagnostics
    preserved — never as a valid rollback. UndoMode is restored in every
    exit path.
    """

    ctx.check_document_idle(doc)
    _reject_user_transaction(ctx, doc)
    # Reject runaway complexity before any effects when the targets are
    # known up front; create flows (callable) re-check after the body and
    # roll back explicitly instead of skipping dependents.
    pre_targets: list[Any] | None = None
    baseline: dict[str, int] = {}
    if not callable(objects):
        pre_targets = list(objects)
        pre_dependents, exceeded = _dependents_of(pre_targets)
        if exceeded:
            raise ToolError(
                VALIDATION_FAILED,
                f"mutation '{label}' would affect more than {_MAX_DEPENDENTS} "
                "dependent objects; refusing before any effects",
                {"reason": "too_many_dependents"},
            )
        # Capture the pre-mutation solid counts of the known dependents
        # before the transaction or the body can change anything. Deps that
        # only appear afterwards (dynamic links, created objects) have no
        # baseline and stay under the default solid contract.
        baseline = _baseline_solid_counts(pre_dependents)

    undo_mode = getattr(doc, "UndoMode", 0)
    applied: list[str] = []
    try:
        doc.UndoMode = 1
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"cannot enable undo for mutation '{label}': {_describe(exc)}",
        ) from exc

    try:
        try:
            doc.openTransaction(label)
        except Exception as exc:
            raise ToolError(
                VALIDATION_FAILED,
                f"cannot open mutation transaction '{label}': {_describe(exc)}",
            ) from exc

        try:
            yield applied

            targets = pre_targets if pre_targets is not None else list(objects())
            if not targets:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"mutation '{label}' affected no objects",
                )
            dependents, exceeded = _dependents_of(targets)
            if exceeded:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"mutation '{label}' would affect more than "
                    f"{_MAX_DEPENDENTS} dependent objects; rolled back",
                    {"reason": "too_many_dependents"},
                )

            doc.recompute()
            errors: list[str] = []
            target_names = {str(getattr(obj, "Name", "")) for obj in targets}
            for obj in targets:
                problem = object_validity_error(obj)
                if problem is not None:
                    errors.append(problem)
                    continue
                report = geometry_report(obj, expected_solids)
                if not report["ok"]:
                    errors.append(str(report["error"]))
            for dep in dependents:
                dep_name = str(getattr(dep, "Name", ""))
                if dep_name in target_names:
                    continue
                problem = object_validity_error(dep)
                if problem is not None:
                    errors.append(problem)
                    continue
                # A dependent captured valid before the mutation keeps its
                # observed solid count as the contract; everything else —
                # new dependents, previously invalid or shapeless ones —
                # falls back to the default contract (expected_solids=None).
                dep_report = geometry_report(dep, baseline.get(dep_name))
                if not dep_report["ok"]:
                    errors.append(str(dep_report["error"]))
            if errors:
                raise ToolError(
                    VALIDATION_FAILED,
                    "recompute left the document invalid; the mutation was rolled back",
                    {"errors": errors[:_MAX_DIAGNOSTICS]},
                )
            doc.commitTransaction()
        except BaseException as exc:
            applied.clear()
            try:
                doc.abortTransaction()
            except Exception as abort_exc:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"mutation '{label}' failed and its rollback also failed: "
                    f"{_describe(abort_exc)}",
                    _rollback_failure_details("abort", exc),
                ) from exc
            # FreeCAD's abortTransaction undoes the recorded changes without
            # recomputing: targets and dependents can stay Touched/Invalid
            # with stale cached state, leaving the previous valid geometry
            # unusable. Recompute so the pre-mutation state is usable again.
            try:
                doc.recompute()
            except Exception as recompute_exc:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"mutation '{label}' failed and its rollback recompute "
                    f"also failed: {_describe(recompute_exc)}",
                    _rollback_failure_details("recompute", exc),
                ) from exc
            if isinstance(exc, Exception):
                if isinstance(exc, ToolError):
                    raise
                raise ToolError(
                    VALIDATION_FAILED,
                    f"mutation '{label}' failed and was rolled back: {_describe(exc)}",
                ) from exc
            raise
    finally:
        doc.UndoMode = undo_mode

    applied.extend(sorted({str(getattr(obj, "Name", "")) for obj in targets}))
