"""Authenticated HTTP/SSE transport for the embedded MCP server.

Owns transport concerns only: bearer authentication, Host/Origin and
peer-IP restrictions, bounded body/header checks, JSON responses, chunked
request-thread-owned SSE streams and shutdown. Envelope and routing-header
validation is delegated to the ``protocol`` module contract; this module
never imports FreeCAD.

Construction API::

    server = McpHTTPServer(
        dispatch,                    # (message, principal, connection_id) -> dict | StreamResponse
        token="...",                 # bearer token; required in remote mode
        host="127.0.0.1",            # bind "0.0.0.0" when remote_enabled
        port=9876,                   # 0 binds an OS-assigned port; see .port
        allowed_ips="",              # peer allow-list; empty = any peer
        remote_enabled=False,        # True: token required, loopback Host/Origin skipped
    )
    server.start()                   # daemon thread running serve_forever
    server.port                      # actual bound port (resolves port 0)
    server.stop()                    # closes active streams, stops accept loop

``dispatch`` receives the protocol-validated message dict produced by
``protocol.validate_request`` (the transport never dispatches notifications)
and returns the full JSON-RPC response dict, or a :class:`StreamResponse` for
SSE replies. ``dispatch`` may raise ``protocol.ProtocolError``; the transport
maps it to an HTTP status and wraps it as a JSON-RPC error response. The
principal is a non-reversible fingerprint of the bearer token;
``connection_id`` is unique per TCP connection.
"""

import base64
import hmac
import http.server
import ipaddress
import json
import queue
import re
import socket
import sys
import threading
import traceback
import uuid
from hashlib import sha256

from .ip_parse import parse_allowed_networks
from .protocol import ProtocolError, error_response, validate_request

MCP_ENDPOINT = "/mcp"
DEFAULT_PORT = 9876
MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_READ_TIMEOUT = 10.0
DEFAULT_KEEPALIVE_INTERVAL = 15.0

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

# Headers that smuggle requests or routing when repeated; duplicates are
# rejected before any body byte is read.
_SINGLE_VALUE_HEADERS = frozenset(
    {
        "host",
        "authorization",
        "content-length",
        "content-type",
        "transfer-encoding",
        "mcp-protocol-version",
        "mcp-method",
        "mcp-name",
    }
)

_PARAM_HEADER_RE = re.compile(r"^mcp-param-(.+)$")
_DIGITS_RE = re.compile(r"[0-9]+")

# JSON-RPC error code -> HTTP status for protocol-level failures.
_HTTP_STATUS_BY_CODE = {
    -32700: 400,
    -32600: 400,
    -32601: 404,
    -32602: 400,
    -32603: 500,
    -32020: 400,
    -32021: 400,
    -32022: 400,
}

_KEEPALIVE = object()  # sentinel event: emit an SSE comment line

_ALLOWED_CHARACTERS = frozenset(chr(code) for code in (9, 32, *range(0x21, 0x7F)))


def peer_in_allowed_networks(ip_text, networks):
    """Return True when ``ip_text`` parses and falls inside ``networks``."""
    try:
        addr = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return any(addr in network for network in networks)


def _check_header_value_legality(value):
    """Validate the raw form of a routing/param header value.

    Plain values must consist of visible ASCII plus space/tab with no
    leading/trailing whitespace; ``=?base64?...?=`` sentinel values must
    decode as strict base64 UTF-8. Raises ValueError when illegal.
    """
    if value.startswith("=?base64?") and value.endswith("?="):
        inner = value[len("=?base64?") : -len("?=")]
        try:
            base64.b64decode(inner, validate=True).decode("utf-8")
        except Exception as exc:
            raise ValueError("malformed base64 sentinel header value") from exc
        return
    for char in value:
        if char not in _ALLOWED_CHARACTERS:
            raise ValueError("header value contains invalid characters")
    if value != value.strip():
        raise ValueError("header value has leading/trailing whitespace")


def _reject_json_constant(name):
    raise ValueError(f"invalid JSON constant: {name}")


class _StreamClosed(Exception):
    """Internal: the stream source ended abnormally or was force-closed."""


class StreamResponse:
    """Iterable SSE event source consumed by its owning request thread.

    ``events`` yields JSON-serializable message dicts. Queue-like sources
    (anything with ``get(timeout=...)``, e.g. ``queue.SimpleQueue``) end the
    stream with a ``None`` sentinel and support keepalive comments plus
    prompt server-shutdown closes while blocked; plain iterators end via
    ``StopIteration`` and cannot keepalive while blocked on ``next()``.

    ``on_disconnect`` is invoked exactly once, from the owning request
    thread, when the stream ends without its source completing — client
    disconnect, source failure, or a transport-forced close. It is never
    called after normal exhaustion, so a completed response never cancels
    work. Producers that finish normally must terminate their source
    themselves; :meth:`close` is the transport's bounded fallback.

    Server shutdown drains instead of dropping: once shutdown drain is
    requested (from :meth:`McpHTTPServer.stop`), every event already queued
    — including a final result and its ``None`` sentinel — is still
    delivered and the stream still terminates with a zero chunk, while a
    source with nothing queued ends promptly and counts as disconnected.
    A hard :meth:`close` overrides the drain and drops queued events.
    """

    def __init__(self, events, *, on_disconnect=None, keepalive_interval=None):
        self.events = events
        self.on_disconnect = on_disconnect
        self.keepalive_interval = keepalive_interval
        self._close_requested = threading.Event()
        self._drain_requested = threading.Event()
        self._finalize_lock = threading.Lock()
        self._finalized = False

    def close(self):
        """Request prompt termination (server shutdown fallback)."""
        self._close_requested.set()

    def _graceful_close(self):
        """Server-shutdown drain: deliver what is queued, then end promptly.

        Events already queued — including a final result and the ``None``
        sentinel — are still handed out; the wait for *new* events drops to
        zero, so a silent source cannot stall shutdown. A hard :meth:`close`
        takes precedence.
        """
        self._drain_requested.set()

    def next_event(self, timeout=None):
        """Return the next event dict, keepalive marker, or None at the end.

        Raises ``_StreamClosed`` when the stream was force-closed or the
        source raised. ``timeout`` applies only to queue-like sources. Once
        shutdown drain is requested, only already-queued events are
        delivered: the wait drops to zero and an empty source ends the
        stream instead of emitting keepalives.
        """
        if self._close_requested.is_set():
            raise _StreamClosed("stream closed by server")
        getter = getattr(self.events, "get", None)
        draining = self._drain_requested.is_set()
        if callable(getter):
            try:
                item = getter(timeout=0 if draining else timeout)
            except queue.Empty:
                if draining:
                    raise _StreamClosed("stream drained by server shutdown") from None
                return _KEEPALIVE
            except Exception as exc:
                raise _StreamClosed("stream source failed") from exc
            return None if item is None else item
        if draining:
            raise _StreamClosed("stream drained by server shutdown")
        try:
            return next(self.events)
        except StopIteration:
            return None
        except Exception as exc:
            raise _StreamClosed("stream source failed") from exc

    def _finalize(self, *, disconnected):
        """Run finalization at most once; fire ``on_disconnect`` when broken."""
        with self._finalize_lock:
            if self._finalized:
                return
            self._finalized = True
        if not disconnected:
            return
        callback, self.on_disconnect = self.on_disconnect, None
        if callback is None:
            return
        try:
            callback()
        except Exception:
            traceback.print_exc(file=sys.stderr)


class McpHTTPServer(http.server.ThreadingHTTPServer):
    """Authenticated ThreadingHTTPServer hosting ``POST /mcp``.

    Binds in the constructor (so ``port=0`` resolves immediately via
    :attr:`port`) and fails closed on invalid ``allowed_ips``.

    Security model: local mode (default) accepts loopback peers only and
    needs no token — the loopback Host/Origin checks stay active there.
    Remote mode (``remote_enabled=True``) requires a non-empty ``token``
    (bearer auth on every request) and skips the loopback Host/Origin
    checks; a non-empty ``allowed_ips`` list additionally restricts peers,
    an empty list accepts any peer.
    """

    daemon_threads = True

    def __init__(
        self,
        dispatch,
        *,
        token=None,
        host="127.0.0.1",
        port=DEFAULT_PORT,
        allowed_ips="",
        remote_enabled=False,
        max_body_bytes=MAX_BODY_BYTES,
        read_timeout=DEFAULT_READ_TIMEOUT,
        keepalive_interval=DEFAULT_KEEPALIVE_INTERVAL,
        service_hook=None,
    ):
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise ValueError("token must be None or a non-empty string")
        if remote_enabled and not (isinstance(token, str) and token.strip()):
            raise ValueError("remote_enabled requires a token")
        self.dispatch = dispatch
        self.token = token or ""
        # Non-reversible principal fingerprint; never log the token itself.
        # Tokenless local mode shares one stable principal so consent
        # challenges still bind to a single identity.
        if self.token:
            self.principal = (
                "sha256:" + sha256(self.token.encode("utf-8")).hexdigest()[:32]
            )
        else:
            self.principal = "local"
        self.allowed_networks = parse_allowed_networks(allowed_ips)
        self.remote_enabled = bool(remote_enabled)
        self.bound_host = host
        self.max_body_bytes = max_body_bytes
        self.read_timeout = read_timeout
        self.keepalive_interval = keepalive_interval
        #: Callable invoked from the serve loop's service_actions: the
        #: server's independent monotonic deadline sweep. Must never touch
        #: the GUI or FreeCAD objects; exceptions are swallowed so the
        #: accept loop is never disturbed.
        self.service_hook = service_hook
        self._active_streams = set()
        self._streams_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._serving = False
        self._stopped = False
        super().__init__((host, port), _McpRequestHandler)

    def service_actions(self):
        """Called by ``serve_forever`` each poll; runs the hooked sweep."""

        hook = self.service_hook
        if hook is None:
            return
        try:
            hook()
        except Exception:
            pass

    @property
    def port(self):
        """Actual bound port (differs from the requested one when port=0)."""
        return self.server_address[1]

    def peer_allowed(self, ip_text):
        """Return True when the peer IP may connect under the active mode.

        Remote mode: any peer when ``allowed_ips`` is empty, otherwise only
        listed addresses/subnets.
        """
        try:
            addr = ipaddress.ip_address(ip_text)
        except ValueError:
            return False
        if not self.remote_enabled:
            return addr.is_loopback
        if not self.allowed_networks:
            return True
        return any(addr in network for network in self.allowed_networks)


    def start(self):
        """Serve in a daemon thread; returns the thread."""
        if self._serving:
            raise RuntimeError("server is already serving")
        self._serving = True
        thread = threading.Thread(
            target=self.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="mcp-http-server",
            daemon=True,
        )
        thread.start()
        return thread

    def stop(self):
        """Drain active streams, stop the accept loop and release the socket.

        Streams are drained gracefully: events already queued on a stream —
        including a shutdown-enqueued final result and its ``None`` sentinel
        — are delivered and the response still terminates with a zero chunk;
        sources with nothing queued end within one keepalive interval and
        run ``on_disconnect``. Enqueue final results before calling
        ``stop()``. Never joins request threads (they are daemons), so a
        stuck GUI call cannot block shutdown. Idempotent.
        """
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        with self._streams_lock:
            streams = tuple(self._active_streams)
        for stream in streams:
            stream._graceful_close()
        if self._serving:
            self.shutdown()
        self.server_close()


class _McpRequestHandler(http.server.BaseHTTPRequestHandler):
    """Single-connection handler; the request thread owns any SSE stream."""

    protocol_version = "HTTP/1.1"
    server_version = "freecad-mcp-addon/2.0.0"
    sys_version = ""

    def setup(self):
        self.timeout = self.server.read_timeout
        super().setup()
        self.connection_id = uuid.uuid4().hex
        self._response_started = False

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # keep the FreeCAD console free of per-request noise

    # ----------------------------------------------------------- dispatching

    def __getattr__(self, name):
        # Every verb — known or custom — resolves to the authenticated
        # pipeline, so the stdlib fallback never answers with an
        # unauthenticated 501.
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    def _handle(self):
        try:
            self.connection.settimeout(self.server.read_timeout)
            self._response_started = False
            self._process()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            traceback.print_exc(file=sys.stderr)
            if not self._response_started:
                try:
                    self._send_rpc_error(
                        500, -32603, "Unexpected server error.", close=True
                    )
                except OSError:
                    pass

    # -------------------------------------------------------------- plumbing

    def _send_json(self, status, payload, *, close=False, extra_headers=()):
        body = (
            b""
            if payload is None
            else json.dumps(payload, allow_nan=False).encode("utf-8")
        )
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        # HEAD responses carry the would-be body's headers but no payload.
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _send_rpc_error(self, status, code, message, *, request_id=None, close=False):
        error = {"code": code, "message": message}
        self._send_json(status, error_response(error, request_id), close=close)

    def _send_protocol_error(self, exc, request_id):
        status = _HTTP_STATUS_BY_CODE.get(exc.code, 400)
        error = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        self._send_json(status, error_response(error, request_id))

    def _reject_unauthorized(self):
        self._send_json(
            401,
            {"error": "unauthorized"},
            extra_headers=(("WWW-Authenticate", 'Bearer realm="freecad-mcp"'),),
        )

    def _reject_forbidden(self, detail):
        self._send_json(403, {"error": "forbidden", "detail": detail})

    def _lowered_headers(self):
        return {name.lower(): value for name, value in self.headers.items()}

    # -------------------------------------------------------------- pipeline

    def _process(self):
        structural_error = self._header_structure_error()
        if structural_error is not None:
            # Framing cannot be trusted; reject without draining the body.
            code, message = structural_error
            return self._send_rpc_error(400, code, message, close=True)

        if self.command == "POST":
            length = self._content_length()
            if length is None:
                return self._send_rpc_error(
                    400,
                    -32600,
                    "A valid Content-Length header is required.",
                    close=True,
                )
            if length > self.server.max_body_bytes:
                return self._send_rpc_error(
                    413, -32600, "Request body exceeds the allowed size.", close=True
                )
            try:
                body = self.rfile.read(length) if length else b""
            except (OSError, TimeoutError):
                self.close_connection = True
                return
            # The request thread now owns the connection for the whole exchange.
            self.connection.settimeout(None)
        else:
            if self._content_length() not in (None, 0):
                return self._send_rpc_error(
                    400, -32600, "Unexpected request body.", close=True
                )
            body = b""

        if not self.server.peer_allowed(self.client_address[0]):
            return self._reject_forbidden("peer address is not allowed")
        if not self.server.remote_enabled and not self._host_allowed():
            return self._reject_forbidden("Host header is not the loopback endpoint")
        if not self.server.remote_enabled and not self._origin_allowed():
            return self._reject_forbidden("Origin header is not allowed")
        if not self._authorized():
            return self._reject_unauthorized()
        if self.command != "POST" or self.path.split("?", 1)[0] != MCP_ENDPOINT:
            return self._send_rpc_error(404, -32601, "Method not found.")
        if not self._content_type_is_json():
            return self._send_json(415, {"error": "unsupported media type"})
        if not self._accept_isacceptable():
            return self._send_json(406, {"error": "unacceptable response types"})

        try:
            message = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError):
            return self._send_rpc_error(
                400, -32700, "Parse error: body is not valid JSON."
            )
        if not isinstance(message, dict):
            return self._send_rpc_error(
                400, -32600, "Malformed envelope: expected a single JSON object."
            )

        request_id = self._extract_request_id(message)
        is_notification = "id" not in message
        try:
            validated = validate_request(message, self._lowered_headers())
        except ProtocolError as exc:
            return self._send_protocol_error(exc, request_id)

        if is_notification:
            # Valid notifications are accepted, never dispatched.
            return self._send_json(202, None)

        try:
            outcome = self.server.dispatch(
                validated, self.server.principal, self.connection_id
            )
        except ProtocolError as exc:
            return self._send_protocol_error(exc, request_id)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self._send_rpc_error(500, -32603, "Unexpected server error.")

        if isinstance(outcome, StreamResponse):
            return self._send_stream(outcome)
        if not isinstance(outcome, dict):
            return self._send_rpc_error(500, -32603, "Unexpected server error.")
        self._send_json(200, outcome)

    # ---------------------------------------------------------------- checks

    def _header_structure_error(self):
        """Return ``(code, message)`` for duplicate/illegal/TE-framed headers else None."""
        for name in _SINGLE_VALUE_HEADERS:
            values = self.headers.get_all(name)
            if values is not None and len(values) > 1:
                return -32600, f"Duplicate {name} header."
        if self.command == "POST" and self.headers.get("Transfer-Encoding") is not None:
            # A TE-framed body would disagree with Content-Length framing;
            # reject before any body byte is read (PLAN section 3).
            return -32600, "Transfer-Encoding request bodies are not accepted."
        seen_params = {}
        for raw_name, value in self.headers.items():
            match = _PARAM_HEADER_RE.match(raw_name.lower())
            if match is None:
                continue
            param_name = match.group(1).lower()
            seen_params[param_name] = seen_params.get(param_name, 0) + 1
            try:
                _check_header_value_legality(value)
            except ValueError as exc:
                return -32020, f"Invalid Mcp-Param header value: {exc}"
        for param_name, count in seen_params.items():
            if count > 1:
                return -32020, f"Duplicate Mcp-Param-{param_name} header."
        return None

    def _content_length(self):
        """Return the declared body length, or None when missing/malformed."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        raw = raw.strip()
        if not raw or not _DIGITS_RE.fullmatch(raw):
            return None
        return int(raw)

    def _host_allowed(self):
        host = self.headers.get("Host")
        if not host:
            return False
        port = self.server.port
        allowed = {f"{name}:{port}" for name in _LOOPBACK_HOSTS}
        return host.strip().lower() in allowed

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # absence is allowed; non-browser clients send none
        port = self.server.port
        allowed = {f"http://{name}:{port}" for name in _LOOPBACK_HOSTS}
        return origin.strip().lower() in allowed

    def _authorized(self):
        token = self.server.token
        if not token:
            return True  # tokenless local mode; loopback gates apply instead
        auth = self.headers.get("Authorization")
        if not isinstance(auth, str):
            return False
        scheme, _, credentials = auth.partition(" ")
        if scheme.lower() != "bearer" or not credentials:
            return False
        return hmac.compare_digest(
            credentials.strip().encode("utf-8"),
            token.encode("utf-8"),
        )

    def _content_type_is_json(self):
        content_type = self.headers.get("Content-Type", "")
        mediatype = content_type.split(";", 1)[0].strip().lower()
        return mediatype == "application/json"

    def _accept_isacceptable(self):
        """Require JSON and SSE ranges explicitly offered with q > 0.

        Media ranges carry RFC 9110 quality values: ``q=0`` (or a malformed
        quality) explicitly disables a type, so negotiation fails closed.
        """
        accept = self.headers.get("Accept", "")
        quality = {}
        for entry in accept.split(","):
            parts = entry.split(";")
            media = parts[0].strip().lower()
            value = 1.0
            for param in parts[1:]:
                name, _, raw = param.partition("=")
                if name.strip().lower() != "q":
                    continue
                try:
                    value = float(raw.strip())
                except ValueError:
                    value = 0.0
                if not 0.0 <= value <= 1.0:
                    value = 0.0
            quality[media] = value
        return (
            quality.get("application/json", 0.0) > 0.0
            and quality.get("text/event-stream", 0.0) > 0.0
        )

    @staticmethod
    def _extract_request_id(message):
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return None
        return request_id

    # ------------------------------------------------------------------ SSE

    def _send_stream(self, stream):
        server = self.server
        with server._streams_lock:
            server._active_streams.add(stream)
        completed = False
        try:
            self._response_started = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(None)

            interval = (
                stream.keepalive_interval
                if stream.keepalive_interval is not None
                else server.keepalive_interval
            )
            while True:
                try:
                    event = stream.next_event(timeout=interval)
                except _StreamClosed:
                    break
                if event is None:
                    completed = True
                    break
                if event is _KEEPALIVE:
                    payload = b": keepalive\n\n"
                else:
                    data = json.dumps(
                        event, allow_nan=False, separators=(",", ":")
                    ).encode("utf-8")
                    payload = b"data: " + data + b"\n\n"
                self._write_chunk(payload)
            if completed:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            completed = False
        except Exception:
            completed = False
            traceback.print_exc(file=sys.stderr)
        finally:
            with server._streams_lock:
                server._active_streams.discard(stream)
            stream._finalize(disconnected=not completed)

    def _write_chunk(self, payload):
        self.wfile.write(f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n")
        self.wfile.flush()
