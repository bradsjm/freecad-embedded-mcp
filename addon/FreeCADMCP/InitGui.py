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
    # FreeCAD 1.1 executes InitGui class bodies in a restricted namespace
    # where module-level imports are not visible, so the workbench icon is
    # embedded as SVG content instead of a computed file path.
    Icon = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
  <g stroke="#f4f6f8" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"><path d="M32 10 51 21v22L32 54 13 43V21Z" fill="#4c9bd6"/><path d="m13 21 19 11 19-11M32 32v22" fill="none"/></g>
  <g stroke="#263445" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><path d="M32 10 51 21v22L32 54 13 43V21Z" fill="#4c9bd6"/><path d="m13 21 19 11 19-11M32 32v22" fill="none"/></g>
</svg>"""

    def Initialize(self):
        from mcp_server import commands
        commands.register_commands()

        command_list = [
            "Start_MCP_Server",
            "Stop_MCP_Server",
            "Toggle_Auto_Start",
            "Toggle_Remote_Connections",
            "Configure_Allowed_IPs",
            "Show_Auth_Token",
        ]
        self.appendToolbar("FreeCAD MCP", command_list)
        self.appendMenu("FreeCAD MCP", command_list)
        # The controller survives workbench switches; Initialize may run
        # again after a deferred startup already created it (idempotent).
        commands.initialize_ui()

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
        from mcp_server import commands

        commands.initialize_ui()
        from mcp_server import server as mcp_server_module
        from mcp_server.settings import load_settings

        if not load_settings().get("auto_start", False):
            return

        status = mcp_server_module.start_server()
        FreeCAD.Console.PrintMessage(
            f"[MCP] Auto-start: {status.get('state')} at {status.get('endpoint')}\n"
        )
    except Exception as e:
        # Auto-start failures stay non-modal: status bar plus Report view.
        FreeCAD.Console.PrintWarning(f"[MCP] Auto-start failed: {e}\n")
        try:
            from mcp_server import commands as _commands

            window = None
            try:
                import FreeCADGui

                window = FreeCADGui.getMainWindow()
            except Exception:
                window = None
            if window is not None:
                window.statusBar().showMessage(
                    "MCP auto-start failed; see Report view", 5000
                )
        except Exception:
            pass


from PySide import QtCore

QtCore.QTimer.singleShot(0, _auto_start_mcp)
