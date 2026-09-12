# Sketcher profiles through MCP

Use this reference to build or edit a sketch profile. Read a sketch with `inspect_sketch`. Change it with `edit_sketch` in one atomic batch. Use `run_script` only for the cases this file marks as out of reach.

Create the `PartDesign::Body` with `create_object` and the attached sketch with `create_feature`, using the payloads in [Common recipes](recipes.md); a standalone sketch uses `create_object` with type `Sketcher::SketchObject`. Property forms, units, and recompute belong to [FreeCAD fundamentals](fundamentals.md); support, `MapMode`, and attachment offsets belong to [Placement and attachment](placement-attachment.md).

The [Sketcher scripting](https://wiki.freecad.org/Sketcher_scripting) page defines the constraint arguments and the index rules. Every statement below was checked against FreeCAD 1.1.3, and the enforced forms are recorded in `tests/native_contract.json`.

## Contents

- [Read the sketch before editing it](#read-the-sketch-before-editing-it)
- [Index model](#index-model)
- [Batch edit contract](#batch-edit-contract)
- [Geometry entries](#geometry-entries)
- [Constraint entries](#constraint-entries)
- [setDatums](#setdatums)
- [setExpressions](#setexpressions)
- [Recipe: fully constrained rectangle](#recipe-fully-constrained-rectangle)
- [Confirm the profile](#confirm-the-profile)
- [Scripted sketches](#scripted-sketches)
- [Sources](#sources)

## Read the sketch before editing it

`inspect_sketch(document, sketch)` returns `geometry` rows, `constraint` rows, `expressionBindings`, and a `solver` summary.

- Geometry rows use `kind`: `point`, `lineSegment`, `circle`, `arcOfCircle`, or `unsupported` with the native type name.
- An `arcOfCircle` element also exposes endpoints. The server tests the arc fields first and classifies it as `arcOfCircle`.
- Constraint rows carry `index`, `type`, `first`/`firstPos`, `second`/`secondPos`, `third`/`thirdPos`, `datum`, `driving`, `active`, and `name`. `driving` and `active` read the native `Driving` and `IsActive` attributes.
- Ignore `datum` on a geometric row. The reporter echoes the numeric `Value` of every constraint, so `Coincident`, `Horizontal`, and `Vertical` rows show `0.0 mm`.
- `solver.fullyConstrained` and `solver.degreesOfFreedom` read the native `FullyConstrained` and `DoF` attributes. `solve()` returns a status code, not a degree count. Do not read the status as the degree count.

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
- `setDatums` and `setExpressions` indices refer to the final constraint state after the deletes and the additions. One index cannot receive both a datum and an expression in the same batch.
- `deleteGeometry` cannot be combined with `setDatums` or `setExpressions` in one batch: deleting geometry removes the constraints attached to it and renumbers the survivors. Delete first, then edit the surviving constraints from a fresh `inspect_sketch`.
- A constraint that references geometry added in the same batch must use the index that the new geometry will receive. Add the geometry in one batch, read `addedGeometry`, then add the constraints in the next batch. This keeps the indices unambiguous.
- `edit_sketch` refuses an object that is not a `Sketcher::SketchObject`, and refuses an empty batch.

## Geometry entries

| `kind` | Required fields | Notes |
|---|---|---|
| `point` | `x`, `y` | Free point. |
| `lineSegment` | `start`, `end` | Each value is an `[x, y]` pair, in millimetres. |
| `circle` | `center`, `radius` | `radius` must be positive. |
| `arcOfCircle` | `center`, `radius`, `startAngle`, `endAngle` | Angles are radians, not degrees. |
| `rectangle` | `origin`, `width`, `height` | Expands to four line segments. Width and height are positive. |
| `polyline` | `points`, `closed` | 2–32 `[x, y]` points; `closed: true` needs 3+. No adjacent duplicate points and no repeated closing point. |
| `regularPolygon` | `center`, `radius`, `sides` | 3–32 sides; `radius` is the circumscribed radius; optional `rotation` in degrees. |

The composite kinds (`rectangle`, `polyline`, `regularPolygon`) are semantic profiles: `edit_sketch` expands them into proven `lineSegment` operations and appends the closing constraints for you — `Coincident` joints on every composite, plus `Horizontal`/`Vertical` on a rectangle. The expanded geometry consumes one geometry index per segment. In the same batch, the explicit `addConstraints` take the lower constraint indices and the auto constraints follow them. Expansion also counts against the 64-entry cap: a rectangle is 10 operations (4 geometry, 6 constraints). Read `addedGeometry` and `addedConstraints` from the response to learn the real indices.

Every entry accepts `construction: true` for construction geometry. `inspect_sketch` reports the flag from the native `getConstruction(index)` call. Confirm the angle unit on the same call. Passing `startAngle: 0` and `endAngle: 180` produced a native arc that ends at 4.07 radians, which is 180 radians reduced modulo 2π.

A sketch whose geometry is all construction has a null shape: the batch succeeds and the sketch reports `solid_count: 0`. It cannot pad until it holds one normal closed edge.

## Constraint entries

Send `{"type": ..., "arguments": [...], "datum": ...}`. `datum` is optional except where the table marks it required. The table lists the argument forms the server enforces. Every form was accepted by the native constructor on FreeCAD 1.1.3, and the acceptance is recorded in `tests/native_contract.json`.

| `type` | Accepted `arguments` | `datum` |
|---|---|---|
| `Coincident` | `[geoA, posA, geoB, posB]`. `[0, 1, -1, 1]` locks the start point to the X axis. | no |
| `Horizontal`, `Vertical`, `Block` | `[geo]` | no |
| `PointOnObject` | `[pointGeo, pos, targetGeo]` | no |
| `Parallel`, `Perpendicular`, `Equal`, `Tangent` | `[geoA, geoB]` | no |
| `Symmetric` | `[geoA, posA, geoB, posB, axisGeo]` or `[geoA, posA, geoB, posB, geoA2, posA2]` | no |
| `DistanceX`, `DistanceY` | `[geoA, posA, geoB, posB]`, or `[geo, pos]` for one edge | required |
| `Distance` | `[geo, pos]`, `[geo, posA, posB]`, or `[geoA, posA, geoB, posB]` | required |
| `Radius`, `Diameter` | `[geo]` | required |
| `Angle` | `[geoA, geoB]`, or `[geoA, geoB, geo, pos]` | required |

Geometry indices must be `>= 0`, axis references must be `-1` or `-2`, and point positions must be `0`, `1`, or `2`. A point position outside that domain is the recorded crash input for the native constructor; the server rejects it with `VALIDATION_FAILED` before any native call.

`Collinear`, `InternalAlignment`, `SnellsLaw`, `AngleViaPoint`, and `Weight` are rejected before any native call. The native 1.1.3 constructor accepted no verified form of them. Use `Tangent` between two lines instead of `Collinear`, or use `run_script` with a form recorded in `tests/native_contract.json`.

`edit_sketch` refuses a `(type, argument-count)` shape outside the recorded native contract before execution, with a `VALIDATION_FAILED` error rather than a native call. The error's `acceptedArgumentCounts` lists the safe arities for the requested type (`null` when the type has no recorded form), alongside `reason: unrecorded_constraint_shape` and `nextTool: inspect_sketch`.

Datum strings carry a unit: `"40 mm"`, `"30 mm"`, `"90 deg"`. The value lands in the property's internal unit.

## setDatums

The server converts each datum string to a native `FreeCAD.Units.Quantity` before it calls `setDatum`. A datum that is not a valid quantity fails with `VALIDATION_FAILED` before the transaction opens.

## setExpressions

`setExpressions` binds a FreeCAD expression to a datum constraint: each entry is `{"index": <final constraint index>, "expression": "..."}`. The server calls `setExpression("Constraints[index]", expression)`; an `expression` of `null` clears an existing binding. Expressions are at most 256 characters. `inspect_sketch` and `edit_sketch` return the live bindings in `expressionBindings`.

## Recipe: fully constrained rectangle

Verified on FreeCAD 1.1.3 with structured calls only. Create the Body and the origin-plane sketch as in [Common recipes](recipes.md), then add the profile with one batch:

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

Constrain it with a second batch:

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

Pad it with the `pad` payload in [Common recipes](recipes.md). The profile solves to `fullyConstrained: true` with `degreesOfFreedom: 0` and forms one closed wire of 40 x 30 mm; the verified result is one solid with volume `12000.0 mm3`. Remove one `Horizontal` and one `Vertical` entry when the profile must stay skewed.

### Confirm the profile

1. Read `inspect_sketch` again. Confirm the returned `addedGeometry` and `addedConstraints` indices, the geometry kinds, and the `datum` values.
2. Confirm the profile closes. The wires must come from coincident endpoints, not from near-identical coordinates.
3. Read the reported `degreesOfFreedom` and `fullyConstrained` fields. `degreesOfFreedom == 0` means fully constrained. Conflicting constraints can produce a negative `DoF` and state `['Touched', 'Invalid']`, because the native `addConstraint` accepts a conflicting entry (verified: two `DistanceX` datums of `10 mm` and `20 mm` produced solve status `-3`). `edit_sketch` rejects a sketch left in an invalid state; read `state`, `statusText`, and `solver.solverStatus` from either sketch response, and read [failure recovery](troubleshooting.md) to repair it.
4. Validate the solid after the profile becomes a feature. See [Geometry validation](validation.md).

## Scripted sketches

Stay with the structured tools when they cover the operation. Use `run_script` for external geometry, `Weight`, constraint names, driving and reference toggles, and datum edits through the native quantity API; read [the run_script contract](python-export.md). Read the native `sketch.Geometry` and `sketch.Constraints` to confirm the result, and apply this reference's index rules unchanged.

An unverified `Sketcher.Constraint` argument form can abort the FreeCAD process. Build constraints with the structured tool, or with a form recorded in `tests/native_contract.json`.

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
