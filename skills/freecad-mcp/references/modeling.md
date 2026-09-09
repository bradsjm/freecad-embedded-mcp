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

Prefer sketches attached to Body Origin planes or stable datum geometry. Avoid attaching critical sketches to generated faces when the model will be edited: face numbering can change after upstream edits (the [topological naming problem](https://wiki.freecad.org/Topological_naming_problem)).

### Draft and Sketcher

Use Draft for simple planar construction, annotations, working-plane geometry, arrays, and shape strings. Use Sketcher for constrained profiles used by Part or PartDesign. `create_object` and `run_script` can create many FreeCAD types even when a workbench GUI command is not exposed. Read [Draft Workbench](https://wiki.freecad.org/Draft_Workbench), [Sketcher Workbench](https://wiki.freecad.org/Sketcher_Workbench), and [Sketcher scripting](https://wiki.freecad.org/Sketcher_scripting).

If `create_object` fails with `not a document object type`, do not keep retrying the same type. Read the `supportedTypes` list from `discover_capabilities` and use a registered type or build the equivalent shape with `Part` in a `Part::Feature`.

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

## Start with a state snapshot

Never assume names or active-document state:

```python
# via run_script
print([(d.Name, d.FileName) for d in App.listDocuments().values()])
doc = App.ActiveDocument
if doc:
    print([(o.Name, o.Label, o.TypeId) for o in doc.Objects])
```

`inspect_objects(document)` covers object inspection; enumerate open documents with `App.listDocuments()` in the same script. Use the actual names returned by the tools. `Label` may be changed for display; `Name` is the stable internal reference for the current session.

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

Use explicit unit strings for quantities whose property type requires them (`"5 mm"`, `"100 N"`, `"210 GPa"`); assign these through `run_script` when the mapper's plain-number mapping is not precise enough. Read one object with `inspect_objects(document, detail="full")` before editing unfamiliar properties and copy the reported property convention.

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
