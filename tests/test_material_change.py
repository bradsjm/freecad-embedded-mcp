"""Tests for mcp_server.tools.material_change with plain shape doubles.

The module measures through ``geometry``'s placement and difference helpers,
which work on duck-typed shapes, so these tests exercise the real before/after
path with no FreeCAD install and no native stubs.
"""

import sys
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server import protocol
from mcp_server.tools import material_change


class FakeBoundBox:
    """Shape ``BoundBox`` double."""

    def __init__(self, bounds):
        (
            self.XMin,
            self.YMin,
            self.ZMin,
            self.XMax,
            self.YMax,
            self.ZMax,
        ) = (float(value) for value in bounds)


class FakeShape:
    """``Part.Shape`` double exposing only what material measurement reads."""

    def __init__(
        self,
        *,
        volume=0.0,
        bounds=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        faces=6,
        solids=1,
        null=False,
        cut_volume=0.0,
        cut_bounds=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        cut_null=False,
        cut_error=None,
        cut_forbidden=False,
    ):
        self.Volume = volume
        self.BoundBox = FakeBoundBox(bounds)
        self.Faces = [object() for _ in range(faces)]
        self.Solids = [object() for _ in range(solids)]
        self.Placement = None
        self.cut_volume = cut_volume
        self.cut_bounds = cut_bounds
        self.cut_null = cut_null
        self.cut_error = cut_error
        self.cut_forbidden = cut_forbidden
        self._null = null

    def isNull(self):
        return self._null

    def isValid(self):
        return True

    def copy(self):
        """Return an independent double, as a native shape copy does."""
        clone = FakeShape(
            volume=self.Volume,
            bounds=_bounds(self),
            faces=len(self.Faces),
            solids=len(self.Solids),
            null=self._null,
            cut_volume=self.cut_volume,
            cut_bounds=self.cut_bounds,
            cut_null=self.cut_null,
            cut_error=self.cut_error,
            cut_forbidden=self.cut_forbidden,
        )
        clone.Placement = self.Placement
        return clone

    def cut(self, other):
        """Return the configured cut outcome, or fail when the test forbids it."""
        if self.cut_forbidden:
            raise AssertionError("cut must not run")
        if self.cut_error is not None:
            raise self.cut_error
        if self.cut_null:
            return FakeShape(null=True)
        return FakeShape(volume=self.cut_volume, bounds=self.cut_bounds)


class FakeObject:
    """Object double with a shape and one global placement."""

    def __init__(self, name, shape, *, placement=None):
        self.Name = name
        self.Shape = shape
        self.placement = placement if placement is not None else object()

    def getGlobalPlacement(self):
        return self.placement


class UnplaceableObject(FakeObject):
    """Object double whose global placement access raises."""

    def getGlobalPlacement(self):
        raise RuntimeError("placement unavailable")


class UnreadableShapeObject:
    """Object double whose ``Shape`` accessor raises."""

    Name = "Broken"

    @property
    def Shape(self):
        raise RuntimeError("shape unavailable")


class PoisonObject:
    """Object double that fails the test when any shape access happens."""

    Name = "Poison"

    @property
    def Shape(self):
        raise AssertionError("shape work must not run")


def _bounds(shape):
    box = shape.BoundBox
    return (box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax)


def _assert_rows(rows):
    """Validate rows against the shared fragment with the repository validator."""
    protocol.validate_schema(rows, material_change.MATERIAL_CHANGES_SCHEMA)


def _healthy_object(name="Pad"):
    """Return an object whose both cut directions are empty."""
    return FakeObject(name, FakeShape(cut_null=True))


def _skipped_baseline_row(obj, *, name="Pad"):
    """Capture ``obj`` as a baseline and report it against a healthy target."""
    baseline = material_change.capture_baseline(obj)
    return material_change.report_material_change(name, baseline, _healthy_object(name))


def test_edit_reports_added_and_removed_volumes_and_bounds():
    """An edit reports each directional cut in document space."""
    before_shape = FakeShape(
        volume=1000.0,
        bounds=(0.0, 0.0, 0.0, 10.0, 10.0, 10.0),
        cut_volume=100.0,
        cut_bounds=(0.0, 0.0, 0.0, 5.0, 10.0, 10.0),
    )
    baseline = material_change.capture_baseline(FakeObject("Pad", before_shape))
    after_shape = FakeShape(
        volume=1200.0,
        bounds=(0.0, 0.0, 0.0, 12.0, 10.0, 10.0),
        cut_volume=300.0,
        cut_bounds=(10.0, 0.0, 0.0, 12.0, 10.0, 10.0),
    )

    row = material_change.report_material_change("Pad", baseline, FakeObject("Pad", after_shape))

    _assert_rows([row])
    assert row == {
        "object": "Pad",
        "status": "ok",
        "reason": None,
        "added": {"volume": 300.0, "bounds": [10.0, 0.0, 0.0, 12.0, 10.0, 10.0]},
        "removed": {"volume": 100.0, "bounds": [0.0, 0.0, 0.0, 5.0, 10.0, 10.0]},
    }


def test_create_uses_an_empty_baseline():
    """A create adds the whole new shape and removes nothing."""
    # An empty baseline never inspects the before object.
    baseline = material_change.capture_baseline(PoisonObject(), empty=True)
    after = FakeObject("Box", FakeShape(volume=500.0, bounds=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)))

    row = material_change.report_material_change("Box", baseline, after)

    _assert_rows([row])
    assert row["status"] == "ok"
    assert row["reason"] is None
    assert row["added"] == {"volume": 500.0, "bounds": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]}
    assert row["removed"] == {"volume": 0.0, "bounds": None}


def test_empty_cut_results_report_zero_volume_and_null_bounds():
    """An empty boolean result is data: volume 0 with null bounds."""
    baseline = material_change.capture_baseline(FakeObject("Pad", FakeShape(cut_null=True)))

    row = material_change.report_material_change(
        "Pad", baseline, FakeObject("Pad", FakeShape(cut_null=True))
    )

    _assert_rows([row])
    assert row["status"] == "ok"
    assert row["added"] == {"volume": 0.0, "bounds": None}
    assert row["removed"] == {"volume": 0.0, "bounds": None}


def test_baseline_copy_is_independent_of_later_object_mutation():
    """The baseline keeps its own placed copy, not a live object reference."""
    placement = object()
    before_shape = FakeShape(
        volume=1000.0,
        cut_volume=100.0,
        cut_bounds=(0.0, 0.0, 0.0, 5.0, 10.0, 10.0),
    )
    before = FakeObject("Pad", before_shape, placement=placement)

    baseline = material_change.capture_baseline(before)

    # One placement assignment on a copy: no live reference, no second transform.
    assert baseline["shape"] is not before_shape
    assert baseline["shape"].Placement is placement
    # Mutating the live object and its shape afterwards must change nothing.
    before_shape.Volume = 9999.0
    before_shape.cut_volume = 55.0
    before_shape.cut_bounds = (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)
    before.Shape = FakeShape(volume=1.0)
    before.placement = object()

    row = material_change.report_material_change(
        "Pad", baseline, FakeObject("Pad", FakeShape(volume=1200.0))
    )

    _assert_rows([row])
    assert baseline["shape"].Volume == 1000.0
    assert row["status"] == "ok"
    assert row["removed"] == {"volume": 100.0, "bounds": [0.0, 0.0, 0.0, 5.0, 10.0, 10.0]}


def test_baseline_refusals_are_skipped_without_raising():
    """Every unusable before-state becomes a named skipped row."""
    rows = [
        _skipped_baseline_row(FakeObject("Faces", FakeShape(faces=257))),
        _skipped_baseline_row(FakeObject("Shapeless", None)),
        _skipped_baseline_row(FakeObject("Empty", FakeShape(solids=0))),
        _skipped_baseline_row(UnreadableShapeObject()),
    ]

    _assert_rows(rows)
    assert [(row["status"], row["reason"]) for row in rows] == [
        ("skipped", "cost_limit"),
        ("skipped", "non_volumetric_target"),
        ("skipped", "non_volumetric_target"),
        ("skipped", "snapshot_failed"),
    ]
    assert all(row["added"] is None and row["removed"] is None for row in rows)


def test_after_failures_are_reported_without_raising():
    """A failed snapshot, boolean or measurement is an unavailable row."""
    baseline = material_change.capture_baseline(FakeObject("Pad", FakeShape()))

    def report(after):
        return material_change.report_material_change("Pad", baseline, after)

    rows = [
        report(FakeObject("Pad", FakeShape(solids=0))),
        report(UnplaceableObject("Pad", FakeShape())),
        report(FakeObject("Pad", FakeShape(cut_error=RuntimeError("cut failed")))),
        report(FakeObject("Pad", FakeShape(cut_volume=float("nan")))),
    ]

    _assert_rows(rows)
    assert [(row["status"], row["reason"]) for row in rows] == [
        ("skipped", "non_volumetric_target"),
        ("unavailable", "snapshot_failed"),
        ("unavailable", "boolean_failed"),
        ("unavailable", "measurement_unavailable"),
    ]
    assert all(row["added"] is None and row["removed"] is None for row in rows)


def test_operand_and_target_caps_bound_shape_work():
    """Face and pair caps refuse work before any cut or shape access."""
    assert material_change.MAX_MATERIAL_TARGETS == 8
    assert material_change.MAX_OPERAND_FACES == 256
    baseline = material_change.capture_baseline(FakeObject("Pad", FakeShape(faces=200)))

    over = material_change.report_material_change(
        "Pad", baseline, FakeObject("Pad", FakeShape(faces=57, cut_forbidden=True))
    )
    at_cap = material_change.report_material_change(
        "Pad", baseline, FakeObject("Pad", FakeShape(faces=56, cut_null=True))
    )

    assert (over["status"], over["reason"], over["added"], over["removed"]) == (
        "skipped",
        "cost_limit",
        None,
        None,
    )
    assert at_cap["status"] == "ok"

    pairs = []
    for index in range(material_change.MAX_MATERIAL_TARGETS):
        name = f"Pad{index}"
        pairs.append(
            (
                name,
                material_change.capture_baseline(FakeObject(name, FakeShape())),
                _healthy_object(name),
            )
        )
    # The excess pair would fail on any shape access.
    pairs.append(("Extra", {}, PoisonObject()))

    rows = material_change.report_material_changes(pairs)

    _assert_rows(rows)
    assert len(rows) == material_change.MAX_MATERIAL_TARGETS + 1
    assert rows[-1] == {
        "object": "Extra",
        "status": "skipped",
        "reason": "cost_limit",
        "added": None,
        "removed": None,
    }
    assert all(row["status"] == "ok" for row in rows[:-1])
