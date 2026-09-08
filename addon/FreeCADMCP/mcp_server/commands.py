"""Native workbench commands for the embedded MCP v2 add-on.

Registers the six ``FreeCADGui`` commands exposed by the workbench:

* ``Toggle_Auto_Start`` — checkable opt-in persisted in v2 settings.
* ``Toggle_Remote_Connections`` — checkable opt-in for non-loopback binding.
* ``Configure_Allowed_IPs`` — dialog editing the peer-IP allow-list.
* ``Show_Auth_Token`` — local dialog with the endpoint and bearer token.

Every command reports the *real* observable state (already running, not
running, start refusal, settings failure) and never prints the token: the
token exists only in the dialog and, after an explicit Copy click, on the
local clipboard.

This module is GUI-only and is imported exclusively from ``InitGui.py``
after the addon directory has been put on ``sys.path``.
"""

import secrets

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtGui, QtWidgets

from mcp_server import server as mcp_server_module
from mcp_server.settings import SettingsError, load_settings, save_settings
from mcp_server.ip_parse import validate_allowed_ips


class StartMCPServerCommand:
    def GetResources(self):
        return {
            "MenuText": "Start MCP Server",
            "ToolTip": "Start the embedded MCP server",
        }

    def Activated(self):
        before = mcp_server_module.server_status()
        try:
            status = mcp_server_module.start_server()
        except (SettingsError, RuntimeError, OSError) as exc:
            FreeCAD.Console.PrintError(f"[MCP] Start failed: {exc}\n")
            return
        endpoint = status.get("endpoint")
        if before.get("running"):
            FreeCAD.Console.PrintMessage(
                f"[MCP] Server already running at {endpoint}\n"
            )
        else:
            FreeCAD.Console.PrintMessage(f"[MCP] Server started at {endpoint}\n")

    def IsActive(self):
        return True


class StopMCPServerCommand:
    def GetResources(self):
        return {
            "MenuText": "Stop MCP Server",
            "ToolTip": "Stop the embedded MCP server",
        }

    def Activated(self):
        before = mcp_server_module.server_status()
        result = mcp_server_module.stop_server()
        if not before.get("running"):
            FreeCAD.Console.PrintMessage(
                f"[MCP] Server is not running (state: {result.get('state')}).\n"
            )
            return
        FreeCAD.Console.PrintMessage(
            "[MCP] Server stopped (state: %s, pending operations: %s)\n"
            % (result.get("state"), result.get("pendingOperations"))
        )

    def IsActive(self):
        return True


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
        }

    def Activated(self, checked=0):
        try:
            settings = load_settings()
            settings["auto_start"] = bool(checked)
            save_settings(settings)
        except SettingsError as exc:
            FreeCAD.Console.PrintError(f"[MCP] Cannot change auto-start: {exc}\n")
            return
        _sync_toggle_actions()
        if settings["auto_start"]:
            FreeCAD.Console.PrintMessage(
                "[MCP] Server will start automatically on next FreeCAD launch.\n"
            )
        else:
            FreeCAD.Console.PrintMessage("[MCP] Auto-start disabled.\n")

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
        }

    def Activated(self, checked=0):
        try:
            settings = load_settings()
            settings["remote_enabled"] = bool(checked)
            if checked and not settings.get("token"):
                settings["token"] = secrets.token_urlsafe(32)
            save_settings(settings)
        except SettingsError as exc:
            FreeCAD.Console.PrintError(f"[MCP] Cannot change remote access: {exc}\n")
            return
        _sync_toggle_actions()
        if settings["remote_enabled"]:
            FreeCAD.Console.PrintMessage(
                "[MCP] Remote connections enabled. Clients must present the "
                "bearer token (see Show Auth Token). Allowed IPs: "
                f"{settings['allowed_ips'] or '(any host - open)'}\n"
            )
        else:
            FreeCAD.Console.PrintMessage("[MCP] Remote connections disabled.\n")
        if mcp_server_module.server_status().get("running"):
            FreeCAD.Console.PrintMessage(
                "[MCP] Restart the MCP server for changes to take effect.\n"
            )

    def IsActive(self):
        return True


class ConfigureAllowedIPsCommand:
    def GetResources(self):
        return {
            "MenuText": "Configure Allowed IPs",
            "ToolTip": "Set which IP addresses or subnets may connect to the MCP server.",
        }

    def Activated(self):
        try:
            settings = load_settings()
        except SettingsError as exc:
            FreeCAD.Console.PrintError(f"[MCP] Cannot load settings: {exc}\n")
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
            FreeCAD.Console.PrintMessage("[MCP] Allowed IPs not changed.\n")
            return
        if not text.strip():
            settings["allowed_ips"] = ""
            try:
                save_settings(settings)
            except SettingsError as exc:
                FreeCAD.Console.PrintError(
                    f"[MCP] Cannot save allowed IPs: {exc}\n"
                )
                return
            FreeCAD.Console.PrintMessage(
                "[MCP] Allowed IPs cleared: any host may connect "
                "(token still required).\n"
            )
            if mcp_server_module.server_status().get("running"):
                FreeCAD.Console.PrintMessage(
                    "[MCP] Restart the MCP server for changes to take effect.\n"
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
            FreeCAD.Console.PrintWarning(
                "[MCP] Allowed IPs not changed - no valid entries.\n"
            )
            return
        settings["allowed_ips"] = ", ".join(valid)
        try:
            save_settings(settings)
        except SettingsError as exc:
            FreeCAD.Console.PrintError(f"[MCP] Cannot save allowed IPs: {exc}\n")
            return
        FreeCAD.Console.PrintMessage(
            f"[MCP] Allowed IPs updated to: {settings['allowed_ips']}\n"
        )
        if mcp_server_module.server_status().get("running"):
            FreeCAD.Console.PrintMessage(
                "[MCP] Restart the MCP server for changes to take effect.\n"
            )

    def IsActive(self):
        return True


class ShowAuthTokenCommand:
    def GetResources(self):
        return {
            "MenuText": "Show Auth Token",
            "ToolTip": "Show the MCP endpoint and bearer token for connecting clients.",
        }

    def Activated(self):
        try:
            settings = load_settings()
        except SettingsError as exc:
            FreeCAD.Console.PrintError(f"[MCP] Cannot show token: {exc}\n")
            return

        status = mcp_server_module.server_status()
        endpoint = status.get("endpoint")
        if not status.get("running") or not endpoint:
            host = "0.0.0.0" if settings.get("remote_enabled") else "127.0.0.1"
            endpoint = f"http://{host}:{settings['port']}/mcp"

        parent = FreeCADGui.getMainWindow()
        dialog = QtWidgets.QDialog(parent)
        dialog.setWindowTitle("FreeCAD MCP — Endpoint and Token")
        dialog.setMinimumWidth(480)

        endpoint_field = QtWidgets.QLineEdit(endpoint)
        endpoint_field.setReadOnly(True)
        local_only = not settings.get("remote_enabled")
        token_field = QtWidgets.QLineEdit(
            settings["token"]
            if not local_only
            else "(not required - local connections only)"
        )
        token_field.setReadOnly(True)

        copy_button = QtWidgets.QPushButton("Copy Token")
        close_button = QtWidgets.QPushButton("Close")
        if local_only or not settings["token"]:
            copy_button.setEnabled(False)

        def _copy_token():
            QtWidgets.QApplication.clipboard().setText(settings["token"])
            copy_button.setText("Copied")

        copy_button.clicked.connect(_copy_token)
        close_button.clicked.connect(dialog.accept)

        layout = QtWidgets.QFormLayout(dialog)
        layout.addRow("Endpoint:", endpoint_field)
        layout.addRow("Token:", token_field)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(copy_button)
        buttons.addWidget(close_button)
        layout.addRow(buttons)

        dialog.exec()


def _sync_toggle_actions() -> None:
    """Apply persisted checkable state; GetResources alone is unreliable."""
    try:
        settings = load_settings()
    except SettingsError:
        settings = {}
    for name, key in (
        ("Toggle_Auto_Start", "auto_start"),
        ("Toggle_Remote_Connections", "remote_enabled"),
    ):
        for action in FreeCADGui.getMainWindow().findChildren(QtGui.QAction):
            if action.objectName() == name:
                action.setCheckable(True)
                action.setChecked(bool(settings.get(key, False)))


def register_commands() -> None:
    FreeCADGui.addCommand("Start_MCP_Server", StartMCPServerCommand())
    FreeCADGui.addCommand("Stop_MCP_Server", StopMCPServerCommand())
    FreeCADGui.addCommand("Toggle_Auto_Start", ToggleAutoStartCommand())
    FreeCADGui.addCommand("Toggle_Remote_Connections", ToggleRemoteConnectionsCommand())
    FreeCADGui.addCommand("Configure_Allowed_IPs", ConfigureAllowedIPsCommand())
    FreeCADGui.addCommand("Show_Auth_Token", ShowAuthTokenCommand())
    QtCore.QTimer.singleShot(0, _sync_toggle_actions)
