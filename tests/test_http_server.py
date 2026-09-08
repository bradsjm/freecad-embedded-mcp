"""Real-HTTP-client tests for the embedded MCP transport (PLAN section 3).

Every test drives an actual ThreadingHTTPServer bound to port 0 through
``http.client`` or raw sockets, covering the plan's transport failures,
notification 202 handling, chunked SSE framing/keepalive/disconnect behavior
and shutdown semantics.
"""

import base64
import contextlib
import hashlib
import http.client
import json
import queue
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.http_server import (  # noqa: E402
    MAX_BODY_BYTES,
    McpHTTPServer,
    StreamResponse,
    peer_in_allowed_networks,
)
from mcp_server.ip_parse import parse_allowed_networks  # noqa: E402
from mcp_server.protocol import ProtocolError  # noqa: E402

TOKEN = "test-token-value"
SUPPORTED_VERSION = "2026-07-28"

STREAM_METHOD = "test/stream"


# --------------------------------------------------------------------------
# helpers


def _meta(version=SUPPORTED_VERSION):
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {
            "name": "transport-test",
            "version": "1",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def valid_request(rpc_id=1, method="test/echo", params=None, version=SUPPORTED_VERSION):
    if params is None:
        params = {"echo": "hi", "_meta": _meta(version)}
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}


def routing_headers(
    method="test/echo", version=SUPPORTED_VERSION, name=None, raw_name=None
):
    headers = {
        "MCP-Protocol-Version": version,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = raw_name if raw_name is not None else name
    return headers


class StreamFixture:
    """Test-owned event source handed out through the dispatch callback."""

    def __init__(self):
        self.queue = queue.SimpleQueue()
        self.lock = threading.Lock()
        self.disconnects = 0
        self.disconnect_event = threading.Event()

    def on_disconnect(self):
        with self.lock:
            self.disconnects += 1
        self.disconnect_event.set()

    def stream_response(self, keepalive_interval=None):
        return StreamResponse(
            self.queue,
            on_disconnect=self.on_disconnect,
            keepalive_interval=keepalive_interval,
        )


def stream_dispatch(fixture, keepalive_interval=None):
    def dispatch(message, principal, connection_id):
        if message["method"] == STREAM_METHOD:
            return fixture.stream_response(keepalive_interval)
        raise ProtocolError(-32601, f"unknown method: {message['method']}")

    return dispatch


def echo_dispatch(message, principal, connection_id):
    if message["method"] == "test/echo":
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"resultType": "complete", "echo": message["params"].get("echo")},
        }
    raise ProtocolError(-32601, f"unknown method: {message['method']}")


class Server:
    """Started McpHTTPServer on port 0 with a recording dispatch wrapper."""

    def __init__(self, dispatch, *, token=TOKEN, **kwargs):
        self.calls = []
        self._calls_lock = threading.Lock()
        self._user_dispatch = dispatch

        def recording_dispatch(message, principal, connection_id):
            with self._calls_lock:
                self.calls.append((message, principal, connection_id))
            return dispatch(message, principal, connection_id)

        self.server = McpHTTPServer(recording_dispatch, token=token, port=0, **kwargs)
        self.server.start()

    @property
    def port(self):
        return self.server.port

    def dispatch_calls(self):
        with self._calls_lock:
            return list(self.calls)

    def post(self, body, headers=None, *, path="/mcp", http_method="POST"):
        if body is None:
            payload = b""
        elif isinstance(body, (bytes, bytearray)):
            payload = bytes(body)
        else:
            payload = json.dumps(body).encode("utf-8")
        base = {
            "Host": f"127.0.0.1:{self.port}",
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        merged = {**base, **(headers or {})}
        base = {name: value for name, value in merged.items() if value is not None}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(http_method, path, body=payload, headers=base)
            response = conn.getresponse()
            data = response.read()
            return (
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                data,
            )
        finally:
            conn.close()

    def stop(self):
        self.server.stop()


@contextlib.contextmanager
def running_server(dispatch, **kwargs):
    server = Server(dispatch, **kwargs)
    try:
        yield server
    finally:
        server.stop()


def raw_post(server, *, extra_headers=(), body=b"", path="/mcp", method="POST"):
    """Send a raw request with the full valid header set plus extras."""
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: 127.0.0.1:{server.port}",
        f"Authorization: Bearer {TOKEN}",
        "Content-Type: application/json",
        "Accept: " + "application/json, text/event-stream",
        f"MCP-Protocol-Version: {SUPPORTED_VERSION}",
        "Mcp-Method: test/echo",
    ]
    lines.extend(extra_headers)
    payload = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    return raw_exchange(server.port, payload)


def raw_exchange(port, payload, *, timeout=5.0):
    """Send raw bytes and read until EOF; returns (status, headers, body)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(payload)
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
    return split_response(b"".join(chunks))


def split_response(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, body


def open_raw_stream(server, body_obj):
    """Open a keep-alive raw socket POST and return the connected socket."""
    payload = json.dumps(body_obj).encode("utf-8")
    base = {
        "Host": f"127.0.0.1:{server.port}",
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": SUPPORTED_VERSION,
        "Mcp-Method": STREAM_METHOD,
    }
    lines = ["POST /mcp HTTP/1.1"]
    lines.extend(f"{name}: {value}" for name, value in base.items())
    lines.append(f"Content-Length: {len(payload)}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    sock.sendall(request)
    return sock


def recv_until(sock, marker, *, deadline_s=5.0):
    buffer = b""
    end = time.monotonic() + deadline_s
    while marker not in buffer:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for {marker!r}; got {buffer!r}")
        sock.settimeout(min(0.2, remaining))
        try:
            data = sock.recv(65536)
        except socket.timeout:
            continue
        if not data:
            raise AssertionError(
                f"connection closed waiting for {marker!r}; got {buffer!r}"
            )
        buffer += data
    return buffer


def recv_window(sock, *, window_s=1.0):
    buffer = b""
    end = time.monotonic() + window_s
    while time.monotonic() < end:
        sock.settimeout(max(0.05, end - time.monotonic()))
        try:
            data = sock.recv(65536)
        except socket.timeout:
            continue
        if not data:
            break
        buffer += data
    return buffer


def recv_until_eof(sock, *, deadline_s=3.0):
    """Read until the server closes the connection; returns the tail bytes."""
    end = time.monotonic() + deadline_s
    tail = b""
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise AssertionError("connection did not close before the deadline")
        sock.settimeout(min(0.2, remaining))
        try:
            data = sock.recv(65536)
        except socket.timeout:
            continue
        tail += data
        if not data:
            return tail


def parse_chunks(body):
    """Parse chunked framing; returns payload list with None as terminator."""
    chunks = []
    index = 0
    while index < len(body):
        line_end = body.index(b"\r\n", index)
        size = int(body[index:line_end], 16)
        start = line_end + 2
        if size == 0:
            assert body[start:] == b"\r\n", "zero chunk must end the stream"
            chunks.append(None)
            return chunks
        chunks.append(body[start : start + size])
        index = start + size + 2
    raise AssertionError("chunked body ended without a zero chunk")


def sse_request(rpc_id=9):
    return valid_request(rpc_id=rpc_id, method=STREAM_METHOD, params={"_meta": _meta()})


# --------------------------------------------------------------------------
# authentication and restrictions


def test_missing_authorization_returns_401():
    with running_server(echo_dispatch) as server:
        status, headers, _ = server.post(
            valid_request(),
            {**routing_headers(), "Authorization": None},
        )
        assert status == 401
        assert headers["www-authenticate"].startswith("Bearer")
        assert server.dispatch_calls() == []


def test_wrong_token_returns_401():
    with running_server(echo_dispatch, token="correct-token") as server:
        status, _, _ = server.post(
            valid_request(),
            {**routing_headers(), "Authorization": f"Bearer {TOKEN}"},
        )
        assert status == 401
        assert server.dispatch_calls() == []


def test_disallowed_origin_returns_403():
    with running_server(echo_dispatch) as server:
        status, _, _ = server.post(
            valid_request(),
            {**routing_headers(), "Origin": "http://evil.example"},
        )
        assert status == 403
        assert server.dispatch_calls() == []


def test_disallowed_host_returns_403():
    with running_server(echo_dispatch) as server:
        status, _, _ = server.post(
            valid_request(),
            {**routing_headers(), "Host": "evil.example:1"},
        )
        assert status == 403


def test_missing_host_returns_403():
    with running_server(echo_dispatch) as server:
        status, _, _ = server.post(
            valid_request(),
            {**routing_headers(), "Host": ""},
        )
        assert status == 403


def test_host_header_without_actual_port_is_rejected():
    with running_server(echo_dispatch) as server:
        status, _, _ = server.post(
            valid_request(),
            {**routing_headers(), "Host": "127.0.0.1:1"},
        )
        assert status == 403


def test_peer_network_restriction_is_additional():
    networks = parse_allowed_networks("127.0.0.0/8")
    assert peer_in_allowed_networks("127.0.0.1", networks)
    assert not peer_in_allowed_networks("192.168.1.5", networks)
    assert not peer_in_allowed_networks("not-an-ip", networks)
    strict = parse_allowed_networks("127.0.0.1")
    assert peer_in_allowed_networks("127.0.0.1", strict)
    assert not peer_in_allowed_networks("127.0.0.2", strict)


def test_construction_fails_closed_on_invalid_allowed_ips():
    with pytest.raises(ValueError):
        McpHTTPServer(echo_dispatch, token=TOKEN, port=0, allowed_ips="nope")


def test_remote_mode_accepts_non_loopback_host_and_origin():
    with running_server(echo_dispatch, remote_enabled=True) as server:
        status, _, _ = server.post(
            valid_request(),
            {
                **routing_headers(),
                "Host": "192.168.1.10:9876",
                "Origin": "http://192.168.1.10:9876",
            },
        )
        assert status == 200
        assert len(server.dispatch_calls()) == 1


def test_remote_mode_still_rejects_peer_outside_allow_list():
    with running_server(
        echo_dispatch, remote_enabled=True, allowed_ips="10.0.0.0/8"
    ) as server:
        # The test client connects from 127.0.0.1, which is not allowed.
        status, _, _ = server.post(valid_request(), routing_headers())
        assert status == 403
        assert server.dispatch_calls() == []


def test_remote_mode_still_requires_bearer_token():
    with running_server(echo_dispatch, remote_enabled=True) as server:
        status, _, _ = server.post(
            valid_request(),
            {**routing_headers(), "Authorization": None},
        )
        assert status == 401
        assert server.dispatch_calls() == []


def test_remote_mode_reports_bound_host():
    with running_server(echo_dispatch, remote_enabled=True, host="127.0.0.1") as server:
        assert server.server.remote_enabled is True
        assert server.server.bound_host == "127.0.0.1"


def test_remote_mode_open_list_accepts_loopback_peer():
    # allowed_ips defaults to empty: any peer may connect; token gates access.
    with running_server(
        echo_dispatch, remote_enabled=True, allowed_ips=""
    ) as server:
        status, _, _ = server.post(valid_request(), routing_headers())
        assert status == 200


def test_local_mode_without_token_accepts_request():
    with running_server(echo_dispatch, token=None) as server:
        status, _, _ = server.post(
            valid_request(),
            {**routing_headers(), "Authorization": None},
        )
        assert status == 200
        assert len(server.dispatch_calls()) == 1
        assert server.dispatch_calls()[0][1] == "local"


def test_local_mode_without_token_still_rejects_browser_host():
    with running_server(echo_dispatch, token=None) as server:
        status, _, _ = server.post(
            valid_request(),
            {**routing_headers(), "Host": "evil.example:1"},
        )
        assert status == 403
        assert server.dispatch_calls() == []


def test_local_mode_without_token_still_rejects_peer_outside_loopback():
    # Local mode consults loopback membership, not allowed_ips: even an
    # open remote-style list must not widen a loopback-bound server.
    with running_server(echo_dispatch, token=None, allowed_ips="") as server:
        assert server.server.peer_allowed("127.0.0.1") is True
        assert server.server.peer_allowed("192.168.1.5") is False
        assert server.server.peer_allowed("::1") is True
        assert server.server.peer_allowed("not-an-ip") is False

    with running_server(
        echo_dispatch, token=TOKEN, remote_enabled=True, allowed_ips=""
    ) as server:
        assert server.server.peer_allowed("192.168.1.5") is True

    with running_server(
        echo_dispatch, token=TOKEN, remote_enabled=True, allowed_ips="10.0.0.0/8"
    ) as server:
        assert server.server.peer_allowed("192.168.1.5") is False
        assert server.server.peer_allowed("10.1.2.3") is True


def test_non_json_content_type_returns_415():
    with running_server(echo_dispatch) as server:
        status, _, _ = server.post(
            valid_request(),
            {**routing_headers(), "Content-Type": "text/plain"},
        )
        assert status == 415
        assert server.dispatch_calls() == []


def test_accept_without_sse_returns_406():
    with running_server(echo_dispatch) as server:
        status, _, _ = server.post(
            valid_request(),
            {**routing_headers(), "Accept": "application/json"},
        )
        assert status == 406
        assert server.dispatch_calls() == []


def test_accept_quality_zero_media_returns_406():
    with running_server(echo_dispatch) as server:
        for accept in (
            "application/json;q=0, text/event-stream",
            "application/json, text/event-stream;q=0.0",
            "application/json;q=abc, text/event-stream",
        ):
            status, _, _ = server.post(
                valid_request(), {**routing_headers(), "Accept": accept}
            )
            assert status == 406
        assert server.dispatch_calls() == []
        status, _, _ = server.post(
            valid_request(),
            {
                **routing_headers(),
                "Accept": "application/json;q=0.9, text/event-stream;q=0.1",
            },
        )
        assert status == 200


def test_every_verb_is_authenticated_before_routing():
    with running_server(echo_dispatch) as server:
        for method in ("OPTIONS", "HEAD", "PROPFIND"):
            status, headers, _ = server.post(
                None,
                {**routing_headers(), "Authorization": None},
                http_method=method,
            )
            assert status == 401
            assert headers["www-authenticate"].startswith("Bearer")
        assert server.dispatch_calls() == []


def test_authenticated_non_post_verbs_return_404_head_without_body():
    with running_server(echo_dispatch) as server:
        for method in ("OPTIONS", "PATCH", "PROPFIND"):
            status, _, body = server.post(None, routing_headers(), http_method=method)
            assert status == 404
            assert json.loads(body)["error"]["code"] == -32601
        status, headers, body = server.post(None, routing_headers(), http_method="HEAD")
        assert status == 404
        assert body == b""  # HEAD carries the headers a GET would, no payload
        assert int(headers["content-length"]) > 0
        assert server.dispatch_calls() == []


# --------------------------------------------------------------------------
# bounded body and header checks


def test_duplicate_content_length_is_rejected():
    with running_server(echo_dispatch) as server:
        status, _, body = raw_post(
            server,
            extra_headers=["Content-Length: 2", "Content-Length: 2"],
            body=b"{}",
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32600
        assert server.dispatch_calls() == []


def test_duplicate_authorization_is_rejected():
    with running_server(echo_dispatch) as server:
        status, _, body = raw_post(
            server,
            extra_headers=[
                "Content-Length: 2",
                f"Authorization: Bearer {TOKEN}",
            ],
            body=b"{}",
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32600
        assert server.dispatch_calls() == []


def test_transfer_encoding_request_body_is_rejected():
    with running_server(echo_dispatch) as server:
        valid = json.dumps(valid_request(rpc_id=7)).encode()
        status, _, body = raw_post(
            server,
            extra_headers=[
                f"Content-Length: {len(valid)}",
                "Transfer-Encoding: chunked",
            ],
            body=valid,
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32600
        assert server.dispatch_calls() == []


def test_malformed_content_length_is_rejected():
    with running_server(echo_dispatch) as server:
        status, _, body = raw_post(
            server, extra_headers=["Content-Length: twelve"], body=b"{}"
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32600
        negative, _, _ = raw_post(server, extra_headers=["Content-Length: -1"])
        assert negative == 400
        assert server.dispatch_calls() == []


def test_oversized_body_returns_413_before_reading():
    with running_server(echo_dispatch) as server:
        status, _, _ = raw_post(
            server,
            extra_headers=[f"Content-Length: {MAX_BODY_BYTES + 1}"],
        )
        assert status == 413
        assert server.dispatch_calls() == []


def test_missing_content_length_is_rejected():
    with running_server(echo_dispatch) as server:
        status, _, body = raw_post(server, extra_headers=[])
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32600
        assert server.dispatch_calls() == []


# --------------------------------------------------------------------------
# routing and protocol-level failures


def test_unknown_path_returns_404_method_not_found():
    with running_server(echo_dispatch) as server:
        status, _, body = server.post(valid_request(), routing_headers(), path="/other")
        assert status == 404
        parsed = json.loads(body)
        assert parsed["error"]["code"] == -32601
        assert server.dispatch_calls() == []


def test_get_mcp_returns_404_method_not_found():
    with running_server(echo_dispatch) as server:
        status, _, body = server.post(None, routing_headers(), http_method="GET")
        assert status == 404
        assert json.loads(body)["error"]["code"] == -32601
        assert server.dispatch_calls() == []


def test_parse_error_returns_400_without_fabricated_id():
    with running_server(echo_dispatch) as server:
        status, _, body = server.post(b"{not json", routing_headers())
        assert status == 400
        parsed = json.loads(body)
        assert parsed["error"]["code"] == -32700
        assert "id" not in parsed  # unknown id is omitted, never fabricated
        assert server.dispatch_calls() == []


def test_non_object_body_returns_400():
    with running_server(echo_dispatch) as server:
        status, _, body = server.post(b"[1,2]", routing_headers())
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32600
        assert server.dispatch_calls() == []


def test_missing_metadata_returns_400_invalid_params():
    with running_server(echo_dispatch) as server:
        message = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "test/echo",
            "params": {"echo": "hi"},
        }
        status, _, body = server.post(message, routing_headers())
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32602
        assert server.dispatch_calls() == []


def test_method_header_mismatch_returns_400_header_mismatch():
    with running_server(echo_dispatch) as server:
        status, _, body = server.post(
            valid_request(), routing_headers(method="other/method")
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32020
        assert server.dispatch_calls() == []


def test_missing_method_header_returns_400_header_mismatch():
    with running_server(echo_dispatch) as server:
        status, _, body = server.post(valid_request(), {})
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32020
        assert server.dispatch_calls() == []


def test_name_header_mismatch_returns_400():
    params = {"name": "real_tool", "_meta": _meta()}
    with running_server(echo_dispatch) as server:
        status, _, body = server.post(
            valid_request(method="tools/call", params=params),
            routing_headers(method="tools/call", name="other_tool"),
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32020
        assert server.dispatch_calls() == []


def _name_dispatch(message, principal, connection_id):
    return {
        "jsonrpc": "2.0",
        "id": message["id"],
        "result": {"resultType": "complete", "name": message["params"]["name"]},
    }


def test_sentinel_encoded_name_header_matches_body():
    unicode_name = "töol"
    params = {"name": unicode_name, "_meta": _meta()}
    encoded = (
        "=?base64?"
        + base64.b64encode(unicode_name.encode("utf-8")).decode("ascii")
        + "?="
    )

    with running_server(_name_dispatch) as server:
        status, _, body = server.post(
            valid_request(method="tools/call", params=params),
            routing_headers(method="tools/call", name=unicode_name, raw_name=encoded),
        )
        assert status == 200
        assert json.loads(body)["result"]["name"] == unicode_name


def test_sentinel_lookalike_name_requires_encoding():
    lookalike = "=?base64?x?="
    params = {"name": lookalike, "_meta": _meta()}
    encoded = (
        "=?base64?" + base64.b64encode(lookalike.encode("utf-8")).decode("ascii") + "?="
    )

    with running_server(_name_dispatch) as server:
        status, _, body = server.post(
            valid_request(method="tools/call", params=params),
            routing_headers(method="tools/call", name=lookalike, raw_name=encoded),
        )
        assert status == 200
        assert json.loads(body)["result"]["name"] == lookalike
        # A plain (unencoded) lookalike header must be rejected as malformed.
        status, _, _ = server.post(
            valid_request(method="tools/call", params=params),
            routing_headers(method="tools/call", name=lookalike),
        )
        assert status == 400


def test_unsupported_version_returns_400_with_data():
    version = "2025-11-25"
    with running_server(echo_dispatch) as server:
        status, _, body = server.post(
            valid_request(version=version),
            routing_headers(version=version),
        )
        assert status == 400
        error = json.loads(body)["error"]
        assert error["code"] == -32022
        assert error["data"] == {"supported": [SUPPORTED_VERSION], "requested": version}
        assert server.dispatch_calls() == []


def test_header_version_mismatch_precedes_version_negotiation():
    # Header says an unsupported version, body says the supported one: the
    # mismatch (-32020) must win over version negotiation (-32022).
    with running_server(echo_dispatch) as server:
        status, _, body = server.post(
            valid_request(),
            routing_headers(version="2025-11-25"),
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32020
        assert server.dispatch_calls() == []


def test_unknown_method_returns_404_with_error_id():
    with running_server(echo_dispatch) as server:
        status, _, body = server.post(
            valid_request(rpc_id=17, method="nope/x"),
            routing_headers(method="nope/x"),
        )
        assert status == 404
        parsed = json.loads(body)
        assert parsed["error"]["code"] == -32601
        assert parsed["id"] == 17
        assert len(server.dispatch_calls()) == 1


# --------------------------------------------------------------------------
# notifications


def test_valid_notification_returns_empty_202_without_dispatch():
    with running_server(echo_dispatch) as server:
        notification = {
            "jsonrpc": "2.0",
            "method": "test/notify",
            "params": {"_meta": _meta()},
        }
        status, headers, body = server.post(
            notification, routing_headers(method="test/notify")
        )
        assert status == 202
        assert body == b""
        assert int(headers["content-length"]) == 0
        assert server.dispatch_calls() == []


def test_invalid_notification_is_rejected_and_not_dispatched():
    with running_server(echo_dispatch) as server:
        notification = {
            "jsonrpc": "2.0",
            "method": "test/notify",
            "params": {"_meta": _meta()},
        }
        status, _, body = server.post(
            notification, routing_headers(method="other/notify")
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32020
        assert server.dispatch_calls() == []


# --------------------------------------------------------------------------
# JSON request path


def test_valid_request_returns_json_with_principal_and_port0():
    with running_server(echo_dispatch) as server:
        assert 0 < server.port < 65536  # port 0 really bound an OS port
        status, headers, body = server.post(valid_request(rpc_id=11), routing_headers())
        assert status == 200
        assert headers["content-type"] == "application/json"
        assert int(headers["content-length"]) == len(body)
        assert json.loads(body) == {
            "jsonrpc": "2.0",
            "id": 11,
            "result": {"resultType": "complete", "echo": "hi"},
        }
        message, principal, connection_id = server.dispatch_calls()[0]
        assert message["id"] == 11
        assert message["method"] == "test/echo"
        expected = "sha256:" + hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:32]
        assert principal == expected
        assert isinstance(connection_id, str) and connection_id


# --------------------------------------------------------------------------
# SSE streams


def test_sse_stream_is_consumable_by_http_client():
    fixture = StreamFixture()
    fixture.queue.put(
        {"jsonrpc": "2.0", "id": 7, "result": {"resultType": "complete", "step": "ack"}}
    )
    with running_server(stream_dispatch(fixture)) as server:
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            body = json.dumps(sse_request(rpc_id=7)).encode("utf-8")
            headers = {
                "Host": f"127.0.0.1:{server.port}",
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": SUPPORTED_VERSION,
                "Mcp-Method": STREAM_METHOD,
                "Content-Length": str(len(body)),
            }
            conn.request("POST", "/mcp", body=body, headers=headers)
            response = conn.getresponse()
            assert response.status == 200
            response_headers = {
                name.lower(): value for name, value in response.getheaders()
            }
            assert response_headers["content-type"] == "text/event-stream"
            fixture.queue.put(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "result": {"resultType": "complete", "final": True},
                }
            )
            fixture.queue.put(None)
            payload = response.read()
        finally:
            conn.close()

    events = [
        line[len("data: ") :]
        for line in payload.decode("utf-8").split("\n\n")
        if line.startswith("data: ")
    ]
    assert len(events) == 2
    assert json.loads(events[0])["result"]["step"] == "ack"
    assert json.loads(events[1])["result"]["final"] is True
    assert fixture.disconnect_event.wait(0.5) is False
    assert fixture.disconnects == 0


def test_sse_chunked_framing_terminates_with_zero_chunk():
    fixture = StreamFixture()
    with running_server(stream_dispatch(fixture)) as server:
        sock = open_raw_stream(server, sse_request(rpc_id=9))
        try:
            head = recv_until(sock, b"\r\n\r\n")
            status_line, *header_lines = head.decode("latin-1").split("\r\n")
            assert status_line.endswith(" 200 OK")
            header_map = {}
            for line in header_lines[1:]:
                name, _, value = line.partition(":")
                header_map[name.strip().lower()] = value.strip()
            assert header_map["transfer-encoding"].lower() == "chunked"
            assert header_map["content-type"] == "text/event-stream"
            assert header_map["cache-control"] == "no-cache"
            assert header_map["x-accel-buffering"] == "no"

            fixture.queue.put(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "result": {"resultType": "complete", "step": "ack"},
                }
            )
            ack = recv_until(sock, b'"step":"ack"')
            fixture.queue.put(
                {"jsonrpc": "2.0", "id": 9, "result": {"resultType": "complete"}}
            )
            fixture.queue.put(None)
            raw = recv_until(sock, b"0\r\n\r\n")
            # Nothing — keepalive, notification, trailer bytes — may follow
            # the zero chunk; the server must close promptly after it.
            assert recv_until_eof(sock, deadline_s=1.0) == b""
        finally:
            sock.close()

    # `head` above already consumed exactly the header block, so everything
    # read since (`ack`, `raw`) is pure chunked body. Partitioning the header
    # delimiter a second time would discard already-read chunk bytes.
    chunks = parse_chunks(ack + raw)

    assert chunks[-1] is None
    events = chunks[:-1]
    assert len(events) == 2
    assert events[0].startswith(b"data: ") and events[0].endswith(b"\n\n")
    assert events[1].startswith(b"data: ") and events[1].endswith(b"\n\n")
    first_ack = json.loads(events[0][len(b"data: ") : -2])
    assert first_ack["result"]["step"] == "ack"
    assert b'"resultType":"complete"' in events[1]


def test_sse_keepalive_comments_during_idle():
    fixture = StreamFixture()
    with running_server(stream_dispatch(fixture), keepalive_interval=0.2) as server:
        sock = open_raw_stream(server, sse_request(rpc_id=9))
        try:
            recv_until(sock, b"\r\n\r\n")
            window = recv_window(sock, window_s=0.8)
            assert window.count(b": keepalive") >= 2
            fixture.queue.put(
                {"jsonrpc": "2.0", "id": 9, "result": {"resultType": "complete"}}
            )
            fixture.queue.put(None)
            recv_until(sock, b"0\r\n\r\n")
        finally:
            sock.close()
    assert fixture.disconnect_event.wait(0.5) is False
    assert fixture.disconnects == 0


def test_client_disconnect_invokes_on_disconnect_exactly_once():
    fixture = StreamFixture()
    fixture.queue.put({"jsonrpc": "2.0", "id": 9, "result": {"partial": True}})
    with running_server(stream_dispatch(fixture), keepalive_interval=0.2) as server:
        sock = open_raw_stream(server, sse_request(rpc_id=9))
        try:
            recv_until(sock, b'"partial":true')
        finally:
            sock.close()  # abrupt client disconnect mid-stream
        assert fixture.disconnect_event.wait(5.0), "on_disconnect was not invoked"
        assert fixture.disconnects == 1


def test_normal_completion_skips_on_disconnect():
    fixture = StreamFixture()
    with running_server(stream_dispatch(fixture), keepalive_interval=0.2) as server:
        sock = open_raw_stream(server, sse_request(rpc_id=9))
        try:
            recv_until(sock, b"\r\n\r\n")
            fixture.queue.put(
                {"jsonrpc": "2.0", "id": 9, "result": {"resultType": "complete"}}
            )
            fixture.queue.put(None)
            recv_until(sock, b"0\r\n\r\n")
        finally:
            sock.close()
        assert fixture.disconnect_event.wait(1.0) is False
        assert fixture.disconnects == 0


def test_stream_source_failure_finalizes_as_disconnect():
    fixture = StreamFixture()
    with running_server(stream_dispatch(fixture), keepalive_interval=0.2) as server:
        sock = open_raw_stream(server, sse_request(rpc_id=9))
        try:
            recv_until(sock, b"\r\n\r\n")
            fixture.queue.put({"unserializable": float("nan")})
            remaining = recv_window(sock, window_s=2.0)
            assert b"0\r\n\r\n" not in remaining
        finally:
            sock.close()
        assert fixture.disconnect_event.wait(5.0)
        assert fixture.disconnects == 1


def test_stop_closes_active_streams_and_returns_promptly():
    fixture = StreamFixture()
    with running_server(stream_dispatch(fixture), keepalive_interval=0.3) as server:
        sock = open_raw_stream(server, sse_request(rpc_id=9))
        try:
            recv_until(sock, b"\r\n\r\n")
            started = time.monotonic()
            server.stop()
            elapsed = time.monotonic() - started
            assert elapsed < 5.0, "stop() must not wait on the streaming thread"
            assert fixture.disconnect_event.wait(5.0)
            assert fixture.disconnects == 1
            recv_until_eof(sock)
        finally:
            sock.close()


def test_stop_delivers_queued_final_result_with_zero_chunk():
    fixture = StreamFixture()
    with running_server(stream_dispatch(fixture)) as server:
        sock = open_raw_stream(server, sse_request(rpc_id=9))
        try:
            recv_until(sock, b"\r\n\r\n")  # headers only; nothing queued yet
            fixture.queue.put(
                {"jsonrpc": "2.0", "id": 9, "result": {"resultType": "complete"}}
            )
            fixture.queue.put(None)  # shutdown protocol: final, then sentinel
            started = time.monotonic()
            server.stop()
            assert time.monotonic() - started < 5.0, "stop must not join streams"
            tail = recv_until_eof(sock, deadline_s=5.0)
        finally:
            sock.close()
    chunks = parse_chunks(tail)
    assert chunks[-1] is None  # zero chunk: graceful termination, not truncation
    (final,) = chunks[:-1]
    assert final.startswith(b"data: ")
    assert b'"resultType":"complete"' in final
    assert fixture.disconnect_event.wait(0.5) is False
    assert fixture.disconnects == 0  # delivered final counts as completion
