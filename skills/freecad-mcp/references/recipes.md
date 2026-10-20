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
- [Store FFF process parameters](#store-fff-process-parameters)
- [Audit overhangs and horizontal ceilings](#audit-overhangs-and-horizontal-ceilings)
- [Chamfer bed-facing edges](#chamfer-bed-facing-edges)
- [Cut a teardrop horizontal hole](#cut-a-teardrop-horizontal-hole)
- [Check wall thickness against line width](#check-wall-thickness-against-line-width)
- [Check build volume fit](#check-build-volume-fit)

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

## Store FFF process parameters

Store the process values once, then bind the geometry to them. The values below are starting heuristics for an unknown printer.

Create the parameter host:

```json
{"document":"Bracket","type":"App::VarSet","name":"Process"}
```

Call `create_object`. Use the returned name in the expressions below.

Add the values and bind the wall thickness in one atomic call:

```json
{
  "document":"Bracket",
  "object":"Process",
  "add":[
    {"name":"NozzleDiameter","type":"App::PropertyLength","value":0.4},
    {"name":"LineWidth","type":"App::PropertyLength","value":0.45},
    {"name":"LayerHeight","type":"App::PropertyLength","value":0.2},
    {"name":"WallLines","type":"App::PropertyInteger","value":3},
    {"name":"ClearancePerSide","type":"App::PropertyLength","value":0.15},
    {"name":"WallThickness","type":"App::PropertyLength","value":1.35}
  ],
  "expressions":{"WallThickness":"Process.WallLines * Process.LineWidth"}
}
```

Use `edit_parameters`. It applies `add` before `expressions`, so one call binds a property that the same call adds. The expression names the object, not a bare property name.

The nozzle, line width, and layer height are planning assumptions, not machine limits. Use the [extrusion-scale rules](printability.md#scale-features-to-extrusion-lines-and-layers) for another profile. `ClearancePerSide` is a surface gap, not a diametral value or a process tolerance. The 0.15 mm locating fit is synthesized and needs a coupon. Change `WallLines` or `LineWidth` to drive `WallThickness`.

Read back the binding:

```json
{
  "document":"Bracket",
  "objects":["Process"],
  "detail":"full",
  "property_filter":["LineWidth","WallLines","WallThickness"]
}
```

Use `inspect_objects`. Read `properties.WallThickness` for the resolved value and `propertyMetadata.WallThickness.expression` for the stored formula. An empty expression means the binding did not persist.

Bind a feature parameter to `Process.WallThickness` with `edit_parameters`, or enter the same expression on a feature with the `{"expression":"..."}` scalar form.

## Audit overhangs and horizontal ceilings

Measure overhangs from the vertical. A vertical wall is 0°, a horizontal ceiling is 90°. Treat 45° or less as self-supporting on an unknown printer.

Convert a guide that measures from the horizontal: `θ_vertical = 90° − angle_from_horizontal`. Compare only values in one convention.

For a planar face, `normal[2]` is the negated sine of θ: 0 on a vertical wall, -1 on a horizontal ceiling. The limit for an allowed angle θ_max is `-sin(θ_max)`; for 45° it is −0.7071, so a 45° chamfer or teardrop roof sits exactly on it. Flag a planar face when `normal[2] < -0.708` (the limit minus a 0.001 numerical tolerance) and the face lies above the bed.

1. Enumerate every face with pagination:

```json
{"document":"Bracket","target":{"object":"Body"},"detail":"full","limit":100}
```

Use `inspect_topology`. A whole-object target enumerates faces. `detail:"full"` is required for `center`, `normal`, `axis`, and `radius`. Read `total`, then follow `nextCursor` until it is null. The cursor binds to the document generation, so a recompute refuses it with `stale_cursor`; restart from the first page.

2. Keep each `surfaceType:"Plane"` item with `normal[2] < -0.708`, unless both `bounds[2]` and `bounds[5]` equal the bed z within 0.01 mm. That set is the overhang list beyond the 45° baseline. Only a face that lies wholly on the bed is bed contact; a sloped face that starts at the bed and rises is still an overhang.

3. From the same pages, keep each `surfaceType:"Cylinder"` item whose `axis` z-component is near zero. Those faces are horizontal bores. The item `normal` is sampled at one parameter midpoint, so never classify a curved crown from it. Treat each bore with [Cut a teardrop horizontal hole](#cut-a-teardrop-horizontal-hole).

4. Screen the bridges:

Read `bounds` for each remaining horizontal ceiling. The span of a rectangular ceiling is the shorter of `xmax − xmin` and `ymax − ymin`. Compare it with the 10 mm bridge screen. UltiMaker describes about 10 mm as a span that basic filaments often bridge without support. It is a validation threshold, not a limit.

5. Record each overhang that stays, with its face `reference`, its span, and the decision: bridge, support, or redesign.

## Chamfer bed-facing edges

Chamfer the edges that touch the bed. A chamfer removes the elephant-foot lip from a fit-critical bottom edge and starts the wall at 45°.

```json
{
  "document":"Bracket",
  "body":"Body",
  "kind":"chamfer",
  "name":"BedRelief",
  "parameters":{
    "base":{"object":"Pad"},
    "subelements":[{"object":"Pad","query":[{"role":"face","selector":"<Z"},{"role":"edge"}]}],
    "size":0.4
  },
  "expected_solids":1
}
```

Use `create_feature`. `base` names the current Body Tip solid feature; read `bodyTip` from the previous result. The query selects the lowest face cluster and expands its edges. The payload sets no chamfer type, so the native equal-distance chamfer applies: on the 90° edge between the bed face and a vertical wall it makes a 45° face, and `size` is also the vertical relief.

Read `resolvedSelections` for the bound edges and `bodyReport.solid_count` for the solid count. A chamfer keeps one solid, so `expected_solids` 1 holds.

- 0.2–0.4 mm is a synthesized starting range for a 0.4 mm nozzle and 0.2 mm layers. It is not a slicer limit.
- One call accepts at most 32 edges; a query that expands to more refuses the whole call before any change. When the bed face has a long outline, run the same query through `inspect_topology` first, then chamfer the edges in batches of at most 32 signed references. Re-inspect the new Tip before each later batch.
- Raise `size` above the expected elephant-foot width when the bottom edge sets a fit. The chamfer is the relief that protects the fit.
- Chamfer a bed-facing edge. Do not fillet it: a bed-facing fillet grows outward at each layer and becomes an unsupported curve.

## Cut a teardrop horizontal hole

A horizontal hole has an unsupported crown. A teardrop roof replaces the crown with two 45° faces.

The profile is a circle of radius r plus a square. The upper sides of the square are the 45° tangents to the circle. The top vertex sits r√2 above the centre.

1. Create the bore sketch on a vertical origin plane:

```json
{"document":"Bracket","body":"Body","kind":"sketch","name":"TeardropCircle","parameters":{"plane":"xz"}}
```

Use `create_feature`. The `xz` plane is vertical: sketch x follows document X and sketch y follows document Z. Confirm the height once with step 8.

2. Add the circle:

```json
{"document":"Bracket","sketch":"TeardropCircle","addGeometry":[{"kind":"circle","center":[20,6],"radius":2.5}]}
```

Use `edit_sketch`. The example uses r = 2.5 mm at sketch (20, 6) in a part 12 mm tall, so the crown top at 6 + r√2 = 9.54 mm leaves a 2.46 mm roof. Replace both values, and keep the crown top below the part top.

3. Cut the bore:

```json
{"document":"Bracket","body":"Body","kind":"pocket","name":"TeardropBore","profile":"TeardropCircle","parameters":{"extent":"distance","length":60,"symmetric":true},"expected_solids":1}
```

Use `create_feature`. `symmetric` is valid only with the `distance` extent; `through_all` with `symmetric` is refused. The sketch sits on the origin plane at Y = 0, so set `length` to at least twice the part depth along Y: the example part spans Y 0–30 mm, so 60 mm cuts from −30 to +30 and covers it.

4. Create the crown sketch on the same plane:

```json
{"document":"Bracket","body":"Body","kind":"sketch","name":"TeardropCrown","parameters":{"plane":"xz"}}
```

Use `create_feature`. The crown needs its own sketch because the two profiles overlap.

5. Add the closed square:

```json
{
  "document":"Bracket",
  "sketch":"TeardropCrown",
  "addGeometry":[
    {"kind":"polyline","closed":true,"points":[[20,6],[21.7678,7.7678],[20,9.5355],[18.2322,7.7678]]}
  ]
}
```

Use `edit_sketch`. The points are offsets from the hole centre:

| Point | Offset from centre |
|---|---|
| bottom | (0, 0) |
| right | (r/√2, r/√2) = (1.7678, 1.7678) |
| top | (0, r√2) = (0, 3.5355) |
| left | (−r/√2, r/√2) = (−1.7678, 1.7678) |

The values are rounded to four decimals. The 45° faces absorb that rounding.

6. Cut the crown:

```json
{"document":"Bracket","body":"Body","kind":"pocket","name":"TeardropRoof","profile":"TeardropCrown","parameters":{"extent":"distance","length":60,"symmetric":true},"expected_solids":1}
```

Use `create_feature`. Two pockets are used, not one, because the circle and the square overlap and one sketch with two overlapping wires cannot form a pocket face.

7. Keep the outer bounds unchanged:

```json
{"document":"Bracket","objects":["Body"],"expected_solids":1,"expected_bounds":{"Body":[0,0,0,40,30,12]},"bounds_tolerance":0.01}
```

Use `validate_geometry` with the bounds you read before the cuts. A changed extent means the hole broke an outer face. Unchanged bounds do not prove that the roof survived: a local opening leaves the bounds unchanged, so compare the crown top with the part top as in step 2.

8. Confirm the bore:

```json
{"document":"Bracket","target":{"object":"Body","query":[{"role":"face","selector":"%CYLINDER","radius":{"min":2.49,"max":2.51}}]},"detail":"full"}
```

Use `inspect_topology`. Read `items[].axis` for the horizontal direction. The crown cut removes the upper arc, so the remaining cylinder faces do not span centre ± r. Take the lowest `bounds[2]` across the returned items as the bore bottom; the centre height is that value plus r. An empty set means the radius or the position is wrong. Compare the centre height with the design value once to confirm the `xz` plane mapping.

A horizontal hole below about 2 mm diameter is a calibration risk. UltiMaker gives about 2 mm as a practical floor. Add a drilled pilot when the diameter controls a fit.

## Check wall thickness against line width

Measure the wall in the solid. A parameter value is not thickness evidence.

Faces mode:

```json
{"document":"Bracket","a":{"object":"Body"},"mode":"faces"}
```

Use `measure`. Read `faces[].normal`, `faces[].center`, and `faces[].area`; if `truncated` is true, the face list is capped, so use section mode. Take two planar faces that bound one wall: their normals are opposite, and the wall material lies between them. The thickness is `|(center_b − center_a) · normal_a|`. Use section mode when the pairing is uncertain.

Section mode:

```json
{"document":"Bracket","a":{"object":"Body"},"mode":"section","plane":{"z":5}}
```

Use `measure`. Read `curves`. Each `kind:"line"` entry carries `start`, `end`, and `length`. One vertical wall appears as two opposed lines; the perpendicular gap between them is the thickness. The `z` value is a design value inside the wall.

Use faces mode for a planar wall. Its normal is one midpoint sample, so it is not reliable on a curved wall; use section mode there.

Compare the result with `Process.WallLines * Process.LineWidth`. The two-line floor is a design heuristic, not a slicer limit.

- Below two lines: review functional strength and slicer paths. Reserve one-line walls for deliberately fragile features, such as a cosmetic membrane.
- Prefer whole-line thicknesses when the function permits. Verify any other thickness in the intended slicer profile; do not change a fit-critical dimension only to reach a whole line count.
- Report the measured value, not the design value.

## Check build volume fit

A part that meets the nominal envelope can still miss the usable area. Brim, skirt, and purge structures use the edge margin.

```json
{"document":"Bracket","objects":["Body"],"expected_solids":1}
```

Use `validate_geometry`. Read `objects[].bounds` in `[xmin, ymin, zmin, xmax, ymax, zmax]` order, and compute each extent as `max − min`.

Example: a P1S has a 256 mm nominal envelope. With a 10 mm margin on each side, the usable extents are 236 mm in X and Y and 256 mm in Z. The margin is a stated assumption for brim, skirt, and purge. It is not a machine limit.

Check the placed part against the usable box `[10, 10, 0]` to `[246, 246, 256]`:

- `xmin ≥ 10` and `xmax ≤ 246`, and the same for Y.
- `zmax − zmin ≤ 256`.
- `zmin` equals the bed z when the part is modelled in print orientation.

When the part fails, rotate it for a smaller footprint or split it. Do not scale a functional part to fit. Report the envelope, the margin, and each extent.
