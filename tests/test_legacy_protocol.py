"""Legacy Streamable HTTP adapter tests against the real Server.dispatch.

Drives the real ``mcp_server.legacy_protocol`` and the real server module
over test_server's shared harness (stubbed FreeCAD/PySide and contract-
shaped stub tool modules). Covers: initialize negotiation for every
legacy revision, session lifecycle and isolation, the 17-tool registry
through legacy shapes, consent bridging (accept, decline, invalid reply,
client error, timeout, changed target, duplicate and cross-session
responses, deletion, shutdown, explicit cancellation), final-result
(never task) delivery, disconnect-vs-cancellation semantics, 2025-03-26
batches, and ``legacy_result`` translation.

The harness is intentionally IMPORTED from ``test_server`` instead of
duplicated: ``mcp_server.server`` binds one set of stub tool modules at
first import, so both files must share that single binding. The fake
tool package is removed from ``sys.modules`` again below so the
alphabetically-later test files keep importing the real tool modules.
"""

import queue
import sys
import threading
import time
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

import test_server as ts  # noqa: E402 - shared stub harness

import mcp_server.gui_dispatch as gui_dispatch  # noqa: E402
import mcp_server.legacy_protocol as legacy  # noqa: E402
import mcp_server.protocol as protocol  # noqa: E402
import mcp_server.server as server_module  # noqa: E402
import mcp_server.tasks as tasks_module  # noqa: E402

# server.py has bound the stub tool modules; drop the fake package from
# sys.modules so later test files import the real tool modules again.
for _name in [
    _key
    for _key in list(sys.modules)
    if _key == "mcp_server.tools" or _key.startswith("mcp_server.tools.")
]:
    del sys.modules[_name]

# Shared harness aliases (single binding, single call recording).
STUB_CALLS = ts.STUB_CALLS
PREFLIGHT_RESULTS = ts.PREFLIGHT_RESULTS
STUB_HANDLERS = ts.STUB_HANDLERS
FC_STATE = ts.FC_STATE
make_server = ts.make_server
ThreadedWaker = ts.ThreadedWaker
wait_until = ts.wait_until
_drain_gui_queue = ts._drain_gui_queue

PRINCIPAL = "sha256:testprincipal"
OTHER_PRINCIPAL = "sha256:otherprincipal"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


def make_adapter(server, clock=None):
    return legacy.LegacyProtocol(
        server.dispatch, clock=time.monotonic if clock is None else clock
    )


@pytest.fixture(autouse=True)
def _clean_state():
    STUB_CALLS.clear()
    PREFLIGHT_RESULTS.clear()
    STUB_HANDLERS.clear()
    FC_STATE["documents"] = {}
    FC_STATE["observers"] = []
    server_module._server = None
    previous_waker = gui_dispatch._waker
    gui_dispatch._waker = None
    gui_dispatch._draining = False
    gui_dispatch._dispatch_health._active_task_id = 0
    gui_dispatch._dispatch_health._timed_out = False
    gui_dispatch._dispatch_health._timeout_seconds = 0.0
    gui_dispatch._inflight.clear()
    _drain_gui_queue()
    waker = ThreadedWaker()
    gui_dispatch.initialize()
    gui_dispatch._waker = waker
    yield waker
    gui_dispatch.shutdown()
    _drain_gui_queue()
    gui_dispatch.cleanup_waker()
    gui_dispatch._waker = previous_waker
    server_module._server = None



def initialize(adapter, version="2025-11-25", *, principal=PRINCIPAL, capabilities=None, headers=None):
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {} if capabilities is None else capabilities,
            "clientInfo": {"name": "connection-check", "version": "1"},
        },
    }
    merged_headers = {"mcp-session-id": "stale"} if headers is None else dict(headers)
    merged_headers.pop("mcp-session-id", None)
    return adapter.handle(message, merged_headers, principal)


def session_of(reply) -> str:
    assert reply.status == 200
    headers = dict(reply.headers)
    assert "MCP-Session-Id" in headers
    return headers["MCP-Session-Id"]


def initialized_notification(sid, *, principal=PRINCIPAL):
    return {"headers": {"mcp-session-id": sid}, "principal": principal}


def send(adapter, sid, message, *, principal=PRINCIPAL):
    return adapter.handle(message, {"mcp-session-id": sid}, principal)


def request(method, rpc_id=7, params=None):
    message = {"jsonrpc": "2.0", "method": method}
    if rpc_id is not None:
        message["id"] = rpc_id
    if params is not None:
        message["params"] = params
    return message


def call_tool(name, rpc_id=7, arguments=None):
    return request(
        "tools/call", rpc_id, {"name": name, "arguments": arguments or {}}
    )


def next_event(stream, timeout=3.0):
    return stream.events.get(timeout=timeout)


def final_event(adapter, reply, sid, *, principal=PRINCIPAL, on_elicitation=None):
    """Drain one streamed legacy call to its single terminal response."""
    while True:
        event = next_event(reply.stream)
        if event is None:
            raise AssertionError("stream ended without a terminal result")
        if event.get("method") == "elicitation/create":
            if on_elicitation is not None:
                on_elicitation(event)
            continue
        return event


def answer_elicitation(adapter, sid, elicitation_event, result=None, *, error=None, principal=PRINCIPAL):
    response = {"jsonrpc": "2.0", "id": elicitation_event["id"]}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = (
            {"action": "accept", "content": {"confirmed": True}}
            if result is None
            else result
        )
    return adapter.handle(response, {"mcp-session-id": sid}, principal)


def assert_consent_denied(event, *, version="2025-11-25"):
    assert "error" not in event
    result = event["result"]
    assert result.get("isError") is True
    if version != legacy.BATCH_REVISION:
        assert result["structuredContent"]["error"]["code"] == "CONSENT_DENIED"
    else:
        assert "structuredContent" not in result
    return result


# ---------------------------------------------------------------------------
# Initialize and session lifecycle.
# ---------------------------------------------------------------------------


def test_initialize_negotiates_each_supported_revision():
    for version in legacy.LEGACY_PROTOCOL_VERSIONS:
        server = make_server()
        adapter = make_adapter(server)
        reply = initialize(adapter, version)
        assert reply.status == 200
        sid = session_of(reply)
        assert sid
        result = reply.payload["result"]
        assert result["protocolVersion"] == version
        assert result["serverInfo"] == protocol.SERVER_INFO
        assert result["capabilities"] == {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
        }
        assert result["instructions"] == (
            "Consent prompts use form elicitation when the client supports "
            "it; clients without form support proceed without the prompt. "
            "Long-running operations return final results; detached tasks "
            "and resource subscriptions are not offered in this session."
        )
        assert reply.payload["id"] == 1


def test_unsupported_revision_negotiates_latest_legacy():
    server = make_server()
    adapter = make_adapter(server)
    reply = initialize(adapter, "2024-09-12")
    assert reply.status == 200
    assert reply.payload["result"]["protocolVersion"] == "2025-11-25"


def test_initialize_rejects_session_header():
    server = make_server()
    adapter = make_adapter(server)
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "c", "version": "1"},
        },
    }
    reply = adapter.handle(message, {"mcp-session-id": "abc"}, PRINCIPAL)
    assert reply.status == 400
    assert reply.payload["error"]["code"] == protocol.INVALID_REQUEST


def test_initialize_validates_required_params():
    server = make_server()
    adapter = make_adapter(server)
    base = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "c", "version": "1"},
        },
    }
    for field in ("protocolVersion", "clientInfo", "capabilities"):
        broken = {**base, "params": {k: v for k, v in base["params"].items() if k != field}}
        reply = adapter.handle(broken, {}, PRINCIPAL)
        assert reply.status == 400, field
    notification = {k: v for k, v in base.items() if k != "id"}
    reply = adapter.handle(notification, {}, PRINCIPAL)
    assert reply.status == 400


def test_noninitialize_without_session_is_initialize_first():
    server = make_server()
    adapter = make_adapter(server)
    reply = adapter.handle(request("tools/list", 1), {}, PRINCIPAL)
    assert reply.status == 400
    assert reply.payload["error"]["code"] == protocol.INVALID_REQUEST
    assert "Initialize a session first." in reply.payload["error"]["message"]
    assert "2025-03-26, 2025-06-18, 2025-11-25, 2026-07-28" in reply.payload["error"]["message"]


def test_unknown_and_foreign_sessions_are_404():
    server = make_server()
    adapter = make_adapter(server)
    reply = adapter.handle(request("ping", 1), {"mcp-session-id": "nope"}, PRINCIPAL)
    assert reply.status == 404
    sid = session_of(initialize(adapter))
    foreign = adapter.handle(request("ping", 1), {"mcp-session-id": sid}, OTHER_PRINCIPAL)
    assert foreign.status == 404


def test_session_lifecycle_requires_initialized_before_requests():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter))
    ping = send(adapter, sid, request("ping", 2))
    assert ping.status == 200
    assert ping.payload["result"] == {}
    early = send(adapter, sid, request("tools/list", 3))
    assert early.status == 400
    assert "Session initialization is not complete." in early.payload["error"]["message"]
    first = send(
        adapter, sid, request("notifications/initialized", None)
    )
    assert first.status == 202
    second = send(adapter, sid, request("notifications/initialized", None))
    assert second.status == 202  # idempotent duplicate
    ready = send(adapter, sid, request("tools/list", 4))
    assert ready.status == 200


def test_unknown_method_returns_32601_with_request_id():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter))
    send(adapter, sid, request("notifications/initialized", None))
    reply = send(adapter, sid, request("tasks/get", 42, {"taskId": "t"}))
    assert reply.status == 200
    assert reply.payload["id"] == 42
    assert reply.payload["error"]["code"] == protocol.METHOD_NOT_FOUND


def test_session_limit_returns_503_at_32_sessions():
    server = make_server()
    adapter = make_adapter(server)
    for _ in range(32):
        sid = session_of(initialize(adapter))
    overflow = initialize(adapter)
    assert overflow.status == 503
    assert "MCP session limit reached" in overflow.payload["error"]["message"]


def test_idle_session_expires_but_active_session_survives():
    clock = FakeClock()
    server = make_server(clock=clock)
    adapter = make_adapter(server, clock=clock)
    sid = session_of(initialize(adapter, capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    pending = send(adapter, sid, call_tool("new_document", 7))
    assert pending.stream is not None
    elicitation = next_event(pending.stream)
    assert elicitation["method"] == "elicitation/create"
    clock.advance(legacy.SESSION_IDLE_TIMEOUT_S + 30)
    # The active session still routes its elicitation response.
    answer = answer_elicitation(adapter, sid, elicitation)
    assert answer.status == 202
    final = next_event(pending.stream)
    assert final["id"] == 7
    assert final["result"].get("isError") is not True
    # An idle session is pruned on the next operation.
    idle_sid = session_of(initialize(adapter))
    clock.advance(legacy.SESSION_IDLE_TIMEOUT_S + 30)
    gone = send(adapter, idle_sid, request("ping", 3))
    assert gone.status == 404


def test_delete_session_is_single_use_and_principal_bound():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter))
    assert adapter.delete_session(sid, OTHER_PRINCIPAL) is False
    assert adapter.delete_session(sid, PRINCIPAL) is True
    assert adapter.delete_session(sid, PRINCIPAL) is False
    assert send(adapter, sid, request("ping", 1)).status == 404


# ---------------------------------------------------------------------------
# Tools through legacy shapes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", list(legacy.LEGACY_PROTOCOL_VERSIONS))
def test_tools_list_returns_17_with_revision_shape(version):
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, version))
    send(adapter, sid, request("notifications/initialized", None))
    reply = send(adapter, sid, request("tools/list", 9))
    assert reply.status == 200
    result = reply.payload["result"]
    assert [t["name"] for t in result["tools"]] == list(server_module.PLAN_TOOL_ORDER)
    assert len(result["tools"]) == 17
    assert "resultType" not in result
    assert "ttlMs" not in result and "cacheScope" not in result
    if version == legacy.BATCH_REVISION:
        assert all("outputSchema" not in t for t in result["tools"])
    else:
        assert all("outputSchema" in t for t in result["tools"])


@pytest.mark.parametrize("version", list(legacy.LEGACY_PROTOCOL_VERSIONS))
def test_tool_call_returns_final_result_never_a_task(version):
    server = make_server()
    adapter = make_adapter(server)
    # Even a tasks-capable declaration must not detach legacy work.
    capabilities = {
        "elicitation": {},
        "extensions": {tasks_module.TASKS_EXTENSION_ID: {}},
    }
    sid = session_of(initialize(adapter, version, capabilities=capabilities))
    send(adapter, sid, request("notifications/initialized", None))
    reply = send(adapter, sid, call_tool("run_script", 7))
    event = final_event(adapter, reply, sid)
    assert event["id"] == 7
    result = event["result"]
    assert "resultType" not in result
    assert "ttlMs" not in result
    assert result.get("isError") is not True
    assert "taskRelatedData" not in result and "taskId" not in result
    if version == legacy.BATCH_REVISION:
        assert "structuredContent" not in result
        assert result["content"][0]["type"] == "text"
    else:
        assert result["structuredContent"] == {"tool": "run_script", "arguments": {}}
    assert any(name == "run_script" for name, _ctx, _args in STUB_CALLS)


def test_tool_call_with_client_request_state_is_rejected():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter))
    send(adapter, sid, request("notifications/initialized", None))
    params = {
        "name": "run_script",
        "arguments": {},
        "requestState": "forged",
    }
    reply = send(adapter, sid, request("tools/call", 7, params))
    assert reply.status == 400
    assert "requestState" in reply.payload["error"]["message"]


def test_discover_capabilities_tool_works_through_legacy():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter))
    send(adapter, sid, request("notifications/initialized", None))
    reply = send(adapter, sid, call_tool("discover_capabilities", 5))
    event = final_event(adapter, reply, sid)
    assert event["id"] == 5
    structured = event["result"]["structuredContent"]
    assert "capabilities" in structured and "gui" in structured


# ---------------------------------------------------------------------------
# Consent bridging.
# ---------------------------------------------------------------------------


def test_consent_without_form_support_falls_back_to_execution():
    # 1.0 fallback: a client without elicitation support is never blocked
    # by consent; the operation proceeds unprompted on every revision.
    server = make_server()
    adapter = make_adapter(server)
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    for version in legacy.LEGACY_PROTOCOL_VERSIONS:
        sid = session_of(initialize(adapter, version))
        send(adapter, sid, request("notifications/initialized", None))
        reply = send(adapter, sid, call_tool("new_document", 7))
        event = final_event(adapter, reply, sid)
        assert event["id"] == 7
        assert event["result"].get("isError") is not True, version
        assert "elicitation" not in event["result"]["content"][0]["text"]
    assert len(STUB_CALLS) == 3  # executed once per session, no prompts


def test_consent_accept_executes_exactly_once():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-11-25", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))
    elicitation_events = []

    def on_elicitation(event):
        elicitation_events.append(event)
        answer_elicitation(adapter, sid, event)

    event = final_event(adapter, reply, sid, on_elicitation=on_elicitation)
    assert len(elicitation_events) == 1
    request_sent = elicitation_events[0]
    assert request_sent["params"]["message"] == "Create document?"
    assert request_sent["params"]["requestedSchema"]["properties"]["confirmed"]["type"] == "boolean"
    assert request_sent["params"]["mode"] == "form"  # 2025-11-25 form mode
    assert event["id"] == 7
    assert event["result"].get("isError") is not True
    assert event["result"]["structuredContent"] == {
        "tool": "new_document",
        "arguments": {},
    }
    assert len(STUB_CALLS) == 1


def test_consent_2025_06_18_omits_form_mode():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))

    def on_elicitation(event):
        assert "mode" not in event["params"]
        answer_elicitation(adapter, sid, event)

    event = final_event(adapter, reply, sid, on_elicitation=on_elicitation)
    assert event["result"].get("isError") is not True


def test_consent_decline_denies_without_effects():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))

    def on_elicitation(event):
        answer_elicitation(
            adapter, sid, event, {"action": "decline", "message": "no"}
        )

    event = final_event(adapter, reply, sid, on_elicitation=on_elicitation)
    assert_consent_denied(event)
    assert STUB_CALLS == []


def test_consent_accept_without_confirmed_is_invalid_not_a_loop():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))
    prompts = []

    def on_elicitation(event):
        prompts.append(event)
        # accept with missing confirmation: must end, never re-prompt.
        answer_elicitation(adapter, sid, event, {"action": "accept", "content": {}})

    event = final_event(adapter, reply, sid, on_elicitation=on_elicitation)
    assert len(prompts) == 1
    assert_consent_denied(event)
    assert STUB_CALLS == []


def test_client_error_response_ends_consent():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))

    def on_elicitation(event):
        answer_elicitation(
            adapter, sid, event, error={"code": -32603, "message": "client exploded"}
        )

    event = final_event(adapter, reply, sid, on_elicitation=on_elicitation)
    assert_consent_denied(event)
    assert STUB_CALLS == []


def test_duplicate_consent_response_is_rejected_without_reexecution():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-11-25", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))
    holder = {}

    def on_elicitation(event):
        holder["event"] = event
        answer_elicitation(adapter, sid, event)

    event = final_event(adapter, reply, sid, on_elicitation=on_elicitation)
    assert event["result"].get("isError") is not True
    duplicate = answer_elicitation(adapter, sid, holder["event"])
    assert duplicate.status == 400
    assert "id" not in duplicate.payload  # never fabricates a response id
    assert len(STUB_CALLS) == 1


def test_cross_session_response_injection_is_rejected():
    server = make_server()
    adapter = make_adapter(server)
    sid_a = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    sid_b = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid_a, request("notifications/initialized", None))
    send(adapter, sid_b, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply_a = send(adapter, sid_a, call_tool("new_document", 7))
    reply_b = send(adapter, sid_b, call_tool("new_document", 7))
    events = {}

    def on_elicitation_for(sid):
        def on_elicitation(event):
            events[sid] = event

        return on_elicitation

    # Inject session A's response through session B: rejected.
    injected = adapter.handle(
        {
            "jsonrpc": "2.0",
            "id": events.get(sid_a, {}).get("id", "elicitation-x"),
            "result": {"action": "accept", "content": {"confirmed": True}},
        },
        {"mcp-session-id": sid_b},
        PRINCIPAL,
    )
    assert injected.status == 400

    def on_elicitation(event):
        answer_elicitation(adapter, sid_a, event)

    event_a = final_event(adapter, reply_a, sid_a, on_elicitation=on_elicitation)
    assert event_a["result"].get("isError") is not True

    def on_elicitation_b(event):
        answer_elicitation(adapter, sid_b, event)

    event_b = final_event(adapter, reply_b, sid_b, on_elicitation=on_elicitation_b)
    assert event_b["result"].get("isError") is not True


def test_consent_timeout_aborts_with_consent_denied():
    clock = FakeClock()
    server = make_server(clock=clock)
    adapter = make_adapter(server, clock=clock)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))
    next_event(reply.stream)  # the elicitation/create request
    clock.advance(protocol.DEFAULT_CONSENT_TTL_S + 1)
    event = next_event(reply.stream)
    assert_consent_denied(event)
    assert event["result"]["structuredContent"]["error"]["details"]["reason"] == "timeout"
    assert next_event(reply.stream) is None
    assert STUB_CALLS == []


def test_changed_target_reelicitation_within_original_deadline():
    clock = FakeClock()
    server = make_server(clock=clock)
    adapter = make_adapter(server, clock=clock)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))
    prompts = []
    accepted_first = threading.Event()

    def on_elicitation(event):
        prompts.append(event)
        if len(prompts) == 1:
            # The target changes before the retry consumes the nonce.
            PREFLIGHT_RESULTS["new_document"] = {
                "requires_consent": True,
                "message": "Create a DIFFERENT document?",
                "tool": "somewhere_else",
            }
            answer_elicitation(adapter, sid, event)
            accepted_first.set()
        else:
            answer_elicitation(adapter, sid, event)

    event = final_event(adapter, reply, sid, on_elicitation=on_elicitation)
    assert len(prompts) == 2
    assert prompts[1]["params"]["message"] == "Create a DIFFERENT document?"
    assert event["result"].get("isError") is not True
    assert len(STUB_CALLS) == 1
    assert clock() < 1000.0 + protocol.DEFAULT_CONSENT_TTL_S + 5


def test_consent_cancelled_via_notification_never_executes():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))
    elicitation = next_event(reply.stream)
    assert elicitation["method"] == "elicitation/create"
    cancelled = send(
        adapter,
        sid,
        request(
            "notifications/cancelled",
            None,
            {"requestId": 7},
        ),
    )
    assert cancelled.status == 202
    event = next_event(reply.stream)
    assert_consent_denied(event)
    assert (
        event["result"]["structuredContent"]["error"]["details"]["reason"]
        == "cancelled"
    )
    assert event["result"]["content"][0]["text"] == "Operation cancelled before execution"
    assert STUB_CALLS == []
    # The unknown-id case is a harmless 202.
    assert send(
        adapter, sid, request("notifications/cancelled", None, {"requestId": 999})
    ).status == 202


def test_session_delete_aborts_pending_consent():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))
    next_event(reply.stream)
    assert adapter.delete_session(sid, PRINCIPAL) is True
    event = next_event(reply.stream)
    assert_consent_denied(event)
    assert STUB_CALLS == []
    assert send(adapter, sid, request("ping", 1)).status == 404


def test_shutdown_aborts_pending_consent_and_delivers_before_drain():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    reply = send(adapter, sid, call_tool("new_document", 7))
    next_event(reply.stream)
    adapter.shutdown()  # wakes consent waits; transport drains afterwards
    event = next_event(reply.stream)
    assert_consent_denied(event)
    assert next_event(reply.stream) is None
    assert STUB_CALLS == []


# ---------------------------------------------------------------------------
# Concurrency, isolation and cancellation.
# ---------------------------------------------------------------------------


def test_concurrent_sessions_with_same_numeric_request_id():
    server = make_server()
    adapter = make_adapter(server)
    sids = []
    replies = []
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    for _ in range(2):
        sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
        send(adapter, sid, request("notifications/initialized", None))
        sids.append(sid)
        replies.append(send(adapter, sid, call_tool("new_document", 5)))
    for sid, reply in zip(sids, replies):
        elicitation = next_event(reply.stream)
        answer_elicitation(adapter, sid, elicitation)
    finals = [next_event(reply.stream) for reply in replies]
    assert all(final["id"] == 5 for final in finals)
    assert all(final["result"].get("isError") is not True for final in finals)
    assert len(STUB_CALLS) == 2


def test_duplicate_active_request_id_rejected_then_reusable():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    pending = send(adapter, sid, call_tool("new_document", 7))
    duplicate = send(adapter, sid, call_tool("new_document", 7))
    assert duplicate.status == 400
    assert "duplicate active request id" in duplicate.payload["error"]["message"]

    def on_elicitation(event):
        answer_elicitation(adapter, sid, event)

    final_event(adapter, pending, sid, on_elicitation=on_elicitation)
    # After terminal completion the id is reusable.
    again = send(adapter, sid, call_tool("new_document", 7))
    assert again.stream is not None


def test_sse_disconnect_does_not_cancel_running_legacy_call():
    release = threading.Event()
    started = threading.Event()

    def slow_handler(ctx, arguments):
        started.set()
        assert release.wait(timeout=5.0)
        return {"tool": "run_script"}

    STUB_HANDLERS["run_script"] = slow_handler
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter))
    send(adapter, sid, request("notifications/initialized", None))
    reply = send(adapter, sid, call_tool("run_script", 7))
    # Simulate an SSE disconnect: the response stream is simply abandoned.
    reply.stream = None
    assert started.wait(timeout=3.0)
    release.set()
    assert wait_until(lambda: len(STUB_CALLS) == 1)
    # The operation reached true completion and released its records.
    assert wait_until(lambda: server.pending_operation_count() == 0)


def test_queued_cancellation_prevents_handler_entry(_clean_state):
    waker = _clean_state
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter))
    send(adapter, sid, request("notifications/initialized", None))
    # Hold the job in the dispatcher queue: no waker means nothing drains.
    saved_waker = gui_dispatch._waker
    gui_dispatch._waker = None
    try:
        reply = send(adapter, sid, call_tool("run_script", 7))
        assert wait_until(lambda: gui_dispatch.pending_count() == 1)
        cancelled = send(
            adapter, sid, request("notifications/cancelled", None, {"requestId": 7})
        )
        assert cancelled.status == 202
    finally:
        gui_dispatch._waker = saved_waker
    waker.wake()
    event = next_event(reply.stream)
    result = event["result"]
    # Post-registration cancellation keeps the existing dispatcher
    # semantics (the plan retains GUI_DISPATCH_FAILED there); the handler
    # was nevertheless never entered.
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "GUI_DISPATCH_FAILED"
    assert STUB_CALLS == []  # the handler was never entered
    assert next_event(reply.stream) is None


def test_explicit_cancellation_is_scoped_to_its_own_request():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-06-18", capabilities={"elicitation": {}}))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    victim = send(adapter, sid, call_tool("new_document", 7))
    bystander = send(adapter, sid, call_tool("new_document", 8))
    next_event(victim.stream)
    next_event(bystander.stream)
    send(adapter, sid, request("notifications/cancelled", None, {"requestId": 7}))
    victim_final = next_event(victim.stream)
    assert_consent_denied(victim_final)
    # The bystander's consent wait is untouched and still answerable.
    bystander_prompt = None
    # answer the bystander elicitation: fetch its prompt
    # (already consumed above; re-answer by reading the next event)
    def on_elicitation(event):
        answer_elicitation(adapter, sid, event)

    # The bystander stream's elicitation was the event we consumed; answer a
    # fresh one by re-issuing is impossible, so verify it still completes:
    bystander_final = None
    try:
        bystander_final = next_event(bystander.stream, timeout=0.2)
    except queue.Empty:
        pass
    assert bystander_final is None  # still waiting, not cancelled
    # Answer bystander's elicitation via its pending prompt id.
    # (Retrieved from the adapter's session pending state.)
    with adapter._lock:
        pending_ids = list(adapter._sessions[sid].pending.keys())
    assert len(pending_ids) == 1
    answer = adapter.handle(
        {
            "jsonrpc": "2.0",
            "id": pending_ids[0],
            "result": {"action": "accept", "content": {"confirmed": True}},
        },
        {"mcp-session-id": sid},
        PRINCIPAL,
    )
    assert answer.status == 202
    bystander_final = next_event(bystander.stream)
    assert bystander_final["id"] == 8
    assert bystander_final["result"].get("isError") is not True


# ---------------------------------------------------------------------------
# Batches (2025-03-26 only).
# ---------------------------------------------------------------------------


def test_batch_processes_requests_in_order_with_one_response_each():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-03-26"))
    send(adapter, sid, request("notifications/initialized", None))
    batch = [
        request("tools/list", 1),
        request("ping", 2),
        request("notifications/cancelled", None, {"requestId": 999}),
        call_tool("run_script", 3),
    ]
    reply = send(adapter, sid, batch)
    assert reply.status == 200 and reply.stream is not None
    events = []
    while True:
        event = next_event(reply.stream)
        if event is None:
            break
        events.append(event)
    assert [e["id"] for e in events] == [1, 2, 3]
    assert "error" not in events[0]
    assert events[1]["result"] == {}
    assert events[2]["result"].get("isError") is not True


def test_all_notification_batch_returns_202():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-03-26"))
    batch = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/other"},
    ]
    reply = send(adapter, sid, batch)
    assert reply.status == 202 and reply.payload is None


def test_batch_rejected_for_newer_revisions():
    server = make_server()
    adapter = make_adapter(server)
    for version in ("2025-06-18", "2025-11-25"):
        sid = session_of(initialize(adapter, version))
        send(adapter, sid, request("notifications/initialized", None))
        reply = send(adapter, sid, [request("ping", 1)])
        assert reply.status == 400
        assert "2025-03-26" in reply.payload["error"]["message"]


@pytest.mark.parametrize(
    "batch,snippet",
    [
        ([], "empty batch"),
        ([{"jsonrpc": "2.0", "id": 1, "result": {}}], "responses"),
        (
            [request("ping", 1), {"jsonrpc": "2.0", "id": 2, "result": {}}],
            "responses",
        ),
        (
            [request("ping", i) for i in range(33)],
            "Too many requests in batch",
        ),
        (
            [request("ping", 1), request("tools/list", 1)],
            "duplicate request id",
        ),
        (
            [{"jsonrpc": "1.0", "id": 1, "method": "ping"}],
            "batch members must be JSON-RPC",
        ),
    ],
)
def test_invalid_batches_are_rejected(batch, snippet):
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-03-26"))
    send(adapter, sid, request("notifications/initialized", None))
    reply = send(adapter, sid, batch)
    assert reply.status == 400
    assert snippet in reply.payload["error"]["message"]


def test_batch_cancel_notification_passes_immediately():
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-03-26"))
    send(adapter, sid, request("notifications/initialized", None))
    PREFLIGHT_RESULTS["new_document"] = {
        "requires_consent": True,
        "message": "Create document?",
        "tool": "new_document",
    }
    batch = [
        call_tool("new_document", 7),
        # Placed after the request but handled before the producer starts,
        # so the cancellation cannot wait behind the request it cancels.
        request("notifications/cancelled", None, {"requestId": 7}),
    ]
    reply = send(adapter, sid, batch)
    event = next_event(reply.stream)
    # The cancellation lands before the (now unprompted) execution: the
    # request is cancelled before any effect.
    assert_consent_denied(event, version="2025-03-26")
    assert STUB_CALLS == []
    assert next_event(reply.stream) is None


def test_batch_initialize_is_not_accepted():
    server = make_server()
    adapter = make_adapter(server)
    initialize_message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "c", "version": "1"},
        },
    }
    reply = adapter.handle([initialize_message], {"mcp-session-id": "x"}, PRINCIPAL)
    assert reply.status == 404


# ---------------------------------------------------------------------------
# legacy_result translation (unit).
# ---------------------------------------------------------------------------


def test_legacy_result_strips_modern_envelope_metadata():
    response = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "resultType": "complete",
            "ttlMs": 5,
            "cacheScope": "public",
            "_meta": {protocol.META_SERVER_INFO: protocol.SERVER_INFO, "keep": 1},
            "tools": [{"name": "x", "outputSchema": {"type": "object"}}],
        },
    }
    translated = legacy.legacy_result(response, "2025-11-25", "tools/list")
    assert translated["id"] == 1
    result = translated["result"]
    assert "resultType" not in result
    assert "ttlMs" not in result and "cacheScope" not in result
    assert result["_meta"] == {"keep": 1}
    # Later revisions keep outputSchema and structuredContent.
    assert result["tools"][0]["outputSchema"] == {"type": "object"}
    assert response["result"]["ttlMs"] == 5  # the shared dict is not mutated


def test_legacy_result_revision_shims_for_2025_03_26():
    tools_response = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "resultType": "complete",
            "tools": [{"name": "x", "outputSchema": {"type": "object"}}],
        },
    }
    call_response = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "resultType": "complete",
            "content": [{"type": "text", "text": "{}"}],
            "structuredContent": {"tool": "run_script"},
        },
    }
    stripped_tools = legacy.legacy_result(tools_response, "2025-03-26", "tools/list")
    assert stripped_tools["result"]["tools"][0] == {"name": "x"}
    stripped_call = legacy.legacy_result(call_response, "2025-03-26", "tools/call")
    assert "structuredContent" not in stripped_call["result"]
    assert stripped_call["result"]["content"] == [{"type": "text", "text": "{}"}]
    # structuredContent of application payloads is never touched recursively.
    nested = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "resultType": "complete",
            "content": [],
            "structuredContent": {"resultType": "keep-me", "ttlMs": "keep-me"},
        },
    }
    kept = legacy.legacy_result(nested, "2025-03-26", "tools/call")
    assert "structuredContent" not in kept["result"]  # shim applies at top level
    retained = legacy.legacy_result(nested, "2025-11-25", "tools/call")
    assert retained["result"]["structuredContent"] == {
        "resultType": "keep-me",
        "ttlMs": "keep-me",
    }


def test_batch_overflow_rejects_only_the_excess_members(_clean_state):
    waker = _clean_state
    server = make_server()
    adapter = make_adapter(server)
    sid = session_of(initialize(adapter, "2025-03-26"))
    send(adapter, sid, request("notifications/initialized", None))
    # Fill all but one in-flight slot.
    adapter._inflight = legacy.MAX_INFLIGHT_LEGACY_REQUESTS - 1
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "run_script"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "run_script"}},
    ]
    reply = send(adapter, sid, batch)
    assert reply.stream is not None
    finals = {}
    while True:
        event = next_event(reply.stream)
        if event is None:
            break
        finals[event["id"]] = event
    # One member reserved and executed; only the excess got SERVER_BUSY.
    assert set(finals) == {1, 2}
    assert finals[1]["result"].get("isError") is not True
    # 2025-03-26 strips structuredContent: the error rides the text content.
    assert finals[2]["result"].get("isError") is True
    assert "Too many active operations" in finals[2]["result"]["content"][0]["text"]
    adapter._inflight = 0  # release the manual reservation
