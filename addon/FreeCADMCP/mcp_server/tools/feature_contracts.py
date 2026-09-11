"""Bounded feature contracts shared by creation, editing and the mutation gate.

The semantic parameter names a designer writes are mapped here onto the
native PartDesign/Part properties they control, and the resulting feature
state is checked against fixed operational budgets. These are input budgets
that keep one operation small enough to reason about; they are not a
guarantee of native runtime or crash safety.

Nothing in this module imports FreeCAD or Qt: it only reads properties off
objects handed to it by the caller.
"""

from __future__ import annotations

import math
from typing import Any

from ..protocol import VALIDATION_FAILED, ToolError

#: Semantic parameter -> native property, per feature kind.
#: ``scalar`` values are written as numbers, ``quantity`` values as unit
#: strings, ``flag`` values as booleans and ``enum`` values through the
#: native enumeration string.
SEMANTIC_PROPERTIES: dict[str, dict[str, tuple[str, str]]] = {
    "sketch": {
        "offset": ("AttachmentOffset", "quantity"),
    },
    "datum_plane": {
        "offset": ("AttachmentOffset", "quantity"),
    },
    "pad": {
        "extent": ("Type", "enum"),
        "length": ("Length", "quantity"),
        "face": ("UpToFace", "link"),
        "symmetric": ("SideType", "side"),
        "reversed": ("Reversed", "flag"),
    },
    "pocket": {
        "extent": ("Type", "enum"),
        "length": ("Length", "quantity"),
        "face": ("UpToFace", "link"),
        "symmetric": ("SideType", "side"),
        "reversed": ("Reversed", "flag"),
    },
    "hole": {
        "diameter": ("Diameter", "quantity"),
        "depth": ("Depth", "quantity"),
        "cut": ("HoleCutType", "enum"),
        "depth_type": ("DepthType", "enum"),
        "counterbore_diameter": ("HoleCutDiameter", "quantity"),
        "counterbore_depth": ("HoleCutDepth", "quantity"),
        "countersink_diameter": ("HoleCutDiameter", "quantity"),
        "countersink_angle": ("HoleCutCountersinkAngle", "quantity"),
        "thread": ("ThreadType", "enum"),
    },
    "gear_profile": {
        "teeth": ("NumberOfTeeth", "integer"),
        "module": ("Modules", "quantity"),
        "pressure_angle": ("PressureAngle", "quantity"),
    },
    "revolve": {
        "angle": ("Angle", "quantity"),
        "reversed": ("Reversed", "flag"),
    },
    "groove": {
        "angle": ("Angle", "quantity"),
        "reversed": ("Reversed", "flag"),
    },
    "fillet": {"radius": ("Radius", "quantity")},
    "chamfer": {"size": ("Size", "quantity")},
    "thickness": {
        "thickness": ("Value", "quantity"),
        "inward": ("Reversed", "inward"),
    },
    "draft": {
        "angle": ("Angle", "quantity"),
        "reversed": ("Reversed", "flag"),
    },
    "linear_pattern": {
        "count": ("Occurrences", "integer"),
        "length": ("Length", "quantity"),
    },
    "polar_pattern": {
        "count": ("Occurrences", "integer"),
        "angle": ("Angle", "quantity"),
    },
    "loft": {"ruled": ("Ruled", "flag")},
    "helix": {
        "helix_mode": ("Mode", "enum"),
        "pitch": ("Pitch", "quantity"),
        "height": ("Height", "quantity"),
        "turns": ("Turns", "quantity"),
        "angle": ("Angle", "quantity"),
        "growth": ("Growth", "quantity"),
        "left_handed": ("LeftHanded", "flag"),
        "reversed": ("Reversed", "flag"),
    },
    "primitive": {
        "length": ("Length", "quantity"),
        "width": ("Width", "quantity"),
        "height": ("Height", "quantity"),
        "radius": ("Radius", "quantity"),
        "radius1": ("Radius1", "quantity"),
        "radius2": ("Radius2", "quantity"),
        "radius3": ("Radius3", "quantity"),
        "polygon": ("Polygon", "integer"),
        "circumradius": ("Circumradius", "quantity"),
        "x_min": ("Xmin", "quantity"),
        "x_max": ("Xmax", "quantity"),
        "y_min": ("Ymin", "quantity"),
        "y_max": ("Ymax", "quantity"),
        "z_min": ("Zmin", "quantity"),
        "z_max": ("Zmax", "quantity"),
        "x2_min": ("X2min", "quantity"),
        "x2_max": ("X2max", "quantity"),
        "z2_min": ("Z2min", "quantity"),
        "z2_max": ("Z2max", "quantity"),
    },
    "subshape_binder": {"make_face": ("MakeFace", "flag")},
    "scaled": {"factor": ("Factor", "quantity"), "count": ("Occurrences", "integer")},
}

#: Native TypeId per semantic creation kind.
KIND_TYPES = {
    "sketch": "Sketcher::SketchObject",
    "datum_plane": "PartDesign::Plane",
    "datum_line": "PartDesign::Line",
    "pad": "PartDesign::Pad",
    "pocket": "PartDesign::Pocket",
    "hole": "PartDesign::Hole",
    "revolve": "PartDesign::Revolution",
    "groove": "PartDesign::Groove",
    "fillet": "PartDesign::Fillet",
    "chamfer": "PartDesign::Chamfer",
    "thickness": "PartDesign::Thickness",
    "draft": "PartDesign::Draft",
    "linear_pattern": "PartDesign::LinearPattern",
    "polar_pattern": "PartDesign::PolarPattern",
    "mirrored": "PartDesign::Mirrored",
    "gear_profile": "Part::Part2DObjectPython",
    "subshape_binder": "PartDesign::SubShapeBinder",
    "multi_transform": "PartDesign::MultiTransform",
    "scaled": "PartDesign::Scaled",
    "datum_point": "PartDesign::Point",
}

#: Loft and pipe choose their native TypeId from the requested mode.
MODE_KIND_TYPES = {
    "loft": {
        "additive": "PartDesign::AdditiveLoft",
        "subtractive": "PartDesign::SubtractiveLoft",
    },
    "pipe": {
        "additive": "PartDesign::AdditivePipe",
        "subtractive": "PartDesign::SubtractivePipe",
    },
    "helix": {
        "additive": "PartDesign::AdditiveHelix",
        "subtractive": "PartDesign::SubtractiveHelix",
    },
}

#: The eight primitive shapes; each is created additive or subtractive, so
#: the TypeId is looked up by the (shape, mode) pair. The legacy unqualified
#: aliases (``PartDesign::Box`` and friends) are deliberately absent: only
#: the recorded Additive/Subtractive types are accepted.
PRIMITIVE_SHAPES = (
    "box",
    "cylinder",
    "cone",
    "sphere",
    "prism",
    "torus",
    "ellipsoid",
    "wedge",
)
PRIMITIVE_TYPES = {
    ("box", "additive"): "PartDesign::AdditiveBox",
    ("box", "subtractive"): "PartDesign::SubtractiveBox",
    ("cylinder", "additive"): "PartDesign::AdditiveCylinder",
    ("cylinder", "subtractive"): "PartDesign::SubtractiveCylinder",
    ("cone", "additive"): "PartDesign::AdditiveCone",
    ("cone", "subtractive"): "PartDesign::SubtractiveCone",
    ("sphere", "additive"): "PartDesign::AdditiveSphere",
    ("sphere", "subtractive"): "PartDesign::SubtractiveSphere",
    ("prism", "additive"): "PartDesign::AdditivePrism",
    ("prism", "subtractive"): "PartDesign::SubtractivePrism",
    ("torus", "additive"): "PartDesign::AdditiveTorus",
    ("torus", "subtractive"): "PartDesign::SubtractiveTorus",
    ("ellipsoid", "additive"): "PartDesign::AdditiveEllipsoid",
    ("ellipsoid", "subtractive"): "PartDesign::SubtractiveEllipsoid",
    ("wedge", "additive"): "PartDesign::AdditiveWedge",
    ("wedge", "subtractive"): "PartDesign::SubtractiveWedge",
}

#: Kinds whose creation attaches through a base/subelement link list.
BASE_KINDS = {"fillet": "edge", "chamfer": "edge", "thickness": "face", "draft": "face"}

#: Reference parameters per kind: semantic name -> (native property, form).
#: Forms: ``axis`` a whole-object origin axis or a sketch axis literal,
#: ``plane`` a whole-object plane or a signed planar face, ``line`` a
#: whole-object datum line, ``base_list`` a bounded signed subelement list,
#: ``originals`` same-Body source features, ``sections`` ordered profiles,
#: ``spine`` one whole-object sketch.
REFERENCE_PROPERTIES = {
    "revolve": {"axis": ("ReferenceAxis", "axis")},
    "groove": {"axis": ("ReferenceAxis", "axis")},
    "draft": {
        "neutral_plane": ("NeutralPlane", "plane"),
        "pull_direction": ("PullDirection", "line"),
    },
    "linear_pattern": {
        "axis": ("Direction", "axis"),
        "originals": ("Originals", "originals"),
    },
    "polar_pattern": {
        "axis": ("Axis", "axis"),
        "originals": ("Originals", "originals"),
    },
    "mirrored": {
        "plane": ("MirrorPlane", "mirror_plane"),
        "originals": ("Originals", "originals"),
    },
    "multi_transform": {"originals": ("Originals", "originals")},
    "scaled": {"originals": ("Originals", "originals")},
    "loft": {"sections": ("Sections", "sections")},
    "pipe": {"spine": ("Spine", "spine")},
    "datum_line": {"axis": ("AttachmentSupport", "origin_axis")},
    "helix": {"axis": ("ReferenceAxis", "axis")},
    "subshape_binder": {"references": ("Support", "binder_support")},
}

#: Origin axis names per datum-line role.
ORIGIN_AXIS_NAMES = {"x": "X_Axis", "y": "Y_Axis", "z": "Z_Axis"}

#: Upper bounds for the semantic reference lists.
MAX_PATTERN_ORIGINALS_SEMANTIC = 8
MAX_LOFT_SECTIONS = 7
MAX_BINDER_REFERENCES = 16
MAX_TRANSFORM_STEPS = 4
MAX_HELIX_TURNS = 100.0
MIN_PRISM_POLYGON = 3
MAX_PRISM_POLYGON = 100

#: Upper bounds per kind for the bounded reference lists.
MAX_BASE_FACES = 512
MAX_BASE_EDGES = 1024
MAX_SUBELEMENTS = 32

#: Native enumeration values per kind and semantic parameter name, as
#: recorded by the installed PartDesign tests and the live 1.1.3
#: enumerations: pad/pocket ``Type`` positions are assigned as integers by
#: the native tests (``Pocket001.Type = 1`` for through-all, ``= 3`` for
#: up-to-face, default ``0`` for a distance extent), while hole and helix
#: strings come from the recorded live enumerations (the helix ``Mode``
#: positions 0-3 are the recorded ``pitch-height-angle`` ... order).
ENUM_NAMES: dict[str, dict[str, dict[str, int | str]]] = {
    "pad": {"extent": {"distance": 0, "up_to_face": 3}},
    "pocket": {"extent": {"distance": 0, "through_all": 1, "up_to_face": 3}},
    "hole": {
        "cut": {
            "none": "None",
            "counterbore": "Counterbore",
            "countersink": "Countersink",
            "counterdrill": "Counterdrill",
        },
        "depth_type": {"dimension": "Dimension", "through_all": "ThroughAll"},
        "thread": {
            "none": "None",
            "iso_metric": "ISOMetricProfile",
            "iso_metric_fine": "ISOMetricFineProfile",
            "unc": "UNC",
            "unf": "UNF",
            "unef": "UNEF",
            "npt": "NPT",
            "bsp": "BSP",
            "bsw": "BSW",
            "bsf": "BSF",
        },
    },
    "helix": {
        "helix_mode": {
            "pitch_height": 0,
            "pitch_turns": 1,
            "height_turns": 2,
            "height_growth": 3,
        }
    },
}

#: Exact recorded ``SideType`` enumeration strings. The native 1.1.3 tests
#: record ``1`` and ``"One side"`` for the default one-sided pad, so the
#: symmetric and two-sided forms are matched by exact string against the
#: live enumeration; an integer position is never guessed.
SIDE_TYPE_SYMMETRIC = "Symmetric"
SIDE_TYPE_TWO_SIDES = "Two sides"
SIDE_TYPE_ONE_SIDE = "One side"

#: Bounded budgets for one operation.
MAX_SKETCH_OPERATIONS = 64
MAX_SKETCH_ROWS = 4096
MAX_PATTERN_OCCURRENCES = 32
MAX_PATTERN_ORIGINALS = 8
MAX_GEAR_TEETH = 80
MIN_GEAR_TEETH = 8
MAX_GEAR_PITCH_DIAMETER_MM = 200.0
MIN_GEAR_MODULE_MM = 0.1
MAX_GEAR_MODULE_MM = 10.0

#: Feature TypeIds whose native algorithms are expensive enough to warrant a
#: recovery checkpoint before the mutation runs. Ordinary pads, pockets and
#: holes are deliberately absent: the approved policy checkpoints dress-ups,
#: patterns, lofts and pipes only, never every operation.
EXPENSIVE_TYPES = frozenset(
    {
        "PartDesign::Fillet",
        "PartDesign::Chamfer",
        "PartDesign::Thickness",
        "PartDesign::Draft",
        "PartDesign::LinearPattern",
        "PartDesign::PolarPattern",
        "PartDesign::Mirrored",
        "PartDesign::AdditiveLoft",
        "PartDesign::SubtractiveLoft",
        "PartDesign::AdditivePipe",
        "PartDesign::SubtractivePipe",
        "PartDesign::AdditiveHelix",
        "PartDesign::SubtractiveHelix",
        "PartDesign::MultiTransform",
        "PartDesign::Scaled",
    }
)

#: Semantic creation kinds whose native algorithm is expensive.
EXPENSIVE_KINDS = frozenset(
    {
        "fillet",
        "chamfer",
        "thickness",
        "draft",
        "linear_pattern",
        "polar_pattern",
        "mirrored",
        "loft",
        "pipe",
        "helix",
        "multi_transform",
        "scaled",
    }
)

_MAX_DEPENDENT_SCAN = 256


def expensive_feature_present(targets: list[Any]) -> bool:
    """True when a target or its bounded dependent closure is expensive.

    Reuses the mutation gate's 256-object traversal bound. A closure that
    exceeds the bound is refused explicitly with ``too_many_dependents``
    instead of being silently truncated, because a truncated scan could miss
    an expensive feature and skip its recovery checkpoint.
    """

    seen: set[str] = set()
    queue = list(targets)
    scanned = 0
    expensive = False
    while queue and scanned < _MAX_DEPENDENT_SCAN:
        obj = queue.pop(0)
        scanned += 1
        if str(getattr(obj, "TypeId", "")) in EXPENSIVE_TYPES:
            expensive = True
        name = str(getattr(obj, "Name", ""))
        if name:
            seen.add(name)
        try:
            links = list(getattr(obj, "InList", ()) or ())
        except Exception:
            continue
        for link in links:
            link_name = str(getattr(link, "Name", ""))
            if link_name and link_name not in seen:
                queue.append(link)
    if queue:
        raise _fail(
            f"the dependent closure exceeds the {_MAX_DEPENDENT_SCAN}-object "
            "traversal bound; refusing instead of skipping recovery",
            {"reason": "too_many_dependents"},
        )
    return expensive


def _fail(message: str, details: dict | None = None) -> ToolError:
    return ToolError(VALIDATION_FAILED, message, details)


def finite_number(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when unusable."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def gear_pitch_diameter(teeth: Any, module: Any) -> float | None:
    """Return ``teeth * module`` in mm when both are usable numbers."""

    count = finite_number(teeth)
    module_mm = finite_number(module)
    if count is None or module_mm is None:
        return None
    return count * module_mm


def check_gear_bounds(teeth: Any, module: Any) -> None:
    """Refuse an unbounded involute gear definition before it is applied."""

    if isinstance(teeth, bool) or not isinstance(teeth, int):
        raise _fail("gear teeth must be an integer")
    if not MIN_GEAR_TEETH <= teeth <= MAX_GEAR_TEETH:
        raise _fail(f"gear teeth must be between {MIN_GEAR_TEETH} and {MAX_GEAR_TEETH}")
    module_mm = finite_number(module)
    if module_mm is None or not MIN_GEAR_MODULE_MM <= module_mm <= MAX_GEAR_MODULE_MM:
        raise _fail(f"gear module must be between {MIN_GEAR_MODULE_MM} and {MAX_GEAR_MODULE_MM} mm")
    pitch = teeth * module_mm
    if pitch > MAX_GEAR_PITCH_DIAMETER_MM:
        raise _fail(
            "gear pitch diameter (teeth * module) exceeds the "
            f"{MAX_GEAR_PITCH_DIAMETER_MM:.0f} mm bound",
            {"reason": "pitch_diameter", "pitchDiameter": pitch},
        )


def native_value(kind: str, name: str, value: Any) -> tuple[str, Any] | None:
    """Map one semantic parameter onto ``(native property, native value)``."""

    spec = SEMANTIC_PROPERTIES.get(kind, {}).get(name)
    if spec is None:
        return None
    prop, form = spec
    if form == "flag":
        return prop, bool(value)
    if form == "integer":
        return prop, int(value)
    if form == "quantity":
        number = finite_number(value)
        if number is None:
            return None
        return prop, number
    if form == "enum":
        names = ENUM_NAMES.get(kind, {}).get(name, {})
        if value not in names:
            raise _fail(f"{kind} {name} {value!r} has no native mapping")
        return prop, names[value]
    if form == "side":
        # Resolved against the live enumeration by the caller, which owns
        # the feature object; a boolean here means "not one-sided".
        return prop, bool(value)
    if form == "inward":
        # ``Reversed`` carries the semantic inward flag directly.
        return prop, bool(value)
    # A link value is resolved by the caller, which owns the document.
    return prop, value


def check_workload(ctx: Any, targets: list[Any]) -> None:
    """Bound the native work one committed mutation may request.

    Called by the mutation gate after the body assigned every property and
    before the first native recompute, so a lower-level edit (including a
    generic ``edit_object``) cannot bypass the counts the semantic handlers
    also prevalidate.
    """

    for obj in targets:
        type_id = str(getattr(obj, "TypeId", ""))
        if type_id == "Part::Part2DObjectPython":
            _check_gear(obj)
        elif type_id in (
            "PartDesign::LinearPattern",
            "PartDesign::PolarPattern",
            "PartDesign::MultiTransform",
            "PartDesign::Scaled",
        ):
            _check_pattern(obj)
        elif type_id == "PartDesign::Mirrored":
            _check_originals(obj, MAX_PATTERN_ORIGINALS)
        elif type_id == "Sketcher::SketchObject":
            _check_sketch(obj)


def _check_gear(obj: Any) -> None:
    if str(getattr(getattr(obj, "Proxy", None), "Type", "")) != "InvoluteGear":
        return
    if getattr(obj, "ExternalGear", None) is None:
        raise _fail(f"gear '{getattr(obj, 'Name', '')}' exposes no ExternalGear flag")
    if getattr(obj, "HighPrecision", None) is None:
        raise _fail(f"gear '{getattr(obj, 'Name', '')}' exposes no HighPrecision flag")
    if getattr(obj, "HighPrecision", None) is True:
        raise _fail(f"gear '{getattr(obj, 'Name', '')}' enables the unexposed high-precision mode")
    if getattr(obj, "ExternalGear", None) is False:
        raise _fail(
            f"gear '{getattr(obj, 'Name', '')}' is internal; this contract "
            "creates external gear profiles only"
        )
    teeth = getattr(obj, "NumberOfTeeth", None)
    module = quantity_mm(getattr(obj, "Modules", None))
    if module is None:
        raise _fail(f"gear '{getattr(obj, 'Name', '')}' exposes no module value")
    check_gear_bounds(teeth, module)


def _check_pattern(obj: Any) -> None:
    occurrences = getattr(obj, "Occurrences", None)
    if isinstance(occurrences, bool) or not isinstance(occurrences, int):
        return
    if occurrences < 2:
        raise _fail(f"pattern '{getattr(obj, 'Name', '')}' requires at least 2 occurrences")
    if occurrences > MAX_PATTERN_OCCURRENCES:
        raise _fail(
            f"pattern '{getattr(obj, 'Name', '')}' requests {occurrences} "
            f"occurrences; the bound is {MAX_PATTERN_OCCURRENCES}"
        )
    originals = _original_count(obj)
    if originals > MAX_PATTERN_ORIGINALS:
        raise _fail(
            f"feature '{getattr(obj, 'Name', '')}' references {originals} "
            f"originals; the bound is {MAX_PATTERN_ORIGINALS}"
        )
    if occurrences * originals > MAX_PATTERN_OCCURRENCES * 2:
        raise _fail(
            f"pattern '{getattr(obj, 'Name', '')}' requests "
            f"{occurrences} occurrences over {originals} originals; the "
            f"combined bound is {MAX_PATTERN_OCCURRENCES * 2}"
        )


def _check_originals(obj: Any, limit: int) -> None:
    count = _original_count(obj)
    if count > limit:
        raise _fail(
            f"feature '{getattr(obj, 'Name', '')}' references {count} "
            f"originals; the bound is {limit}"
        )


def _original_count(obj: Any) -> int:
    originals = getattr(obj, "Originals", None)
    if originals is None:
        return 0
    try:
        return len(list(originals))
    except Exception as exc:
        raise _fail(
            f"feature '{getattr(obj, 'Name', '')}' has an unreadable "
            f"Originals list: {type(exc).__name__}: {exc}"
        ) from exc


def _check_sketch(obj: Any) -> None:
    for attribute, label in (("Geometry", "geometry"), ("Constraints", "constraint")):
        try:
            count = len(list(getattr(obj, attribute, ()) or ()))
        except Exception as exc:
            raise _fail(
                f"sketch '{getattr(obj, 'Name', '')}' has unreadable "
                f"{label} rows: {type(exc).__name__}: {exc}"
            ) from exc
        if count > MAX_SKETCH_ROWS:
            raise _fail(
                f"sketch '{getattr(obj, 'Name', '')}' holds {count} "
                f"{label} rows; the bound is {MAX_SKETCH_ROWS}"
            )


def quantity_mm(value: Any) -> float | None:
    """Read a native quantity or plain number in millimetres."""

    if value is None:
        return None
    try:
        raw = getattr(value, "Value", value)
    except Exception as exc:
        raise _fail(f"quantity value is unusable: {type(exc).__name__}: {exc}") from exc
    return finite_number(raw)
