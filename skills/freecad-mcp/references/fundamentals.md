# FreeCAD fundamentals for MCP

Use this reference when a task involves document state, object types, properties, or the App/GUI boundary.

## Contents

- [App and GUI objects](#app-and-gui-objects)
- [Documents and object identity](#documents-and-object-identity)
- [Object types and properties](#object-types-and-properties)
- [Property types](#property-types)
- [Dependency queries](#dependency-queries)
- [Recompute discipline](#recompute-discipline)
- [Units and quantities](#units-and-quantities)
- [Sources](#sources)

## App and GUI objects

FreeCAD separates application data from its graphical representation. `App`/`FreeCAD` owns documents, objects, geometry definitions, and properties; `Gui`/`FreeCADGui` owns views and presentation. The [Document structure](https://wiki.freecad.org/Document_structure) and [FreeCAD Scripting Basics](https://wiki.freecad.org/FreeCAD_Scripting_Basics) pages describe this split.

For MCP work:

- Use `inspect_objects` to inspect application objects; request `detail: "full"` when serialized properties are useful.
- Use the structured tools (`create_object`, `create_objects`, `edit_object`, `edit_objects`, `edit_parameters`, `delete_object`) for covered document mutations; use `run_script` for `FreeCAD`/`App` and `FreeCADGui`/`Gui` operations the tools do not cover.
- Use `capture_view` for a screenshot rather than trying to treat a screenshot as geometry evidence. It needs an explicit orientation and a focus object and returns PNG image content.
- All document and GUI handlers run on the GUI thread. `run_fem`, `run_script`, `export`, and `measure` may detach under the Tasks extension; clients without that extension receive blocking results.

## Documents and object identity

A document contains the objects in a scene and is what FreeCAD saves to disk. It can contain groups and objects from multiple workbenches. Documents may have multiple views and only one active document at a time.

When `allow_scripts` is enabled and direct Python is useful, the same state looks like this:

```python
import FreeCAD as App

doc = App.ActiveDocument or App.newDocument("BuiltPart")
print(doc.Name, doc.FileName)
print([(obj.Name, obj.Label, obj.TypeId) for obj in doc.Objects])
```

Use MCP `new_document` for a simple new document. Call `inspect_documents` to list the open documents: it takes no arguments and returns one row per document with its name, label, file path, object count, generation, dirty/active flags, transaction state, and the object under GUI edit, plus the `activeDocument`. Use `reload_document` only after an external process edited the associated file; it asks for consent when the document is dirty.

Object `Name` is the internal identifier used for links and MCP follow-up calls. `Label` is display text and may change. FreeCAD sanitizes and de-duplicates names; every create response returns the actual name. Never assume the requested name survived unchanged.

## Object types and properties

FreeCAD modules register document object types. `discover_capabilities` reports the installation's complete `supportedTypes` list. With `allow_scripts` enabled, the same inspection through `run_script` looks like this:

```python
print("Part::Box" in App.ActiveDocument.supportedTypes())
print([t for t in App.ActiveDocument.supportedTypes() if t.startswith(("Part::", "PartDesign::", "Draft::", "Fem::"))])
```

`create_object` uses `doc.addObject(type, name)` for generic Part/App types and an explicit `ObjectsFem` factory mapping for FEM types. The registered object type determines which properties exist. Inspect `PropertiesList` through `inspect_objects(detail="full")` before editing unfamiliar objects.


A Part feature stores BRep geometry in `Shape`; a mesh feature stores mesh data in `Mesh`; a Body stores a feature history and exposes a `Tip`. Do not assign a mesh to a Shape property or overwrite a parametric feature casually.

## Property types

The value shape must match the property type. `create_object`, `create_objects`, `edit_object`, and `edit_objects` map these forms.

| Property type | Send |
|---|---|
| `App::PropertyBool`, `Integer`, `Float` | JSON boolean, integer, or number. |
| `App::PropertyString` | String. |
| `App::PropertyLength`, `Distance`, `Angle`, `Quantity`, `Area`, `Volume`, `Speed`, `Percent` | A finite JSON number in the property's internal unit. A unit string such as `"5 mm"` is rejected. Strip the unit, or use `run_script`. |
| `App::PropertyVector`, `VectorDistance`, `Direction` | `[x, y, z]` or `{"x": n, "y": n, "z": n}`. |
| `App::PropertyPlacement` | `{"position": [x, y, z], "axis": [x, y, z], "angle_deg": n}`. |
| `App::PropertyLink`, `LinkSub`, `XLink`, `XLinkSub` | `{"object": "<Name>", "subelement": ""}`. A link-sub may carry `"Face1"`. |
| `App::PropertyLinkList`, `LinkSubList`, `XLinkList`, `XLinkSubList` | Array of the link form. |
| `App::PropertyColor` | `[r, g, b]` or `[r, g, b, a]`. |
| `App::PropertyEnumeration` | The exact allowed string. |
| `Part::PropertyPartShape` and other unmapped types | The raw JSON value passes through and FreeCAD rejects most of them. Assign a shape through `run_script`. |

Every mapped number must be a JSON number, not a numeric string, and must be finite.

Object bounds use `[xmin, ymin, zmin, xmax, ymax, zmax]` order in document coordinates.

`edit_parameters` adds dynamic properties, renames dynamic properties, and binds or clears expressions. The added property types are `App::PropertyBool`, `Integer`, `Float`, `String`, `Length`, `Distance`, `Angle`, `Vector`, `Color`, `StringList`, `FloatList`, and `IntegerList`. It refuses to rename a built-in property.

A `Part::FeaturePython` object keeps a custom property such as `Side` across a save and reload. Its Python proxy does not survive unless the proxy class is importable from an installed module.

## Dependency queries

Use the dependency lists before a delete or a repair. `delete_object` refuses an object that has dependents. These queries show which objects those are.

```python
obj.OutList            # objects this object references
obj.InList             # objects that reference this object
obj.InListRecursive    # full set of ancestors
```

Verified on a `Part::Cut` with `Base` and `Tool` links: `Result.OutList` reports `['Base', 'Tool']`, and `Base.InList` reports `['Result']`. `inspect_objects` also reports a `links` field per row.

## Recompute discipline

FreeCAD marks dependent features for recomputation after a property or geometry change. Recompute explicitly in scripts before inspecting metrics or exporting:

```python
doc.recompute()
print(obj.State, obj.Shape.isValid())
```

`create_object`, `create_objects`, `edit_object`, `edit_objects`, `edit_parameters`, and `delete_object` recompute inside MCP-owned transactions and validate the edited objects, their dependents, and solid-count baselines. A successful tool response is not permission to ignore an invalid state; inspect the returned geometry report.

## Units and quantities

Use millimetres for CAD dimensions and state the unit assumption. FreeCAD quantity properties accept plain numbers through `edit_object` in the property's internal unit, or explicit strings such as `"5 mm"`, `"100 N"`, `"210 GPa"` through `run_script`. STL has no unit metadata; the FreeCAD export guidance assumes millimetres.

The unit API, verified on FreeCAD 1.1.3:

```python
App.Units.Quantity("1 in")                 # 25.4, in the internal length unit
App.Units.Quantity("1 in").getValueAs("mm")  # "25.4" (a string)
App.Units.parseQuantity("2.5 in")          # 63.5 mm
float(App.Units.parseQuantity("2.5 in"))   # 63.5
```

`float()` on a quantity yields the internal unit value. Constraint datums and native property assignments in `run_script` need a `Quantity`, not a bare string.

## Sources

- [Document structure](https://wiki.freecad.org/Document_structure)
- [FreeCAD Scripting Basics](https://wiki.freecad.org/FreeCAD_Scripting_Basics)
- [Property](https://wiki.freecad.org/Property)
- [Property editor](https://wiki.freecad.org/Property_editor)
- [Object name](https://wiki.freecad.org/Object_name)
- [Units](https://wiki.freecad.org/Units)
- [Quantity](https://wiki.freecad.org/Quantity)
- [FreeCAD 1.1 release notes](https://wiki.freecad.org/Release_notes_1.1)
