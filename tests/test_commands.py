"""Native UI command tests (controller, actions, Connection Details).

Shares ``test_server``'s harness for the FreeCAD stubs and the single
server-module binding, then installs a RICHER fake PySide before loading
``mcp_server.commands`` under importlib. Covers: the pure status-to-UI
mapping, contextual lifecycle action synchronization and dispatch,
transition states, consolidated settings validation and persistence,
command registration, active-vs-saved connection settings, wildcard
endpoint mapping, token masking in Connection Details, and the draining
to stopped transition only after true completion.
"""

import sys
import types
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import test_server as ts

# ---------------------------------------------------------------------------
# Rich fake PySide (replaces the minimal harness stub before commands loads).
# ---------------------------------------------------------------------------


class FakeSignal:
    # probes["qt.signals"]: queued Qt signals deliver on the
    # application thread between GUI operations.
    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self, *args) -> None:
        for slot in list(self._slots):
            slot(*args)


class FakeQObject:
    def __init__(self, parent=None) -> None:
        self._qt_parent = parent


class FakeTimer:
    instances: list["FakeTimer"] = []

    def __init__(self, parent=None) -> None:
        self.interval: int | None = None
        self.timeout = FakeSignal()
        self.running = False
        FakeTimer.instances.append(self)

    def setInterval(self, milliseconds: int) -> None:
        self.interval = milliseconds

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    @staticmethod
    def singleShot(_delay, _callback) -> None:
        pass


class FakeSignalBlocker:
    def __init__(self, *_args) -> None:
        self.blocked: list = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class FakeApp:
    instance_calls: list = []
    aboutToQuit = FakeSignal()

    @classmethod
    def instance(cls):
        cls.instance_calls.append(True)
        return cls

    @staticmethod
    def translate(_context, text):
        return text

    @staticmethod
    def clipboard():
        return FakeClipboard


class FakeClipboard:
    text_value = ""
    calls: list[str] = []

    @classmethod
    def setText(cls, value: str) -> None:
        cls.calls.append(value)
        cls.text_value = value


class FakeQtNamespace:
    ToolButtonTextBesideIcon = "text-beside-icon"


class FakeAction:
    def __init__(self, name: str) -> None:
        self._name = name
        self._enabled = True
        self._text = ""
        self._tooltip = ""
        self._icon = None
        self.set_enabled_calls: list[bool] = []

    def objectName(self) -> str:
        return self._name

    def isEnabled(self) -> bool:
        return self._enabled

    def setEnabled(self, value: bool) -> None:
        self._enabled = bool(value)
        self.set_enabled_calls.append(bool(value))

    def text(self) -> str:
        return self._text

    def setText(self, value: str) -> None:
        self._text = value

    def toolTip(self) -> str:
        return self._tooltip

    def setToolTip(self, value: str) -> None:
        self._tooltip = value

    def setIcon(self, value) -> None:
        self._icon = value


class FakeToolButton:
    def __init__(self, parent=None) -> None:
        self.clicked = FakeSignal()
        self._text = ""
        self._tooltip = ""
        self._icon = None
        self._accessible = None
        self._style = None
        self._auto_raise = False
        self._object_name = ""

    def setObjectName(self, name: str) -> None:
        self._object_name = name

    def setAutoRaise(self, value: bool) -> None:
        self._auto_raise = bool(value)

    def setToolButtonStyle(self, style) -> None:
        self._style = style

    def setAccessibleName(self, name: str) -> None:
        self._accessible = name

    def setIcon(self, icon) -> None:
        self._icon = icon

    def setText(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text

    def setToolTip(self, text: str) -> None:
        self._tooltip = text

    def toolTip(self) -> str:
        return self._tooltip


class FakeStatusBar:
    def __init__(self) -> None:
        self.messages: list[tuple[str, int]] = []
        self.permanent: list = []

    def showMessage(self, message: str, milliseconds: int = 0) -> None:
        self.messages.append((message, milliseconds))

    def addPermanentWidget(self, widget) -> None:
        self.permanent.append(widget)


class FakeMainWindow:
    def __init__(self, actions=None) -> None:
        self.status_bar = FakeStatusBar()
        self._actions = list(actions or [])

    def statusBar(self) -> FakeStatusBar:
        return self.status_bar

    def findChildren(self, _cls) -> list:
        return list(self._actions)


class FakeMessageBox:
    warnings: list[tuple] = []

    @classmethod
    def warning(cls, parent, title, text):
        cls.warnings.append((parent, title, text))


class FakeWidget:
    """Generic widget double; every setter records its argument."""

    instances: list["FakeWidget"] = []
    auto_accept = False

    def __init__(self, *args, **kwargs) -> None:
        self._text = args[0] if args and isinstance(args[0], str) else ""
        self._echo = None
        self._enabled = True
        self.checked = False
        self.toggled = FakeSignal()
        self.clicked = FakeSignal()
        self.rejected_signal = FakeSignal()
        self._parent = kwargs.get("parent")
        FakeWidget.instances.append(self)

    def setText(self, text) -> None:
        self._text = text

    def text(self):
        return self._text

    def setReadOnly(self, value) -> None:
        self._read_only = bool(value)

    def isReadOnly(self) -> bool:
        return getattr(self, "_read_only", False)

    def setPlaceholderText(self, text) -> None:
        self._placeholder = text

    def setEchoMode(self, mode) -> None:
        self._echo = mode

    def echoMode(self):
        return self._echo

    def setEnabled(self, value) -> None:
        self._enabled = bool(value)

    def isEnabled(self) -> bool:
        return self._enabled

    def isChecked(self) -> bool:
        return self.checked

    def setRange(self, minimum, maximum) -> None:
        self._range = (minimum, maximum)

    def setValue(self, value) -> None:
        self._value = value

    def value(self):
        return self._value

    def setPlainText(self, text) -> None:
        self._text = text

    def toPlainText(self):
        return self._text

    def setWordWrap(self, _value) -> None:
        pass

    def setIndent(self, _value) -> None:
        pass

    def hide(self) -> None:
        self._visible = False

    def setVisible(self, value) -> None:
        self._visible = bool(value)

    def accept(self) -> None:
        self._accepted = True

    def setChecked(self, value) -> None:
        self.checked = bool(value)

    def setWindowTitle(self, _title) -> None:
        pass

    def setMinimumWidth(self, _width) -> None:
        pass

    def exec(self) -> None:
        if self.auto_accept and FakeDialogButtonBox.instances:
            FakeDialogButtonBox.instances[-1].accepted.emit()

    def reject(self) -> None:
        self.rejected_signal.emit()

    Password = "password"
    Normal = "normal"

    def setTextValue(self, text) -> None:  # pragma: no cover - unused
        self._text = text


class FakeLayout:
    def addRow(self, *args) -> None:
        for arg in args:
            if hasattr(arg, "addWidget"):
                pass

    def addWidget(self, *args) -> None:
        pass

    def addStretch(self, *args) -> None:
        pass


class FakeDialogButtonBox:
    Close = 1
    Ok = 2
    Cancel = 4
    instances: list["FakeDialogButtonBox"] = []

    def __init__(self, _buttons, parent=None) -> None:
        self.accepted = FakeSignal()
        self.rejected = FakeSignal()
        FakeDialogButtonBox.instances.append(self)


def _build_fake_pyside():
    qt_core = types.SimpleNamespace(
        QObject=FakeQObject,
        QTimer=FakeTimer,
        QSignalBlocker=FakeSignalBlocker,
        QCoreApplication=FakeApp,
        Qt=FakeQtNamespace,
    )
    qt_gui = types.SimpleNamespace(
        QAction=FakeAction,
        QIcon=lambda path: {"icon": path},
    )
    qt_widgets = types.SimpleNamespace(
        QApplication=FakeApp,
        QMessageBox=FakeMessageBox,
        QToolButton=FakeToolButton,
        QDialog=FakeWidget,
        QLabel=FakeWidget,
        QLineEdit=FakeWidget,
        QCheckBox=FakeWidget,
        QSpinBox=FakeWidget,
        QPlainTextEdit=FakeWidget,
        QPushButton=FakeWidget,
        QFormLayout=lambda *_a, **_k: FakeLayout(),
        QHBoxLayout=lambda *_a, **_k: FakeLayout(),
        QDialogButtonBox=FakeDialogButtonBox,
        QInputDialog=types.SimpleNamespace(getText=lambda *_a, **_k: ("", False)),
        QWidget=types.SimpleNamespace(setTabOrder=lambda *_a: None),
    )
    pyside = types.ModuleType("PySide")
    pyside.QtCore = qt_core
    pyside.QtWidgets = qt_widgets
    pyside.QtGui = qt_gui
    return pyside, qt_core, qt_gui, qt_widgets


_pyside, _core, _gui, _widgets = _build_fake_pyside()
sys.modules["PySide"] = _pyside
sys.modules["PySide.QtCore"] = _core
sys.modules["PySide.QtGui"] = _gui
sys.modules["PySide.QtWidgets"] = _widgets

import mcp_server.server as server_module
from mcp_server import (
    commands,
    gui_dispatch,
    protocol,
)

SAVED_SETTINGS = {
    "port": 9876,
    "token": "saved-token-value",
    "auto_start": False,
    "remote_enabled": False,
    "allowed_ips": "127.0.0.1",
    "allowed_roots": ["/tmp/fc-test"],
}


def _status(**overrides):
    status = {
        "running": False,
        "state": "stopped",
        "port": None,
        "endpoint": None,
        "pendingOperations": 0,
        "gui": {"state": "healthy"},
        "connection": {
            "remote_enabled": False,
            "allowed_ips": "127.0.0.1",
            "allowed_roots": ["/tmp/fc-test"],
            "configured_port": 9876,
        },
    }
    status.update(overrides)
    return status


@pytest.fixture(autouse=True)
def _clean_commands_state(monkeypatch):
    ts.STUB_CALLS.clear()
    ts.PREFLIGHT_RESULTS.clear()
    ts.STUB_HANDLERS.clear()
    ts.FC_STATE["documents"] = {}
    server_module._server = None
    previous_waker = gui_dispatch._waker
    gui_dispatch._waker = None
    gui_dispatch._draining = False
    gui_dispatch._dispatch_health._active_task_id = 0
    gui_dispatch._dispatch_health._timed_out = False
    gui_dispatch._dispatch_health._timeout_seconds = 0.0
    ts._drain_gui_queue()
    monkeypatch.setattr(commands, "load_settings", lambda: dict(SAVED_SETTINGS))
    monkeypatch.setattr(commands, "save_settings", lambda settings: None)
    monkeypatch.setattr(FakeMessageBox, "warnings", [])
    FakeClipboard.calls = []
    FakeTimer.instances = []
    FakeWidget.instances.clear()
    FakeWidget.auto_accept = False
    FakeDialogButtonBox.instances.clear()
    commands._controller = None
    commands._REGISTERED = False
    ts.FakeConsole.messages.clear()
    yield
    gui_dispatch.shutdown()
    ts._drain_gui_queue()
    gui_dispatch.cleanup_waker()
    gui_dispatch._waker = previous_waker
    server_module._server = None
    commands._controller = None


# ---------------------------------------------------------------------------
# Pure status mapping.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Controller refresh.
# ---------------------------------------------------------------------------


def _make_window(monkeypatch, actions):
    window = FakeMainWindow(actions)
    monkeypatch.setattr(commands, "_main_window", lambda: window)
    return window


def test_controller_refresh_updates_indicator_and_shared_action(monkeypatch):
    action = FakeAction("Toggle_MCP_Server")
    window = _make_window(monkeypatch, [action])
    monkeypatch.setattr(
        server_module,
        "server_status",
        lambda: _status(state="running", pendingOperations=0),
    )
    controller = commands.McpUiController(window)

    assert controller._button.text() == "MCP: Running (Local only)"
    assert controller._button._accessible == "FreeCAD MCP server status"
    assert action.text() == "Stop MCP Server"
    assert action._icon["icon"].endswith("mcp-stop.svg")
    assert action.toolTip() == "Stop the embedded MCP server"
    assert action.isEnabled() is True
    assert controller._timer.interval == 500
    assert "Dispatch health" in controller._button.toolTip()
    assert SAVED_SETTINGS["token"] not in controller._button.toolTip()
    controller.shutdown()
    assert controller._timer.running is False


def test_initialize_ui_is_idempotent_and_survives_missing_window(monkeypatch):
    monkeypatch.setattr(commands, "_main_window", lambda: None)
    assert commands.initialize_ui() is None
    commands._controller = None
    _make_window(monkeypatch, [])
    first = commands.initialize_ui()
    second = commands.initialize_ui()
    assert first is not None and first is second
    assert len(FakeTimer.instances) == 1
    commands._controller = None


# ---------------------------------------------------------------------------
# Command UX and settings.
# ---------------------------------------------------------------------------


def test_settings_save_uses_existing_owner_and_never_restarts(monkeypatch):
    settings = dict(SAVED_SETTINGS)
    settings["token"] = ""
    settings["remote_enabled"] = True
    saved = []
    monkeypatch.setattr(commands, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(commands, "save_settings", lambda value: saved.append(dict(value)))
    monkeypatch.setattr(server_module, "start_server", lambda: pytest.fail("implicit start"))
    monkeypatch.setattr(server_module, "stop_server", lambda: pytest.fail("implicit stop"))
    monkeypatch.setattr(server_module, "server_status", lambda: _status(state="stopped"))
    FakeWidget.auto_accept = True

    commands.MCPSettingsCommand().Activated()

    assert len(saved) == 1
    assert saved[0]["port"] == 9876
    assert saved[0]["auto_start"] is False
    assert saved[0]["remote_enabled"] is True
    assert saved[0]["allowed_ips"] == "127.0.0.1"
    assert saved[0]["allowed_roots"] == ["/tmp/fc-test"]
    assert saved[0]["token"]


@pytest.mark.parametrize(
    "field,value",
    [("allowed_ips", "not-an-ip"), ("allowed_roots", [])],
)
def test_settings_refuses_invalid_ip_or_empty_roots(monkeypatch, field, value):
    settings = dict(SAVED_SETTINGS)
    settings[field] = value
    saved = []
    monkeypatch.setattr(commands, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(commands, "save_settings", lambda result: saved.append(result))
    FakeWidget.auto_accept = True

    commands.MCPSettingsCommand().Activated()

    assert saved == []


# ---------------------------------------------------------------------------
# Connection Details.
# ---------------------------------------------------------------------------


def test_connection_details_dialog_masks_token_and_maps_endpoint(monkeypatch):
    status = _status(
        state="running",
        running=True,
        port=9876,
        endpoint="http://0.0.0.0:9876/mcp",
        connection={
            "remote_enabled": True,
            "allowed_ips": "192.168.1.0/24",
            "configured_port": 9876,
        },
    )
    active = server_module.Server(
        settings=dict(SAVED_SETTINGS, token="active-token-value"),
        signer=protocol.ConsentSigner(),
        task_store=ts.tasks_module.TaskStore(),
        registry=ts.subs_module.SubscriptionRegistry(),
    )
    monkeypatch.setattr(server_module, "server_status", lambda: status)
    monkeypatch.setattr(server_module, "get_server", lambda: active)
    window = _make_window(monkeypatch, [])

    commands.ConnectionDetailsCommand().Activated()

    # The local endpoint is the loopback mapping; the wildcard address is
    # only ever the bind address. The active token is never echoed into
    # the status bar, console output or tooltips.
    endpoint_texts = [call[0] for call in window.status_bar.messages]
    assert endpoint_texts == []
    assert all("0.0.0.0" not in message for message in ts.FakeConsole.messages)
    assert all("0.0.0.0" not in message for message, _ms in window.status_bar.messages)
    # The dialog used the ACTIVE server's token, never a stale saved one.
    active_token = active.settings["token"]
    assert active_token not in window.status_bar.messages
    assert active_token not in "".join(ts.FakeConsole.messages)
    # Masking: one token field, holding the ACTIVE token, password-echoed
    # and read-only.
    token_fields = [
        widget for widget in FakeWidget.instances if widget.echoMode() == FakeWidget.Password
    ]
    assert len(token_fields) == 1
    assert token_fields[0].text() == active_token
    assert token_fields[0].isReadOnly() is True


def test_status_snapshots_never_contain_the_token():
    server = ts.make_server()
    status = server.status()
    serialized = repr(status)
    assert "test-token" not in serialized
    assert "token" not in status["connection"]
    module_status = server_module.server_status()
    assert "test-token" not in repr(module_status)


# ---------------------------------------------------------------------------
# Draining to stopped only after true completion.
# ---------------------------------------------------------------------------


def test_draining_finishes_into_stopped_only_after_true_completion():
    server = ts.make_server()
    with server._state_lock:
        server._state = "draining"
    # A retained operation keeps the server in draining.
    op = server._register_operation(
        "run_script", kind="blocking", task_id=None, principal="p", deadline_s=60
    )
    server._maybe_finish_draining()
    with server._state_lock:
        assert server._state == "draining"
    server._remove_op(op)
    with server._state_lock:
        assert server._state == "stopped"
    # server_status reflects the finish without another Start/Stop click.
    assert server.status()["state"] == "stopped"
