"""Native UI command tests (controller, actions, Connection Details).

Shares ``test_server``'s harness for the FreeCAD stubs and the single
server-module binding, then installs a RICHER fake PySide before loading
``mcp_server.commands`` under importlib. Covers: the pure status→UI
mapping shared by the controller and ``IsActive``, controller refresh
with signal-blocked checks and disabled toggles when settings are
unreadable, start/stop command UX (status bar, Report view, warning
dialogs, token-free output), failed settings persistence restoring the
previous check state, active-vs-saved security settings, the wildcard
endpoint mapping, token masking in Connection Details, and the draining
to stopped transition only after true completion.
"""

import importlib
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

import test_server as ts  # noqa: E402 - shared FreeCAD/server stub harness

# ---------------------------------------------------------------------------
# Rich fake PySide (replaces the minimal harness stub before commands loads).
# ---------------------------------------------------------------------------


class FakeSignal:
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
        self._checkable = False
        self._checked = False
        self.set_checked_calls: list[bool] = []
        self.set_enabled_calls: list[bool] = []

    def objectName(self) -> str:
        return self._name

    def isEnabled(self) -> bool:
        return self._enabled

    def setEnabled(self, value: bool) -> None:
        self._enabled = bool(value)
        self.set_enabled_calls.append(bool(value))

    def isCheckable(self) -> bool:
        return self._checkable

    def setCheckable(self, value: bool) -> None:
        self._checkable = bool(value)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, value: bool) -> None:
        self.set_checked_calls.append(bool(value))
        self._checked = bool(value)


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

    def __init__(self, *args, **kwargs) -> None:
        self._text = args[0] if args and isinstance(args[0], str) else ""
        self._echo = None
        self._enabled = True
        self.checked = False
        self.toggled = FakeSignal()
        self.clicked = FakeSignal()
        self.rejected_signal = FakeSignal()
        self._parent = kwargs.get("parent")

    def setText(self, text) -> None:
        self._text = text

    def text(self):
        return self._text

    def setReadOnly(self, value) -> None:
        self._read_only = bool(value)

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

    def setChecked(self, value) -> None:
        self.checked = bool(value)

    def setWindowTitle(self, _title) -> None:
        pass

    def setMinimumWidth(self, _width) -> None:
        pass

    def exec(self) -> None:
        pass

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
    Close = "close"

    def __init__(self, _buttons, parent=None) -> None:
        self.rejected = FakeSignal()


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
        QPushButton=FakeWidget,
        QFormLayout=lambda *_a, **_k: FakeLayout(),
        QHBoxLayout=lambda *_a, **_k: FakeLayout(),
        QDialogButtonBox=FakeDialogButtonBox,
        QInputDialog=types.SimpleNamespace(
            getText=lambda *_a, **_k: ("", False)
        ),
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

# server.py has already bound the harness's stub tool modules; drop the
# fake tool package from sys.modules so alphabetically-later test files
# import the real tool modules again.
for _name in [
    _key
    for _key in list(sys.modules)
    if _key == "mcp_server.tools" or _key.startswith("mcp_server.tools.")
]:
    del sys.modules[_name]

import mcp_server.commands as commands  # noqa: E402
import mcp_server.gui_dispatch as gui_dispatch  # noqa: E402
import mcp_server.protocol as protocol  # noqa: E402
import mcp_server.server as server_module  # noqa: E402
from mcp_server.settings import SettingsError  # noqa: E402

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
    monkeypatch.setattr(
        commands, "load_settings", lambda: dict(SAVED_SETTINGS)
    )
    monkeypatch.setattr(
        commands, "save_settings", lambda settings: None
    )
    monkeypatch.setattr(FakeMessageBox, "warnings", [])
    FakeClipboard.calls = []
    FakeTimer.instances = []
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


def test_indicator_state_mapping_covers_every_state():
    stopped = commands._indicator_state(_status(state="stopped"))
    assert stopped["text"] == "MCP: Stopped"
    assert stopped["start_enabled"] is True
    assert stopped["stop_enabled"] is False

    running_local = commands._indicator_state(_status(state="running"))
    assert running_local["text"] == "MCP: Running (Local)"
    assert running_local["start_enabled"] is False
    assert running_local["stop_enabled"] is True

    running_remote = commands._indicator_state(
        _status(
            state="running",
            connection={
                "remote_enabled": True,
                "allowed_ips": "",
                "configured_port": 9876,
            },
        )
    )
    assert running_remote["text"] == "MCP: Running (Remote)"

    starting = commands._indicator_state(_status(state="starting"))
    assert starting["text"] == "MCP: Starting"
    assert starting["start_enabled"] is False
    assert starting["stop_enabled"] is False

    draining = commands._indicator_state(
        _status(state="draining", pendingOperations=3)
    )
    assert draining["text"] == "MCP: Stopping (3 operations)"
    assert draining["start_enabled"] is False
    assert draining["stop_enabled"] is False


def test_running_with_stuck_gui_reports_blocked_but_keeps_stop():
    mapping = commands._indicator_state(
        _status(state="running", gui={"state": "stuck"})
    )
    assert mapping["text"] == "MCP: Running — GUI blocked"
    assert mapping["stop_enabled"] is True


def test_restart_required_distinguishes_active_vs_saved_settings():
    saved = {"remote_enabled": True, "allowed_ips": "", "port": 9876}
    active_remote = _status(
        state="running",
        connection={
            "remote_enabled": True,
            "allowed_ips": "",
            "configured_port": 9876,
        },
    )
    active_local = _status(state="running")
    assert commands._restart_required(saved, active_remote) is False
    assert commands._restart_required(saved, active_local) is True
    # A stopped server has nothing to restart.
    assert commands._restart_required(saved, _status(state="stopped")) is False


# ---------------------------------------------------------------------------
# Controller refresh.
# ---------------------------------------------------------------------------


def _make_window(monkeypatch, actions):
    window = FakeMainWindow(actions)
    monkeypatch.setattr(commands, "_main_window", lambda: window)
    return window


def test_controller_refresh_updates_indicator_and_action_state(monkeypatch):
    start_action = FakeAction("Start_MCP_Server")
    stop_action = FakeAction("Stop_MCP_Server")
    auto_action = FakeAction("Toggle_Auto_Start")
    remote_action = FakeAction("Toggle_Remote_Connections")
    window = _make_window(
        monkeypatch, [start_action, stop_action, auto_action, remote_action]
    )
    monkeypatch.setattr(
        server_module,
        "server_status",
        lambda: _status(state="running", pendingOperations=0),
    )

    controller = commands.McpUiController(window)

    assert controller._button is not None
    assert controller._button.text() == "MCP: Running (Local)"
    assert "FreeCAD MCP server status" == controller._button._accessible
    assert start_action.isEnabled() is False
    assert stop_action.isEnabled() is True
    assert auto_action.isChecked() is False
    assert remote_action.isChecked() is False
    # The indicator survives; the polling timer runs at 500 ms.
    assert controller._timer.interval == 500
    assert controller._timer.running is True
    # Tooltips carry health and pending work — never the token.
    tooltip = controller._button.toolTip()
    assert "Dispatch health" in tooltip
    assert SAVED_SETTINGS["token"] not in tooltip

    # Shutdown stops the timer only.
    controller.shutdown()
    assert controller._timer.running is False


def test_controller_disables_settings_toggles_when_settings_unreadable(
    monkeypatch,
):
    def _explode():
        raise SettingsError("settings file corrupt")

    monkeypatch.setattr(commands, "load_settings", _explode)
    auto_action = FakeAction("Toggle_Auto_Start")
    window = _make_window(monkeypatch, [auto_action])
    controller = commands.McpUiController(window)
    controller.refresh()
    assert auto_action.isEnabled() is False


def test_refresh_applies_only_differences_with_blocked_signals(monkeypatch):
    start_action = FakeAction("Start_MCP_Server")
    auto_action = FakeAction("Toggle_Auto_Start")
    auto_action.setCheckable(True)
    auto_action.setChecked(False)  # already correct: no setChecked call
    window = _make_window(monkeypatch, [start_action, auto_action])
    monkeypatch.setattr(
        server_module, "server_status", lambda: _status(state="stopped")
    )
    controller = commands.McpUiController(window)
    start_action.set_enabled_calls.clear()
    auto_action.set_checked_calls.clear()
    controller.refresh()
    assert start_action.isEnabled() is True
    assert start_action.set_enabled_calls == []  # unchanged, untouched
    assert auto_action.set_checked_calls == []  # already checked
    assert auto_action._checked is False


def test_initialize_ui_is_idempotent_and_survives_missing_window(monkeypatch):
    monkeypatch.setattr(commands, "_main_window", lambda: None)
    assert commands.initialize_ui() is None
    commands._controller = None
    window = _make_window(monkeypatch, [])
    first = commands.initialize_ui()
    second = commands.initialize_ui()
    assert first is not None and first is second
    assert len(FakeTimer.instances) == 1
    commands._controller = None


# ---------------------------------------------------------------------------
# Command UX.
# ---------------------------------------------------------------------------


def test_start_command_reports_running_state_and_endpoint(monkeypatch):
    window = _make_window(monkeypatch, [])
    monkeypatch.setattr(
        server_module,
        "start_server",
        lambda: _status(state="running", endpoint="http://127.0.0.1:9876/mcp"),
    )
    commands.StartMCPServerCommand().Activated()
    assert window.status_bar.messages[-1][0] == (
        "MCP server running at http://127.0.0.1:9876/mcp"
    )
    assert window.status_bar.messages[-1][1] == 5000


def test_failed_startup_shows_warning_dialog_and_report_entry(monkeypatch):
    window = _make_window(monkeypatch, [])

    def _explode():
        raise OSError("address already in use")

    monkeypatch.setattr(server_module, "start_server", _explode)
    commands.StartMCPServerCommand().Activated()
    assert FakeMessageBox.warnings, "user-triggered failure needs a dialog"
    _parent, title, text = FakeMessageBox.warnings[-1]
    assert title == "FreeCAD MCP"
    assert "address already in use" in text
    assert any("Start failed" in message for message in ts.FakeConsole.messages)


def test_stop_command_reports_stopping_with_active_operations(monkeypatch):
    window = _make_window(monkeypatch, [])
    monkeypatch.setattr(
        server_module,
        "server_status",
        lambda: _status(state="running", running=True),
    )

    def _stop():
        return _status(state="draining", pendingOperations=3)

    monkeypatch.setattr(server_module, "stop_server", _stop)
    commands.StopMCPServerCommand().Activated()
    assert window.status_bar.messages[-1][0] == (
        "MCP is stopping; 3 operations are still active."
    )


def test_stop_command_reports_plain_stop_when_drained(monkeypatch):
    window = _make_window(monkeypatch, [])
    monkeypatch.setattr(
        server_module,
        "server_status",
        lambda: _status(state="running", running=True),
    )

    def _stop():
        return _status(state="stopped")

    monkeypatch.setattr(server_module, "stop_server", _stop)
    commands.StopMCPServerCommand().Activated()
    assert window.status_bar.messages[-1][0] == "MCP server stopped."


def test_isactive_uses_the_shared_state_mapping(monkeypatch):
    monkeypatch.setattr(
        server_module, "server_status", lambda: _status(state="draining")
    )
    assert commands.StartMCPServerCommand().IsActive() is False
    assert commands.StopMCPServerCommand().IsActive() is False
    monkeypatch.setattr(
        server_module, "server_status", lambda: _status(state="stopped")
    )
    assert commands.StartMCPServerCommand().IsActive() is True
    monkeypatch.setattr(
        server_module, "server_status", lambda: _status(state="running")
    )
    assert commands.StartMCPServerCommand().IsActive() is False
    assert commands.StopMCPServerCommand().IsActive() is True


def test_failed_settings_persistence_restores_previous_check_state(
    monkeypatch,
):
    auto_action = FakeAction("Toggle_Auto_Start")
    window = _make_window(monkeypatch, [auto_action])
    controller = commands.McpUiController(window)

    def _explode(_settings):
        raise SettingsError("disk full")

    monkeypatch.setattr(commands, "save_settings", _explode)
    commands.ToggleAutoStartCommand().Activated(checked=True)
    # The user sees a native warning; the disk kept the previous value.
    assert FakeMessageBox.warnings
    assert any(
        "Cannot change auto-start" in message
        for message in ts.FakeConsole.messages
    )
    controller.refresh()
    # Refresh re-read the saved settings (still disabled) and blocks
    # signals while restoring the previous check state.
    assert auto_action._checked is False


def test_saved_settings_reload_after_successful_toggle(monkeypatch):
    auto_action = FakeAction("Toggle_Auto_Start")
    window = _make_window(monkeypatch, [auto_action])
    controller = commands.McpUiController(window)
    saved = dict(SAVED_SETTINGS)
    monkeypatch.setattr(
        commands,
        "load_settings",
        lambda: dict(saved),
    )

    def _save(settings):
        saved.update(settings)

    monkeypatch.setattr(commands, "save_settings", _save)
    commands.ToggleAutoStartCommand().Activated(checked=True)
    # The re-created controller (initialize_ui) refreshed with the new
    # saved settings; the stale direct instance never overwrites that.
    assert auto_action._checked is True


# ---------------------------------------------------------------------------
# Connection Details.
# ---------------------------------------------------------------------------


def test_connection_details_maps_wildcard_bind_to_local_endpoint():
    status = _status(
        state="running",
        port=9876,
        endpoint="http://0.0.0.0:9876/mcp",
        connection={
            "remote_enabled": True,
            "allowed_ips": "192.168.1.0/24",
            "configured_port": 9876,
        },
    )
    model = commands._connection_details(status, dict(SAVED_SETTINGS))
    assert model["endpoint"] == "http://127.0.0.1:9876/mcp"
    assert "0.0.0.0" not in model["endpoint"]
    assert model["bind_address"] == "0.0.0.0"
    assert model["endpoint_copyable"] is True
    assert model["mode"] == "Remote"


def test_connection_details_stopped_reports_configured_endpoint():
    model = commands._connection_details(_status(state="stopped"), None)
    assert model["listening"] is False
    assert model["endpoint"] == "http://127.0.0.1:9876/mcp"
    assert model["endpoint_copyable"] is True

    without_port = _status(
        state="stopped",
        connection={
            "remote_enabled": False,
            "allowed_ips": "",
            "configured_port": 0,
        },
    )
    model = commands._connection_details(without_port, None)
    assert model["endpoint"] is None
    assert model["endpoint_copyable"] is False


def test_connection_details_dialog_masks_token_and_maps_endpoint(monkeypatch):
    status = _status(
        state="running",
        port=9876,
        endpoint="http://0.0.0.0:9876/mcp",
        connection={
            "remote_enabled": True,
            "allowed_ips": "192.168.1.0/24",
            "configured_port": 9876,
        },
    )
    active = server_module.Server(
        settings=dict(SAVED_SETTINGS),
        signer=protocol.ConsentSigner(),
        task_store=ts.tasks_module.TaskStore(),
        registry=ts.subs_module.SubscriptionRegistry(),
    )
    monkeypatch.setattr(server_module, "server_status", lambda: status)
    monkeypatch.setattr(server_module, "get_server", lambda: active)
    window = _make_window(monkeypatch, [])

    commands.ShowAuthTokenCommand().Activated()

    # The local endpoint is the loopback mapping; the wildcard address is
    # only ever the bind address. The active token is never echoed into
    # the status bar, console output or tooltips.
    endpoint_texts = [
        call[0]
        for call in window.status_bar.messages
    ]
    assert endpoint_texts == []
    assert all(
        "0.0.0.0" not in message for message in ts.FakeConsole.messages
    )
    assert all("0.0.0.0" not in message for message, _ms in window.status_bar.messages)
    # The dialog used the ACTIVE server's token, never a stale saved one.
    active_token = active.settings["token"]
    assert active_token not in window.status_bar.messages
    assert active_token not in "".join(ts.FakeConsole.messages)


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
