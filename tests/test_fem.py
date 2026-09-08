"""Tests for mcp_server/tools/fem.py (run_fem).

Loaded in isolation with FreeCAD/FEM/PySide stubs: no FreeCAD import on the
host. The stubs mirror the inspected native sources (femtools/objecttools.py
connects process.finished -> _process_finished on successful exit; the local
subclass must guard BEFORE super().update_properties()).
"""

from contextlib import contextmanager
import concurrent.futures
import importlib.util
from pathlib import Path
import sys
import threading
import types
from typing import Any, Iterator

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
FEM_PATH = ADDON_DIR / "mcp_server" / "tools" / "fem.py"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.protocol import ToolError


# ---------------------------------------------------------------------------
# Stubs mirroring the native lifecycle.
# ---------------------------------------------------------------------------


class FakeSignal:
    def __init__(self) -> None:
        self.slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self.slots.append(slot)

    def emit(self, *args: Any) -> None:
        for slot in tuple(self.slots):
            slot(*args)


class FakeQProcess:
    ExitStatus = types.SimpleNamespace(NormalExit="NormalExit", CrashExit="CrashExit")
    ProcessError = types.SimpleNamespace(FailedToStart="FailedToStart")

    def __init__(self) -> None:
        self.finished = FakeSignal()
        self.errorOccurred = FakeSignal()
        self.stdout = ""
        self.stderr = ""
        self._error: Any = None

    def readAllStandardOutput(self) -> Any:
        return types.SimpleNamespace(data=lambda: self.stdout.encode("utf-8"))

    def readAllStandardError(self) -> Any:
        return types.SimpleNamespace(data=lambda: self.stderr.encode("utf-8"))

    def error(self) -> Any:
        return self._error


class FakeQTimer:
    instances: list["FakeQTimer"] = []

    def __init__(self) -> None:
        self.interval: int | None = None
        self.started = False
        self.stopped = False
        self.timeout = FakeSignal()
        FakeQTimer.instances.append(self)

    def setInterval(self, interval: int) -> None:
        self.interval = interval

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class FakeCalculiXToolsBase:
    """Native ObjectTools/CalculiXTools contract: on exit 0, load results."""

    def __init__(self, obj: Any) -> None:
        self.obj = obj
        self.process = FakeQProcess()
        self.prepare_called = False
        self.compute_called = False
        self.super_update_called = False
        self.process.finished.connect(self._process_finished)

    def _process_finished(self, code: int, status: Any) -> None:
        if status == FakeQProcess.ExitStatus.NormalExit and code == 0:
            self.update_properties()

    def prepare(self) -> None:
        self.prepare_called = True

    def compute(self) -> None:
        self.compute_called = True

    def update_properties(self) -> None:
        # The native loader: converts .frd and loads results into the doc.
        self.super_update_called = True
        self.obj.Results.append(self.obj.loaded_pipeline)


class FakeDocument:
    def __init__(self, name: str = "Doc") -> None:
        self.Name = name
        self.identity = f"identity-{name}"
        self.closed = False
        self.recompute_count = 0

    def recompute(self) -> None:
        self.recompute_count += 1


class FakeAnalysis:
    Name = "Analysis"
    TypeId = "Fem::FemAnalysis"

    def __init__(self) -> None:
        self.Group: list[Any] = []

    def isDerivedFrom(self, type_id: str) -> bool:
        return type_id == "Fem::FemAnalysis"

    def addObject(self, obj: Any) -> None:
        self.Group.append(obj)


class FakeSolver:
    Name = "SolverCalculiX"
    TypeId = "Fem::FemSolverObjectPython"

    def __init__(self, proxy_type: str = "Fem::SolverCalculiX") -> None:
        self.Proxy = types.SimpleNamespace(Type=proxy_type)
        self.Results: list[Any] = []
        self.WorkingDirectory = ""
        self.loaded_pipeline = FakePipeline()

    def isDerivedFrom(self, type_id: str) -> bool:
        return False


class FakePipeline:
    Name = "SolverCalculiXResult"

    def __init__(self) -> None:
        self.Data: Any = None

    def isDerivedFrom(self, type_id: str) -> bool:
        return type_id == "Fem::FemPostPipeline"


class FakeCtx:
    def __init__(self, doc: FakeDocument, allowed_root: Path) -> None:
        self.settings = {"allowed_roots": [str(allowed_root)]}
        self.active_solves: dict[str, Any] = {}
        self.cancel_event: threading.Event | None = None
        self.generation = 7
        self.finished_ops: list[Any] = []
        self.mutations: list[tuple[str, str]] = []
        self._doc = doc
        self._analysis = FakeAnalysis()

    def require_document(self, name: str) -> Any:
        if name != self._doc.Name:
            raise ToolError("DOCUMENT_NOT_FOUND", f"unknown document {name!r}")
        if self._doc.closed:
            raise ToolError("DOCUMENT_NOT_FOUND", "document is closed")
        return self._doc

    def require_object(self, doc: Any, name: str) -> Any:
        if name == FakeAnalysis.Name:
            return self._analysis
        raise ToolError("OBJECT_NOT_FOUND", f"unknown object {name!r}")

    def document_identity(self, doc: Any) -> str:
        if doc.closed:
            raise RuntimeError("document no longer available")
        return doc.identity

    def document_generation(self, doc: Any) -> int:
        return self.generation

    def operation_finished(self) -> None:
        self.finished_ops.append("finished")


@contextmanager
def fake_mutation(
    _ctx: Any, _doc: Any, label: str, _objects: Any, expected_solids: Any = None
) -> Iterator[list[str]]:
    _ctx.mutations.append((_doc.Name, label))
    yield ["solver"]


@contextmanager
def load_fem(
    *,
    checks_message: str = "",
    mesh_message: str = "",
    calculix_found: bool = True,
    has_frd_to_vtk: bool = True,
) -> Iterator[Any]:
    """Load tools/fem.py with the lazy FEM imports stubbed."""
    module_names = [
        "Fem",
        "femsolver",
        "femsolver.settings",
        "femsolver.calculix",
        "femsolver.calculix.calculixtools",
        "femtools",
        "femtools.checksanalysis",
        "femtools.membertools",
        "ObjectsFem",
        "PySide",
        "PySide.QtCore",
        "mcp_server.object_validation",
    ]
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in module_names}

    fem_module = types.ModuleType("Fem")
    if has_frd_to_vtk:
        fem_module.frdToVTK = lambda *args, **kwargs: None
    settings_module = types.ModuleType("femsolver.settings")
    settings_module.get_binary = lambda name, silent=False: (
        sys.executable if calculix_found else None
    )
    calculixtools = types.ModuleType("femsolver.calculix.calculixtools")
    calculixtools.CalculiXTools = FakeCalculiXToolsBase
    checksanalysis = types.ModuleType("femtools.checksanalysis")
    checksanalysis.check_member_for_solver_calculix = (
        lambda analysis, solver, mesh, member: checks_message
    )
    membertools = types.ModuleType("femtools.membertools")
    membertools.get_mesh_to_solve = lambda analysis: (FakeMesh(), mesh_message)
    membertools.AnalysisMember = lambda analysis: types.SimpleNamespace()
    objects_fem = types.ModuleType("ObjectsFem")
    objects_fem.makeSolverCalculiX = lambda doc, name="SolverCalculiX": FakeSolver()
    qt_core = types.ModuleType("PySide.QtCore")
    qt_core.QTimer = FakeQTimer
    object_validation = types.ModuleType("mcp_server.object_validation")
    object_validation.mutation = fake_mutation

    sys.modules["Fem"] = fem_module
    sys.modules["femsolver"] = types.ModuleType("femsolver")
    sys.modules["femsolver.settings"] = settings_module
    sys.modules["femsolver.calculix"] = types.ModuleType("femsolver.calculix")
    sys.modules["femsolver.calculix.calculixtools"] = calculixtools
    sys.modules["femtools"] = types.ModuleType("femtools")
    sys.modules["femtools.checksanalysis"] = checksanalysis
    sys.modules["femtools.membertools"] = membertools
    sys.modules["ObjectsFem"] = objects_fem
    sys.modules["PySide"] = types.ModuleType("PySide")
    sys.modules["PySide.QtCore"] = qt_core
    sys.modules["mcp_server.object_validation"] = object_validation

    module_name = f"_fem_test_{id(object())}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, FEM_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        FakeQTimer.instances = []
        yield module
    finally:
        sys.modules.pop(module_name, None)
        for name, value in saved.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class FakeMesh:
    Name = "Mesh"


def start_solve(
    fem: Any,
    ctx: FakeCtx,
    tmp_path: Path,
    *,
    analysis: FakeAnalysis | None = None,
    solver_in_group: bool = True,
) -> tuple[Any, FakeDocument, FakeAnalysis]:
    doc = ctx._doc
    if analysis is not None:
        ctx._analysis = analysis
    else:
        analysis = ctx._analysis
        if solver_in_group:
            analysis.Group = [FakeSolver()]
    result = fem.run_fem(ctx, {"document": doc.Name, "analysis": analysis.Name})
    assert isinstance(result, concurrent.futures.Future)
    return result, doc, analysis


def working_dir(tmp_path: Path) -> Path:
    dirs = [entry for entry in tmp_path.iterdir() if entry.is_dir()]
    assert len(dirs) == 1
    return dirs[0]


# ---------------------------------------------------------------------------
# Tool definition contract.
# ---------------------------------------------------------------------------


def test_tool_definition_is_finite_and_explicit() -> None:
    with load_fem() as fem:
        assert [definition["name"] for definition in fem.TOOL_DEFINITIONS] == [
            "run_fem"
        ]
        schema = fem.TOOL_DEFINITIONS[0]["inputSchema"]
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["document", "analysis"]
        assert schema["properties"]["timeout_s"]["default"] == 600
        assert schema["properties"]["timeout_s"]["minimum"] == 1
        assert schema["properties"]["timeout_s"]["maximum"] == 3600
        assert fem.HANDLERS["run_fem"] is fem.run_fem


# ---------------------------------------------------------------------------
# Handler gates.
# ---------------------------------------------------------------------------


def test_busy_when_a_solve_is_already_active(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        ctx.active_solves["identity-Doc"] = object()

        with pytest.raises(ToolError) as excinfo:
            fem.run_fem(ctx, {"document": "Doc", "analysis": "Analysis"})

        assert excinfo.value.code == "SERVER_BUSY"
        assert excinfo.value.details["document"] == "identity-Doc"


def test_missing_prerequisites_fail_before_any_mutation(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        saved = fem.availability
        try:
            fem.availability = lambda: {
                "available": False,
                "has_frd_to_vtk": False,
                "vtk_support": False,
                "calculix_binary": None,
                "reason": "CalculiX executable not found",
            }
            with pytest.raises(ToolError) as excinfo:
                fem.run_fem(ctx, {"document": "Doc", "analysis": "Analysis"})
        finally:
            fem.availability = saved

        assert excinfo.value.code == "SOLVER_FAILED"
        assert "CalculiX executable not found" in excinfo.value.message
        assert ctx.mutations == []  # failed before touching the document
        assert ctx.active_solves == {}


def test_missing_calculix_binary_is_a_prerequisite_failure(tmp_path: Path) -> None:
    with load_fem(calculix_found=False) as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        with pytest.raises(ToolError) as excinfo:
            fem.run_fem(ctx, {"document": "Doc", "analysis": "Analysis"})

    assert excinfo.value.code == "SOLVER_FAILED"
    assert "CalculiX executable not found" in excinfo.value.message
    assert excinfo.value.details["available"] is False


def test_missing_frd_to_vtk_is_a_prerequisite_failure(tmp_path: Path) -> None:
    with load_fem(has_frd_to_vtk=False) as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        with pytest.raises(ToolError) as excinfo:
            fem.run_fem(ctx, {"document": "Doc", "analysis": "Analysis"})

    assert excinfo.value.code == "SOLVER_FAILED"
    assert "frdToVTK" in excinfo.value.message


def test_non_analysis_object_is_rejected(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)

        with pytest.raises(ToolError) as excinfo:
            fem.run_fem(ctx, {"document": "Doc", "analysis": "Nope"})

        assert excinfo.value.code == "OBJECT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Solver selection.
# ---------------------------------------------------------------------------


def test_legacy_only_solvers_are_rejected_without_conversion(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        analysis = FakeAnalysis()
        analysis.Group = [
            FakeSolver("Fem::SolverCcxTools"),
            FakeSolver("Fem::SolverCcxTools"),
        ]
        ctx._analysis = analysis

        with pytest.raises(ToolError) as excinfo:
            fem.run_fem(ctx, {"document": "Doc", "analysis": "Analysis"})

        assert excinfo.value.code == "SOLVER_FAILED"
        assert "never converts or deletes" in excinfo.value.message
        assert excinfo.value.details["legacy_solvers"] == [
            "SolverCalculiX",
            "SolverCalculiX",
        ]
        assert ctx.mutations == []


def test_mixed_or_multiple_solvers_are_ambiguous(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        analysis = FakeAnalysis()
        analysis.Group = [FakeSolver(), FakeSolver()]
        ctx._analysis = analysis

        with pytest.raises(ToolError) as excinfo:
            fem.run_fem(ctx, {"document": "Doc", "analysis": "Analysis"})

        assert excinfo.value.code == "SOLVER_FAILED"
        assert "ambiguous" in excinfo.value.message


def test_missing_solver_creates_one_modern_solver_through_the_mutation_gate(
    tmp_path: Path,
) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        analysis = FakeAnalysis()
        analysis.Group = []

        future, _doc, _analysis = start_solve(fem, ctx, tmp_path, analysis=analysis)

        assert len(ctx.mutations) == 1
        assert ctx.mutations[0] == ("Doc", "run_fem:create-solver")
        assert len(analysis.Group) == 1
        assert analysis.Group[0].Proxy.Type == "Fem::SolverCalculiX"
        assert ctx.active_solves["identity-Doc"].tool.compute_called is True
        # No completion signal was emitted, so the Future must still be
        # pending. Checked without blocking — never an unbounded wait.
        assert not future.done()


def test_analysis_check_messages_are_honored(tmp_path: Path) -> None:
    with load_fem(
        checks_message="No material object defined in the analysis.\n"
    ) as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        ctx._analysis.Group = [FakeSolver()]  # existing modern solver, reused
        with pytest.raises(ToolError) as excinfo:
            fem.run_fem(ctx, {"document": "Doc", "analysis": "Analysis"})

        assert excinfo.value.code == "VALIDATION_FAILED"
        assert excinfo.value.details["checks"].startswith("No material")
        assert ctx.mutations == []  # existing solver reused, nothing created
        assert ctx.active_solves == {}


def test_multiple_meshes_are_reported_before_prepare(tmp_path: Path) -> None:
    with load_fem(
        mesh_message="FEM: multiple mesh in analysis not yet supported!"
    ) as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)

        with pytest.raises(ToolError) as excinfo:
            fem.run_fem(ctx, {"document": "Doc", "analysis": "Analysis"})

        assert excinfo.value.code == "VALIDATION_FAILED"
        assert "multiple mesh" in excinfo.value.details["checks"]


# ---------------------------------------------------------------------------
# Async lifecycle: guard before native loading, exactly-once finalizer.
# ---------------------------------------------------------------------------


def test_stale_generation_rejects_before_native_loading(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        future, _doc, analysis = start_solve(fem, ctx, tmp_path)
        operation = ctx.active_solves["identity-Doc"]
        tool = operation.tool
        op = operation
        assert tool.prepare_called and tool.compute_called
        solver = analysis.Group[0]

        ctx.generation = 8  # user edited the document during the solve
        tool.update_properties()

        assert tool.super_update_called is False
        assert solver.Results == []  # native loading never touched the doc
        error = future.exception()
        assert isinstance(error, ToolError)
        assert error.code == "VALIDATION_FAILED"
        assert "changed during the solve" in error.message
        assert error.details["expected_generation"] == 7
        assert error.details["actual_generation"] == 8
        assert ctx.active_solves == {}
        assert len(ctx.finished_ops) == 1


def test_changed_or_closed_identity_rejects_before_native_loading(
    tmp_path: Path,
) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        future, doc, _analysis = start_solve(fem, ctx, tmp_path)
        operation = ctx.active_solves["identity-Doc"]
        tool = operation.tool
        op = operation

        doc.identity = "identity-Doc2"
        tool.update_properties()

        assert tool.super_update_called is False
        error = future.exception()
        assert isinstance(error, ToolError)
        assert error.code == "VALIDATION_FAILED"
        assert "identity changed" in error.message
        assert ctx.active_solves == {}

        # A closed document (identity lookup fails) reports the same guard.
        ctx2 = FakeCtx(FakeDocument(), tmp_path)
        future2, doc2, _analysis2 = start_solve(fem, ctx2, tmp_path)
        operation2 = ctx2.active_solves["identity-Doc"]
        tool2 = operation2.tool
        op2 = operation2
        doc2.closed = True
        tool2.update_properties()

        error2 = future2.exception()
        assert isinstance(error2, ToolError)
        assert error2.code == "VALIDATION_FAILED"
        assert "no longer available" in error2.message


def test_successful_native_chain_finalizes_once_with_payload(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        future, _doc, analysis = start_solve(fem, ctx, tmp_path)
        solver = analysis.Group[0]
        operation = ctx.active_solves["identity-Doc"]
        tool = operation.tool
        op = operation

        # Fake the generated VTM/VTU artifacts inside the unique directory.
        work = working_dir(tmp_path)
        (work / "Result.vtm").write_text("<VTKFile/>")
        (work / "Result").mkdir()
        (work / "Result" / "Result_0.vtu").write_text("<VTKFile/>")

        # The full native chain: exit 0 -> native slot -> update_properties.
        tool.process.finished.emit(0, FakeQProcess.ExitStatus.NormalExit)

        assert tool.super_update_called is True
        assert solver.Results == [solver.loaded_pipeline]
        payload = future.result(timeout=1)
        assert payload["pipeline"] == "SolverCalculiXResult"
        assert payload["solver"] == "SolverCalculiX"
        assert payload["analysis"] == "Analysis"
        assert payload["working_dir"] == str(work)
        assert payload["vtk_path"].endswith("Result.vtm")
        assert payload["vtu_files"] == [str(work / "Result" / "Result_0.vtu")]
        assert payload["blocks"] == []
        assert payload["aggregates"] == {
            "block_count": 0,
            "point_count_sum": 0,
            "cell_count_sum": 0,
        }
        assert payload["cancellation_requested"] is False
        assert ctx.active_solves == {}
        assert len(ctx.finished_ops) == 1

        # Late failure/exit signals after completion cannot re-finalize.
        op._on_error(FakeQProcess.ProcessError.FailedToStart)
        op._on_finished(1, FakeQProcess.ExitStatus.CrashExit)
        assert len(ctx.finished_ops) == 1
        assert future.exception() is None
        assert solver.Results == [solver.loaded_pipeline]


def test_result_block_summary_reports_sums(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        future, _doc, _analysis = start_solve(fem, ctx, tmp_path)
        operation = ctx.active_solves["identity-Doc"]
        tool = operation.tool
        op = operation
        work = working_dir(tmp_path)
        (work / "Result.vtm").write_text("<VTKFile/>")

        class FakeArray:
            def __init__(self, result: Any) -> None:
                self.result = result

        class FakeFieldData:
            def __init__(self, arrays: list[tuple[str, Any]]) -> None:
                self._arrays = arrays

            def GetNumberOfArrays(self) -> int:
                return len(self._arrays)

            def GetArray(self, index: int) -> Any:
                return self._arrays[index][1]

            def GetArrayName(self, index: int) -> str:
                return self._arrays[index][0]

        class FakeGrid:
            def __init__(
                self, points: int, cells: int, arrays: list[tuple[str, Any]]
            ) -> None:
                self._points = points
                self._cells = cells
                self._arrays = arrays

            def GetNumberOfPoints(self) -> int:
                return self._points

            def GetNumberOfCells(self) -> int:
                return self._cells

            def GetPointData(self) -> Any:
                return FakeFieldData(self._arrays)

        class FakeMultiBlock:
            def __init__(self, children: list[Any]) -> None:
                self._children = children

            def GetNumberOfBlocks(self) -> int:
                return len(self._children)

            def GetBlock(self, index: int) -> Any:
                return self._children[index]

        grid_a = FakeGrid(
            200,
            96,
            [
                ("Displacement", FakeArray(("vector", (0.0, 10.0)))),
                ("von Mises Stress", FakeArray(("scalar", (0.0, 250.0)))),
                ("AllNaN", FakeArray(None)),
            ],
        )
        grid_b = FakeGrid(61, 12, [])
        pipeline = FakeSolver().loaded_pipeline
        pipeline.Data = FakeMultiBlock([None, FakeMultiBlock([grid_a, grid_b])])
        tool.obj.loaded_pipeline = pipeline

        saved_range = fem._array_range
        try:
            fem._array_range = staticmethod(  # type: ignore[assignment]
                lambda array: getattr(array, "result", None)
            )
            tool.process.finished.emit(0, FakeQProcess.ExitStatus.NormalExit)
        finally:
            fem._array_range = saved_range

        payload = future.result(timeout=1)
        assert [block["block"] for block in payload["blocks"]] == [0, 1]
        first = payload["blocks"][0]
        assert first["points"] == 200 and first["cells"] == 96
        assert first["vectors"] == {"Displacement": {"min": 0.0, "max": 10.0}}
        assert first["scalars"] == {"von Mises Stress": {"min": 0.0, "max": 250.0}}
        second = payload["blocks"][1]
        assert second["points"] == 61 and second["cells"] == 12
        assert second["scalars"] == {} and second["vectors"] == {}
        aggregates = payload["aggregates"]
        assert aggregates["block_count"] == 2
        assert aggregates["point_count_sum"] == 261  # labeled sums, not unique nodes
        assert aggregates["cell_count_sum"] == 108


def test_nonzero_exit_captures_process_diagnostics(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        future, _doc, _analysis = start_solve(fem, ctx, tmp_path)
        operation = ctx.active_solves["identity-Doc"]
        tool = operation.tool
        op = operation
        expected_dir = tool.obj.WorkingDirectory
        tool.process.stdout = "*ERROR in e_cub\n"
        tool.process.stderr = "ccx terminated\n"

        op._on_finished(1, FakeQProcess.ExitStatus.NormalExit)

        error = future.exception()
        assert isinstance(error, ToolError)
        assert error.code == "SOLVER_FAILED"
        assert "nonzero code 1" in error.message
        assert error.details["exit_code"] == 1
        assert error.details["exit_status"] == "NormalExit"
        assert error.details["stdout"] == "*ERROR in e_cub\n"
        assert error.details["stderr"] == "ccx terminated\n"
        assert error.details["working_dir"] == expected_dir
        assert ctx.active_solves == {}
        assert len(ctx.finished_ops) == 1


def test_crashed_process_is_reported(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        future, _doc, _analysis = start_solve(fem, ctx, tmp_path)
        operation = ctx.active_solves["identity-Doc"]
        tool = operation.tool
        op = operation

        op._on_finished(-1, FakeQProcess.ExitStatus.CrashExit)

        error = future.exception()
        assert isinstance(error, ToolError)
        assert error.code == "SOLVER_FAILED"
        assert "crashed" in error.message
        assert error.details["exit_status"] == "CrashExit"


def test_failed_to_start_finalizes_exactly_once(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        future, _doc, _analysis = start_solve(fem, ctx, tmp_path)
        operation = ctx.active_solves["identity-Doc"]
        tool = operation.tool
        op = operation

        op._on_error(FakeQProcess.ProcessError.FailedToStart)
        # QProcess also emits finished(-1, CrashExit) after FailedToStart.
        op._on_finished(-1, FakeQProcess.ExitStatus.CrashExit)

        error = future.exception()
        assert isinstance(error, ToolError)
        assert error.code == "SOLVER_FAILED"
        assert "failed to start" in error.message
        assert error.details["reason"] == "failed_to_start"
        assert len(ctx.finished_ops) == 1

        # Finished-path fallback when errorOccurred was missed entirely.
        ctx2 = FakeCtx(FakeDocument(), tmp_path)
        future2, _doc2, _analysis2 = start_solve(fem, ctx2, tmp_path)
        operation2 = ctx2.active_solves["identity-Doc"]
        tool2 = operation2.tool
        op2 = operation2
        tool2.process._error = FakeQProcess.ProcessError.FailedToStart
        op2._on_finished(-1, FakeQProcess.ExitStatus.CrashExit)

        error2 = future2.exception()
        assert isinstance(error2, ToolError)
        assert "failed to start" in error2.message


def test_exit_zero_without_a_completed_loader_is_not_success(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        future, _doc, _analysis = start_solve(fem, ctx, tmp_path)
        operation = ctx.active_solves["identity-Doc"]
        tool = operation.tool
        op = operation

        # Native update_properties never ran (loader failed before our slot).
        op._on_finished(0, FakeQProcess.ExitStatus.NormalExit)

        error = future.exception()
        assert isinstance(error, ToolError)
        assert error.code == "SOLVER_FAILED"
        assert "result loader did not complete" in error.message


def test_compute_failure_finalizes_and_clears_the_solve(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        analysis = FakeAnalysis()
        analysis.Group = [FakeSolver()]

        original_compute = FakeCalculiXToolsBase.compute

        def exploding_compute(self: Any) -> None:
            raise RuntimeError("QProcess start failed")

        FakeCalculiXToolsBase.compute = exploding_compute
        try:
            future, _doc, _analysis2 = start_solve(
                fem, ctx, tmp_path, analysis=analysis
            )
        finally:
            FakeCalculiXToolsBase.compute = original_compute

        error = future.exception()
        assert isinstance(error, ToolError)
        assert error.code == "SOLVER_FAILED"
        assert "starting the CalculiX process failed" in error.message
        assert ctx.active_solves == {}
        assert len(ctx.finished_ops) == 1


def test_prepare_failure_raises_before_registration(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)

        original_prepare = FakeCalculiXToolsBase.prepare

        def failing_prepare(self: Any) -> None:
            raise RuntimeError("mesh sets failed")

        FakeCalculiXToolsBase.prepare = failing_prepare
        try:
            with pytest.raises(ToolError) as excinfo:
                fem.run_fem(ctx, {"document": "Doc", "analysis": "Analysis"})
        finally:
            FakeCalculiXToolsBase.prepare = original_prepare

        assert excinfo.value.code == "SOLVER_FAILED"
        assert "preparing the CalculiX run failed" in excinfo.value.message
        assert ctx.active_solves == {}
        assert ctx.finished_ops == []


def test_cancel_event_marks_requested_without_touching_the_result(
    tmp_path: Path,
) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        cancel_event = threading.Event()
        ctx.cancel_event = cancel_event
        future, _doc, _analysis = start_solve(fem, ctx, tmp_path)
        operation = ctx.active_solves["identity-Doc"]
        tool = operation.tool
        op = operation
        work = working_dir(tmp_path)
        (work / "Result.vtm").write_text("<VTKFile/>")

        cancel_event.set()
        timer = FakeQTimer.instances[0]
        timer.timeout.emit()

        assert ctx.active_solves["identity-Doc"].cancel_requested is True
        assert timer.stopped is True  # watch stops; the solver keeps running

        tool.process.finished.emit(0, FakeQProcess.ExitStatus.NormalExit)

        payload = future.result(timeout=1)
        assert payload["cancellation_requested"] is True
        # Finished before cancellation could take effect: completed, with
        # the diagnostic — never a false cancelled state.
        assert future.exception() is None


def test_request_cancel_is_a_truthful_no_kill_boundary(tmp_path: Path) -> None:
    with load_fem() as fem:
        ctx = FakeCtx(FakeDocument(), tmp_path)
        future, _doc, _analysis = start_solve(fem, ctx, tmp_path)
        operation = ctx.active_solves["identity-Doc"]
        tool = operation.tool

        operation.request_cancel()

        assert operation.cancel_requested is True
        assert tool.compute_called is True  # process was started, never killed
        # The solve is still active until it actually completes.
        assert ctx.active_solves.get("identity-Doc") is operation
