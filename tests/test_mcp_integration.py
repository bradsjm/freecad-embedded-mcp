"""End-to-end integration: real McpHTTPServer wired to the real Server.dispatch.

FreeCAD and the GUI dispatcher are stubbed at their actual boundary (the
shared ``test_server`` harness); the wire is REAL: a bound
``McpHTTPServer`` on an OS-assigned port driven through ``http.client``.
Covers the plan's acceptance chain — legacy initialize on every revision
with a session header, the initialized notification, tools/list with
exactly 24 tools, discover_capabilities and a harmless run_script as
legacy-shaped final results — plus the guarantee that modern requests
still validate unchanged and never acquire a legacy session implicitly.
"""

import http.client
import json
import sys
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import test_server as ts

# server.py has already bound the harness's stub tool modules; drop the
# fake tool package so later test files import the real modules again.
for _name in [
    _key
    for _key in list(sys.modules)
    if _key == "mcp_server.tools" or _key.startswith("mcp_server.tools.")
]:
    del sys.modules[_name]

from mcp_server.http_server import McpHTTPServer
from mcp_server.legacy_protocol import (
    LEGACY_PROTOCOL_VERSIONS,
)
from mcp_server.protocol import (
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSION,
)

TOKEN = "integration-token"

PRINCIPAL = "sha256:testprincipal"


@pytest.fixture()
def wired_server():
    """Real HTTP transport + real dispatch, GUI queue drained by a waker."""
    ts.STUB_CALLS.clear()
    ts.PREFLIGHT_RESULTS.clear()
    ts.STUB_HANDLERS.clear()
    waker = ts._reset_dispatcher_for_tests()
    server = ts.make_server()
    server._static_capabilities = {"freecad": {"version": [1, 1, 3]}}
    http = McpHTTPServer(
        server.dispatch,
        token=TOKEN,
        host="127.0.0.1",
        port=0,
        remote_enabled=True,
        service_hook=server._service_actions,
    )
    http.start()
    try:
        yield server, http, waker
    finally:
        http.stop()


def _post(transport, body: dict, headers: dict) -> http.client.HTTPResponse:
    connection = http.client.HTTPConnection("127.0.0.1", transport.port, timeout=5)
    try:
        payload = json.dumps(body).encode("utf-8")
        base = {
            "Host": f"127.0.0.1:{transport.port}",
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        base.update(headers)
        connection.request("POST", "/mcp", body=payload, headers=base)
        return connection.getresponse()
    except Exception:
        connection.close()
        raise


def _read_json(response) -> tuple[int, dict, dict]:
    body = response.read()
    headers = {name.lower(): value for name, value in response.getheaders()}
    return response.status, json.loads(body), headers


def _read_sse(response) -> list[dict]:
    """Read one chunked SSE response and return its data events."""
    body = response.read().decode("utf-8")
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


def _initialize(version: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "connection-check", "version": "1"},
        },
    }


def _modern(method: str, rpc_id: int, params: dict | None = None) -> tuple[dict, dict]:
    params = dict(params or {})
    params["_meta"] = {
        META_PROTOCOL_VERSION: SUPPORTED_PROTOCOL_VERSION,
        META_CLIENT_INFO: {"name": "modern-client", "version": "1"},
        META_CLIENT_CAPABILITIES: {},
    }
    message = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
    headers = {"MCP-Protocol-Version": SUPPORTED_PROTOCOL_VERSION, "Mcp-Method": method}
    return message, headers


def test_legacy_initialize_negotiates_every_revision_over_real_http(wired_server):
    _server, http, _waker = wired_server
    for version in LEGACY_PROTOCOL_VERSIONS:
        response = _post(http, _initialize(version), {})
        status, body, headers = _read_json(response)
        assert status == 200
        assert headers.get("mcp-session-id")
        assert body["result"]["protocolVersion"] == version
        assert body["result"]["serverInfo"]["name"] == "freecad-mcp-addon"
        assert body["result"]["capabilities"]["tools"] == {"listChanged": False}


def test_legacy_session_flow_delivers_final_results_over_sse(wired_server):
    _server, http, _waker = wired_server
    response = _post(http, _initialize("2025-11-25"), {})
    _status, _body, headers = _read_json(response)
    session = {"mcp-session-id": headers["mcp-session-id"]}

    # notifications/initialized: accepted, empty 202.
    initialized = _post(
        http,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session,
    )
    assert initialized.status == 202
    assert initialized.read() == b""

    # tools/list: exactly 24 tools, no modern envelope metadata.
    response = _post(
        http,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        session,
    )
    status, body, _headers = _read_json(response)
    assert status == 200
    result = body["result"]
    assert len(result["tools"]) == 24
    assert [tool["name"] for tool in result["tools"]] == list(ts.server_module.PLAN_TOOL_ORDER)
    assert "resultType" not in result
    assert "ttlMs" not in result

    # discover_capabilities through tools/call: a streamed final result.
    response = _post(
        http,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "discover_capabilities", "arguments": {}},
        },
        session,
    )
    assert response.getheader("Content-Type") == "text/event-stream"
    events = _read_sse(response)
    finals = [event for event in events if "result" in event and event.get("id") == 3]
    assert len(finals) == 1
    assert finals[0]["result"].get("isError") is not True
    assert finals[0]["result"]["structuredContent"]["capabilities"]

    # A harmless run_script: a final result, never a task id.
    response = _post(
        http,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "run_script", "arguments": {}},
        },
        session,
    )
    events = _read_sse(response)
    finals = [event for event in events if event.get("id") == 4]
    assert len(finals) == 1
    assert "taskId" not in json.dumps(finals[0])
    assert finals[0]["result"]["content"][0]["type"] == "text"


def test_legacy_formless_client_falls_back_to_execution(wired_server):
    # 1.0 fallback over real HTTP: a formless 2025-03-26 client is never
    # blocked by consent; the consent-requiring tool runs unprompted.
    _server, http, _waker = wired_server
    response = _post(http, _initialize("2025-03-26"), {})
    _status, _body, headers = _read_json(response)
    session = {"mcp-session-id": headers["mcp-session-id"]}
    initialized = _post(
        http,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session,
    )
    assert initialized.status == 202
    ts.PREFLIGHT_RESULTS["close_document"] = {
        "requires_consent": True,
        "message": "Close document?",
        "tool": "close_document",
    }
    response = _post(
        http,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "close_document", "arguments": {}},
        },
        session,
    )
    events = _read_sse(response)
    finals = [event for event in events if event.get("id") == 9]
    assert len(finals) == 1
    assert finals[0]["result"].get("isError") is not True
    assert "elicitation" not in finals[0]["result"]["content"][0]["text"]
    assert len(ts.STUB_CALLS) == 1  # executed once, without any prompt


def test_modern_requests_validate_unchanged_and_skip_legacy_sessions(
    wired_server,
):
    _server, http, _waker = wired_server
    message, headers = _modern("tools/list", 11)
    response = _post(http, message, headers)
    status, body, response_headers = _read_json(response)
    assert status == 200
    assert len(body["result"]["tools"]) == 24
    assert body["result"]["resultType"] == "complete"
    # A modern request never acquires a legacy session implicitly.
    assert response_headers.get("mcp-session-id") is None
    assert http.legacy._sessions == {}


def test_modern_metadata_rejections_are_unchanged_over_http(wired_server):
    _server, http, _waker = wired_server
    message = {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "tools/list",
        "params": {
            "_meta": {
                META_PROTOCOL_VERSION: SUPPORTED_PROTOCOL_VERSION,
                META_CLIENT_INFO: {"name": "modern-client", "version": "1"},
                META_CLIENT_CAPABILITIES: {},
            }
        },
    }
    headers = {"MCP-Protocol-Version": SUPPORTED_PROTOCOL_VERSION}
    response = _post(http, message, headers)
    status, body, _response_headers = _read_json(response)
    # Missing the Mcp-Method mirror header: modern header validation.
    assert status == 400
    assert body["error"]["code"] == -32020
    # A session id alongside modern metadata is a hard error, not a
    # downgrade.
    headers = {
        "MCP-Protocol-Version": SUPPORTED_PROTOCOL_VERSION,
        "Mcp-Method": "tools/list",
        "mcp-session-id": "whatever",
    }
    response = _post(http, message, headers)
    status, body, _response_headers = _read_json(response)
    assert status == 400
    assert body["error"]["code"] == -32600
