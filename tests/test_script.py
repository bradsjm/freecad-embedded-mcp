"""Tests for mcp_server/tools/script.py (run_script).

Loaded in isolation with FreeCAD stubs: no FreeCAD import on the host.
"""

from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import threading
import types
from typing import Any, Iterator

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
SCRIPT_PATH = ADDON_DIR / "mcp_server" / "tools" / "script.py"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.protocol import ToolError


@contextmanager
def load_script() -> Iterator[types.ModuleType]:
    module_name = f"_script_test_{id(object())}"
    saved = sys.modules.pop("mcp_server.tools.script", None)
    try:
        spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        if saved is not None:
            sys.modules["mcp_server.tools.script"] = saved


class FakeCtx:
    def __init__(self) -> None:
        self.App = types.SimpleNamespace(Name="FreeCAD")
        self.Gui = types.SimpleNamespace(Name="Gui")
        self.script_namespaces: dict[str, dict[str, Any]] = {}
        self.active_solves: dict[str, Any] = {}
        self.cancel_event: threading.Event | None = None


def call(module: types.ModuleType, code: str, **kwargs: Any) -> Any:
    ctx = kwargs.pop("ctx")
    arguments: dict[str, Any] = {"code": code, **kwargs}
    return module.run_script(ctx, arguments)


def test_success_captures_stdout_and_persists_the_namespace() -> None:
    with load_script() as script:
        ctx = FakeCtx()
        result = call(script, "print(6 * 7)", ctx=ctx)

        assert result == {
            "session_id": "default",
            "stdout": "42\n",
            "stderr": "",
            "executed": True,
        }
        namespace = ctx.script_namespaces["default"]
        assert namespace["App"] is ctx.App
        assert namespace["FreeCAD"] is ctx.App
        assert namespace["Gui"] is ctx.Gui

        # Live state persists across calls in the same session.
        call(script, "answer = 42", ctx=ctx, session_id="default")
        again = call(script, "print(answer)", ctx=ctx, session_id="default")
        assert again["stdout"] == "42\n"
        assert ctx.script_namespaces["default"] is namespace


def test_explicit_session_ids_are_separate() -> None:
    with load_script() as script:
        ctx = FakeCtx()
        call(script, "marker = 'a'", ctx=ctx, session_id="alpha")
        call(script, "marker = 'b'", ctx=ctx, session_id="beta")
        first = call(script, "print(marker)", ctx=ctx, session_id="alpha")
        second = call(script, "print(marker)", ctx=ctx, session_id="beta")
        assert first["stdout"] == "a\n"
        assert second["stdout"] == "b\n"
        assert set(ctx.script_namespaces) == {"alpha", "beta"}


def test_failure_raises_tool_error_with_stdout_stderr_and_traceback() -> None:
    with load_script() as script:
        ctx = FakeCtx()
        with pytest.raises(ToolError) as excinfo:
            call(script, "print('before'); raise ValueError('smoke')", ctx=ctx)

        error = excinfo.value
        assert error.code == "VALIDATION_FAILED"
        details = error.details
        assert details["stdout"] == "before\n"
        assert details["session_id"] == "default"
        assert "ValueError: smoke" in details["traceback"]
        assert "Traceback (most recent call last)" in details["traceback"]
        # stderr capture is wired even when unused by the failure.
        assert details["stderr"] == ""
        # The namespace survives the failure (live state, not evicted).
        assert ctx.script_namespaces["default"]


def test_namespace_cap_rejects_new_sessions_without_eviction() -> None:
    with load_script() as script:
        ctx = FakeCtx()
        for index in range(32):
            call(script, f"value = {index}", ctx=ctx, session_id=f"s{index}")
        assert len(ctx.script_namespaces) == 32

        with pytest.raises(ToolError) as excinfo:
            call(script, "value = 99", ctx=ctx, session_id="overflow")

        assert excinfo.value.code == "VALIDATION_FAILED"
        assert excinfo.value.details["limit"] == 32
        assert "overflow" not in excinfo.value.details["sessions"]
        # Full, but every stored session is still usable.
        assert (
            call(script, "print(value)", ctx=ctx, session_id="s31")["stdout"] == "31\n"
        )
        assert len(ctx.script_namespaces) == 32


def test_refused_while_a_fem_solve_is_active() -> None:
    with load_script() as script:
        ctx = FakeCtx()
        ctx.active_solves["doc-identity"] = object()

        with pytest.raises(ToolError) as excinfo:
            call(script, "print('never')", ctx=ctx)

        assert excinfo.value.code == "SERVER_BUSY"
        assert excinfo.value.details["active_solves"] == ["doc-identity"]
        assert ctx.script_namespaces == {}


def test_cancelled_before_execution_never_runs_the_code() -> None:
    with load_script() as script:
        ctx = FakeCtx()
        cancel_event = threading.Event()
        cancel_event.set()
        ctx.cancel_event = cancel_event

        result = call(script, "executed_marker = True; print('side effect')", ctx=ctx)

        assert result == {
            "session_id": "default",
            "stdout": "",
            "stderr": "",
            "executed": False,
        }
        assert "executed_marker" not in ctx.script_namespaces["default"]
