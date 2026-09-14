"""Tests for mcp_server.tools.geometry with isolated FreeCAD/Part doubles.

The suite never imports real FreeCAD: ``Part``/``FreeCAD`` are stubbed per
test through ``monkeypatch`` (geometry imports them lazily inside its
handlers) and ``object_validation`` is stubbed at import time only when the
real module is not present yet, so the same assertions run against the real
implementation once it lands.
"""

import importlib
import math
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))


def _ensure_object_validation() -> None:
    """Use the real module when present; otherwise install the pinned stub."""

    try:
        importlib.import_module("mcp_server.object_validation")
    except Exception:
        module = types.ModuleType("mcp_server.object_validation")

        def object_validity_error(obj):
            return getattr(obj, "validity_error", None)

        def geometry_report(obj, expected_solids=None):
            error = object_validity_error(obj)
            shape = getattr(obj, "Shape", None)
            if shape is None:
                shape_valid = None
                solid_count = None
                volume = None
                bounds = None
                diagnostics: list = []
                max_tolerance = None
            else:
                shape_valid = bool(shape.isValid())
                solid_count = len(shape.Solids)
                volume = shape.Volume
                box = shape.BoundBox
                bounds = [box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax]
                diagnostics = list(shape.check())
                max_tolerance = shape.getTolerance(1)
            if expected_solids is None:
                ok, report_error = True, None
            elif shape is None or solid_count != expected_solids:
                ok = False
                report_error = f"expected {expected_solids} solids"
            else:
                ok, report_error = True, None
            return {
                "name": obj.Name,
                "state": list(getattr(obj, "State", [])),
                "object_valid": error is None,
                "shape_valid": shape_valid,
                "solid_count": solid_count,
                "volume": volume,
                "bounds": bounds,
                "diagnostics": diagnostics,
                "max_tolerance": max_tolerance,
                "ok": ok,
                "error": report_error,
            }

        module.object_validity_error = object_validity_error
        module.geometry_report = geometry_report
        sys.modules["mcp_server.object_validation"] = module


_ensure_object_validation()

from mcp_server import protocol
from mcp_server.tools import geometry

# ---------------------------------------------------------------------------
# FreeCAD doubles.
# ---------------------------------------------------------------------------


class FakeVector:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = float(x), float(y), float(z)


class FakeBoundBox:
    def __init__(self, xmin, ymin, zmin, xmax, ymax, zmax):
        self.XMin, self.YMin, self.ZMin = float(xmin), float(ymin), float(zmin)
        self.XMax, self.YMax, self.ZMax = float(xmax), float(ymax), float(zmax)


class FakeShape:
    def __init__(
        self,
        *,
        volume=1000.0,
        bounds=(0.0, 0.0, 0.0, 10.0, 10.0, 10.0),
        solids=1,
        faces=(),
        edges=(),
        check_results=(),
        tolerance=1e-7,
        is_null=False,
        valid=True,
    ):
        self.Volume = float(volume)
        self.BoundBox = FakeBoundBox(*bounds)
        self.Solids = [SimpleNamespace() for _ in range(solids)]
        self.Faces = list(faces)
        self.Edges = list(edges)
        self._check = list(check_results)
        self._tolerance = float(tolerance)
        self._is_null = bool(is_null)
        self._valid = bool(valid)
        self._distance = None
        self._common = None
        self._section = None
        self._cut = None

    def copy(self):
        copied = FakeShape(
            volume=self.Volume,
            bounds=(
                self.BoundBox.XMin,
                self.BoundBox.YMin,
                self.BoundBox.ZMin,
                self.BoundBox.XMax,
                self.BoundBox.YMax,
                self.BoundBox.ZMax,
            ),
            solids=len(self.Solids),
            faces=list(self.Faces),
            edges=list(self.Edges),
            check_results=list(self._check),
            tolerance=self._tolerance,
            is_null=self._is_null,
            valid=self._valid,
        )
        copied._distance = self._distance
        copied._common = self._common
        copied._section = self._section
        copied._cut = self._cut
        return copied

    def isNull(self):
        # probes["shape.null_attributes"]: a real shape is not null.
        return self._is_null

    def isValid(self):
        return self._valid

    def check(self):
        return list(self._check)

    def getTolerance(self, mode):
        return self._tolerance

    def distToShape(self, other):
        assert self._distance is not None, "unexpected distToShape call"
        return self._distance

    def common(self, other):
        assert self._common is not None, "unexpected common call"
        return self._common

    def section(self, other):
        assert self._section is not None, "unexpected section call"
        return self._section

    def cut(self, other):
        assert self._cut is not None, "unexpected cut call"
        return self._cut


class Plane:
    """A native ``Part.Plane`` stand-in: the class name drives %TYPE."""


class Cylinder:
    def __init__(self, radius=None, axis=(0.0, 0.0, 1.0)):
        self.Radius = radius
        self.Axis = FakeVector(*axis)


class FakeFace:
    def __init__(
        self,
        *,
        area,
        bounds,
        normal=(0.0, 0.0, 1.0),
        center=(0.0, 0.0, 0.0),
        surface=None,
    ):
        self.Area = float(area)
        self.BoundBox = FakeBoundBox(*bounds)
        self.ParameterRange = (0.0, 10.0, 0.0, 10.0)
        self._normal = FakeVector(*normal)
        self._center = FakeVector(*center)
        self._distance = None
        self.Surface = Plane() if surface is None else surface
        self.CenterOfMass = FakeVector(*center)
        self.Edges = []

    def normalAt(self, u, v):
        return self._normal

    def valueAt(self, u, v):
        return self._center

    def distToShape(self, other):
        assert self._distance is not None, "unexpected distToShape call"
        return self._distance


class Line:
    """A native ``Part.Line`` stand-in: the class name drives %TYPE."""


class Circle:
    """A native ``Part.Circle`` stand-in: the class name drives %TYPE."""

    def __init__(self, radius=None, axis=(0.0, 0.0, 1.0), center=(0.0, 0.0, 0.0)):
        self.Radius = radius
        self.Axis = FakeVector(*axis)
        self.Center = FakeVector(*center)


class FakeEdge:
    def __init__(
        self,
        *,
        length,
        bounds,
        curve=None,
        closed=False,
        points=(),
        center=(0.0, 0.0, 0.0),
    ):
        self.Length = float(length)
        self.BoundBox = FakeBoundBox(*bounds)
        self._default_curve = curve
        self._closed = bool(closed)
        self.Vertexes = [SimpleNamespace(Point=FakeVector(*point)) for point in points if point]
        self.ParameterRange = (0.0, 1.0)
        self.FirstParameter = 0.0
        self.LastParameter = 1.0
        self.Curve = Line() if curve is None else curve
        self.CenterOfMass = FakeVector(*center)
        # Mirror the native surface attribute so query records classify edges.
        self._direction = (0.0, 0.0, 1.0)

    def isClosed(self):
        return self._closed

    def tangentAt(self, parameter):
        return FakeVector(*self._direction)

    def valueAt(self, parameter):
        """Linear interpolation between the stored endpoints, as a native edge does."""

        fraction = float(parameter) - self.ParameterRange[0]
        first = self.Vertexes[0].Point
        last = self.Vertexes[-1].Point
        return FakeVector(
            first.x + fraction * (last.x - first.x),
            first.y + fraction * (last.y - first.y),
            first.z + fraction * (last.z - first.z),
        )

    def isSame(self, other):
        return self is other


class FakeObject:
    def __init__(self, name, shape=None):
        self.Name = name
        self.Label = name
        self.Shape = shape
        self.State = []
        self.validity_error = None
        # Opaque placement stand-in: global equals local (no ancestors).
        self.Placement = SimpleNamespace()

    def getGlobalPlacement(self):
        return self.Placement

    def isValid(self):
        return True


class FakeDocument:
    Name = "Doc"


class FakeCtx:
    def __init__(self, objects, generation=1):
        self.signer = protocol.ConsentSigner(ttl_s=3600)
        self._objects = dict(objects)
        self.generation = generation
        self._identities = {}
        self.doc = FakeDocument()

    def document_identity(self, doc):
        if doc not in self._identities:
            self._identities[doc] = f"identity-{len(self._identities) + 1}"
        return self._identities[doc]

    def document_generation(self, doc):
        return self.generation

    def require_document(self, name):
        if name != self.doc.Name:
            raise protocol.ToolError(protocol.DOCUMENT_NOT_FOUND, f"unknown document {name!r}")
        return self.doc

    def require_object(self, doc, name):
        try:
            return self._objects[name]
        except KeyError:
            raise protocol.ToolError(
                protocol.OBJECT_NOT_FOUND, f"unknown object {name!r}"
            ) from None


# ---------------------------------------------------------------------------
# Test helpers.
# ---------------------------------------------------------------------------


@pytest.fixture
def part_stub(monkeypatch):
    class Line:
        pass

    class Circle:
        def __init__(self, radius=None, center=None, axis=None):
            self.Radius = radius
            self.Center = center
            self.Axis = axis

    class Polygon:
        def __init__(self, points):
            self.points = points

    class Face:
        def __init__(self, wire):
            self.wire = wire

    module = types.ModuleType("Part")
    module.Line = Line
    module.Circle = Circle
    module.makePolygon = lambda points: Polygon(points)
    module.Face = lambda wire: Face(wire)
    monkeypatch.setitem(sys.modules, "Part", module)
    return module


@pytest.fixture
def freecad_stub(monkeypatch):
    class Vector:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = float(x), float(y), float(z)

    module = types.ModuleType("FreeCAD")
    module.Vector = Vector
    monkeypatch.setitem(sys.modules, "FreeCAD", module)
    return module


def _definition(name):
    return next(entry for entry in geometry.TOOL_DEFINITIONS if entry["name"] == name)


def _assert_output_schema(result, name):
    protocol.validate_schema(result, _definition(name)["outputSchema"])


def _two_face_object():
    face1 = FakeFace(
        area=100.0,
        bounds=(0.0, 0.0, 0.0, 10.0, 10.0, 1.0),
        center=(5.0, 5.0, 0.5),
    )
    face2 = FakeFace(
        area=100.0,
        bounds=(0.0, 0.0, 5.0, 10.0, 10.0, 6.0),
        center=(5.0, 5.0, 5.5),
    )
    shell = FakeShape(volume=500.0, solids=1, faces=[face1, face2])
    return FakeObject("Shell", shell)


# ---------------------------------------------------------------------------
# validate_geometry.
# ---------------------------------------------------------------------------


def test_validate_geometry_solid_invariants():
    ctx = FakeCtx({"Box": FakeObject("Box", FakeShape(volume=1000.0, tolerance=1e-7))})
    result = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "objects": ["Box"],
            "expected_solids": 1,
            "expected_bounds": {"Box": [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]},
        },
    )
    assert result["document"] == {"name": "Doc", "generation": 1}
    assert result["units"]["volume"] == "mm3"
    entry = result["objects"][0]
    assert entry["name"] == "Box"
    assert entry["solid_count"] == 1
    assert entry["volume"] == 1000.0
    assert entry["bounds"] == [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]
    assert entry["max_tolerance"] == 1e-7
    assert entry["diagnostics"] == []
    verdicts = entry["verdicts"]
    assert verdicts["solids"] == "match"
    assert verdicts["volume"] == "positive"
    assert verdicts["bounds"]["verdict"] == "match"
    assert verdicts["bounds"]["deviations"] == [0.0] * 6
    assert entry["valid"] is True
    assert result["all_valid"] is True
    _assert_output_schema(result, "validate_geometry")


def test_validate_geometry_bounds_tolerance_window():
    shape = FakeShape(volume=1000.0)
    ctx = FakeCtx({"Box": FakeObject("Box", shape)})
    base = {"document": "Doc", "objects": ["Box"]}
    drifted = {"Box": [0.0, 0.0, 0.0, 10.0, 10.0, 10.5]}
    result = geometry.HANDLERS["validate_geometry"](ctx, {**base, "expected_bounds": drifted})
    verdict = result["objects"][0]["verdicts"]["bounds"]
    assert verdict["verdict"] == "mismatch"
    assert verdict["deviations"] == [0.0, 0.0, 0.0, 0.0, 0.0, 0.5]
    assert result["objects"][0]["valid"] is False
    assert result["all_valid"] is False
    tolerant = geometry.HANDLERS["validate_geometry"](
        ctx, {**base, "expected_bounds": drifted, "bounds_tolerance": 0.6}
    )
    assert tolerant["objects"][0]["verdicts"]["bounds"]["verdict"] == "match"
    assert tolerant["all_valid"] is True


def test_validate_geometry_unavailable_bounds_verdict_through_shared_helper():
    # Shapeless objects keep null bounds; an expected_bounds request then
    # produces the shared helper's "unavailable" verdict and the entry is
    # invalid — identical semantics to the pre-refactor inline comparison.
    ctx = FakeCtx({"Group": FakeObject("Group", None)})
    result = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "objects": ["Group"],
            "expected_bounds": {"Group": [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]},
        },
    )
    entry = result["objects"][0]
    verdict = entry["verdicts"]["bounds"]
    assert verdict["verdict"] == "unavailable"
    assert verdict["deviations"] is None
    assert entry["valid"] is False
    assert result["all_valid"] is False
    _assert_output_schema(result, "validate_geometry")


def test_validate_geometry_without_expected_bounds_has_no_verdict():
    ctx = FakeCtx({"Box": FakeObject("Box", FakeShape(volume=1000.0))})
    result = geometry.HANDLERS["validate_geometry"](ctx, {"document": "Doc", "objects": ["Box"]})
    assert "bounds" not in result["objects"][0]["verdicts"]
    assert result["objects"][0]["valid"] is True


def test_placed_shape_applies_global_placement_exactly_once():
    local = SimpleNamespace(name="local")
    global_placement = SimpleNamespace(name="global")
    shape = FakeShape(bounds=(0.0, 0.0, 0.0, 10.0, 10.0, 10.0))
    obj = FakeObject("Child", shape)
    obj.Placement = local
    obj.getGlobalPlacement = lambda: global_placement

    placed = geometry.placed_shape(obj)

    assert placed is not shape  # a copy, the source is untouched
    # The copy carries the global placement itself; the local placement was
    # never multiplied a second time.
    assert placed.Placement is global_placement
    assert obj.Placement is local
    assert obj.Shape is shape


def test_placed_shape_failures_are_fail_closed():
    # Shapeless object.
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.placed_shape(FakeObject("Empty", None))
    assert excinfo.value.code == "VALIDATION_FAILED"
    assert excinfo.value.details == {"object": "Empty"}

    # Failed global-placement access.
    broken = FakeObject("Broken", FakeShape())
    broken.Placement = SimpleNamespace()

    def _boom():
        raise RuntimeError("no ancestor chain")

    broken.getGlobalPlacement = _boom
    with pytest.raises(protocol.ToolError):
        geometry.placed_shape(broken)

    # App::Link with a non-identity scale is unsupported geometry.
    link = FakeObject("Link", FakeShape())
    link.TypeId = "App::Link"
    link.Placement = SimpleNamespace()
    link.Scale = FakeVector(2.0, 1.0, 1.0)
    with pytest.raises(protocol.ToolError):
        geometry.placed_shape(link)

    # An ordinary unscaled link resolves through the native API.
    plain_link = FakeObject("PlainLink", FakeShape())
    plain_link.TypeId = "App::Link"
    plain_link.Placement = SimpleNamespace()
    plain_link.Scale = FakeVector(1.0, 1.0, 1.0)
    placed = geometry.placed_shape(plain_link)
    assert placed.Placement is plain_link.Placement


def test_validate_geometry_valid_non_solid():
    ctx = FakeCtx({"Group": FakeObject("Group", None)})
    result = geometry.HANDLERS["validate_geometry"](ctx, {"document": "Doc", "objects": ["Group"]})
    entry = result["objects"][0]
    assert entry["shape_valid"] is None
    assert entry["solid_count"] is None
    assert entry["volume"] is None
    assert entry["verdicts"]["solids"] == "unspecified"
    assert entry["verdicts"]["volume"] == "not_applicable"
    assert entry["valid"] is True
    forced = geometry.HANDLERS["validate_geometry"](
        ctx, {"document": "Doc", "objects": ["Group"], "expected_solids": 1}
    )
    entry = forced["objects"][0]
    assert entry["verdicts"]["solids"] == "unavailable"
    assert entry["error"] is not None
    assert entry["valid"] is False


def test_validate_geometry_zero_volume_solid_fails():
    shape = FakeShape(volume=0.0, check_results=["surface BRep check failed"])
    ctx = FakeCtx({"Box": FakeObject("Box", shape)})
    result = geometry.HANDLERS["validate_geometry"](
        ctx, {"document": "Doc", "objects": ["Box"], "expected_solids": 1}
    )
    entry = result["objects"][0]
    assert entry["verdicts"]["volume"] == "nonpositive"
    assert entry["diagnostics"] == ["surface BRep check failed"]
    assert entry["valid"] is False
    assert result["all_valid"] is False


def test_validate_geometry_missing_document_and_object():
    ctx = FakeCtx({"Box": FakeObject("Box", FakeShape())})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["validate_geometry"](ctx, {"document": "Ghost", "objects": ["Box"]})
    assert excinfo.value.code == protocol.DOCUMENT_NOT_FOUND
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["validate_geometry"](ctx, {"document": "Doc", "objects": ["Ghost"]})
    assert excinfo.value.code == protocol.OBJECT_NOT_FOUND


# ---------------------------------------------------------------------------
# measure: distance and interference.
# ---------------------------------------------------------------------------


def test_measure_distance_between_whole_objects():
    box_a = FakeObject("BoxA", FakeShape(bounds=(0.0, 0.0, 0.0, 10.0, 10.0, 10.0)))
    box_b = FakeObject("BoxB", FakeShape(bounds=(35.0, 0.0, 0.0, 45.0, 10.0, 10.0)))
    box_a.Shape._distance = (25.0, [(FakeVector(10, 0, 0), FakeVector(35, 0, 0))], None)
    ctx = FakeCtx({"BoxA": box_a, "BoxB": box_b})
    result = geometry.HANDLERS["measure"](
        ctx,
        {"document": "Doc", "a": {"object": "BoxA"}, "mode": "distance", "b": {"object": "BoxB"}},
    )
    assert result["mode"] == "distance"
    assert result["distance"] == 25.0
    assert result["points"] == {"a": [10.0, 0.0, 0.0], "b": [35.0, 0.0, 0.0]}
    assert result["a"] == {"object": "BoxA"}
    assert result["b"] == {"object": "BoxB"}
    assert result["units"]["length"] == "mm"
    _assert_output_schema(result, "measure")


def test_measure_requires_b_for_distance_and_interference():
    box = FakeObject("Box", FakeShape())
    ctx = FakeCtx({"Box": box})
    for mode in ("distance", "interference", "difference"):
        with pytest.raises(protocol.ToolError) as excinfo:
            geometry.HANDLERS["measure"](
                ctx, {"document": "Doc", "a": {"object": "Box"}, "mode": mode}
            )
        assert excinfo.value.code == protocol.VALIDATION_FAILED
        assert "'b'" in excinfo.value.message


def test_measure_interference_common_volume():
    box_a = FakeObject("BoxA", FakeShape())
    box_b = FakeObject("BoxB", FakeShape())
    box_a.Shape._common = FakeShape(volume=500.0, solids=0)
    ctx = FakeCtx({"BoxA": box_a, "BoxB": box_b})
    result = geometry.HANDLERS["measure"](
        ctx,
        {
            "document": "Doc",
            "a": {"object": "BoxA"},
            "mode": "interference",
            "b": {"object": "BoxB"},
        },
    )
    assert result["common_volume"] == 500.0
    assert result["overlaps"] is True
    assert result["units"]["volume"] == "mm3"
    box_a.Shape._common = FakeShape(volume=0.0, solids=0)
    result = geometry.HANDLERS["measure"](
        ctx,
        {
            "document": "Doc",
            "a": {"object": "BoxA"},
            "mode": "interference",
            "b": {"object": "BoxB"},
        },
    )
    assert result["common_volume"] == 0.0
    assert result["overlaps"] is False
    _assert_output_schema(result, "measure")


def test_measure_interference_rejects_nonfinite_common_volume():
    box_a = FakeObject("BoxA", FakeShape())
    box_b = FakeObject("BoxB", FakeShape())
    box_a.Shape._common = FakeShape(volume=float("nan"), solids=0)
    ctx = FakeCtx({"BoxA": box_a, "BoxB": box_b})

    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "a": {"object": "BoxA"},
                "mode": "interference",
                "b": {"object": "BoxB"},
            },
        )

    assert excinfo.value.code == protocol.VALIDATION_FAILED
    assert excinfo.value.details == {
        "reason": "measurement_unavailable",
        "measurement": "common_volume",
    }


def test_measure_difference_volume_and_shape_facts():
    box_a = FakeObject("BoxA", FakeShape())
    box_b = FakeObject("BoxB", FakeShape())
    box_a.Shape._cut = FakeShape(
        volume=875.0,
        solids=1,
        bounds=(0.0, 0.0, 0.0, 10.0, 10.0, 10.0),
    )
    ctx = FakeCtx({"BoxA": box_a, "BoxB": box_b})
    result = geometry.HANDLERS["measure"](
        ctx,
        {"document": "Doc", "a": {"object": "BoxA"}, "mode": "difference", "b": {"object": "BoxB"}},
    )
    assert result["mode"] == "difference"
    assert result["difference_volume"] == 875.0
    assert result["solid_count"] == 1
    assert result["bounds"] == [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]
    assert result["shape_valid"] is True
    assert result["units"]["volume"] == "mm3"
    _assert_output_schema(result, "measure")


def test_measure_difference_empty_result():
    box_a = FakeObject("BoxA", FakeShape())
    box_b = FakeObject("BoxB", FakeShape())
    box_a.Shape._cut = FakeShape(volume=0.0, solids=0, is_null=True)
    ctx = FakeCtx({"BoxA": box_a, "BoxB": box_b})
    result = geometry.HANDLERS["measure"](
        ctx,
        {"document": "Doc", "a": {"object": "BoxA"}, "mode": "difference", "b": {"object": "BoxB"}},
    )
    assert result["difference_volume"] == 0.0
    assert result["solid_count"] == 0
    assert result["bounds"] is None
    assert result["shape_valid"] is True
    _assert_output_schema(result, "measure")


def test_measure_difference_reports_null_bounds_for_an_occ_empty_cut():
    """OCC keeps a fully consumed cut non-null with an inverted bounding box.

    Live 1.1.3 reports ``isNull() False``, ``Volume 0``, no solids and a box
    of ±DBL_MAX for ``big.cut(small)``; reporting that box as bounds would
    publish a nonsense extent instead of the documented empty result.
    """

    box_a = FakeObject("BoxA", FakeShape())
    box_b = FakeObject("BoxB", FakeShape())
    limit = sys.float_info.max
    box_a.Shape._cut = FakeShape(
        volume=0.0,
        solids=0,
        bounds=(limit, limit, limit, -limit, -limit, -limit),
    )
    ctx = FakeCtx({"BoxA": box_a, "BoxB": box_b})
    result = geometry.HANDLERS["measure"](
        ctx,
        {"document": "Doc", "a": {"object": "BoxA"}, "mode": "difference", "b": {"object": "BoxB"}},
    )
    assert result["difference_volume"] == 0.0
    assert result["solid_count"] == 0
    assert result["bounds"] is None
    _assert_output_schema(result, "measure")


def test_measure_difference_rejects_nonfinite_volume():
    box_a = FakeObject("BoxA", FakeShape())
    box_b = FakeObject("BoxB", FakeShape())
    # One solid with an unreadable volume: not the empty-result path.
    box_a.Shape._cut = FakeShape(volume=float("nan"), solids=1)
    ctx = FakeCtx({"BoxA": box_a, "BoxB": box_b})

    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "a": {"object": "BoxA"},
                "mode": "difference",
                "b": {"object": "BoxB"},
            },
        )

    assert excinfo.value.code == protocol.VALIDATION_FAILED
    assert excinfo.value.details == {
        "reason": "measurement_unavailable",
        "measurement": "difference_volume",
    }


# ---------------------------------------------------------------------------
# measure: subshape selection.
# ---------------------------------------------------------------------------


def test_measure_query_ambiguity_reports_signed_candidates():
    shell = _two_face_object()
    # Both faces share the same center height, so the >Z cluster holds two
    # matches and a singleton consumer must refuse with bounded evidence.
    shell.Shape.Faces[1].CenterOfMass = FakeVector(5.0, 5.0, 0.5)
    ctx = FakeCtx({"Shell": shell, "BoxB": FakeObject("BoxB", FakeShape())})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "a": {"object": "Shell", "query": [{"role": "face", "selector": ">Z"}]},
                "mode": "distance",
                "b": {"object": "BoxB"},
            },
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED
    details = excinfo.value.details
    assert details["reason"] == "selection_ambiguous"
    assert details["matchCount"] == 2
    assert details["nextTool"] == "inspect_topology"
    candidates = details["candidates"]
    assert len(candidates) == 2
    assert all(candidate["object"] == "Shell" for candidate in candidates)
    assert all("." in candidate["subelement"] for candidate in candidates)


def test_measure_query_resolves_unique_reference():
    shell = _two_face_object()
    other = FakeObject("BoxB", FakeShape(bounds=(20.0, 0.0, 5.0, 30.0, 10.0, 15.0)))
    # The top face (z=5.5 center) needs a distinct center for >Z selection.
    shell.Shape.Faces[1].CenterOfMass = FakeVector(5.0, 5.0, 5.5)
    shell.Shape.Faces[1]._distance = (
        10.0,
        [(FakeVector(10, 5, 5.5), FakeVector(20, 5, 5.5))],
        None,
    )
    ctx = FakeCtx({"Shell": shell, "BoxB": other})
    result = geometry.HANDLERS["measure"](
        ctx,
        {
            "document": "Doc",
            "a": {"object": "Shell", "query": [{"role": "face", "selector": ">Z"}]},
            "mode": "distance",
            "b": {"object": "BoxB"},
        },
    )
    assert result["distance"] == 10.0
    token = result["a"]["subelement"]
    assert result["a"]["object"] == "Shell"
    assert "." in token and not token.startswith("Face")
    doc = ctx.require_document("Doc")
    obj, native = geometry.resolve_reference(ctx, doc, result["a"])
    assert obj is shell and native == "Face2"
    _assert_output_schema(result, "measure")


def test_measure_query_no_match_is_selection_empty():
    shell = _two_face_object()
    # Neither face is circular, so %CIRCLE matches nothing: an empty query
    # result is a named singleton refusal, not a silent zero.
    ctx = FakeCtx({"Shell": shell, "BoxB": FakeObject("BoxB", FakeShape())})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "a": {"object": "Shell", "query": [{"role": "face", "selector": "%CIRCLE"}]},
                "mode": "distance",
                "b": {"object": "BoxB"},
            },
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED
    assert excinfo.value.details["reason"] == "selection_empty"
    assert excinfo.value.details["matchCount"] == 0


def test_measure_rejects_removed_selector_forms():
    """Bare strings and exact-box selectors are removed wire forms."""

    definition = _definition("measure")
    for removed in (
        "Shell",
        {"object": "Shell", "role": "face", "box": [0.0, 0.0, 0.0, 10.0, 10.0, 1.0]},
        {"object": "Shell", "subelement": ""},
        {"object": "Shell", "query": [{"role": "face"}], "subelement": "Face1"},
    ):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema(
                {"document": "Doc", "a": removed, "mode": "faces"},
                definition["inputSchema"],
            )


def test_measure_faces_rejects_edge_target():
    shell = _two_face_object()
    ctx = FakeCtx({"Shell": shell})
    edge = FakeEdge(length=1.0, bounds=(0.0, 0.0, 0.0, 1.0, 0.0, 0.0))
    shell.Shape.Edges = [edge]
    doc = ctx.require_document("Doc")
    reference = geometry.make_reference(ctx, doc, shell, "edge", 1)
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {"document": "Doc", "a": reference, "mode": "faces"},
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED


# ---------------------------------------------------------------------------
# measure: section.
# ---------------------------------------------------------------------------


def test_measure_section_requires_plane():
    box = FakeObject("Box", FakeShape())
    ctx = FakeCtx({"Box": box})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx, {"document": "Doc", "a": {"object": "Box"}, "mode": "section"}
        )
    assert "'plane'" in excinfo.value.message
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "a": {"object": "Box"},
                "mode": "section",
                "b": {"object": "Box"},
                "plane": {"z": 5.0},
            },
        )
    assert "takes only 'a' and 'plane'" in excinfo.value.message


def test_measure_section_rejects_zero_normal():
    ctx = FakeCtx({"Box": FakeObject("Box", FakeShape())})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "a": {"object": "Box"},
                "mode": "section",
                "plane": {"normal": [0.0, 0.0, 0.0], "point": [0.0, 0.0, 0.0]},
            },
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED


def test_measure_section_summarizes_curves(part_stub, freecad_stub):
    line = FakeEdge(
        length=10.0,
        bounds=(0.0, 0.0, 5.0, 10.0, 10.0, 5.0),
        curve=part_stub.Line(),
        points=[(0.0, 0.0, 5.0), (10.0, 0.0, 5.0)],
    )
    circle_curve = part_stub.Circle(
        radius=1.0, center=FakeVector(5, 5, 5), axis=FakeVector(0, 0, 1)
    )
    circle = FakeEdge(
        length=2.0 * math.pi,
        bounds=(4.0, 4.0, 5.0, 6.0, 6.0, 5.0),
        curve=circle_curve,
        closed=False,
        points=[(4.0, 5.0, 5.0), (6.0, 5.0, 5.0)],
    )
    compound = FakeShape(volume=0.0, solids=0)
    compound.Edges = [line, circle]
    compound.Wires = [SimpleNamespace()]
    box = FakeObject("Box", FakeShape(bounds=(0.0, 0.0, 0.0, 10.0, 10.0, 10.0)))
    box.Shape._section = compound
    ctx = FakeCtx({"Box": box})
    result = geometry.HANDLERS["measure"](
        ctx,
        {"document": "Doc", "a": {"object": "Box"}, "mode": "section", "plane": {"z": 5.0}},
    )
    assert result["plane"] == {
        "normal": [0.0, 0.0, 1.0],
        "point": [0.0, 0.0, 5.0],
    }
    assert result["edge_count"] == 2
    assert result["wire_count"] == 1
    assert result["truncated"] is False
    line_summary, circle_summary = result["curves"]
    assert line_summary["kind"] == "line"
    assert line_summary["start"] == [0.0, 0.0, 5.0]
    assert line_summary["end"] == [10.0, 0.0, 5.0]
    assert circle_summary["kind"] == "circle"
    assert circle_summary["radius"] == 1.0
    assert circle_summary["closed"] is False
    assert result["total_length"] == pytest.approx(10.0 + 2.0 * math.pi)
    assert result["a"] == {"object": "Box"}
    _assert_output_schema(result, "measure")


def test_measure_section_normal_and_point_plane(part_stub, freecad_stub):
    compound = FakeShape(volume=0.0, solids=0)
    compound.Edges = []
    compound.Wires = []
    box = FakeObject("Box", FakeShape(bounds=(0.0, 0.0, 0.0, 10.0, 10.0, 10.0)))
    box.Shape._section = compound
    ctx = FakeCtx({"Box": box})
    result = geometry.HANDLERS["measure"](
        ctx,
        {
            "document": "Doc",
            "a": {"object": "Box"},
            "mode": "section",
            "plane": {"normal": [0.0, 2.0, 0.0], "point": [5.0, 5.0, 5.0]},
        },
    )
    assert result["plane"]["normal"] == pytest.approx([0.0, 1.0, 0.0])
    assert result["edge_count"] == 0
    assert result["total_length"] == 0.0


def test_section_total_length_sums_all_edges_beyond_curve_cap(part_stub, freecad_stub):
    compound = FakeShape(volume=0.0, solids=0)
    compound.Edges = [
        FakeEdge(
            length=1.0,
            bounds=(0.0, 0.0, 5.0, 1.0, 0.0, 5.0),
            points=[(0.0, 0.0, 5.0), (1.0, 0.0, 5.0)],
        )
        for _ in range(40)
    ]
    compound.Wires = [SimpleNamespace()]
    box = FakeObject("Box", FakeShape(bounds=(0.0, 0.0, 0.0, 10.0, 10.0, 10.0)))
    box.Shape._section = compound
    ctx = FakeCtx({"Box": box})
    result = geometry.HANDLERS["measure"](
        ctx,
        {"document": "Doc", "a": {"object": "Box"}, "mode": "section", "plane": {"z": 5.0}},
    )
    assert result["total_length"] == pytest.approx(40.0)
    assert (
        len(result["curves"])
        == _definition("measure")["outputSchema"]["properties"]["curves"]["maxItems"]
    )
    assert result["edge_count"] == 40
    assert result["truncated"] is True
    _assert_output_schema(result, "measure")


# ---------------------------------------------------------------------------
# measure: faces.
# ---------------------------------------------------------------------------


def test_measure_faces_lists_areas_normals_and_references():
    shell = _two_face_object()
    ctx = FakeCtx({"Shell": shell})
    result = geometry.HANDLERS["measure"](
        ctx, {"document": "Doc", "a": {"object": "Shell"}, "mode": "faces"}
    )
    assert result["mode"] == "faces"
    assert result["truncated"] is False
    assert result["face_count"] == 2
    assert len(result["faces"]) == 2
    first = result["faces"][0]
    assert first["index"] == 1
    assert first["area"] == 100.0
    assert first["normal"] == [0.0, 0.0, 1.0]
    assert first["center"] == [5.0, 5.0, 0.5]
    doc = ctx.require_document("Doc")
    for entry in result["faces"]:
        obj, native = geometry.resolve_reference(ctx, doc, entry["reference"])
        assert obj is shell
        assert native == f"Face{entry['index']}"
    _assert_output_schema(result, "measure")


def test_measure_faces_truncates_and_single_face_selection():
    faces = [
        FakeFace(
            area=float(index),
            bounds=(0.0, 0.0, float(index), 10.0, 10.0, float(index) + 1.0),
            center=(0.0, 0.0, float(index) + 0.5),
        )
        for index in range(70)
    ]
    shell = FakeObject("Shell", FakeShape(volume=0.0, solids=0, faces=faces))
    ctx = FakeCtx({"Shell": shell})
    result = geometry.HANDLERS["measure"](
        ctx, {"document": "Doc", "a": {"object": "Shell"}, "mode": "faces"}
    )
    limit = _definition("measure")["outputSchema"]["properties"]["faces"]["maxItems"]
    assert len(result["faces"]) == limit
    assert result["face_count"] == 70
    assert result["truncated"] is True
    # Face 3's center sits at z=3.5, above the other 69 faces, so a query
    # selects exactly it (the retired box selector is replaced by the
    # shared query vocabulary).
    selected = geometry.HANDLERS["measure"](
        ctx,
        {
            "document": "Doc",
            "a": {"object": "Shell", "query": [{"role": "face", "selector": ">Z"}]},
            "mode": "faces",
        },
    )
    assert len(selected["faces"]) == 1
    assert selected["face_count"] == 1
    assert selected["faces"][0]["area"] == 69.0
    assert selected["truncated"] is False


# ---------------------------------------------------------------------------
# Signed topology references.
# ---------------------------------------------------------------------------


def test_reference_round_trip_and_whole_object():
    edge = FakeEdge(length=2.0, bounds=(0.0, 0.0, 0.0, 2.0, 0.0, 0.0))
    box = FakeObject("Box", FakeShape(edges=[edge]))
    ctx = FakeCtx({"Box": box})
    doc = ctx.require_document("Doc")
    reference = geometry.make_reference(ctx, doc, box, "edge", 1)
    assert reference["object"] == "Box"
    token = reference["subelement"]
    assert token and "." in token
    assert geometry.resolve_reference(ctx, doc, reference) == (box, "Edge1")
    # A whole object is identity without a subelement; the empty-string
    # sentinel is the retired form and is refused.
    assert geometry.whole_reference(box) == {"object": "Box"}
    assert geometry.resolve_reference(ctx, doc, {"object": "Box"}) == (box, "")
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.resolve_reference(ctx, doc, {"object": "Box", "subelement": ""})
    assert excinfo.value.details["reason"] == "empty_subelement"


def test_resolve_rejects_numeric_subelement():
    box = FakeObject("Box", FakeShape())
    ctx = FakeCtx({"Box": box})
    doc = ctx.require_document("Doc")
    for selector in ("Face7", "Edge7"):
        with pytest.raises(protocol.ToolError) as excinfo:
            geometry.resolve_reference(ctx, doc, {"object": "Box", "subelement": selector})
        assert excinfo.value.code == protocol.VALIDATION_FAILED
        assert "not durable" in excinfo.value.message


def test_resolve_rejects_stale_generation():
    box = FakeObject("Box", FakeShape())
    ctx = FakeCtx({"Box": box}, generation=1)
    doc = ctx.require_document("Doc")
    reference = geometry.make_reference(ctx, doc, box, "face", 1)
    ctx.generation = 2
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.resolve_reference(ctx, doc, reference)
    assert excinfo.value.code == protocol.VALIDATION_FAILED
    assert excinfo.value.details["reason"] == "stale_generation"
    assert excinfo.value.details["expectedGeneration"] == 1
    assert excinfo.value.details["actualGeneration"] == 2
    assert excinfo.value.details["nextTool"] == "inspect_topology"


def test_resolve_rejects_tampered_token():
    box = FakeObject("Box", FakeShape())
    ctx = FakeCtx({"Box": box})
    doc = ctx.require_document("Doc")
    reference = geometry.make_reference(ctx, doc, box, "face", 1)
    body, mac = reference["subelement"].split(".")
    replacement = "A" if body[0] != "A" else "B"
    reference["subelement"] = replacement + body[1:] + "." + mac
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.resolve_reference(ctx, doc, reference)
    assert excinfo.value.code == protocol.VALIDATION_FAILED


def test_resolve_rejects_foreign_document_and_unknown_object():
    box_a = FakeObject("BoxA", FakeShape())
    box_b = FakeObject("BoxB", FakeShape())
    ctx = FakeCtx({"BoxA": box_a, "BoxB": box_b})
    doc = ctx.require_document("Doc")
    geometry.make_reference(ctx, doc, box_a, "face", 1)
    payload = {
        "document": "other-document",
        "generation": ctx.generation,
        "object": "BoxA",
        "role": "face",
        "index": 1,
    }
    forged = {"object": "BoxA", "subelement": ctx.signer.sign("topology", payload)}
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.resolve_reference(ctx, doc, forged)
    assert excinfo.value.details["reason"] == "document_mismatch"
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.resolve_reference(ctx, doc, {"object": "Ghost"})
    assert excinfo.value.code == protocol.OBJECT_NOT_FOUND


def test_resolve_rejects_out_of_range_index():
    box = FakeObject("Box", FakeShape())
    ctx = FakeCtx({"Box": box})
    doc = ctx.require_document("Doc")
    reference = geometry.make_reference(ctx, doc, box, "face", 9)
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.resolve_reference(ctx, doc, reference)
    assert excinfo.value.details["reason"] == "missing_subelement"


# ---------------------------------------------------------------------------
# inspect_topology.
# ---------------------------------------------------------------------------


def _cylinder_surface(radius=5.0):
    """A mapped analytic surface double (class name ``Cylinder``)."""

    return Cylinder(radius=radius, axis=(0.0, 0.0, 1.0))


def _circle_curve(radius=2.5):
    """A mapped analytic curve double (class name ``Circle``)."""

    return Circle(radius=radius, center=(1.0, 2.0, 3.0), axis=(0.0, 0.0, 1.0))


def _topology_doc(face_count=130, edge_count=2):
    faces = [
        FakeFace(
            area=float(index),
            bounds=(0.0, 0.0, 0.0, 10.0, 10.0, float(index)),
        )
        for index in range(1, face_count + 1)
    ]
    faces[0].Surface = _cylinder_surface()
    edges = [
        FakeEdge(
            length=4.0,
            bounds=(0.0, 0.0, 0.0, 4.0, 0.0, 0.0),
            curve=_circle_curve() if index == 1 else None,
            closed=index == 1,
            points=[(0.0, 0.0, 0.0), (4.0, 0.0, 0.0)],
        )
        for index in range(1, edge_count + 1)
    ]
    # The cylinder face carries the circular root edge, so a face->edge
    # query expands to it through native isSame correspondence.
    faces[0].Edges = [edges[0]]
    shape = FakeShape(volume=10.0, faces=faces, edges=edges)
    return FakeCtx({"Shell": FakeObject("Shell", shape)})


def test_inspect_topology_pages_every_face_once_through_signed_cursors():
    ctx = _topology_doc()
    arguments = {"document": "Doc", "target": {"object": "Shell"}}

    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        page_arguments = {**arguments, "limit": 50}
        if cursor is not None:
            page_arguments["cursor"] = cursor
        result = geometry.HANDLERS["inspect_topology"](ctx, page_arguments)
        _assert_output_schema(result, "inspect_topology")
        seen.extend(item["index"] for item in result["items"])
        pages += 1
        cursor = result["nextCursor"]
        if cursor is None:
            break

    assert seen == list(range(1, 131))
    assert len(set(seen)) == 130
    assert pages == 3


def test_topology_limit_above_max_is_clamped():
    ctx = _topology_doc()
    arguments = {"document": "Doc", "target": {"object": "Shell"}, "limit": 500}
    # The schema caps limit at 100; an out-of-range value never reaches the
    # handler, so the clamp is exercised through the schema boundary.
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_schema(arguments, _definition("inspect_topology")["inputSchema"])
    result = geometry.HANDLERS["inspect_topology"](
        ctx, {"document": "Doc", "target": {"object": "Shell"}, "limit": 100}
    )

    _assert_output_schema(result, "inspect_topology")
    assert result["count"] == 100
    assert result["total"] == 130
    assert result["nextCursor"] is not None


def test_inspect_topology_face_items_carry_descriptive_data():
    ctx = _topology_doc()
    result = geometry.HANDLERS["inspect_topology"](
        ctx, {"document": "Doc", "target": {"object": "Shell"}, "limit": 1, "detail": "full"}
    )

    assert result["total"] == 130
    assert result["count"] == 1
    assert result["object"] == "Shell"
    assert result["role"] == "face"
    item = result["items"][0]
    assert item["index"] == 1
    assert item["bounds"] == [0.0, 0.0, 0.0, 10.0, 10.0, 1.0]
    assert item["area"] == 1.0
    assert item["center"] == [0.0, 0.0, 0.0]
    assert item["normal"] == [0.0, 0.0, 1.0]
    assert item["surfaceType"] == "Cylinder"
    assert item["radius"] == 5.0
    assert item["axis"] == [0.0, 0.0, 1.0]
    reference = item["reference"]
    assert reference["object"] == "Shell"
    assert reference["subelement"]


def test_inspect_topology_edge_items_carry_descriptive_data():
    ctx = _topology_doc()
    result = geometry.HANDLERS["inspect_topology"](
        ctx,
        {
            "document": "Doc",
            "target": {"object": "Shell", "query": [{"role": "edge"}]},
            "detail": "full",
        },
    )

    assert result["role"] == "edge"
    assert result["total"] == 2
    first, second = result["items"]
    assert first["curveType"] == "Circle"
    assert first["closed"] is True
    assert first["length"] == 4.0
    assert first["radius"] == 2.5
    assert first["center"] == [1.0, 2.0, 3.0]
    assert first["axis"] == [0.0, 0.0, 1.0]
    assert second["curveType"] == "Line"
    assert second["closed"] is False
    assert second["start"] == [0.0, 0.0, 0.0]
    assert second["end"] == [4.0, 0.0, 0.0]
    assert second["radius"] is None


def test_topology_cursor_rejects_generation_change_and_mismatched_arguments():
    ctx = _topology_doc()
    arguments = {"document": "Doc", "target": {"object": "Shell"}, "limit": 50}
    first = geometry.HANDLERS["inspect_topology"](ctx, arguments)
    cursor = first["nextCursor"]
    assert cursor is not None

    ctx.generation += 1
    with pytest.raises(protocol.ToolError) as stale:
        geometry.HANDLERS["inspect_topology"](ctx, {**arguments, "cursor": cursor})
    assert stale.value.details["reason"] == "stale_cursor"
    assert stale.value.details["nextTool"] == "inspect_topology"

    ctx.generation -= 1
    with pytest.raises(protocol.ToolError) as limit_changed:
        geometry.HANDLERS["inspect_topology"](ctx, {**arguments, "limit": 10, "cursor": cursor})
    assert limit_changed.value.details["reason"] == "stale_cursor"
    assert limit_changed.value.details["nextTool"] == "inspect_topology"

    # The cursor is bound to the canonical target: changing the selector is
    # a different request and refuses as stale.
    selector_changed = {
        "document": "Doc",
        "target": {"object": "Shell", "query": [{"role": "face", "selector": "%CYLINDER"}]},
        "limit": 50,
        "cursor": cursor,
    }
    with pytest.raises(protocol.ToolError) as changed:
        geometry.HANDLERS["inspect_topology"](ctx, selector_changed)
    assert changed.value.details["reason"] == "stale_cursor"


def test_topology_cursor_rejects_malformed_indexes():
    ctx = _topology_doc()
    arguments = {"document": "Doc", "target": {"object": "Shell"}, "limit": 50}

    for bad_last in (0, -3, "7", True, 1.5):
        payload = {
            "kind": "topology-page",
            "identity": ctx.document_identity(ctx.doc),
            "generation": ctx.generation,
            "object": "Shell",
            "role": "face",
            "limit": 50,
            "queryHash": protocol.fingerprint({"object": "Shell"}),
            "last": bad_last,
        }
        cursor = ctx.signer.sign("cursor", payload)
        with pytest.raises(protocol.ToolError) as malformed:
            geometry.HANDLERS["inspect_topology"](ctx, {**arguments, "cursor": cursor})
        assert malformed.value.details["reason"] == "malformed_cursor"
        assert malformed.value.details["nextTool"] == "inspect_topology"


def test_topology_cursor_rejects_malformed_signed_token():
    ctx = _topology_doc()
    with pytest.raises(protocol.ToolError) as malformed:
        geometry.HANDLERS["inspect_topology"](
            ctx,
            {
                "document": "Doc",
                "target": {"object": "Shell"},
                "cursor": "bogus.cursor",
            },
        )

    assert malformed.value.code == protocol.VALIDATION_FAILED
    assert malformed.value.details["reason"] == "malformed_cursor"
    assert malformed.value.details["nextTool"] == "inspect_topology"


def test_inspect_topology_signed_target_returns_one_row():
    ctx = _topology_doc()
    page = geometry.HANDLERS["inspect_topology"](
        ctx, {"document": "Doc", "target": {"object": "Shell"}, "limit": 2}
    )
    reference = page["items"][1]["reference"]

    result = geometry.HANDLERS["inspect_topology"](ctx, {"document": "Doc", "target": reference})

    _assert_output_schema(result, "inspect_topology")
    assert result["total"] == 1
    assert result["count"] == 1
    assert [item["index"] for item in result["items"]] == [2]
    assert result["items"][0]["reference"] == reference
    assert result["nextCursor"] is None


def test_inspect_topology_query_returns_final_stage_set_only():
    ctx = _topology_doc()
    result = geometry.HANDLERS["inspect_topology"](
        ctx,
        {
            "document": "Doc",
            "target": {
                "object": "Shell",
                "query": [{"role": "face", "selector": "%CYLINDER"}, {"role": "edge"}],
            },
        },
    )

    _assert_output_schema(result, "inspect_topology")
    assert result["role"] == "edge"
    # One cylinder face contributes its one circular edge.
    assert result["total"] == 1
    assert result["items"][0]["curveType"] == "Circle"


def test_inspect_topology_rejects_removed_input_forms():
    """Top-level object/role/indices are removed wire inputs."""

    definition = _definition("inspect_topology")
    for removed in (
        {"document": "Doc", "object": "Shell", "role": "face"},
        {"document": "Doc", "object": "Shell", "role": "face", "indices": [1]},
    ):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema(removed, definition["inputSchema"])
    # A raw numeric label is a resolvable-looking string, so the wire schema
    # cannot reject it; the shared resolver refuses it as non-durable.
    with pytest.raises(protocol.ToolError) as numeric:
        geometry.HANDLERS["inspect_topology"](
            ctx=_topology_doc(),
            arguments={"document": "Doc", "target": {"object": "Shell", "subelement": "Face1"}},
        )
    assert numeric.value.code == protocol.VALIDATION_FAILED
    assert "not durable" in numeric.value.message


def test_inspect_topology_compact_rows_carry_only_the_compact_keys():
    ctx = _topology_doc()

    faces = geometry.HANDLERS["inspect_topology"](
        ctx, {"document": "Doc", "target": {"object": "Shell"}, "limit": 1}
    )
    assert set(faces["items"][0]) == {"index", "reference", "bounds", "surfaceType"}

    edges = geometry.HANDLERS["inspect_topology"](
        ctx,
        {
            "document": "Doc",
            "target": {"object": "Shell", "query": [{"role": "edge"}]},
        },
    )
    for item in edges["items"]:
        assert set(item) == {"index", "reference", "bounds", "curveType"}

    doc = ctx.require_document("Doc")
    obj, subelement = geometry.resolve_reference(ctx, doc, edges["items"][0]["reference"])
    assert obj.Name == "Shell"
    assert subelement == "Edge1"


def test_inspect_topology_unknown_object_is_object_not_found():
    ctx = _topology_doc()
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["inspect_topology"](
            ctx, {"document": "Doc", "target": {"object": "Ghost"}}
        )
    assert excinfo.value.code == protocol.OBJECT_NOT_FOUND


def test_measure_accepts_signed_topology_reference_selectors():
    ctx = _topology_doc(face_count=3)
    page = geometry.HANDLERS["inspect_topology"](
        ctx, {"document": "Doc", "target": {"object": "Shell"}, "limit": 2}
    )
    reference = page["items"][1]["reference"]

    result = geometry.HANDLERS["measure"](
        ctx,
        {
            "document": "Doc",
            "mode": "faces",
            "a": {"object": reference["object"], "subelement": reference["subelement"]},
        },
    )

    assert [face["index"] for face in result["faces"]] == [2]
    assert result["a"] == reference


def test_measure_rejects_reference_incompatible_with_mode():
    ctx = _topology_doc()
    page = geometry.HANDLERS["inspect_topology"](
        ctx,
        {
            "document": "Doc",
            "target": {"object": "Shell", "query": [{"role": "edge"}]},
        },
    )
    edge_reference = page["items"][0]["reference"]

    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "mode": "faces",
                "a": {
                    "object": edge_reference["object"],
                    "subelement": edge_reference["subelement"],
                },
            },
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED


# ---------------------------------------------------------------------------
# Bounded reference lists (dress-up bases).
# ---------------------------------------------------------------------------


def _face_reference(ctx, doc, obj, index):
    return geometry.make_reference(ctx, doc, obj, "face", index)


def _two_face_objects():
    box = (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)
    shape = FakeShape(faces=[FakeFace(area=1.0, bounds=box) for _ in range(3)])
    base = FakeObject("Base", shape)
    other = FakeObject("Other", shape)
    ctx = FakeCtx({"Base": base, "Other": other})
    return ctx, ctx.doc, base, other


def _many_face_objects(count):
    box = (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)
    shape = FakeShape(faces=[FakeFace(area=1.0, bounds=box) for _ in range(count)])
    base = FakeObject("Base", shape)
    ctx = FakeCtx({"Base": base})
    return ctx, ctx.doc, base


def test_reference_list_accepts_same_object_faces_in_order():
    ctx, doc, base, _other = _two_face_objects()

    resolved, labels = geometry.resolve_reference_list(
        ctx,
        doc,
        {"object": "Base"},
        [_face_reference(ctx, doc, base, 2), _face_reference(ctx, doc, base, 1)],
        "face",
    )

    assert resolved is base
    assert labels == ["Face2", "Face1"]


def test_reference_list_rejects_a_foreign_object():
    ctx, doc, _base, other = _two_face_objects()

    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.resolve_reference_list(
            ctx,
            doc,
            {"object": "Base"},
            [_face_reference(ctx, doc, other, 1)],
            "face",
        )

    assert "does not" not in excinfo.value.message
    assert "but the base is" in excinfo.value.message


def test_reference_list_rejects_duplicates_and_wrong_role():
    ctx, doc, base, _other = _two_face_objects()
    duplicate = _face_reference(ctx, doc, base, 1)

    with pytest.raises(protocol.ToolError) as dup:
        geometry.resolve_reference_list(
            ctx, doc, {"object": "Base"}, [duplicate, duplicate], "face"
        )
    assert "duplicates" in dup.value.message

    with pytest.raises(protocol.ToolError) as role:
        geometry.resolve_reference_list(
            ctx,
            doc,
            {"object": "Base"},
            [_face_reference(ctx, doc, base, 1)],
            "edge",
        )
    assert "signed edge token" in role.value.message


def test_reference_list_rejects_a_whole_object_entry():
    ctx, doc, _base, _other = _two_face_objects()

    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.resolve_reference_list(
            ctx,
            doc,
            {"object": "Base"},
            [{"object": "Base"}],
            "face",
        )

    assert "signed subelement token" in excinfo.value.message


def test_reference_list_enforces_the_32_item_boundary():
    ctx, doc, base = _many_face_objects(40)

    accepted = [_face_reference(ctx, doc, base, index) for index in range(1, 33)]
    resolved, labels = geometry.resolve_reference_list(
        ctx,
        doc,
        {"object": "Base"},
        accepted,
        "face",
    )
    assert resolved is base
    assert labels == [f"Face{index}" for index in range(1, 33)]

    oversized = [_face_reference(ctx, doc, base, index) for index in range(1, 34)]
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.resolve_reference_list(
            ctx,
            doc,
            {"object": "Base"},
            oversized,
            "face",
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED
    assert "at most 32" in excinfo.value.message


# ---------------------------------------------------------------------------
# validate_geometry: declarative acceptance checks.
# ---------------------------------------------------------------------------


def _solid(name, volume=1000.0, bounds=(0.0, 0.0, 0.0, 10.0, 10.0, 10.0)):
    return FakeObject(name, FakeShape(volume=volume, bounds=bounds, solids=1))


def test_validate_requires_objects_or_checks():
    ctx = FakeCtx({"Box": _solid("Box")})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["validate_geometry"](ctx, {"document": "Doc"})
    assert excinfo.value.code == protocol.VALIDATION_FAILED
    assert "never defaults" in excinfo.value.message


def test_volume_range_check_pass_and_fail():
    ctx = FakeCtx({"Box": _solid("Box", volume=10.0)})
    within = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [{"kind": "volume_range", "object": {"object": "Box"}, "min": 5.0}],
        },
    )
    _assert_output_schema(within, "validate_geometry")
    assert within["checks"][0]["id"] == "check-1"
    assert within["checks"][0]["status"] == "pass"
    assert within["checks"][0]["measured"] == 10.0
    assert within["checksPassed"] is True
    assert within["accepted"] is True
    # The derived owner object report is present without an explicit objects.
    assert [entry["name"] for entry in within["objects"]] == ["Box"]

    above = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [{"kind": "volume_range", "object": {"object": "Box"}, "max": 5.0}],
        },
    )
    assert above["checks"][0]["status"] == "fail"
    assert above["checksPassed"] is False
    assert above["accepted"] is False


def test_volume_range_missing_bounds_refuses_before_evaluation():
    ctx = FakeCtx({"Box": _solid("Box")})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["validate_geometry"](
            ctx,
            {"document": "Doc", "checks": [{"kind": "volume_range", "object": {"object": "Box"}}]},
        )
    assert excinfo.value.details["reason"] == "empty_volume_range"

    with pytest.raises(protocol.ToolError) as inverted:
        geometry.HANDLERS["validate_geometry"](
            ctx,
            {
                "document": "Doc",
                "checks": [
                    {"kind": "volume_range", "object": {"object": "Box"}, "min": 5.0, "max": 1.0}
                ],
            },
        )
    assert inverted.value.details["reason"] == "invalid_volume_range"


def test_check_ids_default_by_position_and_duplicates_refuse():
    ctx = FakeCtx({"A": _solid("A"), "B": _solid("B")})
    result = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [
                {"kind": "volume_range", "object": {"object": "A"}, "min": 1.0},
                {"kind": "volume_range", "object": {"object": "B"}, "min": 1.0},
            ],
        },
    )
    assert [row["id"] for row in result["checks"]] == ["check-1", "check-2"]

    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["validate_geometry"](
            ctx,
            {
                "document": "Doc",
                "checks": [
                    {"kind": "volume_range", "object": {"object": "A"}, "min": 1.0, "id": "dup"},
                    {"kind": "volume_range", "object": {"object": "B"}, "min": 1.0, "id": "dup"},
                ],
            },
        )
    assert excinfo.value.details["reason"] == "duplicate_check_id"


def test_check_unknown_object_fails_the_call():
    ctx = FakeCtx({"Box": _solid("Box")})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["validate_geometry"](
            ctx,
            {
                "document": "Doc",
                "checks": [{"kind": "volume_range", "object": {"object": "Ghost"}, "min": 1.0}],
            },
        )
    assert excinfo.value.code == protocol.OBJECT_NOT_FOUND


def test_check_shapeless_object_is_indeterminate():
    shapeless = FakeObject("Sketch", None)
    ctx = FakeCtx({"Sketch": shapeless})
    result = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [{"kind": "volume_range", "object": {"object": "Sketch"}, "min": 1.0}],
        },
    )
    _assert_output_schema(result, "validate_geometry")
    row = result["checks"][0]
    assert row["status"] == "indeterminate"
    assert row["reason"] == "target_shapeless"
    assert result["checksPassed"] is False
    assert result["accepted"] is False


def test_check_non_volumetric_shell_cannot_pass():
    # A valid shell with no solids and zero volume must never satisfy a fit
    # check: zero common volume is not clearance evidence.
    shell = FakeObject("Shell", FakeShape(volume=0.0, solids=0, bounds=(0, 0, 0, 5, 5, 5)))
    ctx = FakeCtx({"Shell": shell, "Box": _solid("Box")})
    result = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [
                {
                    "kind": "interference_max",
                    "a": {"object": "Shell"},
                    "b": {"object": "Box"},
                }
            ],
        },
    )
    row = result["checks"][0]
    assert row["status"] == "indeterminate"
    assert row["reason"] == "non_volumetric_target"


def test_check_clearance_and_interference_semantics():
    a = _solid("A")
    b = _solid("B")
    ctx = FakeCtx({"A": a, "B": b})

    # Positive clearance: distance 2, no common volume.
    a.Shape._distance = (2.0, [(FakeVector(0, 0, 0), FakeVector(2, 0, 0))], None)
    a.Shape._common = FakeShape(volume=0.0, solids=0)
    passed = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [
                {"kind": "clearance_min", "a": {"object": "A"}, "b": {"object": "B"}, "min": 1.0}
            ],
        },
    )
    _assert_output_schema(passed, "validate_geometry")
    row = passed["checks"][0]
    assert row["status"] == "pass"
    assert row["distance"] == 2.0
    assert row["common_volume"] == 0.0
    assert row["max_interference"] == 0

    # Positive distance with overlapping volume is NOT clearance.
    a.Shape._common = FakeShape(volume=1.0, solids=1)
    overlapped = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [
                {"kind": "clearance_min", "a": {"object": "A"}, "b": {"object": "B"}, "min": 1.0}
            ],
        },
    )
    assert overlapped["checks"][0]["status"] == "fail"
    assert overlapped["checks"][0]["common_volume"] == 1.0

    # Touching (zero distance, zero common volume) satisfies a zero-margin
    # interference_max but never a positive clearance.
    a.Shape._distance = (0.0, [(FakeVector(0, 0, 0), FakeVector(0, 0, 0))], None)
    a.Shape._common = FakeShape(volume=0.0, solids=0)
    touching_interference = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [{"kind": "interference_max", "a": {"object": "A"}, "b": {"object": "B"}}],
        },
    )
    assert touching_interference["checks"][0]["status"] == "pass"
    assert touching_interference["checks"][0]["max"] == 0
    touching_clearance = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [
                {"kind": "clearance_min", "a": {"object": "A"}, "b": {"object": "B"}, "min": 1.0}
            ],
        },
    )
    assert touching_clearance["checks"][0]["status"] == "fail"


def test_clearance_min_rejects_a_zero_margin():
    ctx = FakeCtx({"A": _solid("A"), "B": _solid("B")})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["validate_geometry"](
            ctx,
            {
                "document": "Doc",
                "checks": [
                    {"kind": "clearance_min", "a": {"object": "A"}, "b": {"object": "B"}, "min": 0}
                ],
            },
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED
    assert "greater than zero" in excinfo.value.message

    definition = _definition("validate_geometry")
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_schema(
            {
                "document": "Doc",
                "checks": [
                    {"kind": "clearance_min", "a": {"object": "A"}, "b": {"object": "B"}, "min": 0}
                ],
            },
            definition["inputSchema"],
        )


def test_nonfinite_measurement_is_indeterminate_never_a_type_error():
    a = _solid("A")
    b = _solid("B")
    # A nonfinite distance must not explode into a TypeError from a
    # comparison, and must never count as a pass.
    a.Shape._distance = (float("nan"), None, None)
    a.Shape._common = FakeShape(volume=0.0, solids=0)
    ctx = FakeCtx({"A": a, "B": b})
    result = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [
                {"kind": "clearance_min", "a": {"object": "A"}, "b": {"object": "B"}, "min": 1.0}
            ],
        },
    )
    _assert_output_schema(result, "validate_geometry")
    row = result["checks"][0]
    assert row["status"] == "indeterminate"
    assert row["reason"] == "measurement_unavailable"
    assert result["checksPassed"] is False


def test_checks_absent_omits_check_only_fields():
    ctx = FakeCtx({"Box": _solid("Box")})
    result = geometry.HANDLERS["validate_geometry"](ctx, {"document": "Doc", "objects": ["Box"]})
    _assert_output_schema(result, "validate_geometry")
    for key in ("checks", "checksPassed", "accepted", "resolvedSelections"):
        assert key not in result


def test_query_check_targets_report_receipts():
    a = _solid("A")
    b = _solid("B")
    face = FakeFace(area=1.0, bounds=(0.0, 0.0, 0.0, 1.0, 1.0, 1.0), center=(0.0, 0.0, 0.5))
    a.Shape.Faces = [face]
    a.Shape._distance = (2.0, [(FakeVector(0, 0, 0), FakeVector(2, 0, 0))], None)
    a.Shape._common = FakeShape(volume=0.0, solids=0)
    ctx = FakeCtx({"A": a, "B": b})
    result = geometry.HANDLERS["validate_geometry"](
        ctx,
        {
            "document": "Doc",
            "checks": [
                {
                    "kind": "clearance_min",
                    "a": {"object": "A", "query": [{"role": "face", "selector": ">Z"}]},
                    "b": {"object": "B"},
                    "min": 1.0,
                }
            ],
        },
    )
    _assert_output_schema(result, "validate_geometry")
    row = result["checks"][0]
    # A single face is not a volumetric target, so the row is indeterminate;
    # the receipt still records the selection-time resolution.
    assert row["status"] == "indeterminate"
    assert row["reason"] == "non_volumetric_target"
    receipts = result["resolvedSelections"]
    assert receipts[0]["parameter"] == "checks.check-1.a"
    assert receipts[0]["count"] == 1
    assert receipts[0]["generation"] == ctx.generation


def test_query_axis_predicate_matches_a_cylindrical_face():
    """Analytic axis data is read for cylinder faces, not only cones/tori."""

    bore = FakeFace(
        area=1.0,
        bounds=(0.0, 0.0, 0.0, 1.0, 1.0, 1.0),
        center=(0.0, 0.0, 5.0),
        surface=Cylinder(radius=4.0, axis=(0.0, 0.0, 1.0)),
    )
    shell = FakeObject("Shell", FakeShape(faces=[bore]))
    ctx = FakeCtx({"Shell": shell})

    def axis_query(direction):
        return geometry.HANDLERS["inspect_topology"](
            ctx,
            {
                "document": "Doc",
                "target": {
                    "object": "Shell",
                    "query": [{"role": "face", "axis": {"direction": direction}}],
                },
            },
        )

    # Coaxial (and sign-insensitively opposite) matches; perpendicular does not.
    assert axis_query([0.0, 0.0, 1.0])["total"] == 1
    assert axis_query([0.0, 0.0, -1.0])["total"] == 1
    assert axis_query([1.0, 0.0, 0.0])["total"] == 0


def test_query_radius_predicate_matches_a_cylindrical_face():
    bore = FakeFace(
        area=1.0,
        bounds=(0.0, 0.0, 0.0, 1.0, 1.0, 1.0),
        center=(0.0, 0.0, 5.0),
        surface=Cylinder(radius=4.0, axis=(0.0, 0.0, 1.0)),
    )
    shell = FakeObject("Shell", FakeShape(faces=[bore]))
    ctx = FakeCtx({"Shell": shell})

    def radius_query(minimum, maximum):
        return geometry.HANDLERS["inspect_topology"](
            ctx,
            {
                "document": "Doc",
                "target": {
                    "object": "Shell",
                    "query": [{"role": "face", "radius": {"min": minimum, "max": maximum}}],
                },
            },
        )

    assert radius_query(3.0, 5.0)["total"] == 1
    assert radius_query(5.0, 6.0)["total"] == 0


# ---------------------------------------------------------------------------
# Subshape fingerprints (post-normalization correspondence evidence).
# ---------------------------------------------------------------------------


def _segment(length=10.0, *, curve=None, dims=(0.0, 0.0, 0.0)):
    """A straight native edge double from the origin along +X."""

    _, y, z = dims
    return FakeEdge(
        length=length,
        bounds=(0.0, y, z, length, y, z),
        curve=curve,
        points=((0.0, y, z), (length, y, z)),
        center=(length / 2.0, y, z),
    )


def test_fingerprint_is_plain_immutable_evidence():
    """The fingerprint carries no native reference and no mutable field."""

    fingerprint = geometry.subshape_fingerprint("edge", _segment())

    assert fingerprint["role"] == "edge"
    assert fingerprint["type"] == "LINE"
    assert fingerprint["closed"] is False
    assert fingerprint["bounds"] == (0.0, 0.0, 0.0, 10.0, 0.0, 0.0)
    assert fingerprint["measure"] == 10.0
    assert fingerprint["center"] == (5.0, 0.0, 0.0)
    assert fingerprint["point"] == (5.0, 0.0, 0.0)
    assert fingerprint["start"] == (0.0, 0.0, 0.0)
    assert fingerprint["end"] == (10.0, 0.0, 0.0)
    assert fingerprint["radius"] is None and fingerprint["axis"] is None
    # No native object survives into the snapshot, so a recompute that
    # replaces the shape cannot reach into it.
    assert all(
        isinstance(value, (str, bool, float, tuple, type(None))) for value in fingerprint.values()
    )


def test_fingerprints_match_regenerated_but_equivalent_edges():
    """Equal regenerated geometry matches; the tolerance window governs floats."""

    original = geometry.subshape_fingerprint("edge", _segment())

    # A brand-new double with identical geometry.
    assert geometry.fingerprints_match(original, geometry.subshape_fingerprint("edge", _segment()))
    # Inside the shared tolerance the regenerated length still matches...
    assert geometry.fingerprints_match(
        original, geometry.subshape_fingerprint("edge", _segment(10.0 + 1e-9))
    )
    # ...and outside it the fingerprint refuses.
    assert not geometry.fingerprints_match(
        original, geometry.subshape_fingerprint("edge", _segment(10.0 + 1e-3))
    )


def test_fingerprints_compare_discrete_fields_exactly():
    """Same coarse facts, different analytic type or direction: a mismatch."""

    line = geometry.subshape_fingerprint("edge", _segment())
    # Identical bounds, length, center and endpoints; only the mapped curve
    # class differs.
    circle = geometry.subshape_fingerprint(
        "edge", _segment(curve=Circle(radius=5.0, axis=(0.0, 0.0, 1.0)))
    )
    assert circle["bounds"] == line["bounds"] and circle["measure"] == line["measure"]
    assert not geometry.fingerprints_match(line, circle)
    assert not geometry.fingerprints_match(circle, line)

    # Reversed traversal (and therefore tangent) with the same bounds, length
    # and center of mass.
    reversed_edge = FakeEdge(
        length=10.0,
        bounds=(0.0, 0.0, 0.0, 10.0, 0.0, 0.0),
        points=((10.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
        center=(5.0, 0.0, 0.0),
    )
    reversed_edge._direction = (-1.0, 0.0, 0.0)
    mirrored = geometry.subshape_fingerprint("edge", reversed_edge)
    assert mirrored["bounds"] == line["bounds"] and mirrored["center"] == line["center"]
    assert not geometry.fingerprints_match(line, mirrored)


def test_fingerprint_refuses_when_required_evidence_is_unreadable():
    """An unreadable or inapplicable field is a gap, never a silent match."""

    opaque = SimpleNamespace(Length=10.0)
    fingerprint = geometry.subshape_fingerprint("edge", opaque)
    assert fingerprint["type"] is None and fingerprint["bounds"] is None
    # Two identical opaque doubles must still not match: neither can prove
    # the geometry they stand for.
    assert not geometry.fingerprints_match(
        fingerprint, geometry.subshape_fingerprint("edge", SimpleNamespace(Length=10.0))
    )
    assert not geometry.fingerprints_match(
        geometry.subshape_fingerprint("edge", _segment()), fingerprint
    )


def test_subshape_fingerprints_require_the_complete_index_set():
    """An unreadable array or an out-of-range index yields no evidence."""

    shape = FakeShape(edges=[_segment(), _segment(dims=(0.0, 5.0, 0.0))])

    fingerprints = geometry.subshape_fingerprints(shape, "edge", [1, 2])
    assert set(fingerprints) == {1, 2}
    assert not geometry.fingerprints_match(fingerprints[1], fingerprints[2])

    assert geometry.subshape_fingerprints(shape, "edge", [3]) is None
    assert geometry.subshape_fingerprints(shape, "edge", [0]) is None


def test_subshape_fingerprints_return_none_when_the_array_is_unreadable():
    class UnreadableShape:
        @property
        def Edges(self):
            raise RuntimeError("element map is gone")

    assert geometry.subshape_fingerprints(UnreadableShape(), "edge", [1]) is None
