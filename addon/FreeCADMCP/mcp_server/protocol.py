"""Wire contract for the embedded MCP v2 server.

This module owns the exact released ``2026-07-28`` protocol surface used by
the FreeCAD add-on: envelope/metadata/header validation, result and error
builders, finite JSON Schema checking, and the consent signing machinery.

Authoritative sources (do not infer fields from an older SDK):

- Core schema ``schema/2026-07-28/schema.ts`` in ``modelcontextprotocol/specification``.
- MRTR: https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr
- Streamable HTTP: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http

Hard rules implemented here:

1. Validation precedence is envelope (-32600), then request metadata
   (-32602), then routing headers (-32020), then version negotiation
   (-32022). ``clientInfo`` is required even though the released schema
   marks it optional.
2. Unknown extension metadata is accepted, never rejected.
3. Schemas are finite: only constructs this server actually emits are
   supported; unsupported keywords are rejected at registration time and
   NaN/infinity never pass validation.
4. The consent signer holds a fresh in-memory HMAC-SHA256 key per server
   start and provides domain-separated signing plus principal/expiry/
   arguments/target-bound challenges with locked nonce consumption.

This module imports the Python standard library only and MUST stay
GUI independent: no FreeCAD imports here or anywhere in ``mcp_server``
until explicit startup.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

# ---------------------------------------------------------------------------
# Released protocol constants.
# ---------------------------------------------------------------------------

SUPPORTED_PROTOCOL_VERSION = "2026-07-28"
JSONRPC_VERSION = "2.0"

SERVER_INFO = {"name": "freecad-mcp-addon", "version": "2.0.0"}

# Streamable HTTP request-metadata headers (case-insensitive names,
# case-sensitive values).
PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
METHOD_HEADER = "Mcp-Method"
NAME_HEADER = "Mcp-Name"
PARAM_HEADER_PREFIX = "Mcp-Param-"

# Base64 sentinel encoding for header values that are not plain header-safe
# ASCII. The markers are case sensitive.
SENTINEL_PREFIX = "=?base64?"
SENTINEL_SUFFIX = "?="

# Reserved ``_meta`` keys.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP-reserved protocol error codes (the -32020..-32099 sub-range).
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

# Application tool error codes. These are never JSON-RPC error codes: tools
# report them as complete tool results with isError:true.
DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
OBJECT_NOT_FOUND = "OBJECT_NOT_FOUND"
VALIDATION_FAILED = "VALIDATION_FAILED"
GUI_DISPATCH_FAILED = "GUI_DISPATCH_FAILED"
CONSENT_DENIED = "CONSENT_DENIED"
PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
UNSUPPORTED_VIEW = "UNSUPPORTED_VIEW"
SOLVER_FAILED = "SOLVER_FAILED"

# Signing domains. The HMAC key is shared, every signature is bound to one
# domain and can never be replayed under another.
DOMAIN_CONSENT = "consent"
DOMAIN_CURSOR = "cursor"
DOMAIN_TOPOLOGY = "topology"

# Methods whose Mcp-Name header mirrors this ``params`` field. Task methods
# mirror the task ID per the implementation plan.
_NAME_SOURCES = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
    "tasks/get": "taskId",
    "tasks/update": "taskId",
    "tasks/cancel": "taskId",
}


# ---------------------------------------------------------------------------
# Error types.
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    """A JSON-RPC protocol-level failure carrying a wire error code."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class ToolError(Exception):
    """A tool execution failure.

    The server converts this into a complete tool result with
    ``isError:true`` and a structured ``{error: {code, message, details}}``
    payload; it is never a JSON-RPC error response.
    """

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class InputRequired(Exception):
    """Raised by tools that need an MRTR round trip before execution.

    The server converts this into an ``input_required`` result. The client
    retries the original request with ``params.requestState`` and
    ``params.inputResponses``; tool arguments are never used for retries.
    """

    def __init__(
        self,
        request_state: str,
        input_requests: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("additional input required before the request can complete")
        self.request_state = request_state
        self.input_requests = dict(input_requests) if input_requests else None


class TokenError(ValueError):
    """Internal: a signed token failed verification with a machine reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Canonical JSON helpers.
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Serialize ``value`` deterministically; NaN/infinity are errors."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def fingerprint(value: Any) -> str:
    """Stable SHA-256 fingerprint of a JSON-serializable value."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Result and error builders.
# ---------------------------------------------------------------------------


def _finish_result(result_type: str, payload: Mapping[str, Any]) -> dict:
    result = {"resultType": result_type}
    result.update(payload)
    meta = dict(result.get("_meta") or {})
    meta.setdefault(META_SERVER_INFO, dict(SERVER_INFO))
    result["_meta"] = meta
    return result


def complete_result(payload: Mapping[str, Any]) -> dict:
    """Build a ``resultType:"complete"`` result for a successful request."""

    return _finish_result("complete", payload)


def tool_result(payload: Any, *, is_error: bool = False) -> dict:
    """Build a complete ``tools/call`` result.

    ``payload`` is either the tool's structured payload mapping (returned as
    ``structuredContent`` plus its JSON text representation as content) or a
    list of content blocks (e.g. the PNG image content of ``capture_view``).
    """

    result: dict = {"resultType": "complete"}
    if isinstance(payload, Mapping):
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        result["content"] = [{"type": "text", "text": text}]
        result["structuredContent"] = dict(payload)
    elif isinstance(payload, list):
        result["content"] = list(payload)
    elif payload is None:
        result["content"] = []
    else:
        raise TypeError("tool payload must be a mapping, a content list, or None")
    if is_error:
        result["isError"] = True
    return _finish_result("complete", result)


def tool_error_result(error: ToolError) -> dict:
    """Convert a :class:`ToolError` into its complete isError tool result."""

    structured: dict = {"code": error.code, "message": error.message}
    if error.details is not None:
        structured["details"] = error.details
    return tool_result(
        [{"type": "text", "text": error.message}],
        is_error=True,
    ) | {
        "structuredContent": {"error": structured},
    }


def input_required_result(
    input_requests: Mapping[str, Any] | None,
    request_state: str | None,
) -> dict:
    """Build an ``input_required`` result per the released MRTR schema.

    At least one of ``inputRequests`` / ``requestState`` must be present.
    """

    if input_requests is None and request_state is None:
        raise ValueError("input_required_result requires inputRequests or requestState")
    payload: dict = {}
    if input_requests is not None:
        payload["inputRequests"] = dict(input_requests)
    if request_state is not None:
        payload["requestState"] = request_state
    return _finish_result("input_required", payload)


def _error_object(error: Any) -> dict:
    if isinstance(error, ProtocolError):
        err: dict = {"code": error.code, "message": error.message}
        if error.data is not None:
            err["data"] = error.data
        return err
    if isinstance(error, Mapping):
        if not isinstance(error.get("code"), int) or not isinstance(error.get("message"), str):
            raise TypeError("error mapping requires integer code and string message")
        err = {"code": error["code"], "message": error["message"]}
        if "data" in error and error["data"] is not None:
            err["data"] = error["data"]
        return err
    raise TypeError("error must be a ProtocolError or a {code, message} mapping")


def error_response(error: Any, request_id: Any = None) -> dict:
    """Build a JSON-RPC error response.

    A known ``request_id`` is echoed verbatim. ``None`` (parse errors,
    notifications, unknown ids) omits the ``id`` field per the released
    JSONRPCErrorResponse contract instead of fabricating a null id.
    """

    response = {"jsonrpc": JSONRPC_VERSION, "error": _error_object(error)}
    if request_id is not None:
        response["id"] = request_id
    return response


def parse_error_response() -> dict:
    """Response for a body that is not valid JSON (-32700); id is omitted."""

    return error_response(ProtocolError(PARSE_ERROR, "invalid JSON"))


# ---------------------------------------------------------------------------
# Header value encoding (released Base64 sentinel format).
# ---------------------------------------------------------------------------


def _is_header_safe(value: str) -> bool:
    """RFC 9110 field value: visible ASCII, space, horizontal tab."""

    return all(c == "\t" or " " <= c <= "~" for c in value)


def _decode_sentinel(value: str) -> str:
    """Decode ``=?base64?...?=`` strictly; raise ValueError when malformed."""

    inner = value[len(SENTINEL_PREFIX) : -len(SENTINEL_SUFFIX)]
    if not inner or any(c not in _B64_CHARS for c in inner):
        raise ValueError("invalid base64 payload in encoded header value")
    try:
        return base64.b64decode(inner, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid base64 payload in encoded header value") from exc


def header_source_value(raw: Any) -> str:
    """Validate one raw header value and return its effective source value.

    Sentinel-encoded values are decoded; plain values are returned as-is.
    Raises ``ValueError`` for characters or encodings that are not allowed.
    """

    if not isinstance(raw, str):
        raise ValueError("header value must be a string")
    if raw.startswith(SENTINEL_PREFIX) and raw.endswith(SENTINEL_SUFFIX):
        if len(raw) < len(SENTINEL_PREFIX) + len(SENTINEL_SUFFIX) + 1:
            raise ValueError("empty base64 payload in encoded header value")
        return _decode_sentinel(raw)
    if not _is_header_safe(raw):
        raise ValueError("header value contains invalid characters")
    return raw


def header_matches_body(raw: Any, body_value: Any) -> bool:
    """Compare a raw header value against its mirrored body source value.

    Integer/number sources compare numerically (``42`` equals ``42.0``),
    booleans compare as lowercase ``true``/``false``, strings exactly.
    """

    try:
        effective = header_source_value(raw)
    except ValueError:
        return False
    if isinstance(body_value, bool):
        return effective == ("true" if body_value else "false")
    if isinstance(body_value, (int, float)):
        try:
            return math.isfinite(body_value) and float(effective) == body_value
        except ValueError:
            return False
    if isinstance(body_value, str):
        return effective == body_value
    return False


# ---------------------------------------------------------------------------
# Request validation.
# ---------------------------------------------------------------------------


def _envelope_error(code: int, message: str) -> ProtocolError:
    return ProtocolError(code, message)


def validate_request(
    message: Any,
    headers: Mapping[str, Any] | None,
    *,
    param_paths: Mapping[str, Sequence[str]] | None = None,
) -> dict:
    """Validate one JSON-RPC message plus its routing headers.

    Returns a validated view of the message::

        {
          "id": <string|int|None>,          # None for notifications
          "is_notification": bool,
          "method": str,
          "params": dict,                   # {} when absent
          "_meta": dict,
          "protocol_version": "2026-07-28",
          "client_info": {"name": str, "version": str},
          "client_capabilities": dict,
        }

    Precedence: envelope (-32600) -> metadata (-32602) -> headers (-32020)
    -> version (-32022). ``param_paths`` optionally maps an ``Mcp-Param-*``
    suffix to the ``params`` property path that mirrors it (e.g.
    ``{"Region": ("arguments", "region")}``); when provided, mirrored values
    are matched against the body in both directions.
    """

    # 1. Envelope.
    if not isinstance(message, dict):
        raise _envelope_error(
            INVALID_REQUEST, "malformed envelope: expected a single JSON-RPC message"
        )
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise _envelope_error(INVALID_REQUEST, 'malformed envelope: jsonrpc must be "2.0"')
    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise _envelope_error(
            INVALID_REQUEST, "malformed envelope: method must be a non-empty string"
        )
    request_id = message.get("id")
    is_notification = "id" not in message
    if not is_notification and (
        isinstance(request_id, bool) or not isinstance(request_id, (str, int))
    ):
        raise _envelope_error(INVALID_REQUEST, "malformed envelope: id must be a string or integer")
    params = message.get("params", {})
    if not isinstance(params, dict):
        raise _envelope_error(INVALID_REQUEST, "malformed envelope: params must be an object")

    # 2. Required request metadata. clientInfo is required by the plan even
    # though the released schema marks it optional.
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise ProtocolError(
            INVALID_PARAMS,
            "invalid parameters: params._meta is required",
        )
    version = meta.get(META_PROTOCOL_VERSION)
    if not isinstance(version, str) or not version:
        raise ProtocolError(
            INVALID_PARAMS,
            f"invalid parameters: {META_PROTOCOL_VERSION} must be a string",
        )
    client_info = meta.get(META_CLIENT_INFO)
    if not isinstance(client_info, dict):
        raise ProtocolError(
            INVALID_PARAMS,
            f"invalid parameters: {META_CLIENT_INFO} is required",
        )
    if (
        not isinstance(client_info.get("name"), str)
        or not client_info["name"]
        or not isinstance(client_info.get("version"), str)
        or not client_info["version"]
    ):
        raise ProtocolError(
            INVALID_PARAMS,
            f"invalid parameters: {META_CLIENT_INFO} requires string name and version",
        )
    capabilities = meta.get(META_CLIENT_CAPABILITIES)
    if not isinstance(capabilities, dict):
        raise ProtocolError(
            INVALID_PARAMS,
            f"invalid parameters: {META_CLIENT_CAPABILITIES} must be an object",
        )

    # 3. Routing headers. Header names compare case-insensitively, values
    # are case sensitive and sentinel-encoded values are decoded first.
    raw_headers: dict = {}
    if headers:
        for key, value in headers.items():
            raw_headers[str(key).lower()] = value

    def raw_header(name: str) -> Any:
        return raw_headers.get(name.lower())

    version_header = raw_header(PROTOCOL_VERSION_HEADER)
    if version_header is None:
        raise ProtocolError(
            HEADER_MISMATCH, f"header mismatch: {PROTOCOL_VERSION_HEADER} is required"
        )
    try:
        if header_source_value(version_header) != version:
            raise ValueError("value does not match request metadata")
    except ValueError as exc:
        raise ProtocolError(
            HEADER_MISMATCH,
            f"header mismatch: {PROTOCOL_VERSION_HEADER}: {exc}",
        ) from exc

    method_header = raw_header(METHOD_HEADER)
    if method_header is None:
        raise ProtocolError(HEADER_MISMATCH, f"header mismatch: {METHOD_HEADER} is required")
    try:
        if header_source_value(method_header) != method:
            raise ValueError("value does not match request method")
    except ValueError as exc:
        raise ProtocolError(HEADER_MISMATCH, f"header mismatch: {METHOD_HEADER}: {exc}") from exc

    name_source = _NAME_SOURCES.get(method)
    if name_source is not None:
        name_header = raw_header(NAME_HEADER)
        if name_header is None:
            raise ProtocolError(
                HEADER_MISMATCH,
                f"header mismatch: {NAME_HEADER} is required for {method}",
            )
        if name_source not in params:
            raise ProtocolError(
                HEADER_MISMATCH,
                f"header mismatch: params.{name_source} is missing for {method}",
            )
        if not header_matches_body(name_header, params[name_source]):
            raise ProtocolError(
                HEADER_MISMATCH,
                f"header mismatch: {NAME_HEADER} does not match params.{name_source}",
            )
    elif raw_header(NAME_HEADER) is not None:
        try:
            header_source_value(raw_header(NAME_HEADER))
        except ValueError as exc:
            raise ProtocolError(HEADER_MISMATCH, f"header mismatch: {NAME_HEADER}: {exc}") from exc

    _validate_param_headers(params, raw_headers, param_paths)

    # 4. Version negotiation last; discovery has no exemption.
    if version != SUPPORTED_PROTOCOL_VERSION:
        raise ProtocolError(
            UNSUPPORTED_PROTOCOL_VERSION,
            f"unsupported protocol version: {version}",
            {"supported": [SUPPORTED_PROTOCOL_VERSION], "requested": version},
        )

    return {
        "id": None if is_notification else request_id,
        "is_notification": is_notification,
        "method": method,
        "params": params,
        "_meta": meta,
        "protocol_version": version,
        "client_info": client_info,
        "client_capabilities": capabilities,
    }


def _validate_param_headers(
    params: Mapping[str, Any],
    raw_headers: Mapping[str, Any],
    param_paths: Mapping[str, Sequence[str]] | None,
) -> None:
    """Validate ``Mcp-Param-*`` headers against their body sources.

    Encoding validity is always enforced. When ``param_paths`` declares a
    mirror for a header suffix, presence and value are checked in both
    directions; unknown suffixes are only checked for valid encoding.
    """

    annotated: dict = {}
    for suffix, path in (param_paths or {}).items():
        annotated[suffix.lower()] = tuple(path)

    for raw_name, raw_value in raw_headers.items():
        if not raw_name.startswith("mcp-param-"):
            continue
        suffix = raw_name[len("mcp-param-") :]
        try:
            header_source_value(raw_value)
        except ValueError as exc:
            raise ProtocolError(
                HEADER_MISMATCH, f"header mismatch: Mcp-Param-{suffix}: {exc}"
            ) from exc

    for suffix, path in annotated.items():
        raw_value = raw_headers.get(f"mcp-param-{suffix}")
        body_value: Any = params
        for key in path:
            if not isinstance(body_value, Mapping) or key not in body_value:
                body_value = None
                break
            body_value = body_value[key]
        if raw_value is None:
            if body_value is not None:
                raise ProtocolError(
                    HEADER_MISMATCH,
                    f"header mismatch: Mcp-Param-{suffix} is required when "
                    "the mirrored argument is present",
                )
            continue
        if body_value is None:
            raise ProtocolError(
                HEADER_MISMATCH,
                f"header mismatch: Mcp-Param-{suffix} has no mirrored body value",
            )
        if not header_matches_body(raw_value, body_value):
            raise ProtocolError(
                HEADER_MISMATCH,
                f"header mismatch: Mcp-Param-{suffix} does not match the request body",
            )


# ---------------------------------------------------------------------------
# Client capability checks.
# ---------------------------------------------------------------------------


def missing_capability(required: Mapping[str, Any]) -> ProtocolError:
    """The -32021 error for a missing declared client capability."""

    return ProtocolError(
        MISSING_REQUIRED_CLIENT_CAPABILITY,
        "missing required client capability",
        {"requiredCapabilities": dict(required)},
    )


def require_client_capabilities(
    capabilities: Mapping[str, Any], required: Mapping[str, Any]
) -> None:
    """Ensure the declared capabilities contain every required entry.

    ``required`` mirrors the shape of ``clientCapabilities``, e.g.
    ``{"extensions": {"io.modelcontextprotocol/tasks": {}}}`` or
    ``{"elicitation": {"form": {}}}``. Nested mappings recurse; scalar or
    empty values only require key presence.
    """

    def _walk(declared: Any, wanted: Mapping[str, Any], path: str) -> None:
        if not isinstance(declared, Mapping):
            raise missing_capability(required)
        for key, sub in wanted.items():
            if key not in declared:
                raise missing_capability(required)
            if isinstance(sub, Mapping) and sub:
                _walk(declared[key], sub, f"{path}.{key}" if path else str(key))

    _walk(capabilities, required, "")


# ---------------------------------------------------------------------------
# Finite JSON Schema checking and validation.
# ---------------------------------------------------------------------------

_SUPPORTED_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")

_SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "anyOf",
        "type",
        "title",
        "description",
        "default",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "properties",
        "required",
        "additionalProperties",
        "items",
    }
)

_B64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")

_URLSAFE_B64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")


def _finite_number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{what} must be finite")
    return float(value)


def _nonnegative_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{what} must be a non-negative integer")
    return value


def _check_ref(schema: Mapping[str, Any], root: Mapping[str, Any], path: str) -> str:
    ref = schema["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        raise ValueError(f"{path}: only local $ref of the form #/$defs/<name> is supported")
    name = ref[len("#/$defs/") :]
    defs = root.get("$defs")
    if not isinstance(defs, dict) or name not in defs:
        raise ValueError(f"{path}: $ref {ref!r} does not resolve in $defs")
    return name


def check_schema(
    schema: Any,
    root: Mapping[str, Any] | None = None,
    path: str = "$",
    _refs: frozenset[str] | None = None,
) -> None:
    """Reject any schema construct this server does not emit.

    Supported: finite types (a single name or a non-empty array), enum/const,
    numeric and string/array bounds, properties/required/
    additionalProperties(false or schema), items(schema), bounded ``anyOf``
    composition and local ``$defs``/``$ref``. oneOf/allOf/not, conditionals,
    patterns, remote refs and recursion are unsupported and raise
    ``ValueError`` so registration fails loudly instead of silently skipping
    validation.
    """

    if root is None:
        root = schema
    if not isinstance(schema, Mapping):
        raise ValueError(f"{path}: schema must be an object")
    for key in schema:
        if key not in _SUPPORTED_SCHEMA_KEYWORDS:
            raise ValueError(f"{path}: unsupported schema keyword {key!r}")

    if "type" in schema:
        declared = schema["type"]
        names = [declared] if isinstance(declared, str) else declared
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(entry, str) and entry in _SUPPORTED_TYPES for entry in names)
        ):
            raise ValueError(f"{path}: unsupported type {declared!r}")

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            raise ValueError(f"{path}: enum must be a non-empty array")
    if (
        "const" in schema
        and isinstance(schema["const"], float)
        and not math.isfinite(schema["const"])
    ):
        raise ValueError(f"{path}: const must be finite")

    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema:
            _finite_number(schema[key], f"{path}.{key}")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            _nonnegative_int(schema[key], f"{path}.{key}")

    refs = _refs if _refs is not None else frozenset()
    if "$ref" in schema:
        name = _check_ref(schema, root, path)
        if name in refs:
            raise ValueError(f"{path}: recursive $ref {schema['$ref']!r} is unsupported")
        check_schema(root["$defs"][name], root, f"{path}->$defs.{name}", refs | {name})

    if "anyOf" in schema:
        branches = schema["anyOf"]
        if not isinstance(branches, list) or not branches:
            raise ValueError(f"{path}: anyOf must be a non-empty array of schemas")
        for index, branch in enumerate(branches):
            check_schema(branch, root, f"{path}.anyOf[{index}]", refs)

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, Mapping):
            raise ValueError(f"{path}: properties must be an object")
        for name, sub in properties.items():
            check_schema(sub, root, f"{path}.properties.{name}", refs)
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
            raise ValueError(f"{path}: required must be an array of strings")
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if extra is False:
            pass
        elif isinstance(extra, Mapping):
            check_schema(extra, root, f"{path}.additionalProperties", refs)
        else:
            raise ValueError(f"{path}: additionalProperties must be false or a schema object")
    if "items" in schema:
        items = schema["items"]
        if not isinstance(items, Mapping):
            raise ValueError(f"{path}: items must be a schema object")
        check_schema(items, root, f"{path}.items", refs)

    if "$defs" in schema:
        if root is not schema:
            raise ValueError(f"{path}: $defs is only supported at the schema root")
        defs = schema["$defs"]
        if not isinstance(defs, Mapping):
            raise ValueError(f"{path}: $defs must be an object")
        for name, sub in defs.items():
            check_schema(sub, schema, f"{path}->$defs.{name}", refs | {name})


def _same_json(value: Any, candidate: Any) -> bool:
    if value is candidate:
        return True
    return type(value) is type(candidate) and value == candidate


def _resolve_ref(
    schema: Mapping[str, Any], root: Mapping[str, Any], depth: int
) -> Mapping[str, Any]:
    if depth > 32:  # check_schema rejects recursion; this is defense in depth.
        raise ProtocolError(INTERNAL_ERROR, "schema $ref depth exceeded")
    while isinstance(schema, Mapping) and "$ref" in schema:
        ref = schema["$ref"]
        name = ref[len("#/$defs/") :] if isinstance(ref, str) else None
        defs = root.get("$defs") if isinstance(root, Mapping) else None
        if not name or not isinstance(defs, Mapping) or name not in defs:
            raise ProtocolError(INTERNAL_ERROR, f"unresolvable schema $ref {ref!r}")
        schema = defs[name]
    return schema


def _ensure_finite(value: Any, path: str) -> None:
    """Reject non-finite numbers anywhere inside ``value``.

    Runs once per top-level validation before schema dispatch so NaN or
    infinities cannot hide under permissive schemas (``{}``, unconstrained
    objects/arrays) that never recurse into their children.
    """

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(INVALID_PARAMS, f"invalid parameters: {path} must be finite")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_finite(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _ensure_finite(item, f"{path}.{key}")


def validate_schema(
    value: Any,
    schema: Any,
    root: Mapping[str, Any] | None = None,
    path: str = "$",
    _depth: int = 0,
) -> None:
    """Validate ``value`` against a checked schema.

    Raises ``ProtocolError`` with code -32602 on the first mismatch.
    NaN/infinity are rejected anywhere in the payload, even under permissive
    schemas; booleans are not integers; ``anyOf`` branches and ``type``
    arrays are honored.
    """

    if root is None:
        root = schema
    if _depth == 0:
        _ensure_finite(value, path)
    schema = _resolve_ref(schema, root, _depth)
    if not isinstance(schema, Mapping):
        raise ProtocolError(INTERNAL_ERROR, "registered schema must be an object")

    def fail(message: str) -> None:
        raise ProtocolError(INVALID_PARAMS, f"invalid parameters: {path} {message}")

    if "const" in schema and not _same_json(value, schema["const"]):
        fail(f"must equal {schema['const']!r}")
    if "enum" in schema and not any(_same_json(value, candidate) for candidate in schema["enum"]):
        fail(f"must be one of {schema['enum']!r}")

    expected = schema.get("type")
    if expected is not None:
        if isinstance(expected, str):
            _validate_type(value, expected, fail)
        elif isinstance(expected, list):  # members are pre-checked strings.
            if not any(_matches_type(value, entry) for entry in expected):
                fail(f"does not match any declared type {expected!r}")
        else:  # check_schema rejects other shapes; defense in depth.
            fail(f"has unsupported type {expected!r}")

    branches = schema.get("anyOf")
    if branches is not None:
        if not isinstance(branches, list):  # check_schema rejects first.
            fail("anyOf must be an array")
        for branch in branches:
            try:
                validate_schema(value, branch, root, path, _depth + 1)
                break
            except ProtocolError:
                continue
        else:
            fail("does not match any anyOf branch")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            fail(f"must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            fail(f"must be <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            fail(f"must be > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            fail(f"must be < {schema['exclusiveMaximum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            fail(f"length must be >= {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            fail(f"length must be <= {schema['maxLength']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            fail(f"must have at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            fail(f"must have at most {schema['maxItems']} items")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                validate_schema(item, items, root, f"{path}[{index}]", _depth + 1)

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        if not isinstance(properties, Mapping):  # check_schema rejects first.
            properties = {}
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                fail(f"missing required property {key!r}")
        extra_schema = schema.get("additionalProperties")
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], root, f"{path}.{key}", _depth + 1)
            elif isinstance(extra_schema, Mapping):
                validate_schema(item, extra_schema, root, f"{path}.{key}", _depth + 1)
            elif extra_schema is False:
                fail(f"unexpected property {key!r}")
            # additionalProperties absent: the JSON Schema default allows
            # extras; every emitted schema states it explicitly.


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return isinstance(value, float) and math.isfinite(value) and value.is_integer()
        return True
    if expected == "number":
        return not isinstance(value, bool) and isinstance(value, (int, float))
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False  # check_schema rejects unknown types; defense in depth.


def _validate_type(value: Any, expected: str, fail) -> None:
    if expected not in _SUPPORTED_TYPES:
        fail(f"has unsupported type {expected!r}")
    if not _matches_type(value, expected):
        if expected == "null":
            fail("must be null")
        article = "an" if expected in ("object", "array", "integer") else "a"
        fail(f"must be {article} {expected}")


# ---------------------------------------------------------------------------
# Consent signing (MRTR requestState).
# ---------------------------------------------------------------------------

CONSENT_INPUT_KEY = "confirm"
DEFAULT_CONSENT_TTL_S = 300.0


def consent_input_request(message: str) -> dict:
    """The fixed consent elicitation form: key ``confirm``, boolean confirmed."""

    return {
        CONSENT_INPUT_KEY: {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": message,
                "requestedSchema": {
                    "type": "object",
                    "properties": {
                        "confirmed": {
                            "type": "boolean",
                            "title": "Confirm",
                            "description": "Confirm to run this operation.",
                        }
                    },
                    "required": ["confirmed"],
                },
            },
        }
    }


class ConsentSigner:
    """HMAC-SHA256 signer with a fresh in-memory key per server start.

    ``sign``/``verify`` are reusable and domain separated: a token signed
    for one domain (consent, cursor, topology) can never be verified under
    another. Consent challenges bind the authenticated principal, a short
    expiry, a random nonce, the method plus an arguments digest and the
    target fingerprint; consumption is locked and single-use.
    """

    def __init__(self, *, ttl_s: float = DEFAULT_CONSENT_TTL_S) -> None:
        self._key = secrets.token_bytes(32)
        self._ttl_s = float(ttl_s)
        self._lock = threading.Lock()
        self._consumed_nonces: dict[str, float] = {}

    # -- reusable domain-separated signing --------------------------------

    def sign(self, domain: str, payload: Mapping[str, Any]) -> str:
        body = {
            "domain": domain,
            "payload": json.loads(canonical_json(payload)),
        }
        body_bytes = canonical_json(body).encode("utf-8")
        mac = hmac.new(
            self._key, domain.encode("utf-8") + b"." + body_bytes, hashlib.sha256
        ).digest()
        return (
            base64.urlsafe_b64encode(body_bytes).decode("ascii")
            + "."
            + base64.urlsafe_b64encode(mac).decode("ascii")
        )

    def verify(self, domain: str, token: str) -> dict:
        """Return the signed payload or raise ProtocolError(-32602)."""

        try:
            return self._open(domain, token)
        except TokenError as exc:
            raise ProtocolError(
                INVALID_PARAMS, f"invalid signed token: {exc}", {"reason": exc.reason}
            ) from exc

    def _open(self, domain: str, token: str) -> dict:
        if not isinstance(token, str) or token.count(".") != 1:
            raise TokenError("malformed", "token is not a signed blob")
        body_part, mac_part = token.split(".")
        for part in (body_part, mac_part):
            if not part or any(c not in _URLSAFE_B64_CHARS for c in part):
                raise TokenError("malformed", "token is not base64url encoded")
        try:
            body_bytes = base64.urlsafe_b64decode(body_part.encode("ascii"))
            mac = base64.urlsafe_b64decode(mac_part.encode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TokenError("malformed", "token is not base64url encoded") from exc
        expected = hmac.new(
            self._key, domain.encode("utf-8") + b"." + body_bytes, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, mac):
            raise TokenError("tampered", "token signature mismatch")
        try:
            body = json.loads(body_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TokenError("tampered", "token payload is not valid JSON") from exc
        if (
            not isinstance(body, dict)
            or body.get("domain") != domain
            or not isinstance(body.get("payload"), dict)
        ):
            raise TokenError("tampered", "token does not belong to this domain")
        return body["payload"]

    # -- consent challenges ------------------------------------------------

    def challenge(
        self,
        *,
        principal: str,
        method: str,
        arguments: Mapping[str, Any],
        target: Mapping[str, Any],
        message: str,
        ttl_s: float | None = None,
    ) -> str:
        """Create a signed, self-contained consent challenge token."""

        payload = {
            "kind": "consent",
            "principal": fingerprint(principal),
            "exp": time.time() + (self._ttl_s if ttl_s is None else float(ttl_s)),
            "nonce": secrets.token_urlsafe(16),
            "method": method,
            "args": fingerprint(dict(arguments)),
            "target": fingerprint(dict(target)),
            "inputRequests": consent_input_request(message),
        }
        return self.sign(DOMAIN_CONSENT, payload)

    def consume(
        self,
        token: str,
        *,
        principal: str,
        method: str,
        arguments: Mapping[str, Any],
        target: Mapping[str, Any],
        input_responses: Any,
    ) -> dict:
        """Verify a returned challenge and consume its nonce exactly once.

        Order per MRTR: signature, expiry, principal, method/arguments,
        target, and only then the ``inputResponses`` interpretation. The
        nonce is consumed under a lock before the caller schedules work, so
        concurrent duplicate acceptance grants exactly once.

        Returns the verified signed payload on success. Raises:

        - :class:`InputRequired` when the necessary response is missing
          (the same challenge is repeated);
        - ``ToolError`` ``CONSENT_DENIED`` with a machine ``reason`` in
          ``details`` for declined/cancelled/tampered/expired/principal/
          argument/target/replay rejections;
        - ``ProtocolError`` -32602 for a malformed ``inputResponses`` map.
        """

        try:
            payload = self._open(DOMAIN_CONSENT, token)
        except TokenError as exc:
            raise ToolError(
                CONSENT_DENIED,
                "consent state rejected",
                {"reason": exc.reason},
            ) from exc
        if payload.get("kind") != "consent":
            raise ToolError(
                CONSENT_DENIED,
                "consent state rejected",
                {"reason": "tampered"},
            )
        if time.time() >= payload.get("exp", 0):
            raise ToolError(CONSENT_DENIED, "consent expired", {"reason": "expired"})
        if payload.get("principal") != fingerprint(principal):
            raise ToolError(
                CONSENT_DENIED,
                "consent was issued to a different principal",
                {"reason": "principal_mismatch"},
            )
        if payload.get("method") != method or payload.get("args") != fingerprint(dict(arguments)):
            raise ToolError(
                CONSENT_DENIED,
                "consent does not cover these arguments",
                {"reason": "argument_mismatch"},
            )
        nonce = payload.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise ToolError(CONSENT_DENIED, "consent state rejected", {"reason": "tampered"})
        with self._lock:
            self._prune()
            if nonce in self._consumed_nonces:
                raise ToolError(
                    CONSENT_DENIED,
                    "consent state was already used",
                    {"reason": "replayed"},
                )
        if payload.get("target") != fingerprint(dict(target) if target is not None else None):
            raise ToolError(
                CONSENT_DENIED,
                "consented target changed; new consent required",
                {"reason": "target_changed"},
            )

        # Signature, expiry, principal and arguments verified; only now may
        # inputResponses be interpreted.
        if input_responses is None:
            input_responses = {}
        if not isinstance(input_responses, Mapping):
            raise ProtocolError(
                INVALID_PARAMS, "invalid parameters: inputResponses must be an object"
            )
        response = input_responses.get(CONSENT_INPUT_KEY)

        def repeat_challenge() -> InputRequired:
            return InputRequired(token, payload.get("inputRequests"))

        if not isinstance(response, Mapping):
            raise repeat_challenge()
        action = response.get("action")
        if action == "decline":
            raise ToolError(CONSENT_DENIED, "consent declined", {"reason": "declined"})
        if action == "cancel":
            raise ToolError(CONSENT_DENIED, "consent cancelled", {"reason": "cancelled"})
        if action != "accept":
            raise repeat_challenge()
        content = response.get("content")
        confirmed = content.get("confirmed") if isinstance(content, Mapping) else None
        if confirmed is False:
            raise ToolError(CONSENT_DENIED, "consent declined", {"reason": "declined"})
        if confirmed is not True:
            # Missing necessary response: repeat the challenge, do not burn
            # the nonce.
            raise repeat_challenge()

        with self._lock:
            self._prune()
            if nonce in self._consumed_nonces:
                raise ToolError(
                    CONSENT_DENIED,
                    "consent state was already used",
                    {"reason": "replayed"},
                )
            self._consumed_nonces[nonce] = float(payload.get("exp", 0))
        return payload

    def _prune(self) -> None:
        now = time.time()
        for nonce in [n for n, exp in self._consumed_nonces.items() if exp <= now]:
            del self._consumed_nonces[nonce]
