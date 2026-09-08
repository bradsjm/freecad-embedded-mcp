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
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .protocol import (
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
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

CANCEL_REQUESTED_MESSAGE = "Cancellation requested"

_BUSY_CODE = "SERVER_BUSY"
_UNKNOWN_TASK_MESSAGE = "Unknown or expired task"


def _utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_tasks_capability(client_capabilities: object) -> None:
    """Raise ``-32021`` unless the request declared the Tasks extension.

    ``client_capabilities`` is the ``io.modelcontextprotocol/clientCapabilities``
    object of the current request (may be ``None``). Extensions live under the
    ``extensions`` key and an extension's value must be an object.
    """
    extensions: Any = None
    if isinstance(client_capabilities, dict):
        extensions = client_capabilities.get("extensions")
    if not isinstance(extensions, dict) or not isinstance(
        extensions.get(TASKS_EXTENSION_ID), dict
    ):
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
        wire["result"] = dict(task.result or {})
    elif task.status == "failed":
        wire["error"] = dict(task.error or {})
    elif task.status == "input_required":
        # This release never transitions to input_required (all tools elicit
        # before task creation), so no outstanding keys can exist.
        wire["inputRequests"] = {}
    return wire


def create_task_wire(task: Task) -> dict[str, Any]:
    """Flat ``CreateTaskResult`` fields; caller stamps ``_meta`` serverInfo."""
    return {"resultType": "task", **task_fields(task)}


def get_task_wire(task: Task) -> dict[str, Any]:
    """Flat ``GetTaskResult`` fields with ``resultType: "complete"``."""
    return {"resultType": "complete", **detailed_task_wire(task)}


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
    """

    def __init__(
        self,
        *,
        initial_ttl_ms: int = DEFAULT_TTL_MS,
        terminal_ttl_ms: int = TERMINAL_TTL_MS,
        poll_interval_ms: int = POLL_INTERVAL_MS,
        max_retained: int = MAX_RETAINED_TASKS,
        max_active: int = MAX_ACTIVE_TASKS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._initial_ttl_ms = initial_ttl_ms
        self._terminal_ttl_ms = terminal_ttl_ms
        self._poll_interval_ms = poll_interval_ms
        self._max_retained = max_retained
        self._max_active = max_active
        self._clock = clock
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}

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
        when the nonterminal cap is reached or no terminal record can be
        evicted to stay under the retention cap.
        """
        now = self._clock()
        created_iso = _utc_now_iso()
        with self._lock:
            self._sweep_locked(now)
            if self._active_count_locked() >= self._max_active:
                raise ToolError(
                    _BUSY_CODE,
                    "Too many active tasks; wait for a task to finish",
                    {"limit": self._max_active, "reason": "active_task_limit"},
                )
            while len(self._tasks) >= self._max_retained:
                if not self._evict_oldest_terminal_locked():
                    raise ToolError(
                        _BUSY_CODE,
                        "Task retention limit reached; retry later",
                        {"limit": self._max_retained, "reason": "retention_limit"},
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
            )
            self._tasks[task.task_id] = task
            return task

    # -- queries ------------------------------------------------------

    def get(self, task_id: str, *, principal: str | None = None) -> Task:
        """Return the record or raise ``-32602`` for unknown/expired/foreign."""
        now = self._clock()
        with self._lock:
            self._sweep_locked(now)
            return self._get_checked_locked(task_id, principal)

    def cancel_event_for(
        self, task_id: str, *, principal: str | None = None
    ) -> threading.Event:
        """Cancellation event for the operation runner to poll."""
        return self.get(task_id, principal=principal).cancel_event

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
            task.status = "completed"
            task.status_message = status_message
            task.result = copy.deepcopy(dict(result))
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
            task.status = "failed"
            task.status_message = status_message
            task.error = payload
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
                task.status_message = status_message
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
        now = self._clock()
        with self._lock:
            task = self._get_checked_locked(task_id, principal)
            if task.terminal:
                return False
            task.cancel_event.set()
            if status_message is not None:
                task.status_message = status_message
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

    def _get_checked_locked(self, task_id: str, principal: str | None) -> Task:
        task = self._tasks.get(task_id)
        # A principal mismatch is indistinguishable from an unknown id so a
        # foreign principal cannot probe another principal's task ids.
        if task is None or task.principal != principal:
            raise ProtocolError(
                INVALID_PARAMS, _UNKNOWN_TASK_MESSAGE, {"taskId": task_id}
            )
        return task

    def _active_count_locked(self) -> int:
        return sum(1 for task in self._tasks.values() if not task.terminal)

    def _evict_oldest_terminal_locked(self) -> bool:
        for task_id, task in self._tasks.items():
            if task.terminal:
                del self._tasks[task_id]
                return True
        return False

    def _mark_terminal_locked(self, task: Task, now: float) -> None:
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
            del self._tasks[task_id]
