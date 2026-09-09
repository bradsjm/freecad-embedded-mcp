"""Focused tests for mcp_server object tools and the mutation gate.

Runs without FreeCAD: FreeCAD, ObjectsFem and the sibling geometry resolver
are installed as isolated stubs before the modules load (same pattern as
test_object_validation.py).
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.protocol import (
    DOCUMENT_NOT_FOUND,
    OBJECT_NOT_FOUND,
    VALIDATION_FAILED,
    ConsentSigner,
    ToolError,
    validate_schema,
)

# ---------------------------------------------------------------------------
# Stubs installed before the modules under test load.
# ---------------------------------------------------------------------------


class StubVector:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StubVector) and (self.x, self.y, self.z) == (
            other.x,
            other.y,
            other.z,
        )


class StubRotation:
    def __init__(self, axis: StubVector | None = None, angle: float = 0.0) -> None:
        self.Axis = axis or StubVector(0, 0, 1)
        self.Angle = float(angle)


class StubPlacement:
    def __init__(
        self, base: StubVector | None = None, rotation: StubRotation | None = None
    ) -> None:
        self.Base = base or StubVector()
        self.Rotation = rotation or StubRotation()


_STUB_FREECAD = types.ModuleType("FreeCAD")
_STUB_FREECAD.Vector = StubVector
_STUB_FREECAD.Rotation = StubRotation
_STUB_FREECAD.Placement = StubPlacement
_STUB_FREECAD_INSTALL = ("FreeCAD", _STUB_FREECAD)

_STUB_OBJECTSFEM = types.ModuleType("ObjectsFem")
_STUB_FEM_CALLS: list[tuple[str, tuple, dict]] = []


def _record_factory(factory_name: str):
    def factory(doc: Any, name: str = "Fem", **kwargs: Any) -> Any:
        _STUB_FEM_CALLS.append((factory_name, (doc,), {"name": name, **kwargs}))
        return doc.addObject("Fem::Stub", name)

    return factory


_STUB_OBJECTSFEM.makeAnalysis = _record_factory("makeAnalysis")
_STUB_OBJECTSFEM.makeSolverCalculiX = _record_factory("makeSolverCalculiX")
_STUB_OBJECTSFEM.makeMaterialSolid = _record_factory("makeMaterialSolid")
_STUB_OBJECTSFEM.makeMaterialMechanicalNonlinear = _record_factory(
    "makeMaterialMechanicalNonlinear"
)
_STUB_OBJECTSFEM_INSTALL = ("ObjectsFem", _STUB_OBJECTSFEM)

_STUB_GEOMETRY = types.ModuleType("mcp_server.tools.geometry")
_NUMERIC_SUB = re.compile(r"^(Face|Edge|Vertex|Wire)\d+$")


def _stub_resolve_reference(ctx: Any, doc: Any, reference: dict) -> tuple[Any, str]:
    name = reference.get("object")
    subelement = reference.get("subelement") or ""
    if _NUMERIC_SUB.match(subelement):
        raise ToolError(
            VALIDATION_FAILED,
            "numeric topology selectors are not durable; use signed tokens",
        )
    obj = doc.getObject(name)
    if obj is None:
        raise ToolError(OBJECT_NOT_FOUND, f"object '{name}' not found")
    return obj, subelement


_STUB_GEOMETRY.resolve_reference = _stub_resolve_reference


def _stub_placed_shape(obj: Any) -> Any:
    """No-ancestor stand-in for geometry.placed_shape (global == local)."""

    shape = getattr(obj, "Shape", None)
    if shape is None:
        raise ToolError(
            VALIDATION_FAILED,
            "Cannot resolve document-space geometry",
            {"object": str(getattr(obj, "Name", ""))},
        )
    return shape


_STUB_GEOMETRY.placed_shape = _stub_placed_shape
_STUB_GEOMETRY_INSTALL = ("mcp_server.tools.geometry", _STUB_GEOMETRY)

_STUB_MODULES = (
    _STUB_FREECAD_INSTALL,
    _STUB_OBJECTSFEM_INSTALL,
    _STUB_GEOMETRY_INSTALL,
)
_SAVED_MODULES: dict[str, types.ModuleType | None] = {}


def _install_stubs() -> None:
    import mcp_server.tools as tools_pkg

    for name, stub in _STUB_MODULES:
        _SAVED_MODULES[name] = sys.modules.get(name)
        sys.modules[name] = stub
    tools_pkg.geometry = _STUB_GEOMETRY


def _uninstall_stubs() -> None:
    import mcp_server.tools as tools_pkg

    for name, _ in _STUB_MODULES:
        saved = _SAVED_MODULES.pop(name, None)
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved
    if getattr(tools_pkg, "geometry", None) is _STUB_GEOMETRY:
        del tools_pkg.geometry


_install_stubs()
try:
    from mcp_server import object_validation as ov
    from mcp_server.tools import objects as objects_mod
    from mcp_server.tools import parameters as parameters_mod
finally:
    _uninstall_stubs()


@pytest.fixture(autouse=True)
def _stub_runtime():
    """Reinstall the isolated modules for each test, restore afterwards."""

    _install_stubs()
    yield
    _uninstall_stubs()


# ---------------------------------------------------------------------------
# Fake FreeCAD domain.
# ---------------------------------------------------------------------------


class FakeApp:
    def __init__(self) -> None:
        self.active_transaction: Any = None

    def getActiveTransaction(self) -> Any:
        return self.active_transaction


class FakeShape:
    def __init__(
        self,
        *,
        valid: bool = True,
        solids: int = 1,
        volume: float = 1000.0,
        bounds: tuple[float, ...] = (0.0, 0.0, 0.0, 10.0, 10.0, 10.0),
        check: list[str] | None = None,
        tolerance: float = 1e-7,
    ) -> None:
        self._valid = valid
        self._solids = solids
        self.Volume = volume
        self._bounds = bounds
        self._check = check or []
        self._tolerance = tolerance

    def isValid(self) -> bool:
        return self._valid

    @property
    def Solids(self) -> list[Any]:
        return [object()] * self._solids

    @property
    def BoundBox(self) -> Any:
        box = types.SimpleNamespace()
        for name, value in zip(
            ("XMin", "YMin", "ZMin", "XMax", "YMax", "ZMax"),
            self._bounds,
            strict=True,
        ):
            setattr(box, name, value)
        return box

    def check(self) -> list[str]:
        return list(self._check)

    def getTolerance(self, _: int) -> float:
        return self._tolerance


class FakeObj:
    """Declared-property writes are recorded; blocked writes raise."""

    def __init__(
        self,
        name: str,
        *,
        label: str | None = None,
        TypeId: str = "Part::Feature",
        state: tuple[str, ...] = (),
        valid: bool = True,
        status: str = "",
        properties: tuple[str, ...] = (),
        prop_types: dict[str, str] | None = None,
        prop_status: dict[str, list[str]] | None = None,
        enumerations: dict[str, list[str]] | None = None,
        in_list: tuple[Any, ...] = (),
        out_list: tuple[Any, ...] = (),
        shape: Any = None,
        is_body: bool = False,
        tip: Any = None,
        values: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "Name", name)
        object.__setattr__(self, "Label", label or name)
        object.__setattr__(self, "TypeId", TypeId)
        object.__setattr__(self, "_state", list(state))
        object.__setattr__(self, "_valid", valid)
        object.__setattr__(self, "_status", status)
        object.__setattr__(self, "PropertiesList", list(properties))
        object.__setattr__(self, "_types", dict(prop_types or {}))
        object.__setattr__(self, "_prop_status", dict(prop_status or {}))
        object.__setattr__(self, "_enums", dict(enumerations or {}))
        object.__setattr__(self, "InList", list(in_list))
        object.__setattr__(self, "OutList", list(out_list))
        object.__setattr__(self, "_shape", shape)
        object.__setattr__(self, "_is_body", is_body)
        object.__setattr__(self, "_tip", tip)
        object.__setattr__(self, "_values", dict(values or {}))
        object.__setattr__(self, "_blocked", set())
        object.__setattr__(self, "history", [])

    @property
    def State(self) -> list[str]:
        return list(self._state)

    @property
    def Shape(self) -> Any:
        return self._shape

    def isValid(self) -> bool:
        return self._valid

    def getStatusString(self) -> str:
        return self._status

    def getTypeIdOfProperty(self, prop: str) -> str:
        return self._types[prop]

    def getPropertyStatus(self, prop: str) -> list[str]:
        return list(self._prop_status.get(prop, ()))

    def getEnumerationsOfProperty(self, prop: str) -> list[str]:
        return list(self._enums[prop])

    def isDerivedFrom(self, type_id: str) -> bool:
        return type_id == "PartDesign::Body" and self._is_body

    @property
    def Tip(self) -> Any:
        return self._tip

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in object.__getattribute__(self, "_types"):
            if name in object.__getattribute__(self, "_blocked"):
                raise RuntimeError(f"cannot set {name}")
            self.history.append((name, value))
            self._values[name] = value
            return
        object.__setattr__(self, name, value)

    def block(self, prop: str) -> None:
        """Make the next FreeCAD-level assignment of ``prop`` raise."""

        self._blocked.add(prop)
        self._values.pop(prop, None)


class FakeDoc:
    """Transactions snapshot/restore declared property values and membership."""

    def __init__(
        self,
        name: str = "Doc",
        *,
        objects: list[FakeObj] | None = None,
        supported: tuple[str, ...] = (
            "Part::Box",
            "Part::Feature",
            "App::DocumentObjectGroup",
        ),
    ) -> None:
        self.Name = name
        self.Label = name
        self.Objects: list[Any] = list(objects or [])
        self._supported = tuple(supported)
        self.calls: list[tuple[str, Any]] = []
        self.UndoMode = 0
        self.HasPendingTransaction = False
        self.recompute_count = 0
        self.generation = 1
        self.identity = f"identity:{name}"
        self.recompute_observers: list[Any] = []
        self._by_name: dict[str, FakeObj] = {obj.Name: obj for obj in self.Objects}
        self._tx: list[tuple[FakeObj, dict, list[str]]] | None = None

    def supportedTypes(self) -> tuple[str, ...]:
        return self._supported

    def getObject(self, name: str) -> Any:
        found = self._by_name.get(name)
        if found is not None and found in self.Objects:
            return found
        return None

    def addObject(self, type_id: str, name: str) -> FakeObj:
        actual = name
        if any(obj.Name == actual for obj in self.Objects):
            actual = f"{name}001"
        obj = FakeObj(actual, TypeId=type_id)
        self.Objects.append(obj)
        self._by_name[obj.Name] = obj
        return obj

    def removeObject(self, name: str) -> None:
        obj = self.getObject(name)
        if obj is None:
            raise RuntimeError(f"object {name} not found")
        self.Objects.remove(obj)
        self._by_name.pop(name, None)

    def recompute(self) -> None:
        self.recompute_count += 1
        for obj in self.Objects:
            obj._state[:] = []
        for observer in self.recompute_observers:
            observer()

    def openTransaction(self, label: str) -> None:
        self.calls.append(("open", label))
        self._tx = [(obj, dict(obj._values), list(obj._state)) for obj in self.Objects]

    def commitTransaction(self) -> None:
        self.calls.append(("commit", None))
        self._tx = None

    def abortTransaction(self) -> None:
        self.calls.append(("abort", None))
        snapshot = self._tx
        self._tx = None
        if snapshot is None:
            return
        for obj, values, state in snapshot:
            obj._values.clear()
            obj._values.update(values)
            obj._state[:] = state
            obj.history.clear()
        self.Objects = [entry[0] for entry in snapshot]
        self._by_name = {obj.Name: obj for obj in self.Objects}


class FakeCtx:
    def __init__(self, doc: FakeDoc, app: FakeApp | None = None) -> None:
        self.App = app or FakeApp()
        self.signer = ConsentSigner()
        self._doc = doc
        self.idle_error: ToolError | None = None

    def document_generation(self, doc: FakeDoc) -> int:
        return int(doc.generation)

    def document_identity(self, doc: FakeDoc) -> str:
        return str(doc.identity)

    def require_document(self, name: str) -> FakeDoc:
        if name != self._doc.Name:
            raise ToolError(DOCUMENT_NOT_FOUND, f"document '{name}' not found")
        return self._doc

    def require_object(self, doc: FakeDoc, name: str) -> Any:
        obj = doc.getObject(name)
        if obj is None:
            raise ToolError(OBJECT_NOT_FOUND, f"object '{name}' not found")
        return obj

    def check_document_idle(self, doc: FakeDoc) -> None:
        if self.idle_error is not None:
            raise self.idle_error


_BOX_PROPS = ("Length", "Width", "Height", "Placement", "Base")
_BOX_TYPES = {
    "Length": "App::PropertyLength",
    "Width": "App::PropertyLength",
    "Height": "App::PropertyLength",
    "Placement": "App::PropertyPlacement",
    "Base": "App::PropertyLink",
}


def expect_tool_error(exc_info: Any, code: str) -> ToolError:
    error = exc_info.value
    assert isinstance(error, ToolError)
    assert error.code == code
    return error


# ---------------------------------------------------------------------------
# Registration: schemas are finite and checkable.
# ---------------------------------------------------------------------------


def box(
    name: str = "Box",
    *,
    shape: Any | None = None,
    properties: tuple[str, ...] | None = None,
    prop_types: dict[str, str] | None = None,
    **kwargs: Any,
) -> FakeObj:
    merged_types = dict(_BOX_TYPES)
    if prop_types:
        merged_types.update(prop_types)
    defaults = {
        "Length": 0.0,
        "Width": 0.0,
        "Height": 0.0,
        "Placement": StubPlacement(),
        "Base": None,
    }
    names = tuple(properties) if properties is not None else _BOX_PROPS
    values = kwargs.pop("values", None) or {}
    return FakeObj(
        name,
        properties=names,
        prop_types=merged_types,
        shape=shape if shape is not None else FakeShape(),
        values={
            **{key: value for key, value in defaults.items() if key in names},
            **values,
        },
        **kwargs,
    )


# ---------------------------------------------------------------------------
# edit_object prevalidation.
# ---------------------------------------------------------------------------


def test_invalid_second_property_leaves_first_unchanged() -> None:
    doc = FakeDoc(objects=[box()])
    obj = doc.getObject("Box")
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Box",
                "properties": {"Length": 5, "NotAProperty": 1},
            },
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert obj.history == []
    assert obj.Length == 0
    assert doc.calls == []
    assert doc.recompute_count == 0


def test_read_only_property_is_rejected_before_transaction() -> None:
    doc = FakeDoc(objects=[box(prop_status={"Length": ["ReadOnly"]})])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "read-only" in exc_info.value.message
    assert doc.calls == []


def test_missing_reference_makes_no_mutation() -> None:
    doc = FakeDoc(objects=[box()])
    obj = doc.getObject("Box")
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Box",
                "properties": {"Base": {"object": "Ghost"}},
            },
        )

    expect_tool_error(exc_info, OBJECT_NOT_FOUND)
    assert obj.history == []
    assert doc.calls == []


def test_numeric_subelement_is_rejected() -> None:
    doc = FakeDoc(objects=[box()])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Box",
                "properties": {"Base": {"object": "Box", "subelement": "Face7"}},
            },
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert doc.calls == []


def test_edit_applies_converted_values_and_commits() -> None:
    doc = FakeDoc(objects=[box(), box("Other", shape=None)])
    ctx = FakeCtx(doc)

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Box",
            "properties": {
                "Length": 12,
                "Placement": {
                    "Position": {"x": 1, "y": 2, "z": 3},
                    "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 90},
                },
                "Base": {"object": "Other"},
            },
        },
    )

    obj = doc.getObject("Box")
    assert obj.Length == 12
    assert obj.Placement.Base == StubVector(1, 2, 3)
    assert obj.Placement.Rotation.Angle == 90
    assert obj.Base is doc.getObject("Other")
    assert result["object"]["name"] == "Box"
    assert result["applied"] == ["Box"]
    assert ("commit", None) in doc.calls
    assert doc.UndoMode == 0
    validate_schema(result, objects_mod.TOOL_DEFINITIONS[2]["outputSchema"])


def test_fuzzy_tolerance_rejected_when_not_a_property() -> None:
    doc = FakeDoc(objects=[box()])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Box",
                "properties": {"FuzzyTolerance": 0.1},
            },
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert doc.calls == []


# ---------------------------------------------------------------------------
# Recompute validation and rollback.
# ---------------------------------------------------------------------------


def test_valid_target_with_invalid_dependent_shape_rolls_back() -> None:
    # An observably invalid preexisting dependent shape is never
    # grandfathered by the baseline solid-count contract.
    dependent = FakeObj(
        "Broken",
        TypeId="Part::Feature",
        shape=FakeShape(valid=False, check=["free edge"]),
    )
    doc = FakeDoc(objects=[box(in_list=(dependent,)), dependent])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert any(
        "Broken" in message and "invalid shape" in message for message in error.details["errors"]
    )
    assert ("abort", None) in doc.calls
    assert ("commit", None) not in doc.calls
    assert doc.getObject("Box").Length == 0


def test_preexisting_multisolid_dependent_with_unchanged_count_accepts_edit() -> None:
    dependent = FakeObj("Multi", TypeId="Part::Feature", shape=FakeShape(solids=2))
    doc = FakeDoc(objects=[box(in_list=(dependent,)), dependent])
    ctx = FakeCtx(doc)

    result = objects_mod.edit_object(
        ctx,
        {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
    )

    # The preexisting valid multisolid contract is grandfathered: an edit
    # that leaves the dependent's solid count unchanged commits.
    assert result["applied"] == ["Box"]
    assert result["report"]["ok"] is True
    assert ("commit", None) in doc.calls
    assert ("abort", None) not in doc.calls
    assert len(doc.getObject("Multi").Shape.Solids) == 2


def test_dependent_gaining_solids_during_recompute_rolls_back() -> None:
    dependent = FakeObj("Split", TypeId="Part::Feature", shape=FakeShape(solids=1))
    doc = FakeDoc(objects=[box(in_list=(dependent,)), dependent])
    ctx = FakeCtx(doc)

    def fuse_second_solid() -> None:
        # The recomputed geometry itself creates the topology change; this
        # also fires during the post-abort rollback recompute, which is fine.
        object.__setattr__(dependent, "_shape", FakeShape(solids=2))

    doc.recompute_observers.append(fuse_second_solid)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    # A previously single-solid dependent turning multisolid is a topology
    # change, not a grandfathered multisolid contract.
    assert any(
        "Split" in message and "expected_solids" in message for message in error.details["errors"]
    )
    assert ("abort", None) in doc.calls
    assert ("commit", None) not in doc.calls
    # The post-abort rollback recompute still ran and restored the target.
    assert doc.recompute_count == 2
    assert doc.getObject("Box").Length == 0


def test_overwide_dependent_closure_is_refused_before_effects() -> None:
    dependents = [FakeObj(f"Dep{i:03d}") for i in range(260)]
    target = box("Hub", in_list=tuple(dependents))
    doc = FakeDoc(objects=[target, *dependents])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Hub", "properties": {"Length": 5}},
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details == {"reason": "too_many_dependents"}
    assert doc.calls == []
    assert doc.recompute_count == 0
    assert target in doc.Objects


def test_failed_recompute_rolls_back_and_restores_undo_mode() -> None:
    doc = FakeDoc(objects=[box()])
    obj = doc.getObject("Box")

    def break_object() -> None:
        object.__setattr__(obj, "_valid", False)
        object.__setattr__(obj, "_status", "Linked shape object is empty")
        object.__setattr__(obj, "_state", ["Error"])

    doc.recompute_observers.append(break_object)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "rolled back" in error.message
    assert ("abort", None) in doc.calls
    assert ("commit", None) not in doc.calls
    assert obj.history == []
    assert obj.Length == 0
    assert doc.UndoMode == 0


def test_dependents_are_validated_after_recompute() -> None:
    dependent = FakeObj("Pad", TypeId="PartDesign::Pad")
    doc = FakeDoc(objects=[box(in_list=(dependent,)), dependent])
    ctx = FakeCtx(doc)

    def break_dependent() -> None:
        object.__setattr__(dependent, "_state", ["Invalid"])

    doc.recompute_observers.append(break_dependent)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert any("Pad" in message for message in error.details["errors"])
    assert ("abort", None) in doc.calls


def test_assignment_failure_with_failing_rollback_is_explicit() -> None:
    doc = FakeDoc(objects=[box()])
    obj = doc.getObject("Box")
    obj.block("Length")

    def failing_abort() -> None:
        doc.calls.append(("abort", None))
        raise RuntimeError("undo stack corrupted")

    doc.abortTransaction = failing_abort  # type: ignore[method-assign]
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["rollbackFailed"] is True
    assert "undo stack corrupted" in error.message
    assert obj.history == []


class _StaleAbortDoc(FakeDoc):
    """FakeDoc variant mimicking native FreeCAD rollback behavior: abort
    restores values and membership but leaves Touched/Invalid cached state
    until the next recompute clears it."""

    def abortTransaction(self) -> None:
        self.calls.append(("abort", None))
        snapshot = self._tx
        self._tx = None
        if snapshot is None:
            return
        for obj, values, _state in snapshot:
            obj._values.clear()
            obj._values.update(values)
            obj._state[:] = ["Touched", "Invalid"]
            obj.history.clear()
        self.Objects = [entry[0] for entry in snapshot]
        self._by_name = {obj.Name: obj for obj in self.Objects}

    def recompute(self) -> None:
        self.calls.append(("recompute", None))
        super().recompute()


def test_failed_assignment_rollback_recomputes_stale_native_state() -> None:
    doc = _StaleAbortDoc(objects=[box()])
    obj = doc.getObject("Box")
    obj.block("Length")
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "cannot set Length" in error.message
    assert doc.calls == [
        ("open", "edit_object:Box"),
        ("abort", None),
        ("recompute", None),
    ]
    assert obj.history == []
    assert obj.State == []


def test_failed_validation_rollback_recomputes_dependents_stale_state() -> None:
    dependent = FakeObj("Multi", TypeId="Part::Feature", shape=FakeShape(solids=1))
    doc = _StaleAbortDoc(objects=[box(in_list=(dependent,)), dependent])
    ctx = FakeCtx(doc)

    def fuse_second_solid() -> None:
        # Creates the single-to-multisolid topology change the mutation
        # must reject; also fires during the post-abort rollback recompute,
        # which is harmless because the assertions check restored state.
        object.__setattr__(dependent, "_shape", FakeShape(solids=2))

    doc.recompute_observers.append(fuse_second_solid)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    # The original failure and its diagnostics survive the rollback intact.
    assert any(
        "Multi" in message and "expected_solids" in message for message in error.details["errors"]
    )
    assert doc.calls == [
        ("open", "edit_object:Box"),
        ("recompute", None),
        ("abort", None),
        ("recompute", None),
    ]
    obj = doc.getObject("Box")
    assert obj.Length == 0
    assert obj.history == []
    assert obj.State == []
    assert dependent.State == []


def test_rollback_recompute_failure_is_explicit_and_preserves_original() -> None:
    class RaisingRecomputeDoc(_StaleAbortDoc):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._recomputes = 0

        def recompute(self) -> None:
            self._recomputes += 1
            if self._recomputes > 1:
                raise RuntimeError("undo recompute exploded")
            super().recompute()

    dependent = FakeObj("Multi", TypeId="Part::Feature", shape=FakeShape(solids=1))

    def fuse_second_solid() -> None:
        # The recompute creates the topology change the mutation rejects.
        object.__setattr__(dependent, "_shape", FakeShape(solids=2))

    doc = RaisingRecomputeDoc(objects=[box(in_list=(dependent,)), dependent])
    doc.recompute_observers.append(fuse_second_solid)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["rollbackFailed"] is True
    assert error.details["rollbackStage"] == "recompute"
    assert "rollback recompute" in error.message
    assert "undo recompute exploded" in error.message
    # The original mutation failure is preserved with its diagnostics; the
    # rollback is never claimed as valid.
    assert error.details["originalError"] == (
        "ToolError: recompute left the document invalid; the mutation was rolled back"
    )
    assert any("Multi" in message for message in error.details["originalDetails"]["errors"])
    assert doc.UndoMode == 0
    assert doc.getObject("Box").Length == 0


def test_user_pending_transaction_is_rejected() -> None:
    doc = FakeDoc(objects=[box()])
    doc.HasPendingTransaction = True
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert exc_info.value.details == {"reason": "pending_transaction"}
    assert doc.calls == []


def test_active_app_transaction_is_rejected() -> None:
    doc = FakeDoc(objects=[box()])
    app = FakeApp()
    app.active_transaction = "user-transaction"
    ctx = FakeCtx(doc, app)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert exc_info.value.details == {"reason": "user_transaction_active"}
    assert doc.calls == []


def test_busy_document_is_rejected_via_ctx() -> None:
    doc = FakeDoc(objects=[box()])
    ctx = FakeCtx(doc)
    ctx.idle_error = ToolError("SOLVER_FAILED", "document is solving")

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {"document": doc.Name, "object": "Box", "properties": {"Length": 5}},
        )

    expect_tool_error(exc_info, "SOLVER_FAILED")
    assert doc.calls == []


def test_undo_mode_is_preserved_for_enabled_documents() -> None:
    doc = FakeDoc(objects=[box()])
    doc.UndoMode = 2
    ctx = FakeCtx(doc)

    objects_mod.edit_object(
        ctx, {"document": doc.Name, "object": "Box", "properties": {"Length": 5}}
    )

    assert doc.UndoMode == 2
    assert doc.getObject("Box").Length == 5


# ---------------------------------------------------------------------------
# create_object.
# ---------------------------------------------------------------------------


def test_create_generic_object_returns_actual_identity() -> None:
    doc = FakeDoc()
    ctx = FakeCtx(doc)

    result = objects_mod.create_object(
        ctx, {"document": doc.Name, "type": "Part::Box", "name": "Box"}
    )

    created = doc.getObject("Box")
    assert created.TypeId == "Part::Box"
    assert result["object"]["name"] == "Box"
    assert result["applied"] == ["Box"]
    assert result["report"]["ok"] is True
    assert result["report"]["solid_count"] is None  # shapeless stub object
    assert ("commit", None) in doc.calls
    validate_schema(result, objects_mod.TOOL_DEFINITIONS[1]["outputSchema"])


def test_create_rejects_unsupported_type_without_transaction() -> None:
    doc = FakeDoc()
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_object(ctx, {"document": doc.Name, "type": "Part::NotReal", "name": "X"})

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert doc.calls == []
    assert doc.recompute_count == 0


def test_create_shapeless_group_is_valid() -> None:
    doc = FakeDoc()
    ctx = FakeCtx(doc)

    result = objects_mod.create_object(
        ctx,
        {"document": doc.Name, "type": "App::DocumentObjectGroup", "name": "Grp"},
    )

    assert result["report"]["shape_valid"] is None
    assert result["report"]["ok"] is True
    assert ("commit", None) in doc.calls


def test_create_multisolid_requires_explicit_expected_solids() -> None:
    doc = FakeDoc()

    def fuse() -> None:
        # Also fires during the post-abort rollback recompute, when the
        # created object is already gone.
        if not doc.Objects:
            return
        created = doc.Objects[-1]
        object.__setattr__(created, "_shape", FakeShape(solids=2))

    doc.recompute_observers.append(fuse)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_object(ctx, {"document": doc.Name, "type": "Part::Box", "name": "Fused"})

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "expected_solids" in exc_info.value.details["errors"][0]
    assert ("abort", None) in doc.calls
    assert doc.getObject("Fused") is None

    result = objects_mod.create_object(
        ctx,
        {
            "document": doc.Name,
            "type": "Part::Box",
            "name": "Fused",
            "expected_solids": 2,
        },
    )
    assert result["report"]["ok"] is True
    assert result["report"]["solid_count"] == 2


def test_create_fem_material_uses_explicit_factory() -> None:
    doc = FakeDoc()
    ctx = FakeCtx(doc)
    _STUB_FEM_CALLS.clear()

    result = objects_mod.create_object(
        ctx, {"document": doc.Name, "type": "Fem::MaterialCommon", "name": "Mat"}
    )

    assert [call[0] for call in _STUB_FEM_CALLS] == ["makeMaterialSolid"]
    assert _STUB_FEM_CALLS[0][2]["name"] == "Mat"
    assert result["object"]["typeId"] == "Fem::Stub"
    assert ("commit", None) in doc.calls


def test_create_fem_nonlinear_material_requires_base_material_link() -> None:
    doc = FakeDoc()
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_object(
            ctx,
            {
                "document": doc.Name,
                "type": "Fem::MaterialMechanicalNonlinear",
                "name": "NL",
            },
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "BaseMaterial" in exc_info.value.message
    assert doc.calls == []


def test_create_fem_mesh_type_is_refused_with_guidance() -> None:
    doc = FakeDoc()
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_object(
            ctx, {"document": doc.Name, "type": "Fem::FemMeshGmsh", "name": "Mesh"}
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "run_script" in exc_info.value.message
    assert doc.calls == []


def test_create_invalid_property_rolls_back_created_object() -> None:
    doc = FakeDoc()
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_object(
            ctx,
            {
                "document": doc.Name,
                "type": "Part::Box",
                "name": "Box",
                "properties": {"Length": 5, "NotAProperty": 1},
            },
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert ("abort", None) in doc.calls
    assert ("commit", None) not in doc.calls
    assert doc.getObject("Box") is None


# ---------------------------------------------------------------------------
# delete_object.
# ---------------------------------------------------------------------------


def test_delete_refuses_object_with_dependents() -> None:
    dependent = FakeObj("Pad")
    target = box("Sketch", shape=None, in_list=(dependent,))
    doc = FakeDoc(objects=[target, dependent])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.delete_object(ctx, {"document": doc.Name, "object": "Sketch"})

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["dependents"] == ["Pad"]
    assert target in doc.Objects
    assert doc.calls == []


def test_delete_without_dependents_removes_and_commits() -> None:
    doc = FakeDoc(objects=[box("Lonely")])
    ctx = FakeCtx(doc)

    result = objects_mod.delete_object(ctx, {"document": doc.Name, "object": "Lonely"})

    assert result["removed"]["name"] == "Lonely"
    assert result["removed"]["typeId"] == "Part::Feature"
    assert result["applied"] == ["Lonely"]
    assert doc.getObject("Lonely") is None
    assert ("commit", None) in doc.calls


# ---------------------------------------------------------------------------
# inspect_objects pagination.
# ---------------------------------------------------------------------------


def _three_box_doc() -> tuple[FakeDoc, FakeCtx]:
    doc = FakeDoc(objects=[box("B"), box("A"), box("C", shape=None)])
    return doc, FakeCtx(doc)


def test_inspect_returns_sorted_compact_rows() -> None:
    doc, ctx = _three_box_doc()

    result = objects_mod.inspect_objects(ctx, {"document": doc.Name, "limit": 2})

    names = [row["name"] for row in result["objects"]]
    assert names == ["A", "B"]
    assert result["total"] == 3
    assert result["count"] == 2
    assert result["nextCursor"] is not None
    row = result["objects"][0]
    assert set(row) == {
        "name",
        "label",
        "typeId",
        "state",
        "placement",
        "globalPlacement",
        "boundsCoordinateSystem",
        "bounds",
        "shape_valid",
        "solid_count",
        "tip",
        "links",
        "properties",
        "propertyMetadata",
        "propertyCount",
        "nextPropertyOffset",
        "truncatedProperties",
    }
    assert row["properties"] == {}
    validate_schema(result, objects_mod.TOOL_DEFINITIONS[0]["outputSchema"])


def test_inspect_pagination_walks_all_objects() -> None:
    doc, ctx = _three_box_doc()

    first = objects_mod.inspect_objects(ctx, {"document": doc.Name, "limit": 2})
    second = objects_mod.inspect_objects(
        ctx, {"document": doc.Name, "limit": 2, "cursor": first["nextCursor"]}
    )

    names = [row["name"] for row in first["objects"] + second["objects"]]
    assert names == ["A", "B", "C"]
    assert second["nextCursor"] is None


def test_stale_cursor_after_generation_change_is_rejected() -> None:
    doc, ctx = _three_box_doc()

    first = objects_mod.inspect_objects(ctx, {"document": doc.Name, "limit": 2})
    doc.generation += 1

    with pytest.raises(ToolError) as exc_info:
        objects_mod.inspect_objects(
            ctx,
            {"document": doc.Name, "limit": 2, "cursor": first["nextCursor"]},
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details == {"reason": "stale_cursor"}


def test_cursor_with_changed_filters_is_rejected() -> None:
    doc, ctx = _three_box_doc()

    first = objects_mod.inspect_objects(
        ctx,
        {
            "document": doc.Name,
            "limit": 2,
            "detail": "full",
            "property_filter": ["Length"],
        },
    )

    with pytest.raises(ToolError) as exc_info:
        objects_mod.inspect_objects(
            ctx,
            {
                "document": doc.Name,
                "limit": 2,
                "detail": "full",
                "property_filter": ["Width"],
                "cursor": first["nextCursor"],
            },
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)


def test_full_detail_uses_typed_unavailable_markers() -> None:
    doc, ctx = _three_box_doc()

    result = objects_mod.inspect_objects(
        ctx,
        {
            "document": doc.Name,
            "detail": "full",
            "property_filter": ["Length", "Placement", "NotThere"],
        },
    )

    properties = result["objects"][0]["properties"]
    assert properties["Length"] == 0
    assert properties["Placement"]["position"] == [0.0, 0.0, 0.0]
    assert properties["NotThere"] == {"unavailable": "no-such-property"}


def test_empty_document_returns_empty_page() -> None:
    doc = FakeDoc()
    ctx = FakeCtx(doc)

    result = objects_mod.inspect_objects(ctx, {"document": doc.Name})

    assert result == {
        "document": doc.Name,
        "generation": 1,
        "detail": "compact",
        "total": 0,
        "count": 0,
        "objects": [],
        "nextCursor": None,
    }


def test_inspect_reports_body_tip_and_links() -> None:
    tip = FakeObj("Tip", shape=FakeShape())
    body = box(
        "Body",
        TypeId="PartDesign::Body",
        shape=None,
        is_body=True,
        tip=tip,
        out_list=(tip,),
    )
    doc = FakeDoc(objects=[body, tip])
    ctx = FakeCtx(doc)

    result = objects_mod.inspect_objects(ctx, {"document": doc.Name})

    row = result["objects"][0]
    assert row["tip"] == "Tip"
    assert row["links"] == ["Tip"]


# ---------------------------------------------------------------------------
# geometry_report / object_validity_error contracts.
# ---------------------------------------------------------------------------


def test_geometry_report_solid_contract() -> None:
    solid = FakeObj("S", shape=FakeShape(solids=1))
    multi = FakeObj("M", shape=FakeShape(solids=2))
    shapeless = FakeObj("G", shape=None)

    assert ov.geometry_report(solid)["ok"] is True
    multi_report = ov.geometry_report(multi)
    assert multi_report["ok"] is False
    assert "expected_solids" in multi_report["error"]
    assert ov.geometry_report(multi, expected_solids=2)["ok"] is True
    shapeless_report = ov.geometry_report(shapeless)
    assert shapeless_report["shape_valid"] is None
    assert shapeless_report["ok"] is True
    assert ov.geometry_report(shapeless, expected_solids=1)["ok"] is False


def test_geometry_report_requires_positive_volume_with_solids() -> None:
    negative = FakeObj("Neg", shape=FakeShape(solids=1, volume=-5.0))
    zero = FakeObj("Zero", shape=FakeShape(solids=2, volume=0.0))

    negative_report = ov.geometry_report(negative)
    assert negative_report["ok"] is False
    assert "Neg" in negative_report["error"]
    assert "positive volume" in negative_report["error"]
    # The volume gate runs ahead of the count contract, so even an
    # explicit expected_solids match cannot excuse non-positive volume.
    assert ov.geometry_report(zero, expected_solids=2)["ok"] is False
    assert "Zero" in ov.geometry_report(zero)["error"]
    assert "positive volume" in ov.geometry_report(zero)["error"]


def test_geometry_report_invalid_shape_and_diagnostics() -> None:
    bad = FakeObj("Bad", shape=FakeShape(valid=False, check=["free edge"]))
    report = ov.geometry_report(bad)
    assert report["ok"] is False
    assert report["shape_valid"] is False
    assert report["diagnostics"] == ["free edge"]
    assert report["max_tolerance"] == 1e-7
    assert report["bounds"] == [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]


def test_touched_state_is_rejected_by_validity_check() -> None:
    obj = FakeObj("X", state=("Touched",), status="Touched")

    error = ov.object_validity_error(obj)
    assert error is not None
    assert "Touched" in error


def test_document_objects_are_never_stringified_in_output() -> None:
    doc, ctx = _three_box_doc()
    linked = box("LinkTarget", shape=None)
    source = box(
        "Source",
        shape=None,
        properties=("Length", "Link"),
        prop_types={"Length": "App::PropertyLength", "Link": "App::PropertyLink"},
        values={"Link": linked},
    )
    doc.Objects.append(source)
    doc._by_name[source.Name] = source

    result = objects_mod.inspect_objects(
        ctx,
        {"document": doc.Name, "detail": "full", "property_filter": ["Link"]},
    )

    row = next(r for r in result["objects"] if r["name"] == "Source")
    assert row["properties"]["Link"] == {
        "object": "LinkTarget",
        "subelement": "",
    }


# ---------------------------------------------------------------------------
# Property paging (full detail), metadata and truncation.
# ---------------------------------------------------------------------------


def _paging_doc() -> tuple[FakeDoc, FakeCtx, FakeObj]:
    obj = FakeObj(
        "Box",
        properties=("Visibility", "Length", "Placement", "Type"),
        prop_types={
            "Visibility": "App::PropertyBool",
            "Length": "App::PropertyLength",
            "Placement": "App::PropertyPlacement",
            "Type": "App::PropertyEnumeration",
        },
        prop_status={"Placement": ["ReadOnly"]},
        enumerations={"Type": ["Box", "Cylinder", "Sphere"]},
        values={"Visibility": True, "Length": 10.0, "Type": "Box"},
    )
    doc = FakeDoc(objects=[obj])
    return doc, FakeCtx(doc), obj


def test_property_paging_discovers_metadata_without_known_names() -> None:
    doc, ctx, _obj = _paging_doc()

    first = objects_mod.inspect_objects(
        ctx, {"document": doc.Name, "detail": "full", "property_limit": 2}
    )
    row = first["objects"][0]
    assert row["propertyCount"] == 4
    assert list(row["properties"]) == ["Length", "Placement"]
    assert row["nextPropertyOffset"] == 2
    # Disclosed mutability and enumeration choices.
    assert row["propertyMetadata"]["Length"] == {
        "type": "App::PropertyLength",
        "readOnly": False,
        "enumeration": None,
        "enumerationCount": 0,
        "enumerationTruncated": False,
        "expression": None,
    }
    assert row["propertyMetadata"]["Placement"]["readOnly"] is True

    second = objects_mod.inspect_objects(
        ctx,
        {
            "document": doc.Name,
            "detail": "full",
            "property_offset": 2,
            "property_limit": 2,
        },
    )
    row2 = second["objects"][0]
    assert list(row2["properties"]) == ["Type", "Visibility"]
    assert row2["nextPropertyOffset"] is None
    assert row2["propertyMetadata"]["Type"]["enumeration"] == [
        "Box",
        "Cylinder",
        "Sphere",
    ]
    assert row2["propertyMetadata"]["Type"]["enumerationCount"] == 3


def test_property_offset_past_end_preserves_total() -> None:
    doc, ctx, _obj = _paging_doc()
    result = objects_mod.inspect_objects(
        ctx, {"document": doc.Name, "detail": "full", "property_offset": 99}
    )
    row = result["objects"][0]
    assert row["properties"] == {}
    assert row["propertyMetadata"] == {}
    assert row["propertyCount"] == 4
    assert row["nextPropertyOffset"] is None


def test_overlong_list_value_is_truncated_and_named() -> None:
    obj = FakeObj(
        "List",
        properties=("History",),
        prop_types={"History": "App::PropertyStringList"},
        values={"History": [f"step-{index}" for index in range(70)]},
    )
    doc = FakeDoc(objects=[obj])
    result = objects_mod.inspect_objects(
        _ctx := FakeCtx(doc), {"document": doc.Name, "detail": "full"}
    )
    row = result["objects"][0]
    assert len(row["properties"]["History"]) == 64
    assert row["properties"]["History"][-1] == "step-63"
    assert row["truncatedProperties"] == ["History"]


def test_missing_filtered_property_keeps_null_metadata() -> None:
    doc, ctx, _obj = _paging_doc()
    result = objects_mod.inspect_objects(
        ctx,
        {
            "document": doc.Name,
            "detail": "full",
            "property_filter": ["Length", "DoesNotExist"],
        },
    )
    row = result["objects"][0]
    assert list(row["properties"]) == ["Length", "DoesNotExist"]
    assert row["properties"]["DoesNotExist"] == {"unavailable": "no-such-property"}
    assert row["propertyMetadata"]["DoesNotExist"] is None
    assert row["propertyMetadata"]["Length"]["type"] == "App::PropertyLength"
    assert row["propertyCount"] == 2
    assert row["nextPropertyOffset"] is None


class _ExprBox(FakeObj):
    def setExpression(self, prop: str, expression: str) -> None:
        self.history.append(("setExpression", prop, expression))


def test_mutation_force_closes_surviving_empty_transaction() -> None:
    """FreeCAD 1.1 leaves an EMPTY transaction alive through its commit.

    The gate must force-close its own surviving label, or every later
    mutation wedges behind "user transaction already active".
    """

    doc = FakeDoc(
        objects=[
            _ExprBox("Box", properties=("Length",), prop_types={"Length": "App::PropertyLength"})
        ]
    )
    ctx = FakeCtx(doc)
    closed: list[bool] = []
    calls = {"n": 0}

    def _active():
        # Entry check sees a clean stack; after the commit our own label
        # survived FreeCAD's commit (the observed 1.1.3 quirk).
        calls["n"] += 1
        return None if calls["n"] == 1 else ("edit_parameters", 5)

    def _close(commit: bool) -> None:
        closed.append(bool(commit))

    ctx.App = types.SimpleNamespace(getActiveTransaction=_active, closeActiveTransaction=_close)

    result = parameters_mod.HANDLERS["edit_parameters"](
        ctx,
        {"document": doc.Name, "object": "Box", "expressions": {"Length": "1"}},
    )

    assert result["expressions"] == ["Length"]
    assert closed == [True]  # the surviving label was force-closed


def test_mutation_never_closes_a_foreign_surviving_transaction() -> None:
    doc = FakeDoc(
        objects=[
            _ExprBox("Box", properties=("Length",), prop_types={"Length": "App::PropertyLength"})
        ]
    )
    ctx = FakeCtx(doc)
    closed: list[bool] = []
    calls = {"n": 0}

    def _active():
        # Entry check sees a clean stack; after the commit a transaction
        # from somewhere else is on top. It must never be touched.
        calls["n"] += 1
        return None if calls["n"] == 1 else ("someone else's transaction", 9)

    def _close(commit: bool) -> None:
        closed.append(bool(commit))

    ctx.App = types.SimpleNamespace(getActiveTransaction=_active, closeActiveTransaction=_close)

    parameters_mod.HANDLERS["edit_parameters"](
        ctx,
        {"document": doc.Name, "object": "Box", "expressions": {"Length": "1"}},
    )

    assert closed == []  # only our own surviving label is ever closed


def test_placement_rows_report_angle_in_degrees() -> None:
    """The wire field is angle_deg; a 30-degree rotation must read 30."""

    import math

    obj = FakeObj(
        "Rotated",
        properties=("Placement",),
        prop_types={"Placement": "App::PropertyPlacement"},
        values={
            "Placement": StubPlacement(
                StubVector(1, 2, 3),
                StubRotation(StubVector(0, 0, 1), math.pi / 6),
            )
        },
    )
    doc = FakeDoc(objects=[obj])
    result = objects_mod.HANDLERS["inspect_objects"](
        FakeCtx(doc), {"document": doc.Name, "detail": "compact"}
    )
    row = result["objects"][0]
    assert abs(row["placement"]["angle_deg"] - 30.0) < 1e-9


# ---------------------------------------------------------------------------
# Phase 1: selected-object inspection, cursor binding, expression metadata,
# native link serialization, mutation deltas and expected-bounds commits.
# ---------------------------------------------------------------------------


def test_inspect_selection_returns_only_requested_sorted_rows() -> None:
    doc, ctx = _three_box_doc()

    result = objects_mod.inspect_objects(ctx, {"document": doc.Name, "objects": ["B", "A"]})

    assert [row["name"] for row in result["objects"]] == ["A", "B"]
    assert result["total"] == 2
    assert result["count"] == 2
    assert result["nextCursor"] is None


def test_inspect_selection_resolves_names_before_rows() -> None:
    doc, ctx = _three_box_doc()

    with pytest.raises(ToolError) as exc_info:
        objects_mod.inspect_objects(ctx, {"document": doc.Name, "objects": ["A", "Ghost"]})

    expect_tool_error(exc_info, OBJECT_NOT_FOUND)


def test_inspect_selection_rejects_duplicates_and_oversized_lists() -> None:
    doc, ctx = _three_box_doc()

    with pytest.raises(ToolError) as duplicate:
        objects_mod.inspect_objects(ctx, {"document": doc.Name, "objects": ["A", "A"]})
    expect_tool_error(duplicate, VALIDATION_FAILED)

    with pytest.raises(ToolError) as oversized:
        objects_mod.inspect_objects(
            ctx,
            {
                "document": doc.Name,
                "objects": [f"Obj{index}" for index in range(65)],
            },
        )
    expect_tool_error(oversized, VALIDATION_FAILED)


def test_selection_cursor_paginates_the_selected_objects_only() -> None:
    doc, ctx = _three_box_doc()

    first = objects_mod.inspect_objects(
        ctx, {"document": doc.Name, "objects": ["B", "A", "C"], "limit": 2}
    )
    assert [row["name"] for row in first["objects"]] == ["A", "B"]
    assert first["total"] == 3
    assert first["nextCursor"] is not None

    second = objects_mod.inspect_objects(
        ctx,
        {
            "document": doc.Name,
            "objects": ["B", "A", "C"],
            "limit": 2,
            "cursor": first["nextCursor"],
        },
    )
    assert [row["name"] for row in second["objects"]] == ["C"]
    assert second["nextCursor"] is None


def test_cursor_bound_to_a_different_selection_is_stale() -> None:
    doc, ctx = _three_box_doc()

    first = objects_mod.inspect_objects(
        ctx, {"document": doc.Name, "objects": ["A", "B"], "limit": 1}
    )

    # A different selection must not resume the paged selection...
    with pytest.raises(ToolError) as mismatched:
        objects_mod.inspect_objects(
            ctx,
            {
                "document": doc.Name,
                "objects": ["A", "C"],
                "limit": 1,
                "cursor": first["nextCursor"],
            },
        )
    expect_tool_error(mismatched, VALIDATION_FAILED)

    # ...and neither may a cursor carrying a selection continue a
    # selection-less listing.
    with pytest.raises(ToolError) as unselected:
        objects_mod.inspect_objects(
            ctx, {"document": doc.Name, "limit": 1, "cursor": first["nextCursor"]}
        )
    expect_tool_error(unselected, VALIDATION_FAILED)


def test_expression_metadata_is_disclosed_in_full_detail() -> None:
    obj = FakeObj(
        "Box",
        properties=("Length", "Width"),
        prop_types={
            "Length": "App::PropertyLength",
            "Width": "App::PropertyLength",
        },
        values={"Length": 10.0, "Width": 5.0},
    )
    # FreeCAD's getExpression returns (expression string, path); the width
    # property has no expression and the raw getter result is None.
    object.__setattr__(obj, "_expressions", {"Length": ("Width * 2", "Width")})
    obj.getExpression = lambda prop: obj._expressions.get(prop)
    doc = FakeDoc(objects=[obj])
    ctx = FakeCtx(doc)

    result = objects_mod.inspect_objects(
        ctx,
        {
            "document": doc.Name,
            "detail": "full",
            "property_filter": ["Length", "Width"],
        },
    )

    metadata = result["objects"][0]["propertyMetadata"]
    assert metadata["Length"]["expression"] == "Width * 2"
    assert metadata["Width"]["expression"] is None


def test_link_subelement_pairs_serialize_as_descriptive_references() -> None:
    doc, ctx = _three_box_doc()
    linked = box("LinkTarget", shape=None)
    source = box(
        "Source",
        shape=None,
        properties=("Mount",),
        prop_types={"Mount": "App::PropertyLinkSub"},
        values={"Mount": (linked, ["Face1"])},
    )
    doc.Objects.append(source)
    doc._by_name[source.Name] = source

    result = objects_mod.inspect_objects(
        ctx,
        {"document": doc.Name, "detail": "full", "property_filter": ["Mount"]},
    )

    row = next(r for r in result["objects"] if r["name"] == "Source")
    assert row["properties"]["Mount"] == {
        "object": "LinkTarget",
        "subelement": "Face1",
    }


def test_edit_object_reports_property_geometry_and_dependent_deltas() -> None:
    dependent = FakeObj("Dep", shape=None)
    obj = box("Box", values={"Length": 4.0}, in_list=(dependent,))
    doc = FakeDoc(objects=[obj])
    ctx = FakeCtx(doc)

    result = objects_mod.edit_object(
        ctx,
        {"document": doc.Name, "object": "Box", "properties": {"Length": 40}},
    )

    change = result["change"]
    assert change["properties"] == [{"name": "Length", "before": 4.0, "after": 40.0}]
    geometry = change["geometry"]
    assert geometry["solidCountBefore"] == 1
    assert geometry["solidCountAfter"] == 1
    assert geometry["volumeBefore"] == 1000.0
    assert geometry["volumeAfter"] == 1000.0
    assert geometry["boundsBefore"] == [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]
    assert geometry["boundsAfter"] == [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]
    assert change["dependentCount"] == 1


def test_create_object_change_uses_null_before_fields() -> None:
    doc = FakeDoc()

    def add_box(type_id: str, name: str) -> FakeObj:
        created = box(name)
        doc.Objects.append(created)
        doc._by_name[created.Name] = created
        return created

    doc.addObject = add_box  # type: ignore[method-assign]
    ctx = FakeCtx(doc)

    result = objects_mod.create_object(
        ctx,
        {
            "document": doc.Name,
            "type": "Part::Box",
            "name": "Created",
            "properties": {"Length": 4},
        },
    )

    change = result["change"]
    assert change["properties"] == [{"name": "Length", "before": None, "after": 4.0}]
    assert change["geometry"]["solidCountBefore"] is None
    assert change["geometry"]["volumeBefore"] is None
    assert change["geometry"]["boundsBefore"] is None
    assert change["dependentCount"] == 0


def test_matching_expected_bounds_commit_the_edit() -> None:
    obj = box("Box", values={"Length": 4.0})
    doc = FakeDoc(objects=[obj])
    ctx = FakeCtx(doc)

    objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Box",
            "properties": {"Length": 40},
            "expected_bounds": [0.0, 0.0, 0.0, 10.0, 10.0, 10.0],
        },
    )

    assert obj.Length == 40
    assert [call[0] for call in doc.calls] == ["open", "commit"]


def test_incompatible_expected_bounds_roll_the_edit_back() -> None:
    obj = box("Box", values={"Length": 4.0})
    doc = FakeDoc(objects=[obj])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Box",
                "properties": {"Length": 40},
                "expected_bounds": [0.0, 0.0, 0.0, 10.0, 10.0, 14.0],
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["operationState"] == "rolled_back"
    assert error.details["nextAction"] == "retry_from_original_state"
    assert error.details["reason"] == "expected_bounds"
    # The rollback restored the original value and no commit happened.
    assert obj.Length == 4.0
    assert "abort" in [call[0] for call in doc.calls]


def test_prevalidation_failures_stay_without_operation_state() -> None:
    doc, ctx = _three_box_doc()

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "B",
                "properties": {"Length": 5, "NotAProperty": 1},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "operationState" not in (error.details or {})
    assert doc.calls == []


# ---------------------------------------------------------------------------
# Phase 2: edit_objects atomic batch.
# ---------------------------------------------------------------------------


def _batch_doc() -> tuple[FakeDoc, FakeCtx, FakeObj, FakeObj]:
    first = box("First", values={"Length": 1.0})
    second = box("Second", values={"Length": 2.0})
    doc = FakeDoc(objects=[first, second])
    return doc, FakeCtx(doc), first, second


def test_edit_objects_applies_all_edits_in_one_transaction() -> None:
    doc, ctx, first, second = _batch_doc()

    result = objects_mod.edit_objects(
        ctx,
        {
            "document": doc.Name,
            "edits": [
                {"object": "First", "properties": {"Length": 10}},
                {"object": "Second", "properties": {"Length": 20}},
            ],
        },
    )

    assert first.Length == 10
    assert second.Length == 20
    assert [call[0] for call in doc.calls] == ["open", "commit"]
    assert doc.recompute_count == 1
    assert [obj["name"] for obj in result["objects"]] == ["First", "Second"]
    assert result["changes"][0]["properties"] == [{"name": "Length", "before": 1.0, "after": 10.0}]
    assert result["changes"][1]["properties"] == [{"name": "Length", "before": 2.0, "after": 20.0}]
    assert result["applied"] == ["First", "Second"]


def test_edit_objects_rejects_duplicate_object_names_before_transaction() -> None:
    doc, ctx, first, _second = _batch_doc()

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_objects(
            ctx,
            {
                "document": doc.Name,
                "edits": [
                    {"object": "First", "properties": {"Length": 10}},
                    {"object": "First", "properties": {"Length": 20}},
                ],
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "more than once" in error.message
    assert first.history == []
    assert doc.calls == []


def test_edit_objects_rejects_unknown_expectation_names() -> None:
    doc, ctx, _first, _second = _batch_doc()

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_objects(
            ctx,
            {
                "document": doc.Name,
                "edits": [{"object": "First", "properties": {"Length": 10}}],
                "expectations": {"Ghost": {"expected_solids": 1}},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "not edited" in error.message
    assert doc.calls == []


def test_edit_objects_rolls_back_the_whole_batch_on_expectation_failure() -> None:
    doc, ctx, first, second = _batch_doc()

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_objects(
            ctx,
            {
                "document": doc.Name,
                "edits": [
                    {"object": "First", "properties": {"Length": 10}},
                    {"object": "Second", "properties": {"Length": 20}},
                ],
                "expectations": {"Second": {"expected_bounds": [0.0, 0.0, 0.0, 10.0, 10.0, 14.0]}},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["operationState"] == "rolled_back"
    assert error.details["reason"] == "expected_bounds"
    # The first target's property change rolled back with the batch.
    assert first.Length == 1.0
    assert second.Length == 2.0
    assert "abort" in [call[0] for call in doc.calls]


def test_edit_objects_per_target_expectations_pass_and_commit() -> None:
    doc, ctx, first, second = _batch_doc()

    result = objects_mod.edit_objects(
        ctx,
        {
            "document": doc.Name,
            "edits": [
                {"object": "First", "properties": {"Length": 10}},
                {"object": "Second", "properties": {"Length": 20}},
            ],
            "expectations": {
                "First": {"expected_solids": 1},
                "Second": {
                    "expected_bounds": [0.0, 0.0, 0.0, 10.0, 10.0, 10.0],
                    "bounds_tolerance": 0.5,
                },
            },
        },
    )

    assert first.Length == 10
    assert second.Length == 20
    assert [call[0] for call in doc.calls] == ["open", "commit"]
    assert len(result["changes"]) == 2
    definition = next(
        entry for entry in objects_mod.TOOL_DEFINITIONS if entry["name"] == "edit_objects"
    )
    validate_schema(result, definition["outputSchema"])
