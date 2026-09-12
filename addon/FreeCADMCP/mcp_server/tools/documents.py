"""Document lifecycle tools for MCP v2: new, open, save, close, reload.

Every handler runs on the GUI thread; the server dispatches tool calls, so
these functions never dispatch themselves and never wait. ``preflight`` is
called by the server on the GUI thread before task creation and before any
mutation, both for the initial consent challenge and again on an accepted
retry; it must therefore stay pure (no document mutations, no recompute) and
deterministic for the same arguments.

Consent targets
---------------

``preflight`` returns ``None`` when the operation needs no consent and a
target dictionary otherwise. The server binds the consent challenge to a
fingerprint of the target while excluding the ``requires_consent`` and
``message`` keys, so those two keys never influence consent identity. File
targets carry the canonical real path plus the server's file fingerprint at
preflight time; document targets carry the stable document identity and the
current generation. Handlers recompute the same target immediately before an
effect and require it to match ``ctx.approved_target`` (identity keys only)
under :func:`_require_approved`; a mismatch is a CONSENT_DENIED
``target_changed`` tool error that mutates nothing and makes the client
request fresh consent.

Dirty-state detection
---------------------

FreeCAD 1.1.3's ``Document.Modified`` is a plain bool, but runtime evidence
showed it can stay ``True`` even immediately after a native save. The tool
therefore never interprets ``Modified`` as authoritative truth for consent:
``True`` always prompts, and anything that is not a bool is treated
conservatively as ``True`` so consent is never skipped on unknown state. An
unsaved nonempty document (no ``FileName`` but objects present) is dirty too.

Failure semantics
-----------------

``open_document`` removes only documents the failed load newly introduced,
never preexisting ones. ``reload_document`` closes first and reports a failed
reopen truthfully as "closed, reopen failed" without claiming rollback.
Saving to the document's own current file path is ordinary save behavior;
only an explicit save-as onto a different existing file requires overwrite
consent, and native ``save``/``saveAs`` are used (no staged copies).
"""

from __future__ import annotations

import os
from typing import Any

from ..protocol import (
    CONSENT_DENIED,
    PATH_NOT_ALLOWED,
    VALIDATION_FAILED,
    ToolError,
    fingerprint,
)

_FCSTD_SUFFIX = ".fcstd"

# Keys the server excludes from the consent-binding fingerprint; identity
# comparison ignores them exactly the same way (agreed with ServerIntegration).
_IDENTITY_EXCLUDED_KEYS = frozenset({"requires_consent", "message"})

_MAX_NAME_LENGTH = 100


# Shared helpers.
# ---------------------------------------------------------------------------


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _validate_fcstd_path(path: str, *, what: str) -> None:
    """Reject any target the native document tools must not touch."""

    if os.path.splitext(path)[1].casefold() != _FCSTD_SUFFIX:
        raise ToolError(
            VALIDATION_FAILED,
            f"{what} must have a .FCStd extension, got '{os.path.basename(path)}'",
        )


def _canonical_document_path(ctx: Any, path: Any, *, what: str) -> str:
    """Canonicalize ``path`` for open/save; raises PATH_NOT_ALLOWED outside."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError(VALIDATION_FAILED, f"{what} must be a non-empty string")
    canonical = ctx.canonical_path(path)
    if not isinstance(canonical, str) or not canonical:
        raise ToolError(
            PATH_NOT_ALLOWED,
            f"{what} is outside the configured allowed roots",
        )
    return canonical


def _reload_path(ctx: Any, doc: Any) -> str:
    """Canonical saved file of ``doc``, validated against the allowed roots.

    Refuses an unsaved document, a path outside ``allowed_roots``, a
    non-FCStd file and a missing file. The same check runs again immediately
    before the native close/reopen.
    """

    raw = str(getattr(doc, "FileName", "") or "")
    if not raw:
        raise ToolError(
            VALIDATION_FAILED,
            f"document '{doc.Name}' has never been saved; there is no file to reload from",
        )
    canonical = _canonical_document_path(ctx, raw, what="reload path")
    _validate_fcstd_path(canonical, what="reload path")
    if not os.path.isfile(canonical):
        raise ToolError(
            VALIDATION_FAILED,
            f"the saved file for '{doc.Name}' no longer exists: '{canonical}'",
        )
    return canonical


def _document_save_path(ctx: Any, doc: Any) -> str | None:
    """Canonical containment-checked path of ``doc``'s current file.

    Returns ``None`` for a document that has never been saved, and raises
    ``PATH_NOT_ALLOWED`` when the current file sits outside the allowed
    roots: an implicit save must not write outside the containment policy
    that an explicit save-as already enforces.
    """

    raw = str(getattr(doc, "FileName", "") or "")
    if not raw:
        return None
    canonical = _canonical_document_path(ctx, raw, what="document path")
    _validate_fcstd_path(canonical, what="document path")
    return canonical


def _is_dirty(ctx: Any, doc: Any) -> bool:
    """Conservative dirty verdict; unknown state prompts.

    ``Modified`` being ``True`` always requires consent. A missing or
    non-bool ``Modified`` is treated as dirty. An unsaved nonempty document
    is dirty even when ``Modified`` reports ``False``.
    """

    modified: Any = None
    try:
        gui_doc = ctx.Gui.getDocument(doc.Name)
    except Exception:
        gui_doc = None
    if gui_doc is not None:
        modified = getattr(gui_doc, "Modified", None)
    if modified is None:
        modified = getattr(doc, "Modified", None)
    if modified is True:
        return True
    if not isinstance(modified, bool):
        return True
    if modified:
        return True
    has_file = bool(getattr(doc, "FileName", ""))
    has_objects = bool(getattr(doc, "Objects", None))
    return not has_file and has_objects


def _target_identity(target: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in target.items() if key not in _IDENTITY_EXCLUDED_KEYS}


def _require_approved(ctx: Any, target: dict[str, Any] | None) -> None:
    """Recheck the consent target immediately before an effect.

    The server stored ``ctx.approved_target`` when it consumed the consent
    nonce. The recomputed target must carry the same identity keys; anything
    else (including no approved target at all) is a changed-target rejection
    that performs no effect and asks the client for fresh consent.
    """

    if target is None:
        return
    approved = getattr(ctx, "approved_target", None)
    if approved is None or fingerprint(_target_identity(approved)) != fingerprint(
        _target_identity(target)
    ):
        raise ToolError(
            CONSENT_DENIED,
            "the approved consent target no longer matches the current state; "
            "retry the operation to request fresh consent",
            {"reason": "target_changed"},
        )


def _file_consent_target(ctx: Any, path: str, *, purpose: str, message: str) -> dict[str, Any]:
    return {
        "kind": "file",
        "path": path,
        "fingerprint": ctx.file_fingerprint(path),
        "purpose": purpose,
        "requires_consent": True,
        "message": message,
    }


def _document_consent_target(ctx: Any, doc: Any, *, purpose: str, message: str) -> dict[str, Any]:
    return {
        "kind": "document",
        "identity": ctx.document_identity(doc),
        "generation": ctx.document_generation(doc),
        "purpose": purpose,
        "requires_consent": True,
        "message": message,
    }


def _document_payload(doc: Any, *, path: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": str(doc.Name),
        "label": str(doc.Label),
        "objectCount": len(getattr(doc, "Objects", None) or ()),
    }
    if path is not None:
        payload["path"] = str(path)
    return payload


# ---------------------------------------------------------------------------
# Preflight (called by the server on the GUI thread; pure, no effects).
# ---------------------------------------------------------------------------


def preflight(ctx: Any, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Return the consent target for ``name`` or ``None`` when none is needed."""

    if name == "open_document":
        return _open_preflight(ctx, args)
    if name == "save_document":
        return _save_preflight(ctx, args)
    if name == "close_document":
        return _close_preflight(ctx, args)
    if name == "reload_document":
        return _reload_preflight(ctx, args)
    return None  # new_document


def _open_preflight(ctx: Any, args: dict[str, Any]) -> dict[str, Any] | None:
    path = _canonical_document_path(ctx, args.get("path"), what="open path")
    _validate_fcstd_path(path, what="open path")
    if not os.path.isfile(path):
        raise ToolError(VALIDATION_FAILED, f"no such document file: '{path}'")
    untrusted = args.get("untrusted", True)
    if untrusted is not True and untrusted is not False:
        raise ToolError(VALIDATION_FAILED, "untrusted must be a boolean when provided")
    if not untrusted:
        return None
    message = (
        f"Open the FreeCAD document from '{path}'? The file is untrusted: "
        "FCStd documents can execute embedded Python when loaded, which runs "
        "with your full user privileges."
    )
    return _file_consent_target(ctx, path, purpose="open", message=message)


def _save_preflight(ctx: Any, args: dict[str, Any]) -> dict[str, Any] | None:
    doc = ctx.require_document(args["document"])
    path = args.get("path")
    if path is None:
        # An implicit save writes to the document's own file, so the same
        # containment check as an explicit save-as must run before the write.
        _document_save_path(ctx, doc)
        return None
    canonical = _canonical_document_path(ctx, path, what="save path")
    _validate_fcstd_path(canonical, what="save path")
    current = getattr(doc, "FileName", "")
    if current and os.path.realpath(current) == canonical:
        return None  # ordinary save to the document's own file
    if not os.path.exists(canonical):
        parent = os.path.dirname(canonical)
        if not os.path.isdir(parent):
            raise ToolError(
                VALIDATION_FAILED,
                f"save path parent directory does not exist: '{os.path.dirname(canonical)}'",
            )
        return None  # new file; nothing to overwrite
    message = (
        f"Saving document '{doc.Name}' will overwrite the existing file "
        f"'{canonical}'. Its current content will be replaced."
    )
    return _file_consent_target(ctx, canonical, purpose="overwrite", message=message)


def _close_preflight(ctx: Any, args: dict[str, Any]) -> dict[str, Any] | None:
    doc = ctx.require_document(args["document"])
    if not _is_dirty(ctx, doc):
        return None
    message = (
        f"Close document '{doc.Name}'? It has unsaved changes (or has never "
        "been saved); closing will discard them."
    )
    return _document_consent_target(ctx, doc, purpose="close-discard", message=message)


def _reload_preflight(ctx: Any, args: dict[str, Any]) -> dict[str, Any] | None:
    doc = ctx.require_document(args["document"])
    path = _reload_path(ctx, doc)
    if not _is_dirty(ctx, doc):
        return None
    message = (
        f"Reload document '{doc.Name}' from '{path}'? The document has "
        "unsaved changes that will be discarded."
    )
    return _document_consent_target(ctx, doc, purpose="reload-discard", message=message)


# ---------------------------------------------------------------------------
# Tool definitions.
# ---------------------------------------------------------------------------

_DOCUMENT_SCHEMA = {"type": "string", "minLength": 1}

_DOCUMENT_COUNT_PROPERTIES = {
    "name": {"type": "string", "minLength": 1},
    "label": {"type": "string"},
    "objectCount": {"type": "integer", "minimum": 0},
}

_DOCUMENT_COUNT_REQUIRED = ["name", "label", "objectCount"]


def _definition(
    name: str,
    description: str,
    input_properties: dict[str, Any],
    required: list[str],
    output_properties: dict[str, Any],
    output_required: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": input_properties,
            "required": required,
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": output_properties,
            "required": output_required,
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS = [
    _definition(
        "new_document",
        "Create a new empty FreeCAD document and return its actual sanitized "
        "Name, Label and object count. The requested name is sanitized and "
        "de-duplicated by FreeCAD; only the returned Name is a valid "
        "document identity for later calls.",
        {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_NAME_LENGTH,
                "description": "Requested document name; FreeCAD sanitizes it.",
            }
        },
        ["name"],
        dict(_DOCUMENT_COUNT_PROPERTIES),
        list(_DOCUMENT_COUNT_REQUIRED),
    ),
    _definition(
        "open_document",
        "Open an existing .FCStd document from an allowed root and return its "
        "actual Name, Label, object count and file path. Untrusted opens "
        "(the default) require MRTR consent first, because loading an FCStd "
        "file can execute embedded Python; a granted consent never certifies "
        "the file safe. A failed load removes only documents the failure "
        "newly introduced, never preexisting ones. A path that is already "
        "open returns the live in-memory document without re-reading the "
        "file, reported as alreadyOpen.",
        {
            "path": {"type": "string", "minLength": 1},
            "untrusted": {
                "type": "boolean",
                "default": True,
                "description": "Require consent before opening (default true).",
            },
        },
        ["path"],
        {
            **_DOCUMENT_COUNT_PROPERTIES,
            "path": {"type": "string", "minLength": 1},
            "alreadyOpen": {"type": "boolean"},
        },
        [*_DOCUMENT_COUNT_REQUIRED, "path", "alreadyOpen"],
    ),
    _definition(
        "save_document",
        "Save a document with native save/saveAs. Without ``path`` the "
        "document is saved to its existing file; an unsaved document without "
        "``path`` is an actionable error. An explicit ``path`` performs a "
        "native save-as: saving to the document's own current file is normal "
        "behavior, while saving over a different existing file requires "
        "overwrite consent first.",
        {
            "document": _DOCUMENT_SCHEMA,
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Explicit save-as target (.FCStd).",
            },
        },
        ["document"],
        {
            "document": {"type": "string", "minLength": 1},
            "path": {"type": "string", "minLength": 1},
        },
        ["document", "path"],
    ),
    _definition(
        "close_document",
        "Close exactly one document. A dirty or unsaved nonempty document "
        "requires MRTR consent first; unsaved changes are then discarded. "
        "Only the named document is closed. The result reports the path and "
        "whether the pre-close state discarded unsaved changes.",
        {"document": _DOCUMENT_SCHEMA},
        ["document"],
        {
            "document": {"type": "string", "minLength": 1},
            "path": {"type": ["string", "null"]},
            "discardedChanges": {"type": "boolean"},
        },
        ["document", "path", "discardedChanges"],
    ),
    _definition(
        "reload_document",
        "Reload a document from its saved .FCStd file: requires a saved "
        "existing path, asks for dirty-discard consent when the document is "
        "dirty, then closes and reopens it. Returns the actual reopened "
        "Name, Label, object count and file path. If reopening fails after "
        "the close, the error says so truthfully; no rollback is claimed "
        "because the old in-memory document is already gone.",
        {"document": _DOCUMENT_SCHEMA},
        ["document"],
        {
            **_DOCUMENT_COUNT_PROPERTIES,
            "path": {"type": "string", "minLength": 1},
        },
        [*_DOCUMENT_COUNT_REQUIRED, "path"],
    ),
]


# ---------------------------------------------------------------------------
# Handlers (GUI thread; the server dispatches and consent-checks first).
# ---------------------------------------------------------------------------


def _new_document(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    name = arguments["name"]
    try:
        doc = ctx.App.newDocument(name)
        doc.recompute()
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED, f"creating document '{name}' failed: {_describe(exc)}"
        ) from exc
    return _document_payload(doc)


def _open_document(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    canonical = _canonical_document_path(ctx, arguments.get("path"), what="open path")
    _validate_fcstd_path(canonical, what="open path")
    if not os.path.isfile(canonical):
        raise ToolError(VALIDATION_FAILED, f"no such document file: '{canonical}'")
    untrusted = arguments.get("untrusted", True)
    if untrusted is not True and untrusted is not False:
        raise ToolError(VALIDATION_FAILED, "untrusted must be a boolean when provided")
    if untrusted:
        _require_approved(ctx, _open_preflight(ctx, arguments))

    app = ctx.App
    before = set(app.listDocuments())
    try:
        doc = app.openDocument(canonical)
    except Exception as exc:
        # FreeCAD may leave a partially loaded document behind. Remove only
        # documents this failed load newly introduced; preexisting documents
        # are never touched.
        newly_introduced: list[str] = []
        cleanup_failures: list[str] = []
        for doc_name in list(app.listDocuments()):
            if doc_name in before:
                continue
            newly_introduced.append(doc_name)
            try:
                app.closeDocument(doc_name)
            except Exception as cleanup_exc:
                cleanup_failures.append(f"'{doc_name}': {_describe(cleanup_exc)}")
        details: dict[str, Any] = {
            "removedFailedOpenDocuments": newly_introduced,
        }
        if cleanup_failures:
            details["cleanupFailures"] = cleanup_failures
        raise ToolError(
            VALIDATION_FAILED,
            f"failed to open '{canonical}': {_describe(exc)}",
            details,
        ) from exc
    payload = _document_payload(doc, path=doc.FileName)
    # FreeCAD returns the already-open document for a path that is open
    # instead of re-reading the file. Say so: the in-memory document may
    # hold unsaved changes the caller did not expect to see.
    payload["alreadyOpen"] = doc.Name in before
    return payload


def _save_document(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    doc = ctx.require_document(arguments["document"])
    ctx.check_document_idle(doc)
    path = arguments.get("path")
    try:
        if path is None:
            if _document_save_path(ctx, doc) is None:
                raise ToolError(
                    VALIDATION_FAILED,
                    f"document '{doc.Name}' has never been saved; provide an explicit save path",
                )
            doc.save()
        else:
            canonical = _canonical_document_path(ctx, path, what="save path")
            _validate_fcstd_path(canonical, what="save path")
            parent = os.path.dirname(canonical)
            if not os.path.isdir(parent):
                raise ToolError(
                    VALIDATION_FAILED,
                    f"save path parent directory does not exist: '{parent}'",
                )
            current = doc.FileName
            if current and os.path.realpath(current) == canonical:
                doc.save()  # ordinary save to the document's own file
            else:
                if os.path.exists(canonical):
                    _require_approved(ctx, _save_preflight(ctx, arguments))
                doc.saveAs(canonical)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"saving document '{doc.Name}' failed: {_describe(exc)}",
        ) from exc
    saved_path = str(doc.FileName)
    if not saved_path:
        raise ToolError(
            VALIDATION_FAILED,
            f"saving document '{doc.Name}' left it without a file path",
        )
    return {"document": doc.Name, "path": saved_path}


def _close_document(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    doc = ctx.require_document(arguments["document"])
    ctx.check_document_idle(doc)
    discarded_changes = _is_dirty(ctx, doc)
    if discarded_changes:
        _require_approved(ctx, _close_preflight(ctx, arguments))
    name = str(doc.Name)
    path = str(getattr(doc, "FileName", "")) or None
    try:
        ctx.App.closeDocument(name)
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"closing document '{name}' failed: {_describe(exc)}",
        ) from exc
    return {
        "document": name,
        "path": path,
        "discardedChanges": discarded_changes,
    }


def _reload_document(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    doc = ctx.require_document(arguments["document"])
    ctx.check_document_idle(doc)
    path = _reload_path(ctx, doc)
    if _is_dirty(ctx, doc):
        _require_approved(ctx, _reload_preflight(ctx, arguments))
    name = str(doc.Name)
    try:
        ctx.App.closeDocument(name)
    except Exception as exc:
        raise ToolError(
            VALIDATION_FAILED,
            f"closing document '{name}' before reload failed: {_describe(exc)}",
        ) from exc
    try:
        reopened = ctx.App.openDocument(path)
    except Exception as exc:
        # The document was already closed: report the truth instead of
        # claiming a rollback.
        raise ToolError(
            VALIDATION_FAILED,
            f"document '{name}' was closed, but reopening '{path}' failed: "
            f"{_describe(exc)}; the document is no longer open",
        ) from exc
    return _document_payload(reopened, path=reopened.FileName)


HANDLERS = {
    "new_document": _new_document,
    "open_document": _open_document,
    "save_document": _save_document,
    "close_document": _close_document,
    "reload_document": _reload_document,
}
