# FreeCAD MCP recipes

Use this file for common payloads. Replace names, dimensions, paths, and expectations with design values. Read `mcp-tools.md` for full schemas.

## Contents

- [Start or select a document](#start-or-select-a-document)
- [Create and edit a primitive](#create-and-edit-a-primitive)
- [Create independent objects atomically](#create-independent-objects-atomically)
- [Create a PartDesign pad](#create-a-partdesign-pad)
- [Add a pocket or hole](#add-a-pocket-or-hole)
- [Add parameters and expressions](#add-parameters-and-expressions)
- [Write and verify spreadsheet cells](#write-and-verify-spreadsheet-cells)
- [Inspect topology and measure fit](#inspect-topology-and-measure-fit)
- [Import a reference model](#import-a-reference-model)
- [Validate save and export](#validate-save-and-export)
- [Run FEM](#run-fem)
- [Use the Python escape hatch](#use-the-python-escape-hatch)

## Start or select a document

For new work:

```json
{"name":"Bracket"}
```

Call `new_document`. Reuse its returned `name` as `document`.

For existing work, call `inspect_documents` with `{}`. Then call:

```json
{"document":"Bracket","detail":"compact"}
```

Use `inspect_objects`. Add a nonempty `objects` list for an explicit selection. Use `detail:"full"` and `property_filter` only for needed properties.

To open an FCStd file:

```json
{"path":"/absolute/allowed/path/Bracket.FCStd","untrusted":true}
```

Use `open_document`. Expect consent for untrusted input.

## Create and edit a primitive

Create one supported document object:

```json
{
  "document":"Bracket",
  "type":"Part::Box",
  "name":"Base",
  "properties":{"Length":40,"Width":30,"Height":5},
  "expected_solids":1,
  "expected_bounds":[0,0,0,40,30,5]
}
```

Use `create_object`. Reuse the returned object name.

Edit only known properties:

```json
{
  "document":"Bracket",
  "object":"Base",
  "properties":{"Height":6},
  "expected_solids":1,
  "expected_bounds":[0,0,0,40,30,6],
  "bounds_tolerance":0.01,
  "response_detail":"compact"
}
```

Use `edit_object`. Inspect with `detail:"full"` before an unfamiliar property edit.

Delete only an unneeded leaf object:

```json
{"document":"Bracket","object":"CutTool"}
```

Use `delete_object`. If it reports dependents, repair the graph instead of cascading deletion.

## Create independent objects atomically

```json
{
  "document":"Bracket",
  "entries":[
    {"type":"Part::Box","name":"Left","properties":{"Length":10,"Width":20,"Height":4}},
    {"type":"Part::Box","name":"Right","properties":{"Length":10,"Width":20,"Height":4}}
  ],
  "expectations":{
    "Left":{"expected_solids":1},
    "Right":{"expected_solids":1}
  },
  "response_detail":"compact"
}
```

Use `create_objects`. Read `nameMapping`. Then add cross-object links with `edit_objects`:

```json
{
  "document":"Bracket",
  "edits":[
    {"object":"Right","properties":{"Placement":{"position":[30,0,0],"axis":[0,0,1],"angle_deg":0}}}
  ],
  "response_detail":"compact"
}
```

Do not put sibling links in `create_objects`.

## Create a PartDesign pad

Create the Body:

```json
{"document":"Bracket","type":"PartDesign::Body","name":"Body"}
```

Create an origin-plane sketch:

```json
{
  "document":"Bracket",
  "body":"Body",
  "kind":"sketch",
  "name":"Profile",
  "parameters":{"plane":"xy"}
}
```

Add a semantic rectangle:

```json
{
  "document":"Bracket",
  "sketch":"Profile",
  "addGeometry":[{"kind":"rectangle","origin":[0,0],"width":40,"height":30}]
}
```

Call `inspect_sketch`. Use its real geometry and constraint indices for later edits. Read `sketcher.md` when the profile must be fully constrained.

Create the pad:

```json
{
  "document":"Bracket",
  "body":"Body",
  "kind":"pad",
  "name":"Pad",
  "profile":"Profile",
  "parameters":{"extent":"distance","length":10},
  "expected_solids":1,
  "expected_bounds":[0,0,0,40,30,10]
}
```

Prefer typed `parameters`. Do not combine `parameters` with raw `properties`.

Edit a supported scalar feature after inspection:

```json
{
  "document":"Bracket",
  "body":"Body",
  "object":"Pad",
  "parameters":{"extent":"distance","length":12},
  "expected_generation":4,
  "expected_solids":1,
  "response_detail":"compact"
}
```

Use the current document generation, not the example value.

## Add a pocket or hole

Create another Body sketch and profile before a pocket:

```json
{
  "document":"Bracket",
  "body":"Body",
  "kind":"pocket",
  "name":"Pocket",
  "profile":"CutProfile",
  "parameters":{"extent":"through_all"},
  "expected_solids":1
}
```

For a sketch-driven counterbore:

```json
{
  "document":"Bracket",
  "body":"Body",
  "kind":"hole",
  "name":"MountHole",
  "profile":"HoleCenters",
  "parameters":{
    "diameter":4.5,
    "depth_type":"through_all",
    "cut":"counterbore",
    "counterbore_diameter":8,
    "counterbore_depth":3,
    "thread":"none"
  },
  "expected_solids":1
}
```

Use `inspect_topology` before a fillet, chamfer, thickness, or face-bound feature. Use its signed references when the tool accepts them.

## Add parameters and expressions

For an independent parameter host, create `App::VarSet` when `discover_capabilities` lists it:

```json
{"document":"Bracket","type":"App::VarSet","name":"Parameters"}
```

```json
{
  "document":"Bracket",
  "object":"Parameters",
  "add":[
    {"name":"Wall","type":"App::PropertyLength","value":2.4}
  ]
}
```

Bind a dependent scalar property:

```json
{
  "document":"Bracket",
  "object":"Base",
  "expressions":{"Height":"Parameters.Wall * 2"}
}
```

Use `edit_parameters`. Use it for dynamic properties and expression bindings. Use `edit_object` for ordinary native property values.

## Write and verify spreadsheet cells

Write native cell content as strings through `properties.cells`:

```json
{
  "document":"Bracket",
  "object":"ParametersSheet",
  "properties":{"cells":{"B2":"2.4 mm","B3":"=B2 * 2"}},
  "response_detail":"compact"
}
```

Require `cellContentsPersisted: true` in the change result. Then inspect only the sheet with `detail:"full"` and a targeted property page. Confirm each cell's content, formula, alias, evaluated value, and error.

## Inspect topology and measure fit

Get stable data for selected faces:

```json
{"document":"Assembly","object":"Housing","role":"faces","indices":[1,4],"detail":"full"}
```

Use `inspect_topology`. Reinspect after an upstream geometry change because signed references bind to a generation.

Check two mating objects with both modes:

```json
{"document":"Assembly","a":"Insert","b":"Housing","mode":"distance"}
```

```json
{"document":"Assembly","a":"Insert","b":"Housing","mode":"interference"}
```

Use `measure`. Require the intended minimum distance and zero unintended common volume.

## Import a reference model

```json
{
  "document":"Enclosure",
  "path":"/absolute/allowed/path/connector.step",
  "format":"step"
}
```

Use `import_model`. Expect consent. Use the returned created object names. Validate bounds against the source drawing before you design around the model.

For STL, set `format:"stl"` and optionally set `name`.

## Validate save and export

Validate the final object:

```json
{
  "document":"Bracket",
  "objects":["Body"],
  "expected_solids":1,
  "expected_bounds":{"Body":[0,0,0,40,30,12]},
  "bounds_tolerance":0.05
}
```

Use `validate_geometry`. Use actual schema shapes from `tools/list` if validation rejects an expectation map.

Capture useful views:

```json
{"document":"Bracket","focus_object":"Body","view_name":"Isometric","width":1024,"height":768}
```

Also inspect `Front`, `Top`, and the planned bed-facing view when they reveal different risks.

Save the editable source:

```json
{"document":"Bracket","path":"/absolute/allowed/path/Bracket.FCStd"}
```

Export only the final object:

```json
{
  "document":"Bracket",
  "objects":["Body"],
  "format":"stl",
  "path":"/absolute/allowed/path/Bracket.stl",
  "linear_deflection":0.03,
  "angular_deflection":0.12,
  "bed_align":true
}
```

Use the `export` readback as file evidence. Use `format:"step"` for solid exchange, `"3mf"` for mesh exchange, or `"fcstd"` for a verified native copy.

## Run FEM

Read `fem.md` first. Build the analysis graph with `create_object` and `edit_object`. Then call:

```json
{"document":"Bracket","analysis":"Analysis","timeout_s":600}
```

Use `run_fem`. Poll `tasks/get` if the call detaches. Do not interpret a successful solve as a manufacturing safety proof.

## Use the Python escape hatch

Use `run_script` only when no structured tool expresses the operation:

```json
{
  "session_id":"bracket-build",
  "timeout_s":90,
  "code":"import Part\ndoc = App.getDocument('Bracket')\nobj = doc.getObject('Final') or doc.addObject('Part::Feature', 'Final')\nobj.Shape = Part.makeBox(40, 30, 5).cut(Part.makeCylinder(4, 5, App.Vector(20, 15, 0)))\ndoc.recompute()\nprint(obj.Name, obj.Shape.isValid(), len(obj.Shape.Solids), obj.Shape.Volume)"
}
```

Use internal document names. Reuse one session only when persistent variables help. Inspect and validate the resulting document through structured tools.
