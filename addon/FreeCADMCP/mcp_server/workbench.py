"""FreeCAD workbench registration resources and deferred startup."""

from pathlib import Path

import FreeCAD
import FreeCADGui
from FreeCADGui import Workbench
from PySide import QtCore

from . import commands

_ICON = str(Path(__file__).resolve().parent.parent / "Resources" / "icons" / "mcp-workbench.svg")

_TOOLBAR_COMMANDS = ["Toggle_MCP_Server"]

# FreeCAD's appendMenu treats a literal "Separator" item as a separator:
# the lifecycle action is grouped apart from the two dialogs.
_MENU_COMMANDS = ["Toggle_MCP_Server", "Separator", "Connection_Details", "MCP_Settings"]


class FreeCADMCPAddonWorkbench(Workbench):
    """Register the MCP Addon workbench toolbar, menu and UI commands."""

    MenuText = "MCP Addon"
    ToolTip = "Addon for MCP Communication"
    Icon = _ICON

    def Initialize(self):
        """Register commands, build the toolbar and menu, initialize the UI."""
        commands.register_commands()
        self.appendToolbar("FreeCAD MCP", _TOOLBAR_COMMANDS)
        self.appendMenu("FreeCAD MCP", _MENU_COMMANDS)
        commands.initialize_ui()

    def GetClassName(self):
        """Return the native workbench class name for FreeCAD's framework."""
        return "Gui::PythonWorkbench"


def _auto_start_mcp():
    """Start the server when auto_start is enabled; warn on failure, never raise."""
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
                window.statusBar().showMessage("MCP auto-start failed; see Report view", 5000)
        except Exception:
            pass


def schedule_auto_start():
    """Schedule startup after FreeCAD finishes loading workbenches."""

    QtCore.QTimer.singleShot(0, _auto_start_mcp)
