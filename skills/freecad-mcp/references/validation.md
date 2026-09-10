# Geometry validation

A successful MCP response only means the requested operation returned successfully. It does not prove that the result is a valid, correctly placed, single solid. Use this gate before export.

## 1. Recompute and inspect

After each meaningful modeling stage:

1. Call `inspect_objects(document)`; add `detail: "full"` with a `property_filter` when serialized properties are useful.
2. Read the final object's `typeId`, internal `name`, `state`, `placement`, `bounds`, `shape_valid`, and `solid_count` from the returned row.
3. Reject `Shape.error`, missing shape data, zero volume when a solid is expected, or an unexpected compound/multiple-solid result.
4. Check the final Body `Tip` if using PartDesign.

The MCP add-on itself recomputes after create/edit and rejects objects reporting `invalid`, `error`, or `touched` state, or `isValid()==False`. Treat that as a stop condition, not a warning. Call `validate_geometry(document, objects=["Final"])` for the structured report: state, validity, solid count, volume, bounds, `shape.check` diagnostics, and maximum tolerance, optionally against `expected_solids` and `expected_bounds`.

## 2. Validate the BRep

The English [Part Check Geometry](https://wiki.freecad.org/Part_CheckGeometry) page says the command verifies a BRep and reports whether a model is a valid solid. It can report topology counts and mass properties, and can run a Boolean-operation check. It does **not** automatically repair faults. FreeCAD's modeling history must be fixed.

Call `validate_geometry` for the programmatic check. Use a `run_script` check when deeper inspection is useful:

```python
obj = App.ActiveDocument.getObject("Final")
App.ActiveDocument.recompute()
print({
    "name": obj.Name,
    "type": obj.TypeId,
    "state": list(obj.State) if not isinstance(obj.State, str) else [obj.State],
    "is_valid": obj.Shape.isValid() if hasattr(obj, "Shape") else None,
    "shape_type": obj.Shape.ShapeType if hasattr(obj, "Shape") else None,
    "solids": len(obj.Shape.Solids) if hasattr(obj, "Shape") else None,
    "volume": obj.Shape.Volume if hasattr(obj, "Shape") else None,
})
```

Use the built-in Check Geometry command for deeper diagnostics. Do not infer that a positive volume alone means the model is a valid solid.

## 3. Enforce one solid

Prefer exactly one valid solid unless a multi-part result is intended. A compound may be intentional, but it should be an explicit decision because separate solids can overlap or float.

Check:

- `len(shape.Solids) == 1` for a single-solid deliverable.
- No unintended gaps or overlaps between fused pieces.
- No zero-thickness faces, self-intersections, sliver edges, or open shells.
- Holes are real through/blind cuts with an adequate wall around them.
- Features are not merely visual helpers hidden in the export selection.

When booleans leave redundant coplanar edges, consider a refined copy or `removeSplitter()` late in the graph. Validate again after refining. See [Part RefineShape](https://wiki.freecad.org/Part_RefineShape) and [Part Boolean](https://wiki.freecad.org/Part_Boolean).

## 4. Assert the expected global bounds


Compute the final shape's global bounding box. `validate_geometry` reports document-space bounds; use `expected_bounds` with `bounds_tolerance` to assert them. Placement and shape definition both affect the result, so check the actual global bounds, not only object dimensions. This `run_script` screen prints the raw values:

```python
obj = App.ActiveDocument.getObject("Final")
box = obj.Shape.BoundBox
print({
    "xmin": box.XMin, "xmax": box.XMax,
    "ymin": box.YMin, "ymax": box.YMax,
    "zmin": box.ZMin, "zmax": box.ZMax,
    "length": box.XLength, "width": box.YLength, "height": box.ZLength,
})
```

## 5. Review the result visually

Inspect at least `Bottom`, `Front`, `Top`, and `Isometric` views with `capture_view(document, focus_object=<final object>, view_name=...)` to catch accidental rotations, offsets, or missed features.

Read the capture against this list, and report the answer for each group:

**Shape and proportions**

- The overall form matches what the user asked for, not an approximation of it.
- No wall is thin enough to become a weak point. None is so thick that it wastes material or warps.
- Every requested feature appears in the view: each hole, slot, cutout, and boss.

**Printability**

- One large flat face is available for bed adhesion at the print orientation.
- No overhang above the printable angle is left unsupported without a stated reason. See [Printability and print orientation](printability.md).
- Every freestanding wall, pin, and tab is thick enough to print.
- Each boss and standoff connects to a wall or a rib instead of standing alone.
- Bottom edges are chamfered rather than filleted.

**Geometry quality**

- No feature is missing, doubled, or in the wrong place.
- Boolean results are clean, with no floating fragment and no leftover tool body in the export set.
- Each fillet and chamfer sits on the intended edge, and none failed silently.
- A hollow part is actually hollow, with the intended wall thickness.

**Dimensions**

- Each bound in the report matches the stated requirement. Each critical fit dimension matches the value recorded in the model.
- The exported selection contains the final object and not a helper, a sketch, or a per-feature intermediate.

Do not use screenshot appearance as a substitute for BRep validation. A view can look correct while the BRep carries an invalid or non-manifold region.

## 6. Mesh sanity before export

STL is a triangle mesh, not a parametric solid. For curved parts, choose a tessellation deviation small enough that the faceted surface does not affect fit or function. The [Export to STL or OBJ](https://wiki.freecad.org/Export_to_STL_or_OBJ) tutorial notes that default export settings may produce visibly jagged curves; verify current exporter/tessellation behavior programmatically in FreeCAD 1.1 with `FreeCAD.getExporters()`, `dir(MeshPart)`, and `supportedTypes()`.

If importing an existing mesh, read [Mesh to Part](https://wiki.freecad.org/Mesh_to_Part), [Part Shape From Mesh](https://wiki.freecad.org/Part_ShapeFromMesh), and [Part MakeSolid](https://wiki.freecad.org/Part_MakeSolid). Mesh repair tools can help with holes and normals, but they do not guarantee a valid BRep.

## 7. Final acceptance checklist

Before reporting the CAD export as validated:

- [ ] Correct document and final object identified by internal name.
- [ ] Final object recomputed and has no invalid/error/touched state.
- [ ] Intended object has a valid shape and positive volume.
- [ ] Single-solid requirement satisfied or multi-part intent explicitly confirmed.
- [ ] Bounding box matches the asserted expected bounds.
- [ ] Result reviewed through `capture_view` where visual evidence is useful.
- [ ] Print orientation chosen, with a flat bed face and no unexplained overhang. Recommendations reported.
- [ ] Every sourced real-world dimension records its source and date, and every assumed value is marked as assumed.
- [ ] Every mating interface has a named fit class and per-side clearance; uncalibrated values are reported as assumptions.
- [ ] Snap-fit arms/hooks have load orientation, root fillet, lead-in, and deflection/fatigue review.
- [ ] Fastener bosses, insert pilots, captive-nut pockets, or self-tapping pilots have vendor or material data, or are marked test heuristics.
- [ ] Export includes only intended geometry and the file was checked programmatically.

## Sources

- [Part Check Geometry](https://wiki.freecad.org/Part_CheckGeometry)
- [Part RefineShape](https://wiki.freecad.org/Part_RefineShape)
- [Part Boolean](https://wiki.freecad.org/Part_Boolean)
- [Mesh from Part Shape](https://wiki.freecad.org/Mesh_FromPartShape)
- [Mesh to Part](https://wiki.freecad.org/Mesh_to_Part)
- [Part Shape From Mesh](https://wiki.freecad.org/Part_ShapeFromMesh)
- [Part MakeSolid](https://wiki.freecad.org/Part_MakeSolid)
- [Export to STL or OBJ](https://wiki.freecad.org/Export_to_STL_or_OBJ)
