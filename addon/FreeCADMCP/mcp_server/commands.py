"""Native workbench commands for the embedded MCP v2 add-on.

Registers the four ``FreeCADGui`` commands exposed by the workbench:

* ``Start_MCP_Server`` — start via :func:`mcp_server.server.start_server`.
* ``Stop_MCP_Server`` — stop via :func:`mcp_server.server.stop_server`.
* ``Toggle_Auto_Start`` — checkable opt-in persisted in v2 settings.
* ``Show_Auth_Token`` — local dialog with the endpoint and bearer token.

Every command reports the *real* observable state (already running, not
running, start refusal, settings failure) and never prints the token: the
token exists only in the dialog and, after an explicit Copy click, on the
local clipboard.

This module is GUI-only and is imported exclusively from ``InitGui.py``
after the addon directory has been put on ``sys.path``.
"""

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtGui, QtWidgets

from mcp_server import server as mcp_server_module
from mcp_server.settings import SettingsError, load_settings, save_settings


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
        _sync_auto_start_action()
        if settings["auto_start"]:
            FreeCAD.Console.PrintMessage(
                "[MCP] Server will start automatically on next FreeCAD launch.\n"
            )
        else:
            FreeCAD.Console.PrintMessage("[MCP] Auto-start disabled.\n")

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
            endpoint = f"http://127.0.0.1:{settings['port']}/mcp"

        parent = FreeCADGui.getMainWindow()
        dialog = QtWidgets.QDialog(parent)
        dialog.setWindowTitle("FreeCAD MCP — Endpoint and Token")
        dialog.setMinimumWidth(480)

        endpoint_field = QtWidgets.QLineEdit(endpoint)
        endpoint_field.setReadOnly(True)
        token_field = QtWidgets.QLineEdit(settings["token"])
        token_field.setReadOnly(True)

        copy_button = QtWidgets.QPushButton("Copy Token")
        close_button = QtWidgets.QPushButton("Close")

        def _copy_token():
            QtWidgets.QApplication.clipboard().setText(token_field.text())
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


def _sync_auto_start_action() -> None:
    try:
        enabled = load_settings()["auto_start"]
    except SettingsError:
        enabled = False
    for action in FreeCADGui.getMainWindow().findChildren(QtGui.QAction):
        if action.objectName() == "Toggle_Auto_Start":
            action.setCheckable(True)
            action.setChecked(enabled)


def register_commands() -> None:
    FreeCADGui.addCommand("Start_MCP_Server", StartMCPServerCommand())
    FreeCADGui.addCommand("Stop_MCP_Server", StopMCPServerCommand())
    FreeCADGui.addCommand("Toggle_Auto_Start", ToggleAutoStartCommand())
    FreeCADGui.addCommand("Show_Auth_Token", ShowAuthTokenCommand())
    QtCore.QTimer.singleShot(0, _sync_auto_start_action)
