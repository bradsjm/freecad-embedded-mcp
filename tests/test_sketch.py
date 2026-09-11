"""Focused tests for mcp_server/tools/sketch.py (inspect_sketch/edit_sketch).

Runs headless: FreeCAD, Part and Sketcher are installed as isolated stubs
while the module under test loads (same pattern as test_script.py and
test_mcp_objects.py). The shared mutation gate runs for real against fake
document doubles.
"""

from __future__ import annotations

import importlib.util
import math
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

from mcp_server.protocol import ProtocolError, ToolError, tool_error_result, validate_schema

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
        # Native arcs expose endpoints too; the arc must still be
        # classified first (probes["geometry.attributes"], ArcOfCircle).
        self.StartPoint = StubVector(
            circle.Center.x + circle.Radius * math.cos(start),
            circle.Center.y + circle.Radius * math.sin(start),
        )
        self.EndPoint = StubVector(
            circle.Center.x + circle.Radius * math.cos(end),
            circle.Center.y + circle.Radius * math.sin(end),
        )


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
        self.Driving = True
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
        solver_status: Any = 0,
        state: list[Any] | None = None,
        status_text: str = "",
        with_methods: bool = True,
    ) -> None:
        self.Name = name
        self.Label = name
        self.TypeId = "Sketcher::SketchObject"
        self.State: Any = list(state or [])
        self.InList: list[Any] = []
        self.Shape = None
        self.Geometry: list[Any] = list(geometry or [])
        self.Constraints: list[Any] = list(constraints or [])
        self.degrees_of_freedom = degrees_of_freedom
        # Native attribute names; solve() returns a status code, the DoF
        # attribute carries the degree count (probes["solver.attributes"]).
        self.DoF = degrees_of_freedom
        self.FullyConstrained = degrees_of_freedom == 0 if degrees_of_freedom is not None else None
        self.ops: list[tuple] = []
        self.datums: dict[int, str] = {}
        self.construction_flags: dict[int, bool] = {}
        self.expression_engine: list[tuple[str, str]] = []
        self.fail_set_datum: int | None = None
        self.solver_status = solver_status
        self.status_text = status_text
        if with_methods:
            self.addGeometry = self._add_geometry
            self.addConstraint = self._add_constraint
            self.delGeometry = self._del_geometry
            self.delConstraint = self._del_constraint
            self.setDatum = self._set_datum
            self.getStatusString = self._status_string
        self.solve_calls = 0

    # Native-style methods (bound only when with_methods is true).
    def _add_geometry(self, geo: Any, construction: bool = False) -> int:
        # Native addGeometry sets no Construction attribute on the
        # element; the state lives in getConstruction
        # (probes["geometry.getConstruction"]).
        self.construction_flags[len(self.Geometry)] = construction
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

    def _set_datum(self, index: int, datum: Any) -> None:
        # Native setDatum fails with exactly this error for a bare
        # string (probes["setDatum.string"]); the edit path must hand it
        # a quantity.
        if isinstance(datum, str):
            raise TypeError("Wrong arguments")
        if self.fail_set_datum == index:
            raise RuntimeError(f"cannot set datum {index}")
        self.ops.append(("setDatum", index, getattr(datum, "text", datum)))
        self.datums[index] = getattr(datum, "text", str(datum))

    def isDerivedFrom(self, type_id: str) -> bool:
        return type_id == "Sketcher::SketchObject"

    def solve(self) -> Any:
        # Solver status code, never a degree count
        # (probes["solver.attributes"]).
        self.solve_calls += 1
        return self.solver_status

    def _status_string(self) -> str:
        return self.status_text

    def getConstruction(self, index: int) -> bool:
        return self.construction_flags.get(index, False)


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
    def __init__(self, doc: FakeDoc, *, generation: int = 1) -> None:
        self.App = FakeApp()
        self._doc = doc
        self.generation = generation

    def document_generation(self, doc: FakeDoc) -> int:
        return self.generation

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


def rectangle_sketch(**overrides: Any) -> FakeSketch:
    return FakeSketch(
        geometry=[
            line(0, 0, 10, 0),
            line(10, 0, 10, 10),
            line(10, 10, 0, 10),
            line(0, 10, 0, 0),
        ],
        **overrides,
    )


def call_inspect(module: types.ModuleType, ctx: FakeCtx, sketch: str = "Sketch"):
    return module.HANDLERS["inspect_sketch"](ctx, {"document": "Doc", "sketch": sketch})


def call_edit(module: types.ModuleType, ctx: FakeCtx, **operations: Any):
    arguments: dict[str, Any] = {"document": "Doc", "sketch": "Sketch"}
    arguments.update(operations)
    return module.HANDLERS["edit_sketch"](ctx, arguments)


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
    sketch.construction_flags[1] = True  # native: getConstruction(index)
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
        "construction": True,
    }
    assert result["geometry"][2]["radius"] == 3.0
    assert result["geometry"][3]["startAngle"] == 0.0
    assert result["geometry"][3]["endAngle"] == 1.5
    assert result["geometry"][3]["radius"] == 5.0
    assert result["geometry"][4] == {
        "index": 4,
        "kind": "unsupported",
        "type": "UnsupportedGeometry",
    }
    assert result["geometryCount"] == len(result["geometry"])
    assert result["constraintsCount"] == len(result["constraints"])
    assert result["expressionBindingsCount"] == len(result["expressionBindings"])
    assert result["geometryTruncated"] is False
    assert result["constraintsTruncated"] is False
    assert result["expressionBindingsTruncated"] is False


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
    assert result["geometryCount"] == len(result["geometry"])
    assert result["constraintsCount"] == len(result["constraints"])
    assert result["expressionBindingsCount"] == len(result["expressionBindings"])
    assert result["geometryTruncated"] is False
    assert result["constraintsTruncated"] is False
    assert result["expressionBindingsTruncated"] is False


def test_inspect_solver_summary_reads_dof_attributes(sketch_module) -> None:
    sketch = rectangle_sketch()
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_inspect(sketch_module, ctx)

    assert result["solver"] == {
        "fullyConstrained": True,
        "degreesOfFreedom": 0,
        "solverMessages": [],
        "solverStatus": 0,
    }
    assert sketch.solve_calls == 1


def test_inspect_solver_summary_is_null_when_dof_attribute_is_missing(
    sketch_module,
) -> None:
    # Build variant: a null DoF attribute with no getter reports nulls
    # instead of failing the page.
    sketch = rectangle_sketch()
    sketch.DoF = None
    sketch.FullyConstrained = None
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_inspect(sketch_module, ctx)

    assert result["solver"] == {
        "fullyConstrained": None,
        "degreesOfFreedom": None,
        "solverMessages": [],
        "solverStatus": 0,
    }


def test_inspect_solver_summary_uses_getter_fallback_not_the_solve_value(
    sketch_module,
) -> None:
    # Build variant: a build with getSolverDoF but a null DoF attribute
    # falls back to the getter. The recorded fixture (probes
    # ["solver.attributes"]) returned solve() == 0 with DoF == 4: the
    # status code is never read as the degree count.
    sketch = rectangle_sketch()
    sketch.DoF = None
    sketch.FullyConstrained = None
    sketch.getSolverDoF = lambda: 4
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_inspect(sketch_module, ctx)

    assert result["solver"] == {
        "fullyConstrained": False,
        "degreesOfFreedom": 4,
        "solverMessages": [],
        "solverStatus": 0,
    }


def test_inspect_solver_summary_reports_the_solve_status_code(sketch_module) -> None:
    # The recorded conflict case returned solve() == -3; the code is a
    # diagnostic, so it is reported as-is.
    sketch = rectangle_sketch()
    sketch.solver_status = -3
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_inspect(sketch_module, ctx)

    assert result["solver"]["solverStatus"] == -3
    assert sketch.solve_calls == 1


def test_inspect_solver_status_is_null_when_solve_is_unusable(sketch_module) -> None:
    def boom() -> int:
        raise RuntimeError("solver unavailable")

    for unusable in (None, boom, lambda: "0", lambda: True):
        sketch = rectangle_sketch()
        sketch.solve = unusable
        ctx = FakeCtx(FakeDoc(sketch))

        result = call_inspect(sketch_module, ctx)

        assert result["solver"]["solverStatus"] is None
        # The rest of the summary survives a missing or failing solver.
        assert result["solver"]["degreesOfFreedom"] == 0


def test_inspect_reports_object_state_and_status_text(sketch_module) -> None:
    sketch = rectangle_sketch(
        state=["Touched", "Invalid"],
        status_text="Under-constrained: 3 DoF",
    )
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_inspect(sketch_module, ctx)

    assert result["state"] == ["Touched", "Invalid"]
    assert result["statusText"] == "Under-constrained: 3 DoF"


def test_inspect_status_text_distinguishes_empty_from_absent(sketch_module) -> None:
    # A working accessor that reports nothing is distinct from an absent
    # one: the empty string is a successful read, null is the fallback.
    quiet = rectangle_sketch()

    assert call_inspect(sketch_module, FakeCtx(FakeDoc(quiet)))["statusText"] == ""


def test_inspect_state_entries_are_coerced_and_capped(sketch_module) -> None:
    capped = rectangle_sketch(state=[f"State{index}" for index in range(40)])
    assert call_inspect(sketch_module, FakeCtx(FakeDoc(capped)))["state"] == [
        f"State{index}" for index in range(32)
    ]

    coerced = rectangle_sketch(state=[7])
    assert call_inspect(sketch_module, FakeCtx(FakeDoc(coerced)))["state"] == ["7"]


def test_inspect_state_and_status_text_fall_back_when_unusable(sketch_module) -> None:
    for unusable in (None, "Invalid", 7):
        sketch = rectangle_sketch()
        sketch.State = unusable
        ctx = FakeCtx(FakeDoc(sketch))

        assert call_inspect(sketch_module, ctx)["state"] == []

    for unusable_status in (None, lambda: 7):
        sketch = rectangle_sketch()
        sketch.getStatusString = unusable_status
        ctx = FakeCtx(FakeDoc(sketch))

        assert call_inspect(sketch_module, ctx)["statusText"] is None


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
    # The recorded datum-ful horizontal/vertical forms are the four-token
    # ones; probes["constraint.forms"] rejected a datum appended to the
    # two-token form.
    {"type": "DistanceX", "arguments": [0, 1, 3, 2], "datum": "10 mm"},
    {"type": "DistanceY", "arguments": [1, 2, 2, 1], "datum": "10 mm"},
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


def test_edit_response_carries_solver_status_state_and_status_text(
    sketch_module,
) -> None:
    sketch = rectangle_sketch(
        solver_status=-3,
        # "Up-to-date" is a healthy state (test_object_validation.py); a
        # failed state would roll the batch back before the response.
        state=["Up-to-date"],
        status_text="Not solved",
    )
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_edit(sketch_module, ctx, addConstraints=CONSTRAINT_OPS)

    assert result["solver"]["solverStatus"] == -3
    assert result["state"] == ["Up-to-date"]
    assert result["statusText"] == "Not solved"


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
    assert sketch.getConstruction(3) is True
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
        addConstraints=[{"type": "DistanceX", "arguments": [0, 1, 3, 2], "datum": "10 mm"}],
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


def test_stale_expected_generation_is_rejected_before_any_mutation(
    sketch_module,
) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc, generation=5)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            expected_generation=4,
            deleteGeometry=[3],
            addConstraints=[{"type": "Horizontal", "arguments": [0]}],
        )

    assert excinfo.value.code == VALIDATION_FAILED
    assert excinfo.value.message == "sketch changed since inspection; re-run inspect_sketch"
    assert excinfo.value.details == {
        "reason": "stale_generation",
        "expectedGeneration": 4,
        "actualGeneration": 5,
        "nextTool": "inspect_sketch",
    }
    # The valid batch never reaches the native methods or the transaction.
    assert sketch.ops == []
    assert doc.transactions == []
    assert doc.open_count == 0
    assert doc.recompute_count == 0
    rendered = tool_error_result(excinfo.value)
    assert rendered["isError"] is True
    assert rendered["structuredContent"]["error"]["code"] == VALIDATION_FAILED


def test_matching_expected_generation_proceeds(sketch_module) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc, generation=5)

    result = call_edit(sketch_module, ctx, expected_generation=5, deleteGeometry=[3])

    assert result["deletedGeometry"] == [3]
    assert sketch.ops == [("delGeometry", 3)]
    assert doc.open_count == 1
    assert doc.transactions[-1] == ("commit",)


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
            {"type": "Radius", "arguments": [0], "datum": "3 mm"},
            {"type": "Radius", "arguments": [1], "datum": "3 mm"},
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
                {"type": "Radius", "arguments": [0], "datum": "3 mm"},
                {"type": "Radius", "arguments": [1], "datum": "3 mm"},
            ],
            setDatums=[{"index": 2, "datum": "3 mm"}],
        )
    assert excinfo.value.code == VALIDATION_FAILED


def test_expression_on_the_new_datum_index_is_refused(sketch_module) -> None:
    # The one-index rule covers datums carried by addConstraints entries,
    # not only setDatums: the created constraint's final index cannot
    # receive both in one batch (skills/references/sketcher.md).
    sketch = rectangle_sketch()
    sketch.setExpression = lambda path, expression: sketch.expression_engine.append(
        (path, expression)
    )
    ctx = FakeCtx(FakeDoc(sketch))

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addConstraints=[{"type": "DistanceX", "arguments": [0, 1, 3, 2], "datum": "10 mm"}],
            setExpressions=[{"index": 0, "expression": "Width"}],
        )

    assert "cannot receive both" in excinfo.value.message
    assert sketch.ops == []
    assert sketch.expression_engine == []


def test_expression_on_another_index_plans_with_a_new_datum_constraint(
    sketch_module,
) -> None:
    # Exclusivity is per index: a datum-carrying addition plus an
    # expression bound to a different constraint is one valid batch.
    sketch = rectangle_sketch()
    sketch.Constraints = [StubConstraint("DistanceX", 0, 1, 1)]
    sketch.setExpression = lambda path, expression: sketch.expression_engine.append(
        (path, expression)
    )
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_edit(
        sketch_module,
        ctx,
        addConstraints=[{"type": "DistanceX", "arguments": [0, 1, 3, 2], "datum": "10 mm"}],
        setExpressions=[{"index": 0, "expression": "Width"}],
    )

    assert result["addedConstraints"] == [1]
    assert sketch.expression_engine == [("Constraints[0]", "Width")]


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
                        "arguments": [0, 1],
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
    assert "accepted Sketcher constraint types" in excinfo.value.message
    # An unrecorded type and an unrecorded arity are the same refusal:
    # one shape gate, not two unrelated messages.
    assert excinfo.value.details == {
        "reason": "unrecorded_constraint_shape",
        "type": "Magic",
        "argumentCount": 1,
        "acceptedArgumentCounts": None,
        "nextTool": "inspect_sketch",
    }
    assert sketch.ops == []


def test_unrecorded_constraint_arities_are_rejected_before_any_native_call(
    sketch_module,
) -> None:
    # probes["constraint.forms"]: DistanceX recorded 2 and 4 arguments,
    # Coincident 4 and nothing else. An arity outside the recorded map is
    # refused before planning reaches the native constructor, which the
    # recorded crash shows can abort the process on unclassified input.
    for unrecorded, accepted in (
        ({"type": "DistanceX", "arguments": [0]}, [2, 4]),
        ({"type": "Coincident", "arguments": [0, 2]}, [4]),
    ):
        sketch = rectangle_sketch()
        doc = FakeDoc(sketch)
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call_edit(sketch_module, ctx, addConstraints=[unrecorded])

        assert excinfo.value.code == VALIDATION_FAILED
        assert unrecorded["type"] in excinfo.value.message
        assert str(len(unrecorded["arguments"])) in excinfo.value.message
        assert excinfo.value.details == {
            "reason": "unrecorded_constraint_shape",
            "type": unrecorded["type"],
            "argumentCount": len(unrecorded["arguments"]),
            "acceptedArgumentCounts": accepted,
            "nextTool": "inspect_sketch",
        }
        assert sketch.ops == []
        assert doc.transactions == []
        assert doc.open_count == 0
        assert doc.recompute_count == 0


def test_unrecorded_constraint_type_reports_no_accepted_argument_counts(
    sketch_module,
) -> None:
    # Weight has no recorded form at any arity, so there is no accepted
    # argument count to report: the detail is null, not an empty list.
    sketch = FakeSketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addConstraints=[{"type": "Weight", "arguments": [5, 1]}],
        )

    assert excinfo.value.code == VALIDATION_FAILED
    assert excinfo.value.details == {
        "reason": "unrecorded_constraint_shape",
        "type": "Weight",
        "argumentCount": 2,
        "acceptedArgumentCounts": None,
        "nextTool": "inspect_sketch",
    }
    assert sketch.ops == []
    assert doc.transactions == []
    assert doc.open_count == 0
    error = tool_error_result(excinfo.value)["structuredContent"]["error"]
    assert error["code"] == VALIDATION_FAILED
    assert error["details"]["reason"] == "unrecorded_constraint_shape"


def test_recorded_constraint_shapes_plan_and_execute(sketch_module) -> None:
    # Recorded shapes reach addConstraint unchanged, with a datum
    # (probes["constraint.forms"] DistanceX:[0, 1] carrying a quantity)
    # and without one (DistanceX:[0, 1] and Horizontal:[0]).
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    result = call_edit(
        sketch_module,
        ctx,
        addConstraints=[
            {"type": "DistanceX", "arguments": [0, 1, 3, 2], "datum": "40 mm"},
            {"type": "DistanceX", "arguments": [2, 1]},
            {"type": "Horizontal", "arguments": [0]},
        ],
    )

    assert result["addedConstraints"] == [0, 1, 2]
    assert [op[0] for op in sketch.ops] == ["addConstraint"] * 3
    dimensional = sketch.Constraints[0]
    assert dimensional.Type == "DistanceX"
    assert dimensional.Arguments[:4] == (0, 1, 3, 2)
    assert isinstance(dimensional.Arguments[-1], StubQuantity)
    # The two-token arity was recorded datum-free, so it carries none.
    assert sketch.Constraints[1].Arguments == (2, 1)
    assert sketch.Constraints[2].Type == "Horizontal"
    assert sketch.Constraints[2].Arguments == (0,)
    assert doc.transactions[-1] == ("commit",)


def test_datum_on_a_type_recorded_without_one_is_rejected(sketch_module) -> None:
    # probes["constraint.forms"]: Horizontal:[0, 1] was rejected, so a
    # datum (one more native argument) on Horizontal repeats a rejected
    # arity and is refused before the constructor runs. Arity 1 was
    # accepted only datum-free, hence datumForbidden rather than a
    # missing-datum report.
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addConstraints=[{"type": "Horizontal", "arguments": [0], "datum": "10 mm"}],
        )

    assert excinfo.value.code == VALIDATION_FAILED
    assert "no recorded native form with a datum" in excinfo.value.message
    assert excinfo.value.details == {
        "reason": "unrecorded_constraint_shape",
        "type": "Horizontal",
        "argumentCount": 1,
        "acceptedArgumentCounts": [1],
        "datumForbidden": True,
        "nextTool": "inspect_sketch",
    }
    assert sketch.ops == []
    assert doc.transactions == []


def test_angle_without_a_datum_is_refused(sketch_module) -> None:
    # probes["constraint.forms"] probed Angle:[0, 1] and Angle:[0, 1, 0, 2]
    # with a "deg" quantity only, so no datum-free Angle form is recorded.
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addConstraints=[{"type": "Angle", "arguments": [0, 1]}],
        )

    assert excinfo.value.code == VALIDATION_FAILED
    assert "Angle" in excinfo.value.message
    assert excinfo.value.details == {
        "reason": "unrecorded_constraint_shape",
        "type": "Angle",
        "argumentCount": 2,
        "acceptedArgumentCounts": [2, 4],
        "datumRequired": True,
        "nextTool": "inspect_sketch",
    }
    assert sketch.ops == []
    assert doc.transactions == []
    assert doc.open_count == 0
    assert doc.recompute_count == 0


def test_angle_with_a_datum_plans_and_executes(sketch_module) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    result = call_edit(
        sketch_module,
        ctx,
        addConstraints=[{"type": "Angle", "arguments": [0, 1], "datum": "45 deg"}],
    )

    assert result["addedConstraints"] == [0]
    angle = sketch.Constraints[0]
    assert angle.Type == "Angle"
    assert angle.Arguments[:2] == (0, 1)
    assert isinstance(angle.Arguments[-1], StubQuantity)
    assert angle.Arguments[-1].text == "45 deg"
    assert doc.transactions[-1] == ("commit",)


def test_distance_without_a_datum_is_refused_and_with_one_executes(
    sketch_module,
) -> None:
    # Distance recorded every accepted arity with a datum; its datum-free
    # arities were not probed, so a datum-less entry is refused.
    sketch = FakeSketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addConstraints=[{"type": "Distance", "arguments": [2, 0]}],
        )

    assert excinfo.value.details["datumRequired"] is True
    assert excinfo.value.details["acceptedArgumentCounts"] == [2, 3, 4]
    assert sketch.ops == []
    assert doc.transactions == []

    result = call_edit(
        sketch_module,
        ctx,
        addConstraints=[{"type": "Distance", "arguments": [2, 0], "datum": "10 mm"}],
    )

    assert result["addedConstraints"] == [0]
    assert sketch.Constraints[0].Type == "Distance"


def test_dateless_four_token_distance_forms_are_refused(sketch_module) -> None:
    # examples/native_contract_probe.py:415-419 marks the datum-less
    # four-token DistanceX/DistanceY forms as the D1 crash
    # reproductions, and probes["constraint.forms"] accepted those
    # arities only with a quantity. Both are refused with no native call.
    for crash in (
        {"type": "DistanceX", "arguments": [0, 1, 1, 2]},
        {"type": "DistanceY", "arguments": [1, 1, 0, 2]},
    ):
        sketch = rectangle_sketch()
        doc = FakeDoc(sketch)
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call_edit(sketch_module, ctx, addConstraints=[crash])

        assert excinfo.value.code == VALIDATION_FAILED
        assert excinfo.value.details == {
            "reason": "unrecorded_constraint_shape",
            "type": crash["type"],
            "argumentCount": 4,
            "acceptedArgumentCounts": [2, 4],
            "datumRequired": True,
            "nextTool": "inspect_sketch",
        }
        assert sketch.ops == []
        assert doc.transactions == []
        assert doc.open_count == 0
        assert doc.recompute_count == 0


def test_distance_datum_policy_is_arity_aware(sketch_module) -> None:
    # The two-token forms were accepted datum-free and rejected a datum
    # (the sweep's DistanceX:[0, 1, 2] with a quantity); the four-token
    # forms were accepted with a datum only. The policy therefore follows
    # the arity, not the type.
    sketch = FakeSketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    without = call_edit(
        sketch_module,
        ctx,
        addConstraints=[{"type": "DistanceX", "arguments": [0, 1]}],
    )
    assert without["addedConstraints"] == [0]
    assert sketch.Constraints[0].Arguments == (0, 1)

    with_datum = call_edit(
        sketch_module,
        ctx,
        addConstraints=[{"type": "DistanceX", "arguments": [0, 1, 1, 2], "datum": "10 mm"}],
    )
    assert with_datum["addedConstraints"] == [1]
    assert sketch.Constraints[1].Arguments[:4] == (0, 1, 1, 2)
    assert isinstance(sketch.Constraints[1].Arguments[-1], StubQuantity)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addConstraints=[{"type": "DistanceX", "arguments": [0, 1], "datum": "10 mm"}],
        )
    assert excinfo.value.code == VALIDATION_FAILED
    assert excinfo.value.details == {
        "reason": "unrecorded_constraint_shape",
        "type": "DistanceX",
        "argumentCount": 2,
        "acceptedArgumentCounts": [2, 4],
        "datumForbidden": True,
        "nextTool": "inspect_sketch",
    }


def test_radius_and_diameter_require_a_datum_at_arity_one(sketch_module) -> None:
    # probes["constraint.forms"] recorded Radius:[2] and Diameter:[2]
    # accepted with a quantity and Radius:[2, 0]/Diameter:[2, 0] accepted
    # datum-free, so the single-token form has no recorded datum-free
    # acceptance either.
    sketch = FakeSketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    for kind in ("Radius", "Diameter"):
        with pytest.raises(ToolError) as excinfo:
            call_edit(
                sketch_module,
                ctx,
                addConstraints=[{"type": kind, "arguments": [0]}],
            )
        assert excinfo.value.details["datumRequired"] is True
        assert excinfo.value.details["acceptedArgumentCounts"] == [1, 2]
    assert sketch.ops == []
    assert doc.transactions == []


def test_recorded_two_token_radius_value_form_plans(sketch_module) -> None:
    # probes["constraint.forms"]: Radius:[2, 0] and Diameter:[2, 0] were
    # accepted by the native constructor. The map keeps that arity, and
    # the recorded sweep probed no slot roles for it.
    sketch = FakeSketch()
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_edit(
        sketch_module,
        ctx,
        addConstraints=[
            {"type": "Radius", "arguments": [2, 0]},
            {"type": "Diameter", "arguments": [3, 0]},
        ],
    )

    assert result["addedConstraints"] == [0, 1]
    assert sketch.Constraints[0].Type == "Radius"
    assert sketch.Constraints[0].Arguments == (2, 0)
    assert sketch.Constraints[1].Type == "Diameter"


def test_recorded_symmetric_axis_form_plans(sketch_module) -> None:
    # probes["constraint.forms"]: Symmetric:[0, 1, 1, 2, -1] was accepted
    # by the native constructor, so the fifth slot of the five-token form
    # is an axis-or-geometry reference, not a geometry index alone.
    sketch = FakeSketch()
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_edit(
        sketch_module,
        ctx,
        addConstraints=[{"type": "Symmetric", "arguments": [0, 1, 1, 2, -1]}],
    )

    assert result["addedConstraints"] == [0]
    assert sketch.Constraints[0].Arguments == (0, 1, 1, 2, -1)


def test_a_later_unrecorded_shape_refuses_the_whole_batch(sketch_module) -> None:
    # One bad entry refuses the batch: the recorded first entry and the
    # delete never reach a native method and the transaction never opens.
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            deleteGeometry=[3],
            addConstraints=[
                {"type": "Horizontal", "arguments": [0]},
                {"type": "Coincident", "arguments": [1, 0]},
            ],
        )

    assert excinfo.value.code == VALIDATION_FAILED
    assert excinfo.value.details["type"] == "Coincident"
    assert sketch.ops == []
    assert doc.transactions == []
    assert doc.open_count == 0
    assert doc.recompute_count == 0


def test_point_position_outside_the_domain_is_rejected_before_the_native_call(
    sketch_module,
) -> None:
    # probes["constraint.forms"]: the four-token DistanceX form with a
    # position value outside 0..2 is the recorded D1 crash input; the
    # recorded slot roles reject it before Sketcher.Constraint runs.
    sketch = rectangle_sketch()
    ctx = FakeCtx(FakeDoc(sketch))

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addConstraints=[{"type": "DistanceX", "arguments": [0, 1, 1, 7], "datum": "10 mm"}],
        )

    assert excinfo.value.code == VALIDATION_FAILED
    assert "point position" in excinfo.value.message


def test_datum_edits_reach_the_native_call_as_quantities(sketch_module) -> None:
    # probes["setDatum.quantity"]: the native call accepts a quantity;
    # the string variant fails with TypeError: Wrong arguments.
    sketch = rectangle_sketch()
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_edit(
        sketch_module,
        ctx,
        addConstraints=[{"type": "DistanceX", "arguments": [0, 1, 3, 2], "datum": "10 mm"}],
        setDatums=[{"index": 0, "datum": "12 mm"}],
    )

    assert result["changedDatums"] == [{"index": 0, "datum": "12 mm"}]
    assert sketch.datums == {0: "12 mm"}
    with pytest.raises(TypeError, match="Wrong arguments"):
        sketch._set_datum(0, "12 mm")


def test_construction_state_comes_from_getconstruction(sketch_module) -> None:
    # probes["geometry.getConstruction"]: the element attribute does not
    # exist; the flag is read from the sketch.
    sketch = FakeSketch(geometry=[line(0, 0, 10, 0), line(0, 5, 10, 5)])
    sketch.construction_flags[1] = True
    ctx = FakeCtx(FakeDoc(sketch))

    result = call_inspect(sketch_module, ctx)

    assert [row["construction"] for row in result["geometry"]] == [False, True]


# ---------------------------------------------------------------------------
# Request-local geometry identifiers.
# ---------------------------------------------------------------------------


def test_local_geometry_ids_commit_geometry_and_constraints_in_one_call(
    sketch_module,
) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    result = call_edit(
        sketch_module,
        ctx,
        addGeometry=[
            {"kind": "lineSegment", "id": "left", "start": [0, 0], "end": [0, 5]},
            {"kind": "lineSegment", "id": "bottom", "start": [0, 0], "end": [5, 0]},
        ],
        addConstraints=[
            {
                "type": "Coincident",
                "arguments": [{"geometry": "left"}, 2, {"geometry": "bottom"}, 1],
            },
        ],
    )

    assert result["addedGeometryIds"] == {"left": 4, "bottom": 5}
    assert result["addedGeometry"] == [4, 5]
    constraint = sketch.Constraints[-1]
    assert constraint.Type == "Coincident"
    assert constraint.First == 4
    assert constraint.Second == 5
    assert doc.transactions[-1] == ("commit",)
    definition = next(
        entry for entry in sketch_module.TOOL_DEFINITIONS if entry["name"] == "edit_sketch"
    )
    validate_schema(result, definition["outputSchema"])


def test_local_geometry_forward_reference_resolves(sketch_module) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    result = call_edit(
        sketch_module,
        ctx,
        addGeometry=[
            {"kind": "lineSegment", "id": "first", "start": [0, 0], "end": [0, 5]},
            {"kind": "lineSegment", "id": "second", "start": [0, 0], "end": [5, 0]},
        ],
        addConstraints=[
            {
                "type": "Coincident",
                "arguments": [{"geometry": "second"}, 1, {"geometry": "first"}, 1],
            },
        ],
    )

    assert result["addedGeometryIds"] == {"first": 4, "second": 5}
    constraint = sketch.Constraints[-1]
    assert (constraint.First, constraint.Second) == (5, 4)
    assert doc.transactions[-1] == ("commit",)


def test_local_geometry_ids_track_expansion_and_deletion(sketch_module) -> None:
    """The planned index must follow expanded positions and deletions.

    A regression that used pre-expansion ``addGeometry`` positions would
    plan index 5 here while the native call returns 7.
    """

    sketch = rectangle_sketch()  # four existing lines
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    result = call_edit(
        sketch_module,
        ctx,
        deleteGeometry=[1],
        addGeometry=[
            {"kind": "rectangle", "origin": [0, 0], "width": 4, "height": 3},
            {"kind": "circle", "id": "bore", "center": [2, 2], "radius": 1},
        ],
    )

    assert result["addedGeometryIds"] == {"bore": 7}
    assert result["addedGeometry"] == [3, 4, 5, 6, 7]
    assert doc.transactions[-1] == ("commit",)


def test_added_geometry_ids_is_empty_without_ids(sketch_module) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    result = call_edit(
        sketch_module,
        ctx,
        addGeometry=[{"kind": "lineSegment", "start": [0, 0], "end": [1, 0]}],
    )

    assert result["addedGeometryIds"] == {}
    assert result["addedGeometry"] == [4]
    assert doc.transactions[-1] == ("commit",)


def test_local_geometry_unknown_id_is_refused_before_the_transaction(
    sketch_module,
) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addGeometry=[{"kind": "lineSegment", "id": "left", "start": [0, 0], "end": [0, 5]}],
            addConstraints=[{"type": "Horizontal", "arguments": [{"geometry": "nope"}]}],
        )

    assert excinfo.value.details["reason"] == "unknown_geometry_id"
    assert excinfo.value.details["id"] == "nope"
    assert doc.transactions == []
    assert sketch.ops == []


def test_local_geometry_duplicate_id_is_refused_before_the_transaction(
    sketch_module,
) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addGeometry=[
                {"kind": "lineSegment", "id": "dup", "start": [0, 0], "end": [0, 5]},
                {"kind": "lineSegment", "id": "dup", "start": [0, 0], "end": [5, 0]},
            ],
        )

    assert excinfo.value.details == {"reason": "duplicate_geometry_id", "id": "dup"}
    assert doc.transactions == []
    assert sketch.ops == []


def test_local_geometry_reference_in_point_slot_is_refused(sketch_module) -> None:
    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addGeometry=[
                {"kind": "lineSegment", "id": "left", "start": [0, 0], "end": [0, 5]},
                {"kind": "lineSegment", "id": "bottom", "start": [0, 0], "end": [5, 0]},
            ],
            addConstraints=[
                {
                    "type": "Coincident",
                    "arguments": [{"geometry": "left"}, {"geometry": "bottom"}, 3, 1],
                },
            ],
        )

    assert excinfo.value.details == {"reason": "local_ref_wrong_slot", "position": 1}
    assert doc.transactions == []
    assert sketch.ops == []


def test_local_geometry_reference_in_radius_value_slot_is_refused(
    sketch_module,
) -> None:
    """Radius/Diameter arity 2 is [geometry, value]; the value slot takes no ref.

    Resolving a reference there would pass its index as the numeric radius
    (verified: the native constraint became ``Radius (4, 5)`` with
    ``Value = 5.0``), silently changing the constraint instead of refusing.
    """

    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addGeometry=[
                {"kind": "circle", "id": "c1", "center": [0, 0], "radius": 3},
                {"kind": "circle", "id": "c2", "center": [9, 9], "radius": 5},
            ],
            addConstraints=[
                {"type": "Radius", "arguments": [{"geometry": "c1"}, {"geometry": "c2"}]},
            ],
        )

    assert excinfo.value.details == {"reason": "local_ref_wrong_slot", "position": 1}
    assert doc.transactions == []
    assert sketch.ops == []


def test_local_geometry_reference_in_axis_slot_resolves(sketch_module) -> None:
    """An ``A`` slot is geometry-or-axis, so a local reference resolves there."""

    sketch = rectangle_sketch()
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    result = call_edit(
        sketch_module,
        ctx,
        addGeometry=[
            {"kind": "lineSegment", "id": "axis", "start": [0, 0], "end": [0, 5]},
            {"kind": "lineSegment", "id": "other", "start": [0, 0], "end": [5, 0]},
        ],
        addConstraints=[
            {
                "type": "Coincident",
                "arguments": [{"geometry": "other"}, 1, {"geometry": "axis"}, 1],
            },
        ],
    )

    assert result["addedGeometryIds"] == {"axis": 4, "other": 5}
    constraint = sketch.Constraints[-1]
    assert (constraint.First, constraint.Second) == (5, 4)
    assert doc.transactions[-1] == ("commit",)


def test_geometry_id_on_a_composite_entry_fails_schema_validation(
    sketch_module,
) -> None:
    definition = next(
        entry for entry in sketch_module.TOOL_DEFINITIONS if entry["name"] == "edit_sketch"
    )
    arguments = {
        "document": "Doc",
        "sketch": "Sketch",
        "addGeometry": [
            {"kind": "rectangle", "id": "frame", "origin": [0, 0], "width": 4, "height": 3}
        ],
    }
    with pytest.raises(ProtocolError):
        validate_schema(arguments, definition["inputSchema"])


def test_native_index_mismatch_for_an_id_entry_rolls_the_batch_back(
    sketch_module,
) -> None:
    class MisreportingSketch(FakeSketch):
        def _add_geometry(self, geo: Any, construction: bool = False) -> int:
            super()._add_geometry(geo, construction)
            return 99  # native reported an index the plan never predicted

    sketch = MisreportingSketch(geometry=rectangle_sketch().Geometry)
    doc = FakeDoc(sketch)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as excinfo:
        call_edit(
            sketch_module,
            ctx,
            addGeometry=[{"kind": "lineSegment", "id": "left", "start": [0, 0], "end": [0, 5]}],
        )

    assert excinfo.value.details["reason"] == "native_index_mismatch"
    assert excinfo.value.details["expectedIndex"] == 4
    assert excinfo.value.details["actualIndex"] == 99
    assert excinfo.value.details["operationState"] == "rolled_back"
    assert doc.transactions[-1] == ("abort",)
