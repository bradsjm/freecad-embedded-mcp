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
    mutation,
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


class CountObj:
    def __init__(self, name: str, in_list: tuple["CountObj", ...] = ()) -> None:
        self.Name = name
        self.InList = list(in_list)


def test_dependent_count_walks_the_transitive_closure() -> None:
    source = CountObj("Source")
    middle = CountObj("Middle", (source,))
    top = CountObj("Top", (middle,))

    assert dependent_count([top]) == 2


def test_dependent_count_is_capped_at_the_limit() -> None:
    fan = [CountObj(f"D{index}") for index in range(8)]
    source = CountObj("Source", tuple(fan))

    assert dependent_count([source], limit=3) == 3
    assert dependent_count([source]) == 8


# ---------------------------------------------------------------------------
# mutation operationState contract.
# ---------------------------------------------------------------------------


class ShapeObj:
    """Minimal valid solid-shaped object for gate drives."""

    def __init__(self, name: str, in_list: tuple[Any, ...] = ()) -> None:
        self.Name = name
        self.TypeId = "Part::Feature"
        self.State: list[str] = []
        self.InList = list(in_list)
        self.Shape = None  # shapeless: valid without a solid contract

    def isValid(self) -> bool:
        return True

    def getStatusString(self) -> str:
        return ""


class GateDoc:
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


class GateApp:
    def getActiveTransaction(self) -> None:
        return None


class GateCtx:
    def __init__(self, doc: GateDoc) -> None:
        self.App = GateApp()
        self._doc = doc

    def check_document_idle(self, doc: GateDoc) -> None:
        pass


def run_gate(doc: GateDoc, obj: ShapeObj, body: Any) -> None:
    with mutation(GateCtx(doc), doc, "gate", [obj]):
        body()


def test_body_failure_with_successful_rollback_reports_rolled_back() -> None:
    obj = ShapeObj("Box")
    doc = GateDoc([obj])

    with pytest.raises(ToolError) as excinfo:
        run_gate(doc, obj, lambda: (_ for _ in ()).throw(RuntimeError("nope")))

    details = excinfo.value.details or {}
    assert details["operationState"] == "rolled_back"
    assert details["nextAction"] == "retry_from_original_state"


def test_failed_abort_reports_rollback_failed() -> None:
    obj = ShapeObj("Box")
    doc = GateDoc([obj])
    doc.fail_abort = True

    with pytest.raises(ToolError) as excinfo:
        run_gate(doc, obj, lambda: (_ for _ in ()).throw(RuntimeError("nope")))

    details = excinfo.value.details or {}
    assert details["operationState"] == "rollback_failed"
    assert details["rollbackFailed"] is True
    assert details["rollbackStage"] == "abort"
    assert details["originalError"] == "RuntimeError: nope"


def test_failed_rollback_recompute_reports_rollback_failed() -> None:
    obj = ShapeObj("Box")
    doc = GateDoc([obj])
    doc.fail_recompute = True

    with pytest.raises(ToolError) as excinfo:
        run_gate(doc, obj, lambda: (_ for _ in ()).throw(RuntimeError("nope")))

    details = excinfo.value.details or {}
    assert details["operationState"] == "rollback_failed"
    assert details["rollbackStage"] == "recompute"


def test_failed_commit_reports_may_have_changed_with_inspect_action() -> None:
    obj = ShapeObj("Box")
    doc = GateDoc([obj])
    doc.fail_commit = True

    with pytest.raises(ToolError) as excinfo:
        run_gate(doc, obj, lambda: None)

    details = excinfo.value.details or {}
    assert details["operationState"] == "may_have_changed"
    assert details["nextAction"] == "inspect_target"
    assert "commit exploded" in details["commitError"]


def test_expected_bounds_failure_after_recompute_rolls_back() -> None:
    obj = ShapeObj("Box")
    doc = GateDoc([obj])

    # document_bounds resolves through the stubbed geometry module in
    # heavier suites; here it returns None (unavailable), which must fail
    # the commit condition and roll back.
    with (
        pytest.raises(ToolError) as excinfo,
        mutation(
            GateCtx(doc),
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
    obj = ShapeObj("Box")
    doc = GateDoc([obj])

    # A busy document is rejected before the transaction opens: nothing
    # started, so no operationState may appear.
    class BusyCtx(GateCtx):
        def check_document_idle(self, doc: GateDoc) -> None:
            raise ToolError(SERVER_BUSY_LIKE, "busy")

    with pytest.raises(ToolError) as excinfo, mutation(BusyCtx(doc), doc, "gate", [obj]):
        pass

    assert (excinfo.value.details or {}).get("operationState") is None
