# Parametric and scripted workflows

Use this reference to choose a stable modeling history and to recover from feature failures.

## PartDesign workflow

A PartDesign Body represents one component and contains cumulative features. Shapeless and null-shape objects are valid, so the whole chain can be built with the structured tools; no `run_script` bootstrap is needed. A typical sequence is:

1. Create the Body with `create_object` and type `PartDesign::Body`; it reports `solid_count: 0` until it holds a solid.
2. Create the sketch and its attachment with `create_feature` (`kind: "sketch"`, a `support`, and an explicit `MapMode`).
3. Constrain a closed profile with `edit_sketch`. See [Sketcher profiles](sketcher.md).
4. Pad or revolve it into a base with `create_feature` (`kind: "pad"` and `profile`, or `kind: "revolve"` with an axis). `create_feature` acts on an empty Body.
5. Add pockets, holes, additive/subtractive features, patterns, dress-ups, primitives, helix sweeps, binders, and transforms with `create_feature`.
6. Keep fillets/chamfers/thickness late where possible.
7. Inspect the Body `Tip` after each feature.

The [PartDesign Workbench](https://wiki.freecad.org/PartDesign_Workbench), [PartDesign Body](https://wiki.freecad.org/PartDesign_Body), [PartDesign Pad](https://wiki.freecad.org/PartDesign_Pad), and [PartDesign Pocket](https://wiki.freecad.org/PartDesign_Pocket) pages are the primary references.

PartDesign is best when the user wants editable design intent. It is not automatically the best representation for every object; a scripted Part feature can be easier to validate and reproduce.

## Sketcher scripting

Sketcher geometry and constraints are index-based. A wrong index either fails the operation or silently constrains the wrong element. Use `inspect_sketch` to read the current rows. Use `edit_sketch` to change them in one atomic batch. Read [Sketcher profiles through MCP](sketcher.md) for the index model, the per-type constraint arguments, the verified rectangle recipe, and the `setDatums` limitation. That reference records the FreeCAD 1.1.3 findings.

The exact sketch object type and feature integration depend on the current FreeCAD registration. Query `supportedTypes()` and inspect the running installation if a direct `create_object` call fails.

## Topological naming risk

Generated face/edge names are not a stable design API. A change to an upstream feature can change which geometry is `Face1` or `Edge3`. Prefer Origin planes, explicit datums, master sketches, and stable external references. If a face reference is unavoidable, validate it after every upstream edit and before export.

See [Topological naming problem](https://wiki.freecad.org/Topological_naming_problem).

## Draft workflow

Draft objects are useful for planar construction, working-plane geometry, arrays, and shape strings. The [Draft Workbench](https://wiki.freecad.org/Draft_Workbench) and [Draft scripting/API](https://wiki.freecad.org/Draft_API) pages document the available commands and scripting, but the wiki marks the API page as outdated. In FreeCAD 1.1, verify the current object type and function signature in the running Python help.

The MCP’s generic `create_object` path may reject an apparently documented Draft type if the module is not registered in the current session. Do not treat that as a reason to mutate the document with guessed properties; use `supportedTypes()` or a deterministic Part shape instead.

## Python objects and proxy persistence

A `Part::FeaturePython` object stores its custom properties in the document. Its Python proxy class is not part of the file. Verified on FreeCAD 1.1.3: after a save and reopen, the custom property and the generated `Shape` survived. The value of `obj.Proxy` was `None` until some installed module rebound the class.

Treat a proxy object as session state:

- Do not promise a reloadable parametric object from a proxy defined only in a `run_script` session. Put the class in a module on the FreeCAD path when it must survive a reload.
- Prefer native registered types, or a plain `Part::Feature` with an assigned shape, when the model must reopen predictably.
- Do not rebind a foreign proxy. Inspect the object first and report the missing proxy instead of overwriting it.

## Editing an existing model

1. Snapshot documents and objects.
2. Identify the final object/Body Tip and its dependencies.
3. Change the smallest property set needed.
4. Recompute.
5. Inspect state, shape metrics, bounds, and view.
6. Validate the final solid.
7. Save a new `.FCStd` if preserving the original is important.

Do not delete an intermediate feature to silence an error. Find the first invalid dependency and correct its input, support, profile, placement, or dimensions.

## Sources

- [PartDesign Workbench](https://wiki.freecad.org/PartDesign_Workbench)
- [PartDesign Body](https://wiki.freecad.org/PartDesign_Body)
- [Feature editing](https://wiki.freecad.org/Feature_editing)
- [PartDesign Pad](https://wiki.freecad.org/PartDesign_Pad)
- [PartDesign Pocket](https://wiki.freecad.org/PartDesign_Pocket)
- [Sketcher Workbench](https://wiki.freecad.org/Sketcher_Workbench)
- [Sketcher scripting](https://wiki.freecad.org/Sketcher_scripting)
- [Sketcher SketchObject](https://wiki.freecad.org/Sketcher_SketchObject)
- [Draft Workbench](https://wiki.freecad.org/Draft_Workbench)
- [Draft API](https://wiki.freecad.org/Draft_API)
- [Topological naming problem](https://wiki.freecad.org/Topological_naming_problem)
