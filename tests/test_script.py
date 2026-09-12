"""Tests for mcp_server/tools/script.py (run_script).

Loaded in isolation with FreeCAD stubs: no FreeCAD import on the host.
"""

import importlib.util
import sys
import threading
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
        self.allocation_calls: list[str] = []

    def ensure_script_namespace(self, session_id: str) -> dict[str, Any]:
        """Server-contract double: get-or-create one seeded namespace."""
        self.allocation_calls.append(session_id)
        namespace = self.script_namespaces.get(session_id)
        if namespace is None:
            namespace = {"FreeCAD": self.App, "App": self.App, "Gui": self.Gui}
            self.script_namespaces[session_id] = namespace
        return namespace


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
            "stdoutTruncated": False,
            "stderrTruncated": False,
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
        # The message is the last traceback line, as before the cap existed.
        assert error.message == "ValueError: smoke"
        details = error.details
        assert details["stdout"] == "before\n"
        assert details["session_id"] == "default"
        assert "ValueError: smoke" in details["traceback"]
        assert "Traceback (most recent call last)" in details["traceback"]
        assert details["tracebackTruncated"] is False
        # stderr capture is wired even when unused by the failure.
        assert details["stderr"] == ""
        # The namespace survives the failure (live state, not evicted).
        assert ctx.script_namespaces["default"]


def test_session_allocation_goes_through_the_server_allocator() -> None:
    """The 32-session cap and its refusal code are the server's policy; this
    module must never allocate a namespace on its own."""

    with load_script() as script:
        ctx = FakeCtx()
        call(script, "marker = 1", ctx=ctx)
        call(script, "marker = 2", ctx=ctx, session_id="other")

        assert ctx.allocation_calls == ["default", "other"]

    class RefusingCtx(FakeCtx):
        def ensure_script_namespace(self, session_id: str) -> dict[str, Any]:
            raise ToolError("SERVER_BUSY", "script session limit reached")

    with load_script() as script:
        ctx = RefusingCtx()
        with pytest.raises(ToolError) as excinfo:
            call(script, "print('never')", ctx=ctx)
        assert excinfo.value.code == "SERVER_BUSY"
        assert ctx.script_namespaces == {}


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
            "stdoutTruncated": False,
            "stderrTruncated": False,
            "executed": False,
        }
        # A cancelled call allocates no session at all.
        assert ctx.script_namespaces == {}
        assert ctx.allocation_calls == []


def test_output_of_exactly_the_limit_is_not_flagged() -> None:
    with load_script() as script:
        ctx = FakeCtx()
        # 65535 characters plus the trailing newline is exactly the cap.
        result = call(script, "print('x' * 65535)", ctx=ctx)

        assert result["stdout"] == "x" * 65535 + "\n"
        assert result["stdoutTruncated"] is False
        assert result["stderrTruncated"] is False


def test_output_over_the_limit_keeps_the_head_and_sets_the_flag() -> None:
    with load_script() as script:
        ctx = FakeCtx()
        result = call(script, "print('x' * 65536)", ctx=ctx)

        assert result["stdout"] == "x" * script.OUTPUT_LIMIT_CHARS
        assert result["stdoutTruncated"] is True


def test_output_flood_never_grows_the_capture_buffers_past_the_limit() -> None:
    with load_script() as script:
        ctx = FakeCtx()
        result = call(
            script,
            "import sys\n"
            "sys.stdout.write('x' * 200000)\n"
            "for _ in range(1000):\n"
            "    sys.stderr.write('y' * 1000)\n"
            "observed = len(sys.stdout.getvalue()), len(sys.stderr.getvalue())\n",
            ctx=ctx,
        )

        # The buffers are already capped while the script runs, not after it
        # returns: the flood is dropped on write instead of being retained.
        assert ctx.script_namespaces["default"]["observed"] == (
            script.OUTPUT_LIMIT_CHARS,
            script.OUTPUT_LIMIT_CHARS,
        )
        assert result["stdout"] == "x" * script.OUTPUT_LIMIT_CHARS
        assert result["stderr"] == "y" * script.OUTPUT_LIMIT_CHARS
        assert result["stdoutTruncated"] is True
        assert result["stderrTruncated"] is True
        assert result["executed"] is True


def test_failure_details_are_capped_and_report_may_have_changed() -> None:
    with load_script() as script:
        ctx = FakeCtx()
        with pytest.raises(ToolError) as excinfo:
            call(
                script,
                "print('y' * 70000)\npartial_marker = 1\nraise ValueError('boom')",
                ctx=ctx,
            )

        details = excinfo.value.details
        assert details["stdout"] == "y" * script.OUTPUT_LIMIT_CHARS
        assert details["stdoutTruncated"] is True
        assert details["stderrTruncated"] is False
        assert details["operationState"] == "may_have_changed"
        # The mutation before the raise really happened; the state is
        # reported truthfully as may-have-changed, not rolled back.
        assert ctx.script_namespaces["default"]["partial_marker"] == 1


def test_traceback_is_streamed_into_the_capped_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with load_script() as script:
        ctx = FakeCtx()
        limit = script.TRACEBACK_LIMIT_CHARS
        chunks = [
            "Traceback (most recent call last):\n",
            "  a deep frame\n",
            "Z" * (limit + 100),  # one oversized chunk, as a huge message is
            "\n",
        ]

        class FakeTracebackException:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def format(self, **_kwargs: Any) -> Iterator[str]:
                # A generator, exactly like TracebackException.format: the
                # caller must cap while consuming, never collect it first.
                return iter(chunks)

        monkeypatch.setattr(
            script.traceback,
            "TracebackException",
            FakeTracebackException,
        )
        monkeypatch.setattr(
            script.traceback,
            "format_exc",
            lambda: pytest.fail("format_exc materializes the whole traceback"),
        )
        monkeypatch.setattr(
            script.traceback,
            "format_exception",
            lambda *_args, **_kwargs: pytest.fail(
                "format_exception collects every chunk before returning"
            ),
        )

        with pytest.raises(ToolError) as excinfo:
            call(script, "raise ValueError('boom')", ctx=ctx)

        details = excinfo.value.details
        assert details["traceback"] == "".join(chunks)[-limit:]
        assert details["tracebackTruncated"] is True
        assert details["operationState"] == "may_have_changed"
