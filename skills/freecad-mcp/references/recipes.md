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
- [Cut and fuse without scripts](#cut-and-fuse-without-scripts)

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

Use `delete_object`. A PartDesign dependent whose only link is the target's `BaseFeature` is rerouted by the native removal and reported in `rerouted` with its resulting link (null after the cleared link), so a mid-chain solid can be deleted in one call. If the tool instead reports dependents, repair the graph rather than cascading deletion.

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

Edit a supported scalar feature after inspection. Omit `response_detail` for the compact default; pass `"full"` when the `parameterValues` rows must also carry their `before` state:

```json
{
  "document":"Bracket",
  "body":"Body",
  "object":"Pad",
  "parameters":{"extent":"distance","length":12},
  "expected_generation":4,
  "expected_solids":1
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

Enumerate an object's faces with a whole-object target:

```json
{"document":"Assembly","target":{"object":"Housing"},"detail":"full"}
```

Select declaratively with shared query descriptors, and reuse the exact payload in every consumer that accepts its shape. Two cover the common cases: the face query `{"object":"Pad","query":[{"role":"face","selector":">Z"}]}` resolves to exactly the one face farthest along +Z, and the edge query `{"object":"Pad","query":[{"role":"edge","selector":"|Z"}]}` matches the four vertical edges of a box pad.

```json
{"document":"Bracket","target":{"object":"Pad","query":[{"role":"face","selector":">Z"}]}}
```

Use `inspect_topology` to resolve either descriptor and read its signed references; a two-step chain such as `faces >Z` then `edges %CIRCLE` narrows to the top perimeter. Reinspect after an upstream geometry change because signed references bind to a generation, and a pagination cursor refuses `stale_cursor` after a target or generation change.

The face payload is reused verbatim as a `measure` target — it resolves to one shape, so the singleton consumer accepts it:

```json
{"document":"Bracket","a":{"object":"Pad","query":[{"role":"face","selector":">Z"}]},"b":{"object":"Lid"},"mode":"distance"}
```

The edge payload is the same four-candidate set everywhere. `measure` needs exactly one shape, so it refuses with `selection_ambiguous`, `matchCount: 4`, and the bounded candidate list:

```json
{"document":"Bracket","a":{"object":"Pad","query":[{"role":"edge","selector":"|Z"}]},"b":{"object":"Lid"},"mode":"distance"}
```

Use `measure`. Require the intended minimum distance and zero unintended common volume.

A fillet subelement list is a set consumer: the same edge payload expands to its four matches within the 32-reference cap:

```json
{
  "document":"Bracket",
  "body":"Body",
  "kind":"fillet",
  "name":"EdgeRound",
  "parameters":{
    "base":{"object":"Pad"},
    "subelements":[{"object":"Pad","query":[{"role":"edge","selector":"|Z"}]}],
    "radius":2
  }
}
```

`capture_view` focus consumes exactly one result, so the same face payload drives the `detail` orientation:

```json
{"document":"Bracket","mode":"detail","focus":{"object":"Pad","query":[{"role":"face","selector":">Z"}]}}
```

Every operation that resolved a query reports `resolvedSelections`: the parameter path, the selection-time generation, and the signed references that were bound.

Check two mating whole objects with both modes:

```json
{"document":"Assembly","a":{"object":"Insert"},"b":{"object":"Housing"},"mode":"distance"}
```

```json
{"document":"Assembly","a":{"object":"Insert"},"b":{"object":"Housing"},"mode":"interference"}
```

To quantify material a change added or removed, use `difference` (`a.cut(b)`):

```json
{"document":"Bracket","a":{"object":"BracketFinal"},"b":{"object":"BracketBlank"},"mode":"difference"}
```

`difference_volume` is the material of `a` that `b` does not cover; a fully consumed `a` reports 0 with null bounds and no solids.

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

Capture one review sheet:

```json
{"tool": "capture_view", "arguments": {"document": "Bracket", "focus": {"object": "Body"}}}
```

The default `overview` mode returns one labeled sheet covering `Isometric`, `Front`, `Top`, `Bottom`, and the side orientations, so the bed-facing view needs no second call. Use `interior` for internal features and `fit` for a mating interface when the question needs them.

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

Read `fem.md` first. Build the analysis graph with `create_object` and `edit_object`. Bind a constraint with the same edge query the fillet used — `References` are set consumers, and query entries expand in order:

```json
{
  "document":"Bracket",
  "object":"FixBase",
  "properties":{"References":[{"object":"Pad","query":[{"role":"edge","selector":"|Z"}]}]}
}
```

Use `edit_object`; the receipt reports the signed references chosen at selection time. Keep the `analysis` name explicit when the constraint joins the analysis group. Then call:

```json
{"document":"Bracket","analysis":"Analysis","timeout_s":600}
```

Use `run_fem`. Poll `tasks/get` if the call detaches. Do not interpret a successful solve as a manufacturing safety proof.

## Cut and fuse without scripts

Part booleans are document objects. Wire them with `create_object` through canonical links, gate the geometry, and keep scripts for the operations no structured tool covers.

Create the operands (any supported Part route):

```json
{"document":"Bracket","type":"Part::Box","name":"Blank","properties":{"Length":40,"Width":30,"Height":10},"expected_solids":1}
```

```json
{"document":"Bracket","type":"Part::Cylinder","name":"Bore","properties":{"Radius":4,"Height":10,"Placement":{"position":[20,15,0],"axis":[0,0,1],"angle_deg":0}},"expected_solids":1}
```

Subtract through `Part::Cut`. `Base` and `Tool` are `PropertyLink` positions and take whole objects only — query targets refuse `subshape_not_allowed`:

```json
{
  "document":"Bracket",
  "type":"Part::Cut",
  "name":"BracketFinal",
  "properties":{"Base":{"object":"Blank"},"Tool":{"object":"Bore"}},
  "expected_solids":1,
  "expected_bounds":[0,0,0,40,30,10]
}
```

Fuse through `Part::MultiFuse` with `Shapes` (a `PropertyLinkList`) — here two overlapping operands intended to form one connected solid:

```json
{
  "document":"Bracket",
  "type":"Part::MultiFuse",
  "name":"Joined",
  "properties":{"Shapes":[{"object":"Left"},{"object":"Right"}]},
  "expected_solids":1
}
```

Gate with `expected_solids`/`expected_bounds`, then confirm with `validate_geometry`. Set `expected_solids` from the design: disjoint operands legitimately produce multiple solids, and a mismatching expectation refuses and rolls back the creation. This is verified server wiring, not live boolean validation: the `supportedTypes` inventory proves availability, not the correctness of a boolean result. Scripts remain the escape hatch for named Part shape operations the structured tools do not express (`common`, `section`, shape fillets, sweeps); read [the run_script contract](python-export.md), use internal document names, and inspect and validate the resulting document through structured tools afterward.
