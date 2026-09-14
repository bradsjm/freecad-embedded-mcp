"""Detached task store for the Tasks extension (``io.modelcontextprotocol/tasks``).

Implements PLAN section 4: locked task records with immutable terminal
states, principal binding, honest cooperative cancellation, creation-based
TTL and retention caps. Pure stdlib; no FreeCAD, GUI, or network imports.

Wire shapes (released ``modelcontextprotocol/ext-tasks``
``schema/2026-07-28``):

* ``CreateTaskResult`` is ``Result & Task`` flattened::
    ``{"resultType": "task", "taskId", "status", "statusMessage"?,``
    ``"createdAt", "lastUpdatedAt", "ttlMs", "pollIntervalMs"}``
* ``GetTaskResult`` is ``Result & DetailedTask`` flattened with
  ``resultType: "complete"``; ``completed`` tasks inline their terminal
  ``result`` and ``failed`` tasks inline the JSON-RPC ``error`` object.
* ``notifications/tasks`` params carry the same flat ``DetailedTask``.

This module only produces the DetailedTask fields; callers stamp
``resultType``/``_meta`` envelope metadata through :mod:`.protocol`.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .protocol import (
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    SERVER_BUSY,
    ProtocolError,
    ToolError,
)

TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"

#: Capability object a request must declare under ``clientCapabilities``.
TASKS_REQUIRED_CAPABILITY = {"extensions": {TASKS_EXTENSION_ID: {}}}

#: Fixed task-eligible operations (PLAN section 4).
TASK_ELIGIBLE_OPERATIONS = frozenset({"run_script", "run_fem", "export", "measure"})

TASK_STATUSES = ("working", "input_required", "completed", "failed", "cancelled")
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

DEFAULT_TTL_MS = 3_600_000
TERMINAL_TTL_MS = 3_600_000
POLL_INTERVAL_MS = 500

MAX_RETAINED_TASKS = 1024
MAX_ACTIVE_TASKS = 32

# Aggregate retained-memory budget across every stored task (args plus
# terminal payloads), measured as canonical JSON bytes.
MAX_RETAINED_TASK_BYTES = 67_108_864

# Every terminal transition must fit the budget even after the retained
# args are dropped, so creation reserves this much headroom for a bounded
# terminal record. The dropped-payload substitute echoes the budget and
# byte counts bounded to the 2**63 ceiling (a larger dropped payload is
# reported clamped with droppedBytesClamped), so the bounded substitute and
# status message fit within the terminal reserve for every accepted
# configuration. Budgets outside the accepted range are rejected at
# construction instead of silently overflowing.
_TERMINAL_RESERVE_BYTES = 768
_MIN_RETAINED_TASK_BYTES = 1024
_MAX_RETAINED_TASK_BYTES = 2**63 - 1
_MAX_COUNTED_BYTES = 2**63 - 1
_MAX_STATUS_MESSAGE_BYTES = 256
_STATUS_TRUNCATION_SUFFIX = "...[truncated]"

CANCEL_REQUESTED_MESSAGE = "Cancellation requested"

_UNKNOWN_TASK_MESSAGE = "Unknown or expired task"


def _utc_now_iso() -> str:
    now = datetime.now(UTC)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_status_message(value: str | None) -> str | None:
    """Return a deterministic canonical-JSON bounded status message."""

    if value is None:
        return None
    text = str(value)
    if _canonical_json_size({"statusMessage": text}) <= _MAX_STATUS_MESSAGE_BYTES:
        return text
    suffix = _STATUS_TRUNCATION_SUFFIX
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle] + suffix
        if _canonical_json_size({"statusMessage": candidate}) <= _MAX_STATUS_MESSAGE_BYTES:
            low = middle
        else:
            high = middle - 1
    return text[:low] + suffix


def _status_message_size(value: str | None) -> int:
    """Return the canonical retained-byte size of one status message."""

    return _canonical_json_size({"statusMessage": value}) if value else 0


def declares_tasks_capability(client_capabilities: object) -> bool:
    """True when the request declared the Tasks extension as an object.

    ``client_capabilities`` is the
    ``io.modelcontextprotocol/clientCapabilities`` object of the current
    request (may be ``None``). Extensions live under the ``extensions`` key
    and an extension's value must be an object.

    The server selects the detached task path with this exact predicate, so
    a client whose extension value is not an object can never be handed a
    task it is unable to poll or cancel.
    """

    if not isinstance(client_capabilities, dict):
        return False
    extensions = client_capabilities.get("extensions")
    if not isinstance(extensions, dict):
        return False
    return isinstance(extensions.get(TASKS_EXTENSION_ID), dict)


def require_tasks_capability(client_capabilities: object) -> None:
    """Raise ``-32021`` unless the request declared the Tasks extension."""

    if not declares_tasks_capability(client_capabilities):
        raise ProtocolError(
            MISSING_REQUIRED_CLIENT_CAPABILITY,
            "This request requires the MCP Tasks extension capability",
            {"requiredCapabilities": TASKS_REQUIRED_CAPABILITY},
        )


@dataclass(slots=True)
class Task:
    """One detached operation record. Mutated only under the store lock."""

    task_id: str
    operation: str
    args: dict[str, Any]
    principal: str | None
    status: str
    status_message: str | None
    created_at: str
    last_updated_at: str
    ttl_ms: int
    poll_interval_ms: int
    cancel_event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    _created_mono: float = 0.0
    _expiry_mono: float | None = None
    #: Canonical JSON byte sizes backing the retained-memory budget.
    _args_bytes: int = 0
    _payload_bytes: int = 0
    _status_bytes: int = 0
    _terminal_reservation_bytes: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def task_fields(task: Task) -> dict[str, Any]:
    """Flat ``Task`` wire fields (no ``resultType``/``_meta``)."""
    fields: dict[str, Any] = {
        "taskId": task.task_id,
        "status": task.status,
        "createdAt": task.created_at,
        "lastUpdatedAt": task.last_updated_at,
        "ttlMs": task.ttl_ms,
        "pollIntervalMs": task.poll_interval_ms,
    }
    if task.status_message is not None:
        fields["statusMessage"] = task.status_message
    return fields


def detailed_task_wire(task: Task) -> dict[str, Any]:
    """Flat ``DetailedTask`` fields including status-specific payloads."""
    wire = task_fields(task)
    if task.status == "completed":
        wire["result"] = copy.deepcopy(task.result or {})
    elif task.status == "failed":
        wire["error"] = copy.deepcopy(task.error or {})
    elif task.status == "input_required":
        # This release never transitions to input_required (all tools elicit
        # before task creation), so no outstanding keys can exist.
        wire["inputRequests"] = {}
    return wire


def create_task_wire(task: Task) -> dict[str, Any]:
    """Flat ``CreateTaskResult`` fields; caller stamps ``_meta`` serverInfo."""
    return {"resultType": "task", **task_fields(task)}


def _error_payload(error: Any) -> dict[str, Any]:
    if isinstance(error, ProtocolError):
        payload: dict[str, Any] = {"code": error.code, "message": error.message}
        if error.data is not None:
            payload["data"] = error.data
        return payload
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, int) or isinstance(code, bool):
            raise ValueError("task error dict requires an integer 'code'")
        if not isinstance(message, str):
            raise ValueError("task error dict requires a string 'message'")
        payload = {"code": code, "message": message}
        data = error.get("data")
        if data is not None:
            payload["data"] = data
        return payload
    raise ValueError("task error must be a ProtocolError or a code/message dict")


class TaskStore:
    """Locked registry of detached task records.

    All state transitions happen under one lock so terminal outcomes are
    decided exactly once: the first terminal transition wins and later
    ``complete``/``fail``/``finalize_cancelled`` calls are no-ops.

    TTL is measured from creation. Nonterminal records are kept alive by
    :meth:`service_tick`, which extends their advertised ``ttlMs`` before
    expiry; terminal records expire at ``createdAt + ttlMs`` and are swept
    on every store access and tick. No background thread is created.

    Retention is bounded two ways and never evicts a live record: when the
    retained count reaches ``max_retained`` or the canonical JSON byte
    budget ``max_retained_bytes`` would be exceeded, creation is rejected
    with a busy error naming the reason; an unexpired terminal task is
    never evicted to admit a new one. Terminal transitions assign the
    terminal payload before the terminal status while holding the lock, so
    a snapshot can never observe a terminal record without its result or
    error; ``args`` are dropped at the same transition and their budget
    handed to the terminal payload. Swept task ids keep a bounded
    tombstone so late lookups report ``expired`` instead of
    ``unknown_task``. Wire consumers must use :meth:`snapshot`, which
    returns a deep copy, never the live record.
    """

    def __init__(
        self,
        *,
        initial_ttl_ms: int = DEFAULT_TTL_MS,
        terminal_ttl_ms: int = TERMINAL_TTL_MS,
        poll_interval_ms: int = POLL_INTERVAL_MS,
        max_retained: int = MAX_RETAINED_TASKS,
        max_active: int = MAX_ACTIVE_TASKS,
        max_retained_bytes: int = MAX_RETAINED_TASK_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _MIN_RETAINED_TASK_BYTES <= max_retained_bytes <= _MAX_RETAINED_TASK_BYTES:
            raise ValueError(
                "max_retained_bytes must hold one bounded terminal record "
                f"({_MIN_RETAINED_TASK_BYTES}..{_MAX_RETAINED_TASK_BYTES} bytes)"
            )
        self._initial_ttl_ms = initial_ttl_ms
        self._terminal_ttl_ms = terminal_ttl_ms
        self._poll_interval_ms = poll_interval_ms
        self._max_retained = max_retained
        self._max_active = max_active
        self._max_retained_bytes = max_retained_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._retained_bytes = 0
        self._reserved_terminal_bytes = 0
        self._expired: dict[str, None] = {}

    # -- creation -----------------------------------------------------

    def create(
        self,
        operation: str,
        args: dict[str, Any],
        *,
        principal: str | None = None,
        status_message: str | None = None,
    ) -> Task:
        """Create a ``working`` record bound to ``principal``.

        The supplied ``args`` are deep-copied so later caller mutation
        cannot rewrite what the task records. Raises a busy ``ToolError``
        when the nonterminal cap is reached, when unexpired terminal
        records already fill the retention cap (no eviction of unexpired
        records), or when the creation would exceed the retained-byte
        budget.
        """
        now = self._clock()
        created_iso = _utc_now_iso()
        args_size = _canonical_json_size(args)
        status_message = _bounded_status_message(status_message)
        status_size = _status_message_size(status_message)
        with self._lock:
            self._sweep_locked(now)
            if self._active_count_locked() >= self._max_active:
                raise ToolError(
                    SERVER_BUSY,
                    "Too many active tasks; wait for a task to finish",
                    {"limit": self._max_active, "reason": "active_task_limit"},
                )
            if len(self._tasks) >= self._max_retained:
                raise ToolError(
                    SERVER_BUSY,
                    "Task retention limit reached; retry later",
                    {"limit": self._max_retained, "reason": "retention_limit"},
                )
            # The terminal headroom reservation guarantees that any later
            # bounded terminal record - including the dropped-payload
            # substitute - always fits the budget.
            if (
                self._retained_bytes
                + self._reserved_terminal_bytes
                + args_size
                + status_size
                + _TERMINAL_RESERVE_BYTES
                > self._max_retained_bytes
            ):
                raise ToolError(
                    SERVER_BUSY,
                    "Retained task memory limit reached; retry later",
                    {
                        "reason": "retained_byte_limit",
                        "limit": self._max_retained_bytes,
                        "retainedBytes": self._retained_bytes,
                    },
                )
            task = Task(
                task_id=uuid.uuid4().hex,
                operation=operation,
                args=copy.deepcopy(args),
                principal=principal,
                status="working",
                status_message=status_message,
                created_at=created_iso,
                last_updated_at=created_iso,
                ttl_ms=self._initial_ttl_ms,
                poll_interval_ms=self._poll_interval_ms,
                _created_mono=now,
                _args_bytes=args_size,
                _status_bytes=status_size,
                _terminal_reservation_bytes=_TERMINAL_RESERVE_BYTES,
            )
            self._tasks[task.task_id] = task
            self._retained_bytes += args_size + status_size
            self._reserved_terminal_bytes += _TERMINAL_RESERVE_BYTES
            return task

    # -- queries ------------------------------------------------------

    def get(self, task_id: str, *, principal: str | None = None) -> Task:
        """Return the record or raise ``-32602`` for unknown/expired/foreign."""
        now = self._clock()
        with self._lock:
            self._sweep_locked(now)
            return self._get_checked_locked(task_id, principal)

    def cancel_event_for(self, task_id: str, *, principal: str | None = None) -> threading.Event:
        """Cancellation event for the operation runner to poll."""
        return self.get(task_id, principal=principal).cancel_event

    def snapshot(self, task_id: str, *, principal: str | None = None) -> dict[str, Any]:
        """Deep-copied ``DetailedTask`` wire snapshot for one task.

        ``tasks/get`` and status notifications must build wire payloads
        from this copy; handing out the live record lets a concurrent
        terminal transition change the payload mid-serialization.
        """
        now = self._clock()
        with self._lock:
            self._sweep_locked(now)
            task = self._get_checked_locked(task_id, principal)
            return copy.deepcopy(detailed_task_wire(task))

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)

    # -- terminal transitions (first writer wins) ----------------------

    def complete(
        self,
        task_id: str,
        result: dict[str, Any],
        *,
        principal: str | None = None,
        status_message: str | None = None,
    ) -> bool:
        """Mark ``completed`` with the original tool result payload.

        A tool failure reported with ``isError: true`` is a complete tool
        result, so it finalizes here too — ``fail`` is only for JSON-RPC
        level failures. Returns ``False`` (changing nothing) when the task
        is already terminal; the real result always beats a racing cancel.
        The status message is cleared unless ``status_message`` supplies
        final diagnostics, so a stale "cancellation requested" note cannot
        survive a successful completion.
        """
        now = self._clock()
        with self._lock:
            task = self._get_checked_locked(task_id, principal)
            if task.terminal:
                return False
            stored, size, status_message, status_size, dropped = self._bounded_terminal_locked(
                task, copy.deepcopy(dict(result)), status_message
            )
            if dropped:
                # The real result does not fit the retained-byte budget.
                # The record must stay terminal and readable, so it keeps a
                # bounded error payload naming the drop instead of the
                # oversized result.
                task.result = None
                task.error = stored
                task.status_message = status_message
            else:
                task.result = stored
                task.status_message = status_message
            # Terminal payload is assigned before the terminal status: a
            # snapshot taken between the two must never see a terminal
            # record without its result or error.
            task.status = "failed" if dropped else "completed"
            task._payload_bytes = size
            self._retained_bytes += status_size - task._status_bytes
            task._status_bytes = status_size
            self._mark_terminal_locked(task, now)
            return True

    def fail(
        self,
        task_id: str,
        error: Any,
        *,
        principal: str | None = None,
        status_message: str | None = None,
    ) -> bool:
        """Mark ``failed`` with a JSON-RPC error object.

        ``error`` is a ``ProtocolError`` or ``{"code": int, "message": str,
        "data"?: ...}`` dict. Returns ``False`` when already terminal.
        """
        payload = _error_payload(error)
        now = self._clock()
        with self._lock:
            task = self._get_checked_locked(task_id, principal)
            if task.terminal:
                return False
            stored, size, status_message, status_size, _dropped = self._bounded_terminal_locked(
                task, payload, status_message
            )
            task.error = stored
            task.status_message = status_message
            task.status = "failed"
            task._payload_bytes = size
            self._retained_bytes += status_size - task._status_bytes
            task._status_bytes = status_size
            self._mark_terminal_locked(task, now)
            return True

    def finalize_cancelled(
        self,
        task_id: str,
        *,
        principal: str | None = None,
        status_message: str | None = None,
    ) -> bool:
        """Transition a nonterminal task to ``cancelled``.

        Called by the operation runner when it observes the cancel event at
        a safe boundary: queued work abandoned before execution, or a
        boundary that abandoned remaining work (state any effects already
        made via ``status_message``). Never overwrites a terminal state.
        With no explicit message the current one (e.g. "Cancellation
        requested") is kept.
        """
        now = self._clock()
        with self._lock:
            task = self._get_checked_locked(task_id, principal)
            if task.terminal:
                return False
            task.status = "cancelled"
            if status_message is not None:
                task.status_message = _bounded_status_message(status_message)
            task._payload_bytes = 0
            new_status_size = _status_message_size(task.status_message)
            self._retained_bytes += new_status_size - task._status_bytes
            task._status_bytes = new_status_size
            self._mark_terminal_locked(task, now)
            return True

    # -- cancellation request ------------------------------------------

    def request_cancel(
        self,
        task_id: str,
        *,
        principal: str | None = None,
        status_message: str | None = CANCEL_REQUESTED_MESSAGE,
    ) -> bool:
        """Request cooperative cancellation.

        Sets the task's cancel event. The record stays ``working`` with a
        cancellation-requested status message until the runner reaches a
        boundary; it is the runner's job to finalize via
        :meth:`finalize_cancelled` (queued abandonment) or let the real
        outcome win. Returns ``False`` for already-terminal tasks without
        changing them (``tasks/cancel`` still acknowledges at the protocol
        layer).
        """
        with self._lock:
            task = self._get_checked_locked(task_id, principal)
            if task.terminal:
                return False
            task.cancel_event.set()
            if status_message is not None:
                bounded = _bounded_status_message(status_message)
                new_size = _status_message_size(bounded)
                projected = self._retained_bytes - task._status_bytes + new_size
                projected_with_reserve = projected + self._reserved_terminal_bytes
                if projected_with_reserve <= self._max_retained_bytes:
                    self._retained_bytes = projected
                    task._status_bytes = new_size
                    task.status_message = bounded
                task.last_updated_at = _utc_now_iso()
            return True

    # -- service integration -------------------------------------------

    def service_tick(self) -> None:
        """Extend working TTLs before expiry and sweep expired terminals.

        Called from the server's existing service tick and implicitly on
        every store access; no dedicated thread.
        """
        now = self._clock()
        with self._lock:
            self._sweep_locked(now)
            keep_alive_s = self._initial_ttl_ms / 1000
            for task in self._tasks.values():
                if task.terminal:
                    continue
                remaining = task._created_mono + task.ttl_ms / 1000 - now
                if remaining <= keep_alive_s:
                    elapsed_ms = round((now - task._created_mono) * 1000)
                    extended = max(task.ttl_ms, elapsed_ms + self._initial_ttl_ms)
                    if extended != task.ttl_ms:
                        task.ttl_ms = extended
                        task.last_updated_at = _utc_now_iso()

    # -- internals ------------------------------------------------------

    def _bounded_terminal_locked(
        self, task: Task, payload: dict[str, Any], status_message: str | None
    ) -> tuple[dict[str, Any], int, str | None, int, bool]:
        """Bound one terminal payload against the retained-byte budget.

        Returns ``(payload, size, status_message, status_size, dropped)``. An over-budget payload is
        replaced by a small error record naming the drop; the store never
        silently exceeds its budget through a terminal transition.
        """
        size = _canonical_json_size(payload)
        status_message = _bounded_status_message(status_message)
        status_size = _status_message_size(status_message)
        reserved_other = self._reserved_terminal_bytes - task._terminal_reservation_bytes
        projected = (
            self._retained_bytes
            - task._args_bytes
            - task._status_bytes
            + size
            + status_size
            + reserved_other
        )
        if projected <= self._max_retained_bytes:
            return payload, size, status_message, status_size, False
        # An absurdly large dropped payload must not inflate the bounded
        # record past the reserved headroom, so its byte count is echoed
        # clamped and flagged.
        if size > _MAX_COUNTED_BYTES:
            substitute_data = {
                "reason": "retained_byte_limit",
                "limit": self._max_retained_bytes,
                "retainedBytes": self._retained_bytes - task._args_bytes,
                "droppedBytes": _MAX_COUNTED_BYTES,
                "droppedBytesClamped": True,
            }
        else:
            substitute_data = {
                "reason": "retained_byte_limit",
                "limit": self._max_retained_bytes,
                "retainedBytes": self._retained_bytes - task._args_bytes,
                "droppedBytes": size,
            }
        substitute = {
            "code": -32000,
            "message": "terminal payload exceeded the retained byte budget",
            "data": substitute_data,
        }
        substitute_size = _canonical_json_size(substitute)
        if (
            self._retained_bytes
            - task._args_bytes
            - task._status_bytes
            + substitute_size
            + status_size
            + reserved_other
            > self._max_retained_bytes
        ):
            status_message = None
            status_size = 0
        return substitute, substitute_size, status_message, status_size, True

    def _get_checked_locked(self, task_id: str, principal: str | None) -> Task:
        task = self._tasks.get(task_id)
        # A principal mismatch is indistinguishable from an unknown id so a
        # foreign principal cannot probe another principal's task ids.
        if task is None or task.principal != principal:
            reason = "expired" if task is None and task_id in self._expired else "unknown_task"
            raise ProtocolError(
                INVALID_PARAMS,
                _UNKNOWN_TASK_MESSAGE,
                {"taskId": task_id, "reason": reason},
            )
        return task

    def _active_count_locked(self) -> int:
        return sum(1 for task in self._tasks.values() if not task.terminal)

    def _mark_terminal_locked(self, task: Task, now: float) -> None:
        # Arguments are no longer needed once the outcome is fixed: drop
        # them and hand their budget to the terminal payload.
        task.args = {}
        self._retained_bytes -= task._args_bytes
        task._args_bytes = 0
        self._retained_bytes -= task._status_bytes
        self._reserved_terminal_bytes -= task._terminal_reservation_bytes
        task._terminal_reservation_bytes = 0
        self._retained_bytes += task._payload_bytes + task._status_bytes
        # TTL stays creation-based: a terminal record survives for
        # terminal_ttl_ms measured from its terminal transition.
        elapsed_ms = round((now - task._created_mono) * 1000)
        task.ttl_ms = elapsed_ms + self._terminal_ttl_ms
        task._expiry_mono = now + self._terminal_ttl_ms / 1000
        task.last_updated_at = _utc_now_iso()

    def _sweep_locked(self, now: float) -> None:
        expired = [
            task_id
            for task_id, task in self._tasks.items()
            if task._expiry_mono is not None and now >= task._expiry_mono
        ]
        for task_id in expired:
            task = self._tasks.pop(task_id)
            self._retained_bytes -= task._args_bytes + task._payload_bytes + task._status_bytes
            self._record_expired_locked(task_id)

    def _record_expired_locked(self, task_id: str) -> None:
        """Keep a bounded tombstone so late lookups report ``expired``."""
        self._expired[task_id] = None
        while len(self._expired) > self._max_retained:
            self._expired.pop(next(iter(self._expired)))


def _canonical_json_size(value: Any) -> int:
    """Byte size of the canonical JSON encoding (sorted keys, tight)."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        return len(repr(value).encode("utf-8", "replace"))
    return len(encoded.encode("utf-8", "replace"))
