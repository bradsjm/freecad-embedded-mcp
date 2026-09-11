"""Focused tests for mcp_server/tools/import_model.py (import_model).

Runs headless: Import, Mesh and the document doubles are isolated stubs.
The consent preflight and the shared mutation gate run for real.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
IMPORT_PATH = ADDON_DIR / "mcp_server" / "tools" / "import_model.py"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server.protocol import (
    CONSENT_DENIED,
    ToolError,
    validate_schema,
)

VALIDATION_FAILED = "VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Stubs and module loader.
# ---------------------------------------------------------------------------

IMPORT_STATE: dict[str, Any] = {"insert_error": None, "created": ()}


class StubMesh:
    def __init__(self, path: str) -> None:
        self.path = path
        self.facets = IMPORT_STATE.get("facets", 12)
        self.Facets = [object()] * self.facets
        box = types.SimpleNamespace(XMin=0.0, YMin=0.0, ZMin=0.0, XMax=10.0, YMax=10.0, ZMax=10.0)
        self.BoundBox = box

    def isValid(self) -> bool:
        return self.facets > 0


_STUB_MESH_MODULE = types.ModuleType("Mesh")
_STUB_MESH_MODULE.Mesh = StubMesh

_STUB_IMPORT = types.ModuleType("Import")


def _insert(path: str, document: str) -> None:
    # probes["object.property_status"]: Import.insert(name, document) is
    # the recorded native signature (insert() demands `name` first).
    if IMPORT_STATE["insert_error"] is not None:
        raise RuntimeError(IMPORT_STATE["insert_error"])
    IMPORT_STATE["insert_calls"] = [
        *IMPORT_STATE.get("insert_calls", []),
        (path, document),
    ]


_STUB_IMPORT.insert = _insert


@contextmanager
def load_import() -> Iterator[types.ModuleType]:
    module_name = f"mcp_server.tools._import_test_{id(object())}"
    saved = {
        name: sys.modules.get(name)
        for name in (
            "Import",
            "Mesh",
            "mcp_server.tools.import_model",
        )
    }
    sys.modules["Import"] = _STUB_IMPORT
    sys.modules["Mesh"] = _STUB_MESH_MODULE
    sys.modules.pop("mcp_server.tools.import_model", None)
    try:
        spec = importlib.util.spec_from_file_location(module_name, IMPORT_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture(autouse=True)
def _reset_state():
    IMPORT_STATE.clear()
    IMPORT_STATE.update({"insert_error": None, "created": ()})
    yield
    IMPORT_STATE.clear()


# ---------------------------------------------------------------------------
# Fake document and ctx.
# ---------------------------------------------------------------------------


class StubShape:
    def __init__(self) -> None:
        box = types.SimpleNamespace(XMin=0.0, YMin=0.0, ZMin=0.0, XMax=10.0, YMax=20.0, ZMax=30.0)
        self.BoundBox = box
        self.Solids = [object()]
        self.Volume = 1000.0

    def isValid(self) -> bool:
        return True

    def copy(self) -> StubShape:
        return self

    def check(self) -> list[str]:
        return []

    def getTolerance(self, _: int) -> float:
        return 1e-7


class StubObject:
    def __init__(self, name: str, type_id: str, *, shape: Any = None, mesh: Any = None) -> None:
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self.State: list[str] = []
        self.InList: list[Any] = []
        self.Shape = shape
        self.Mesh = mesh
        self.Placement = types.SimpleNamespace()

    def isValid(self) -> bool:
        return True

    def getStatusString(self) -> str:
        return ""

    def getGlobalPlacement(self) -> Any:
        return self.Placement


class FakeApp:
    def getActiveTransaction(self) -> None:
        return None


class FakeDoc:
    def __init__(self, path: str, objects: list[StubObject] | None = None) -> None:
        self.Name = "Doc"
        self.Objects: list[StubObject] = list(objects or [])
        self.UndoMode = 0
        self.HasPendingTransaction = False
        self.transactions: list[tuple] = []
        self.recompute_count = 0
        self.removed: list[str] = []
        self.path = path

    def addObject(self, type_id: str, name: str) -> StubObject:
        obj = StubObject(name, type_id)
        self.Objects.append(obj)
        return obj

    def removeObject(self, name: str) -> None:
        self.removed.append(name)
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self) -> None:
        self.recompute_count += 1

    def openTransaction(self, label: str) -> None:
        self.transactions.append(("open", label))

    def commitTransaction(self) -> None:
        self.transactions.append(("commit",))

    def abortTransaction(self) -> None:
        self.transactions.append(("abort",))


class FakeCtx:
    def __init__(self, doc: FakeDoc, *, approved: dict | None = None) -> None:
        self.App = FakeApp()
        self._doc = doc
        self.approved_target = approved

    def document_generation(self, doc: FakeDoc) -> int:
        return 1

    def document_identity(self, doc: FakeDoc) -> str:
        return "identity"

    def require_document(self, name: str) -> FakeDoc:
        if name != self._doc.Name:
            raise ToolError("DOCUMENT_NOT_FOUND", f"unknown document {name!r}")
        return self._doc

    def canonical_path(self, path: Any) -> str:
        if not isinstance(path, str) or not path:
            raise ToolError(VALIDATION_FAILED, "path must be a non-empty string")
        return str(path)

    def file_fingerprint(self, path: Any) -> dict | None:
        return {"size": 10, "mtime_ns": 1}

    def check_document_idle(self, doc: FakeDoc) -> None:
        pass


def call(module: types.ModuleType, ctx: FakeCtx, **arguments: Any):
    arguments.setdefault("document", "Doc")
    return module.HANDLERS["import_model"](ctx, arguments)


def consent_target(module: types.ModuleType, ctx: FakeCtx, path: str, fmt: str) -> dict:
    return module.preflight(ctx, "import_model", {"document": "Doc", "path": path, "format": fmt})


# ---------------------------------------------------------------------------
# Registration and preflight.
# ---------------------------------------------------------------------------


def test_preflight_targets_the_file_with_import_purpose(tmp_path) -> None:
    with load_import() as module:
        path = tmp_path / "part.step"
        path.write_text("ISO-10303-21")
        doc = FakeDoc(str(path))
        ctx = FakeCtx(doc)

        target = consent_target(module, ctx, str(path), "step")

        assert target["kind"] == "file"
        assert target["purpose"] == "import"
        assert target["requires_consent"] is True
        assert target["path"] == str(path)


def test_preflight_rejects_a_missing_file(tmp_path) -> None:
    with load_import() as module:
        ctx = FakeCtx(FakeDoc(str(tmp_path / "missing.step")))

        with pytest.raises(ToolError) as excinfo:
            consent_target(module, ctx, str(tmp_path / "missing.step"), "step")

        assert excinfo.value.code == VALIDATION_FAILED


# ---------------------------------------------------------------------------
# STEP import.
# ---------------------------------------------------------------------------


def test_step_import_reports_identity_difference_objects(tmp_path) -> None:
    path = tmp_path / "part.step"
    path.write_text("ISO-10303-21")
    pre_existing = StubObject("Preexisting", "Part::Feature", shape=StubShape())

    with load_import() as module:
        doc = FakeDoc(str(path), objects=[pre_existing])
        ctx = FakeCtx(doc)
        target = consent_target(module, ctx, str(path), "step")

        # Import.insert simulates the native creation of two objects.
        def fake_insert(path: str, document: str) -> None:
            doc.Objects.append(StubObject("Imported1", "Part::Feature", shape=StubShape()))
            doc.Objects.append(StubObject("Imported2", "Part::Feature", shape=StubShape()))

        module._import_step = fake_insert
        ctx.approved_target = target
        result = call(module, ctx, path=str(path), format="step")

    assert result["format"] == "step"
    assert result["units"] == "file_defined"
    assert [row["name"] for row in result["objects"]] == ["Imported1", "Imported2"]
    assert all(row["geometryKind"] == "shape" for row in result["objects"])
    assert all(row["solidCount"] == 1 for row in result["objects"])
    assert result["objects"][0]["bounds"] == [0.0, 0.0, 0.0, 10.0, 20.0, 30.0]
    assert pre_existing in doc.Objects  # pre-existing objects are untouched
    assert doc.removed == []
    assert doc.transactions[-1] == ("commit",)
    definition = next(entry for entry in module.TOOL_DEFINITIONS if entry["name"] == "import_model")
    validate_schema(result, definition["outputSchema"])


def test_step_import_that_creates_nothing_fails_and_rolls_back(tmp_path) -> None:
    path = tmp_path / "empty.step"
    path.write_text("ISO-10303-21")

    with load_import() as module:
        doc = FakeDoc(str(path))
        ctx = FakeCtx(doc)
        ctx.approved_target = consent_target(module, ctx, str(path), "step")

        with pytest.raises(ToolError) as excinfo:
            call(module, ctx, path=str(path), format="step")

    assert excinfo.value.code == VALIDATION_FAILED
    assert "created no objects" in excinfo.value.message
    assert doc.transactions[-1] == ("abort",)


def test_step_import_failure_removes_only_new_objects(tmp_path) -> None:
    path = tmp_path / "bad.step"
    path.write_text("ISO-10303-21")
    pre_existing = StubObject("Same", "Part::Feature", shape=StubShape())

    with load_import() as module:
        doc = FakeDoc(str(path), objects=[pre_existing])
        ctx = FakeCtx(doc)
        ctx.approved_target = consent_target(module, ctx, str(path), "step")

        def failing_insert(path: str, document: str) -> None:
            doc.Objects.append(StubObject("Fresh", "Part::Feature", shape=StubShape()))
            raise RuntimeError("corrupt step file")

        module._import_step = failing_insert
        with pytest.raises(ToolError) as excinfo:
            call(module, ctx, path=str(path), format="step")

    assert "corrupt step file" in excinfo.value.message
    assert doc.removed == ["Fresh"]  # never the pre-existing "Same"
    assert [obj.Name for obj in doc.Objects] == ["Same"]


def test_step_import_accepts_a_multisolid_file(tmp_path) -> None:
    """An imported multi-body STEP defines its own solid topology.

    The gate's default contract accepts one solid and refuses more, which
    made every multi-solid STEP (including one this server exported from two
    objects) unimportable with advice the schema cannot express.
    """

    path = tmp_path / "assembly.step"
    path.write_text("ISO-10303-21")

    with load_import() as module:
        doc = FakeDoc(str(path))
        ctx = FakeCtx(doc)
        ctx.approved_target = consent_target(module, ctx, str(path), "step")

        def fake_insert(path: str, document: str) -> None:
            shape = StubShape()
            shape.Solids = [object(), object()]
            doc.Objects.append(StubObject("Assembly", "Part::Feature", shape=shape))

        module._import_step = fake_insert
        result = call(module, ctx, path=str(path), format="step")

    assert [row["name"] for row in result["objects"]] == ["Assembly"]
    assert result["objects"][0]["solidCount"] == 2
    assert doc.transactions[-1] == ("commit",)


# ---------------------------------------------------------------------------
# STL import.
# ---------------------------------------------------------------------------


def test_stl_import_loads_a_mesh_feature(tmp_path) -> None:
    path = tmp_path / "part.stl"
    path.write_text("solid part")

    with load_import() as module:
        doc = FakeDoc(str(path))
        ctx = FakeCtx(doc)
        ctx.approved_target = consent_target(module, ctx, str(path), "stl")
        result = call(module, ctx, path=str(path), format="stl", name="Mesh001")

    assert result["units"] == "unitless_assumed_mm"
    row = result["objects"][0]
    assert row["name"] == "Mesh001"
    assert row["geometryKind"] == "mesh"
    assert row["meshValid"] is True
    assert row["shapeValid"] is None
    assert row["bounds"] == [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]
    assert doc.transactions[-1] == ("commit",)


def test_stl_import_without_facets_fails_and_rolls_back(tmp_path) -> None:
    path = tmp_path / "empty.stl"
    path.write_text("solid empty")
    IMPORT_STATE["facets"] = 0

    with load_import() as module:
        doc = FakeDoc(str(path))
        ctx = FakeCtx(doc)
        ctx.approved_target = consent_target(module, ctx, str(path), "stl")

        with pytest.raises(ToolError) as excinfo:
            call(module, ctx, path=str(path), format="stl")

    assert "no facets" in excinfo.value.message
    assert doc.transactions[-1] == ("abort",)
    assert doc.removed == ["ImportedMesh"]


# ---------------------------------------------------------------------------
# Consent enforcement.
# ---------------------------------------------------------------------------


def test_denied_consent_performs_no_import(tmp_path) -> None:
    path = tmp_path / "part.step"
    path.write_text("ISO-10303-21")

    with load_import() as module:
        doc = FakeDoc(str(path))
        ctx = FakeCtx(doc)  # no approved target: consent was not granted
        calls: list[tuple] = []
        module._import_step = lambda path, document: calls.append((path, document))

        with pytest.raises(ToolError) as excinfo:
            call(module, ctx, path=str(path), format="step")

    assert excinfo.value.code == CONSENT_DENIED
    assert calls == []
    assert doc.transactions == []


def test_tampered_consent_target_fails_without_effect(tmp_path) -> None:
    path = tmp_path / "part.step"
    path.write_text("ISO-10303-21")

    with load_import() as module:
        doc = FakeDoc(str(path))
        ctx = FakeCtx(doc)
        approved = consent_target(module, ctx, str(path), "step")
        ctx.approved_target = dict(approved, path=str(tmp_path / "other.step"))
        calls: list[tuple] = []
        module._import_step = lambda path, document: calls.append((path, document))

        with pytest.raises(ToolError) as excinfo:
            call(module, ctx, path=str(path), format="step")

    assert excinfo.value.code == CONSENT_DENIED
    assert calls == []
    assert doc.transactions == []
