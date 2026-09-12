"""Focused tests for the ``edit_parameters`` MCP v2 tool.

Runs on the host without FreeCAD. ``mcp_server.tools.parameters`` consumes
``mcp_server.object_validation.mutation`` (the real gate) with fake
FreeCAD-like doubles: a fake object with dynamic-property bookkeeping, a fake
document implementing the transaction calls the gate needs, and a fake ctx
implementing the agreed server contract.
"""

import sys
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.protocol import VALIDATION_FAILED, ToolError
from mcp_server.tools import parameters


class FakeObj:
    # probes["object.property_status"]: property containers report
    # PropertiesList and type ids via getTypeIdOfProperty, and a
    # missing property raises AttributeError.
    """Dynamic-property bookkeeping without FreeCAD."""

    def __init__(
        self,
        name="Box",
        *,
        properties=(),
        read_only=(),
        builtin=(),
        fail_add=None,
        supported=None,
    ):
        self.Name = name
        self.Label = name
        self.State = []
        self.InList = []
        self._props: dict[str, dict] = {}
        self._order: list[str] = []
        self._modes = {prop: ["ReadOnly"] for prop in read_only}
        self._builtin = set(builtin)
        self.fail_add = fail_add
        self.expressions: dict[str, str] = {}
        self.ops: list[tuple] = []
        for prop in properties:
            self._props[prop] = {"type": "App::PropertyLength", "value": None}
            self._order.append(prop)
        self._supported = list(parameters._PROPERTY_TYPES) if supported is None else list(supported)

    @property
    def PropertiesList(self):
        return list(self._order)

    def supportedProperties(self):
        return self._supported

    def getEditorMode(self, name):
        return list(self._modes.get(name, []))

    def addProperty(self, prop_type, name, group="", doc="", *args, **kwargs):
        self.ops.append(("add", name, prop_type, group))
        if name in self._props:
            raise RuntimeError(f"property '{name}' already exists")
        if self.fail_add == name:
            raise RuntimeError(f"cannot add '{name}' (simulated FreeCAD failure)")
        self._props[name] = {"type": prop_type, "value": None}
        self._order.append(name)

    def renameProperty(self, old, new):
        self.ops.append(("rename", old, new))
        if old in self._builtin or old not in self._props:
            raise RuntimeError(f"cannot rename '{old}': not a dynamic property")
        if new in self._props:
            raise RuntimeError(f"property '{new}' already exists")
        self._props[new] = self._props.pop(old)
        self._order[self._order.index(old)] = new

    def setExpression(self, prop, expression):
        self.ops.append(("expression", prop, expression))
        self.expressions[prop] = expression

    def snapshot(self):
        return (
            dict(self._props),
            list(self._order),
            dict(self.expressions),
        )

    def restore(self, snapshot):
        props, order, expressions = snapshot
        self._props = dict(props)
        self._order = list(order)
        self.expressions = dict(expressions)


class FakeDoc:
    def __init__(self, obj):
        self.obj = obj
        self.Name = "Doc"
        self.HasPendingTransaction = False
        self.UndoMode = 0
        self.generation = 1
        self.transactions: list[tuple] = []
        self.recompute_count = 0
        self._snapshot = None

    def openTransaction(self, label):
        self.transactions.append(("open", label))
        self._snapshot = self.obj.snapshot()

    def commitTransaction(self):
        self.transactions.append(("commit",))

    def abortTransaction(self):
        self.transactions.append(("abort",))
        self.obj.restore(self._snapshot)

    def recompute(self):
        self.recompute_count += 1

    def getObject(self, name):
        return self.obj if name == self.obj.Name else None


class FakeApp:
    def getActiveTransaction(self):
        return None


class FakeCtx:
    def __init__(self, obj):
        self.obj = obj
        self.doc = FakeDoc(obj)
        self.App = FakeApp()
        self.idle: list[str] = []
        self.busy: str | None = None

    def require_document(self, name):
        if name != self.doc.Name:
            raise ToolError("DOCUMENT_NOT_FOUND", f"document '{name}' not found", None)
        return self.doc

    def require_object(self, doc, name):
        if name != self.obj.Name:
            raise ToolError("OBJECT_NOT_FOUND", f"object '{name}' not found", None)
        return self.obj

    def document_generation(self, doc):
        return int(doc.generation)

    def check_document_idle(self, doc):
        self.idle.append(doc.Name)
        if self.busy:
            raise ToolError("SERVER_BUSY", f"document busy: {self.busy}", None)


def call(ctx, arguments):
    return parameters.HANDLERS["edit_parameters"](ctx, arguments)


def base(document="Doc", object="Box"):
    return {"document": document, "object": object}


# ---------------------------------------------------------------------------
# Validate-all-first: nothing mutates when any request is invalid.
# ---------------------------------------------------------------------------
def test_second_invalid_add_leaves_first_unapplied_and_never_opens_a_transaction():
    obj = FakeObj()
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(
            ctx,
            {
                **base(),
                "add": [
                    {"name": "Depth", "type": "App::PropertyLength", "value": 10},
                    {"name": "Boom", "type": "App::PropertyNonsense", "value": 1},
                ],
            },
        )
    assert excinfo.value.code == VALIDATION_FAILED
    assert obj.ops == []  # no property was touched
    assert ctx.doc.transactions == []  # no transaction was ever opened
    assert ctx.doc.recompute_count == 0


def test_second_invalid_add_name_rejected_before_any_mutation():
    obj = FakeObj()
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(
            ctx,
            {
                **base(),
                "add": [
                    {"name": "Depth", "type": "App::PropertyLength", "value": 10},
                    {"name": "not an identifier", "type": "App::PropertyFloat"},
                ],
            },
        )
    assert excinfo.value.code == VALIDATION_FAILED
    assert obj.ops == []
    assert ctx.doc.transactions == []
    assert "Depth" not in obj.PropertiesList


def test_add_collision_with_existing_property_rejected_up_front():
    obj = FakeObj(properties=["Length"])
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "add": [{"name": "Length", "type": "App::PropertyFloat"}]})
    assert "collides" in excinfo.value.message
    assert obj.ops == []


def test_unsupported_property_type_rejected_up_front():
    obj = FakeObj()
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "add": [{"name": "Weird", "type": "App::PropertyNonsense"}]})
    assert "unsupported property type" in excinfo.value.message
    assert obj.ops == []

    # The object's own supportedProperties() narrows the tool allow-list.
    limited = FakeObj(supported=("App::PropertyBool",))
    ctx = FakeCtx(limited)
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "add": [{"name": "Depth", "type": "App::PropertyFloat"}]})
    assert "not supported by this object" in excinfo.value.message
    assert limited.ops == []


def test_value_shape_mismatch_rejected_up_front():
    obj = FakeObj()
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(
            ctx,
            {
                **base(),
                "add": [{"name": "Count", "type": "App::PropertyInteger", "value": True}],
            },
        )
    assert "needs an integer" in excinfo.value.message
    assert obj.ops == []


def test_non_finite_float_value_rejected_up_front():
    obj = FakeObj()
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError):
        call(
            ctx,
            {
                **base(),
                "add": [{"name": "Depth", "type": "App::PropertyLength", "value": 1e400}],
            },
        )
    assert obj.ops == []


def test_rename_source_missing_rejected_up_front():
    obj = FakeObj(properties=["Length"])
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "rename": {"Ghost": "New"}})
    assert "not an existing property" in excinfo.value.message
    assert obj.ops == []


def test_rename_collision_with_existing_property_rejected_up_front():
    obj = FakeObj(properties=["Length", "Other"])
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "rename": {"Other": "Length"}})
    assert "collides with an existing property" in excinfo.value.message
    assert obj.ops == []


def test_two_renames_to_same_target_rejected_up_front():
    obj = FakeObj(properties=["A", "B"])
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "rename": {"A": "C", "B": "C"}})
    assert "two renames" in excinfo.value.message
    assert obj.ops == []


def test_read_only_rename_rejected_up_front():
    obj = FakeObj(properties=["Locked"], read_only=["Locked"])
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "rename": {"Locked": "Unlocked"}})
    assert "read-only" in excinfo.value.message
    assert obj.ops == []


def test_expression_on_read_only_property_rejected_up_front():
    obj = FakeObj(properties=["Locked"], read_only=["Locked"])
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "expressions": {"Locked": "1 + 1"}})
    assert "read-only" in excinfo.value.message
    assert obj.ops == []


def test_expression_key_using_renamed_away_name_rejected():
    obj = FakeObj(properties=["Old"])
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(
            ctx,
            {**base(), "rename": {"Old": "New"}, "expressions": {"Old": "1 + 1"}},
        )
    assert "renamed-away" in excinfo.value.message
    assert obj.ops == []


def test_expression_key_unknown_rejected():
    obj = FakeObj(properties=["Length"])
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "expressions": {"Missing": "1 + 1"}})
    assert "not a property" in excinfo.value.message
    assert obj.ops == []


def test_dotted_expression_path_uses_existing_property_root():
    obj = FakeObj(properties=["Placement"])
    ctx = FakeCtx(obj)

    result = call(
        ctx,
        {**base(), "expressions": {".Placement.Base.y": "Params.CenterY"}},
    )

    assert result["expressions"] == [".Placement.Base.y"]
    assert ("expression", ".Placement.Base.y", "Params.CenterY") in obj.ops


def test_dotted_expression_path_with_unknown_root_is_rejected():
    obj = FakeObj(properties=["Placement"])
    ctx = FakeCtx(obj)

    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "expressions": {"Missing.Base.y": "1"}})

    assert "not a property" in excinfo.value.message
    assert obj.ops == []


def test_dotted_expression_path_checks_non_container_read_only_root():
    obj = FakeObj(properties=["Locked"], read_only=["Locked"])
    ctx = FakeCtx(obj)

    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "expressions": {"Locked.Base.y": "1"}})

    assert "read-only" in excinfo.value.message
    assert obj.ops == []


# ---------------------------------------------------------------------------
# Atomicity inside the shared mutation gate.
# ---------------------------------------------------------------------------


def test_free_cad_failure_inside_gate_rolls_back_first_add():
    obj = FakeObj(fail_add="Boom")
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(
            ctx,
            {
                **base(),
                "add": [
                    {"name": "Depth", "type": "App::PropertyLength", "value": 10},
                    {"name": "Boom", "type": "App::PropertyFloat", "value": 1},
                ],
            },
        )
    assert excinfo.value.code == VALIDATION_FAILED
    # Wait: fail_add raises only for Boom, but prevalidation passed both, so
    # Depth IS added inside the gate before Boom fails and everything rolls
    # back: Depth must not survive, the transaction aborted, nothing committed.
    assert obj.ops[0][0] == "add"  # Depth was attempted inside the gate
    assert "Depth" not in obj.PropertiesList  # rolled back
    assert ("abort",) in ctx.doc.transactions
    assert ("commit",) not in ctx.doc.transactions
    assert ctx.doc.recompute_count == 1  # the rollback's own recompute


def test_renaming_a_builtin_property_fails_atomically():
    obj = FakeObj(properties=["Length"], builtin=["Length"])
    ctx = FakeCtx(obj)
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "rename": {"Length": "Len"}})
    assert excinfo.value.code == VALIDATION_FAILED
    assert "rolled back" in excinfo.value.message
    assert ("abort",) in ctx.doc.transactions
    assert ("commit",) not in ctx.doc.transactions
    assert obj.PropertiesList == ["Length"]


def test_invalid_expression_reference_fails_recompute_and_rolls_back():
    class FailingRecomputeDoc(FakeDoc):
        def recompute(self):
            self.recompute_count += 1
            self.obj.State = ["Invalid"]
            raise RuntimeError("expression references unknown 'Nope'")

    obj = FakeObj(properties=["Length"])
    doc = FailingRecomputeDoc(obj)
    ctx = FakeCtx(obj)
    ctx.doc = doc
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "expressions": {"Length": "Nope + 1"}})
    assert excinfo.value.code == VALIDATION_FAILED
    # The recompute fails again during the rollback, so the error reports
    # the rollback failure explicitly instead of claiming a valid rollback.
    assert excinfo.value.details["rollbackFailed"] is True
    assert excinfo.value.details["rollbackStage"] == "recompute"
    assert "Nope" in excinfo.value.details["originalError"]
    assert ("abort",) in doc.transactions
    assert ("commit",) not in doc.transactions
    assert obj.expressions == {}  # aborted before the rollback recompute


def test_rollback_after_failed_expression_recomputes_native_stale_state():
    class NativeAbortDoc(FakeDoc):
        """Native FreeCAD abort: property changes are undone but the
        Touched/Invalid cached state survives until a recompute."""

        def __init__(self, obj):
            super().__init__(obj)
            self._recomputes = 0

        def abortTransaction(self):
            self.transactions.append(("abort",))
            self.obj.restore(self._snapshot)
            self.obj.State = ["Touched", "Invalid"]

        def recompute(self):
            self.recompute_count += 1
            self._recomputes += 1
            if self._recomputes == 1:
                self.obj.State = ["Invalid"]
                raise RuntimeError("expression references unknown 'Nope'")
            self.obj.State = []

    obj = FakeObj(properties=["Length"])
    doc = NativeAbortDoc(obj)
    ctx = FakeCtx(obj)
    ctx.doc = doc
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "expressions": {"Length": "Nope + 1"}})
    assert excinfo.value.code == VALIDATION_FAILED
    assert "rolled back" in excinfo.value.message
    assert "Nope" in excinfo.value.message
    assert doc.transactions[-1] == ("abort",)
    assert doc.recompute_count == 2  # gate recompute + rollback recompute
    assert ("commit",) not in doc.transactions
    assert obj.expressions == {}  # original properties restored
    assert obj.State == []  # rollback recompute left the object usable


def test_idle_gate_rejects_before_any_mutation():
    obj = FakeObj(properties=["Length"])
    ctx = FakeCtx(obj)
    ctx.busy = "run_fem"
    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "expressions": {"Length": "2"}})
    assert excinfo.value.code == "SERVER_BUSY"
    assert obj.ops == []
    assert ctx.doc.transactions == []


# ---------------------------------------------------------------------------
# Successful flow: add -> rename -> expression, then recompute and commit.
# ---------------------------------------------------------------------------


def test_successful_flow_applies_add_rename_expression_in_order():
    obj = FakeObj(properties=["Old"])
    ctx = FakeCtx(obj)
    result = call(
        ctx,
        {
            **base(),
            "add": [{"name": "Depth", "type": "App::PropertyLength", "value": 10}],
            "rename": {"Old": "New"},
            "expressions": {"New": "Depth * 2"},
        },
    )
    assert obj.ops == [
        ("add", "Depth", "App::PropertyLength", parameters._GROUP),
        ("rename", "Old", "New"),
        ("expression", "New", "Depth * 2"),
    ]
    assert obj.PropertiesList == ["New", "Depth"]
    assert obj.expressions == {"New": "Depth * 2"}
    assert ctx.doc.recompute_count == 1
    assert ctx.doc.transactions[-1] == ("commit",)
    assert ctx.idle == ["Doc"]
    assert result == {
        "document": "Doc",
        "generation": 1,
        "object": "Box",
        "added": ["Depth"],
        "renamed": [{"from": "Old", "to": "New"}],
        "expressions": ["New"],
        "clearedExpressions": [],
        "applied": ["add:Depth", "rename:Old->New", "expression:New", "Box"],
    }


def test_added_property_without_value_keeps_free_cad_default():
    obj = FakeObj()
    ctx = FakeCtx(obj)
    result = call(ctx, {**base(), "add": [{"name": "Note", "type": "App::PropertyString"}]})
    assert result["added"] == ["Note"]
    assert obj._props["Note"]["value"] is None
    assert ("commit",) in ctx.doc.transactions


def test_expression_may_target_a_property_added_in_the_same_call():
    obj = FakeObj()
    ctx = FakeCtx(obj)
    call(
        ctx,
        {
            **base(),
            "add": [{"name": "Width", "type": "App::PropertyFloat", "value": 3.5}],
            "expressions": {"Width": "2 * 1.75"},
        },
    )
    assert obj.expressions == {"Width": "2 * 1.75"}
    assert ctx.doc.transactions[-1] == ("commit",)


# ---------------------------------------------------------------------------
# clear_expressions: removal after renames, before sets.
# ---------------------------------------------------------------------------


def test_successful_clear_removes_expression_after_renames_before_sets():
    obj = FakeObj(properties=["Old", "Other"])
    obj.expressions["Old"] = "1"
    ctx = FakeCtx(obj)

    result = call(
        ctx,
        {
            **base(),
            "rename": {"Old": "New"},
            "clear_expressions": ["New"],
            "expressions": {"Other": "2"},
        },
    )

    assert obj.ops == [
        ("rename", "Old", "New"),
        ("expression", "New", None),
        ("expression", "Other", "2"),
    ]
    assert obj.expressions["New"] is None
    assert obj.expressions["Old"] == "1"
    assert obj.expressions["Other"] == "2"
    assert result["clearedExpressions"] == ["New"]
    assert ctx.doc.transactions[-1] == ("commit",)


def test_clear_and_set_of_the_same_property_is_rejected():
    obj = FakeObj(properties=["Length"])
    ctx = FakeCtx(obj)

    with pytest.raises(ToolError) as excinfo:
        call(
            ctx,
            {
                **base(),
                "clear_expressions": ["Length"],
                "expressions": {"Length": "2"},
            },
        )

    assert excinfo.value.code == VALIDATION_FAILED
    assert "cleared and given an expression" in excinfo.value.message
    assert obj.ops == []
    assert ctx.doc.transactions == []


def test_clear_of_a_renamed_away_name_is_rejected():
    obj = FakeObj(properties=["Old"])
    ctx = FakeCtx(obj)

    with pytest.raises(ToolError) as excinfo:
        call(
            ctx,
            {**base(), "rename": {"Old": "New"}, "clear_expressions": ["Old"]},
        )

    assert "renamed-away name" in excinfo.value.message
    assert obj.ops == []


def test_clear_of_an_unknown_final_name_is_rejected():
    obj = FakeObj(properties=["Length"])
    ctx = FakeCtx(obj)

    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "clear_expressions": ["Ghost"]})

    assert "not a property" in excinfo.value.message
    assert obj.ops == []


def test_clear_targets_read_only_property_is_rejected():
    obj = FakeObj(properties=["Length"], read_only=["Length"])
    ctx = FakeCtx(obj)

    with pytest.raises(ToolError) as excinfo:
        call(ctx, {**base(), "clear_expressions": ["Length"]})

    assert "read-only" in excinfo.value.message
    assert obj.ops == []


def test_clear_lists_a_property_twice_is_rejected():
    obj = FakeObj(properties=["Length"])
    ctx = FakeCtx(obj)

    with pytest.raises(ToolError) as excinfo:
        call(
            ctx,
            {**base(), "clear_expressions": ["Length", "Length"]},
        )

    assert "twice" in excinfo.value.message
    assert obj.ops == []


def test_clear_may_target_a_property_added_in_the_same_call():
    obj = FakeObj()
    ctx = FakeCtx(obj)

    result = call(
        ctx,
        {
            **base(),
            "add": [{"name": "Width", "type": "App::PropertyFloat", "value": 3.5}],
            "clear_expressions": ["Width"],
        },
    )

    assert obj.ops == [
        ("add", "Width", "App::PropertyFloat", parameters._GROUP),
        ("expression", "Width", None),
    ]
    assert result["clearedExpressions"] == ["Width"]


def test_cleared_expressions_reported_in_request_order():
    obj = FakeObj(properties=["A", "B", "C"])
    ctx = FakeCtx(obj)

    result = call(
        ctx,
        {**base(), "clear_expressions": ["C", "A"]},
    )

    assert result["clearedExpressions"] == ["C", "A"]


# ---------------------------------------------------------------------------
# Response parity: document, generation and applied operation labels.
# ---------------------------------------------------------------------------


def test_response_reports_document_generation_and_applied_labels_in_order():
    obj = FakeObj(properties=["Old", "Other"])
    obj.expressions["Old"] = "1"
    ctx = FakeCtx(obj)

    result = call(
        ctx,
        {
            **base(),
            "add": [{"name": "Depth", "type": "App::PropertyLength", "value": 10}],
            "rename": {"Old": "New"},
            "clear_expressions": ["New"],
            "expressions": {"Other": "2"},
        },
    )

    assert result["document"] == "Doc"
    assert result["generation"] == 1
    # Labels follow the execution order the object records (adds, renames,
    # clears, sets); the gate then appends the names it mutated.
    assert result["applied"] == [
        "add:Depth",
        "rename:Old->New",
        "clear:New",
        "expression:Other",
        "Box",
    ]
    assert obj.ops == [
        ("add", "Depth", "App::PropertyLength", parameters._GROUP),
        ("rename", "Old", "New"),
        ("expression", "New", None),
        ("expression", "Other", "2"),
    ]


def test_response_validates_against_the_output_schema():
    from mcp_server.protocol import validate_schema

    obj = FakeObj(properties=["Old"])
    ctx = FakeCtx(obj)

    result = call(
        ctx,
        {**base(), "rename": {"Old": "New"}, "expressions": {"New": "2"}},
    )

    validate_schema(result, parameters.TOOL_DEFINITIONS[0]["outputSchema"])
    assert result["applied"] == ["rename:Old->New", "expression:New", "Box"]


# ---------------------------------------------------------------------------
# Tool definition sanity.
# ---------------------------------------------------------------------------


def test_definition_is_finite_and_bound():
    from mcp_server.protocol import check_schema

    definition = parameters.TOOL_DEFINITIONS[0]
    check_schema(definition["inputSchema"])
    check_schema(definition["outputSchema"])
    assert definition["inputSchema"]["additionalProperties"] is False
    assert definition["outputSchema"]["additionalProperties"] is False
