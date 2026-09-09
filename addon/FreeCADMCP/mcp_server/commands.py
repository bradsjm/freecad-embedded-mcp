"""Native workbench commands for the embedded MCP v2 add-on.

Three commands with native icons: one lifecycle toggle
(:class:`ToggleMCPServerCommand`) whose text, icon and availability change
with the confirmed server state, the :class:`ConnectionDetailsCommand`
dialog, and the :class:`MCPSettingsCommand` dialog. One GUI-thread
:class:`McpUiController` owns the permanent status indicator, refreshes the
toggle action from the shared pure mapping, and its button opens only
Connection Details. Start and stop show transient status-bar messages plus
detailed Report-view output, and failures surface through native warning
dialogs for user-triggered actions. Tokens never appear in status
snapshots, the settings dialog, tooltips or logs.
"""

import os
import secrets

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtGui, QtWidgets

from mcp_server import server as mcp_server_module
from mcp_server.ip_parse import validate_allowed_ips
from mcp_server.settings import SettingsError, load_settings, save_settings

_TOGGLE_COMMAND = "Toggle_MCP_Server"
_DETAILS_COMMAND = "Connection_Details"
_SETTINGS_COMMAND = "MCP_Settings"

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

    Shared by the controller and the toggle command so both can never
    disagree. Start is available only for a fully stopped server with
    zero retained operations; Stop only while running; both are
    unavailable during starting and draining, and the action copy always
    matches the confirmed server state.
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
            text = "MCP: Running (Network enabled)"
        else:
            text = "MCP: Running (Local only)"
    elif state == "starting":
        text = "MCP: Starting"
    elif state == "draining":
        text = f"MCP: Stopping ({pending} operations)"
    elif state == "stopped":
        text = "MCP: Stopped"
    else:
        text = "MCP: State unknown"
    if state == "running":
        action_text = "Stop MCP Server"
        action_tooltip = "Stop the embedded MCP server"
        action_icon = "mcp-stop.svg"
    elif state == "starting":
        action_text = "Starting MCP Server…"
        action_tooltip = "The MCP server is starting; wait for it to finish"
        action_icon = "mcp-start.svg"
    elif state == "draining":
        action_text = "Stopping MCP Server…"
        action_tooltip = f"MCP is stopping ({pending} operations still active)"
        action_icon = "mcp-stop.svg"
    elif state == "stopped":
        action_text = "Start MCP Server"
        action_tooltip = "Start the embedded MCP server"
        action_icon = "mcp-start.svg"
        if pending:
            action_tooltip = f"Waiting for {pending} retained operations to finish"
    else:
        action_text = "MCP Server State Unknown"
        action_tooltip = "The MCP server state cannot be determined"
        action_icon = "mcp-workbench.svg"
    start_enabled = state == "stopped" and pending == 0
    stop_enabled = state == "running"
    return {
        "text": text,
        "start_enabled": start_enabled,
        "stop_enabled": stop_enabled,
        "action_text": action_text,
        "action_tooltip": action_tooltip,
        "action_icon": action_icon,
        "action_enabled": start_enabled or stop_enabled,
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
        or saved.get("port") != connection.get("configured_port")
    )


class McpUiController(QtCore.QObject):
    """Single GUI-thread owner of the status indicator and action state.

    Polls only actual server state every 500 ms; failures already have
    their own native dialog/status message, so no error state is cached
    here. The indicator button opens Connection Details. Saved settings
    are loaded on initialization, after a successful settings save and
    when Connection Details opens — never in the timer.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = self._load_settings()
        self._toggle_action = None
        self._toggle_icon = None
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
        """Re-read saved settings (after a successful settings save)."""

        self._settings = self._load_settings()
        self.refresh()

    # -- indicator ---------------------------------------------------------

    def _build_indicator(self):
        window = _main_window()
        if window is None:
            return None
        button = QtWidgets.QToolButton(window.statusBar())
        button.setObjectName("FreeCAD MCP Status")
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
            FreeCADGui.runCommand(_DETAILS_COMMAND)
        except Exception:
            ConnectionDetailsCommand().Activated()

    def _tooltip(self, status: dict) -> str:
        connection = status.get("connection") or {}
        gui = status.get("gui") or {}
        remote = bool(connection.get("remote_enabled"))
        bind_address = "0.0.0.0" if remote else "127.0.0.1"
        lines = [
            f"Endpoint: {status.get('endpoint') or 'not listening'}",
            f"Bind address: {bind_address}",
            f"Access: {'Network enabled' if remote else 'Local only'}",
            f"Dispatch health: {gui.get('state', 'unknown')}",
            f"Pending operations: {int(status.get('pendingOperations') or 0)}",
        ]
        if _restart_required(self._settings, status):
            lines.append("Restart required to apply saved connection settings")
        return "\n".join(lines)

    # -- refresh -------------------------------------------------------------

    def refresh(self) -> None:
        """One bounded pass: poll state, then sync indicator and action.

        QAction handles are resolved fresh every pass (no caching of
        destroyed widgets); the toggle action's text, icon, tooltip and
        availability are re-derived from the pure mapping every time.
        """

        status = mcp_server_module.server_status()
        state_map = _indicator_state(status)
        if self._button is not None:
            self._button.setText(state_map["text"])
            self._button.setToolTip(self._tooltip(status))
        window = _main_window()
        if window is None:
            return
        for action in window.findChildren(QtGui.QAction):
            if action.objectName() != _TOGGLE_COMMAND:
                continue
            if action is not self._toggle_action:
                # A rebuilt action starts from the GetResources defaults.
                self._toggle_action = action
                self._toggle_icon = None
            if action.text() != state_map["action_text"]:
                action.setText(state_map["action_text"])
            if state_map["action_icon"] != self._toggle_icon:
                action.setIcon(QtGui.QIcon(_icon_path(state_map["action_icon"])))
                self._toggle_icon = state_map["action_icon"]
            if action.toolTip() != state_map["action_tooltip"]:
                action.setToolTip(state_map["action_tooltip"])
            if action.isEnabled() != state_map["action_enabled"]:
                action.setEnabled(state_map["action_enabled"])

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
# Lifecycle toggle.
# ---------------------------------------------------------------------------


class ToggleMCPServerCommand:
    """One command for both directions: stop while running, start while fully stopped."""

    def GetResources(self):
        # Initial resources describe the stopped state; the controller
        # refresh rewrites text, icon, tooltip and availability from the
        # confirmed server state.
        return {
            "MenuText": "Start MCP Server",
            "ToolTip": "Start the embedded MCP server",
            "Pixmap": _icon_path("mcp-start.svg"),
        }

    def Activated(self):
        state_map = _indicator_state(mcp_server_module.server_status())
        if state_map["stop_enabled"]:
            self._stop()
        elif state_map["start_enabled"]:
            self._start()
        else:
            # The state moved between the last refresh and this click;
            # report truth instead of forcing an invalid transition.
            message = state_map["text"]
            _show_status_bar(message)
            _report_message(f"[MCP] No action taken: {message}")

    def _start(self):
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

    def _stop(self):
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
            f"[MCP] Server stop requested (state: {state}, pending operations: {pending})"
        )
        _controller_refresh()

    def IsActive(self):
        return _indicator_state(mcp_server_module.server_status())["action_enabled"]


# ---------------------------------------------------------------------------
# Connection Details.
# ---------------------------------------------------------------------------


def _connection_details(status: dict, saved: dict | None) -> dict:
    """Pure rows model for the Connection Details dialog.

    The copyable endpoint is always the local loopback form: a network
    wildcard listener maps to ``http://127.0.0.1:<actual port>/mcp``; the
    wildcard address is only ever shown as the bind address. Tokens are
    NOT part of the model — the dialog handles them separately and they
    never reach status snapshots or tooltips.
    """

    state = status.get("state")
    connection = status.get("connection") or {}
    network_active = bool(connection.get("remote_enabled"))
    actual_port = status.get("port")
    configured_port = connection.get("configured_port")
    running = state in ("running", "starting", "draining")
    if running and actual_port:
        endpoint = (
            f"http://127.0.0.1:{actual_port}/mcp" if network_active else str(status.get("endpoint"))
        )
        listening = True
    else:
        endpoint = f"http://127.0.0.1:{configured_port}/mcp" if configured_port else None
        listening = False
    mode = "Network enabled" if network_active else "Local only"
    return {
        "state": state or "stopped",
        "mode": mode,
        "bind_address": "0.0.0.0" if network_active else "127.0.0.1",
        "allowed_ips": str(connection.get("allowed_ips", "") or "(any host - open)"),
        "endpoint": endpoint,
        "listening": listening,
        "endpoint_copyable": bool(listening or configured_port),
        "configured_port": configured_port,
        "restart_required": _restart_required(saved, status),
    }


class ConnectionDetailsCommand:
    def GetResources(self):
        return {
            "MenuText": "Connection Details…",
            "ToolTip": (
                "Show the MCP endpoint, access mode and bearer token for connecting clients."
            ),
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
        active_settings = dict(active_server.settings) if active_server is not None else None
        local_only = not bool((status.get("connection") or {}).get("remote_enabled"))
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
            token_field.setText(_tr("(not required - Local only)"))
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
            restart_note.setText(_tr("Restart required to apply saved connection settings."))
        lan_note = None
        if model["mode"] == "Network enabled":
            lan_note = QtWidgets.QLabel(dialog)
            lan_note.setText(
                _tr(
                    "Network clients connect to this computer's LAN address; "
                    "the endpoint above is this machine's local loopback form."
                )
            )

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)

        layout = QtWidgets.QFormLayout(dialog)
        layout.addRow(_tr("State:"), state_field)
        layout.addRow(_tr("Access:"), mode_field)
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
# Settings.
# ---------------------------------------------------------------------------


class MCPSettingsCommand:
    def GetResources(self):
        return {
            "MenuText": "MCP Settings…",
            "ToolTip": "Configure the MCP server port, auto-start, network access, "
            "allowed IPs and allowed roots.",
            "Pixmap": _icon_path("mcp-workbench.svg"),
        }

    def Activated(self):
        try:
            settings = load_settings()
        except SettingsError as exc:
            _report_error(f"[MCP] Cannot load settings: {exc}")
            _warn(f"Loading the saved settings failed:\n{exc}")
            return

        parent = _main_window()
        dialog = QtWidgets.QDialog(parent)
        dialog.setWindowTitle(_tr("FreeCAD MCP — Settings"))
        dialog.setMinimumWidth(460)

        port_field = QtWidgets.QSpinBox(dialog)
        port_field.setRange(0, 65535)
        port_field.setValue(int(settings.get("port") or 0))

        auto_start = QtWidgets.QCheckBox(
            _tr("Start the server automatically when FreeCAD launches"), dialog
        )
        auto_start.setChecked(bool(settings.get("auto_start", False)))

        network = QtWidgets.QCheckBox(
            _tr("Allow network connections (non-loopback clients)"), dialog
        )
        network.setChecked(bool(settings.get("remote_enabled", False)))
        network_note = QtWidgets.QLabel(
            _tr("Network clients must present the bearer token shown in Connection Details."),
            dialog,
        )
        network_note.setWordWrap(True)
        network_note.setIndent(20)

        ips_field = QtWidgets.QLineEdit(str(settings.get("allowed_ips", "")), dialog)
        ips_field.setPlaceholderText(
            _tr("e.g. 192.168.1.0/24, 10.0.0.5 - empty allows any host (the token stays required)")
        )
        ips_error = QtWidgets.QLabel(dialog)
        ips_error.setWordWrap(True)
        ips_error.hide()

        roots_field = QtWidgets.QPlainTextEdit(dialog)
        roots_field.setPlaceholderText(_tr("One directory per line"))
        roots_field.setPlainText("\n".join(str(root) for root in settings.get("allowed_roots", [])))
        roots_error = QtWidgets.QLabel(dialog)
        roots_error.setWordWrap(True)
        roots_error.hide()

        outcome = {}

        def _set_error(label, message: str) -> None:
            label.setText(message)
            label.setVisible(bool(message))

        def _save() -> None:
            _set_error(ips_error, "")
            _set_error(roots_error, "")
            valid_ips, ip_errors = validate_allowed_ips(ips_field.text().strip())
            if ip_errors:
                _set_error(ips_error, "Allowed IPs: " + "; ".join(ip_errors))
                return
            roots = [line.strip() for line in roots_field.toPlainText().splitlines()]
            roots = [line for line in roots if line]
            if not roots:
                _set_error(roots_error, "Allowed roots: add at least one directory.")
                return
            saved = dict(settings)
            saved["port"] = port_field.value()
            saved["auto_start"] = auto_start.isChecked()
            saved["remote_enabled"] = network.isChecked()
            saved["allowed_ips"] = ", ".join(valid_ips)
            saved["allowed_roots"] = roots
            token_generated = False
            if saved["remote_enabled"] and not saved.get("token"):
                # save_settings rejects a missing token; generate one here
                # instead. It is never shown in this dialog — clients read
                # it from Connection Details.
                saved["token"] = secrets.token_urlsafe(32)
                token_generated = True
            try:
                save_settings(saved)
            except SettingsError as exc:
                _report_error(f"[MCP] Cannot save settings: {exc}")
                _warn(f"Saving the settings failed:\n{exc}\nSettings are unchanged.")
                return
            outcome["saved"] = saved
            outcome["token_generated"] = token_generated
            dialog.accept()

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=dialog,
        )
        buttons.accepted.connect(_save)
        buttons.rejected.connect(dialog.reject)

        layout = QtWidgets.QFormLayout(dialog)
        layout.addRow(_tr("Port:"), port_field)
        layout.addRow("", auto_start)
        layout.addRow("", network)
        layout.addRow("", network_note)
        layout.addRow(_tr("Allowed IPs:"), ips_field)
        layout.addRow("", ips_error)
        layout.addRow(_tr("Allowed roots:"), roots_field)
        layout.addRow("", roots_error)
        layout.addRow(buttons)

        # Keyboard tab order follows the form.
        QtWidgets.QWidget.setTabOrder(port_field, auto_start)
        QtWidgets.QWidget.setTabOrder(auto_start, network)
        QtWidgets.QWidget.setTabOrder(network, ips_field)
        QtWidgets.QWidget.setTabOrder(ips_field, roots_field)

        dialog.exec()
        if "saved" not in outcome:
            return
        saved = outcome["saved"]
        _controller_settings_changed()
        _report_message(
            "[MCP] Settings saved: port "
            f"{saved['port']}; auto-start "
            f"{'on' if saved['auto_start'] else 'off'}; network access "
            f"{'enabled' if saved['remote_enabled'] else 'disabled (Local only)'}; "
            f"allowed IPs {saved['allowed_ips'] or '(any host - open)'}; "
            f"{len(saved['allowed_roots'])} allowed root(s)."
        )
        if outcome["token_generated"]:
            _report_message(
                "[MCP] A bearer token was generated for network access; "
                "clients can view it in Connection Details."
            )
        if _restart_required(saved, mcp_server_module.server_status()):
            _report_message("[MCP] Restart the MCP server for changes to take effect.")

    def IsActive(self):
        return True


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------

_REGISTERED = False


def register_commands() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    FreeCADGui.addCommand(_TOGGLE_COMMAND, ToggleMCPServerCommand())
    FreeCADGui.addCommand(_DETAILS_COMMAND, ConnectionDetailsCommand())
    FreeCADGui.addCommand(_SETTINGS_COMMAND, MCPSettingsCommand())
    _REGISTERED = True
