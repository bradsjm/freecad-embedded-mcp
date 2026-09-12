# Export, save, and mesh work

Use this reference for every delivered file: saving the editable source, choosing an output format, tessellating a solid, verifying the written file, and importing or repairing a mesh. Use the `export` tool for STL, STEP, 3MF, and native FCStd output. Use `run_script` only for mesh routes the tool does not cover, and follow [the run_script contract](python-export.md).

Do not instruct a human to use FreeCAD's UI.

## Contents

- [`save_document`](#save_document)
- [`export`](#export)
- [Choose the format](#choose-the-format)
- [Tessellation](#tessellation)
- [Bed alignment](#bed-alignment)
- [Export readback](#export-readback)
- [Export only the intended objects](#export-only-the-intended-objects)
- [Mesh import and repair](#mesh-import-and-repair)
- [Export report](#export-report)
- [Sources](#sources)

## `save_document`

Save the `.FCStd` source before or alongside the exported mesh.

```text
save_document(document=<name>, path="/absolute/path/Final.FCStd")
```

- Without `path`, the document saves to its existing file. An unsaved document without `path` is an actionable error.
- An explicit `path` is a native save-as. Saving over a different existing file requires consent.
- Use `reload_document(document)` only after an external process changed the on-disk file. It closes and reopens the stale in-memory copy; it is not a general undo or refresh command.
- `close_document` discards unsaved changes. Save first when the source must persist.

## `export`

Required arguments: `document`, `objects` (internal names), `format` (`stl`, `step`, `3mf`, `fcstd`), and `path`.

```text
export(document=<name>, objects=["Final"], format="stl", path="/absolute/path/Final.stl",
       linear_deflection=0.03, angular_deflection=0.12, bed_align=true)
```

- `stl`, `step`, and `3mf` require a nonempty `objects` list. `fcstd` serializes the entire native document: pass an empty `objects` list and no mesh or bed options, because `linear_deflection`, `angular_deflection`, and `bed_align` are rejected for it.
- `linear_deflection` and `angular_deflection` apply to `stl` and `3mf` only; `step` accepts the argument but ignores it. Defaults are `0.03` and `0.12`; maximums are `1000.0` and `3.14159`.
- `bed_align` applies to `stl`, `3mf`, and `step`, and defaults off. It applies one collective translation that moves the minimum Z of all copies to zero.
- An absolute path inside an allowed root is required. A path outside `allowed_roots` fails with `PATH_NOT_ALLOWED`.
- The tool writes to a temporary sibling file, verifies the result by readback, then publishes. Overwriting an existing destination requires consent.
- `export` may detach as a task under the Tasks extension. Poll with `tasks/get` until terminal.

## Choose the format

| Format | Use | Notes |
|---|---|---|
| `step` | Precise CAD interchange | Prefer STEP when the downstream workflow accepts it. Readback reports validity, solid count, volume, and bounds. |
| `stl` | Mesh for printing or mesh tooling | Simple triangle mesh with no unit metadata. Treat the model as millimetres before export. |
| `3mf` | Mesh with container metadata | The tool writes it through the registered exporter, so the result is a generic 3MF without application-specific settings. |
| `fcstd` | Editable native copy | Serializes the whole document through `saveCopy`. The tool verifies the reopened copy's identities, count, and placements internally, then reports `objectCount`. Use it to mirror the source, not to replace `save_document`. |

OBJ is not an `export` format; export STL or 3MF instead. Use OBJ only when the downstream workflow requires it, through a scripted mesh writer.

Sources: [Export to STL or OBJ](https://wiki.freecad.org/Export_to_STL_or_OBJ), [Standard Export](https://wiki.freecad.org/Std_Export), and [Import Export](https://wiki.freecad.org/Import_Export).

## Tessellation

A CAD solid must be converted to triangles for STL. The [Mesh from Part Shape](https://wiki.freecad.org/Mesh_FromPartShape) and [Mesh Scripting](https://wiki.freecad.org/Mesh_Scripting) pages document the deflection tradeoff. For the common case, call `export` with `format: "stl"` and explicit `linear_deflection`/`angular_deflection`; it tessellates, verifies the written mesh by readback, and reports facet count and bounds.

The best deviation depends on curvature, fit, and file size. Smaller linear and angular deflection produce a finer mesh, but do not use extreme values without reason. Use a finer mesh for small radii, mating surfaces, and curved cosmetic surfaces, and avoid unnecessarily huge files.

When the structured tool does not fit the case, script the `MeshPart` route through `run_script` and verify the written file afterward:

```python
import os
import MeshPart

obj = App.ActiveDocument.getObject("Final")
App.ActiveDocument.recompute()
assert obj is not None and obj.Shape.isValid()
mesh = MeshPart.meshFromShape(
    Shape=obj.Shape,
    LinearDeflection=0.05,
    AngularDeflection=0.15,
    Relative=False,
)
out = "/absolute/path/Final.stl"
mesh.write(out)
assert os.path.isfile(out)
print({"path": out, "facets": mesh.CountFacets})
```

The values are examples. When fidelity matters, import the written mesh into a temporary document with synchronous code and compare bounds and facet count against the source.

## Bed alignment

`bed_align` is one collective translation of the minimum Z of all exported copies to zero. Use it when the downstream slicer expects the model on the bed. It changes placement, not geometry, so re-check bounds against the source before and after.

## Export readback

The result reports the readback for the written file:

- STL and 3MF: mesh solidity, facet count, and bounds.
- STEP: validity, solid count, volume, and bounds.
- FCStd: the copied document's `objectCount`. The identity, count, and placement comparison happens inside the tool to verify the reopened copy and is not returned.

The readback verifies the written file. It does not validate how another application will consume the file. Report the readback values as the verification evidence, not as inferred success.

## Export only the intended objects

Build an explicit list of internal names for the `objects` argument. Do not export every visible object by accident.

```python
obj = App.ActiveDocument.getObject("Final")
assert obj is not None
assert obj.Shape.isValid()
print([obj.Name])  # pass this list to export
```

If multiple independent parts are intentional, name and export them deliberately. Avoid exporting construction sketches, hidden tools, duplicate Bodies, or both a source feature and its final Tip.

## Mesh import and repair

Use `import_model` for STEP and STL behind file consent. Use `run_script` with the registered FreeCAD modules to import other formats and to inspect holes, normals, non-manifold regions, and other mesh problems.

If conversion back to BRep is required, the documented route is:

1. Evaluate and repair the mesh.
2. Harmonize normals and close or fill holes where appropriate.
3. Convert with [Part Shape From Mesh](https://wiki.freecad.org/Part_ShapeFromMesh), using a justified sewing tolerance.
4. Refine if appropriate.
5. Convert to a solid with [Part MakeSolid](https://wiki.freecad.org/Part_MakeSolid).
6. Run Check Geometry again.

This is not guaranteed repair. FreeCAD has no universal automatic BRep repair; if the source modeling operation created a fault, rebuild or correct it. Preserve the source mesh and report every repair and conversion operation.

### Mesh to shape, exact API

Verified on FreeCAD 1.1.3: `makeShapeFromMesh` returns `None` and modifies the shape in place, so do not assign its return value.

```python
import MeshPart
import Part

mesh = MeshPart.meshFromShape(Shape=source.Shape, LinearDeflection=0.5, AngularDeflection=0.5)
shape = Part.Shape()
shape.makeShapeFromMesh(mesh.Topology, 0.05)   # returns None; sews into a Shell
assert shape.isValid(), "sewing failed"
solid = Part.makeSolid(shape)                  # Shell -> Solid
print(solid.ShapeType, len(solid.Solids))
```

The tolerance argument controls the sewing distance. A tolerance that is too small leaves an open shell and `makeSolid` then produces a shell-like result instead of a solid. Assert `ShapeType == "Solid"` and `len(Solids) == 1` before you use the result.

## Export report

Report:

- Output path and format.
- Units assumed.
- Source object and internal name, `TypeId`, shape type, solid count, volume, and global bounds.
- Facet count and tessellation tolerances for a mesh.
- Whether `bed_align` was applied.
- Whether the file was re-imported and whether bounds and validity matched.

## Sources

- [Mesh Workbench](https://wiki.freecad.org/Mesh_Workbench)
- [Mesh from Part Shape](https://wiki.freecad.org/Mesh_FromPartShape)
- [Mesh Scripting](https://wiki.freecad.org/Mesh_Scripting)
- [Mesh Evaluation](https://wiki.freecad.org/Mesh_Evaluation)
- [Mesh to Part](https://wiki.freecad.org/Mesh_to_Part)
- [Part Shape From Mesh](https://wiki.freecad.org/Part_ShapeFromMesh)
- [Part MakeSolid](https://wiki.freecad.org/Part_MakeSolid)
- [Part CheckGeometry](https://wiki.freecad.org/Part_CheckGeometry)
- [Part RefineShape](https://wiki.freecad.org/Part_RefineShape)
- [Export to STL or OBJ](https://wiki.freecad.org/Export_to_STL_or_OBJ)
