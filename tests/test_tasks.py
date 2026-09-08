"""Focused tests for the detached task store (PLAN section 4).

Covers: flat wire fields, record queryability, unknown/principal isolation,
terminal immutability and cancellation races, honest cancellation of queued
work behind event barriers, TTL measured from creation with service-tick
extension, retention/active caps, and the Tasks capability helper.

No timing sleeps: all ordering uses threading.Event barriers and an
injected fake monotonic clock.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
import sys

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server import tasks as tasks_module  # noqa: E402
from mcp_server.protocol import INVALID_PARAMS, ProtocolError, ToolError  # noqa: E402


class FakeClock:
    """Controllable monotonic clock; deterministic, no sleeps."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_store(**kwargs) -> tuple[tasks_module.TaskStore, FakeClock]:
    clock = FakeClock()
    return tasks_module.TaskStore(clock=clock, **kwargs), clock


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed


# -- wire shapes -------------------------------------------------------


def test_create_task_wire_is_flat_and_exact() -> None:
    store, _ = make_store()
    record = store.create("run_script", {"code": "6*7"}, principal="alice")
    wire = tasks_module.create_task_wire(record)
    assert wire["resultType"] == "task"
    assert set(wire) == {
        "resultType",
        "taskId",
        "status",
        "createdAt",
        "lastUpdatedAt",
        "ttlMs",
        "pollIntervalMs",
    }
    assert wire["status"] == "working"
    assert wire["ttlMs"] == 3_600_000
    assert wire["pollIntervalMs"] == 500
    parse_utc(wire["createdAt"])
    parse_utc(wire["lastUpdatedAt"])


def test_create_freezes_arguments_against_caller_mutation() -> None:
    store, _ = make_store()
    args = {"code": "6*7"}
    record = store.create("run_script", args, principal="alice")
    args["code"] = "mutated"
    assert record.args == {"code": "6*7"}


def test_record_queryable_before_task_response_sent() -> None:
    store, _ = make_store()
    record = store.create("export", {"format": "step"}, principal="alice")
    fetched = store.get(record.task_id, principal="alice")
    assert fetched is record
    assert fetched.operation == "export"


# -- identity and isolation --------------------------------------------


def test_unknown_task_id_is_invalid_params() -> None:
    store, _ = make_store()
    with pytest.raises(ProtocolError) as excinfo:
        store.get("missing")
    assert excinfo.value.code == INVALID_PARAMS == -32602


def test_principal_mismatch_is_indistinguishable_from_unknown() -> None:
    store, _ = make_store()
    record = store.create("run_fem", {}, principal="alice")
    with pytest.raises(ProtocolError) as alice_view:
        store.get(record.task_id, principal="mallory")
    with pytest.raises(ProtocolError) as unknown_view:
        store.get("no-such-task", principal="mallory")
    assert alice_view.value.code == unknown_view.value.code == -32602
    assert alice_view.value.message == unknown_view.value.message
    # The rightful principal still sees it.
    assert store.get(record.task_id, principal="alice") is record


def test_transitions_reject_foreign_principals() -> None:
    store, _ = make_store()
    record = store.create("measure", {}, principal="alice")
    with pytest.raises(ProtocolError):
        store.request_cancel(record.task_id, principal="mallory")
    assert not record.cancel_event.is_set()
    with pytest.raises(ProtocolError):
        store.complete(record.task_id, {"ok": True}, principal="mallory")
    assert record.status == "working"


# -- terminal states and races -----------------------------------------


def test_complete_sets_terminal_result_and_creation_based_ttl() -> None:
    store, clock = make_store()
    record = store.create("run_fem", {}, principal="alice")
    clock.advance(300)  # 300 s of work
    assert store.complete(
        record.task_id, {"resultType": "complete", "ok": True}, principal="alice"
    )
    wire = tasks_module.get_task_wire(record)
    assert wire["resultType"] == "complete"
    assert wire["status"] == "completed"
    assert wire["result"] == {"resultType": "complete", "ok": True}
    assert "error" not in wire
    # ttl is measured from creation: elapsed + 1 h terminal window.
    assert wire["ttlMs"] == (300 + 3_600) * 1_000


def test_fail_records_jsonrpc_error_and_is_terminal() -> None:
    store, clock = make_store()
    record = store.create("run_script", {}, principal="alice")
    clock.advance(7)
    store.fail(
        record.task_id,
        {"code": -32603, "message": "boom", "data": {"trace": "..."}},
        principal="alice",
    )
    wire = tasks_module.detailed_task_wire(record)
    assert wire["status"] == "failed"
    assert wire["error"] == {
        "code": -32603,
        "message": "boom",
        "data": {"trace": "..."},
    }
    assert "result" not in wire


def test_fail_accepts_protocol_error_instances() -> None:
    store, _ = make_store()
    record = store.create("export", {}, principal=None)
    store.fail(record.task_id, ProtocolError(-32602, "bad args"))
    assert record.error == {"code": -32602, "message": "bad args"}


def test_terminal_state_is_immutable_against_late_writers() -> None:
    store, _ = make_store()
    record = store.create("run_script", {}, principal="alice")
    assert store.complete(record.task_id, {"stdout": "42"}, principal="alice")
    # Every later writer loses the race against the terminal state.
    assert not store.fail(
        record.task_id, {"code": -32603, "message": "late"}, principal="alice"
    )
    assert not store.finalize_cancelled(record.task_id, principal="alice")
    assert not store.request_cancel(record.task_id, principal="alice")
    assert record.status == "completed"
    assert record.result == {"stdout": "42"}
    assert record.error is None
    assert not record.cancel_event.is_set()


def test_cancellation_request_while_running_is_honest() -> None:
    store, _ = make_store()
    record = store.create("run_fem", {}, principal="alice")
    assert store.request_cancel(record.task_id, principal="alice") is True
    # Still truthfully working; only the event and message changed.
    assert record.status == "working"
    assert record.status_message == "Cancellation requested"
    assert record.cancel_event.is_set()
    wire = tasks_module.detailed_task_wire(record)
    assert wire["status"] == "working"
    assert wire["statusMessage"] == "Cancellation requested"


def test_real_completion_beats_pending_cancellation() -> None:
    store, clock = make_store()
    record = store.create("run_script", {}, principal="alice")
    store.request_cancel(record.task_id, principal="alice")
    clock.advance(2)
    # Work finished before cancellation took effect: real result wins.
    assert store.complete(
        record.task_id, {"stdout": "42"}, principal="alice", status_message="done"
    )
    assert record.status == "completed"
    assert record.result == {"stdout": "42"}
    assert record.status_message == "done"
    # A racing cancel-finalizer must not flip the completed task.
    assert not store.finalize_cancelled(record.task_id, principal="alice")
    assert record.status == "completed"


def test_tasks_cancel_on_terminal_task_changes_nothing() -> None:
    store, _ = make_store()
    record = store.create("measure", {}, principal="alice")
    store.fail(record.task_id, {"code": -32603, "message": "x"}, principal="alice")
    before = tasks_module.detailed_task_wire(record)
    # tasks/cancel acknowledges terminal tasks without changing them.
    assert store.request_cancel(record.task_id, principal="alice") is False
    assert tasks_module.detailed_task_wire(record) == before


# -- queued cancellation behind event barriers -------------------------


def _queued_runner(
    store: tasks_module.TaskStore,
    record: tasks_module.Task,
    cancel_observed: threading.Event,
    gate: threading.Event,
    executed: list[bool],
) -> None:
    """Mimics the dispatcher wrapper around a queued GUI job."""
    cancel_observed.wait(timeout=5)
    if record.cancel_event.is_set():
        # A cancelled queued job never enters FreeCAD.
        store.finalize_cancelled(
            record.task_id,
            principal="alice",
            status_message="Cancelled before execution",
        )
        return
    gate.wait(timeout=5)
    executed.append(True)
    store.complete(record.task_id, {"ok": True}, principal="alice")


def test_queued_task_cancelled_before_execution_never_runs() -> None:
    store, _ = make_store()
    record = store.create("run_fem", {}, principal="alice")
    cancel_observed = threading.Event()
    gate = threading.Event()
    executed: list[bool] = []
    worker = threading.Thread(
        target=_queued_runner,
        args=(store, record, cancel_observed, gate, executed),
    )
    worker.start()
    store.request_cancel(record.task_id, principal="alice")
    cancel_observed.set()
    gate.set()
    worker.join(timeout=5)
    assert not executed
    assert record.status == "cancelled"
    assert record.status_message == "Cancelled before execution"
    wire = tasks_module.detailed_task_wire(record)
    assert wire["status"] == "cancelled"
    assert "result" not in wire and "error" not in wire


def test_completed_task_survives_cancel_requested_during_run() -> None:
    store, _ = make_store()
    record = store.create("run_script", {}, principal="alice")
    release = threading.Event()
    executed: list[bool] = []

    def runner() -> None:
        release.wait(timeout=5)
        if record.cancel_event.is_set():
            # A safe boundary abandoned remaining work.
            store.finalize_cancelled(
                record.task_id, principal="alice", status_message="abandoned"
            )
            return
        executed.append(True)
        store.complete(record.task_id, {"stdout": "42"}, principal="alice")

    worker = threading.Thread(target=runner)
    worker.start()
    # Let the work finish first; only then request cancellation.
    release.set()
    worker.join(timeout=5)
    assert store.request_cancel(record.task_id, principal="alice") is False
    assert executed == [True]
    assert record.status == "completed"
    assert record.result == {"stdout": "42"}


# -- TTL lifecycle ------------------------------------------------------


def test_terminal_records_expire_at_creation_plus_ttl() -> None:
    store, clock = make_store()
    record = store.create("export", {}, principal="alice")
    clock.advance(120)
    store.complete(record.task_id, {"ok": True}, principal="alice")
    clock.advance(3_600 - 1)  # one second before the terminal TTL elapses
    assert store.get(record.task_id, principal="alice") is record
    clock.advance(1)
    with pytest.raises(ProtocolError):
        store.get(record.task_id, principal="alice")


def test_working_records_extend_ttl_on_service_tick() -> None:
    store, clock = make_store()
    record = store.create("run_fem", {}, principal="alice")
    clock.advance(1_800)
    store.service_tick()
    assert record.ttl_ms == (1_800 + 3_600) * 1_000
    assert record.status == "working"
    # Long-running work past the original window stays queryable.
    clock.advance(100_000)
    store.service_tick()
    assert store.get(record.task_id, principal="alice") is record
    # Terminal transition later measures from creation, not the tick.
    clock.advance(3_600)
    store.complete(record.task_id, {"ok": True}, principal="alice")
    assert record.ttl_ms == (1_800 + 100_000 + 3_600 + 3_600) * 1_000


def test_sweep_runs_on_access_and_tick_never_expires_working_tasks() -> None:
    store, clock = make_store()
    finished = store.create("export", {}, principal="alice")
    store.complete(finished.task_id, {}, principal="alice")
    running = store.create("run_fem", {}, principal="alice")
    clock.advance(10 * 3_600)  # far past every TTL, with no ticks in between
    store.service_tick()
    with pytest.raises(ProtocolError):
        store.get(finished.task_id, principal="alice")
    assert store.get(running.task_id, principal="alice") is running


# -- caps ---------------------------------------------------------------


def test_active_task_cap_rejects_with_busy_tool_error() -> None:
    store, _ = make_store(max_active=2)
    first = store.create("run_script", {}, principal="alice")
    second = store.create("run_fem", {}, principal="alice")
    with pytest.raises(ToolError) as excinfo:
        store.create("export", {}, principal="alice")
    assert excinfo.value.code == "SERVER_BUSY"
    assert excinfo.value.details["reason"] == "active_task_limit"
    # Finishing one frees an accepted slot; no running work was evicted.
    store.complete(first.task_id, {}, principal="alice")
    third = store.create("measure", {}, principal="alice")
    assert store.get(second.task_id, principal="alice") is second
    assert store.get(third.task_id, principal="alice") is third


def test_retention_cap_evicts_oldest_terminal_only() -> None:
    store, _ = make_store(max_retained=3)
    oldest = store.create("run_script", {}, principal="alice").task_id
    store.complete(oldest, {"n": 1}, principal="alice")
    middle = store.create("run_script", {}, principal="alice").task_id
    store.complete(middle, {"n": 2}, principal="alice")
    running = store.create("run_fem", {}, principal="alice")
    # Oldest terminal record is evicted; the running one is never touched.
    newest = store.create("measure", {}, principal="alice")
    assert len(store) == 3
    with pytest.raises(ProtocolError):
        store.get(oldest, principal="alice")
    assert store.get(middle, principal="alice")
    assert store.get(running.task_id, principal="alice") is running
    assert store.get(newest.task_id, principal="alice") is newest


def test_retention_cap_with_no_terminal_records_rejects_busy() -> None:
    store, _ = make_store(max_retained=2)
    store.create("run_script", {}, principal="alice")
    store.create("run_fem", {}, principal="alice")
    with pytest.raises(ToolError) as excinfo:
        store.create("measure", {}, principal="alice")
    assert excinfo.value.code == "SERVER_BUSY"
    assert excinfo.value.details["reason"] == "retention_limit"


# -- Tasks capability helper --------------------------------------------


def test_require_tasks_capability_accepts_declared_extension() -> None:
    tasks_module.require_tasks_capability(
        {"extensions": {"io.modelcontextprotocol/tasks": {}}}
    )
    # Other extensions alongside do not interfere.
    tasks_module.require_tasks_capability(
        {
            "elicitation": {"form": {}},
            "extensions": {
                "io.modelcontextprotocol/tasks": {},
                "com.example/other": {"x": 1},
            },
        }
    )


@pytest.mark.parametrize(
    "capabilities",
    [
        None,
        {},
        {"extensions": {}},
        {"extensions": {"io.modelcontextprotocol/tasks": "yes"}},
        {"io.modelcontextprotocol/tasks": {}},
    ],
)
def test_require_tasks_capability_rejects_missing_extension(
    capabilities: object,
) -> None:
    with pytest.raises(ProtocolError) as excinfo:
        tasks_module.require_tasks_capability(capabilities)
    assert excinfo.value.code == -32021
    assert excinfo.value.data == {
        "requiredCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}
    }


# -- status vocabulary ---------------------------------------------------


def test_status_vocabulary_matches_released_schema() -> None:
    assert tasks_module.TASK_STATUSES == (
        "working",
        "input_required",
        "completed",
        "failed",
        "cancelled",
    )
    assert tasks_module.TERMINAL_STATUSES == {"completed", "failed", "cancelled"}
    assert tasks_module.TASK_ELIGIBLE_OPERATIONS == frozenset(
        {"run_script", "run_fem", "export", "measure"}
    )
