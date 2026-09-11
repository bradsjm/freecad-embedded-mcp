"""GUI-thread dispatch lifecycle for the embedded MCP server.

The HTTP server runs on request threads. FreeCAD APIs that touch the GUI or
the document tree must run on the main GUI thread. This module owns the Qt
signal queue that ferries callables onto the GUI thread and the Future-based
lifecycle callers use to submit them.

Mechanics reused from the legacy ``rpc_server/gui_dispatch.py``:

1. Per-call isolation: every submission owns its job record and Future. A
   timeout in one call can never corrupt the response of another call.
2. Immediate wake via Qt signal; the 500 ms heartbeat stays as a fallback.
3. Mouse-button/popup/modal deferral: a tick is skipped while the user drags
   or a popup/modal dialog is open, so MCP work never interrupts navigation.
4. Re-entrancy guard: ``process_gui_tasks`` never drains nested inside a
   running task's ``processEvents``.
5. Stuck fail-fast: once a started task times out, later GUI calls fail
   immediately until that task actually returns (``DispatchHealth``).

v2 lifecycle (PLAN section 4):

- ``submit_to_gui(fn, *, operation_name, cancel_event, on_finished)`` returns
  a ``concurrent.futures.Future`` resolving to one structured ``Outcome``
  (value, stdout, stderr, traceback, error). Tools return plain payloads and
  the dispatcher wraps them; a callable may return an ``Outcome`` directly to
  carry output it captured itself (e.g. ``run_script``). The dispatcher never
  redirects the process-wide stdout/stderr streams.
- ``dispatch_to_gui`` is the blocking wrapper for internal callers: it waits
  ``timeout`` seconds for true completion and never raises
  ``TimeoutError``. A timeout while the job runs returns a stuck outcome to
  the caller only — the job's Future stays pending.
- A queued job that is cancelled (waiter timeout, ``cancel_event`` or
  shutdown) never enters FreeCAD; its ``on_finished`` fires exactly once with
  the cancellation outcome.
- A waiter timeout never ends running work: health stays ``stuck`` until the
  callable actually returns, and only then does the Future settle with the
  real outcome (including exceptions) and ``on_finished`` fire exactly once.
  ``cancel_event`` of a running job is only a cooperative request — it can
  never produce a false completion.
- ``initialize``/``shutdown``/``is_draining`` own the dispatcher lifetime.
  Qt objects (wake signal, heartbeat timer chain) are created on the GUI
  thread in ``initialize`` and disposed on that same thread (or handed to
  it via a queued ``deleteLater``) by ``cleanup_waker``. Each
  ``initialize`` starts a new dispatcher generation: it refuses while a
  previous generation's job is still actually running, retires stop
  markers that were never drained so a restart cannot stall on them, and
  arms exactly one heartbeat chain — ticks of retired generations drain
  but never rearm.
- ``request_timeout(future, timeout_s)`` enforces a caller-side deadline
  for one detached dispatch from any thread: a queued job is cancelled
  before FreeCAD; a running job's health is marked stuck without settling
  its Future.
"""

import concurrent.futures
import itertools
import queue
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtWidgets

from mcp_server.dispatch_health import DispatchHealth, stuck_failure


@dataclass(frozen=True)
class Outcome:
    """Structured result of exactly one GUI dispatch.

    ``error is None`` means the callable completed: ``value`` holds its
    return value (or the ``Outcome`` it returned, passed through with any
    ``stdout``/``stderr`` it captured). A non-None ``error`` explains why no
    value exists: the callable raised (``traceback`` carries the formatted
    traceback), the job was cancelled before it started, the dispatcher is
    draining, or the GUI thread is stuck on an earlier timed-out task.
    """

    value: Any = None
    stdout: str = ""
    stderr: str = ""
    # Field name mirrors the wire payload, not the stdlib module: module
    # functions below still resolve ``traceback`` to the stdlib module.
    traceback: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def wrap(cls, result: Any) -> "Outcome":
        """Pass an ``Outcome`` through unchanged; wrap any other payload."""
        if isinstance(result, Outcome):
            return result
        return cls(value=result)


class _Job:
    """One submitted GUI operation and its exactly-once lifecycle state.

    ``_lock`` guards every transition. The job settles exactly once at true
    completion: the Future receives its single outcome and ``on_finished``
    fires. A blocking caller's timeout never settles a running job — it
    only returns a stuck outcome; the Future stays pending until the
    callable actually returns. ``done_event`` signals settled state to
    blocking waiters.
    """

    __slots__ = (
        "_cancelled",
        "_finished_fired",
        "_future_done",
        "_lock",
        "_started",
        "cancel_event",
        "done_event",
        "fn",
        "future",
        "on_finished",
        "operation",
        "task_id",
    )

    def __init__(
        self,
        task_id: int,
        operation: str,
        fn: Callable[[], Any],
        cancel_event: threading.Event | None,
        on_finished: Callable[[Outcome], Any] | None,
    ) -> None:
        self.task_id = task_id
        self.operation = operation
        self.fn = fn
        self.cancel_event = cancel_event
        self.on_finished = on_finished
        self.future: concurrent.futures.Future[Outcome] = concurrent.futures.Future()
        self.done_event = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._cancelled = False
        self._future_done = False
        self._finished_fired = False


_gui_request_queue: "queue.Queue[Any]" = queue.Queue()
_MAX_QUEUED_JOBS = 64
_queued_jobs = 0
_generation = 0  # bumped by initialize; guarded by _state_lock
_processing = False  # re-entrancy guard: True while process_gui_tasks drains
_processing_since: float = 0.0  # monotonic time when _processing became True
_task_ids = itertools.count(1)
_dispatch_health = DispatchHealth()
_waker: "_WakeSignal | None" = None
_state_lock = threading.Lock()  # guards the lifecycle fields below
_draining = False
_inflight: "dict[int, _Job]" = {}
_jobs_by_future: "dict[concurrent.futures.Future[Outcome], _Job]" = {}


class _WakeSignal(QtCore.QObject):
    """Qt signal bridge for cross-thread GUI-task wakeup.

    Must be created on the GUI thread (``initialize``). Emitting from other
    threads is safe: Qt delivers the connection with ``QueuedConnection``, so
    the slot always fires in the GUI thread's event loop.
    """

    _sig = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._sig.connect(self._on_wake, QtCore.Qt.QueuedConnection)

    def wake(self) -> None:
        self._sig.emit()

    def _on_wake(self) -> None:
        process_gui_tasks(reschedule=False)


class _ShutdownSentinel:
    """Stop marker queued by ``shutdown``, tagged with its generation.

    ``process_gui_tasks`` stops the heartbeat only for a marker whose
    generation is still current; ``initialize`` bumps the generation and
    purges retired markers, so a stop sentinel that was never drained
    (waker disposed first) can never stall or stop a restarted dispatcher.
    """

    __slots__ = ("generation",)

    def __init__(self, generation: int) -> None:
        self.generation = generation


def _operation_label(fn: Callable[[], Any], operation_name: str | None) -> str:
    operation = operation_name or getattr(fn, "__name__", "GUI operation")
    if operation == "<lambda>":
        operation = "GUI operation"
    return operation


def _safe_set_result(future: "concurrent.futures.Future[Outcome]", outcome: Outcome) -> None:
    try:
        future.set_result(outcome)
    except concurrent.futures.InvalidStateError:
        pass  # a waiter timeout already resolved it with a stuck outcome


def _safe_console_error(message: str) -> None:
    """Report an add-on failure without allowing host logging to raise."""

    try:
        FreeCAD.Console.PrintError(message)
    except BaseException:
        pass


def _fire_on_finished(job: _Job, outcome: Outcome) -> None:
    """Invoke the terminal callback exactly once; never raise."""
    if job.on_finished is None:
        return
    try:
        job.on_finished(outcome)
    except BaseException as exc:
        _safe_console_error(
            f"MCP: on_finished callback for '{job.operation}' raised "
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        )


def _stuck_outcome(snapshot: dict[str, Any], *, just_timed_out: bool) -> Outcome:
    return Outcome(error=stuck_failure(snapshot, just_timed_out=just_timed_out)["error"])


def _queued_timeout_outcome(timeout: float) -> Outcome:
    """Timeout message for a job that never started, with the busy hint."""
    hint = ""
    if _processing:
        busy_for = time.monotonic() - _processing_since
        hint = (
            f" (GUI thread has been busy for {busy_for:.1f}s — "
            "consider a detached task for heavy OCCT operations)"
        )
    return Outcome(error=f"GUI dispatch timed out after {timeout}s{hint}")


def _create_job(
    fn: Callable[[], Any],
    *,
    operation_name: str | None,
    cancel_event: threading.Event | None,
    on_finished: Callable[[Outcome], Any] | None,
) -> _Job:
    job = _Job(
        next(_task_ids),
        _operation_label(fn, operation_name),
        fn,
        cancel_event,
        on_finished,
    )

    def _reject(outcome: Outcome) -> None:
        job._future_done = True
        job._finished_fired = True
        _safe_set_result(job.future, outcome)
        _fire_on_finished(job, outcome)
        job.done_event.set()

    rejection = _dispatch_health.rejection()
    if rejection is not None:
        _reject(Outcome(error=rejection["error"]))
        return job
    if cancel_event is not None and cancel_event.is_set():
        _reject(Outcome(error=f"'{job.operation}' was cancelled before execution"))
        return job

    refused: Outcome | None = None
    global _queued_jobs
    with _state_lock:
        if _draining:
            refused = Outcome(
                error=f"'{job.operation}' was not started: GUI dispatcher is draining"
            )
        elif _queued_jobs >= _MAX_QUEUED_JOBS:
            refused = Outcome(
                error=(
                    f"'{job.operation}' was not started: GUI dispatcher queue "
                    f"limit {_MAX_QUEUED_JOBS} was reached"
                )
            )
        else:
            _inflight[job.task_id] = job
            _jobs_by_future[job.future] = job
            _queued_jobs += 1
    if refused is not None:
        _reject(refused)
        return job

    _gui_request_queue.put(job)
    if _waker is not None:
        _waker.wake()  # immediate wake via Qt signal (thread-safe)
    return job


def _remove_queued_job(job: _Job) -> bool:
    """Remove a canceled job before the GUI drain can dequeue it."""

    with _gui_request_queue.mutex:
        try:
            _gui_request_queue.queue.remove(job)
        except ValueError:
            return False
        _gui_request_queue.not_full.notify()
        return True


def _run_job(job: _Job) -> None:
    """Execute one dequeued job on the GUI thread (never raises)."""
    with job._lock:
        if job._finished_fired:
            return  # abandoned before start: queued timeout or shutdown won
        if job._cancelled or (job.cancel_event is not None and job.cancel_event.is_set()):
            # Cancellation won the race before FreeCAD was entered.
            job._future_done = True
            job._finished_fired = True
            outcome = Outcome(error=f"'{job.operation}' was cancelled before execution")
        else:
            job._started = True
            _dispatch_health.start(job.task_id, job.operation)
            outcome = None
    if outcome is not None:
        with _state_lock:
            _inflight.pop(job.task_id, None)
            _jobs_by_future.pop(job.future, None)
        _safe_set_result(job.future, outcome)
        _fire_on_finished(job, outcome)
        job.done_event.set()
        return
    _execute_job(job)


def _execute_job(job: _Job) -> None:
    """Run the callable and finalize the job exactly once."""
    outcome: Outcome | None = None
    try:
        try:
            result = job.fn()
        except BaseException as exc:
            _safe_console_error(
                f"MCP: GUI task '{job.operation}' raised "
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            )
            outcome = Outcome(
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )
        else:
            outcome = Outcome.wrap(result)
    finally:
        if outcome is None:
            # Non-Exception escape (KeyboardInterrupt, SystemExit): the GUI
            # thread keeps it, but waiters must never hang on the Future.
            outcome = Outcome(error=f"'{job.operation}' ended without a catchable exception")
        _dispatch_health.finish(job.task_id)
        with job._lock:
            first = not job._finished_fired
            job._finished_fired = True
            resolve = not job._future_done
            job._future_done = True
        with _state_lock:
            _inflight.pop(job.task_id, None)
            _jobs_by_future.pop(job.future, None)
        if resolve:
            _safe_set_result(job.future, outcome)
        if first:
            # Exactly-once terminal notification after true completion —
            # even when a waiter already timed out on the running job.
            _fire_on_finished(job, outcome)
        job.done_event.set()


def _abandon(job: _Job, timeout: float) -> Outcome:
    """Resolve a waiter timeout without lying about a running job.

    For a job that never started, the job settles as cancelled: it will
    never enter FreeCAD, it leaves the inflight set (like shutdown's queued
    cancellations), the Future receives the cancellation outcome, and
    ``on_finished`` fires here. For a running job, health is marked stuck
    and the stuck outcome is only the blocking caller's return value: the
    Future stays pending and ``on_finished`` stays unused until the callable
    actually returns.
    """
    fetch = False
    settle_outcome: Outcome | None = None
    with job._lock:
        if job._future_done:
            fetch = True
        elif job._started:
            snapshot = _dispatch_health.mark_timed_out(job.task_id, timeout)
            if snapshot is None:
                fetch = True  # finished under our feet; Future is resolving
            else:
                return _stuck_outcome(snapshot, just_timed_out=True)
        else:
            job._cancelled = True
            job._future_done = True
            job._finished_fired = True
            settle_outcome = _queued_timeout_outcome(timeout)
    if fetch:
        return job.future.result()
    removed = _remove_queued_job(job) if settle_outcome is not None else False
    with _state_lock:
        # The abandoned queued job is final: drop it from the inflight set
        # and the future registry like the shutdown and cancel-before-start
        if removed:
            global _queued_jobs
            _queued_jobs = max(0, _queued_jobs - 1)
        _inflight.pop(job.task_id, None)
        _jobs_by_future.pop(job.future, None)
    _safe_set_result(job.future, settle_outcome)
    _fire_on_finished(job, settle_outcome)
    job.done_event.set()
    return settle_outcome


def _flush_gui_events(delay_ms: int = 20) -> None:
    FreeCADGui.updateGui()
    app = QtWidgets.QApplication.instance()
    if app is None:
        return

    # ExcludeUserInputEvents: skip mouse/keyboard events to avoid re-entrancy
    # with ongoing navigation. ExcludeSocketNotifiers keeps network I/O out.
    flags = QtCore.QEventLoop.ExcludeUserInputEvents | QtCore.QEventLoop.ExcludeSocketNotifiers
    app.processEvents(flags, delay_ms)
    if delay_ms > 0:
        QtCore.QThread.msleep(delay_ms)
        app.processEvents(flags, delay_ms)


def _generation_live(generation: int | None) -> bool:
    """True while ``generation`` is the dispatcher's current generation.

    ``None`` means the caller is not a heartbeat tick (a direct drain)
    and keeps the legacy always-rearm behavior.
    """
    if generation is None:
        return True
    with _state_lock:
        return generation == _generation


def _arm_heartbeat() -> None:
    """Schedule the next 500 ms fallback tick for the current generation."""
    with _state_lock:
        generation = _generation

    def _tick() -> None:
        process_gui_tasks(reschedule=True, generation=generation)

    QtCore.QTimer.singleShot(500, _tick)


def process_gui_tasks(reschedule: bool = True, *, generation: int | None = None) -> None:
    """Drain queued GUI-thread jobs and optionally reschedule.

    Skips the current tick when any mouse button is held (e.g. 3D navigation
    drag), when a popup or modal dialog is open, or when already executing a
    task (re-entrancy guard). The guard prevents ``doc.recompute()`` or
    ``processEvents()`` inside a task from triggering a nested drain that
    would corrupt FreeCAD state.

    ``reschedule=False`` is used by the immediate-wake path so it does not
    start a second heartbeat chain alongside the existing 500 ms one.
    ``generation`` identifies the heartbeat chain that scheduled this tick:
    a retired generation's tick still drains (dropping retired stop
    sentinels) but never rearms, so a restart can never duplicate chains.
    """
    global _processing, _processing_since, _queued_jobs
    if _processing:
        return  # re-entrant call from processEvents inside a task; skip

    shutdown = False
    try:
        if _gui_request_queue.empty():
            return  # nothing queued; skip cursor/status-bar churn on idle ticks
        if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            return  # user is dragging; defer to next tick
        if QtWidgets.QApplication.activePopupWidget() is not None:
            return  # context menu or popup open; defer to next tick
        if QtWidgets.QApplication.activeModalWidget() is not None:
            return  # modal dialog open; defer to next tick

        _processing = True
        _processing_since = time.monotonic()
        app = QtWidgets.QApplication.instance()
        try:
            status_bar = FreeCADGui.getMainWindow().statusBar()
        except Exception:
            status_bar = None

        if app is not None:
            app.setOverrideCursor(QtCore.Qt.WaitCursor)
        if status_bar is not None:
            status_bar.showMessage("MCP: processing…")
        try:
            while not _gui_request_queue.empty():
                item = _gui_request_queue.get()
                if isinstance(item, _ShutdownSentinel):
                    with _state_lock:
                        current = _generation
                    if item.generation == current:
                        shutdown = True
                        return
                    continue  # retired stop marker from before a restart
                with _state_lock:
                    _queued_jobs = max(0, _queued_jobs - 1)
                try:
                    _run_job(item)
                except BaseException as e:
                    _safe_console_error(
                        f"MCP: unhandled exception in GUI job dispatch: "
                        f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                    )
        finally:
            if app is not None:
                app.restoreOverrideCursor()
            if status_bar is not None:
                status_bar.clearMessage()
    finally:
        _processing = False
        if not shutdown and reschedule and _generation_live(generation):
            _arm_heartbeat()


def submit_to_gui(
    fn: Callable[[], Any],
    *,
    operation_name: str | None = None,
    cancel_event: threading.Event | None = None,
    on_finished: Callable[[Outcome], Any] | None = None,
) -> "concurrent.futures.Future[Outcome]":
    """Queue ``fn`` for the GUI thread; returns a Future of ``Outcome``.

    ``cancel_event`` is checked before the job starts (set it to stop queued
    work); while the callable runs it is only a cooperative request and can
    never end the job by itself. ``on_finished`` fires exactly once after
    true completion — real result, exception, or confirmed
    cancellation-before-start — never at waiter timeout while work is still
    running. Lifecycle conditions (pre-set cancellation, draining, stuck
    fail-fast) resolve the Future immediately instead of queueing.
    """
    return _create_job(
        fn,
        operation_name=operation_name,
        cancel_event=cancel_event,
        on_finished=on_finished,
    ).future


def dispatch_to_gui(
    fn: Callable[[], Any],
    *,
    timeout: float = 60.0,
    operation_name: str | None = None,
    cancel_event: threading.Event | None = None,
    on_finished: Callable[[Outcome], Any] | None = None,
) -> Outcome:
    """Blocking wrapper around ``submit_to_gui`` for internal callers.

    Waits up to ``timeout`` seconds for true completion. On timeout a queued
    job is cancelled before it can enter FreeCAD (its Future settles with
    the cancellation outcome); for a running job the stuck outcome is
    returned to the caller only — the job's Future stays pending and settles
    with the real outcome when the callable actually returns, and
    ``on_finished`` fires exactly once then. Never raises ``TimeoutError``;
    always returns an ``Outcome``. Must not be called from the GUI thread.
    """
    job = _create_job(
        fn,
        operation_name=operation_name,
        cancel_event=cancel_event,
        on_finished=on_finished,
    )
    if job.done_event.wait(timeout):
        return job.future.result()
    return _abandon(job, timeout)


def request_timeout(
    future: "concurrent.futures.Future[Outcome]", timeout_s: float
) -> Outcome | None:
    """Enforce a caller-side deadline for one detached dispatch.

    The public deadline API for a Future from ``submit_to_gui`` whose
    owning operation is swept outside the dispatcher (e.g. the server's
    ``service_actions``). A still-queued job is cancelled before it can
    enter FreeCAD: its Future settles with the cancellation ``Outcome``
    and ``on_finished`` fires exactly once. A running job has its health
    marked stuck (later submissions fail fast) while its Future stays
    pending and settles only with the real outcome when the callable
    actually returns; the stuck ``Outcome`` is the return value. An
    already-settled Future returns its ``Outcome``; a Future that is not
    an active dispatch of this module returns ``None``. Deadlines match
    their own job only and can never touch another call. Never blocks on
    running work; never raises ``TimeoutError``.
    """
    if future.done():
        return future.result()
    with _state_lock:
        job = _jobs_by_future.get(future)
    if job is None:
        return None
    return _abandon(job, timeout_s)


def _job_is_running(job: _Job) -> bool:
    """True while the job's callable is actually executing on the GUI thread."""
    with job._lock:
        return job._started and not job._finished_fired


def _purge_retired_sentinels() -> None:
    """Drop stop markers queued by earlier dispatcher generations.

    Called from ``initialize`` after the generation bump. Queue internals
    are only touched under the queue's own mutex, so concurrent producers
    stay safe; a marker raced in afterwards is still retired by the drain
    loop's generation check.
    """
    with _state_lock:
        current = _generation
    with _gui_request_queue.mutex:
        retired = [
            item
            for item in _gui_request_queue.queue
            if isinstance(item, _ShutdownSentinel) and item.generation != current
        ]
        for item in retired:
            _gui_request_queue.queue.remove(item)


def initialize() -> None:
    """Start the dispatcher. Must be called on the GUI thread.

    Refuses with ``RuntimeError`` while a job of the previous generation
    is still actually executing — a restart must never race live work.
    Otherwise it retires stop markers queued by earlier generations (a
    ``shutdown`` whose sentinel was never drained cannot stall the new
    generation), clears the draining flag, creates the wake-signal
    bridge, and arms exactly one 500 ms heartbeat chain; ticks of retired
    generations never rearm, so a restart can never duplicate the chain.
    Idempotent while already running.
    """
    global _waker, _draining, _generation
    with _state_lock:
        if _waker is not None and not _draining:
            return  # already live: never arm a competing chain
        running = [
            (job.task_id, job.operation) for job in _inflight.values() if _job_is_running(job)
        ]
    if running:
        task_id, operation = running[0]
        raise RuntimeError(
            f"cannot initialize the MCP GUI dispatcher: '{operation}' "
            f"(task {task_id}) from the previous dispatcher is still "
            "running; retry once it finishes"
        )
    with _state_lock:
        _draining = False
        _generation += 1
    _purge_retired_sentinels()
    _waker = _WakeSignal()
    _arm_heartbeat()


def cleanup_waker() -> None:
    """Dispose the wake-signal bridge; safe from any thread.

    The module reference is dropped under the state lock. Destroying a
    QObject off its owner thread is unsafe, so when a real wake signal
    exists the teardown is scheduled on its owning GUI thread via a
    queued ``deleteLater``; stub/fake wakers without Qt teardown just
    drop.
    """
    global _waker
    with _state_lock:
        waker = _waker
        _waker = None
    if waker is None:
        return
    delete_later = getattr(waker, "deleteLater", None)
    if delete_later is None:
        return
    try:
        delete_later()
    except RuntimeError:
        pass  # the underlying QObject is already gone


def shutdown() -> dict[str, int]:
    """Begin draining. Safe from any thread; never joins a stuck GUI call.

    Queued jobs are cancelled and never execute. Running jobs only get their
    ``cancel_event`` set (cooperative cancellation request) and finalize
    themselves when their callables actually return. The generation-tagged
    stop sentinel makes the next tick of the draining generation stop
    rescheduling the heartbeat; a later ``initialize`` retires an
    undrained sentinel instead of stalling on it. Returns counts of
    affected jobs.
    """
    global _draining, _queued_jobs
    with _state_lock:
        if _draining:
            return {"cancelled_queued": 0, "cancel_requested": 0}
        _draining = True
        jobs = list(_inflight.values())
        generation = _generation
    _gui_request_queue.put(_ShutdownSentinel(generation))
    if _waker is not None:
        _waker.wake()

    cancelled = 0
    requested = 0
    for job in jobs:
        outcome: Outcome | None = None
        with job._lock:
            if job._finished_fired:
                continue
            if job._started:
                if job.cancel_event is not None:
                    job.cancel_event.set()
                    requested += 1
                continue
            job._cancelled = True
            job._future_done = True
            job._finished_fired = True
            outcome = Outcome(
                error=(f"'{job.operation}' was cancelled during GUI dispatcher shutdown")
            )
        cancelled += 1
        removed = _remove_queued_job(job)
        with _state_lock:
            if removed:
                _queued_jobs = max(0, _queued_jobs - 1)
            _inflight.pop(job.task_id, None)
            _jobs_by_future.pop(job.future, None)
        _safe_set_result(job.future, outcome)
        _fire_on_finished(job, outcome)
        job.done_event.set()
    return {"cancelled_queued": cancelled, "cancel_requested": requested}


def is_draining() -> bool:
    """True between ``shutdown`` and the next ``initialize``."""
    with _state_lock:
        return _draining


def pending_count() -> int:
    """Number of jobs not yet finalized (queued plus running)."""
    with _state_lock:
        return len(_inflight)


def get_dispatch_status() -> dict[str, Any]:
    """GUI dispatch health without touching FreeCAD's GUI thread.

    The health snapshot fields (``state``, ``task_id``, ``operation``,
    ``running_for_seconds``, ``timeout_seconds``) plus ``queued_jobs`` and
    ``draining``.
    """
    with _state_lock:
        jobs = list(_inflight.values())
        draining = _draining
    queued = 0
    for job in jobs:
        with job._lock:
            if not job._started:
                queued += 1
    snapshot = _dispatch_health.snapshot()
    snapshot["queued_jobs"] = queued
    snapshot["draining"] = draining
    return snapshot
