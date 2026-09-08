"""Native workbench commands for the embedded MCP v2 add-on.

Six commands with native icons, one GUI-thread :class:`McpUiController`
that owns the permanent status indicator and refreshes the six actions,
and a Connection Details dialog. Command state derives from ONE pure
mapping (:func:`_indicator_state`) shared with the controller, Start and
Stop show transient status-bar messages plus detailed Report-view output,
and failures surface through native warning dialogs for user-triggered
actions. Tokens never appear in status snapshots, tooltips or logs.
"""

import os
import secrets

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtGui, QtWidgets

from mcp_server import server as mcp_server_module
from mcp_server.settings import SettingsError, load_settings, save_settings
from mcp_server.ip_parse import validate_allowed_ips

_COMMAND_NAMES = (
    "Start_MCP_Server",
    "Stop_MCP_Server",
    "Toggle_Auto_Start",
    "Toggle_Remote_Connections",
    "Configure_Allowed_IPs",
    "Show_Auth_Token",
)

_TOGGLE_ACTIONS = (
    ("Toggle_Auto_Start", "auto_start"),
    ("Toggle_Remote_Connections", "remote_enabled"),
)

_TOGGLE_KEYS = {"Toggle_Auto_Start": "auto_start", "Toggle_Remote_Connections": "remote_enabled"}

_STATUSBAR_MESSAGE_MS = 5000

_ICONS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "Resources", "icons")
)


def _icon_path(name: str) -> str:
    return os.path.join(_ICONS_DIR, name)


def _tr(text: str) -> str:
    """Translate with the add-on's Qt translation context."""
    return QtWidgets.QApplication.translate("FreeCADMCP", text)


def _main_window():
    try:
        return FreeCADGui.getMainWindow()
    except Exception:
        return None


def _show_status_bar(message: str) -> None:
    """Transient native status-bar message (Report view keeps the detail)."""

    window = _main_window()
    if window is not None:
        try:
            window.statusBar().showMessage(message, _STATUSBAR_MESSAGE_MS)
            return
        except Exception:
            pass
    FreeCAD.Console.PrintMessage(f"[MCP] {message}\n")


def _report_message(message: str) -> None:
    FreeCAD.Console.PrintMessage(f"{message}\n")


def _report_error(message: str) -> None:
    FreeCAD.Console.PrintError(f"{message}\n")


def _warn(message: str) -> None:
    window = _main_window()
    QtWidgets.QMessageBox.warning(window, "FreeCAD MCP", message)


def _indicator_state(status: dict) -> dict:
    """Pure mapping from one server status to indicator and action state.

    Shared by the controller and the command ``IsActive`` implementations
    so both can never disagree. Start is available only for a fully
    stopped server with zero retained operations; Stop only while
    running; both are unavailable during starting and draining.
    """

    state = status.get("state")
    pending = int(status.get("pendingOperations") or 0)
    gui = status.get("gui") or {}
    connection = status.get("connection") or {}
    remote = bool(connection.get("remote_enabled"))
    if state == "running":
        if gui.get("state") == "stuck":
            text = "MCP: Running — GUI blocked"
        elif remote:
            text = "MCP: Running (Remote)"
        else:
            text = "MCP: Running (Local)"
    elif state == "starting":
        text = "MCP: Starting"
    elif state == "draining":
        text = f"MCP: Stopping ({pending} operations)"
    else:
        text = "MCP: Stopped"
    return {
        "text": text,
        "start_enabled": state == "stopped" and pending == 0,
        "stop_enabled": state == "running",
    }


def _restart_required(saved: dict | None, status: dict) -> bool:
    """True when saved connection settings differ from the active ones."""

    if saved is None:
        return False
    connection = status.get("connection") or {}
    active = status.get("state") in ("running", "starting", "draining")
    if not active:
        return False
    return (
        bool(saved.get("remote_enabled", False)) != bool(connection.get("remote_enabled"))
        or str(saved.get("allowed_ips", "")) != str(connection.get("allowed_ips", ""))
    )


class McpUiController(QtCore.QObject):
    """Single GUI-thread owner of the status indicator and action state.

    Polls only actual server state every 500 ms; failures already have
    their own native dialog/status message, so no error state is cached
    here. Saved settings are loaded on initialization, after a successful
    command save and when Connection Details opens — never in the timer.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = self._load_settings()
        self._button = self._build_indicator()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        # Paint the indicator immediately instead of waiting one tick.
        self.refresh()

    # -- settings ---------------------------------------------------------

    @staticmethod
    def _load_settings():
        try:
            return load_settings()
        except SettingsError:
            return None

    def settings_changed(self) -> None:
        """Re-read saved settings (after a successful command save)."""

        self._settings = self._load_settings()
        self.refresh()

    # -- indicator ---------------------------------------------------------

    def _build_indicator(self):
        window = _main_window()
        if window is None:
            return None
        button = QtWidgets.QToolButton(window.statusBar())
        button.setObjectName("FreeCADMCPStatus")
        button.setAutoRaise(True)
        button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        button.setAccessibleName("FreeCAD MCP server status")
        button.setIcon(QtGui.QIcon(_icon_path("mcp-workbench.svg")))
        button.clicked.connect(self._open_connection_details)
        window.statusBar().addPermanentWidget(button)
        return button

    def _open_connection_details(self):
        # Invoke the existing command so the dialog works even before the
        # workbench has registered anything special for the button.
        try:
            FreeCADGui.runCommand("Show_Auth_Token")
        except Exception:
            ShowAuthTokenCommand().Activated()

    def _tooltip(self, status: dict) -> str:
        connection = status.get("connection") or {}
        gui = status.get("gui") or {}
        bind_address = "0.0.0.0" if connection.get("remote_enabled") else "127.0.0.1"
        lines = [
            f"Endpoint: {status.get('endpoint') or 'not listening'}",
            f"Bind address: {bind_address}",
            f"Dispatch health: {gui.get('state', 'unknown')}",
            f"Pending operations: {int(status.get('pendingOperations') or 0)}",
        ]
        if _restart_required(self._settings, status):
            lines.append("Restart required to apply saved connection settings")
        return "\n".join(lines)

    # -- refresh -------------------------------------------------------------

    def refresh(self) -> None:
        """One bounded pass: poll state, then set each action if different.

        QAction handles are resolved fresh every pass (no caching of
        destroyed widgets); programmatic checks are signal-blocked.
        """

        status = mcp_server_module.server_status()
        state_map = _indicator_state(status)
        if self._button is not None:
            self._button.setText(state_map["text"])
            self._button.setToolTip(self._tooltip(status))
        window = _main_window()
        if window is None:
            return
        enabled = {
            "Start_MCP_Server": state_map["start_enabled"],
            "Stop_MCP_Server": state_map["stop_enabled"],
        }
        for action in window.findChildren(QtGui.QAction):
            name = action.objectName()
            if name in enabled:
                if action.isEnabled() != enabled[name]:
                    action.setEnabled(enabled[name])
            elif name in _TOGGLE_KEYS:
                if self._settings is None:
                    if action.isEnabled():
                        action.setEnabled(False)
                    continue
                if not action.isCheckable():
                    action.setCheckable(True)
                checked = bool(self._settings.get(_TOGGLE_KEYS[name], False))
                if not action.isEnabled():
                    action.setEnabled(True)
                if action.isChecked() != checked:
                    with QtCore.QSignalBlocker(action):
                        action.setChecked(checked)

    def shutdown(self) -> None:
        """Stop polling; the indicator dies with its parent window."""

        if self._timer is not None:
            self._timer.stop()


_controller = None


def initialize_ui():
    """Idempotently create the UI controller; ``None`` without a main window.

    Called from the deferred startup callback before the auto-start check
    and again from workbench Initialize after toolbar/menu construction.
    """

    global _controller
    if _controller is not None:
        return _controller
    window = _main_window()
    if window is None:
        return None
    try:
        _controller = McpUiController(window)
        application = QtCore.QCoreApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(_controller.shutdown)
    except Exception:
        _controller = None
        return None
    return _controller


def _controller_refresh():
    controller = initialize_ui()
    if controller is not None:
        controller.refresh()


def _controller_settings_changed():
    controller = initialize_ui()
    if controller is not None:
        controller.settings_changed()


# ---------------------------------------------------------------------------
# Start / Stop.
# ---------------------------------------------------------------------------


class StartMCPServerCommand:
    def GetResources(self):
        return {
            "MenuText": "Start MCP Server",
            "ToolTip": "Start the embedded MCP server",
            "Pixmap": _icon_path("mcp-start.svg"),
        }

    def Activated(self):
        try:
            status = mcp_server_module.start_server()
        except (SettingsError, RuntimeError, OSError) as exc:
            _report_error(f"[MCP] Start failed: {exc}")
            _warn(f"Starting the MCP server failed:\n{exc}")
            _controller_refresh()
            return
        state = status.get("state")
        endpoint = status.get("endpoint")
        if state == "running":
            _show_status_bar(f"MCP server running at {endpoint}")
            _report_message(f"[MCP] Server started at {endpoint}")
        else:
            _show_status_bar(f"MCP server state: {state}")
            _report_message(f"[MCP] Server start returned state {state}")
        _controller_refresh()

    def IsActive(self):
        return _indicator_state(mcp_server_module.server_status())["start_enabled"]


class StopMCPServerCommand:
    def GetResources(self):
        return {
            "MenuText": "Stop MCP Server",
            "ToolTip": "Stop the embedded MCP server",
            "Pixmap": _icon_path("mcp-stop.svg"),
        }

    def Activated(self):
        before = mcp_server_module.server_status()
        if not before.get("running"):
            result = mcp_server_module.stop_server()
            _show_status_bar(f"MCP server is not running (state: {result.get('state')})")
            _report_message(
                "[MCP] Server is not running (state: %s).\n" % result.get("state")
            )
            _controller_refresh()
            return
        try:
            result = mcp_server_module.stop_server()
        except (SettingsError, RuntimeError, OSError) as exc:
            _report_error(f"[MCP] Stop failed: {exc}")
            _warn(f"Stopping the MCP server failed:\n{exc}")
            _controller_refresh()
            return
        state = result.get("state")
        pending = int(result.get("pendingOperations") or 0)
        if state == "draining" and pending:
            message = f"MCP is stopping; {pending} operations are still active."
        elif state == "draining":
            message = "MCP is stopping."
        else:
            message = "MCP server stopped."
        _show_status_bar(message)
        _report_message(
            "[MCP] Server stop requested (state: %s, pending operations: %s)"
            % (state, pending)
        )
        _controller_refresh()

    def IsActive(self):
        return _indicator_state(mcp_server_module.server_status())["stop_enabled"]


# ---------------------------------------------------------------------------
# Settings toggles.
# ---------------------------------------------------------------------------


class ToggleAutoStartCommand:
    def GetResources(self):
        try:
            auto_start = bool(load_settings().get("auto_start", False))
        except SettingsError:
            auto_start = False
        return {
            "MenuText": "Auto-Start Server",
            "ToolTip": "Automatically start the MCP server when FreeCAD launches.",
            "Checkable": auto_start,
            "Pixmap": _icon_path("mcp-autostart.svg"),
        }

    def Activated(self, checked=0):
        try:
            settings = load_settings()
            settings["auto_start"] = bool(checked)
            save_settings(settings)
        except SettingsError as exc:
            _report_error(f"[MCP] Cannot change auto-start: {exc}")
            _warn(f"Changing auto-start failed:\n{exc}\nThe saved setting is unchanged.")
            _controller_settings_changed()
            return
        _controller_settings_changed()
        if settings["auto_start"]:
            _report_message(
                "[MCP] Server will start automatically on next FreeCAD launch."
            )
        else:
            _report_message("[MCP] Auto-start disabled.")

    def IsActive(self):
        return True


class ToggleRemoteConnectionsCommand:
    def GetResources(self):
        try:
            remote = bool(load_settings().get("remote_enabled", False))
        except SettingsError:
            remote = False
        return {
            "MenuText": "Remote Connections",
            "ToolTip": "Enable or disable non-loopback connections to the MCP server.",
            "Checkable": remote,
            "Pixmap": _icon_path("mcp-remote.svg"),
        }

    def Activated(self, checked=0):
        try:
            settings = load_settings()
            settings["remote_enabled"] = bool(checked)
            if checked and not settings.get("token"):
                settings["token"] = secrets.token_urlsafe(32)
            save_settings(settings)
        except SettingsError as exc:
            _report_error(f"[MCP] Cannot change remote access: {exc}")
            _warn(f"Changing remote access failed:\n{exc}\nThe saved setting is unchanged.")
            _controller_settings_changed()
            return
        _controller_settings_changed()
        if settings["remote_enabled"]:
            _report_message(
                "[MCP] Remote connections enabled. Clients must present the "
                "bearer token (see Connection Details). Allowed IPs: "
                f"{settings['allowed_ips'] or '(any host - open)'}\n"
            )
        else:
            _report_message("[MCP] Remote connections disabled.\n")
        if mcp_server_module.server_status().get("running"):
            _report_message(
                "[MCP] Restart the MCP server for changes to take effect."
            )

    def IsActive(self):
        return True


class ConfigureAllowedIPsCommand:
    def GetResources(self):
        return {
            "MenuText": "Configure Allowed IPs",
            "ToolTip": "Set which IP addresses or subnets may connect to the MCP server.",
            "Pixmap": _icon_path("mcp-allowed-ips.svg"),
        }

    def Activated(self):
        try:
            settings = load_settings()
        except SettingsError as exc:
            _report_error(f"[MCP] Cannot load settings: {exc}")
            _warn(f"Loading the saved settings failed:\n{exc}")
            return
        current_ips = settings.get("allowed_ips", "")
        text, ok = QtWidgets.QInputDialog.getText(
            None,
            "Allowed IP Addresses",
            "Enter allowed IP addresses or subnets (comma-separated).\n"
            "Leave empty to allow any host (the bearer token stays required).\n"
            "Examples: 192.168.1.0/24, 10.0.0.5",
            QtWidgets.QLineEdit.Normal,
            current_ips,
        )
        if not ok:
            _report_message("[MCP] Allowed IPs not changed.")
            return
        if not text.strip():
            settings["allowed_ips"] = ""
            try:
                save_settings(settings)
            except SettingsError as exc:
                _report_error(f"[MCP] Cannot save allowed IPs: {exc}")
                _warn(f"Saving the allowed IPs failed:\n{exc}\nSettings are unchanged.")
                _controller_settings_changed()
                return
            _report_message(
                "[MCP] Allowed IPs cleared: any host may connect "
                "(token still required).\n"
            )
            _controller_settings_changed()
            if mcp_server_module.server_status().get("running"):
                _report_message(
                    "[MCP] Restart the MCP server for changes to take effect."
                )
            return
        valid, errors = validate_allowed_ips(text.strip())
        if errors:
            QtWidgets.QMessageBox.warning(
                None,
                "Invalid IP Configuration",
                "The following errors were found:\n\n"
                + "\n".join(f"- {e}" for e in errors)
                + ("\n\nOnly valid entries will be saved."
                   if valid else "\n\nNo valid entries found. Settings not changed."),
            )
        if not valid:
            _report_warning("[MCP] Allowed IPs not changed - no valid entries.")
            return
        settings["allowed_ips"] = ", ".join(valid)
        try:
            save_settings(settings)
        except SettingsError as exc:
            _report_error(f"[MCP] Cannot save allowed IPs: {exc}")
            _warn(f"Saving the allowed IPs failed:\n{exc}\nSettings are unchanged.")
            _controller_settings_changed()
            return
        _report_message(f"[MCP] Allowed IPs updated to: {settings['allowed_ips']}")
        _controller_settings_changed()
        if mcp_server_module.server_status().get("running"):
            _report_message(
                "[MCP] Restart the MCP server for changes to take effect."
            )

    def IsActive(self):
        return True


def _report_warning(message: str) -> None:
    FreeCAD.Console.PrintWarning(f"{message}\n")


# ---------------------------------------------------------------------------
# Connection Details.
# ---------------------------------------------------------------------------


def _connection_details(status: dict, saved: dict | None) -> dict:
    """Pure rows model for the Connection Details dialog.

    The copyable endpoint is always the local loopback form: a remote
    wildcard listener maps to ``http://127.0.0.1:<actual port>/mcp``; the
    wildcard address is only ever shown as the bind address. Tokens are
    NOT part of the model — the dialog handles them separately and they
    never reach status snapshots or tooltips.
    """

    state = status.get("state")
    connection = status.get("connection") or {}
    remote_active = bool(connection.get("remote_enabled"))
    actual_port = status.get("port")
    configured_port = connection.get("configured_port")
    running = state in ("running", "starting", "draining")
    if running and actual_port:
        endpoint = (
            f"http://127.0.0.1:{actual_port}/mcp"
            if remote_active
            else str(status.get("endpoint"))
        )
        listening = True
    else:
        endpoint = (
            f"http://127.0.0.1:{configured_port}/mcp"
            if configured_port
            else None
        )
        listening = False
    mode = "Remote" if remote_active else "Local"
    return {
        "state": state or "stopped",
        "mode": mode,
        "bind_address": "0.0.0.0" if remote_active else "127.0.0.1",
        "allowed_ips": str(connection.get("allowed_ips", "") or "(any host - open)"),
        "endpoint": endpoint,
        "listening": listening,
        "endpoint_copyable": bool(listening or configured_port),
        "configured_port": configured_port,
        "restart_required": _restart_required(saved, status),
    }


class ShowAuthTokenCommand:
    def GetResources(self):
        return {
            "MenuText": "Connection Details",
            "ToolTip": "Show the MCP endpoint and bearer token for connecting clients.",
            "Pixmap": _icon_path("mcp-connection.svg"),
        }

    def Activated(self):
        # Saved settings are re-loaded every time the dialog opens, so
        # external file changes are reflected without a watcher.
        try:
            settings = load_settings()
        except SettingsError as exc:
            _report_error(f"[MCP] Cannot load settings: {exc}")
            _warn(f"Loading the saved settings failed:\n{exc}")
            return
        saved = settings

        status = mcp_server_module.server_status()
        # The ACTIVE server's settings hold the running token; it is used
        # only inside this dialog and never enters status snapshots.
        active_server = mcp_server_module.get_server()
        active_settings = (
            dict(active_server.settings) if active_server is not None else None
        )
        local_only = not bool(
            (status.get("connection") or {}).get("remote_enabled")
        )
        if active_settings is not None and not local_only and status.get("running"):
            token = str(active_settings.get("token") or "")
        else:
            token = str(settings.get("token") or "")

        model = _connection_details(status, saved)

        parent = _main_window()
        dialog = QtWidgets.QDialog(parent)
        dialog.setWindowTitle(_tr("FreeCAD MCP — Connection Details"))
        dialog.setMinimumWidth(480)

        endpoint_field = QtWidgets.QLineEdit(model["endpoint"] or "", dialog)
        endpoint_field.setReadOnly(True)
        if not model["endpoint"]:
            endpoint_field.setPlaceholderText("Configured endpoint (not listening)")

        bind_field = QtWidgets.QLineEdit(model["bind_address"], dialog)
        bind_field.setReadOnly(True)
        mode_field = QtWidgets.QLineEdit(model["mode"], dialog)
        mode_field.setReadOnly(True)
        state_field = QtWidgets.QLineEdit(str(model["state"]), dialog)
        state_field.setReadOnly(True)
        allowed_field = QtWidgets.QLineEdit(model["allowed_ips"], dialog)
        allowed_field.setReadOnly(True)

        token_field = QtWidgets.QLineEdit(dialog)
        token_field.setReadOnly(True)
        token_field.setEchoMode(QtWidgets.QLineEdit.Password)
        if local_only:
            token_field.setText(_tr("(not required - local connections only)"))
        else:
            token_field.setText(token)

        reveal = QtWidgets.QCheckBox(_tr("Reveal"), dialog)
        reveal.setChecked(False)

        def _set_reveal(checked: bool) -> None:
            token_field.setEchoMode(
                QtWidgets.QLineEdit.Normal if checked else QtWidgets.QLineEdit.Password
            )

        reveal.toggled.connect(_set_reveal)

        copy_endpoint = QtWidgets.QPushButton(_tr("Copy Endpoint"), dialog)
        copy_endpoint.setEnabled(bool(model["endpoint_copyable"] and model["endpoint"]))

        def _copy_endpoint():
            QtWidgets.QApplication.clipboard().setText(endpoint_field.text())
            copy_endpoint.setText(_tr("Copied"))

        copy_endpoint.clicked.connect(_copy_endpoint)

        copy_token = QtWidgets.QPushButton(_tr("Copy Token"), dialog)
        copy_token.setEnabled(bool(token) and not local_only)

        def _copy_token():
            QtWidgets.QApplication.clipboard().setText(token_field.text())
            copy_token.setText(_tr("Copied"))

        copy_token.clicked.connect(_copy_token)

        restart_note = QtWidgets.QLabel(dialog)
        if model["restart_required"]:
            restart_note.setText(
                _tr("Restart required to apply saved connection settings.")
            )
        lan_note = None
        if model["mode"] == "Remote":
            lan_note = QtWidgets.QLabel(dialog)
            lan_note.setText(
                _tr(
                    "Remote clients connect to this computer's LAN address; "
                    "the endpoint above is this machine's local loopback form."
                )
            )

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Close, parent=dialog
        )
        buttons.rejected.connect(dialog.reject)

        layout = QtWidgets.QFormLayout(dialog)
        layout.addRow(_tr("State:"), state_field)
        layout.addRow(_tr("Connection mode:"), mode_field)
        layout.addRow(_tr("Endpoint:"), endpoint_field)
        layout.addRow(_tr("Bind address:"), bind_field)
        layout.addRow(_tr("Allowed IPs:"), allowed_field)
        layout.addRow(_tr("Token:"), token_field)
        layout.addRow("", reveal)
        buttons_row = QtWidgets.QHBoxLayout()
        buttons_row.addWidget(copy_endpoint)
        buttons_row.addWidget(copy_token)
        buttons_row.addStretch(1)
        layout.addRow(buttons_row)
        if model["restart_required"]:
            layout.addRow(restart_note)
        if lan_note is not None:
            layout.addRow(lan_note)
        layout.addRow(buttons)

        # Keyboard tab order follows the form.
        QtWidgets.QWidget.setTabOrder(endpoint_field, bind_field)
        QtWidgets.QWidget.setTabOrder(bind_field, mode_field)
        QtWidgets.QWidget.setTabOrder(mode_field, state_field)
        QtWidgets.QWidget.setTabOrder(state_field, allowed_field)
        QtWidgets.QWidget.setTabOrder(allowed_field, token_field)
        QtWidgets.QWidget.setTabOrder(token_field, reveal)
        QtWidgets.QWidget.setTabOrder(reveal, copy_endpoint)
        QtWidgets.QWidget.setTabOrder(copy_endpoint, copy_token)

        dialog.exec()


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------

_REGISTERED = False


def register_commands() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    FreeCADGui.addCommand("Start_MCP_Server", StartMCPServerCommand())
    FreeCADGui.addCommand("Stop_MCP_Server", StopMCPServerCommand())
    FreeCADGui.addCommand("Toggle_Auto_Start", ToggleAutoStartCommand())
    FreeCADGui.addCommand("Toggle_Remote_Connections", ToggleRemoteConnectionsCommand())
    FreeCADGui.addCommand("Configure_Allowed_IPs", ConfigureAllowedIPsCommand())
    FreeCADGui.addCommand("Show_Auth_Token", ShowAuthTokenCommand())
    _REGISTERED = True
