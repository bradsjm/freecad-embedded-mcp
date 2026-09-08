import sys as _sys
import os as _os

try:
    _addon_dir = _os.path.dirname(_os.path.abspath(__file__))
except NameError:
    import inspect as _inspect

    _addon_dir = _os.path.dirname(
        _os.path.abspath(_inspect.getfile(_inspect.currentframe()))
    )
if _addon_dir not in _sys.path:
    _sys.path.insert(0, _addon_dir)


class FreeCADMCPAddonWorkbench(Workbench):
    MenuText = "MCP Addon"
    ToolTip = "Addon for MCP Communication"

    def Initialize(self):
        from mcp_server import commands

        command_list = [
            "Start_MCP_Server",
            "Stop_MCP_Server",
            "Toggle_Auto_Start",
            "Show_Auth_Token",
        ]
        self.appendToolbar("FreeCAD MCP", command_list)
        self.appendMenu("FreeCAD MCP", command_list)

    def Activated(self):
        pass

    def Deactivated(self):
        pass

    def ContextMenu(self, recipient):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(FreeCADMCPAddonWorkbench())


def _auto_start_mcp():
    try:
        from mcp_server import server as mcp_server_module
        from mcp_server.settings import load_settings

        if not load_settings().get("auto_start", False):
            return

        status = mcp_server_module.start_server()
        FreeCAD.Console.PrintMessage(
            f"[MCP] Auto-start: {status.get('state')} at {status.get('endpoint')}\n"
        )
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"[MCP] Auto-start failed: {e}\n")


from PySide import QtCore

QtCore.QTimer.singleShot(0, _auto_start_mcp)
