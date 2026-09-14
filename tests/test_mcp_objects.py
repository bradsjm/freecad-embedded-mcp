"""Focused tests for mcp_server object tools and the mutation gate.

Runs without FreeCAD: FreeCAD, ObjectsFem and the sibling geometry resolver
are installed as isolated stubs before the modules load (same pattern as
test_object_validation.py).
"""

from __future__ import annotations

import hashlib
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server import topology_query as tq
from mcp_server.protocol import (
    DOCUMENT_NOT_FOUND,
    INVALID_PARAMS,
    OBJECT_NOT_FOUND,
    VALIDATION_FAILED,
    ConsentSigner,
    ProtocolError,
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
_MAX_CARDINALITY_CANDIDATES = 16


def _stub_token(ctx: Any, doc: Any, obj: Any, role: str, index: int) -> str:
    """A signed-looking opaque token: bound fields plus a payload digest."""

    head = ".".join(
        (
            "topology",
            str(ctx.document_identity(doc)),
            str(int(ctx.document_generation(doc))),
            str(obj.Name),
            role,
            str(int(index)),
        )
    )
    return f"{head}.{hashlib.sha256(head.encode()).hexdigest()[:12]}"


def _stub_make_reference(ctx: Any, doc: Any, obj: Any, role: str, index: int) -> dict:
    return {"object": obj.Name, "subelement": _stub_token(ctx, doc, obj, role, index)}


def _stub_whole_reference(obj: Any) -> dict:
    return {"object": obj.Name}


def _stub_cardinality_error(
    reason: str,
    message: str,
    parameter: str,
    ctx: Any,
    doc: Any,
    obj: Any,
    role: str,
    indices: list,
) -> ToolError:
    """Mirror ``geometry._cardinality_error``'s bounded evidence."""

    return ToolError(
        VALIDATION_FAILED,
        message,
        {
            "reason": reason,
            "parameter": parameter,
            "object": obj.Name,
            "role": role,
            "matchCount": len(indices),
            "candidates": [
                _stub_make_reference(ctx, doc, obj, role, index)
                for index in indices[:_MAX_CARDINALITY_CANDIDATES]
            ],
            "candidatesTruncated": len(indices) > _MAX_CARDINALITY_CANDIDATES,
            "nextTool": "inspect_topology",
        },
    )


def _stub_query_record(role: str, index: int, subshape: Any) -> dict:
    """A minimal evaluation record; centers are read when the fake provides them."""

    record: dict[str, Any] = {
        "index": index,
        "role": role,
        "type": None,
        "typeStatus": "unreadable",
        "center": None,
        "direction": None,
        "radius": None,
        "axis": None,
    }
    center = getattr(subshape, "CenterOfMass", None)
    if center is not None:
        record["center"] = [float(center[0]), float(center[1]), float(center[2])]
    return record


def _stub_resolve_query(ctx: Any, doc: Any, target: Any, parameter: str = "query") -> tuple:
    """Resolve a shared query target against the fake document's shapes."""

    obj = ctx.require_object(doc, str(target.get("object")))
    expected_generation = target.get("expected_generation")
    if expected_generation is not None and expected_generation != int(ctx.document_generation(doc)):
        raise ToolError(
            VALIDATION_FAILED,
            "query target is stale for the current document generation",
            {
                "reason": "stale_generation",
                "expected": int(expected_generation),
                "actual": int(ctx.document_generation(doc)),
                "nextTool": "inspect_topology",
            },
        )
    steps = tq.normalize_query(target.get("query"), parameter)
    shape = _stub_placed_shape(obj)
    role: str | None = None
    current: list[dict] = []
    selected: list[int] = []
    for step in steps:
        if role is None:
            role = step["role"]
            subshapes = list(shape.Faces if role == "face" else shape.Edges)
            current = [
                _stub_query_record(role, index, subshape)
                for index, subshape in enumerate(subshapes, 1)
            ]
        elif step["role"] == role:
            by_index = {record["index"]: record for record in current}
            current = [by_index[index] for index in selected]
        else:
            raise ToolError(
                VALIDATION_FAILED,
                "stub queries do not expand face->edge; use the real module for that",
                {"reason": "unsupported_query_transition"},
            )
        if step["selector"] is not None:
            indices = tq.evaluate_selector(tq.parse_selector(step["selector"]), current)
        else:
            indices = [record["index"] for record in current]
        selected = sorted(indices)
    assert role is not None
    return obj, role, selected


def _stub_resolve_reference(
    ctx: Any, doc: Any, reference: Any, parameter: str = "reference"
) -> tuple[Any, str]:
    """Shared-target resolution: whole (no key), signed token, or one query match."""

    if not isinstance(reference, dict):
        raise ToolError(VALIDATION_FAILED, f"{parameter} must be a target object mapping")
    if "query" in reference:
        obj, role, indices = _stub_resolve_query(ctx, doc, reference, parameter)
        if not indices:
            raise _stub_cardinality_error(
                "selection_empty",
                f"{parameter} matched no {role} of {obj.Name}",
                parameter,
                ctx,
                doc,
                obj,
                role,
                indices,
            )
        if len(indices) > 1:
            raise _stub_cardinality_error(
                "selection_ambiguous",
                f"{parameter} matched {len(indices)} {role}s of {obj.Name}",
                parameter,
                ctx,
                doc,
                obj,
                role,
                indices,
            )
        return obj, ("Face" if role == "face" else "Edge") + str(indices[0])
    if "subelement" not in reference or reference.get("subelement") is None:
        name = reference.get("object")
        if not isinstance(name, str) or not name:
            raise ToolError(VALIDATION_FAILED, f"{parameter} is missing an object name")
        return ctx.require_object(doc, name), ""
    subelement = reference.get("subelement")
    if not isinstance(subelement, str):
        raise ToolError(VALIDATION_FAILED, f"{parameter}.subelement must be a string")
    if not subelement:
        raise ToolError(
            VALIDATION_FAILED,
            f"{parameter}.subelement is empty; omit it for a whole object or "
            "pass a signed reference",
            {"parameter": parameter, "reason": "empty_subelement"},
        )
    if "." not in subelement:
        if _NUMERIC_SUB.fullmatch(subelement):
            raise ToolError(
                VALIDATION_FAILED,
                "numeric topology selectors (e.g. Face7) are not durable; use the"
                " signed reference returned by inspect_topology or measure",
            )
        raise ToolError(VALIDATION_FAILED, f"{parameter}.subelement is not a signed token")
    head, _, digest = subelement.rpartition(".")
    expected_digest = hashlib.sha256(head.encode()).hexdigest()[:12]
    if digest != expected_digest:
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference signature rejected",
            {"reason": "invalid"},
        )
    domain, identity, generation, name, role, index = head.split(".")
    if domain != "topology":
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference signature rejected",
            {"reason": "invalid"},
        )
    obj = ctx.require_object(doc, name)
    if identity != str(ctx.document_identity(doc)):
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference belongs to a different document",
            {"reason": "document_mismatch"},
        )
    if int(generation) != int(ctx.document_generation(doc)):
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference is stale for the current document generation",
            {
                "reason": "stale_generation",
                "expected": int(generation),
                "actual": int(ctx.document_generation(doc)),
                "nextTool": "inspect_topology",
            },
        )
    if name != obj.Name:
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference does not match the referenced object",
            {"reason": "object_mismatch"},
        )
    if role not in ("face", "edge") or not index.isdigit() or int(index) < 1:
        raise ToolError(
            VALIDATION_FAILED,
            "topology reference role or index is invalid",
            {"reason": "malformed"},
        )
    return obj, ("Face" if role == "face" else "Edge") + index


_STUB_GEOMETRY.resolve_reference = _stub_resolve_reference
_STUB_GEOMETRY.make_reference = _stub_make_reference
_STUB_GEOMETRY.whole_reference = _stub_whole_reference
_STUB_GEOMETRY.resolve_query = _stub_resolve_query
_STUB_GEOMETRY._cardinality_error = _stub_cardinality_error
_STUB_GEOMETRY.cardinality_error = _stub_cardinality_error


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


def _stub_subshape_fingerprints(shape: Any, role: str, indices: Any) -> None:
    """The doubles carry no readable subshape geometry: no fingerprint evidence.

    ``_PreparedQueries.resolve`` records this as an absent snapshot, exactly
    as the real module's fail-closed extraction does for these doubles; no
    objects-level flow re-verifies a query selection.
    """

    return None


_STUB_GEOMETRY.subshape_fingerprints = _stub_subshape_fingerprints
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
        faces: tuple[Any, ...] = (),
        edges: tuple[Any, ...] = (),
    ) -> None:
        self._valid = valid
        self._solids = solids
        self.Volume = volume
        self._bounds = bounds
        self._check = check or []
        self._tolerance = tolerance
        self.Faces = list(faces)
        self.Edges = list(edges)

    def isNull(self) -> bool:
        # probes["shape.null_attributes"]: a real shape is not null; the
        # null path goes through shape_is_null's True branch.
        return False

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
        derived_from: tuple[str, ...] = (),
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
        object.__setattr__(self, "_derived_from", tuple(derived_from))
        object.__setattr__(self, "_blocked", set())
        object.__setattr__(self, "Document", None)
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
        # probes["object.property_status"]: a missing property raises
        # AttributeError, not KeyError.
        try:
            return self._types[prop]
        except KeyError:
            raise AttributeError(f"Property container has no property '{prop}'") from None

    def getPropertyStatus(self, prop: str) -> list[str]:
        return list(self._prop_status.get(prop, ()))

    def getEnumerationsOfProperty(self, prop: str) -> list[str]:
        return list(self._enums[prop])

    def isDerivedFrom(self, type_id: str) -> bool:
        return (type_id == "PartDesign::Body" and self._is_body) or type_id in self._derived_from

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


class FakeSheet(FakeObj):
    """Spreadsheet cell authority with the native Sheet Python API shape."""

    def __init__(
        self,
        name: str = "Params",
        *,
        cells: dict[str, str] | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        object.__setattr__(self, "_sheet_cells", dict(cells or {}))
        object.__setattr__(self, "_aliases", dict(aliases or {}))
        addresses = tuple(sorted(self._sheet_cells))
        super().__init__(
            name,
            TypeId="Spreadsheet::Sheet",
            properties=(*addresses, "cells"),
            prop_types={
                **dict.fromkeys(addresses, "App::PropertyString"),
                "cells": "Spreadsheet::PropertySheet",
            },
            values={address: self._value(address) for address in addresses},
        )

    def _value(self, address: str) -> Any:
        content = self._sheet_cells[address]
        return 3.7 if content.startswith("=") else content

    def getCellFromAlias(self, alias: str) -> str:
        return self._aliases.get(alias, "")

    def getAlias(self, address: str) -> str:
        return next((alias for alias, cell in self._aliases.items() if cell == address), "")

    def getContents(self, address: str) -> str:
        return self._sheet_cells[address]

    def get(self, address: str) -> Any:
        return self._value(address)

    def set(self, address: str, content: str) -> None:
        self._sheet_cells[address] = content
        self._values[address] = self._value(address)
        self.history.append(("cell", address, content))

    def getUsedCells(self) -> list[str]:
        return sorted(self._sheet_cells)

    def getUsedRange(self) -> tuple[str, str]:
        return ("A1", "B2") if self._sheet_cells else ("", "")


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
        for obj in self.Objects:
            obj.Document = self

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
        obj.Document = self
        self.Objects.append(obj)
        self._by_name[obj.Name] = obj
        return obj

    def removeObject(self, name: str) -> None:
        obj = self.getObject(name)
        if obj is None:
            raise RuntimeError(f"object {name} not found")
        self.Objects.remove(obj)
        self._by_name.pop(name, None)
        # Native Body::removeObject reroutes a following feature's BaseFeature
        # to the removed feature's own base so the chain stays connected.
        for survivor in self.Objects:
            if getattr(survivor, "BaseFeature", None) is obj:
                survivor.BaseFeature = getattr(obj, "BaseFeature", None)

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


def _output_schema(name: str) -> dict:
    return next(entry for entry in objects_mod.TOOL_DEFINITIONS if entry["name"] == name)[
        "outputSchema"
    ]


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


def test_subelement_bearing_link_position_is_refused() -> None:
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

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    # A plain Link position never strips or widens a subshape target.
    assert error.details == {"parameter": "Base", "reason": "subshape_not_allowed"}
    assert doc.calls == []

    with pytest.raises(ToolError) as query_refusal:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Box",
                "properties": {"Base": {"object": "Box", "query": [{"role": "face"}]}},
            },
        )

    query_error = expect_tool_error(query_refusal, VALIDATION_FAILED)
    assert query_error.details == {"parameter": "Base", "reason": "subshape_not_allowed"}
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
    validate_schema(result, _output_schema("edit_object"))


def test_edit_accepts_documented_lowercase_placement() -> None:
    doc = FakeDoc(objects=[box()])
    ctx = FakeCtx(doc)

    objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Box",
            "properties": {
                "Placement": {
                    "position": [1, 2, 3],
                    "axis": [0, 0, 1],
                    "angle_deg": 90,
                }
            },
        },
    )

    placement = doc.getObject("Box").Placement
    assert placement.Base == StubVector(1, 2, 3)
    assert placement.Rotation.Axis == StubVector(0, 0, 1)
    assert placement.Rotation.Angle == 90


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


def test_unknown_property_suggests_close_names_and_next_tool() -> None:
    doc = FakeDoc(objects=[box()])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Box",
                "properties": {"Lenght": 1.0},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.message == "object 'Box' has no property 'Lenght'"
    assert error.details["object"] == "Box"
    assert error.details["property"] == "Lenght"
    assert "Length" in error.details["suggestions"]
    assert len(error.details["suggestions"]) <= 5
    assert error.details["nextTool"] == "inspect_objects"
    assert doc.calls == []


def test_unknown_property_without_close_match_keeps_next_tool() -> None:
    doc = FakeDoc(objects=[box()])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Box",
                "properties": {"Zzzzzzzz": 1.0},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["suggestions"] == []
    assert error.details["nextTool"] == "inspect_objects"
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


def test_create_unsupported_type_suggests_close_supported_types() -> None:
    doc = FakeDoc()
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_object(ctx, {"document": doc.Name, "type": "Part::Boxx", "name": "X"})

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["supportedTypes"] == [
        "Part::Box",
        "Part::Feature",
        "App::DocumentObjectGroup",
    ]
    assert error.details["suggestions"] == ["Part::Box"]
    assert error.details["nextTool"] == "inspect_objects"
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
    assert "FEM mesh objects are not created by create_object" in exc_info.value.message
    # The opt-in scripting tool is never named here: a disabled tool must stay
    # invisible, so the refusal carries no pointer to it.
    assert "run_script" not in exc_info.value.message
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
    assert result["rerouted"] == []
    assert result["applied"] == ["Lonely"]
    assert doc.getObject("Lonely") is None
    assert ("commit", None) in doc.calls


def test_delete_allows_a_feature_grouped_by_its_body() -> None:
    """A Body's ``Group`` membership is not a data dependency.

    Every PartDesign feature lists its Body in ``InList``, which made every
    feature under a Body undeletable; the native removal updates the Group
    entry and the Body Tip.
    """

    feature = box("Pad")
    body = FakeObj(
        "Body",
        TypeId="PartDesign::Body",
        properties=("Group",),
        values={"Group": [feature]},
    )
    feature.InList.append(body)
    doc = FakeDoc(objects=[body, feature])
    ctx = FakeCtx(doc)

    result = objects_mod.delete_object(ctx, {"document": doc.Name, "object": "Pad"})

    assert result["removed"]["name"] == "Pad"
    assert doc.getObject("Pad") is None
    assert ("commit", None) in doc.calls


def test_delete_still_refuses_a_feature_a_later_feature_uses() -> None:
    """Body membership is excused; a real dependency is still refused."""

    feature = box("Pad")
    body = FakeObj(
        "Body",
        TypeId="PartDesign::Body",
        properties=("Group",),
        values={"Group": [feature]},
    )
    pocket = box("Pocket", in_list=(feature,))
    feature.InList.extend([body, pocket])
    doc = FakeDoc(objects=[body, feature, pocket])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.delete_object(ctx, {"document": doc.Name, "object": "Pad"})

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["dependents"] == ["Pocket"]


def test_delete_reroutes_base_feature_dependent() -> None:
    """A lone BaseFeature link is rerouted by the native removal."""

    pad = box("Pad")
    pocket = box(
        "Pocket",
        derived_from=("PartDesign::Feature",),
    )
    pocket.BaseFeature = pad
    fillet = box(
        "Fillet",
        derived_from=("PartDesign::Feature",),
    )
    fillet.BaseFeature = pocket
    pocket.InList.append(fillet)
    doc = FakeDoc(objects=[pad, pocket, fillet])
    ctx = FakeCtx(doc)

    result = objects_mod.delete_object(ctx, {"document": doc.Name, "object": "Pocket"})

    assert result["removed"]["name"] == "Pocket"
    assert result["rerouted"] == [{"feature": "Fillet", "baseFeature": "Pad"}]
    assert doc.getObject("Pocket") is None
    assert fillet.BaseFeature is pad
    validate_schema(result, _output_schema("delete_object"))


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
        "bounds",
        "shape_valid",
        "solid_count",
        "tip",
        "links",
        "linkCount",
        "linksTruncated",
    }
    validate_schema(result, objects_mod.TOOL_DEFINITIONS[0]["outputSchema"])


def test_full_detail_rows_add_placement_and_property_pages() -> None:
    doc, ctx = _three_box_doc()

    result = objects_mod.inspect_objects(ctx, {"document": doc.Name, "detail": "full", "limit": 1})

    row = result["objects"][0]
    compact = {"name", "label", "typeId", "state", "bounds", "shape_valid", "solid_count"}
    assert set(row) - compact == {
        "tip",
        "links",
        "linkCount",
        "linksTruncated",
        "placement",
        "globalPlacement",
        "boundsCoordinateSystem",
        "properties",
        "propertyMetadata",
        "propertyCount",
        "nextPropertyOffset",
        "truncatedProperties",
        "bodyTip",
        "features",
        "featuresTruncated",
        "featureCount",
        "origins",
    }
    assert row["boundsCoordinateSystem"] == "document"
    assert row["properties"]["Length"] == 0
    validate_schema(result, objects_mod.TOOL_DEFINITIONS[0]["outputSchema"])


def test_inspect_spreadsheet_reports_formula_alias_and_value() -> None:
    sheet = FakeSheet(
        cells={"A1": "1.85 mm", "B28": "=A1 * 2"},
        aliases={"SocketCenter": "A1"},
    )
    doc = FakeDoc(objects=[sheet])
    result = objects_mod.inspect_objects(
        FakeCtx(doc),
        {"document": doc.Name, "detail": "full", "property_filter": ["B28"]},
    )

    row = result["objects"][0]
    assert row["propertyMetadata"]["B28"]["formula"] == "=A1 * 2"
    assert row["spreadsheet"]["usedRange"] == {"from": "A1", "to": "B2"}
    assert row["spreadsheet"]["cells"] == [
        {
            "address": "A1",
            "alias": "SocketCenter",
            "content": "1.85 mm",
            "contentTruncated": False,
            "formula": None,
            "formulaTruncated": False,
            "value": "1.85 mm",
            "valueTruncated": False,
            "error": None,
        },
        {
            "address": "B28",
            "alias": None,
            "content": "=A1 * 2",
            "contentTruncated": False,
            "formula": "=A1 * 2",
            "formulaTruncated": False,
            "value": 3.7,
            "valueTruncated": False,
            "error": None,
        },
    ]
    validate_schema(result, _output_schema("inspect_objects"))


def test_edit_spreadsheet_cells_uses_native_set_and_verifies_contents() -> None:
    sheet = FakeSheet(cells={"A1": "1.85 mm"}, aliases={"SocketCenter": "A1"})
    doc = FakeDoc(objects=[sheet])

    result = objects_mod.edit_object(
        FakeCtx(doc),
        {
            "document": doc.Name,
            "object": sheet.Name,
            "properties": {"cells": {"SocketCenter": "0.01 mm", "B2": "=A1"}},
            "response_detail": "full",
        },
    )

    assert sheet.getContents("A1") == "0.01 mm"
    assert sheet.getContents("B2") == "=A1"
    assert sheet.history[-2:] == [
        ("cell", "A1", "0.01 mm"),
        ("cell", "B2", "=A1"),
    ]
    assert result["change"]["properties"] == [
        {"name": "cells.A1", "before": "1.85 mm", "after": "0.01 mm"},
        {"name": "cells.B2", "before": "", "after": "=A1"},
    ]
    assert result["change"]["cellContentsPersisted"] is True
    validate_schema(result, _output_schema("edit_object"))


def test_edit_spreadsheet_cells_rejects_non_string_contents_before_transaction() -> None:
    sheet = FakeSheet(cells={"A1": "1.85 mm"})
    doc = FakeDoc(objects=[sheet])

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            FakeCtx(doc),
            {
                "document": doc.Name,
                "object": sheet.Name,
                "properties": {"cells": {"A1": 0.01}},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "contents must be a string" in error.message
    assert doc.calls == []


def test_edit_spreadsheet_rejects_direct_cell_property_writes() -> None:
    sheet = FakeSheet(cells={"A1": "1.85 mm"})
    doc = FakeDoc(objects=[sheet])

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            FakeCtx(doc),
            {
                "document": doc.Name,
                "object": sheet.Name,
                "properties": {"A1": "0.01 mm"},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "properties.cells" in error.message
    assert doc.calls == []


def test_edit_spreadsheet_accepts_native_numeric_content_normalization() -> None:
    class CanonicalSheet(FakeSheet):
        def set(self, address: str, content: str) -> None:
            super().set(address, "1" if content == "001" else content)

    sheet = CanonicalSheet(cells={"A1": "0"})
    doc = FakeDoc(objects=[sheet])

    result = objects_mod.edit_object(
        FakeCtx(doc),
        {
            "document": doc.Name,
            "object": sheet.Name,
            "properties": {"cells": {"A1": "001"}},
        },
    )

    assert sheet.getContents("A1") == "1"
    assert result["change"]["cellContentsPersisted"] is True


def test_edit_spreadsheet_accepts_native_string_content_normalization() -> None:
    class StringSheet(FakeSheet):
        def set(self, address: str, content: str) -> None:
            super().set(address, "'" + content if not content.startswith("'") else content)

    sheet = StringSheet(cells={"A1": "'old"})
    doc = FakeDoc(objects=[sheet])

    result = objects_mod.edit_object(
        FakeCtx(doc),
        {
            "document": doc.Name,
            "object": sheet.Name,
            "properties": {"cells": {"A1": "new"}},
        },
    )

    assert sheet.getContents("A1") == "'new"
    assert result["change"]["cellContentsPersisted"] is True

    second = objects_mod.edit_object(
        FakeCtx(doc),
        {
            "document": doc.Name,
            "object": sheet.Name,
            "properties": {"cells": {"A1": "ERR: literal text"}},
        },
    )
    assert sheet.getContents("A1") == "'ERR: literal text"
    assert second["change"]["cellContentsPersisted"] is True


def test_spreadsheet_content_equivalence_accepts_native_noop_normalization() -> None:
    assert objects_mod._spreadsheet_content_equivalent("001", "1") is True
    assert objects_mod._spreadsheet_content_equivalent("1.85 mm", "1.850 mm") is True
    assert objects_mod._spreadsheet_content_equivalent("foo", "'foo") is True
    assert objects_mod._spreadsheet_content_equivalent("=New", "=Old") is False
    assert objects_mod._spreadsheet_content_equivalent("123abc", "'123abc") is True


def test_inspect_spreadsheet_truncates_long_cell_content_and_value() -> None:
    long_content = "x" * (objects_mod._MAX_SPREADSHEET_CONTENT + 100)
    sheet = FakeSheet(cells={"A1": long_content})
    doc = FakeDoc(objects=[sheet])

    result = objects_mod.inspect_objects(
        FakeCtx(doc),
        {"document": doc.Name, "detail": "full"},
    )
    row = result["objects"][0]
    cell = row["spreadsheet"]["cells"][0]

    assert len(cell["content"]) == objects_mod._MAX_SPREADSHEET_CONTENT
    assert cell["contentTruncated"] is True
    assert len(cell["value"]) == objects_mod._MAX_SPREADSHEET_CONTENT
    assert cell["valueTruncated"] is True
    assert row["truncatedProperties"] == ["A1"]
    validate_schema(result, _output_schema("inspect_objects"))


def test_inspect_default_page_limit_is_32() -> None:
    doc = FakeDoc(objects=[box(f"Obj{index:03d}") for index in range(33)])
    ctx = FakeCtx(doc)

    first = objects_mod.inspect_objects(ctx, {"document": doc.Name})

    assert first["total"] == 33
    assert first["count"] == 32
    assert first["nextCursor"] is not None

    second = objects_mod.inspect_objects(ctx, {"document": doc.Name, "cursor": first["nextCursor"]})
    assert [row["name"] for row in second["objects"]] == ["Obj032"]
    assert second["nextCursor"] is None


def test_large_selection_pages_through_all_requested_objects() -> None:
    # 64 is the advertised selection bound: two full pages, no third page.
    doc = FakeDoc(objects=[box(f"Obj{index:03d}") for index in range(64)])
    ctx = FakeCtx(doc)
    selection = [f"Obj{index:03d}" for index in range(64)]
    request = {"document": doc.Name, "objects": selection, "limit": 32}
    validate_schema(request, objects_mod.TOOL_DEFINITIONS[0]["inputSchema"])

    first = objects_mod.inspect_objects(ctx, request)
    assert first["count"] == 32
    assert first["nextCursor"] is not None

    second = objects_mod.inspect_objects(
        ctx,
        {"document": doc.Name, "objects": selection, "limit": 32, "cursor": first["nextCursor"]},
    )
    assert second["count"] == 32
    assert second["nextCursor"] is None
    assert second["total"] == 64
    names = [row["name"] for row in first["objects"] + second["objects"]]
    assert len(names) == 64
    assert len(set(names)) == 64
    validate_schema(second, objects_mod.TOOL_DEFINITIONS[0]["outputSchema"])


def test_input_schema_rejects_selection_and_limit_over_the_handler_page_bound() -> None:
    """The wire schema carries the handler's real bound, not the 500 cap.

    Dispatch validates before the handler runs, so an over-bound selection
    or page size is refused as invalid parameters; the exact bound itself
    stays usable.
    """

    schema = objects_mod.TOOL_DEFINITIONS[0]["inputSchema"]
    with pytest.raises(ProtocolError) as too_many_names:
        validate_schema(
            {"document": "Doc", "objects": [f"Obj{index:03d}" for index in range(65)]},
            schema,
        )
    assert too_many_names.value.code == INVALID_PARAMS
    with pytest.raises(ProtocolError) as too_large_limit:
        validate_schema({"document": "Doc", "limit": 65}, schema)
    assert too_large_limit.value.code == INVALID_PARAMS
    validate_schema(
        {
            "document": "Doc",
            "objects": [f"Obj{index:03d}" for index in range(64)],
            "limit": 64,
        },
        schema,
    )


def test_create_objects_reports_per_target_dependent_counts() -> None:
    """Each created row reports its own post-commit dependent closure.

    The mutation gate's outcome carries only the batch-wide closure union,
    so a row must never fall back to that aggregate: a preexisting
    dependent linked to exactly one created target shows up in that
    target's count alone.
    """

    fan = FakeObj("Fan", shape=None)
    in_lists: dict[str, list[Any]] = {"Hub1": [fan]}

    class _LinkedDoc(FakeDoc):
        def addObject(self, type_id: str, name: str) -> FakeObj:
            actual = name
            if any(obj.Name == actual for obj in self.Objects):
                actual = f"{name}001"
            obj = box(actual)
            obj.InList = list(in_lists.get(name, ()))
            obj.Document = self
            self.Objects.append(obj)
            self._by_name[obj.Name] = obj
            return obj

    doc = _LinkedDoc(objects=[fan])
    ctx = FakeCtx(doc)

    result = objects_mod.create_objects(
        ctx,
        {
            "document": doc.Name,
            "response_detail": "full",
            "entries": [
                {"type": "Part::Box", "name": "Hub1"},
                {"type": "Part::Box", "name": "Hub2"},
            ],
        },
    )

    counts = {
        row["actual"]: change["dependentCount"]
        for row, change in zip(result["nameMapping"], result["changes"], strict=True)
    }
    assert counts == {"Hub1": 1, "Hub2": 0}
    assert {change["dependentCountBefore"] for change in result["changes"]} == {0}
    assert [call[0] for call in doc.calls] == ["open", "commit"]
    definition = next(
        entry for entry in objects_mod.TOOL_DEFINITIONS if entry["name"] == "create_objects"
    )
    validate_schema(result, definition["outputSchema"])


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
    assert error.details == {"reason": "stale_cursor", "nextTool": "inspect_objects"}


def test_malformed_cursor_is_rejected_as_a_pagination_error() -> None:
    """A truncated cursor is a client mistake, not a dispatch failure.

    The raw signature error used to escape as GUI_DISPATCH_FAILED with a
    server traceback, while the documented answer is restart-pagination.
    """

    doc, ctx = _three_box_doc()

    with pytest.raises(ToolError) as exc_info:
        objects_mod.inspect_objects(
            ctx, {"document": doc.Name, "limit": 2, "cursor": "not-a-token"}
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details == {"reason": "malformed_cursor"}


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
    assert row["linkCount"] == len(row["links"])
    assert row["linksTruncated"] is False


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
    linked.Document = doc
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
    # A same-document whole link reads back as bare object identity; the
    # retired empty-subelement sentinel is gone.
    assert row["properties"]["Link"] == {"object": "LinkTarget"}


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


def test_large_property_filter_pages_via_property_offset() -> None:
    names = [f"Prop{index:03d}" for index in range(97)]
    obj = FakeObj(
        "Paged",
        properties=tuple(names),
        prop_types=dict.fromkeys(names, "App::PropertyString"),
        values={name: f"value-{index}" for index, name in enumerate(names)},
    )
    doc = FakeDoc(objects=[obj])
    ctx = FakeCtx(doc)

    request = {
        "document": doc.Name,
        "detail": "full",
        "property_limit": 130,
    }
    validate_schema(request, objects_mod.TOOL_DEFINITIONS[0]["inputSchema"])

    first = objects_mod.inspect_objects(ctx, request)
    row = first["objects"][0]
    assert row["propertyCount"] == 97
    assert list(row["properties"]) == names[:64]
    assert row["nextPropertyOffset"] == 64
    validate_schema(first, objects_mod.TOOL_DEFINITIONS[0]["outputSchema"])

    second = objects_mod.inspect_objects(
        ctx,
        {
            "document": doc.Name,
            "detail": "full",
            "property_limit": 130,
            "property_offset": 64,
        },
    )
    row2 = second["objects"][0]
    assert row2["propertyCount"] == 97
    assert list(row2["properties"]) == names[64:]
    assert row2["nextPropertyOffset"] is None
    assert list(row["properties"]) + list(row2["properties"]) == names


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
    surviving = {"open": True}

    def _active():
        # Entry check sees a clean stack; after the commit our own label
        # survived FreeCAD's commit (the observed 1.1.3 quirk).
        calls["n"] += 1
        if calls["n"] == 1 or not surviving["open"]:
            return None
        return ("edit_parameters", 5)

    def _close(commit: bool) -> None:
        closed.append(bool(commit))
        # The native call clears the transaction; without this the double
        # would report a wedged stack after a successful cleanup.
        surviving["open"] = False

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
        FakeCtx(doc), {"document": doc.Name, "detail": "full"}
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


def test_inspect_selection_rejects_duplicates() -> None:
    doc, ctx = _three_box_doc()

    with pytest.raises(ToolError) as duplicate:
        objects_mod.inspect_objects(ctx, {"document": doc.Name, "objects": ["A", "A"]})
    expect_tool_error(duplicate, VALIDATION_FAILED)


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


def test_link_subelement_pairs_serialize_as_signed_references() -> None:
    doc, ctx = _three_box_doc()
    linked = box("LinkTarget", shape=FakeShape(faces=[object(), object()]))
    linked.Document = doc
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
    expected = _STUB_GEOMETRY.make_reference(ctx, doc, linked, "face", 1)["subelement"]
    # A supported native subshape pair is re-signed, never published as a
    # reusable raw FaceN label.
    assert row["properties"]["Mount"] == {"object": "LinkTarget", "subelement": expected}


def test_unsupported_stale_and_foreign_links_read_back_as_unavailable() -> None:
    doc, ctx = _three_box_doc()
    foreign = box("Foreign", shape=FakeShape(faces=[object()]))
    foreign.Document = FakeDoc(name="Other")
    linked = box("LinkTarget", shape=FakeShape(faces=[object()]))
    linked.Document = doc
    source = box(
        "Source",
        shape=None,
        properties=("Mount", "Backup", "Alias"),
        prop_types={
            "Mount": "App::PropertyLinkSub",
            "Backup": "App::PropertyLinkSub",
            "Alias": "App::PropertyLink",
        },
        values={
            "Mount": (linked, ["Vertex3"]),
            "Backup": (linked, ["Face9"]),
            "Alias": foreign,
        },
    )
    source.Document = doc
    doc.Objects.append(source)
    doc._by_name[source.Name] = source

    result = objects_mod.inspect_objects(
        ctx,
        {
            "document": doc.Name,
            "detail": "full",
            "property_filter": ["Mount", "Backup", "Alias"],
        },
    )

    row = next(r for r in result["objects"] if r["name"] == "Source")
    assert row["properties"]["Mount"] == {
        "unavailableLink": {
            "object": "LinkTarget",
            "reason": "unsupported_subelement",
            "nativeSubelement": "Vertex3",
        }
    }
    assert row["properties"]["Backup"] == {
        "unavailableLink": {
            "object": "LinkTarget",
            "reason": "stale_subelement",
            "nativeSubelement": "Face9",
        }
    }
    assert row["properties"]["Alias"] == {
        "unavailableLink": {"object": "Foreign", "reason": "cross_document"}
    }


def test_context_free_pair_fallback_is_unavailable_link() -> None:
    """Without document context, _jsonify never publishes a reusable FaceN."""

    linked = box("LinkTarget", shape=None)
    assert objects_mod._jsonify((linked, ["Face1"])) == {
        "unavailableLink": {
            "object": "LinkTarget",
            "reason": "link_readback_unavailable",
            "nativeSubelement": "Face1",
        }
    }


def test_edit_object_reports_property_geometry_and_dependent_deltas() -> None:
    dependent = FakeObj("Dep", shape=None)
    obj = box("Box", values={"Length": 4.0}, in_list=(dependent,))
    doc = FakeDoc(objects=[obj, dependent])
    ctx = FakeCtx(doc)

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Box",
            "properties": {"Length": 40},
            # Deltas are full-detail evidence; the handler default is compact.
            "response_detail": "full",
        },
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
    assert change["dependentCountBefore"] == 1
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
            "response_detail": "full",
        },
    )

    change = result["change"]
    assert change["properties"] == [{"name": "Length", "before": None, "after": 4.0}]
    assert change["geometry"]["solidCountBefore"] is None
    assert change["geometry"]["volumeBefore"] is None
    assert change["geometry"]["boundsBefore"] is None
    assert change["dependentCountBefore"] == 0
    assert change["dependentCount"] == 0


class _CountingShape(FakeShape):
    """FakeShape whose ``check()`` counts every probe."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.check_calls = 0

    def check(self) -> list[str]:
        self.check_calls += 1
        return super().check()


def test_create_reuses_the_gate_report_without_reprobing_geometry(monkeypatch) -> None:
    """The handler must reuse the gate's post-recompute report.

    Re-probing would run a second ``check()`` on the new shape and a second
    document-space bounds read; both must come from the gate's single pass.
    """

    shape = _CountingShape()
    doc = FakeDoc()

    def add_box(type_id: str, name: str) -> FakeObj:
        created = box(name, shape=shape)
        doc.Objects.append(created)
        doc._by_name[created.Name] = created
        return created

    doc.addObject = add_box  # type: ignore[method-assign]
    ctx = FakeCtx(doc)

    bounds_probes: list[str] = []
    real_document_bounds = objects_mod.document_bounds

    def counting_bounds(obj: Any) -> list[float] | None:
        bounds_probes.append(str(obj.Name))
        return real_document_bounds(obj)

    monkeypatch.setattr(objects_mod, "document_bounds", counting_bounds)

    result = objects_mod.create_object(
        ctx,
        {
            "document": doc.Name,
            "type": "Part::Box",
            "name": "Created",
            "response_detail": "full",
        },
    )

    assert shape.check_calls == 1  # gate only, no handler re-probe
    assert bounds_probes == []
    assert result["change"]["geometry"]["boundsAfter"] == [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]


def test_edit_object_probes_geometry_only_before_the_transaction(monkeypatch) -> None:
    """The handler's own probes are the pre-mutation snapshots, nothing more.

    ``object_validation`` resolves ``geometry_report``/``document_bounds``
    from its own module globals, so patching them here records exactly the
    handler-level calls. Both must happen before the transaction opens.
    """

    obj = box("Box", shape=_CountingShape(), values={"Length": 4.0})
    doc = FakeDoc(objects=[obj])
    ctx = FakeCtx(doc)

    # (name, number of document calls already made when the probe ran).
    probes: list[tuple[str, str, int]] = []
    real_geometry_report = objects_mod.geometry_report
    real_document_bounds = objects_mod.document_bounds

    def recording_report(target: Any, *args: Any, **kwargs: Any) -> dict:
        probes.append(("report", str(target.Name), len(doc.calls)))
        return real_geometry_report(target, *args, **kwargs)

    def recording_bounds(target: Any) -> list[float] | None:
        probes.append(("bounds", str(target.Name), len(doc.calls)))
        return real_document_bounds(target)

    monkeypatch.setattr(objects_mod, "geometry_report", recording_report)
    monkeypatch.setattr(objects_mod, "document_bounds", recording_bounds)

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Box",
            "properties": {"Length": 40},
            "response_detail": "full",
        },
    )

    # Exactly the two pre-mutation snapshots, both before openTransaction.
    assert probes == [("report", "Box", 0), ("bounds", "Box", 0)]
    assert obj.Shape.check_calls == 2  # pre-mutation snapshot + gate report
    assert [call[0] for call in doc.calls] == ["open", "commit"]
    assert result["change"]["geometry"]["boundsAfter"] == [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]


class _RewireBox(FakeObj):
    """A link-list property whose assignment rewires ``InList``."""

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name == "Deps":
            object.__setattr__(self, "InList", list(value or ()))


def test_link_rewiring_edit_reports_changed_dependent_closure() -> None:
    added = FakeObj("Added", shape=None)
    source = _RewireBox(
        "Source",
        properties=("Length", "Deps"),
        prop_types={"Length": "App::PropertyLength", "Deps": "App::PropertyLinkList"},
        shape=None,
        values={"Length": 1.0, "Deps": []},
    )
    doc = FakeDoc(objects=[source, added])
    ctx = FakeCtx(doc)

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Source",
            "properties": {"Deps": [{"object": "Added"}]},
            "response_detail": "full",
        },
    )

    change = result["change"]
    # The edit points the link at a new target, so the closure grows.
    assert change["dependentCountBefore"] == 0
    assert change["dependentCount"] == 1
    assert source.InList == [added]


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
            "response_detail": "full",
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


def test_edit_objects_reports_dependency_counts_per_target() -> None:
    first_dependent = FakeObj("FirstDep")
    second_dependent = FakeObj("SecondDep")
    first = box("First", values={"Length": 1.0}, in_list=(first_dependent,))
    second = box("Second", values={"Length": 2.0}, in_list=())
    doc = FakeDoc(objects=[first, second, first_dependent, second_dependent])
    ctx = FakeCtx(doc)

    result = objects_mod.edit_objects(
        ctx,
        {
            "document": doc.Name,
            "edits": [
                {"object": "First", "properties": {"Length": 10}},
                {"object": "Second", "properties": {"Length": 20}},
            ],
            "response_detail": "full",
        },
    )

    assert result["changes"][0]["dependentCount"] == 1
    assert result["changes"][1]["dependentCount"] == 0


# ---------------------------------------------------------------------------
# Opt-in compact mutation responses.
# ---------------------------------------------------------------------------


def test_edit_object_compact_response_detail_drops_before_state() -> None:
    dependent = FakeObj("Dep", shape=None)
    obj = box("Box", values={"Length": 4.0}, in_list=(dependent,))
    doc = FakeDoc(objects=[obj, dependent])
    ctx = FakeCtx(doc)

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Box",
            "properties": {"Length": 40},
            "response_detail": "compact",
        },
    )

    change = result["change"]
    assert set(change) == {"properties"}
    assert change["properties"] == [{"name": "Length", "after": 40.0}]
    assert result["report"]["solid_count"] == 1
    validate_schema(result, _output_schema("edit_object"))


def test_create_object_compact_response_detail_drops_before_state() -> None:
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
            "response_detail": "compact",
        },
    )

    change = result["change"]
    assert set(change) == {"properties"}
    assert change["properties"] == [{"name": "Length", "after": 4.0}]
    validate_schema(result, objects_mod.TOOL_DEFINITIONS[1]["outputSchema"])


def test_edit_objects_compact_response_detail_applies_to_every_change() -> None:
    doc, ctx, _first, _second = _batch_doc()

    result = objects_mod.edit_objects(
        ctx,
        {
            "document": doc.Name,
            "edits": [
                {"object": "First", "properties": {"Length": 10}},
                {"object": "Second", "properties": {"Length": 20}},
            ],
            "response_detail": "compact",
        },
    )

    assert result["changes"][0] == {"properties": [{"name": "Length", "after": 10.0}]}
    assert result["changes"][1] == {"properties": [{"name": "Length", "after": 20.0}]}
    validate_schema(result, _output_schema("edit_objects"))


# ---------------------------------------------------------------------------
# create_objects atomic batch.
# ---------------------------------------------------------------------------


class _BoxDoc(FakeDoc):
    """A document whose addObject creates box-shaped, name-deduped objects."""

    def addObject(self, type_id: str, name: str) -> FakeObj:
        actual = name
        if any(obj.Name == actual for obj in self.Objects):
            actual = f"{name}001"
        obj = box(actual)
        obj.Document = self
        self.Objects.append(obj)
        self._by_name[obj.Name] = obj
        return obj


def test_create_objects_commits_a_batch_with_name_mapping() -> None:
    doc = _BoxDoc()
    ctx = FakeCtx(doc)

    result = objects_mod.create_objects(
        ctx,
        {
            "document": doc.Name,
            "entries": [
                {"type": "Part::Box", "name": "Box", "properties": {"Length": 4}},
                {"type": "Part::Box", "name": "Box"},
            ],
        },
    )

    assert [obj["name"] for obj in result["objects"]] == ["Box", "Box001"]
    assert result["nameMapping"] == [
        {"requested": "Box", "actual": "Box"},
        {"requested": "Box", "actual": "Box001"},
    ]
    assert doc.getObject("Box").Length == 4.0
    assert [call[0] for call in doc.calls] == ["open", "commit"]
    assert doc.recompute_count == 1
    assert result["applied"] == ["Box", "Box001"]
    definition = next(
        entry for entry in objects_mod.TOOL_DEFINITIONS if entry["name"] == "create_objects"
    )
    validate_schema(result, definition["outputSchema"])


def test_create_objects_invalid_second_entry_rolls_back_the_batch() -> None:
    doc = _BoxDoc()
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_objects(
            ctx,
            {
                "document": doc.Name,
                "entries": [
                    {"type": "Part::Box", "name": "First"},
                    {
                        "type": "Part::Box",
                        "name": "Second",
                        "properties": {"Length": 5, "NotAProperty": 1},
                    },
                ],
            },
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert ("abort", None) in doc.calls
    assert ("commit", None) not in doc.calls
    assert doc.Objects == []


def test_create_objects_expectations_keyed_by_requested_name() -> None:
    doc = _BoxDoc()
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_objects(
            ctx,
            {
                "document": doc.Name,
                "entries": [{"type": "Part::Box", "name": "Box"}],
                "expectations": {"Box": {"expected_solids": 2}},
            },
        )

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert ("abort", None) in doc.calls
    assert doc.Objects == []

    result = objects_mod.create_objects(
        ctx,
        {
            "document": doc.Name,
            "entries": [{"type": "Part::Box", "name": "Box"}],
            "expectations": {"Box": {"expected_solids": 1}},
        },
    )

    assert result["nameMapping"] == [{"requested": "Box", "actual": "Box"}]
    assert ("commit", None) in doc.calls


def test_create_objects_compact_response_detail() -> None:
    doc = _BoxDoc()
    ctx = FakeCtx(doc)

    result = objects_mod.create_objects(
        ctx,
        {
            "document": doc.Name,
            "entries": [{"type": "Part::Box", "name": "Box", "properties": {"Length": 4}}],
            "response_detail": "compact",
        },
    )

    assert result["changes"][0] == {"properties": [{"name": "Length", "after": 4.0}]}
    definition = next(
        entry for entry in objects_mod.TOOL_DEFINITIONS if entry["name"] == "create_objects"
    )
    validate_schema(result, definition["outputSchema"])


def test_create_objects_rejects_more_than_32_entries() -> None:
    doc = FakeDoc()
    ctx = FakeCtx(doc)
    entries = [{"type": "Part::Box", "name": f"Box{index}"} for index in range(33)]

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_objects(ctx, {"document": doc.Name, "entries": entries})

    expect_tool_error(exc_info, VALIDATION_FAILED)
    assert doc.calls == []


# ---------------------------------------------------------------------------
# Shared link-target contract: query receipts and LinkSubList wiring.
# ---------------------------------------------------------------------------


def _link_doc(*, faces: tuple = (), edges: tuple = ()) -> tuple[FakeDoc, FakeCtx, FakeObj]:
    target = box("Target", shape=FakeShape(faces=faces, edges=edges))
    source = box(
        "Source",
        shape=None,
        properties=("Mount", "Deps"),
        prop_types={"Mount": "App::PropertyLinkSub", "Deps": "App::PropertyLinkSubList"},
        values={"Mount": None, "Deps": []},
    )
    doc = FakeDoc(objects=[target, source])
    return doc, FakeCtx(doc), source


def test_edit_linksub_query_resolves_once_and_reports_receipt() -> None:
    doc, ctx, source = _link_doc(faces=[object()])
    target = doc.getObject("Target")

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Source",
            "properties": {"Mount": {"object": "Target", "query": [{"role": "face"}]}},
        },
    )

    # The singleton consumer bound the single match; the receipt carries the
    # selection-time generation and the signed reference, and only appears
    # because a query was used.
    assert source.Mount == (target, ["Face1"])
    token = _STUB_GEOMETRY.make_reference(ctx, doc, target, "face", 1)["subelement"]
    assert result["resolvedSelections"] == [
        {
            "parameter": "Mount",
            "document": doc.Name,
            "generation": 1,
            "references": [{"object": "Target", "subelement": token}],
            "count": 1,
        }
    ]
    validate_schema(result, _output_schema("edit_object"))


def test_edit_linksub_ambiguous_query_refuses_without_mutation() -> None:
    doc, ctx, source = _link_doc(faces=[object(), object()])

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Source",
                "properties": {"Mount": {"object": "Target", "query": [{"role": "face"}]}},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["reason"] == "selection_ambiguous"
    assert error.details["matchCount"] == 2
    assert len(error.details["candidates"]) == 2
    assert source.Mount is None
    assert doc.calls == []


def test_edit_linksub_empty_query_refuses_without_mutation() -> None:
    doc, ctx, source = _link_doc()

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Source",
                "properties": {"Mount": {"object": "Target", "query": [{"role": "face"}]}},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["reason"] == "selection_empty"
    assert source.Mount is None
    assert doc.calls == []


def test_edit_linksub_accepts_signed_token_from_readback() -> None:
    doc, ctx, source = _link_doc(faces=[object(), object()])
    target = doc.getObject("Target")
    token = _STUB_GEOMETRY.make_reference(ctx, doc, target, "face", 2)["subelement"]

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Source",
            "properties": {"Mount": {"object": "Target", "subelement": token}},
        },
    )

    assert source.Mount == (target, ["Face2"])
    # A signed reference is not a query: no receipt scaffolding.
    assert "resolvedSelections" not in result
    validate_schema(result, _output_schema("edit_object"))


def test_edit_linksub_whole_object_binds_empty_subelement() -> None:
    doc, ctx, source = _link_doc()
    target = doc.getObject("Target")

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Source",
            "properties": {"Mount": {"object": "Target"}},
        },
    )

    # Native LinkSub keeps the whole-object binding as the empty subelement.
    assert source.Mount == (target, [""])
    assert "resolvedSelections" not in result
    validate_schema(result, _output_schema("edit_object"))


def test_numeric_label_is_refused_on_linksub() -> None:
    doc, ctx, source = _link_doc(faces=[object()])

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Source",
                "properties": {"Mount": {"object": "Target", "subelement": "Face7"}},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "not durable" in error.message
    assert source.Mount is None
    assert doc.calls == []


def test_empty_subelement_is_refused_by_the_closed_target_schema() -> None:
    """The retired empty sentinel cannot slip through the permissive union."""

    doc, ctx, source = _link_doc()

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Source",
                "properties": {"Mount": {"object": "Target", "subelement": ""}},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "not a valid shared link target" in error.message
    assert error.details == {"parameter": "Mount"}
    assert source.Mount is None
    assert doc.calls == []


def test_tampered_token_is_refused_without_mutation() -> None:
    doc, ctx, source = _link_doc(faces=[object()])
    token = _STUB_GEOMETRY.make_reference(ctx, doc, doc.getObject("Target"), "face", 1)[
        "subelement"
    ]
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Source",
                "properties": {"Mount": {"object": "Target", "subelement": tampered}},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert "signature rejected" in error.message
    assert source.Mount is None
    assert doc.calls == []


def test_edit_linksublist_query_expands_and_reports_receipt() -> None:
    doc, ctx, source = _link_doc(edges=[object(), object()])
    target = doc.getObject("Target")

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Source",
            "properties": {"Deps": [{"object": "Target", "query": [{"role": "edge"}]}]},
        },
    )

    # The set consumer expands the multi-match query in root-index order.
    assert source.Deps == [(target, ["Edge1"]), (target, ["Edge2"])]
    tokens = [
        _STUB_GEOMETRY.make_reference(ctx, doc, target, "edge", index)["subelement"]
        for index in (1, 2)
    ]
    assert result["resolvedSelections"] == [
        {
            "parameter": "Deps[0]",
            "document": doc.Name,
            "generation": 1,
            "references": [
                {"object": "Target", "subelement": tokens[0]},
                {"object": "Target", "subelement": tokens[1]},
            ],
            "count": 2,
        }
    ]
    validate_schema(result, _output_schema("edit_object"))


def test_edit_linksublist_expands_whole_and_query_entries_in_order() -> None:
    doc, ctx, source = _link_doc(edges=[object(), object()])
    target = doc.getObject("Target")

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Source",
            "properties": {
                "Deps": [
                    {"object": "Target"},
                    {"object": "Target", "query": [{"role": "edge"}]},
                ]
            },
        },
    )

    # Entries expand in request order; only the query entry earns a receipt.
    assert source.Deps == [(target, [""]), (target, ["Edge1"]), (target, ["Edge2"])]
    assert [receipt["parameter"] for receipt in result["resolvedSelections"]] == ["Deps[1]"]
    assert result["resolvedSelections"][0]["count"] == 2
    validate_schema(result, _output_schema("edit_object"))


def test_linksublist_expansion_cap_refuses_before_mutation() -> None:
    doc, ctx, source = _link_doc(edges=tuple(object() for _ in range(65)))

    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Source",
                "properties": {"Deps": [{"object": "Target", "query": [{"role": "edge"}]}]},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["reason"] == "selection_limit"
    assert source.Deps == []
    assert doc.calls == []


def test_omitted_response_detail_equals_explicit_compact() -> None:
    """The mutation handlers default to compact: omitted equals explicit."""

    doc = FakeDoc()

    def add_box(type_id: str, name: str) -> FakeObj:
        created = box(name)
        doc.Objects.append(created)
        doc._by_name[created.Name] = created
        return created

    doc.addObject = add_box  # type: ignore[method-assign]
    ctx = FakeCtx(doc)

    omitted = objects_mod.create_object(
        ctx,
        {"document": doc.Name, "type": "Part::Box", "name": "First", "properties": {"Length": 4}},
    )
    explicit = objects_mod.create_object(
        ctx,
        {
            "document": doc.Name,
            "type": "Part::Box",
            "name": "Second",
            "properties": {"Length": 4},
            "response_detail": "compact",
        },
    )

    assert omitted["change"] == explicit["change"]
    assert omitted["change"] == {"properties": [{"name": "Length", "after": 4.0}]}


# ---------------------------------------------------------------------------
# Phase 5: Part CSG wiring — declared link surfaces, rollback and refusals.
# ---------------------------------------------------------------------------


class _CSGDoc(FakeDoc):
    """A document declaring Part::Cut / Part::MultiFuse property surfaces.

    ``Part::Cut`` exposes Base/Tool as App::PropertyLink and
    ``Part::MultiFuse`` exposes Shapes as App::PropertyLinkList. The fake
    boolean result shape is seeded by the document, so CSG assertions prove
    operand link resolution, sanitized output identity and commit wiring —
    never an OCC subtraction or union.
    """

    def __init__(self, *args: Any, cut_solids: int = 1, fuse_solids: int = 1, **kwargs: Any):
        kwargs.setdefault(
            "supported",
            ("Part::Box", "Part::Cut", "Part::MultiFuse", "App::DocumentObjectGroup"),
        )
        super().__init__(*args, **kwargs)
        self.cut_solids = cut_solids
        self.fuse_solids = fuse_solids

    def addObject(self, type_id: str, name: str) -> FakeObj:
        actual = name
        if any(obj.Name == actual for obj in self.Objects):
            actual = f"{name}001"
        if type_id == "Part::Cut":
            obj = FakeObj(
                actual,
                TypeId=type_id,
                properties=("Base", "Tool"),
                prop_types={"Base": "App::PropertyLink", "Tool": "App::PropertyLink"},
                shape=FakeShape(solids=self.cut_solids),
                values={"Base": None, "Tool": None},
            )
        elif type_id == "Part::MultiFuse":
            obj = FakeObj(
                actual,
                TypeId=type_id,
                properties=("Shapes",),
                prop_types={"Shapes": "App::PropertyLinkList"},
                shape=FakeShape(solids=self.fuse_solids),
                values={"Shapes": []},
            )
        else:
            obj = box(actual)
        obj.Document = self
        self.Objects.append(obj)
        self._by_name[obj.Name] = obj
        return obj


def test_create_part_cut_resolves_base_tool_links_and_commits() -> None:
    doc = _CSGDoc(objects=[box("Base"), box("Tool")])
    ctx = FakeCtx(doc)

    result = objects_mod.create_object(
        ctx,
        {
            "document": doc.Name,
            "type": "Part::Cut",
            "name": "Cut",
            "properties": {"Base": {"object": "Base"}, "Tool": {"object": "Tool"}},
            "expected_solids": 1,
        },
    )

    cut = doc.getObject("Cut")
    assert cut.Base is doc.getObject("Base")
    assert cut.Tool is doc.getObject("Tool")
    assert result["object"] == {"name": "Cut", "label": "Cut", "typeId": "Part::Cut"}
    assert result["report"]["ok"] is True
    assert result["report"]["solid_count"] == 1
    # Plain object links add no receipt scaffolding, and the compact
    # default serializes whole links as bare object identity.
    assert "resolvedSelections" not in result
    assert result["change"] == {
        "properties": [
            {"name": "Base", "after": {"object": "Base"}},
            {"name": "Tool", "after": {"object": "Tool"}},
        ]
    }
    assert ("commit", None) in doc.calls
    validate_schema(result, _output_schema("create_object"))


def test_create_part_multifuse_resolves_shape_links_and_commits() -> None:
    doc = _CSGDoc(objects=[box("First"), box("Second"), box("Third")])
    ctx = FakeCtx(doc)

    result = objects_mod.create_object(
        ctx,
        {
            "document": doc.Name,
            "type": "Part::MultiFuse",
            "name": "Fused",
            "properties": {
                "Shapes": [
                    {"object": "First"},
                    {"object": "Second"},
                    {"object": "Third"},
                ]
            },
            "expected_solids": 1,
        },
    )

    fused = doc.getObject("Fused")
    assert fused.Shapes == [
        doc.getObject("First"),
        doc.getObject("Second"),
        doc.getObject("Third"),
    ]
    assert result["object"]["typeId"] == "Part::MultiFuse"
    assert result["report"]["solid_count"] == 1
    assert ("commit", None) in doc.calls
    validate_schema(result, _output_schema("create_object"))


def test_create_part_cut_missing_tool_reference_restores_document() -> None:
    doc = _CSGDoc(objects=[box("Base"), box("Tool")])
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_object(
            ctx,
            {
                "document": doc.Name,
                "type": "Part::Cut",
                "name": "Cut",
                "properties": {"Base": {"object": "Base"}, "Tool": {"object": "Ghost"}},
                "expected_solids": 1,
            },
        )

    error = expect_tool_error(exc_info, OBJECT_NOT_FOUND)
    # The rollback restored the document inventory, not merely logged an abort.
    assert error.details["operationState"] == "rolled_back"
    assert doc.getObject("Cut") is None
    assert [obj.Name for obj in doc.Objects] == ["Base", "Tool"]
    assert ("abort", None) in doc.calls
    assert ("commit", None) not in doc.calls


def test_create_part_cut_expected_solids_mismatch_rolls_back() -> None:
    doc = _CSGDoc(objects=[box("Base"), box("Tool")], cut_solids=2)
    ctx = FakeCtx(doc)

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_object(
            ctx,
            {
                "document": doc.Name,
                "type": "Part::Cut",
                "name": "Cut",
                "properties": {"Base": {"object": "Base"}, "Tool": {"object": "Tool"}},
                "expected_solids": 1,
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    # The mismatched fake boolean result is refused and the created object
    # is removed by the mutation-gate rollback.
    assert error.details["operationState"] == "rolled_back"
    assert any("expected_solids" in message for message in error.details["errors"])
    assert doc.getObject("Cut") is None
    assert [obj.Name for obj in doc.Objects] == ["Base", "Tool"]
    assert ("abort", None) in doc.calls
    assert ("commit", None) not in doc.calls


def test_part_cut_link_positions_reject_subshape_targets() -> None:
    doc = _CSGDoc(objects=[box("Base"), box("Tool")])
    ctx = FakeCtx(doc)
    token = _STUB_GEOMETRY.make_reference(ctx, doc, doc.getObject("Base"), "face", 1)["subelement"]

    with pytest.raises(ToolError) as query_refusal:
        objects_mod.create_object(
            ctx,
            {
                "document": doc.Name,
                "type": "Part::Cut",
                "name": "Cut",
                "properties": {
                    "Base": {"object": "Base", "query": [{"role": "face"}]},
                    "Tool": {"object": "Tool"},
                },
            },
        )

    # A queryTarget in a PropertyLink position is refused outright instead
    # of being widened into the owner shape.
    assert expect_tool_error(query_refusal, VALIDATION_FAILED).details == {
        "parameter": "Base",
        "reason": "subshape_not_allowed",
        "operationState": "rolled_back",
        "nextAction": "retry_from_original_state",
    }
    assert doc.getObject("Cut") is None
    assert ("commit", None) not in doc.calls

    with pytest.raises(ToolError) as signed_refusal:
        objects_mod.create_object(
            ctx,
            {
                "document": doc.Name,
                "type": "Part::Cut",
                "name": "Cut",
                "properties": {
                    "Base": {"object": "Base", "subelement": token},
                    "Tool": {"object": "Tool"},
                },
            },
        )

    assert expect_tool_error(signed_refusal, VALIDATION_FAILED).details == {
        "parameter": "Base",
        "reason": "subshape_not_allowed",
        "operationState": "rolled_back",
        "nextAction": "retry_from_original_state",
    }
    assert doc.getObject("Cut") is None
    assert [obj.Name for obj in doc.Objects] == ["Base", "Tool"]


def test_part_multifuse_shapes_reject_subshape_entries() -> None:
    doc = _CSGDoc(objects=[box("First"), box("Second")])
    ctx = FakeCtx(doc)
    token = _STUB_GEOMETRY.make_reference(ctx, doc, doc.getObject("First"), "face", 1)["subelement"]

    with pytest.raises(ToolError) as exc_info:
        objects_mod.create_object(
            ctx,
            {
                "document": doc.Name,
                "type": "Part::MultiFuse",
                "name": "Fused",
                "properties": {
                    "Shapes": [{"object": "First"}, {"object": "First", "subelement": token}]
                },
            },
        )

    assert expect_tool_error(exc_info, VALIDATION_FAILED).details == {
        "parameter": "Shapes[1]",
        "reason": "subshape_not_allowed",
        "operationState": "rolled_back",
        "nextAction": "retry_from_original_state",
    }
    assert doc.getObject("Fused") is None
    assert ("commit", None) not in doc.calls


def test_full_detail_link_rows_keep_document_context_before_and_after() -> None:
    """Before and after link rows serialize with the same signed form.

    A before-state row that fell back to the context-free serializer would
    report ``unavailableLink`` for a property the after row reports as a
    signed reference, so one unchanged property could never compare equal.
    """

    doc, ctx, source = _link_doc(faces=[object(), object()])
    target = doc.getObject("Target")
    before_token = _STUB_GEOMETRY.make_reference(ctx, doc, target, "face", 1)["subelement"]
    after_token = _STUB_GEOMETRY.make_reference(ctx, doc, target, "face", 2)["subelement"]
    source.Mount = (target, ["Face1"])

    result = objects_mod.edit_object(
        ctx,
        {
            "document": doc.Name,
            "object": "Source",
            "properties": {"Mount": {"object": "Target", "subelement": after_token}},
            "response_detail": "full",
        },
    )

    row = result["change"]["properties"][0]
    assert row["name"] == "Mount"
    # Both sides are canonical signed references carrying document context.
    assert row["before"] == {"object": "Target", "subelement": before_token}
    assert row["after"] == {"object": "Target", "subelement": after_token}
    validate_schema(result, _output_schema("edit_object"))


def test_query_scan_ignores_a_cells_map_aliased_query() -> None:
    """A spreadsheet cell alias named "query" is not a shared target.

    The pre-transaction scan only pre-resolves values that actually look
    like a shared query target, so an address-or-alias map such as
    ``properties.cells`` is never misread as a link value.
    """

    doc = FakeDoc(objects=[])
    ctx = FakeCtx(doc)
    queries = objects_mod._PreparedQueries(ctx, doc)

    # A cells map whose alias is literally "query" is left alone...
    queries.scan_property("cells", {"query": "=1"})
    assert queries.resolutions == {}
    assert queries.receipts == []

    # ...while a real query target (which always names its object) resolves.
    _doc, _ctx, _source = _link_doc(faces=[object()])
    scanning = objects_mod._PreparedQueries(_ctx, _doc)
    scanning.scan_property("Mount", {"object": "Target", "query": [{"role": "face"}]})
    assert scanning.resolutions[("", "Mount")][2] == [1]
    assert scanning.receipts[0]["parameter"] == "Mount"


def test_batch_query_resolutions_do_not_collide_across_entries() -> None:
    """Each batch entry resolves and binds its OWN query selection.

    The prepared-query cache is keyed per target object as well as per
    property path, so two entries editing a same-named property with
    different queries never share one resolution.
    """

    target_a = box("TargetA", shape=FakeShape(faces=[object()]))
    target_b = box("TargetB", shape=FakeShape(faces=[object()]))
    source_a = box(
        "SourceA",
        shape=None,
        properties=("Mount",),
        prop_types={"Mount": "App::PropertyLinkSub"},
        values={"Mount": None},
    )
    source_b = box(
        "SourceB",
        shape=None,
        properties=("Mount",),
        prop_types={"Mount": "App::PropertyLinkSub"},
        values={"Mount": None},
    )
    doc = FakeDoc(objects=[target_a, target_b, source_a, source_b])
    ctx = FakeCtx(doc)

    result = objects_mod.edit_objects(
        ctx,
        {
            "document": doc.Name,
            "edits": [
                {
                    "object": "SourceA",
                    "properties": {"Mount": {"object": "TargetA", "query": [{"role": "face"}]}},
                },
                {
                    "object": "SourceB",
                    "properties": {"Mount": {"object": "TargetB", "query": [{"role": "face"}]}},
                },
            ],
        },
    )

    # The bug bound target_a's cached resolution to entry B.
    assert source_a.Mount == (target_a, ["Face1"])
    assert source_b.Mount[0] is target_b, "entry B bound entry A's resolution"
    assert source_b.Mount == (target_b, ["Face1"])
    receipts = result["resolvedSelections"]
    assert [row["references"][0]["object"] for row in receipts] == ["TargetA", "TargetB"]
    validate_schema(result, _output_schema("edit_objects"))


def test_batch_creation_query_resolutions_do_not_collide() -> None:
    """Batch creation keys each entry's query resolution by its target name.

    Two entries create a same-named link property whose query selects a
    DIFFERENT target object; each created object must bind its own
    selection rather than reusing the first entry's cached resolution.
    """

    target_a = box("TargetA", shape=FakeShape(faces=[object()]))
    target_b = box("TargetB", shape=FakeShape(faces=[object()]))
    doc = _BoxDoc(objects=[target_a, target_b])

    def add_with_mount(self: Any, type_id: str, name: str) -> Any:
        obj = _BoxDoc.addObject(self, type_id, name)
        obj.PropertiesList.append("Mount")
        obj._types["Mount"] = "App::PropertyLinkSub"
        obj._values["Mount"] = None
        return obj

    doc.addObject = add_with_mount.__get__(doc, _BoxDoc)  # type: ignore[method-assign]
    ctx = FakeCtx(doc)

    result = objects_mod.create_objects(
        ctx,
        {
            "document": doc.Name,
            "entries": [
                {
                    "type": "Part::Box",
                    "name": "HolderA",
                    "properties": {
                        "Length": 1.0,
                        "Mount": {"object": "TargetA", "query": [{"role": "face"}]},
                    },
                },
                {
                    "type": "Part::Box",
                    "name": "HolderB",
                    "properties": {
                        "Length": 2.0,
                        "Mount": {"object": "TargetB", "query": [{"role": "face"}]},
                    },
                },
            ],
        },
    )

    holder_a = doc.getObject("HolderA")
    holder_b = doc.getObject("HolderB")
    assert holder_a.Mount == (target_a, ["Face1"])
    assert holder_b.Mount[0] is target_b, "entry B reused entry A's resolution"
    assert holder_b.Mount == (target_b, ["Face1"])
    receipts = result["resolvedSelections"]
    assert [row["references"][0]["object"] for row in receipts] == ["TargetA", "TargetB"]


def test_prepared_singleton_refusal_carries_full_evidence() -> None:
    """The zero-match refusal reports the shared bounded evidence payload."""

    doc, ctx, _source = _link_doc(faces=[])
    with pytest.raises(ToolError) as exc_info:
        objects_mod.edit_object(
            ctx,
            {
                "document": doc.Name,
                "object": "Source",
                "properties": {"Mount": {"object": "Target", "query": [{"role": "face"}]}},
            },
        )

    error = expect_tool_error(exc_info, VALIDATION_FAILED)
    assert error.details["reason"] == "selection_empty"
    assert error.details["parameter"] == "Mount"
    assert error.details["object"] == "Target"
    assert error.details["role"] == "face"
    assert error.details["matchCount"] == 0
    assert error.details["candidates"] == []
    assert error.details["candidatesTruncated"] is False
    assert error.details["nextTool"] == "inspect_topology"


def test_prepared_cache_keys_by_owner_and_path() -> None:
    """The cache distinguishes owners with the same property path."""

    target_a = box("TargetA", shape=FakeShape(faces=[object()]))
    target_b = box("TargetB", shape=FakeShape(faces=[object()]))
    doc = FakeDoc(objects=[target_a, target_b])
    ctx = FakeCtx(doc)
    queries = objects_mod._PreparedQueries(ctx, doc)

    queries.scan_property("Mount", {"object": "TargetA", "query": [{"role": "face"}]}, owner="A")
    queries.scan_property("Mount", {"object": "TargetB", "query": [{"role": "face"}]}, owner="B")

    assert queries.resolutions[("A", "Mount")][0] is target_a
    assert queries.resolutions[("B", "Mount")][0] is target_b
    assert len(queries.receipts) == 2
