import sys
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


def test_valid_shapeless_object_is_not_rejected() -> None:
    obj = FakeObject(Name="Body", Shape=None)

    assert object_validity_error(obj) is None


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


def test_object_without_validity_api_is_left_unchanged() -> None:
    obj = type("LegacyObject", (), {"Name": "Legacy"})()

    assert object_validity_error(obj) is None


def test_touched_object_is_rejected_even_when_is_valid_returns_true() -> None:
    obj = FakeObject(valid=True, State=["Touched"], status="Touched")

    error = object_validity_error(obj)

    assert error is not None
    assert "Touched" in error


def test_invalid_state_is_used_when_validity_api_is_missing() -> None:
    obj = type(
        "LegacyObject",
        (),
        {"Name": "Legacy", "State": ["Invalid"]},
    )()

    error = object_validity_error(obj)

    assert error is not None
    assert "Invalid" in error


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


def test_committed_mutation_reveals_its_targets() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])
    ctx = FakeGateCtx(doc)

    with mutation(ctx, doc, "gate", [obj]) as applied:
        applied.append("Box")

    assert [target.Name for _doc, targets in ctx.revealed for target in targets] == ["Box"]


def test_rolled_back_mutation_never_reveals() -> None:
    obj = FakeShapeObj("Box")
    doc = FakeGateDoc([obj])
    ctx = FakeGateCtx(doc)

    with pytest.raises(ToolError), mutation(ctx, doc, "gate", [obj]):
        raise RuntimeError("nope")

    assert ctx.revealed == []


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

from mcp_server import object_validation


def test_outcome_reports_are_the_validated_geometry_report_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = FakeShapeObj("Box")
    pad = FakeShapeObj("Pad")
    box.InList = [pad]
    doc = FakeGateDoc([box, pad])
    built: dict[str, dict] = {}
    real_geometry_report = object_validation.geometry_report

    def recording_geometry_report(obj: Any, expected_solids: int | None = None) -> dict:
        report = real_geometry_report(obj, expected_solids)
        built[str(getattr(obj, "Name", ""))] = report
        return report

    # Identity matters: a handler reusing these reports must receive the very
    # dicts the gate validated, never a copy and never a second OCC pass.
    monkeypatch.setattr(object_validation, "geometry_report", recording_geometry_report)

    outcome: dict = {}
    with mutation(FakeGateCtx(doc), doc, "gate", [box], outcome=outcome):
        pass

    assert sorted(outcome["reports"]) == ["Box", "Pad"]
    assert outcome["reports"]["Box"] is built["Box"]
    assert outcome["reports"]["Pad"] is built["Pad"]
    assert outcome["reports"]["Box"]["ok"] is True
    assert outcome["reports"]["Pad"]["ok"] is True


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


def test_omitted_outcome_leaves_the_gate_result_unchanged() -> None:
    def drive(outcome: Any = None) -> tuple[list[str], str]:
        box = FakeShapeObj("Box")
        pad = FakeShapeObj("Pad")
        box.InList = [pad]
        doc = FakeGateDoc([box, pad])
        with (
            pytest.raises(ToolError) as excinfo,
            mutation(FakeGateCtx(doc), doc, "gate", [box], outcome=outcome) as applied,
        ):
            # Touch the dependent so the gate's own validation, not the
            # outcome plumbing, decides the rollback.
            pad.State = ["Touched"]
        assert doc.UndoMode == 0
        return list(applied), str(excinfo.value.details)

    assert drive() == drive({})

    box = FakeShapeObj("Box")
    doc = FakeGateDoc([box])
    with mutation(FakeGateCtx(doc), doc, "gate", [box]) as applied:
        pass

    assert applied == ["Box"]


def test_outcome_is_filled_before_the_commit() -> None:
    box = FakeShapeObj("Box")
    pad = FakeShapeObj("Pad")
    box.InList = [pad]
    outcome: dict = {}
    at_commit: list[dict] = []

    class RecordingDoc(FakeGateDoc):
        def commitTransaction(self) -> None:
            at_commit.append(dict(outcome))
            super().commitTransaction()

    doc = RecordingDoc([box, pad])
    with mutation(FakeGateCtx(doc), doc, "gate", [box], outcome=outcome):
        pass

    assert at_commit == [
        {"reports": outcome["reports"], "dependentCountBefore": 1, "dependentCountAfter": 1}
    ]


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


def test_shape_is_null_is_false_for_a_real_shape_double() -> None:
    assert shape_is_null(FakeShapeObj("Box").Shape) is False
    assert shape_is_null(None) is True
    assert shape_is_null(object()) is False
