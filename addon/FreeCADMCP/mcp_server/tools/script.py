"""run_script — arbitrary FreeCAD code in per-session namespaces (PLAN item 17).

Executes on the GUI thread inside a persistent per-session namespace seeded
with ``FreeCAD``/``App``/``Gui``. stdout/stderr/traceback are captured even
on failure and reported through structured ToolError details. Execution is
synchronous — there is no untracked async path; deadlines are enforced by
the server, which reports the uninterruptible limitation truthfully while
the code keeps running.

``run_script`` executes arbitrary code with the FreeCAD user's privileges
and is explicitly NOT sandboxed by allowed_roots: the bearer token is full
local code-execution authority. While any FEM solve is active the tool is
refused as busy because script target documents cannot be inferred.
"""

from __future__ import annotations

import io
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any

from mcp_server.protocol import VALIDATION_FAILED, ToolError

# Agreed with ServerIntegration: shared busy rejection code.
SERVER_BUSY = "SERVER_BUSY"

# Stored namespaces are bounded; new sessions are rejected when full rather
# than evicting live state (PLAN item 17).
# Stream caps: capture is bounded while the script writes, so an output
# flood never grows a buffer past the limit; the flags make truncation
# explicit instead of silent (autonomous-context contract).
OUTPUT_LIMIT_CHARS = 65536
TRACEBACK_LIMIT_CHARS = 32768

SESSION_LIMIT = 32

_SESSION_ID_MAX_LENGTH = 128

_RUN_SCRIPT_DEFINITION: dict[str, Any] = {
    "name": "run_script",
    "description": (
        "Execute arbitrary Python code on the FreeCAD GUI thread inside a "
        "persistent per-session namespace seeded with FreeCAD/App/Gui. "
        "stdout, stderr and the traceback are captured even when the script "
        "fails. Execution is not interruptible and not sandboxed: the "
        "bearer token grants full local code-execution authority. Refused "
        "while a FEM solve is active."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "minLength": 1},
            "session_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": _SESSION_ID_MAX_LENGTH,
                "default": "default",
            },
            "timeout_s": {
                # Enforced as a cooperative server deadline; execution itself
                # cannot be preempted.
                "type": "integer",
                "minimum": 1,
                "maximum": 3600,
                "default": 90,
            },
        },
        "required": ["code"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "stdoutTruncated": {"type": "boolean"},
            "stderrTruncated": {"type": "boolean"},
            "executed": {"type": "boolean"},
        },
        "required": [
            "session_id",
            "stdout",
            "stderr",
            "stdoutTruncated",
            "stderrTruncated",
            "executed",
        ],
        "additionalProperties": False,
    },
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [_RUN_SCRIPT_DEFINITION]

HANDLERS: dict[str, Callable[[Any, dict[str, Any]], Any]] = {}


def run_script(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run ``code`` in the session namespace; returns captured streams."""
    code = arguments["code"]
    session_id = arguments.get("session_id") or "default"

    active_solves = getattr(ctx, "active_solves", None) or {}
    if active_solves:
        raise ToolError(
            SERVER_BUSY,
            "run_script is refused while a FEM solve is active because its "
            "target documents cannot be inferred",
            details={"active_solves": sorted(active_solves)},
        )

    cancel_event = getattr(ctx, "cancel_event", None)
    if cancel_event is not None and cancel_event.is_set():
        # Safe boundary: the operation was cancelled before execution, so
        # the code truthfully never ran and no state changed — including the
        # session budget, which a cancelled call must not consume.
        return {
            "session_id": session_id,
            "stdout": "",
            "stderr": "",
            "stdoutTruncated": False,
            "stderrTruncated": False,
            "executed": False,
        }

    # One allocator for every script session: the server owns the cap, the
    # refusal code and the seeding, so this module only executes code.
    namespace = ctx.ensure_script_namespace(session_id)

    stdout = _BoundedStream(OUTPUT_LIMIT_CHARS)
    stderr = _BoundedStream(OUTPUT_LIMIT_CHARS)
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = stdout
        sys.stderr = stderr
        exec(compile(code, f"<run_script:{session_id}>", "exec"), namespace)
    except BaseException:
        stdout_text, stdout_truncated = stdout.captured()
        stderr_text, stderr_truncated = stderr.captured()
        traceback_text, traceback_truncated = _format_traceback_tail()
        # Arbitrary code may have mutated documents before raising: the
        # truthful state is may_have_changed, never a clean rollback claim.
        raise ToolError(
            VALIDATION_FAILED,
            _script_error_message(traceback_text),
            details={
                "session_id": session_id,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "stdoutTruncated": stdout_truncated,
                "stderrTruncated": stderr_truncated,
                "traceback": traceback_text,
                "tracebackTruncated": traceback_truncated,
                "operationState": "may_have_changed",
            },
        ) from None
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr

    stdout_text, stdout_truncated = stdout.captured()
    stderr_text, stderr_truncated = stderr.captured()
    return {
        "session_id": session_id,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdoutTruncated": stdout_truncated,
        "stderrTruncated": stderr_truncated,
        "executed": True,
    }


class _BoundedStream(io.StringIO):
    """Captured stream that retains at most ``limit`` characters.

    Text is capped as it is written, so a flood cannot grow the capture
    buffer past the limit: the excess is dropped and only the truncation
    flag records it. Writes report the length they were offered, like a
    pipe that silently drops what it cannot hold.
    """

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit
        self._truncated = False
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            room = self._limit - self.tell()
            if room <= 0:
                self._truncated = True
                return len(text)
            kept = text[:room]
            super().write(kept)
            if len(kept) < len(text):
                self._truncated = True
        return len(text)

    def captured(self) -> tuple[str, bool]:
        """Return the retained head and whether anything was dropped."""

        with self._lock:
            return self.getvalue(), self._truncated


def _format_traceback_tail() -> tuple[str, bool]:
    """Format the pending exception, keeping only its last characters.

    The traceback is consumed as a stream of chunks into a sliding tail, so
    neither a deep stack nor a huge exception message is joined into one
    unbounded string before the cap applies.
    """

    exc_type, exc_value, exc_tb = sys.exc_info()
    # ``compact=True`` is what traceback.format_exc() uses; the streaming
    # generator replaces it so the full text is never built.
    formatter = traceback.TracebackException(exc_type, exc_value, exc_tb, compact=True)
    tail = ""
    total = 0
    for chunk in formatter.format(chain=True):
        total += len(chunk)
        if len(chunk) >= TRACEBACK_LIMIT_CHARS:
            tail = chunk[-TRACEBACK_LIMIT_CHARS:]
        else:
            tail = (tail + chunk)[-TRACEBACK_LIMIT_CHARS:]
    return tail, total > TRACEBACK_LIMIT_CHARS


def _script_error_message(traceback_text: str) -> str:
    lines = traceback_text.strip().splitlines()
    return lines[-1] if lines else "the script raised an exception"


HANDLERS["run_script"] = run_script
