"""Shared GUI-state snapshot helpers for tools that disturb caller state.

``capture_selection_snapshot``/``restore_selection_snapshot`` are used by
both ``tools/view.py`` (``capture_view``) and ``tools/export.py`` (native
FCStd export): reframing the viewport and opening the hidden verification
copy can wipe the global selection, so the caller's selection — including
face/edge subelement selections that plain object lists lose — is captured
as per-document native ``SelectionObject`` snapshots and restored exactly.

The module deliberately imports neither FreeCAD nor Qt: every helper
operates only on the ``App``/``Gui`` handles carried by the dispatch
``ctx``, so importing it never crosses a GUI or headless import boundary.
"""

from __future__ import annotations

from typing import Any


def capture_selection_snapshot(ctx: Any) -> list[dict[str, Any]]:
    """Subelement-preserving selection snapshot across all documents.

    ``clearSelection`` wipes the selection of every document, so the
    caller's selection — including face/edge subelement selections that
    plain object lists lose — is captured as per-document
    ``SelectionObject`` snapshots and restored exactly.
    """

    snapshots: list[dict[str, Any]] = []
    try:
        documents = list(ctx.App.listDocuments().values())
    except Exception:
        documents = []
    for doc in documents:
        try:
            selection_ex = ctx.Gui.Selection.getSelectionEx(str(doc.Name))
        except Exception:
            continue
        for selected in selection_ex:
            obj = getattr(selected, "Object", None)
            if obj is None:
                continue
            subelements = [str(sub) for sub in (selected.SubElementNames or [])]
            snapshots.append({"object": obj, "subelements": subelements})
    return snapshots


def restore_selection_snapshot(ctx: Any, snapshots: list[dict[str, Any]]) -> None:
    """Restore a snapshot taken by ``capture_selection_snapshot`` exactly.

    The whole selection is cleared first because ``addSelection`` is
    additive; each entry then re-selects its object, re-adding every
    captured subelement individually so face/edge selections survive
    the restore.
    """

    ctx.Gui.Selection.clearSelection()
    for snapshot in snapshots:
        obj = snapshot["object"]
        subelements = snapshot["subelements"]
        if subelements:
            for subelement in subelements:
                ctx.Gui.Selection.addSelection(obj, subelement)
        else:
            ctx.Gui.Selection.addSelection(obj)
