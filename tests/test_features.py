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

from mcp_server import protocol
from mcp_server.protocol import ToolError, validate_schema

VALIDATION_FAILED = "VALIDATION_FAILED"


class StubVector:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = float(x), float(y), float(z)


class StubPlacement:
    def __init__(self) -> None:
        self.Base = StubVector()


_STUB_FREECAD = types.ModuleType("FreeCAD")
_STUB_FREECAD.Vector = StubVector
_STUB_FREECAD.Placement = StubPlacement


@contextmanager
def load_features() -> Iterator[types.ModuleType]:
    module_name = f"mcp_server.tools._features_test_{id(object())}"
    saved = {
        name: sys.modules.get(name)
        for name in ("FreeCAD", "mcp_server.tools", "mcp_server.tools.objects")
    }
    sys.modules["FreeCAD"] = _STUB_FREECAD
    # A prior server-harness import leaves a stub "mcp_server.tools"
    # package whose "objects" attribute would shadow the real module we
    # restore below; remove both so features.py rebinds to the real one.
    sys.modules.pop("mcp_server.tools", None)
    for stub in (
        "mcp_server.tools.objects",
        "mcp_server.tools.features",
        "mcp_server.tools.geometry",
    ):
        sys.modules.pop(stub, None)
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
        faces: list[Any] | None = None,
        edges: list[Any] | None = None,
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
        object.__setattr__(self, "_faces", list(faces or []))
        object.__setattr__(self, "_edges", list(edges or []))

    def isValid(self) -> bool:
        return self._valid

    @property
    def Solids(self) -> list[Any]:
        return [object()] * self._solids

    @property
    def Faces(self) -> list[Any]:
        return list(object.__getattribute__(self, "_faces"))

    @property
    def Edges(self) -> list[Any]:
        return list(object.__getattribute__(self, "_edges"))

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
        enumerations: dict[str, list[str]] | None = None,
    ) -> None:
        object.__setattr__(self, "Name", name)
        object.__setattr__(self, "Label", name)
        object.__setattr__(self, "TypeId", type_id)
        object.__setattr__(self, "State", [])
        object.__setattr__(self, "InList", [])
        object.__setattr__(self, "PropertiesList", list(properties))
        object.__setattr__(self, "_enumerations", enumerations or {})
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

    def getEnumerationsOfProperty(self, prop: str) -> list[str]:
        return list(object.__getattribute__(self, "_enumerations").get(prop, []))

    def getPropertyStatus(self, prop: str) -> list[str]:
        return []

    def setExpression(self, prop: str, expression: Any) -> None:
        """Fake bound-expression registry: mirrors the native setter."""
        if prop.split(".")[0] not in object.__getattribute__(self, "PropertiesList"):
            raise AttributeError(prop)
        object.__getattribute__(self, "_values").setdefault("_expressions", {})
        object.__getattribute__(self, "_values")["_expressions"][prop] = expression
        self.history.append(("expression", (prop, expression)))

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
        origin_features: list[Any] | None = None,
        feature_enumerations: dict[str, dict[str, list[str]]] | None = None,
    ) -> None:
        super().__init__(
            name,
            "PartDesign::Body",
            properties=("Group", "Tip", "Placement"),
            shape=shape,
        )
        object.__setattr__(self, "_values", {"Group": list(members or [])})
        object.__setattr__(self, "_tip", tip)
        object.__setattr__(self, "_feature_enumerations", feature_enumerations or {})
        if origin_features is not None:
            origin = FakeFeature("Origin", "App::Origin", properties=())
            object.__setattr__(origin, "OriginFeatures", list(origin_features))
            object.__setattr__(self, "Origin", origin)

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
            "PartDesign::Fillet": ("Base", "Radius"),
            "PartDesign::Chamfer": ("Base", "Size"),
            "PartDesign::Thickness": ("Base", "Value", "Reversed"),
            "PartDesign::LinearPattern": ("Originals", "Direction", "Length", "Occurrences"),
            "PartDesign::PolarPattern": ("Originals", "Axis", "Angle", "Occurrences"),
            "PartDesign::Mirrored": ("Originals", "MirrorPlane"),
            "PartDesign::Revolution": ("Profile", "ReferenceAxis", "Angle", "Reversed"),
            "PartDesign::AdditiveLoft": ("Profile", "Sections", "Ruled"),
            "PartDesign::AdditiveHelix": (
                "Profile",
                "ReferenceAxis",
                "Mode",
                "Pitch",
                "Height",
                "Turns",
                "Angle",
                "Growth",
                "LeftHanded",
                "Reversed",
            ),
            "PartDesign::SubtractiveHelix": (
                "Profile",
                "ReferenceAxis",
                "Mode",
                "Pitch",
                "Height",
                "Turns",
                "Angle",
                "Growth",
                "LeftHanded",
                "Reversed",
            ),
            "PartDesign::AdditiveBox": ("Length", "Width", "Height"),
            "PartDesign::SubtractiveBox": ("Length", "Width", "Height"),
            "PartDesign::AdditiveCylinder": ("Radius", "Height", "Angle"),
            "PartDesign::SubtractiveCylinder": ("Radius", "Height", "Angle"),
            "PartDesign::AdditiveCone": ("Radius1", "Radius2", "Height"),
            "PartDesign::SubtractiveCone": ("Radius1", "Radius2", "Height"),
            "PartDesign::AdditiveSphere": ("Radius",),
            "PartDesign::SubtractiveSphere": ("Radius",),
            "PartDesign::AdditivePrism": ("Polygon", "Circumradius", "Height"),
            "PartDesign::SubtractivePrism": ("Polygon", "Circumradius", "Height"),
            "PartDesign::AdditiveTorus": ("Radius1", "Radius2"),
            "PartDesign::SubtractiveTorus": ("Radius1", "Radius2"),
            "PartDesign::AdditiveEllipsoid": ("Radius1", "Radius2", "Radius3"),
            "PartDesign::SubtractiveEllipsoid": ("Radius1", "Radius2", "Radius3"),
            "PartDesign::AdditiveWedge": (
                "Xmin",
                "Xmax",
                "Ymin",
                "Ymax",
                "Zmin",
                "Zmax",
                "X2min",
                "X2max",
                "Z2min",
                "Z2max",
            ),
            "PartDesign::SubtractiveWedge": (
                "Xmin",
                "Xmax",
                "Ymin",
                "Ymax",
                "Zmin",
                "Zmax",
                "X2min",
                "X2max",
                "Z2min",
                "Z2max",
            ),
            "PartDesign::SubShapeBinder": ("Support", "MakeFace"),
            "PartDesign::MultiTransform": ("Originals", "Transformations"),
            "PartDesign::Scaled": ("Originals", "Factor", "Occurrences"),
            "PartDesign::Point": (
                "AttachmentSupport",
                "MapMode",
                "AttachmentOffset",
                "Placement",
            ),
            "PartDesign::Hole": (
                "Profile",
                "Diameter",
                "Depth",
                "DepthType",
                "HoleCutType",
                "ThreadType",
                "Threaded",
                "ThreadSize",
                "DrillPoint",
                "Tapered",
                "ModelThread",
                "DrillForDepth",
                "UseCustomThreadClearance",
                "ThreadDirection",
                "HoleCutDiameter",
                "HoleCutDepth",
                "HoleCutCountersinkAngle",
            ),
        }.get(type_id, ("Profile", "Length", "Type"))
        enumerations = object.__getattribute__(self, "_feature_enumerations").get(type_id)
        feature = FakeFeature(
            name,
            type_id,
            properties=properties,
            shape=FakeShape() if type_id.startswith("PartDesign::") else None,
            enumerations=enumerations,
        )
        self._values["Group"] = [*list(self._values.get("Group", [])), feature]
        if type_id in (
            "PartDesign::Pad",
            "PartDesign::Pocket",
            "PartDesign::Hole",
            "PartDesign::Revolution",
            "PartDesign::Groove",
            "PartDesign::Fillet",
            "PartDesign::Chamfer",
            "PartDesign::Thickness",
            "PartDesign::LinearPattern",
            "PartDesign::PolarPattern",
            "PartDesign::Mirrored",
            "PartDesign::AdditiveLoft",
            "PartDesign::SubtractiveLoft",
            "PartDesign::AdditivePipe",
            "PartDesign::SubtractivePipe",
            "PartDesign::AdditiveHelix",
            "PartDesign::SubtractiveHelix",
            "PartDesign::AdditiveBox",
            "PartDesign::SubtractiveBox",
            "PartDesign::AdditiveCylinder",
            "PartDesign::SubtractiveCylinder",
            "PartDesign::AdditiveCone",
            "PartDesign::SubtractiveCone",
            "PartDesign::AdditiveSphere",
            "PartDesign::SubtractiveSphere",
            "PartDesign::AdditivePrism",
            "PartDesign::SubtractivePrism",
            "PartDesign::AdditiveTorus",
            "PartDesign::SubtractiveTorus",
            "PartDesign::AdditiveEllipsoid",
            "PartDesign::SubtractiveEllipsoid",
            "PartDesign::AdditiveWedge",
            "PartDesign::SubtractiveWedge",
            "PartDesign::MultiTransform",
            "PartDesign::Scaled",
        ):
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
        self.signer = protocol.ConsentSigner(ttl_s=3600)
        self.settings: dict[str, Any] = {}

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
    "PartDesign::Line",
    "PartDesign::Pad",
    "PartDesign::Pocket",
    "PartDesign::Hole",
    "PartDesign::Revolution",
    "PartDesign::Fillet",
    "PartDesign::Chamfer",
    "PartDesign::Thickness",
    "PartDesign::LinearPattern",
    "PartDesign::PolarPattern",
    "PartDesign::Mirrored",
    "PartDesign::AdditiveLoft",
    "Sketcher::SketchObject",
    "PartDesign::SubtractiveLoft",
    "PartDesign::AdditivePipe",
    "PartDesign::SubtractivePipe",
    "PartDesign::AdditiveHelix",
    "PartDesign::SubtractiveHelix",
    "PartDesign::AdditiveBox",
    "PartDesign::SubtractiveBox",
    "PartDesign::AdditiveCylinder",
    "PartDesign::SubtractiveCylinder",
    "PartDesign::AdditiveCone",
    "PartDesign::SubtractiveCone",
    "PartDesign::AdditiveSphere",
    "PartDesign::SubtractiveSphere",
    "PartDesign::AdditivePrism",
    "PartDesign::SubtractivePrism",
    "PartDesign::AdditiveTorus",
    "PartDesign::SubtractiveTorus",
    "PartDesign::AdditiveEllipsoid",
    "PartDesign::SubtractiveEllipsoid",
    "PartDesign::AdditiveWedge",
    "PartDesign::SubtractiveWedge",
    "PartDesign::SubShapeBinder",
    "PartDesign::MultiTransform",
    "PartDesign::Scaled",
    "PartDesign::Point",
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
        assert doc.recompute_count == 1  # The mutation gate performs the recompute
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


def test_linear_pattern_uses_the_sketch_axis_and_native_count() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="linear_pattern",
            name="Pattern",
            parameters={
                "originals": ["Sketch"],
                "axis": {"object": "Sketch", "sketchAxis": "H_Axis"},
                "count": 4,
                "length": 30,
            },
        )

        feature = doc.getObject("Pattern")
        assert result["object"]["typeId"] == "PartDesign::LinearPattern"
        assert feature is not None
        assert feature.Occurrences == 4
        assert feature.Length == 30.0
        assert feature.Direction[1] == ["H_Axis"]
        # A solid feature becomes the Body Tip even when FreeCAD did not
        # advance it, so the Body result includes the pattern.
        assert result["bodyTip"] == "Pattern"
        assert doc.transactions[-1] == ("commit",)


def test_dressup_requires_a_subelement_list_before_the_transaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="fillet",
                name="Fillet",
                parameters={
                    "base": {"object": "Sketch", "subelement": ""},
                    "subelements": [],
                    "radius": 1,
                },
            )

        assert "subelements must be a nonempty list" in excinfo.value.message
        assert doc.transactions == []


def test_loft_requires_a_known_mode_before_the_transaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="loft",
                name="Loft",
                profile="Sketch",
                parameters={"sections": ["Sketch"], "mode": "sideways"},
            )

        assert "mode 'additive' or 'subtractive'" in excinfo.value.message
        assert doc.transactions == []


def test_loft_sections_are_wire_and_handler_bounded_to_seven() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        schema = module._SEMANTIC_PARAM_SCHEMAS["loft"]["properties"]["sections"]
        assert schema["maxItems"] == 7

        definition = next(
            entry for entry in module.TOOL_DEFINITIONS if entry["name"] == "create_feature"
        )
        arguments = {
            "document": "Doc",
            "body": "Body",
            "kind": "loft",
            "name": "Loft",
            "parameters": {
                "sections": [f"S{index}" for index in range(8)],
                "mode": "additive",
            },
        }
        with pytest.raises(protocol.ProtocolError):
            validate_schema(arguments, definition["inputSchema"])

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="loft",
                name="Loft",
                profile="Sketch",
                parameters=arguments["parameters"],
            )

        assert "at most 7" in excinfo.value.message
        assert doc.transactions == []


def test_dressup_base_rejects_a_signed_subelement_before_the_transaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="fillet",
                name="Fillet",
                parameters={
                    "base": {"object": "Sketch", "subelement": "Face1.<token>"},
                    "subelements": [{"object": "Sketch", "subelement": "Edge1.<token>"}],
                    "radius": 1,
                },
            )

        assert "whole-object" in excinfo.value.message
        assert doc.transactions == []


def test_fillet_resolves_base_and_subelements_into_one_link() -> None:
    """The dress-up wiring maps base + subelements onto the native Base."""
    with load_features() as module:
        from mcp_server.tools import geometry

        edge_shape = FakeShape(solids=1, edges=[object(), object()])
        plate = FakeFeature(
            "Plate", "PartDesign::Pad", properties=("Profile", "Length"), shape=edge_shape
        )
        body = FakeBody(members=[plate], shape=edge_shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.append(plate)
        ctx = FakeCtx(doc)

        references = [
            geometry.make_reference(ctx, doc, plate, "edge", 1),
            geometry.make_reference(ctx, doc, plate, "edge", 2),
        ]

        result = call(
            module,
            ctx,
            kind="fillet",
            name="Fillet",
            parameters={
                "base": {"object": "Plate", "subelement": ""},
                "subelements": references,
                "radius": 1.5,
            },
        )

        feature = doc.getObject("Fillet")
        assert result["object"]["typeId"] == "PartDesign::Fillet"
        assert feature is not None
        linked_obj, labels = feature.Base
        assert linked_obj is plate
        assert labels == ["Edge1", "Edge2"]
        assert feature.Radius == 1.5
        assert result["bodyTip"] == "Fillet"


def test_fillet_rejects_a_foreign_base_before_the_transaction() -> None:
    with load_features() as module:
        from mcp_server.tools import geometry

        edge_shape = FakeShape(solids=1, edges=[object()])
        plate = FakeFeature("Plate", "PartDesign::Pad", properties=("Profile",), shape=edge_shape)
        outsider = FakeFeature(
            "Outsider", "PartDesign::Pad", properties=("Profile",), shape=edge_shape
        )
        body = FakeBody(members=[plate], shape=edge_shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.extend([plate, outsider])
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="fillet",
                name="Fillet",
                parameters={
                    "base": {"object": "Plate", "subelement": ""},
                    "subelements": [geometry.make_reference(ctx, doc, outsider, "edge", 1)],
                    "radius": 1,
                },
            )

        assert "but the base is" in excinfo.value.message
        assert doc.transactions == []


def test_revolve_refuses_an_unverified_sketch_axis_before_the_transaction() -> None:
    """Revolution's ReferenceAxis is verified with a real origin axis only."""
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="revolve",
                name="Rev",
                profile="Sketch",
                parameters={"axis": {"object": "Sketch", "sketchAxis": "H_Axis"}, "angle": 360},
            )

        assert "requires a whole-object native origin axis" in excinfo.value.message
        assert doc.transactions == []


def test_semantic_only_kind_refuses_missing_parameters_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(module, FakeCtx(doc), kind="fillet", name="Fillet")

        assert "requires parameters" in excinfo.value.message
        assert doc.transactions == []


def test_revolve_requires_an_angle_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="revolve",
                name="Rev",
                profile="Sketch",
                parameters={"axis": {"object": "Sketch", "sketchAxis": "H_Axis"}},
            )

        assert "requires parameters: angle" in excinfo.value.message
        assert doc.transactions == []


def test_nonpositive_dressup_scalar_is_refused_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="fillet",
                name="Fillet",
                parameters={
                    "base": {"object": "Sketch", "subelement": ""},
                    "subelements": [{"object": "Sketch", "subelement": "token"}],
                    "radius": 0,
                },
            )

        assert "radius must be a positive finite number" in excinfo.value.message
        assert doc.transactions == []


# ---------------------------------------------------------------------------
# Roadmap kinds 19-24: helix, primitive, subshape_binder, multi_transform,
# scaled and datum_point.
# ---------------------------------------------------------------------------


def test_kind_enum_lists_all_24_kinds() -> None:
    with load_features() as module:
        definition = next(
            entry for entry in module.TOOL_DEFINITIONS if entry["name"] == "create_feature"
        )
        assert definition["inputSchema"]["properties"]["kind"]["enum"] == [
            "datum_plane",
            "datum_line",
            "sketch",
            "pad",
            "pocket",
            "hole",
            "revolve",
            "groove",
            "fillet",
            "chamfer",
            "thickness",
            "draft",
            "linear_pattern",
            "polar_pattern",
            "mirrored",
            "loft",
            "pipe",
            "gear_profile",
            "helix",
            "primitive",
            "subshape_binder",
            "multi_transform",
            "scaled",
            "datum_point",
        ]


def test_helix_writes_native_mode_and_advances_tip() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="helix",
            name="Helix",
            profile="Sketch",
            parameters={
                "axis": {"object": "Sketch", "sketchAxis": "V_Axis"},
                "helix_mode": "pitch_turns",
                "mode": "additive",
                "pitch": 3,
                "turns": 2,
                "angle": 0,
            },
        )

        feature = doc.getObject("Helix")
        assert result["object"]["typeId"] == "PartDesign::AdditiveHelix"
        assert feature.Mode == 1
        assert feature.Pitch == 3.0
        assert feature.Turns == 2.0
        assert feature.Angle == 0.0
        assert feature.ReferenceAxis == (doc.getObject("Sketch"), ["V_Axis"])
        assert result["bodyTip"] == "Helix"
        assert doc.transactions == [("open", "create_feature:Body"), ("commit",)]


def test_helix_missing_driver_pair_fails_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="helix",
                name="Helix",
                profile="Sketch",
                parameters={
                    "axis": {"object": "Sketch", "sketchAxis": "V_Axis"},
                    "helix_mode": "pitch_turns",
                    "mode": "additive",
                    "pitch": 3,
                },
            )

        assert "helix_mode 'pitch_turns' requires turns" in excinfo.value.message
        assert doc.transactions == []


def test_helix_rejects_a_parameter_outside_the_driver_pair() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="helix",
                name="Helix",
                profile="Sketch",
                parameters={
                    "axis": {"object": "Sketch", "sketchAxis": "V_Axis"},
                    "helix_mode": "pitch_turns",
                    "mode": "additive",
                    "pitch": 3,
                    "turns": 2,
                    "height": 9,
                },
            )

        assert "helix_mode 'pitch_turns' does not accept 'height'" in excinfo.value.message
        assert doc.transactions == []


def test_helix_requires_mode_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="helix",
                name="Helix",
                profile="Sketch",
                parameters={
                    "axis": {"object": "Sketch", "sketchAxis": "V_Axis"},
                    "helix_mode": "pitch_turns",
                    "pitch": 3,
                    "turns": 2,
                },
            )

        assert "mode 'additive' or 'subtractive'" in excinfo.value.message
        assert doc.transactions == []


_PRIMITIVE_NATIVE_NAMES = {
    "length": "Length",
    "width": "Width",
    "height": "Height",
    "radius": "Radius",
    "radius1": "Radius1",
    "radius2": "Radius2",
    "radius3": "Radius3",
    "circumradius": "Circumradius",
    "x2_min": "X2min",
    "x2_max": "X2max",
    "z2_min": "Z2min",
    "z2_max": "Z2max",
}

_PRIMITIVE_CASES = [
    ("box", "additive", {"length": 11, "width": 11, "height": 11}),
    ("box", "subtractive", {"length": 10, "width": 10, "height": 10}),
    ("cylinder", "additive", {"radius": 11, "height": 10}),
    ("cylinder", "subtractive", {"radius": 10, "height": 10}),
    ("cone", "additive", {"radius1": 0, "radius2": 4, "height": 10}),
    ("cone", "subtractive", {"radius1": 0, "radius2": 3, "height": 10}),
    ("sphere", "additive", {"radius": 6}),
    ("sphere", "subtractive", {"radius": 5}),
    ("prism", "additive", {"polygon": 6, "circumradius": 4, "height": 10}),
    ("prism", "subtractive", {"polygon": 6, "circumradius": 3, "height": 10}),
    ("torus", "additive", {"radius1": 10, "radius2": 4}),
    ("torus", "subtractive", {"radius1": 10, "radius2": 3}),
    ("ellipsoid", "additive", {"radius1": 2, "radius2": 4, "radius3": 4}),
    ("ellipsoid", "subtractive", {"radius1": 1.5, "radius2": 3, "radius3": 3}),
    ("wedge", "additive", {"x2_min": 5, "x2_max": 5, "z2_min": 0, "z2_max": 10}),
    ("wedge", "subtractive", {"x2_min": 5, "x2_max": 5, "z2_min": 0, "z2_max": 10}),
]


@pytest.mark.parametrize(("shape", "mode", "dimensions"), _PRIMITIVE_CASES)
def test_primitive_matrix_creates_and_advances_tip(
    shape: str, mode: str, dimensions: dict[str, Any]
) -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)
        name = f"Prim_{shape}_{mode}"

        result = call(
            module,
            ctx,
            kind="primitive",
            name=name,
            parameters={"shape": shape, "mode": mode, **dimensions},
        )

        feature = doc.getObject(name)
        assert result["object"]["typeId"] == (
            f"PartDesign::{mode.capitalize()}{shape.capitalize()}"
        )
        for semantic, value in dimensions.items():
            if semantic == "polygon":
                assert feature.Polygon == int(value)
            else:
                assert getattr(feature, _PRIMITIVE_NATIVE_NAMES[semantic]) == float(value)
        assert result["bodyTip"] == name
        assert doc.transactions[-1] == ("commit",)


def test_primitive_missing_required_parameter_fails_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="primitive",
                name="Prim",
                parameters={"shape": "box", "mode": "additive", "length": 11},
            )

        assert "primitive shape 'box' requires width" in excinfo.value.message
        assert doc.transactions == []


def test_primitive_wedge_ordering_fails_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="primitive",
                name="Prim",
                parameters={
                    "shape": "wedge",
                    "mode": "additive",
                    "x2_min": 6,
                    "x2_max": 5,
                    "z2_min": 0,
                    "z2_max": 10,
                },
            )

        assert "wedge requires x2_min <= x2_max" in excinfo.value.message
        assert doc.transactions == []


def test_primitive_cone_accepts_zero_first_radius() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="primitive",
            name="PrimCone",
            parameters={
                "shape": "cone",
                "mode": "additive",
                "radius1": 0,
                "radius2": 4,
                "height": 10,
            },
        )

        feature = doc.getObject("PrimCone")
        assert result["object"]["typeId"] == "PartDesign::AdditiveCone"
        assert feature.Radius1 == 0.0
        assert feature.Radius2 == 4.0
        assert doc.transactions[-1] == ("commit",)


def test_subshape_binder_binds_whole_object_and_signed_face() -> None:
    """Binder references may leave the Body: no membership check applies."""
    with load_features() as module:
        from mcp_server.tools import geometry

        _body, doc = make_body_and_doc(shape=FakeShape())
        plate_shape = FakeShape(faces=[object(), object(), object()])
        plate = FakeFeature("Plate", "Part::Feature", properties=(), shape=plate_shape)
        object.__setattr__(plate, "Document", doc)
        doc.Objects.append(plate)
        ctx = FakeCtx(doc)

        references = [
            {"object": "Plate", "subelement": ""},
            geometry.make_reference(ctx, doc, plate, "face", 3),
        ]

        result = call(
            module,
            ctx,
            kind="subshape_binder",
            name="Binder",
            parameters={"references": references, "make_face": False},
        )

        feature = doc.getObject("Binder")
        assert result["object"]["typeId"] == "PartDesign::SubShapeBinder"
        assert feature.Support == [(plate, [""]), (plate, ["Face3"])]
        assert feature.MakeFace is False
        assert "references->Support(2)" in result["applied"]
        # The binder is deliberately not a Tip kind.
        assert result["bodyTip"] is None
        assert doc.transactions[-1] == ("commit",)


def test_subshape_binder_rejects_empty_references_on_the_wire() -> None:
    with load_features() as module:
        definition = next(
            entry for entry in module.TOOL_DEFINITIONS if entry["name"] == "create_feature"
        )
        arguments = {
            "document": "Doc",
            "body": "Body",
            "kind": "subshape_binder",
            "name": "Binder",
            "parameters": {"references": []},
        }
        with pytest.raises(protocol.ProtocolError):
            validate_schema(arguments, definition["inputSchema"])


def test_multi_transform_creates_parent_and_children() -> None:
    with load_features() as module:
        pad_shape = FakeShape()
        sketch = FakeFeature("Sketch", "Sketcher::SketchObject", properties=())
        pad = FakeFeature(
            "Pad", "PartDesign::Pad", properties=("Profile", "Length"), shape=pad_shape
        )
        body = FakeBody(members=[sketch, pad], shape=pad_shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.extend([sketch, pad])
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="multi_transform",
            name="MT",
            parameters={
                "originals": ["Pad"],
                "transformations": [
                    {
                        "kind": "mirrored",
                        "plane": {"object": "Sketch", "sketchAxis": "H_Axis"},
                    },
                    {
                        "kind": "linear",
                        "axis": {"object": "Sketch", "sketchAxis": "H_Axis"},
                        "length": 20,
                        "count": 3,
                    },
                    {
                        "kind": "polar",
                        "axis": {"object": "Sketch", "sketchAxis": "N_Axis"},
                        "count": 4,
                    },
                ],
            },
        )

        children = [doc.getObject("MTTf0"), doc.getObject("MTTf1"), doc.getObject("MTTf2")]
        parent = doc.getObject("MT")
        assert result["object"]["name"] == "MT"
        assert result["bodyTip"] == "MT"
        assert [child.TypeId for child in children] == [
            "PartDesign::Mirrored",
            "PartDesign::LinearPattern",
            "PartDesign::PolarPattern",
        ]
        assert children[0].MirrorPlane == (sketch, ["H_Axis"])
        assert children[1].Direction == (sketch, ["H_Axis"])
        assert children[1].Length == 20.0
        assert children[1].Occurrences == 3
        assert children[2].Axis == (sketch, ["N_Axis"])
        assert children[2].Occurrences == 4
        assert children[2].Angle == 360.0
        assert parent.Transformations == children
        assert parent.Originals == [pad]
        assert "transformations[0]->MTTf0" in result["applied"]
        assert "originals->Originals(1)" in result["applied"]
        assert doc.transactions[-1] == ("commit",)


def test_multi_transform_mirrored_child_requires_plane_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="multi_transform",
                name="MT",
                parameters={
                    "originals": ["Sketch"],
                    "transformations": [{"kind": "mirrored"}],
                },
            )

        assert "transformations[0] kind 'mirrored' requires plane" in excinfo.value.message
        assert doc.transactions == []


def test_multi_transform_rejects_expression_scalars_in_transformations() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="multi_transform",
                name="MT",
                parameters={
                    "originals": ["Sketch"],
                    "transformations": [
                        {
                            "kind": "linear",
                            "axis": {"object": "Sketch", "sketchAxis": "H_Axis"},
                            "length": {"expression": "Sketch.Constraints.length"},
                            "count": 3,
                        }
                    ],
                },
            )

        assert "transformation scalars accept numbers only" in excinfo.value.message
        assert doc.transactions == []


def test_scaled_writes_factor_and_occurrences() -> None:
    with load_features() as module:
        pad_shape = FakeShape()
        pad = FakeFeature(
            "Pad", "PartDesign::Pad", properties=("Profile", "Length"), shape=pad_shape
        )
        body = FakeBody(members=[pad], shape=pad_shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.append(pad)
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="scaled",
            name="Scaled",
            parameters={"originals": ["Pad"], "factor": 2, "count": 2},
        )

        feature = doc.getObject("Scaled")
        assert result["object"]["typeId"] == "PartDesign::Scaled"
        assert feature.Factor == 2.0
        assert feature.Occurrences == 2
        assert feature.Originals == [pad]
        assert result["bodyTip"] == "Scaled"
        assert "originals->Originals" in result["applied"]
        assert doc.transactions[-1] == ("commit",)


def test_scaled_rejects_nonpositive_factor_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="scaled",
                name="Scaled",
                parameters={"originals": ["Sketch"], "factor": 0, "count": 2},
            )

        assert "factor must be a positive finite number" in excinfo.value.message
        assert doc.transactions == []


def test_datum_point_attaches_to_origin_plane_with_offset() -> None:
    with load_features() as module:
        xy_plane = FakeFeature("XY_Plane", "App::Plane", properties=())
        sketch = FakeFeature("Sketch", "Sketcher::SketchObject", properties=())
        body = FakeBody(members=[sketch], shape=FakeShape(), origin_features=[xy_plane])
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.append(sketch)
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="datum_point",
            name="DatumPoint",
            parameters={"plane": "xy", "offset": 5},
        )

        feature = doc.getObject("DatumPoint")
        assert result["object"]["typeId"] == "PartDesign::Point"
        assert feature.AttachmentSupport == [(xy_plane, "")]
        assert feature.MapMode == "ObjectOrigin"
        assert feature.AttachmentOffset.Base.z == 5.0
        assert "plane->AttachmentSupport" in result["applied"]
        assert "offset->AttachmentOffset" in result["applied"]
        # The datum point is deliberately not a Tip kind.
        assert result["bodyTip"] is None
        assert doc.transactions[-1] == ("commit",)


def test_hole_through_all_succeeds_without_depth() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="hole",
            name="Hole",
            profile="Sketch",
            parameters={"diameter": 4, "depth_type": "through_all"},
        )

        feature = doc.getObject("Hole")
        assert feature.DepthType == "ThroughAll"
        assert feature.Threaded is False
        assert feature.DrillPoint == "Flat"
        assert result["bodyTip"] == "Hole"
        assert doc.transactions[-1] == ("commit",)


def test_hole_through_all_rejects_depth_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="hole",
                name="Hole",
                profile="Sketch",
                parameters={"diameter": 4, "depth": 10, "depth_type": "through_all"},
            )

        assert "through_all ignores depth" in excinfo.value.message
        assert doc.transactions == []


def test_hole_counterbore_requires_its_diameter_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="hole",
                name="Hole",
                profile="Sketch",
                parameters={
                    "diameter": 4,
                    "depth": 10,
                    "cut": "counterbore",
                    "counterbore_depth": 5,
                },
            )

        assert "hole cut 'counterbore' requires counterbore_diameter" in excinfo.value.message
        assert doc.transactions == []


def test_hole_countersink_writes_native_cut_values() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="hole",
            name="Hole",
            profile="Sketch",
            parameters={
                "diameter": 4,
                "depth": 10,
                "cut": "countersink",
                "countersink_diameter": 8,
                "countersink_angle": 90,
            },
        )

        feature = doc.getObject("Hole")
        assert feature.HoleCutType == "Countersink"
        assert feature.HoleCutDiameter == 8.0
        assert feature.HoleCutCountersinkAngle == 90.0
        assert any(entry == "cut->HoleCutType=Countersink" for entry in result["applied"])
        assert doc.transactions[-1] == ("commit",)


def test_hole_thread_size_is_verified_against_the_live_enumeration() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(
            shape=FakeShape(),
            feature_enumerations={"PartDesign::Hole": {"ThreadSize": ["M6", "M8"]}},
        )
        ctx = FakeCtx(doc)

        result = call(
            module,
            ctx,
            kind="hole",
            name="Hole",
            profile="Sketch",
            parameters={
                "diameter": 4,
                "depth": 10,
                "thread": "iso_metric",
                "thread_size": "M6",
            },
        )

        feature = doc.getObject("Hole")
        assert feature.ThreadType == "ISOMetricProfile"
        assert feature.ThreadSize == "M6"
        assert feature.Threaded is True
        assert "thread->ThreadType=ISOMetricProfile" in result["applied"]
        assert "thread_size->ThreadSize=M6" in result["applied"]
        assert doc.transactions[-1] == ("commit",)


def test_hole_unknown_thread_size_reports_valid_entries() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(
            shape=FakeShape(),
            feature_enumerations={"PartDesign::Hole": {"ThreadSize": ["M6", "M8"]}},
        )

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="hole",
                name="Hole",
                profile="Sketch",
                parameters={
                    "diameter": 4,
                    "depth": 10,
                    "thread": "iso_metric",
                    "thread_size": "M99",
                },
            )

        assert "not in the live ThreadSize enumeration" in excinfo.value.message
        assert excinfo.value.details["valid"] == ["M6", "M8"]
        assert excinfo.value.details["validCount"] == 2
        assert "abort" in [transaction[0] for transaction in doc.transactions]


def test_hole_thread_size_without_thread_fails_pretransaction() -> None:
    with load_features() as module:
        _body, doc = make_body_and_doc(shape=FakeShape())

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                FakeCtx(doc),
                kind="hole",
                name="Hole",
                profile="Sketch",
                parameters={"diameter": 4, "depth": 10, "thread_size": "M6"},
            )

        assert "thread_size requires a thread" in excinfo.value.message
        assert doc.transactions == []


def test_edit_feature_applies_hole_cut_and_reports_rows() -> None:
    with load_features() as module:
        hole_shape = FakeShape()
        hole = FakeFeature(
            "Hole",
            "PartDesign::Hole",
            properties=(
                "Diameter",
                "Depth",
                "DepthType",
                "HoleCutType",
                "HoleCutDiameter",
                "HoleCutCountersinkAngle",
                "ThreadType",
                "Threaded",
                "ThreadSize",
            ),
            shape=hole_shape,
        )
        object.__setattr__(hole, "_values", {"Diameter": 4.0, "Depth": 10.0})
        body = FakeBody(members=[hole], shape=hole_shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.append(hole)
        ctx = FakeCtx(doc)

        result = module.HANDLERS["edit_feature"](
            ctx,
            {
                "document": "Doc",
                "body": "Body",
                "object": "Hole",
                "parameters": {
                    "cut": "countersink",
                    "countersink_diameter": 8,
                    "countersink_angle": 90,
                },
            },
        )

        assert hole.HoleCutType == "Countersink"
        assert hole.HoleCutDiameter == 8.0
        assert hole.HoleCutCountersinkAngle == 90.0
        rows = {row["name"]: row for row in result["change"]["properties"]}
        assert rows["cut"]["after"] == "Countersink"
        assert rows["countersink_diameter"]["after"] == 8.0
        assert doc.transactions[-1] == ("commit",)


def test_edit_feature_updates_thread_size_on_existing_threaded_hole() -> None:
    with load_features() as module:
        hole = FakeFeature(
            "Hole",
            "PartDesign::Hole",
            properties=("Diameter", "Depth", "ThreadType", "Threaded", "ThreadSize"),
            shape=FakeShape(),
            enumerations={"ThreadSize": ["M6", "M8"]},
        )
        object.__setattr__(
            hole,
            "_values",
            {
                "Diameter": 4.0,
                "Depth": 10.0,
                "ThreadType": "ISOMetricProfile",
                "Threaded": True,
                "ThreadSize": "M6",
            },
        )
        body = FakeBody(members=[hole], shape=hole.Shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.append(hole)
        ctx = FakeCtx(doc)

        result = module.HANDLERS["edit_feature"](
            ctx,
            {
                "document": "Doc",
                "body": "Body",
                "object": "Hole",
                "parameters": {"thread_size": "M8"},
            },
        )

        assert hole.ThreadSize == "M8"
        assert "thread_size->ThreadSize=M8" in result["applied"]
        rows = {row["name"]: row for row in result["change"]["properties"]}
        assert rows["thread_size"] == {"name": "thread_size", "before": "M6", "after": "M8"}
        assert doc.transactions[-1] == ("commit",)


class _Quantity:
    """Native ``Base.Quantity`` double: a length property reads back as this."""

    def __init__(self, value: float) -> None:
        self.Value = float(value)
        self.UserString = f"{self.Value:g} mm"

    def __str__(self) -> str:
        return self.UserString


class _QuantityPad(FakeFeature):
    """A pad whose ``Length`` reads back as a quantity, like native FreeCAD."""

    def __getattr__(self, name: str) -> Any:
        value = super().__getattr__(name)
        return _Quantity(value) if name == "Length" else value


def test_edit_feature_accepts_a_native_quantity_length() -> None:
    """A native length property reads back as ``Base.Quantity``, not a float.

    Reading it with a plain-number test made every pad/pocket length edit
    report "length resolved to a non-positive value" after the value had
    already been applied, so the edit could never succeed.
    """

    with load_features() as module:
        shape = FakeShape()
        pad = _QuantityPad(
            "Pad",
            "PartDesign::Pad",
            properties=("Profile", "Length", "Type"),
            shape=shape,
        )
        object.__setattr__(pad, "_values", {"Length": 10.0, "Type": "Length"})
        body = FakeBody(members=[pad], tip=pad, shape=shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.append(pad)
        ctx = FakeCtx(doc)

        result = module.HANDLERS["edit_feature"](
            ctx,
            {
                "document": "Doc",
                "body": "Body",
                "object": "Pad",
                "parameters": {"length": 25},
            },
        )

    assert pad.Length.Value == 25.0
    assert doc.transactions[-1] == ("commit",)
    rows = {row["name"]: row for row in result["change"]["properties"]}
    assert rows["length"]["after"] == "25 mm"
