"""Server orchestration for the embedded MCP v2 add-on.

Owns startup/shutdown, the exact 24-tool registry, request dispatch
(discovery, tools, tasks, subscriptions and document resources), the
document observer with per-document generations, the shared consent
preflight choreography and the one execution lifecycle for blocking calls
and Tasks.

Deliberately no separate startup/context/runner/capability abstraction
layers: :class:`Server` *is* the ``ctx`` handed to tool handlers, plus a
small per-operation view (:class:`_OpContext`) that carries only what must
be operation-local (cancel event, approved consent target, deadline).

Lifecycle API for the later ``InitGui``/``commands.py`` cutover (GUI
thread)::

    from mcp_server import server as mcp_server_module
    mcp_server_module.start_server()   # -> status dict, duplicate start is
                                       #    idempotent, bind failure unwinds
    mcp_server_module.stop_server()    # -> nonblocking, retains late
                                       #    finalizers until they run
    mcp_server_module.server_status()  # -> {"running": bool, ...}
    mcp_server_module.get_server()     # -> Server | None
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import FreeCAD
import FreeCADGui

from mcp_server import gui_dispatch
from mcp_server.http_server import McpHTTPServer, StreamResponse
from mcp_server.protocol import (
    CONSENT_DENIED,
    DOCUMENT_NOT_FOUND,
    GUI_DISPATCH_FAILED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    OBJECT_NOT_FOUND,
    PATH_NOT_ALLOWED,
    SUPPORTED_PROTOCOL_VERSION,
    VALIDATION_FAILED,
    ConsentSigner,
    InputRequired,
    ProtocolError,
    ToolError,
    check_schema,
    complete_result,
    consent_input_request,
    error_response,
    input_required_result,
    tool_error_result,
    tool_result,
    validate_schema,
)
from mcp_server.settings import load_settings
from mcp_server.subscriptions import (
    DOCUMENTS_RESOURCE_URI,
    SubscriptionClosed,
    SubscriptionRegistry,
)
from mcp_server.tasks import (
    TASK_ELIGIBLE_OPERATIONS,
    TASKS_EXTENSION_ID,
    TaskStore,
    create_task_wire,
    detailed_task_wire,
    require_tasks_capability,
)
from mcp_server.tools.documents import (
    HANDLERS as _DOCUMENT_HANDLERS,
)
from mcp_server.tools.documents import (
    TOOL_DEFINITIONS as _DOCUMENT_DEFS,
)
from mcp_server.tools.documents import (
    preflight as _documents_preflight,
)
from mcp_server.tools.export import (
    HANDLERS as _EXPORT_HANDLERS,
)
from mcp_server.tools.export import (
    TOOL_DEFINITIONS as _EXPORT_DEFS,
)
from mcp_server.tools.export import (
    preflight as _export_preflight,
)
from mcp_server.tools.features import (
    HANDLERS as _FEATURES_HANDLERS,
)
from mcp_server.tools.features import (
    TOOL_DEFINITIONS as _FEATURES_DEFS,
)
from mcp_server.tools.fem import (
    HANDLERS as _FEM_HANDLERS,
)
from mcp_server.tools.fem import (
    TOOL_DEFINITIONS as _FEM_DEFS,
)
from mcp_server.tools.geometry import (
    HANDLERS as _GEOMETRY_HANDLERS,
)
from mcp_server.tools.geometry import (
    TOOL_DEFINITIONS as _GEOMETRY_DEFS,
)
from mcp_server.tools.import_model import (
    HANDLERS as _IMPORT_HANDLERS,
)
from mcp_server.tools.import_model import (
    TOOL_DEFINITIONS as _IMPORT_DEFS,
)
from mcp_server.tools.import_model import (
    preflight as _import_preflight,
)
from mcp_server.tools.objects import (
    HANDLERS as _OBJECTS_HANDLERS,
)
from mcp_server.tools.objects import (
    TOOL_DEFINITIONS as _OBJECTS_DEFS,
)
from mcp_server.tools.parameters import (
    HANDLERS as _PARAMETERS_HANDLERS,
)
from mcp_server.tools.parameters import (
    TOOL_DEFINITIONS as _PARAMETERS_DEFS,
)
from mcp_server.tools.script import (
    HANDLERS as _SCRIPT_HANDLERS,
)
from mcp_server.tools.script import (
    TOOL_DEFINITIONS as _SCRIPT_DEFS,
)
from mcp_server.tools.sketch import (
    HANDLERS as _SKETCH_HANDLERS,
)
from mcp_server.tools.sketch import (
    TOOL_DEFINITIONS as _SKETCH_DEFS,
)
from mcp_server.tools.view import (
    HANDLERS as _VIEW_HANDLERS,
)
from mcp_server.tools.view import (
    TOOL_DEFINITIONS as _VIEW_DEFS,
)

#: Tool errors are complete ``isError`` results, never JSON-RPC errors.
SERVER_BUSY = "SERVER_BUSY"

#: The 26 registered tools, in the exact plan section 5 order.
PLAN_TOOL_ORDER = (
    "discover_capabilities",
    "inspect_documents",
    "new_document",
    "open_document",
    "import_model",
    "save_document",
    "close_document",
    "reload_document",
    "inspect_objects",
    "create_object",
    "create_objects",
    "edit_object",
    "edit_objects",
    "delete_object",
    "validate_geometry",
    "measure",
    "inspect_topology",
    "edit_parameters",
    "inspect_sketch",
    "edit_sketch",
    "create_feature",
    "edit_feature",
    "export",
    "capture_view",
    "run_fem",
    "run_script",
)

_READ_ONLY_TOOLS = frozenset(
    {
        "discover_capabilities",
        "inspect_documents",
        "inspect_objects",
        "inspect_topology",
        "validate_geometry",
        "measure",
    }
)
_IDEMPOTENT_TOOLS = _READ_ONLY_TOOLS | {"inspect_sketch", "capture_view"}
_OPEN_WORLD_TOOLS = frozenset(
    {
        "open_document",
        "import_model",
        "save_document",
        "reload_document",
        "close_document",
        "export",
        "run_fem",
        "run_script",
    }
)


def _tool_annotations(name: str) -> dict[str, bool]:
    """Return the fixed advisory policy for one registered tool."""

    if name not in PLAN_TOOL_ORDER:
        raise RuntimeError(f"tool {name} has no annotation policy")
    read_only = name in _READ_ONLY_TOOLS
    return {
        "readOnlyHint": read_only,
        "destructiveHint": not read_only,
        "idempotentHint": name in _IDEMPOTENT_TOOLS,
        "openWorldHint": name in _OPEN_WORLD_TOOLS,
    }


#: One operation cap across blocking calls and tasks (plan section 4).
MAX_OPERATIONS = 32
MAX_SUBSCRIPTIONS = 32
MAX_SUBSCRIPTION_ID_LENGTH = 1024

#: Orientation applied when a committed mutation reveals its targets. This is
#: the same Isometric perspective ``capture_view`` documents as its first
#: named view, so a reveal and a capture show the model the same way.
_REVEAL_ORIENTATION = "Isometric"

#: Ordinary GUI deadline; export/measure get 600 s; script/FEM use their
#: ``timeout_s`` argument clamped to 1..3600 (plan section 6).
DEFAULT_DEADLINE_S = 60.0
TOOL_DEADLINE_S = {"export": 600.0, "measure": 600.0}
SCRIPT_TIMEOUT_DEFAULT_S = 90.0
FEM_TIMEOUT_DEFAULT_S = 600.0
ASYNC_TIMEOUT_MIN_S = 1.0
ASYNC_TIMEOUT_MAX_S = 3600.0

_PREFLIGHT_TIMEOUT_S = 30.0
_RESOURCE_READ_TIMEOUT_S = 10.0

MAX_SCRIPT_SESSIONS = 32


def _client_supports_form(client_capabilities: Mapping[str, Any] | None) -> bool:
    """True when the client can answer a form elicitation round trip.

    Mirrors the legacy adapter's capability normalization: an elicitation
    object with ``form`` (or the backwards-compatible empty elicitation
    object) means form support; anything else falls back to unprompted
    execution.
    """

    if not isinstance(client_capabilities, Mapping):
        return False
    elicitation = client_capabilities.get("elicitation")
    if not isinstance(elicitation, Mapping):
        return False
    return "form" in elicitation or len(elicitation) == 0


#: Released ``CacheableResult`` hints, applied as TOP-LEVEL ``ttlMs`` /
#: ``cacheScope`` fields on complete discovery/list/resource results (there
#: is no ``io.modelcontextprotocol/caching`` ``_meta`` key).
CACHE_PUBLIC = {"ttlMs": 3_600_000, "cacheScope": "public"}
CACHE_PRIVATE = {"ttlMs": 0, "cacheScope": "private"}

#: Marker inside ``Outcome.error`` produced by the stuck-running timeout
#: path of ``gui_dispatch`` (a running job is never falsely reported as
#: stopped; the blocking caller gets this truthful message instead).
_STUCK_RUNNING_MARKER = "cannot be safely cancelled"

#: Poll granularity while a retained async Future (FEM) is awaited: the
#: wait must wake for the deadline and for a shared cancellation request
#: as well as for the Future itself, so it sleeps in bounded ticks.
_ASYNC_WAIT_TICK_S = 0.05

_CANCELLED_OUTCOME_MARKERS = ("was cancelled", "is draining")


def _rpc_result(request_id: Any, result: dict) -> dict:
    """Full JSON-RPC success response for one dispatched request."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _target_identity(target: Mapping[str, Any] | None) -> dict | None:
    """Consent-identity of a preflight target.

    ``requires_consent`` and ``message`` are advisory server state, not
    identity: they are excluded so a consent granted for a target stays
    valid when only the advisory fields flip between challenge and retry.
    """

    if target is None:
        return None
    return {
        key: value for key, value in target.items() if key not in ("requires_consent", "message")
    }


def _clamp_async_timeout(raw: Any, default: float) -> float:
    value = default if raw is None else raw
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ProtocolError(
            INVALID_PARAMS, "invalid parameters: timeout_s must be a number"
        ) from None
    return max(ASYNC_TIMEOUT_MIN_S, min(ASYNC_TIMEOUT_MAX_S, value))


def _deadline_for(name: str, arguments: Mapping[str, Any]) -> float:
    """Operation deadline in seconds for one tool call (plan section 6)."""

    if name in ("run_script", "run_fem"):
        default = FEM_TIMEOUT_DEFAULT_S if name == "run_fem" else SCRIPT_TIMEOUT_DEFAULT_S
        return _clamp_async_timeout(arguments.get("timeout_s"), default)
    return TOOL_DEADLINE_S.get(name, DEFAULT_DEADLINE_S)


def _check_freecad_version() -> None:
    """Startup version guard: FreeCAD 1.1.3 <= version < 1.2 required."""

    raw = FreeCAD.Version()
    try:
        major, minor, patch = (int(raw[0]), int(raw[1]), int(raw[2]))
    except (IndexError, TypeError, ValueError):
        raise RuntimeError(
            f"MCP server requires FreeCAD 1.1.3+ but could not parse version {raw!r}"
        ) from None
    if (major, minor, patch) < (1, 1, 3):
        raise RuntimeError(f"MCP server requires FreeCAD 1.1.3+; found {major}.{minor}.{patch}")
    if (major, minor) >= (1, 2):
        raise RuntimeError(f"MCP server supports FreeCAD < 1.2; found {major}.{minor}.{patch}")


class _DocumentObserver:
    """Plain Python FreeCAD document observer (no invented superclass).

    Increments per-document generations on every needed document, object,
    change and save event, and publishes ``notifications/resources/updated``
    for ``freecad://documents`` to subscribers whose payload could change.
    Callbacks never touch the GUI or the network: publishing only enqueues
    onto bounded subscription queues, and every callback swallows errors so
    FreeCAD's observer dispatch is never disturbed.
    """

    def __init__(self, server: Server) -> None:
        self._server = server

    def slotCreatedDocument(self, doc) -> None:
        self._server._on_document_event(doc, bump=True, publish=True)

    def slotDeletedDocument(self, doc) -> None:
        # Deletion bumps the monotonic generation and publishes like any
        # other change; the entry is retained so stale instances of the
        # deleted document can never adopt or report a live identity.
        self._server._on_document_event(doc, bump=True, publish=True)
        self._server._invalidate_capabilities_for_document(doc)

    def slotRelabelDocument(self, doc) -> None:
        self._server._on_document_event(doc, bump=True, publish=True)

    def slotActivateDocument(self, doc) -> None:
        self._server._on_document_event(doc, bump=False, publish=True)

    def slotBeforeRecomputeDocument(self, doc) -> None:
        self._server._on_document_event(doc, bump=True, publish=False)

    def slotRecomputedDocument(self, doc) -> None:
        self._server._on_document_event(doc, bump=True, publish=False)

    def slotCreatedObject(self, doc, obj) -> None:
        self._server._on_document_event(doc, bump=True, publish=True)

    def slotDeletedObject(self, doc, obj) -> None:
        self._server._on_document_event(doc, bump=True, publish=True)

    def slotBeforeChangeObject(self, doc, obj) -> None:
        self._server._on_document_event(doc, bump=True, publish=False)

    def slotChangedObject(self, doc, obj) -> None:
        self._server._on_document_event(doc, bump=True, publish=True)

    def slotRecomputedObject(self, doc, obj) -> None:
        self._server._on_document_event(doc, bump=True, publish=False)

    def slotStartSaveDocument(self, doc) -> None:
        self._server._on_document_event(doc, bump=True, publish=False)

    def slotFinishSaveDocument(self, doc) -> None:
        self._server._on_document_event(doc, bump=True, publish=True)

    def slotUndoDocument(self, doc) -> None:
        self._server._on_document_event(doc, bump=True, publish=True)

    def slotRedoDocument(self, doc) -> None:
        self._server._on_document_event(doc, bump=True, publish=True)


@dataclass
class _Operation:
    """One in-flight operation, blocking or task (shared 32-op cap)."""

    op_id: str
    name: str
    kind: str  # "blocking" | "task"
    task_id: str | None
    principal: str | None
    deadline_mono: float
    deadline_s: float
    cancel_event: threading.Event = field(default_factory=threading.Event)
    deadline_noted: bool = False
    #: Dispatcher handle for submitted work (task path): lets the
    #: deadline sweep mark a detached job timed out without a waiter.
    future: concurrent.futures.Future | None = None
    #: Verified recovery receipt once this operation published a checkpoint.
    #: Kept on the operation, not the context, so timeout and infrastructure
    #: error paths can still report the copy that was created.
    checkpoint: dict | None = None


class _OpContext:
    """Per-operation ``ctx`` view handed to one tool handler.

    Everything else delegates to the :class:`Server` instance, so the
    server class stays the single context type (no abstraction modules).
    ``cancel_event``/``approved_target`` are operation-local and never
    shared across overlapped FEM solves.
    """

    __slots__ = (
        "_server",
        "approved_target",
        "cancel_event",
        "deadline_mono",
        "operation",
    )

    def __init__(
        self,
        server: Server,
        *,
        operation: _Operation,
        approved_target: dict | None,
    ) -> None:
        self._server = server
        self.operation = operation
        self.cancel_event = operation.cancel_event
        self.approved_target = approved_target
        self.deadline_mono = operation.deadline_mono

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)

    def operation_finished(self, *_args: Any) -> None:
        """Hook for asynchronous handlers (FEM): stop deadline tracking.

        The QProcess has finished; finalization continues through the
        retained Future. The deadline can no longer truthfully expire.
        Accepts and ignores any completion payload the handler passes.
        """

        self.operation.deadline_mono = float("inf")

    def checkpoint_before_mutation(self, doc: Any, label: str) -> dict:
        """Create and verify one recovery copy when recovery is enabled."""

        if not bool(self.settings.get("recovery_enabled", False)):
            return {}
        from .tools.recovery import checkpoint_before_mutation

        receipt = checkpoint_before_mutation(self, doc, label)
        self.operation.checkpoint = receipt
        return receipt

    def reveal_objects(self, doc: Any, targets: list[Any]) -> None:
        """Frame the 3D view on the objects a mutation just changed.

        The user should see a change without hunting for it. The view is
        switched to the same orientation ``capture_view`` uses by default
        (Isometric) and then framed on the changed objects, so a reveal and a
        capture show the same perspective. As with capture, navigation
        animations are disabled around the orientation change: an animated
        camera would otherwise still be mid-flight when the framing runs.

        The mutation's own document is activated first, so a mutation on a
        background document frames that document instead of the visible one,
        and the caller's selection and active document are restored
        afterwards. Never raises: a headless run, an unloaded GUI module or a
        third-party view that rejects the call must not fail the mutation
        that already committed.
        """

        live = [obj for obj in targets if obj is not None]
        if not live:
            return
        try:
            import FreeCADGui

            from .tools import view as view_tools

            document_name = str(getattr(doc, "Name", ""))
            if not document_name:
                return
            previous_document = FreeCADGui.ActiveDocument
            previous_name = str(getattr(previous_document, "Name", "")) or None
            gui_document = FreeCADGui.getDocument(document_name)
            if gui_document is None:
                return
            view = getattr(gui_document, "ActiveView", None)
            if view is None:
                return

            # ``clearSelection`` is global, so selections in every open
            # document are snapshotted and restored, not just the active one.
            previous_selections: list[tuple[str, list[Any]]] = []
            for other_name in FreeCAD.listDocuments():
                other_gui = FreeCADGui.getDocument(other_name)
                if other_gui is None:
                    continue
                try:
                    entries = list(FreeCADGui.Selection.getSelectionEx(other_name) or ())
                except Exception:
                    entries = []
                if entries:
                    previous_selections.append((other_name, entries))

            if previous_name != document_name:
                self._safe(lambda: self.App.setActiveDocument(document_name))
                self._safe(lambda: FreeCADGui.setActiveDocument(document_name))

            def _reselect(entry: Any, subelement: str | None) -> None:
                """Re-add one snapshot entry; ``None`` restores the whole object."""

                if subelement is None:
                    FreeCADGui.Selection.addSelection(entry.Object)
                else:
                    FreeCADGui.Selection.addSelection(entry.Object, subelement)

            animations = view_tools._disable_navigation_animations()
            try:
                orientation = getattr(view, view_tools._VIEW_METHODS[_REVEAL_ORIENTATION], None)
                if callable(orientation):
                    orientation()
                    gui_dispatch._flush_gui_events()
                FreeCADGui.Selection.clearSelection()
                for obj in live:
                    try:
                        FreeCADGui.Selection.addSelection(obj)
                    except Exception:
                        continue
                # Framing the selection both recenters the camera and zooms
                # it to the changed geometry, so the change is visible in the
                # standard capture perspective whatever the user had open.
                FreeCADGui.SendMsgToActiveView("ViewSelection")
                gui_dispatch._flush_gui_events()
            finally:
                self._safe(lambda: view_tools._restore_navigation_animations(animations))
                self._safe(FreeCADGui.Selection.clearSelection)
                for _other_name, entries in previous_selections:
                    for entry in entries:
                        subelements = [
                            str(sub) for sub in (getattr(entry, "SubElementNames", None) or [])
                        ]
                        if not subelements:
                            self._safe(lambda entry=entry: _reselect(entry, None))
                            continue
                        for subelement in subelements:
                            self._safe(
                                lambda entry=entry, subelement=subelement: _reselect(
                                    entry, subelement
                                )
                            )
                if previous_name is not None and previous_name != document_name:
                    self._safe(lambda: self.App.setActiveDocument(previous_name))
                    self._safe(lambda: FreeCADGui.setActiveDocument(previous_name))
        except Exception:
            return

    @staticmethod
    def _safe(step: Callable[[], None]) -> None:
        """Run one best-effort restore step; failures never skip the rest."""

        try:
            step()
        except Exception:
            return

    @property
    def checkpoint(self) -> dict | None:
        """The recovery receipt of this operation, or ``None``."""

        return self.operation.checkpoint


@dataclass
class _DocEntry:
    """Lifetime identity of one in-session document instance.

    ``uid`` is a fresh UUID per document instance, so a closed document
    reopened under the same ``Name`` can never validate stale identity- or
    generation-bound tokens. ``generation`` is monotonic per name across
    replacements and never resets to zero.
    """

    uid: str
    instance: Any
    generation: int


class Server:
    """The embedded MCP server and the ``ctx`` for every tool handler."""

    def __init__(
        self,
        *,
        settings: Mapping[str, Any] | None = None,
        settings_path: str | None = None,
        signer: ConsentSigner | None = None,
        task_store: TaskStore | None = None,
        registry: SubscriptionRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = dict(settings) if settings is not None else load_settings(settings_path)
        self.signer = signer or ConsentSigner()
        self._task_store = task_store or TaskStore()
        self._registry = registry or SubscriptionRegistry(
            supported_resource_uris=(DOCUMENTS_RESOURCE_URI,)
        )
        self._clock = clock

        #: Actual FreeCAD modules, bound explicitly so tool handlers can
        #: use ``ctx.App`` / ``ctx.Gui`` (the Server is the ctx type).
        self.App = FreeCAD
        self.Gui = FreeCADGui

        self._state_lock = threading.Lock()
        self._state = "stopped"  # stopped | starting | running | draining
        self._bound = False
        self._http: McpHTTPServer | None = None
        self._observer: _DocumentObserver | None = None
        self._observer_registered = False

        self._doc_lock = threading.Lock()
        self._doc_entries: dict[str, _DocEntry] = {}
        self._ops_lock = threading.Lock()
        self._ops: dict[str, _Operation] = {}
        self._direct_gui_lock = threading.Lock()
        self._direct_gui_jobs = 0
        self._subscription_admission_lock = threading.Lock()

        self._script_lock = threading.Lock()
        self.script_namespaces: dict[str, dict] = {}
        self._solves_lock = threading.Lock()
        self.active_solves: dict[str, dict] = {}

        self._static_capabilities: dict | None = None
        self._capabilities_lock = threading.Lock()
        self._register_tools()

    # ------------------------------------------------------------------
    # Tool registration (exact plan order, finite schemas, no silent skips)
    # ------------------------------------------------------------------

    def _register_tools(self) -> None:
        modules = (
            (_DOCUMENT_DEFS, _DOCUMENT_HANDLERS, _documents_preflight),
            (_OBJECTS_DEFS, _OBJECTS_HANDLERS, None),
            (_GEOMETRY_DEFS, _GEOMETRY_HANDLERS, None),
            (_PARAMETERS_DEFS, _PARAMETERS_HANDLERS, None),
            (_EXPORT_DEFS, _EXPORT_HANDLERS, _export_preflight),
            (_VIEW_DEFS, _VIEW_HANDLERS, None),
            (_FEM_DEFS, _FEM_HANDLERS, None),
            (_SCRIPT_DEFS, _SCRIPT_HANDLERS, None),
            (_SKETCH_DEFS, _SKETCH_HANDLERS, None),
            (_FEATURES_DEFS, _FEATURES_HANDLERS, None),
            (_IMPORT_DEFS, _IMPORT_HANDLERS, _import_preflight),
        )
        self._handlers: dict[str, Callable[[_OpContext, dict], Any]] = {}
        self._preflights: dict[str, Callable[..., Any]] = {}
        self._definitions: dict[str, dict] = {}
        for defs, handlers, preflight in modules:
            for definition in defs:
                self._add_definition(definition, handlers, preflight)
        self._add_definition(
            _discover_definition(),
            {"discover_capabilities": self._tool_discover_capabilities},
            None,
        )
        self._add_definition(
            _inspect_documents_definition(),
            {"inspect_documents": self._handle_inspect_documents},
            None,
        )
        missing = [n for n in PLAN_TOOL_ORDER if n not in self._definitions]
        if missing:
            raise RuntimeError(f"tool registration incomplete; missing: {missing}")
        extra = [n for n in self._definitions if n not in PLAN_TOOL_ORDER]
        if extra:
            raise RuntimeError(f"unexpected tools registered: {extra}")
        self._tool_defs = [self._definitions[name] for name in PLAN_TOOL_ORDER]

    def _tool_enabled(self, name: str) -> bool:
        """Return whether one registered tool is exposed on the wire.

        Only ``run_script`` depends on a setting: full local scripting is
        opt-in through ``allow_scripts`` (default false). Every other
        registered tool is always enabled.
        """

        if name != "run_script":
            return True
        return bool(self.settings.get("allow_scripts", False))

    def _add_definition(self, definition: Any, handlers: Any, preflight: Any) -> None:
        name, description, input_schema, output_schema = _unpack_definition(definition)
        if name in self._definitions:
            raise RuntimeError(f"duplicate tool definition: {name}")
        if not isinstance(input_schema, Mapping) or not isinstance(output_schema, Mapping):
            raise RuntimeError(f"tool {name} must declare finite input/output schemas")
        for schema in (input_schema, output_schema):
            check_schema(schema)  # rejects unsupported constructs at registration
        self._definitions[name] = {
            "name": name,
            "description": description,
            "inputSchema": dict(input_schema),
            "outputSchema": dict(output_schema),
            "annotations": _tool_annotations(name),
        }
        self._handlers[name] = self._resolve_handler(name, handlers)
        if preflight is not None:
            self._preflights[name] = preflight

    @staticmethod
    def _resolve_handler(name: str, handlers: Any) -> Callable[..., Any]:
        if handlers is None or name not in handlers:
            raise RuntimeError(f"tool {name} has no handler")
        return handlers[name]

    # ------------------------------------------------------------------
    # ctx surface (contract: local://v2-tool-contract.json)
    # ------------------------------------------------------------------

    def _live_doc_entry(self, doc: Any) -> _DocEntry:
        """Resolve (or lazily adopt) the lifetime entry of a live document.

        A stale instance of a deleted/replaced document never adopts the
        live entry and never reports its identity: this raises, which is
        exactly the fail-closed signal the FEM result-loading guard checks
        BEFORE the native loader mutates anything.
        """

        name = doc.Name  # a dying document object may not answer
        with self._doc_lock:
            entry = self._doc_entries.get(name)
            if entry is None:
                # First sighting (e.g. open before observer registration).
                entry = _DocEntry(uid=uuid.uuid4().hex, instance=doc, generation=0)
                self._doc_entries[name] = entry
            elif entry.instance is not doc:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"document '{name}' is no longer the current instance",
                    {"name": name},
                )
            return entry

    def document_identity(self, doc: Any) -> str:
        """Lifetime UUID of the live document instance behind ``doc``.

        Unlike a ``doc.Name`` key, a document closed and reopened under the
        same name gets a fresh identity, so identity/generation-bound
        tokens from the previous instance are stale by construction.
        """

        return self._live_doc_entry(doc).uid

    def document_generation(self, doc: Any) -> int:
        """Monotonic per-name change counter; never resets across reopen."""

        return self._live_doc_entry(doc).generation

    def require_document(self, name: str) -> Any:
        if not isinstance(name, str) or not name:
            raise ToolError(DOCUMENT_NOT_FOUND, "document name must be a non-empty string")
        doc = FreeCAD.listDocuments().get(name)
        if doc is None:
            raise ToolError(DOCUMENT_NOT_FOUND, f"no such document: {name}", {"name": name})
        return doc

    def require_object(self, doc: Any, name: str) -> Any:
        obj = doc.getObject(name)
        if obj is None:
            try:
                from .tools.objects import _suggestions

                candidates = [str(entry.Name) for entry in (doc.Objects or ())]
                suggestions = _suggestions(name, candidates)
            except Exception:
                suggestions = []
            raise ToolError(
                OBJECT_NOT_FOUND,
                f"no such object: {name}",
                {
                    "document": doc.Name,
                    "object": name,
                    "suggestions": suggestions,
                    "nextTool": "inspect_objects",
                },
            )
        return obj

    def check_document_idle(self, doc: Any) -> None:
        """Reject mutations while a FEM solve retains the document."""

        identity = self.document_identity(doc)
        with self._solves_lock:
            solving = identity in self.active_solves
        if solving:
            raise ToolError(
                SERVER_BUSY,
                f"document '{identity}' has a running FEM solve",
                {"reason": "document_solving", "document": identity},
            )

    def canonical_path(self, path: Any) -> str:
        """Realpath ``path`` and require containment in an allowed root.

        The configured ``recovery_directory`` is implicitly allowed: naming
        it once in settings is enough, so it never has to be repeated in
        ``allowed_roots``. It widens containment only while it is an
        absolute path to an existing directory: a relative value would
        otherwise resolve against the process working directory, which
        differs between the settings dialog and the FreeCAD process.
        """

        if not isinstance(path, str) or not path:
            raise ToolError(VALIDATION_FAILED, "path must be a non-empty string")
        real = os.path.realpath(os.path.expanduser(path))
        roots = list(self.settings.get("allowed_roots") or ())
        recovery_directory = os.path.expanduser(str(self.settings.get("recovery_directory") or ""))
        if os.path.isabs(recovery_directory) and os.path.isdir(recovery_directory):
            roots.append(recovery_directory)
        for root in roots:
            root_real = os.path.realpath(os.path.expanduser(root))
            try:
                if os.path.commonpath([real, root_real]) == root_real:
                    return real
            except ValueError:
                continue  # disjoint drive letters etc.
        raise ToolError(
            PATH_NOT_ALLOWED,
            f"path is outside the allowed roots: {path}",
            {"path": real},
        )

    def file_fingerprint(self, path: Any) -> dict | None:
        """Size/mtime identity of ``path``; ``None`` when it does not exist."""

        try:
            st = os.stat(path)
        except OSError:
            return None
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}

    def ensure_script_namespace(self, session_id: str) -> dict:
        """Return the named script session, capped at 32 live sessions."""

        if not isinstance(session_id, str) or not session_id:
            raise ToolError(VALIDATION_FAILED, "session_id must be a non-empty string")
        with self._script_lock:
            namespace = self.script_namespaces.get(session_id)
            if namespace is None:
                if len(self.script_namespaces) >= MAX_SCRIPT_SESSIONS:
                    raise ToolError(
                        SERVER_BUSY,
                        "script session limit reached; restart the server to reset",
                        {"reason": "session_limit", "limit": MAX_SCRIPT_SESSIONS},
                    )
                namespace = {}
                self.script_namespaces[session_id] = namespace
            return namespace

    # ------------------------------------------------------------------
    # Document observer wiring
    # ------------------------------------------------------------------

    def _on_document_event(self, doc: Any, *, bump: bool, publish: bool) -> None:
        """Observer callback: generation bump + replacement detection.

        A document event for a different instance under a known name means
        that document was closed and reopened: the entry is replaced with a
        fresh lifetime UUID while the generation counter continues
        monotonically. Deleted documents keep their entry (with its final
        generation), so stale instances fail identity lookups and no
        generation ever resets to zero.
        """

        try:
            name = doc.Name
        except Exception:
            return  # a dying document may not answer; never break FreeCAD
        with self._doc_lock:
            entry = self._doc_entries.get(name)
            if entry is None:
                entry = _DocEntry(uid=uuid.uuid4().hex, instance=doc, generation=0)
                self._doc_entries[name] = entry
            elif entry.instance is not doc:
                # Same-name reopen: new lifetime identity, monotonic count.
                entry = _DocEntry(
                    uid=uuid.uuid4().hex,
                    instance=doc,
                    generation=entry.generation + 1,
                )
                self._doc_entries[name] = entry
            if bump:
                entry.generation += 1
        if publish:
            try:
                self._registry.publish_resource_updated(DOCUMENTS_RESOURCE_URI)
            except Exception:
                pass  # observer callbacks must never raise into FreeCAD

    def _invalidate_capabilities_for_document(self, doc: Any) -> None:
        """Retire cached supported-types provenance when its source closes.

        The static snapshot records which document the supported-types
        probe ran in. Once that document closes the provenance is stale:
        keep the static blocks, but mark the type list unavailable so a
        cached read stops presenting a closed document as its source.
        Invalidation happens at event time precisely so a cached read
        never needs the GUI thread; a refresh recaptures through the GUI
        path. Runs after ``_on_document_event``, so ``_doc_lock`` and
        ``_capabilities_lock`` are never held together.
        """

        try:
            name = str(doc.Name)
        except Exception:
            return  # a dying document may not answer; never break FreeCAD
        with self._capabilities_lock:
            snapshot = self._static_capabilities
            if not snapshot or snapshot.get("supportedTypesDocument") != name:
                return
            tombstone = dict(snapshot)
            tombstone["supportedTypes"] = {"unavailable": f"snapshot document '{name}' was closed"}
            tombstone["supportedTypesDocument"] = None
            self._static_capabilities = tombstone

    def _register_observer(self) -> None:
        self._observer = _DocumentObserver(self)
        FreeCAD.addDocumentObserver(self._observer)
        self._observer_registered = True

    def _remove_observer(self) -> None:
        if self._observer_registered:
            try:
                FreeCAD.removeDocumentObserver(self._observer)
            finally:
                self._observer_registered = False
                self._observer = None

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(self, validated: dict, principal: str, connection_id: Any) -> dict:
        """Route one protocol-validated request. Never raises for tool
        failures (those are complete isError results); raises
        :class:`ProtocolError` for protocol-level failures only."""

        method = validated["method"]
        if method == "server/discover":
            return self._dispatch_discover(validated)
        if method == "tools/list":
            return self._dispatch_tools_list(validated)
        if method == "tools/call":
            return self._dispatch_tools_call(validated, principal, connection_id)
        if method == "tasks/get":
            return self._dispatch_tasks_get(validated, principal)
        if method == "tasks/update":
            return self._dispatch_tasks_update(validated, principal)
        if method == "tasks/cancel":
            return self._dispatch_tasks_cancel(validated, principal)
        if method == "subscriptions/listen":
            return self._dispatch_listen(validated, principal, connection_id)
        if method == "resources/list":
            return self._dispatch_resources_list(validated)
        if method == "resources/read":
            return self._dispatch_resources_read(validated)
        raise ProtocolError(METHOD_NOT_FOUND, f"unknown method: {method}")

    # -- discovery / tools list (GUI independent, cached responses) ------

    def _dispatch_discover(self, validated: dict) -> dict:
        params = validated.get("params") or {}
        unknown = set(params) - {"refresh", "document", "_meta"}
        if unknown:
            raise ProtocolError(
                INVALID_PARAMS,
                f"invalid parameters: unknown discover parameters: {sorted(unknown)}",
            )
        refresh = params.get("refresh", False)
        if not isinstance(refresh, bool):
            raise ProtocolError(INVALID_PARAMS, "invalid parameters: refresh must be a boolean")
        document = params.get("document")
        if document is not None and (not isinstance(document, str) or not document):
            raise ProtocolError(
                INVALID_PARAMS,
                "invalid parameters: document must be a non-empty string",
            )
        if document is not None and refresh is not True:
            return _rpc_result(
                validated["id"],
                tool_error_result(
                    ToolError(
                        VALIDATION_FAILED,
                        "document requires refresh=true",
                        {"document": document},
                    )
                ),
            )
        if refresh is not True:
            # GUI-independent: the latest successful immutable cache.
            payload = self._discover_payload(self._capability_snapshot(), None)
            return _with_caching(
                _rpc_result(validated["id"], complete_result(payload)),
                CACHE_PUBLIC,
            )
        snapshot, refresh_error = self._refresh_capabilities(document)
        if refresh_error is None:
            payload = self._discover_payload(snapshot, None)
            # Live document data: ttl 0, private scope.
            return _with_caching(
                _rpc_result(validated["id"], complete_result(payload)),
                CACHE_PRIVATE,
            )
        payload = self._discover_payload(self._capability_snapshot(), refresh_error)
        payload["gui"] = _gui_health_snapshot()
        return _rpc_result(validated["id"], complete_result(payload))

    def _discover_payload(self, snapshot: dict, refresh_error: dict | None) -> dict:
        capabilities = {
            key: value for key, value in snapshot.items() if key != "supportedTypesDocument"
        }
        capabilities["scriptingEnabled"] = bool(self.settings.get("allow_scripts", False))
        capabilities["recoveryEnabled"] = bool(self.settings.get("recovery_enabled", False))
        return {
            "supportedVersions": [SUPPORTED_PROTOCOL_VERSION],
            "capabilities": capabilities,
            "supportedTypesDocument": snapshot.get("supportedTypesDocument"),
            "refreshError": refresh_error,
        }

    def _refresh_capabilities(self, document: str | None) -> tuple[dict, dict | None]:
        """Refresh the static capability snapshot on the GUI thread.

        The GUI callable only returns a candidate (or a structured
        ToolError payload); the waiting caller publishes it under the
        capabilities lock ONLY after a successful Outcome, so a late
        result from a timed-out dispatch can never reach the cache. A
        failed refresh leaves the cache untouched.
        """

        def _candidate() -> dict:
            try:
                if document is not None:
                    doc = FreeCAD.listDocuments().get(document)
                    if doc is None:
                        raise ToolError(
                            DOCUMENT_NOT_FOUND,
                            f"no such document: {document}",
                            {"name": document},
                        )
                else:
                    docs = FreeCAD.listDocuments()
                    doc = docs[sorted(docs.keys())[0]] if docs else None
                return _capture_static_capabilities(doc)
            except ToolError as exc:
                error: dict = {"code": exc.code, "message": exc.message}
                if exc.details is not None:
                    error["details"] = exc.details
                return {"_refreshError": error}

        with self._direct_gui_scope("discover:refresh") as rejection:
            if rejection is not None:
                return {}, {"code": GUI_DISPATCH_FAILED, "message": rejection.error}
            outcome = gui_dispatch.dispatch_to_gui(
                _candidate,
                timeout=_PREFLIGHT_TIMEOUT_S,
                operation_name="discover:refresh",
            )
            if outcome.error is not None:
                error: dict = {
                    "code": GUI_DISPATCH_FAILED,
                    "message": f"capability refresh failed: {outcome.error}",
                }
                if outcome.traceback:
                    error["details"] = {"traceback": outcome.traceback}
                return {}, error
            candidate = outcome.value
            if not isinstance(candidate, dict):
                return {}, {
                    "code": GUI_DISPATCH_FAILED,
                    "message": "capability refresh returned an invalid snapshot",
                }
            refresh_error = candidate.pop("_refreshError", None)
            if refresh_error is not None:
                return {}, refresh_error
            with self._capabilities_lock:
                self._static_capabilities = candidate
            return candidate, None

    def _capability_snapshot(self) -> dict:
        """Static startup snapshot; live GUI health is reported separately.

        A deep copy under the capabilities lock: readers can never observe
        a half-published refresh, and no lock is held while they use it.
        """

        with self._capabilities_lock:
            snapshot = self._static_capabilities
        return copy.deepcopy(snapshot) if snapshot else {}

    def _dispatch_tools_list(self, validated: dict) -> dict:
        payload = {"tools": [dict(d) for d in self._tool_defs if self._tool_enabled(d["name"])]}
        # The exposed surface depends on the active settings, so the list is
        # never publicly cacheable: a cached full list would outlive a
        # restart that disables scripting.
        return _with_caching(_rpc_result(validated["id"], complete_result(payload)), CACHE_PRIVATE)

    def _tool_discover_capabilities(self, ctx: Any, arguments: dict) -> dict:
        """The private snapshot tool: no nested GUI dispatch, ever.

        Without ``refresh`` this answers from the published cache and stays
        GUI-independent. With ``refresh: true`` it runs on the GUI thread
        already (the server routes it through the ordinary blocking call),
        so it captures the static snapshot DIRECTLY — a
        ``_refresh_capabilities`` call here would nest a GUI dispatch —
        and publishes it under the capabilities lock exactly like the
        startup/discover refresh path does.
        """

        capabilities = self._capability_snapshot()
        if arguments.get("refresh") is True:
            fresh = _capture_static_capabilities(None)
            with self._capabilities_lock:
                self._static_capabilities = fresh
            capabilities = self._capability_snapshot()
        if arguments.get("detail", "compact") == "full":
            projection = capabilities
        else:
            projection = _compact_capabilities(capabilities)
        # Settings-derived flags are part of every discovery projection, not
        # of the cached static snapshot they are merged into.
        projection["scriptingEnabled"] = bool(self.settings.get("allow_scripts", False))
        projection["recoveryEnabled"] = bool(self.settings.get("recovery_enabled", False))
        return {
            "capabilities": projection,
            "gui": _gui_health_snapshot(),
        }

    def _handle_inspect_documents(self, ctx: Any, arguments: dict) -> dict:
        """Read-only inventory of the open documents (GUI thread).

        One row per live document: the same lifetime generation the consent
        targets are bound to, the conservative dirty verdict owned by the
        documents tool (unknown state reads as dirty, never as clean), the
        active document, a pending transaction, and the object currently
        open in that document's GUI edit session.
        """

        # Sibling import of a private helper, mirroring the FEM probe: the
        # documents tool owns the dirty rule and is imported where used.
        from mcp_server.tools.documents import _is_dirty

        active_document = getattr(FreeCAD, "ActiveDocument", None)
        documents = []
        active_name = None
        for doc in FreeCAD.listDocuments().values():
            try:
                file_name = str(doc.FileName or "")
            except Exception:
                file_name = ""
            try:
                gui_document = ctx.Gui.getDocument(doc.Name)
            except Exception:
                gui_document = None
            if doc is active_document:
                # Only a listed document can be reported as the active one.
                active_name = str(doc.Name)
            documents.append(
                {
                    "name": str(doc.Name),
                    "label": str(doc.Label),
                    "fileName": file_name,
                    "objectCount": len(getattr(doc, "Objects", None) or ()),
                    "generation": int(ctx.document_generation(doc)),
                    "dirty": bool(_is_dirty(ctx, doc)),
                    "active": doc is active_document,
                    "transactionOpen": bool(getattr(doc, "HasPendingTransaction", False)),
                    "editObject": _active_edit_object(gui_document),
                }
            )
        return {"documents": documents, "activeDocument": active_name}

    # -- tools/call -------------------------------------------------------

    def _dispatch_tools_call(
        self, validated: dict, principal: str, connection_id: Any
    ) -> dict | StreamResponse:
        params = validated["params"]
        name = params.get("name")
        if isinstance(name, str) and name in self._definitions and not self._tool_enabled(name):
            # A disabled tool is not part of the surface: reject it before
            # argument-shape validation, schema checks, consent, operation
            # allocation or GUI dispatch.
            raise ProtocolError(METHOD_NOT_FOUND, f"unknown tool: {name}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProtocolError(
                INVALID_PARAMS,
                "invalid parameters: tools/call arguments must be an object",
            )
        definition = self._definitions.get(name)
        if definition is None or name not in self._handlers:
            raise ProtocolError(METHOD_NOT_FOUND, f"unknown tool: {name}")
        if not self._tool_enabled(name):
            raise ProtocolError(METHOD_NOT_FOUND, f"unknown tool: {name}")
        validate_schema(arguments, definition["inputSchema"])

        request_id = validated["id"]
        if name == "discover_capabilities" and arguments.get("refresh") is not True:
            # GUI-independent by design: answered from the startup snapshot
            # even while the GUI thread is stuck on other work. A refresh
            # needs the GUI thread and takes the normal blocking path.
            try:
                payload = self._tool_discover_capabilities(self, arguments)
            except ToolError as exc:
                return _rpc_result(request_id, tool_error_result(exc))
            return _with_caching(
                _rpc_result(request_id, self._validated_tool_result(name, payload)),
                CACHE_PRIVATE,
            )
        cancel_event = validated.get("cancel_event")
        if cancel_event is not None and cancel_event.is_set():
            # A legacy cancellation that arrived while the request was
            # queued: no effects, no consent round trip, no registration.
            return _rpc_result(
                request_id,
                tool_error_result(
                    ToolError(
                        CONSENT_DENIED,
                        "Operation cancelled before execution",
                        {"reason": "cancelled"},
                    )
                ),
            )
        try:
            target = self._consent_phase(
                name, arguments, params, principal, validated["client_capabilities"]
            )
        except ToolError as exc:
            return _rpc_result(request_id, tool_error_result(exc))
        except InputRequired as exc:
            return _rpc_result(
                request_id, input_required_result(exc.input_requests, exc.request_state)
            )
        if cancel_event is not None and cancel_event.is_set():
            # The event may have been set while consent was pending.
            return _rpc_result(
                request_id,
                tool_error_result(
                    ToolError(
                        CONSENT_DENIED,
                        "Operation cancelled before execution",
                        {"reason": "cancelled"},
                    )
                ),
            )

        deadline_s = _deadline_for(name, arguments)
        use_task = self._client_declares_tasks(validated) and name in (TASK_ELIGIBLE_OPERATIONS)
        if use_task:
            return self._start_task_call(validated, principal, name, arguments, target, deadline_s)
        return self._start_blocking_call(validated, principal, name, arguments, target, deadline_s)

    def _client_declares_tasks(self, validated: dict) -> bool:
        extensions = validated["client_capabilities"].get("extensions")
        return isinstance(extensions, Mapping) and TASKS_EXTENSION_ID in extensions

    # -- consent choreography ---------------------------------------------

    def _consent_phase(
        self,
        name: str,
        arguments: dict,
        params: dict,
        principal: str,
        client_capabilities: Mapping[str, Any] | None,
    ) -> dict | None:
        """Run preflight and consent for one tool call.

        Form-elicitation is offered, never required: a client that
        declares the ``elicitation.form`` capability gets the full MRTR
        round trip (challenge, retry, single-use nonce consumption), and
        a client WITHOUT that capability falls back to 1.0 behavior and
        proceeds without a prompt. Nothing is fabricated: a fallback
        execution is an unprompted execution, reported as such in the
        Report view.

        On a retry, the signed ``requestState`` is verified — even when
        the target no longer needs consent — before any effect. A
        changed target gets a fresh challenge for form-capable clients;
        every other consent failure is a ``CONSENT_DENIED`` tool error.
        The nonce is consumed under the signer lock exactly once, before
        task creation and mutations.
        """

        preflight = self._preflights.get(name)
        request_state = params.get("requestState")
        input_responses = params.get("inputResponses")
        method_key = f"tools/call:{name}"

        if preflight is None:
            if request_state is not None:
                raise ProtocolError(
                    INVALID_PARAMS,
                    f"invalid parameters: tool '{name}' does not use consent retries",
                )
            return None

        form_capable = _client_supports_form(client_capabilities)
        target = self._run_preflight(name, arguments)
        identity = _target_identity(target)

        if request_state is not None:
            try:
                self.signer.consume(
                    request_state,
                    principal=principal,
                    method=method_key,
                    arguments=arguments,
                    target=identity,
                    input_responses=input_responses,
                )
            except ToolError as exc:
                reason = (exc.details or {}).get("reason")
                if (
                    reason == "target_changed"
                    and target
                    and target.get("requires_consent")
                    and form_capable
                ):
                    message = target.get("message") or "Confirm this operation."
                    token = self.signer.challenge(
                        principal=principal,
                        method=method_key,
                        arguments=arguments,
                        target=identity,
                        message=message,
                    )
                    raise InputRequired(token, consent_input_request(message)) from exc
                raise
            return target

        if target is not None and target.get("requires_consent"):
            if not form_capable:
                # 1.0 fallback: no elicitation support, no prompt — the
                # operation proceeds unprompted. Make the bypass visible.
                FreeCAD.Console.PrintMessage(
                    f"[MCP] '{name}' proceeded without its consent prompt: "
                    "the client does not support form elicitation.\n"
                )
                return target
            message = target.get("message") or "Confirm this operation."
            token = self.signer.challenge(
                principal=principal,
                method=method_key,
                arguments=arguments,
                target=identity,
                message=message,
            )
            raise InputRequired(token, consent_input_request(message))
        return target

    def _run_preflight(self, name: str, arguments: dict) -> dict | None:
        preflight = self._preflights[name]

        def guarded() -> Any:
            try:
                return preflight(self, name, arguments)
            except ToolError as exc:
                # Preserve the domain code through the dispatcher: a refused
                # path or a missing file is PATH_NOT_ALLOWED/VALIDATION_FAILED,
                # not a broken GUI dispatch. Same channel the tool handlers
                # use to carry a ToolError back as the outcome value.
                return exc

        with self._direct_gui_scope(f"preflight:{name}") as rejection:
            if rejection is not None:
                raise ToolError(GUI_DISPATCH_FAILED, rejection.error)
            outcome = gui_dispatch.dispatch_to_gui(
                guarded,
                timeout=_PREFLIGHT_TIMEOUT_S,
                operation_name=f"preflight:{name}",
            )
            if outcome.error is not None:
                details = {"traceback": outcome.traceback} if outcome.traceback else None
                raise ToolError(
                    GUI_DISPATCH_FAILED,
                    f"preflight for '{name}' failed: {outcome.error}",
                    details,
                )
            target = outcome.value
            if isinstance(target, ToolError):
                raise target
            if target is not None and not isinstance(target, dict):
                raise ToolError(
                    GUI_DISPATCH_FAILED,
                    f"preflight for '{name}' returned an invalid target",
                )
            return target

    # -- operation registry ------------------------------------------------

    def _register_operation(
        self,
        name: str,
        *,
        kind: str,
        task_id: str | None,
        principal: str | None,
        deadline_s: float,
        cancel_event: threading.Event | None = None,
    ) -> _Operation:
        with self._state_lock:
            if self._bound and self._state != "running":
                raise ToolError(
                    SERVER_BUSY,
                    "MCP server is stopping; retry after it returns to running",
                    {"reason": "server_not_running", "state": self._state},
                )
        with self._ops_lock:
            if len(self._ops) >= MAX_OPERATIONS:
                raise ToolError(
                    SERVER_BUSY,
                    "Too many active operations; wait for one to finish",
                    {"reason": "operation_limit", "limit": MAX_OPERATIONS},
                )
            op = _Operation(
                op_id=uuid.uuid4().hex,
                name=name,
                kind=kind,
                task_id=task_id,
                principal=principal,
                deadline_mono=self._clock() + deadline_s,
                deadline_s=deadline_s,
                cancel_event=(cancel_event if cancel_event is not None else threading.Event()),
            )
            self._ops[op.op_id] = op
            return op

    def _remove_op(self, op: _Operation) -> None:
        with self._ops_lock:
            self._ops.pop(op.op_id, None)
            empty = not self._ops
        if empty:
            self._maybe_finish_draining()

    def _remove_op_by_id(self, op_id: str) -> None:
        with self._ops_lock:
            self._ops.pop(op_id, None)
            empty = not self._ops
        if empty:
            self._maybe_finish_draining()

    @contextmanager
    def _direct_gui_scope(self, operation_name: str):
        """Retain ownership until a direct GUI caller fully unwinds."""

        with self._state_lock:
            if self._bound and self._state != "running":
                yield gui_dispatch.Outcome(
                    error=f"'{operation_name}' was not started: MCP server is {self._state}"
                )
                return
        with self._direct_gui_lock:
            self._direct_gui_jobs += 1
        try:
            yield None
        finally:
            with self._direct_gui_lock:
                self._direct_gui_jobs -= 1
            self._maybe_finish_draining()

    def _operation_checkpoint(self, op_id: str) -> dict | None:
        """Receipt of one registered operation, or ``None``."""

        with self._ops_lock:
            op = self._ops.get(op_id)
        return dict(op.checkpoint) if op is not None and op.checkpoint else None

    def _release_operation_on_outcome(self, op: _Operation, outcome: Any) -> None:
        """Drop the operation at TRUE completion, flattening async futures.

        ``on_finished`` fires exactly once when the GUI callable actually
        completes (never at a waiter timeout). When that completion is a
        retained async Future (FEM), the operation stays registered until
        the Future resolves for real, so deadline tracking and the
        restart refusal remain truthful in between.
        """

        value = outcome.value
        if outcome.error is None and isinstance(value, concurrent.futures.Future):

            def on_resolved(_resolved: concurrent.futures.Future) -> None:
                self._remove_op(op)

            try:
                value.add_done_callback(on_resolved)
            except Exception:
                self._remove_op(op)
            return
        self._remove_op(op)

    def _maybe_finish_draining(self) -> None:
        """Once a draining server has no work left, finish into stopped.

        Qt/observer cleanup must not happen while any GUI work can still
        call back into the server. BOTH queues must be empty: the server's
        retained operations (an async FEM Future keeps its slot after the
        dispatcher job itself completed) AND the dispatcher's own inflight
        jobs (a stuck GUI callable has not returned yet). Only then is the
        state flipped to stopped — without another Start/Stop click — and
        the waker disposal scheduled, safely off the worker finalizer
        thread.
        """

        with self._state_lock:
            draining = self._state == "draining"
        if not draining:
            return
        if (
            self.pending_operation_count() != 0
            or gui_dispatch.pending_count() != 0
            or self._direct_gui_jobs != 0
        ):
            return
        with self._state_lock:
            if (
                self._state == "draining"
                and self.pending_operation_count() == 0
                and gui_dispatch.pending_count() == 0
                and self._direct_gui_jobs == 0
            ):
                self._state = "stopped"
                finish = True
            else:
                finish = False
        if finish:
            try:
                gui_dispatch.cleanup_waker()
            except Exception:
                pass

    def _overdue_operations(self) -> list[_Operation]:
        now = self._clock()
        with self._ops_lock:
            overdue = []
            for op in self._ops.values():
                if not op.deadline_noted and now >= op.deadline_mono:
                    op.deadline_noted = True
                    overdue.append(op)
            return overdue

    def has_pending_operations(self) -> bool:
        """True while any operation still awaits a late finalizer."""

        with self._ops_lock:
            operations = bool(self._ops)
        with self._direct_gui_lock:
            return operations or self._direct_gui_jobs != 0

    def pending_operation_count(self) -> int:
        """Number of retained operations (thread-safe read)."""

        with self._ops_lock:
            operations = len(self._ops)
        with self._direct_gui_lock:
            return operations + self._direct_gui_jobs

    # -- blocking execution -------------------------------------------------

    def _start_blocking_call(
        self,
        validated: dict,
        principal: str,
        name: str,
        arguments: dict,
        target: dict | None,
        deadline_s: float,
    ) -> dict | StreamResponse:
        """Blocking tools/call streamed over the request SSE.

        The response (final tool result or timeout error) is produced by a
        short-lived producer thread that owns the dispatch wait; the stream
        exists for the whole wait so a client disconnect requests
        cancellation of *that operation only* (queued work is rejected
        before FreeCAD entry; running work only gets a cooperative request).
        """

        request_id = validated["id"]
        # Legacy requests carry a session-owned cancel event and never get
        # a disconnect hook: an SSE disconnect must not cancel legacy work.
        is_legacy = validated.get("legacy_session_id") is not None
        try:
            op = self._register_operation(
                name,
                kind="blocking",
                task_id=None,
                principal=principal,
                deadline_s=deadline_s,
                cancel_event=validated.get("cancel_event"),
            )
        except ToolError as exc:
            return _rpc_result(request_id, tool_error_result(exc))
        op_ctx = _OpContext(self, operation=op, approved_target=_target_identity(target))
        handler = self._handlers[name]

        def runner() -> Any:
            try:
                result = handler(op_ctx, arguments)
                if isinstance(result, dict) and op_ctx.checkpoint:
                    result["checkpoint"] = dict(op_ctx.checkpoint)
                return result
            except ToolError as exc:
                if op_ctx.checkpoint and not isinstance(exc.details, str):
                    merged = dict(exc.details or {})
                    merged.setdefault("checkpoint", dict(op_ctx.checkpoint))
                    exc.details = merged
                return exc  # preserved through the dispatcher as the value

        events: queue.SimpleQueue = queue.SimpleQueue()

        def on_disconnect() -> None:
            op.cancel_event.set()  # cancellation of that operation only

        def produce() -> None:
            try:
                outcome = gui_dispatch.dispatch_to_gui(
                    runner,
                    timeout=deadline_s,
                    operation_name=f"tools/call:{name}",
                    cancel_event=op.cancel_event,
                    on_finished=lambda _outcome: self._release_operation_on_outcome(op, _outcome),
                )
                result = self._blocking_result(name, outcome, op)
            except ProtocolError as exc:
                # Infrastructure failure (e.g. schema-violating output):
                # a JSON-RPC error, never a fabricated tool result.
                events.put(error_response(exc, request_id))
                events.put(None)
                return
            except BaseException as exc:  # never lose the stream silently
                events.put(
                    error_response(
                        ProtocolError(INTERNAL_ERROR, f"unexpected dispatch failure: {exc}"),
                        request_id,
                    )
                )
                events.put(None)
                return
            events.put(_rpc_result(request_id, result))
            events.put(None)  # terminal sentinel: zero-chunk after final result

        threading.Thread(target=produce, name=f"mcp-blocking-{name}", daemon=True).start()
        return StreamResponse(
            events,
            on_disconnect=None if is_legacy else on_disconnect,
        )

    def _blocking_result(self, name: str, outcome: Any, op: _Operation) -> dict:
        """Convert one dispatch outcome into the wire tool result.

        A retained async Future (FEM) is flattened here: the SSE response
        completes only at the operation's actual completion, while the
        operation slot itself is released by
        :meth:`_release_operation_on_outcome` at the same real moment. A
        stuck-running timeout keeps the work truthfully running — both the
        dispatcher's stuck outcome for a running callable and this wait's
        own deadline/cancellation check for a retained Future.
        """

        if outcome.error is None:
            value = outcome.value
            if isinstance(value, concurrent.futures.Future):
                value = self._await_async_value(name, value, op)
            if isinstance(value, ToolError):
                self._merge_receipt(value, op.checkpoint)
                return tool_error_result(value)
            if name == "discover_capabilities" and isinstance(value, dict):
                # The handler captured GUI health mid-flight, while its own
                # job was still the active dispatch. That job has finalized
                # before the waiter resolves (health finish precedes future
                # resolution), so recapture here: the response reports the
                # dispatcher as it stands after this operation completed.
                value["gui"] = _gui_health_snapshot()
            return self._validated_tool_result(name, value)
        error = outcome.error
        if _STUCK_RUNNING_MARKER in error:
            tool_exc = ToolError(
                SERVER_BUSY,
                f"operation deadline exceeded; '{name}' is still running on the GUI thread",
                {"reason": "deadline_exceeded", "stillRunning": True},
            )
        else:
            details = {"traceback": outcome.traceback} if outcome.traceback else None
            tool_exc = ToolError(GUI_DISPATCH_FAILED, error, details)
        self._merge_receipt(tool_exc, op.checkpoint)
        return tool_error_result(tool_exc)

    def _merge_receipt(self, error: ToolError, receipt: dict | None) -> None:
        """Report a published checkpoint on a failed operation.

        A verified pre-operation copy already exists; the caller needs its
        path even though the operation failed, timed out, or was cancelled.
        """

        if not receipt:
            return
        if not isinstance(error.details, Mapping):
            error.details = {}
        merged = dict(error.details or {})
        merged.setdefault("checkpoint", dict(receipt))
        error.details = merged

    def _await_async_value(
        self, name: str, future: concurrent.futures.Future, op: _Operation
    ) -> Any:
        """Wait for the retained Future's real result, bounded by ``op``.

        Never fabricates a result: the wait ends at Future resolution, at
        the operation's monotonic deadline, or at a shared cancellation
        request — whichever comes first. Deadline expiry and cancellation
        return a truthful SERVER_BUSY tool error (the work is still
        running and cannot be safely cancelled) while the Future and the
        operation slot stay retained for the real completion. A
        ``ToolError`` resolution is a complete tool failure (isError
        result); any other escape is an infrastructure failure surfaced by
        the producer as a JSON-RPC error.
        """

        resolved = threading.Event()
        future.add_done_callback(lambda _future: resolved.set())
        while not future.done():
            remaining = op.deadline_mono - self._clock()
            if remaining <= 0:
                return ToolError(
                    SERVER_BUSY,
                    f"operation deadline exceeded; '{name}' is still running on the GUI thread",
                    {"reason": "deadline_exceeded", "stillRunning": True},
                )
            if op.cancel_event.is_set():
                return ToolError(
                    SERVER_BUSY,
                    f"cancellation of '{name}' was requested while it is "
                    "still running on the GUI thread",
                    {"reason": "cancelled", "stillRunning": True},
                )
            resolved.wait(timeout=min(_ASYNC_WAIT_TICK_S, remaining))
        try:
            return future.result()
        except concurrent.futures.CancelledError as exc:
            raise ProtocolError(INTERNAL_ERROR, f"async operation '{name}' was cancelled") from exc
        except ToolError as exc:
            return exc

    # -- task execution ------------------------------------------------------

    def _start_task_call(
        self,
        validated: dict,
        principal: str,
        name: str,
        arguments: dict,
        target: dict | None,
        deadline_s: float,
    ) -> dict:
        # The shared operation slot is reserved first so a full operation
        # cap never leaves an orphan task record behind; the record is then
        # created (queryable) before the flat resultType:"task" response is
        # sent, and consent was already consumed.
        try:
            op = self._register_operation(
                name,
                kind="task",
                task_id=None,
                principal=principal,
                deadline_s=deadline_s,
            )
        except ToolError as exc:
            return _rpc_result(validated["id"], tool_error_result(exc))
        try:
            record = self._task_store.create(name, arguments, principal=principal)
        except ToolError as exc:
            self._remove_op(op)
            return _rpc_result(validated["id"], tool_error_result(exc))
        op.task_id = record.task_id
        # ONE shared cancellation event per operation: tasks/cancel, the
        # service-tick deadline sweep, stop() and a blocking disconnect all
        # set the very Event the dispatcher and ctx handlers poll. Built
        # before _OpContext so ctx.cancel_event is that same object.
        op.cancel_event = record.cancel_event
        op_ctx = _OpContext(self, operation=op, approved_target=_target_identity(target))
        handler = self._handlers[name]

        def runner() -> Any:
            try:
                result = handler(op_ctx, arguments)
                if isinstance(result, dict) and op_ctx.checkpoint:
                    result["checkpoint"] = dict(op_ctx.checkpoint)
                return result
            except ToolError as exc:
                if op_ctx.checkpoint and not isinstance(exc.details, str):
                    merged = dict(exc.details or {})
                    merged.setdefault("checkpoint", dict(op_ctx.checkpoint))
                    exc.details = merged
                return exc

        task_id = record.task_id
        op_id = op.op_id

        def on_finished(outcome: Any) -> None:
            self._finalize_task(task_id, op_id, principal, name, outcome)

        submitted = gui_dispatch.submit_to_gui(
            runner,
            operation_name=f"tools/call:{name}",
            cancel_event=op.cancel_event,
            on_finished=on_finished,
        )
        # Deadline-sweep handle: lets the service thread mark this detached
        # job timed out without any waiter (see _service_actions).
        op.future = submitted
        return _rpc_result(validated["id"], complete_result(create_task_wire(record)))

    def _finalize_task(
        self,
        task_id: str,
        op_id: str,
        principal: str,
        name: str,
        outcome: Any,
    ) -> None:
        """Exactly-once task finalization after true dispatch completion."""

        if outcome.error is not None:
            error_text = outcome.error
            if any(marker in error_text for marker in _CANCELLED_OUTCOME_MARKERS):
                # True completion of work that never started (queued
                # cancellation / dispatcher shutdown): cancelled, not failed.
                self._task_store.finalize_cancelled(
                    task_id, principal=principal, status_message=error_text
                )
            else:
                details = {"traceback": outcome.traceback} if outcome.traceback else None
                tool_exc = ToolError(GUI_DISPATCH_FAILED, error_text, details)
                receipt = self._operation_checkpoint(op_id)
                if receipt:
                    self._merge_receipt(tool_exc, receipt)
                self._task_store.complete(
                    task_id,
                    tool_error_result(tool_exc),
                    principal=principal,
                    status_message=error_text,
                )
            self._publish_task_update(task_id, principal)
            self._remove_op_by_id(op_id)
            return

        value = outcome.value
        if isinstance(value, concurrent.futures.Future):
            # Asynchronous FEM: the handler returned its retained Future.
            # The operation stays registered (and the task stays working)
            # until the real QProcess/native result resolves it.
            self._attach_async_finalizer(
                value, name=name, task_id=task_id, op_id=op_id, principal=principal
            )
            return
        self._complete_task_from_value(task_id, op_id, principal, name, value)

    def _complete_task_from_value(
        self, task_id: str, op_id: str, principal: str, name: str, value: Any
    ) -> None:
        if isinstance(value, ToolError):
            receipt = self._operation_checkpoint(op_id)
            if receipt:
                self._merge_receipt(value, receipt)
            self._task_store.complete(
                task_id,
                tool_error_result(value),
                principal=principal,
                status_message=value.message,
            )
        else:
            try:
                result = self._validated_tool_result(name, value)
            except ProtocolError as exc:
                # A schema-violating output is a protocol infrastructure
                # failure, not a tool failure: the task fails truthfully.
                self._task_store.fail(task_id, exc, principal=principal)
            except (TypeError, ValueError) as exc:
                self._task_store.fail(
                    task_id,
                    ProtocolError(INTERNAL_ERROR, f"tool result not serializable: {exc}"),
                    principal=principal,
                )
            else:
                self._task_store.complete(task_id, result, principal=principal)
        self._publish_task_update(task_id, principal)
        self._remove_op_by_id(op_id)

    def _attach_async_finalizer(
        self,
        future: concurrent.futures.Future,
        *,
        name: str,
        task_id: str,
        op_id: str,
        principal: str,
    ) -> None:
        """Finalize a task from the retained async Future's actual result.

        The callback captures only this operation's values (task id,
        principal, tool name, op id); it never reads mutable
        current-operation state, so late resolution after overlapping
        operations cannot cross wires.
        """

        def on_done(resolved: concurrent.futures.Future) -> None:
            try:
                value = resolved.result()
                self._complete_task_from_value(task_id, op_id, principal, name, value)
            except BaseException as exc:
                try:
                    if isinstance(exc, ToolError):
                        self._task_store.complete(
                            task_id,
                            tool_error_result(exc),
                            principal=principal,
                            status_message=exc.message,
                        )
                    else:
                        self._task_store.fail(
                            task_id,
                            ProtocolError(
                                INTERNAL_ERROR,
                                f"async operation failed: {type(exc).__name__}: {exc}",
                            ),
                            principal=principal,
                        )
                    self._publish_task_update(task_id, principal)
                    self._remove_op_by_id(op_id)
                except Exception:
                    pass

        future.add_done_callback(on_done)

    def _publish_task_update(self, task_id: str, principal: str | None) -> None:
        try:
            task = self._task_store.get(task_id, principal=principal)
            self._registry.publish_task_status(detailed_task_wire(task))
        except Exception:
            pass  # notification delivery must never break finalization

    def _validated_tool_result(self, name: str, value: Any) -> dict:
        """Validate handler output against its registered schema, then wrap.

        ``capture_view``-style image payloads (``mimeType``/``data``) are
        released as PNG image content blocks, and their structured payload
        — including the real ``width``/``height`` — is kept as
        ``structuredContent`` so clients that resolve results through the
        declared output schema get the promised fields. A schema violation
        is a -32603 protocol infrastructure failure — never a -32602 input
        error — and non-finite values are rejected.
        """

        if isinstance(value, ToolError):
            return tool_error_result(value)
        definition = self._definitions[name]
        try:
            # Validate the handler's raw payload against its registered
            # schema BEFORE the capture_view image conversion: the schema
            # describes the handler's structured payload.
            validate_schema(value, definition["outputSchema"])
        except ProtocolError as exc:
            raise ProtocolError(
                INTERNAL_ERROR,
                f"tool '{name}' output violates its registered schema: {exc.message}",
                exc.data,
            ) from None
        payload: Any = value
        structured: dict | None = None
        if isinstance(payload, Mapping) and payload.get("mimeType") == "image/png":
            # Released both ways: the image content block carries the pixels
            # for visual clients, and the structured payload — including the
            # real width/height — satisfies the declared output schema.
            structured = dict(payload)
            payload = [
                {
                    "type": "image",
                    "mimeType": payload["mimeType"],
                    "data": payload["data"],
                }
            ]
        try:
            result = tool_result(payload)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                INTERNAL_ERROR,
                f"tool '{name}' produced a non-serializable result: {exc}",
            ) from exc
        if structured is not None:
            result["structuredContent"] = structured
        return result

    # -- task methods ---------------------------------------------------------

    def _dispatch_tasks_get(self, validated: dict, principal: str) -> dict:
        require_tasks_capability(validated["client_capabilities"])
        task = self._task_store.get(validated["params"].get("taskId"), principal=principal)
        return _rpc_result(validated["id"], complete_result(detailed_task_wire(task)))

    def _dispatch_tasks_update(self, validated: dict, principal: str) -> dict:
        require_tasks_capability(validated["client_capabilities"])
        task_id = validated["params"].get("taskId")
        self._task_store.get(task_id, principal=principal)
        # All tools elicit before task creation, so no input keys can be
        # outstanding: every supplied key is ignored (never granted
        # authority) and the acknowledgement is empty.
        return _rpc_result(validated["id"], complete_result({}))

    def _dispatch_tasks_cancel(self, validated: dict, principal: str) -> dict:
        require_tasks_capability(validated["client_capabilities"])
        self._task_store.request_cancel(validated["params"].get("taskId"), principal=principal)
        # Honest acknowledgement: a terminal task is not re-cancelled, the
        # cancel event is only a cooperative request either way.
        return _rpc_result(validated["id"], complete_result({}))

    # -- subscriptions ----------------------------------------------------------

    def _dispatch_listen(
        self, validated: dict, principal: str, connection_id: Any
    ) -> StreamResponse:
        params = validated["params"]
        notifications = params.get("notifications")
        if notifications is None:
            notifications = {}
        subscription_id = validated["id"]
        if isinstance(subscription_id, str) and len(subscription_id) > MAX_SUBSCRIPTION_ID_LENGTH:
            raise ProtocolError(
                INVALID_PARAMS,
                "invalid parameters: subscription id is too long",
            )
        if not isinstance(notifications, Mapping):
            raise ProtocolError(
                INVALID_PARAMS,
                "invalid parameters: notifications must be an object",
            )
        if notifications.get("taskIds"):
            # A task subscription is a task method: it requires the Tasks
            # extension capability on this request.
            require_tasks_capability(validated["client_capabilities"])
            for task_id in notifications["taskIds"]:
                # Existence and principal ownership are checked BEFORE the
                # stream is registered; a foreign id is indistinguishable
                # from an unknown one.
                self._task_store.get(task_id, principal=principal)
        with self._subscription_admission_lock:
            if len(self._registry) >= MAX_SUBSCRIPTIONS:
                raise ToolError(
                    SERVER_BUSY,
                    "Too many active subscriptions; close one and retry",
                    {"reason": "subscription_limit", "limit": MAX_SUBSCRIPTIONS},
                )
            subscription = self._registry.register(
                connection_id,
                subscription_id,
                notifications,
                principal=principal,
            )
        return StreamResponse(
            _SubscriptionStream(subscription),
            on_disconnect=subscription.close,
        )

    # -- document resources -------------------------------------------------------

    def _dispatch_resources_list(self, validated: dict) -> dict:
        payload = {
            "resources": [
                {
                    "uri": DOCUMENTS_RESOURCE_URI,
                    "name": "FreeCAD documents",
                    "description": "Live list of open FreeCAD documents",
                    "mimeType": "application/json",
                }
            ]
        }
        # Live document data: ttl 0, private scope.
        return _with_caching(_rpc_result(validated["id"], complete_result(payload)), CACHE_PRIVATE)

    def _dispatch_resources_read(self, validated: dict) -> dict:
        uri = validated["params"].get("uri")
        if uri != DOCUMENTS_RESOURCE_URI:
            raise ProtocolError(
                INVALID_PARAMS,
                f"invalid parameters: unknown resource uri: {uri}",
                {"uri": uri},
            )
        with self._direct_gui_scope("resources/read:freecad://documents") as rejection:
            if rejection is not None:
                raise ProtocolError(INTERNAL_ERROR, rejection.error)
            outcome = gui_dispatch.dispatch_to_gui(
                self._read_documents,
                timeout=_RESOURCE_READ_TIMEOUT_S,
                operation_name="resources/read:freecad://documents",
            )
            if outcome.error is not None:
                details = {"traceback": outcome.traceback} if outcome.traceback else None
                raise ProtocolError(
                    INTERNAL_ERROR, f"resource read failed: {outcome.error}", details
                )
            documents = outcome.value
            # Native ReadResourceResult: one JSON content entry whose text is
            # the compact document listing. Live document data: ttl 0, private.
            result = complete_result(
                {
                    "contents": [
                        {
                            "uri": DOCUMENTS_RESOURCE_URI,
                            "mimeType": "application/json",
                            "text": json.dumps(
                                documents,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                allow_nan=False,
                            ),
                        }
                    ]
                }
            )
            return _with_caching(_rpc_result(validated["id"], result), CACHE_PRIVATE)

    @staticmethod
    def _read_documents() -> dict:
        documents = []
        for doc in FreeCAD.listDocuments().values():
            try:
                file_name = doc.FileName or ""
            except Exception:
                file_name = ""
            documents.append(
                {
                    "name": doc.Name,
                    "label": doc.Label,
                    "fileName": file_name,
                    "objectCount": len(doc.Objects),
                }
            )
        return {"documents": documents}

    # ------------------------------------------------------------------
    def _service_actions(self) -> None:
        """Deadline sweep + task TTL maintenance on the HTTP service thread.

        Wired into ``ThreadingHTTPServer.service_actions`` (see
        ``McpHTTPServer``), so monotonic deadlines are checked independently
        of a stuck GUI. This thread never touches FreeCAD objects, never
        logs through ``FreeCAD.Console`` and never finalizes running work:
        overdue operations get their (shared) cancellation event set and
        task records a truthful status note, and a submitted dispatcher job
        is marked timed out so its health is truthful while the callable
        still runs (a queued one settles cancelled, as a waiter would).
        """

        self._task_store.service_tick()
        for op in self._overdue_operations():
            # Cooperative request only: OCC/Python work cannot be killed,
            # and the handler's own finalizer decides the real outcome.
            op.cancel_event.set()
            if op.kind == "task" and op.task_id is not None:
                try:
                    self._task_store.request_cancel(
                        op.task_id,
                        principal=op.principal,
                        status_message=(
                            f"deadline exceeded after {op.deadline_s:g}s; still running"
                        ),
                    )
                except Exception:
                    pass  # sweep must never raise into the HTTP loop
            if op.future is not None:
                # Detached job: no waiter would ever mark it, so the sweep
                # does — idempotent with a blocking caller's own timeout.
                try:
                    gui_dispatch.request_timeout(op.future, op.deadline_s)
                except Exception:
                    pass  # sweep must never raise into the HTTP loop

    # ------------------------------------------------------------------
    # Static capability capture (GUI thread, never touches user documents)
    # ------------------------------------------------------------------

    def _capture_static_capabilities(self, document: Any = None) -> None:
        self._static_capabilities = _capture_static_capabilities(document)

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    def bind(self) -> None:
        """Capture capabilities, initialize the dispatcher, start HTTP,
        then register the observer. Must run on the GUI thread. On failure
        the partial state is unwound and the server never reports running."""

        with self._state_lock:
            if self._state != "stopped":
                raise RuntimeError(f"server is {self._state}, not stopped")
            self._state = "starting"
        try:
            self._capture_static_capabilities()
            gui_dispatch.initialize()
            remote = bool(self.settings.get("remote_enabled", False))
            token = self.settings.get("token", "")
            if remote and not token:
                raise RuntimeError("remote_enabled requires a token")
            self._http = McpHTTPServer(
                self.dispatch,
                token=token if remote else None,
                host="0.0.0.0" if remote else "127.0.0.1",
                port=self.settings["port"],
                allowed_ips=self.settings.get("allowed_ips", ""),
                remote_enabled=remote,
                service_hook=self._service_actions,
            )
            # Constructor binds; failure above leaves nothing to unwind
            # except the dispatcher waker, handled by the caller.
            self._http.start()
            self._register_observer()
            # Deadline sweeping rides the HTTP server's own service loop
            # (ThreadingHTTPServer.service_actions); no GUI timer, no extra
            # thread.
        except BaseException:
            self._unwind()
            raise
        with self._state_lock:
            self._state = "running"
            self._bound = True

    def _unwind(self) -> None:
        """Release every partial startup artifact; never reports running."""

        self._remove_observer()
        if self._http is not None:
            try:
                self._http.stop()
            except Exception:
                pass
            self._http = None
        try:
            gui_dispatch.cleanup_waker()
        except Exception:
            pass
        with self._state_lock:
            self._state = "stopped"
            self._bound = False

    def stop(self) -> dict:
        """Stop accepting work, close streams, cancel queued jobs and
        request cancellation of running ones. Nonblocking: late finalizers
        of already-running work stay alive and are retained until they
        actually finish (this server object is kept alive by them).

        ``status()`` is never called while ``_state_lock`` is held (it
        takes that lock itself), subscriptions receive their graceful
        final result while the transport is still writable, and the Qt
        waker is only disposed once no retained GUI work is left.
        """

        with self._state_lock:
            if self._state not in ("running", "starting"):
                was_running = False
            else:
                self._state = "draining"
                was_running = True
        if not was_running:
            return self.status()
        # Graceful first: final complete subscription responses go out
        # through transports that are still writable.
        closed = self._registry.shutdown()
        if self._http is not None:
            try:
                self._http.stop()
            finally:
                self._http = None
        counts = gui_dispatch.shutdown()
        self._remove_observer()
        # Qt/waker disposal is deferred until the last retained operation
        # actually finishes (see _maybe_finish_draining).
        self._maybe_finish_draining()
        result = {
            "running": False,
            "state": "draining",
            "closedSubscriptions": closed,
            **counts,
        }
        result.update(self.status())
        return result

    def status(self) -> dict:
        with self._state_lock:
            state = self._state
        port = self._http.port if self._http is not None else None
        return {
            "running": state == "running",
            "state": state,
            "port": port,
            "endpoint": (
                f"http://{self._http.bound_host}:{port}/mcp"
                if self._http is not None and port
                else None
            ),
            "pendingOperations": self.pending_operation_count(),
            # Token-free fields for the native UI: dispatch health plus the
            # ACTIVE connection settings copied from this server's settings.
            "gui": _gui_health_snapshot(),
            "connection": {
                "remote_enabled": bool(self.settings.get("remote_enabled", False)),
                "allowed_ips": str(self.settings.get("allowed_ips", "")),
                "configured_port": self.settings.get("port"),
                "allow_scripts": bool(self.settings.get("allow_scripts", False)),
                "recovery_enabled": bool(self.settings.get("recovery_enabled", False)),
                "recovery_directory": str(self.settings.get("recovery_directory", "")),
            },
        }


class _SubscriptionStream:
    """``StreamResponse`` source bridging one ``Subscription``.

    Exposes the queue-like ``get(timeout=...)`` protocol the transport
    expects: wire messages pass through, poll timeouts surface as
    ``queue.Empty`` (SSE keepalive), and a closed subscription — graceful
    final result delivered, overflow, or disconnect — ends the stream
    normally with a ``None`` sentinel. The raw queue is never used
    directly: it cannot report closure once full (final/overflow sentinels
    are dropped), which would hang the stream forever.
    """

    __slots__ = ("_subscription",)

    def __init__(self, subscription: Any) -> None:
        self._subscription = subscription

    def get(self, timeout: float | None = None) -> Any:
        try:
            message = self._subscription.receive(timeout=timeout)
        except SubscriptionClosed:
            return None  # normal end of stream after close/final/overflow
        if message is None:
            # Still open and the poll timed out: transport keepalive.
            raise queue.Empty
        return message


# ---------------------------------------------------------------------------
# Static capability snapshot helpers (GUI thread).
# ---------------------------------------------------------------------------


def _probe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _supported_types_sample(document: Any = None) -> Any:
    """Complete sorted ``supportedTypes`` of the selected document.

    Read-only; never creates or mutates a document. No cut-off: the
    complete array supplies its own count.
    """

    if document is None:
        return {"unavailable": "no open document"}
    try:
        return sorted({str(entry) for entry in document.supportedTypes()})
    except Exception as exc:
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def _capture_static_capabilities(document: Any = None) -> dict:
    """Private snapshot of versions, workbenches, types, exporters, FEM and
    paths from actual FreeCAD getters. ``App.ApplicationDirectories`` is not
    a directory mapping and is never treated as one.

    ``document`` (or, when None, the first open document by sorted internal
    name) scopes the supported-types probe; the chosen document's name is
    reported as ``supportedTypesDocument``.
    """

    docs = FreeCAD.listDocuments()
    if document is None and docs:
        document = docs[sorted(docs.keys())[0]]
    document_name = None
    if document is not None:
        try:
            document_name = str(document.Name)
        except Exception:
            document_name = None
    try:
        from mcp_server.tools import fem as _fem_module

        availability_getter = getattr(_fem_module, "availability", None)
        fem_readiness = (
            _probe(availability_getter)
            if callable(availability_getter)
            else {"unavailable": "availability probe missing"}
        )
    except Exception as exc:
        fem_readiness = {"unavailable": f"{type(exc).__name__}: {exc}"}
    version = list(FreeCAD.Version())
    workbenches = _probe(lambda: sorted(FreeCADGui.listWorkbenches()))
    try:
        import Part

        occ_version = getattr(Part, "OCC_VERSION", None)
    except Exception as exc:
        occ_version = {"unavailable": f"{type(exc).__name__}: {exc}"}
    return {
        "freecad": {
            "version": version[:3] if len(version) >= 3 else version,
            "full": version,
        },
        "occ": {"version": occ_version},
        "workbenches": workbenches,
        "supportedTypes": _probe(lambda: _supported_types_sample(document)),
        "exporters": {
            "mesh": _module_available("Mesh"),
            "meshPart": _module_available("MeshPart"),
            "part": _module_available("Part"),
            "step": _module_available("Import"),
            "fem": _module_available("Fem"),
        },
        "fem": {
            "module": _module_available("Fem"),
            "objectsFem": _module_available("ObjectsFem"),
            "femsolver": _module_available("femsolver"),
            "readiness": fem_readiness,
        },
        "supportedTypesDocument": document_name,
        "paths": {
            "home": _probe(FreeCAD.getHomePath),
            "userAppData": _probe(FreeCAD.getUserAppDataDir),
            "userMacro": _probe(FreeCAD.getUserMacroDir),
            "temp": _probe(FreeCAD.getTempPath),
            "resource": _probe(FreeCAD.getResourceDir),
        },
    }


def _gui_health_snapshot() -> dict:
    """GUI dispatch health without waiting for (or touching) the GUI."""

    try:
        snapshot = gui_dispatch.get_dispatch_status()
    except Exception as exc:
        return {"unavailable": f"{type(exc).__name__}: {exc}"}
    return {
        "state": snapshot.get("state"),
        "operation": snapshot.get("operation"),
        "runningForSeconds": snapshot.get("running_for_seconds"),
        "timeoutSeconds": snapshot.get("timeout_seconds"),
        "queuedJobs": snapshot.get("queued_jobs"),
        "draining": snapshot.get("draining"),
    }


def _active_edit_object(gui_document: Any) -> str | None:
    """Name of the object a GUI document has in active edit, or None.

    Null-safe probes only: no GUI document (headless, closed, or missing)
    and no active object both answer None, and a value provider is resolved
    through the document object it wraps when it exposes one.
    """

    active = getattr(gui_document, "ActiveObject", None)
    if active is None:
        return None
    target = getattr(active, "Object", None)
    if target is None:
        target = active
    name = getattr(target, "Name", None)
    return str(name) if name else None


def _unpack_definition(definition: Any) -> tuple[str, str, Any, Any]:
    if isinstance(definition, Mapping):
        return (
            definition["name"],
            definition.get("description", ""),
            definition.get("inputSchema"),
            definition.get("outputSchema"),
        )
    name, description, input_schema, output_schema = definition
    return name, description, input_schema, output_schema


#: Compact ``discover_capabilities`` keeps only the summary blocks; the
#: heavy arrays (workbenches, supportedTypes, paths) need ``detail: "full"``.
_COMPACT_CAPABILITY_KEYS = ("freecad", "occ", "exporters", "fem")

#: Complete document-row shape: every field is always present so a client
#: can rely on one stable row layout.
_DOCUMENT_ROW_PROPERTIES = {
    "name": {"type": "string"},
    "label": {"type": "string"},
    "fileName": {"type": "string"},
    "objectCount": {"type": "integer", "minimum": 0},
    "generation": {"type": "integer", "minimum": 0},
    "dirty": {"type": "boolean"},
    "active": {"type": "boolean"},
    "transactionOpen": {"type": "boolean"},
    "editObject": {"type": ["string", "null"]},
}

_DOCUMENT_ROW_REQUIRED = [
    "name",
    "label",
    "fileName",
    "objectCount",
    "generation",
    "dirty",
    "active",
    "transactionOpen",
    "editObject",
]


def _compact_capabilities(snapshot: Mapping[str, Any]) -> dict:
    """Summary projection of one capability snapshot.

    The four summary blocks plus the supported-types count (``None`` when
    no list was probed, with the reason in ``supportedTypesUnavailable``)
    and the document the types came from. Never mutates ``snapshot``: the
    cache is a shared object.
    """

    compact = {key: snapshot[key] for key in _COMPACT_CAPABILITY_KEYS if key in snapshot}
    supported = snapshot.get("supportedTypes")
    compact["supportedTypesCount"] = len(supported) if isinstance(supported, list) else None
    compact["supportedTypesDocument"] = snapshot.get("supportedTypesDocument")
    if isinstance(supported, Mapping) and "unavailable" in supported:
        # A snapshot captured with no document open cannot carry the type
        # list; compact must say why instead of reporting bare nulls.
        compact["supportedTypesUnavailable"] = str(supported["unavailable"])
    return compact


def _discover_definition() -> dict:
    return {
        "name": "discover_capabilities",
        "description": (
            "Private snapshot of FreeCAD/OCC versions, workbenches, complete "
            "supportedTypes, exporter and FEM availability, and FreeCAD paths, "
            "plus GUI dispatch health reported separately without waiting for "
            "a GUI dispatch. The default compact detail returns the summary "
            "blocks plus supportedTypesCount and supportedTypesDocument; "
            "when the snapshot was captured with no document open, both are "
            "null and supportedTypesUnavailable carries the reason until a "
            "call with refresh true recaptures them. detail 'full' returns "
            "the complete snapshot."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Recapture the static snapshot on the GUI thread before "
                        "answering (default false: the cached snapshot is used)."
                    ),
                },
                "detail": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "default": "compact",
                    "description": (
                        "compact (default) returns the summary blocks plus "
                        "supportedTypesCount/supportedTypesDocument; full returns "
                        "the complete snapshot including workbenches, "
                        "supportedTypes and paths."
                    ),
                },
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "capabilities": {"type": "object"},
                "gui": {"type": "object"},
            },
            "required": ["capabilities", "gui"],
            "additionalProperties": False,
        },
    }


def _inspect_documents_definition() -> dict:
    return {
        "name": "inspect_documents",
        "description": (
            "Inventory of the open FreeCAD documents: one row per document "
            "with its actual Name and Label, file path, object count, live "
            "change generation, conservative dirty verdict, whether it is the "
            "active document, whether a transaction is pending, and the object "
            "currently open in that document's GUI edit session. Read-only: "
            "nothing is recomputed or mutated, and activeDocument is null when "
            "no listed document is active."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "documents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": dict(_DOCUMENT_ROW_PROPERTIES),
                        "required": list(_DOCUMENT_ROW_REQUIRED),
                        "additionalProperties": False,
                    },
                },
                "activeDocument": {"type": ["string", "null"]},
            },
            "required": ["documents", "activeDocument"],
            "additionalProperties": False,
        },
    }


def _with_caching(response: dict, hints: Mapping[str, Any]) -> dict:
    """Stamp the released ``CacheableResult`` fields on one result.

    ``ttlMs`` and ``cacheScope`` are TOP-LEVEL result fields per the
    released schema — never an invented ``_meta`` caching key.
    """

    result = response.get("result")
    if isinstance(result, dict):
        result["ttlMs"] = hints["ttlMs"]
        result["cacheScope"] = hints["cacheScope"]
    return response


# ---------------------------------------------------------------------------
# Module lifecycle (the later commands.py/InitGui cutover calls these).
# ---------------------------------------------------------------------------

_server_lock = threading.RLock()
_server: Server | None = None


def get_server() -> Server | None:
    """The live server, if one exists (running or draining)."""

    with _server_lock:
        return _server


def start_server() -> dict:
    """Start the embedded MCP server. GUI thread only.

    Version guard (1.1.3 <= v < 1.2) runs before any binding or observer
    registration; invalid settings fail closed. A duplicate start of an
    already-running server returns its existing state. A start refused
    while a previous server's GUI work is still active raises an actionable
    error; a bind failure unwinds observers/waker and never reports running.
    """

    global _server
    with _server_lock:
        current = _server
        if current is not None:
            with current._state_lock:
                state = current._state
            if state == "running":
                return current.status()
            if state == "starting":
                raise RuntimeError("MCP server is starting; retry in a moment")
            # draining/stopped: refuse while late GUI work is still active.
            if current.has_pending_operations() or gui_dispatch.pending_count() > 0:
                raise RuntimeError(
                    "previous MCP server still has running GUI operations; "
                    "restart is refused until they finish"
                )
        _check_freecad_version()
        settings = load_settings()  # SettingsError propagates: fail closed
        server = Server(settings=settings)
        server.bind()
        _server = server
        return server.status()


def stop_server() -> dict:
    """Stop the server. GUI thread preferred (observer/waker cleanup).
    Nonblocking; safe to call when not running."""

    global _server
    with _server_lock:
        server = _server
    if server is None:
        return {"running": False, "state": "stopped"}
    result = server.stop()
    with _server_lock:
        # Retain the drained-but-still-finishing server so its late
        # finalizers can complete; a fresh start replaces it only when it
        # has no pending operations left.
        if not server.has_pending_operations() and gui_dispatch.pending_count() == 0:
            _server = None
    return result


def server_status() -> dict:
    with _server_lock:
        server = _server
    if server is None:
        return {
            "running": False,
            "state": "stopped",
            "port": None,
            "endpoint": None,
            "pendingOperations": 0,
            "gui": _gui_health_snapshot(),
            "connection": {},
        }
    return server.status()
