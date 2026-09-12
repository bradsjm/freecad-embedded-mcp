"""Verified recovery checkpoints before expensive feature work.

A checkpoint is a verified FCStd copy of the document written into the
configured recovery directory. It reuses the native ``saveCopy`` path and
the same readback verification the ``export`` tool applies, publishes the
copy through the shared no-clobber publish helper, and never prunes or
deletes previous checkpoints.

A checkpoint failure refuses the impending mutation before its transaction
opens, with reason ``checkpoint_failed`` and nextAction
``inspect_recovery_directory``. Every failure path removes only the staging
file this call created; an existing file at the destination is never
touched.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from ..protocol import VALIDATION_FAILED, ToolError

_SUFFIX_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_NAME = 64


def checkpoint_before_mutation(ctx: Any, doc: Any, label: str) -> dict:
    """Create, verify and publish one recovery copy of ``doc``.

    Returns ``{path, document, generation}`` after successful publication.
    Raises :class:`ToolError` with ``reason: checkpoint_failed`` when the
    copy cannot be created or verified; the caller must refuse the mutation
    rather than continue unverified.
    """

    document_name = str(getattr(doc, "Name", ""))
    generation = int(ctx.document_generation(doc))
    directory = _require_recovery_directory(ctx)
    destination = os.path.join(directory, _checkpoint_file_name(document_name, generation))
    # Native ``saveCopy`` applies FreeCAD's extension handling, so the stage
    # keeps the ``.FCStd`` suffix exactly like the export path it mirrors.
    staged = os.path.join(
        directory,
        f".{os.path.splitext(os.path.basename(destination))[0]}.{uuid.uuid4().hex}.FCStd",
    )
    # Reserve the stage name exclusively: a colliding file must never be
    # overwritten by saveCopy, and cleanup may only remove what this call
    # created. The stage's device/inode identity is captured now so the
    # final cleanup can prove it still owns the path it removes.
    created_identity: tuple[int, int] | None = None
    try:
        staged_fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            status = os.fstat(staged_fd)
            created_identity = (status.st_dev, status.st_ino)
        finally:
            os.close(staged_fd)
    except FileExistsError as exc:
        raise _checkpoint_failed(
            "a recovery stage file already exists with the generated name",
            {"path": staged},
        ) from exc
    except OSError as exc:
        raise _checkpoint_failed(
            f"recovery stage could not be reserved: {type(exc).__name__}: {exc}",
            {"path": staged},
        ) from exc
    original_file_name = str(getattr(doc, "FileName", "") or "")
    original_label = str(getattr(doc, "Label", "") or "")
    original_modified = getattr(doc, "Modified", None)
    try:
        try:
            doc.saveCopy(staged)
        except Exception as exc:
            raise _checkpoint_failed(
                f"recovery saveCopy failed: {type(exc).__name__}: {exc}",
                {"path": destination},
            ) from exc
        if str(getattr(doc, "FileName", "") or "") != original_file_name:
            raise _checkpoint_failed(
                "recovery saveCopy changed the document save identity",
                {"path": destination},
            )
        if str(getattr(doc, "Label", "") or "") != original_label:
            raise _checkpoint_failed(
                "recovery saveCopy changed the document label",
                {"path": destination},
            )
        if (
            isinstance(original_modified, bool)
            and getattr(doc, "Modified", None) != original_modified
        ):
            # The copy must not silently change the source's unsaved state;
            # an unproven restore fails the checkpoint instead.
            raise _checkpoint_failed(
                "recovery saveCopy changed the document's unsaved state",
                {"path": destination},
            )
        _verify_staged_copy(doc, staged, destination)
        # The copy is only a valid pre-operation snapshot if the source did
        # not change while it was written and verified; a changed generation
        # means the caller must stop rather than mutate from an unknown state.
        current_generation = int(ctx.document_generation(doc))
        if current_generation != generation:
            raise _checkpoint_failed(
                "document changed while the recovery copy was written",
                {
                    "path": destination,
                    "expectedGeneration": generation,
                    "actualGeneration": current_generation,
                },
            )
        _publish(ctx, staged, destination)
    finally:
        _remove_created(staged, created_identity)
    return {
        "path": destination,
        "document": document_name,
        "generation": generation,
    }


def _verify_staged_copy(doc: Any, staged: str, destination: str) -> None:
    """Reopen the staged copy and compare it with the live document."""

    import FreeCAD

    try:
        reopened = FreeCAD.openDocument(staged, True)
    except Exception as exc:
        raise _checkpoint_failed(
            f"recovery readback failed: {type(exc).__name__}: {exc}",
            {"path": destination},
        ) from exc
    try:
        from .export import _verify_reopened_copy

        _verify_reopened_copy(doc, reopened)
    except ToolError as exc:
        raise _checkpoint_failed(
            f"recovery readback failed: {exc.message}",
            {"path": destination},
        ) from exc
    except Exception as exc:
        raise _checkpoint_failed(
            f"recovery readback failed: {type(exc).__name__}: {exc}",
            {"path": destination},
        ) from exc
    finally:
        try:
            FreeCAD.closeDocument(str(reopened.Name))
        except Exception as exc:
            raise _checkpoint_failed(
                f"recovery readback close failed: {type(exc).__name__}: {exc}",
                {"path": destination},
            ) from exc


def _publish(ctx: Any, staged: str, destination: str) -> None:
    """Publish the staged copy without ever overwriting an existing file."""

    from .export import publish

    try:
        publish(ctx, staged, destination)
    except ToolError as exc:
        raise _checkpoint_failed(
            f"recovery publish failed: {exc.message}",
            {"path": destination},
        ) from exc
    except Exception as exc:
        raise _checkpoint_failed(
            f"recovery publish failed: {type(exc).__name__}: {exc}",
            {"path": destination},
        ) from exc


def _checkpoint_file_name(document_name: str, generation: int) -> str:
    """Return the bounded `<document>-<generation>-<uuid>.FCStd` name."""

    sanitized = _SUFFIX_PATTERN.sub("-", document_name)[:_MAX_NAME].strip("-")
    if not sanitized:
        sanitized = "document"
    return f"{sanitized}-{generation}-{uuid.uuid4().hex}.FCStd"


def _require_recovery_directory(ctx: Any) -> str:
    """Return the configured recovery directory, which is implicitly allowed."""

    settings = getattr(ctx, "settings", None) or {}
    directory = str(settings.get("recovery_directory", "") or "")
    if not directory.strip():
        raise _checkpoint_failed(
            "recovery is enabled but recovery_directory is empty",
            {},
        )
    expanded = os.path.expanduser(directory)
    if not os.path.isabs(expanded):
        # A relative value would resolve against the process working
        # directory, so it must never become a checkpoint destination.
        raise _checkpoint_failed(
            "recovery_directory must be an absolute path",
            {"path": directory},
        )
    real = os.path.realpath(expanded)
    if not os.path.isdir(real):
        raise _checkpoint_failed(
            f"recovery_directory does not exist or is not a directory: {directory}",
            {"path": real},
        )
    return real


def _checkpoint_failed(message: str, details: dict) -> ToolError:
    enriched = dict(details)
    enriched.update(
        {
            "reason": "checkpoint_failed",
            "nextAction": "inspect_recovery_directory",
        }
    )
    return ToolError(VALIDATION_FAILED, message, enriched)


def _remove_created(path: str, identity: tuple[int, int] | None) -> None:
    """Remove ``path`` only when this checkpoint created and still owns it."""

    if identity is None:
        return
    try:
        current = os.stat(path)
    except OSError:
        return
    if (current.st_dev, current.st_ino) != identity:
        # The path was replaced after this checkpoint reserved it: the
        # replacement is not ours to remove, and unlinking it would destroy
        # a file another writer created at that path.
        return
    try:
        os.unlink(path)
    except OSError:
        pass
