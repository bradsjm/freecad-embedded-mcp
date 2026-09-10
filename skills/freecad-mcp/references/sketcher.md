# Sketcher profiles through MCP

Use this reference to build or edit a constrained profile. Read a sketch with `inspect_sketch`. Change it with `edit_sketch` in one atomic batch. Use `run_script` for sketch creation and for the cases this file marks as out of reach.

The [Sketcher scripting](https://wiki.freecad.org/Sketcher_scripting) page defines the constraint arguments and the index rules. Every statement below was checked against FreeCAD 1.1.3.

## Read the sketch before editing it

`inspect_sketch(document, sketch)` returns `geometry` rows, `constraint` rows, `expressionBindings`, and a `solver` summary.

- Geometry rows use `kind`: `point`, `lineSegment`, `circle`, `arcOfCircle`, or `unsupported` with the native type name.
- An arc built from `Part.ArcOfCircle` appears as `lineSegment` in FreeCAD 1.1.3, because that native type also exposes `StartPoint` and `EndPoint`. Read the native geometry through `run_script` when the distinction matters.
- Constraint rows carry `index`, `type`, `first`/`firstPos`, `second`/`secondPos`, `third`/`thirdPos`, `datum`, `active`, and `name`.
- Ignore `datum` on a geometric row. The reporter echoes the numeric `Value` of every constraint, so `Coincident`, `Horizontal`, and `Vertical` rows show `0.0 mm`.
- `driving` is `null` in FreeCAD 1.1.3 because the native attribute is `Driving`, not `IsDriving`.
- `solver.fullyConstrained` and `solver.degreesOfFreedom` are `null` in FreeCAD 1.1.3 because the native `getSolverDoF` and `getSolverMessages` methods are absent. A null summary is not an error and not a constraint count. Read the real value through `run_script` with `sketch.solve()`. It returns the degrees of freedom, and it reports a negative value for an inconsistent sketch.

## Index model

- Geometry indices are 0-based and match the `index` values from `inspect_sketch`.
- The constraint arguments `first`, `second`, and `third` are geometry indices.
- A point position selects one part of the geometry. `0` = whole edge. `1` = start point. `2` = end point. `3` = center of a circle, arc, or ellipse. `4` or higher = the numbered B-spline pole.
- A negative geometry index selects the sketch frame. `-1` = horizontal X axis. `-2` = vertical Y axis. `-3` and below select external geometry elements in order, where `-3` is the first external element.
- Deleting geometry or constraints renumbers the remaining entries. Never reuse a pre-delete index after a delete.

## Batch edit contract

`edit_sketch` applies one batch inside one transaction. A failure rolls back the whole batch and leaves the document unchanged.

- Every operation accepts at most 64 entries.
- `deleteGeometry` and `deleteConstraints` run first, in descending index order. A multi-delete therefore uses pre-delete indices.
- `addGeometry` then appends in list order. The response returns the new indices in `addedGeometry`.
- `addConstraints` then appends in list order. The response returns the new indices in `addedConstraints`.
- `setDatums` indices refer to the final constraint state after the deletes and the additions.
- A constraint that references geometry added in the same batch must use the index that the new geometry will receive. Add the geometry in one batch, read `addedGeometry`, then add the constraints in the next batch. This keeps the indices unambiguous.
- `edit_sketch` refuses an object that is not a `Sketcher::SketchObject`, and refuses an empty batch.

## Geometry entries

| `kind` | Required fields | Notes |
|---|---|---|
| `point` | `x`, `y` | Free point. |
| `lineSegment` | `start`, `end` | Each value is an `[x, y]` pair, in millimetres. |
| `circle` | `center`, `radius` | `radius` must be positive. |
| `arcOfCircle` | `center`, `radius`, `startAngle`, `endAngle` | Angles are radians, not degrees. |

Every entry accepts `construction: true` for construction geometry. Confirm the angle unit on the same call. Passing `startAngle: 0` and `endAngle: 180` produced a native arc that ends at 4.07 radians, which is 180 radians reduced modulo 2π.

Two facts about construction geometry on FreeCAD 1.1.3:

- The flag works, but `inspect_sketch` always reports `"construction": false`. The reporter reads `Geometry[i].Construction`, which does not exist on the native element type. Read the real flag through `run_script` with `sketch.getConstruction(index)`.
- A sketch whose geometry is all construction has a null shape. The batch that leaves it in that state rolls back with `RuntimeError: shape is invalid`. Include one normal edge in the same batch when the sketch needs construction geometry.

## Constraint entries

Send `{"type": ..., "arguments": [...], "datum": ...}`. `datum` is optional except where the table marks it required. Argument layouts below were accepted by FreeCAD 1.1.3.

| `type` | Verified `arguments` | `datum` |
|---|---|---|
| `Coincident` | `[geoA, posA, geoB, posB]`. `[geo, 1, -1, 1]` locks the start point to the X axis. The form `[0, 1, -1, 0]` does not | no |
| `Horizontal`, `Vertical`, `Block` | `[geo]` | no |
| `PointOnObject` | `[pointGeo, pos, targetGeo]` | no |
| `Parallel`, `Perpendicular`, `Equal`, `Tangent` | `[geoA, geoB]` | no |
| `Symmetric` | `[geoA, posA, geoB, posB, axisGeo]` or `[geoA, posA, geoB, posB, geoA2, posA2]` | no |
| `DistanceX`, `DistanceY` | `[geo, posA, geoB, posB]`, or `[geo, posA, posB]` for one edge | required |
| `Distance` | `[geo, 0]`, `[geo, posA, posB]`, or `[geoA, posA, geoB, posB]` | required |
| `Radius`, `Diameter` | `[geo]` | required |
| `Angle` | `[geoA, geoB]`, or `[geoA, geoB, geo, pos]` | required |
| `Weight` | `[geo, pole]` on a B-spline | required |

Datum strings carry a unit: `"40 mm"`, `"30 mm"`, `"90 deg"`. The value lands in the property's internal unit.

`Tangent` on two line segments produced collinear lines in the verified case. `Collinear` is listed in the tool schema, but the native constructor rejected both `[0, 2]` and `[0, 1, 2, 1]` with `TypeError: Invalid parameters`. A `Collinear` entry therefore rolls the batch back. Use `Tangent` between two lines, or `run_script`. The same 1.1.3 restriction applies to `InternalAlignment`, `SnellsLaw`, `AngleViaPoint`, and some `Weight` forms. For these, the native constructor accepts a narrower argument list than the wiki documents. Confirm the exact native form in the running installation before you rely on one.

### `setDatums` is broken in FreeCAD 1.1.3

The server passes the datum string straight to the native `setDatum`, which requires a `FreeCAD.Units.Quantity`. The batch fails with `TypeError: Wrong arguments` and rolls back with `nextAction: retry_from_original_state`. The rollback is safe: repair the document, then choose one of these routes.

- Add the constraint again with the corrected `datum` after `deleteConstraints`.
- Change the datum through `run_script` with a native quantity:

```python
sketch.setDatum(index, App.Units.Quantity("45 mm"))
```

## Create the sketch before you can edit it

`create_object` and `create_feature` cannot produce an empty sketch, because an empty sketch has a null shape. Create the sketch through `run_script`, then use the structured tools for its contents.

```python
import FreeCAD as App

doc = App.ActiveDocument
sketch = doc.addObject("Sketcher::SketchObject", "Profile")
doc.recompute()
print(sketch.Name)
```

Inside a `PartDesign::Body`, create the sketch with `body.newObject("Sketcher::SketchObject", "Profile")` and attach it through the native properties:

```python
sketch = body.newObject("Sketcher::SketchObject", "Profile")
sketch.AttachmentSupport = [(doc.getObject("XY_Plane"), "")]
sketch.MapMode = "FlatFace"
doc.recompute()
```

A sketch inside a Body exposes `AttachmentSupport`, not `Support`. Never pass `support` to `create_feature` for a sketch: that call fails with `feature '...' exposes no Support property`.

## Sequence that works

1. Create the sketch with `run_script`.
2. `edit_sketch` the geometry, then `edit_sketch` the constraints. Read `addedGeometry` from the first batch so the second batch refers to real indices.
3. `inspect_sketch` to confirm the indices, the geometry kinds, and the `datum` values.
4. Read `sketch.solve()` through `run_script`. `0` means fully constrained. A negative value means conflicting or redundant constraints.
5. Use the sketch as a profile, then validate the resulting solid. See [Geometry validation](validation.md).

This sequence applies to a standalone sketch. A sketch inside a `PartDesign::Body` cannot be edited until the Body holds one solid, so bootstrap the first Body feature natively. See [Null-shape objects block the mutation gate](#null-shape-objects-block-the-mutation-gate) and the recipe below.

## Recipe: fully constrained rectangle

Verified on FreeCAD 1.1.3. The profile solves to zero degrees of freedom and forms one closed wire of 40 x 30 mm.

Two paths build it. Use the path that matches where the sketch lives.

### Path A: into a `PartDesign::Body` (native bootstrap)

A Body cannot receive structured calls until it holds one solid, so build the Body, the profile, and the first Pad in a single `run_script` call.

```python
import FreeCAD as App
import Part
import Sketcher
from FreeCAD import Vector

doc = App.ActiveDocument
body = doc.addObject("PartDesign::Body", "Body")
sketch = body.newObject("Sketcher::SketchObject", "Profile")
sketch.AttachmentSupport = [(doc.getObject("XY_Plane"), "")]
sketch.MapMode = "FlatFace"

for start, end in (((0, 0), (40, 0)), ((40, 0), (40, 30)),
                   ((40, 30), (0, 30)), ((0, 30), (0, 0))):
    sketch.addGeometry(Part.LineSegment(Vector(*start, 0), Vector(*end, 0)), False)

for index in range(3):
    sketch.addConstraint(Sketcher.Constraint("Coincident", index, 2, index + 1, 1))
sketch.addConstraint(Sketcher.Constraint("Coincident", 3, 2, 0, 1))
sketch.addConstraint(Sketcher.Constraint("Coincident", 0, 1, -1, 1))
sketch.addConstraint(Sketcher.Constraint("Horizontal", 0))
sketch.addConstraint(Sketcher.Constraint("Horizontal", 2))
sketch.addConstraint(Sketcher.Constraint("Vertical", 1))
sketch.addConstraint(Sketcher.Constraint("Vertical", 3))
sketch.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, 0, 2, App.Units.Quantity("40 mm")))
sketch.addConstraint(Sketcher.Constraint("DistanceY", 1, 1, 1, 2, App.Units.Quantity("30 mm")))

pad = body.newObject("PartDesign::Pad", "Pad")
pad.Profile = sketch
pad.Length = 10.0
doc.recompute()
print(body.Tip.Name, len(body.Shape.Solids), round(body.Shape.Volume, 3), sketch.solve())
```

The verified result is `Pad 1 12000.0 0`. From this point the structured tools work on the Body. This verified call added a pocket:

```json
{
  "document": "Part",
  "body": "Body",
  "kind": "pocket",
  "name": "Pocket",
  "profile": "Profile",
  "properties": {"Length": 5, "Type": "Length"}
}
```

`Type` is an enumeration and must be the exact string, not an index. Do not list the profile sketch in a `validate_geometry` call that carries `expected_solids`. The contract applies to every listed object, so a zero-solid sketch reports a mismatch and the document is reported as not fully valid.

### Path B: a standalone sketch through `edit_sketch`

`create_object` cannot create an empty sketch, so create it with `run_script` first.

```python
sketch = App.ActiveDocument.addObject("Sketcher::SketchObject", "Profile")
App.ActiveDocument.recompute()
print(sketch.Name)
```

Then add the rectangle with one batch:

```json
{
  "document": "Part",
  "sketch": "Profile",
  "addGeometry": [
    {"kind": "lineSegment", "start": [0, 0], "end": [40, 0]},
    {"kind": "lineSegment", "start": [40, 0], "end": [40, 30]},
    {"kind": "lineSegment", "start": [40, 30], "end": [0, 30]},
    {"kind": "lineSegment", "start": [0, 30], "end": [0, 0]}
  ]
}
```

Then constrain it with a second batch. The same batch shape applies to a Body sketch once the Body holds a solid.

```json
{
  "document": "Part",
  "sketch": "Profile",
  "addConstraints": [
    {"type": "Coincident", "arguments": [0, 2, 1, 1]},
    {"type": "Coincident", "arguments": [1, 2, 2, 1]},
    {"type": "Coincident", "arguments": [2, 2, 3, 1]},
    {"type": "Coincident", "arguments": [3, 2, 0, 1]},
    {"type": "Coincident", "arguments": [0, 1, -1, 1]},
    {"type": "Horizontal", "arguments": [0]},
    {"type": "Horizontal", "arguments": [2]},
    {"type": "Vertical", "arguments": [1]},
    {"type": "Vertical", "arguments": [3]},
    {"type": "DistanceX", "arguments": [0, 1, 0, 2], "datum": "40 mm"},
    {"type": "DistanceY", "arguments": [1, 1, 1, 2], "datum": "30 mm"}
  ]
}
```

Remove one `Horizontal` and one `Vertical` entry when the profile must stay skewed.

### Confirm the profile

1. Read `inspect_sketch` again. Confirm the returned `addedGeometry` and `addedConstraints` indices.
2. Confirm the profile closes. The wires must come from coincident endpoints, not from near-identical coordinates.
3. Read `sketch.solve()` through `run_script`. `0` means fully constrained. A negative value means conflicting or redundant constraints: the native `addConstraint` accepts a conflicting entry, and the sketch then reports `Invalid`. In the verified case, two `DistanceX` datums of `10 mm` and `20 mm` produced `solve() == -3` and state `['Touched', 'Invalid']`. A `Horizontal` plus a zero-degree `Angle` produced `solve() == -2`.
4. `inspect_sketch` does not report the object state. Read it with `str(sketch.State)` through `run_script`, or rely on the mutation gate: `edit_sketch` rejects a sketch left in an invalid state.
5. Validate the resulting solid after the profile becomes a feature. See [Geometry validation](validation.md).

## Null-shape objects block the mutation gate

A fresh object has a null shape until geometry is assigned. On a null shape, FreeCAD 1.1.3 raises `RuntimeError: shape is invalid` for `.Volume`, `.Area`, `.ShapeType`, and `.isValid()`. `len(shape.Solids)` returns `0` safely.

The mutation gate reports the shape volume of every validated object. A mutation whose final state contains a null-shape object therefore aborts and rolls back with `RuntimeError: shape is invalid`. Verified consequences:

| Operation | Result |
|---|---|
| `create_object` with `Part::Box` | Works. The solid exists after recompute. |
| `create_object` with `PartDesign::Body` | Fails and rolls back. |
| `create_object` with `Part::Feature` | Fails and rolls back. |
| `create_feature(kind: "sketch")` inside a Body without a solid | Fails and rolls back. |
| `edit_sketch` on a sketch inside a Body without a solid | Fails and rolls back. The Body is a validated dependent. |
| `edit_sketch` on a standalone sketch | Works. |
| `create_feature(kind: "pad")` on a Body with a solid, with a profile from that Body | Works. |
| `create_feature(kind: "pocket")` on a Body with a solid | Works. |
| `create_feature(kind: "datum_plane")` without support, on a Body with a solid | Works. The reported bounds are ±1e100 for the unbounded plane. |
| `create_feature` with `support` for `sketch` or `datum_plane` | Fails: those types expose `AttachmentSupport`, not `Support`. |

The rollback is clean: the document keeps its pre-call content and the error carries `nextAction: retry_from_original_state`. Do not retry the same call. Change the route.

## Scripted sketches

Stay with the structured tools when they cover the operation. Use `run_script` for external geometry, `InternalAlignment`, `Weight`, `SnellsLaw`, constraint names, driving and reference toggles, datum edits, and sketch creation. Read the native `sketch.Geometry` and `sketch.Constraints` to confirm the result, and apply this reference's index rules unchanged.

## Sources

- [Sketcher scripting](https://wiki.freecad.org/Sketcher_scripting)
- [Sketcher Workbench](https://wiki.freecad.org/Sketcher_Workbench)
- [Sketcher SketchObject](https://wiki.freecad.org/Sketcher_SketchObject)
- [Sketcher ConstrainCoincident](https://wiki.freecad.org/Sketcher_ConstrainCoincident)
- [Sketcher ConstrainDistance](https://wiki.freecad.org/Sketcher_ConstrainDistance)
- [Sketcher ConstrainDistanceX](https://wiki.freecad.org/Sketcher_ConstrainDistanceX)
- [Sketcher ConstrainDistanceY](https://wiki.freecad.org/Sketcher_ConstrainDistanceY)
- [Sketcher ConstrainRadius](https://wiki.freecad.org/Sketcher_ConstrainRadius)
- [Sketcher ConstrainAngle](https://wiki.freecad.org/Sketcher_ConstrainAngle)
- [Sketcher ConstrainSymmetric](https://wiki.freecad.org/Sketcher_ConstrainSymmetric)
- [Sketcher ConstrainTangent](https://wiki.freecad.org/Sketcher_ConstrainTangent)
- [PartDesign Pad](https://wiki.freecad.org/PartDesign_Pad)
