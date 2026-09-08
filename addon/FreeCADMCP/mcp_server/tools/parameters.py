"""``edit_parameters`` tool: expressions, dynamic renames and added properties.

One tool, three change kinds, applied atomically inside the shared mutation
gate (``object_validation.mutation``): add new dynamic properties, rename
dynamic properties, and register expressions with ``setExpression``. The
mutation gate opens its own transaction, recomputes, validates the edited
object plus every recomputed dependent, commits on success and aborts with a
rollback on any failure — so an invalid change leaves the document exactly
as it was.

Validation order (nothing mutates before all of it passes):

1. Every ``add`` entry: name shape, duplicate/collision check against the
   object's existing properties, known property type, per-type value shape.
2. Every ``rename`` pair: the source must be an existing property, the
   target name must be free (not colliding with existing, added or other
   renamed-to names), and neither source nor expression targets may be
   read-only. Renames are for *dynamic* properties: FreeCAD 1.1 exposes no
   Python is-dynamic query, so the enforcement is real but deferred —
   ``renameProperty`` itself refuses built-in properties inside the
   transaction, the mutation gate catches that and rolls the whole operation
   back (reported limitation, verified as atomic).
3. Every ``expression`` key: must be a final (post-rename) property name and
   must not be read-only. Expression values are FreeCAD expression strings;
   an expression that references something invalid fails the recompute
   inside the gate and rolls back.

Expression keys are matched against the final property set (adds applied
first, then renames, then expressions). FreeCAD's rename propagates through
existing expressions via its expression-engine rename visitor, so renaming a
property does not silently break references; any breakage that does occur
fails the recompute and rolls back.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

from ..object_validation import mutation
from ..protocol import VALIDATION_FAILED, ToolError

# Property types accepted for ``add``; the FreeCAD-level
# ``supportedProperties()`` check below additionally guards at runtime.
_PROPERTY_TYPES: tuple[str, ...] = (
    "App::PropertyBool",
    "App::PropertyInteger",
    "App::PropertyFloat",
    "App::PropertyString",
    "App::PropertyLength",
    "App::PropertyDistance",
    "App::PropertyAngle",
    "App::PropertyVector",
    "App::PropertyColor",
    "App::PropertyStringList",
    "App::PropertyFloatList",
    "App::PropertyIntegerList",
)

_MAX_NAME_LENGTH = 100
_MAX_ADDS = 32
_MAX_LIST_ITEMS = 1024
_GROUP = "Parameters"
_DOC = "Added by freecad-mcp edit_parameters."

_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

TOOL_DEFINITIONS: list[dict[str, Any]] = []
HANDLERS: dict[str, Callable[[Any, dict[str, Any]], Any]] = {}

# Keys whose edit-mode status makes the property read-only to MCP.
_READ_ONLY_MODES = frozenset({"ReadOnly", "Immutable"})


# ---------------------------------------------------------------------------
# Input schema fragments.
# ---------------------------------------------------------------------------

_VALUE_SCHEMA: dict[str, Any] = {
    "type": ["boolean", "integer", "number", "string", "array"],
    "items": {"type": ["boolean", "integer", "number", "string"]},
    "maxItems": _MAX_LIST_ITEMS,
    "description": "Initial value; its shape must match the property type.",
}

_PROPERTY_ADD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_NAME_LENGTH,
            "description": "New dynamic property name (identifier-shaped).",
        },
        "type": {
            "type": "string",
            "enum": list(_PROPERTY_TYPES),
        },
        "value": _VALUE_SCHEMA,
    },
    "required": ["name", "type"],
    "additionalProperties": False,
}


def _definition() -> dict[str, Any]:
    return {
        "name": "edit_parameters",
        "description": (
            "Edit one object's parameters atomically: add dynamic properties, "
            "rename dynamic properties (built-in properties cannot be renamed "
            "and are rejected), and register FreeCAD expressions with "
            "setExpression. All names, types and value shapes are validated "
            "before anything changes; the changes then run inside the shared "
            "mutation gate so a failing recompute, a failed property "
            "operation or an invalid expression reference rolls the whole "
            "operation back. Expression keys are final (post-rename) property "
            "names. Recomputed dependents are validated, not just the edited "
            "object."
        ),
        "inputSchema": {
            "type": "object",
            "$defs": {"property_add": _PROPERTY_ADD_SCHEMA},
            "properties": {
                "document": {"type": "string", "minLength": 1},
                "object": {"type": "string", "minLength": 1},
                "expressions": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "minLength": 1},
                    "description": (
                        "Final property name -> FreeCAD expression string."
                    ),
                },
                "rename": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "minLength": 1},
                    "description": "Existing dynamic property name -> new name.",
                },
                "add": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/property_add"},
                    "maxItems": _MAX_ADDS,
                },
            },
            "required": ["document", "object"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "$defs": {
                "rename_pair": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "required": ["from", "to"],
                    "additionalProperties": False,
                }
            },
            "properties": {
                "object": {"type": "string", "minLength": 1},
                "added": {"type": "array", "items": {"type": "string"}},
                "renamed": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/rename_pair"},
                },
                "expressions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["object", "added", "renamed", "expressions"],
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS = [_definition()]


# ---------------------------------------------------------------------------
# Validation (pure reads of the object; nothing mutates).
# ---------------------------------------------------------------------------


def _fail(message: str) -> ToolError:
    return ToolError(VALIDATION_FAILED, message)


def _check_name(name: Any, *, what: str) -> str:
    if not isinstance(name, str) or not _NAME_PATTERN.match(name):
        raise _fail(
            f"{what} must be an identifier (letters, digits, underscores; not "
            f"starting with a digit), got {name!r}"
        )
    if len(name) > _MAX_NAME_LENGTH:
        raise _fail(f"{what} must be at most {_MAX_NAME_LENGTH} characters")
    return name


def _check_type(prop_type: Any, obj: Any) -> str:
    if not isinstance(prop_type, str) or prop_type not in _PROPERTY_TYPES:
        raise _fail(
            f"unsupported property type {prop_type!r}; supported types are "
            f"{', '.join(_PROPERTY_TYPES)}"
        )
    supported = getattr(obj, "supportedProperties", None)
    if callable(supported):
        try:
            known = list(supported())
        except Exception:
            known = None
        if known is not None and prop_type not in known:
            raise _fail(f"property type {prop_type!r} is not supported by this object")
    return prop_type


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _check_value_shape(prop_type: str, value: Any, name: str) -> None:
    """Reject value shapes that cannot be assigned to ``prop_type``."""

    def numbers(item: Any, count: int) -> None:
        if not isinstance(value, list) or len(value) != count:
            raise _fail(
                f"property '{name}' of type {prop_type} needs a list of "
                f"{count} numbers, got {value!r}"
            )
        if any(_finite_number(item) is None for item in value):
            raise _fail(f"property '{name}' needs finite numbers, got {value!r}")

    if prop_type == "App::PropertyBool":
        if not isinstance(value, bool):
            raise _fail(f"property '{name}' of type {prop_type} needs a boolean")
        return
    if prop_type == "App::PropertyInteger":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(f"property '{name}' of type {prop_type} needs an integer")
        return
    if prop_type in (
        "App::PropertyFloat",
        "App::PropertyLength",
        "App::PropertyDistance",
        "App::PropertyAngle",
    ):
        if _finite_number(value) is None:
            raise _fail(f"property '{name}' of type {prop_type} needs a finite number")
        return
    if prop_type == "App::PropertyString":
        if not isinstance(value, str):
            raise _fail(f"property '{name}' of type {prop_type} needs a string")
        return
    if prop_type == "App::PropertyVector":
        numbers(value, 3)
        return
    if prop_type == "App::PropertyColor":
        if not isinstance(value, list) or len(value) not in (3, 4):
            raise _fail(
                f"property '{name}' of type {prop_type} needs [r, g, b(, a)], "
                f"got {value!r}"
            )
        if any(_finite_number(item) is None for item in value):
            raise _fail(f"property '{name}' needs finite color components")
        return
    # List types.
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        raise _fail(
            f"property '{name}' of type {prop_type} needs an array of at most "
            f"{_MAX_LIST_ITEMS} items"
        )
    if prop_type == "App::PropertyStringList":
        if any(not isinstance(item, str) for item in value):
            raise _fail(f"property '{name}' needs an array of strings")
        return
    if prop_type == "App::PropertyIntegerList":
        if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            raise _fail(f"property '{name}' needs an array of integers")
        return
    if prop_type == "App::PropertyFloatList":
        if any(_finite_number(item) is None for item in value):
            raise _fail(f"property '{name}' needs an array of finite numbers")
        return
    raise _fail(f"property type {prop_type!r} has no value shape rule")


def _editor_modes(obj: Any, name: str) -> list[str]:
    editor_mode = getattr(obj, "getEditorMode", None)
    if not callable(editor_mode):
        return []
    try:
        modes = editor_mode(name)
    except Exception:
        return []
    if isinstance(modes, str):
        return [modes]
    try:
        return [str(mode) for mode in modes]
    except Exception:
        return []


def _reject_read_only(obj: Any, name: str, *, what: str) -> None:
    modes = _editor_modes(obj, name)
    blocked = sorted(_READ_ONLY_MODES.intersection(modes))
    if blocked:
        raise _fail(
            f"{what} targets read-only property '{name}' (editor mode: "
            f"{', '.join(blocked)})"
        )


def _validate_all(obj: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate every requested change up front; return the ordered plan."""

    expressions = arguments.get("expressions") or {}
    rename = arguments.get("rename") or {}
    add = arguments.get("add") or []

    if not isinstance(expressions, dict) or not isinstance(rename, dict):
        raise _fail("expressions and rename must be objects")
    if not isinstance(add, list) or len(add) > _MAX_ADDS:
        raise _fail(f"add must be an array of at most {_MAX_ADDS} entries")

    existing = [str(name) for name in (getattr(obj, "PropertiesList", None) or ())]
    existing_set = set(existing)

    # 1. Adds: names, collisions, types, value shapes.
    added_names: list[str] = []
    added_values: dict[str, Any] = {}
    for entry in add:
        if not isinstance(entry, dict):
            raise _fail("every add entry must be an object with name and type")
        name = _check_name(entry.get("name"), what="add name")
        if name in existing_set:
            raise _fail(
                f"add collides with existing property '{name}' on "
                f"'{getattr(obj, 'Name', '<unknown>')}'"
            )
        if name in added_names:
            raise _fail(f"add lists property '{name}' twice")
        prop_type = _check_type(entry.get("type"), obj)
        if "value" in entry and entry["value"] is not None:
            _check_value_shape(prop_type, entry["value"], name)
            added_values[name] = entry["value"]
        added_names.append(name)

    # 2. Renames: existing source, free target, collisions, read-only.
    rename_pairs: list[tuple[str, str]] = []
    renamed_to: set[str] = set()
    for old, new in rename.items():
        _check_name(old, what="rename source")
        _check_name(new, what="rename target")
        if old not in existing_set:
            raise _fail(
                f"rename source '{old}' is not an existing property of "
                f"'{getattr(obj, 'Name', '<unknown>')}'"
            )
        if old == new:
            raise _fail(f"rename '{old}' -> '{new}' changes nothing")
        if new in existing_set:
            raise _fail(f"rename target '{new}' collides with an existing property")
        if new in added_names:
            raise _fail(f"rename target '{new}' collides with an added property")
        if new in renamed_to:
            raise _fail(f"two renames target the same new name '{new}'")
        _reject_read_only(obj, old, what="rename")
        rename_pairs.append((old, new))
        renamed_to.add(new)

    # 3. Expressions: final (post-rename) property names, read-only targets.
    final_names = existing_set.union(added_names).union(renamed_to)
    final_names.difference_update(old for old, _new in rename_pairs)
    expression_pairs: list[tuple[str, str]] = []
    for prop, expression in expressions.items():
        if not isinstance(prop, str) or not isinstance(expression, str):
            raise _fail("expressions must map property names to strings")
        if prop in rename:
            raise _fail(
                f"expression key '{prop}' uses a renamed-away name; use the "
                "final (post-rename) property name"
            )
        if prop not in final_names:
            raise _fail(
                f"expression key '{prop}' is not a property of "
                f"'{getattr(obj, 'Name', '<unknown>')}' after the requested changes"
            )
        if not expression.strip():
            raise _fail(f"expression for '{prop}' must be a non-empty string")
        _reject_read_only(obj, prop, what="expression")
        expression_pairs.append((prop, expression))

    return {
        "added_entries": add,
        "added_names": added_names,
        "added_values": added_values,
        "renames": rename_pairs,
        "expressions": expression_pairs,
    }


# ---------------------------------------------------------------------------
# Handler (GUI thread).
# ---------------------------------------------------------------------------


def _edit_parameters(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    doc = ctx.require_document(arguments["document"])
    obj = ctx.require_object(doc, arguments["object"])

    plan = _validate_all(obj, arguments)

    with mutation(ctx, doc, "edit_parameters", [obj]):
        for entry in plan["added_entries"]:
            obj.addProperty(entry["type"], entry["name"], _GROUP, _DOC)
            value = plan["added_values"].get(entry["name"])
            if value is not None:
                setattr(obj, entry["name"], value)
        for old, new in plan["renames"]:
            obj.renameProperty(old, new)
        for prop, expression in plan["expressions"]:
            obj.setExpression(prop, expression)

    return {
        "object": str(obj.Name),
        "added": plan["added_names"],
        "renamed": [{"from": old, "to": new} for old, new in plan["renames"]],
        "expressions": [prop for prop, _expression in plan["expressions"]],
    }


HANDLERS = {"edit_parameters": _edit_parameters}
