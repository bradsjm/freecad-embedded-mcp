# Mesh export, import, and repair

Use this reference for mesh export, import, and repair. The `export` tool writes STL, STEP, 3MF, and native FCStd copies with readback verification; use `run_script` for scripted tessellation routes, mesh import, and repair. Do not instruct a human to use FreeCAD's UI.

## Tessellating a solid

A CAD solid must be converted to triangles for STL. The [Mesh from Part Shape](https://wiki.freecad.org/Mesh_FromPartShape) and [Mesh Scripting](https://wiki.freecad.org/Mesh_Scripting) pages document mesh creation and the linear/angular deflection tradeoff. For the common case, call `export` with `format: "stl"` and explicit `linear_deflection`/`angular_deflection`; it tessellates, verifies the written mesh by readback, and reports facet count and bounds.

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

The values are examples. Use a finer mesh for small radii, mating surfaces, and curved cosmetic surfaces, but avoid unnecessarily huge files. When fidelity matters, import the written mesh into a temporary document with synchronous code and compare bounds/facets against the source.

## STL, OBJ, 3MF, and STEP

- **STL:** simple triangle mesh with no unit metadata; report millimetres as the project unit.
- **OBJ:** mesh interchange; use only when the downstream workflow requires it.
- **3MF:** may carry more metadata, but the FreeCAD exporter writes a generic 3MF without application-specific settings.
- **STEP:** precise CAD interchange; prefer STEP when the downstream workflow accepts it, otherwise export STL/3MF.

Sources: [Export to STL or OBJ](https://wiki.freecad.org/Export_to_STL_or_OBJ), [Standard Export](https://wiki.freecad.org/Std_Export), and [Import Export](https://wiki.freecad.org/Import_Export).

## Importing and repairing an existing mesh

Use `run_script` with the registered FreeCAD modules to inspect holes, normals, non-manifold regions, and other mesh problems. If conversion back to BRep is required, the documented route is conceptually:

1. evaluate/repair the mesh;
2. harmonize normals and close/fill holes where appropriate;
3. convert with [Part Shape From Mesh](https://wiki.freecad.org/Part_ShapeFromMesh), using a justified sewing tolerance;
4. refine if appropriate;
5. convert to a solid with [Part MakeSolid](https://wiki.freecad.org/Part_MakeSolid);
6. run Check Geometry again.

This is not guaranteed repair. FreeCAD has no universal automatic BRep repair; if the source modeling operation created a fault, rebuild or correct it. Preserve the source mesh and report every repair/conversion operation.

## Export report

When reporting an export, include from the export result and `run_script` output:

- output path and format;
- units assumed;
- source object and internal name;
- source `TypeId`, shape type, solid count, volume, and global bounds;
- facet count and tessellation tolerances for a mesh;
- whether the file was re-imported and whether bounds/validity matched.

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
