"""Isolated tests for the v2 server orchestration (mcp_server/server).

Runs the real server module against stubbed FreeCAD/PySide and stubbed tool
modules (contract-shaped), defending: the exact 17-tool registry and plan
order, GUI-independent discovery/list, wire result envelopes, task creation
and lifecycle, the Tasks capability gate, consent preflight ordering and
single-use nonces, the shared 32-operation cap, blocking deadline
semantics, document observer generations with resources/updated events, and
startup failure cleanup / duplicate / refused-restart semantics.

No sockets: requests are driven through ``protocol.validate_request`` +
``Server.dispatch`` exactly as the HTTP layer does.
"""

import queue
from concurrent.futures import Future
from pathlib import Path
import sys
import threading
import time
import types

import pytest

# ---------------------------------------------------------------------------
# Runtime stubs (installed before mcp_server.server is imported).
# ---------------------------------------------------------------------------

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

FC_STATE = {
    "version": ["1", "1", "3", "dev"],
    "documents": {},  # name -> FakeDoc
    "observers": [],
}


class FakeConsole:
    messages: list[str] = []

    @staticmethod
    def PrintMessage(message: str) -> None:
        FakeConsole.messages.append(str(message))

    @staticmethod
    def PrintError(message: str) -> None:
        FakeConsole.messages.append(str(message))


class FakeDoc:
    def __init__(self, name: str, label: str | None = None) -> None:
        self.Name = name
        self.Label = label or name
        self.FileName = ""
        self.Objects: list = []


def _install_freeCAD_stubs() -> None:
    freecad = types.ModuleType("FreeCAD")
    freecad.Console = FakeConsole

    def _version() -> list:
        return list(FC_STATE["version"])

    freecad.Version = _version
    freecad.listDocuments = lambda: dict(FC_STATE["documents"])
    freecad.addDocumentObserver = lambda obs: FC_STATE["observers"].append(obs)
    freecad.removeDocumentObserver = lambda obs: (
        FC_STATE["observers"].remove(obs) if obs in FC_STATE["observers"] else None
    )
    freecad.ConfigGet = lambda _key: ""
    for getter in (
        "getHomePath",
        "getUserAppDataDir",
        "getUserMacroDir",
        "getTempPath",
        "getResourceDir",
    ):
        setattr(freecad, getter, lambda _g=getter: f"/tmp/fc-stub/{getter}")

    freecad_gui = types.ModuleType("FreeCADGui")
    freecad_gui.listWorkbenches = lambda: {"Part": object(), "Mesh": object()}
    freecad_gui.updateGui = lambda: None

    timer_calls: list = []
    qt_core = types.SimpleNamespace(
        QObject=object,
        Signal=lambda *_args, **_kw: type(
            "FakeSignal",
            (),
            {
                "__init__": lambda self: None,
                "connect": lambda self, cb, *_a: None,
                "emit": lambda self: None,
            },
        )(),
        Qt=types.SimpleNamespace(QueuedConnection=0, NoButton=0, WaitCursor=0),
        QEventLoop=types.SimpleNamespace(
            ExcludeUserInputEvents=1, ExcludeSocketNotifiers=2
        ),
        QThread=types.SimpleNamespace(msleep=lambda _d: None),
        QTimer=types.SimpleNamespace(
            singleShot=lambda delay, cb: timer_calls.append((delay, cb))
        ),
    )
    qt_widgets = types.SimpleNamespace(
        QApplication=types.SimpleNamespace(
            mouseButtons=lambda: 0,
            activePopupWidget=lambda: None,
            activeModalWidget=lambda: None,
            instance=lambda: None,  # real Qt returns None without an app
        )
    )
    pyside = types.ModuleType("PySide")
    pyside.QtCore = qt_core
    pyside.QtWidgets = qt_widgets

    sys.modules["FreeCAD"] = freecad
    sys.modules["FreeCADGui"] = freecad_gui
    sys.modules["PySide"] = pyside
    sys.modules["PySide.QtCore"] = qt_core
    sys.modules["PySide.QtWidgets"] = qt_widgets
    _install_freeCAD_stubs.timer_calls = timer_calls  # type: ignore[attr-defined]


_install_freeCAD_stubs()


# ---------------------------------------------------------------------------
# Stub tool modules (mcp_server.tools.*), contract-shaped.
# ---------------------------------------------------------------------------

STUB_CALLS: list[tuple[str, object, dict]] = []
PREFLIGHT_RESULTS: dict[str, dict | None] = {}
STUB_HANDLERS: dict[str, object] = {}

_DOCUMENT_TOOLS = (
    "new_document",
    "open_document",
    "save_document",
    "close_document",
    "reload_document",
)
_OBJECT_TOOLS = (
    "inspect_objects",
    "create_object",
    "edit_object",
    "delete_object",
)
_GEOMETRY_TOOLS = ("validate_geometry", "measure")
_OTHER_TOOLS = {
    "parameters": ("edit_parameters",),
    "export": ("export",),
    "view": ("capture_view",),
    "fem": ("run_fem",),
    "script": ("run_script",),
}
CONSENT_TOOLS = _DOCUMENT_TOOLS + _GEOMETRY_TOOLS  # documents + export preflights


def _stub_schema() -> dict:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def _stub_definition(name: str) -> dict:
    return {
        "name": name,
        "description": f"stub {name}",
        "inputSchema": _stub_schema(),
        "outputSchema": {"type": "object"},
    }


def _make_handler(name: str):
    def handler(ctx, arguments):
        STUB_CALLS.append((name, ctx, dict(arguments)))
        override = STUB_HANDLERS.get(name)
        if override is not None:
            return override(ctx, arguments)
        return {"tool": name, "arguments": dict(arguments)}

    return handler


def _documents_preflight(ctx, name, arguments):
    return PREFLIGHT_RESULTS.get(name)


def _export_preflight(ctx, name, arguments):
    return PREFLIGHT_RESULTS.get(name)


def _install_tool_modules() -> None:
    package = types.ModuleType("mcp_server.tools")
    package.__path__ = []  # namespace-style marker; submodules are injected
    sys.modules["mcp_server.tools"] = package

    def _module(modname, tools, preflight):
        mod = types.ModuleType(f"mcp_server.tools.{modname}")
        mod.TOOL_DEFINITIONS = [_stub_definition(t) for t in tools]
        mod.HANDLERS = {t: _make_handler(t) for t in tools}
        if preflight is not None:
            mod.preflight = preflight
        sys.modules[f"mcp_server.tools.{modname}"] = mod
        setattr(package, modname, mod)

    _module("documents", _DOCUMENT_TOOLS, _documents_preflight)
    _module("objects", _OBJECT_TOOLS, None)
    _module("geometry", _GEOMETRY_TOOLS, None)
    _module("parameters", _OTHER_TOOLS["parameters"], None)
    _module("export", _OTHER_TOOLS["export"], _export_preflight)
    _module("view", _OTHER_TOOLS["view"], None)
    _module("fem", _OTHER_TOOLS["fem"], None)
    _module("script", _OTHER_TOOLS["script"], None)


_install_tool_modules()

import mcp_server.gui_dispatch as gui_dispatch  # noqa: E402
import mcp_server.protocol as protocol  # noqa: E402
import mcp_server.server as server_module  # noqa: E402
import mcp_server.subscriptions as subs_module  # noqa: E402
import mcp_server.tasks as tasks_module  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

PRINCIPAL = "sha256:testprincipal"
CONN = "conn-1"
TASKS_CAPS = {"extensions": {tasks_module.TASKS_EXTENSION_ID: {}}}
META_PROTOCOL_VERSION = protocol.META_PROTOCOL_VERSION
META_CLIENT_INFO = protocol.META_CLIENT_INFO
META_CLIENT_CAPABILITIES = protocol.META_CLIENT_CAPABILITIES
META_SERVER_INFO = protocol.META_SERVER_INFO
META_SUBSCRIPTION_ID = protocol.META_SUBSCRIPTION_ID


class ThreadedWaker:
    """Drain the GUI job queue from background threads (real-Qt-like)."""

    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []

    def wake(self) -> None:
        thread = threading.Thread(
            target=lambda: gui_dispatch.process_gui_tasks(reschedule=False),
            daemon=True,
        )
        self.threads.append(thread)
        thread.start()

    def join(self, timeout: float = 5.0) -> None:
        for thread in list(self.threads):
            thread.join(timeout=timeout)


def make_server(clock=None) -> "server_module.Server":
    # A None clock must never reach Server: deadlines are monotonic-clock
    # based, so the fixture default is time.monotonic.
    return server_module.Server(
        settings={
            "port": 9876,
            "token": "test-token",
            "auto_start": False,
            "allowed_ips": "127.0.0.1",
            "allowed_roots": ["/tmp/fc-test"],
        },
        signer=protocol.ConsentSigner(ttl_s=60.0),
        task_store=tasks_module.TaskStore(),
        registry=subs_module.SubscriptionRegistry(),
        clock=time.monotonic if clock is None else clock,
    )


def validated_view(method, params=None, *, rpc_id=1, capabilities=None):
    params = dict(params or {})
    params["_meta"] = {
        META_PROTOCOL_VERSION: "2026-07-28",
        META_CLIENT_INFO: {"name": "t", "version": "1"},
        META_CLIENT_CAPABILITIES: {} if capabilities is None else capabilities,
    }
    message = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
    headers = {"mcp-protocol-version": "2026-07-28", "mcp-method": method}
    name_sources = {
        "tools/call": "name",
        "resources/read": "uri",
        "tasks/get": "taskId",
        "tasks/update": "taskId",
        "tasks/cancel": "taskId",
    }
    source = name_sources.get(method)
    if source is not None and source in params:
        headers["mcp-name"] = str(params[source])
    return protocol.validate_request(message, headers)


def dispatch(server, method, params=None, *, rpc_id=1, capabilities=None):
    view = validated_view(method, params, rpc_id=rpc_id, capabilities=capabilities)
    return server.dispatch(view, PRINCIPAL, CONN)


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _drain_gui_queue() -> int:
    """Drop stale dispatcher queue items so the next GUI tick runs jobs.

    ``gui_dispatch.shutdown()`` (teardown, and module-level stop paths)
    enqueues a stop sentinel; a sentinel left behind would make the next
    test's first drain tick swallow it and return, leaving real jobs
    queued until a 30s preflight timeout — the artifact://255 failure.
    """

    drained = 0
    while True:
        try:
            gui_dispatch._gui_request_queue.get_nowait()
        except queue.Empty:
            return drained
        drained += 1


def install_waker() -> ThreadedWaker:
    waker = ThreadedWaker()
    gui_dispatch._waker = waker
    return waker


def drain_stream(stream, expected: int, timeout: float = 3.0) -> list:
    events = []
    for _ in range(expected):
        events.append(stream.events.get(timeout=timeout))
    return events


@pytest.fixture(autouse=True)
def _clean_state():
    STUB_CALLS.clear()
    PREFLIGHT_RESULTS.clear()
    STUB_HANDLERS.clear()
    FC_STATE["documents"] = {}
    FC_STATE["observers"] = []
    FakeConsole.messages.clear()
    server_module._server = None
    previous_waker = gui_dispatch._waker
    gui_dispatch._waker = None
    gui_dispatch._draining = False
    gui_dispatch._dispatch_health._active_task_id = 0
    gui_dispatch._dispatch_health._timed_out = False
    gui_dispatch._dispatch_health._timeout_seconds = 0.0
    gui_dispatch._inflight.clear()
    _drain_gui_queue()
    yield
    gui_dispatch.shutdown()
    _drain_gui_queue()  # drop the sentinel shutdown just queued
    gui_dispatch.cleanup_waker()
    gui_dispatch._waker = previous_waker
    server_module._server = None


def _reset_dispatcher_for_tests() -> ThreadedWaker:
    gui_dispatch.initialize()
    return install_waker()


# ---------------------------------------------------------------------------
# Registry and discovery.
# ---------------------------------------------------------------------------


def test_tools_list_returns_exactly_17_in_plan_order():
    server = make_server()
    response = dispatch(server, "tools/list")
    result = response["result"]
    names = [tool["name"] for tool in result["tools"]]
    assert names == list(server_module.PLAN_TOOL_ORDER)
    assert len(names) == 17
    assert result["resultType"] == "complete"
    assert result["_meta"][META_SERVER_INFO] == protocol.SERVER_INFO
    assert result["ttlMs"] == 3_600_000
    assert result["cacheScope"] == "public"


def test_discovery_is_gui_independent_and_complete():
    # A GUI dispatch would hang or poison this: make any dispatch attempt fail loudly.
    def _forbid(*_args, **_kwargs):
        raise AssertionError("discovery must not dispatch to the GUI")

    original = gui_dispatch.dispatch_to_gui
    gui_dispatch.dispatch_to_gui = _forbid
    try:
        server = make_server()
        server._static_capabilities = {"freecad": {"version": [1, 1, 3]}}
        response = dispatch(server, "server/discover")
    finally:
        gui_dispatch.dispatch_to_gui = original
    result = response["result"]
    assert result["supportedVersions"] == ["2026-07-28"]
    assert result["capabilities"] == {
        "tools": {},
        "resources": {"subscribe": True},
        "extensions": {"io.modelcontextprotocol/tasks": {}},
    }
    assert "freecad" not in result["capabilities"]
    assert "gui" not in result
    assert result["ttlMs"] == 3_600_000
    assert result["cacheScope"] == "public"


def test_discover_capabilities_tool_never_touches_the_gui():
    def _forbid(*_args, **_kwargs):
        raise AssertionError("discover_capabilities must not dispatch to the GUI")

    original = gui_dispatch.dispatch_to_gui
    gui_dispatch.dispatch_to_gui = _forbid
    try:
        server = make_server()
        server._static_capabilities = {"paths": {"home": "/fc"}}
        response = dispatch(
            server,
            "tools/call",
            {"name": "discover_capabilities", "arguments": {}},
        )
    finally:
        gui_dispatch.dispatch_to_gui = original
    result = response["result"]
    assert result["resultType"] == "complete"
    assert result["structuredContent"] == {
        "capabilities": {"paths": {"home": "/fc"}},
        "gui": result["structuredContent"]["gui"],
    }
    assert STUB_CALLS == []
    # Machine-specific capability data: cached, private, GUI independent.
    assert result["ttlMs"] == 0
    assert result["cacheScope"] == "private"


def test_unknown_tool_and_unknown_method_are_protocol_errors():
    server = make_server()
    with pytest.raises(protocol.ProtocolError) as tool_exc:
        dispatch(server, "tools/call", {"name": "nope", "arguments": {}})
    assert tool_exc.value.code == protocol.METHOD_NOT_FOUND
    with pytest.raises(protocol.ProtocolError) as method_exc:
        dispatch(server, "other/method", {})
    assert method_exc.value.code == protocol.METHOD_NOT_FOUND


def test_tool_arguments_are_schema_validated():
    server = make_server()
    with pytest.raises(protocol.ProtocolError) as exc:
        dispatch(
            server,
            "tools/call",
            {"name": "inspect_objects", "arguments": {"unexpected": 1}},
        )
    assert exc.value.code == protocol.INVALID_PARAMS


# ---------------------------------------------------------------------------
# Blocking tools/call lifecycle.
# ---------------------------------------------------------------------------


def test_blocking_call_streams_complete_tool_result():
    server = make_server()
    waker = _reset_dispatcher_for_tests()
    response = dispatch(
        server,
        "tools/call",
        {"name": "new_document", "arguments": {}},
        rpc_id=11,
    )
    assert isinstance(response, server_module.StreamResponse)
    events = drain_stream(response, 2)
    assert events[1] is None  # terminal sentinel
    result = events[0]["result"]
    assert events[0] == {
        "jsonrpc": "2.0",
        "id": 11,
        "result": result,
    }
    assert result["resultType"] == "complete"
    assert result["structuredContent"] == {
        "tool": "new_document",
        "arguments": {},
    }
    assert result["_meta"][META_SERVER_INFO] == protocol.SERVER_INFO
    assert wait_until(lambda: not server.has_pending_operations())
    waker.join()


def test_blocking_tool_error_is_a_complete_is_error_result():
    server = make_server()
    _reset_dispatcher_for_tests()
    STUB_HANDLERS["new_document"] = lambda ctx, args: (_ for _ in ()).throw(
        protocol.ToolError(protocol.DOCUMENT_NOT_FOUND, "no such document")
    )
    response = dispatch(
        server, "tools/call", {"name": "new_document", "arguments": {}}, rpc_id=3
    )
    events = drain_stream(response, 2)
    result = events[0]["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_blocked_running_deadline_reports_still_running_then_completes():
    server = make_server()
    _reset_dispatcher_for_tests()
    release = threading.Event()
    started = threading.Event()

    def blocked(ctx, arguments):
        started.set()
        release.wait(timeout=5.0)
        return {"tool": "inspect_objects"}

    STUB_HANDLERS["inspect_objects"] = blocked
    original_deadline = server_module.DEFAULT_DEADLINE_S
    server_module.DEFAULT_DEADLINE_S = 0.3
    try:
        response = dispatch(
            server,
            "tools/call",
            {"name": "inspect_objects", "arguments": {}},
            rpc_id=7,
        )
        # The job must be running before the deadline expires; otherwise
        # the queued-timeout path would win instead.
        assert started.wait(timeout=5.0)
        events = drain_stream(response, 2, timeout=5.0)
        result = events[0]["result"]
        assert result["structuredContent"]["error"]["code"] == "SERVER_BUSY"
        assert result["structuredContent"]["error"]["details"]["stillRunning"] is True
        # Truthful: the operation is still tracked until it actually ends.
        assert server.has_pending_operations()
        release.set()
        assert wait_until(lambda: not server.has_pending_operations())
    finally:
        server_module.DEFAULT_DEADLINE_S = original_deadline


def test_queued_deadline_cancels_before_execution():
    server = make_server()
    gui_dispatch._waker = None  # nothing drains the queue: job never starts
    original_deadline = server_module.DEFAULT_DEADLINE_S
    server_module.DEFAULT_DEADLINE_S = 0.05
    try:
        # A preflight-free tool: the queued call job is the FIRST dispatch,
        # so the 0.05s queued deadline (not a 30s preflight wait) is what
        # cancels it before FreeCAD is ever entered.
        response = dispatch(
            server,
            "tools/call",
            {"name": "inspect_objects", "arguments": {}},
            rpc_id=8,
        )
        events = drain_stream(response, 2, timeout=5.0)
        result = events[0]["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["error"]["code"] == "GUI_DISPATCH_FAILED"
        assert STUB_CALLS == []  # never entered FreeCAD
        assert not server.has_pending_operations()
    finally:
        server_module.DEFAULT_DEADLINE_S = original_deadline


# ---------------------------------------------------------------------------
# Consent choreography.
# ---------------------------------------------------------------------------


CONSENT_TARGET = {
    "kind": "document",
    "document": "Smoke",
    "generation": 1,
    "requires_consent": True,
    "message": "Close unsaved document?",
}


ELICITATION_FORM_CAPS = {"elicitation": {"form": {}}}


def _call_close(
    server,
    *,
    rpc_id,
    request_state=None,
    input_responses=None,
    arguments=None,
    capabilities=None,
):
    params = {"name": "close_document", "arguments": arguments or {}}
    if request_state is not None:
        params["requestState"] = request_state
    if input_responses is not None:
        params["inputResponses"] = input_responses
    # Consent MRTR challenges and accepted retries require the per-request
    # form elicitation capability. An explicitly passed value — including
    # {} — is preserved verbatim so the absent-capability path stays
    # testable.
    if capabilities is None:
        capabilities = ELICITATION_FORM_CAPS
    return dispatch(
        server, "tools/call", params, rpc_id=rpc_id, capabilities=capabilities
    )


def test_consent_challenge_precedes_any_execution():
    server = make_server()
    _reset_dispatcher_for_tests()
    PREFLIGHT_RESULTS["close_document"] = dict(CONSENT_TARGET)
    response = _call_close(server, rpc_id=1)
    result = response["result"]
    assert result["resultType"] == "input_required"
    assert result["requestState"]
    confirm = result["inputRequests"]["confirm"]
    assert confirm["method"] == "elicitation/create"
    assert confirm["params"]["requestedSchema"]["required"] == ["confirmed"]
    assert STUB_CALLS == []  # nothing executed before consent
    assert not server.has_pending_operations()


def test_consent_without_form_capability_is_rejected_before_execution():
    server = make_server()
    _reset_dispatcher_for_tests()
    PREFLIGHT_RESULTS["close_document"] = dict(CONSENT_TARGET)
    # Explicit empty capability declaration: neither the MRTR challenge
    # nor an accepted retry may run without the declared form capability.
    with pytest.raises(protocol.ProtocolError) as challenge_exc:
        _call_close(server, rpc_id=1, capabilities={})
    assert challenge_exc.value.code == protocol.MISSING_REQUIRED_CLIENT_CAPABILITY
    assert challenge_exc.value.data["requiredCapabilities"] == ELICITATION_FORM_CAPS
    assert STUB_CALLS == []  # no challenge returned, nothing executed
    assert not server.has_pending_operations()
    # The same holds for an accepted retry: a signed token without the
    # per-request capability never reaches execution.
    token = _call_close(server, rpc_id=2)["result"]["requestState"]
    accept = {"confirm": {"action": "accept", "content": {"confirmed": True}}}
    with pytest.raises(protocol.ProtocolError) as retry_exc:
        _call_close(
            server,
            rpc_id=3,
            request_state=token,
            input_responses=accept,
            capabilities={},
        )
    assert retry_exc.value.code == protocol.MISSING_REQUIRED_CLIENT_CAPABILITY
    assert retry_exc.value.data["requiredCapabilities"] == ELICITATION_FORM_CAPS
    assert STUB_CALLS == []  # accepted retry never executed
    assert not server.has_pending_operations()
    # The gate precedes nonce consumption: the same token still executes
    # once the capability is declared.
    accepted = _call_close(
        server,
        rpc_id=4,
        request_state=token,
        input_responses=accept,
    )
    events = drain_stream(accepted, 2)
    assert events[0]["result"]["resultType"] == "complete"
    assert len(STUB_CALLS) == 1


def test_accepted_retry_executes_once_and_replay_is_rejected():
    server = make_server()
    _reset_dispatcher_for_tests()
    PREFLIGHT_RESULTS["close_document"] = dict(CONSENT_TARGET)
    first = _call_close(server, rpc_id=1)
    token = first["result"]["requestState"]
    accept = {"confirm": {"action": "accept", "content": {"confirmed": True}}}
    retry = _call_close(server, rpc_id=2, request_state=token, input_responses=accept)
    assert isinstance(retry, server_module.StreamResponse)
    events = drain_stream(retry, 2)
    assert events[0]["result"]["resultType"] == "complete"
    assert len(STUB_CALLS) == 1  # executed exactly once

    # Replay: the same requestState cannot execute a second time.
    replay = _call_close(server, rpc_id=3, request_state=token, input_responses=accept)
    assert len(STUB_CALLS) == 1
    assert replay["result"]["isError"] is True
    assert replay["result"]["structuredContent"]["error"]["code"] == "CONSENT_DENIED"


def test_decline_does_not_burn_the_nonce():
    server = make_server()
    _reset_dispatcher_for_tests()
    PREFLIGHT_RESULTS["close_document"] = dict(CONSENT_TARGET)
    token = _call_close(server, rpc_id=1)["result"]["requestState"]
    declined = _call_close(
        server,
        rpc_id=2,
        request_state=token,
        input_responses={"confirm": {"action": "decline"}},
    )
    assert declined["result"]["isError"] is True
    assert declined["result"]["structuredContent"]["error"]["code"] == "CONSENT_DENIED"
    assert STUB_CALLS == []
    # The nonce was not consumed by the decline: acceptance still works.
    accepted = _call_close(
        server,
        rpc_id=3,
        request_state=token,
        input_responses={
            "confirm": {"action": "accept", "content": {"confirmed": True}}
        },
    )
    events = drain_stream(accepted, 2)
    assert events[0]["result"]["resultType"] == "complete"
    assert len(STUB_CALLS) == 1


def test_tampered_arguments_are_rejected_before_execution():
    server = make_server()
    _reset_dispatcher_for_tests()
    PREFLIGHT_RESULTS["close_document"] = dict(CONSENT_TARGET)
    server._definitions["close_document"]["inputSchema"] = {
        "type": "object",
        "properties": {"document": {"type": "string"}},
        "additionalProperties": False,
    }
    token = _call_close(server, rpc_id=1)["result"]["requestState"]
    forged = _call_close(
        server,
        rpc_id=2,
        request_state=token,
        input_responses={
            "confirm": {"action": "accept", "content": {"confirmed": True}}
        },
        arguments={"document": "DifferentDocument"},
    )
    # Consent for the original arguments cannot authorize a different target.
    assert forged["result"]["isError"] is True
    assert forged["result"]["structuredContent"]["error"]["code"] == "CONSENT_DENIED"
    assert STUB_CALLS == []


def test_target_change_gets_a_fresh_challenge():
    server = make_server()
    _reset_dispatcher_for_tests()
    PREFLIGHT_RESULTS["close_document"] = dict(CONSENT_TARGET)
    token = _call_close(server, rpc_id=1)["result"]["requestState"]
    changed = dict(CONSENT_TARGET, generation=2)
    PREFLIGHT_RESULTS["close_document"] = changed
    stale = _call_close(
        server,
        rpc_id=2,
        request_state=token,
        input_responses={
            "confirm": {"action": "accept", "content": {"confirmed": True}}
        },
    )
    result = stale["result"]
    assert result["resultType"] == "input_required"
    assert result["requestState"] != token  # fresh challenge for the new target
    assert STUB_CALLS == []
    # The fresh challenge executes against the new target.
    PREFLIGHT_RESULTS["close_document"] = changed
    retry = _call_close(
        server,
        rpc_id=3,
        request_state=result["requestState"],
        input_responses={
            "confirm": {"action": "accept", "content": {"confirmed": True}}
        },
    )
    events = drain_stream(retry, 2)
    assert events[0]["result"]["resultType"] == "complete"
    assert len(STUB_CALLS) == 1


def test_requeststate_verified_even_when_consent_no_longer_needed():
    server = make_server()
    _reset_dispatcher_for_tests()
    PREFLIGHT_RESULTS["close_document"] = dict(CONSENT_TARGET)
    token = _call_close(server, rpc_id=1)["result"]["requestState"]
    # The target identity is unchanged but no longer needs consent.
    PREFLIGHT_RESULTS["close_document"] = dict(CONSENT_TARGET, requires_consent=False)
    retry = _call_close(
        server,
        rpc_id=2,
        request_state=token,
        input_responses={
            "confirm": {"action": "accept", "content": {"confirmed": True}}
        },
    )
    events = drain_stream(retry, 2)
    assert events[0]["result"]["resultType"] == "complete"
    assert len(STUB_CALLS) == 1


def test_consent_free_target_executes_with_approved_target_binding():
    server = make_server()
    _reset_dispatcher_for_tests()
    observed: dict = {}

    def spy(ctx, arguments):
        STUB_CALLS.append(("close_document", ctx, dict(arguments)))
        observed["approved_target"] = ctx.approved_target
        observed["cancel_event"] = ctx.cancel_event
        return {"tool": "close_document"}

    STUB_HANDLERS["close_document"] = spy
    PREFLIGHT_RESULTS["close_document"] = {
        "kind": "document",
        "document": "Smoke",
        "generation": 1,
        "requires_consent": False,
    }
    response = _call_close(server, rpc_id=1)
    events = drain_stream(response, 2)
    assert events[0]["result"]["resultType"] == "complete"
    assert observed["approved_target"] == {
        "kind": "document",
        "document": "Smoke",
        "generation": 1,
    }
    assert isinstance(observed["cancel_event"], threading.Event)


def test_requeststate_on_consentless_tool_is_invalid_params():
    server = make_server()
    with pytest.raises(protocol.ProtocolError) as exc:
        dispatch(
            server,
            "tools/call",
            {
                "name": "inspect_objects",
                "arguments": {},
                "requestState": "whatever",
            },
        )
    assert exc.value.code == protocol.INVALID_PARAMS


# ---------------------------------------------------------------------------
# Tasks.
# ---------------------------------------------------------------------------


def test_task_created_queryable_and_completed_with_tool_result():
    server = make_server()
    _reset_dispatcher_for_tests()
    entered = threading.Event()
    release = threading.Event()

    def gated(ctx, arguments):
        entered.set()
        release.wait(timeout=5.0)
        return {"tool": "run_script", "arguments": {}}

    STUB_HANDLERS["run_script"] = gated
    response = dispatch(
        server,
        "tools/call",
        {"name": "run_script", "arguments": {}},
        rpc_id=4,
        capabilities=TASKS_CAPS,
    )
    result = response["result"]
    assert result["resultType"] == "task"
    task_id = result["taskId"]
    # An instant tool may already be terminal when the response returns;
    # the barrier pins the record as truthfully working for the assertions
    # below.
    assert entered.wait(timeout=5.0)
    assert result["status"] == "working"
    assert result["pollIntervalMs"] == 500
    # Queryable before completion.
    get_response = dispatch(
        server,
        "tasks/get",
        {"taskId": task_id},
        rpc_id=5,
        capabilities=TASKS_CAPS,
    )
    assert get_response["result"]["status"] == "working"
    assert get_response["result"]["resultType"] == "complete"
    release.set()
    assert wait_until(
        lambda: server._task_store.get(task_id, principal=PRINCIPAL).terminal
    )
    terminal = server._task_store.get(task_id, principal=PRINCIPAL)
    assert terminal.status == "completed"
    assert terminal.result["structuredContent"] == {
        "tool": "run_script",
        "arguments": {},
    }
    detailed = dispatch(
        server,
        "tasks/get",
        {"taskId": task_id},
        rpc_id=6,
        capabilities=TASKS_CAPS,
    )["result"]
    assert detailed["status"] == "completed"


def test_task_tool_error_completes_not_fails():
    server = make_server()
    _reset_dispatcher_for_tests()
    STUB_HANDLERS["run_script"] = lambda ctx, args: (_ for _ in ()).throw(
        protocol.ToolError(protocol.GUI_DISPATCH_FAILED, "boom")
    )
    response = dispatch(
        server,
        "tools/call",
        {"name": "run_script", "arguments": {}},
        rpc_id=1,
        capabilities=TASKS_CAPS,
    )
    task_id = response["result"]["taskId"]
    assert wait_until(
        lambda: server._task_store.get(task_id, principal=PRINCIPAL).terminal
    )
    task = server._task_store.get(task_id, principal=PRINCIPAL)
    assert task.status == "completed"  # isError tool result, not failed
    assert task.result["isError"] is True


def test_task_schema_violation_fails_with_protocol_error():
    server = make_server()
    # The real dispatcher must run: without a waker nothing drains the
    # queued job, so the violating handler never executes and the task
    # record never reaches a terminal state.
    _reset_dispatcher_for_tests()
    STUB_HANDLERS["run_script"] = lambda ctx, args: {"unexpected": "payload"}
    # The stub's permissive output schema is tightened for this test so the
    # handler's payload genuinely violates the registered contract.
    server._definitions["run_script"]["outputSchema"] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    response = dispatch(
        server,
        "tools/call",
        {"name": "run_script", "arguments": {}},
        rpc_id=1,
        capabilities=TASKS_CAPS,
    )
    task_id = response["result"]["taskId"]
    assert wait_until(
        lambda: server._task_store.get(task_id, principal=PRINCIPAL).terminal
    )
    task = server._task_store.get(task_id, principal=PRINCIPAL)
    assert task.status == "failed"
    assert task.error["code"] == protocol.INTERNAL_ERROR


def test_task_eligible_without_tasks_capability_runs_blocking():
    server = make_server()
    _reset_dispatcher_for_tests()
    response = dispatch(
        server, "tools/call", {"name": "run_script", "arguments": {}}, rpc_id=1
    )
    assert isinstance(response, server_module.StreamResponse)
    events = drain_stream(response, 2)
    assert events[0]["result"]["resultType"] == "complete"


def test_tasks_get_requires_extension_capability():
    server = make_server()
    with pytest.raises(protocol.ProtocolError) as exc:
        dispatch(server, "tasks/get", {"taskId": "abc"}, rpc_id=1)
    assert exc.value.code == protocol.MISSING_REQUIRED_CLIENT_CAPABILITY
    assert exc.value.data["requiredCapabilities"] == {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }


def test_tasks_get_unknown_id_is_invalid_params():
    server = make_server()
    with pytest.raises(protocol.ProtocolError) as exc:
        dispatch(
            server,
            "tasks/get",
            {"taskId": "missing"},
            rpc_id=1,
            capabilities=TASKS_CAPS,
        )
    assert exc.value.code == protocol.INVALID_PARAMS
    assert exc.value.data["taskId"] == "missing"


def test_tasks_update_ignores_keys_and_acks_empty():
    server = make_server()
    _reset_dispatcher_for_tests()
    task_id = dispatch(
        server,
        "tools/call",
        {"name": "run_script", "arguments": {}},
        rpc_id=1,
        capabilities=TASKS_CAPS,
    )["result"]["taskId"]
    response = dispatch(
        server,
        "tasks/update",
        {"taskId": task_id, "inputResponses": {"confirm": {"action": "accept"}}},
        rpc_id=2,
        capabilities=TASKS_CAPS,
    )
    assert response["result"] == {
        "resultType": "complete",
        "_meta": {META_SERVER_INFO: protocol.SERVER_INFO},
    }


def test_tasks_cancel_requests_cooperative_cancellation_and_real_result_wins():
    server = make_server()
    _reset_dispatcher_for_tests()
    entered = threading.Event()
    release = threading.Event()

    def blocked(ctx, arguments):
        entered.set()
        release.wait(timeout=5.0)
        return {"tool": "run_script", "finished": True}

    STUB_HANDLERS["run_script"] = blocked
    task_id = dispatch(
        server,
        "tools/call",
        {"name": "run_script", "arguments": {}},
        rpc_id=1,
        capabilities=TASKS_CAPS,
    )["result"]["taskId"]
    assert entered.wait(timeout=5.0)
    dispatch(
        server,
        "tasks/cancel",
        {"taskId": task_id},
        rpc_id=2,
        capabilities=TASKS_CAPS,
    )
    task = server._task_store.get(task_id, principal=PRINCIPAL)
    assert task.status == "working"
    assert task.status_message == "Cancellation requested"
    assert task.cancel_event.is_set()
    release.set()
    assert wait_until(
        lambda: server._task_store.get(task_id, principal=PRINCIPAL).terminal
    )
    # Work finished before cancellation took effect: real result wins.
    assert server._task_store.get(task_id, principal=PRINCIPAL).status == "completed"


def test_task_subscription_requires_tasks_capability():
    server = make_server()
    with pytest.raises(protocol.ProtocolError) as exc:
        dispatch(
            server,
            "subscriptions/listen",
            {"notifications": {"taskIds": ["anything"]}},
            rpc_id=9,
        )
    assert exc.value.code == protocol.MISSING_REQUIRED_CLIENT_CAPABILITY


def test_task_notifications_flow_to_subscribed_stream_only():
    server = make_server()
    _reset_dispatcher_for_tests()
    entered = threading.Event()
    release = threading.Event()

    def blocked(ctx, arguments):
        entered.set()
        release.wait(timeout=5.0)
        return {"tool": "run_script"}

    STUB_HANDLERS["run_script"] = blocked
    task_id = dispatch(
        server,
        "tools/call",
        {"name": "run_script", "arguments": {}},
        rpc_id=1,
        capabilities=TASKS_CAPS,
    )["result"]["taskId"]
    assert entered.wait(timeout=5.0)
    stream = dispatch(
        server,
        "subscriptions/listen",
        {"notifications": {"taskIds": [task_id]}},
        rpc_id=2,
        capabilities=TASKS_CAPS,
    )
    ack = stream.events.get(timeout=3.0)
    assert ack["method"] == "notifications/subscriptions/acknowledged"
    assert ack["params"]["notifications"] == {"taskIds": [task_id]}
    assert ack["params"]["_meta"][META_SUBSCRIPTION_ID] == 2
    release.set()
    notification = stream.events.get(timeout=3.0)
    assert notification["method"] == "notifications/tasks"
    assert notification["params"]["taskId"] == task_id
    assert notification["params"]["status"] == "completed"
    assert notification["params"]["_meta"][META_SUBSCRIPTION_ID] == 2
    assert not server.has_pending_operations()


# ---------------------------------------------------------------------------
# Operation cap and FEM-style async flattening.
# ---------------------------------------------------------------------------


def test_operation_cap_counts_blocking_and_tasks():
    server = make_server()
    for index in range(server_module.MAX_OPERATIONS):
        server._register_operation(
            "inspect_objects",
            kind="blocking",
            task_id=None,
            principal=PRINCIPAL,
            deadline_s=60.0,
        )
    response = dispatch(
        server,
        "tools/call",
        {"name": "inspect_objects", "arguments": {}},
        rpc_id=1,
    )
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "SERVER_BUSY"
    assert (
        result["structuredContent"]["error"]["details"]["reason"] == "operation_limit"
    )
    # Task path shares the cap.
    response = dispatch(
        server,
        "tools/call",
        {"name": "run_script", "arguments": {}},
        rpc_id=2,
        capabilities=TASKS_CAPS,
    )
    assert response["result"]["structuredContent"]["error"]["code"] == "SERVER_BUSY"
    assert len(server._task_store._tasks) == 0  # no orphan task record


def test_async_fem_future_is_flattened_and_operation_retained():
    server = make_server()
    _reset_dispatcher_for_tests()
    deferred: Future = Future()

    STUB_HANDLERS["run_fem"] = lambda ctx, arguments: deferred
    response = dispatch(
        server,
        "tools/call",
        {"name": "run_fem", "arguments": {}},
        rpc_id=1,
        capabilities=TASKS_CAPS,
    )
    result = response["result"]
    task_id = result["taskId"]
    assert result["status"] == "working"
    # The operation stays tracked while only the Future is retained.
    assert server.has_pending_operations()
    task = server._task_store.get(task_id, principal=PRINCIPAL)
    assert task.status == "working"
    deferred.set_result({"pipeline": "Result", "blocks": 1})
    assert wait_until(
        lambda: server._task_store.get(task_id, principal=PRINCIPAL).terminal
    )
    task = server._task_store.get(task_id, principal=PRINCIPAL)
    assert task.status == "completed"
    assert task.result["structuredContent"] == {"pipeline": "Result", "blocks": 1}
    assert not server.has_pending_operations()


def test_async_fem_tool_error_flattens_to_completed_is_error():
    server = make_server()
    _reset_dispatcher_for_tests()
    deferred: Future = Future()
    STUB_HANDLERS["run_fem"] = lambda ctx, arguments: deferred
    task_id = dispatch(
        server,
        "tools/call",
        {"name": "run_fem", "arguments": {}},
        rpc_id=1,
        capabilities=TASKS_CAPS,
    )["result"]["taskId"]
    deferred.set_exception(
        protocol.ToolError("SOLVER_FAILED", "CalculiX failed", {"exit": 1})
    )
    assert wait_until(
        lambda: server._task_store.get(task_id, principal=PRINCIPAL).terminal
    )
    task = server._task_store.get(task_id, principal=PRINCIPAL)
    assert task.status == "completed"
    assert task.result["structuredContent"]["error"]["code"] == "SOLVER_FAILED"
    assert not server.has_pending_operations()


def test_blocking_async_future_completes_stream_at_real_completion():
    server = make_server()
    _reset_dispatcher_for_tests()
    deferred: Future = Future()
    STUB_HANDLERS["run_fem"] = lambda ctx, arguments: deferred
    response = dispatch(
        server, "tools/call", {"name": "run_fem", "arguments": {}}, rpc_id=1
    )
    assert isinstance(response, server_module.StreamResponse)
    # Nothing is emitted while only the Future is pending: the operation
    # stays truthfully tracked and the SSE wait is not finalized early.
    assert server.has_pending_operations()
    assert wait_until(lambda: not _stream_has_event(response))
    deferred.set_result({"pipeline": "Result", "blocks": 2})
    events = drain_stream(response, 2, timeout=5.0)
    assert events[1] is None
    result = events[0]["result"]
    assert result["resultType"] == "complete"
    assert result["structuredContent"] == {"pipeline": "Result", "blocks": 2}
    # The operation is released only by the Future's real resolution.
    assert wait_until(lambda: not server.has_pending_operations())


def test_blocking_async_tool_error_flattens_to_is_error_result():
    server = make_server()
    _reset_dispatcher_for_tests()
    deferred: Future = Future()
    STUB_HANDLERS["run_fem"] = lambda ctx, arguments: deferred
    response = dispatch(
        server, "tools/call", {"name": "run_fem", "arguments": {}}, rpc_id=1
    )
    deferred.set_exception(
        protocol.ToolError("SOLVER_FAILED", "CalculiX failed", {"exit": 1})
    )
    events = drain_stream(response, 2, timeout=5.0)
    result = events[0]["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "SOLVER_FAILED"
    assert wait_until(lambda: not server.has_pending_operations())


def test_blocking_async_future_deadline_reports_still_running_and_retains():
    server = make_server()
    _reset_dispatcher_for_tests()
    deferred: Future = Future()
    STUB_HANDLERS["run_fem"] = lambda ctx, arguments: deferred
    stream = dispatch(
        server, "tools/call", {"name": "run_fem", "arguments": {}}, rpc_id=1
    )
    op = next(iter(server._ops.values()))
    # Force the operation deadline overdue while the producer awaits the
    # retained Future: the wait must end with a truthful still-running
    # tool error (never a fabricated result) and keep the slot retained.
    op.deadline_mono = server._clock() - 0.001
    events = drain_stream(stream, 2, timeout=5.0)
    assert events[1] is None
    result = events[0]["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["code"] == "SERVER_BUSY"
    assert error["details"]["reason"] == "deadline_exceeded"
    assert error["details"]["stillRunning"] is True
    assert server.has_pending_operations()
    # The retained Future's real resolution still releases the slot.
    deferred.set_result({"pipeline": "Result", "blocks": 1})
    assert wait_until(lambda: not server.has_pending_operations())


def test_blocking_async_cancellation_request_reports_still_running():
    server = make_server()
    _reset_dispatcher_for_tests()
    deferred: Future = Future()
    ran = threading.Event()

    def fem(ctx, arguments):
        ran.set()
        return deferred

    STUB_HANDLERS["run_fem"] = fem
    stream = dispatch(
        server, "tools/call", {"name": "run_fem", "arguments": {}}, rpc_id=1
    )
    assert ran.wait(timeout=5.0)  # job created: the shared event is wired
    op = next(iter(server._ops.values()))
    # A cancellation request (disconnect, stop, or the deadline sweep —
    # this is the same shared Event) ends the SSE wait honestly: the
    # native work keeps running and the slot stays retained.
    op.cancel_event.set()
    events = drain_stream(stream, 2, timeout=5.0)
    assert events[1] is None
    result = events[0]["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["code"] == "SERVER_BUSY"
    assert error["details"]["reason"] == "cancelled"
    assert error["details"]["stillRunning"] is True
    assert server.has_pending_operations()
    deferred.set_result({"pipeline": "Result", "blocks": 1})
    assert wait_until(lambda: not server.has_pending_operations())


def test_stop_defers_waker_disposal_until_async_future_resolves(lifecycle):
    server_module.start_server()
    server = server_module.get_server()
    # bind() already initialized the dispatcher; swap in the real-Qt-like
    # drainer for the one queued job.
    install_waker()
    deferred: Future = Future()
    STUB_HANDLERS["run_fem"] = lambda ctx, arguments: deferred
    stream = dispatch(
        server, "tools/call", {"name": "run_fem", "arguments": {}}, rpc_id=1
    )
    assert isinstance(stream, server_module.StreamResponse)
    # The dispatcher job itself is done; only the server still retains
    # the async Future.
    assert wait_until(lambda: gui_dispatch.pending_count() == 0)
    assert server.has_pending_operations()
    result = server_module.stop_server()
    assert result["running"] is False
    assert result["state"] == "draining"
    # Draining with a retained async operation must NOT dispose the waker
    # (dispatcher pending_count is already zero — ops are not).
    assert server.status()["pendingOperations"] == 1
    assert gui_dispatch._waker is not None
    deferred.set_result({"pipeline": "Result", "blocks": 1})
    # True completion releases the slot; only then is the Qt cleanup
    # scheduled.
    assert wait_until(lambda: gui_dispatch._waker is None)
    assert not server.has_pending_operations()


def _stream_has_event(stream) -> bool:
    try:
        stream.events.get_nowait()
    except queue.Empty:
        return False
    return True


# ---------------------------------------------------------------------------
# Document observer, resources and subscriptions.
# ---------------------------------------------------------------------------


def test_resources_list_and_read_with_event_publication():
    import json as _json

    server = make_server()
    _reset_dispatcher_for_tests()
    listing = dispatch(server, "resources/list", rpc_id=1)
    assert listing["result"]["resources"][0]["uri"] == "freecad://documents"
    # Live document listing: ttl 0, private.
    assert listing["result"]["ttlMs"] == 0
    assert listing["result"]["cacheScope"] == "private"

    stream = dispatch(
        server,
        "subscriptions/listen",
        {"notifications": {"resourceSubscriptions": ["freecad://documents"]}},
        rpc_id=2,
    )
    ack = stream.events.get(timeout=3.0)
    assert ack["method"] == "notifications/subscriptions/acknowledged"

    doc = FakeDoc("Smoke", "Smoke label")
    doc.FileName = "/tmp/fc-test/Smoke.FCStd"
    doc.Objects = [object(), object()]
    FC_STATE["documents"]["Smoke"] = doc
    server._on_document_event(doc, bump=True, publish=True)

    event = stream.events.get(timeout=3.0)
    assert event["method"] == "notifications/resources/updated"
    assert event["params"]["uri"] == "freecad://documents"
    assert event["params"]["_meta"][META_SUBSCRIPTION_ID] == 2
    # Every real event bumps the monotonic generation relative to the
    # prior snapshot; the lifetime entry is retained (a stale instance
    # never adopts a fresh identity) and deletion never resets it.
    generation_before = server.document_generation(doc)
    server._on_document_event(doc, bump=True, publish=True)
    assert server.document_generation(doc) == generation_before + 1

    gui_dispatch._waker = None  # read must go through dispatch_to_gui
    read_threadbox: dict = {}

    def reader():
        read_threadbox["response"] = dispatch(
            server, "resources/read", {"uri": "freecad://documents"}, rpc_id=3
        )

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    # The read job was queued; drain it like the GUI thread would.
    assert wait_until(lambda: gui_dispatch.pending_count() > 0)
    gui_dispatch.process_gui_tasks(reschedule=False)
    reader_thread.join(timeout=5.0)
    read_result = read_threadbox["response"]["result"]
    # Native ReadResourceResult: contents=[{uri, mimeType, text}] with the
    # compact JSON document listing, ttl 0 and private scope.
    assert read_result["ttlMs"] == 0
    assert read_result["cacheScope"] == "private"
    (content,) = read_result["contents"]
    assert content["uri"] == "freecad://documents"
    assert content["mimeType"] == "application/json"
    assert _json.loads(content["text"]) == {
        "documents": [
            {
                "name": "Smoke",
                "label": "Smoke label",
                "fileName": "/tmp/fc-test/Smoke.FCStd",
                "objectCount": 2,
            }
        ]
    }
    assert set(content) == {"uri", "mimeType", "text"}


def test_resource_read_unknown_uri_is_invalid_params():
    server = make_server()
    with pytest.raises(protocol.ProtocolError) as exc:
        dispatch(server, "resources/read", {"uri": "freecad://other"}, rpc_id=1)
    assert exc.value.code == protocol.INVALID_PARAMS


def test_duplicate_subscription_id_on_same_connection_is_rejected():
    server = make_server()
    first = dispatch(server, "subscriptions/listen", {"notifications": {}}, rpc_id=5)
    assert isinstance(first, server_module.StreamResponse)
    with pytest.raises(protocol.ProtocolError) as exc:
        dispatch(server, "subscriptions/listen", {"notifications": {}}, rpc_id=5)
    assert exc.value.code == protocol.INVALID_PARAMS


def test_disconnect_closes_only_that_connection():
    server = make_server()
    stream_a = dispatch(server, "subscriptions/listen", {"notifications": {}}, rpc_id=1)
    # A different subscription id on the same connection is fine.
    stream_b = dispatch(server, "subscriptions/listen", {"notifications": {}}, rpc_id=2)
    # The same JSON-RPC id on a separate connection is isolated identity.
    view = validated_view("subscriptions/listen", {"notifications": {}}, rpc_id=1)
    stream_c = server.dispatch(view, PRINCIPAL, "conn-2")
    assert len(server._registry) == 3
    server._registry.disconnect("conn-1")
    assert len(server._registry) == 1
    # Disconnecting never fabricated cancellations: task records untouched.
    with pytest.raises(protocol.ProtocolError):
        server._task_store.get("missing", principal=PRINCIPAL)


def test_same_name_reopen_replaces_identity_with_monotonic_generation():
    server = make_server()
    doc = FakeDoc("Smoke")
    FC_STATE["documents"]["Smoke"] = doc
    server._on_document_event(doc, bump=True, publish=False)
    identity_before = server.document_identity(doc)
    generation_before = server.document_generation(doc)
    assert generation_before >= 1  # created event bumped the fresh entry

    # Identity- and generation-bound consumers (consent targets, topology
    # tokens, cursors) captured before the close/reopen.
    captured = (identity_before, generation_before)

    # Same Name, NEW document instance (close + reopen).
    reopened = FakeDoc("Smoke")
    FC_STATE["documents"]["Smoke"] = reopened
    server._on_document_event(reopened, bump=True, publish=False)

    identity_after = server.document_identity(reopened)
    generation_after = server.document_generation(reopened)
    assert identity_after != captured[0]  # fresh lifetime UUID
    # The stale instance can never adopt or report the live identity.
    with pytest.raises(protocol.ToolError):
        server.document_identity(doc)
    with pytest.raises(protocol.ToolError):
        server.document_generation(doc)


def test_subscription_listen_rejects_unknown_or_foreign_task_ids():
    server = make_server()
    _reset_dispatcher_for_tests()
    owned = dispatch(
        server,
        "tools/call",
        {"name": "run_script", "arguments": {}},
        rpc_id=1,
        capabilities=TASKS_CAPS,
    )["result"]["taskId"]
    assert wait_until(
        lambda: server._task_store.get(owned, principal=PRINCIPAL).terminal
    )
    # Unknown id: -32602 before any stream is registered.
    with pytest.raises(protocol.ProtocolError) as unknown_exc:
        dispatch(
            server,
            "subscriptions/listen",
            {"notifications": {"taskIds": ["missing"]}},
            rpc_id=2,
            capabilities=TASKS_CAPS,
        )
    assert unknown_exc.value.code == protocol.INVALID_PARAMS
    # Foreign principal: indistinguishable from unknown, still rejected.
    view = validated_view(
        "subscriptions/listen",
        {"notifications": {"taskIds": [owned]}},
        rpc_id=3,
        capabilities=TASKS_CAPS,
    )
    with pytest.raises(protocol.ProtocolError) as foreign_exc:
        server.dispatch(view, "sha256:someone-else", CONN)
    assert foreign_exc.value.code == protocol.INVALID_PARAMS
    assert len(server._registry) == 0


def test_subscription_stream_ends_with_final_result_and_sentinel():
    server = make_server()
    stream = dispatch(server, "subscriptions/listen", {"notifications": {}}, rpc_id=1)
    ack = stream.events.get(timeout=3.0)
    assert ack["method"] == "notifications/subscriptions/acknowledged"
    # Graceful shutdown: the final complete listen result is delivered and
    # the stream then terminates with the None sentinel (zero chunk).
    assert server._registry.shutdown() == 1
    final = stream.events.get(timeout=3.0)
    assert final["id"] == 1
    assert final["result"]["resultType"] == "complete"
    assert final["result"]["_meta"][META_SUBSCRIPTION_ID] == 1
    assert stream.events.get(timeout=3.0) is None


def test_subscription_stream_terminates_after_disconnect_close():
    server = make_server()
    stream = dispatch(server, "subscriptions/listen", {"notifications": {}}, rpc_id=1)
    ack = stream.events.get(timeout=3.0)
    assert ack["method"] == "notifications/subscriptions/acknowledged"
    # A broken transport closes the subscription (registry drops it on
    # next access) and the source still terminates instead of hanging.
    server._registry.disconnect(CONN)
    assert stream.events.get(timeout=3.0) is None
    assert len(server._registry) == 0


# ---------------------------------------------------------------------------
# Startup / shutdown lifecycle.
# ---------------------------------------------------------------------------


class FakeHTTP:
    instances: list = []

    def __init__(
        self,
        dispatch,
        *,
        token,
        host,
        port,
        allowed_ips,
        remote_enabled=False,
        service_hook=None,
    ):
        self.dispatch = dispatch
        self.token = token
        self.host = host
        self.bound_host = host
        self.port = port
        self.allowed_ips = allowed_ips
        self.remote_enabled = remote_enabled
        self.service_hook = service_hook
        self.started = False
        self.stopped = False
        FakeHTTP.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


TEST_SETTINGS = {
    "port": 9876,
    "token": "lifecycle-token",
    "auto_start": False,
    "allowed_ips": "127.0.0.1",
    "allowed_roots": ["/tmp/fc-test"],
    "remote_enabled": False,
}


@pytest.fixture()
def lifecycle(monkeypatch):
    monkeypatch.setattr(
        server_module, "load_settings", lambda path=None: dict(TEST_SETTINGS)
    )
    monkeypatch.setattr(server_module, "McpHTTPServer", FakeHTTP)
    FakeHTTP.instances = []
    return FakeHTTP


def test_version_guard_fails_before_any_binding(monkeypatch):
    FC_STATE["version"] = ["1", "1", "2", "dev"]
    try:
        with pytest.raises(RuntimeError) as exc:
            server_module.start_server()
        assert "1.1.3" in str(exc.value)
        assert server_module.get_server() is None
        assert FC_STATE["observers"] == []
        FC_STATE["version"] = ["1", "2", "0"]
        with pytest.raises(RuntimeError):
            server_module.start_server()
    finally:
        FC_STATE["version"] = ["1", "1", "3", "dev"]


def test_bind_failure_unwinds_and_never_reports_running(lifecycle, monkeypatch):
    def exploding(*_args, **_kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr(server_module, "McpHTTPServer", exploding)
    with pytest.raises(OSError):
        server_module.start_server()
    assert server_module.get_server() is None
    assert server_module.server_status() == {"running": False, "state": "stopped"}
    assert FC_STATE["observers"] == []  # observer unwound, never registered
    assert gui_dispatch._waker is None  # waker disposed
    assert server_module.server_status()["running"] is False


def test_startup_captures_static_capabilities_and_registers_observer(lifecycle):
    status = server_module.start_server()
    assert status["running"] is True
    assert status["port"] == 9876
    assert status["endpoint"] == "http://127.0.0.1:9876/mcp"
    server = server_module.get_server()
    assert server is not None
    snapshot = server._static_capabilities
    assert snapshot["freecad"]["version"] == ["1", "1", "3"]
    assert snapshot["workbenches"] == ["Mesh", "Part"]
    assert isinstance(snapshot["supportedTypes"], dict)  # no open documents
    assert FC_STATE["observers"] == [server._observer]
    # Duplicate start returns the existing state.
    again = server_module.start_server()
    assert again == status
    assert len(lifecycle.instances) == 1
    server_module.stop_server()


def test_stop_closes_streams_and_retains_late_finalizers(lifecycle):
    server_module.start_server()
    server = server_module.get_server()
    stream = dispatch(server, "subscriptions/listen", {"notifications": {}}, rpc_id=1)
    assert isinstance(stream, server_module.StreamResponse)
    server._register_operation(
        "run_fem", kind="task", task_id=None, principal=PRINCIPAL, deadline_s=60.0
    )
    # The service hook rides the HTTP server instance.
    assert lifecycle.instances[0].service_hook is not None
    # Stop enters draining: the stream gets its graceful final result, and
    # the late finalizer's operation slot stays retained.
    result = server_module.stop_server()
    assert result["running"] is False
    assert result["state"] == "draining"
    assert result["closedSubscriptions"] == 1
    assert server.status()["pendingOperations"] == 1
    # Qt/waker disposal is DEFERRED while a late finalizer is retained —
    # both while running and once draining.
    assert gui_dispatch._waker is not None
    # Restart is refused while the retained GUI work is still active.
    with pytest.raises(RuntimeError) as exc:
        server_module.start_server()
    assert "restart is refused" in str(exc.value)
    # Once the retained work is truly done AND the dispatcher has no
    # inflight jobs, the Qt cleanup happens.
    server._remove_op_by_id(next(iter(server._ops)))
    assert gui_dispatch._waker is None
    # Once drained, the retained server is dropped and a start succeeds.
    assert server_module.stop_server()["running"] is False
    assert server_module.get_server() is None


def test_service_actions_deadline_sets_one_shared_cancellation_event():
    server = make_server()
    _reset_dispatcher_for_tests()
    entered = threading.Event()
    release = threading.Event()
    seen: dict = {}

    def blocked(ctx, arguments):
        seen["ctx"] = ctx
        entered.set()
        release.wait(timeout=5.0)
        return {"tool": "run_script"}

    STUB_HANDLERS["run_script"] = blocked
    task_id = dispatch(
        server,
        "tools/call",
        {"name": "run_script", "arguments": {}},
        rpc_id=1,
        capabilities=TASKS_CAPS,
    )["result"]["taskId"]
    assert entered.wait(timeout=5.0)
    op = next(iter(server._ops.values()))
    record = server._task_store.get(task_id, principal=PRINCIPAL)
    # One Event per operation, shared by every cancellation route.
    assert op.cancel_event is record.cancel_event
    assert op.cancel_event is seen["ctx"].cancel_event

    # Force the monotonic deadline overdue and sweep exactly once (as the
    # HTTP service thread would).
    op.deadline_mono = server._clock() - 0.001
    server._service_actions()
    assert op.deadline_noted
    assert record.cancel_event.is_set()
    # Truthful: still working, never finalized by the sweep, and the status
    # states the deadline without touching FreeCAD Console.
    assert record.status == "working"
    assert "deadline" in (record.status_message or "")
    assert server.has_pending_operations()
    assert FakeConsole.messages == []
    # The detached dispatcher job is marked timed out through the stored
    # Future handle: health reports the stuck running work while the job
    # stays inflight until true completion.
    assert op.future is not None
    health = gui_dispatch.get_dispatch_status()
    assert health["state"] == "stuck"
    assert health["timeout_seconds"] == op.deadline_s
    assert gui_dispatch.pending_count() == 1
    # A second sweep does not repeat the note.
    server._service_actions()
    assert len([op for op in server._ops.values() if op.deadline_noted]) == 1
    release.set()
    assert wait_until(
        lambda: server._task_store.get(task_id, principal=PRINCIPAL).terminal
    )
    # The real result still wins over the deadline cancellation request.
    assert server._task_store.get(task_id, principal=PRINCIPAL).status == "completed"
    # True completion clears the stuck health and the inflight job.
    assert wait_until(lambda: gui_dispatch.get_dispatch_status()["state"] == "healthy")
    assert gui_dispatch.pending_count() == 0


def test_service_actions_stay_responsive_while_gui_work_is_stuck():
    server = make_server()
    _reset_dispatcher_for_tests()
    entered = threading.Event()
    release = threading.Event()

    def stuck(ctx, arguments):
        entered.set()
        release.wait(timeout=5.0)
        return {"tool": "inspect_objects"}

    STUB_HANDLERS["inspect_objects"] = stuck
    response = dispatch(
        server,
        "tools/call",
        {"name": "inspect_objects", "arguments": {}},
        rpc_id=1,
    )
    assert entered.wait(timeout=5.0)
    # The service thread sweeps independently of the stuck GUI callable:
    # discovery and list stay answered from cached/static state too.
    server._service_actions()
    server._service_actions()
    assert server.has_pending_operations()  # nothing was finalized
    release.set()
    assert wait_until(lambda: not server.has_pending_operations())
    events = drain_stream(response, 2, timeout=5.0)
    assert events[0]["result"]["resultType"] == "complete"


def test_script_session_cap_is_bounded():
    server = make_server()
    for index in range(server_module.MAX_SCRIPT_SESSIONS):
        server.ensure_script_namespace(f"s{index}")
    with pytest.raises(protocol.ToolError) as exc:
        server.ensure_script_namespace("one-too-many")
    assert exc.value.code == "SERVER_BUSY"
    assert exc.value.details["reason"] == "session_limit"


def test_canonical_path_and_fingerprint_helpers():
    server = make_server()
    assert server.canonical_path("/tmp/fc-test/sub/file.FCStd") == (
        "/private/tmp/fc-test/sub/file.FCStd"
        if sys.platform == "darwin"
        else "/tmp/fc-test/sub/file.FCStd"
    )
    with pytest.raises(protocol.ToolError) as exc:
        server.canonical_path("/etc/passwd")
    assert exc.value.code == protocol.PATH_NOT_ALLOWED
    assert server.file_fingerprint("/tmp/fc-test/missing") is None
