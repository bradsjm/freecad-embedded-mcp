"""Bounded CadQuery-style selector grammar over plain topology records.

This module is the shared parser, evaluator and schema-fragment owner for the
MCP geometry query contract. It never imports FreeCAD or Qt: the parser and
evaluator work on plain record dictionaries supplied by native adapters
(``tools/geometry.py``), so every consumer can share one vocabulary without an
import cycle.

Semantics intentionally follow the CadQuery string selector grammar (source:
``cadquery/selectors.py``, inspected at implementation time):

- ``%TYPE`` filters on analytic type names (case-insensitive).
- Directional operators ``+D``/``-D``/``|D``/``#D`` (and bare ``D`` as ``+D``)
  apply to planar faces and linear edges only. ``+``/``-`` compare directed
  angles, ``|`` the parallel cross product, ``#`` the perpendicular offset;
  the numerical tolerance is 0.0001 (radians for angles, cross-product length
  for ``|``). A custom direction keeps its magnitude for projections and
  cross products, matching the source behavior.
- Extrema ``>D``/``<D``/``>>D``/``<<D`` rank centers of mass projected on the
  direction and return a whole cluster. With an explicit ``[n]`` index the
  single-angle operators first restrict to ``|D``-compatible records while the
  doubled operators rank every candidate; clusters are anchored on the first
  value of each cluster (no neighbor chaining) and reversed for ``<``/``<<``.
- Set algebra evaluates every operand against the SAME candidate universe:
  ``and`` intersects, ``or`` unions, ``exc``/``except`` subtracts
  (left-associative) and prefix ``not`` complements within the universe with
  the lowest precedence, exactly like the pyparsing ``infix_notation`` source.

Deviations from CadQuery are deliberate and bounded: raw IndexError on a
missing nth cluster becomes ``selector_index_out_of_range``, unreadable
geometry evidence becomes ``selector_geometry_unavailable`` instead of a
silent zero-match, and class-constructor syntax is not part of this grammar.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from itertools import pairwise
from typing import Any

from .protocol import VALIDATION_FAILED, ToolError

# Wire limits, published through discover_capabilities as geometryQueries.
MAX_QUERY_STEPS = 3
SELECTOR_MAX_LENGTH = 256
MAX_QUERY_CANDIDATES = 4096
MAX_QUERY_REFERENCES = 64
MAX_REFERENCED_SUBSHAPES = 64

# Parser budgets: token, AST-node and nesting caps refuse pathological
# expressions before any evaluation work.
MAX_SELECTOR_TOKENS = 128
MAX_AST_NODES = 64
MAX_SELECTOR_NESTING = 8
MAX_NTH_INDEX = 4095

# Numerical selector tolerance from the CadQuery source (radians for angle
# comparisons, projected-center distance for clustering). This is a selector
# tolerance, never a manufacturing fit tolerance.
SELECTOR_TOLERANCE = 0.0001

# Analytic type names accepted after ``%``, uppercased: the union of the
# CadQuery face and edge geom-type tables. Applicability (a face is never
# LINE) is decided at evaluation, exactly like the source TypeSelector.
_SELECTOR_TYPES = frozenset(
    {
        "PLANE",
        "CYLINDER",
        "CONE",
        "SPHERE",
        "TORUS",
        "BEZIER",
        "BSPLINE",
        "REVOLUTION",
        "EXTRUSION",
        "OFFSET",
        "OTHER",
        "LINE",
        "CIRCLE",
        "ELLIPSE",
        "HYPERBOLA",
        "PARABOLA",
    }
)

_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "X": (1.0, 0.0, 0.0),
    "Y": (0.0, 1.0, 0.0),
    "Z": (0.0, 0.0, 1.0),
    "XY": (1.0, 1.0, 0.0),
    "XZ": (1.0, 0.0, 1.0),
    "YZ": (0.0, 1.0, 1.0),
}

#: Named views keep the CadQuery meanings, not FreeCAD camera names: each is
#: a DirectionMinMaxSelector over the given axis (max for the first column).
_NAMED_VIEWS: dict[str, tuple[str, bool]] = {
    "front": (">", "Z"),
    "back": ("<", "Z"),
    "left": ("<", "X"),
    "right": (">", "X"),
    "top": (">", "Y"),
    "bottom": ("<", "Y"),
}

_KEYWORDS = ("and", "or", "exc", "except", "not")


# ---------------------------------------------------------------------------
# Schema fragments. Consumers merge QUERY_DEFS into their own root $defs;
# no fragment embeds an unresolved local $ref under another object.
# ---------------------------------------------------------------------------

_TOPOLOGY_OBJECT_FIELD = {"type": "string", "minLength": 1}

_TOPOLOGY_WHOLE_TARGET_DEF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object"],
    "properties": {"object": _TOPOLOGY_OBJECT_FIELD},
}

_TOPOLOGY_REFERENCE_TARGET_DEF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object", "subelement"],
    "properties": {
        "object": _TOPOLOGY_OBJECT_FIELD,
        "subelement": {"type": "string", "minLength": 1},
    },
}

_TOPOLOGY_QUERY_STEP_DEF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["role"],
    "properties": {
        "role": {"enum": ["face", "edge"]},
        "selector": {"type": "string", "minLength": 1, "maxLength": SELECTOR_MAX_LENGTH},
        "radius": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "min": {"type": "number", "minimum": 0},
                "max": {"type": "number", "minimum": 0},
            },
        },
        "axis": {
            "type": "object",
            "additionalProperties": False,
            "required": ["direction"],
            "properties": {
                "direction": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                },
                "tolerance_deg": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 90,
                    "default": 0.1,
                },
            },
        },
    },
}

_TOPOLOGY_QUERY_DEF = {
    "type": "array",
    "items": {"$ref": "#/$defs/topologyQueryStep"},
    "minItems": 1,
    "maxItems": MAX_QUERY_STEPS,
}

_TOPOLOGY_QUERY_TARGET_DEF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object", "query"],
    "properties": {
        "object": _TOPOLOGY_OBJECT_FIELD,
        "query": {"$ref": "#/$defs/topologyQuery"},
        "expected_generation": {"type": "integer", "minimum": 0},
    },
}

_TOPOLOGY_TARGET_DEF = {
    "anyOf": [
        {"$ref": "#/$defs/topologyWholeTarget"},
        {"$ref": "#/$defs/topologyReferenceTarget"},
        {"$ref": "#/$defs/topologyQueryTarget"},
    ]
}

_TOPOLOGY_RESOLVED_SELECTION_DEF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["parameter", "document", "generation", "references", "count"],
    "properties": {
        "parameter": {"type": "string", "minLength": 1},
        "document": {"type": "string", "minLength": 1},
        "generation": {"type": "integer", "minimum": 0},
        "references": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["object", "subelement"],
                "properties": {
                    "object": {"type": "string", "minLength": 1},
                    "subelement": {"type": "string", "minLength": 1},
                },
            },
            "minItems": 0,
            "maxItems": MAX_QUERY_REFERENCES,
        },
        "count": {"type": "integer", "minimum": 0, "maximum": MAX_QUERY_REFERENCES},
    },
}

#: Shared definition set consumed by every query-bearing input/output schema.
QUERY_DEFS = {
    "topologyQueryStep": _TOPOLOGY_QUERY_STEP_DEF,
    "topologyQuery": _TOPOLOGY_QUERY_DEF,
    "topologyWholeTarget": _TOPOLOGY_WHOLE_TARGET_DEF,
    "topologyReferenceTarget": _TOPOLOGY_REFERENCE_TARGET_DEF,
    "topologyQueryTarget": _TOPOLOGY_QUERY_TARGET_DEF,
    "topologyTarget": _TOPOLOGY_TARGET_DEF,
    "topologyResolvedSelection": _TOPOLOGY_RESOLVED_SELECTION_DEF,
}


def merge_query_defs(schema: dict) -> dict:
    """Merge the shared topology query definitions into a schema's ``$defs``.

    Consumers call this once per schema root so every ``$ref`` resolves
    locally; the schema dict is modified in place and returned.
    """

    defs = schema.setdefault("$defs", {})
    for name, definition in QUERY_DEFS.items():
        if name in defs and defs[name] is not definition:
            raise ValueError(f"schema already defines {name}")
        defs[name] = definition
    return schema


# ---------------------------------------------------------------------------
# Selector parsing.
# ---------------------------------------------------------------------------


def _syntax_error(position: int, expected: list[str], message: str) -> ToolError:
    return ToolError(
        VALIDATION_FAILED,
        f"selector syntax error at position {position}: {message}",
        {
            "reason": "selector_syntax",
            "position": position,
            "expected": expected[:8],
            "nextTool": "inspect_topology",
        },
    )


def _limit_error(detail: str) -> ToolError:
    return ToolError(
        VALIDATION_FAILED,
        f"selector exceeds the grammar budget: {detail}",
        {"reason": "selector_limit", "nextTool": "inspect_topology"},
    )


def _index_error(index: int, group_count: int) -> ToolError:
    return ToolError(
        VALIDATION_FAILED,
        f"selector index [{index}] is outside the {group_count} matched "
        "cluster(s); use inspect_topology to compare candidates",
        {
            "reason": "selector_index_out_of_range",
            "index": index,
            "groupCount": group_count,
            "nextTool": "inspect_topology",
        },
    )


def _geometry_unavailable_error(detail: str) -> ToolError:
    return ToolError(
        VALIDATION_FAILED,
        f"selector evidence unavailable: {detail}",
        {"reason": "selector_geometry_unavailable", "nextTool": "inspect_topology"},
    )


def _parse_number(text: str, position: int) -> tuple[float, int]:
    """Parse ``[+-]digits(.digits?)?`` starting at ``position``.

    Returns ``(value, next_offset)``; the fraction digit is optional after
    the point but the integer part is required, and exponent notation is
    rejected by refusing any letter glued to the number.
    """

    offset = position
    length = len(text)
    if offset < length and text[offset] in "+-":
        offset += 1
    digits_start = offset
    while offset < length and text[offset].isdigit():
        offset += 1
    if offset == digits_start:
        raise _syntax_error(position, ["number"], "expected a number")
    if offset < length and text[offset] == ".":
        offset += 1
        while offset < length and text[offset].isdigit():
            offset += 1
    if offset < length and (text[offset].isalpha() or text[offset] == "_"):
        raise _syntax_error(position, ["number"], f"unexpected {text[offset]!r} in number")
    return float(text[position:offset]), offset


def _skip_ws(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _parse_direction(text: str, position: int) -> tuple[tuple[float, float, float], int]:
    """Parse a direction token (named axis or ``(x, y, z)``) at ``position``.

    A named axis keeps the source magnitude (``XY`` is ``(1, 1, 0)``, not
    normalized); a custom vector also keeps its magnitude for center
    projections and parallel cross products. Zero vectors are refused.
    """

    for name, vector in _DIRECTIONS.items():
        if text.startswith(name, position):
            rest = position + len(name)
            if rest < len(text) and (text[rest].isalnum() or text[rest] == "_"):
                continue
            return vector, rest
    if text.startswith("(", position):
        open_position = position
        offset = _skip_ws(text, position + 1)
        components: list[float] = []
        for index in range(3):
            if index:
                if offset >= len(text) or text[offset] != ",":
                    raise _syntax_error(offset, [",", ")"], "expected ',' in vector")
                offset = _skip_ws(text, offset + 1)
            value, offset = _parse_number(text, offset)
            components.append(value)
            offset = _skip_ws(text, offset)
        if offset >= len(text) or text[offset] != ")":
            raise _syntax_error(offset, [")"], "expected ')' to close the vector")
        if not all(math.isfinite(value) for value in components):
            raise _syntax_error(open_position, ["nonzero direction"], "vector is not finite")
        if abs(components[0]) + abs(components[1]) + abs(components[2]) <= 0.0:
            raise _syntax_error(open_position, ["nonzero direction"], "zero vector")
        return tuple(components), offset + 1  # type: ignore[return-value]
    raise _syntax_error(
        position,
        ["direction (X, Y, Z, XY, XZ, YZ or (x,y,z))"],
        "expected a direction",
    )


def _parse_index(text: str, position: int) -> tuple[int | None, int]:
    """Parse the optional ``[n]`` index after an extremum operator."""

    if position >= len(text) or text[position] != "[":
        return None, position
    offset = _skip_ws(text, position + 1)
    negative = False
    if offset < len(text) and text[offset] == "-":
        negative = True
        offset += 1
    digits_start = offset
    while offset < len(text) and text[offset].isdigit():
        offset += 1
    if offset == digits_start:
        raise _syntax_error(position, ["[n]"], "expected an integer index")
    value = int(text[digits_start:offset])
    if negative:
        value = -value
    if abs(value) > MAX_NTH_INDEX:
        raise _limit_error(f"index magnitude {abs(value)} exceeds {MAX_NTH_INDEX}")
    offset = _skip_ws(text, offset)
    if offset >= len(text) or text[offset] != "]":
        raise _syntax_error(offset, ["]"], "expected ']' to close the index")
    return value, offset + 1


class _Tokens:
    """Bounded token stream with parse-position tracking."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.items: list[tuple[str, Any, int]] = []
        self.position = 0

    def error(self, expected: list[str], message: str) -> ToolError:
        return _syntax_error(self.position, expected, message)


def _tokenize(text: str) -> list[tuple[str, Any, int]]:
    """Tokenize one selector string into ``(kind, value, position)`` tuples.

    Selector atoms are tokenized whole (operator + direction + optional
    index) so the reported error position is exact; keywords and parens
    become their own tokens. Requires word boundaries around keywords, a
    deliberate strictness over the pyparsing source.
    """

    tokens: list[tuple[str, Any, int]] = []
    position = 0
    length = len(text)

    def _atom_keyword_boundary(next_offset: int) -> bool:
        following = text[next_offset] if next_offset < length else ""
        return not (following.isalnum() or following == "_")

    while True:
        position = _skip_ws(text, position)
        if position >= length:
            break
        if len(tokens) >= MAX_SELECTOR_TOKENS:
            raise _limit_error(f"more than {MAX_SELECTOR_TOKENS} tokens")
        start = position
        char = text[position]
        if char == "(":
            tokens.append(("lparen", None, start))
            position += 1
            continue
        if char == ")":
            tokens.append(("rparen", None, start))
            position += 1
            continue
        if char == "%":
            offset = position + 1
            name_start = offset
            while offset < length and (text[offset].isalnum() or text[offset] == "_"):
                offset += 1
            name = text[name_start:offset].upper()
            if not name or name not in _SELECTOR_TYPES:
                raise _syntax_error(
                    name_start,
                    sorted(_SELECTOR_TYPES),
                    f"unknown geometry type {text[name_start:offset]!r}",
                )
            tokens.append(("atom", ("type", name), start))
            position = offset
            continue
        if text.startswith(">>", start) or text.startswith("<<", start):
            operator = text[start : start + 2]
            vector, offset = _parse_direction(text, start + 2)
            index, offset = _parse_index(text, offset)
            tokens.append(("atom", ("extrema", operator, vector, index), start))
            position = offset
            continue
        if char in "<>":
            vector, offset = _parse_direction(text, start + 1)
            index, offset = _parse_index(text, offset)
            tokens.append(("atom", ("extrema", char, vector, index), start))
            position = offset
            continue
        if char in "+-|#":
            vector, offset = _parse_direction(text, start + 1)
            tokens.append(("atom", ("direction", char, vector, None), start))
            position = offset
            continue
        if char.isalpha() or char == "_":
            offset = start
            while offset < length and (text[offset].isalnum() or text[offset] == "_"):
                offset += 1
            word = text[start:offset]
            if word in _KEYWORDS:
                if not _atom_keyword_boundary(offset):
                    raise _syntax_error(start, ["keyword", "selector"], f"unknown token {word!r}")
                tokens.append((word, None, start))
                position = offset
                continue
            if word in _NAMED_VIEWS:
                operator, axis = _NAMED_VIEWS[word]
                tokens.append(("atom", ("extrema", operator, _DIRECTIONS[axis], None), start))
                position = offset
                continue
            if word in _DIRECTIONS:
                if not _atom_keyword_boundary(offset):
                    raise _syntax_error(start, ["selector", "keyword"], f"unknown token {word!r}")
                tokens.append(("atom", ("direction", "+", _DIRECTIONS[word], None), start))
                position = offset
                continue
            raise _syntax_error(start, ["selector", "keyword"], f"unknown token {word!r}")
        raise _syntax_error(start, ["selector", "keyword", "(", ")"], f"unexpected {char!r}")
    return tokens


class _Parser:
    """Recursive-descent parser matching the pyparsing precedence.

    Levels from strongest to weakest: ``and``, ``or``, left-associative
    ``exc``/``except``, then right-associative prefix ``not`` (lowest). A
    ``not`` prefix therefore wraps the ENTIRE remaining expression, exactly
    like the infix_notation source, so ``not >X and >Y`` is
    ``not (>X and >Y)``. ``not`` is also accepted as a bare operand prefix
    after a binary operator (``A and not B``), a permissive superset that
    keeps the documented precedence intact.
    """

    def __init__(self, tokens: list[tuple[str, Any, int]]) -> None:
        self.tokens = tokens
        self.offset = 0
        self.nodes = 0

    def _peek(self) -> tuple[str, Any, int] | None:
        return self.tokens[self.offset] if self.offset < len(self.tokens) else None

    def _next(self) -> tuple[str, Any, int] | None:
        token = self._peek()
        if token is not None:
            self.offset += 1
        return token

    def _node(self, ast: tuple) -> tuple:
        self.nodes += 1
        if self.nodes > MAX_AST_NODES:
            raise _limit_error(f"more than {MAX_AST_NODES} expression nodes")
        return ast

    def parse(self) -> tuple:
        ast = self._expression(0)
        leftover = self._peek()
        if leftover is not None:
            raise _syntax_error(leftover[2], ["keyword", "end of selector"], "unexpected input")
        return ast

    def _expression(self, depth: int) -> tuple:
        """Lowest level: an optional ``not`` prefix over a difference chain."""

        if depth > MAX_SELECTOR_NESTING:
            raise _limit_error(f"nesting deeper than {MAX_SELECTOR_NESTING}")
        token = self._peek()
        if token is not None and token[0] == "not":
            self._next()
            return self._node(("not", self._expression(depth + 1)))
        return self._difference(depth)

    def _difference(self, depth: int) -> tuple:
        """``exc``/``except`` chains, left-associative."""

        left = self._disjunction(depth)
        while True:
            operator = self._peek()
            if operator is None or operator[0] not in ("exc", "except"):
                return left
            self._next()
            right = self._disjunction(depth)
            left = self._node(("exc", left, right))

    def _disjunction(self, depth: int) -> tuple:
        """``or`` chains, left-associative."""

        left = self._conjunction(depth)
        while True:
            operator = self._peek()
            if operator is None or operator[0] != "or":
                return left
            self._next()
            right = self._conjunction(depth)
            left = self._node(("or", left, right))

    def _conjunction(self, depth: int) -> tuple:
        """``and`` chains, left-associative and strongest."""

        left = self._unary(depth)
        while True:
            operator = self._peek()
            if operator is None or operator[0] != "and":
                return left
            self._next()
            right = self._unary(depth)
            left = self._node(("and", left, right))

    def _unary(self, depth: int) -> tuple:
        if depth > MAX_SELECTOR_NESTING:
            raise _limit_error(f"nesting deeper than {MAX_SELECTOR_NESTING}")
        token = self._next()
        if token is None:
            raise _syntax_error(0, ["selector"], "unexpected end of selector")
        kind, value, position = token
        if kind == "atom":
            return self._node(value)
        if kind == "not":
            return self._node(("not", self._expression(depth + 1)))
        if kind == "lparen":
            inner = self._expression(depth + 1)
            closing = self._next()
            if closing is None or closing[0] != "rparen":
                raise _syntax_error(
                    closing[2] if closing is not None else position,
                    [")"],
                    "expected ')'",
                )
            return inner
        raise _syntax_error(position, ["selector", "not", "("], f"unexpected {kind!r}")


def parse_selector(text: str) -> tuple:
    """Parse one selector string into an AST of plain tuples.

    AST nodes are ``("type", name)``, ``("direction", op, vector, None)``,
    ``("extrema", op, vector, index_or_None)``, ``("and", l, r)``,
    ``("or", l, r)``, ``("exc", l, r)`` and ``("not", child)``.
    """

    if not isinstance(text, str) or not text.strip():
        raise _syntax_error(0, ["selector"], "empty selector")
    if len(text) > SELECTOR_MAX_LENGTH:
        raise _limit_error(f"longer than {SELECTOR_MAX_LENGTH} characters")
    tokens = _tokenize(text)
    if not tokens:
        raise _syntax_error(0, ["selector"], "empty selector")
    return _Parser(tokens).parse()


# ---------------------------------------------------------------------------
# Selector evaluation over plain records.
# ---------------------------------------------------------------------------


def _record_direction(record: Mapping) -> tuple[float, float, float] | None:
    direction = record.get("direction")
    if direction is None:
        return None
    return (float(direction[0]), float(direction[1]), float(direction[2]))


def _require_direction(record: Mapping) -> tuple[float, float, float]:
    """Return the record's direction, refusing eligible-but-unreadable rows.

    Directional operators are defined only for planar faces and linear
    edges. A record whose type says it is eligible but whose direction
    could not be read must fail the selector rather than silently drop the
    candidate.
    """

    direction = _record_direction(record)
    if direction is not None:
        return direction
    if record.get("type") in ("PLANE", "LINE"):
        raise _geometry_unavailable_error(
            f"{record.get('role', 'subshape')} {record.get('index')} is "
            f"{record.get('type')} but its direction could not be read"
        )
    raise _geometry_unavailable_error(
        f"{record.get('role', 'subshape')} {record.get('index')} has no "
        "directional evidence for this operator"
    )


def _require_center(record: Mapping) -> tuple[float, float, float]:
    center = record.get("center")
    if center is not None:
        return (float(center[0]), float(center[1]), float(center[2]))
    raise _geometry_unavailable_error(
        f"{record.get('role', 'subshape')} {record.get('index')} has no readable center of mass"
    )


def _type_matches(record: Mapping, name: str) -> bool:
    status = record.get("typeStatus", "ok")
    if status != "ok":
        if name == "OTHER":
            # An unclassified record may or may not be "other" geometry;
            # refusing beats a false zero-match.
            raise _geometry_unavailable_error(
                f"{record.get('role', 'subshape')} {record.get('index')} could "
                "not be classified, so %OTHER cannot be decided"
            )
        if status == "unreadable":
            raise _geometry_unavailable_error(
                f"{record.get('role', 'subshape')} {record.get('index')} could "
                f"not be classified, so %{name} cannot be decided"
            )
        # An unmapped-but-known analytic class is definitively not one of
        # the mapped names.
        return False
    return record.get("type") == name


def _directional_matches(
    operator: str, vector: tuple[float, float, float], record: Mapping
) -> bool:
    """Apply one directional operator to a record with readable direction.

    ``+``/``-`` compare the directed angle to the requested vector (magnitude
    preserved), ``|`` the cross-product length against the unit geometry
    direction and ``#`` the perpendicular angular offset. Only planar faces
    and linear edges are direction-compatible: other types are correctly not
    matches, while an eligible record whose direction cannot be read refuses
    the selector instead of silently dropping the candidate.
    """

    direction = _record_direction(record)
    if direction is None:
        if record.get("type") in ("PLANE", "LINE"):
            raise _geometry_unavailable_error(
                f"{record.get('role', 'subshape')} {record.get('index')} is "
                f"{record.get('type')} but its direction could not be read"
            )
        return False
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        raise _geometry_unavailable_error("selector direction is a zero vector")
    dot = sum(a * b for a, b in zip(vector, direction, strict=True))
    cosine = max(-1.0, min(1.0, dot / norm))
    angle = math.acos(cosine)
    if operator == "+":
        return angle < SELECTOR_TOLERANCE
    if operator == "-":
        return (math.pi - angle) < SELECTOR_TOLERANCE
    if operator == "|":
        cross = (
            vector[1] * direction[2] - vector[2] * direction[1],
            vector[2] * direction[0] - vector[0] * direction[2],
            vector[0] * direction[1] - vector[1] * direction[0],
        )
        return math.sqrt(sum(value * value for value in cross)) < SELECTOR_TOLERANCE
    return abs(angle - math.pi / 2.0) < SELECTOR_TOLERANCE


def _cluster_keys(
    records: list[Mapping], vector: tuple[float, float, float]
) -> list[tuple[float, Mapping]]:
    keys = []
    for record in records:
        center = _require_center(record)
        key = sum(a * b for a, b in zip(vector, center, strict=True))
        keys.append((key, record))
    keys.sort(key=lambda entry: entry[0])
    return keys


def _cluster(
    keys: list[tuple[float, Mapping]],
) -> list[list[Mapping]]:
    """Cluster ascending keys anchored on the FIRST value of each cluster."""

    clustered: list[list[Mapping]] = []
    anchor = 0.0
    for key, record in keys:
        if not clustered or abs(key - anchor) > SELECTOR_TOLERANCE:
            clustered.append([record])
            anchor = key
        else:
            clustered[-1].append(record)
    return clustered


def _nth(clustered: list[list[Mapping]], index: int | None, direction_max: bool) -> list[Mapping]:
    ordered = clustered if direction_max else list(reversed(clustered))
    if not ordered:
        return []
    if index is None:
        return ordered[-1]
    try:
        return ordered[index]
    except IndexError:
        raise _index_error(index, len(ordered)) from None


def _evaluate(ast: tuple, universe: list[Mapping]) -> list[Mapping]:
    tag = ast[0]
    if tag == "type":
        return [record for record in universe if _type_matches(record, ast[1])]
    if tag == "direction":
        _operator, operator, vector, _index = ast
        return [record for record in universe if _directional_matches(operator, vector, record)]
    if tag == "extrema":
        _operator, operator, vector, index = ast
        direction_max = operator in (">", ">>")
        candidates = list(universe)
        if index is not None and operator in (">", "<"):
            # A single-angle extremum with an explicit index first restricts
            # to direction-compatible records, exactly like the source
            # DirectionNthSelector; doubled operators rank every candidate.
            candidates = [
                record for record in universe if _directional_matches("|", vector, record)
            ]
        if not candidates:
            return []
        return _nth(_cluster(_cluster_keys(candidates, vector)), index, direction_max)
    if tag == "and":
        left = {record["index"] for record in _evaluate(ast[1], universe)}
        return [record for record in _evaluate(ast[2], universe) if record["index"] in left]
    if tag == "or":
        chosen = {record["index"] for record in _evaluate(ast[1], universe)} | {
            record["index"] for record in _evaluate(ast[2], universe)
        }
        return [record for record in universe if record["index"] in chosen]
    if tag == "exc":
        excluded = {record["index"] for record in _evaluate(ast[2], universe)}
        return [record for record in _evaluate(ast[1], universe) if record["index"] not in excluded]
    if tag == "not":
        excluded = {record["index"] for record in _evaluate(ast[1], universe)}
        return [record for record in universe if record["index"] not in excluded]
    raise _geometry_unavailable_error(f"unknown selector node {tag!r}")


def evaluate_selector(ast: tuple, records: list[dict]) -> list[int]:
    """Evaluate one parsed selector over ``records``.

    Records carry ``index``, an uppercase analytic ``type`` (with a
    ``typeStatus`` of ``ok``/``unmapped``/``unreadable``), a finite
    ``center`` and an optional unit ``direction`` plus optional ``radius``
    and ``axis`` analytic data. The returned indices keep the original
    native order even though set algebra and ranking sort internally.
    """

    chosen = {record["index"] for record in _evaluate(ast, records)}
    return [record["index"] for record in records if record["index"] in chosen]


# ---------------------------------------------------------------------------
# Query-descriptor validation (beyond the JSON schema).
# ---------------------------------------------------------------------------


def normalize_query_step(step: Mapping, what: str) -> dict:
    """Apply the omission defaults of one query step.

    Schema ``default`` annotations are never injected values: the boundary
    normalizer fills omitted options so an omitted selector means "all
    candidates" and tolerance_deg becomes exactly 0.1.
    """

    selector = step.get("selector")
    if selector is not None:
        parse_selector(selector)  # refuse malformed syntax before any native access
    radius = step.get("radius")
    if radius is not None and radius.get("min") is None and radius.get("max") is None:
        raise ToolError(
            VALIDATION_FAILED,
            f"{what}.radius requires at least one of min or max",
            {"parameter": what, "reason": "empty_radius_range", "nextTool": "inspect_topology"},
        )
    axis = step.get("axis")
    if axis is not None:
        direction = [float(value) for value in axis["direction"]]
        if not all(math.isfinite(value) for value in direction):
            raise ToolError(
                VALIDATION_FAILED,
                f"{what}.axis.direction must be finite",
                {"parameter": what, "nextTool": "inspect_topology"},
            )
        if abs(direction[0]) + abs(direction[1]) + abs(direction[2]) <= 0.0:
            raise ToolError(
                VALIDATION_FAILED,
                f"{what}.axis.direction must be a nonzero vector",
                {"parameter": what, "nextTool": "inspect_topology"},
            )
    return {
        "role": step["role"],
        "selector": selector,
        "radius": dict(radius) if radius is not None else None,
        "axis": (
            {
                "direction": direction,
                "tolerance_deg": float(axis.get("tolerance_deg", 0.1)),
            }
            if axis is not None
            else None
        ),
    }


def normalize_query(query: Any, what: str) -> list[dict]:
    """Validate and default-normalize one query array (1..MAX_QUERY_STEPS)."""

    if not isinstance(query, list) or not 1 <= len(query) <= MAX_QUERY_STEPS:
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} must be an array of 1 to {MAX_QUERY_STEPS} query steps",
            {"parameter": what, "nextTool": "inspect_topology"},
        )
    steps = []
    for position, step in enumerate(query):
        if not isinstance(step, Mapping) or step.get("role") not in ("face", "edge"):
            raise ToolError(
                VALIDATION_FAILED,
                f"{what}[{position}].role must be 'face' or 'edge'",
                {"parameter": f"{what}[{position}]", "nextTool": "inspect_topology"},
            )
        steps.append(normalize_query_step(step, f"{what}[{position}]"))
    for previous, following in pairwise(steps):
        if previous["role"] == "edge" and following["role"] == "face":
            raise ToolError(
                VALIDATION_FAILED,
                f"{what}: an edge step cannot be followed by a face step "
                "(no implicit ancestor query)",
                {
                    "parameter": what,
                    "reason": "unsupported_query_transition",
                    "nextTool": "inspect_topology",
                },
            )
    return steps


def radius_predicate_matches(step: Mapping, record: Mapping) -> bool:
    bounds = step.get("radius")
    if bounds is None:
        # No radius predicate: every candidate passes, including records
        # that carry no radius data at all.
        return True
    radius = record.get("radius")
    if radius is None:
        # A radius predicate matches only geometry with radius data.
        return False
    minimum = bounds.get("min")
    maximum = bounds.get("max")
    if minimum is not None and radius < float(minimum):
        return False
    return not (maximum is not None and radius > float(maximum))


def axis_predicate_matches(step: Mapping, record: Mapping) -> bool:
    bounds = step.get("axis")
    axis = record.get("axis")
    if bounds is None:
        return True
    if axis is None:
        # The analytic-axis predicate matches only geometry that carries the
        # required analytic axis data; other records are correctly not matches.
        return False
    direction = bounds["direction"]
    tolerance = math.radians(float(bounds.get("tolerance_deg", 0.1)))
    norm = math.sqrt(sum(value * value for value in direction))
    if norm <= 0.0:
        return False
    cosine = max(
        -1.0,
        min(1.0, sum(a * b for a, b in zip(direction, axis, strict=True)) / norm),
    )
    angle = math.acos(cosine)
    # Sign-insensitive: an analytic axis is a line, not an orientation.
    return angle <= tolerance or (math.pi - angle) <= tolerance
