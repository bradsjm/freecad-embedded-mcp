# Part geometry and topology

Use this reference for deterministic shape construction, CSG, topology inspection, and scripted shape work. Use `run_script` for document mutation because it runs synchronously on the GUI thread; use `export` for STL/STEP/3MF/FCStd output and `validate_geometry` for structured validity reports.

## Geometry versus document features

The Part module creates OpenCASCADE BRep geometry. A `Part.Shape`/TopoShape contains a hierarchy such as compounds, compsolids, solids, shells, faces, wires, edges, and vertices. A document feature is the container that stores the shape and gives it a name/properties/view representation.

The [Part scripting](https://wiki.freecad.org/Part_scripting) and [Topological data scripting](https://wiki.freecad.org/Topological_data_scripting) pages are the primary references. The basic pattern is:

```python
import FreeCAD as App
import Part

doc = App.ActiveDocument or App.newDocument("BuiltPart")
obj = doc.addObject("Part::Feature", "Final")
obj.Shape = Part.makeBox(40, 30, 5)
doc.recompute()
```

## Primitives and booleans

Use `Part.makeBox`, `Part.makeCylinder`, `Part.makeSphere`, `Part.makeCone`, and related constructors for simple exact shapes. Combine shapes with `fuse`, `common`, `cut`, or `section` when the operation is geometrically appropriate:

```python
base = Part.makeBox(40, 30, 8)
through = Part.makeCylinder(4, 8, App.Vector(20, 15, 0))
result = base.cut(through)
obj.Shape = result
```

Avoid coincident or nearly coincident faces in boolean inputs. Give cutting tools enough depth to pass fully through the target, or intentionally stop them for blind holes. Recompute and inspect validity after each Boolean stage.

The [Part Boolean](https://wiki.freecad.org/Part_Boolean), [Part Cut](https://wiki.freecad.org/Part_Cut), [Part Fuse](https://wiki.freecad.org/Part_Fuse), and [Part Common](https://wiki.freecad.org/Part_Common) pages describe the GUI equivalents.

## Topology inspection

Use shape metrics and topology to decide whether a result is a valid solid:

```python
shape = obj.Shape
print({
    "shape_type": shape.ShapeType,
    "solids": len(shape.Solids),
    "shells": len(shape.Shells),
    "faces": len(shape.Faces),
    "edges": len(shape.Edges),
    "vertices": len(shape.Vertexes),
    "volume": shape.Volume,
    "valid": shape.isValid(),
    "bounds": (shape.BoundBox.XMin, shape.BoundBox.XMax,
               shape.BoundBox.YMin, shape.BoundBox.YMax,
               shape.BoundBox.ZMin, shape.BoundBox.ZMax),
})
```

For a single-solid result, expect one valid solid unless a deliberate multi-part export was requested. A positive volume does not prove that a shape is a valid, watertight solid.

## Transformations

Use `Placement` for an object’s location/orientation. Use shape transformations only when the geometry definition itself must change. The topology-scripting guidance distinguishes rigid transforms from general transformations; non-uniform transforms can alter curves and surfaces and may produce less robust geometry. Test the resulting shape and recheck bounds.

Keep the source shape and final shape separate while developing if a reversible history matters. Do not repeatedly replace the same Body feature with transient shape results.

## Refinement and tolerance

Boolean operations can leave redundant edges. A late refined copy or `removeSplitter()` may simplify the result, but refinement can also remove edges that later features rely on. Refine only after the feature graph is complete and validate again. Read [Part RefineShape](https://wiki.freecad.org/Part_RefineShape) and [Part ToleranceSet](https://wiki.freecad.org/Part_ToleranceSet) for current FreeCAD 1.1 behavior.

FreeCAD’s [Check Geometry](https://wiki.freecad.org/Part_CheckGeometry) reports BRep problems and can run Boolean checks; it does not automatically repair them. Fix the modeling operation that created the problem.

## Sources

- [Part Workbench](https://wiki.freecad.org/Part_Workbench)
- [Part scripting](https://wiki.freecad.org/Part_scripting)
- [Topological data scripting](https://wiki.freecad.org/Topological_data_scripting)
- [Part Primitives](https://wiki.freecad.org/Part_Primitives)
- [Part Boolean](https://wiki.freecad.org/Part_Boolean)
- [Part Cut](https://wiki.freecad.org/Part_Cut)
- [Part Fuse](https://wiki.freecad.org/Part_Fuse)
- [Part Common](https://wiki.freecad.org/Part_Common)
- [Part RefineShape](https://wiki.freecad.org/Part_RefineShape)
- [Part ToleranceSet](https://wiki.freecad.org/Part_ToleranceSet)
- [Part CheckGeometry](https://wiki.freecad.org/Part_CheckGeometry)
