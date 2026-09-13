"""Tests for the ``capture_view`` tool (mcp_server/tools/view.py).

Loads the real module against stubbed FreeCAD/FreeCADGui/PySide, defending:
explicit focus prevalidation that never reframes on error, five-argument
Framebuffer capture only, the viewport/omitted/explicit size rules,
navigation-animation suppression with preference restoration, and
subelement-preserving selection + active-document restoration in ``finally``
— including when the capture itself fails.
"""

import base64
import importlib.util
import os
import sys
import tempfile
import types
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
    def __init__(self, obj: Any, subelements: list[str]) -> None:
        self.Object = obj
        self.SubElementNames = list(subelements)


class FakeSelection:
    def __init__(self) -> None:
        self.complete: list[dict[str, Any]] = []
        self.clear_count = 0

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

    def getSelectionEx(self, docname: str | None = None) -> list[FakeSelectionObject]:
        return [
            FakeSelectionObject(entry["object"], entry["subelements"])
            for entry in self.complete
            if docname is None or entry["doc"] == docname
        ]


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


class FakeGuiDocument:
    def __init__(self, view: Any) -> None:
        self.ActiveView = view


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

    def require_document(self, name: str) -> FakeAppDocument:
        doc = self.App.documents.get(name)
        if doc is None:
            raise ToolError("DOCUMENT_NOT_FOUND", f"unknown document '{name}'", {})
        return doc

    def document_generation(self, doc: Any) -> int:
        return 7

    def require_object(self, doc: Any, name: str) -> Any:
        obj = self.objects.get(doc.Name, {}).get(name)
        if obj is None:
            raise ToolError("OBJECT_NOT_FOUND", f"unknown object '{name}'", {"object": name})
        return obj

    def check_document_idle(self, doc: Any) -> None:
        pass


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
        "focus_object": "Box",
        "view_name": "Isometric",
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
    assert result["focus_object"] == "Box"
    assert result["focus_subelement"] is None
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


def test_unknown_focus_fails_without_reframing(view_module) -> None:
    ctx = make_ctx(active="Other")
    view = ctx.Gui.views["Smoke"]
    lid = ctx.objects["Other"]["Lid"]
    ctx.Gui.selection.addSelection(lid, "Face3")

    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args(focus_object="Nope"))

    assert excinfo.value.code == "OBJECT_NOT_FOUND"
    # Nothing touched the view: no orientation, no selection framing, no
    # capture, no fitAll fallback, no active-document switch, and no
    # navigation-animation preference change.
    assert view.calls == []
    assert ctx.App.set_active_calls == []
    assert ctx.Gui.set_active_calls == []
    assert ctx.Gui.selection.getSelectionEx("Other")[0].SubElementNames == ["Face3"]
    assert FakeParamGet.set_calls == []
    assert ctx.Gui.messages == []


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


def test_orientation_is_applied(view_module) -> None:
    ctx = make_ctx()
    view = ctx.Gui.views["Smoke"]
    view_module.capture_view(ctx, capture_args(view_name="Front"))
    assert view.calls[0] == "viewFront"


def test_omitted_size_scales_active_viewport_to_768(view_module) -> None:
    ctx = make_ctx(active="Smoke")
    view = ctx.Gui.views["Smoke"]
    result = view_module.capture_view(ctx, capture_args())
    save_call = next(call for call in view.calls if isinstance(call, tuple))
    assert save_call[1] == 768
    assert save_call[2] == 576
    assert (result["width"], result["height"]) == (768, 576)


def test_small_active_viewport_is_not_upscaled(view_module) -> None:
    ctx = make_ctx(
        active="Smoke",
        gui_views={"Smoke": FakeView(size=(640, 480)), "Other": FakeView()},
    )
    view = ctx.Gui.views["Smoke"]
    result = view_module.capture_view(ctx, capture_args())
    save_call = next(call for call in view.calls if isinstance(call, tuple))
    assert (save_call[1], save_call[2]) == (640, 480)
    assert (result["width"], result["height"]) == (640, 480)


def test_background_target_uses_active_viewport(view_module) -> None:
    # The raised tab is the sizing reference; the backgrounded target's own
    # 400x300 report is stale restored geometry and must not be trusted.
    ctx = make_ctx(
        active="Other",
        gui_views={"Smoke": FakeView(size=(400, 300)), "Other": FakeView(size=(1600, 900))},
    )
    view = ctx.Gui.views["Smoke"]
    result = view_module.capture_view(ctx, capture_args())
    save_call = next(call for call in view.calls if isinstance(call, tuple))
    assert (save_call[1], save_call[2]) == (768, 432)
    assert (result["width"], result["height"]) == (768, 432)


def test_unreliable_size_falls_back_without_active_viewport(view_module) -> None:
    ctx = make_ctx(
        gui_views={"Smoke": FakeView(size=(400, 300)), "Other": FakeView(size=(400, 300))}
    )
    view = ctx.Gui.views["Smoke"]
    result = view_module.capture_view(ctx, capture_args())
    save_call = next(call for call in view.calls if isinstance(call, tuple))
    assert (save_call[1], save_call[2]) == (768, 576)
    assert (result["width"], result["height"]) == (768, 576)


def test_one_omitted_side_uses_view_dimension_unclamped(view_module) -> None:
    ctx = make_ctx(active="Smoke")
    view = ctx.Gui.views["Smoke"]
    view_module.capture_view(ctx, capture_args(width=640))
    save_call = next(call for call in view.calls if isinstance(call, tuple))
    assert save_call[1] == 640
    assert save_call[2] == 1500


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


def test_explicit_4097_fails_before_capture(view_module) -> None:
    ctx = make_ctx()
    view = ctx.Gui.views["Smoke"]
    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args(height=4097))
    assert excinfo.value.code == "VALIDATION_FAILED"
    assert view.calls == []


def test_explicit_size_beyond_limit_is_rejected(view_module) -> None:
    ctx = make_ctx()
    view = ctx.Gui.views["Smoke"]
    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args(width=5000))
    assert excinfo.value.code == "VALIDATION_FAILED"
    assert view.calls == []


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
    ctx = make_ctx(active="Smoke")
    result = view_module.capture_view(
        ctx, capture_args(focus_subelement="Face3", width=640, height=480)
    )
    assert result["focus_subelement"] == "Face3"
    assert result["document"] == "Smoke"
    assert result["focus_object"] == "Box"
    assert result["view_name"] == "Isometric"
    assert ctx.Gui.messages == ["ViewSelection", "ViewSelection"]


def test_subelement_rejected_before_gui_changes(view_module) -> None:
    for bad in ("Vertex2", "Face99", "Edge99", "face3"):
        ctx = make_ctx()
        with pytest.raises(ToolError) as excinfo:
            view_module.capture_view(ctx, capture_args(focus_subelement=bad))
        assert excinfo.value.code == "VALIDATION_FAILED"
        assert ctx.Gui.messages == []
        assert ctx.App.set_active_calls == []
        assert FakeParamGet.set_calls == []


def test_unknown_view_name_fails_before_capture(view_module) -> None:
    ctx = make_ctx()
    view = ctx.Gui.views["Smoke"]
    with pytest.raises(ToolError) as excinfo:
        view_module.capture_view(ctx, capture_args(view_name="SpiderView"))
    assert excinfo.value.code == "UNSUPPORTED_VIEW"
    assert view.calls == []
    assert FakeParamGet.set_calls == []


def test_tool_schemas_are_finite(view_module) -> None:
    (definition,) = view_module.TOOL_DEFINITIONS
    protocol.check_schema(definition["inputSchema"])
    protocol.check_schema(definition["outputSchema"])


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

    result = view_module.capture_view(ctx, capture_args(view_name=None))

    assert result["generation"] == 7
    assert result["mode"] == "overview"
    assert result["focus_object"] == "Box"
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
    assert result["focus_object"] is None
    assert ctx.Gui.messages.count("ViewFit") == 14
    assert "ViewSelection" not in ctx.Gui.messages
    assert view.calls[0] == "viewIsometric"


def test_view_name_conflicts_with_sheet_modes(view_module) -> None:
    for mode in ("overview", "interior", "fit"):
        ctx = make_ctx()
        with pytest.raises(ToolError) as excinfo:
            view_module.capture_view(ctx, capture_args(view_name="Front", mode=mode))
        assert excinfo.value.code == "VALIDATION_FAILED"
        assert excinfo.value.details["reason"] == "view_name_mode_conflict"
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

    result = view_module.capture_view(
        ctx, capture_args(view_name=None, mode="detail", focus_subelement="Face1")
    )

    assert result["mode"] == "detail"
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
    for arguments in (
        capture_args(view_name=None, mode="detail"),
        capture_args(view_name=None, mode="detail", focus_subelement="Edge1"),
    ):
        ctx = make_ctx()
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
    assert result["focus_object"] is None
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


def test_compositor_places_panels_and_labels(view_module) -> None:
    paths = []
    for name in ("a", "b"):
        fd, path = tempfile.mkstemp(suffix=".png", prefix="mcp-test-")
        os.close(fd)
        with open(path, "wb") as handle:
            handle.write(b"panel-" + name.encode("ascii"))
        paths.append(path)
    try:
        sheet_bytes, rects = view_module._compose_sheet(
            [
                {
                    "label": "Left panel",
                    "path": paths[0],
                    "x": 0,
                    "y": 0,
                    "w": 512,
                    "h": 552,
                },
                {
                    "label": "Legend",
                    "x": 512,
                    "y": 0,
                    "w": 512,
                    "h": 552,
                    "legend": ["document: Smoke", "generation: 7"],
                },
            ]
        )
    finally:
        for path in paths:
            if os.path.exists(path):
                os.unlink(path)

    assert sheet_bytes == b"\x89PNG-composed"
    assert rects == [
        {"x": 0, "y": 0, "w": 512, "h": 552},
        {"x": 512, "y": 0, "w": 512, "h": 552},
    ]
    draws = [op for op in FakeQPainter.operations if op[0] == "drawImage"]
    assert [(op[1], op[2], op[3].args[0]) for op in draws] == [(0, 0, paths[0])]
    texts = [op for op in FakeQPainter.operations if op[0] == "drawText"]
    assert ("drawText", 12, 538, "Left panel") in texts
    assert ("drawText", 524, 20, "document: Smoke") in texts
    assert ("drawText", 524, 36, "generation: 7") in texts
    assert all(not os.path.exists(path) for path in FakeQImage.saved)


def test_mode_schemas_are_finite(view_module) -> None:
    (definition,) = view_module.TOOL_DEFINITIONS
    protocol.check_schema(definition["inputSchema"])
    protocol.check_schema(definition["outputSchema"])
    protocol.validate_schema({"document": "Smoke", "mode": "overview"}, definition["inputSchema"])
    protocol.validate_schema(
        {
            "document": "Smoke",
            "mode": "fit",
            "a": {"object": "Pin", "subelement": "Face1"},
            "b": {"object": "Hole"},
            "section_axis": [0.0, 0.0, 1.0],
            "section_point": [2.0, 3.0, 4.0],
        },
        definition["inputSchema"],
    )
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_schema({"document": "Smoke", "mode": "spider"}, definition["inputSchema"])
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
            "focus_object": None,
            "focus_subelement": None,
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
