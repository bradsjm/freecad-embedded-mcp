"""Focused tests for mcp_server/tools/sketch.py (inspect_sketch/edit_sketch).

Runs headless: FreeCAD, Part and Sketcher are installed as isolated stubs
while the module under test loads (same pattern as test_script.py and
test_mcp_objects.py). The shared mutation gate runs for real against fake
document doubles.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
SKETCH_PATH = ADDON_DIR / "mcp_server" / "tools" / "sketch.py"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.protocol import ToolError, check_schema

VALIDATION_FAILED = "VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# FreeCAD / Part / Sketcher stubs.
# ---------------------------------------------------------------------------


class StubVector:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = float(x), float(y), float(z)


class StubQuantity:
    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Quantity({self.text!r})"


class StubPoint:
    def __init__(self, vector: StubVector) -> None:
        self.x, self.y = vector.x, vector.y


class StubLineSegment:
    def __init__(self, start: StubVector, end: StubVector) -> None:
        self.StartPoint = start
        self.EndPoint = end


class StubCircle:
    def __init__(self, center: StubVector, axis: StubVector, radius: float) -> None:
        self.Center = center
        self.Axis = axis
        self.Radius = float(radius)


class StubArcOfCircle:
    def __init__(self, circle: StubCircle, start: float, end: float) -> None:
        self.Circle = circle
        self.FirstParameter = float(start)
        self.LastParameter = float(end)


class StubConstraint:
    _DIMENSIONAL = {
        "Angle",
        "Diameter",
        "Distance",
        "DistanceX",
        "DistanceY",
        "Radius",
        "SnellsLaw",
        "Weight",
    }

    def __init__(self, constraint_type: str, *arguments: Any) -> None:
        self.Type = constraint_type
        self.Arguments = arguments
        self.Value = None
        self.Name = ""
        self.IsDriving = True
        self.IsActive = True
        self.First = arguments[0] if len(arguments) > 0 else None
        self.FirstPos = arguments[1] if len(arguments) > 1 else None
        self.Second = arguments[2] if len(arguments) > 2 else None
        self.SecondPos = arguments[3] if len(arguments) > 3 else None
        self.Third = arguments[4] if len(arguments) > 4 else None
        self.ThirdPos = arguments[5] if len(arguments) > 5 else None
        if constraint_type in self._DIMENSIONAL and arguments:
            last = arguments[-1]
            if isinstance(last, StubQuantity):
                self.Value = float(last.text.split()[0])
            elif isinstance(last, (int, float)) and not isinstance(last, bool):
                self.Value = float(last)


_STUB_FREECAD = types.ModuleType("FreeCAD")
_STUB_FREECAD.Vector = StubVector


class _StubUnits:
    Quantity = staticmethod(lambda text: StubQuantity(text))


_STUB_FREECAD.Units = _StubUnits

_STUB_PART = types.ModuleType("Part")
_STUB_PART.Point = StubPoint
_STUB_PART.LineSegment = StubLineSegment
_STUB_PART.Circle = StubCircle
_STUB_PART.ArcOfCircle = StubArcOfCircle

_STUB_SKETCHER = types.ModuleType("Sketcher")
_STUB_SKETCHER.Constraint = StubConstraint


# ---------------------------------------------------------------------------
# Module loader.
# ---------------------------------------------------------------------------


@contextmanager
def load_sketch() -> Iterator[types.ModuleType]:
    saved = {
        name: sys.modules.get(name)
        for name in ("FreeCAD", "Part", "Sketcher", "mcp_server.tools.sketch")
    }
    # Load under the real package so the module's relative imports resolve.
    module_name = f"mcp_server.tools._sketch_test_{id(object())}"
    sys.modules["FreeCAD"] = _STUB_FREECAD
    sys.modules["Part"] = _STUB_PART
    sys.modules["Sketcher"] = _STUB_SKETCHER
    sys.modules.pop("mcp_server.tools.sketch", None)
    try:
        spec = importlib.util.spec_from_file_location(module_name, SKETCH_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# ---------------------------------------------------------------------------
# Fake Sketcher object and mutation-gate doubles.
# ---------------------------------------------------------------------------


class FakeSketch:
    def __init__(
        self,
        name: str = "Sketch",
        *,
        geometry: list[Any] | None = None,
        constraints: list[Any] | None = None,
        degrees_of_freedom: int | None = 0,
        with_methods: bool = True,
    ) -> None:
        self.Name = name
        self.Label = name
        self.TypeId = "Sketcher::SketchObject"
        self.State: list[str] = []
        self.InList: list[Any] = []
        self.Shape = None
        self.Geometry: list[Any] = list(geometry or [])
        self.Constraints: list[Any] = list(constraints or [])
        self.degrees_of_freedom = degrees_of_freedom
        self.ops: list[tuple] = []
        self.datums: dict[int, str] = {}
        self.expression_engine: list[tuple[str, str]] = []
        self.fail_set_datum: int | None = None
        if with_methods:
            self.addGeometry = self._add_geometry
            self.addConstraint = self._add_constraint
            self.delGeometry = self._del_geometry
            self.delConstraint = self._del_constraint
            self.setDatum = self._set_datum
        self.solve_calls = 0

    # Native-style methods (bound only when with_methods is true).
    def _add_geometry(self, geo: Any, construction: bool = False) -> int:
        geo.Construction = construction
        self.Geometry.append(geo)
        self.ops.append(("addGeometry", type(geo).__name__, construction))
        return len(self.Geometry) - 1

    def _add_constraint(self, constraint: Any) -> int:
        self.Constraints.append(constraint)
        self.ops.append(("addConstraint", constraint.Type, constraint.Arguments))
        return len(self.Constraints) - 1

    def _del_geometry(self, index: int) -> None:
        self.ops.append(("delGeometry", index))
        self.Geometry.pop(index)

    def _del_constraint(self, index: int) -> None:
        self.ops.append(("delConstraint", index))
        self.Constraints.pop(index)

    def _set_datum(self, index: int, datum: str) -> None:
        if self.fail_set_datum == index:
            raise RuntimeError(f"cannot set datum {index}")
        self.ops.append(("setDatum", index, datum))
        self.datums[index] = datum

    def isDerivedFrom(self, type_id: str) -> bool:
        return type_id == "Sketcher::SketchObject"

    def solve(self) -> int:
        self.solve_calls += 1
        return 0

    def getSolverDoF(self) -> int:
        return self.degrees_of_freedom


class NoSolverDoFSketch(FakeSketch):
    """Build variant without the native degree-of-freedom getter."""

    getSolverDoF = None  # type: ignore[assignment]


class FakeApp:
    def getActiveTransaction(self) -> None:
        return None


class FakeDoc:
    def __init__(self, sketch: FakeSketch) -> None:
        self.Name = "Doc"
        self.Objects = [sketch]
        self.UndoMode = 0
        self.HasPendingTransaction = False
        self.transactions: list[tuple] = []
        self.recompute_count = 0
        self.open_count = 0

    def openTransaction(self, label: str) -> None:
        self.open_count += 1
        self.transactions.append(("open", label))

    def commitTransaction(self) -> None:
        self.transactions.append(("commit",))

    def abortTransaction(self) -> None:
        self.transactions.append(("abort",))

    def recompute(self) -> None:
        self.recompute_count += 1

    def getObject(self, name: str) -> Any:
        for obj in self.Objects:
            if obj.Name == name:
                return obj
        return None


class FakeCtx:
    def __init__(self, doc: FakeDoc) -> None:
        self.App = FakeApp()
        self._doc = doc

    def document_generation(self, doc: FakeDoc) -> int:
        return 1

    def document_identity(self, doc: FakeDoc) -> str:
        return "identity"

    def require_document(self, name: str) -> FakeDoc:
        if name != self._doc.Name:
            raise ToolError("DOCUMENT_NOT_FOUND", f"unknown document {name!r}")
        return self._doc

    def require_object(self, doc: FakeDoc, name: str) -> Any:
        found = doc.getObject(name)
        if found is None:
            raise ToolError("OBJECT_NOT_FOUND", f"unknown object {name!r}")
        return found

    def check_document_idle(self, doc: FakeDoc) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures and helpers.
# ---------------------------------------------------------------------------


@pytest.fixture()
def sketch_module():
    with load_sketch() as module:
        yield module


def line(x1: float, y1: float, x2: float, y2: float) -> StubLineSegment:
    return StubLineSegment(StubVector(x1, y1), StubVector(x2, y2))


def rectangle_sketch() -> FakeSketch:
    return FakeSketch(
        geometry=[
            line(0, 0, 10, 0),
            line(10, 0, 10, 10),
            line(10, 10, 0, 10),
            line(0, 10, 0, 0),
        ]
    )


def call_inspect(module: types.ModuleType, ctx: FakeCtx, sketch: str = "Sketch"):
    return module.HANDLERS["inspect_sketch"](ctx, {"document": "Doc", "sketch": sketch})


def call_edit(module: types.ModuleType, ctx: FakeCtx, **operations: Any):
    arguments: dict[str, Any] = {"document": "Doc", "sketch": "Sketch"}
    arguments.update(operations)
    return module.HANDLERS["edit_sketch"](ctx, arguments)


# ---------------------------------------------------------------------------
# Registration sanity.
# ---------------------------------------------------------------------------


def test_definitions_are_finite_and_handlers_registered(sketch_module) -> None:
    assert [definition["name"] for definition in sketch_module.TOOL_DEFINITIONS] == [
        "inspect_sketch",
        "edit_sketch",
    ]
    for definition in sketch_module.TOOL_DEFINITIONS:
        check_schema(definition["inputSchema"])
        check_schema(definition["outputSchema"])
    assert sorted(sketch_module.HANDLERS) == ["edit_sketch", "inspect_sketch"]


# ---------------------------------------------------------------------------
# inspect_sketch.
# ---------------------------------------------------------------------------


def test_inspect_reports_geometry_kinds_in_native_order(sketch_module) -> None:
    class UnsupportedGeometry:
        pass

    unsupported = UnsupportedGeometry()
    sketch = FakeSketch(
        geometry=[
            StubPoint(StubVector(1.0, 2.0)),
            line(0, 0, 4, 4),
            StubCircle(StubVector(0, 0), StubVector(0, 0, 1), 3.0),
            StubArcOfCircle(StubCircle(StubVector(0, 0), StubVector(0, 0, 1), 5.0), 0.0, 1.5),
            unsupported,
        ]
    )
    sketch.Geometry[4].Construction = True
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_inspect(sketch_module, ctx)

    kinds = [row["kind"] for row in result["geometry"]]
    assert kinds == [
        "point",
        "lineSegment",
        "circle",
        "arcOfCircle",
        "unsupported",
    ]
    assert result["geometry"][0] == {
        "index": 0,
        "kind": "point",
        "x": 1.0,
        "y": 2.0,
        "construction": False,
    }
    assert result["geometry"][1] == {
        "index": 1,
        "kind": "lineSegment",
        "startX": 0.0,
        "startY": 0.0,
        "endX": 4.0,
        "endY": 4.0,
        "construction": False,
    }
    assert result["geometry"][2]["radius"] == 3.0
    assert result["geometry"][3]["startAngle"] == 0.0
    assert result["geometry"][3]["endAngle"] == 1.5
    assert result["geometry"][4] == {
        "index": 4,
        "kind": "unsupported",
        "type": "UnsupportedGeometry",
    }


def test_inspect_reports_constraints_datum_and_bindings(sketch_module) -> None:
    sketch = rectangle_sketch()
    coincident = StubConstraint("Coincident", 0, 2, 1, 1)
    distance = StubConstraint("DistanceX", 0, 1, 1, 10.0)
    distance.Name = "width"
    sketch.Constraints = [coincident, distance]
    sketch.ExpressionEngine = [(".Constraints.width", "BaseWidth")]
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_inspect(sketch_module, ctx)

    first, second = result["constraints"]
    assert first["type"] == "Coincident"
    assert first["first"] == 0
    assert first["firstPos"] == 2
    assert first["second"] == 1
    assert first["secondPos"] == 1
    assert first["datum"] is None
    assert first["driving"] is True
    assert second["name"] == "width"
    assert second["datum"] == "10.0 mm"
    assert result["expressionBindings"] == [{"constraint": "width", "expression": "BaseWidth"}]


def test_inspect_solver_summary_from_solve_and_get_solver_dof(sketch_module) -> None:
    sketch = rectangle_sketch()
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_inspect(sketch_module, ctx)

    assert result["solver"] == {
        "fullyConstrained": True,
        "degreesOfFreedom": 0,
        "solverMessages": [],
    }
    assert sketch.solve_calls == 1


def test_inspect_solver_summary_is_null_without_native_getters(sketch_module) -> None:
    sketch = NoSolverDoFSketch(geometry=rectangle_sketch().Geometry)
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_inspect(sketch_module, ctx)

    assert result["solver"] == {
        "fullyConstrained": None,
        "degreesOfFreedom": None,
        "solverMessages": [],
    }


def test_inspect_rejects_non_sketch_objects(sketch_module) -> None:
    box = types.SimpleNamespace(Name="Box", TypeId="Part::Box")
    doc = FakeDoc(FakeSketch())
    doc.Objects.append(box)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_inspect(sketch_module, ctx, sketch="Box")

    assert excinfo.value.code == VALIDATION_FAILED
    assert "Sketcher::SketchObject" in excinfo.value.message


# ---------------------------------------------------------------------------
# edit_sketch.
# ---------------------------------------------------------------------------


CONSTRAINT_OPS = [
    {"type": "Coincident", "arguments": [0, 2, 1, 1]},
    {"type": "Coincident", "arguments": [1, 2, 2, 1]},
    {"type": "Coincident", "arguments": [2, 2, 3, 1]},
    {"type": "Coincident", "arguments": [3, 2, 0, 1]},
    {"type": "Horizontal", "arguments": [0]},
    {"type": "Horizontal", "arguments": [2]},
    {"type": "Vertical", "arguments": [1]},
    {"type": "Vertical", "arguments": [3]},
    {"type": "DistanceX", "arguments": [0, 1, 1], "datum": "10 mm"},
    {"type": "DistanceY", "arguments": [1, 2, 2], "datum": "10 mm"},
]


def test_rectangle_batch_reports_indexes_and_zero_dof(sketch_module) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    result = call_edit(
        sketch_module,
        ctx,
        addGeometry=[],
        addConstraints=CONSTRAINT_OPS,
    )

    assert result["addedGeometry"] == []
    assert result["addedConstraints"] == list(range(0, 10))
    assert result["solver"]["degreesOfFreedom"] == 0
    assert result["solver"]["fullyConstrained"] is True
    assert doc.open_count == 1
    assert doc.recompute_count == 1
    assert doc.transactions[-1] == ("commit",)


def test_add_geometry_supports_all_four_kinds(sketch_module) -> None:
    sketch = FakeSketch()
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_edit(
        sketch_module,
        ctx,
        addGeometry=[
            {"kind": "point", "x": 1.0, "y": 2.0},
            {"kind": "lineSegment", "start": [0.0, 0.0], "end": [10.0, 0.0]},
            {"kind": "circle", "center": [5.0, 5.0], "radius": 2.0},
            {
                "kind": "arcOfCircle",
                "center": [0.0, 0.0],
                "radius": 5.0,
                "startAngle": 0.0,
                "endAngle": 1.5,
                "construction": True,
            },
        ],
    )

    assert result["addedGeometry"] == [0, 1, 2, 3]
    kinds = [type(entry).__name__ for entry in sketch.Geometry]
    assert kinds == [
        "StubPoint",
        "StubLineSegment",
        "StubCircle",
        "StubArcOfCircle",
    ]
    assert sketch.Geometry[3].Construction is True
    assert isinstance(sketch.Geometry[0], StubPoint)


def test_deletes_apply_in_descending_index_order(sketch_module) -> None:
    sketch = rectangle_sketch()
    sketch.Constraints = [StubConstraint("Coincident", 0, 2, 1, 1) for _ in range(4)]
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_edit(
        sketch_module,
        ctx,
        deleteGeometry=[3, 0],
        deleteConstraints=[2, 1],
    )

    assert result["deletedGeometry"] == [3, 0]
    assert result["deletedConstraints"] == [2, 1]
    assert [op for op in sketch.ops if op[0] == "delGeometry"] == [
        ("delGeometry", 3),
        ("delGeometry", 0),
    ]
    assert [op for op in sketch.ops if op[0] == "delConstraint"] == [
        ("delConstraint", 2),
        ("delConstraint", 1),
    ]


def test_set_datums_run_after_additions_with_final_indexes(sketch_module) -> None:
    sketch = FakeSketch(geometry=[line(0, 0, 10, 0)])
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_edit(
        sketch_module,
        ctx,
        addConstraints=[{"type": "DistanceX", "arguments": [0, 1, 1], "datum": "10 mm"}],
        setDatums=[{"index": 0, "datum": "12 mm"}],
    )

    assert result["addedConstraints"] == [0]
    assert result["changedDatums"] == [{"index": 0, "datum": "12 mm"}]
    assert sketch.datums == {0: "12 mm"}
    assert ("setDatum", 0, "12 mm") in sketch.ops


def test_constraint_datum_reaches_native_constraint_as_quantity(
    sketch_module,
) -> None:
    sketch = FakeSketch()
    ctx = FakeCtx(FakeDoc(sketch))

    call_edit(
        sketch_module,
        ctx,
        addConstraints=[{"type": "Angle", "arguments": [0, 1], "datum": "45 deg"}],
    )

    constraint = sketch.Constraints[0]
    assert constraint.Type == "Angle"
    assert isinstance(constraint.Arguments[-1], StubQuantity)
    assert constraint.Arguments[-1].text == "45 deg"


def test_bad_index_never_opens_the_transaction(sketch_module) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(sketch_module, ctx, deleteGeometry=[9])

    assert excinfo.value.code == VALIDATION_FAILED
    assert doc.transactions == []
    assert doc.open_count == 0
    assert doc.recompute_count == 0


def test_datum_edits_validate_against_the_final_constraint_state(
    sketch_module,
) -> None:
    sketch = FakeSketch()
    sketch.Constraints = [StubConstraint("Radius", 0) for _ in range(2)]
    ctx = FakeCtx(FakeDoc(sketch))

    # Two deletions and two additions leave the count at two, so index 1
    # exists in the final state while index 2 does not.
    call_edit(
        sketch_module,
        ctx,
        deleteConstraints=[0, 1],
        addConstraints=[
            {"type": "Radius", "arguments": [0]},
            {"type": "Radius", "arguments": [1]},
        ],
        setDatums=[{"index": 1, "datum": "3 mm"}],
    )
    assert sketch.datums == {1: "3 mm"}

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            deleteConstraints=[0, 1],
            addConstraints=[
                {"type": "Radius", "arguments": [0]},
                {"type": "Radius", "arguments": [1]},
            ],
            setDatums=[{"index": 2, "datum": "3 mm"}],
        )
    assert excinfo.value.code == VALIDATION_FAILED


def test_missing_native_method_is_rejected_before_the_transaction(
    sketch_module,
) -> None:
    sketch = rectangle_sketch()
    del sketch.addConstraint  # type: ignore[attr-defined]
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addConstraints=[{"type": "Horizontal", "arguments": [0]}],
        )

    assert excinfo.value.code == VALIDATION_FAILED
    assert "addConstraint" in excinfo.value.message
    assert doc.transactions == []


def test_empty_operation_batch_is_rejected(sketch_module) -> None:
    sketch = rectangle_sketch()
    ctx = FakeCtx(FakeDoc(sketch))

    with pytest.raises(ToolError) as excinfo:
        call_edit(sketch_module, ctx)

    assert excinfo.value.code == VALIDATION_FAILED
    assert "at least one operation" in excinfo.value.message


def test_mid_batch_failure_rolls_the_batch_back(sketch_module) -> None:
    sketch = FakeSketch()
    sketch.Constraints = [StubConstraint("DistanceX", 0, 1, 1)]
    sketch.fail_set_datum = 0
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            setDatums=[{"index": 0, "datum": "12 mm"}],
        )

    assert excinfo.value.code == VALIDATION_FAILED
    assert (excinfo.value.details or {}).get("operationState") == "rolled_back"
    assert "abort" in [transaction[0] for transaction in doc.transactions]
    assert sketch.datums == {}


def test_invalid_datum_strings_are_rejected(sketch_module) -> None:
    sketch = FakeSketch()
    ctx = FakeCtx(FakeDoc(sketch))

    for bad_datum in ("", "mm", "ten mm"):
        with pytest.raises(ToolError) as excinfo:
            call_edit(
                sketch_module,
                ctx,
                addConstraints=[
                    {
                        "type": "DistanceX",
                        "arguments": [0, 1, 1],
                        "datum": bad_datum,
                    }
                ],
            )
        assert excinfo.value.code == VALIDATION_FAILED


def test_unknown_constraint_type_is_rejected(sketch_module) -> None:
    sketch = FakeSketch()
    ctx = FakeCtx(FakeDoc(sketch))

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addConstraints=[{"type": "Magic", "arguments": [0]}],
        )

    assert excinfo.value.code == VALIDATION_FAILED
    assert "documented Sketcher constraint types" in excinfo.value.message
