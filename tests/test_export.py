"""Tests for the ``export`` tool (mcp_server/tools/export.py..

Loads the real module against stubbed FreeCAD/Mesh/Part modules, defending:
staged sibling publication with exclusive no-clobber links, failed readbacks
never replacing an original, consent fingerprint rechecks before overwrite,
the canonical path guard, fcstd option rejection, collective bed alignment,
and temporary-file cleanup.
"""

from contextlib import contextmanager
import dataclasses
import errno
import importlib.util
import os
from pathlib import Path
import sys
import types
from typing import Any, Iterator

import pytest

ADDON_DIR = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"
EXPORT_PATH = ADDON_DIR / "mcp_server" / "tools" / "export.py"
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from mcp_server import protocol
from mcp_server.protocol import ToolError


# ---------------------------------------------------------------------------
# FreeCAD test doubles.
# ---------------------------------------------------------------------------


class FakeVector:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = float(x), float(y), float(z)


class FakeBoundBox:
    def __init__(
        self,
        xmin: float,
        ymin: float,
        zmin: float,
        xmax: float,
        ymax: float,
        zmax: float,
    ) -> None:
        self.XMin, self.YMin, self.ZMin = xmin, ymin, zmin
        self.XMax, self.YMax, self.ZMax = xmax, ymax, zmax

    def shifted(self, dx: float, dy: float, dz: float) -> "FakeBoundBox":
        return FakeBoundBox(
            self.XMin + dx,
            self.YMin + dy,
            self.ZMin + dz,
            self.XMax + dx,
            self.YMax + dy,
            self.ZMax + dz,
        )


class FakePlacement:
    def __init__(self, base: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        self.base = tuple(float(value) for value in base)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, FakePlacement) and self.base == other.base


class FakeShape:
    def __init__(
        self, zmin: float = 0.0, placement: FakePlacement | None = None
    ) -> None:
        self.Placement = placement if placement is not None else FakePlacement()
        self.BoundBox = FakeBoundBox(0.0, 0.0, zmin, 10.0, 10.0, zmin + 10.0)

    def copy(self) -> "FakeShape":
        return FakeShape(zmin=self.BoundBox.ZMin, placement=self.Placement)

    def translate(self, vector: Any) -> None:
        self.BoundBox = self.BoundBox.shifted(vector.x, vector.y, vector.z)

    def isValid(self) -> bool:
        return True


class FakeReadShape:
    """What the stubbed ``Part.read`` returns for a good STEP file."""

    def __init__(self) -> None:
        self.Solids = [object()]
        self.Volume = 1000.0
        self.BoundBox = FakeBoundBox(0.0, 0.0, 0.0, 10.0, 10.0, 10.0)

    def isValid(self) -> bool:
        return True


class FakeMesh:
    instances: list["FakeMesh"] = []

    def __init__(self, *args: Any) -> None:
        self.facets = 0
        self.BoundBox = FakeBoundBox(0.0, 0.0, 0.0, 10.0, 10.0, 10.0)
        self.write_calls: list[tuple[str, str | None]] = []
        if args and isinstance(args[0], (str, bytes, os.PathLike)):
            with open(args[0], "rb") as handle:
                data = handle.read()
            if data.startswith(b"BAD"):
                raise RuntimeError("corrupt mesh file")
            self.facets = int(data.decode().split(":", 1)[1])
        type(self).instances.append(self)

    def addMesh(self, other: "FakeMesh") -> None:
        self.facets += other.facets

    def isSolid(self) -> bool:
        return True

    @property
    def CountFacets(self) -> int:
        return self.facets

    def write(self, path: str, Format: str | None = None) -> None:
        self.write_calls.append((str(path), Format))
        with open(path, "wb") as handle:
            handle.write(
                b"BAD" if hooks.mesh_write_poison else f"FACETS:{self.facets}".encode()
            )


@dataclasses.dataclass
class FakeObject:
    Name: str
    zmin: float = 0.0
    Volume: float = 1000.0
    report_ok: bool = True
    report_error: str | None = None
    Placement: FakePlacement = dataclasses.field(default_factory=FakePlacement)

    def __post_init__(self) -> None:
        self.Shape = FakeShape(zmin=self.zmin, placement=self.Placement)


class FakeSolid:
    def __init__(self, volume: float = 1000.0) -> None:
        self.Volume = volume


class FakeReadShape:
    """What the stubbed ``Part.read`` returns for a good STEP file."""

    def __init__(self) -> None:
        self.Solids = [FakeSolid()]
        self.Volume = 1000.0
        self.BoundBox = FakeBoundBox(0.0, 0.0, 0.0, 10.0, 10.0, 10.0)

    def isValid(self) -> bool:
        return True


class FakeReopenedDocument:
    def __init__(self, objects: dict[str, FakeObject]) -> None:
        self.Name = "ReopenedSmoke"
        self.objects = dict(objects)

    @property
    def Objects(self) -> list[FakeObject]:
        return list(self.objects.values())

    def getObject(self, name: str) -> FakeObject | None:
        return self.objects.get(name)


class FakeDocument:
    def __init__(self, name: str, objects: dict[str, FakeObject]) -> None:
        self.Name = name
        self.objects = dict(objects)
        self.FileName = ""
        self.save_copy_paths: list[str] = []

    @property
    def Objects(self) -> list[FakeObject]:
        return list(self.objects.values())

    def getObject(self, name: str) -> FakeObject | None:
        return self.objects.get(name)

    def saveCopy(self, path: str) -> None:
        self.save_copy_paths.append(str(path))
        with open(path, "wb") as handle:
            handle.write(b"FCSTD-SAVECOPY")


class FakeCtx:
    def __init__(self, root: str) -> None:
        self.root = str(root)
        self.docs: dict[str, FakeDocument] = {}
        self.approved_target: dict[str, Any] | None = None
        self.requested_objects: list[str] = []

    def add_document(self, doc: FakeDocument) -> FakeDocument:
        self.docs[doc.Name] = doc
        return doc

    def require_document(self, name: str) -> FakeDocument:
        doc = self.docs.get(str(name))
        if doc is None:
            raise ToolError("DOCUMENT_NOT_FOUND", f"unknown document '{name}'", {})
        return doc

    def require_object(self, doc: Any, name: str) -> FakeObject:
        self.requested_objects.append(str(name))
        obj = doc.objects.get(str(name))
        if obj is None:
            raise ToolError(
                "OBJECT_NOT_FOUND", f"unknown object '{name}'", {"object": name}
            )
        return obj

    def check_document_idle(self, doc: Any) -> None:
        pass

    def canonical_path(self, path: Any) -> str:
        resolved = os.path.realpath(str(path))
        if resolved != self.root and not resolved.startswith(self.root + os.sep):
            raise ToolError(
                "PATH_NOT_ALLOWED",
                f"path outside allowed roots: {path}",
                {"path": str(path)},
            )
        return resolved

    def file_fingerprint(self, path: Any) -> dict[str, int] | None:
        try:
            stats = os.stat(path)
        except FileNotFoundError:
            return None
        return {
            "size": stats.st_size,
            "mtime_ns": stats.st_mtime_ns,
            "inode": stats.st_ino,
        }


# ---------------------------------------------------------------------------
# Module loading with stubs.
# ---------------------------------------------------------------------------


class _Hooks:
    def __init__(self) -> None:
        self.mesh_write_poison = False
        self.step_write_poison = False
        self.mesh_shapes: list[float] = []
        self.mesh_from_shape_calls: list[dict[str, Any]] = []
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.open_document_result: FakeReopenedDocument | None = None
        self.open_document_error: Exception | None = None

    def open_document(self, path: str, hidden: bool = False) -> FakeReopenedDocument:
        self.opened.append(str(path))
        if self.open_document_error is not None:
            raise self.open_document_error
        assert self.open_document_result is not None
        return self.open_document_result


hooks = _Hooks()


def _fake_geometry_report(
    obj: Any, expected_solids: int | None = None
) -> dict[str, Any]:
    if not getattr(obj, "report_ok", True):
        return {
            "ok": False,
            "error": obj.report_error or "object failed validation",
            "name": obj.Name,
            "volume": obj.Volume,
            "solid_count": 0,
        }
    return {
        "ok": True,
        "error": None,
        "name": obj.Name,
        "volume": obj.Volume,
        "solid_count": 1,
    }


@contextmanager
def load_export_module() -> Iterator[types.ModuleType]:
    """Load mcp_server/tools/export.py.against stubbed FreeCAD/Mesh/Part modules."""

    module_names = [
        "FreeCAD",
        "Mesh",
        "MeshPart",
        "Part",
        "mcp_server.object_validation",
        "mcp_server.tools.export",
    ]
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in module_names}

    for name in module_names[:4]:
        sys.modules.pop(name, None)
    sys.modules.pop("mcp_server.object_validation", None)
    sys.modules.pop("mcp_server.tools.export", None)
    if "mcp_server.tools" not in sys.modules:
        import mcp_server.tools  # noqa: F401  (real docstring-only package)

    hooks.__init__()
    FakeMesh.instances.clear()

    freecad = types.ModuleType("FreeCAD")
    freecad.Vector = FakeVector
    freecad.openDocument = lambda path, hidden=False: hooks.open_document(path, hidden)
    freecad.closeDocument = lambda name: hooks.closed.append(str(name))

    mesh = types.ModuleType("Mesh")
    mesh.Mesh = FakeMesh

    mesh_part = types.ModuleType("MeshPart")

    def fake_mesh_from_shape(**kwargs: Any) -> FakeMesh:
        hooks.mesh_from_shape_calls.append(kwargs)
        hooks.mesh_shapes.append(kwargs["Shape"].BoundBox.ZMin)
        mesh = FakeMesh()
        mesh.facets = 42
        return mesh

    mesh_part.meshFromShape = fake_mesh_from_shape

    part = types.ModuleType("Part")

    class FakeCompound:
        def __init__(self, shapes: list[FakeShape]) -> None:
            self.shapes = list(shapes)

        def exportStep(self, path: str) -> None:
            with open(path, "wb") as handle:
                handle.write(b"BAD-STEP" if hooks.step_write_poison else b"STEP-OK")

    def fake_read(path: str) -> FakeReadShape:
        with open(path, "rb") as handle:
            data = handle.read()
        if data.startswith(b"BAD"):
            raise RuntimeError("corrupt step file")
        return FakeReadShape()

    part.Compound = FakeCompound
    part.read = fake_read

    object_validation = types.ModuleType("mcp_server.object_validation")
    object_validation.geometry_report = _fake_geometry_report
    object_validation.object_validity_error = lambda obj: None

    sys.modules["FreeCAD"] = freecad
    sys.modules["Mesh"] = mesh
    sys.modules["MeshPart"] = mesh_part
    sys.modules["Part"] = part
    sys.modules["mcp_server.object_validation"] = object_validation

    module_name = "mcp_server.tools.export"
    try:
        spec = importlib.util.spec_from_file_location(module_name, EXPORT_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load export tool from {EXPORT_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        module.hooks = hooks
        yield module
    finally:
        sys.modules.pop(module_name, None)
        for name, value in saved.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture()
def export_module():
    with load_export_module() as module:
        yield module


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def make_box_document(
    ctx: FakeCtx, zmin_one: float = 0.0, zmin_two: float = 0.0
) -> FakeDocument:
    doc = FakeDocument(
        "Smoke",
        {
            "Box1": FakeObject(Name="Box1", zmin=zmin_one),
            "Box2": FakeObject(Name="Box2", zmin=zmin_two),
        },
    )
    doc.FileName = os.path.join(ctx.root, "smoke.FCStd")
    return ctx.add_document(doc)


def staged_leftovers(directory: str) -> list[str]:
    return [name for name in os.listdir(directory) if name.startswith(".mcp-export-")]


# ---------------------------------------------------------------------------
# Mesh export.
# ---------------------------------------------------------------------------


def test_stl_export_publishes_new_file(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "out.stl")

    result = export_module.export(
        ctx,
        {
            "document": "Smoke",
            "objects": ["Box1", "Box2"],
            "format": "stl",
            "path": destination,
        },
    )

    assert result["format"] == "stl"
    assert result["path"] == destination
    assert result["objects"] == ["Box1", "Box2"]
    assert result["mesh"] == {
        "isSolid": True,
        "countFacets": 84,
        "bounds": [0.0, 0.0, 0.0, 10.0, 10.0, 10.0],
    }
    with open(destination, "rb") as handle:
        assert handle.read() == b"FACETS:84"
    assert staged_leftovers(ctx.root) == []
    # Server-applied defaults.
    assert export_module.hooks.mesh_from_shape_calls[-2]["LinearDeflection"] == 0.03
    assert export_module.hooks.mesh_from_shape_calls[-2]["AngularDeflection"] == 0.12
    assert export_module.hooks.mesh_from_shape_calls[-2]["Relative"] is False


def test_3mf_write_uses_3mf_format(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "out.3mf")

    result = export_module.export(
        ctx,
        {
            "document": "Smoke",
            "objects": ["Box1"],
            "format": "3mf",
            "path": destination,
        },
    )

    assert result["mesh"]["countFacets"] == 42
    combined = next(mesh for mesh in FakeMesh.instances if mesh.write_calls)
    staged, written_format = combined.write_calls[-1]
    assert written_format == "3MF"
    assert staged.startswith(os.path.dirname(destination))
    assert staged.endswith(".3mf")
    with open(destination, "rb") as handle:
        assert handle.read() == b"FACETS:42"
    assert staged_leftovers(ctx.root) == []


def test_bed_align_uses_one_collective_translation(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx, zmin_one=5.0, zmin_two=-3.0)
    destination = os.path.join(ctx.root, "aligned.stl")

    export_module.export(
        ctx,
        {
            "document": "Smoke",
            "objects": ["Box1", "Box2"],
            "format": "stl",
            "path": destination,
            "bed_align": True,
        },
    )

    assert export_module.hooks.mesh_shapes == [8.0, 0.0]
    os.remove(destination)
    export_module.hooks.mesh_shapes.clear()
    export_module.export(
        ctx,
        {
            "document": "Smoke",
            "objects": ["Box1", "Box2"],
            "format": "stl",
            "path": destination,
            "bed_align": False,
        },
    )
    assert export_module.hooks.mesh_shapes == [5.0, -3.0]


def test_failed_mesh_readback_never_replaces_original(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "keep.stl")
    with open(destination, "wb") as handle:
        handle.write(b"ORIGINAL")

    export_module.hooks.mesh_write_poison = True
    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": ["Box1"],
                "format": "stl",
                "path": destination,
            },
        )

    assert excinfo.value.code == "VALIDATION_FAILED"
    with open(destination, "rb") as handle:
        assert handle.read() == b"ORIGINAL"
    assert staged_leftovers(ctx.root) == []


def test_failed_step_readback_never_replaces_original(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "keep.step")
    with open(destination, "wb") as handle:
        handle.write(b"ORIGINAL-STEP")

    export_module.hooks.step_write_poison = True
    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": ["Box1"],
                "format": "step",
                "path": destination,
            },
        )

    assert excinfo.value.code == "VALIDATION_FAILED"
    with open(destination, "rb") as handle:
        assert handle.read() == b"ORIGINAL-STEP"
    assert staged_leftovers(ctx.root) == []


def test_step_export_publishes_and_returns_readback(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "out.step")

    result = export_module.export(
        ctx,
        {
            "document": "Smoke",
            "objects": ["Box1"],
            "format": "step",
            "path": destination,
        },
    )

    assert result["step"] == {
        "isValid": True,
        "solidCount": 1,
        "volume": 1000.0,
        "bounds": [0.0, 0.0, 0.0, 10.0, 10.0, 10.0],
    }
    with open(destination, "rb") as handle:
        assert handle.read() == b"STEP-OK"


def test_new_file_race_requires_fresh_consent(
    export_module, tmp_path, monkeypatch
) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "raced.stl")

    def collide(source: str, destination_name: str) -> None:
        raise FileExistsError(errno.EEXIST, "file exists", destination_name)

    monkeypatch.setattr(os, "link", collide)

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": ["Box1"],
                "format": "stl",
                "path": destination,
            },
        )

    assert excinfo.value.code == "CONSENT_DENIED"
    assert excinfo.value.details["reason"] == "target_changed"
    assert not os.path.exists(destination)
    assert staged_leftovers(ctx.root) == []


def test_hardlink_unsupported_fails_without_overwrite(
    export_module, tmp_path, monkeypatch
) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "nolink.stl")

    def refuse(source: str, destination_name: str) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", refuse)

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": ["Box1"],
                "format": "stl",
                "path": destination,
            },
        )

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert "hard link" in excinfo.value.message
    assert not os.path.exists(destination)
    assert staged_leftovers(ctx.root) == []


def test_approved_overwrite_uses_replace(export_module, tmp_path, monkeypatch) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "existing.stl")
    with open(destination, "wb") as handle:
        handle.write(b"OLD")
    ctx.approved_target = {
        "kind": "file",
        "path": destination,
        "fingerprint": ctx.file_fingerprint(destination),
        "purpose": "overwrite",
    }

    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(source: str, destination_name: str) -> None:
        replaced.append((source, destination_name))
        real_replace(source, destination_name)

    monkeypatch.setattr(os, "replace", spy)

    export_module.export(
        ctx,
        {
            "document": "Smoke",
            "objects": ["Box1"],
            "format": "stl",
            "path": destination,
        },
    )

    assert replaced and replaced[0][1] == destination
    with open(destination, "rb") as handle:
        assert handle.read() == b"FACETS:42"


def test_changed_target_fingerprint_asks_for_retry(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "changed.stl")
    with open(destination, "wb") as handle:
        handle.write(b"OLD")
    ctx.approved_target = {
        "kind": "file",
        "path": destination,
        "fingerprint": ctx.file_fingerprint(destination),
        "purpose": "overwrite",
    }
    with open(destination, "wb") as handle:
        handle.write(b"TAMPERED")

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": ["Box1"],
                "format": "stl",
                "path": destination,
            },
        )

    assert excinfo.value.code == "CONSENT_DENIED"
    assert excinfo.value.details["reason"] == "target_changed"
    with open(destination, "rb") as handle:
        assert handle.read() == b"TAMPERED"
    assert staged_leftovers(ctx.root) == []


def test_existing_destination_without_consent_requires_fresh_consent(
    export_module, tmp_path
) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "unapproved.stl")
    with open(destination, "wb") as handle:
        handle.write(b"OLD")

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": ["Box1"],
                "format": "stl",
                "path": destination,
            },
        )

    assert excinfo.value.code == "CONSENT_DENIED"
    with open(destination, "rb") as handle:
        assert handle.read() == b"OLD"


def test_path_outside_allowed_roots_is_rejected(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = "/etc/freecad-out.stl"

    with pytest.raises(ToolError) as excinfo:
        export_module.preflight(
            ctx,
            "export",
            {
                "document": "Smoke",
                "objects": ["Box1"],
                "format": "stl",
                "path": destination,
            },
        )
    assert excinfo.value.code == "PATH_NOT_ALLOWED"

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": ["Box1"],
                "format": "stl",
                "path": destination,
            },
        )
    assert excinfo.value.code == "PATH_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# FCStd export.
# ---------------------------------------------------------------------------


def test_fcstd_rejects_objects_and_meshing_options(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "doc.FCStd")
    for arguments in (
        {
            "document": "Smoke",
            "objects": ["Box1"],
            "format": "fcstd",
            "path": destination,
        },
        {
            "document": "Smoke",
            "objects": [],
            "format": "fcstd",
            "path": destination,
            "bed_align": True,
        },
        {
            "document": "Smoke",
            "objects": [],
            "format": "fcstd",
            "path": destination,
            "linear_deflection": 0.5,
        },
        {
            "document": "Smoke",
            "objects": [],
            "format": "fcstd",
            "path": destination,
            "angular_deflection": 0.5,
        },
    ):
        with pytest.raises(ToolError) as excinfo:
            export_module.export(ctx, arguments)
        assert excinfo.value.code == "VALIDATION_FAILED"
    assert not os.path.exists(destination)


def test_fcstd_export_publishes_verified_copy(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    doc = make_box_document(ctx)
    destination = os.path.join(ctx.root, "copy.FCStd")
    export_module.hooks.open_document_result = FakeReopenedDocument(doc.objects)

    result = export_module.export(
        ctx,
        {
            "document": "Smoke",
            "objects": [],
            "format": "fcstd",
            "path": destination,
        },
    )

    assert result == {
        "format": "fcstd",
        "path": destination,
        "objects": [],
        "objectCount": 2,
    }
    with open(destination, "rb") as handle:
        assert handle.read() == b"FCSTD-SAVECOPY"
    # saveCopy retained the document's save identity and dirty state.
    assert doc.save_copy_paths == export_module.hooks.opened
    assert doc.FileName == os.path.join(ctx.root, "smoke.FCStd")
    # Only the temporary reopened document was closed.
    assert export_module.hooks.opened == [doc.save_copy_paths[0]]
    assert export_module.hooks.closed == ["ReopenedSmoke"]
    assert staged_leftovers(ctx.root) == []


def test_fcstd_readback_mismatch_never_publishes(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    doc = make_box_document(ctx)
    destination = os.path.join(ctx.root, "copy.FCStd")
    reopened = {
        "Box1": doc.objects["Box1"],
        "Renamed": FakeObject(Name="Renamed"),
    }
    export_module.hooks.open_document_result = FakeReopenedDocument(reopened)

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": [],
                "format": "fcstd",
                "path": destination,
            },
        )

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert "Box2" in excinfo.value.message
    assert not os.path.exists(destination)
    assert export_module.hooks.closed == ["ReopenedSmoke"]
    assert staged_leftovers(ctx.root) == []


def test_fcstd_reopen_failure_never_publishes(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    doc = make_box_document(ctx)
    destination = os.path.join(ctx.root, "copy.FCStd")
    export_module.hooks.open_document_error = RuntimeError("cannot reopen")

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": [],
                "format": "fcstd",
                "path": destination,
            },
        )

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert not os.path.exists(destination)
    assert staged_leftovers(ctx.root) == []


def test_fcstd_placement_mismatch_is_rejected(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    doc = make_box_document(ctx)
    destination = os.path.join(ctx.root, "copy.FCStd")
    moved = {
        "Box1": doc.objects["Box1"],
        "Box2": FakeObject(
            Name="Box2", zmin=0.0, Placement=FakePlacement((5.0, 0.0, 0.0))
        ),
    }
    export_module.hooks.open_document_result = FakeReopenedDocument(moved)

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": [],
                "format": "fcstd",
                "path": destination,
            },
        )

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert "Box2" in excinfo.value.message
    assert not os.path.exists(destination)


# ---------------------------------------------------------------------------
# Validation and preflight.
# ---------------------------------------------------------------------------


def test_unsolid_object_is_rejected(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    doc = FakeDocument(
        "Smoke",
        {
            "Broken": FakeObject(
                Name="Broken", report_ok=False, report_error="Shape is invalid"
            ),
        },
    )
    ctx.add_document(doc)
    destination = os.path.join(ctx.root, "out.stl")

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": ["Broken"],
                "format": "stl",
                "path": destination,
            },
        )

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert "Shape is invalid" in excinfo.value.message
    assert not os.path.exists(destination)


def test_zero_volume_object_is_rejected(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    doc = FakeDocument("Smoke", {"Flat": FakeObject(Name="Flat", Volume=0.0)})
    ctx.add_document(doc)
    destination = os.path.join(ctx.root, "out.step")

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": ["Flat"],
                "format": "step",
                "path": destination,
            },
        )

    assert excinfo.value.code == "VALIDATION_FAILED"
    assert "positive volume" in excinfo.value.message


def test_mesh_format_requires_objects(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "out.stl")

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {"document": "Smoke", "objects": [], "format": "stl", "path": destination},
        )

    assert excinfo.value.code == "VALIDATION_FAILED"


def test_unknown_document_and_object_are_tool_errors(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    destination = os.path.join(ctx.root, "out.stl")

    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Missing",
                "objects": ["Box1"],
                "format": "stl",
                "path": destination,
            },
        )
    assert excinfo.value.code == "DOCUMENT_NOT_FOUND"

    make_box_document(ctx)
    with pytest.raises(ToolError) as excinfo:
        export_module.export(
            ctx,
            {
                "document": "Smoke",
                "objects": ["Ghost"],
                "format": "stl",
                "path": destination,
            },
        )
    assert excinfo.value.code == "OBJECT_NOT_FOUND"


def test_preflight_returns_target_for_existing_destination(
    export_module, tmp_path
) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    destination = os.path.join(ctx.root, "existing.stl")
    with open(destination, "wb") as handle:
        handle.write(b"OLD")

    target = export_module.preflight(
        ctx,
        "export",
        {
            "document": "Smoke",
            "objects": ["Box1"],
            "format": "stl",
            "path": destination,
        },
    )

    assert target is not None
    assert target["kind"] == "file"
    assert target["path"] == destination
    assert target["purpose"] == "overwrite"
    assert target["fingerprint"] == ctx.file_fingerprint(destination)


def test_preflight_returns_none_for_new_destination(export_module, tmp_path) -> None:
    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)

    assert (
        export_module.preflight(
            ctx,
            "export",
            {
                "document": "Smoke",
                "objects": ["Box1"],
                "format": "stl",
                "path": os.path.join(ctx.root, "new.stl"),
            },
        )
        is None
    )

    with pytest.raises(ToolError) as excinfo:
        export_module.preflight(
            ctx,
            "export",
            {
                "document": "Missing",
                "objects": [],
                "format": "fcstd",
                "path": os.path.join(ctx.root, "new.FCStd"),
            },
        )
    assert excinfo.value.code == "DOCUMENT_NOT_FOUND"


def test_tool_schemas_are_finite_and_outputs_validate(export_module, tmp_path) -> None:
    (definition,) = export_module.TOOL_DEFINITIONS
    assert definition["name"] == "export"
    protocol.check_schema(definition["inputSchema"])
    protocol.check_schema(definition["outputSchema"])

    ctx = FakeCtx(str(tmp_path))
    make_box_document(ctx)
    payload = export_module.export(
        ctx,
        {
            "document": "Smoke",
            "objects": ["Box1", "Box2"],
            "format": "stl",
            "path": os.path.join(ctx.root, "checked.stl"),
        },
    )
    protocol.validate_schema(payload, definition["outputSchema"])
