"""Focused tests for embedded MCP settings persistence and IP parsing.

Covers the v2 schema bootstrap, atomic user-only secret writes, strict
failures on invalid settings, and the rule that legacy RPC autostart keys are
dropped without being interpreted as v2 consent.
"""

import ipaddress
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.ip_parse import parse_allowed_networks, validate_allowed_ips  # noqa: E402
from mcp_server.settings import (  # noqa: E402
    DEFAULT_ALLOWED_IPS,
    DEFAULT_PORT,
    SettingsError,
    load_settings,
    save_settings,
)

POSIX = os.name == "posix"


def valid_settings(**overrides):
    settings = {
        "port": 9876,
        "token": "unit-test-token",
        "auto_start": False,
        "allowed_ips": "127.0.0.1",
        "allowed_roots": [os.path.expanduser("~")],
    }
    settings.update(overrides)
    return settings


# --------------------------------------------------------------------------
# GUI independence


def test_transport_and_settings_import_without_freecad():
    """Package import and explicit-path loading must stay GUI independent."""
    code = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, {addon!r})
        import mcp_server                     # package init re-exports protocol
        import mcp_server.http_server         # transport imports
        import mcp_server.settings
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        loaded = mcp_server.settings.load_settings(path)
        assert loaded["port"] == 9876 and loaded["token"]
        assert "FreeCAD" not in sys.modules, "FreeCAD must stay unimported"
        print("GUI-INDEPENDENT-OK")
        """
    ).format(addon=str(ADDON_DIR))
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "GUI-INDEPENDENT-OK" in result.stdout


# --------------------------------------------------------------------------
# bootstrap and roundtrip


def test_missing_file_bootstraps_defaults_with_persisted_token(tmp_path):
    path = tmp_path / "settings.json"
    settings = load_settings(str(path))

    assert settings["port"] == DEFAULT_PORT
    assert settings["auto_start"] is False
    assert settings["allowed_ips"] == DEFAULT_ALLOWED_IPS
    assert settings["allowed_roots"] == [os.path.abspath(os.path.expanduser("~"))]
    # secrets.token_urlsafe(32) yields a 43-character URL-safe secret.
    assert isinstance(settings["token"], str) and len(settings["token"]) == 43

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == settings


def test_second_load_returns_same_generated_token(tmp_path):
    path = tmp_path / "settings.json"
    first = load_settings(str(path))
    second = load_settings(str(path))
    assert first["token"] == second["token"]


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(
        valid_settings(
            port=12345,
            auto_start=True,
            allowed_ips="127.0.0.1, 10.0.0.0/8",
            allowed_roots=["~/Projects", "/tmp"],
        ),
        str(path),
    )
    loaded = load_settings(str(path))
    assert loaded["port"] == 12345
    assert loaded["token"] == "unit-test-token"
    assert loaded["auto_start"] is True
    assert loaded["allowed_ips"] == "127.0.0.1, 10.0.0.0/8"
    assert loaded["allowed_roots"] == [
        os.path.abspath(os.path.expanduser("~/Projects")),
        "/tmp",
    ]


def test_missing_token_in_existing_file_is_generated_and_persisted(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"port": 9999}), encoding="utf-8")

    settings = load_settings(str(path))

    assert settings["port"] == 9999
    assert isinstance(settings["token"], str) and settings["token"]
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == settings["token"]


# --------------------------------------------------------------------------
# atomic user-only secret writes


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_saved_file_has_user_only_permissions(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(valid_settings(), str(path))
    mode = stat.S_IMODE(os.stat(str(path)).st_mode)
    assert mode == 0o600


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_replacement_file_restores_user_only_permissions(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(valid_settings()), encoding="utf-8")
    os.chmod(str(path), 0o644)
    save_settings(valid_settings(port=1), str(path))
    mode = stat.S_IMODE(os.stat(str(path)).st_mode)
    assert mode == 0o600


def test_save_leaves_no_temporary_files(tmp_path):
    path = tmp_path / "settings.json"
    for port in (1, 2, 3):
        save_settings(valid_settings(port=port), str(path))
    assert [entry.name for entry in sorted(tmp_path.iterdir())] == [path.name]


# --------------------------------------------------------------------------
# strict failures


def test_malformed_json_fails_closed(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SettingsError):
        load_settings(str(path))


def test_non_object_json_fails_closed(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SettingsError):
        load_settings(str(path))


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
@pytest.mark.skipif(POSIX and os.geteuid() == 0, reason="root reads any file")
def test_unreadable_file_fails_closed(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(valid_settings()), encoding="utf-8")
    os.chmod(str(path), 0o000)
    try:
        with pytest.raises(SettingsError):
            load_settings(str(path))
    finally:
        os.chmod(str(path), 0o600)


@pytest.mark.parametrize("port", ["9876", 70000, -1, True, False, None, 9876.0, [9876]])
def test_invalid_port_fails_closed(tmp_path, port):
    with pytest.raises(SettingsError):
        save_settings(valid_settings(port=port), str(tmp_path / "settings.json"))


@pytest.mark.parametrize("token", ["", "   ", None, 42, b"token", ["x"]])
def test_invalid_token_fails_closed(tmp_path, token):
    with pytest.raises(SettingsError):
        save_settings(valid_settings(token=token), str(tmp_path / "settings.json"))


@pytest.mark.parametrize("auto_start", ["yes", 1, 0, None])
def test_invalid_auto_start_fails_closed(tmp_path, auto_start):
    with pytest.raises(SettingsError):
        save_settings(
            valid_settings(auto_start=auto_start), str(tmp_path / "settings.json")
        )


@pytest.mark.parametrize(
    "allowed_ips", ["not-an-ip", "127.0.0.1,", ",127.0.0.1", "127.0.0.1,,10.0.0.1", 5]
)
def test_invalid_allowed_ips_fails_closed(tmp_path, allowed_ips):
    with pytest.raises(SettingsError):
        save_settings(
            valid_settings(allowed_ips=allowed_ips), str(tmp_path / "settings.json")
        )


@pytest.mark.parametrize("allowed_roots", ["home", "", [""], [42], {"root": 1}, [None]])
def test_invalid_allowed_roots_fails_closed(tmp_path, allowed_roots):
    with pytest.raises(SettingsError):
        save_settings(
            valid_settings(allowed_roots=allowed_roots), str(tmp_path / "settings.json")
        )


def test_unknown_key_fails_closed(tmp_path):
    with pytest.raises(SettingsError):
        save_settings(
            valid_settings(unattended_upgrades=True), str(tmp_path / "settings.json")
        )


def test_save_rejects_invalid_settings_and_leaves_file_unchanged(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(valid_settings(), str(path))
    before = path.read_bytes()

    with pytest.raises(SettingsError):
        save_settings(valid_settings(port="bogus"), str(path))

    assert path.read_bytes() == before


# --------------------------------------------------------------------------
# legacy keys are dropped, never migrated


def test_legacy_keys_are_ignored_and_never_migrated(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"remote_enabled": True, "auto_start_rpc": True}),
        encoding="utf-8",
    )

    settings = load_settings(str(path))

    # Legacy automatic RPC startup must not become v2 auto-start consent.
    assert settings["auto_start"] is False
    assert "remote_enabled" not in settings
    assert "auto_start_rpc" not in settings
    assert settings["token"]

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "remote_enabled" not in on_disk
    assert "auto_start_rpc" not in on_disk
    assert on_disk == settings


def test_legacy_keys_alongside_valid_settings_are_dropped(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({**valid_settings(port=4321), "remote_enabled": True}),
        encoding="utf-8",
    )
    settings = load_settings(str(path))
    assert settings["port"] == 4321
    assert "remote_enabled" not in settings


# --------------------------------------------------------------------------
# extracted IP parsing


def test_validate_allowed_ips_accepts_and_reports():
    valid, errors = validate_allowed_ips("127.0.0.1, 10.0.0.0/8, ::1/128")
    assert valid == ["127.0.0.1", "10.0.0.0/8", "::1/128"]
    assert errors == []

    valid, errors = validate_allowed_ips("nope, 127.0.0.1")
    assert valid == ["127.0.0.1"]
    assert len(errors) == 1

    valid, errors = validate_allowed_ips("   ")
    assert valid == []
    assert errors

    valid, errors = validate_allowed_ips("127.0.0.1,,10.0.0.1")
    assert valid == []
    assert errors


def test_parse_allowed_networks_membership():
    networks = parse_allowed_networks("127.0.0.0/8, 192.168.1.7")
    assert ipaddress.ip_address("127.0.0.1") in networks[0]
    assert ipaddress.ip_address("192.168.1.7") in networks[1]
    assert not any(ipaddress.ip_address("10.1.2.3") in network for network in networks)
    assert not any(ipaddress.ip_address("::1") in network for network in networks)


def test_parse_allowed_networks_raises_listing_all_errors():
    with pytest.raises(ValueError) as excinfo:
        parse_allowed_networks("999.1.2.3, also-bad")
    assert "999.1.2.3" in str(excinfo.value)
    assert "also-bad" in str(excinfo.value)
