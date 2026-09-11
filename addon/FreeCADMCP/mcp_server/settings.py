"""Persistence of embedded MCP server settings.

Schema (v2): ``port``, ``token``, ``auto_start``, ``remote_enabled``,
``allowed_ips``, ``allowed_roots`` stored as JSON under FreeCAD's user app
data dir using the same filename as the legacy RPC server. FreeCAD is
imported lazily inside :func:`default_settings_path` so this module stays
GUI-independent until the default path is actually resolved; tests pass an
explicit path instead.

Failures are strict: an unreadable or invalid settings file raises
:class:`SettingsError` so startup fails closed instead of silently weakening
access restrictions. The legacy ``auto_start_rpc`` key is removed and is
never interpreted as v2 auto-start consent. Secrets are written atomically
(temp file + ``os.replace``) with user-only permissions.
"""

import json
import os
import secrets
import tempfile

from .ip_parse import validate_allowed_ips

SETTINGS_FILENAME = "freecad_mcp_settings.json"

DEFAULT_PORT = 9876
DEFAULT_ALLOWED_IPS = ""

LEGACY_KEYS = frozenset({"auto_start_rpc"})
_SETTINGS_KEYS = frozenset(
    {
        "port",
        "token",
        "auto_start",
        "remote_enabled",
        "allowed_ips",
        "allowed_roots",
        "recovery_enabled",
        "recovery_directory",
        "allow_scripts",
    }
)


class SettingsError(Exception):
    """Raised when settings are unreadable or invalid; callers must fail."""


def default_settings_path():
    """Return the settings path under FreeCAD's user app data dir.

    Imports FreeCAD lazily so importing this module never requires FreeCAD.
    """
    from FreeCAD import getUserAppDataDir

    return os.path.join(getUserAppDataDir(), SETTINGS_FILENAME)


def _default_allowed_roots():
    return [os.path.abspath(os.path.expanduser("~"))]


def _normalize_settings(raw, *, generate_token):
    """Validate a raw settings mapping and return the normalized dict.

    ``token`` is optional: local-only mode needs none. When
    ``remote_enabled`` is set and the token is missing, a missing token is
    generated (``generate_token=True``) or rejected (``False``).
    """
    if not isinstance(raw, dict):
        raise SettingsError("Settings must be a JSON object.")

    unknown = sorted(set(raw) - _SETTINGS_KEYS - LEGACY_KEYS)
    if unknown:
        raise SettingsError(f"Unknown settings keys: {', '.join(unknown)}")

    port = raw.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise SettingsError(f"Invalid port: {port!r}")

    remote_enabled = raw.get("remote_enabled", False)
    if not isinstance(remote_enabled, bool):
        raise SettingsError(f"Invalid remote_enabled: {remote_enabled!r}")

    token = raw.get("token")
    if token is None:
        token = ""
    if not isinstance(token, str):
        raise SettingsError(f"Invalid token: {token!r}")
    if not token.strip():
        token = ""
    if remote_enabled and not token:
        if generate_token:
            token = secrets.token_urlsafe(32)
        else:
            raise SettingsError("remote_enabled requires a token; none is set.")

    auto_start = raw.get("auto_start", False)
    if not isinstance(auto_start, bool):
        raise SettingsError(f"Invalid auto_start: {auto_start!r}")

    allowed_ips = raw.get("allowed_ips", DEFAULT_ALLOWED_IPS)
    if not isinstance(allowed_ips, str):
        raise SettingsError(f"Invalid allowed_ips: {allowed_ips!r}")
    _, ip_errors = validate_allowed_ips(allowed_ips)
    if ip_errors:
        raise SettingsError("Invalid allowed_ips: " + "; ".join(ip_errors))

    roots = raw.get("allowed_roots", _default_allowed_roots())
    if not isinstance(roots, list):
        raise SettingsError("Invalid allowed_roots: must be a list of directory paths.")
    normalized_roots = []
    for root in roots:
        if not isinstance(root, str) or not root.strip():
            raise SettingsError(f"Invalid allowed_roots entry: {root!r}")
        expanded = os.path.abspath(os.path.expanduser(root))
        if expanded not in normalized_roots:
            normalized_roots.append(expanded)

    recovery_enabled = raw.get("recovery_enabled", False)
    if not isinstance(recovery_enabled, bool):
        raise SettingsError(f"Invalid recovery_enabled: {recovery_enabled!r}")

    recovery_directory = raw.get("recovery_directory", "")
    if not isinstance(recovery_directory, str):
        raise SettingsError(f"Invalid recovery_directory: {recovery_directory!r}")
    if recovery_enabled:
        if not recovery_directory.strip():
            raise SettingsError("recovery_enabled requires a non-empty recovery_directory.")
        expanded_directory = os.path.expanduser(recovery_directory)
        if not os.path.isabs(expanded_directory):
            # Tested before expansion: ``abspath`` would silently resolve a
            # relative setting against the current directory, which differs
            # between the settings dialog and the FreeCAD process.
            raise SettingsError("recovery_directory must be an absolute path.")

    allow_scripts = raw.get("allow_scripts", False)
    if not isinstance(allow_scripts, bool):
        raise SettingsError(f"Invalid allow_scripts: {allow_scripts!r}")

    return {
        "port": port,
        "token": token,
        "auto_start": auto_start,
        "remote_enabled": remote_enabled,
        "allowed_ips": allowed_ips.strip(),
        "allowed_roots": normalized_roots,
        "recovery_enabled": recovery_enabled,
        "recovery_directory": recovery_directory,
        "allow_scripts": allow_scripts,
    }


def load_settings(path=None):
    """Load settings from ``path`` (default: FreeCAD user app data dir).
    A missing file is bootstrapped: defaults (local-only, no token) are
    persisted atomically. When loaded settings enable remote access without
    a token, one is generated and persisted. Unreadable or invalid content
    raises :class:`SettingsError`. Legacy keys are dropped and defaults are
    filled in; the rewritten file is persisted so restarts observe a stable
    schema.
    """
    if path is None:
        path = default_settings_path()
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        settings = _normalize_settings({}, generate_token=True)
        save_settings(settings, path)
        return settings
    except (OSError, ValueError) as exc:
        raise SettingsError(f"Cannot read settings file {path!r}: {exc}") from exc

    settings = _normalize_settings(raw, generate_token=True)
    if raw != settings:
        # Persist added defaults / dropped legacy keys for restart stability.
        save_settings(settings, path)
    return settings


def save_settings(settings, path=None):
    """Validate and atomically persist ``settings`` with user-only permissions."""
    if path is None:
        path = default_settings_path()
    normalized = _normalize_settings(settings, generate_token=False)

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    payload = json.dumps(normalized, indent=2)

    fd, tmp_path = tempfile.mkstemp(prefix=".freecad_mcp_settings-", dir=directory)
    fd_open = True
    try:
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd_open = False
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except OSError as exc:
            raise SettingsError(f"Cannot write settings file {path!r}: {exc}") from exc
    finally:
        if fd_open:
            # fdopen never took ownership; avoid leaking the descriptor.
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
