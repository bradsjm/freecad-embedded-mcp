import os
import sys

try:
    addon_dir = os.path.dirname(os.path.abspath(__file__))
except NameError:
    import inspect

    addon_dir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
if addon_dir not in sys.path:
    sys.path.insert(0, addon_dir)

from mcp_server.workbench import FreeCADMCPAddonWorkbench, schedule_auto_start

# ``Gui`` is injected into this module's globals by the FreeCAD host when
# the add-on is loaded; it is not an importable name.
Gui.addWorkbench(FreeCADMCPAddonWorkbench())  # noqa: F821
schedule_auto_start()
