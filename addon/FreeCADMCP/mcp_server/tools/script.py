"""run_script — arbitrary FreeCAD code in per-session namespaces (PLAN item 17).

Executes on the GUI thread inside a persistent per-session namespace seeded
with ``FreeCAD``/``App``/``Gui``. stdout/stderr/traceback are captured even
on failure and reported through structured ToolError details. Execution is
synchronous — there is no untracked async path; deadlines are enforced by
the server, which reports the uninterruptible limitation truthfully while
the code keeps running. The capture is hardened against script-owned
stream damage: a script may close or break its own streams, but the
retained text stays readable and any unrecoverable capture failure is
reported as a structured error with ``operationState:
"may_have_changed"``, never as a GUI traceback.

``run_script`` executes arbitrary code with the FreeCAD user's privileges
and is explicitly NOT sandboxed by allowed_roots: the bearer token is full
local code-execution authority. While any FEM solve is active the tool is
refused as busy because script target documents cannot be inferred.
"""

from __future__ import annotations

import io
import sys
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from mcp_server.protocol import SERVER_BUSY, VALIDATION_FAILED, ToolError

# Stored namespaces are bounded; new sessions are rejected when full rather
# than evicting live state (PLAN item 17).
# Stream caps: capture is bounded while the script writes, so an output
# flood never grows a buffer past the limit; the flags make truncation
# explicit instead of silent (autonomous-context contract).
OUTPUT_LIMIT_CHARS = 65536
TRACEBACK_LIMIT_CHARS = 32768

SESSION_LIMIT = 32

_SESSION_ID_MAX_LENGTH = 128

#: Bound on the retained code of one active task (PLAN item 12): keeps
#: detached ``run_script`` retention below the transport message cap.
CODE_MAX_LENGTH = 1_048_576

_RUN_SCRIPT_DEFINITION: dict[str, Any] = {
    "name": "run_script",
    "description": (
        "Execute arbitrary Python code on the FreeCAD GUI thread inside a "
        "persistent per-session namespace seeded with FreeCAD/App/Gui. "
        "stdout, stderr and the traceback are captured even when the script "
        "fails, even when the script closes or damages the streams "
        "themselves; the result reports cancellation_requested and "
        "deadline_exceeded factually. Execution is not interruptible and "
        "not sandboxed: the bearer token grants full local code-execution "
        "authority. Refused while a FEM solve is active."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "minLength": 1, "maxLength": CODE_MAX_LENGTH},
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
            "cancellation_requested": {"type": "boolean"},
            "deadline_exceeded": {"type": "boolean"},
        },
        "required": [
            "session_id",
            "stdout",
            "stderr",
            "stdoutTruncated",
            "stderrTruncated",
            "executed",
            "cancellation_requested",
            "deadline_exceeded",
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
            "cancellation_requested": True,
            "deadline_exceeded": _deadline_exceeded(ctx),
        }

    # One allocator for every script session: the server owns the cap, the
    # refusal code and the seeding, so this module only executes code.
    namespace = ctx.ensure_script_namespace(session_id)

    stdout = _BoundedStream(OUTPUT_LIMIT_CHARS)
    stderr = _BoundedStream(OUTPUT_LIMIT_CHARS)
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    failed = False
    exc_info: tuple[Any, Any, Any] = (None, None, None)
    stdout_capture: tuple[str, bool] | None = None
    stderr_capture: tuple[str, bool] | None = None
    try:
        sys.stdout = stdout
        sys.stderr = stderr
        exec(compile(code, f"<run_script:{session_id}>", "exec"), namespace)
    except BaseException:
        failed = True
        exc_info = sys.exc_info()
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        # Capture the retained text after the real streams are restored and
        # before anything is formatted, so even script-owned stream damage
        # cannot leak into the reporting path.
        stdout_capture = _captured_stream(stdout)
        stderr_capture = _captured_stream(stderr)

    if failed:
        try:
            traceback_text, traceback_truncated = _format_traceback_tail(exc_info)
        except Exception:
            # A hostile exception must not turn the structured failure
            # path into an unstructured GUI traceback.
            traceback_text, traceback_truncated = "", False
        # Arbitrary code may have mutated documents before raising: the
        # truthful state is may_have_changed, never a clean rollback claim.
        raise ToolError(
            VALIDATION_FAILED,
            _script_error_message(traceback_text),
            details={
                "session_id": session_id,
                "stdout": "" if stdout_capture is None else stdout_capture[0],
                "stderr": "" if stderr_capture is None else stderr_capture[0],
                "stdoutTruncated": (True if stdout_capture is None else stdout_capture[1]),
                "stderrTruncated": (True if stderr_capture is None else stderr_capture[1]),
                "traceback": traceback_text,
                "tracebackTruncated": traceback_truncated,
                "cancellation_requested": _cancel_requested(ctx),
                "deadline_exceeded": _deadline_exceeded(ctx),
                "operationState": "may_have_changed",
            },
        ) from None

    if stdout_capture is None or stderr_capture is None:
        # Script-owned stream damage defeated the capture; report a
        # structured failure instead of exposing any GUI traceback. The
        # code itself may have completed, so the state stays truthful.
        raise ToolError(
            VALIDATION_FAILED,
            "capturing the script output failed; the code may have completed",
            details={
                "session_id": session_id,
                "cancellation_requested": _cancel_requested(ctx),
                "deadline_exceeded": _deadline_exceeded(ctx),
                "operationState": "may_have_changed",
            },
        )

    return {
        "session_id": session_id,
        "stdout": stdout_capture[0],
        "stderr": stderr_capture[0],
        "stdoutTruncated": stdout_capture[1],
        "stderrTruncated": stderr_capture[1],
        "executed": True,
        "cancellation_requested": _cancel_requested(ctx),
        "deadline_exceeded": _deadline_exceeded(ctx),
    }


def _cancel_requested(ctx: Any) -> bool:
    """True through the shared event (tasks/cancel, deadline sweep, stop)."""
    event = getattr(ctx, "cancel_event", None)
    return event is not None and event.is_set()


def _deadline_exceeded(ctx: Any) -> bool:
    """True when the operation deadline has passed; a factual payload flag.

    A detached task stays ``completed`` when the code finished; the server
    reads these booleans from the terminal payload instead of guessing.
    """
    deadline_mono = getattr(ctx, "deadline_mono", None)
    if deadline_mono is None:
        return False
    clock = getattr(ctx, "_clock", None)
    try:
        now = clock() if callable(clock) else time.monotonic()
        return now >= float(deadline_mono)
    except Exception:
        return False


def _captured_stream(stream: _BoundedStream) -> tuple[str, bool] | None:
    """Read one captured stream; ``None`` only for unrecoverable damage."""
    try:
        return stream.captured()
    except Exception:
        return None


class _BoundedStream(io.StringIO):
    """Captured stream that retains at most ``limit`` characters.

    Text is capped as it is written, so a flood cannot grow the capture
    buffer past the limit: the excess is dropped and only the truncation
    flag records it. Writes report the length they were offered, like a
    pipe that silently drops what it cannot hold.
    """

    def __init__(self, limit: int) -> None:
        """Initialize the bounded capture with its retention limit."""
        super().__init__()
        self._limit = limit
        self._truncated = False
        self._closed = False
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        """Retain only the head up to ``limit``, always report the offered length."""
        with self._lock:
            if self._closed:
                raise ValueError("I/O operation on closed file")
            if not text:
                return 0
            room = self._limit - self.tell()
            if room <= 0:
                self._truncated = True
                return len(text)
            kept = text[:room]
            super().write(kept)
            if len(kept) < len(text):
                self._truncated = True
        return len(text)

    def close(self) -> None:
        """Mark the stream closed without closing the retained buffer.

        ``io.StringIO.close()`` would destroy the captured evidence, so
        only the closed flag is set: later writes raise ``ValueError``
        like a closed ``StringIO``, while ``captured()`` keeps returning
        the retained text.
        """
        with self._lock:
            self._closed = True

    @property
    def closed(self) -> bool:
        """True once closed, through close() or the raw underlying buffer."""
        return self._closed or super().closed

    def captured(self) -> tuple[str, bool]:
        """Return the retained head and whether anything was dropped.

        Never raises: even script-owned damage that closed the underlying
        buffer only makes the retained text unreadable, reported as lost.
        """
        with self._lock:
            try:
                text = self.getvalue()
            except ValueError:
                return "", True
            return text, self._truncated


def _format_traceback_tail(exc_info: tuple[Any, Any, Any]) -> tuple[str, bool]:
    """Format the exception, keeping only its last characters.

    The traceback is consumed as a stream of chunks into a sliding tail, so
    neither a deep stack nor a huge exception message is joined into one
    unbounded string before the cap applies.
    """

    exc_type, exc_value, exc_tb = exc_info
    if exc_type is None:
        return "", False
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
    """Extract the final line of a traceback as the user-facing error."""
    lines = traceback_text.strip().splitlines()
    return lines[-1] if lines else "the script raised an exception"


HANDLERS["run_script"] = run_script
