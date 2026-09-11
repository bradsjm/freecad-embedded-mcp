"""Focused tests for the subscription registry (PLAN section 4).

Covers: acknowledgement-first ordering, honored-filter strictness (only
requested and supported kinds, including Tasks taskIds), connection-scoped
duplicate JSON-RPC ids, bounded queues closing on overflow, disconnect
removing only that connection's streams without touching detached tasks,
and shutdown delivering the final complete result.

No timing sleeps: queues are drained synchronously and ``receive`` polls
with zero timeouts.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server import subscriptions as subs_module
from mcp_server import tasks as tasks_module
from mcp_server.protocol import (
    INVALID_PARAMS,
    SERVER_INFO,
    ProtocolError,
)
from mcp_server.subscriptions import (
    SERVER_INFO_META_KEY,
    SUBSCRIPTION_ID_META_KEY,
    SubscriptionClosed,
    SubscriptionRegistry,
)


def drain(sub) -> list[dict]:
    """Collect every currently queued message without waiting.

    Stops at a poll timeout or at stream closure, whichever comes first.
    """
    messages = []
    while True:
        try:
            message = sub.receive(timeout=0)
        except SubscriptionClosed:
            break
        if message is None:
            break
        messages.append(message)
    return messages


def make_registry(**overrides: Any) -> SubscriptionRegistry:
    """Build a registry from the source's real defaults.

    One supported resource URI (``freecad://documents``) and no supported
    ``*ListChanged`` kinds, so filter tests pin exactly what the actual
    server honors. ``overrides`` tunes one facet per scenario, e.g.
    ``queue_limit=2`` for overflow tests.
    """
    return SubscriptionRegistry(**overrides)


# -- acknowledgement -----------------------------------------------------


def test_acknowledgement_is_first_with_honored_filter() -> None:
    registry = make_registry()
    sub = registry.register(
        "conn-a",
        1,
        {
            "taskIds": ["t1"],
            "toolsListChanged": True,
            "resourcesListChanged": True,
            "resourceSubscriptions": [subs_module.DOCUMENTS_RESOURCE_URI],
        },
        principal="alice",
    )
    [ack] = drain(sub)
    assert ack == {
        "jsonrpc": "2.0",
        "method": "notifications/subscriptions/acknowledged",
        "params": {
            "notifications": {
                "taskIds": ["t1"],
                "resourceSubscriptions": [subs_module.DOCUMENTS_RESOURCE_URI],
            },
            "_meta": {SUBSCRIPTION_ID_META_KEY: 1},
        },
    }
    # Unsupported kinds are absent, not merely False.
    assert "toolsListChanged" not in ack["params"]["notifications"]
    assert "resourcesListChanged" not in ack["params"]["notifications"]
    # Nothing else was enqueued behind the acknowledgement.
    assert sub.receive(timeout=0) is None


def test_empty_filter_is_acknowledged_as_honoring_nothing() -> None:
    registry = make_registry()
    sub = registry.register("conn-a", 7, {}, principal="alice")
    [ack] = drain(sub)
    assert ack["params"]["notifications"] == {}
    assert registry.publish_task_status({"taskId": "t1", "status": "working"}) == 0
    assert registry.publish_resource_updated(subs_module.DOCUMENTS_RESOURCE_URI) == 0


# -- filter strictness ----------------------------------------------------


def test_task_notifications_reach_only_subscribed_task_ids() -> None:
    registry = make_registry()
    sub_a = registry.register("conn-a", 1, {"taskIds": ["t1"]}, principal="alice")
    sub_b = registry.register("conn-b", 1, {}, principal="bob")
    assert sub_b.receive(timeout=0)["method"] == "notifications/subscriptions/acknowledged"

    working = tasks_module.detailed_task_wire(_fake_task("t1", status="working"))
    assert registry.publish_task_status(working) == 1

    [event_a] = drain(sub_a)[1:]  # drop the acknowledgement
    assert event_a["method"] == "notifications/tasks"
    assert event_a["params"]["taskId"] == "t1"
    assert event_a["params"]["status"] == "working"
    assert event_a["params"]["_meta"][SUBSCRIPTION_ID_META_KEY] == 1
    # sub_b never asked for task notifications.
    assert sub_b.receive(timeout=0) is None

    # Strict honoring: t2 is not in sub_a's filter.
    assert (
        registry.publish_task_status(
            tasks_module.detailed_task_wire(_fake_task("t2", status="working"))
        )
        == 0
    )
    assert sub_a.receive(timeout=0) is None


def test_resource_updates_honor_only_supported_uris() -> None:
    registry = make_registry()
    sub = registry.register(
        "conn-a",
        1,
        {
            "resourceSubscriptions": [
                subs_module.DOCUMENTS_RESOURCE_URI,
                "file:///etc/passwd",
            ]
        },
        principal="alice",
    )
    [ack] = drain(sub)
    assert ack["params"]["notifications"]["resourceSubscriptions"] == [
        subs_module.DOCUMENTS_RESOURCE_URI
    ]
    assert registry.publish_resource_updated("file:///etc/passwd") == 0
    assert registry.publish_resource_updated(subs_module.DOCUMENTS_RESOURCE_URI) == 1
    [event] = drain(sub)
    assert event == {
        "jsonrpc": "2.0",
        "method": "notifications/resources/updated",
        "params": {
            "uri": subs_module.DOCUMENTS_RESOURCE_URI,
            "_meta": {SUBSCRIPTION_ID_META_KEY: 1},
        },
    }


def test_malformed_filters_are_invalid_params() -> None:
    registry = make_registry()
    for bad in [
        None,
        "taskIds",
        {"taskIds": "t1"},
        {"taskIds": ["t1", 2]},
        {"resourceSubscriptions": [""]},
        {"toolsListChanged": "yes"},
    ]:
        with pytest.raises(ProtocolError) as excinfo:
            registry.register("conn-a", 1, bad, principal="alice")
        assert excinfo.value.code == INVALID_PARAMS == -32602


# -- connection-scoped identity ------------------------------------------


def test_duplicate_jsonrpc_ids_are_isolated_per_connection() -> None:
    registry = make_registry()
    sub_a = registry.register("conn-a", 1, {"taskIds": ["t1"]}, principal="alice")
    sub_b = registry.register("conn-b", 1, {"taskIds": ["t2"]}, principal="bob")
    [ack_a] = drain(sub_a)[:1]
    [ack_b] = drain(sub_b)[:1]
    # Same JSON-RPC id, distinct streams, each stamped with its own id.
    assert ack_a["params"]["_meta"][SUBSCRIPTION_ID_META_KEY] == 1
    assert ack_b["params"]["_meta"][SUBSCRIPTION_ID_META_KEY] == 1
    assert (
        registry.publish_task_status(
            tasks_module.detailed_task_wire(_fake_task("t1", status="working"))
        )
        == 1
    )
    assert drain(sub_a)[0]["method"] == "notifications/tasks"
    assert sub_b.receive(timeout=0) is None


def test_duplicate_id_on_same_connection_rejected_until_closed() -> None:
    registry = make_registry()
    registry.register("conn-a", 1, {}, principal="alice")
    with pytest.raises(ProtocolError) as excinfo:
        registry.register("conn-a", 1, {"taskIds": ["t1"]}, principal="alice")
    assert excinfo.value.code == -32602
    # After disconnect the id may be reused on that connection.
    registry.disconnect("conn-a")
    registry.register("conn-a", 1, {"taskIds": ["t1"]}, principal="alice")


# -- overflow -------------------------------------------------------------


def test_queue_overflow_closes_instead_of_blocking() -> None:
    registry = make_registry(queue_limit=2)
    sub = registry.register("conn-a", 1, {"taskIds": ["t1"]}, principal="alice")
    # Ack fills slot 1; first event fills slot 2; next offer overflows.
    assert (
        registry.publish_task_status(
            tasks_module.detailed_task_wire(_fake_task("t1", status="working"))
        )
        == 1
    )
    assert (
        registry.publish_task_status(
            tasks_module.detailed_task_wire(_fake_task("t1", status="completed"))
        )
        == 0
    )
    assert sub.closed
    assert len(registry) == 0  # closed streams are pruned from the registry

    messages = drain(sub)
    assert [m["method"] for m in messages] == [
        "notifications/subscriptions/acknowledged",
        "notifications/tasks",
    ]
    with pytest.raises(SubscriptionClosed):
        sub.receive(timeout=0)
    # Publishing to a closed stream is a no-op, never a block.
    assert (
        registry.publish_task_status(
            tasks_module.detailed_task_wire(_fake_task("t1", status="failed"))
        )
        == 0
    )


def test_queue_byte_limit_closes_before_memory_growth() -> None:
    registry = make_registry(queue_limit=256, queue_bytes=128)
    sub = registry.register("conn-a", 1, {"taskIds": ["t1"]}, principal="alice")
    assert sub.closed
    assert len(registry) == 0


# -- disconnect -----------------------------------------------------------


def test_disconnect_closes_only_that_connection_without_final_result() -> None:
    registry = make_registry()
    sub_a = registry.register("conn-a", 1, {"taskIds": ["t1"]}, principal="alice")
    sub_b = registry.register("conn-b", 1, {"taskIds": ["t1"]}, principal="bob")
    assert sub_a.receive(timeout=0)["method"] == "notifications/subscriptions/acknowledged"
    assert sub_b.receive(timeout=0)["method"] == "notifications/subscriptions/acknowledged"
    assert registry.disconnect("conn-a") == 1
    assert registry.disconnect("conn-a") == 0  # idempotent

    messages = drain(sub_a)
    assert messages == []  # abrupt close: no final result, queue drained-empty
    with pytest.raises(SubscriptionClosed):
        sub_a.receive(timeout=0)

    # The other connection's stream is untouched and still delivers.
    assert (
        registry.publish_task_status(
            tasks_module.detailed_task_wire(_fake_task("t1", status="working"))
        )
        == 1
    )
    assert drain(sub_b)[0]["method"] == "notifications/tasks"


def test_disconnect_never_cancels_a_detached_task() -> None:
    store = tasks_module.TaskStore()
    registry = make_registry()
    record = store.create("run_fem", {}, principal="alice")
    sub = registry.register("conn-a", 1, {"taskIds": [record.task_id]}, principal="alice")
    registry.disconnect("conn-a")
    # The detached task keeps its truthful working state and cancel event.
    fetched = store.get(record.task_id, principal="alice")
    assert fetched.status == "working"
    assert not fetched.cancel_event.is_set()
    # Publishing its completion afterwards is simply not delivered anywhere.
    store.complete(record.task_id, {"ok": True}, principal="alice")
    assert registry.publish_task_status(tasks_module.detailed_task_wire(fetched)) == 0
    assert sub.closed


# -- shutdown -------------------------------------------------------------


def test_shutdown_delivers_final_complete_response_then_ends_stream() -> None:
    registry = make_registry()
    sub_a = registry.register("conn-a", 1, {"taskIds": ["t1"]}, principal="alice")
    sub_b = registry.register("conn-b", 2, {}, principal="bob")
    assert registry.shutdown() == 2
    assert registry.shutdown() == 0  # idempotent

    for sub, request_id in ((sub_a, 1), (sub_b, 2)):
        messages = drain(sub)
        finals = [m for m in messages if "result" in m]
        assert len(finals) == 1
        final = finals[0]
        assert final["jsonrpc"] == "2.0"
        assert final["id"] == request_id
        assert final["result"]["resultType"] == "complete"
        assert final["result"]["_meta"][SUBSCRIPTION_ID_META_KEY] == request_id
        assert final["result"]["_meta"][SERVER_INFO_META_KEY] == SERVER_INFO
        with pytest.raises(SubscriptionClosed):
            sub.receive(timeout=0)

    # Nothing is delivered to torn-down streams afterwards.
    assert (
        registry.publish_task_status(
            tasks_module.detailed_task_wire(_fake_task("t1", status="working"))
        )
        == 0
    )


def test_publishing_never_blocks_the_publisher() -> None:
    registry = make_registry(queue_limit=1)
    sub = registry.register(
        "conn-a", 1, {"resourceSubscriptions": [subs_module.DOCUMENTS_RESOURCE_URI]}
    )
    # Ack fills the queue; the next publish overflows and returns promptly.
    assert registry.publish_resource_updated(subs_module.DOCUMENTS_RESOURCE_URI) == 0
    assert sub.closed


# -- helpers --------------------------------------------------------------


def _fake_task(task_id: str, *, status: str) -> tasks_module.Task:
    """Minimal record for wire building without going through TaskStore."""
    record = tasks_module.Task(
        task_id=task_id,
        operation="run_script",
        args={},
        principal="alice",
        status=status,
        status_message=None,
        created_at="2026-01-01T00:00:00.000Z",
        last_updated_at="2026-01-01T00:00:00.000Z",
        ttl_ms=3_600_000,
        poll_interval_ms=500,
    )
    if status == "completed":
        record.result = {"ok": True}
    return record
