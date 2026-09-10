"""Qt-stub tests for the v2 GUI dispatch lifecycle (mcp_server/gui_dispatch).

Runs the real module against stubbed FreeCAD/PySide, defending: queued vs
running timeouts, per-call isolation, reentrancy, exactly-once finalization,
cooperative cancellation that cannot falsely end running work, and the
initialize/shutdown/draining lifecycle.
"""

import concurrent.futures
import importlib.util
import sys
import threading
import time
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
GUI_DISPATCH_PATH = ADDON_DIR / "mcp_server" / "gui_dispatch.py"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))


class FakeSignal:
    # probes["qt.signals"]: queued Qt signals deliver on the
    # application thread between GUI operations.
    def __init__(self) -> None:
        self.callback = None

    def connect(self, callback, *_args) -> None:
        self.callback = callback

    def emit(self) -> None:
        if self.callback is not None:
            self.callback()


class FakeStatusBar:
    def showMessage(self, _message: str) -> None:
        pass

    def clearMessage(self) -> None:
        pass


class FakeApplication:
    @staticmethod
    def mouseButtons() -> int:
        return 0

    @staticmethod
    def activePopupWidget() -> None:
        return None

    @staticmethod
    def activeModalWidget() -> None:
        return None

    @staticmethod
    def instance() -> "FakeApplication":
        return FakeApplication()

    def setOverrideCursor(self, _cursor) -> None:
        pass

    def restoreOverrideCursor(self) -> None:
        pass


@contextmanager
def load_gui_dispatch() -> Iterator[types.ModuleType]:
    module_names = ["FreeCAD", "FreeCADGui", "PySide"]
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in module_names}

    freecad = types.ModuleType("FreeCAD")
    freecad.Console = types.SimpleNamespace(PrintError=lambda _message: None)

    status_bar = FakeStatusBar()
    freecad_gui = types.ModuleType("FreeCADGui")
    freecad_gui.updateGui = lambda: None
    freecad_gui.getMainWindow = lambda: types.SimpleNamespace(statusBar=lambda: status_bar)

    class FakeTimer:
        """Records QTimer.singleShot callbacks so tests can fire ticks."""

        def __init__(self) -> None:
            self.calls: list = []

        def singleShot(self, _delay_ms: int, callback) -> None:
            self.calls.append(callback)

    timer = FakeTimer()
    qt_core = types.SimpleNamespace(
        QObject=object,
        Signal=FakeSignal,
        Qt=types.SimpleNamespace(
            QueuedConnection=0,
            NoButton=0,
            WaitCursor=0,
        ),
        QEventLoop=types.SimpleNamespace(
            ExcludeUserInputEvents=1,
            ExcludeSocketNotifiers=2,
        ),
        QThread=types.SimpleNamespace(msleep=lambda _delay: None),
        QTimer=timer,
    )
    qt_widgets = types.SimpleNamespace(QApplication=FakeApplication)
    pyside = types.ModuleType("PySide")
    pyside.QtCore = qt_core
    pyside.QtWidgets = qt_widgets

    sys.modules["FreeCAD"] = freecad
    sys.modules["FreeCADGui"] = freecad_gui
    sys.modules["PySide"] = pyside

    module_name = f"_mcp_gui_dispatch_test_{time.monotonic_ns()}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, GUI_DISPATCH_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load gui_dispatch from {GUI_DISPATCH_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        module._test_timer = timer
        yield module
    finally:
        sys.modules.pop(module_name, None)
        for name, value in saved.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class ThreadedWaker:
    """Drain the queue from background threads, like real Qt wake slots."""

    def __init__(self, gui_dispatch: types.ModuleType):
        self.gui_dispatch = gui_dispatch
        self.threads: list[threading.Thread] = []

    def wake(self) -> None:
        thread = threading.Thread(
            target=lambda: self.gui_dispatch.process_gui_tasks(reschedule=False),
            daemon=True,
        )
        self.threads.append(thread)
        thread.start()

    def join(self) -> None:
        for thread in self.threads:
            thread.join(timeout=5.0)


def test_running_timeout_blocks_followups_until_task_finishes() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch._waker = waker
        started = threading.Event()
        release = threading.Event()
        finished: list = []

        def blocked_task() -> bool:
            started.set()
            release.wait(timeout=5.0)
            return True

        # Waiter in its own thread so the running timeout is deterministic.
        box: dict = {}

        def waiter() -> None:
            box["first"] = gui_dispatch.dispatch_to_gui(
                blocked_task,
                timeout=0.05,
                operation_name="remove_broken_feature",
                on_finished=finished.append,
            )

        waiter_thread = threading.Thread(target=waiter, daemon=True)
        waiter_thread.start()
        assert started.wait(timeout=5.0)
        waiter_thread.join(timeout=5.0)
        first = box["first"]

        assert first.value is None
        assert first.error is not None
        assert "cannot be safely cancelled" in first.error
        # on_finished is pending: the job has not truly completed yet.
        assert finished == []

        before = time.monotonic()
        second = gui_dispatch.dispatch_to_gui(
            lambda: True,
            timeout=5.0,
            operation_name="list_documents",
        )
        elapsed = time.monotonic() - before

        assert second.error is not None
        assert "still running" in second.error
        assert elapsed < 1.0  # stuck fail-fast rejects immediately
        assert gui_dispatch.get_dispatch_status()["state"] == "stuck"

        release.set()
        waker.join()
        assert gui_dispatch.get_dispatch_status()["state"] == "healthy"

        # Exactly-once terminal notification, after real completion.
        assert len(finished) == 1
        assert finished[0].value is True
        assert finished[0].error is None

        third = gui_dispatch.dispatch_to_gui(
            lambda: "recovered",
            timeout=5.0,
            operation_name="recovered_call",
        )
        waker.join()

        assert third.value == "recovered"
        assert third.error is None


def test_stuck_timeout_future_carries_eventual_result() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch._waker = waker
        started = threading.Event()
        release = threading.Event()
        finished: list = []

        def blocked_task() -> str:
            started.set()
            release.wait(timeout=5.0)
            return "actual result"

        box: dict = {}

        def waiter() -> None:
            box["returned"] = gui_dispatch.dispatch_to_gui(
                blocked_task,
                timeout=0.05,
                operation_name="stuck_future",
                on_finished=finished.append,
            )

        waiter_thread = threading.Thread(target=waiter, daemon=True)
        waiter_thread.start()
        assert started.wait(timeout=5.0)
        waiter_thread.join(timeout=5.0)

        # The stuck outcome is only the blocking caller's return value.
        assert "cannot be safely cancelled" in box["returned"].error
        # The timeout must not settle the job: its Future stays pending for
        # the eventual actual outcome.
        (job,) = gui_dispatch._inflight.values()
        assert not job.future.done()
        assert finished == []

        release.set()
        waker.join()

        assert len(finished) == 1
        assert finished[0].value == "actual result"
        assert finished[0].error is None
        assert job.future.done()
        eventual = job.future.result(timeout=1.0)
        assert eventual.value == "actual result"
        assert eventual.error is None
        assert gui_dispatch.pending_count() == 0


def test_queued_timeout_cancels_task_without_marking_dispatch_stuck() -> None:
    with load_gui_dispatch() as gui_dispatch:
        ran = threading.Event()
        finished: list = []

        result = gui_dispatch.dispatch_to_gui(
            lambda: ran.set(),
            timeout=0.01,
            operation_name="stale_call",
            on_finished=finished.append,
        )
        gui_dispatch.process_gui_tasks(reschedule=False)

        assert result.value is None
        assert "timed out" in result.error
        assert not ran.is_set()  # cancelled before entering FreeCAD
        assert gui_dispatch.get_dispatch_status()["state"] == "healthy"
        # Exactly-once: cancelled-before-start is the true completion.
        assert len(finished) == 1
        assert finished[0] is result


def test_queued_timeout_does_not_corrupt_the_next_call() -> None:
    with load_gui_dispatch() as gui_dispatch:
        stale_ran = threading.Event()

        first = gui_dispatch.dispatch_to_gui(
            lambda: stale_ran.set(),
            timeout=0.01,
            operation_name="stale_call",
        )
        # Submit instead of blocking: a second dispatch_to_gui would wait on
        # the pump this thread still has to run. The still-unsettled Future
        # proves the stale call's timeout left the queued fresh call alone.
        fresh_future = gui_dispatch.submit_to_gui(
            lambda: "fresh",
            operation_name="fresh_call",
        )
        assert not fresh_future.done()

        gui_dispatch.process_gui_tasks(reschedule=False)

        # The stale job was cancelled before it could execute.
        assert "timed out" in first.error
        assert not stale_ran.is_set()
        # ...and the fresh call settled with its own real result.
        second = fresh_future.result(timeout=1.0)
        assert second.value == "fresh"
        assert second.error is None
        assert gui_dispatch.get_dispatch_status()["state"] == "healthy"
        # Both jobs finalized: the abandoned queued job left the inflight set.
        assert gui_dispatch.pending_count() == 0


def test_no_nested_gui_dispatch_inside_a_running_task() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch._waker = waker
        outer_entered = threading.Event()
        release_outer = threading.Event()
        inner_ran = threading.Event()
        seen: dict = {}

        def outer() -> None:
            outer_entered.set()
            gui_dispatch.process_gui_tasks(reschedule=False)
            seen["inner_during_outer"] = inner_ran.is_set()
            release_outer.wait(timeout=5.0)

        def second() -> None:
            inner_ran.set()

        gui_dispatch.submit_to_gui(outer, operation_name="outer")
        gui_dispatch.submit_to_gui(second, operation_name="second")
        assert outer_entered.wait(timeout=5.0)
        release_outer.set()
        waker.join()

        # The re-entrant drain inside the running task was skipped; the
        # second job ran afterwards in the same or a later drain.
        assert seen["inner_during_outer"] is False
        assert inner_ran.is_set()


def test_on_finished_fires_exactly_once_including_exceptions() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch._waker = waker
        finished: list = []

        def raiser() -> None:
            raise ValueError("boom")

        ok_future = gui_dispatch.submit_to_gui(
            lambda: 42, operation_name="ok_job", on_finished=finished.append
        )
        boom_future = gui_dispatch.submit_to_gui(
            raiser, operation_name="boom_job", on_finished=finished.append
        )
        waker.join()

        ok = ok_future.result(timeout=1.0)
        boom = boom_future.result(timeout=1.0)
        assert ok.value == 42
        assert ok.error is None
        assert boom.value is None
        assert boom.error == "ValueError: boom"
        assert "ValueError: boom" in boom.traceback
        assert len(finished) == 2
        assert finished[0].value == 42
        assert finished[1].error == "ValueError: boom"

        # Re-draining must not re-run or re-finalize anything.
        gui_dispatch.process_gui_tasks(reschedule=False)
        assert len(finished) == 2


def test_failing_on_finished_does_not_break_the_dispatch_loop() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch._waker = waker

        def bad_callback(_outcome) -> None:
            raise RuntimeError("callback blew up")

        first = gui_dispatch.dispatch_to_gui(
            lambda: "fine",
            timeout=5.0,
            operation_name="fine",
            on_finished=bad_callback,
        )
        waker.join()
        assert first.value == "fine"

        second = gui_dispatch.dispatch_to_gui(lambda: "after", timeout=5.0, operation_name="after")
        waker.join()
        assert second.value == "after"


def test_cancel_event_before_start_never_enters_freecad() -> None:
    with load_gui_dispatch() as gui_dispatch:
        ran = threading.Event()
        finished: list = []
        cancel = threading.Event()
        cancel.set()

        future = gui_dispatch.submit_to_gui(
            lambda: ran.set(),
            operation_name="cancelled_early",
            cancel_event=cancel,
            on_finished=finished.append,
        )
        outcome = future.result(timeout=1.0)
        gui_dispatch.process_gui_tasks(reschedule=False)

        assert not ran.is_set()
        assert "cancelled before execution" in outcome.error
        assert len(finished) == 1
        assert finished[0] is outcome
        assert gui_dispatch.get_dispatch_status()["state"] == "healthy"


def test_cancel_while_queued_skips_execution() -> None:
    with load_gui_dispatch() as gui_dispatch:
        ran = threading.Event()
        finished: list = []
        cancel = threading.Event()

        future = gui_dispatch.submit_to_gui(
            lambda: ran.set(),
            operation_name="cancelled_queued",
            cancel_event=cancel,
            on_finished=finished.append,
        )
        cancel.set()
        gui_dispatch.process_gui_tasks(reschedule=False)

        outcome = future.result(timeout=1.0)
        assert not ran.is_set()
        assert "cancelled before execution" in outcome.error
        assert len(finished) == 1
        assert finished[0] is outcome


def test_cancel_during_a_run_cannot_end_the_work_falsely() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch._waker = waker
        cancel = threading.Event()
        started = threading.Event()
        release = threading.Event()
        finished: list = []

        def work() -> str:
            started.set()
            release.wait(timeout=5.0)
            return "real result"

        box: dict = {}

        def waiter() -> None:
            box["outcome"] = gui_dispatch.dispatch_to_gui(
                work,
                timeout=30.0,
                operation_name="long_running",
                cancel_event=cancel,
                on_finished=finished.append,
            )

        waiter_thread = threading.Thread(target=waiter, daemon=True)
        waiter_thread.start()
        assert started.wait(timeout=5.0)
        cancel.set()  # cancellation requested while running: cooperative only
        release.set()
        waiter_thread.join(timeout=5.0)
        waker.join()

        outcome = box["outcome"]
        assert outcome.value == "real result"
        assert outcome.error is None  # not a false cancelled completion
        assert len(finished) == 1
        assert finished[0] is outcome
        assert gui_dispatch.get_dispatch_status()["state"] == "healthy"


def test_stuck_fail_fast_rejects_submitted_jobs_immediately() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch._waker = waker
        started = threading.Event()
        release = threading.Event()
        finished: list = []

        def blocked_task() -> bool:
            started.set()
            release.wait(timeout=5.0)
            return True

        box: dict = {}

        def waiter() -> None:
            box["first"] = gui_dispatch.dispatch_to_gui(
                blocked_task,
                timeout=0.05,
                operation_name="stuck_call",
            )

        waiter_thread = threading.Thread(target=waiter, daemon=True)
        waiter_thread.start()
        assert started.wait(timeout=5.0)
        waiter_thread.join(timeout=5.0)
        assert "cannot be safely cancelled" in box["first"].error

        late = gui_dispatch.submit_to_gui(
            lambda: "never", operation_name="late_call", on_finished=finished.append
        )
        outcome = late.result(timeout=1.0)
        assert outcome.value is None
        assert "cannot be safely cancelled" in outcome.error
        assert finished == [outcome]

        release.set()
        waker.join()


def test_shutdown_cancels_queued_and_requests_running_cancellation() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch._waker = waker
        cancel = threading.Event()
        started = threading.Event()
        release = threading.Event()
        queued_ran = threading.Event()
        finished: list = []

        def blocked_task() -> str:
            started.set()
            release.wait(timeout=5.0)
            return "blocked result"

        box: dict = {}

        def waiter() -> None:
            box["outcome"] = gui_dispatch.dispatch_to_gui(
                blocked_task,
                timeout=30.0,
                operation_name="running_call",
                cancel_event=cancel,
                on_finished=finished.append,
            )

        waiter_thread = threading.Thread(target=waiter, daemon=True)
        waiter_thread.start()
        assert started.wait(timeout=5.0)

        gui_dispatch.submit_to_gui(
            lambda: queued_ran.set(),
            operation_name="queued_call",
            on_finished=finished.append,
        )

        stats = gui_dispatch.shutdown()

        assert stats == {"cancelled_queued": 1, "cancel_requested": 1}
        assert cancel.is_set()
        assert gui_dispatch.is_draining()
        assert gui_dispatch.pending_count() == 1  # only the running job remains

        release.set()
        waiter_thread.join(timeout=5.0)
        waker.join()

        # Running work finished with its real result, not a fake cancellation.
        assert box["outcome"].value == "blocked result"
        assert box["outcome"].error is None
        assert gui_dispatch.pending_count() == 0

        # The already-finalized queued job still never executes past shutdown.
        gui_dispatch.process_gui_tasks(reschedule=False)
        assert not queued_ran.is_set()
        # One finalize for the queued cancellation, one for the real result.
        assert len(finished) == 2

        # Draining refuses new submissions.
        refused = gui_dispatch.submit_to_gui(lambda: "never", operation_name="during_drain")
        assert "draining" in refused.result(timeout=1.0).error

        # Re-initializing clears draining and dispatches again.
        gui_dispatch.initialize()
        assert not gui_dispatch.is_draining()
        again = gui_dispatch.dispatch_to_gui(
            lambda: "back", timeout=5.0, operation_name="back_call"
        )
        waker.join()
        assert again.value == "back"


def test_initialize_is_idempotent_and_reports_status() -> None:
    with load_gui_dispatch() as gui_dispatch:
        gui_dispatch.initialize()
        gui_dispatch.initialize()  # second call must be a no-op
        assert not gui_dispatch.is_draining()

        status = gui_dispatch.get_dispatch_status()
        assert status["state"] == "healthy"
        assert status["queued_jobs"] == 0
        assert status["draining"] is False

        gui_dispatch.shutdown()
        gui_dispatch.shutdown()  # second call must be a no-op
        assert gui_dispatch.get_dispatch_status()["draining"] is True


def test_stale_sentinel_cannot_stop_a_restarted_dispatch() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch.initialize()
        gui_dispatch._waker = None  # GUI loop gone: nothing drains the stop
        gui_dispatch.shutdown()  # marker before the waker is cleaned up.
        gui_dispatch.cleanup_waker()
        assert gui_dispatch.is_draining()

        gui_dispatch.initialize()  # restart must retire the stale marker
        gui_dispatch._waker = waker
        assert not gui_dispatch.is_draining()

        # A retired marker that slips past the purge (queued by the dead
        # generation afterwards) is dropped by the drain loop; it can never
        # stop the new generation.
        gui_dispatch._gui_request_queue.put(gui_dispatch._ShutdownSentinel(1))

        first = gui_dispatch.dispatch_to_gui(
            lambda: "first", timeout=5.0, operation_name="first_after_restart"
        )
        waker.join()
        assert first.value == "first"
        assert first.error is None
        assert gui_dispatch.get_dispatch_status()["state"] == "healthy"

        # The restarted heartbeat chain survived the retired marker.
        ticks = gui_dispatch._test_timer
        armed = len(ticks.calls)
        ticks.calls[-1]()
        assert len(ticks.calls) == armed + 1


def test_initialize_refuses_while_previous_work_is_still_running() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch.initialize()
        gui_dispatch._waker = waker
        started = threading.Event()
        release = threading.Event()

        def blocked() -> str:
            started.set()
            release.wait(timeout=5.0)
            return "late"

        future = gui_dispatch.submit_to_gui(blocked, operation_name="old_work")
        assert started.wait(timeout=5.0)
        gui_dispatch.shutdown()
        assert gui_dispatch.is_draining()

        with pytest.raises(RuntimeError, match="old_work"):
            gui_dispatch.initialize()

        release.set()
        assert future.result(timeout=1.0).value == "late"
        waker.join()

        gui_dispatch.initialize()
        assert not gui_dispatch.is_draining()
        again = gui_dispatch.dispatch_to_gui(lambda: "back", timeout=5.0, operation_name="back")
        waker.join()
        assert again.value == "back"


def test_request_timeout_marks_running_job_stuck_until_real_result() -> None:
    with load_gui_dispatch() as gui_dispatch:
        waker = ThreadedWaker(gui_dispatch)
        gui_dispatch.initialize()
        gui_dispatch._waker = waker
        started = threading.Event()
        release = threading.Event()
        finished: list = []

        def blocked() -> str:
            started.set()
            release.wait(timeout=5.0)
            return "real result"

        future = gui_dispatch.submit_to_gui(
            blocked, operation_name="detached", on_finished=finished.append
        )
        assert started.wait(timeout=5.0)

        outcome = gui_dispatch.request_timeout(future, 0.05)
        assert outcome is not None
        assert "cannot be safely cancelled" in (outcome.error or "")
        # The deadline never settles a running job's Future...
        assert not future.done()
        assert finished == []
        # ...but dispatch health tells the truth immediately.
        status = gui_dispatch.get_dispatch_status()
        assert status["state"] == "stuck"
        assert status["timeout_seconds"] == 0.05

        # A repeated sweep stays truthful while the work continues.
        again = gui_dispatch.request_timeout(future, 0.05)
        assert again is not None
        assert "cannot be safely cancelled" in (again.error or "")

        release.set()
        waker.join()
        eventual = future.result(timeout=1.0)
        assert eventual.value == "real result"
        assert eventual.error is None
        assert len(finished) == 1
        assert finished[0] is eventual
        assert gui_dispatch.get_dispatch_status()["state"] == "healthy"
        assert gui_dispatch.pending_count() == 0


def test_request_timeout_cancels_queued_and_isolates_futures() -> None:
    with load_gui_dispatch() as gui_dispatch:
        ran = threading.Event()
        finished: list = []

        stale = gui_dispatch.submit_to_gui(
            lambda: ran.set(), operation_name="stale", on_finished=finished.append
        )
        fresh = gui_dispatch.submit_to_gui(lambda: "fresh", operation_name="fresh")
        stranger = concurrent.futures.Future()

        outcome = gui_dispatch.request_timeout(stale, 0.01)
        assert outcome is not None
        assert "timed out" in (outcome.error or "")
        # The queued job was cancelled before entering FreeCAD...
        assert not ran.is_set()
        assert stale.done()
        assert stale.result(timeout=1.0) is outcome
        assert len(finished) == 1
        # ...and no other Future was ever touched.
        assert gui_dispatch.request_timeout(stranger, 1.0) is None
        assert not stranger.done()
        assert not fresh.done()
        assert gui_dispatch.get_dispatch_status()["state"] == "healthy"

        gui_dispatch.process_gui_tasks(reschedule=False)
        result = fresh.result(timeout=1.0)
        assert result.value == "fresh"
        assert result.error is None
        assert gui_dispatch.pending_count() == 0


def test_old_heartbeat_tick_does_not_rearm_or_duplicate_chains() -> None:
    with load_gui_dispatch() as gui_dispatch:
        gui_dispatch.initialize()
        ticks = gui_dispatch._test_timer
        assert len(ticks.calls) == 1

        ticks.calls[0]()  # a live generation's idle tick rearms itself
        assert len(ticks.calls) == 2

        gui_dispatch._waker = None  # keep the stop marker undrained
        gui_dispatch.shutdown()
        gui_dispatch.initialize()  # restart before the pending tick fires
        assert len(ticks.calls) == 3  # exactly one fresh chain, no duplicate

        ticks.calls[1]()  # the retired generation's tick fires late
        assert len(ticks.calls) == 3  # retired ticks never rearm

        ticks.calls[2]()  # the new chain keeps itself alive
        assert len(ticks.calls) == 4
