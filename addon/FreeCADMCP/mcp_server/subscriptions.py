"""Subscription registry for ``subscriptions/listen`` streams.

Implements PLAN section 4: connection-scoped subscriptions keyed by
 ``(connection_id, subscription_id)`` so different clients may reuse the
same JSON-RPC id, bounded 256-event queues per connection, strict opt-in
notification filters (including Tasks ``taskIds``), first-event
``notifications/subscriptions/acknowledged``, and ``io.modelcontextprotocol/
subscriptionId`` metadata stamped on every event and on the final shutdown
result.

Publishing only enqueues pre-built wire messages onto in-memory queues;
observer callbacks never perform network writes. The HTTP transport owns
stream consumption: it drains :attr:`Subscription.queue` (or calls
:meth:`Subscription.receive`) and tears the stream down on disconnect.

Pure stdlib; no FreeCAD, GUI, or socket imports.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Collection
from typing import Any

from .protocol import (
    INVALID_PARAMS,
    META_SERVER_INFO,
    META_SUBSCRIPTION_ID,
    SERVER_INFO,
    ProtocolError,
)

SUBSCRIPTION_ID_META_KEY = META_SUBSCRIPTION_ID
SERVER_INFO_META_KEY = META_SERVER_INFO

ACK_NOTIFICATION_METHOD = "notifications/subscriptions/acknowledged"
TASKS_NOTIFICATION_METHOD = "notifications/tasks"
RESOURCE_UPDATED_NOTIFICATION_METHOD = "notifications/resources/updated"

DOCUMENTS_RESOURCE_URI = "freecad://documents"

DEFAULT_QUEUE_LIMIT = 256

_FILTER_BOOLEAN_KEYS = (
    "toolsListChanged",
    "promptsListChanged",
    "resourcesListChanged",
)
_FILTER_LIST_KEYS = ("resourceSubscriptions", "taskIds")


class SubscriptionClosed(Exception):
    """Raised by :meth:`Subscription.receive` once the stream is exhausted."""


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def honor_filter(
    requested: Any,
    *,
    supported_resource_uris: Collection[str],
    support_tools_list_changed: bool,
    support_prompts_list_changed: bool,
    support_resources_list_changed: bool,
) -> dict[str, Any]:
    """Intersect a requested ``notifications`` filter with server support.

    Returns the honored filter: only notification kinds the server actually
    supports and the client explicitly requested. Malformed filter values
    raise ``-32602``; unknown filter keys are ignored (never honored).
    """
    if not isinstance(requested, dict):
        raise ProtocolError(
            INVALID_PARAMS,
            "subscriptions/listen params.notifications must be an object",
        )
    honored: dict[str, Any] = {}
    supported_booleans = {
        "toolsListChanged": support_tools_list_changed,
        "promptsListChanged": support_prompts_list_changed,
        "resourcesListChanged": support_resources_list_changed,
    }
    for key in _FILTER_BOOLEAN_KEYS:
        value = requested.get(key)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise ProtocolError(INVALID_PARAMS, f"subscriptions filter {key!r} must be a boolean")
        if value and supported_booleans[key]:
            honored[key] = True
    for key in _FILTER_LIST_KEYS:
        value = requested.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ProtocolError(
                INVALID_PARAMS,
                f"subscriptions filter {key!r} must be a list of nonempty strings",
            )
        deduped = _dedupe(value)
        if key == "resourceSubscriptions":
            deduped = [uri for uri in deduped if uri in supported_resource_uris]
        if deduped:
            honored[key] = deduped
    return honored


class Subscription:
    """One open listen stream: a bounded queue of wire messages.

    Queue entries are complete JSON-RPC message dicts ready to serialize:
    notifications while open, then either the final ``subscriptions/listen``
    result response (graceful close) or an abrupt exhaustion (overflow /
    disconnect). :meth:`receive` returns ``None`` only for a poll timeout;
    :class:`SubscriptionClosed` means the stream has ended.
    """

    def __init__(
        self,
        connection_id: Any,
        subscription_id: Any,
        *,
        principal: str | None,
        honored: dict[str, Any],
        queue_limit: int,
    ) -> None:
        self.connection_id = connection_id
        self.subscription_id = subscription_id
        self.principal = principal
        self.honored = honored
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_limit)
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def queue(self) -> queue.Queue[Any]:
        """Consumer-side queue owned by the HTTP stream writer thread."""
        return self._queue

    def offer(self, message: dict[str, Any]) -> bool:
        """Enqueue one wire message without ever blocking.

        A full queue closes the subscription instead of blocking the
        publisher (GUI observers must never wait on a slow client). Returns
        ``False`` when the subscription is closed or this offer overflowed.
        """
        with self._lock:
            if self._closed:
                return False
            try:
                self._queue.put_nowait(message)
            except queue.Full:
                self._close_locked()
                return False
            return True

    def receive(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Return the next wire message.

        ``None`` means no message arrived within ``timeout`` (or no timeout
        was requested and the queue is momentarily empty while still open).
        Raises :class:`SubscriptionClosed` once closed and fully drained —
        including after the final result message was delivered.
        """
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            if self._closed:
                raise SubscriptionClosed() from None
            return None
        if item is None:
            raise SubscriptionClosed()
        return item

    def close(self, *, final: dict[str, Any] | None = None) -> bool:
        """Close the stream, optionally enqueueing a final result first.

        Idempotent; returns ``False`` when already closed. The terminal
        ``None`` marker is best-effort: if the queue is already full the
        consumer still observes closure via the ``closed`` flag once it
        drains the pending messages.
        """
        with self._lock:
            if self._closed:
                return False
            if final is not None:
                try:
                    self._queue.put_nowait(final)
                except queue.Full:
                    pass
            self._close_locked()
            return True

    def _close_locked(self) -> None:
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # The queue was already full at close time; the drained consumer
            # observes ``closed`` and raises SubscriptionClosed on Empty.
            pass


class SubscriptionRegistry:
    """Connection-scoped registry of open subscription streams."""

    def __init__(
        self,
        *,
        supported_resource_uris: Collection[str] = (DOCUMENTS_RESOURCE_URI,),
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        support_tools_list_changed: bool = False,
        support_prompts_list_changed: bool = False,
        support_resources_list_changed: bool = False,
    ) -> None:
        self._supported_resource_uris = frozenset(supported_resource_uris)
        self._queue_limit = queue_limit
        self._support_tools_list_changed = support_tools_list_changed
        self._support_prompts_list_changed = support_prompts_list_changed
        self._support_resources_list_changed = support_resources_list_changed
        self._lock = threading.Lock()
        self._subscriptions: dict[tuple[Any, Any], Subscription] = {}

    # -- lifecycle ------------------------------------------------------

    def register(
        self,
        connection_id: Any,
        subscription_id: Any,
        requested_notifications: Any,
        *,
        principal: str | None = None,
    ) -> Subscription:
        """Open a stream and queue its acknowledgement as the first event.

        ``subscription_id`` is the JSON-RPC id of the ``subscriptions/listen``
        request; identity is scoped to ``connection_id`` so separate
        connections may reuse the same id. Reusing an id that is still open
        on the same connection raises ``-32602``.
        """
        honored = honor_filter(
            requested_notifications,
            supported_resource_uris=self._supported_resource_uris,
            support_tools_list_changed=self._support_tools_list_changed,
            support_prompts_list_changed=self._support_prompts_list_changed,
            support_resources_list_changed=self._support_resources_list_changed,
        )
        with self._lock:
            self._prune_locked()
            key = (connection_id, subscription_id)
            existing = self._subscriptions.get(key)
            if existing is not None and not existing.closed:
                raise ProtocolError(
                    INVALID_PARAMS,
                    "subscription id already active on this connection",
                )
            subscription = Subscription(
                connection_id,
                subscription_id,
                principal=principal,
                honored=honored,
                queue_limit=self._queue_limit,
            )
            self._subscriptions[key] = subscription
            # Offer the acknowledgement while still holding the registry
            # lock so no publish can ever precede it in the queue.
            subscription.offer(
                {
                    "jsonrpc": "2.0",
                    "method": ACK_NOTIFICATION_METHOD,
                    "params": {
                        "notifications": dict(honored),
                        "_meta": {SUBSCRIPTION_ID_META_KEY: subscription_id},
                    },
                }
            )
            return subscription

    def disconnect(self, connection_id: Any) -> int:
        """Close and drop every subscription of one connection.

        Returns the number of streams closed. Streams close without a final
        result (the transport is gone); detached tasks are untouched —
        cancellation of detached work only ever happens via tasks/cancel.
        """
        with self._lock:
            doomed = [key for key, sub in self._subscriptions.items() if key[0] == connection_id]
            for key in doomed:
                self._subscriptions.pop(key).close()
            return len(doomed)

    def shutdown(self) -> int:
        """Deliver the final complete response to every open stream.

        Called at server shutdown where the transport remains writable;
        each stream receives its ``subscriptions/listen`` result response
        (stamped with the subscription id and server info) and then ends.
        Idempotent; returns the number of streams closed.
        """
        with self._lock:
            self._prune_locked()
            open_subscriptions = [sub for sub in self._subscriptions.values() if not sub.closed]
            for subscription in open_subscriptions:
                self._subscriptions.pop((subscription.connection_id, subscription.subscription_id))
        for subscription in open_subscriptions:
            subscription.close(
                final={
                    "jsonrpc": "2.0",
                    "id": subscription.subscription_id,
                    "result": {
                        "resultType": "complete",
                        "_meta": {
                            SUBSCRIPTION_ID_META_KEY: subscription.subscription_id,
                            SERVER_INFO_META_KEY: SERVER_INFO,
                        },
                    },
                }
            )
        return len(open_subscriptions)

    # -- lookups ----------------------------------------------------------

    def subscription(self, connection_id: Any, subscription_id: Any) -> Subscription | None:
        with self._lock:
            self._prune_locked()
            return self._subscriptions.get((connection_id, subscription_id))

    def __len__(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._subscriptions)

    # -- publishing (enqueues only; never writes to the network) ----------

    def publish_resource_updated(self, uri: str) -> int:
        """Emit ``notifications/resources/updated`` to subscribers of ``uri``.

        Returns the number of streams the event was enqueued on.
        """
        with self._lock:
            targets = self._targets_locked(
                lambda sub: uri in sub.honored.get("resourceSubscriptions", ())
            )
        return self._deliver(
            targets,
            RESOURCE_UPDATED_NOTIFICATION_METHOD,
            lambda sub: {"uri": uri},
        )

    def publish_task_status(self, task: dict[str, Any]) -> int:
        """Emit ``notifications/tasks`` with flat DetailedTask ``task``.

        ``task`` is the payload built by
        :func:`mcp_server.tasks.detailed_task_wire`; only streams whose
        honored filter lists the task's id under ``taskIds`` receive it.
        """
        task_id = task.get("taskId")
        with self._lock:
            targets = self._targets_locked(lambda sub: task_id in sub.honored.get("taskIds", ()))
        return self._deliver(targets, TASKS_NOTIFICATION_METHOD, lambda sub: dict(task))

    # -- internals ---------------------------------------------------------

    def _targets_locked(self, predicate: Callable[[Subscription], bool]) -> list[Subscription]:
        self._prune_locked()
        return [sub for sub in self._subscriptions.values() if predicate(sub)]

    def _deliver(
        self,
        targets: list[Subscription],
        method: str,
        params_for: Callable[[Subscription], dict[str, Any]],
    ) -> int:
        delivered = 0
        for subscription in targets:
            if subscription.offer(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": {
                        **params_for(subscription),
                        "_meta": {SUBSCRIPTION_ID_META_KEY: subscription.subscription_id},
                    },
                }
            ):
                delivered += 1
        return delivered

    def _prune_locked(self) -> None:
        closed = [key for key, sub in self._subscriptions.items() if sub.closed]
        for key in closed:
            del self._subscriptions[key]
