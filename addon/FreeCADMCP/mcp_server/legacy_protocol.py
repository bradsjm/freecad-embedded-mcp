"""Server-side legacy Streamable HTTP compatibility for MCP 2025 revisions.

Bridges clients speaking the ``2025-03-26``, ``2025-06-18`` or
``2025-11-25`` Streamable HTTP transports onto the modern
``Server.dispatch`` contract without weakening modern validation or
requiring modern request metadata on legacy messages.

Scope contracts (plan section 2):

- Sessions are explicit: ``initialize`` creates one, every other legacy
  message names it via ``MCP-Session-Id``. Session ids never authenticate.
- Legacy clients receive final tool results, never detached tasks; consent
  travels as native ``elicitation/create`` requests on the request-scoped
  SSE stream when the client declares form support. Clients without form
  support fall back to unprompted execution (1.0 behavior). No adapter
  path fabricates consent: acceptance is only ever relayed to the existing
  consent signer via ``requestState``/``inputResponses``.
- Results are translated at one boundary (:func:`legacy_result`): modern
  envelope metadata is stripped, application payloads pass through.

This module is stdlib-only and never imports FreeCAD or Qt. It imports
``http_server`` names lazily inside stream-producing methods to avoid an
import cycle.
"""

from __future__ import annotations

import queue
import secrets
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping

from mcp_server.protocol import (
    CONSENT_DENIED,
    DEFAULT_CONSENT_TTL_S,
    HEADER_MISMATCH,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    METHOD_NOT_FOUND,
    SERVER_INFO,
    ProtocolError,
    ToolError,
    error_response,
    header_matches_body,
    header_source_value,
    tool_error_result,
)
# Shared envelope helpers reused so the header mirroring rules cannot drift
# between the modern and legacy validators (plan section 2a).
from mcp_server.protocol import (  # noqa: F401 - same-package private reuse
    _NAME_SOURCES,
    _validate_param_headers,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp_server.http_server import StreamResponse

#: Legacy Streamable HTTP revisions accepted by this server, oldest first.
LEGACY_PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")

#: Revision answered when a client offers none of the supported ones.
LATEST_LEGACY_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[-1]

#: Only this revision's transport defines JSON-RPC batch requests.
BATCH_REVISION = "2025-03-26"

#: Application tool code shared with ``server.py`` (never a JSON-RPC code).
SERVER_BUSY = "SERVER_BUSY"

MAX_LEGACY_SESSIONS = 32
MAX_INFLIGHT_LEGACY_REQUESTS = 32
MAX_BATCH_REQUEST_MEMBERS = 32
SESSION_IDLE_TIMEOUT_S = 3600.0

#: Poll granularity while waiting for an elicitation response: the wait
#: must wake for the deadline and a shared cancellation request as well as
#: for the response event, so it waits in bounded ticks (same pattern as
#: ``server._await_async_value``).
_CONSENT_WAIT_TICK_S = 0.05

SESSION_HEADER = "mcp-session-id"
PROTOCOL_VERSION_HEADER = "mcp-protocol-version"
METHOD_HEADER = "mcp-method"
NAME_HEADER = "mcp-name"

INITIALIZE_FIRST_MESSAGE = (
    "Initialize a session first. Supported MCP versions: "
    "2025-03-26, 2025-06-18, 2025-11-25, 2026-07-28."
)
NOT_INITIALIZED_MESSAGE = "Session initialization is not complete."
SESSION_LIMIT_MESSAGE = (
    "MCP session limit reached; close an existing session and retry."
)
TOO_MANY_REQUESTS_MESSAGE = "Too many requests in batch"
UNKNOWN_SESSION_MESSAGE = "unknown or expired MCP session"
INITIALIZER_SESSION_ID_MESSAGE = (
    "MCP-Session-Id must be omitted when initializing a new session"
)
CANCELLED_BEFORE_EXECUTION_MESSAGE = "Operation cancelled before execution"
OPERATION_LIMIT_MESSAGE = "Too many active operations; wait for one to finish"

_DISPATCH_METHODS = ("tools/list", "tools/call", "resources/list", "resources/read")

_SERVER_INSTRUCTIONS = (
    "Consent prompts use form elicitation when the client supports it; "
    "clients without form support proceed without the prompt. Long-running "
    "operations return final results; detached tasks and resource "
    "subscriptions are not offered in this session."
)


def legacy_result(response: dict, version: str, method: str) -> dict:
    """Translate one modern JSON-RPC response for a legacy client.

    Copies (never mutates) the shared response and strips modern envelope
    metadata from the result — ``resultType``, ``ttlMs``, ``cacheScope``
    and ``io.modelcontextprotocol/serverInfo``. Application payloads
    (``structuredContent``/``content``) are never stripped recursively.

    For ``2025-03-26`` the tool shims remove ``outputSchema`` from
    ``tools/list`` definitions and ``structuredContent`` from tool
    results; later revisions retain both.
    """

    translated = dict(response)
    result = translated.get("result")
    if not isinstance(result, dict):
        return translated
    stripped = dict(result)
    stripped.pop("resultType", None)
    stripped.pop("ttlMs", None)
    stripped.pop("cacheScope", None)
    meta = stripped.get("_meta")
    if isinstance(meta, Mapping) and META_SERVER_INFO in meta:
        meta = {k: v for k, v in meta.items() if k != META_SERVER_INFO}
        if meta:
            stripped["_meta"] = meta
        else:
            stripped.pop("_meta", None)
    if version == BATCH_REVISION:
        if method == "tools/list":
            tools = stripped.get("tools")
            if isinstance(tools, list):
                stripped["tools"] = [
                    {k: v for k, v in tool.items() if k != "outputSchema"}
                    if isinstance(tool, Mapping)
                    else tool
                    for tool in tools
                ]
        elif method == "tools/call":
            stripped.pop("structuredContent", None)
    translated["result"] = stripped
    return translated


def has_modern_metadata(message: Any) -> bool:
    """True when the message carries modern namespaced protocol metadata."""

    if not isinstance(message, dict):
        return False
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    meta = params.get("_meta")
    return isinstance(meta, dict) and META_PROTOCOL_VERSION in meta


@dataclass
class LegacyReply:
    """One HTTP answer produced by :class:`LegacyProtocol`.

    ``payload`` responses (and empty bodies) are emitted through the
    handler's JSON writer; ``stream`` responses through its SSE writer.
    Only initialize replies carry extra headers (``MCP-Session-Id``).
    """

    status: int
    payload: dict | None
    headers: tuple[tuple[str, str], ...] = ()
    stream: "StreamResponse | None" = None


@dataclass
class _PendingElicitation:
    """One outstanding ``elicitation/create`` wait."""

    event: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None
    delivered: bool = False


@dataclass
class _ActiveRequest:
    """One in-flight legacy request owned by a producer thread."""

    request_id: Any
    cancel_event: threading.Event = field(default_factory=threading.Event)
    #: The consent wait currently blocking this request, if any. At most
    #: one: the producing thread waits on a single elicitation at a time.
    consent_waiter: _PendingElicitation | None = None


@dataclass
class _LegacySession:
    """Server-side state for one legacy client session."""

    session_id: str
    principal: str
    version: str
    client_info: dict
    capabilities: dict  # normalized for the modern consent gate only
    initialized: bool = False
    last_activity: float = 0.0
    active: int = 0
    active_requests: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)  # elicitation id -> waiter
    closed: bool = False

    @property
    def dispatch_principal(self) -> str:
        """Session-bound principal so consent state cannot cross sessions."""

        return f"{self.principal}:legacy:{self.session_id}"


class LegacyProtocol:
    """Legacy-era session store, request router and consent bridge.

    Owns no FreeCAD or Qt objects: tool execution is delegated to the
    existing ``dispatch`` callback (the ``Server.dispatch`` signature).
    The session lock is never held while dispatching, waiting for
    elicitation or consuming an inner stream.
    """

    def __init__(
        self,
        dispatch: Callable[[dict, str, Any], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dispatch = dispatch
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, _LegacySession] = {}
        self._inflight = 0
        self._shutdown = False

    # ------------------------------------------------------------------
    # Public API (called by the HTTP handler)
    # ------------------------------------------------------------------

    def handle(
        self,
        message: dict | list,
        headers: Mapping[str, str],
        principal: str,
    ) -> LegacyReply:
        """Route one legacy-era message (or 2025-03-26 batch)."""

        lowered = {str(name).lower(): value for name, value in headers.items()}
        session_id = lowered.get(SESSION_HEADER)
        with self._lock:
            shutting_down = self._shutdown
        if shutting_down:
            return _error_reply(503, INTERNAL_ERROR, "server is shutting down")
        if isinstance(message, list):
            return self._handle_batch(message, lowered, session_id, principal)
        if not isinstance(message, dict):
            return _error_reply(
                400, INVALID_REQUEST, "malformed envelope: expected a JSON-RPC message"
            )
        if message.get("method") == "initialize" and not has_modern_metadata(message):
            if session_id is not None:
                return _error_reply(
                    400, INVALID_REQUEST, INITIALIZER_SESSION_ID_MESSAGE
                )
            return self._handle_initialize(message, lowered, principal)
        if session_id is None:
            return _error_reply(400, INVALID_REQUEST, INITIALIZE_FIRST_MESSAGE)
        session = self._lookup_session(session_id, principal)
        if session is None:
            return _error_reply(404, INVALID_REQUEST, UNKNOWN_SESSION_MESSAGE)
        return self._route(session, message, lowered)

    def delete_session(self, session_id: str, principal: str) -> bool:
        """Remove one same-principal session; wake everything waiting on it.

        Returns ``True`` only when a live session of this principal was
        removed. Foreign ids are indistinguishable from unknown ones.
        """

        if not isinstance(session_id, str) or not session_id:
            return False
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.closed or session.principal != principal:
                return False
            del self._sessions[session_id]
            session.closed = True
            actives = list(session.active_requests.values())
            waiters = list(session.pending.values())
        for active in actives:
            active.cancel_event.set()
        for waiter in waiters:
            waiter.event.set()
        return True

    def shutdown(self) -> None:
        """Prevent new work and wake every consent wait.

        Called by ``McpHTTPServer.stop`` *before* transport streams drain,
        so producers can enqueue their final results and the drain still
        delivers them. Never waits for running work.
        """

        with self._lock:
            self._shutdown = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
            for session in sessions:
                session.closed = True
            actives = [
                active
                for session in sessions
                for active in session.active_requests.values()
            ]
            waiters = [
                waiter for session in sessions for waiter in session.pending.values()
            ]
        for active in actives:
            active.cancel_event.set()
        for waiter in waiters:
            waiter.event.set()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _lookup_session(self, session_id: Any, principal: str) -> _LegacySession | None:
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_id)
            if session is None or session.closed or session.principal != principal:
                return None
            session.last_activity = self._clock()
            return session

    def _prune_locked(self) -> None:
        """Expire idle sessions that hold no in-flight requests."""

        now = self._clock()
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.active == 0
            and now - session.last_activity > SESSION_IDLE_TIMEOUT_S
        ]
        for session_id in expired:
            session = self._sessions.pop(session_id)
            session.closed = True

    # ------------------------------------------------------------------
    # Initialize
    # ------------------------------------------------------------------

    def _handle_initialize(
        self, message: dict, lowered: dict, principal: str
    ) -> LegacyReply:
        envelope_error = self._envelope_error(message, require_request=True)
        if envelope_error is not None:
            return envelope_error
        params = message.get("params", {})
        offered = params.get("protocolVersion")
        client_info = params.get("clientInfo")
        capabilities = params.get("capabilities")
        if not isinstance(offered, str) or not offered:
            return _error_reply(
                400,
                INVALID_PARAMS,
                "invalid parameters: protocolVersion must be a non-empty string",
            )
        if (
            not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not client_info["name"]
            or not isinstance(client_info.get("version"), str)
            or not client_info["version"]
        ):
            return _error_reply(
                400,
                INVALID_PARAMS,
                "invalid parameters: clientInfo requires string name and version",
            )
        if not isinstance(capabilities, dict):
            return _error_reply(
                400,
                INVALID_PARAMS,
                "invalid parameters: capabilities must be an object",
            )
        negotiated = (
            offered
            if offered in LEGACY_PROTOCOL_VERSIONS
            else LATEST_LEGACY_PROTOCOL_VERSION
        )
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_locked()
            if len(self._sessions) >= MAX_LEGACY_SESSIONS:
                return _error_reply(503, INTERNAL_ERROR, SESSION_LIMIT_MESSAGE)
            session = _LegacySession(
                session_id=session_id,
                principal=principal,
                version=negotiated,
                client_info={
                    "name": client_info["name"],
                    "version": client_info["version"],
                },
                capabilities=_normalize_capabilities(capabilities, negotiated),
                last_activity=self._clock(),
            )
            self._sessions[session_id] = session
        result = {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": dict(SERVER_INFO),
            "instructions": _SERVER_INSTRUCTIONS,
        }
        return LegacyReply(
            200,
            {"jsonrpc": JSONRPC_VERSION, "id": message["id"], "result": result},
            headers=(("MCP-Session-Id", session_id),),
        )

    # ------------------------------------------------------------------
    # Per-session routing
    # ------------------------------------------------------------------

    def _route(
        self, session: _LegacySession, message: dict, lowered: dict
    ) -> LegacyReply:
        header_error = self._header_error(session, message, lowered)
        if header_error is not None:
            return header_error
        if "method" not in message:
            return self._route_client_response(session, message)
        envelope_error = self._envelope_error(message, require_request=False)
        if envelope_error is not None:
            return envelope_error
        if "id" not in message:
            return self._route_notification(session, message)
        if not session.initialized and message["method"] != "ping":
            return _error_reply(400, INVALID_REQUEST, NOT_INITIALIZED_MESSAGE)
        method = message["method"]
        if method == "ping":
            return LegacyReply(
                200, {"jsonrpc": JSONRPC_VERSION, "id": message["id"], "result": {}}
            )
        if method == "tools/call":
            param_error = _reject_client_consent_fields(message.get("params", {}))
            if param_error is not None:
                return param_error
            return self._start_tool_call(session, message)
        if method in _DISPATCH_METHODS:
            return self._dispatch_inline(session, message)
        return LegacyReply(
            200,
            error_response(
                ProtocolError(METHOD_NOT_FOUND, f"unknown method: {method}"),
                message["id"],
            ),
        )

    def _route_client_response(
        self, session: _LegacySession, message: dict
    ) -> LegacyReply:
        """Route a client JSON-RPC response to its pending elicitation."""

        response_id = message.get("id")
        if isinstance(response_id, bool) or not isinstance(response_id, (str, int)):
            return _error_reply(
                400,
                INVALID_REQUEST,
                "malformed envelope: response id must be a string or integer",
            )
        if ("result" in message) == ("error" in message):
            return _error_reply(
                400,
                INVALID_REQUEST,
                "malformed envelope: response requires exactly one of result or error",
            )
        with self._lock:
            waiter = session.pending.get(response_id)
            if waiter is None or waiter.delivered:
                return _error_reply(
                    400,
                    INVALID_REQUEST,
                    "no outstanding request for this response id",
                )
            # Single-assignment delivery: the first response wins; a losing
            # duplicate only finds delivered=True and is rejected above.
            waiter.response = message
            waiter.delivered = True
            waiter.event.set()
        return LegacyReply(202, None)

    def _route_notification(
        self, session: _LegacySession, message: dict
    ) -> LegacyReply:
        method = message["method"]
        if method == "notifications/initialized":
            with self._lock:
                session.initialized = True  # duplicates are idempotent
            return LegacyReply(202, None)
        if method == "notifications/cancelled":
            params = message.get("params", {})
            request_id = params.get("requestId") if isinstance(params, dict) else None
            with self._lock:
                active = (
                    session.active_requests.get(request_id)
                    if isinstance(request_id, (str, int))
                    and not isinstance(request_id, bool)
                    else None
                )
                if active is not None:
                    active.cancel_event.set()
                    waiter = active.consent_waiter
                    if waiter is not None:
                        waiter.event.set()
            # Unknown or already-finished ids are harmless.
            return LegacyReply(202, None)
        # Other notifications are ignored; they never reach tool execution.
        return LegacyReply(202, None)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _envelope_error(message: dict, *, require_request: bool) -> LegacyReply | None:
        if message.get("jsonrpc") != JSONRPC_VERSION:
            return _error_reply(
                400, INVALID_REQUEST, 'malformed envelope: jsonrpc must be "2.0"'
            )
        if "method" in message:
            method = message["method"]
            if not isinstance(method, str) or not method:
                return _error_reply(
                    400,
                    INVALID_REQUEST,
                    "malformed envelope: method must be a non-empty string",
                )
        if not isinstance(message.get("params", {}), dict):
            return _error_reply(
                400, INVALID_REQUEST, "malformed envelope: params must be an object"
            )
        if "id" in message:
            request_id = message["id"]
            if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
                return _error_reply(
                    400,
                    INVALID_REQUEST,
                    "malformed envelope: id must be a string or integer",
                )
        elif require_request:
            return _error_reply(
                400, INVALID_REQUEST, "malformed envelope: initialize requires an id"
            )
        return None

    @staticmethod
    def _header_error(
        session: _LegacySession, message: dict, lowered: dict
    ) -> LegacyReply | None:
        """Validate optional mirror headers against the actual legacy body."""

        version_header = lowered.get(PROTOCOL_VERSION_HEADER)
        if version_header is not None:
            try:
                value = header_source_value(version_header)
            except ValueError as exc:
                return _error_reply(
                    400,
                    HEADER_MISMATCH,
                    f"header mismatch: MCP-Protocol-Version: {exc}",
                )
            if value != session.version:
                return _error_reply(
                    400,
                    HEADER_MISMATCH,
                    "header mismatch: MCP-Protocol-Version does not match the "
                    f"negotiated {session.version} session",
                )
        if "method" not in message:
            return None
        method = message["method"]
        params = message.get("params", {})
        method_header = lowered.get(METHOD_HEADER)
        if method_header is not None:
            try:
                value = header_source_value(method_header)
            except ValueError as exc:
                return _error_reply(
                    400, HEADER_MISMATCH, f"header mismatch: Mcp-Method: {exc}"
                )
            if value != method:
                return _error_reply(
                    400,
                    HEADER_MISMATCH,
                    "header mismatch: Mcp-Method does not match the request",
                )
        name_header = lowered.get(NAME_HEADER)
        if name_header is not None:
            try:
                header_source_value(name_header)
            except ValueError as exc:
                return _error_reply(
                    400, HEADER_MISMATCH, f"header mismatch: Mcp-Name: {exc}"
                )
            name_source = _NAME_SOURCES.get(method)
            if name_source is not None and (
                name_source not in params
                or not header_matches_body(name_header, params[name_source])
            ):
                return _error_reply(
                    400,
                    HEADER_MISMATCH,
                    f"header mismatch: Mcp-Name does not match params.{name_source}",
                )
        try:
            _validate_param_headers(params, lowered, None)
        except ProtocolError as exc:
            return _error_reply(400, exc.code, exc.message, exc.data)
        return None

    # ------------------------------------------------------------------
    # Inline dispatch (everything except streamed tools/call)
    # ------------------------------------------------------------------

    def _dispatch_inline(self, session: _LegacySession, message: dict) -> LegacyReply:
        try:
            outcome = self._dispatch(
                _normalize_request(session, message),
                session.dispatch_principal,
                session.session_id,
            )
        except ProtocolError as exc:
            return LegacyReply(200, error_response(exc, message["id"]))
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return LegacyReply(
                200,
                error_response(
                    ProtocolError(INTERNAL_ERROR, "Unexpected server error."),
                    message["id"],
                ),
            )
        if not isinstance(outcome, dict):
            return LegacyReply(
                200,
                error_response(
                    ProtocolError(INTERNAL_ERROR, "Unexpected server error."),
                    message["id"],
                ),
            )
        return LegacyReply(
            200, legacy_result(outcome, session.version, message["method"])
        )

    # ------------------------------------------------------------------
    # Streamed tools/call (single request or batch member)
    # ------------------------------------------------------------------

    def _start_tool_call(self, session: _LegacySession, message: dict) -> LegacyReply:
        request_id = message["id"]
        with self._lock:
            if session.closed:
                return _error_reply(404, INVALID_REQUEST, UNKNOWN_SESSION_MESSAGE)
            if self._shutdown:
                return _error_reply(503, INTERNAL_ERROR, "server is shutting down")
            self._prune_locked()
            if request_id in session.active_requests:
                return _error_reply(
                    400, INVALID_REQUEST, "duplicate active request id"
                )
            if self._inflight >= MAX_INFLIGHT_LEGACY_REQUESTS:
                return LegacyReply(
                    200, self._busy_tool_payload(session, request_id)
                )
            active = _ActiveRequest(request_id=request_id)
            session.active_requests[request_id] = active
            session.active += 1
            self._inflight += 1
        return self._spawn_stream(session, [(message, active)])

    def _spawn_stream(
        self,
        session: _LegacySession,
        members: list[tuple[dict, _ActiveRequest | None]],
    ) -> LegacyReply:
        from mcp_server.http_server import StreamResponse  # local: import cycle

        outer: "queue.SimpleQueue" = queue.SimpleQueue()
        threading.Thread(
            target=self._produce_members,
            args=(session, members, outer),
            name="mcp-legacy-call",
            daemon=True,
        ).start()
        return LegacyReply(200, None, stream=StreamResponse(outer))

    def _produce_members(
        self,
        session: _LegacySession,
        members: list[tuple[dict, _ActiveRequest | None]],
        outer: "queue.SimpleQueue",
    ) -> None:
        try:
            self._produce_member_loop(session, members, outer)
        finally:
            outer.put(None)

    def _produce_member_loop(
        self,
        session: _LegacySession,
        members: list[tuple[dict, _ActiveRequest | None]],
        outer: "queue.SimpleQueue",
    ) -> None:
        for message, active in members:
            try:
                if active is None:
                    self._produce_inline_member(session, message, outer)
                else:
                    self._produce_tool_call(session, active, message, outer)
            except Exception:
                # Never lose the stream silently: one member's failure is
                # one INTERNAL_ERROR response, not a dead stream.
                traceback.print_exc(file=sys.stderr)
                outer.put(
                    error_response(
                        ProtocolError(INTERNAL_ERROR, "Unexpected server error."),
                        message.get("id"),
                    )
                )

    def _produce_inline_member(
        self, session: _LegacySession, message: dict, outer: "queue.SimpleQueue"
    ) -> None:
        method = message["method"]
        request_id = message.get("id")
        if not session.initialized and method != "ping":
            outer.put(
                error_response(
                    ProtocolError(INVALID_REQUEST, NOT_INITIALIZED_MESSAGE),
                    request_id,
                )
            )
            return
        if method == "ping":
            outer.put({"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": {}})
            return
        if method in _DISPATCH_METHODS:
            try:
                outcome = self._dispatch(
                    _normalize_request(session, message),
                    session.dispatch_principal,
                    session.session_id,
                )
            except ProtocolError as exc:
                outer.put(error_response(exc, request_id))
                return
            except Exception:
                traceback.print_exc(file=sys.stderr)
                outer.put(
                    error_response(
                        ProtocolError(INTERNAL_ERROR, "Unexpected server error."),
                        request_id,
                    )
                )
                return
            if isinstance(outcome, dict):
                outer.put(legacy_result(outcome, session.version, method))
            else:
                outer.put(
                    error_response(
                        ProtocolError(INTERNAL_ERROR, "Unexpected server error."),
                        request_id,
                    )
                )
            return
        outer.put(
            error_response(
                ProtocolError(METHOD_NOT_FOUND, f"unknown method: {method}"),
                request_id,
            )
        )

    def _produce_tool_call(
        self,
        session: _LegacySession,
        active: _ActiveRequest,
        message: dict,
        outer: "queue.SimpleQueue",
    ) -> None:
        try:
            self._run_tool_call(session, active, message, outer)
        finally:
            with self._lock:
                if session.active_requests.get(active.request_id) is active:
                    del session.active_requests[active.request_id]
                session.active -= 1
                self._inflight -= 1

    def _run_tool_call(
        self,
        session: _LegacySession,
        active: _ActiveRequest,
        message: dict,
        outer: "queue.SimpleQueue",
    ) -> None:
        request_id = message["id"]
        version = session.version
        # The consent TTL bounds the WHOLE consent phase across any
        # re-challenges; it is never extended for this request.
        consent_deadline = self._clock() + DEFAULT_CONSENT_TTL_S
        request = dict(message)
        while True:
            if active.cancel_event.is_set():
                outer.put(_cancelled_result(request_id, version))
                return
            try:
                outcome = self._dispatch(
                    _normalize_request(
                        session, request, cancel_event=active.cancel_event
                    ),
                    session.dispatch_principal,
                    session.session_id,
                )
            except ProtocolError as exc:
                outer.put(error_response(exc, request_id))
                return
            except Exception:
                traceback.print_exc(file=sys.stderr)
                outer.put(
                    error_response(
                        ProtocolError(INTERNAL_ERROR, "Unexpected server error."),
                        request_id,
                    )
                )
                return
            if _is_stream(outcome):
                self._pump_inner_stream(outcome, request_id, version, outer)
                return
            if not isinstance(outcome, dict):
                outer.put(
                    error_response(
                        ProtocolError(INTERNAL_ERROR, "Unexpected server error."),
                        request_id,
                    )
                )
                return
            result = outcome.get("result")
            if isinstance(result, dict) and result.get("resultType") == "input_required":
                retry = self._consent_round(
                    session, active, request, outcome, consent_deadline, outer
                )
                if retry is None:
                    return
                request = retry
                continue
            outer.put(legacy_result(outcome, version, "tools/call"))
            return

    def _consent_round(
        self,
        session: _LegacySession,
        active: _ActiveRequest,
        request: dict,
        outcome: dict,
        deadline: float,
        outer: "queue.SimpleQueue",
    ) -> dict | None:
        """Run one elicitation round trip; return the retry request or None.

        Returning ``None`` means the caller must stop: a final translated
        result has already been enqueued.
        """

        version = session.version
        request_id = request.get("id")
        result = outcome.get("result", {})
        request_state = result.get("requestState")
        input_request = (result.get("inputRequests") or {}).get("confirm") or {}
        params = input_request.get("params") or {}
        message_text = params.get("message")
        requested_schema = params.get("requestedSchema")
        if not isinstance(request_state, str) or not isinstance(message_text, str):
            outer.put(
                error_response(
                    ProtocolError(
                        INTERNAL_ERROR, "malformed consent challenge from tool preflight"
                    ),
                    request_id,
                )
            )
            return None
        elicitation_id = "elicitation-" + str(uuid.uuid4())
        waiter = _PendingElicitation()
        with self._lock:
            if session.closed:
                outer.put(_consent_denied_result(request_id, version, "session closed"))
                return None
            session.pending[elicitation_id] = waiter
            active.consent_waiter = waiter
        elicitation_params: dict = {
            "message": message_text,
            "requestedSchema": requested_schema,
        }
        if version == LATEST_LEGACY_PROTOCOL_VERSION:
            elicitation_params["mode"] = "form"
        try:
            outer.put(
                {
                    "jsonrpc": JSONRPC_VERSION,
                    "id": elicitation_id,
                    "method": "elicitation/create",
                    "params": elicitation_params,
                }
            )
            while True:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    outer.put(_consent_denied_result(request_id, version, "timeout"))
                    return None
                if waiter.event.wait(timeout=min(remaining, _CONSENT_WAIT_TICK_S)):
                    break
                if active.cancel_event.is_set():
                    outer.put(_cancelled_result(request_id, version))
                    return None
            with self._lock:
                delivered = waiter.delivered
                response = waiter.response
            if not delivered:
                # Woken by deletion, shutdown or cancellation without a
                # response: abort without effects.
                if active.cancel_event.is_set():
                    outer.put(_cancelled_result(request_id, version))
                else:
                    outer.put(
                        _consent_denied_result(request_id, version, "session closed")
                    )
                return None
            if _classify_elicitation(response) == "invalid":
                outer.put(
                    _consent_denied_result(
                        request_id, version, "invalid consent response"
                    )
                )
                return None
            retry = dict(request)
            retry_params = dict(request.get("params", {}))
            retry_params["requestState"] = request_state
            retry_params["inputResponses"] = {"confirm": response.get("result")}
            retry["params"] = retry_params
            return retry
        finally:
            with self._lock:
                session.pending.pop(elicitation_id, None)
                if active.consent_waiter is waiter:
                    active.consent_waiter = None

    def _pump_inner_stream(
        self,
        inner: Any,
        request_id: Any,
        version: str,
        outer: "queue.SimpleQueue",
    ) -> None:
        """Relay the modern blocking stream's single terminal message.

        ``_KEEPALIVE`` events are swallowed (the outer queue timeout
        produces the SSE heartbeat); ``None`` is normal completion;
        ``_StreamClosed`` is an inner failure. The inner stream carries no
        disconnect cancellation callback for legacy work, so finalization
        here can never cancel a running operation.
        """

        from mcp_server.http_server import _KEEPALIVE, _StreamClosed

        try:
            while True:
                try:
                    event = inner.next_event()
                except _StreamClosed:
                    inner._finalize(disconnected=True)
                    outer.put(
                        error_response(
                            ProtocolError(
                                INTERNAL_ERROR, "tool result stream failed."
                            ),
                            request_id,
                        )
                    )
                    return
                if event is None:
                    inner._finalize(disconnected=False)
                    return
                if event is _KEEPALIVE:
                    continue
                result = event.get("result") if isinstance(event, dict) else None
                if isinstance(result, dict) and result.get("resultType") in (
                    "input_required",
                    "task",
                ):
                    inner._finalize(disconnected=True)
                    outer.put(
                        error_response(
                            ProtocolError(
                                INTERNAL_ERROR,
                                "modern-only result cannot be delivered to a "
                                "legacy client",
                            ),
                            request_id,
                        )
                    )
                    return
                outer.put(legacy_result(event, version, "tools/call"))
                inner._finalize(disconnected=False)
                return
        except Exception:
            traceback.print_exc(file=sys.stderr)
            inner._finalize(disconnected=True)
            outer.put(
                error_response(
                    ProtocolError(INTERNAL_ERROR, "Unexpected server error."),
                    request_id,
                )
            )

    # ------------------------------------------------------------------
    # Batches (2025-03-26 only)
    # ------------------------------------------------------------------

    def _handle_batch(
        self,
        members: list,
        lowered: dict,
        session_id: Any,
        principal: str,
    ) -> LegacyReply:
        if session_id is None:
            return _error_reply(400, INVALID_REQUEST, INITIALIZE_FIRST_MESSAGE)
        session = self._lookup_session(session_id, principal)
        if session is None:
            return _error_reply(404, INVALID_REQUEST, UNKNOWN_SESSION_MESSAGE)
        if session.version != BATCH_REVISION:
            return _error_reply(
                400,
                INVALID_REQUEST,
                "JSON-RPC batches are only accepted for the 2025-03-26 revision",
            )
        if not members:
            return _error_reply(400, INVALID_REQUEST, "malformed envelope: empty batch")
        requests: list[dict] = []
        notifications: list[dict] = []
        responses: list[dict] = []
        for member in members:
            if not isinstance(member, dict) or member.get("jsonrpc") != JSONRPC_VERSION:
                return _error_reply(
                    400,
                    INVALID_REQUEST,
                    "malformed envelope: batch members must be JSON-RPC messages",
                )
            if "method" in member:
                if "id" in member:
                    requests.append(member)
                else:
                    notifications.append(member)
            else:
                responses.append(member)
        if responses:
            # Mixed responses are invalid, and a response-only batch has no
            # valid outstanding elicitation in 2025-03-26 (no elicitation
            # exists in that revision).
            return _error_reply(
                400,
                INVALID_REQUEST,
                "malformed envelope: batch must not contain JSON-RPC responses",
            )
        if len(requests) > MAX_BATCH_REQUEST_MEMBERS:
            return _error_reply(400, INVALID_REQUEST, TOO_MANY_REQUESTS_MESSAGE)
        for member in requests + notifications:
            envelope_error = self._envelope_error(member, require_request=False)
            if envelope_error is not None:
                return envelope_error
        seen_ids: set = set()
        for member in requests:
            if member["id"] in seen_ids:
                return _error_reply(
                    400,
                    INVALID_REQUEST,
                    "malformed envelope: duplicate request id in batch",
                )
            seen_ids.add(member["id"])
        if not requests:
            # Notifications pass through immediately; nothing can wait.
            for member in notifications:
                self._route_notification(session, member)
            return LegacyReply(202, None)
        # Reserve every request slot BEFORE routing notifications, so a
        # cancellation in the same batch finds its request registered and
        # can prevent it from ever starting.
        call_members = [m for m in requests if m["method"] == "tools/call"]
        reserved: list[tuple[dict, _ActiveRequest]] = []
        with self._lock:
            for member in requests:
                if member["id"] in session.active_requests:
                    return _error_reply(
                        400, INVALID_REQUEST, "duplicate active request id"
                    )
            free = MAX_INFLIGHT_LEGACY_REQUESTS - self._inflight
            overflow: list[dict] = []
            reservable = min(len(call_members), max(0, free))
            for member in call_members[:reservable]:
                active = _ActiveRequest(request_id=member["id"])
                session.active_requests[member["id"]] = active
                session.active += 1
                self._inflight += 1
                reserved.append((member, active))
            overflow = call_members[reservable:]
        for member in notifications:
            self._route_notification(session, member)
        inline = [
            (member, None) for member in requests if member["method"] != "tools/call"
        ]
        if overflow:
            # Excess tools/call members are rejected as SERVER_BUSY tool
            # results; they never reserve state.
            return self._spawn_overflow_batch(session, inline, overflow, reserved)
        return self._spawn_stream(session, [*inline, *reserved])

    def _spawn_overflow_batch(
        self,
        session: _LegacySession,
        inline: list[tuple[dict, _ActiveRequest | None]],
        overflow: list[dict],
        reserved: list[tuple[dict, _ActiveRequest]],
    ) -> LegacyReply:
        from mcp_server.http_server import StreamResponse  # local: import cycle

        outer: "queue.SimpleQueue" = queue.SimpleQueue()

        def produce() -> None:
            try:
                self._produce_member_loop(session, [*inline, *reserved], outer)
                for member in overflow:
                    outer.put(self._busy_tool_payload(session, member.get("id")))
            finally:
                outer.put(None)

        threading.Thread(target=produce, name="mcp-legacy-batch", daemon=True).start()
        return LegacyReply(200, None, stream=StreamResponse(outer))

    def _busy_tool_payload(self, session: _LegacySession, request_id: Any) -> dict:
        return _tool_error_response(
            request_id,
            session.version,
            ToolError(
                SERVER_BUSY,
                OPERATION_LIMIT_MESSAGE,
                {"reason": "operation_limit"},
            ),
        )


def _normalize_request(
    session: _LegacySession,
    message: dict,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Map a legacy request onto the modern dispatch dictionary shape.

    ``_meta`` is an internal view assembled from session state — it is not
    a claim about received wire fields. ``legacy_session_id`` and
    ``cancel_event`` are private non-wire keys consumed by ``Server``.
    """

    normalized = {
        "id": message.get("id"),
        "is_notification": "id" not in message,
        "method": message["method"],
        "params": message.get("params", {}),
        "_meta": {
            META_PROTOCOL_VERSION: session.version,
            META_CLIENT_INFO: dict(session.client_info),
            META_CLIENT_CAPABILITIES: dict(session.capabilities),
        },
        "protocol_version": session.version,
        "client_info": dict(session.client_info),
        "client_capabilities": dict(session.capabilities),
        "legacy_session_id": session.session_id,
    }
    if cancel_event is not None:
        normalized["cancel_event"] = cancel_event
    return normalized


def _normalize_capabilities(capabilities: Mapping[str, Any], version: str) -> dict:
    """Reduce declared capabilities to what the modern consent gate reads.

    ``2025-06-18`` elicitation presence means form support;
    ``2025-11-25`` requires ``elicitation.form`` or the backwards
    compatible empty elicitation object; ``2025-03-26`` has no elicitation.
    Task declarations are never copied into a modern extensions map, so
    legacy calls always receive final results.
    """

    if version == BATCH_REVISION:
        return {}
    elicitation = capabilities.get("elicitation")
    if version == "2025-06-18":
        return {"elicitation": {"form": {}}} if elicitation is not None else {}
    if isinstance(elicitation, Mapping) and (
        "form" in elicitation or len(elicitation) == 0
    ):
        return {"elicitation": {"form": {}}}
    return {}


def _reject_client_consent_fields(params: Mapping[str, Any]) -> LegacyReply | None:
    """Legacy clients may never inject consent state on the wire."""

    if "requestState" in params or "inputResponses" in params:
        return _error_reply(
            400,
            INVALID_REQUEST,
            "legacy tools/call must not carry requestState or inputResponses",
        )
    return None


def _is_stream(outcome: Any) -> bool:
    from mcp_server.http_server import StreamResponse  # local: import cycle

    return isinstance(outcome, StreamResponse)


def _tool_error_response(request_id: Any, version: str, error: ToolError) -> dict:
    """A complete legacy JSON-RPC response carrying one isError tool result."""

    return legacy_result(
        {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "result": tool_error_result(error),
        },
        version,
        "tools/call",
    )


def _cancelled_result(request_id: Any, version: str) -> dict:
    return _tool_error_response(
        request_id,
        version,
        ToolError(
            CONSENT_DENIED,
            CANCELLED_BEFORE_EXECUTION_MESSAGE,
            {"reason": "cancelled"},
        ),
    )


def _consent_denied_result(request_id: Any, version: str, reason: str) -> dict:
    return _tool_error_response(
        request_id,
        version,
        ToolError(
            CONSENT_DENIED,
            "Consent was not granted before the deadline",
            {"reason": reason},
        ),
    )


def _classify_elicitation(response: Any) -> str:
    """``accept`` (relay to the signer), ``decline`` (relay) or ``invalid``.

    A valid accept requires ``content.confirmed === true``. Decline and
    cancel are relayed so the existing signer records them with its own
    semantics. Anything else is invalid and ends the consent phase without
    another challenge.
    """

    if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
        return "invalid"
    result = response["result"]
    action = result.get("action")
    content = result.get("content")
    if action == "accept":
        if isinstance(content, Mapping) and content.get("confirmed") is True:
            return "accept"
        if isinstance(content, Mapping) and content.get("confirmed") is False:
            return "decline"
        return "invalid"
    if action in ("decline", "cancel"):
        return "decline"
    return "invalid"


def _error_reply(
    status: int, code: int, message: str, data: Any = None
) -> LegacyReply:
    return LegacyReply(status, error_response(ProtocolError(code, message, data)))
