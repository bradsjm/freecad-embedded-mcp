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
import traceback
from collections.abc import Callable
from typing import Any

from mcp_server.protocol import VALIDATION_FAILED, ToolError

# Agreed with ServerIntegration: shared busy rejection code.
SERVER_BUSY = "SERVER_BUSY"

# Stored namespaces are bounded; new sessions are rejected when full rather
# than evicting live state (PLAN item 17).
# Stream caps: kept output stays bounded in every path; the flags make
# truncation explicit instead of silent (autonomous-context contract).
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

    namespaces = ctx.script_namespaces
    namespace = namespaces.get(session_id)
    if namespace is None:
        if len(namespaces) >= SESSION_LIMIT:
            raise ToolError(
                VALIDATION_FAILED,
                f"the script session limit of {SESSION_LIMIT} was reached; "
                f"refusing new session {session_id!r} instead of evicting "
                "live state",
                details={
                    "limit": SESSION_LIMIT,
                    "sessions": sorted(namespaces),
                },
            )
        namespace = _seed_namespace(ctx)
        namespaces[session_id] = namespace

    cancel_event = getattr(ctx, "cancel_event", None)
    if cancel_event is not None and cancel_event.is_set():
        # Safe boundary: the operation was cancelled before execution, so
        # the code truthfully never ran and no state changed.
        return {
            "session_id": session_id,
            "stdout": "",
            "stderr": "",
            "stdoutTruncated": False,
            "stderrTruncated": False,
            "executed": False,
        }

    stdout = io.StringIO()
    stderr = io.StringIO()
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = stdout
        sys.stderr = stderr
        exec(compile(code, f"<run_script:{session_id}>", "exec"), namespace)
    except BaseException:
        stdout_text, stdout_truncated = _cap_head(stdout.getvalue())
        stderr_text, stderr_truncated = _cap_head(stderr.getvalue())
        traceback_text, traceback_truncated = _cap_tail(traceback.format_exc())
        # Arbitrary code may have mutated documents before raising: the
        # truthful state is may_have_changed, never a clean rollback claim.
        raise ToolError(
            VALIDATION_FAILED,
            _script_error_message(),
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

    stdout_text, stdout_truncated = _cap_head(stdout.getvalue())
    stderr_text, stderr_truncated = _cap_head(stderr.getvalue())
    return {
        "session_id": session_id,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdoutTruncated": stdout_truncated,
        "stderrTruncated": stderr_truncated,
        "executed": True,
    }


def _cap_head(text: str) -> tuple[str, bool]:
    """Keep the first OUTPUT_LIMIT_CHARS characters; report truncation."""

    if len(text) > OUTPUT_LIMIT_CHARS:
        return text[:OUTPUT_LIMIT_CHARS], True
    return text, False


def _cap_tail(text: str) -> tuple[str, bool]:
    """Keep the last TRACEBACK_LIMIT_CHARS characters; report truncation."""

    if len(text) > TRACEBACK_LIMIT_CHARS:
        return text[-TRACEBACK_LIMIT_CHARS:], True
    return text, False


def _seed_namespace(ctx: Any) -> dict[str, Any]:
    return {"FreeCAD": ctx.App, "App": ctx.App, "Gui": ctx.Gui}


def _script_error_message() -> str:
    lines = traceback.format_exc().strip().splitlines()
    return lines[-1] if lines else "the script raised an exception"


HANDLERS["run_script"] = run_script
