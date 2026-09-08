"""Focused tests for the MCP v2 document lifecycle tools.

Runs on the host without FreeCAD: ``mcp_server.tools.documents`` is imported
against a fake ``ctx`` implementing the agreed server contract (App, Gui,
document identity/generation, canonical paths, file fingerprints, consent
approval) and fake App/Gui documents. The real ``mcp_server.protocol`` module
provides ToolError and fingerprints, so consent-target comparison is tested
against the production primitives.
"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.protocol import (  # noqa: E402
    CONSENT_DENIED,
    PATH_NOT_ALLOWED,
    VALIDATION_FAILED,
    ToolError,
)
from mcp_server.tools import documents  # noqa: E402


class FakeDoc:
    def __init__(
        self,
        name,
        *,
        label=None,
        file_name="",
        modified=False,
        objects=(),
        with_modified_attr=True,
    ):
        self.Name = name
        self.Label = label or name
        self.FileName = file_name
        self.Objects = list(objects)
        if with_modified_attr:
            self.Modified = modified
        # with_modified_attr=False leaves no Modified attribute (unknown state).

    @property
    def object_count(self):
        return len(self.Objects)

    def recompute(self):
        pass


class FakeApp:
    def __init__(self):
        self.documents: dict[str, FakeDoc] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_open_next: Exception | None = None
        self.open_leaves_partial: str | None = None

    def listDocuments(self):
        return dict(self.documents)

    def newDocument(self, name):
        sanitized = name.replace(" ", "_")
        base = sanitized
        counter = 0
        while sanitized in self.documents:
            counter += 1
            sanitized = f"{base}{counter:03d}"
        doc = FakeDoc(sanitized)
        self.documents[sanitized] = doc
        self.calls.append(("newDocument", sanitized))
        return doc

    def openDocument(self, path):
        if self.fail_open_next is not None:
            exc = self.fail_open_next
            self.fail_open_next = None
            if self.open_leaves_partial:
                partial = FakeDoc(
                    self.open_leaves_partial, file_name=path, objects=[object()]
                )
                self.documents[partial.Name] = partial
            raise exc
        if os.path.basename(path).startswith("Existing"):
            doc = FakeDoc("existing", file_name=path)
            self.documents[doc.Name] = doc
            return doc
        for doc in self.documents.values():
            if doc.FileName == path:
                return doc
        raise FileNotFoundError(path)

    def closeDocument(self, name):
        self.calls.append(("closeDocument", name))
        if name not in self.documents:
            raise KeyError(name)
        del self.documents[name]


class _ModifiedHolder:
    def __init__(self, modified: bool):
        self.Modified = modified


class FakeGui:
    def __init__(self, app):
        self._app = app
        self.modified_override: dict[str, bool] = {}

    def getDocument(self, name):
        doc = self._app.documents.get(name)
        if doc is None:
            raise RuntimeError(f"no GUI document '{name}'")
        if name in self.modified_override:
            return _ModifiedHolder(self.modified_override[name])
        return doc


class FakeCtx:
    """Fake server context implementing the agreed contract."""

    def __init__(self, tmp_path):
        self.App = FakeApp()
        self.Gui = FakeGui(self.App)
        self.allowed_root = (tmp_path / "roots" / "home").resolve()
        self.allowed_root.mkdir(parents=True, exist_ok=True)
        self._generations: dict[int, int] = {}
        self.approved_target = None
        self.idle_checks: list[str] = []
        self.busy: str | None = None

    # -- documents ---------------------------------------------------------

    def add_document(self, doc):
        self.App.documents[doc.Name] = doc

    def require_document(self, name):
        doc = self.App.documents.get(name)
        if doc is None:
            raise ToolError("DOCUMENT_NOT_FOUND", f"document '{name}' not found", None)
        return doc

    def document_identity(self, doc):
        return f"identity:{doc.Name}"

    def document_generation(self, doc):
        return self._generations.setdefault(id(doc), 1)

    def check_document_idle(self, doc):
        self.idle_checks.append(doc.Name)
        if self.busy:
            raise ToolError("SERVER_BUSY", f"document busy: {self.busy}", None)

    # -- paths -------------------------------------------------------------

    def canonical_path(self, path):
        real = os.path.realpath(str(path))
        root = str(self.allowed_root)
        if real != root and not real.startswith(root + os.sep):
            raise ToolError(
                PATH_NOT_ALLOWED, f"path '{path}' is outside the allowed roots", None
            )
        return real

    def file_fingerprint(self, path):
        path = str(path)
        try:
            stat = os.stat(path)
        except OSError:
            return None
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        return {
            "size": stat.st_size,
            "digest": digest,
            "mtime_ns": stat.st_mtime_ns,
        }

    def write_file(self, path, content="x"):
        Path(path).write_text(content)
        return str(path)


# ---------------------------------------------------------------------------
# new_document.
# ---------------------------------------------------------------------------


def test_new_document_returns_actual_sanitized_name(tmp_path):
    ctx = FakeCtx(tmp_path)
    payload = documents.HANDLERS["new_document"](ctx, {"name": "My Doc"})
    assert payload["name"] == "My_Doc"
    assert ctx.App.calls == [("newDocument", "My_Doc")]
    assert payload["objectCount"] == 0


def test_new_document_preflight_is_none(tmp_path):
    ctx = FakeCtx(tmp_path)
    assert documents.preflight(ctx, "new_document", {"name": "Smoke"}) is None


# ---------------------------------------------------------------------------
# preflight consent targets (pure: no mutations).
# ---------------------------------------------------------------------------


def test_open_preflight_untrusted_returns_file_target_without_mutation(tmp_path):
    ctx = FakeCtx(tmp_path)
    path = ctx.write_file(ctx.allowed_root / "Thing.FCStd")
    target = documents.preflight(ctx, "open_document", {"path": path})
    assert target["kind"] == "file"
    assert target["purpose"] == "open"
    assert target["requires_consent"] is True
    assert "untrusted" in target["message"]
    assert target["fingerprint"] == ctx.file_fingerprint(path)
    assert ctx.App.calls == []  # preflight never mutates


def test_open_preflight_trusted_flag_skips_consent(tmp_path):
    ctx = FakeCtx(tmp_path)
    path = ctx.write_file(ctx.allowed_root / "Trusted.FCStd")
    target = documents.preflight(
        ctx, "open_document", {"path": path, "untrusted": False}
    )
    assert target is None


def test_open_preflight_rejects_bad_extension_and_missing_file(tmp_path):
    ctx = FakeCtx(tmp_path)
    text = ctx.write_file(ctx.allowed_root / "Thing.txt")
    with pytest.raises(ToolError) as excinfo:
        documents.preflight(ctx, "open_document", {"path": text})
    assert excinfo.value.code == VALIDATION_FAILED

    with pytest.raises(ToolError) as excinfo:
        documents.preflight(
            ctx,
            "open_document",
            {"path": str(ctx.allowed_root / "Nope.FCStd")},
        )
    assert excinfo.value.code == VALIDATION_FAILED


def test_open_preflight_rejects_path_outside_allowed_roots(tmp_path):
    ctx = FakeCtx(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        documents.preflight(ctx, "open_document", {"path": "/tmp/evil.FCStd"})
    assert excinfo.value.code == PATH_NOT_ALLOWED


def test_save_preflight_overwrite_target_but_own_path_and_new_path_none(tmp_path):
    ctx = FakeCtx(tmp_path)
    own = ctx.write_file(ctx.allowed_root / "own.FCStd")
    doc = FakeDoc("doc", file_name=own)
    ctx.add_document(doc)

    other = ctx.write_file(ctx.allowed_root / "other.FCStd")
    target = documents.preflight(
        ctx, "save_document", {"document": "doc", "path": other}
    )
    assert target["kind"] == "file"
    assert target["purpose"] == "overwrite"
    assert target["requires_consent"] is True

    assert documents.preflight(ctx, "save_document", {"document": "doc"}) is None
    # Own current file: normal save, no consent.
    assert (
        documents.preflight(ctx, "save_document", {"document": "doc", "path": own})
        is None
    )
    # Fresh new path: nothing to overwrite.
    fresh = str(ctx.allowed_root / "fresh.FCStd")
    assert (
        documents.preflight(ctx, "save_document", {"document": "doc", "path": fresh})
        is None
    )


def test_save_preflight_rejects_missing_parent_directory(tmp_path):
    ctx = FakeCtx(tmp_path)
    own = ctx.write_file(ctx.allowed_root / "own.FCStd")
    doc = FakeDoc("doc", file_name=own)
    ctx.add_document(doc)
    with pytest.raises(ToolError) as excinfo:
        documents.preflight(
            ctx,
            "save_document",
            {"document": "doc", "path": str(ctx.allowed_root / "nope" / "n.FCStd")},
        )
    assert excinfo.value.code == VALIDATION_FAILED


def test_close_preflight_dirty_consent_and_clean_none(tmp_path):
    ctx = FakeCtx(tmp_path)
    dirty = FakeDoc("dirty", modified=True, objects=[object()])
    clean = FakeDoc(
        "clean", file_name=str(ctx.allowed_root / "c.FCStd"), modified=False
    )
    unsaved_nonempty = FakeDoc("scratch", modified=False, objects=[object()])
    ctx.add_document(dirty)
    ctx.add_document(clean)
    ctx.add_document(unsaved_nonempty)

    target = documents.preflight(ctx, "close_document", {"document": "dirty"})
    assert target["kind"] == "document"
    assert target["identity"] == ctx.document_identity(dirty)
    assert target["generation"] == 1
    assert target["requires_consent"] is True

    assert documents.preflight(ctx, "close_document", {"document": "clean"}) is None
    assert (
        documents.preflight(ctx, "close_document", {"document": "scratch"}) is not None
    )


def test_dirty_state_unknown_is_conservative(tmp_path):
    ctx = FakeCtx(tmp_path)
    unknown = FakeDoc("unknown", objects=[object()], with_modified_attr=False)
    ctx.add_document(unknown)
    target = documents.preflight(ctx, "close_document", {"document": "unknown"})
    assert target is not None and target["kind"] == "document"


def test_gui_modified_conservative_beats_app_false_after_save(tmp_path):
    """App doc reports Modified False but Gui doc still true: prompt anyway."""
    ctx = FakeCtx(tmp_path)
    doc = FakeDoc("stubborn", file_name=str(ctx.allowed_root / "s.FCStd"))
    doc.Modified = False
    ctx.add_document(doc)
    ctx.Gui.modified_override["stubborn"] = True
    assert (
        documents.preflight(ctx, "close_document", {"document": "stubborn"}) is not None
    )


def test_reload_preflight_requires_saved_existing_path(tmp_path):
    ctx = FakeCtx(tmp_path)
    scratch = FakeDoc("scratch", objects=[object()])
    ctx.add_document(scratch)
    with pytest.raises(ToolError) as excinfo:
        documents.preflight(ctx, "reload_document", {"document": "scratch"})
    assert excinfo.value.code == VALIDATION_FAILED

    lost = FakeDoc("lost", file_name=str(ctx.allowed_root / "gone.FCStd"))
    ctx.add_document(lost)
    with pytest.raises(ToolError) as excinfo:
        documents.preflight(ctx, "reload_document", {"document": "lost"})
    assert excinfo.value.code == VALIDATION_FAILED


def test_reload_preflight_dirty_consent_clean_none(tmp_path):
    ctx = FakeCtx(tmp_path)
    dirty_path = ctx.write_file(ctx.allowed_root / "d.FCStd")
    clean_path = ctx.write_file(ctx.allowed_root / "c.FCStd")
    dirty = FakeDoc("dirty", file_name=dirty_path, modified=True)
    clean = FakeDoc("clean", file_name=clean_path, modified=False)
    ctx.add_document(dirty)
    ctx.add_document(clean)
    assert (
        documents.preflight(ctx, "reload_document", {"document": "dirty"}) is not None
    )
    assert documents.preflight(ctx, "reload_document", {"document": "clean"}) is None


def test_preflight_unknown_document_is_document_not_found(tmp_path):
    ctx = FakeCtx(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        documents.preflight(ctx, "close_document", {"document": "ghost"})
    assert excinfo.value.code == "DOCUMENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# open_document.
# ---------------------------------------------------------------------------


def test_open_document_requires_consent_and_does_nothing_without_it(tmp_path):
    ctx = FakeCtx(tmp_path)
    path = ctx.write_file(ctx.allowed_root / "Thing.FCStd")
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["open_document"](ctx, {"path": path})
    assert excinfo.value.code == CONSENT_DENIED
    assert excinfo.value.details["reason"] == "target_changed"
    assert ctx.App.calls == []  # no mutation without an approved target


def test_open_document_with_approved_target_opens_and_reports_identity(tmp_path):
    ctx = FakeCtx(tmp_path)
    path = ctx.write_file(ctx.allowed_root / "Existing.FCStd")
    target = documents.preflight(ctx, "open_document", {"path": path})
    ctx.approved_target = target
    payload = documents.HANDLERS["open_document"](ctx, {"path": path})
    assert payload["name"] == "existing"
    assert payload["path"] == path


def test_open_document_target_changed_between_consent_and_effect(tmp_path):
    ctx = FakeCtx(tmp_path)
    path = ctx.allowed_root / "Thing.FCStd"
    ctx.write_file(path, "v1")
    target = documents.preflight(ctx, "open_document", {"path": str(path)})
    ctx.approved_target = target
    ctx.write_file(path, "v2")  # file changed after consent
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["open_document"](ctx, {"path": str(path)})
    assert excinfo.value.code == CONSENT_DENIED
    assert ctx.App.calls == []


def test_open_document_untrusted_flag_opens_without_consent(tmp_path):
    ctx = FakeCtx(tmp_path)
    path = ctx.write_file(ctx.allowed_root / "ExistingTrusted.FCStd")
    payload = documents.HANDLERS["open_document"](
        ctx, {"path": path, "untrusted": False}
    )
    assert payload["name"] == "existing"


def test_open_document_failure_cleans_only_newly_introduced(tmp_path):
    ctx = FakeCtx(tmp_path)
    keep = FakeDoc("keep", file_name="keep.FCStd", objects=[object()])
    ctx.add_document(keep)
    path = ctx.write_file(ctx.allowed_root / "Bad.FCStd")
    target = documents.preflight(ctx, "open_document", {"path": path})
    ctx.approved_target = target
    ctx.App.fail_open_next = ValueError("corrupt FCStd")
    ctx.App.open_leaves_partial = "bad_partial"
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["open_document"](ctx, {"path": path})
    assert excinfo.value.code == VALIDATION_FAILED
    assert "corrupt FCStd" in excinfo.value.message
    assert excinfo.value.details["removedFailedOpenDocuments"] == ["bad_partial"]
    assert "keep" in ctx.App.documents  # preexisting documents untouched


def test_open_document_missing_file_fails_before_any_mutation(tmp_path):
    ctx = FakeCtx(tmp_path)
    missing = str(ctx.allowed_root / "Missing.FCStd")
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["open_document"](ctx, {"path": missing})
    assert excinfo.value.code == VALIDATION_FAILED
    assert ctx.App.calls == []


# ---------------------------------------------------------------------------
# save_document.
# ---------------------------------------------------------------------------


def test_save_document_without_path_and_without_file_is_actionable_error(tmp_path):
    ctx = FakeCtx(tmp_path)
    doc = FakeDoc("scratch")
    ctx.add_document(doc)
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["save_document"](ctx, {"document": "scratch"})
    assert excinfo.value.code == VALIDATION_FAILED
    assert "never been saved" in excinfo.value.message


def test_save_document_own_path_uses_save_not_saveas(tmp_path):
    ctx = FakeCtx(tmp_path)
    own = ctx.write_file(ctx.allowed_root / "own.FCStd")
    doc = FakeDoc("doc", file_name=own)
    ctx.add_document(doc)
    saved: list[tuple[str, str]] = []
    doc.save = lambda: saved.append(("save", own))

    def _no_save_as(path):
        raise AssertionError("saveAs must not run for an own-path save")

    doc.saveAs = _no_save_as
    payload = documents.HANDLERS["save_document"](ctx, {"document": "doc", "path": own})
    assert saved == [("save", own)]
    assert payload["path"] == own


def test_save_document_new_path_saveas_without_consent(tmp_path):
    ctx = FakeCtx(tmp_path)
    own = ctx.write_file(ctx.allowed_root / "own.FCStd")
    doc = FakeDoc("doc", file_name=own)
    ctx.add_document(doc)
    target = str(ctx.allowed_root / "new.FCStd")
    doc.saveAs = lambda p: setattr(doc, "FileName", p)
    payload = documents.HANDLERS["save_document"](
        ctx, {"document": "doc", "path": target}
    )
    assert payload["path"] == target


def test_save_document_overwrite_requires_approved_target(tmp_path):
    ctx = FakeCtx(tmp_path)
    own = ctx.write_file(ctx.allowed_root / "own.FCStd")
    doc = FakeDoc("doc", file_name=own)
    ctx.add_document(doc)
    other = ctx.write_file(ctx.allowed_root / "other.FCStd")

    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["save_document"](ctx, {"document": "doc", "path": other})
    assert excinfo.value.code == CONSENT_DENIED

    target = documents.preflight(
        ctx, "save_document", {"document": "doc", "path": other}
    )
    ctx.approved_target = target
    doc.saveAs = lambda p: setattr(doc, "FileName", p)
    payload = documents.HANDLERS["save_document"](
        ctx, {"document": "doc", "path": other}
    )
    assert payload["path"] == other


def test_save_document_overwrite_target_changed_after_consent(tmp_path):
    ctx = FakeCtx(tmp_path)
    own = ctx.write_file(ctx.allowed_root / "own.FCStd")
    doc = FakeDoc("doc", file_name=own)
    ctx.add_document(doc)
    other = ctx.allowed_root / "other.FCStd"
    ctx.write_file(other, "v1")
    target = documents.preflight(
        ctx, "save_document", {"document": "doc", "path": str(other)}
    )
    ctx.approved_target = target
    ctx.write_file(other, "v2")

    def _no_save_as(path):
        raise AssertionError("saveAs must not run on a changed target")

    doc.saveAs = _no_save_as
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["save_document"](
            ctx, {"document": "doc", "path": str(other)}
        )
    assert excinfo.value.code == CONSENT_DENIED


def test_save_document_rejects_bad_extension(tmp_path):
    ctx = FakeCtx(tmp_path)
    own = ctx.write_file(ctx.allowed_root / "own.FCStd")
    doc = FakeDoc("doc", file_name=own)
    ctx.add_document(doc)
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["save_document"](
            ctx, {"document": "doc", "path": str(ctx.allowed_root / "new.txt")}
        )
    assert excinfo.value.code == VALIDATION_FAILED


def test_save_document_checks_idle_gate(tmp_path):
    ctx = FakeCtx(tmp_path)
    own = ctx.write_file(ctx.allowed_root / "own.FCStd")
    doc = FakeDoc("doc", file_name=own)
    ctx.add_document(doc)
    doc.save = lambda: None
    ctx.busy = "run_fem"
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["save_document"](ctx, {"document": "doc"})
    assert excinfo.value.code == "SERVER_BUSY"
    ctx.busy = None
    documents.HANDLERS["save_document"](ctx, {"document": "doc"})
    assert ctx.idle_checks == ["doc", "doc"]


# ---------------------------------------------------------------------------
# close_document.
# ---------------------------------------------------------------------------


def test_close_document_clean_closes_without_consent(tmp_path):
    ctx = FakeCtx(tmp_path)
    clean = FakeDoc(
        "clean", file_name=str(ctx.allowed_root / "c.FCStd"), modified=False
    )
    ctx.add_document(clean)
    payload = documents.HANDLERS["close_document"](ctx, {"document": "clean"})
    assert payload == {"document": "clean"}
    assert "clean" not in ctx.App.documents


def test_close_document_dirty_requires_consent(tmp_path):
    ctx = FakeCtx(tmp_path)
    dirty = FakeDoc("dirty", modified=True, objects=[object()])
    ctx.add_document(dirty)
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["close_document"](ctx, {"document": "dirty"})
    assert excinfo.value.code == CONSENT_DENIED
    assert "dirty" in ctx.App.documents  # nothing closed

    target = documents.preflight(ctx, "close_document", {"document": "dirty"})
    ctx.approved_target = target
    documents.HANDLERS["close_document"](ctx, {"document": "dirty"})
    assert "dirty" not in ctx.App.documents


def test_close_document_checks_idle_gate(tmp_path):
    ctx = FakeCtx(tmp_path)
    doc = FakeDoc("clean", file_name=str(ctx.allowed_root / "c.FCStd"), modified=False)
    ctx.add_document(doc)
    ctx.busy = "run_fem"
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["close_document"](ctx, {"document": "clean"})
    assert excinfo.value.code == "SERVER_BUSY"


# ---------------------------------------------------------------------------
# reload_document.
# ---------------------------------------------------------------------------


def test_reload_document_dirty_with_consent_reopens(tmp_path):
    ctx = FakeCtx(tmp_path)
    path = ctx.write_file(ctx.allowed_root / "Existing.FCStd")
    doc = FakeDoc("d", file_name=path, modified=True)
    ctx.add_document(doc)
    target = documents.preflight(ctx, "reload_document", {"document": "d"})
    ctx.approved_target = target
    payload = documents.HANDLERS["reload_document"](ctx, {"document": "d"})
    assert payload["name"] == "existing"  # actual reopened identity
    assert payload["path"] == path
    assert ("closeDocument", "d") in ctx.App.calls


def test_reload_document_dirty_without_consent_refuses(tmp_path):
    ctx = FakeCtx(tmp_path)
    doc = FakeDoc(
        "d", file_name=ctx.write_file(ctx.allowed_root / "d.FCStd"), modified=True
    )
    ctx.add_document(doc)
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["reload_document"](ctx, {"document": "d"})
    assert excinfo.value.code == CONSENT_DENIED
    assert "d" in ctx.App.documents


def test_reload_document_failed_reopen_reports_truthfully(tmp_path):
    ctx = FakeCtx(tmp_path)
    path = ctx.write_file(ctx.allowed_root / "d.FCStd")
    doc = FakeDoc("d", file_name=path, modified=True)
    ctx.add_document(doc)
    target = documents.preflight(ctx, "reload_document", {"document": "d"})
    ctx.approved_target = target
    ctx.App.fail_open_next = OSError("file unreadable")
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["reload_document"](ctx, {"document": "d"})
    assert excinfo.value.code == VALIDATION_FAILED
    message = excinfo.value.message
    assert "closed" in message
    assert "reopening" in message
    assert "rollback" not in message.casefold()
    assert "d" not in ctx.App.documents  # the close really happened


def test_reload_document_unsaved_scratch_is_actionable(tmp_path):
    ctx = FakeCtx(tmp_path)
    doc = FakeDoc("scratch", objects=[object()], modified=False)
    ctx.add_document(doc)
    with pytest.raises(ToolError) as excinfo:
        documents.HANDLERS["reload_document"](ctx, {"document": "scratch"})
    assert excinfo.value.code == VALIDATION_FAILED
    assert ctx.App.calls == []


# ---------------------------------------------------------------------------
# Tool definitions sanity.
# ---------------------------------------------------------------------------


def test_definitions_are_finite_and_bound():
    names = [definition["name"] for definition in documents.TOOL_DEFINITIONS]
    assert names == [
        "new_document",
        "open_document",
        "save_document",
        "close_document",
        "reload_document",
    ]
    assert sorted(documents.HANDLERS) == sorted(names)
    from mcp_server.protocol import check_schema

    for definition in documents.TOOL_DEFINITIONS:
        check_schema(definition["inputSchema"])
        check_schema(definition["outputSchema"])
        assert definition["inputSchema"]["additionalProperties"] is False
        assert definition["outputSchema"]["additionalProperties"] is False
