"""Tests for the ``capture_view`` tool (mcp_server/tools/view.py).

Loads the real module against stubbed FreeCAD/FreeCADGui/PySide, defending:
— including when the capture itself fails.
shared-target focus/a/b resolution (whole object or signed reference)
before any GUI state change, mode routing (overview/detail/interior/fit;
view_name applies to detail only), five-argument Framebuffer capture only,
the viewport/omitted/explicit size rules, navigation-animation suppression
with preference restoration, and subelement-preserving selection +
active-document restoration in ``finally`` — including when the capture
itself fails.
"""

import base64
import importlib.util
import os
import struct
import sys
import types
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
VIEW_PATH = ADDON_DIR / "mcp_server" / "tools" / "view.py"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server import protocol
from mcp_server.protocol import ToolError
from mcp_server.tools import geometry

_ORIENTATION_METHODS = (
    "viewIsometric",
    "viewFront",
    "viewTop",
    "viewRight",
    "viewRear",
    "viewLeft",
    "viewBottom",
    "viewDimetric",
    "viewTrimetric",
)


class FakeParamGet:
    """Stub for ``FreeCAD.ParamGet("User parameter:BaseApp/Preferences/View")``."""

    store: dict[str, Any] = {}
    set_calls: list[tuple[str, str, Any]] = []

    @classmethod
    def reset(cls) -> None:
        cls.store = {"UseNavigationAnimations": True, "AnimationDuration": 500}
        cls.set_calls = []

    def __init__(self, path: str) -> None:
        self.path = path

    def GetBool(self, name: str, default: bool) -> bool:
        return bool(FakeParamGet.store.get(name, default))

    def GetInt(self, name: str, default: int) -> int:
        return int(FakeParamGet.store.get(name, default))

    def SetBool(self, name: str, value: bool) -> None:
        FakeParamGet.store[name] = bool(value)
        FakeParamGet.set_calls.append(("bool", name, bool(value)))

    def SetInt(self, name: str, value: int) -> None:
        FakeParamGet.store[name] = int(value)
        FakeParamGet.set_calls.append(("int", name, int(value)))


class FakeApplication:
    @staticmethod
    def instance() -> "FakeApplication":
        return FakeApplication()

    def processEvents(self, *_args) -> None:
        pass


class FakeSelectionObject:
    def __init__(
        self,
        obj: Any,
        subelements: list[str],
        document_name: str | None = None,
        object_name: str | None = None,
    ) -> None:
        self.Object = obj
        self.SubElementNames = list(subelements)
        self._document_name = (
            document_name if document_name is not None else getattr(obj, "_doc", None)
        )
        self._object_name = object_name if object_name is not None else getattr(obj, "Name", None)

    @property
    def DocumentName(self) -> str | None:
        return self._document_name

    @property
    def ObjectName(self) -> str | None:
        return self._object_name


class FakeSelection:
    def __init__(self) -> None:
        self.complete: list[dict[str, Any]] = []
        self.clear_count = 0
        self.ex_calls: list[tuple[str | None, int]] = []
        self.ex_items: list[Any] | None = None
        self.ex_error: Exception | None = None

    def clearSelection(self) -> None:
        self.complete = []
        self.clear_count += 1

    # probes["gui.selection"]: the native addSelection accepts the
    # single-object form and the six-argument document form; the
    # two-argument object+subelement form is what this tool calls.
    def addSelection(self, obj: Any, subelement: str | None = None, *extra: Any) -> None:
        if extra:
            # Native six-argument form: (docname, objname, sub, x, y, z).
            if len(extra) != 4:
                raise TypeError("addSelection arity not supported")
            subelement = extra[0]
        entry = next((item for item in self.complete if item["object"] is obj), None)
        if entry is None:
            entry = {
                "object": obj,
                "subelements": [],
                "doc": getattr(obj, "_doc", None),
            }
            self.complete.append(entry)
        if subelement is not None and str(subelement) not in entry["subelements"]:
            entry["subelements"].append(str(subelement))

    def getCompleteSelection(self) -> list[Any]:
        return [entry["object"] for entry in self.complete]

    def getSelectionEx(
        self, docname: str | None = None, resolve: int = 0
    ) -> list[FakeSelectionObject]:
        self.ex_calls.append((docname, resolve))
        if self.ex_error is not None:
            raise self.ex_error
        if self.ex_items is not None:
            return list(self.ex_items)
        return [
            FakeSelectionObject(entry["object"], entry["subelements"])
            for entry in self.complete
            if docname is None or entry["doc"] == docname
        ]


class UnreadableSelectionObject:
    """Context double whose native subelement list explodes on read."""

    DocumentName = "Smoke"
    ObjectName = "Box"
    Object = None

    @property
    def SubElementNames(self) -> list[str]:
        raise RuntimeError("native subelement list exploded")


class FakeView:
    def __init__(
        self,
        size: tuple[int, int] = (2000, 1500),
        camera: str | None = None,
    ) -> None:
        self.size = size
        self.calls: list[Any] = []
        self.animation_flags: list[Any] = []
        self.camera_calls: list[str] = []
        self.save_error: Exception | None = None
        self.empty = False
        self.saved_paths: list[str] = []
        self.clip_calls: list[tuple[Any, ...]] = []
        self.clipping = False
        self.camera_string = camera
        self.camera_restore_error: Exception | None = None

    def getCamera(self) -> str:
        if self.camera_string is not None:
            return self.camera_string
        return "#Inventor V2.1 ascii FakeCamera {}"

    def setCamera(self, camera: str) -> None:
        self.camera_calls.append(str(camera))
        if self.camera_restore_error is not None:
            raise self.camera_restore_error

    def toggleClippingPlane(self, *args: Any) -> None:
        self.clip_calls.append(args)
        if args[0] == 1:
            self.clipping = True
        elif args[0] == 0:
            self.clipping = False

    def hasClippingPlane(self) -> bool:
        return self.clipping

    def getSize(self) -> tuple[int, int]:
        return self.size

    def fitAll(self) -> None:
        self.calls.append("fitAll")

    def saveImage(self, path, width, height, mode, method) -> None:
        # probes["gui.selection"]: the recorded call is
        # saveImage(path, width, height, style) and it writes the file;
        # this tool passes the extra framebuffer style argument.
        self.calls.append(("saveImage", width, height, mode, method))
        self.saved_paths.append(str(path))
        if self.save_error is not None:
            raise self.save_error
        if not self.empty:
            with open(path, "wb") as handle:
                handle.write(b"\x89PNG-fake-bytes")

    def __getattr__(self, name: str):
        if name in _ORIENTATION_METHODS:
            return lambda: self._orient(name)
        raise AttributeError(name)

    def _orient(self, name: str) -> None:
        self.calls.append(name)
        self.animation_flags.append(FakeParamGet.store["UseNavigationAnimations"])


class FakeAppDocument:
    def __init__(self, name: str) -> None:
        self.Name = name
        self.Label = name
        self.Objects: list[Any] = []


class FakeGuiDocument:
    def __init__(self, view: Any, active_object: Any = None, edit_holder: Any = None) -> None:
        self.ActiveView = view
        self.ActiveObject = active_object
        self._edit_holder = edit_holder

    def getInEdit(self) -> Any:
        return self._edit_holder


class FakeMainWindow:
    """Context double of the native main window proxy."""

    def __init__(self, gui: "FakeGui") -> None:
        self.gui = gui

    def getActiveWindow(self) -> Any:
        self.gui.active_window_calls += 1
        if self.gui.active_window_error is not None:
            raise self.gui.active_window_error
        return self.gui.context_window


class FakeApp:
    def __init__(self, documents: dict[str, FakeAppDocument]) -> None:
        self.documents = documents
        self.active_name: str | None = None
        self.set_active_calls: list[str] = []

    def listDocuments(self) -> dict[str, FakeAppDocument]:
        return dict(self.documents)

    @property
    def ActiveDocument(self) -> FakeAppDocument | None:
        return self.documents.get(self.active_name) if self.active_name else None

    def setActiveDocument(self, name: str) -> None:
        self.set_active_calls.append(str(name))
        if name not in self.documents:
            raise RuntimeError(f"unknown document '{name}'")
        self.active_name = name


class FakeGui:
    def __init__(self, views: dict[str, Any]) -> None:
        self.views = views
        self.selection = FakeSelection()
        self.messages: list[str] = []
        self.set_active_calls: list[str] = []
        self.active_name: str | None = None
        self.context_window: Any = None
        self.active_window_calls = 0
        self.active_window_error: Exception | None = None
        self.active_workbench_error: Exception | None = None

    def getDocument(self, name: str) -> FakeGuiDocument:
        if name not in self.views:
            raise RuntimeError(f"no GUI document '{name}'")
        return FakeGuiDocument(self.views[name])

    @property
    def ActiveDocument(self) -> FakeGuiDocument | None:
        return self.getDocument(self.active_name) if self.active_name else None

    def setActiveDocument(self, name: str) -> None:
        self.set_active_calls.append(str(name))
        if name not in self.views:
            raise RuntimeError(f"no GUI document '{name}'")
        self.active_name = name

    def SendMsgToActiveView(self, message: str) -> None:
        self.messages.append(message)

    def activeWorkbench(self) -> Any:
        if self.active_workbench_error is not None:
            raise self.active_workbench_error
        return types.SimpleNamespace(name=lambda: "PartDesign")

    def getMainWindow(self) -> FakeMainWindow:
        return FakeMainWindow(self)

    def activateActiveWindow(self) -> None:
        # A setter/activation path must never be reached by the context
        # tool; tripping here fails the test that caused it.
        raise AssertionError("activateActiveWindow must not be called")

    @property
    def Selection(self) -> FakeSelection:
        return self.selection


class FakeCtx:
    def __init__(
        self,
        documents: dict[str, FakeAppDocument],
        gui_views: dict[str, Any],
        objects: dict[str, dict[str, Any]],
    ) -> None:
        self.App = FakeApp(documents)
        self.Gui = FakeGui(gui_views)
        self.objects = objects
        self.signer = protocol.ConsentSigner(ttl_s=3600)
        self._identities: dict[Any, str] = {}
        self._generations: dict[str, int] = {}

    def require_document(self, name: str) -> FakeAppDocument:
        doc = self.App.documents.get(name)
        if doc is None:
            raise ToolError("DOCUMENT_NOT_FOUND", f"unknown document '{name}'", {})
        return doc

    def document_generation(self, doc: Any) -> int:
        return self._generations.get(doc.Name, 7)

    def require_object(self, doc: Any, name: str) -> Any:
        obj = self.objects.get(doc.Name, {}).get(name)
        if obj is None:
            raise ToolError("OBJECT_NOT_FOUND", f"unknown object '{name}'", {"object": name})
        return obj

    def check_document_idle(self, doc: Any) -> None:
        pass

    def document_identity(self, doc: Any) -> str:
        if doc not in self._identities:
            self._identities[doc] = f"identity-{len(self._identities) + 1}"
        return self._identities[doc]


class FakeBoundBox:
    """Bounds double carrying the six attributes geometry reads."""

    def __init__(self, bounds: tuple[float, float, float, float, float, float]) -> None:
        self.XMin, self.YMin, self.ZMin, self.XMax, self.YMax, self.ZMax = bounds


class FakePlacementMarker:
    """Non-None placement marker for ``getGlobalPlacement`` doubles."""


class FakeShape:
    """Shape double with bounds, faces and a copy() for placed_shape."""

    def __init__(
        self,
        bounds: tuple[float, float, float, float, float, float],
        faces: list[Any] | None = None,
        edges: list[Any] | None = None,
    ) -> None:
        self.BoundBox = FakeBoundBox(bounds)
        self.Faces = list(faces or [])
        self.Edges = list(edges or [])
        self.Placement: Any = None

    def copy(self) -> "FakeShape":
        duplicate = FakeShape.__new__(FakeShape)
        duplicate.__dict__.update(self.__dict__)
        return duplicate


class FakeShapeObject:
    """Document object double whose shape resolves through placed_shape."""

    def __init__(self, name: str, doc: str, shape: Any) -> None:
        self.Name = name
        self._doc = doc
        self.TypeId = "Part::Feature"
        self.Shape = shape

    def getGlobalPlacement(self) -> Any:
        return FakePlacementMarker()


class FakeViewObject:
    """View object double recording Transparency writes (0 -> 75 -> 0)."""

    def __init__(self, transparency: int = 0) -> None:
        self._transparency = int(transparency)
        self.transparency_log: list[int] = [int(transparency)]

    @property
    def Transparency(self) -> int:
        return self._transparency

    @Transparency.setter
    def Transparency(self, value: int) -> None:
        self._transparency = int(value)
        self.transparency_log.append(int(value))


class Plane:
    """Surface double literally named for geometry._type_name == 'Plane'."""


class Cylinder:
    """Surface double literally named for geometry._type_name == 'Cylinder'."""

    def __init__(
        self,
        axis: tuple[float, float, float],
        center: tuple[float, float, float],
        radius: float,
    ) -> None:
        self.Axis = types.SimpleNamespace(x=axis[0], y=axis[1], z=axis[2])
        self.Center = types.SimpleNamespace(x=center[0], y=center[1], z=center[2])
        self.Radius = radius


class FakeFace:
    """Face double serving parameter, normal and point reads."""

    def __init__(
        self,
        surface: Any,
        normal: tuple[float, float, float] = (0.0, 0.0, 1.0),
        center: tuple[float, float, float] = (0.0, 0.0, 0.0),
        parameter_range: tuple[float, float, float, float] = (0.0, 1.0, 0.0, 1.0),
    ) -> None:
        self.Surface = surface
        self.Area = 100.0
        self.ParameterRange = parameter_range
        self._normal = normal
        self._center = center

    def normalAt(self, _u: float, _v: float) -> Any:
        return types.SimpleNamespace(x=self._normal[0], y=self._normal[1], z=self._normal[2])

    def valueAt(self, _u: float, _v: float) -> Any:
        return types.SimpleNamespace(x=self._center[0], y=self._center[1], z=self._center[2])


class FakeVector3:
    """FreeCAD.Vector double for the clipping-plane placement stub."""

    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = float(x), float(y), float(z)


class FakeRotation3:
    """FreeCAD.Rotation double recording its constructor arguments.

    Carries no ``.Q``, so the derived-camera quaternion path exercises the
    pure-Python fallback exactly as in headless runs.
    """

    def __init__(self, *args: Any) -> None:
        self.args = args


class FakePlacement3:
    """FreeCAD.Placement double recording base and rotation."""

    def __init__(self, base: Any, rotation: Any) -> None:
        self.Base = base
        self.Rotation = rotation


class FakeQImage:
    """QtGui.QImage double: records loads; save writes marker bytes."""

    Format_RGB32 = 5
    loaded: list[Any] = []
    saved: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.loaded = []
        cls.saved = []

    def __init__(self, *args: Any) -> None:
        self.args = args
        FakeQImage.loaded.append(args)

    def isNull(self) -> bool:
        return False

    def save(self, path: str) -> bool:
        FakeQImage.saved.append(str(path))
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG-composed")
        return True


class FakeQPainter:
    """QtGui.QPainter double recording fill/draw/text operations."""

    operations: list[tuple[Any, ...]] = []

    @classmethod
    def reset(cls) -> None:
        cls.operations = []

    def __init__(self, canvas: Any) -> None:
        self.canvas = canvas

    def fillRect(self, x: int, y: int, w: int, h: int, _color: Any) -> None:
        FakeQPainter.operations.append(("fillRect", x, y, w, h))

    def drawImage(self, x: int, y: int, image: Any) -> None:
        FakeQPainter.operations.append(("drawImage", x, y, image))

    def drawText(self, x: int, y: int, text: str) -> None:
        FakeQPainter.operations.append(("drawText", x, y, text))

    def end(self) -> None:
        pass


@contextmanager
def load_view_module() -> Iterator[types.ModuleType]:
    """Load mcp_server/tools/view.py against stubbed FreeCAD/Qt modules."""

    module_names = ["FreeCAD", "FreeCADGui", "PySide", "mcp_server.tools.view"]
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in module_names}

    FakeParamGet.reset()
    FakeQImage.reset()
    FakeQPainter.reset()
    freecad = types.ModuleType("FreeCAD")
    freecad.ParamGet = FakeParamGet
    freecad.Vector = FakeVector3
    freecad.Rotation = FakeRotation3
    freecad.Placement = FakePlacement3
    freecad_gui = types.ModuleType("FreeCADGui")
    freecad_gui.updateGui = lambda: None
    qt_core = types.SimpleNamespace(
        QObject=object,
        Signal=lambda *_: None,
        Qt=types.SimpleNamespace(QueuedConnection=0),
        QEventLoop=types.SimpleNamespace(ExcludeUserInputEvents=1, ExcludeSocketNotifiers=2),
        QThread=types.SimpleNamespace(msleep=lambda _delay: None),
        QTimer=types.SimpleNamespace(singleShot=lambda *_: None),
    )
    pyside = types.ModuleType("PySide")
    pyside.QtCore = qt_core
    pyside.QtWidgets = types.SimpleNamespace(QApplication=FakeApplication)
    pyside.QtGui = types.SimpleNamespace(
        QImage=FakeQImage,
        QPainter=FakeQPainter,
        QColor=lambda *rgb: tuple(rgb),
    )

    sys.modules["FreeCAD"] = freecad
    sys.modules["FreeCADGui"] = freecad_gui
    sys.modules["PySide"] = pyside
    sys.modules.pop("mcp_server.tools.view", None)
    sys.modules.pop("mcp_server.gui_dispatch", None)
    importlib.import_module("mcp_server.tools")

    module_name = "mcp_server.tools.view"
    try:
        spec = importlib.util.spec_from_file_location(module_name, VIEW_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load view tool from {VIEW_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        for name, value in saved.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture()
def view_module():
    with load_view_module() as module:
        yield module


def make_ctx(
    active: str | None = None,
    gui_views: dict[str, Any] | None = None,
    smoke_objects: dict[str, Any] | None = None,
    document_objects: list[Any] | None = None,
) -> FakeCtx:
    documents = {
        "Smoke": FakeAppDocument("Smoke"),
        "Other": FakeAppDocument("Other"),
    }
    if document_objects is not None:
        documents["Smoke"].Objects = list(document_objects)
    box = types.SimpleNamespace(
        Name="Box",
        _doc="Smoke",
        Shape=types.SimpleNamespace(Faces=[None] * 6, Edges=[None] * 12),
    )
    other_obj = types.SimpleNamespace(Name="Lid", _doc="Other")
    if gui_views is None:
        gui_views = {"Smoke": FakeView(), "Other": FakeView()}
    smoke_objects = smoke_objects if smoke_objects is not None else {"Box": box}
    ctx = FakeCtx(
        documents,
        gui_views,
        {"Smoke": smoke_objects, "Other": {"Lid": other_obj}},
    )
    if active is not None:
        ctx.App.active_name = active
        ctx.Gui.active_name = active
    return ctx


def capture_args(**overrides: Any) -> dict[str, Any]:
    arguments = {
        "document": "Smoke",
        "mode": "detail",
        "view_name": "Isometric",
        "focus": {"object": "Box"},
    }
    arguments.update(overrides)
    return arguments


def test_capture_returns_png_restores_state_and_suppresses_animations(
    view_module,
) -> None:
    ctx = make_ctx(active="Other")
    view = ctx.Gui.views["Smoke"]
    lid = ctx.objects["Other"]["Lid"]
    ctx.Gui.selection.addSelection(lid, "Face3")

    result = view_module.capture_view(ctx, capture_args(width=640, height=480))

    assert result["mimeType"] == "image/png"
    assert result["width"] == 640
    assert result["height"] == 480
    assert base64.b64decode(result["data"]) == b"\x89PNG-fake-bytes"
    assert ("saveImage", 640, 480, "White", "Framebuffer") in view.calls
    assert result["document"] == "Smoke"
    assert result["mode"] == "detail"
    assert result["focus"] == {"object": "Box"}
    assert result["view_name"] == "Isometric"
    assert all(call != "fitAll" for call in view.calls)
    # Orientation ran with animations disabled (never a stale animated
    # orientation) and the preference was restored afterwards.
    assert view.animation_flags == [False]
    assert FakeParamGet.set_calls == [
        ("bool", "UseNavigationAnimations", False),
        ("int", "AnimationDuration", 0),
        ("bool", "UseNavigationAnimations", True),
        ("int", "AnimationDuration", 500),
    ]
    assert FakeParamGet.store == {
        "UseNavigationAnimations": True,
        "AnimationDuration": 500,
    }
    # Both the App and the Gui active document were switched explicitly and
    # restored to the caller's active document.
    assert ctx.App.set_active_calls == ["Smoke", "Other"]
    assert ctx.Gui.set_active_calls == ["Smoke", "Other"]
    assert ctx.App.active_name == "Other"
    # The caller's subelement selection was restored exactly.
    restored = ctx.Gui.selection.getSelectionEx("Other")
    assert len(restored) == 1
    assert restored[0].Object is lid
    assert restored[0].SubElementNames == ["Face3"]
    # The temporary capture file was removed.
    assert all(not os.path.exists(path) for path in view.saved_paths)
    # The pre-capture camera was restored after the framing move.
    assert view.camera_calls == ["#Inventor V2.1 ascii FakeCamera {}"]


def test_document_without_gui_view_fails(view_module) -> None:
    ctx = make_ctx(gui_views={})
    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args())
    assert excinfo.value.code == "GUI_DISPATCH_FAILED"
    assert ctx.App.set_active_calls == []
    assert ctx.Gui.set_active_calls == []
    assert FakeParamGet.set_calls == []


def test_unsupported_view_fails(view_module) -> None:
    ctx = make_ctx(gui_views={"Smoke": None})
    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args())
    assert excinfo.value.code == "UNSUPPORTED_VIEW"
    assert ctx.App.set_active_calls == []
    assert ctx.Gui.set_active_calls == []
    assert FakeParamGet.set_calls == []


def test_one_omitted_side_clamps_to_schema_maximum(view_module) -> None:
    ctx = make_ctx(active="Smoke", gui_views={"Smoke": FakeView(size=(5120, 2880))})
    view = ctx.Gui.views["Smoke"]
    view_module.capture_view(ctx, capture_args(height=1000))
    save_call = next(call for call in view.calls if isinstance(call, tuple))
    # The viewport width exceeds the schema limit; the explicit height is
    # never resized and no aspect ratio is inferred.
    assert save_call[1] == 4096
    assert save_call[2] == 1000


def test_one_omitted_side_clamps_portrait_viewport(view_module) -> None:
    ctx = make_ctx(active="Smoke", gui_views={"Smoke": FakeView(size=(5120, 5000))})
    view = ctx.Gui.views["Smoke"]
    view_module.capture_view(ctx, capture_args(width=1000))
    save_call = next(call for call in view.calls if isinstance(call, tuple))
    assert save_call[1] == 1000
    assert save_call[2] == 4096


def test_state_restored_even_when_capture_fails(view_module) -> None:
    ctx = make_ctx(active="Other")
    view = ctx.Gui.views["Smoke"]
    view.save_error = RuntimeError("compositor died")
    lid = ctx.objects["Other"]["Lid"]
    ctx.Gui.selection.addSelection(lid, "Edge2")

    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args(width=640, height=480))

    assert excinfo.value.code == "GUI_DISPATCH_FAILED"
    assert ctx.App.active_name == "Other"
    assert ctx.Gui.set_active_calls == ["Smoke", "Other"]
    restored = ctx.Gui.selection.getSelectionEx("Other")
    assert restored[0].Object is lid
    assert restored[0].SubElementNames == ["Edge2"]
    # The navigation-animation preference was restored despite the failure.
    assert FakeParamGet.store == {
        "UseNavigationAnimations": True,
        "AnimationDuration": 500,
    }
    assert all(not os.path.exists(path) for path in view.saved_paths)
    # The pre-capture camera was restored despite the failure.
    assert view.camera_calls == ["#Inventor V2.1 ascii FakeCamera {}"]


def test_subelement_capture_frames_and_reports(view_module) -> None:
    face = FakeFace(Plane())
    box_obj = FakeShapeObject(
        "Box",
        "Smoke",
        FakeShape((0.0, 0.0, 0.0, 10.0, 10.0, 10.0), faces=[face]),
    )
    ctx = make_ctx(active="Smoke", smoke_objects={"Box": box_obj})
    doc = ctx.require_document("Smoke")
    focus = geometry.make_reference(ctx, doc, box_obj, "face", 1)

    result = view_module.capture_view(ctx, capture_args(focus=focus, width=640, height=480))

    assert result["focus"] == focus
    assert result["document"] == "Smoke"
    assert result["mode"] == "detail"
    assert result["view_name"] == "Isometric"
    assert ctx.Gui.messages == ["ViewSelection", "ViewSelection"]


def test_raw_numeric_targets_rejected_before_gui_changes(view_module) -> None:
    for bad in ("Vertex2", "Face99", "Edge99", "face3", 3):
        ctx = make_ctx()
        with pytest.raises(ToolError) as excinfo:
            view_module.capture_view(
                ctx,
                capture_args(focus={"object": "Box", "subelement": bad}),
            )
        assert excinfo.value.code == "VALIDATION_FAILED"
        assert ctx.Gui.messages == []
        assert ctx.App.set_active_calls == []
        assert FakeParamGet.set_calls == []


PARSEABLE_CAMERA = "position (0,-100,0) orientation (1,0,0,0)"


def _fit_arguments(**overrides: Any) -> dict[str, Any]:
    arguments = {
        "document": "Smoke",
        "mode": "fit",
        "a": {"object": "Pin"},
        "b": {"object": "Hole"},
    }
    arguments.update(overrides)
    return arguments


def _cylinder_pair() -> tuple[FakeShapeObject, FakeShapeObject]:
    pin = FakeShapeObject(
        "Pin",
        "Smoke",
        FakeShape(
            (5.0, 5.0, 0.0, 15.0, 15.0, 10.0),
            faces=[FakeFace(Cylinder((0.0, 0.0, 1.0), (10.0, 10.0, 5.0), 5.0))],
        ),
    )
    hole = FakeShapeObject(
        "Hole",
        "Smoke",
        FakeShape(
            (5.0, 5.0, 0.0, 15.0, 15.0, 10.0),
            faces=[FakeFace(Cylinder((0.0, 0.0, 1.0), (10.0, 10.0, 5.0), 5.1))],
        ),
    )
    return pin, hole


def test_overview_sheet_seven_panels_and_legend(view_module) -> None:
    ctx = make_ctx(active="Smoke")
    view = ctx.Gui.views["Smoke"]

    result = view_module.capture_view(ctx, {"document": "Smoke"})

    assert result["mimeType"] == "image/png"
    assert base64.b64decode(result["data"]) == b"\x89PNG-composed"
    assert (result["width"], result["height"]) == (2048, 1104)
    assert result["mode"] == "overview"
    assert result["focus"] is None
    labels = [entry["view"] for entry in result["views"]]
    assert labels == [
        "Isometric",
        "Front",
        "Back",
        "Left",
        "Right",
        "Top",
        "Bottom",
        "Legend",
    ]
    assert result["views"][0]["rect"] == {"x": 0, "y": 0, "w": 512, "h": 552}
    assert result["views"][7]["rect"] == {"x": 1536, "y": 552, "w": 512, "h": 552}
    assert result["captured_objects"] == []
    assert result["truncated"] is False
    orientations = [call for call in view.calls if isinstance(call, str)]
    assert orientations == [
        "viewIsometric",
        "viewFront",
        "viewRear",
        "viewLeft",
        "viewRight",
        "viewTop",
        "viewBottom",
    ]
    assert len([call for call in view.calls if isinstance(call, tuple)]) == 7
    assert ctx.Gui.messages == ["ViewFit"] * 14
    # Every temp file (panels and composed sheet) was removed.
    assert all(not os.path.exists(path) for path in view.saved_paths)
    assert all(not os.path.exists(path) for path in FakeQImage.saved)
    assert FakeParamGet.store == {
        "UseNavigationAnimations": True,
        "AnimationDuration": 500,
    }


def test_overview_manifest_carries_generation_and_camera_axes(view_module) -> None:
    ctx = make_ctx(
        active="Smoke",
        gui_views={
            "Smoke": FakeView(camera=PARSEABLE_CAMERA),
            "Other": FakeView(),
        },
    )

    result = view_module.capture_view(ctx, capture_args(mode="overview", view_name=None))

    assert result["generation"] == 7
    assert result["mode"] == "overview"
    assert result["focus"] == {"object": "Box"}
    assert result["view_name"] is None
    assert "captured_objects" not in result
    panels = result["views"][:7]
    assert len(panels) == 7
    # The parseable camera orientation (1,0,0,0) is axis-angle identity:
    # direction -Z, screen up +Y for every panel.
    for entry in panels:
        assert entry["direction"] == [0.0, 0.0, -1.0]
        assert entry["up"] == [0.0, 1.0, 0.0]
        assert entry["derived"] is False
        assert entry["section_plane"] is None
    assert result["views"][7]["direction"] is None
    assert result["views"][7]["view"] == "Legend"


def test_overview_document_scope_uses_viewfit_and_reports_objects(view_module) -> None:
    leg = types.SimpleNamespace(
        Name="Leg", Visibility=True, _doc="Smoke", getParentGeoFeatureGroup=lambda: None
    )
    wick = types.SimpleNamespace(
        Name="Wick", Visibility=False, _doc="Smoke", getParentGeoFeatureGroup=lambda: None
    )
    inner = types.SimpleNamespace(
        Name="Inner", Visibility=True, _doc="Smoke", getParentGeoFeatureGroup=lambda: leg
    )
    ctx = make_ctx(document_objects=[leg, wick, inner])
    view = ctx.Gui.views["Smoke"]

    result = view_module.capture_view(ctx, {"document": "Smoke"})

    assert result["captured_objects"] == ["Leg"]
    assert result["truncated"] is False
    assert result["focus"] is None
    assert ctx.Gui.messages.count("ViewFit") == 14
    assert "ViewSelection" not in ctx.Gui.messages
    assert view.calls[0] == "viewIsometric"


def test_view_name_conflicts_with_sheet_modes(view_module) -> None:
    for mode in ("overview", "interior", "fit"):
        ctx = make_ctx()
        with pytest.raises(ToolError) as excinfo:
            view_module.capture_view(ctx, capture_args(view_name="Front", mode=mode))
        assert excinfo.value.code == "VALIDATION_FAILED"
        assert excinfo.value.details["reason"] == "invalid_parameter_for_mode"
        assert ctx.Gui.views["Smoke"].calls == []
        assert FakeParamGet.set_calls == []


def test_view_name_without_mode_defaults_to_overview_and_refuses(view_module) -> None:
    # mode defaults to overview regardless of view_name, so a bare
    # named-view call is a parameter-for-mode refusal, never an implicit
    # single-view capture.
    ctx = make_ctx()
    arguments = {"document": "Smoke", "view_name": "Isometric", "focus": {"object": "Box"}}
    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, arguments)
    assert excinfo.value.code == "VALIDATION_FAILED"
    assert excinfo.value.details["reason"] == "invalid_parameter_for_mode"
    assert excinfo.value.details["mode"] == "overview"
    assert ctx.Gui.views["Smoke"].calls == []
    assert FakeParamGet.set_calls == []


def test_detail_derives_orientation_from_planar_face(view_module) -> None:
    bounds = (0.0, 0.0, 0.0, 20.0, 30.0, 10.0)
    face = FakeFace(Plane(), normal=(0.0, 0.0, 1.0), center=(10.0, 15.0, 10.0))
    box_obj = FakeShapeObject("Box", "Smoke", FakeShape(bounds, faces=[face]))
    ctx = make_ctx(
        active="Smoke",
        smoke_objects={"Box": box_obj},
        gui_views={
            "Smoke": FakeView(camera=PARSEABLE_CAMERA),
            "Other": FakeView(),
        },
    )
    view = ctx.Gui.views["Smoke"]
    doc = ctx.require_document("Smoke")
    focus = geometry.make_reference(ctx, doc, box_obj, "face", 1)

    result = view_module.capture_view(ctx, capture_args(view_name=None, mode="detail", focus=focus))

    assert result["mode"] == "detail"
    assert result["focus"] == focus
    assert result["views"][0]["view"] == "Detail"
    assert result["views"][0]["derived"] is True
    assert result["views"][0]["section_plane"] is None
    assert result["views"][0]["rect"]["w"] == result["width"]
    # The camera was rewritten between framing and saveImage and restored
    # to the pre-capture string afterwards.
    assert len(view.camera_calls) == 2
    derived_string = view.camera_calls[0]
    assert view.camera_calls[1] == PARSEABLE_CAMERA
    parsed = view_module._parse_camera(derived_string)
    assert parsed is not None
    position = parsed["position"][0]
    # No focalDistance in the camera string: the fallback is 1.5x the
    # focus bounds diagonal.
    expected_distance = 1.5 * (20.0**2 + 30.0**2 + 10.0**2) ** 0.5
    assert position[0] == pytest.approx(10.0)
    assert position[1] == pytest.approx(15.0)
    assert position[2] == pytest.approx(10.0 + expected_distance)
    # Round trip: the derived camera parses back to direction -normal and
    # screen up +X (least parallel basis axis to -Z, tie X>Y>Z).
    axes = view_module._camera_axes(FakeView(camera=derived_string))
    assert axes is not None
    direction, up = axes
    assert direction[0] == pytest.approx(0.0, abs=1e-6)
    assert direction[1] == pytest.approx(0.0, abs=1e-6)
    assert direction[2] == pytest.approx(-1.0, abs=1e-6)
    assert up[0] == pytest.approx(1.0, abs=1e-6)
    assert up[1] == pytest.approx(0.0, abs=1e-6)
    assert up[2] == pytest.approx(0.0, abs=1e-6)


def test_detail_without_derivable_target_refuses(view_module) -> None:
    whole_ctx = make_ctx()
    edge_obj = FakeShapeObject(
        "Box",
        "Smoke",
        FakeShape((0.0, 0.0, 0.0, 10.0, 10.0, 10.0), edges=[None]),
    )
    edge_ctx = make_ctx(smoke_objects={"Box": edge_obj})
    edge_focus = geometry.make_reference(
        edge_ctx, edge_ctx.require_document("Smoke"), edge_obj, "edge", 1
    )
    for ctx, arguments in (
        (whole_ctx, capture_args(view_name=None, mode="detail")),
        (edge_ctx, capture_args(view_name=None, mode="detail", focus=edge_focus)),
    ):
        with pytest.raises(ToolError) as excinfo:
            view_module.capture_view(ctx, arguments)
        assert excinfo.value.code == "VALIDATION_FAILED"
        assert excinfo.value.details["reason"] == "orientation_not_derivable"
        assert excinfo.value.details["suggestions"] == ["view_name"]
        assert ctx.Gui.views["Smoke"].calls == []
        assert ctx.App.set_active_calls == []
        assert FakeParamGet.set_calls == []


def test_interior_sections_and_xray_with_full_restore(view_module) -> None:
    bounds = (0.0, 0.0, 0.0, 20.0, 30.0, 10.0)
    box_obj = FakeShapeObject("Box", "Smoke", FakeShape(bounds, faces=[FakeFace(Plane())]))
    box_obj.ViewObject = FakeViewObject(0)
    ctx = make_ctx(active="Smoke", smoke_objects={"Box": box_obj})
    view = ctx.Gui.views["Smoke"]

    result = view_module.capture_view(ctx, capture_args(view_name=None, mode="interior"))

    assert result["mode"] == "interior"
    assert (result["width"], result["height"]) == (1024, 1104)
    labels = [entry["view"] for entry in result["views"]]
    assert labels == ["SectionX", "SectionY", "SectionZ", "Xray-Isometric"]
    normals = [entry["section_plane"]["normal"] for entry in result["views"][:3]]
    assert normals == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    for entry in result["views"][:3]:
        assert entry["section_plane"]["point"] == [10.0, 15.0, 5.0]
    assert result["views"][3]["section_plane"] is None
    # Clipping sequence: three section enables, then one disable.
    assert [call[0] for call in view.clip_calls] == [1, 1, 1, 0]
    placements = [call[3] for call in view.clip_calls[:3]]
    for placement in placements:
        assert (placement.Base.x, placement.Base.y, placement.Base.z) == (10.0, 15.0, 5.0)
    carried = [placement.Rotation.args[1] for placement in placements]
    assert [(vector.x, vector.y, vector.z) for vector in carried] == [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    ]
    # Each section uses the ortho parallel to its plane normal; the x-ray
    # panel uses Isometric.
    orientations = [call for call in view.calls if isinstance(call, str)]
    assert orientations == ["viewLeft", "viewFront", "viewBottom", "viewIsometric"]
    # X-ray transparency ran 0 -> 75 -> 0 on the focus view object.
    assert box_obj.ViewObject.transparency_log == [0, 75, 0]
    # Full restore: clipping off, animation preference restored, focus
    # framing selection cleared and restored, active document unchanged.
    assert view.clipping is False
    assert FakeParamGet.store == {
        "UseNavigationAnimations": True,
        "AnimationDuration": 500,
    }
    assert ctx.Gui.messages == ["ViewSelection"] * 8
    assert ctx.App.active_name == "Smoke"
    assert all(not os.path.exists(path) for path in view.saved_paths)
    assert all(not os.path.exists(path) for path in FakeQImage.saved)


def test_interior_refuses_when_clipping_plane_active(view_module) -> None:
    box_obj = FakeShapeObject(
        "Box",
        "Smoke",
        FakeShape((0.0, 0.0, 0.0, 10.0, 10.0, 10.0), faces=[FakeFace(Plane())]),
    )
    box_obj.ViewObject = FakeViewObject(0)
    ctx = make_ctx(smoke_objects={"Box": box_obj})
    view = ctx.Gui.views["Smoke"]
    view.clipping = True

    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args(view_name=None, mode="interior"))

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert excinfo.value.details["reason"] == "clipping_plane_active"
    assert (
        excinfo.value.details["nextAction"] == "toggle the clipping plane off in the GUI and retry"
    )
    assert view.clip_calls == []
    assert view.calls == []
    assert ctx.Gui.messages == []
    assert FakeParamGet.set_calls == []


def test_interior_target_shapeless_refuses(view_module) -> None:
    ctx = make_ctx()
    view = ctx.Gui.views["Smoke"]

    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args(view_name=None, mode="interior"))

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert excinfo.value.details["reason"] == "interior_target_shapeless"
    assert view.calls == []
    assert view.clip_calls == []
    assert ctx.Gui.messages == []
    assert FakeParamGet.set_calls == []


def test_interior_subshape_focus_refuses(view_module) -> None:
    box_obj = FakeShapeObject(
        "Box",
        "Smoke",
        FakeShape((0.0, 0.0, 0.0, 10.0, 10.0, 10.0), faces=[FakeFace(Plane())]),
    )
    box_obj.ViewObject = FakeViewObject(0)
    ctx = make_ctx(smoke_objects={"Box": box_obj})
    doc = ctx.require_document("Smoke")
    focus = geometry.make_reference(ctx, doc, box_obj, "face", 1)

    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args(view_name=None, mode="interior", focus=focus))

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert excinfo.value.details["reason"] == "subshape_not_allowed"
    assert ctx.Gui.views["Smoke"].calls == []
    assert ctx.Gui.messages == []
    assert ctx.App.set_active_calls == []
    assert FakeParamGet.set_calls == []


def test_fit_derives_axis_from_coaxial_cylinders(view_module) -> None:
    pin, hole = _cylinder_pair()
    ctx = make_ctx(active="Smoke", smoke_objects={"Pin": pin, "Hole": hole})
    view = ctx.Gui.views["Smoke"]

    result = view_module.capture_view(ctx, _fit_arguments())

    assert result["mode"] == "fit"
    assert (result["width"], result["height"]) == (1024, 552)
    labels = [entry["view"] for entry in result["views"]]
    assert labels == ["Isometric", "MatingSection"]
    assert result["views"][0]["section_plane"] is None
    section = result["views"][1]["section_plane"]
    assert section["normal"] == [1.0, 0.0, 0.0]
    assert section["point"] == [10.0, 10.0, 5.0]
    assert result["focus"] is None
    assert result["a"] == {"object": "Pin"}
    assert result["b"] == {"object": "Hole"}
    assert result["view_name"] is None
    # Uncut panel first (no clip call before it), then one enable + one
    # disable around the section panel.
    assert [call[0] for call in view.clip_calls] == [1, 0]
    placement = view.clip_calls[0][3]
    assert (placement.Base.x, placement.Base.y, placement.Base.z) == (10.0, 10.0, 5.0)
    rotation_target = placement.Rotation.args[1]
    assert (rotation_target.x, rotation_target.y, rotation_target.z) == (1.0, 0.0, 0.0)
    orientations = [call for call in view.calls if isinstance(call, str)]
    assert orientations == ["viewIsometric", "viewLeft"]
    assert ctx.Gui.messages == ["ViewSelection"] * 4
    assert view.clipping is False
    assert FakeParamGet.store == {
        "UseNavigationAnimations": True,
        "AnimationDuration": 500,
    }


def test_fit_subelement_targets_narrow_the_mating_axis(view_module) -> None:
    pin, hole = _cylinder_pair()
    ctx = make_ctx(active="Smoke", smoke_objects={"Pin": pin, "Hole": hole})
    view = ctx.Gui.views["Smoke"]
    doc = ctx.require_document("Smoke")
    a_target = geometry.make_reference(ctx, doc, pin, "face", 1)
    b_target = geometry.make_reference(ctx, doc, hole, "face", 1)

    result = view_module.capture_view(ctx, _fit_arguments(a=a_target, b=b_target))

    assert result["focus"] is None
    assert result["a"] == a_target
    assert result["b"] == b_target
    section = result["views"][1]["section_plane"]
    assert section["normal"] == [1.0, 0.0, 0.0]
    assert section["point"] == [10.0, 10.0, 5.0]
    assert [call[0] for call in view.clip_calls] == [1, 0]


def test_fit_ambiguous_pair_refuses(view_module) -> None:
    pin = FakeShapeObject(
        "Pin",
        "Smoke",
        FakeShape(
            (5.0, 5.0, 0.0, 15.0, 15.0, 11.0),
            faces=[
                FakeFace(Cylinder((0.0, 0.0, 1.0), (10.0, 10.0, 5.0), 5.0)),
                FakeFace(Cylinder((0.0, 0.0, 1.0), (10.0, 10.0, 6.0), 5.0)),
            ],
        ),
    )
    hole = FakeShapeObject(
        "Hole",
        "Smoke",
        FakeShape(
            (5.0, 5.0, 0.0, 15.0, 15.0, 10.0),
            faces=[FakeFace(Cylinder((0.0, 0.0, 1.0), (10.0, 10.5, 5.0), 6.0))],
        ),
    )
    ctx = make_ctx(smoke_objects={"Pin": pin, "Hole": hole})
    view = ctx.Gui.views["Smoke"]

    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, _fit_arguments())

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert excinfo.value.details["reason"] == "ambiguous_mating_axis"
    assert excinfo.value.details["pairs"] == 2
    assert view.calls == []
    assert view.clip_calls == []
    assert FakeParamGet.set_calls == []


def test_fit_explicit_override_used(view_module) -> None:
    pin, hole = _cylinder_pair()
    ctx = make_ctx(active="Smoke", smoke_objects={"Pin": pin, "Hole": hole})
    view = ctx.Gui.views["Smoke"]

    result = view_module.capture_view(
        ctx,
        _fit_arguments(section_axis=[0.0, 0.0, 1.0], section_point=[2.0, 3.0, 4.0]),
    )

    section = result["views"][1]["section_plane"]
    assert section["normal"] == [1.0, 0.0, 0.0]
    assert section["point"] == [2.0, 3.0, 4.0]
    placement = view.clip_calls[0][3]
    assert (placement.Base.x, placement.Base.y, placement.Base.z) == (2.0, 3.0, 4.0)

    refusals_before = len(FakeParamGet.set_calls)
    for bad_arguments in (
        _fit_arguments(section_axis=[0.0, 0.0, 1.0]),
        _fit_arguments(section_point=[1.0, 1.0, 1.0]),
        _fit_arguments(section_axis=[0.0, 0.0, 0.0], section_point=[1.0, 1.0, 1.0]),
    ):
        fresh_ctx = make_ctx(smoke_objects={"Pin": pin, "Hole": hole})
        with pytest.raises(ToolError) as excinfo:
            view_module.capture_view(fresh_ctx, bad_arguments)
        assert excinfo.value.code == "VALIDATION_FAILED"
        assert excinfo.value.details["reason"] == "section_override_invalid"
        assert fresh_ctx.Gui.views["Smoke"].calls == []
        # The refusals never opened a capture session.
        assert len(FakeParamGet.set_calls) == refusals_before


def test_fit_with_focus_refuses(view_module) -> None:
    ctx = make_ctx()
    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(
            ctx,
            _fit_arguments(focus={"object": "Pin"}),
        )

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert excinfo.value.details["mode"] == "fit"
    assert ctx.Gui.views["Smoke"].calls == []
    assert ctx.Gui.messages == []
    assert ctx.App.set_active_calls == []
    assert FakeParamGet.set_calls == []


def test_restoration_failure_raises_with_failed_list(view_module) -> None:
    ctx = make_ctx(active="Other")
    view = ctx.Gui.views["Smoke"]
    view.camera_restore_error = RuntimeError("lost context")

    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args(width=640, height=480))

    assert excinfo.value.code == "GUI_DISPATCH_FAILED"
    assert excinfo.value.details["reason"] == "restoration_failed"
    assert excinfo.value.details["restoration"] == [
        {"item": "camera", "error": "RuntimeError: lost context"}
    ]
    # The remaining restores still ran.
    assert ctx.App.active_name == "Other"
    assert FakeParamGet.store == {
        "UseNavigationAnimations": True,
        "AnimationDuration": 500,
    }
    assert all(not os.path.exists(path) for path in view.saved_paths)


def test_mode_schemas_are_finite(view_module) -> None:
    (definition,) = [
        item for item in view_module.TOOL_DEFINITIONS if item["name"] == "capture_view"
    ]
    protocol.check_schema(definition["inputSchema"])
    protocol.check_schema(definition["outputSchema"])
    protocol.validate_schema({"document": "Smoke", "mode": "overview"}, definition["inputSchema"])
    protocol.validate_schema(
        {
            "document": "Smoke",
            "mode": "fit",
            "a": {"object": "Pin"},
            "b": {"object": "Hole", "subelement": "1.2.3"},
            "section_axis": [0.0, 0.0, 1.0],
            "section_point": [2.0, 3.0, 4.0],
        },
        definition["inputSchema"],
    )
    protocol.validate_schema(
        {
            "document": "Smoke",
            "mode": "detail",
            "focus": {
                "object": "Box",
                "query": [{"role": "face", "selector": ">Z"}],
            },
        },
        definition["inputSchema"],
    )
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_schema({"document": "Smoke", "mode": "spider"}, definition["inputSchema"])
    # Raw numeric subelement payloads are refused at the schema boundary.
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_schema(
            {
                "document": "Smoke",
                "mode": "detail",
                "focus": {"object": "Box", "subelement": 3},
            },
            definition["inputSchema"],
        )
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_schema({"document": "Smoke"}, definition["outputSchema"])
    protocol.validate_schema(
        {
            "mimeType": "image/png",
            "data": "cG5n",
            "width": 2048,
            "height": 1104,
            "document": "Smoke",
            "generation": 7,
            "mode": "overview",
            "focus": None,
            "view_name": None,
            "views": [
                {
                    "view": "Isometric",
                    "direction": [0.0, 0.0, 1.0],
                    "up": [0.0, -1.0, 0.0],
                    "section_plane": None,
                    "rect": {"x": 0, "y": 0, "w": 512, "h": 552},
                    "derived": False,
                }
            ],
            "captured_objects": ["Leg"],
            "truncated": False,
        },
        definition["outputSchema"],
    )
    protocol.validate_schema(
        {
            "mimeType": "image/png",
            "data": "cG5n",
            "width": 1024,
            "height": 552,
            "document": "Smoke",
            "generation": 7,
            "mode": "fit",
            "focus": None,
            "a": {"object": "Pin", "subelement": "1.2.3"},
            "b": {"object": "Hole"},
            "view_name": None,
            "views": [
                {
                    "view": "Isometric",
                    "direction": [0.0, 0.0, 1.0],
                    "up": [0.0, -1.0, 0.0],
                    "section_plane": None,
                    "rect": {"x": 0, "y": 0, "w": 512, "h": 552},
                    "derived": False,
                },
                {
                    "view": "MatingSection",
                    "direction": None,
                    "up": None,
                    "section_plane": {
                        "normal": [1.0, 0.0, 0.0],
                        "point": [10.0, 10.0, 5.0],
                    },
                    "rect": {"x": 512, "y": 0, "w": 512, "h": 552},
                    "derived": False,
                },
            ],
        },
        definition["outputSchema"],
    )


def test_removed_focus_fields_are_refused_at_the_schema(view_module) -> None:
    (definition,) = [
        item for item in view_module.TOOL_DEFINITIONS if item["name"] == "capture_view"
    ]
    sample_output = {
        "mimeType": "image/png",
        "data": "cG5n",
        "width": 640,
        "height": 480,
        "document": "Smoke",
        "generation": 7,
        "mode": "detail",
        "focus": None,
        "view_name": "Isometric",
        "views": [
            {
                "view": "Detail",
                "direction": [0.0, 0.0, 1.0],
                "up": [0.0, -1.0, 0.0],
                "section_plane": None,
                "rect": {"x": 0, "y": 0, "w": 640, "h": 480},
                "derived": False,
            }
        ],
    }
    for removed in ("focus_object", "focus_subelement"):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema(
                {**capture_args(), removed: "Box"},
                definition["inputSchema"],
            )
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_schema(
                {**sample_output, removed: "Box"},
                definition["outputSchema"],
            )


# ---------------------------------------------------------------------------
# inspect_user_context: bounded user-context snapshot.
# ---------------------------------------------------------------------------


def _minimal_png(width: int = 4, height: int = 3) -> bytes:
    """Build a structurally valid grayscale PNG with stdlib only."""

    def _chunk(ctype: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + ctype
            + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    scanlines = b"".join(b"\x00" + b"\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(scanlines))
        + _chunk(b"IEND", b"")
    )


class FakeContextView:
    """3D foreground view double for the context snapshot."""

    def __init__(
        self,
        size: tuple[int, int] = (2000, 1500),
        camera: str | None = None,
        camera_type: str | None = "Perspective",
    ) -> None:
        self.size = size
        self.camera = (
            camera
            if camera is not None
            else "#Inventor V2.1 ascii\nposition 1 2 3\norientation 0 0 1 0\nfocalDistance 10"
        )
        self.camera_type = camera_type
        self.save_calls: list[tuple[str, int, int, str]] = []
        self.save_error: Exception | None = None
        self.payload = _minimal_png()
        self.get_camera_error: Exception | None = None
        # State the tool must never touch during a capture.
        self.clipping = False
        self.transparency = 0

    def getCamera(self) -> str:
        if self.get_camera_error is not None:
            raise self.get_camera_error
        return self.camera

    def getSize(self) -> tuple[int, int]:
        return self.size

    def getCameraType(self) -> str:
        if self.camera_type is None:
            raise RuntimeError("no camera type")
        return self.camera_type

    def saveImage(self, path: str, width: int, height: int, style: str) -> None:
        self.save_calls.append((str(path), width, height, style))
        if self.save_error is not None:
            raise self.save_error
        with open(path, "wb") as handle:
            handle.write(self.payload)


class FakeContextQImage:
    """QtGui.QImage double with controllable decode outcome and dimensions."""

    ok = True
    dims = (768, 576)

    def __init__(self, path: str) -> None:
        self.path = str(path)

    def isNull(self) -> bool:
        return not FakeContextQImage.ok

    def width(self) -> int:
        return FakeContextQImage.dims[0]

    def height(self) -> int:
        return FakeContextQImage.dims[1]


def install_context_qimage(ok: bool = True, dims: tuple[int, int] = (768, 576)) -> None:
    """Point the stubbed PySide QtGui at the controllable QImage double."""

    FakeContextQImage.ok = ok
    FakeContextQImage.dims = dims
    sys.modules["PySide"].QtGui = types.SimpleNamespace(QImage=FakeContextQImage)


def make_context_object(
    name: str,
    doc_name: str,
    type_id: str = "Part::Feature",
    faces: int = 6,
    edges: int = 12,
    derived: Any = None,
) -> Any:
    """Context document object with a real document/name identity.

    The shape resolves through ``geometry.placed_shape`` exactly like a
    capture target, so minted tokens feed the real resolver unchanged.
    """

    shape = FakeShape(
        (0.0, 0.0, 0.0, 10.0, 10.0, 10.0),
        faces=[None] * faces,
        edges=[None] * edges,
    )
    obj = FakeShapeObject(name, doc_name, shape)
    obj.TypeId = type_id
    obj.Label = f"{name}Label"
    obj.Document = types.SimpleNamespace(Name=doc_name)
    if derived is not None:
        obj.isDerivedFrom = derived
    return obj


def make_context_ctx(
    view: Any = None,
    selection_items: list[Any] | None = None,
    active_name: str | None = "Smoke",
) -> FakeCtx:
    """A context-shaped FakeCtx: two documents, distinct live objects."""

    documents = {
        "Smoke": FakeAppDocument("Smoke"),
        "Other": FakeAppDocument("Other"),
    }
    box = make_context_object("Box", "Smoke")
    lid = make_context_object("Lid", "Other")
    link = make_context_object("Rod", "Smoke", type_id="App::Link", faces=0, edges=0)
    sketch = make_context_object(
        "Sketch",
        "Smoke",
        type_id="Sketcher::SketchObject",
        faces=0,
        edges=0,
        derived=lambda type_name: type_name == "Sketcher::SketchObject",
    )
    sketch_plain = make_context_object(
        "SketchPlain", "Smoke", type_id="Sketcher::SketchObject", faces=0, edges=0
    )
    documents["Smoke"].Objects = [box, link, sketch, sketch_plain]
    documents["Other"].Objects = [lid]
    ctx = FakeCtx(
        documents,
        {"Smoke": FakeView(), "Other": FakeView()},
        {
            "Smoke": {"Box": box, "Rod": link, "Sketch": sketch, "SketchPlain": sketch_plain},
            "Other": {"Lid": lid},
        },
    )
    ctx._context_objects = {
        "Smoke": {"Box": box, "Rod": link, "Sketch": sketch, "SketchPlain": sketch_plain},
        "Other": {"Lid": lid},
    }
    ctx.App.active_name = active_name
    ctx._generations = {"Smoke": 7, "Other": 2}
    if view is None:
        view = FakeContextView()
    ctx._context_view = view
    gui_doc = FakeGuiDocument(
        FakeView(),
        active_object=types.SimpleNamespace(Object=box),
        edit_holder=None,
    )
    ctx._context_gui_doc = gui_doc
    ctx.Gui.getDocument = lambda name: gui_doc  # type: ignore[method-assign]
    ctx.Gui.context_window = view
    ctx.Gui.selection.ex_items = list(selection_items or [])
    return ctx


def assert_context_valid(view_module: Any, result: dict[str, Any]) -> None:
    """Validate one handler result against the registered raw schema."""

    protocol.validate_schema(result, view_module._CONTEXT_OUTPUT_SCHEMA)


def test_context_selection_rows_carry_generations_and_targets(view_module) -> None:
    ctx = make_context_ctx()
    box = ctx._context_objects["Smoke"]["Box"]
    lid = ctx._context_objects["Other"]["Lid"]
    ctx.Gui.selection.ex_items = [
        FakeSelectionObject(box, [], "Smoke", "Box"),
        FakeSelectionObject(box, ["Edge1", "Edge3"], "Smoke", "Box"),
        FakeSelectionObject(lid, ["Face2"], "Other", "Lid"),
    ]

    result = view_module.inspect_user_context(ctx, {})

    selection = result["selection"]
    assert selection["status"] == "available"
    assert selection["count"] == 4
    assert selection["truncated"] is False
    rows = selection["entries"]
    assert [(row["object"], row["subelement"]) for row in rows] == [
        ("Box", None),
        ("Box", "Edge1"),
        ("Box", "Edge3"),
        ("Lid", "Face2"),
    ]
    assert [row["generation"] for row in rows] == [7, 7, 7, 2]
    assert rows[0]["target"] == {"object": "Box"}
    assert all(row["target"] is not None for row in rows)
    assert all(row["reason"] is None for row in rows)
    # The explicit resolve-disabling native call shape, exactly once.
    assert ctx.Gui.selection.ex_calls == [("*", 0)]
    # No activation, selection change, or preference write happened.
    assert ctx.App.set_active_calls == []
    assert ctx.Gui.set_active_calls == []
    assert ctx.Gui.messages == []
    assert FakeParamGet.set_calls == []
    assert_context_valid(view_module, result)


def test_context_targets_resolve_and_go_stale(view_module) -> None:
    ctx = make_context_ctx()
    box = ctx._context_objects["Smoke"]["Box"]
    lid = ctx._context_objects["Other"]["Lid"]
    ctx.Gui.selection.ex_items = [
        FakeSelectionObject(box, ["Edge1"], "Smoke", "Box"),
        FakeSelectionObject(lid, ["Face2"], "Other", "Lid"),
    ]
    result = view_module.inspect_user_context(ctx, {})
    edge_row = result["selection"]["entries"][0]
    face_row = result["selection"]["entries"][1]

    edge_obj, edge_selection = geometry._resolve_target(
        ctx, ctx.require_document("Smoke"), edge_row["target"], "focus"
    )
    face_obj, face_selection = geometry._resolve_target(
        ctx, ctx.require_document("Other"), face_row["target"], "focus"
    )
    assert edge_obj is box and edge_selection["role"] == "edge"
    assert edge_selection["index"] == 1
    assert face_obj is lid and face_selection["role"] == "face"
    assert face_selection["index"] == 2

    # A generation bump makes the same token stale for the real resolver.
    ctx._generations["Smoke"] = 8
    with pytest.raises(ToolError) as excinfo:
        geometry._resolve_target(ctx, ctx.require_document("Smoke"), edge_row["target"], "focus")
    assert excinfo.value.details["reason"] == "stale_generation"

    # A same-name replacement document fails identity matching.
    ctx._generations["Smoke"] = 7
    ctx.App.documents["Smoke"] = FakeAppDocument("Smoke")
    with pytest.raises(ToolError) as excinfo:
        geometry._resolve_target(ctx, ctx.require_document("Smoke"), edge_row["target"], "focus")
    assert excinfo.value.details["reason"] == "document_mismatch"


def test_context_unsupported_rows_stay_descriptive(view_module) -> None:
    ctx = make_context_ctx()
    box = ctx._context_objects["Smoke"]["Box"]
    link = ctx._context_objects["Smoke"]["Rod"]
    sketch = ctx._context_objects["Smoke"]["Sketch"]
    sketch_plain = ctx._context_objects["Smoke"]["SketchPlain"]
    ctx.Gui.selection.ex_items = [
        FakeSelectionObject(link, ["Face1"], "Smoke", "Rod"),
        FakeSelectionObject(link, [], "Smoke", "Rod"),
        FakeSelectionObject(sketch, ["Edge1"], "Smoke", "Sketch"),
        FakeSelectionObject(sketch_plain, ["Edge1"], "Smoke", "SketchPlain"),
        FakeSelectionObject(box, ["Face1.Edge2"], "Smoke", "Box"),
        FakeSelectionObject(box, ["Vertex1"], "Smoke", "Box"),
        FakeSelectionObject(box, ["Face0"], "Smoke", "Box"),
        FakeSelectionObject(box, ["Face99"], "Smoke", "Box"),
    ]

    result = view_module.inspect_user_context(ctx, {})

    rows = result["selection"]["entries"]
    by_subelement = {row["subelement"]: row for row in rows}
    assert by_subelement["Face1"]["reason"] == "unsupported_instance"
    # A whole linked instance keeps its instance name.
    assert rows[1]["subelement"] is None
    assert rows[1]["target"] == {"object": "Rod"}
    sketch_rows = [
        row
        for row in rows
        if row["object"] in ("Sketch", "SketchPlain") and row["subelement"] == "Edge1"
    ]
    assert len(sketch_rows) == 2
    for row in sketch_rows:
        assert row["reason"] == "sketch_subelement"
        assert row["target"] is None
    assert by_subelement["Face1.Edge2"]["reason"] == "unsupported_subelement"
    assert by_subelement["Vertex1"]["reason"] == "unsupported_subelement"
    assert by_subelement["Face0"]["reason"] == "unsupported_subelement"
    assert by_subelement["Face99"]["reason"] == "selection_geometry_unavailable"
    # No source-object target is ever minted for a diagnostic row.
    for subelement in ("Face1", "Face1.Edge2", "Vertex1", "Face0", "Face99"):
        assert by_subelement[subelement]["target"] is None
    assert result["selection"]["count"] == 8
    assert_context_valid(view_module, result)


def test_context_selection_states(view_module) -> None:
    # Empty selection: available with count 0 and no marker.
    ctx = make_context_ctx()
    result = view_module.inspect_user_context(ctx, {})
    assert result["selection"] == {
        "status": "available",
        "count": 0,
        "entries": [],
        "truncated": False,
    }
    assert "selection" not in result["unavailable"]

    # Failing global selection read: unavailable, count null, no rows.
    ctx = make_context_ctx()
    ctx.Gui.selection.ex_error = RuntimeError("selection exploded")
    result = view_module.inspect_user_context(ctx, {})
    assert result["selection"] == {
        "status": "unavailable",
        "count": None,
        "entries": [],
        "truncated": False,
    }
    assert "selection" in result["unavailable"]

    # One unreadable row between valid neighbors is retained locally.
    ctx = make_context_ctx()
    box = ctx._context_objects["Smoke"]["Box"]
    ctx.Gui.selection.ex_items = [
        FakeSelectionObject(box, ["Edge1"], "Smoke", "Box"),
        UnreadableSelectionObject(),
        FakeSelectionObject(box, ["Edge2"], "Smoke", "Box"),
    ]
    result = view_module.inspect_user_context(ctx, {})
    rows = result["selection"]["entries"]
    assert len(rows) == 3
    assert rows[0]["target"] is not None and rows[2]["target"] is not None
    assert rows[1]["reason"] == "selection_object_unavailable"
    assert rows[1]["target"] is None
    assert result["selection"]["count"] is None
    assert result["selection"]["status"] == "unavailable"
    assert "selection" in result["unavailable"]
    assert_context_valid(view_module, result)


def test_context_truncation_counts_flattened_rows(view_module) -> None:
    wide = make_context_object("Wide", "Smoke", faces=1, edges=200)
    ctx = make_context_ctx()
    ctx._context_objects["Smoke"]["Wide"] = wide
    ctx.objects["Smoke"]["Wide"] = wide
    ctx.Gui.selection.ex_items = [
        FakeSelectionObject(wide, [f"Edge{i}" for i in range(1, 33)], "Smoke", "Wide"),
        FakeSelectionObject(wide, [f"Edge{i}" for i in range(33, 65)], "Smoke", "Wide"),
    ]
    result = view_module.inspect_user_context(ctx, {})
    assert result["selection"]["count"] == 64
    assert len(result["selection"]["entries"]) == 64
    assert result["selection"]["truncated"] is False

    ctx.Gui.selection.ex_items.append(FakeSelectionObject(wide, ["Edge65"], "Smoke", "Wide"))
    result = view_module.inspect_user_context(ctx, {})
    assert result["selection"]["count"] == 65
    assert len(result["selection"]["entries"]) == 64
    assert result["selection"]["truncated"] is True
    assert_context_valid(view_module, result)


def test_context_identities_and_non_3d_view(view_module) -> None:
    # Distinct active and edit object identities survive unwrapping.
    ctx = make_context_ctx()
    sketch = ctx._context_objects["Smoke"]["Sketch"]
    ctx._context_gui_doc._edit_holder = types.SimpleNamespace(Object=sketch)
    result = view_module.inspect_user_context(ctx, {})
    assert result["activeDocument"] == {"name": "Smoke", "label": "Smoke", "generation": 7}
    assert result["activeObject"] == {"document": "Smoke", "object": "Box"}
    assert result["editObject"] == {"document": "Smoke", "object": "Sketch"}
    assert result["activeObject"] != result["editObject"]
    assert result["workbench"] == "PartDesign"
    assert result["unavailable"] == []

    # No active document: empty state, not an unavailable read.
    ctx = make_context_ctx(active_name=None)
    result = view_module.inspect_user_context(ctx, {})
    assert result["activeDocument"] is None
    assert result["activeObject"] is None
    assert result["editObject"] is None
    assert result["unavailable"] == []

    # A non-3D foreground view stays descriptive with null camera and
    # viewport, marks both unavailable, and activates nothing.
    plain_window = types.SimpleNamespace()
    ctx = make_context_ctx(view=plain_window)
    window_calls_before = ctx.Gui.active_window_calls
    result = view_module.inspect_user_context(ctx, {"include_image": True})
    assert result["activeView"]["type"] == repr(plain_window)
    assert result["activeView"]["camera"] is None
    assert result["activeView"]["viewport"] is None
    assert {"camera", "viewport", "image"} <= set(result["unavailable"])
    assert ctx.Gui.active_window_calls == window_calls_before + 1
    assert_context_valid(view_module, result)

    # A failing foreground-window getter marks activeView unavailable.
    ctx = make_context_ctx()
    ctx.Gui.active_window_error = RuntimeError("no window")
    result = view_module.inspect_user_context(ctx, {})
    assert result["activeView"] is None
    assert "activeView" in result["unavailable"]


def test_context_image_capture(view_module) -> None:
    def install_event_tripwires(pumped: list[str]) -> None:
        """Fail the test if the tool pumps events or updates the GUI."""

        def _forbidden(*_args: Any) -> None:
            raise AssertionError("event pumping attempted during context capture")

        sys.modules["FreeCADGui"].updateGui = _forbidden
        sys.modules["PySide"].QtWidgets.QApplication.processEvents = lambda *a: pumped.append(
            "pump"
        )

    # 2000x1500 scales down to 768x576; capture uses the Current style.
    ctx = make_context_ctx()
    install_context_qimage(ok=True, dims=(768, 576))
    pumped: list[str] = []
    install_event_tripwires(pumped)
    box = ctx._context_objects["Smoke"]["Box"]
    lid = ctx._context_objects["Other"]["Lid"]
    selection_before = [
        FakeSelectionObject(box, ["Edge1", "Edge3"], "Smoke", "Box"),
        FakeSelectionObject(lid, ["Face2"], "Other", "Lid"),
    ]
    ctx.Gui.selection.ex_items = list(selection_before)
    ctx._context_gui_doc._edit_holder = types.SimpleNamespace(
        Object=ctx._context_objects["Smoke"]["Sketch"]
    )
    ctx._context_view.camera = "#Inventor V2.1 ascii\nposition 4 5 6\norientation 0 1 0 0.5"
    ctx._context_view.clipping = True
    ctx._context_view.transparency = 40
    preferences_before = dict(FakeParamGet.store)
    active_before = ctx.App.active_name

    result = view_module.inspect_user_context(ctx, {"include_image": True})

    assert result["mimeType"] == "image/png"
    assert result["width"] == 768
    assert result["height"] == 576
    png_bytes = base64.b64decode(result["data"])
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    (path, width, height, style) = ctx._context_view.save_calls[0]
    assert (width, height, style) == (768, 576, "Current")
    assert len(ctx._context_view.save_calls) == 1
    assert not os.path.exists(path)
    assert result["unavailable"] == []
    assert FakeParamGet.set_calls == []
    # Byte/value-identical state: selection, active document, edit
    # session, camera, clipping, transparency and preferences.
    observed_selection = [
        (so.DocumentName, so.ObjectName, so.SubElementNames) for so in ctx.Gui.selection.ex_items
    ]
    expected_selection = [
        (so.DocumentName, so.ObjectName, so.SubElementNames) for so in selection_before
    ]
    assert observed_selection == expected_selection
    assert ctx.Gui.selection.clear_count == 0
    assert ctx.App.active_name == active_before
    assert ctx._context_gui_doc._edit_holder is not None
    assert ctx._context_view.camera == "#Inventor V2.1 ascii\nposition 4 5 6\norientation 0 1 0 0.5"
    assert ctx._context_view.clipping is True
    assert ctx._context_view.transparency == 40
    assert FakeParamGet.store == preferences_before
    assert pumped == []
    assert ctx.Gui.active_window_calls == 1
    assert_context_valid(view_module, result)

    # Small viewports are never upscaled.
    ctx = make_context_ctx(view=FakeContextView(size=(400, 300)))
    install_context_qimage(ok=True, dims=(400, 300))
    result = view_module.inspect_user_context(ctx, {"include_image": True})
    assert (result["width"], result["height"]) == (400, 300)
    assert ctx._context_view.save_calls[0][1:3] == (400, 300)

    for ok, dims, save_error, payload in (
        (True, (768, 576), None, b""),  # empty capture
        (False, (768, 576), None, _minimal_png()),  # undecodable image
        (True, (640, 480), None, _minimal_png()),  # dimension mismatch
        (True, (768, 576), RuntimeError("save failed"), _minimal_png()),
    ):
        ctx = make_context_ctx()
        ctx._context_view.save_error = save_error
        ctx._context_view.payload = payload
        install_context_qimage(ok=ok, dims=dims)
        result = view_module.inspect_user_context(ctx, {"include_image": True})
        assert "image" in result["unavailable"]
        for field in ("mimeType", "data", "width", "height"):
            assert field not in result
        assert result["selection"]["count"] == 0
        assert not os.path.exists(ctx._context_view.save_calls[0][0])
        assert_context_valid(view_module, result)

    # include_image false performs no capture at all.
    ctx = make_context_ctx()
    result = view_module.inspect_user_context(ctx, {})
    assert ctx._context_view.save_calls == []
    assert "image" not in result["unavailable"]
