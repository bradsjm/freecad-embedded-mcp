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

## Construction vocabulary

These constructors and methods were accepted by FreeCAD 1.1.3. Use them instead of guessing a signature.

| Call | Result |
|---|---|
| `Part.makeBox(l, w, h)` | Solid. A 4th argument is the base `Vector`, a 5th the axis `Vector`. |
| `Part.makeCylinder(r, h)` | Solid. Optional position, direction, and angle in degrees. |
| `Part.makeSphere(r)`, `Part.makeCone(r1, r2, h)`, `Part.makeTorus(major, minor)` | Solids. |
| `Part.makeHelix(pitch, height, radius)` | Wire. |
| `Part.makeWedge(...)` | Solid from the 10 documented corner coordinates. |
| `Part.makePlane(l, w)` | Face. Extrude or revolve it to get a solid. |
| `Part.makeLine(a, b)`, `Part.makeCircle(r)` | Edge. `Part.makeCircle(r, center, axis, startDeg, endDeg)` makes an arc. |
| `Part.Arc(p1, p2, p3).toShape()` | Edge through three points. |
| `Part.Ellipse(center, major, minor).toShape()` | Edge. |
| `Part.BSplineCurve()` then `interpolate(points)` or `buildFromPoles(points)` | Edge through `toShape()`. Both methods mutate the curve and return `None`. |
| `Part.BezierCurve()` then `setPoles(points)` | Edge through `toShape()`. |
| `Part.Wire([edges])` | Wire. Use `Part.__sortEdges__(edges)` first when the order is unknown. |
| `Part.Face(wire)`, or `Part.Face([outer, hole1, hole2])` | Face. Each hole wire must be reversed first. Read the section below. |
| `Part.Shell([faces])`, then `Part.Solid(shell)` | Solid from a closed shell. |
| `Part.Compound([shapes])` | Grouped shapes that are not merged. |
| `shape.fuse(other)`, `.cut(other)`, `.common(other)`, `.multiFuse([...])` | Boolean results. |
| `shape.removeSplitter()` | Copy without redundant seam edges. |
| `shape.makeFillet(r, edges)`, `shape.makeChamfer(d, edges)` | Dress-up. `makeChamfer(d1, d2, edges)` is asymmetric. |
| `shape.makeOffsetShape(distance, tolerance)`, `shape.makeThickness(faces, t, tolerance)` | Offset and shell. |
| `shape.section(Part.makePlane(l, w, origin))` | Intersection curve. |
| `face.extrude(vector)`, `face.revolve(center, axis, degrees)` | Solid. |
| `Part.makeLoft([wires], True)` | Solid through wire sections. |
| `Wire([pathEdge]).makePipeShell([Wire([profile])], True, False)` | Swept solid. |
| `shape.mirror(origin, normal)`, `shape.copy()`, `shape.translate(vector)` | Transformed copy. |
| `shape.Volume`, `.Area`, `.CenterOfMass`, `.BoundBox`, `.ShapeType`, `.isValid()`, `.check()` | Reports. `check()` returns `None` when the BRep has no reported fault. |
| `edge.Length`, `.Curve`, `.firstVertex()`, `.valueAt(u)`, `.tangentAt(u)`, `.parameterAt(vertex)` | Edge data. |
| `face.Area`, `.Surface`, `.normalAt(u, v)`, `.OuterWire`, `.CenterOfMass` | Face data. |
| `BoundBox.Center`, `.DiagonalLength`, `.XLength`, `.YLength`, `.ZLength` | Bounding box data. |

## Pass edges and faces from the shape that owns them

`makeFillet`, `makeChamfer`, and `makeThickness` reject geometry that came from a different shape object. Calling `Part.makeBox(10, 10, 10).makeFillet(1.0, Part.makeBox(10, 10, 10).Edges)` fails with `CADKernelError: edge does not belong to the shape`, because the second call built a new box. Bind the shape once and pass its own sub-elements:

```python
base = Part.makeBox(10, 10, 10)
rounded = base.makeFillet(1.0, base.Edges)
selected = base.makeFillet(1.0, [base.Edges[0], base.Edges[3]])
```

`makePipeShell` sections must be wires. A bare `Part.Circle` raises `OCCError: BRepFill_Section: bad shape type of section`.

## Reverse a hole wire

`Part.Face([outer, hole])` does not require a hole to be inside the outer wire, and it does not infer subtraction from containment. The hole wire must run opposite to the outer wire. Verified on FreeCAD 1.1.3 with a 10 × 10 mm square and a radius-2 mm circle:

| Construction | Area |
|---|---|
| `Part.Face([outer, circle])` | 112.566 mm² (added) |
| `Part.Face([outer, circle])` after `circle.reverse()` | 87.434 mm² (subtracted) |

Prefer the Boolean route, which needs no orientation care:

```python
face = Part.Face(outer)
hole = Part.Face(Part.Wire([Part.makeCircle(2, App.Vector(5, 5, 0))]))
plate = face.cut(hole)                 # planar shell, 87.434 mm²
solid = plate.extrude(App.Vector(0, 0, 3))   # 262.301 mm³
```

Check the area or volume against the value you expect. A silently added hole shows up as a larger area, not as an error.

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
