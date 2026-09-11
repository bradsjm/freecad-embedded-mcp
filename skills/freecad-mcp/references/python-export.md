# Python scripting and export through MCP

Use `run_script` for Python that touches FreeCAD documents, geometry, GUI state, selection, recompute, or import when no structured tool covers the operation. Use the structured tools where they cover the operation: `save_document` for saving, `export` for STL/STEP/3MF/FCStd, `validate_geometry` and `measure` for checks. The [FreeCAD Scripting Basics](https://wiki.freecad.org/FreeCAD_Scripting_Basics) page documents the `App`/`Gui` split, `addObject`, object inspection, and recompute workflow.

## Persistent script sessions

`run_script` keeps one namespace per `session_id` for the server's lifetime and pre-seeds `FreeCAD`/`App` and `Gui`. Do not rely on this for hidden state when a model can be made explicit in the document. Use short, idempotent scripts where possible and print a compact result. At most 32 sessions are kept; new sessions are refused when the limit is reached.

```python
import FreeCAD as App
import FreeCADGui as Gui

print(App.ActiveDocument.Name if App.ActiveDocument else "no active document")
```

`run_script` executes on the GUI thread. A client that declares the Tasks extension may detach it, but the code remains cooperative and cannot be preempted after it starts. Keep scripts within the `timeout_s` deadline (default 90 s, maximum 3600 s) and split long work into stages. `stdout`, `stderr`, and the traceback are captured even when the script fails.

## Scripted modeling skeleton

```python
import FreeCAD as App
import Part

doc = App.ActiveDocument or App.newDocument("BuiltPart")

base = doc.getObject("Base") or doc.addObject("Part::Feature", "Base")
base.Label = "Base"
base.Shape = Part.makeBox(40, 30, 5)

# Keep coordinates explicit. This cylinder starts at z=0.
tool = Part.makeCylinder(4, 5, App.Vector(20, 15, 0))
base.Shape = base.Shape.cut(tool)

doc.recompute()
assert base.Shape.isValid(), "invalid final BRep"
print(base.Name, base.Shape.ShapeType, len(base.Shape.Solids), base.Shape.Volume)
```

If an object is already present, inspect it before overwriting its shape. Do not silently destroy a user's parametric history by replacing a PartDesign feature with a raw shape.

## Structured export

Use the `export` tool instead of ad-hoc writer code. It takes `document`, `objects` (internal names), `format` (`stl`, `step`, `3mf`, `fcstd`), and `path`. Optional arguments: `linear_deflection` and `angular_deflection` for mesh formats (defaults 0.03 and 0.12), and `bed_align` (one collective translation of the minimum Z of all copies to zero). It writes to a temporary sibling file, verifies the result by readback, then publishes; overwriting an existing destination requires consent. The result reports the readback: mesh solidity, facet count, and bounds for STL/3MF; validity, solid count, volume, and bounds for STEP; object identity, count, and placements for an FCStd copy.

An absolute path inside an allowed root is required; paths outside `allowed_roots` fail with `PATH_NOT_ALLOWED`.

### Tessellation control

The best deviation depends on curvature, fit, and file size. Smaller linear/angular deflection generally produces a finer mesh, but do not use extreme values without reason. When the structured tool does not fit the case, use the MeshPart scripting route documented by [Mesh from Part Shape](https://wiki.freecad.org/Mesh_FromPartShape) and [Mesh Scripting](https://wiki.freecad.org/Mesh_Scripting) through `run_script`. Verify the written file afterward. See [Mesh export and repair](export-print.md).

The older [Export to STL or OBJ](https://wiki.freecad.org/Export_to_STL_or_OBJ) tutorial says STL/OBJ has no embedded unit metadata and FreeCAD assumes millimetres on export. Treat the model as millimetres before export.

### 3MF

The `export` tool writes 3MF through the registered exporter; the result is a generic 3MF without application-specific settings.

## Save the editable source

Save the `.FCStd` source before or alongside the exported mesh:

```text
save_document(document=<name>, path="/absolute/path/Final.FCStd")
```

An explicit `path` is a native save-as; saving over a different existing file requires consent. Without `path`, an unsaved document is an actionable error. Use `reload_document(document)` only after an external process has changed the on-disk file. It closes and reopens the stale in-memory copy; it is not a general undo or refresh command.

## Export only the intended object

Do not export every visible object by accident. Build an explicit list of internal names for the `objects` argument:

```python
obj = App.ActiveDocument.getObject("Final")
assert obj is not None
assert obj.Shape.isValid()
print([obj.Name])  # pass this list to export
```

If multiple independent parts are intentional, name and export them deliberately, then report the explicit object list and source bounds. Avoid exporting construction sketches, hidden tools, duplicate Bodies, or both a source feature and its final Tip.

## Output reporting

A good export report contains:

- Internal object names and `TypeId`.
- Shape type, solid count, volume, and bounding box.
- Exact absolute output paths and the export readback fields.
- Mesh facet count and deflection values when tessellating.

The export readback verifies the written file. It does not validate how another application will consume the file; report the readback values as the verification evidence.

## Sources

- [FreeCAD Scripting Basics](https://wiki.freecad.org/FreeCAD_Scripting_Basics)
- [Scripting and macros](https://wiki.freecad.org/Scripting_and_macros)
- [Part scripting](https://wiki.freecad.org/Part_scripting)
- [Topological data scripting](https://wiki.freecad.org/Topological_data_scripting)
- [Mesh from Part Shape](https://wiki.freecad.org/Mesh_FromPartShape)
- [Mesh Scripting](https://wiki.freecad.org/Mesh_Scripting)
- [Export to STL or OBJ](https://wiki.freecad.org/Export_to_STL_or_OBJ)
- [Standard Export](https://wiki.freecad.org/Std_Export)
- [Import Export](https://wiki.freecad.org/Import_Export)
