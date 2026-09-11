"""Focused protocol contract tests: wire precedence, released header
encoding, finite schemas, and MRTR consent signing."""

import base64
import sys
import threading
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server import protocol

# ---------------------------------------------------------------------------
# Message/header fixtures.
# ---------------------------------------------------------------------------


_OMIT = object()  # Field-absent sentinel, distinct from an explicit null.


def make_meta(
    version=protocol.SUPPORTED_PROTOCOL_VERSION,
    client_info=None,
    capabilities=_OMIT,
    extra=None,
):
    meta = {
        protocol.META_PROTOCOL_VERSION: version,
        protocol.META_CLIENT_INFO: client_info
        if client_info is not None
        else {"name": "pytest-client", "version": "1.2.3"},
    }
    # capabilities=None stays an explicit null (invalid: not an object);
    # only an omitted argument defaults to the empty capabilities object.
    meta[protocol.META_CLIENT_CAPABILITIES] = {} if capabilities is _OMIT else capabilities
    if extra:
        meta.update(extra)
    return meta


def make_params(meta=None, **fields):
    params = dict(fields)
    # Default the Mcp-Name mirror so tool messages built without explicit
    # arguments still pass routing checks; explicit values always win.
    params.setdefault("name", "t")
    params["_meta"] = meta if meta is not None else make_meta()
    return params


def make_headers(
    method="tools/call", name=None, version=protocol.SUPPORTED_PROTOCOL_VERSION, **extra
):
    headers = {
        protocol.PROTOCOL_VERSION_HEADER: version,
        protocol.METHOD_HEADER: method,
    }
    if name is not None:
        headers[protocol.NAME_HEADER] = name
    headers.update(extra)
    return headers


def valid_message(method="tools/call", params=None, request_id=7):
    message = {"jsonrpc": "2.0", "method": method, "params": params or make_params()}
    # request_id=_OMIT omits "id" (notification); request_id=None stays an
    # explicit null id, which the envelope must reject.
    if request_id is not _OMIT:
        message["id"] = request_id
    return message


def sentinel(value):
    return (
        protocol.SENTINEL_PREFIX
        + base64.b64encode(value.encode("utf-8")).decode("ascii")
        + protocol.SENTINEL_SUFFIX
    )


def expect_protocol_error(excinfo, code):
    error = excinfo.value
    assert isinstance(error, protocol.ProtocolError)
    assert error.code == code
    return error


# ---------------------------------------------------------------------------
# Exact error precedence: envelope -> metadata -> headers -> version.
# ---------------------------------------------------------------------------


class TestErrorPrecedence:
    def test_envelope_beats_metadata_and_headers(self):
        message = {"jsonrpc": "1.0", "id": 1, "method": "tools/call"}
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, {})
        expect_protocol_error(excinfo, protocol.INVALID_REQUEST)

    def test_envelope_rejects_batch_and_bad_params(self):
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request([valid_message()], make_headers(name="t"))
        expect_protocol_error(excinfo, protocol.INVALID_REQUEST)

        message = valid_message(params=[1, 2])
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, make_headers(name="t"))
        expect_protocol_error(excinfo, protocol.INVALID_REQUEST)

    def test_envelope_rejects_boolean_null_and_float_ids(self):
        for bad_id in (True, None, 1.5):
            message = valid_message(request_id=bad_id)
            with pytest.raises(protocol.ProtocolError) as excinfo:
                protocol.validate_request(message, make_headers(name="t"))
            expect_protocol_error(excinfo, protocol.INVALID_REQUEST)

    def test_envelope_accepts_string_integer_and_missing_ids(self):
        validated = protocol.validate_request(
            valid_message(request_id="abc"), make_headers(name="t")
        )
        assert validated["id"] == "abc"
        validated = protocol.validate_request(valid_message(), make_headers(name="t"))
        assert validated["id"] == 7
        notification = valid_message(request_id=_OMIT)
        validated = protocol.validate_request(notification, make_headers(name="t"))
        assert validated["is_notification"] is True
        assert validated["id"] is None

    def test_metadata_beats_headers(self):
        message = valid_message(params={"name": "t"})  # no _meta at all
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, make_headers(name="t"))
        expect_protocol_error(excinfo, protocol.INVALID_PARAMS)

    def test_malformed_metadata_beats_headers(self):
        meta = make_meta(version=None)
        message = valid_message(params=make_params(meta=meta))
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, make_headers(name="t"))
        expect_protocol_error(excinfo, protocol.INVALID_PARAMS)

    def test_client_info_required_even_though_spec_marks_it_optional(self):
        meta = make_meta(client_info=None)
        del meta[protocol.META_CLIENT_INFO]
        message = valid_message(params=make_params(meta=meta))
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, make_headers(name="t"))
        expect_protocol_error(excinfo, protocol.INVALID_PARAMS)

        for bad_info in (
            {"name": 5, "version": "1"},
            {"name": "x"},
            {"name": "x", "version": ""},
        ):
            meta = make_meta(client_info=bad_info)
            message = valid_message(params=make_params(meta=meta))
            with pytest.raises(protocol.ProtocolError) as excinfo:
                protocol.validate_request(message, make_headers(name="t"))
            expect_protocol_error(excinfo, protocol.INVALID_PARAMS)

    def test_client_capabilities_must_be_an_object(self):
        for bad_caps in (None, []):
            meta = make_meta(capabilities=bad_caps)
            message = valid_message(params=make_params(meta=meta))
            with pytest.raises(protocol.ProtocolError) as excinfo:
                protocol.validate_request(message, make_headers(name="t"))
            expect_protocol_error(excinfo, protocol.INVALID_PARAMS)

    def test_header_mismatch_beats_version_negotiation(self):
        meta = make_meta(version="2025-11-25")
        message = valid_message(params=make_params(meta=meta))
        headers = make_headers(name="t", version=protocol.SUPPORTED_PROTOCOL_VERSION)
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, headers)
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_missing_version_header_beats_version_negotiation(self):
        meta = make_meta(version="2025-11-25")
        message = valid_message(params=make_params(meta=meta))
        headers = make_headers(name="t")
        del headers[protocol.PROTOCOL_VERSION_HEADER]
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, headers)
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_unsupported_version_exact_data_shape(self):
        meta = make_meta(version="2025-11-25")
        message = valid_message(params=make_params(meta=meta))
        headers = make_headers(name="t", version="2025-11-25")
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, headers)
        error = expect_protocol_error(excinfo, protocol.UNSUPPORTED_PROTOCOL_VERSION)
        assert error.data == {
            "supported": ["2026-07-28"],
            "requested": "2025-11-25",
        }

    def test_discovery_gets_no_version_exemption(self):
        meta = make_meta(version="2020-01-01")
        message = valid_message(method="server/discover", params=make_params(meta=meta))
        headers = make_headers(method="server/discover", version="2020-01-01")
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, headers)
        expect_protocol_error(excinfo, protocol.UNSUPPORTED_PROTOCOL_VERSION)

    def test_unknown_extension_metadata_is_accepted(self):
        meta = make_meta(
            extra={
                "com.example/custom": {"anything": [1, 2]},
                protocol.META_SUBSCRIPTION_ID: "ignored-here",
            }
        )
        capabilities = {"elicitation": {"form": {}}, "vendor.experimental": {"x": 1}}
        meta[protocol.META_CLIENT_CAPABILITIES] = capabilities
        message = valid_message(params=make_params(meta=meta))
        validated = protocol.validate_request(message, make_headers(name="t"))
        assert validated["client_capabilities"] == capabilities
        assert validated["_meta"]["com.example/custom"] == {"anything": [1, 2]}

    def test_validated_view_shape(self):
        params = make_params(name="t")
        validated = protocol.validate_request(valid_message(params=params), make_headers(name="t"))
        assert validated["method"] == "tools/call"
        assert validated["params"] is params
        assert validated["protocol_version"] == protocol.SUPPORTED_PROTOCOL_VERSION
        assert validated["client_info"] == {"name": "pytest-client", "version": "1.2.3"}
        assert validated["client_capabilities"] == {}
        assert validated["is_notification"] is False


# ---------------------------------------------------------------------------
# Released request-metadata headers, including Base64 sentinel encoding.
# ---------------------------------------------------------------------------


class TestReleasedHeaderEncoding:
    def test_plain_name_header_matches(self):
        validated = protocol.validate_request(
            valid_message(params=make_params(name="my_tool")),
            make_headers(name="my_tool"),
        )
        assert validated["params"]["name"] == "my_tool"

    def test_header_names_case_insensitive_values_case_sensitive(self):
        headers = {
            "mcp-protocol-version": protocol.SUPPORTED_PROTOCOL_VERSION,
            "MCP-METHOD": "tools/call",
            "McP-nAmE": "my_tool",
        }
        validated = protocol.validate_request(
            valid_message(params=make_params(name="my_tool")), headers
        )
        assert validated["method"] == "tools/call"

        headers[protocol.METHOD_HEADER] = "TOOLS/CALL"
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(valid_message(params=make_params(name="my_tool")), headers)
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_non_ascii_name_requires_sentinel_encoding(self):
        body_name = "file:///wörld/π.json"
        message = valid_message(method="resources/read", params=make_params(uri=body_name))
        validated = protocol.validate_request(
            message, make_headers(method="resources/read", name=sentinel(body_name))
        )
        assert validated["params"]["uri"] == body_name

        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(
                message, make_headers(method="resources/read", name=body_name)
            )
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_sentinel_looking_body_value_must_arrive_encoded(self):
        tricky = "=?base64?dG9vbA==?="
        message = valid_message(params=make_params(name=tricky))
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, make_headers(name=tricky))
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

        validated = protocol.validate_request(message, make_headers(name=sentinel(tricky)))
        assert validated["params"]["name"] == tricky

    def test_malformed_sentinel_values_are_rejected(self):
        for bad in ("=?base64?!!!!?=x", "=?base64?=  ", "=?base64?///bad==?="):
            message = valid_message(params=make_params(name="t"))
            with pytest.raises(protocol.ProtocolError) as excinfo:
                protocol.validate_request(message, make_headers(name=bad))
            expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_header_values_with_invalid_characters_are_rejected(self):
        message = valid_message(params=make_params(name="t"))
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, make_headers(name="bad\nname"))
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_missing_or_mismatched_method_header(self):
        message = valid_message(params=make_params(name="t"))
        headers = make_headers(name="t")
        del headers[protocol.METHOD_HEADER]
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, headers)
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, make_headers(method="tools/list", name="t"))
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_name_header_required_only_for_name_source_methods(self):
        message = valid_message(params=make_params(name="t"))
        headers = make_headers()
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, headers)
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

        # server/discover has no name source and needs no Mcp-Name.
        message = valid_message(method="server/discover")
        validated = protocol.validate_request(message, make_headers(method="server/discover"))
        assert validated["method"] == "server/discover"

    def test_name_header_must_match_body_source(self):
        message = valid_message(params=make_params(name="actual"))
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, make_headers(name="other"))
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

        message = valid_message(params={"_meta": make_meta()})  # name missing
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, make_headers(name="actual"))
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_task_methods_mirror_task_id_in_mcp_name(self):
        params = make_params(taskId="task-9")
        validated = protocol.validate_request(
            valid_message(method="tasks/cancel", params=params),
            make_headers(method="tasks/cancel", name="task-9"),
        )
        assert validated["method"] == "tasks/cancel"

        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(
                valid_message(method="tasks/cancel", params=params),
                make_headers(method="tasks/cancel", name="task-8"),
            )
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_protocol_version_header_must_match_metadata(self):
        message = valid_message(params=make_params())
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(message, make_headers(name="t", version="2025-11-25"))
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_mcp_param_headers_match_body_arguments(self):
        paths = {"Region": ("arguments", "region")}
        params = make_params(name="execute_sql", arguments={"region": "us-west1"})
        validated = protocol.validate_request(
            valid_message(params=params),
            make_headers(name="execute_sql", **{"Mcp-Param-Region": "us-west1"}),
            param_paths=paths,
        )
        assert validated["params"]["arguments"]["region"] == "us-west1"

        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(
                valid_message(params=params),
                make_headers(name="execute_sql", **{"Mcp-Param-Region": "eu-east1"}),
                param_paths=paths,
            )
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_mcp_param_headers_checked_in_both_directions(self):
        paths = {"Region": ("arguments", "region")}
        params = make_params(name="t", arguments={"region": "us-west1"})
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(
                valid_message(params=params), make_headers(name="t"), param_paths=paths
            )
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

        headers = make_headers(name="t", **{"Mcp-Param-Region": "us-west1"})
        empty_params = make_params(name="t", arguments={})
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(
                valid_message(params=empty_params), headers, param_paths=paths
            )
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_mcp_param_numeric_and_boolean_comparison(self):
        paths = {"Count": ("arguments", "count"), "Flag": ("arguments", "flag")}
        params = make_params(name="t", arguments={"count": 42.0, "flag": True})
        headers = make_headers(name="t", **{"Mcp-Param-Count": "42", "Mcp-Param-Flag": "true"})
        protocol.validate_request(valid_message(params=params), headers, param_paths=paths)

        for header, key in (("43", "Count"), ("True", "Flag")):
            bad = make_headers(name="t", **{f"Mcp-Param-{key}": header})
            with pytest.raises(protocol.ProtocolError) as excinfo:
                protocol.validate_request(valid_message(params=params), bad, param_paths=paths)
            expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)

    def test_unknown_mcp_param_headers_only_need_valid_encoding(self):
        params = make_params(name="t")
        validated = protocol.validate_request(
            valid_message(params=params),
            make_headers(name="t", **{"Mcp-Param-Unknown": "us-west1"}),
        )
        assert validated["method"] == "tools/call"

        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_request(
                valid_message(params=params),
                make_headers(name="t", **{"Mcp-Param-Unknown": "bad\x0bvalue"}),
            )
        expect_protocol_error(excinfo, protocol.HEADER_MISMATCH)


# ---------------------------------------------------------------------------
# Finite schemas.
# ---------------------------------------------------------------------------


EMITTED_TOOL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document"],
    "properties": {
        "document": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        "detail": {"type": "string", "enum": ["compact", "full"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "bounds": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 6,
                "maxItems": 6,
            },
        },
        "ref": {"$ref": "#/$defs/marker"},
    },
    "$defs": {
        "marker": {
            "type": "object",
            "additionalProperties": False,
            "required": ["object"],
            "properties": {"object": {"type": "string"}},
        }
    },
}


class TestFiniteSchemas:
    def test_emitted_schema_passes_check(self):
        protocol.check_schema(EMITTED_TOOL_SCHEMA)

    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "pattern": "^a"},
            {"type": "object", "oneOf": []},
            {"type": "string", "format": "uri"},
            {"type": "array", "uniqueItems": True},
            {"type": "string", "minLength": -1},
            {"type": "number", "minimum": float("nan")},
            {"type": "number", "maximum": float("inf")},
            {"type": "integer", "exclusiveMinimum": True},
            {"type": "map"},
            {"type": "object", "enum": []},
            {"type": "object", "properties": []},
            {"type": "object", "additionalProperties": True},
            {"type": "object", "items": []},
            {"$ref": "https://example.com/schema.json"},
            {"$ref": "#/$defs/missing"},
            {"type": "object", "properties": {"a": {"$defs": {}}}},
            {"type": []},
            {"type": ["object", "map"]},
            {"anyOf": []},
            {"anyOf": {}},
            {"anyOf": [{"type": "string"}, 4]},
        ],
    )
    def test_unsupported_constructs_are_rejected_at_registration(self, schema):
        with pytest.raises(ValueError):
            protocol.check_schema(schema)

    @pytest.mark.parametrize(
        "schema",
        [
            {
                "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}},
                "type": "object",
            },
            {"$defs": {"a": {"$ref": "#/$defs/a"}}, "type": "object"},
        ],
    )
    def test_recursive_refs_are_rejected(self, schema):
        with pytest.raises(ValueError):
            protocol.check_schema(schema)

    def test_validate_schema_accepts_valid_nested_payload(self):
        protocol.validate_schema(
            {
                "document": "Doc",
                "limit": 50,
                "detail": "compact",
                "tags": ["a", "b"],
                "bounds": {"Box": [0, 0, 0, 10, 10, 10]},
                "ref": {"object": "Box"},
            },
            EMITTED_TOOL_SCHEMA,
        )

    def test_additional_properties_false_rejects_unknown_keys(self):
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_schema({"document": "Doc", "hacker": 1}, EMITTED_TOOL_SCHEMA)
        assert excinfo.value.code == protocol.INVALID_PARAMS

    def test_missing_required_rejected(self):
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_schema({}, EMITTED_TOOL_SCHEMA)
        assert excinfo.value.code == protocol.INVALID_PARAMS

    def test_non_finite_numbers_never_validate(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(protocol.ProtocolError) as excinfo:
                protocol.validate_schema(
                    {"document": "D", "bounds": {"B": [0] * 5 + [bad]}},
                    EMITTED_TOOL_SCHEMA,
                )
            assert excinfo.value.code == protocol.INVALID_PARAMS

    def test_booleans_are_not_integers(self):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema({"document": "D", "limit": True}, EMITTED_TOOL_SCHEMA)

    def test_integral_floats_count_as_integers(self):
        protocol.validate_schema({"document": "D", "limit": 50.0}, EMITTED_TOOL_SCHEMA)
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema({"document": "D", "limit": 50.5}, EMITTED_TOOL_SCHEMA)

    def test_bounds_and_enums_enforced(self):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema({"document": "D", "limit": 501}, EMITTED_TOOL_SCHEMA)
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema({"document": "D", "limit": 0}, EMITTED_TOOL_SCHEMA)
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema({"document": "D", "detail": "deep"}, EMITTED_TOOL_SCHEMA)

    def test_enum_equality_is_type_aware(self):
        schema = {"type": "string", "enum": ["1", "true"]}
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema(1, schema)
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema(True, schema)
        protocol.validate_schema("1", schema)

    def test_string_and_array_bounds(self):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema({"document": ""}, EMITTED_TOOL_SCHEMA)
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema(
                {"document": "D", "tags": list("abcde") * 3}, EMITTED_TOOL_SCHEMA
            )
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema(
                {"document": "D", "bounds": {"B": [0, 0, 0, 10, 10]}},
                EMITTED_TOOL_SCHEMA,
            )

    def test_local_ref_resolves_during_validation(self):
        protocol.validate_schema({"document": "D", "ref": {"object": "Box"}}, EMITTED_TOOL_SCHEMA)
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema({"document": "D", "ref": {"object": 9}}, EMITTED_TOOL_SCHEMA)

    def test_anyof_accepts_string_or_object_selectors(self):
        selector = {
            "anyOf": [
                {"type": "string", "minLength": 1},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["object"],
                    "properties": {"object": {"type": "string"}},
                },
            ]
        }
        protocol.check_schema(selector)
        protocol.validate_schema("Box", selector)
        protocol.validate_schema({"object": "Box"}, selector)
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_schema({"object": "Box", "hacker": 1}, selector)
        assert excinfo.value.code == protocol.INVALID_PARAMS
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema(7, selector)
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema("", selector)

    def test_type_arrays_express_nullable_outputs(self):
        nullable = {"type": ["string", "null"]}
        protocol.check_schema(nullable)
        protocol.validate_schema("flow", nullable)
        protocol.validate_schema(None, nullable)
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.validate_schema(5, nullable)
        assert excinfo.value.code == protocol.INVALID_PARAMS
        protocol.validate_schema(50.0, {"type": ["integer", "null"]})
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema(True, {"type": ["integer", "null"]})

    def test_non_finite_values_rejected_even_under_empty_schemas(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(protocol.ProtocolError) as excinfo:
                protocol.validate_schema(bad, {})
            assert excinfo.value.code == protocol.INVALID_PARAMS
            with pytest.raises(protocol.ProtocolError):
                protocol.validate_schema([1, {"b": bad}], {})
            with pytest.raises(protocol.ProtocolError):
                protocol.validate_schema({"a": {"b": [1, bad]}}, {"type": "object"})


# ---------------------------------------------------------------------------
# Result and error builders.
# ---------------------------------------------------------------------------


class TestBuilders:
    def test_complete_result_stamps_result_type_and_server_info(self):
        result = protocol.complete_result({"supportedVersions": ["2026-07-28"]})
        assert result["resultType"] == "complete"
        assert result["_meta"][protocol.META_SERVER_INFO] == {
            "name": "freecad-mcp-addon",
            "version": "2.0.0",
        }
        assert result["supportedVersions"] == ["2026-07-28"]

    def test_complete_result_merges_existing_meta(self):
        result = protocol.complete_result({"_meta": {protocol.META_SUBSCRIPTION_ID: 4}})
        assert result["_meta"][protocol.META_SUBSCRIPTION_ID] == 4
        assert result["_meta"][protocol.META_SERVER_INFO] == protocol.SERVER_INFO

    def test_tool_result_structured_payload_has_text_and_structured_content(self):
        payload = {"name": "Box", "objectCount": 1}
        result = protocol.tool_result(payload)
        assert result["resultType"] == "complete"
        assert result["structuredContent"] == payload
        assert "isError" not in result
        import json as _json

        assert _json.loads(result["content"][0]["text"]) == payload

    def test_tool_result_content_list_passthrough_and_is_error(self):
        image = [{"type": "image", "data": "AAAA", "mimeType": "image/png"}]
        result = protocol.tool_result(image)
        assert result["content"] == image
        assert "structuredContent" not in result

        failing = protocol.tool_result({"anything": 1}, is_error=True)
        assert failing["isError"] is True

    def test_tool_result_rejects_non_finite_payloads(self):
        with pytest.raises(ValueError):
            protocol.tool_result({"volume": float("nan")})

    def test_tool_error_result_shape(self):
        error = protocol.ToolError(
            protocol.DOCUMENT_NOT_FOUND, "document not found", details={"name": "X"}
        )
        result = protocol.tool_error_result(error)
        assert result["isError"] is True
        assert result["content"] == [
            {"type": "text", "text": "DOCUMENT_NOT_FOUND: document not found"}
        ]
        assert result["structuredContent"] == {
            "error": {
                "code": "DOCUMENT_NOT_FOUND",
                "message": "document not found",
                "details": {"name": "X"},
            }
        }

        bare = protocol.ToolError(protocol.SOLVER_FAILED, "solver failed")
        result = protocol.tool_error_result(bare)
        assert "details" not in result["structuredContent"]["error"]

    def test_tool_error_result_text_includes_next_tool(self):
        error = protocol.ToolError(
            protocol.VALIDATION_FAILED,
            "stale",
            details={"reason": "stale_generation", "nextTool": "inspect_objects"},
        )
        result = protocol.tool_error_result(error)
        text = result["content"][0]["text"]
        assert '"nextTool": "inspect_objects"' in text

    def test_input_required_result_requires_one_field(self):
        challenge = protocol.consent_input_request("Proceed?")
        result = protocol.input_required_result(challenge, "state-token")
        assert result["resultType"] == "input_required"
        assert result["inputRequests"] == challenge
        assert result["requestState"] == "state-token"
        assert result["_meta"][protocol.META_SERVER_INFO] == protocol.SERVER_INFO

        only_state = protocol.input_required_result(None, "state-token")
        assert "inputRequests" not in only_state

        with pytest.raises(ValueError):
            protocol.input_required_result(None, None)

    def test_error_response_from_protocol_error(self):
        error = protocol.ProtocolError(protocol.HEADER_MISMATCH, "nope", {"x": 1})
        response = protocol.error_response(error, request_id=3)
        assert response == {
            "jsonrpc": "2.0",
            "id": 3,
            "error": {"code": -32020, "message": "nope", "data": {"x": 1}},
        }

    def test_error_response_omits_absent_data_and_fabricates_no_id(self):
        response = protocol.error_response(
            protocol.ProtocolError(protocol.INVALID_REQUEST, "bad"), request_id=None
        )
        assert "id" not in response
        assert response["error"] == {"code": -32600, "message": "bad"}

    def test_error_response_from_mapping(self):
        response = protocol.error_response({"code": -32601, "message": "unknown"})
        assert response["error"]["code"] == -32601
        with pytest.raises(TypeError):
            protocol.error_response({"message": "no code"})

    def test_parse_error_response(self):
        response = protocol.parse_error_response()
        assert response["error"]["code"] == protocol.PARSE_ERROR
        assert "id" not in response

    def test_capability_helpers(self):
        protocol.require_client_capabilities(
            {"extensions": {"io.modelcontextprotocol/tasks": {}}},
            {"extensions": {"io.modelcontextprotocol/tasks": {}}},
        )
        error = protocol.missing_capability({"elicitation": {"form": {}}})
        assert error.code == protocol.MISSING_REQUIRED_CLIENT_CAPABILITY
        assert error.data == {"requiredCapabilities": {"elicitation": {"form": {}}}}
        with pytest.raises(protocol.ProtocolError) as excinfo:
            protocol.require_client_capabilities({}, {"elicitation": {"form": {}}})
        assert excinfo.value.code == protocol.MISSING_REQUIRED_CLIENT_CAPABILITY


# ---------------------------------------------------------------------------
# Consent signing: MRTR challenges, tamper resistance, locked nonces.
# ---------------------------------------------------------------------------


PRINCIPAL = "bearer-token-fingerprint-source"
METHOD = "open_document"
ARGUMENTS = {"path": "/home/u/model.FCStd", "untrusted": True}
TARGET = {"identity": "fcstd", "size": 1234, "mtime_ns": 987654321}
MESSAGE = "Open an untrusted document?"


def accept_responses():
    return {"confirm": {"action": "accept", "content": {"confirmed": True}}}


class TestConsentSigner:
    def make_signer(self):
        return protocol.ConsentSigner()

    def challenge(self, signer, **overrides):
        return signer.challenge(
            principal=overrides.get("principal", PRINCIPAL),
            method=overrides.get("method", METHOD),
            arguments=overrides.get("arguments", ARGUMENTS),
            target=overrides.get("target", TARGET),
            message=overrides.get("message", MESSAGE),
            ttl_s=overrides.get("ttl_s"),
        )

    def consume(self, signer, token, responses=None, **overrides):
        return signer.consume(
            token,
            principal=overrides.get("principal", PRINCIPAL),
            method=overrides.get("method", METHOD),
            arguments=overrides.get("arguments", ARGUMENTS),
            target=overrides.get("target", TARGET),
            input_responses=accept_responses() if responses is None else responses,
        )

    def test_valid_acceptance_grants_and_returns_payload(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        payload = self.consume(signer, token)
        assert payload["kind"] == "consent"
        assert payload["method"] == METHOD

    def test_arguments_order_is_normalized(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        reordered = {"untrusted": True, "path": "/home/u/model.FCStd"}
        self.consume(signer, token, arguments=reordered)

    def test_decline_denies_without_burning_nonce(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, token, {"confirm": {"action": "decline"}})
        assert excinfo.value.code == protocol.CONSENT_DENIED
        assert excinfo.value.details == {"reason": "declined"}

        # A later acceptance with the same state still works exactly once.
        self.consume(signer, token)
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, token)
        assert excinfo.value.details == {"reason": "replayed"}

    def test_cancel_denies(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, token, {"confirm": {"action": "cancel"}})
        assert excinfo.value.details == {"reason": "cancelled"}

    def test_missing_response_repeats_the_same_challenge(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        for responses in (
            {},
            {"confirm": {"action": "accept"}},
            {"other": {"action": "accept"}},
        ):
            with pytest.raises(protocol.InputRequired) as excinfo:
                self.consume(signer, token, responses)
            repeat = excinfo.value
            assert repeat.request_state == token
            assert protocol.CONSENT_INPUT_KEY in repeat.input_requests
            request = repeat.input_requests[protocol.CONSENT_INPUT_KEY]
            assert request["method"] == "elicitation/create"
            assert request["params"]["mode"] == "form"
            assert request["params"]["requestedSchema"]["required"] == ["confirmed"]

        # Unknown keys never grant authority; a proper accept after repeats works.
        with_extra = accept_responses()
        with_extra["unrelated"] = {"action": "accept", "content": {"confirmed": True}}
        self.consume(signer, token, with_extra)

    def test_explicit_false_confirmed_is_a_decline(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(
                signer,
                token,
                {"confirm": {"action": "accept", "content": {"confirmed": False}}},
            )
        assert excinfo.value.details == {"reason": "declined"}

    def test_non_boolean_confirmed_repeats_challenge(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        with pytest.raises(protocol.InputRequired):
            self.consume(
                signer,
                token,
                {"confirm": {"action": "accept", "content": {"confirmed": "yes"}}},
            )

    def test_malformed_input_responses_is_invalid_params(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        with pytest.raises(protocol.ProtocolError) as excinfo:
            self.consume(signer, token, ["not", "a", "map"])
        assert excinfo.value.code == protocol.INVALID_PARAMS

    def test_tampered_tokens_are_rejected(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        body_part, mac_part = token.split(".")

        # Mutate the body through a valid re-encoding: the MAC must fail.
        body_bytes = base64.urlsafe_b64decode(body_part.encode("ascii"))
        mutated_body = base64.urlsafe_b64encode(
            body_bytes[:-1] + bytes([body_bytes[-1] ^ 0x01])
        ).decode("ascii")
        # Flip one character of the MAC: still valid base64url, wrong digest.
        flipped_mac = ("A" if mac_part[0] != "A" else "B") + mac_part[1:]

        for tampered in (f"{body_part}.{flipped_mac}", f"{mutated_body}.{mac_part}"):
            with pytest.raises(protocol.ToolError) as excinfo:
                self.consume(signer, tampered)
            assert excinfo.value.code == protocol.CONSENT_DENIED
            assert excinfo.value.details == {"reason": "tampered"}

        # A structurally broken token is also a rejection (malformed).
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, "not-a-token")
        assert excinfo.value.code == protocol.CONSENT_DENIED
        assert excinfo.value.details["reason"] in {"tampered", "malformed"}

        # A validly signed token from a different key cannot be accepted.
        stranger = protocol.ConsentSigner()
        foreign = self.challenge(stranger)
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, foreign)
        assert excinfo.value.details == {"reason": "tampered"}

    def test_expired_challenge_is_rejected(self):
        signer = self.make_signer()
        token = self.challenge(signer, ttl_s=-1)
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, token)
        assert excinfo.value.details == {"reason": "expired"}

    def test_argument_and_method_binding(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, token, arguments={"path": "/home/u/OTHER.FCStd"})
        assert excinfo.value.details == {"reason": "argument_mismatch"}

        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, token, method="close_document")
        assert excinfo.value.details == {"reason": "argument_mismatch"}

    def test_principal_binding(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, token, principal="someone-else")
        assert excinfo.value.details == {"reason": "principal_mismatch"}

    def test_target_change_requires_new_consent(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        changed = dict(TARGET, size=9999)
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, token, target=changed)
        assert excinfo.value.details == {"reason": "target_changed"}

    @pytest.mark.parametrize("target", [TARGET, dict(TARGET, size=9999), None])
    def test_replay_after_acceptance_is_rejected(self, target):
        signer = self.make_signer()
        token = self.challenge(signer)
        self.consume(signer, token)
        with pytest.raises(protocol.ToolError) as excinfo:
            self.consume(signer, token, target=target)
        assert excinfo.value.details == {"reason": "replayed"}

    def test_concurrent_duplicate_acceptance_grants_exactly_once(self):
        signer = self.make_signer()
        token = self.challenge(signer)
        barrier = threading.Barrier(8)
        outcomes = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            try:
                self.consume(signer, token)
                outcome = ("granted", None)
            except protocol.ToolError as error:
                outcome = ("denied", error.details["reason"])
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        granted = [outcome for outcome in outcomes if outcome[0] == "granted"]
        replayed = [outcome for outcome in outcomes if outcome[1] == "replayed"]
        assert len(granted) == 1
        assert len(replayed) == 7

    def test_domain_separated_reusable_signing(self):
        signer = self.make_signer()
        cursor = signer.sign(protocol.DOMAIN_CURSOR, {"generation": 3, "last": "Box"})
        assert signer.verify(protocol.DOMAIN_CURSOR, cursor) == {
            "generation": 3,
            "last": "Box",
        }

        with pytest.raises(protocol.ProtocolError) as excinfo:
            signer.verify(protocol.DOMAIN_TOPOLOGY, cursor)
        assert excinfo.value.code == protocol.INVALID_PARAMS
        assert excinfo.value.data["reason"] in {"tampered", "malformed"}

        consent = self.challenge(signer)
        with pytest.raises(protocol.ProtocolError):
            signer.verify(protocol.DOMAIN_CURSOR, consent)

        with pytest.raises(protocol.ProtocolError) as excinfo:
            signer.verify(protocol.DOMAIN_CURSOR, "garbage")
        assert excinfo.value.code == protocol.INVALID_PARAMS
