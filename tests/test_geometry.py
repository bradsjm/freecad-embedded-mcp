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
    ):
        self.Volume = float(volume)
        self.BoundBox = FakeBoundBox(*bounds)
        self.Solids = [SimpleNamespace() for _ in range(solids)]
        self.Faces = list(faces)
        self.Edges = list(edges)
        self._check = list(check_results)
        self._tolerance = float(tolerance)
        self._distance = None
        self._common = None
        self._section = None

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
        )
        copied._distance = self._distance
        copied._common = self._common
        copied._section = self._section
        return copied

    def isNull(self):
        # probes["shape.null_attributes"]: a real shape is not null.
        return False

    def isValid(self):
        return True

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


class FakeFace:
    def __init__(self, *, area, bounds, normal=(0.0, 0.0, 1.0), center=(0.0, 0.0, 0.0)):
        self.Area = float(area)
        self.BoundBox = FakeBoundBox(*bounds)
        self.ParameterRange = (0.0, 10.0, 0.0, 10.0)
        self._normal = FakeVector(*normal)
        self._center = FakeVector(*center)
        self._distance = None

    def normalAt(self, u, v):
        return self._normal

    def valueAt(self, u, v):
        return self._center

    def distToShape(self, other):
        assert self._distance is not None, "unexpected distToShape call"
        return self._distance


class FakeEdge:
    def __init__(self, *, length, bounds, curve=None, closed=False, points=()):
        self.Length = float(length)
        self.BoundBox = FakeBoundBox(*bounds)
        self.Curve = curve
        self._closed = bool(closed)
        self.Vertexes = [SimpleNamespace(Point=FakeVector(*point)) for point in points if point]

    def isClosed(self):
        return self._closed


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
        ctx, {"document": "Doc", "a": "BoxA", "mode": "distance", "b": "BoxB"}
    )
    assert result["mode"] == "distance"
    assert result["distance"] == 25.0
    assert result["points"] == {"a": [10.0, 0.0, 0.0], "b": [35.0, 0.0, 0.0]}
    assert result["a"] == {"object": "BoxA", "subelement": ""}
    assert result["b"] == {"object": "BoxB", "subelement": ""}
    assert result["units"]["length"] == "mm"
    _assert_output_schema(result, "measure")


def test_measure_requires_b_for_distance_and_interference():
    box = FakeObject("Box", FakeShape())
    ctx = FakeCtx({"Box": box})
    for mode in ("distance", "interference"):
        with pytest.raises(protocol.ToolError) as excinfo:
            geometry.HANDLERS["measure"](ctx, {"document": "Doc", "a": "Box", "mode": mode})
        assert excinfo.value.code == protocol.VALIDATION_FAILED
        assert "'b'" in excinfo.value.message


def test_measure_interference_common_volume():
    box_a = FakeObject("BoxA", FakeShape())
    box_b = FakeObject("BoxB", FakeShape())
    box_a.Shape._common = FakeShape(volume=500.0, solids=0)
    ctx = FakeCtx({"BoxA": box_a, "BoxB": box_b})
    result = geometry.HANDLERS["measure"](
        ctx, {"document": "Doc", "a": "BoxA", "mode": "interference", "b": "BoxB"}
    )
    assert result["common_volume"] == 500.0
    assert result["overlaps"] is True
    assert result["units"]["volume"] == "mm3"
    box_a.Shape._common = FakeShape(volume=0.0, solids=0)
    result = geometry.HANDLERS["measure"](
        ctx, {"document": "Doc", "a": "BoxA", "mode": "interference", "b": "BoxB"}
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
            {"document": "Doc", "a": "BoxA", "mode": "interference", "b": "BoxB"},
        )

    assert excinfo.value.code == protocol.VALIDATION_FAILED
    assert excinfo.value.details == {
        "reason": "measurement_unavailable",
        "measurement": "common_volume",
    }


# ---------------------------------------------------------------------------
# measure: subshape selection.
# ---------------------------------------------------------------------------


def test_measure_face_selector_ambiguity_reports_candidates():
    shell = _two_face_object()
    shell.Shape.Faces[1].BoundBox = FakeBoundBox(0.0, 0.0, 0.0, 10.0, 10.0, 1.0)
    ctx = FakeCtx({"Shell": shell, "BoxB": FakeObject("BoxB", FakeShape())})
    box = [0.0, 0.0, 0.0, 10.0, 10.0, 1.0]
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "a": {"object": "Shell", "role": "face", "box": box},
                "mode": "distance",
                "b": "BoxB",
            },
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED
    candidates = excinfo.value.details["candidates"]
    assert len(candidates) == 2
    assert {candidate["subelement"] for candidate in candidates} == {"Face1", "Face2"}


def test_measure_face_selector_resolves_unique_reference():
    shell = _two_face_object()
    other = FakeObject("BoxB", FakeShape(bounds=(20.0, 0.0, 5.0, 30.0, 10.0, 15.0)))
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
            "a": {
                "object": "Shell",
                "role": "face",
                "box": [0.0, 0.0, 5.0, 10.0, 10.0, 6.0],
            },
            "mode": "distance",
            "b": "BoxB",
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


def test_measure_face_selector_no_match():
    shell = _two_face_object()
    ctx = FakeCtx({"Shell": shell, "BoxB": FakeObject("BoxB", FakeShape())})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "a": {
                    "object": "Shell",
                    "role": "edge",
                    "box": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
                },
                "mode": "distance",
                "b": "BoxB",
            },
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED
    assert "no edge" in excinfo.value.message


def test_measure_faces_rejects_edge_selector():
    shell = _two_face_object()
    ctx = FakeCtx({"Shell": shell})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "a": {
                    "object": "Shell",
                    "role": "edge",
                    "box": [0.0, 0.0, 0.0, 10.0, 10.0, 1.0],
                },
                "mode": "faces",
            },
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED


# ---------------------------------------------------------------------------
# measure: section.
# ---------------------------------------------------------------------------


def test_measure_section_requires_plane():
    box = FakeObject("Box", FakeShape())
    ctx = FakeCtx({"Box": box})
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](ctx, {"document": "Doc", "a": "Box", "mode": "section"})
    assert "'plane'" in excinfo.value.message
    with pytest.raises(protocol.ToolError) as excinfo:
        geometry.HANDLERS["measure"](
            ctx,
            {
                "document": "Doc",
                "a": "Box",
                "mode": "section",
                "b": "Box",
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
                "a": "Box",
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
        {"document": "Doc", "a": "Box", "mode": "section", "plane": {"z": 5.0}},
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
    assert result["a"] == {"object": "Box", "subelement": ""}
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
            "a": "Box",
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
        {"document": "Doc", "a": "Box", "mode": "section", "plane": {"z": 5.0}},
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
    result = geometry.HANDLERS["measure"](ctx, {"document": "Doc", "a": "Shell", "mode": "faces"})
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
        )
        for index in range(70)
    ]
    shell = FakeObject("Shell", FakeShape(volume=0.0, solids=0, faces=faces))
    ctx = FakeCtx({"Shell": shell})
    result = geometry.HANDLERS["measure"](ctx, {"document": "Doc", "a": "Shell", "mode": "faces"})
    limit = _definition("measure")["outputSchema"]["properties"]["faces"]["maxItems"]
    assert len(result["faces"]) == limit
    assert result["face_count"] == 70
    assert result["truncated"] is True
    selected = geometry.HANDLERS["measure"](
        ctx,
        {
            "document": "Doc",
            "a": {
                "object": "Shell",
                "role": "face",
                "box": [0.0, 0.0, 3.0, 10.0, 10.0, 4.0],
            },
            "mode": "faces",
        },
    )
    assert len(selected["faces"]) == 1
    assert selected["face_count"] == 1
    assert selected["faces"][0]["area"] == 3.0
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
    assert geometry.resolve_reference(ctx, doc, {"object": "Box", "subelement": ""}) == (
        box,
        "",
    )


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
        geometry.resolve_reference(ctx, doc, {"object": "Ghost", "subelement": ""})
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


class FakeCylinderSurface:
    def __init__(self):
        self.Radius = 5.0
        self.Axis = FakeVector(0.0, 0.0, 1.0)


class FakeCircleCurve:
    def __init__(self):
        self.Radius = 2.5
        self.Center = FakeVector(1.0, 2.0, 3.0)
        self.Axis = FakeVector(0.0, 0.0, 1.0)


def _topology_doc(face_count=130, edge_count=2):
    faces = [
        FakeFace(
            area=float(index),
            bounds=(0.0, 0.0, 0.0, 10.0, 10.0, float(index)),
        )
        for index in range(1, face_count + 1)
    ]
    faces[0].Surface = FakeCylinderSurface()
    edges = [
        FakeEdge(
            length=4.0,
            bounds=(0.0, 0.0, 0.0, 4.0, 0.0, 0.0),
            curve=FakeCircleCurve() if index == 1 else None,
            closed=index == 1,
            points=[(0.0, 0.0, 0.0), (4.0, 0.0, 0.0)],
        )
        for index in range(1, edge_count + 1)
    ]
    shape = FakeShape(volume=10.0, faces=faces, edges=edges)
    return FakeCtx({"Shell": FakeObject("Shell", shape)})


def test_inspect_topology_pages_every_face_once_through_signed_cursors():
    ctx = _topology_doc()
    arguments = {"document": "Doc", "object": "Shell", "role": "face"}

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
    arguments = {"document": "Doc", "object": "Shell", "role": "face", "limit": 500}
    protocol.validate_schema(arguments, _definition("inspect_topology")["inputSchema"])
    result = geometry.HANDLERS["inspect_topology"](ctx, arguments)

    _assert_output_schema(result, "inspect_topology")
    assert result["count"] <= 100
    assert result["total"] == 130
    assert result["nextCursor"] is not None


def test_inspect_topology_face_items_carry_descriptive_data():
    ctx = _topology_doc()
    result = geometry.HANDLERS["inspect_topology"](
        ctx, {"document": "Doc", "object": "Shell", "role": "face", "limit": 1}
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
    assert item["surfaceType"] == "FakeCylinderSurface"
    assert item["radius"] == 5.0
    assert item["axis"] == [0.0, 0.0, 1.0]
    reference = item["reference"]
    assert reference["object"] == "Shell"
    assert reference["subelement"]


def test_inspect_topology_edge_items_carry_descriptive_data():
    ctx = _topology_doc()
    result = geometry.HANDLERS["inspect_topology"](
        ctx, {"document": "Doc", "object": "Shell", "role": "edge"}
    )

    assert result["total"] == 2
    first, second = result["items"]
    assert first["curveType"] == "FakeCircleCurve"
    assert first["closed"] is True
    assert first["length"] == 4.0
    assert first["radius"] == 2.5
    assert first["center"] == [1.0, 2.0, 3.0]
    assert first["axis"] == [0.0, 0.0, 1.0]
    assert second["curveType"] is None
    assert second["closed"] is False
    assert second["start"] == [0.0, 0.0, 0.0]
    assert second["end"] == [4.0, 0.0, 0.0]
    assert second["radius"] is None


def test_topology_cursor_rejects_generation_change_and_mismatched_arguments():
    ctx = _topology_doc()
    arguments = {"document": "Doc", "object": "Shell", "role": "face", "limit": 50}
    first = geometry.HANDLERS["inspect_topology"](ctx, arguments)
    cursor = first["nextCursor"]
    assert cursor is not None

    ctx.generation += 1
    with pytest.raises(protocol.ToolError) as stale:
        geometry.HANDLERS["inspect_topology"](ctx, {**arguments, "cursor": cursor})
    assert stale.value.details["reason"] == "stale_cursor"
    assert stale.value.details["nextTool"] == "inspect_topology"

    ctx.generation -= 1
    with pytest.raises(protocol.ToolError) as role_changed:
        geometry.HANDLERS["inspect_topology"](ctx, {**arguments, "role": "edge", "cursor": cursor})
    assert role_changed.value.details["reason"] == "stale_cursor"
    assert role_changed.value.details["nextTool"] == "inspect_topology"

    with pytest.raises(protocol.ToolError) as limit_changed:
        geometry.HANDLERS["inspect_topology"](ctx, {**arguments, "limit": 10, "cursor": cursor})
    assert limit_changed.value.details["reason"] == "stale_cursor"
    assert limit_changed.value.details["nextTool"] == "inspect_topology"


def test_topology_cursor_rejects_malformed_indexes():
    ctx = _topology_doc()
    arguments = {"document": "Doc", "object": "Shell", "role": "face", "limit": 50}

    for bad_last in (0, -3, "7", True, 1.5):
        payload = {
            "kind": "topology-page",
            "identity": ctx.document_identity(ctx.doc),
            "generation": ctx.generation,
            "object": "Shell",
            "role": "face",
            "limit": 50,
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
                "object": "Shell",
                "role": "face",
                "cursor": "bogus.cursor",
            },
        )

    assert malformed.value.code == protocol.VALIDATION_FAILED
    assert malformed.value.details["reason"] == "malformed_cursor"
    assert malformed.value.details["nextTool"] == "inspect_topology"


def test_inspect_topology_indices_return_exact_rows_ascending():
    ctx = _topology_doc()
    result = geometry.HANDLERS["inspect_topology"](
        ctx, {"document": "Doc", "object": "Shell", "role": "face", "indices": [5, 2]}
    )

    _assert_output_schema(result, "inspect_topology")
    assert [item["index"] for item in result["items"]] == [2, 5]
    assert result["total"] == 130
    assert result["count"] == 2
    assert result["nextCursor"] is None
    for item in result["items"]:
        assert item["reference"]["object"] == "Shell"


def test_inspect_topology_rejects_out_of_range_duplicate_and_cursor_mix():
    ctx = _topology_doc()
    base = {"document": "Doc", "object": "Shell", "role": "face"}

    with pytest.raises(protocol.ToolError) as out_of_range:
        geometry.HANDLERS["inspect_topology"](ctx, {**base, "indices": [131]})
    assert out_of_range.value.details == {
        "reason": "index_out_of_range",
        "index": 131,
        "total": 130,
    }

    with pytest.raises(protocol.ToolError) as duplicate:
        geometry.HANDLERS["inspect_topology"](ctx, {**base, "indices": [3, 2, 3]})
    assert duplicate.value.details == {"reason": "duplicate_index", "index": 3}

    first = geometry.HANDLERS["inspect_topology"](ctx, {**base, "limit": 50})
    with pytest.raises(protocol.ToolError) as mixed:
        geometry.HANDLERS["inspect_topology"](
            ctx, {**base, "indices": [2], "cursor": first["nextCursor"]}
        )
    assert mixed.value.details == {"reason": "indices_with_cursor"}


def test_inspect_topology_compact_rows_carry_only_the_compact_keys():
    ctx = _topology_doc()

    faces = geometry.HANDLERS["inspect_topology"](
        ctx,
        {"document": "Doc", "object": "Shell", "role": "face", "indices": [1], "detail": "compact"},
    )
    assert set(faces["items"][0]) == {"index", "reference", "bounds", "surfaceType"}

    edges = geometry.HANDLERS["inspect_topology"](
        ctx,
        {
            "document": "Doc",
            "object": "Shell",
            "role": "edge",
            "indices": [1, 2],
            "detail": "compact",
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
            ctx, {"document": "Doc", "object": "Ghost", "role": "face"}
        )
    assert excinfo.value.code == protocol.OBJECT_NOT_FOUND


def test_measure_accepts_signed_topology_reference_selectors():
    ctx = _topology_doc(face_count=3)
    page = geometry.HANDLERS["inspect_topology"](
        ctx, {"document": "Doc", "object": "Shell", "role": "face", "limit": 2}
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
        ctx, {"document": "Doc", "object": "Shell", "role": "edge"}
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
        {"object": "Base", "subelement": ""},
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
            {"object": "Base", "subelement": ""},
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
            ctx, doc, {"object": "Base", "subelement": ""}, [duplicate, duplicate], "face"
        )
    assert "duplicates" in dup.value.message

    with pytest.raises(protocol.ToolError) as role:
        geometry.resolve_reference_list(
            ctx,
            doc,
            {"object": "Base", "subelement": ""},
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
            {"object": "Base", "subelement": ""},
            [{"object": "Base", "subelement": ""}],
            "face",
        )

    assert "signed subelement token" in excinfo.value.message


def test_reference_list_enforces_the_32_item_boundary():
    ctx, doc, base = _many_face_objects(40)

    accepted = [_face_reference(ctx, doc, base, index) for index in range(1, 33)]
    resolved, labels = geometry.resolve_reference_list(
        ctx,
        doc,
        {"object": "Base", "subelement": ""},
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
            {"object": "Base", "subelement": ""},
            oversized,
            "face",
        )
    assert excinfo.value.code == protocol.VALIDATION_FAILED
    assert "at most 32" in excinfo.value.message
