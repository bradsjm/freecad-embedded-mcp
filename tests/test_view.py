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

    def addSelection(self, obj: Any, subelement: str | None = None) -> None:
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
    def __init__(self, size: tuple[int, int] = (2000, 1500)) -> None:
        self.size = size
        self.calls: list[Any] = []
        self.animation_flags: list[Any] = []
        self.camera_calls: list[str] = []
        self.save_error: Exception | None = None
        self.empty = False
        self.saved_paths: list[str] = []

    def getCamera(self) -> str:
        return "#Inventor V2.1 ascii FakeCamera {}"

    def setCamera(self, camera: str) -> None:
        self.camera_calls.append(str(camera))

    def getSize(self) -> tuple[int, int]:
        return self.size

    def fitAll(self) -> None:
        self.calls.append("fitAll")

    def saveImage(self, path, width, height, mode, method) -> None:
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

    def require_object(self, doc: Any, name: str) -> Any:
        obj = self.objects.get(doc.Name, {}).get(name)
        if obj is None:
            raise ToolError("OBJECT_NOT_FOUND", f"unknown object '{name}'", {"object": name})
        return obj

    def check_document_idle(self, doc: Any) -> None:
        pass


@contextmanager
def load_view_module() -> Iterator[types.ModuleType]:
    """Load mcp_server/tools/view.py against stubbed FreeCAD/Qt modules."""

    module_names = ["FreeCAD", "FreeCADGui", "PySide", "mcp_server.tools.view"]
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in module_names}

    FakeParamGet.reset()
    freecad = types.ModuleType("FreeCAD")
    freecad.ParamGet = FakeParamGet
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
) -> FakeCtx:
    documents = {
        "Smoke": FakeAppDocument("Smoke"),
        "Other": FakeAppDocument("Other"),
    }
    box = types.SimpleNamespace(
        Name="Box",
        _doc="Smoke",
        Shape=types.SimpleNamespace(Faces=[None] * 6, Edges=[None] * 12),
    )
    other_obj = types.SimpleNamespace(Name="Lid", _doc="Other")
    if gui_views is None:
        gui_views = {"Smoke": FakeView(), "Other": FakeView()}
    ctx = FakeCtx(
        documents,
        gui_views,
        {"Smoke": {"Box": box}, "Other": {"Lid": other_obj}},
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
