import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.object_validation import object_validity_error


@dataclass
class FakeObject:
    Name: str = "Object"
    TypeId: str = "Part::Feature"
    valid: bool = True
    State: list[str] = field(default_factory=lambda: ["Up-to-date"])
    status: str = ""
    Shape: object | None = None

    def isValid(self) -> bool:
        return self.valid

    def getStatusString(self) -> str:
        return self.status


def test_invalid_object_reports_name_state_and_freecad_reason() -> None:
    obj = FakeObject(
        Name="Pad",
        valid=False,
        State=["Touched", "Invalid"],
        status="Linked shape object is empty",
    )

    error = object_validity_error(obj)

    assert error is not None
    assert "Pad" in error
    assert "Touched, Invalid" in error
    assert "Linked shape object is empty" in error


def test_touched_object_is_rejected_even_when_is_valid_returns_true() -> None:
    obj = FakeObject(valid=True, State=["Touched"], status="Touched")

    error = object_validity_error(obj)

    assert error is not None
    assert "Touched" in error


def test_validity_check_exception_is_reported_as_failure() -> None:
    class BrokenObject:
        Name = "Broken"

        def isValid(self) -> bool:
            raise RuntimeError("invalid internal state")

    error = object_validity_error(BrokenObject())

    assert error is not None
    assert "validity could not be checked" in error
    assert "invalid internal state" in error


from mcp_server.object_validation import (
    compare_expected_bounds,
    dependent_count,
    geometry_report,
    mutation,
    shape_is_null,
)
from mcp_server.protocol import ToolError

VALIDATION_FAILED = "VALIDATION_FAILED"
SERVER_BUSY_LIKE = "SERVER_BUSY"


def test_matching_bounds_within_tolerance_report_match() -> None:
    verdict, deviations = compare_expected_bounds(
        [0.0, 0.0, 0.0, 10.0, 10.0, 10.0],
        [0.0, 0.0, 0.0, 10.0, 10.0, 10.0],
        0.000001,
    )

    assert verdict == "match"
    assert deviations == [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_bounds_deviation_beyond_tolerance_reports_mismatch() -> None:
    verdict, deviations = compare_expected_bounds(
        [0.0, 0.0, 0.0, 10.0, 10.0, 10.0],
        [0.0, 0.0, 0.0, 10.0, 10.0, 14.0],
        0.000001,
    )

    assert verdict == "mismatch"
    assert deviations == [0.0, 0.0, 0.0, 0.0, 0.0, 4.0]


def test_unavailable_measured_bounds_report_unavailable() -> None:
    verdict, deviations = compare_expected_bounds(None, [0.0, 0.0, 0.0, 10.0, 10.0, 10.0], 0.000001)

    assert verdict == "unavailable"
    assert deviations is None


def test_tolerance_zero_rejects_any_deviation() -> None:
    verdict, _deviations = compare_expected_bounds(
        [0.0, 0.0, 0.0, 10.0, 10.0, 10.0],
        [0.0, 0.0, 0.0, 10.0, 10.0, 10.0000001],
        0.0,
    )

    assert verdict == "mismatch"


# ---------------------------------------------------------------------------
# dependent_count: bounded transitive closure count.
# ---------------------------------------------------------------------------


class FakeCountObj:
    def __init__(self, name: str, in_list: tuple["FakeCountObj", ...] = ()) -> None:
        self.Name = name
        self.InList = list(in_list)


def test_dependent_count_walks_the_transitive_closure() -> None:
    source = FakeCountObj("Source")
    middle = FakeCountObj("Middle", (source,))
    top = FakeCountObj("Top", (middle,))

    assert dependent_count([top]) == 2


def test_dependent_count_is_capped_at_the_limit() -> None:
    fan = [FakeCountObj(f"D{index}") for index in range(8)]
    source = FakeCountObj("Source", tuple(fan))

    assert dependent_count([source], limit=3) == 3
    assert dependent_count([source]) == 8


# ---------------------------------------------------------------------------
# mutation operationState contract.
# ---------------------------------------------------------------------------


class _NonNullShape:
    """Real-shape double: isNull() is False; nothing else is implemented."""

    def isNull(self) -> bool:
        return False


class FakeShapeObj:
    """Minimal valid solid-shaped object for gate drives."""

    def __init__(self, name: str, in_list: tuple[Any, ...] = ()) -> None:
        self.Name = name
        self.TypeId = "Part::Feature"
        self.State: list[str] = []
        self.InList = list(in_list)
        # A non-null shape double (probes["shape.null_attributes"]): the
        # isNull() verdict drives the report, all other reads stay guarded.
        self.Shape = _NonNullShape()

    def isValid(self) -> bool:
        return True

    def getStatusString(self) -> str:
        return ""


class FakeGateDoc:
    def __init__(self, objects: list[Any]) -> None:
        self.Name = "Doc"
        self.Objects = list(objects)
        self.UndoMode = 0
        self.HasPendingTransaction = False
        self.fail_abort = False
        self.fail_recompute = False
        self.fail_commit = False

    def openTransaction(self, label: str) -> None:
        pass

    def commitTransaction(self) -> None:
        if self.fail_commit:
            raise RuntimeError("commit exploded")

    def abortTransaction(self) -> None:
        if self.fail_abort:
            raise RuntimeError("abort exploded")

    def recompute(self) -> None:
        if self.fail_recompute:
            raise RuntimeError("recompute exploded")


class FakeGateApp:
    def getActiveTransaction(self) -> None:
        return None


class FakeGateCtx:
    def __init__(self, doc: FakeGateDoc, reveal: Any = None) -> None:
        self.App = FakeGateApp()
        self._doc = doc
        self.revealed: list[Any] = []
        self.reveal_hook = reveal

    def check_document_idle(self, doc: FakeGateDoc) -> None:
        pass

    def reveal_objects(self, doc: FakeGateDoc, targets: list[Any]) -> None:
        if self.reveal_hook is not None:
            self.reveal_hook(doc, targets)
        self.revealed.append((doc, list(targets)))


def test_reveal_failure_never_fails_the_mutation() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])

    def explode(_doc: Any, _targets: Any) -> None:
        raise RuntimeError("no view")

    ctx = FakeGateCtx(doc, reveal=explode)

    with mutation(ctx, doc, "gate", [obj]) as applied:
        pass

    # The gate reports the target even though the reveal hook raised.
    assert applied == ["Box"]


def run_gate(doc: FakeGateDoc, obj: FakeShapeObj, body: Any) -> None:
    with mutation(FakeGateCtx(doc), doc, "gate", [obj]):
        body()


def test_body_failure_with_successful_rollback_reports_rolled_back() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])

    with pytest.raises(ToolError) as excinfo:
        run_gate(doc, obj, lambda: (_ for _ in ()).throw(RuntimeError("nope")))

    details = excinfo.value.details or {}
    assert details["operationState"] == "rolled_back"
    assert details["nextAction"] == "retry_from_original_state"


def test_failed_abort_reports_rollback_failed() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])
    doc.fail_abort = True

    with pytest.raises(ToolError) as excinfo:
        run_gate(doc, obj, lambda: (_ for _ in ()).throw(RuntimeError("nope")))

    details = excinfo.value.details or {}
    assert details["operationState"] == "rollback_failed"
    assert details["rollbackFailed"] is True
    assert details["rollbackStage"] == "abort"
    assert details["originalError"] == "RuntimeError: nope"


def test_failed_rollback_recompute_reports_rollback_failed() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])
    doc.fail_recompute = True

    with pytest.raises(ToolError) as excinfo:
        run_gate(doc, obj, lambda: (_ for _ in ()).throw(RuntimeError("nope")))

    details = excinfo.value.details or {}
    assert details["operationState"] == "rollback_failed"
    assert details["rollbackStage"] == "recompute"


class _SurvivingTransactionApp:
    """FreeCAD 1.1 stack double: a raised body leaves the label active.

    The entry check must see a clean stack; every later read reports the
    surviving transaction until ``closeActiveTransaction`` clears it, which
    is what that native call did in the observed session.
    """

    def __init__(self, *, label: str = "gate", closable: bool = True) -> None:
        self.label = label
        self.closable = closable
        self.calls = 0
        self.closed: list[bool] = []
        self._survives = True

    def getActiveTransaction(self) -> tuple[str, int] | None:
        self.calls += 1
        if self.calls == 1 or not self._survives:
            return None
        return (self.label, 7)

    def closeActiveTransaction(self, abort: bool) -> None:
        self.closed.append(bool(abort))
        if self.closable:
            self._survives = False


def _run_gate_with(app: Any, doc: FakeGateDoc, obj: FakeShapeObj, body: Any) -> None:
    ctx = FakeGateCtx(doc)
    ctx.App = app
    with mutation(ctx, doc, "gate", [obj]):
        body()


def _raise_in_body() -> None:
    raise RuntimeError("nope")


def test_raised_body_closes_its_surviving_transaction() -> None:
    """A raised body leaves the transaction on FreeCAD's stack.

    ``abortTransaction`` undoes the recorded changes but not the transaction
    itself, so the gate must force-close its own label; otherwise every later
    mutation is refused as a user transaction while health reads healthy.
    """

    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])
    app = _SurvivingTransactionApp()

    with pytest.raises(ToolError) as excinfo:
        _run_gate_with(app, doc, obj, _raise_in_body)

    details = excinfo.value.details or {}
    assert details["operationState"] == "rolled_back"
    assert app.closed == [True]  # this operation's own label was closed


def test_transaction_that_survives_the_cleanup_is_reported_truthfully() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])
    app = _SurvivingTransactionApp(closable=False)

    with pytest.raises(ToolError) as excinfo:
        _run_gate_with(app, doc, obj, _raise_in_body)

    details = excinfo.value.details or {}
    assert details["operationState"] == "rollback_failed"
    assert details["rollbackFailed"] is True
    assert details["rollbackStage"] == "close_transaction"
    assert details["originalError"] == "RuntimeError: nope"


def test_a_foreign_surviving_transaction_is_never_closed() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])
    app = _SurvivingTransactionApp(label="Pad (user edit)")

    with pytest.raises(ToolError) as excinfo:
        _run_gate_with(app, doc, obj, _raise_in_body)

    details = excinfo.value.details or {}
    assert app.closed == []  # a user's transaction is never aborted for us
    assert details["operationState"] == "rolled_back"


def test_committed_mutation_left_open_is_reported_not_hidden() -> None:
    """When the force-close cannot clear this operation's own transaction,
    the commit must not be reported as a clean success: every later
    mutation would be refused as a nested user transaction."""

    class _WedgedApp:
        """Entry check sees a clean stack; the force-close then fails and the
        operation's own transaction stays on the stack."""

        def __init__(self) -> None:
            self.calls = 0

        def getActiveTransaction(self) -> tuple[str, int] | None:
            self.calls += 1
            return None if self.calls == 1 else ("gate", 7)

        def closeActiveTransaction(self, abort: bool) -> None:
            raise RuntimeError("cannot close")

    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])
    ctx = FakeGateCtx(doc)
    ctx.App = _WedgedApp()

    with pytest.raises(ToolError) as excinfo, mutation(ctx, doc, "gate", [obj]):
        pass

    details = excinfo.value.details or {}
    assert details["operationState"] == "may_have_changed"
    assert details["reason"] == "transaction_still_open"
    assert details["nextAction"] == "inspect_target"


class _InsetBox:
    def __init__(self, xmin: float, ymin: float, xmax: float, ymax: float) -> None:
        self.XMin = xmin
        self.YMin = ymin
        self.ZMin = 0.0
        self.XMax = xmax
        self.YMax = ymax
        self.ZMax = 20.0


class _InsetShape:
    """Native 1.1.3 read: a raw bound box is triangulation-approximated."""

    def isNull(self) -> bool:
        return False

    @property
    def BoundBox(self) -> _InsetBox:
        return _InsetBox(-2.9988, -2.9997, 3.0, 2.9997)

    def copy(self) -> Any:
        return _RebuiltShape()


class _RebuiltShape:
    """The document-space copy, whose axis-aligned box is exact."""

    Placement: Any = None

    @property
    def BoundBox(self) -> _InsetBox:
        return _InsetBox(-3.0, -3.0, 3.0, 3.0)


class _CurvedObject:
    """A curved solid whose local and document-space readings disagree."""

    Name = "Cyl"
    TypeId = "Part::Feature"
    State = ["Up-to-date"]

    def __init__(self, *, placeable: bool = True) -> None:
        self.Shape = _InsetShape()
        self._placeable = placeable

    def isValid(self) -> bool:
        return True

    def getStatusString(self) -> str:
        return ""

    def getGlobalPlacement(self) -> Any:
        if not self._placeable:
            raise AttributeError("no global placement")
        return types.SimpleNamespace()


def test_geometry_report_bounds_use_the_document_space_reading() -> None:
    """One bounds authority for every tool.

    validate_geometry, inspect_objects and the gate's expected_bounds check
    report document-space bounds; a report that used the raw local box
    disagreed with them and read a cylinder up to 0.05 mm narrower.
    """

    report = geometry_report(_CurvedObject())

    assert report["bounds"] == [-3.0, -3.0, 0.0, 3.0, 3.0, 20.0]
    assert report["ok"] is True


def test_geometry_report_marks_document_bounds_unavailable_when_unplaceable() -> None:
    report = geometry_report(_CurvedObject(placeable=False))

    assert report["bounds"] is None
    assert report["geometryUnavailable"] == "document-space bounds unavailable"


def test_failed_commit_reports_may_have_changed_with_inspect_action() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])
    doc.fail_commit = True

    with pytest.raises(ToolError) as excinfo:
        run_gate(doc, obj, lambda: None)

    details = excinfo.value.details or {}
    assert details["operationState"] == "may_have_changed"
    assert details["nextAction"] == "inspect_target"
    assert "commit exploded" in details["commitError"]


def test_expected_bounds_failure_after_recompute_rolls_back() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])

    # document_bounds resolves through the stubbed geometry module in
    # heavier suites; here it returns None (unavailable), which must fail
    # the commit condition and roll back.
    with (
        pytest.raises(ToolError) as excinfo,
        mutation(
            FakeGateCtx(doc),
            doc,
            "gate",
            [obj],
            expected_bounds=[0.0, 0.0, 0.0, 10.0, 10.0, 10.0],
        ),
    ):
        pass

    details = excinfo.value.details or {}
    assert excinfo.value.code == VALIDATION_FAILED
    assert details["operationState"] == "rolled_back"
    assert details["reason"] == "expected_bounds"


def test_prevalidation_failure_has_no_operation_state() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])

    # A busy document is rejected before the transaction opens: nothing
    # started, so no operationState may appear.
    class BusyCtx(FakeGateCtx):
        def check_document_idle(self, doc: FakeGateDoc) -> None:
            raise ToolError(SERVER_BUSY_LIKE, "busy")

    with pytest.raises(ToolError) as excinfo, mutation(BusyCtx(doc), doc, "gate", [obj]):
        pass

    assert (excinfo.value.details or {}).get("operationState") is None


# ---------------------------------------------------------------------------
# mutation outcome: the gate hands back the validation it already did.
# ---------------------------------------------------------------------------


def test_outcome_counts_track_a_dependent_removed_by_the_body() -> None:
    box = FakeShapeObj("Box")
    pad = FakeShapeObj("Pad")
    box.InList = [pad]
    doc = FakeGateDoc([box, pad])
    outcome: dict = {}

    with mutation(FakeGateCtx(doc), doc, "gate", [box], outcome=outcome):
        # The body drops the link that made Pad a dependent of Box.
        del box.InList[0]

    assert outcome["dependentCountBefore"] == 1
    assert outcome["dependentCountAfter"] == 0
    assert sorted(outcome["reports"]) == ["Box"]


def test_callable_targets_report_no_pre_mutation_closure() -> None:
    box = FakeShapeObj("Box")
    doc = FakeGateDoc([box])
    outcome: dict = {}

    with mutation(FakeGateCtx(doc), doc, "gate", lambda: [box], outcome=outcome) as applied:
        pad = FakeShapeObj("Pad")
        doc.Objects.append(pad)
        box.InList = [pad]

    # A create flow has no pre-mutation targets, so its before-count is 0;
    # the post-recompute closure still reports what the commit produced.
    assert applied == ["Box"]
    assert outcome["dependentCountBefore"] == 0
    assert outcome["dependentCountAfter"] == 1
    assert sorted(outcome["reports"]) == ["Box", "Pad"]


# ---------------------------------------------------------------------------
# Null-shape doubles (probes["shape.null_attributes"]).
# ---------------------------------------------------------------------------


class FreeCADError(RuntimeError):
    pass


class OCCError(RuntimeError):
    pass


class FakeNullShape:
    """A fresh PartDesign::Body's null shape, per the recorded probe.

    ``Volume`` and ``Area`` raise ``RuntimeError: shape is invalid``;
    ``ShapeType`` raises ``FreeCADError``; ``isValid`` raises
    ``OCCError``; ``check()`` returns None; ``getTolerance`` wants an
    argument; ``Solids`` stays a safe empty list; ``isNull()`` is True.
    """

    def isNull(self) -> bool:
        return True

    @property
    def Volume(self) -> float:
        raise RuntimeError("shape is invalid")

    @property
    def Area(self) -> float:
        raise RuntimeError("shape is invalid")

    @property
    def ShapeType(self) -> str:
        raise FreeCADError("cannot determine type of null shape")

    def isValid(self) -> bool:
        raise OCCError("Standard_NullObject BRepCheck_Analyzer::Init() - NULL shape")

    def check(self) -> None:
        return None

    def getTolerance(self, *_: Any) -> float:
        raise TypeError("function takes at least 1 argument (0 given)")

    @property
    def Solids(self) -> list:
        return []


def test_null_shape_is_valid_without_a_solid_contract() -> None:
    obj = FakeObject(Name="Body", TypeId="PartDesign::Body")
    obj.Shape = FakeNullShape()

    report = geometry_report(obj)

    assert report["ok"] is True
    assert report["error"] is None
    assert report["solid_count"] == 0
    assert report["volume"] is None


def test_null_shape_fails_positive_expected_solids_with_the_recorded_message() -> None:
    obj = FakeObject(Name="Body")
    obj.Shape = FakeNullShape()

    report = geometry_report(obj, expected_solids=1)

    assert report["ok"] is False
    assert "has no geometry; expected_solids=1 cannot be satisfied" in report["error"]


def test_geometry_report_never_raises_on_a_null_shape() -> None:
    obj = FakeObject(Name="Body")
    obj.Shape = FakeNullShape()

    report = geometry_report(obj, expected_solids=0)

    assert report["ok"] is True
    assert report["volume"] is None
    assert shape_is_null(obj.Shape) is True


# ---------------------------------------------------------------------------
# Write-time expectations: expected_volume and expected_topology_counts.
# ---------------------------------------------------------------------------


class Cylinder:
    """Native cylinder surface stand-in: the class name drives ``%CYLINDER``."""

    def __init__(self, radius: float) -> None:
        self.Radius = radius


class Plane:
    """Native plane surface stand-in: the class name drives ``%PLANE``."""


class _CountEdge:
    """A root edge whose identity is what ``isSame`` compares."""

    def isSame(self, other: Any) -> bool:
        return self is other


class _CountFace:
    """A face with a classified surface and its bounding edges."""

    def __init__(self, surface: Any, edges: tuple[Any, ...] = ()) -> None:
        self.Surface = surface
        self.Edges = list(edges)


class _CountShape:
    """A solid-shaped double whose placed copy keeps the same subshapes."""

    def __init__(self, volume: float, solids: int, faces: list[Any], edges: list[Any]) -> None:
        self.Volume = volume
        self.Solids = [object()] * solids
        self.Faces = faces
        self.Edges = edges
        self.Placement: Any = None

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return True

    def check(self) -> list[str]:
        return []

    def getTolerance(self, _mode: int) -> float:
        return 1e-7

    def copy(self) -> "_CountShape":
        return _CountShape(self.Volume, len(self.Solids), self.Faces, self.Edges)


def _counted_object(*, volume: float = 1000.0, solids: int = 1, placeable: bool = True) -> Any:
    """A body with four cylinders (two of radius 3, two of 6), two planes and three edges."""

    e1, e2, e3 = _CountEdge(), _CountEdge(), _CountEdge()
    faces = [
        _CountFace(Cylinder(3.0), (e1, e2)),
        _CountFace(Cylinder(3.0), (e2, e3)),
        _CountFace(Cylinder(6.0)),
        _CountFace(Cylinder(6.0)),
        _CountFace(Plane()),
        _CountFace(Plane()),
    ]
    obj = FakeShapeObj("Box")
    obj.Shape = _CountShape(volume, solids, faces, [e1, e2, e3])
    if placeable:
        obj.getGlobalPlacement = lambda: types.SimpleNamespace()  # type: ignore[attr-defined]
    return obj


class _EventDoc(FakeGateDoc):
    """A gate document recording the transaction calls in order."""

    def __init__(self, objects: list[Any]) -> None:
        super().__init__(objects)
        self.events: list[str] = []

    def openTransaction(self, label: str) -> None:
        self.events.append("open")

    def commitTransaction(self) -> None:
        self.events.append("commit")

    def abortTransaction(self) -> None:
        self.events.append("abort")


class _ExpectationCtx(FakeGateCtx):
    """A gate context that also resolves objects by name for query counts."""

    def require_object(self, doc: _EventDoc, name: str) -> Any:
        return next(obj for obj in doc.Objects if obj.Name == name)


_CYLINDERS = {"role": "face", "selector": "%CYLINDER"}


def _gate(obj: Any, **expectation: Any) -> tuple[_EventDoc, dict]:
    """Run an empty mutation over ``obj`` with the given write-time expectations."""

    doc = _EventDoc([obj])
    outcome: dict = {}
    with mutation(_ExpectationCtx(doc), doc, "gate", [obj], outcome=outcome, **expectation):
        pass
    return doc, outcome


def test_passed_expectations_commit_and_report_only_the_requested_checks() -> None:
    """Counts resolve as sets: zero matches count as zero and shared edges count once."""

    obj = _counted_object()
    counts = [
        {"query": [_CYLINDERS], "min": 4, "max": 4},
        {"query": [{"role": "face", "selector": "%CONE"}], "min": 0, "max": 0},
        {"query": [_CYLINDERS, {"role": "face", "radius": {"min": 5}}], "min": 2},
        {
            "query": [_CYLINDERS, {"role": "face", "radius": {"max": 4}}, {"role": "edge"}],
            "max": 3,
        },
    ]

    doc, outcome = _gate(
        obj,
        expected_volume={"min": 1000.0, "max": 1000.0},
        expected_topology_counts=counts,
    )

    assert doc.events == ["open", "commit"]
    assert outcome["expectationResults"] == [
        {
            "object": "Box",
            "volume": {"min": 1000.0, "max": 1000.0, "measured": 1000.0},
            "topologyCounts": [
                {**counts[0], "measured": 4},
                {**counts[1], "measured": 0},
                {**counts[2], "measured": 2},
                {**counts[3], "measured": 3},
            ],
        }
    ]

    _doc, plain = _gate(_counted_object())
    assert "expectationResults" not in plain


@pytest.mark.parametrize(
    ("obj", "expectation", "reason", "extra"),
    [
        (
            _counted_object(),
            {"expected_volume": {"min": 2000.0}},
            "expected_volume",
            {"status": "fail", "measured": 1000.0, "checkReason": None},
        ),
        (
            _counted_object(volume=0.0, solids=0),
            {"expected_volume": {"min": 1.0}},
            "expected_volume",
            {"status": "indeterminate", "checkReason": "non_volumetric_target"},
        ),
        (
            _counted_object(),
            {
                "expected_topology_counts": [
                    {"query": [_CYLINDERS], "min": 4},
                    {"query": [_CYLINDERS], "min": 5, "max": 5},
                ]
            },
            "expected_topology_count",
            {"checkIndex": 1, "measured": 4, "expected": {"min": 5, "max": 5}},
        ),
        (
            _counted_object(placeable=False),
            {"expected_topology_counts": [{"query": [_CYLINDERS], "min": 4}]},
            "expected_topology_count_unavailable",
            {"checkIndex": 0},
        ),
    ],
)
def test_a_failed_expectation_rolls_the_mutation_back_with_its_reason(
    obj: Any, expectation: dict, reason: str, extra: dict
) -> None:
    """A miss, an indeterminate volume or an unreadable shape never commits."""

    doc = _EventDoc([obj])

    with (
        pytest.raises(ToolError) as excinfo,
        mutation(_ExpectationCtx(doc), doc, "gate", [obj], **expectation),
    ):
        pass

    details = excinfo.value.details or {}
    assert excinfo.value.code == VALIDATION_FAILED
    assert details["reason"] == reason
    assert details["object"] == "Box"
    assert {key: details[key] for key in extra} == extra
    assert details["operationState"] == "rolled_back"
    assert doc.events == ["open", "abort"]


_ONE_COUNT = {"query": [_CYLINDERS], "min": 1}


@pytest.mark.parametrize(
    ("expectation", "reason"),
    [
        ({"expected_volume": {}}, "empty_volume_range"),
        ({"expected_volume": {"min": 5.0, "max": 1.0}}, "invalid_volume_range"),
        ({"expected_topology_counts": [{"query": [_CYLINDERS]}]}, "empty_count_range"),
        (
            {"expected_topology_counts": [{"query": [_CYLINDERS], "min": 3, "max": 1}]},
            "invalid_count_range",
        ),
        (
            {
                "expected_topology_counts": [
                    {"query": [{"role": "face", "selector": "%%"}], "min": 1}
                ]
            },
            "selector_syntax",
        ),
        (
            {
                "expectations": {
                    f"Box{index}": {"expected_topology_counts": [_ONE_COUNT] * 7}
                    for index in range(5)
                }
            },
            "expectation_query_limit",
        ),
    ],
)
def test_a_malformed_expectation_is_refused_before_the_transaction_opens(
    expectation: dict, reason: str
) -> None:
    """Nothing starts: no transaction, no body run and no operationState."""

    obj = _counted_object()
    doc = _EventDoc([obj])
    ran: list[bool] = []

    with (
        pytest.raises(ToolError) as excinfo,
        mutation(_ExpectationCtx(doc), doc, "gate", [obj], **expectation),
    ):
        ran.append(True)

    assert excinfo.value.code == VALIDATION_FAILED
    assert (excinfo.value.details or {})["reason"] == reason
    assert "operationState" not in (excinfo.value.details or {})
    assert doc.events == []
    assert ran == []
