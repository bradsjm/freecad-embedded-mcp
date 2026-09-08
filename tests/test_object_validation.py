from dataclasses import dataclass, field
from pathlib import Path
import sys


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
