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

    def copy(self) -> Any:
        """A placement-bearing copy, as the native global-shape path needs."""

        copied = FakeShape(
            list(object.__getattribute__(self, "_faces")),
            list(object.__getattribute__(self, "_edges")),
            valid=self._valid,
            solids=self._solids,
            volume=self.Volume,
            bounds=self._bounds,
        )
        return copied

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


class Line:
    """A native ``Part.Line`` stand-in: the class name drives the mapped type."""


class _NativeEdge:
    """A readable native edge double: a straight segment.

    Post-normalization re-verification compares document-space geometry
    fingerprints, so an edge double must expose what a native ``Part.Edge``
    does: the mapped ``Curve`` class, length, bounds, center of mass, a
    parameter range with its midpoint value, the tangent, the closed status
    and both endpoint vertices.
    """

    def __init__(
        self,
        start: tuple[float, float, float] = (0.0, 0.0, 0.0),
        end: tuple[float, float, float] = (10.0, 0.0, 0.0),
    ) -> None:
        self._start = tuple(float(value) for value in start)
        self._end = tuple(float(value) for value in end)
        delta = [self._end[axis] - self._start[axis] for axis in range(3)]
        self._delta = tuple(delta)
        self.Curve = Line()
        self.Length = float(sum(value * value for value in delta) ** 0.5)
        self.ParameterRange = (0.0, 1.0)
        self.FirstParameter = 0.0
        self.LastParameter = 1.0
        self.CenterOfMass = StubVector(*self._at(0.5))
        self.Vertexes = [
            types.SimpleNamespace(Point=StubVector(*self._start)),
            types.SimpleNamespace(Point=StubVector(*self._end)),
        ]
        self.BoundBox = types.SimpleNamespace(
            **{
                f"{axis}{bound}": value
                for axis, index in (("X", 0), ("Y", 1), ("Z", 2))
                for bound, value in (
                    ("Min", min(self._start[index], self._end[index])),
                    ("Max", max(self._start[index], self._end[index])),
                )
            }
        )

    def _at(self, fraction: float) -> list[float]:
        return [
            self._start[axis] + fraction * (self._end[axis] - self._start[axis])
            for axis in range(3)
        ]

    def isClosed(self) -> bool:
        return False

    def valueAt(self, parameter: float) -> StubVector:
        return StubVector(*self._at(float(parameter)))

    def tangentAt(self, parameter: float) -> StubVector:
        return StubVector(*self._delta)


def _native_edges(count: int) -> list[Any]:
    """``count`` distinct native edge doubles, one per root index."""

    return [
        _NativeEdge(start=(0.0, float(index), 0.0), end=(10.0, float(index), 0.0))
        for index in range(count)
    ]


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

    def getGlobalPlacement(self) -> Any:
        """Identity global placement: the doubles have no ancestors."""

        return types.SimpleNamespace()

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

    def getExpression(self, prop: str) -> Any:
        """Fake expression readback: the persisted binding or ``None``."""
        return object.__getattribute__(self, "_values").get("_expressions", {}).get(prop)

    @property
    def ExpressionEngine(self) -> list[tuple[str, str]]:
        """Live bindings only: a cleared (``None``) entry leaves the engine."""
        bindings = object.__getattribute__(self, "_values").get("_expressions", {})
        return [
            (path, expression) for path, expression in bindings.items() if expression is not None
        ]

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


def _snapshot_values(feature: FakeFeature) -> dict:
    """Snapshot one feature's values with the expression map cloned.

    ``setExpression`` mutates the ``_expressions`` map in place, so a bare
    ``dict(...)`` copy would alias it and a rollback could not restore the
    pre-mutation binding.
    """

    snapshot = dict(feature._values)
    expressions = snapshot.get("_expressions")
    if isinstance(expressions, dict):
        snapshot["_expressions"] = dict(expressions)
    return snapshot


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
            # Native undo also restores property values and expression
            # bindings, so a rolled-back edit reads as it did before.
            "values": {
                obj.Name: _snapshot_values(obj)
                for obj in self.Objects
                if isinstance(obj, FakeFeature)
            },
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
                if isinstance(obj, FakeFeature):
                    obj._values.clear()
                    obj._values.update(self._undo["values"][obj.Name])
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
                support={"object": "Sketch"},
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
        # response_detail defaults to compact: no before-state geometry deltas.
        assert set(result["change"]) == {"properties"}


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


def test_create_feature_compact_response_detail_keeps_body_report() -> None:
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
            response_detail="compact",
        )

        assert set(result["change"]) == {"properties"}
        assert result["change"]["properties"] == []
        assert result["bodyReport"]["ok"] is True
        definition = next(
            entry for entry in module.TOOL_DEFINITIONS if entry["name"] == "create_feature"
        )
        validate_schema(result, definition["outputSchema"])


def test_edit_feature_compact_detail_reports_uniform_rows() -> None:
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
                "response_detail": "compact",
            },
        )

        # The removed creation-report fields never appear on an edit result.
        assert "change" not in result
        assert "applied" not in result
        assert "geometryChange" not in result
        (row,) = result["parameterValues"]
        assert row["parameter"] == "length"
        assert row["property"] == "Length"
        assert row["after"] == {"value": "25 mm", "expression": None}
        assert "before" not in row
        assert result["bodyReport"]["ok"] is True
        definition = next(
            entry for entry in module.TOOL_DEFINITIONS if entry["name"] == "edit_feature"
        )
        validate_schema(result, definition["outputSchema"])


def test_edit_feature_reports_supported_kinds_for_unsupported_feature() -> None:
    with load_features() as module:
        thickness = FakeFeature(
            "Thickness",
            "PartDesign::Thickness",
            properties=("Base", "Value", "Reversed"),
            shape=FakeShape(),
        )
        body = FakeBody(members=[thickness], tip=thickness, shape=thickness.Shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.append(thickness)
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            module.HANDLERS["edit_feature"](
                ctx,
                {
                    "document": "Doc",
                    "body": "Body",
                    "object": "Thickness",
                    "parameters": {"radius": 2},
                },
            )

        error = excinfo.value
        assert error.code == VALIDATION_FAILED
        assert error.details["typeId"] == "PartDesign::Thickness"
        assert error.details["nextTool"] == "edit_object"
        assert error.details["supportedKinds"] == [
            "pad",
            "pocket",
            "hole",
            "gear_profile",
            "fillet",
            "chamfer",
            "linear_pattern",
            "polar_pattern",
            "revolve",
        ]
        assert doc.transactions == []


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
            support={"object": "Sketch"},
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
                support={"object": "Sketch"},
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
                    "base": {"object": "Sketch"},
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
                "base": {"object": "Plate"},
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
                    "base": {"object": "Plate"},
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
                    "base": {"object": "Sketch"},
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
            {"object": "Plate"},
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
        rows = {row["parameter"]: row for row in result["parameterValues"]}
        assert rows["cut"]["property"] == "HoleCutType"
        assert rows["cut"]["after"] == {"value": "Countersink", "expression": None}
        assert rows["countersink_diameter"]["property"] == "HoleCutDiameter"
        assert rows["countersink_diameter"]["after"]["value"] == 8.0
        assert rows["countersink_angle"]["property"] == "HoleCutCountersinkAngle"
        assert rows["countersink_angle"]["after"]["value"] == 90.0
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
                "response_detail": "full",
            },
        )

        assert hole.ThreadSize == "M8"
        # Full detail is requested explicitly: the before-state delta is
        # opt-in evidence, not the default.
        (row,) = result["parameterValues"]
        assert row["parameter"] == "thread_size"
        assert row["property"] == "ThreadSize"
        assert row["before"] == {"value": "M6", "expression": None}
        assert row["after"] == {"value": "M8", "expression": None}
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
    rows = {row["parameter"]: row for row in result["parameterValues"]}
    assert rows["length"]["after"] == {"value": "25 mm", "expression": None}


def test_edit_feature_stale_expected_generation_refuses_before_the_transaction() -> None:
    with load_features() as module:
        pad = FakeFeature("Pad", "PartDesign::Pad", properties=("Length",), shape=FakeShape())
        object.__setattr__(pad, "_values", {"Length": 10.0, "Type": "Length"})
        body = FakeBody(members=[pad], tip=pad, shape=pad.Shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.append(pad)
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            module.HANDLERS["edit_feature"](
                ctx,
                {
                    "document": "Doc",
                    "body": "Body",
                    "object": "Pad",
                    "expected_generation": 2,
                    "parameters": {"length": 25},
                },
            )

        assert excinfo.value.code == VALIDATION_FAILED
        assert "changed since inspection" in excinfo.value.message
        assert excinfo.value.details == {
            "reason": "stale_generation",
            "expectedGeneration": 2,
            "actualGeneration": 1,
            "nextTool": "inspect_objects",
        }
        assert doc.transactions == []
        assert pad.Length == 10.0


# ---------------------------------------------------------------------------
# Phase 2 semantic edits: five new kinds, uniform parameterValues readback,
# and forced-expression rollback.
# ---------------------------------------------------------------------------


class _AngleQuantity:
    """A native ``Angle`` quantity double: reads back via ``getValueAs``."""

    def __init__(self, degrees: float) -> None:
        self._degrees = float(degrees)
        self.UserString = f"{self._degrees:g} deg"

    def __str__(self) -> str:
        return self.UserString

    def getValueAs(self, unit: str) -> Any:
        return types.SimpleNamespace(Value=self._degrees)

    @property
    def Value(self) -> float:
        return self._degrees


class _RecomputeDoc(FakeDoc):
    """A document whose recompute resolves bound expressions like native FreeCAD.

    ``bindings`` maps a stored expression string onto the value its property
    must read after the recompute; expressions without an entry leave the
    property untouched, like a sketch constraint the fake cannot evaluate.
    """

    def __init__(
        self, body: FakeBody, *, supported: tuple[str, ...], bindings: dict[str, Any]
    ) -> None:
        super().__init__(body, supported=supported)
        self.bindings = dict(bindings)

    def recompute(self) -> None:
        super().recompute()
        for obj in self.Objects:
            if not isinstance(obj, FakeFeature):
                continue
            for prop, expression in obj._values.get("_expressions", {}).items():
                resolved = self.bindings.get(expression)
                if expression is not None and resolved is not None:
                    setattr(obj, prop, resolved)


def make_edit_doc(
    name: str,
    type_id: str,
    properties: tuple[str, ...],
    values: dict[str, Any],
    *,
    bindings: dict[str, Any] | None = None,
) -> tuple[FakeFeature, FakeDoc]:
    feature = FakeFeature(name, type_id, properties=properties, shape=FakeShape())
    object.__setattr__(feature, "_values", dict(values))
    body = FakeBody(members=[feature], tip=feature, shape=feature.Shape)
    if bindings is None:
        doc: FakeDoc = FakeDoc(body, supported=SUPPORTED)
    else:
        doc = _RecomputeDoc(body, supported=SUPPORTED, bindings=bindings)
    doc.Objects.append(feature)
    return feature, doc


def edit(module: types.ModuleType, ctx: FakeCtx, **arguments: Any):
    arguments.setdefault("document", "Doc")
    arguments.setdefault("body", "Body")
    return module.HANDLERS["edit_feature"](ctx, arguments)


def _parameter_rows(result: dict) -> dict[str, dict]:
    return {row["parameter"]: row for row in result["parameterValues"]}


_EDIT_KIND_FIXTURES = [
    (
        "fillet",
        "PartDesign::Fillet",
        ("Base", "Radius"),
        {"Radius": 1.0},
        {"radius": 2.5},
        {"radius": ("Radius", 2.5)},
    ),
    (
        "chamfer",
        "PartDesign::Chamfer",
        ("Base", "Size"),
        {"Size": 1.0},
        {"size": 1.5},
        {"size": ("Size", 1.5)},
    ),
    (
        "linear_pattern",
        "PartDesign::LinearPattern",
        ("Originals", "Direction", "Length", "Occurrences"),
        {"Length": 20.0, "Occurrences": 3},
        {"length": 30, "count": 4},
        {"length": ("Length", 30.0), "count": ("Occurrences", 4)},
    ),
    (
        "polar_pattern",
        "PartDesign::PolarPattern",
        ("Originals", "Axis", "Angle", "Occurrences"),
        {"Angle": 360.0, "Occurrences": 3},
        {"angle": 270, "count": 6},
        {"angle": ("Angle", 270.0), "count": ("Occurrences", 6)},
    ),
    (
        "revolve",
        "PartDesign::Revolution",
        ("Profile", "ReferenceAxis", "Angle", "Reversed"),
        {"Angle": 360.0, "Reversed": False},
        {"angle": 90, "reversed": True},
        {"angle": ("Angle", 90.0), "reversed": ("Reversed", True)},
    ),
]


@pytest.mark.parametrize(
    ("kind", "type_id", "properties", "values", "parameters", "expected"),
    _EDIT_KIND_FIXTURES,
)
def test_edit_feature_new_kinds_write_native_values(
    kind: str,
    type_id: str,
    properties: tuple[str, ...],
    values: dict[str, Any],
    parameters: dict[str, Any],
    expected: dict[str, tuple[str, Any]],
) -> None:
    with load_features() as module:
        feature, doc = make_edit_doc("Target", type_id, properties, values)
        ctx = FakeCtx(doc)

        result = edit(module, ctx, object="Target", parameters=parameters)

        assert result["object"]["typeId"] == type_id
        assert result["bodyTip"] == "Target"
        assert result["bodyReport"]["ok"] is True
        rows = _parameter_rows(result)
        assert sorted(rows) == sorted(parameters)
        for parameter, (prop, native) in expected.items():
            assert rows[parameter]["property"] == prop
            assert rows[parameter]["after"] == {"value": native, "expression": None}
            assert getattr(feature, prop) == native
        assert doc.transactions[-1] == ("commit",)
        definition = next(
            entry for entry in module.TOOL_DEFINITIONS if entry["name"] == "edit_feature"
        )
        validate_schema(result, definition["outputSchema"])


def test_edit_feature_refuses_a_wrong_kind_parameter() -> None:
    with load_features() as module:
        feature, doc = make_edit_doc(
            "Pad",
            "PartDesign::Pad",
            ("Profile", "Length", "Type", "Reversed"),
            {"Length": 10.0, "Type": "Length"},
        )
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            edit(module, ctx, object="Pad", parameters={"radius": 2})

        error = excinfo.value
        assert error.code == VALIDATION_FAILED
        assert error.details["kind"] == "pad"
        assert error.details["parameter"] == "radius"
        assert error.details["nextTool"] == "inspect_objects"
        assert error.details["supportedParameters"] == [
            "extent",
            "face",
            "length",
            "reversed",
            "symmetric",
        ]
        assert doc.transactions == []
        assert feature.Length == 10.0


def test_edit_feature_refuses_pattern_count_on_a_revolve() -> None:
    with load_features() as module:
        feature, doc = make_edit_doc(
            "Rev",
            "PartDesign::Revolution",
            ("Profile", "ReferenceAxis", "Angle", "Reversed"),
            {"Angle": 360.0, "Reversed": False},
        )
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            edit(module, ctx, object="Rev", parameters={"count": 4})

        error = excinfo.value
        assert error.code == VALIDATION_FAILED
        assert error.details["kind"] == "revolve"
        assert error.details["parameter"] == "count"
        assert error.details["supportedParameters"] == ["angle", "reversed"]
        assert error.details["nextTool"] == "inspect_objects"
        assert doc.transactions == []
        assert feature.Angle == 360.0


def test_edit_feature_angle_expression_past_360_rolls_back() -> None:
    """A recompute resolving an angle expression to 400 degrees refuses."""
    with load_features() as module:
        feature, doc = make_edit_doc(
            "Rev",
            "PartDesign::Revolution",
            ("Profile", "ReferenceAxis", "Angle", "Reversed"),
            {"Angle": 360.0, "Reversed": False},
            bindings={"Sketch.Constraints.sweep": _AngleQuantity(400)},
        )
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            edit(
                module,
                ctx,
                object="Rev",
                parameters={"angle": {"expression": "Sketch.Constraints.sweep"}},
            )

        error = excinfo.value
        assert error.code == VALIDATION_FAILED
        assert error.details["operationState"] == "rolled_back"
        assert "resolved" in error.message
        assert "abort" in [transaction[0] for transaction in doc.transactions]
        # The rollback restores the pre-mutation value and clears the binding.
        assert feature.Angle == 360.0
        assert feature.getExpression("Angle") is None


def test_edit_feature_failed_expression_restores_prior_binding() -> None:
    """A rejected replacement expression preserves the previous binding."""

    with load_features() as module:
        feature, doc = make_edit_doc(
            "Rev",
            "PartDesign::Revolution",
            ("Profile", "ReferenceAxis", "Angle", "Reversed"),
            {"Angle": _AngleQuantity(135), "Reversed": False},
            bindings={"Sketch.Valid": _AngleQuantity(135), "Sketch.Invalid": _AngleQuantity(400)},
        )
        feature.setExpression("Angle", "Sketch.Valid")
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            edit(
                module,
                ctx,
                object="Rev",
                parameters={"angle": {"expression": "Sketch.Invalid"}},
            )

        assert excinfo.value.details["operationState"] == "rolled_back"
        assert feature.getExpression("Angle") == "Sketch.Valid"
        assert feature.Angle.Value == 135


class _RestoreFailingDoc(_RecomputeDoc):
    """A document whose recompute refuses the post-rollback restore pass.

    Opt-in and count-keyed so no shared ``FakeDoc``/``_RecomputeDoc`` behavior
    changes. ``recompute_count`` is the number of recomputes already finished,
    so the override delegates and succeeds on counts 0 and 1 — the mutation's
    own post-body recompute and the gate's rollback recompute — and raises
    from count 2 onward, which is the recompute ``_restore_parameter_expressions``
    issues once the transaction has already aborted. Failing at count 1 instead
    would break the gate's rollback recompute, exercising rollback stage
    ``recompute`` rather than the restore path under test. The increment before
    the raise keeps the count truthful and fails every later call too.
    """

    def recompute(self) -> None:
        if self.recompute_count >= 2:
            self.recompute_count += 1
            raise RuntimeError("the restore recompute refused to run")
        super().recompute()


def test_edit_feature_expression_restore_failure_reports_rollback_failed() -> None:
    """A failed restore after a rolled-back edit never reads as a clean rollback."""

    with load_features() as module:
        feature = FakeFeature(
            "Rev",
            "PartDesign::Revolution",
            properties=("Profile", "ReferenceAxis", "Angle", "Reversed"),
            shape=FakeShape(),
        )
        object.__setattr__(feature, "_values", {"Angle": _AngleQuantity(135), "Reversed": False})
        body = FakeBody(members=[feature], tip=feature, shape=feature.Shape)
        doc = _RestoreFailingDoc(
            body,
            supported=SUPPORTED,
            bindings={"Sketch.Valid": _AngleQuantity(135), "Sketch.Invalid": _AngleQuantity(400)},
        )
        doc.Objects.append(feature)
        feature.setExpression("Angle", "Sketch.Valid")
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            edit(
                module,
                ctx,
                object="Rev",
                parameters={"angle": {"expression": "Sketch.Invalid"}},
            )

        error = excinfo.value
        details = error.details
        assert error.code == VALIDATION_FAILED
        # The gate rolled the mutation back, but the expression restore that
        # follows it did not run: the caller must not read this as rolled_back.
        assert details["operationState"] == "rollback_failed"
        assert details["rollbackFailed"] is True
        assert details["rollbackStage"] == "restore_expressions"
        assert details["originalError"].startswith("ToolError: ")
        assert "angle resolved to 400.0 degrees" in details["originalError"]
        assert details["originalDetails"] == {
            "operationState": "rolled_back",
            "nextAction": "retry_from_original_state",
        }
        assert "restoring the prior expression bindings also failed" in error.message
        assert "the restore recompute refused to run" in error.message
        # Two recomputes succeeded before the third one raised, so the failure
        # landed on the restoration call and not on the gate's rollback stage.
        assert doc.recompute_count == 3
        assert ("commit",) not in doc.transactions


def test_edit_feature_length_expression_resolving_to_zero_rolls_back() -> None:
    """A recompute resolving a length expression to zero refuses the edit."""
    with load_features() as module:
        feature, doc = make_edit_doc(
            "Pattern",
            "PartDesign::LinearPattern",
            ("Originals", "Direction", "Length", "Occurrences"),
            {"Length": 20.0, "Occurrences": 3},
            bindings={"Sketch.Constraints.pitch": 0.0},
        )
        ctx = FakeCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            edit(
                module,
                ctx,
                object="Pattern",
                parameters={"length": {"expression": "Sketch.Constraints.pitch"}},
            )

        error = excinfo.value
        assert error.code == VALIDATION_FAILED
        assert error.details["operationState"] == "rolled_back"
        assert "resolved" in error.message
        assert "abort" in [transaction[0] for transaction in doc.transactions]
        assert feature.Length == 20.0
        assert feature.Occurrences == 3
        assert feature.getExpression("Length") is None


def test_edit_feature_valid_expression_reports_binding_and_resolved_value() -> None:
    with load_features() as module:
        feature, doc = make_edit_doc(
            "Rev",
            "PartDesign::Revolution",
            ("Profile", "ReferenceAxis", "Angle", "Reversed"),
            {"Angle": 360.0, "Reversed": False},
            bindings={"Sketch.Constraints.sweep": _AngleQuantity(90)},
        )
        ctx = FakeCtx(doc)

        result = edit(
            module,
            ctx,
            object="Rev",
            parameters={"angle": {"expression": "Sketch.Constraints.sweep"}},
            response_detail="full",
        )

        (row,) = result["parameterValues"]
        assert row["property"] == "Angle"
        # The persisted binding AND the resolved value, not a setter receipt.
        assert row["after"]["expression"] == "Sketch.Constraints.sweep"
        assert row["after"]["value"] == "90 deg"
        assert row["before"] == {"value": 360.0, "expression": None}
        assert feature.Angle.Value == 90.0
        assert doc.transactions[-1] == ("commit",)
        definition = next(
            entry for entry in module.TOOL_DEFINITIONS if entry["name"] == "edit_feature"
        )
        validate_schema(result, definition["outputSchema"])


def test_edit_feature_numeric_write_clears_a_prior_expression() -> None:
    with load_features() as module:
        feature, doc = make_edit_doc(
            "Pad",
            "PartDesign::Pad",
            ("Profile", "Length", "Type"),
            {"Length": 10.0, "Type": "Length"},
        )
        feature.setExpression("Length", "Sketch.Constraints.width")
        ctx = FakeCtx(doc)

        result = edit(module, ctx, object="Pad", parameters={"length": 25})

        assert feature.Length == 25.0
        assert feature.getExpression("Length") is None
        rows = _parameter_rows(result)
        assert rows["length"]["after"] == {"value": 25.0, "expression": None}
        assert doc.transactions[-1] == ("commit",)


def test_edit_feature_omitted_parameters_preserve_scalars_and_links() -> None:
    with load_features() as module:
        sketch = FakeFeature("Sketch", "Sketcher::SketchObject", properties=())
        direction = (sketch, ["H_Axis"])
        feature, doc = make_edit_doc(
            "Pattern",
            "PartDesign::LinearPattern",
            ("Originals", "Direction", "Length", "Occurrences"),
            {"Length": 20.0, "Occurrences": 4, "Direction": direction},
        )
        ctx = FakeCtx(doc)

        result = edit(module, ctx, object="Pattern", parameters={"count": 3})

        assert feature.Occurrences == 3
        assert feature.Length == 20.0
        assert feature.Direction is direction
        rows = _parameter_rows(result)
        assert sorted(rows) == ["count"]
        assert rows["count"]["after"] == {"value": 3, "expression": None}
        assert doc.transactions[-1] == ("commit",)


def test_edit_feature_omitted_parameters_preserve_a_live_expression() -> None:
    with load_features() as module:
        feature, doc = make_edit_doc(
            "Rev",
            "PartDesign::Revolution",
            ("Profile", "ReferenceAxis", "Angle", "Reversed"),
            {"Angle": 360.0, "Reversed": False},
        )
        feature.setExpression("Angle", "Sketch.Constraints.sweep")
        ctx = FakeCtx(doc)

        result = edit(module, ctx, object="Rev", parameters={"reversed": True})

        assert feature.Reversed is True
        assert feature.Angle == 360.0
        assert feature.getExpression("Angle") == "Sketch.Constraints.sweep"
        rows = _parameter_rows(result)
        assert sorted(rows) == ["reversed"]
        assert rows["reversed"]["property"] == "Reversed"
        assert rows["reversed"]["after"] == {"value": True, "expression": None}
        assert doc.transactions[-1] == ("commit",)


def test_edit_feature_full_detail_adds_deltas_and_geometry_change() -> None:
    with load_features() as module:
        _feature, doc = make_edit_doc(
            "Fillet",
            "PartDesign::Fillet",
            ("Base", "Radius"),
            {"Radius": 1.0},
        )
        ctx = FakeCtx(doc)

        full = edit(
            module, ctx, object="Fillet", parameters={"radius": 2.0}, response_detail="full"
        )
        compact = edit(module, ctx, object="Fillet", parameters={"radius": 3.0})

        full_row = _parameter_rows(full)["radius"]
        assert full_row["before"] == {"value": 1.0, "expression": None}
        assert full_row["after"]["value"] == 2.0
        assert full["geometryChange"]["solidCountBefore"] == 1
        assert full["geometryChange"]["solidCountAfter"] == 1

        compact_row = _parameter_rows(compact)["radius"]
        assert "before" not in compact_row
        assert compact_row["after"]["value"] == 3.0
        assert "geometryChange" not in compact

        definition = next(
            entry for entry in module.TOOL_DEFINITIONS if entry["name"] == "edit_feature"
        )
        for result in (full, compact):
            validate_schema(result, definition["outputSchema"])


# ---------------------------------------------------------------------------
# Query-origin references resolve once per operation.
# ---------------------------------------------------------------------------


class _AdvancingCtx(FakeCtx):
    """A context whose document generation advances on every recompute.

    Native FreeCAD bumps the generation when the tool creates an object or
    recomputes the document, so a second resolution of the caller's
    expected_generation would abort a documented call. The prepared query
    context must serve both the preflight and the applier.
    """

    def __init__(self, doc: FakeDoc) -> None:
        super().__init__(doc)
        self.generation = 1
        original = doc.recompute

        def recompute() -> None:
            self.generation += 1
            original()

        doc.recompute = recompute  # type: ignore[method-assign]

    def document_generation(self, doc: FakeDoc) -> int:
        return self.generation


def test_fillet_query_subelements_survive_the_base_recompute() -> None:
    """A generation-stating query subelement list is not re-guarded."""

    with load_features() as module:
        edge_shape = FakeShape(solids=1, edges=_native_edges(2))
        plate = FakeFeature(
            "Plate", "PartDesign::Pad", properties=("Profile", "Length"), shape=edge_shape
        )
        body = FakeBody(members=[plate], shape=edge_shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.append(plate)
        ctx = _AdvancingCtx(doc)
        # The caller states the generation it inspected, as the schema allows.
        stated = ctx.document_generation(doc)

        result = call(
            module,
            ctx,
            kind="fillet",
            name="Fillet",
            parameters={
                "base": {"object": "Plate"},
                "subelements": [
                    {
                        "object": "Plate",
                        "query": [{"role": "edge"}],
                        "expected_generation": stated,
                    }
                ],
                "radius": 1.5,
            },
        )

        feature = doc.getObject("Fillet")
        assert result["object"]["typeId"] == "PartDesign::Fillet"
        linked_obj, labels = feature.Base
        assert linked_obj is plate
        assert labels == ["Edge1", "Edge2"]
        # The receipt names the parameter and its selection-time count.
        assert result["resolvedSelections"][0]["parameter"] == "subelements[0]"
        assert result["resolvedSelections"][0]["count"] == 2


def test_pad_up_to_face_query_survives_the_creation_generation_bump() -> None:
    """The applier consumes the prepared selection, not a second query run."""

    with load_features() as module:
        face_shape = FakeShape(solids=1, faces=[object()])
        sketch = FakeFeature("Sketch", "Sketcher::SketchObject", properties=("Profile",))
        plate = FakeFeature(
            "Plate", "PartDesign::Pad", properties=("Profile", "Length"), shape=face_shape
        )
        body = FakeBody(members=[plate, sketch], shape=face_shape)
        doc = FakeDoc(body, supported=SUPPORTED)
        doc.Objects.extend([plate, sketch])
        # The harness's Pad fallback omits UpToFace; add it for this case.
        original_new_object = body.newObject

        def new_object(type_id: str, name: str) -> Any:
            feature = original_new_object(type_id, name)
            if type_id == "PartDesign::Pad":
                feature.PropertiesList.append("UpToFace")
            return feature

        object.__setattr__(body, "newObject", new_object)
        ctx = _AdvancingCtx(doc)
        stated = ctx.document_generation(doc)

        call(
            module,
            ctx,
            kind="pad",
            name="Pad",
            profile="Sketch",
            parameters={
                "extent": "up_to_face",
                "face": {
                    "object": "Plate",
                    "query": [{"role": "face"}],
                    "expected_generation": stated,
                },
            },
        )

        feature = doc.getObject("Pad")
        assert feature is not None
        linked_obj, labels = feature.UpToFace
        assert linked_obj is plate
        assert labels == ["Face1"]


def _touching_edge_plate(module: types.ModuleType, *, edges: int = 2) -> tuple[Any, ...]:
    """A dress-up base whose normalization actually runs native touch().

    A real PartDesign feature exposes ``touch``, so ``_apply_base_list``
    recomputes the base and advances the generation; a fixture without it
    would never exercise the re-verification path.
    """

    edge_shape = FakeShape(solids=1, edges=_native_edges(edges))
    plate = FakeFeature(
        "Plate", "PartDesign::Pad", properties=("Profile", "Length"), shape=edge_shape
    )
    plate.touch = lambda: None
    body = FakeBody(members=[plate], shape=edge_shape)
    doc = FakeDoc(body, supported=SUPPORTED)
    doc.Objects.append(plate)
    return plate, body, doc


def test_query_subelements_survive_a_real_base_touch_and_recompute() -> None:
    """The post-normalization re-verification is not blocked by the guard."""

    with load_features() as module:
        plate, _body, doc = _touching_edge_plate(module)
        ctx = _AdvancingCtx(doc)
        stated = ctx.document_generation(doc)

        result = call(
            module,
            ctx,
            kind="fillet",
            name="Fillet",
            parameters={
                "base": {"object": "Plate"},
                "subelements": [
                    {
                        "object": "Plate",
                        "query": [{"role": "edge"}],
                        "expected_generation": stated,
                    }
                ],
                "radius": 1.5,
            },
        )

        feature = doc.getObject("Fillet")
        linked_obj, labels = feature.Base
        assert linked_obj is plate
        assert labels == ["Edge1", "Edge2"]
        assert result["resolvedSelections"][0]["count"] == 2


def test_stale_generation_still_refuses_at_operation_start() -> None:
    """The operation-start guard survives; only re-verification drops it."""

    with load_features() as module:
        _plate, _body, doc = _touching_edge_plate(module)
        ctx = _AdvancingCtx(doc)
        stale = ctx.document_generation(doc) - 1

        with pytest.raises(ToolError) as excinfo:
            call(
                module,
                ctx,
                kind="fillet",
                name="Fillet",
                parameters={
                    "base": {"object": "Plate"},
                    "subelements": [
                        {
                            "object": "Plate",
                            "query": [{"role": "edge"}],
                            "expected_generation": stale,
                        }
                    ],
                    "radius": 1.5,
                },
            )

        assert excinfo.value.details["reason"] == "stale_generation"
        # Refused before the transaction: no abort, no created feature.
        assert doc.transactions == []
        assert doc.getObject("Fillet") is None


def _regenerating_plate(
    edges: list[Any],
    *,
    regenerated: list[Any] | None = None,
) -> tuple[Any, Any, Any]:
    """A base whose normalization replaces its subshapes with fresh doubles.

    The base's native re-execution regenerates the element map, so the
    edges the dress-up re-resolves are brand-new objects with no identity
    relation (not even ``isSame``) to the selection-time ones. Fingerprint
    comparison is what decides whether they still bind.
    """

    edge_shape = FakeShape(solids=1, edges=list(edges))
    plate = FakeFeature(
        "Plate", "PartDesign::Pad", properties=("Profile", "Length"), shape=edge_shape
    )

    def regenerate_edges() -> None:
        fresh = FakeShape(solids=1, edges=list(regenerated if regenerated is not None else edges))
        object.__setattr__(plate, "_shape", fresh)
        body._values["_shape"] = fresh

    plate.touch = regenerate_edges
    body = FakeBody(members=[plate], shape=edge_shape)
    doc = FakeDoc(body, supported=SUPPORTED)
    doc.Objects.append(plate)
    return plate, body, doc


def _coarse_facts(edge: Any) -> tuple:
    """The coarse geometry two edges may share while their paths differ."""

    box = edge.BoundBox
    return (
        edge.Length,
        (box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax),
        (edge.CenterOfMass.x, edge.CenterOfMass.y, edge.CenterOfMass.z),
    )


def _fillet_edges(module: types.ModuleType, ctx: FakeCtx, *, name: str = "Fillet") -> Any:
    """Create a fillet over the whole `Plate` edge query."""

    return call(
        module,
        ctx,
        kind="fillet",
        name=name,
        parameters={
            "base": {"object": "Plate"},
            "subelements": [{"object": "Plate", "query": [{"role": "edge"}]}],
            "radius": 1.5,
        },
    )


def test_dressup_accepts_regenerated_but_geometrically_identical_edges() -> None:
    """A recompute that regenerates equal geometry still binds the selection.

    The base's normalization re-executes the feature, so the edges the
    dress-up re-resolves are new native objects that no ``isSame`` call
    could relate to the selection-time ones (the doubles define none).
    Their document-space fingerprints are identical, so the same indices
    must still be accepted.
    """

    with load_features() as module:
        selected = _native_edges(2)
        plate, _body, doc = _regenerating_plate(
            selected,
            # Identical geometry, deliberately different objects.
            regenerated=_native_edges(2),
        )
        original_shape = plate.Shape
        ctx = _AdvancingCtx(doc)

        result = _fillet_edges(module, ctx)

        # The regeneration really happened: different shape, different edges.
        assert plate.Shape is not original_shape
        assert plate.Shape.Edges[0] is not selected[0]
        feature = doc.getObject("Fillet")
        assert feature is not None
        linked_obj, labels = feature.Base
        assert linked_obj is plate
        assert labels == ["Edge1", "Edge2"]
        assert result["resolvedSelections"][0]["count"] == 2


def test_dressup_refuses_when_edge_geometry_changes_across_normalization() -> None:
    """A recompute that moves the selected edges refuses the dress-up."""

    with load_features() as module:
        _plate, _body, doc = _regenerating_plate(
            _native_edges(2),
            # Same edge count and index set; the segments are twice as long.
            regenerated=[
                _NativeEdge(start=(0.0, float(index), 0.0), end=(20.0, float(index), 0.0))
                for index in range(2)
            ],
        )
        ctx = _AdvancingCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            _fillet_edges(module, ctx)

        assert excinfo.value.details["reason"] == "selection_changed"
        assert excinfo.value.details["parameter"] == "subelements[0]"
        assert doc.getObject("Fillet") is None
        # Refused inside the mutation gate: the transaction rolled back.
        assert ("abort",) in doc.transactions


def test_dressup_refuses_endpoint_change_within_the_same_coarse_bounds() -> None:
    """Bounds, length and center agree; the endpoints do not.

    A reversed segment has the identical bounding box, length and center of
    mass, so only endpoint (and tangent) evidence can tell the two apart.
    """

    with load_features() as module:
        original = _NativeEdge(start=(0.0, 0.0, 0.0), end=(10.0, 0.0, 0.0))
        reversed_edge = _NativeEdge(start=(10.0, 0.0, 0.0), end=(0.0, 0.0, 0.0))
        # Only the traversal direction differs: every coarse fact agrees, so
        # bounds/length/center evidence alone could not separate them.
        assert _coarse_facts(reversed_edge) == _coarse_facts(original)
        _plate, _body, doc = _regenerating_plate(
            [original, _native_edges(2)[1]],
            regenerated=[reversed_edge, _native_edges(2)[1]],
        )
        ctx = _AdvancingCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            _fillet_edges(module, ctx)

        assert excinfo.value.details["reason"] == "selection_changed"
        assert doc.getObject("Fillet") is None


def test_dressup_refuses_when_the_selection_geometry_is_unreadable() -> None:
    """An absent fingerprint is never a match.

    The doubles expose no readable geometry, so neither the selection-time
    snapshot nor the re-resolved one carries enough evidence to prove the
    correspondence; the dress-up refuses instead of binding blind.
    """

    with load_features() as module:

        class _UnreadableEdge:
            """A native edge double whose every geometry read fails."""

            def __getattr__(self, name: str) -> Any:
                raise RuntimeError(f"unreadable {name}")

        _plate, _body, doc = _regenerating_plate(
            [_UnreadableEdge(), _UnreadableEdge()],
            regenerated=[_UnreadableEdge(), _UnreadableEdge()],
        )
        ctx = _AdvancingCtx(doc)

        with pytest.raises(ToolError) as excinfo:
            _fillet_edges(module, ctx)

        assert excinfo.value.details["reason"] == "selection_changed"
        assert doc.getObject("Fillet") is None
