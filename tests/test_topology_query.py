"""Tests for the shared topology query grammar and evaluator.

Covers parser acceptance and refusals, exact set semantics, directional
operators, clustering and nth indices, named views, the record-driven
evaluator and the schema fragments — all native-independent.
"""

from __future__ import annotations

import sys
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server import topology_query as tq
from mcp_server.protocol import ProtocolError, ToolError, check_schema, validate_schema


def _plane(index: int, z: int | float, direction_z: float = 1.0) -> dict:
    return {
        "index": index,
        "role": "face",
        "type": "PLANE",
        "typeStatus": "ok",
        "center": (0.0, 0.0, float(z)),
        "direction": (0.0, 0.0, direction_z),
    }


def _cylinder_face(index: int, z: float) -> dict:
    return {
        "index": index,
        "role": "face",
        "type": "CYLINDER",
        "typeStatus": "ok",
        "center": (0.0, 0.0, z),
        "direction": None,
        "axis": (0.0, 0.0, 1.0),
        "radius": 5.0,
    }


def _line(
    index: int, center: tuple[float, float, float], direction: tuple[float, float, float]
) -> dict:
    return {
        "index": index,
        "role": "edge",
        "type": "LINE",
        "typeStatus": "ok",
        "center": center,
        "direction": direction,
    }


# ---------------------------------------------------------------------------
# Schema fragments.
# ---------------------------------------------------------------------------


def test_query_defs_merge_and_validate() -> None:
    schema = tq.merge_query_defs(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"$ref": "#/$defs/topologyQuery"},
                "target": {"$ref": "#/$defs/topologyTarget"},
            },
        }
    )
    check_schema(schema)
    validate_schema({"target": {"object": "Pad"}}, schema)
    validate_schema(
        {
            "target": {
                "object": "Pad",
                "query": [{"role": "edge", "selector": "%CIRCLE"}],
                "expected_generation": 3,
            }
        },
        schema,
    )


def test_closed_union_rejects_retired_forms() -> None:
    schema = tq.merge_query_defs(
        {"type": "object", "properties": {"t": {"$ref": "#/$defs/topologyTarget"}}}
    )
    for rejected in (
        "Box",  # bare string
        {"object": "Box", "role": "face", "box": [0, 0, 0, 1, 1, 1]},  # exact box
        {"object": "Box", "subelement": ""},  # empty sentinel
        {"object": "Box", "query": [], "subelement": "Face1"},  # mixture
    ):
        try:
            validate_schema({"t": rejected}, schema)
        except ProtocolError:
            continue
        raise AssertionError(f"retired form accepted: {rejected!r}")


# ---------------------------------------------------------------------------
# Parser.
# ---------------------------------------------------------------------------


def test_parse_rejects_malformed_and_hostile_syntax() -> None:
    cases = [
        "",
        "   ",
        "lambda x: x",
        "o.fn(3)",
        "%NOSUCHTYPE",
        ">Z[+1]",
        ">Z[99999999999]",
        "(0,0)",  # two-component vector
        "(0,0,0)",  # zero vector
        "1.5e3",  # exponent notation
        ">Z]5[",
        "and >Z",
        ">Z and",
        "(>Z",
        ">Z)(",  # trailing junk
        "> Z [ -1 ] extra",
    ]
    for text in cases:
        try:
            tq.parse_selector(text)
        except ToolError as exc:
            assert exc.details.get("reason") in ("selector_syntax", "selector_limit")
            assert exc.details.get("nextTool") == "inspect_topology"
            if exc.details.get("reason") == "selector_syntax":
                assert isinstance(exc.details.get("position"), int)
            continue
        raise AssertionError(f"malformed selector accepted: {text!r}")


# ---------------------------------------------------------------------------
# Evaluator: exact set semantics.
# ---------------------------------------------------------------------------


def test_type_filter_is_case_insensitive_and_never_matches_other_roles() -> None:
    records = [_plane(1, 0), _cylinder_face(2, 1)]
    assert tq.evaluate_selector(tq.parse_selector("%plane"), records) == [1]
    assert tq.evaluate_selector(tq.parse_selector("%CYLINDER"), records) == [2]
    assert tq.evaluate_selector(tq.parse_selector("%circle"), records) == []


def test_directional_operators() -> None:
    edges = [
        _line(1, (0, 0, 0), (0.0, 0.0, 1.0)),  # +Z
        _line(2, (1, 0, 0), (0.0, 0.0, -1.0)),  # -Z
        _line(3, (2, 0, 0), (1.0, 0.0, 0.0)),  # +X
        {
            "index": 4,
            "role": "edge",
            "type": "CIRCLE",
            "typeStatus": "ok",
            "center": (0, 0, 1),
            "direction": None,
            "radius": 2.0,
            "axis": (0.0, 0.0, 1.0),
        },
    ]
    assert tq.evaluate_selector(tq.parse_selector("+Z"), edges) == [1]
    assert tq.evaluate_selector(tq.parse_selector("-Z"), edges) == [2]
    # | accepts both orientations of parallel geometry.
    assert tq.evaluate_selector(tq.parse_selector("|Z"), edges) == [1, 2]
    # # selects perpendicular geometry.
    assert tq.evaluate_selector(tq.parse_selector("#Z"), edges) == [3]
    # An index after a directional operator is a syntax error: [n] applies
    # to the extremum operators only, exactly like the source grammar.
    try:
        tq.parse_selector("|Z[0]")
    except ToolError as exc:
        assert exc.details["reason"] == "selector_syntax"
    else:
        raise AssertionError("|Z[0] accepted")
    # Curved edges are not direction-eligible and never match directional ops.
    assert tq.evaluate_selector(tq.parse_selector("|Z"), [edges[3]]) == []
    # %CIRCLE matches curved edges.
    assert tq.evaluate_selector(tq.parse_selector("%CIRCLE"), edges) == [4]


def test_bare_extremum_selects_extreme_cluster_by_center() -> None:
    # A box-like solid: four side planes with centers at z=0.5 and one top
    # plane at z=1. >Z selects the top plane by its CENTER, not side faces
    # that share its ZMax.
    records = [
        _plane(1, 0.5, 0.0),  # side, normal +X encoded via z-direction? use axis dirs below
        _plane(2, 0.5, 0.0),
        _plane(3, 0.5, 0.0),
        _plane(4, 0.5, 0.0),
        _plane(5, 1.0),
    ]
    records[0]["direction"] = (1.0, 0.0, 0.0)
    records[1]["direction"] = (-1.0, 0.0, 0.0)
    records[2]["direction"] = (0.0, 1.0, 0.0)
    records[3]["direction"] = (0.0, -1.0, 0.0)
    assert tq.evaluate_selector(tq.parse_selector(">Z"), records) == [5]
    assert tq.evaluate_selector(tq.parse_selector("<Z"), records) == [1, 2, 3, 4]


def test_single_angle_index_restricts_to_parallel_eligible() -> None:
    records = [_cylinder_face(1, 2.0), _plane(2, 0.0), _plane(3, 1.0)]
    # Bare >Z ranks every candidate: the cylinder's center wins.
    assert tq.evaluate_selector(tq.parse_selector(">Z"), records) == [1]
    # >Z[-1] restricts to |Z-compatible records: the highest eligible plane.
    assert tq.evaluate_selector(tq.parse_selector(">Z[-1]"), records) == [3]
    # >>Z[-1] ranks every candidate even with an index.
    assert tq.evaluate_selector(tq.parse_selector(">>Z[-1]"), records) == [1]
    # Center ordering: 0, 1, 2.
    ordered = [_plane(10, 0), _plane(11, 1), _plane(12, 2)]
    assert tq.evaluate_selector(tq.parse_selector(">Z[0]"), ordered) == [10]
    assert tq.evaluate_selector(tq.parse_selector(">Z[-1]"), ordered) == [12]
    assert tq.evaluate_selector(tq.parse_selector("<Z[0]"), ordered) == [12]
    assert tq.evaluate_selector(tq.parse_selector("<Z[-1]"), ordered) == [10]


def test_clusters_anchor_on_first_value() -> None:
    records = [_plane(1, 0.0), _plane(2, 0.00009), _plane(3, 0.00018)]
    # 0.00009 joins the first cluster (distance <= 0.0001 from anchor 0);
    # 0.00018 anchors a new cluster (no neighbor chaining).
    assert tq.evaluate_selector(tq.parse_selector(">Z[0]"), records) == [1, 2]
    assert tq.evaluate_selector(tq.parse_selector(">Z[1]"), records) == [3]
    assert tq.evaluate_selector(tq.parse_selector("<Z[0]"), records) == [3]
    try:
        tq.evaluate_selector(tq.parse_selector(">Z[2]"), records)
    except ToolError as exc:
        assert exc.details["reason"] == "selector_index_out_of_range"
        assert exc.details["index"] == 2
        assert exc.details["groupCount"] == 2
        assert exc.details["nextTool"] == "inspect_topology"
        return
    raise AssertionError("out-of-range cluster index accepted")


def test_index_out_of_range_is_a_named_refusal() -> None:
    records = [_plane(1, 0), _plane(2, 1)]
    for bad in (">Z[2]", ">Z[-3]", "<Z[5]"):
        try:
            tq.evaluate_selector(tq.parse_selector(bad), records)
        except ToolError as exc:
            assert exc.details["reason"] == "selector_index_out_of_range"
            continue
        raise AssertionError(f"bad index accepted: {bad}")


def test_named_views_keep_cadquery_meanings() -> None:
    records = [
        {
            "index": 1,
            "role": "face",
            "type": "PLANE",
            "typeStatus": "ok",
            "center": (5, 0, 0),
            "direction": (1.0, 0.0, 0.0),
        },
        {
            "index": 2,
            "role": "face",
            "type": "PLANE",
            "typeStatus": "ok",
            "center": (-5, 0, 0),
            "direction": (-1.0, 0.0, 0.0),
        },
        {
            "index": 3,
            "role": "face",
            "type": "PLANE",
            "typeStatus": "ok",
            "center": (0, 4, 0),
            "direction": (0.0, 1.0, 0.0),
        },
        {
            "index": 4,
            "role": "face",
            "type": "PLANE",
            "typeStatus": "ok",
            "center": (0, -4, 0),
            "direction": (0.0, -1.0, 0.0),
        },
    ]
    assert tq.evaluate_selector(tq.parse_selector("right"), records) == [1]
    assert tq.evaluate_selector(tq.parse_selector("left"), records) == [2]
    assert tq.evaluate_selector(tq.parse_selector("top"), records) == [3]
    assert tq.evaluate_selector(tq.parse_selector("bottom"), records) == [4]


def test_set_algebra_shares_one_universe() -> None:
    records = [
        _plane(1, 0, 1.0),
        _plane(2, 1, 1.0),
        {
            "index": 3,
            "role": "face",
            "type": "CYLINDER",
            "typeStatus": "ok",
            "center": (0, 0, 2),
            "direction": None,
            "axis": (0.0, 0.0, 1.0),
            "radius": 1.0,
        },
    ]
    universe = {1, 2, 3}
    # not >Z == universe minus the >Z cluster.
    assert set(tq.evaluate_selector(tq.parse_selector("not >Z"), records)) == universe - {3}
    # and intersects; or unions; exc subtracts.
    assert tq.evaluate_selector(tq.parse_selector("%PLANE and >Z"), records) == []
    assert set(tq.evaluate_selector(tq.parse_selector("%PLANE or %CYLINDER"), records)) == universe
    # `not` binds weakest: not(A exc B) is the complement of the difference.
    assert tq.evaluate_selector(tq.parse_selector("not %PLANE exc %CYLINDER"), records) == [3]
    # Each operand sees the same universe: `>Z or <Z` unions both extreme
    # clusters (the middle plane is in neither), it is not a pipeline.
    assert set(tq.evaluate_selector(tq.parse_selector(">Z or <Z"), records)) == {1, 3}


def test_empty_universe_returns_no_matches_including_bare_extrema() -> None:
    assert tq.evaluate_selector(tq.parse_selector(">Z"), []) == []
    assert tq.evaluate_selector(tq.parse_selector("|Z"), []) == []
    assert tq.evaluate_selector(tq.parse_selector("%PLANE"), []) == []


def test_output_keeps_native_order_through_set_algebra() -> None:
    records = [_plane(5, 1), _plane(2, 0), _cylinder_face(9, 2)]
    assert tq.evaluate_selector(tq.parse_selector("%PLANE or %CYLINDER"), records) == [5, 2, 9]


# ---------------------------------------------------------------------------
# Unreadable evidence refuses instead of silently dropping.
# ---------------------------------------------------------------------------


def test_unreadable_direction_on_eligible_record_refuses() -> None:
    records = [
        {
            "index": 1,
            "role": "face",
            "type": "PLANE",
            "typeStatus": "ok",
            "center": (0, 0, 0),
            "direction": None,
        },
    ]
    try:
        tq.evaluate_selector(tq.parse_selector("|Z"), records)
    except ToolError as exc:
        assert exc.details["reason"] == "selector_geometry_unavailable"
        return
    raise AssertionError("unreadable direction silently dropped the candidate")


def test_unreadable_center_refuses_extrema() -> None:
    records = [
        {
            "index": 1,
            "role": "face",
            "type": "PLANE",
            "typeStatus": "ok",
            "center": None,
            "direction": (0.0, 0.0, 1.0),
        }
    ]
    try:
        tq.evaluate_selector(tq.parse_selector(">Z"), records)
    except ToolError as exc:
        assert exc.details["reason"] == "selector_geometry_unavailable"
        return
    raise AssertionError("unreadable center silently dropped the candidate")


def test_other_type_on_unclassified_record_refuses() -> None:
    records = [
        {
            "index": 1,
            "role": "face",
            "type": None,
            "typeStatus": "unmapped",
            "center": (0, 0, 0),
            "direction": None,
        },
        {
            "index": 2,
            "role": "face",
            "type": None,
            "typeStatus": "unreadable",
            "center": (0, 0, 0),
            "direction": None,
        },
    ]
    try:
        tq.evaluate_selector(tq.parse_selector("%OTHER"), records)
    except ToolError as exc:
        assert exc.details["reason"] == "selector_geometry_unavailable"
        return
    raise AssertionError("%OTHER decided on an unclassified record")


def test_unmapped_class_is_not_a_mapped_type() -> None:
    records = [
        {
            "index": 1,
            "role": "face",
            "type": None,
            "typeStatus": "unmapped",
            "center": (0, 0, 0),
            "direction": None,
        },
    ]
    assert tq.evaluate_selector(tq.parse_selector("%PLANE"), records) == []


# ---------------------------------------------------------------------------
# Radius / analytic-axis predicates.
# ---------------------------------------------------------------------------


def test_radius_and_axis_predicates_intersect_after_expression() -> None:
    records = [
        _cylinder_face(1, 0.0),
        {**_cylinder_face(2, 1.0), "radius": 8.0},
        {
            "index": 3,
            "role": "edge",
            "type": "CIRCLE",
            "typeStatus": "ok",
            "center": (0, 0, 0),
            "direction": None,
            "radius": 5.0,
            "axis": (0.0, 0.0, 1.0),
        },
        {
            "index": 4,
            "role": "edge",
            "type": "LINE",
            "typeStatus": "ok",
            "center": (0, 0, 0),
            "direction": (1.0, 0.0, 0.0),
            "radius": None,
            "axis": None,
        },
    ]
    step = {"role": "face", "selector": None, "radius": {"min": 4.0, "max": 6.0}, "axis": None}
    assert [r["index"] for r in records if tq.radius_predicate_matches(step, r)] == [1, 3]
    axis_step = {
        "role": "edge",
        "selector": None,
        "radius": None,
        "axis": {"direction": [0.0, 0.0, 1.0], "tolerance_deg": 0.1},
    }
    assert [r["index"] for r in records if tq.axis_predicate_matches(axis_step, r)] == [1, 2, 3]
    # Sign-insensitive axis match.
    flipped = {**axis_step, "axis": {"direction": [0.0, 0.0, -1.0], "tolerance_deg": 0.1}}
    assert [r["index"] for r in records if tq.axis_predicate_matches(flipped, r)] == [1, 2, 3]
    # A record without axis data is not a match, not an error.
    assert not tq.axis_predicate_matches(axis_step, records[3])


def test_query_step_normalizer_defaults_and_refusals() -> None:
    step = tq.normalize_query_step({"role": "edge"}, "query[0]")
    assert step == {"role": "edge", "selector": None, "radius": None, "axis": None}
    defaulted = tq.normalize_query_step({"role": "edge", "axis": {"direction": [0, 0, 1]}}, "q")
    assert defaulted["axis"]["tolerance_deg"] == 0.1
    for bad in (
        {"role": "edge", "radius": {}},
        {"role": "edge", "axis": {"direction": [0, 0, 0]}},
        {"role": "edge", "selector": ">Z[9] and (lambda)"},
    ):
        try:
            tq.normalize_query_step(bad, "q")
        except ToolError:
            continue
        raise AssertionError(f"bad step accepted: {bad!r}")


def test_query_transition_rules() -> None:
    assert (
        tq.normalize_query([{"role": "face"}, {"role": "edge"}, {"role": "edge"}], "q") is not None
    )
    try:
        tq.normalize_query([{"role": "edge"}, {"role": "face"}], "q")
    except ToolError as exc:
        assert exc.details["reason"] == "unsupported_query_transition"
        return
    raise AssertionError("edge->face transition accepted")


# ---------------------------------------------------------------------------
# Independent truth-table cross-check of the clustering rank.
# ---------------------------------------------------------------------------


def _reference_nth(
    values: list[float], vector: tuple[float, float, float], index: int | None, direction_max: bool
) -> list[int]:
    pairs = sorted(
        (
            (
                sum(a * b for a, b in zip(vector, (0.0, 0.0, float(value)), strict=True)),
                position + 1,
            )
            for position, value in enumerate(values)
        ),
        key=lambda entry: entry[0],
    )
    clustered: list[list[int]] = []
    anchor = 0.0
    for key, position in pairs:
        if not clustered or abs(key - anchor) > tq.SELECTOR_TOLERANCE:
            clustered.append([position])
            anchor = key
        else:
            clustered[-1].append(position)
    if not clustered:
        return []
    ordered = clustered if direction_max else list(reversed(clustered))
    return ordered[index if index is not None else -1]
