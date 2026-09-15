"""Liberal input normalization for enum-valued tool parameters.

Postel's law at the tool boundary: accept the value the calling model
meant when the meaning is unambiguous, and emit only canonical values.
Models arrive trained on several CAD ecosystems at once, so the same
design intent arrives spelled ``pad`` (FreeCAD), ``extrude`` (CadQuery,
Fusion 360, SolidWorks), or with different case and separator habits.
Three liberal forms are accepted, never more:

- Case: ``"front"`` and ``"Front"`` name the same capture_view orientation.
- Separators and case style: ``"through all"``, ``"throughAll"``, and
  ``"through_all"`` name the same hole depth type.
- Cross-tool synonyms: ``"extrude"`` names the PartDesign ``pad`` kind and
  ``"stp"`` names the ``step`` export format.

The acceptance is a closed table, not fuzz. A candidate is normalized only
when it matches exactly one canonical entry after folding to a comparison
key (lowercase alphanumerics); anything else passes through untouched so
the closed input schema refuses it under the caller's own spelling, with
the enum it expects. Tables are built through :func:`build_table`, which
refuses an alias aimed at a non-member and refuses two canonical entries
that fold to the same key, so vocabulary drift fails at import rather than
at call time.

Normalizers registered from this module run on the request arguments
before schema validation, which means consent fingerprints bind the
canonicalized request and a retry that spells a value differently binds
to the same target. The module never imports FreeCAD or Qt.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

#: Characters dropped by the comparison fold. Case, underscores, hyphens,
#: spaces, apostrophes, and dots carry no meaning in any accepted value;
#: what remains is the case-insensitive identifier the corpus spells one way.
_FOLD_RE = re.compile(r"[^a-z0-9]+")


def canonical_key(value: str) -> str:
    """Fold one candidate value to its comparison key.

    ``"pitchHeight"``, ``"pitch_height"``, and ``"pitch height"`` all fold
    to ``"pitchheight"``. Only ``str`` values reach this function.
    """

    return _FOLD_RE.sub("", value.lower())


def build_table(
    canonicals: Iterable[str],
    aliases: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build one comparison-key to canonical-value table.

    Every canonical entry is registered under its own fold, so a request
    spelled in the canonical form round-trips unchanged. Each alias must
    name a member of ``canonicals``; a drift between the alias table and
    the schema enum it serves raises here, at import, instead of failing a
    call months later. Two canonical entries that fold to the same key
    (``"H_Axis"`` and ``"haxis"`` as separate members, say) are a
    vocabulary bug and are refused the same way.
    """

    members = list(canonicals)
    member_set = set(members)
    table: dict[str, str] = {}
    for canonical in members:
        _register(table, canonical_key(canonical), canonical, canonical)
    for alias, canonical in (aliases or {}).items():
        if canonical not in member_set:
            raise ValueError(f"alias {alias!r} targets {canonical!r}, not a canonical member")
        _register(table, canonical_key(alias), canonical, alias)
    return table


def _register(table: dict[str, str], key: str, canonical: str, source: str) -> None:
    """Bind one folded key to its canonical value, refusing collisions."""

    existing = table.get(key)
    if existing is not None and existing != canonical:
        raise ValueError(
            f"{source!r} folds to {key!r}, already bound to {existing!r}; "
            "the vocabulary is ambiguous after folding"
        )
    table[key] = canonical


def normalize_arguments(
    arguments: Mapping[str, object],
    spec: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    """Return a copy of ``arguments`` with alias values canonicalized.

    ``spec`` maps a dotted path to the table for the string values found
    there. A segment written ``key[]`` descends into every element of the
    list at ``key``, so ``"addGeometry[].kind"`` normalizes each geometry
    entry and ``"parameters.transformations[].kind"`` each transformation
    step. Paths that name nothing, non-string leaves, and values absent
    from the table are copied through untouched: the closed schema remains
    the one authority on what is refused, and it rejects the caller's
    original spelling.

    The input mapping is never mutated; handlers receive a plain dict.
    """

    result = dict(arguments)
    for path, table in spec.items():
        result = _normalize_at(result, path.split("."), table)
    return result


def _normalize_at(
    value: object,
    segments: list[str],
    table: Mapping[str, str],
) -> object:
    """Normalize the table-bound string leaves reachable at one path."""

    head = segments[0]
    if head.endswith("[]"):
        # A ``key[]`` segment descends into every element of the list at
        # ``key``; the copy stays copy-on-write like every other descent.
        key = head[:-2]
        if not isinstance(value, dict) or key not in value:
            return value
        items = value[key]
        if not isinstance(items, list):
            return value
        updated = dict(value)
        updated[key] = [_normalize_at(item, segments[1:], table) for item in items]
        return updated
    if not isinstance(value, dict) or head not in value:
        return value
    if len(segments) == 1:
        leaf = value[head]
        if isinstance(leaf, str):
            updated = dict(value)
            updated[head] = table.get(canonical_key(leaf), leaf)
            return updated
        return value
    updated = dict(value)
    updated[head] = _normalize_at(value[head], segments[1:], table)
    return updated
