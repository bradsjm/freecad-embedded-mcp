"""Focused tests for mcp_server/tools/recovery.py staging-file cleanup.

Runs on the host: the module is pure stdlib plus ``mcp_server.protocol``, so
it imports without FreeCAD. The checkpoint itself needs a live document, so
these tests pin the one contract that protects other files: cleanup may
remove only the exact staging file this call created.
"""

import os
import sys
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.tools import recovery


def _identity(path: Path) -> tuple[int, ...]:
    # Mirror the five stat fields production captures for stage ownership.
    status = os.stat(path)
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def test_remove_created_removes_the_stage_it_still_owns(tmp_path: Path) -> None:
    stage = tmp_path / ".checkpoint-stage.FCStd"
    stage.write_text("ours")

    recovery._remove_created(str(stage), _identity(stage))

    assert not stage.exists()


def test_remove_created_leaves_a_replacement_file_alone(tmp_path: Path) -> None:
    """The reserved stage path can be replaced while save/verify runs.

    Deleting that replacement would destroy a file this checkpoint never
    created, so an identity mismatch must abort the cleanup.
    """

    stage = tmp_path / ".checkpoint-stage.FCStd"
    stage.write_text("ours")
    reserved = _identity(stage)
    stage.unlink()
    stage.write_text("someone else's")

    recovery._remove_created(str(stage), reserved)

    assert stage.exists()
    assert stage.read_text() == "someone else's"


def test_remove_created_ignores_an_unknown_or_missing_stage(tmp_path: Path) -> None:
    stage = tmp_path / "gone.FCStd"

    # No reservation identity (the reserve itself failed): nothing is ours.
    recovery._remove_created(str(stage), None)
    assert not stage.exists()

    # The stage already disappeared: cleanup is a no-op, never an error.
    recovery._remove_created(str(stage), (1, 1))
    assert not stage.exists()
