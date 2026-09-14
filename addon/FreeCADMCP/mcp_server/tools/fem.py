"""run_fem — modern CalculiX solve lifecycle (PLAN §5 item 16, §6).

The handler runs on the GUI thread. It performs every synchronous step
document-idle check, prerequisite probes, solver classification, the
native analysis checks and solver creation), then starts the native
QProcess-backed ``CalculiXTools`` run and returns a
:class:`concurrent.futures.Future` for the server to retain. The Future
resolves with the structured result payload — or fails with
:class:`ToolError` — only after the native ``update_properties`` result
loading has actually run. The GUI thread is never blocked on the solver and
the solver process is never waited on or killed.

Preparation is atomic before the process launches: the native analysis
checks run before any solver is created, and a missing solver is created
inside one transaction that also runs the checks before commit, so any
pre-launch failure removes a solver this call introduced while solvers
that existed before the call are never altered. The working directory is
created only after every document mutation has committed. Failures after
that commit (tool construction, ``prepare()``) cannot be rolled back and
are reported truthfully: ``details.operationState`` is
``"may_have_changed"`` exactly when a committed new solver remains, and
omitted when the document was never mutated.

The local ``CalculiXTools`` subclass overrides ``update_properties`` so the
document identity/generation guard runs BEFORE the native loader mutates a
possibly-stale document, and every terminal path (successful load, conversion
failure, failed start, nonzero/crashed exit, server cancellation) funnels
into one exactly-once finalizer.
"""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
import traceback
from collections.abc import Callable
from typing import Any

from mcp_server.protocol import (
    OBJECT_NOT_FOUND,
    PATH_NOT_ALLOWED,
    SERVER_BUSY,
    SOLVER_FAILED,
    VALIDATION_FAILED,
    ToolError,
)

_ANALYSIS_TYPE = "Fem::FemAnalysis"
_MODERN_SOLVER_TYPE = "Fem::SolverCalculiX"
_LEGACY_SOLVER_TYPE = "Fem::SolverCcxTools"
_PIPELINE_TYPE = "Fem::FemPostPipeline"

_CANCEL_POLL_MS = 500

# Hard caps for the returned result summary. Traversal keeps cheap total
# counters for every block and file it walks, but per-block array evidence
# is only collected while a cap has room; the truncation counters make any
# cap honest instead of silent.
MAX_RESULT_FILES = 256
MAX_RESULT_BLOCKS = 256
MAX_RESULT_FIELDS = 128

# Every capability ``unavailable``/``reason`` string on the wire is
# schema-bounded at 512 characters, but raw exception text is not. All
# producers funnel through one head-truncating renderer here so no
# exception type or message can push a reason past its schema limit; the
# server's capability probes import it instead of re-implementing the cut.
MAX_REASON_LENGTH = 512
_REASON_TRUNCATED = "...[truncated]"


def bounded_reason(text: str, *, limit: int = MAX_REASON_LENGTH) -> str:
    """Bound one reason string to ``limit`` characters.

    Truncation is deterministic: the string is cut from the head and the
    explicit ``...[truncated]`` suffix marks any cut, so a reader never
    mistakes bounded text for complete text.
    """

    if len(text) <= limit:
        return text
    if len(_REASON_TRUNCATED) >= limit:
        # Degenerate limit: only a hard cut preserves the bound.
        return text[:limit]
    return text[: limit - len(_REASON_TRUNCATED)] + _REASON_TRUNCATED


def bounded_exception_reason(
    exc: BaseException, prefix: str = "", *, limit: int = MAX_REASON_LENGTH
) -> str:
    """Render ``prefix + "ExceptionType: message"`` within ``limit`` chars."""

    return bounded_reason(f"{prefix}{type(exc).__name__}: {exc}", limit=limit)


# ---------------------------------------------------------------------------
# Tool definition.
# ---------------------------------------------------------------------------

_RANGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "min": {"type": "number"},
        "max": {"type": "number"},
    },
    "required": ["min", "max"],
    "additionalProperties": False,
}

_BLOCK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "block": {"type": "integer", "minimum": 0},
        "points": {"type": "integer", "minimum": 0},
        "cells": {"type": "integer", "minimum": 0},
        "scalars": {
            "type": "object",
            "additionalProperties": _RANGE_SCHEMA,
            "maxProperties": MAX_RESULT_FIELDS,
        },
        "vectors": {
            "type": "object",
            "additionalProperties": _RANGE_SCHEMA,
            "maxProperties": MAX_RESULT_FIELDS,
        },
        "scalar_count": {"type": "integer", "minimum": 0},
        "scalars_truncated": {"type": "boolean"},
        "vector_count": {"type": "integer", "minimum": 0},
        "vectors_truncated": {"type": "boolean"},
    },
    "required": [
        "block",
        "points",
        "cells",
        "scalars",
        "vectors",
        "scalar_count",
        "scalars_truncated",
        "vector_count",
        "vectors_truncated",
    ],
    "additionalProperties": False,
}

_AGGREGATES_SCHEMA: dict[str, Any] = {
    # Sums over every traversed block, not only the returned ones, and
    # not deduplicated nodes.
    "type": "object",
    "properties": {
        "block_count": {"type": "integer", "minimum": 0},
        "point_count_sum": {"type": "integer", "minimum": 0},
        "cell_count_sum": {"type": "integer", "minimum": 0},
    },
    "required": ["block_count", "point_count_sum", "cell_count_sum"],
    "additionalProperties": False,
}

_RUN_FEM_DEFINITION: dict[str, Any] = {
    "name": "run_fem",
    "description": (
        "Run the CalculiX solver of a FEM analysis through the modern "
        "Fem::SolverCalculiX pipeline and return the loaded VTK result "
        "summary (.vtm plus referenced .vtu files, per-block point/cell "
        "counts and finite scalar/vector-magnitude ranges). The summary is "
        "bounded: vtu files, blocks and per-block scalar/vector fields are "
        "capped, with *_count and *_truncated counters reporting what was "
        "traversed. Asynchronous: the result arrives when the solver "
        "process and the native result loading finish; the solver is never "
        "killed on timeout or cancel."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "document": {"type": "string", "minLength": 1, "maxLength": 256},
            "analysis": {"type": "string", "minLength": 1, "maxLength": 256},
            "timeout_s": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3600,
                "default": 600,
            },
        },
        "required": ["document", "analysis"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "$defs": {
            "range": _RANGE_SCHEMA,
            "block": _BLOCK_SCHEMA,
            "aggregates": _AGGREGATES_SCHEMA,
        },
        "properties": {
            "pipeline": {"type": "string"},
            "analysis": {"type": "string"},
            "solver": {"type": "string"},
            "working_dir": {"type": "string"},
            "vtk_path": {"type": "string"},
            "vtu_files": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_RESULT_FILES,
            },
            "vtu_file_count": {"type": "integer", "minimum": 0},
            "vtu_files_truncated": {"type": "boolean"},
            "blocks": {"type": "array", "items": {"$ref": "#/$defs/block"}},
            "block_count": {"type": "integer", "minimum": 0},
            "blocks_truncated": {"type": "boolean"},
            "aggregates": {"$ref": "#/$defs/aggregates"},
            "cancellation_requested": {"type": "boolean"},
        },
        "required": [
            "pipeline",
            "analysis",
            "solver",
            "working_dir",
            "vtk_path",
            "vtu_files",
            "vtu_file_count",
            "vtu_files_truncated",
            "blocks",
            "block_count",
            "blocks_truncated",
            "aggregates",
            "cancellation_requested",
        ],
        "additionalProperties": False,
    },
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [_RUN_FEM_DEFINITION]

# Populated after run_fem is defined below.
HANDLERS: dict[str, Callable[[Any, dict[str, Any]], Any]] = {}


def availability() -> dict[str, Any]:
    """FEM prerequisite snapshot; ``available`` is False with a reason.

    ``has_frd_to_vtk`` comes from the ``Fem`` module itself, not femtools.
    """
    info: dict[str, Any] = {
        "available": False,
        "has_frd_to_vtk": False,
        "vtk_support": False,
        "calculix_binary": None,
        "reason": None,
    }
    try:
        import Fem
    except Exception as exc:  # FreeCAD runtime always provides Fem.
        info["reason"] = bounded_exception_reason(exc, "Fem module unavailable: ")
        return info
    info["has_frd_to_vtk"] = hasattr(Fem, "frdToVTK")
    if not info["has_frd_to_vtk"]:
        info["reason"] = "Fem.frdToVTK is unavailable; results cannot be converted"
        return info
    try:
        from femsolver import settings as fem_settings

        binary = fem_settings.get_binary("Calculix", silent=True)
    except Exception as exc:
        info["reason"] = bounded_exception_reason(exc, "CalculiX solver settings unavailable: ")
        return info
    if not binary or not os.path.isfile(binary):
        info["reason"] = (
            "CalculiX executable not found; configure it in the FEM solver "
            "preferences (it is never installed automatically)"
        )
        return info
    info["calculix_binary"] = binary
    try:
        import femsolver.calculix.calculixtools  # noqa: F401  pulls vtk/numpy
    except Exception as exc:
        info["reason"] = bounded_exception_reason(exc, "VTK result support unavailable: ")
        return info
    info["vtk_support"] = True
    info["available"] = True
    return info


# ---------------------------------------------------------------------------
# Handler.
# ---------------------------------------------------------------------------


def run_fem(ctx: Any, arguments: dict[str, Any]) -> Any:
    """Prepare and launch one modern CalculiX solve; returns a Future."""
    document_name = arguments["document"]
    analysis_name = arguments["analysis"]

    doc = ctx.require_document(document_name)
    analysis = ctx.require_object(doc, analysis_name)
    if not analysis.isDerivedFrom(_ANALYSIS_TYPE):
        raise ToolError(
            OBJECT_NOT_FOUND,
            f"{analysis_name!r} is not a FEM analysis object",
            details={
                "object": analysis_name,
                "TypeId": getattr(analysis, "TypeId", None),
            },
        )

    # The shared gate is the one busy rejection for real contexts; plain
    # handler fakes without the method keep the historical inline check.
    check_idle = getattr(ctx, "check_document_idle", None)
    if callable(check_idle):
        check_idle(doc)
    else:
        identity = ctx.document_identity(doc)
        if identity in (getattr(ctx, "active_solves", None) or {}):
            raise ToolError(
                SERVER_BUSY,
                "a solve is already running on this document",
                details={"document": identity, "operation": "run_fem"},
            )

    prereq = availability()
    if not prereq["available"]:
        raise ToolError(
            SOLVER_FAILED,
            f"FEM prerequisites missing: {prereq['reason']}",
            details=prereq,
        )

    solver, created_solver = _prepare_solver(ctx, doc, analysis)

    # The working directory exists only after every document mutation has
    # committed, so a rolled-back preparation never leaves it behind. A
    # failure here is a post-commit failure: when this call created the
    # solver, it remains committed, so the error reports may_have_changed.
    try:
        working_dir = _create_working_directory(ctx)
    except ToolError as exc:
        if created_solver:
            details = dict(exc.details or {})
            details["operationState"] = "may_have_changed"
            exc.details = details
        raise

    operation = _FemSolve(
        ctx,
        doc=doc,
        analysis=analysis,
        solver=solver,
        working_dir=working_dir,
        created_solver=created_solver,
    )
    operation.start()
    return operation.future


def _find_modern_solver(analysis: Any) -> Any | None:
    """Return the single modern CalculiX solver, or ``None``; never convert."""
    modern: list[Any] = []
    legacy: list[Any] = []
    for member in list(getattr(analysis, "Group", None) or []):
        proxy_type = getattr(getattr(member, "Proxy", None), "Type", None)
        if proxy_type == _MODERN_SOLVER_TYPE:
            modern.append(member)
        elif proxy_type == _LEGACY_SOLVER_TYPE:
            legacy.append(member)

    def _names(objects: list[Any]) -> list[str]:
        """Map solver objects to their names for structured error details."""
        return [getattr(obj, "Name", "?") for obj in objects]

    if modern and legacy:
        raise ToolError(
            SOLVER_FAILED,
            "ambiguous CalculiX setup: the analysis mixes modern and legacy "
            "solvers; no solver was deleted or converted",
            details={"modern": _names(modern), "legacy": _names(legacy)},
        )
    if len(modern) > 1:
        raise ToolError(
            SOLVER_FAILED,
            "ambiguous CalculiX setup: the analysis contains more than one modern solver",
            details={"solvers": _names(modern)},
        )
    if modern:
        return modern[0]
    if legacy:
        raise ToolError(
            SOLVER_FAILED,
            "the analysis contains only a legacy CalculiX solver "
            "(Fem::SolverCcxTools); this server runs modern "
            "Fem::SolverCalculiX analyses only and never converts or "
            "deletes existing solvers",
            details={"legacy_solvers": _names(legacy)},
        )
    return None


def _prepare_solver(ctx: Any, doc: Any, analysis: Any) -> tuple[Any, bool]:
    """Run the native checks and create a missing solver atomically.

    Returns ``(solver, created_solver)``. The checks run before any solver
    is created; a missing solver is created inside one transaction that
    also runs the checks before commit, so a failure aborts the
    transaction and removes the new solver. A solver that existed before
    the call is returned unchanged.
    """
    existing = _find_modern_solver(analysis)
    if existing is not None:
        _run_analysis_checks(analysis, existing)
        return existing, False

    mesh = _analysis_mesh(analysis)
    if _check_api_accepts_none_solver(analysis, mesh):
        # Full no-solver validation runs before any mutation: a check
        # failure raises here without creating anything. (The probe below
        # already ran this check; rerunning keeps the message conversion
        # in one place.)
        _check_analysis_members(analysis, None, mesh)
        return _create_modern_solver(ctx, doc, analysis), True
    # The released check API requires a solver: create it and validate it
    # inside one transaction so any check failure aborts and removes the
    # new solver.
    return _create_modern_solver(ctx, doc, analysis, with_checks=True), True


def _check_api_accepts_none_solver(analysis: Any, mesh: Any) -> bool:
    """True when the native per-member check runs without a solver.

    The probe calls the released ``femtools`` function directly: its first
    statement dereferences solver attributes, so the released API raises
    ``AttributeError`` and this returns ``False``. The wrapping helpers
    convert every failure into ``ToolError``, which would hide exactly
    this distinction, so the probe must bypass them.
    """
    from femtools import membertools
    from femtools.checksanalysis import check_member_for_solver_calculix

    try:
        member = membertools.AnalysisMember(analysis)
    except Exception as exc:
        # An analysis the member scan cannot read is a genuine check
        # failure, not an API incompatibility; report it through the same
        # structured path as the checked run instead of masking it.
        raise ToolError(
            VALIDATION_FAILED,
            f"running the native analysis checks failed: {exc}",
            details={"traceback": traceback.format_exc()},
        ) from exc
    try:
        check_member_for_solver_calculix(analysis, None, mesh, member)
    except AttributeError:
        # Released femtools dereferences solver attributes in its first
        # statement, so this is the unsupported no-solver case. An
        # unrelated internal AttributeError would be retried inside the
        # create-and-check mutation, fail there the same way, and surface
        # as a structured wrapped error after a clean abort.
        return False
    return True


def _create_modern_solver(ctx: Any, doc: Any, analysis: Any, *, with_checks: bool = False) -> Any:
    """Create one modern solver inside the shared mutation gate.

    ``with_checks`` runs the native analysis checks before the commit, so
    a failed check aborts the transaction and the abort removes the new
    solver; used when the check API cannot run without a solver.
    """
    import ObjectsFem

    from mcp_server.object_validation import mutation

    created: list[Any] = []

    def _affected() -> list[Any]:
        """Report the solver this call created to the mutation gate."""
        return list(created)

    try:
        with mutation(ctx, doc, "run_fem:create-solver", _affected):
            solver = ObjectsFem.makeSolverCalculiX(doc)
            analysis.addObject(solver)
            created.append(solver)
            doc.recompute()
            if with_checks:
                _run_analysis_checks(analysis, solver)
    except ToolError:
        raise
    except Exception as exc:
        details: dict[str, Any] = {"traceback": traceback.format_exc()}
        if created:
            # The transaction abort removed the solver this call created.
            details["operationState"] = "rolled_back"
        raise ToolError(
            SOLVER_FAILED,
            f"creating the modern CalculiX solver failed: {exc}",
            details=details,
        ) from exc
    return solver


def _run_analysis_checks(analysis: Any, solver: Any) -> None:
    """Honor the native analysis checks that prepare() would ignore."""
    _check_analysis_members(analysis, solver, _analysis_mesh(analysis))


def _analysis_mesh(analysis: Any) -> Any:
    """Collect the single mesh to solve; raises before any mutation."""
    from femtools import membertools

    try:
        mesh, mesh_message = membertools.get_mesh_to_solve(analysis)
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"collecting the analysis mesh failed: {exc}",
            details={"traceback": traceback.format_exc()},
        ) from exc
    if mesh_message:
        raise ToolError(
            VALIDATION_FAILED,
            "FEM analysis checks failed",
            details={"checks": mesh_message.strip()},
        )
    return mesh


def _check_analysis_members(analysis: Any, solver: Any, mesh: Any) -> None:
    """Run the native per-member checks against one solver (may be ``None``)."""
    from femtools import membertools
    from femtools.checksanalysis import check_member_for_solver_calculix

    try:
        member = membertools.AnalysisMember(analysis)
        message = check_member_for_solver_calculix(analysis, solver, mesh, member)
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"running the native analysis checks failed: {exc}",
            details={"traceback": traceback.format_exc()},
        ) from exc
    if message:
        raise ToolError(
            VALIDATION_FAILED,
            "FEM analysis checks failed",
            details={"checks": message.strip()},
        )


def _create_working_directory(ctx: Any) -> str:
    """Unique empty working directory under the first allowed root that works.

    Created before ``CalculiXTools`` construction so the native working
    directory preference logic keeps it. Runs only after every document
    mutation has committed, so a rolled-back preparation never leaves a
    directory behind.
    """
    roots = list((getattr(ctx, "settings", None) or {}).get("allowed_roots") or [])
    if not roots:
        raise ToolError(
            PATH_NOT_ALLOWED,
            "no allowed_roots configured for FEM working directories",
        )
    last_error: OSError | None = None
    for root in roots:
        try:
            return tempfile.mkdtemp(prefix="fem_mcp_", dir=root)
        except OSError as exc:
            last_error = exc
    raise ToolError(
        VALIDATION_FAILED,
        f"cannot create the FEM working directory under any of the "
        f"{len(roots)} configured allowed roots",
        details={
            "reason": "working_directory_unavailable",
            "os_error": str(last_error),
        },
    ) from last_error


# ---------------------------------------------------------------------------
# Async operation.
# ---------------------------------------------------------------------------


class _FemSolve:
    """Retained state of one asynchronous run_fem operation."""

    def __init__(
        self,
        ctx: Any,
        *,
        doc: Any,
        analysis: Any,
        solver: Any,
        working_dir: str,
        created_solver: bool = False,
    ) -> None:
        """Bind the solve inputs and an unresolved Future; nothing is started."""
        self.ctx = ctx
        self.doc = doc
        self.analysis = analysis
        self.solver = solver
        self.working_dir = working_dir
        self.created_solver = created_solver
        self.identity: str | None = None
        self.generation: int | None = None
        self.future: concurrent.futures.Future = concurrent.futures.Future()
        self.finished = False
        self.cancel_requested = False
        self.tool: Any = None
        self._cancel_timer: Any = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Build the tool, prepare, register, then start the QProcess."""
        original_working_directory = getattr(self.solver, "WorkingDirectory", None)
        try:
            # The unique allowed-root directory is assigned BEFORE
            # construction so the native working-directory preference logic
            # keeps it. All three post-commit steps report structured
            # failures; none may escape as a raw GUI exception.
            self.solver.WorkingDirectory = self.working_dir
            tool_class = _tool_class()
            self.tool = tool_class(self.solver, self)
            self.tool.prepare()
        except ToolError as exc:
            try:
                if original_working_directory is not None:
                    self.solver.WorkingDirectory = original_working_directory
            except Exception:
                pass
            if not self.created_solver:
                raise
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            details["operationState"] = "may_have_changed"
            raise ToolError(exc.code, exc.message, details) from exc
        except Exception as exc:
            restored = True
            try:
                if original_working_directory is not None:
                    self.solver.WorkingDirectory = original_working_directory
            except Exception:
                restored = False
            details = self._launch_failure_details(traceback_text=traceback.format_exc())
            if not restored:
                details["operationState"] = "may_have_changed"
            raise ToolError(
                SOLVER_FAILED,
                f"preparing the CalculiX run failed: {exc}",
                details=details,
            ) from exc

        # Captured after prepare, before compute; the operation's own
        # result-load events all happen after the guard inside
        # update_properties, so they never invalidate this snapshot.
        self.identity = self.ctx.document_identity(self.doc)
        self.generation = self.ctx.document_generation(self.doc)

        self.tool.process.errorOccurred.connect(self._on_error)
        self.tool.process.finished.connect(self._on_finished)
        self.ctx.active_solves[self.identity] = self
        self._start_cancel_watch()
        try:
            self.tool.compute()
        except Exception as exc:
            self._finalize(
                error=ToolError(
                    SOLVER_FAILED,
                    f"starting the CalculiX process failed: {exc}",
                    details=self._launch_failure_details(traceback_text=traceback.format_exc()),
                )
            )

    def _launch_failure_details(self, *, traceback_text: str) -> dict[str, Any]:
        """Details for a failure after the preparation transaction committed.

        A committed new solver from this call remains in the document, so
        the state is truthfully ``may_have_changed``; without one the
        document was never mutated and no ``operationState`` is claimed.
        """
        details: dict[str, Any] = {
            "working_dir": self.working_dir,
            "traceback": traceback_text,
        }
        if self.created_solver:
            details["operationState"] = "may_have_changed"
        return details

    def _cancel_requested(self) -> bool:
        """True through any shared route: direct request or the ctx Event.

        The Event is the server's one cancellation object for this
        operation (tasks/cancel, deadline sweep, disconnect and stop all
        set it). Reading it again here keeps the diagnostics truthful even
        when the poll timer missed a late set.
        """
        if self.cancel_requested:
            return True
        event = getattr(self.ctx, "cancel_event", None)
        return event is not None and event.is_set()

    # -- native result loading (runs on the GUI thread via Qt slots) --------

    def _guard_before_load(self) -> None:
        """Validate existence/identity/generation BEFORE the native loader mutates."""
        try:
            # Existence first: a closed document is never interrogated for
            # identity, and a same-name reopen resolves to the new document
            # whose lifetime identity/generation cannot match the snapshot.
            doc = self.ctx.require_document(self.doc.Name)
            identity = self.ctx.document_identity(doc)
            generation = self.ctx.document_generation(doc)
        except ToolError as exc:
            raise ToolError(
                VALIDATION_FAILED,
                f"the solved document is no longer available: {exc.message}",
                details={"working_dir": self.working_dir},
            ) from exc
        except Exception as exc:
            raise ToolError(
                VALIDATION_FAILED,
                f"the solved document is no longer available: {exc}",
                details={"working_dir": self.working_dir},
            ) from exc
        if identity != self.identity:
            raise ToolError(
                VALIDATION_FAILED,
                "document identity changed during the solve; results were "
                "not loaded; run run_fem again",
                details={
                    "expected_identity": self.identity,
                    "actual_identity": identity,
                    "working_dir": self.working_dir,
                },
            )
        if generation != self.generation:
            raise ToolError(
                VALIDATION_FAILED,
                "document changed during the solve; results were not loaded; run run_fem again",
                details={
                    "reason": "stale_generation",
                    "expectedGeneration": self.generation,
                    "actualGeneration": generation,
                    "nextTool": "inspect_documents",
                    "working_dir": self.working_dir,
                },
            )

    def _finalize_success(self) -> None:
        """Build the result payload and settle the Future with it or an error."""
        try:
            payload = self._result_payload()
        except ToolError as exc:
            self._finalize(error=exc)
        except Exception as exc:
            self._finalize(
                error=ToolError(
                    SOLVER_FAILED,
                    f"reading the loaded result pipeline failed: {exc}",
                    details={
                        "working_dir": self.working_dir,
                        "traceback": traceback.format_exc(),
                    },
                )
            )
        else:
            self._finalize(payload=payload)

    def _finalize(
        self, *, payload: dict[str, Any] | None = None, error: ToolError | None = None
    ) -> None:
        """Exactly-once terminal transition for every completion path."""
        if self.finished:
            return
        self.finished = True
        self._stop_cancel_watch()
        if self.identity is not None:
            self.ctx.active_solves.pop(self.identity, None)
        # The server stops deadline tracking BEFORE the Future settles so
        # a waiter can never observe a resolved result under a live
        # deadline. Exactly-once is owned by the finished flag above.
        operation_finished = getattr(self.ctx, "operation_finished", None)
        if callable(operation_finished):
            try:
                operation_finished()
            except Exception:
                pass  # The finalizer itself must never raise.
        if error is not None:
            self.future.set_exception(error)
        else:
            self.future.set_result(payload)

    # -- QProcess handlers ---------------------------------------------------

    def _on_error(self, error: Any) -> None:
        """QProcess.errorOccurred slot; only failed-to-start finalizes here."""
        if self.finished:
            return
        process_error = getattr(getattr(self.tool, "process", None), "ProcessError", None)
        failed_to_start = getattr(process_error, "FailedToStart", None)
        if failed_to_start is not None and error == failed_to_start:
            self._finalize(
                error=ToolError(
                    SOLVER_FAILED,
                    "the CalculiX process failed to start",
                    details=self._exit_details(reason="failed_to_start"),
                )
            )

    def _on_finished(self, code: int, status: Any) -> None:
        """QProcess.finished slot; success is owned by update_properties."""
        if self.finished:
            return
        process = getattr(self.tool, "process", None)
        exit_status = getattr(process, "ExitStatus", None)
        normal_exit = getattr(exit_status, "NormalExit", None)
        is_normal = normal_exit is not None and status == normal_exit
        if is_normal and code == 0:
            # The native loader should have finalized through
            # update_properties; report truthfully when it did not.
            self._finalize(
                error=ToolError(
                    SOLVER_FAILED,
                    "CalculiX exited with code 0 but the native result loader did not complete",
                    details=self._exit_details(exit_code=code, exit_status="NormalExit"),
                )
            )
            return
        failed_to_start = getattr(getattr(process, "ProcessError", None), "FailedToStart", None)
        actual_error = getattr(process, "error", None)
        process_error = actual_error() if callable(actual_error) else None
        if failed_to_start is not None and process_error == failed_to_start:
            self._finalize(
                error=ToolError(
                    SOLVER_FAILED,
                    "the CalculiX process failed to start",
                    details=self._exit_details(
                        exit_code=code,
                        exit_status="CrashExit",
                        reason="failed_to_start",
                    ),
                )
            )
            return
        if is_normal:
            message = f"CalculiX exited with nonzero code {code}"
        else:
            message = "the CalculiX process crashed before finishing"
        self._finalize(
            error=ToolError(
                SOLVER_FAILED,
                message,
                details=self._exit_details(
                    exit_code=code,
                    exit_status="NormalExit" if is_normal else "CrashExit",
                ),
            )
        )

    def _exit_details(
        self,
        *,
        exit_code: int | None = None,
        exit_status: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Assemble bounded process-exit evidence for a SOLVER_FAILED error."""
        details: dict[str, Any] = {
            "working_dir": self.working_dir,
            "stdout": _process_text(self.tool.process, "readAllStandardOutput"),
            "stderr": _process_text(self.tool.process, "readAllStandardError"),
        }
        if exit_code is not None:
            details["exit_code"] = exit_code
        if exit_status is not None:
            details["exit_status"] = exit_status
        if reason is not None:
            details["reason"] = reason
        if self._cancel_requested():
            details["cancellation_requested"] = True
        return details

    # -- cancellation watch --------------------------------------------------

    def _start_cancel_watch(self) -> None:
        """Poll the shared cancel event on the GUI thread via a QTimer."""
        try:
            from PySide.QtCore import QTimer
        except Exception:  # pragma: no cover - GUI runtime always has PySide
            return
        timer = QTimer()
        timer.setInterval(_CANCEL_POLL_MS)
        timer.timeout.connect(self._poll_cancel_event)
        timer.start()
        self._cancel_timer = timer

    def _poll_cancel_event(self) -> None:
        """Record a requested cancellation and stop the watch timer."""
        if self.finished:
            self._stop_cancel_watch()
            return
        if self._cancel_requested():
            self.cancel_requested = True
            self._stop_cancel_watch()

    def _stop_cancel_watch(self) -> None:
        """Stop and release the cancel-poll timer, tolerating repeat calls."""
        timer = self._cancel_timer
        self._cancel_timer = None
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass

    # -- result extraction ----------------------------------------------------

    def _result_payload(self) -> dict[str, Any]:
        """Assemble the bounded solve result from pipeline and result files."""
        pipeline = self._pipeline()
        blocks, totals = _summarize_pipeline(pipeline)
        vtk_path, vtu_files, vtu_count, vtu_truncated = _result_files(self.working_dir)
        return {
            "pipeline": getattr(pipeline, "Name", ""),
            "analysis": getattr(self.analysis, "Name", ""),
            "solver": getattr(self.solver, "Name", ""),
            "working_dir": self.working_dir,
            "vtk_path": vtk_path,
            "vtu_files": vtu_files,
            "vtu_file_count": vtu_count,
            "vtu_files_truncated": vtu_truncated,
            "blocks": blocks,
            "block_count": totals["block_count"],
            "blocks_truncated": totals["block_count"] > len(blocks),
            "aggregates": {
                # Totals over every traversed block, not only the returned
                # ones, and not deduplicated nodes.
                "block_count": totals["block_count"],
                "point_count_sum": totals["point_count_sum"],
                "cell_count_sum": totals["cell_count_sum"],
            },
            "cancellation_requested": self._cancel_requested(),
        }

    def _pipeline(self) -> Any:
        """Return the last FemPostPipeline in solver.Results, refusing absence."""
        results = list(getattr(self.solver, "Results", None) or [])
        pipeline = None
        # Native loader semantics: with KeepResultsOnReRun the pipeline
        # that was just re-read is the last one in Results.
        for candidate in results:
            if candidate.isDerivedFrom(_PIPELINE_TYPE):
                pipeline = candidate
        if pipeline is not None:
            return pipeline
        raise ToolError(
            SOLVER_FAILED,
            "no Fem::FemPostPipeline in solver.Results after the solve",
            details={
                "working_dir": self.working_dir,
                "results": [getattr(res, "Name", "?") for res in results],
            },
        )


# ---------------------------------------------------------------------------
# Local CalculiXTools subclass.
# ---------------------------------------------------------------------------

_TOOL_CLASS: type | None = None


def _tool_class() -> type:
    """Build (once) the CalculiXTools subclass overriding update_properties."""
    global _TOOL_CLASS
    if _TOOL_CLASS is not None:
        return _TOOL_CLASS

    from femsolver.calculix.calculixtools import CalculiXTools

    class _McpCalculiXTools(CalculiXTools):
        """Intercepts native result loading behind the staleness guard."""

        def __init__(self, obj: Any, solve: _FemSolve) -> None:
            """Attach the owning solve operation to the native tool."""
            super().__init__(obj)
            self._solve = solve

        def update_properties(self) -> None:
            """Run the staleness guard before native loading, then finalize once."""
            # Called by the native _process_finished slot after exit 0.
            try:
                self._solve._guard_before_load()
                super().update_properties()
            except ToolError as exc:
                self._solve._finalize(error=exc)
            except Exception as exc:
                self._solve._finalize(
                    error=ToolError(
                        SOLVER_FAILED,
                        f"native result loading failed: {exc}",
                        details={
                            "working_dir": self._solve.working_dir,
                            "traceback": traceback.format_exc(),
                        },
                    )
                )
            else:
                self._solve._finalize_success()

    _TOOL_CLASS = _McpCalculiXTools
    return _TOOL_CLASS


# ---------------------------------------------------------------------------
# VTK multiblock summarization.
# ---------------------------------------------------------------------------


def _summarize_pipeline(pipeline: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Summarize a pipeline's blocks into capped rows plus full traversal totals."""
    data = getattr(pipeline, "Data", None)
    blocks: list[dict[str, Any]] = []
    totals = {"block_count": 0, "point_count_sum": 0, "cell_count_sum": 0}
    if data is None:
        return blocks, totals
    _collect_blocks(data, blocks, totals)
    return blocks, totals


def _collect_blocks(node: Any, blocks: list[dict[str, Any]], totals: dict[str, int]) -> None:
    """Walk a VTK multiblock tree, capping evidence but never the totals."""
    if hasattr(node, "GetNumberOfBlocks") and hasattr(node, "GetBlock"):
        for index in range(node.GetNumberOfBlocks()):
            child = node.GetBlock(index)
            if child is None:
                continue
            _collect_blocks(child, blocks, totals)
        return
    points = node.GetNumberOfPoints()
    if not points:
        return  # nonempty leaves only
    totals["block_count"] += 1
    totals["point_count_sum"] += int(points)
    get_cells = getattr(node, "GetNumberOfCells", None)
    cells = int(get_cells()) if callable(get_cells) else 0
    totals["cell_count_sum"] += cells
    if len(blocks) >= MAX_RESULT_BLOCKS:
        # The returned summaries are capped; only the cheap totals keep
        # counting past the cap.
        return
    block = {
        "block": len(blocks),
        "points": int(points),
        "cells": cells,
        "scalars": {},
        "vectors": {},
        "scalar_count": 0,
        "scalars_truncated": False,
        "vector_count": 0,
        "vectors_truncated": False,
    }
    get_point_data = getattr(node, "GetPointData", None)
    if callable(get_point_data):
        arrays = get_point_data()
        for array_index in range(arrays.GetNumberOfArrays()):
            array = arrays.GetArray(array_index)
            name = arrays.GetArrayName(array_index)
            if not name:
                name = getattr(array, "GetName", lambda: None)() or f"array_{array_index}"
            summary = _array_range(array)
            if summary is None:
                continue
            kind, (low, high) = summary
            if kind == "vector":
                target = block["vectors"]
                count_key, cap_key = "vector_count", "vectors_truncated"
            else:
                target = block["scalars"]
                count_key, cap_key = "scalar_count", "scalars_truncated"
            block[count_key] += 1
            if len(target) >= MAX_RESULT_FIELDS:
                block[cap_key] = True
                continue
            target[name] = {"min": low, "max": high}
    blocks.append(block)


def _array_range(array: Any) -> tuple[str, tuple[float, float]] | None:
    """Finite (min, max) for one VTK array; magnitude range for 2+ comps.

    Returns ``("scalar"|"vector", (min, max))`` or ``None`` when the array
    has no finite value. Injectable in tests.
    """
    try:
        import numpy as np
        from vtkmodules.util import numpy_support as vtk_np

        values = vtk_np.vtk_to_numpy(array)
    except Exception:
        return None
    if values is None:
        return None
    try:
        flat = np.asarray(values, dtype=float)
        if flat.ndim == 1:
            flat = flat.reshape(-1, 1)
        mask = np.isfinite(flat)
        if flat.shape[1] <= 1:
            finite = flat[mask]
            if finite.size == 0:
                return None
            return "scalar", (float(finite.min()), float(finite.max()))
        complete = mask.all(axis=1)
        if not complete.any():
            return None
        magnitudes = np.linalg.norm(flat[complete], axis=1)
        return "vector", (float(magnitudes.min()), float(magnitudes.max()))
    except Exception:
        return None


def _result_files(working_dir: str) -> tuple[str, list[str], int, bool]:
    """The .vtm path plus the bounded .vtu list with its total count."""
    vtm_candidates: list[tuple[float, str]] = []
    vtu_files: list[str] = []
    for root, _dirs, files in os.walk(working_dir):
        for name in files:
            path = os.path.join(root, name)
            if name.endswith(".vtm"):
                try:
                    mtime = os.stat(path).st_mtime
                except OSError:
                    continue
                vtm_candidates.append((mtime, path))
            elif name.endswith(".vtu"):
                vtu_files.append(path)
    if not vtm_candidates:
        raise ToolError(
            SOLVER_FAILED,
            f"no .vtm result dataset was written under {working_dir}",
            details={"working_dir": working_dir},
        )
    vtm_candidates.sort()
    vtu_files.sort()
    vtu_count = len(vtu_files)
    truncated = vtu_count > MAX_RESULT_FILES
    return vtm_candidates[-1][1], vtu_files[:MAX_RESULT_FILES], vtu_count, truncated


def _process_text(process: Any, method_name: str) -> str:
    """Read a QProcess output channel as lossily decoded UTF-8 text."""
    reader = getattr(process, method_name, None)
    if not callable(reader):
        return ""
    try:
        data = reader()
        raw = data.data() if hasattr(data, "data") else data
    except Exception:
        return ""
    if data is None:
        return ""
    try:
        return bytes(raw).decode("utf-8", errors="replace")
    except Exception:
        return ""


HANDLERS["run_fem"] = run_fem
