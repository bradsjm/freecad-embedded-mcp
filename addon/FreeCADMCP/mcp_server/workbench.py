"""FreeCAD workbench registration resources and deferred startup."""

from pathlib import Path

import FreeCAD
import FreeCADGui
from FreeCADGui import Workbench
from PySide import QtCore

from . import commands

_ICON = str(
    Path(__file__).resolve().parent.parent
    / "Resources"
    / "icons"
    / "mcp-workbench.svg"
)

_COMMANDS = [
    "Start_MCP_Server",
    "Stop_MCP_Server",
    "Toggle_Auto_Start",
    "Toggle_Remote_Connections",
    "Configure_Allowed_IPs",
    "Show_Auth_Token",
]


class FreeCADMCPAddonWorkbench(Workbench):
    MenuText = "MCP Addon"
    ToolTip = "Addon for MCP Communication"
    Icon = _ICON

    def Initialize(self):
        commands.register_commands()
        self.appendToolbar("FreeCAD MCP", _COMMANDS)
        self.appendMenu("FreeCAD MCP", _COMMANDS)
        commands.initialize_ui()

    def Activated(self):
        pass

    def Deactivated(self):
        pass

    def ContextMenu(self, recipient):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


def _auto_start_mcp():
    try:
        commands.initialize_ui()
        from . import server as mcp_server_module
        from .settings import load_settings

        if not load_settings().get("auto_start", False):
            return

        status = mcp_server_module.start_server()
        FreeCAD.Console.PrintMessage(
            f"[MCP] Auto-start: {status.get('state')} at {status.get('endpoint')}\n"
        )
    except Exception as exc:
        FreeCAD.Console.PrintWarning(f"[MCP] Auto-start failed: {exc}\n")
        try:
            window = FreeCADGui.getMainWindow()
            if window is not None:
                window.statusBar().showMessage(
                    "MCP auto-start failed; see Report view", 5000
                )
        except Exception:
            pass


def schedule_auto_start():
    """Schedule startup after FreeCAD finishes loading workbenches."""

    QtCore.QTimer.singleShot(0, _auto_start_mcp)
