# FreeCAD MCP tool contract

Use this file for exact tool behavior. Read [Protocol and security](protocol-security.md) for connection, authentication, paths, consent, tasks, limits, and GUI dispatch.

## Contents

- [Tool matrix](#tool-matrix)
- [Shared topology targets](#shared-topology-targets)
- [Standard sequence](#standard-sequence)
- [Property mapping](#property-mapping)
- [Inspection response](#inspection-response)
- [`inspect_sketch` and `edit_sketch`](#inspect_sketch-and-edit_sketch)
- [`edit_parameters` results](#edit_parameters-results)
- [`create_feature` details](#create_feature-details)
- [`edit_feature` parameters](#edit_feature-parameters)
- [`create_object` details](#create_object-details)
- [Recovery checkpoints](#recovery-checkpoints)
- [Errors](#errors)
- [Sources](#sources)

## Tool matrix

The `tools/list` response is the source of truth for the enabled tool
surface. `run_script` appears there only when the local `allow_scripts`
setting is enabled. Document tools return the actual sanitized
`name`, `label`, and `objectCount`; use the returned `name` as the
`document` argument in later calls.

| Tool | Use | Important arguments |
|---|---|---|
| `discover_capabilities` | Versions, workbenches, supported types, exporter and FEM availability, GUI dispatch health | optional `refresh` (default `false`; `true` re-captures through the GUI path) and `detail` (`compact` default or `full`); GUI-independent without `refresh`. Use `tools/list` for exact tool schemas. |
| `inspect_documents` | Open-document inventory with generation, dirty/active flags, and transaction state | none |
| `new_document` | Create an empty document and return its generation | `name` |
| `open_document` | Open an `.FCStd`; returns `alreadyOpen: true` when that file is already live, plus its generation | `path`; `untrusted` defaults true and requires consent |
| `import_model` | Import STEP or STL behind file consent | `document`, `path`, `format`; optional `name` (STL mesh feature) |
| `save_document` | Save to the existing path, or save-as; reports the saved path and generation | `document`; optional `path` (consent to overwrite a different existing file) |
| `close_document` | Close one document and report its prior path, discarded state, and final known generation | `document`; consent when dirty or unsaved nonempty |
| `reload_document` | Close and reopen the saved file; reports the reopened document and generation | `document`; consent to discard unsaved changes |
| `inspect_objects` | List objects sorted by Name, or an explicit nonempty object selection; signed-cursor pagination | `document`; optional `objects`, `cursor`, `detail` (`compact`/`full`), `property_filter`, `limit` (default 32), `property_offset`, `property_limit` |
| `create_object` | Create a supported Part/App type or a FEM object | `document`, `type`, `name`; optional `properties`, `expected_solids`, `expected_bounds`, `bounds_tolerance`, `response_detail` |
| `create_objects` | Create 1–32 independent objects atomically; returns the requested-to-actual `nameMapping` | `document`, `entries`; optional `expectations` keyed by requested name, `response_detail` |
| `edit_object` | Assign properties with full prevalidation; `Spreadsheet::Sheet` cell contents use `properties.cells`; link values accept the shared targets; reports post-state evidence (`response_detail: "full"` adds before/after deltas; the default is `compact`) | `document`, `object`, `properties`; optional `expected_generation`, `expected_solids`, `expected_bounds`, `bounds_tolerance`, `response_detail` |
| `edit_objects` | Edit 1–32 objects atomically | `document`, `edits`; optional top-level `expected_generation`, `expectations` per object, `response_detail` |
| `delete_object` | Delete one object; refuses objects with dependents, except PartDesign dependents whose only link is the target's `BaseFeature` (rerouted by the native removal and reported in `rerouted`) | `document`, `object`; optional `expected_generation` |
| `validate_geometry` | State, validity, solid count, volume, bounds, tolerance, plus optional declarative checks (`volume_range`, `clearance_min`, `interference_max`) reporting `checks`, `checksPassed`, and `accepted` | `document`; `objects` (max 100, required without `checks`, optional with them, never defaulting to all document objects); optional `checks` (1–16), `expected_solids`, `expected_bounds`, `bounds_tolerance` |
| `measure` | Distance, interference, difference (`a.cut(b)` volume: the added or removed material), section, or face measurement. Positive distance does not prove separation; zero common volume does not prove clearance. Combine modes or use `validate_geometry` checks for fit decisions (see [validation](validation.md)) | `document`, `a` (shared target), `mode`; optional `b` (shared target), `plane`; a query target must resolve to exactly one shape (`selection_empty`/`selection_ambiguous` otherwise); the result carries `document` and `generation` |
| `inspect_topology` | Inspect topology through one shared target: a whole object enumerates its faces, a query reports its final-stage set, a signed reference returns its one subshape; items carry signed references. The pagination cursor is bound to document identity, generation, object, role, page size, and the canonical target hash — any change refuses `stale_cursor` | `document`, `target`; optional `cursor`, `limit` (default 50, max 100), `detail` (`compact` default/`full`) |
| `edit_parameters` | Add/rename dynamic properties, bind expressions, clear expressions; reports `document`, `generation`, `applied`, a post-state `bodyReport`, and explicit `units`; `response_detail: "full"` also returns the pre-edit report | `document`, `object`; optional `add`, `rename`, `expressions`, `clear_expressions`, `expected_generation`, `expected_solids`, `expected_bounds`, `bounds_tolerance`, `response_detail` |
| `inspect_sketch` | Sketch geometry/constraint rows, solver summary, `state`, `statusText`, and `solver.solverStatus` | `document`, `sketch` |
| `edit_sketch` | Atomic sketch batch: geometry (including the `rectangle`, `polyline`, `regularPolygon`, `slot`, and `rounded_rectangle` composites), constraints, datums, constraint expressions, deletes | `document`, `sketch`; optional `addGeometry`, `addConstraints`, `setDatums`, `setExpressions`, `deleteGeometry`, `deleteConstraints`, `expected_generation` |
| `create_feature` | One of 24 PartDesign feature kinds inside a Body: `datum_plane`, `datum_line`, `sketch`, `pad`, `pocket`, `hole`, `revolve`, `groove`, `fillet`, `chamfer`, `thickness`, `draft`, `linear_pattern`, `polar_pattern`, `mirrored`, `loft`, `pipe`, `gear_profile`, `helix`, `primitive`, `subshape_binder`, `multi_transform`, `scaled`, `datum_point` | `document`, `body`, `kind`, `name`; optional `parameters` (typed semantic — required in practice for every kind except the five raw-properties kinds; see its section), `properties` (raw; only the five kinds `sketch`, `pad`, `pocket`, `hole`, `datum_plane`; never combinable with `parameters`), `profile` (required for the eight profile kinds), `support`, `expected_solids`, `expected_bounds`, `bounds_tolerance`, `response_detail` |
| `edit_feature` | Edit nine kinds — `pad`, `pocket`, `hole`, `gear_profile`, `fillet` (`radius`), `chamfer` (`size`), `linear_pattern` (`count`, `length`), `polar_pattern` (`count`, `angle`), `revolve` (`angle`, `reversed`); unsupported types return `nextTool: edit_object`; reports uniform `parameterValues` rows | `document`, `body`, `object`, `parameters`; optional `expected_generation`, `expected_solids`, `expected_bounds`, `bounds_tolerance`, `response_detail` |
| `export` | STL/STEP/3MF or native FCStd copy with readback verification | `document`, `objects`, `format`, `path`; FCStd permits an empty `objects` list; mesh options apply only to STL/3MF |
| `capture_view` | One labeled PNG per inspection intent: `overview` (default) 4x2 sheet of the seven named orientations plus a legend, `detail` one enlarged view, `interior` mid-plane sections plus x-ray, `fit` two objects uncut plus a mating-axis section | `document`; optional `mode` (defaults to `overview` regardless of `view_name`), `focus` (shared target), `view_name` (valid with `detail` only), `a`, `b` (fit shared targets), `section_axis`, `section_point`, `width`, `height` |
| `run_fem` | Modern CalculiX solve; returns a VTK result summary | `document`, `analysis`; optional `timeout_s` (default 600) |
| `run_script` | Arbitrary Python on the GUI thread in a persistent session namespace | `code`; optional `session_id` (default `"default"`), `timeout_s` (default 90) |

`run_script` is opt-in: it is registered but hidden unless
`allow_scripts: true` is saved in `freecad_mcp_settings.json` (or the
settings dialog checkbox is enabled). Saving the setting applies it to a
running server immediately: the tool appears in the next `tools/list`,
and streams that requested the `toolsListChanged` filter receive
`notifications/tools/list_changed`. A
disabled `run_script` answers `tools/call` with METHOD_NOT_FOUND and never
enters schema validation, consent, or the GUI dispatch. Discovery carries
`capabilities.scriptingEnabled` and `capabilities.recoveryEnabled` so
clients can read the active policy without calling anything else.

`discover_capabilities` with `detail: "compact"` (the default) returns `freecad`, `occ`, `exporters`, `fem`, `geometryQueries`, `supportedTypesCount`, `supportedTypesDocument`, `scriptingEnabled`, and `recoveryEnabled`; `detail: "full"` returns the complete snapshot. `refresh: true` re-captures the snapshot through the GUI path.

`inspect_documents` takes no arguments. It returns `documents[]` rows with `name`, `label`, `fileName`, `objectCount`, `generation`, `dirty`, `active`, `transactionOpen`, and `editObject` (FreeCAD's active object for that document: the last object it activated, not an edit session), plus `activeDocument`. Use it when the document name is unknown; it replaces document discovery through `run_script`.

The registered tools cover CAD-side modeling, inspection, validation, export, and FEM operations. The server methods also expose document resources and task/resource subscriptions. Report anything outside these operations as outside this skill's boundary.

## Shared topology targets

Every geometric target boundary accepts one closed union of three forms. The forms have distinct meanings: identity, fresh subshape identity, and a selection recipe.

| Form | Payload | Meaning |
|---|---|---|
| Whole object | `{"object": "Pad"}` | Identity. No `subelement` key; the old empty-string subelement sentinel is refused (`empty_subelement`). |
| Signed reference | `{"object": "Pad", "subelement": "<opaque signed token>"}` | One fresh subshape identity. Take tokens from `inspect_topology` items or `resolvedSelections` receipts; raw numeric `Face7`/`EdgeN` labels are never accepted as durable input. |
| Query | `{"object": "Pad", "query": [{"role": "face", "selector": ">Z"}, {"role": "edge", "selector": "%CIRCLE"}], "expected_generation": 4}` | A declarative selection evaluated at call time. The same descriptor works in `measure` targets, `create_feature` subelement lists, link properties (including FEM `References`), and `capture_view` focus. |

A query is 1–3 steps; each step carries `role` (`face` or `edge`), an optional `selector` (omitted selects every candidate of that role), an optional `radius: {"min", "max"}` range in mm, and an optional analytic `axis: {"direction": [x, y, z], "tolerance_deg": 0.1}` that matches cylinder/cone/torus and circular-curve axes sign-insensitively — it is separate from the CadQuery normal/tangent operators. Later steps narrow: `faces >Z` then `edges %CIRCLE` selects the top perimeter edges. `face` then `edge` expands the selected faces' edges; the reverse order is refused (no implicit ancestor queries).

Selector grammar (bounded CadQuery string syntax):

- `%TYPE` (case-insensitive): faces PLANE, CYLINDER, CONE, SPHERE, TORUS, BEZIER, BSPLINE, REVOLUTION, EXTRUSION, OFFSET; edges LINE, CIRCLE, ELLIPSE, HYPERBOLA, PARABOLA, BEZIER, BSPLINE, OFFSET. `%OTHER` is refused on records the server cannot classify.
- Directions `X Y Z XY XZ YZ` or `(x,y,z)` (integer part required, no exponent, zero vectors refused; magnitude is preserved for projections).
- `+D`/`-D` (directed angle within 0.0001 rad), `|D` (parallel, either orientation), `#D` (perpendicular), bare `D` = `+D`. Eligibility is planar faces (normals) and linear edges (parameter-increasing tangents) only; a cylinder's axis is not its face normal, so `%CIRCLE` includes curved edges but `|Z` does not.
- Extrema `>D`, `<D`, `>>D`, `<<D` with optional `[n]` (signed integer, |n| <= 4095) rank centers of mass projected on the direction — never bounding boxes — and return the whole chosen cluster. `>Z` on a box selects the top face by center, not the side faces sharing its ZMax. With `[n]`, single-angle operators first restrict to `|D`-compatible records while doubled operators rank every candidate; an index past the nonempty clusters refuses `selector_index_out_of_range` with `index` and `groupCount`.
- Named views keep CadQuery meanings, not FreeCAD camera names: front=>Z, back=<Z, left=<X, right=>X, top=>Y, bottom=<Y.
- Set algebra `and`, `or`, `exc`/`except`, and prefix `not`: every operand sees the same candidate universe, and `not` binds loosest, so `not >X and >Y` means `not (>X and >Y)`. Prefer explicit parentheses; chain steps instead of `and` when an extremum must run on a narrowed set.

Refusals and budgets: malformed syntax answers `VALIDATION_FAILED` with `reason: selector_syntax`, a zero-based `position`, bounded `expected`, and `nextTool: inspect_topology`; parser budgets (128 tokens, 64 AST nodes, nesting 8) refuse with `selector_limit`; geometry evidence that cannot be read refuses `selector_geometry_unavailable` instead of returning a false zero-match; extraction and chain expansion cap at 4096 candidates (`query_too_large`). Empty results are data for set consumers such as `inspect_topology`; a consumer that needs exactly one shape refuses `selection_empty`, and one that cannot pick among several refuses `selection_ambiguous` — the evidence carries `parameter`, `object`, `role`, `matchCount`, up to 16 signed candidates, and `candidatesTruncated`.

Operations that consumed a query-origin link or reference report `resolvedSelections` receipts — `parameter` (the input path), `document`, the selection-time `generation`, signed `references`, and `count` — capped at 64 receipts and 64 referenced subshapes per operation (`selection_limit` beyond, refused before any mutation). `discover_capabilities` publishes the grammar as `geometryQueries`: `{"syntax": "cadquery-string-v1", "roles": ["face", "edge"], "maxSteps": 3, "maxSelectorLength": 256, "maxCandidates": 4096}`.

`capture_view` selects its composition from `mode`, so a caller never names a camera orientation to answer an inspection question. `mode` defaults to `overview` regardless of `view_name`; `view_name` is valid only with `mode: "detail"` — any other mode carrying it refuses `invalid_parameter_for_mode` instead of switching modes. `overview` captures the seven named orientations plus a legend on one labeled 2048x1104 sheet; without `focus` it frames the whole visible document and reports `captured_objects` (up to 16 visible roots) with `truncated`, with `focus` it frames that shared target. `detail` requires `focus` and captures one panel at the viewport-derived size; without `view_name` the orientation derives from a planar face focus (camera direction opposite the face normal, screen up the least-parallel document axis), and a target it cannot derive answers `orientation_not_derivable` with `suggestions: ["view_name"]`; with `view_name` it is the explicit single-view capture. `interior` requires a whole-object `focus` (a subshape focus answers `subshape_not_allowed` rather than silently sectioning the owner) and captures `SectionX`/`SectionY`/`SectionZ` mid-plane cuts through the focus bounds plus `Xray-Isometric` (focus `Transparency` 75 for that panel only) on one 1024x1104 sheet; it refuses while a clipping plane is already active (`clipping_plane_active`) or when the target has no shape (`interior_target_shapeless`). `fit` refuses `focus` and requires `a` and `b` shared targets, each resolving to exactly one shape; it captures both objects framed uncut plus `MatingSection` — a plane containing the mating axis through the interface midpoint — on one 1024x552 sheet. The mating axis derives from exactly one coaxial cylindrical face pair (a signed or query target narrows the candidates to that face; a named non-cylinder face answers `no_mating_axis_found`), else the capture answers `no_mating_axis_found` (with `nextTool: inspect_topology`) or `ambiguous_mating_axis`; `section_axis`/`section_point` together override the derivation, and providing them apart or with invalid values answers `section_override_invalid`. An omitted single-panel size resolves from the active on-screen viewport, scaled down to a 768 px longest edge and never upscaled; an explicit size up to 4096 px is honored, and explicit `width`/`height` on a sheet scale its panels with the label strips constant. The result reports `focus` (and `a`/`b` for `fit`) as resolved whole or signed targets under those same keys — no duplicate resolved-target fields accompany them. The `views` manifest reports each panel's camera `direction`/`up`, `section_plane` (`normal`, `point`), `rect`, and whether the orientation was `derived` rather than named. Section and x-ray panels are qualitative: occlusion and cut-surface appearance are not guaranteed; `measure` and `inspect_topology` remain the authoritative evidence. The caller's selection, active document, camera, clipping plane, focus transparency, and navigation-animation preference are restored; a restore failure answers `restoration_failed` and returns no image.

## Standard sequence

1. Call `discover_capabilities`. Read `gui.state`, exporter/FEM availability, and the supported-type inventory; add `detail: "full"` when the complete `supportedTypes` list is needed. Call `tools/list` when you need exact tool schemas.
2. Address the target document by the `name` returned by `new_document` or `open_document`. When the name is unknown, call `inspect_documents` and read its rows before choosing the target.
3. Call `inspect_objects(document)` and read the compact rows before editing. Use `detail: "full"` for a `Spreadsheet::Sheet` when cell contents, formulas, aliases, or evaluated values matter.
4. Create or edit one dependency stage at a time; use `create_objects` only for independent entries, then inspect after each recompute.
5. Run `validate_geometry` and `measure` on the final solid.
6. Call `export`, then `capture_view` with the `mode` that matches the question: `overview` after opens and large modifications, `interior` for internal features, `fit` for mating, `detail` for one enlarged feature.

## Property mapping

Read [Property types](fundamentals.md#property-types) for exact value forms, names, units, links, placements, and bounds.

For `Spreadsheet::Sheet`, use `properties.cells` with address or alias keys. Bare cell property keys are refused. Require `cellContentsPersisted: true` after recompute readback.

## Inspection response

Compact rows carry identity, state, bounds, validity, solid count, Tip, and links. Use an explicit `objects` selection for targeted reads.

Use `detail: "full"` only for selected objects. Limit properties with `property_filter`, `property_offset`, and `property_limit`. Continue object pages with `cursor`.

A full spreadsheet row adds bounded cells, aliases, raw contents, formulas, evaluated values, and errors.

Full-detail rows serialize link values as shared targets: same-document whole links as `{"object"}`, resolvable subshape links as `{"object", "subelement": "<signed token>"}`, and cross-document, unsupported, stale, or otherwise unresolvable links as `{"unavailableLink": {"object", "reason"}}` — a diagnostic `nativeSubelement` label may accompany it but is never accepted as target input. Raw `FaceN` labels are never published as references.

Use `typeId` and internal `name` for automation. Use `label` only for human presentation.

## `inspect_sketch` and `edit_sketch`

Both tools report `state` (a list of state strings), `statusText` (a string or `null`), and `solver.solverStatus` (an integer or `null`, the native `solve()` code) alongside the geometry and constraint rows.

`edit_sketch` accepts optional `expected_generation`. A mismatch fails with `VALIDATION_FAILED` and no transaction opens, so nothing changes; the details carry `reason: stale_generation`, `expectedGeneration`, `actualGeneration`, and `nextTool: inspect_sketch`.

A constraint `(type, argument-count)` shape with no recorded native acceptance is refused before execution with `VALIDATION_FAILED` and no transaction; the details carry `reason: unrecorded_constraint_shape`, the `acceptedArgumentCounts` for the requested type (`null` when the type has no recorded form), and `nextTool: inspect_sketch`. The refusal is a process-safety measure: a malformed `Sketcher.Constraint` constructor call can raise an unhandled C++ exception that terminates the whole FreeCAD process.

Give a new geometry row an `id` when constraints in the same batch must reference it. Use `{"geometry":"<id>"}` in geometry argument slots. Read the committed index from `addedGeometryIds`.

Composite `addGeometry` kinds (`rectangle`, `polyline`, `regularPolygon`, `slot`, `rounded_rectangle`) take no `id`: read their expanded indices from `addedGeometry`. The dimension semantics and operation counts are in [sketcher.md](sketcher.md#geometry-entries).

## `edit_parameters` results

`edit_parameters` results carry `document`, `generation`, `applied`, a post-state `bodyReport`, and `units` (`length: mm`, `volume: mm3`, `tolerance: mm`). Compact results omit the pre-edit report; `response_detail: "full"` adds `beforeReport`. Each `applied` entry is an operation label: `add:NAME`, `rename:OLD->NEW`, `expression:PROP`, or `clear:PROP`. Expression and clear targets may use native dotted paths such as `Placement.Base.y` when the root property exists. The list ends with the mutated object's internal `Name`.

## `create_feature` details

`create_feature` creates the feature through `body.newObject`, so Body membership and the Body Tip are native. The `pad`, `pocket`, `hole`, `revolve`, `groove`, `loft`, `pipe`, and `helix` kinds require a `profile` object that already belongs to the same Body (`revolve`/`groove` add an `axis`, `loft` adds `sections`, `pipe` adds `spine`). A `support` reference requires an explicit `properties.MapMode`, which makes it a raw-properties-mode attachment: `properties` and `parameters` are mutually exclusive, so a sketch or datum plane attaches either through a semantic `plane` or through `support` plus `properties.MapMode`, never both. The tool never invents an attachment mode.

Reference positions follow the shared target vocabulary. `profile`, `sections`, `originals`, `spine`, dress-up `base`, and the Body stay whole-object names; `support`, mirror-plane positions (`mirrored` plane and draft `neutral_plane`), and face/edge support positions such as `pad`/`pocket` `face` accept whole objects, signed references, or queries that resolve to exactly one compatible result. Axis and datum-line positions (`revolve`/`groove`/pattern/helix `axis`, draft `pull_direction`) stay whole objects: a Body origin axis or datum line, or the closed `{object, sketchAxis}` literal for the kinds that accept it (`revolve`/`groove` refuse the sketch-axis form) — queries and subshapes refuse `subshape_not_allowed` there. Query-origin selections are resolved once before the transaction; after the internal base normalization a dress-up query is re-verified against the pre-normalization subshapes by root index and per-index document-space geometry fingerprint, and any drift refuses `selection_changed` inside the mutation instead of binding newly selected geometry.

`create_feature` accepts either raw `properties` or typed semantic `parameters`, never both. Raw `properties` stay available only for the five original kinds (`sketch`, `pad`, `pocket`, `hole`, `datum_plane`); every other kind takes typed `parameters` only, and `gear_profile` accepts typed parameters only. Typed parameters map onto the native properties per kind and are closed schemas, so an incomplete request fails wire validation before a transaction opens:

- `sketch`, `datum_plane`: `plane` (`xy`/`xz`/`yz`) plus optional `offset`.
- `datum_line`: `axis` (`x`/`y`/`z`).
- `pad`: `extent` (`distance`/`up_to_face`), `length`, optional `face`, `symmetric`, `reversed`.
- `pocket`: `extent` (`distance`/`through_all`/`up_to_face`), `length`, optional `face`, `symmetric`, `reversed`.
- `hole`: `diameter`, `depth` (dimension extents), optional `depth_type` (`dimension`/`through_all`), `cut` (`none`/`counterbore`/`countersink`/`counterdrill`) with its required sub-parameters (`counterbore_diameter` + `counterbore_depth`, `countersink_diameter` + `countersink_angle`, or `countersink_diameter` + `counterbore_depth` + `countersink_angle` for counterdrill), and `thread` plus `thread_size` (verified against the live ThreadSize enumeration).
- `gear_profile`: `teeth` (8–80), `module` (0.1–10 mm; pitch diameter capped at 200 mm), optional `pressure_angle` (14.5–25 degrees).
- `revolve`, `groove`: `axis` (a whole-object reference naming a Body origin axis or datum line; the `{object, sketchAxis}` form is refused for these kinds), `angle` (required, in (0, 360] degrees — 0 is refused), optional `reversed`.
- `fillet`: `base` object, `subelements` (1–32 edge references — signed references or query entries expanded within the cap), `radius`.
- `chamfer`: `base`, `subelements` (as fillet), `size`.
- `thickness`: `base`, `subelements` (1–32 signed face references or query entries), `thickness`, optional `inward`.
- `draft`: `base`, `subelements` (as fillet), `neutral_plane` (one compatible plane target), `pull_direction` (a whole-object datum line), `angle`, optional `reversed`.
- `linear_pattern`: `originals` (1–8 names), `axis`, `count` (2–32), `length`.
- `polar_pattern`: `originals` (1–8), `axis`, `count` (2–32), optional `angle`.
- `mirrored`: `originals` (1–8), `plane` (a shared target resolving to one compatible plane or planar face, or the closed `{object, sketchAxis}` form with `H_Axis`/`V_Axis`).
- `loft`: `sections` (1–7 names), `mode` (`additive`/`subtractive`), optional `ruled`.
- `pipe`: `spine` object, `mode` (`additive`/`subtractive`).
- `helix`: `profile` (required, a whole-object Body member), `axis`, `helix_mode` (`pitch_height`/`pitch_turns`/`height_turns`/`height_growth`), `mode` (`additive`/`subtractive`), plus the mode's driver pair (`pitch` + `height`, `pitch` + `turns`, `height` + `turns`, or `height` + `growth`) and optional `angle` (-80–80 degrees), `left_handed`, `reversed`.
- `primitive`: `shape` (`box`/`cylinder`/`cone`/`sphere`/`prism`/`torus`/`ellipsoid`/`wedge`), `mode` (`additive`/`subtractive`), plus the shape's required set — box `length`/`width`/`height`; cylinder `radius`/`height`; cone `radius1` (0 allowed)/`radius2`/`height`; sphere `radius`; prism `polygon` (3–100)/`circumradius`/`height`; torus `radius1`/`radius2`; ellipsoid `radius1`/`radius2`/`radius3`; wedge `x2_min`/`x2_max`/`z2_min`/`z2_max`.
- `subshape_binder`: `references` (1–16 same-document targets — whole objects, signed references, or query entries; queries expand within the 16-pair cap; they may name objects outside the Body), optional `make_face`.
- `multi_transform`: `originals` (1–8 names) plus `transformations` (1–4 steps; each `mirrored` with `plane`, `linear` with `axis`/`length`/`count`, or `polar` with `axis`/`count` and optional `angle`). The composite is atomic: one rollback covers the parent and every child.
- `scaled`: `originals` (1–8 names), `factor`, `count` (2–32).
- `datum_point`: `plane` (`xy`/`xz`/`yz`) plus optional `offset`.

Scalar parameter values take a plain number (lengths mm, angles degrees) or an `{"expression": "..."}` object that binds a native FreeCAD expression (transformation scalars inside `multi_transform` accept numbers only). The kinds that produce a solid — `pad` through `pipe`, plus `helix`, `primitive`, `multi_transform`, and `scaled` — become the Body Tip; datums, sketches, the gear wire profile, `datum_point`, and `subshape_binder` deliberately do not.

The create result adds `resolvedSelections` when a query parameter participated: one receipt per parameter with the input path, the selection-time generation, and the signed references that were bound.

## `edit_feature` parameters

`edit_feature` edits nine kinds — `pad`, `pocket`, `hole`, `gear_profile`, `fillet` (`radius`), `chamfer` (`size`), `linear_pattern` (`count` 2–32, `length`), `polar_pattern` (`count` 2–32, `angle`), and `revolve` (`angle`, `reversed`). Parameters are validated against the selected kind's semantic names before any side effect; a wrong-kind parameter names the `kind`, the `parameter`, and `supportedParameters` with `nextTool: inspect_objects`. The hole keeps its `thread_size` special case.

Fillet radius, chamfer size, and pattern lengths must be positive; revolve and polar-pattern angles must land in (0, 360] degrees after any expression resolves. Omitted parameters keep their native values and expressions — edits stay patch-like and never inject creation defaults. Every requested parameter is reported in `parameterValues` rows (`parameter`, `property`, `after: {value, expression}`); `before` rows appear only for `response_detail: "full"`. Expression bindings are read back after recompute, so a binding that resolves out of range refuses inside the mutation, rolls back, and restores the old expression and value; a valid expression reports the persisted expression and the resolved value. The output always carries `document`, `generation`, `object`, `body`, `bodyTip`, `bodyReport`, and `parameterValues`; `geometryChange` appears in full detail. There are no `change` or `applied` fields — that shape stays `create_feature`'s own creation report.

Shapeless and null-shape objects are valid on FreeCAD 1.1.3: `create_object` creates `PartDesign::Body` and `Part::Feature` successfully, and the report shows `solid_count: 0` until the object holds a solid. A positive `expected_solids` on such an object fails with `has no geometry; expected_solids=N cannot be satisfied`. `create_feature` acts on an empty Body without a bootstrap script.

`support` is applied through the `Support` property when the target type exposes it and through `AttachmentSupport` otherwise. `MapMode` is required either way. A target that exposes neither property fails with `feature '...' exposes neither Support nor AttachmentSupport`.

An enumeration property such as `PartDesign::Pocket.Type` takes the exact string (`"Length"`), never an index.

With `recovery_enabled` in settings, an expensive feature operation first writes one verified recovery copy of the document into the configured `recovery_directory` (see Recovery checkpoints). An operation is expensive when `create_feature` creates one of `fillet`, `chamfer`, `thickness`, `draft`, `linear_pattern`, `polar_pattern`, `mirrored`, `loft`, `pipe`, `helix`, `multi_transform`, or `scaled`, or when `edit_feature` edits a Body whose feature chain contains one of those types. The feature result then carries a `checkpoint` object with `path`, `document`, and `generation`.

## `create_object` details

Generic Part/App types go through `doc.addObject(type, name)`. FEM types use an explicit factory mapping through `ObjectsFem`: `Fem::FemAnalysis` (and the legacy alias `Fem::AnalysisPython`) to `makeAnalysis`, `Fem::SolverCalculiX` to `makeSolverCalculiX`, `Fem::MaterialCommon` to `makeMaterialSolid`, plus materials, element definitions, and `Fem::Constraint*` names. An unsupported or ambiguous type is an explicit error, not a guess.

Shapeless types are created successfully and report `solid_count: 0`. The attachment property routing for `create_feature` is in its section above.

The result carries the actual internal name and a post-recompute geometry report. FreeCAD sanitizes and de-duplicates names (`Box` may become `Box001`). Always use the returned name in later calls.

New volumetric geometry defaults to one solid; pass `expected_solids` to require a different count. Existing valid dependent solid counts are preserved when their inputs change.

Link-valued properties accept the shared target vocabulary. `Link` and `LinkList` positions take whole objects only — a selected subshape or query refuses `subshape_not_allowed`. `LinkSub` accepts exactly one shared target that resolves to one subshape. `LinkSubList` entries expand in order, are all validated before assignment, and cap the expanded pairs at 64 per operation. Query-origin link writes report `resolvedSelections` receipts with the selection-time generation.

### Structured CSG through `create_object`

Part booleans are document objects wired through canonical links — no script required. Subtraction:

```json
{
  "document": "Bracket",
  "type": "Part::Cut",
  "name": "BracketFinal",
  "properties": {"Base": {"object": "Blank"}, "Tool": {"object": "Bore"}},
  "expected_solids": 1,
  "expected_bounds": [0, 0, 0, 40, 30, 10]
}
```

`Base` and `Tool` are `PropertyLink` positions and accept whole objects only; a query target there refuses `subshape_not_allowed`. Fusion uses `Part::MultiFuse` with `Shapes` (a `PropertyLinkList`), for example `{"Shapes": [{"object": "Left"}, {"object": "Right"}]}`. Gate the result with `expected_solids`/`expected_bounds`, then confirm with `validate_geometry`. This is verified server wiring, not live boolean validation: `supportedTypes` listing `Part::Cut` and `Part::MultiFuse` proves availability, not the correctness of a boolean result, and untested boolean variants are not promoted on that basis. Scripts remain the escape hatch for the other Part shape operations.

`run_script` executes on the GUI thread in a namespace seeded with `FreeCAD`/`App` and `Gui`. Variables persist per `session_id` for the server's lifetime. At most 32 sessions are kept; new sessions are refused instead of evicting live state. stdout, stderr, and the traceback are captured even when the code raises. `timeout_s` is a cooperative server deadline (1–3600 s, default 90); execution cannot be preempted, and the tool result says so truthfully. The tool is refused with `SERVER_BUSY` while a FEM solve is active.

`close_document` does not save. Its result includes the document name, the path captured before close, and `discardedChanges`, which reports whether the pre-close state was dirty. Use `save_document` before close when the source must persist. Reopen and inspect the file when an independent persistence check is required.

Use `run_script` for operations outside the structured tools: `FreeCADGui` calls, selection, imports of formats `import_model` does not support (it covers STEP and STL behind file consent), Parts Library access, mesh routes, and specialized property assignments.

`run_script` reaches the native bindings directly and is not covered by the `edit_sketch` guard: malformed native constructor calls, such as an unsupported `Sketcher.Constraint` argument form, can raise an unhandled C++ exception that terminates the whole FreeCAD process.

## Recovery checkpoints

`recovery_enabled` plus an absolute `recovery_directory` turn on verified recovery copies. The directory is allowed automatically for reads and writes and needs no `allowed_roots` entry. When enabled, an expensive feature operation automatically checkpoints before the transaction opens, and the document is checked idle first so a busy document is refused before an unstable copy is captured. A checkpoint is an FCStd copy written through the native `saveCopy` path, reopened, and compared with the live document before the mutation proceeds; the server never prunes or deletes previous checkpoints. A failed checkpoint refuses the mutation with `VALIDATION_FAILED`, `reason: checkpoint_failed`, and `nextAction: inspect_recovery_directory`, and removes only the staging file it created.

Discovery reports `capabilities.recoveryEnabled` so clients can read the active policy.

## Errors

Application failures are complete tool results with `isError: true` and a structured `{code, message, details}` payload. Stable codes: `DOCUMENT_NOT_FOUND`, `OBJECT_NOT_FOUND`, `VALIDATION_FAILED`, `GUI_DISPATCH_FAILED`, `GUI_DISPATCH_STUCK`, `CONSENT_DENIED`, `PATH_NOT_ALLOWED`, `UNSUPPORTED_VIEW`, `SOLVER_FAILED`, `SERVER_BUSY`. Only protocol-level violations become JSON-RPC errors. Output-schema violations are infrastructure errors (`-32603`).

`edit_object`, `edit_objects`, `delete_object`, `edit_sketch`, and `edit_feature` accept an optional `expected_generation`. A mismatch fails with `VALIDATION_FAILED` and `reason: stale_generation` before any transaction opens, so nothing changes. The details carry `expectedGeneration`, `actualGeneration`, and the matching inspector (`inspect_objects` or `inspect_sketch`) as `nextTool`. On `edit_objects` the guard is top-level and refuses the whole batch.

Read `details.nextTool` when present and call that tool next: its value is always the name of a tool this server exposes, so it is safe to call directly. `details.nextAction` is a plain-language instruction, never a tool name — for example `retry_from_original_state`, `inspect_target`, or `inspect_recovery_directory`. Do not pass a `nextAction` value as a tool name. `details.reason` is the stable machine token for the refusal; `details.suggestions` lists close matches when a name or a type was rejected.

## Sources

- [FreeCAD MCP README](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/README.md)
- [Server orchestrator](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/addon/FreeCADMCP/mcp_server/server.py)
- [Tool modules](https://github.com/bradsjm/freecad-embedded-mcp/tree/main/addon/FreeCADMCP/mcp_server/tools)
- [GUI dispatch](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/addon/FreeCADMCP/mcp_server/gui_dispatch.py)
- [Dependency-free client example (FEM)](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/examples/cantilever_fem.py)
