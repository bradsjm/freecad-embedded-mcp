# FreeCAD modeling patterns

Use this file to choose a representation and build a robust dependency graph. The English wiki pages linked here are the durable source references.

## Choose the representation

### Part / scripted Part: deterministic CSG

Use Part primitives and booleans when the model is naturally a set of independent solids, when exact shape construction is more important than a GUI-style feature history, or when a scripted operation is easier to reproduce. Typical types include:

- `Part::Box`, `Part::Cylinder`, `Part::Sphere`, `Part::Cone`, `Part::Torus`, `Part::Wedge`.
- `Part::Feature` for a shape assigned from Python.
- Boolean and feature objects such as `Part::Cut`, `Part::Fuse`, `Part::Common`, `Part::Fillet`, `Part::Chamfer`, `Part::Extrusion`, and `Part::Revolution` when their property schema is known.

The [Part Workbench](https://wiki.freecad.org/Part_Workbench) uses constructive solid geometry: each object is an independent solid and booleans combine or subtract them. The [Part scripting](https://wiki.freecad.org/Part_scripting) and [Topological data scripting](https://wiki.freecad.org/Topological_data_scripting) pages are better references for deterministic Python than guessing generic property payloads.

### PartDesign: one coherent parametric component

Use PartDesign when the result should be one coherent component with a feature history. A `PartDesign::Body` owns the sequence; its `Tip` is the exposed result. Common features are `Pad`, `Pocket`, `Revolution`, `Hole`, `Fillet`, `Chamfer`, dress-up, and pattern features. Read [PartDesign Workbench](https://wiki.freecad.org/PartDesign_Workbench), [PartDesign Body](https://wiki.freecad.org/PartDesign_Body), and [Feature editing](https://wiki.freecad.org/Feature_editing).

Build the whole chain with the structured tools. Shapeless objects are valid, so the empty Body and the empty sketch need no bootstrap:

1. Create the Body: `create_object` with type `PartDesign::Body`.
2. Create the sketch and its attachment: `create_feature` with `kind: "sketch"`, `support: {"object": "XY_Plane"}`, and `properties: {"MapMode": "FlatFace"}`.
3. Draw the profile: `edit_sketch` with `addGeometry`.
4. Constrain the profile: `edit_sketch` with `addConstraints`. Read `addedGeometry` from step 3 so the indices are real.
5. Pad it: `create_feature` with `kind: "pad"` and `profile` set to the sketch.

```json
{
  "document": "Part",
  "body": "Body",
  "kind": "pad",
  "name": "Pad",
  "profile": "Profile",
  "properties": {"Length": 10}
}
```

See [Sketcher profiles](sketcher.md) for the constraint arguments and the full rectangle recipe.

Prefer sketches attached to Body Origin planes or stable datum geometry. Avoid attaching critical sketches to generated faces when the model will be edited: face numbering can change after upstream edits (the [topological naming problem](https://wiki.freecad.org/Topological_naming_problem)).

### Draft and Sketcher

Use Draft for simple planar construction, annotations, working-plane geometry, arrays, and shape strings. Use Sketcher for constrained profiles used by Part or PartDesign. `create_object` and `run_script` can create many FreeCAD types even when a workbench GUI command is not exposed. Read [Draft Workbench](https://wiki.freecad.org/Draft_Workbench), [Sketcher Workbench](https://wiki.freecad.org/Sketcher_Workbench), and [Sketcher scripting](https://wiki.freecad.org/Sketcher_scripting).

If `create_object` fails with `not a document object type`, do not keep retrying the same type. Read the `supportedTypes` list from `discover_capabilities` and use a registered type or build the equivalent shape with `Part` in a `Part::Feature`.

## Establish the requirements before the geometry

Resolve these before the first feature. Ask only the ones that change the geometry, and ask them a few at a time rather than as one list. Use a stated default and say so when the user has no preference.

1. **What is it, and what does it hold or attach to?** Get a concrete mental model. For example: a bracket for a specific motor, a case for a specific phone, a tray for a specific slot.
2. **Which dimensions are non-negotiable?** Board outline, screw spacing, the diameter of the part it wraps, the device footprint. These drive every other dimension.
3. **How does it attach?** Bolted, threaded insert, self-tapping screw, snap fit, adhesive, magnet, or freestanding. This sets wall thickness, boss geometry, and the load path.
4. **How will the part be made?** When fused-filament fabrication will constrain the geometry, ask for the material, nozzle, layer height, build volume, and relevant calibration data. See [Design for fused-filament fabrication](printability.md).
5. **Which functional requirements matter?** Airflow, cable routing, water resistance, an access panel, a visibility window, stacking, or a weight limit. Ask only the relevant ones.
6. **Any aesthetic direction?** Rounded compared with sharp, minimal compared with industrial. Ask briefly. Function usually outranks form.

A non-negotiable fit dimension is a correctness input, not a preference. Never guess one, and never quietly round it.

## Source the fit dimensions of real products

When the part interfaces with an existing product, a connector, or a device, obtain the real dimensions before you write geometry. A small error makes the part unusable, and the error is invisible in a render.

Method:

1. Search for the exact product or component with an explicit unit, for example `"<product> dimensions mm"`, `"<component> mechanical drawing"`, or `"<component> datasheet"`.
2. Prefer a primary source: the manufacturer datasheet, the mechanical drawing, or a published standard. A vendor drawing beats a blog post.
3. Cross-check at least two independent sources when the fit is tight, and record both.
4. Verify that the source matches the exact variant, revision, and generation of the product.
5. Record each sourced value as a named parameter with its source and date next to it.
6. Convert and check the unit. Many drawings publish inches.
7. Add a per-side clearance for a fit, and mark it uncalibrated until a test print confirms it.

```python
# Interface dimensions researched 2026-09-09.
usb_c_opening_w = 8.34    # USB Type-C receptacle opening, per USB-IF Type-C spec
usb_c_opening_h = 2.56    # mm; the opening runs 6.20 mm deep
usb_c_wall_clearance = 0.35   # per side; UNVERIFIED until a test print
```

Two worked corrections, because both values are widely repeated in a wrong form:

| Interface | Verified value | Why the common figure misleads |
|---|---|---|
| USB Type-C receptacle opening | 8.34 mm × 2.56 mm, 6.20 mm deep | The popular "8.4 × 2.6" is rounded. The depth is usually omitted, and the depth controls how far the connector body inserts. |
| Apple 25 W MagSafe charger (A2580) | 55.5 mm diameter, 4.45 mm thick | Measured in a 2024 teardown. The circulating "56 mm × 5.6 mm" is not this product's measured value. A magnetic mount also needs a recess depth and a cable-exit clearance, which a bare diameter does not describe. |

These two cases show the rule: a number without a named source, a variant, and a date is a guess. State the number, its source, and its uncertainty. Otherwise leave it as an explicitly named parameter for the user to confirm.

For electronic components specifically, also read [Electronic component models](electronic-components.md). Use the manufacturer datasheet, and treat an imported model as an envelope rather than a complete keep-out. Model connector openings, mating travel, and cable bend separately from the visible shell.

Never present a researched dimension as a measured one. Distinguish a datasheet value, a third-party measurement, a user measurement, and your own estimate.

## Design decisions before detailing

Before creating detailed features, record the intended orientation, principal load direction, wall-width candidates, and fit-clearance parameters. Prefer geometry that keeps load paths continuous. Replace flat unsupported shelves and roofs with chamfers, arches, teardrop roofs, or ribs where the geometry allows; add root fillets to hooks, snap arms, bosses, and cantilevers; add lead-in chamfers where insertion matters.

Test wall candidates as a small set of named parameters (for example 1.2/1.8/2.4 mm) rather than arbitrary thin dimensions. Treat 0.30 mm per side as a conservative starting clearance for moving parts and expose it as a named parameter; do not present either value as a universal limit.

## Build in dependency order

Use this order unless the requested model requires a different graph:

1. Create or identify the target document.
2. Create the base solid or Body.
3. Create stable profiles, sketches, datum references, or helper solids.
4. Add additive features.
5. Add subtractive features such as holes and pockets.
6. Add booleans, patterns, fillets, chamfers, shell/thickness, and cosmetic features late.
7. Set final placement and visibility.
8. Recompute, inspect, validate, and export.

Keep intermediate helpers named clearly (`Base`, `CutTool`, `MountingSketch`, `FinalSolid`). Hide helpers instead of deleting them until the final result is verified. For an export, select exactly the intended final object or use a script that exports exactly that object.

## Build in reviewable stages

Build a shape, verify it, and save a checkpoint before you add the next layer of detail. Do not write the whole model in one script and validate only at the end. A late failure hides its cause, and the user cannot steer a design they have not seen.

| Stage | Build | Verify before continuing |
|---|---|---|
| 1. Base form | Outer envelope, walls, base plate. No cutouts, no fillets. | Overall bounds match the requirements. A flat face lies on the bed. |
| 2. Features | Holes, cutouts, bosses, slots, vents, internal structure. | Each feature is present and correctly placed. Booleans are clean. |
| 3. Finish | Fillets, chamfers, edge cleanup, cosmetic detail. | Final geometry validation, view review, and the printability checks. |

After each stage:

1. Run `capture_view` from at least `Isometric`, `Top`, and `Front`.
2. Run `validate_geometry` for the bounds, solid count, and validity.
3. Save the milestone with `save_document` before the next stage. See [checkpoint and crash recovery](troubleshooting.md#checkpoint-and-crash-recovery).

Show the user the stage result and the key dimensions, then continue. Treat the user's design approval as a real decision point. A wrong overall envelope or mounting layout is expensive to change after detail work. Do not stall on a stage the user already specified or approved. Do not ask for approval of a detail the requirements already decide.

Add finishing features largest first and late in the graph. Apply fillets after the shell or thickness operation, and revalidate after each dress-up, because a fillet changes the topology that later features reference.

## Start with a state snapshot

Never assume names or active-document state. Call `inspect_documents` first; it takes no arguments and reports every open document with its generation, dirty/active flags, and the active document. Then inspect the target's objects:

Call `inspect_objects(document)`; request `detail: "full"` when serialized properties are useful. Use the actual names returned by the tools. `Label` may be changed for display; `Name` is the stable internal reference for the current session.

## Constructing scripted shapes

For deterministic geometry, use `Part.make*` and assign the resulting shape to a feature:

```python
import FreeCAD as App
import Part

doc = App.ActiveDocument or App.newDocument("BuiltPart")
base = doc.addObject("Part::Feature", "Base")
base.Label = "Base"
base.Shape = Part.makeBox(40, 30, 5)

hole_tool = Part.makeCylinder(4, 5, App.Vector(20, 15, 0))
base.Shape = base.Shape.cut(hole_tool)
doc.recompute()
assert base.Shape.isValid()
```

For a model with several named construction steps, keep the inputs as document objects and use `Part::Cut`/`Part::Fuse` links or separate `Part::Feature` objects. Use `removeSplitter()`/Refine only after confirming that it does not remove useful edges needed downstream.

Do not use an arbitrary `Part::Feature` for a result that must remain natively editable as a PartDesign feature; use the native feature or document the scripted shape as the intended final artifact.

## Links and references

The property mapper accepts links only in the canonical form `{"object": "<Name>", "subelement": ""}`, or `{"object": "<Name>", "subelement": "Face3"}` for link-sub properties; arrays of these values fill link lists. `edit_parameters` adds dynamic properties and binds FreeCAD expression strings to property names.

For sketch geometry, GUI-managed fields, or feature-specific properties the mapper rejects, use `run_script`, inspect the object's `PropertiesList`, and assign the correct FreeCAD object/property type. Do not send a Label where an internal Name is required.

## Placement and attachment

Use `Placement` for an un-attached object’s global location/orientation. A generic MCP payload is:

```json
{
  "Placement": {
    "position": [10, 5, 0],
    "axis": [0, 0, 1],
    "angle_deg": 90
  }
}
```

The [Placement](https://wiki.freecad.org/Placement) page distinguishes position from rotation and explains that the internal rotation is represented as a quaternion. The [Attachment](https://wiki.freecad.org/Part_EditAttachment) page explains attachment modes and offsets.

For an attached sketch or feature, edit its attachment support/offset rather than fighting the derived global `Placement`. Keep the support stable: Origin planes and datum geometry are safer than generated faces for long-lived parametric parts.

## Units and names

Quantity properties reached through `create_object`, `edit_object`, and `edit_objects` take plain JSON numbers in the property's internal unit; the mapper rejects unit strings such as `"5 mm"`. Assign explicit unit strings (`"5 mm"`, `"100 N"`, `"210 GPa"`) only through `run_script`, or inside string maps such as a FEM `Material` map, whose values are strings. Read one object with `inspect_objects(document, detail="full")` before editing unfamiliar properties and copy the reported property convention.

Avoid spaces and punctuation in requested internal names. Use concise ASCII names with a semantic role. A descriptive `Label` can be longer.

## FreeCAD 1.1 compatibility

Read [Release notes 1.1](https://wiki.freecad.org/Release_notes_1.1) when a feature is version-sensitive. FreeCAD 1.1 changed PartDesign Body Origin datum orientation; older files may need conversion on open, and files created by 1.1 may not be safe to reopen in older FreeCAD versions. Do not promise backward compatibility.

## Sources

- [Part Workbench](https://wiki.freecad.org/Part_Workbench)
- [Part scripting](https://wiki.freecad.org/Part_scripting)
- [Topological data scripting](https://wiki.freecad.org/Topological_data_scripting)
- [Part and PartDesign](https://wiki.freecad.org/Part_and_PartDesign)
- [PartDesign Workbench](https://wiki.freecad.org/PartDesign_Workbench)
- [PartDesign Body](https://wiki.freecad.org/PartDesign_Body)
- [Feature editing](https://wiki.freecad.org/Feature_editing)
- [Topological naming problem](https://wiki.freecad.org/Topological_naming_problem)
- [Draft Workbench](https://wiki.freecad.org/Draft_Workbench)
- [Sketcher Workbench](https://wiki.freecad.org/Sketcher_Workbench)
- [Sketcher scripting](https://wiki.freecad.org/Sketcher_scripting)
- [Placement](https://wiki.freecad.org/Placement)
- [Part Attachment](https://wiki.freecad.org/Part_EditAttachment)
- [Release notes 1.1](https://wiki.freecad.org/Release_notes_1.1)
