import sys
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.dispatch_health import DispatchHealth, stuck_failure


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_health_moves_from_busy_to_stuck_and_rejects_immediately() -> None:
    clock = FakeClock()
    health = DispatchHealth(clock)
    assert health.snapshot()["state"] == "healthy"
    assert health.rejection() is None
    health.start(7, "execute_code")
    clock.now += 90.0

    busy = health.snapshot()
    stuck = health.mark_timed_out(7, 90)
    rejection = health.rejection()

    assert busy["state"] == "busy"
    assert busy["running_for_seconds"] == 90.0
    assert stuck is not None
    assert stuck["state"] == "stuck"
    assert rejection is not None
    assert rejection["code"] == "GUI_DISPATCH_STUCK"
    assert "execute_code" in rejection["error"]
    assert "restart FreeCAD" in rejection["error"]


def test_finishing_timed_out_task_restores_healthy_state() -> None:
    clock = FakeClock()
    health = DispatchHealth(clock)
    health.start(11, "delete_object")
    health.mark_timed_out(11, 60)

    health.finish(11)

    assert health.snapshot()["state"] == "healthy"
    assert health.rejection() is None


def test_other_task_cannot_mark_or_clear_active_task() -> None:
    clock = FakeClock()
    health = DispatchHealth(clock)
    health.start(3, "create_object")

    assert health.mark_timed_out(4, 60) is None
    health.finish(4)

    assert health.snapshot()["state"] == "busy"
    assert health.snapshot()["task_id"] == 3


def test_stuck_failure_reports_running_time_never_the_total_budget() -> None:
    """60 s ran inside a 90 s budget: no text may claim 90 s of execution."""
    clock = FakeClock()
    health = DispatchHealth(clock)
    health.start(21, "run_fem")
    clock.now += 60.0

    snapshot = health.mark_timed_out(21, 90)
    rejection = health.rejection()

    assert snapshot is not None
    assert rejection is not None
    timed_out = stuck_failure(snapshot, just_timed_out=True)["error"]
    refused = rejection["error"]
    assert "exceeded its 90s total wait budget" in timed_out
    assert "'run_fem' was still running" in timed_out
    assert "exceeded its 90s total wait budget and is still running (60.0s running)" in refused
    for text in (timed_out, refused):
        # The allowance is a queue-plus-run budget, never execution time.
        assert "90.0s elapsed" not in text
        assert "timed out after 90s" not in text
        assert "cannot be safely cancelled" in text
    # The structured evidence keeps the real split unchanged.
    assert snapshot["running_for_seconds"] == 60.0
    assert snapshot["timeout_seconds"] == 90.0
    assert rejection["code"] == "GUI_DISPATCH_STUCK"
    assert rejection["dispatch"]["operation"] == "run_fem"
