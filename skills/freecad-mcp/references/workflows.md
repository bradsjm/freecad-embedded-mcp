# Parametric and scripted workflows

Use this reference to choose a stable modeling history and to recover from feature failures.

## PartDesign workflow

A PartDesign Body represents one component and contains cumulative features. A typical sequence is:

1. Create a `PartDesign::Body`.
2. Create a sketch on an Origin plane or stable datum.
3. Constrain a closed profile.
4. Pad or revolve it into a base.
5. Add pockets, holes, additive/subtractive features, patterns, and dress-ups.
6. Keep fillets/chamfers/thickness late where possible.
7. Inspect the Body `Tip` after each feature.

The [PartDesign Workbench](https://wiki.freecad.org/PartDesign_Workbench), [PartDesign Body](https://wiki.freecad.org/PartDesign_Body), [PartDesign Pad](https://wiki.freecad.org/PartDesign_Pad), and [PartDesign Pocket](https://wiki.freecad.org/PartDesign_Pocket) pages are the primary references.

PartDesign is best when the user wants editable design intent. It is not automatically the best representation for every object; a scripted Part feature can be easier to validate and reproduce.

## Sketcher scripting

Sketcher geometry and constraints are indexed. When using `run_script`, create the sketch, add geometry, then add constraints that refer to the correct geometry indices. Recompute and inspect solver state before using the sketch as a PartDesign profile.

```python
import Sketcher
import Part
from FreeCAD import Vector

sketch = doc.addObject("Sketcher::SketchObject", "Profile")
geo = sketch.addGeometry(
    [
        Part.LineSegment(Vector(0, 0, 0), Vector(40, 0, 0)),
        Part.LineSegment(Vector(40, 0, 0), Vector(40, 30, 0)),
        Part.LineSegment(Vector(40, 30, 0), Vector(0, 30, 0)),
        Part.LineSegment(Vector(0, 30, 0), Vector(0, 0, 0)),
    ],
    False,
)
# Add only constraints whose indices and geometry are known.
doc.recompute()
```

The exact sketch object type and feature integration depend on the current FreeCAD registration. Query `supportedTypes()` and inspect the running installation if a direct `create_object` call fails.

Sources: [Sketcher Workbench](https://wiki.freecad.org/Sketcher_Workbench), [Sketcher scripting](https://wiki.freecad.org/Sketcher_scripting), [Sketcher SketchObject](https://wiki.freecad.org/Sketcher_SketchObject), and [Sketcher constraints](https://wiki.freecad.org/Sketcher_ConstrainCoincident).

## Topological naming risk

Generated face/edge names are not a stable design API. A change to an upstream feature can change which geometry is `Face1` or `Edge3`. Prefer Origin planes, explicit datums, master sketches, and stable external references. If a face reference is unavoidable, validate it after every upstream edit and before export.

See [Topological naming problem](https://wiki.freecad.org/Topological_naming_problem).

## Draft workflow

Draft objects are useful for planar construction, working-plane geometry, arrays, and shape strings. The [Draft Workbench](https://wiki.freecad.org/Draft_Workbench) and [Draft scripting/API](https://wiki.freecad.org/Draft_API) pages document the available commands and scripting, but the wiki marks the API page as outdated. In FreeCAD 1.1, verify the current object type and function signature in the running Python help.

The MCP’s generic `create_object` path may reject an apparently documented Draft type if the module is not registered in the current session. Do not treat that as a reason to mutate the document with guessed properties; use `supportedTypes()` or a deterministic Part shape instead.

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
