"""Focused tests for mcp_server/tools/features.py (create_feature).

Runs headless: FreeCAD/Part are stubbed, and mcp_server.tools.objects (whose
shared property-conversion helpers this module reuses) is exercised for real
against fake document doubles. The shared mutation gate runs for real.
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
FEATURES_PATH = ADDON_DIR / "mcp_server" / "tools" / "features.py"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.protocol import ToolError, validate_schema

VALIDATION_FAILED = "VALIDATION_FAILED"


class StubVector:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = float(x), float(y), float(z)


_STUB_FREECAD = types.ModuleType("FreeCAD")
_STUB_FREECAD.Vector = StubVector


@contextmanager
def load_features() -> Iterator[types.ModuleType]:
    module_name = f"mcp_server.tools._features_test_{id(object())}"
    saved = {name: sys.modules.get(name) for name in ("FreeCAD", "mcp_server.tools.objects")}
    sys.modules["FreeCAD"] = _STUB_FREECAD
    sys.modules.pop("mcp_server.tools.objects", None)
    sys.modules.pop("mcp_server.tools.features", None)
    try:
        spec = importlib.util.spec_from_file_location(module_name, FEATURES_PATH)
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
# Fake PartDesign domain.
# ---------------------------------------------------------------------------


class FakeShape:
    def __init__(
        self,
        *,
        valid: bool = True,
        solids: int = 1,
        volume: float = 1000.0,
        bounds: tuple[float, ...] = (0.0, 0.0, 0.0, 10.0, 10.0, 10.0),
    ) -> None:
        self._valid = valid
        self._solids = solids
        self.Volume = volume
        self._bounds = bounds

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
        return []

    def getTolerance(self, _: int) -> float:
        return 1e-7


class FakeFeature:
    def __init__(
        self,
        name: str,
        type_id: str,
        *,
        properties: tuple[str, ...] = (),
        shape: Any = None,
    ) -> None:
        object.__setattr__(self, "Name", name)
        object.__setattr__(self, "Label", name)
        object.__setattr__(self, "TypeId", type_id)
        object.__setattr__(self, "State", [])
        object.__setattr__(self, "InList", [])
        object.__setattr__(self, "PropertiesList", list(properties))
        object.__setattr__(self, "_shape", shape)
        object.__setattr__(self, "_values", {})
        object.__setattr__(self, "history", [])

    @property
    def Shape(self) -> Any:
        return self._shape

    def isValid(self) -> bool:
        return True

    def getStatusString(self) -> str:
        return ""

    def getTypeIdOfProperty(self, prop: str) -> str:
        return "App::PropertyLink"

    def getPropertyStatus(self, prop: str) -> list[str]:
        return []

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in object.__getattribute__(self, "PropertiesList"):
            self.history.append((name, value))
        object.__getattribute__(self, "_values")[name] = value


class FakeBody(FakeFeature):
    def __init__(
        self,
        name: str = "Body",
        *,
        members: list[Any] | None = None,
        tip: Any = None,
        shape: Any = None,
    ) -> None:
        super().__init__(
            name,
            "PartDesign::Body",
            properties=("Group", "Tip", "Placement"),
            shape=shape,
        )
        object.__setattr__(self, "_values", {"Group": list(members or [])})
        object.__setattr__(self, "_tip", tip)

    @property
    def Tip(self) -> Any:
        return object.__getattribute__(self, "_tip")

    def isDerivedFrom(self, type_id: str) -> bool:
        return type_id == "PartDesign::Body"

    def newObject(self, type_id: str, name: str) -> FakeFeature:
        # probes["attachment.properties"]: the sketch exposes
        # AttachmentSupport and MapMode but not Support; the datum plane
        # the same; the Pad exposes none of the attachment properties.
        properties = {
            "Sketcher::SketchObject": ("Profile", "AttachmentSupport", "MapMode"),
            "PartDesign::Plane": ("AttachmentSupport", "MapMode", "Placement"),
        }.get(type_id, ("Profile", "Length", "Type"))
        feature = FakeFeature(
            name,
            type_id,
            properties=properties,
            shape=FakeShape() if type_id.startswith("PartDesign::") else None,
        )
        self._values["Group"] = [*list(self._values.get("Group", [])), feature]
        if type_id in ("PartDesign::Pad", "PartDesign::Pocket", "PartDesign::Hole"):
            object.__setattr__(self, "_tip", feature)
        doc = getattr(self, "_doc", None)
        if doc is not None:
            doc.Objects.append(feature)
        return feature


class FakeApp:
    def getActiveTransaction(self) -> None:
        return None


class FakeDoc:
    def __init__(self, body: FakeBody, *, supported: tuple[str, ...]) -> None:
        self.Name = "Doc"
        self.Objects = [body]
        object.__setattr__(body, "_doc", self)
        self._supported = supported
        self.UndoMode = 0
        self.HasPendingTransaction = False
        self.transactions: list[tuple] = []
        self.recompute_count = 0
        self._undo: dict | None = None

    def supportedTypes(self) -> tuple[str, ...]:
        return self._supported

    def addObject(self, type_id: str, name: str) -> Any:
        raise AssertionError("create_feature must never use doc.addObject")

    def recompute(self) -> None:
        self.recompute_count += 1

    def openTransaction(self, label: str) -> None:
        self._undo = {
            "objects": [*self.Objects],
            "groups": {
                obj.Name: [*obj._values.get("Group", [])]
                for obj in self.Objects
                if isinstance(obj, FakeBody)
            },
            "tips": {obj.Name: obj.Tip for obj in self.Objects if isinstance(obj, FakeBody)},
        }
        self.transactions.append(("open", label))

    def commitTransaction(self) -> None:
        self._undo = None
        self.transactions.append(("commit",))

    def abortTransaction(self) -> None:
        if self._undo is not None:
            self.Objects = [*self._undo["objects"]]
            for obj in self.Objects:
                if isinstance(obj, FakeBody):
                    obj._values["Group"] = [*self._undo["groups"][obj.Name]]
                    object.__setattr__(obj, "_tip", self._undo["tips"][obj.Name])
        self.transactions.append(("abort",))

    def removeObject(self, name: str) -> None:
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def getObject(self, name: str) -> Any:
        for obj in self.Objects:
            if obj.Name == name:
                return obj
        return None


class FakeCtx:
    def __init__(self, doc: FakeDoc, *, approved: dict | None = None) -> None:
        self.App = FakeApp()
        self._doc = doc
        self.approved_target = approved

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


SUPPORTED = (
    "PartDesign::Body",
    "PartDesign::Plane",
    "PartDesign::Pad",
    "PartDesign::Pocket",
    "PartDesign::Hole",
    "Sketcher::SketchObject",
)


def make_body_and_doc(**kwargs: Any) -> tuple[FakeBody, FakeDoc]:
    sketch = FakeFeature("Sketch", "Sketcher::SketchObject", properties=())
    body = FakeBody(members=[sketch], **kwargs)
    doc = FakeDoc(body, supported=SUPPORTED)
    doc.Objects.append(sketch)
    return body, doc


def call(module: types.ModuleType, ctx: FakeCtx, **arguments: Any):
    arguments.setdefault("document", "Doc")
    arguments.setdefault("body", "Body")
    return module.HANDLERS["create_feature"](ctx, arguments)


# ---------------------------------------------------------------------------
# Body / kind prevalidation.
# ---------------------------------------------------------------------------


def test_non_body_target_is_rejected() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc()
        other = FakeFeature("Other", "Part::Feature", properties=())
        doc.Objects.append(other)
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(module, ctx, body="Other", kind="sketch", name="Sketch002")

        assert excinfo.value.code == VALIDATION_FAILED
        assert "PartDesign::Body" in excinfo.value.message
        assert doc.transactions == []


def test_absent_datum_plane_type_is_rejected_without_substitute() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc()
        doc._supported = tuple(entry for entry in SUPPORTED if entry != "PartDesign::Plane")
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(module, ctx, kind="datum_plane", name="Plane")

        assert excinfo.value.code == VALIDATION_FAILED
        assert "core datum-plane creation is unavailable" in excinfo.value.message
        assert doc.transactions == []


def test_profile_kinds_require_a_profile() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc()
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(module, ctx, kind="pad", name="Pad")

        assert excinfo.value.code == VALIDATION_FAILED
        assert "requires a profile" in excinfo.value.message


def test_profile_outside_the_body_is_rejected() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc()
        outsider = FakeFeature("Outsider", "Sketcher::SketchObject", properties=())
        doc.Objects.append(outsider)
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(module, ctx, kind="pad", name="Pad", profile="Outsider")

        assert "does not belong to body" in excinfo.value.message
        assert doc.transactions == []


def test_support_requires_explicit_map_mode() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc()
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="sketch",
                name="Sketch002",
                support={"object": "Sketch", "subelement": ""},
            )

        assert "explicit properties.MapMode" in excinfo.value.message
        assert doc.transactions == []


def test_map_mode_without_support_is_rejected() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc()
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="sketch",
                name="Sketch002",
                properties={"MapMode": "FlatFace"},
            )

        assert "requires a support reference" in excinfo.value.message


# ---------------------------------------------------------------------------
# Successful construction.
# ---------------------------------------------------------------------------


def test_sketch_is_created_through_body_new_object() -> None:
    with load_features() as module:
        body, doc = make_body_and_doc()
        ctx = FakeCtx(doc)

        result = call(module, ctx, kind="sketch", name="Sketch002")

        assert result["object"]["typeId"] == "Sketcher::SketchObject"
        assert result["body"]["name"] == "Body"
        assert result["bodyTip"] is None
        assert result["applied"] == ["Body", "Sketch002"]
        assert doc.transactions == [("open", "create_feature:Body"), ("commit",)]
        assert [entry.Name for entry in body.Group] == ["Sketch", "Sketch002"]
        assert result["change"]["geometry"]["solidCountBefore"] is None


def test_pad_requires_tip_update_and_reports_body_report() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="pad",
            name="Pad",
            profile="Sketch",
            expected_solids=1,
        )

        assert result["object"]["typeId"] == "PartDesign::Pad"
        assert result["bodyTip"] == "Pad"
        assert result["bodyReport"]["ok"] is True
        assert result["bodyReport"]["solid_count"] == 1
        assert result["change"]["properties"] == []
        assert doc.recompute_count == 2  # Tip check + gate recompute
        definition = next(
            entry for entry in module.TOOL_DEFINITIONS if entry["name"] == "create_feature"
        )
        validate_schema(result, definition["outputSchema"])


def test_tip_mismatch_rolls_the_creation_back() -> None:
    class TipStuckBody(FakeBody):
        """A build where newObject never advances the Body Tip."""

        def newObject(self, type_id: str, name: str) -> FakeFeature:
            feature = FakeFeature(
                name,
                type_id,
                properties=("Profile",),
                shape=FakeShape(),
            )
            self._values["Group"] = [*list(self._values.get("Group", [])), feature]
            self._doc.Objects.append(feature)
            return feature

    with load_features() as module:
        sketch = FakeFeature("Sketch", "Sketcher::SketchObject", properties=())
        body = TipStuckBody(members=[sketch], shape=FakeShape())
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.append(sketch)
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(module, ctx, kind="pad", name="Pad", profile="Sketch")

        assert excinfo.value.code == VALIDATION_FAILED
        assert excinfo.value.details["reason"] == "tip_mismatch"
        assert (excinfo.value.details or {}).get("operationState") == "rolled_back"
        assert "abort" in [transaction[0] for transaction in doc.transactions]
        assert [entry.Name for entry in doc.Objects] == ["Body", "Sketch"]
        assert [entry.Name for entry in body.Group] == ["Sketch"]


def test_body_expectation_failure_rolls_back() -> None:
    with load_features() as module:
        body, doc = make_body_and_doc(shape=FakeShape(solids=3))
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="pad",
                name="Pad",
                profile="Sketch",
                expected_solids=1,
            )

        assert excinfo.value.code == VALIDATION_FAILED
        assert (excinfo.value.details or {}).get("operationState") == "rolled_back"
        assert "abort" in [transaction[0] for transaction in doc.transactions]
        assert [entry.Name for entry in doc.Objects] == ["Body", "Sketch"]
        assert [entry.Name for entry in body.Group] == ["Sketch"]


def test_body_bounds_expectation_failure_rolls_back() -> None:
    with load_features() as module:
        body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="pad",
                name="Pad",
                profile="Sketch",
                expected_bounds=[0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
            )

        assert excinfo.value.details["reason"] == "expected_bounds"
        assert excinfo.value.details["operationState"] == "rolled_back"
        assert "abort" in [transaction[0] for transaction in doc.transactions]
        assert [entry.Name for entry in doc.Objects] == ["Body", "Sketch"]
        assert [entry.Name for entry in body.Group] == ["Sketch"]


def test_support_and_properties_apply_to_the_created_feature() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc()
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="sketch",
            name="Sketch002",
            support={"object": "Sketch", "subelement": ""},
            properties={"MapMode": "FlatFace"},
        )

        feature = doc.getObject("Sketch002")
        assert feature.MapMode == "FlatFace"
        # probes["attachment.properties"]: a sketch exposes
        # AttachmentSupport, not Support.
        assert feature.AttachmentSupport == [(doc.getObject("Sketch"), "")]
        assert result["applied"] == ["Body", "Sketch002"]


def test_support_without_attachment_properties_is_rejected() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc()
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="pad",
                name="Pad",
                profile="Sketch",
                support={"object": "Sketch", "subelement": ""},
                properties={"MapMode": "FlatFace"},
            )

        # probes["attachment.properties"]: the Pad exposes neither
        # Support nor AttachmentSupport.
        assert "exposes neither Support nor AttachmentSupport" in excinfo.value.message


def test_datum_plane_uses_the_registered_core_type() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc()
        ctx = FakeCtx(doc)

        result = call(module, ctx, kind="datum_plane", name="Plane")

        assert result["object"]["typeId"] == "PartDesign::Plane"
        assert doc.transactions[-1] == ("commit",)
