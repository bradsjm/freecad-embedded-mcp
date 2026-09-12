---
name: freecad-mcp
description: "Design, inspect, edit, validate, simulate, and export FreeCAD models through the embedded FreeCAD MCP server. Use for parametric parts, CSG, sketches, PartDesign, assemblies, STEP/STL import, mesh repair, FEM, enclosures, manufacturing fit, and 3D-printable output. Also use to measure, repair, change, or export an existing FCStd model."
---

# FreeCAD MCP

Drive FreeCAD through the structured MCP tools. Use native CAD knowledge to choose the model strategy. Use this skill for tool order, server-specific payloads, and validation.

## Load only the required references

Follow this graph. Read each selected file directly from this page.

```text
START
├─ Need exact tool arguments or failure rules? → references/mcp-tools.md
├─ Need parameters, expressions, or spreadsheet cells? → references/fundamentals.md
├─ Need connection, auth, tasks, consent, or limits? → references/protocol-security.md
├─ Need a common call sequence or payload? → references/recipes.md
├─ Need to choose the model representation? → references/modeling.md
│  ├─ Sketch profile or constraints? → references/sketcher.md
│  ├─ Scripted Part or booleans? → references/part-topsolids.md
│  └─ Placement, attachment, or links? → references/placement-attachment.md
├─ Need document, property, unit, or identity rules? → references/fundamentals.md
├─ Need fit, bounds, topology, or final proof? → references/validation.md
├─ Need STL, STEP, 3MF, mesh import, or repair?
│  ├─ Standard export, mesh details, or repair? → references/export-print.md
│  └─ Python escape hatch? → references/python-export.md
├─ Need an FFF-printable design? → references/printability.md
├─ Need an electronic component or enclosure? → references/electronic-components.md
├─ Need FEM? → references/fem.md
├─ Did a call fail or time out? → references/troubleshooting.md
└─ Need external issue, release, or community evidence? → references/community-support.md
```

Read `references/mcp-tools.md` only when the active tool schema or a server edge case matters. Read `references/recipes.md` before composing a common multi-call workflow.

## Execute this loop

1. Call `discover_capabilities`.
2. If `gui.state` is busy, wait before dependent GUI calls.
3. If `gui.state` is stuck, stop GUI calls.
4. Call `inspect_documents` before you select an existing document.
5. Call `inspect_objects` before you edit an existing model.
6. Resolve only missing inputs that change geometry or authorization.
7. Choose the smallest correct model strategy.
8. Build one dependency stage.
9. Inspect returned names, states, bounds, and solid counts.
10. Re-read each changed property, cell, link, or file effect.
11. Validate the stage before you add dependent features.
12. Save the editable source before expensive or destructive work.
13. Validate geometry, fit, placement, and visual form.
14. Export only the intended final objects.
15. Report tool results as evidence, not as inferred success.

Reuse each returned internal `name`. Do not use a display `label` as an object reference. Use millimetres unless the live property or source specifies another unit.

## Choose the model strategy

Use existing FreeCAD and CAD knowledge first. Do not discover standard CAD operations by trial and error.

| Goal | Preferred strategy | Use instead when |
|---|---|---|
| One editable mechanical component | `PartDesign::Body` plus `create_feature` | Use scripted Part for compact deterministic CSG without feature-history requirements. |
| Simple primitive or supported object | `create_object` | Use `create_feature(kind="primitive")` when it must live inside a Body. |
| Several independent objects | `create_objects` | Use sequential calls when one object depends on a returned name. |
| Constrained feature profile | `create_feature(kind="sketch")`, then `edit_sketch` | Use scripted geometry only for unsupported sketch operations. |
| Native PartDesign feature | `create_feature` or `edit_feature` | Use `edit_object` only when the typed feature API does not cover the property. |
| Existing native property change | `edit_object` or `edit_objects` | Use `edit_parameters` for dynamic properties and expressions. |
| Independent scalar parameters | Supported `App::VarSet` plus `edit_parameters` | Use `Spreadsheet::Sheet` when formulas, aliases, or a parameter table matter. |
| Stable exact CSG | Short `run_script` with `Part` | Do not script when structured tools express the same design clearly. |
| Imported STEP or STL | `import_model` | Use `run_script` only for unsupported formats or mesh repair. |
| Fit or interference decision | `measure` plus `validate_geometry` | Use `inspect_topology` first when a specific face or edge matters. |
| Manufacturing output | `export` | Do not use ad hoc writer code for STL, STEP, 3MF, or FCStd. |

Prefer origin planes, datum geometry, named parameters, and master sketches. Avoid generated face and edge references when an upstream edit can change topology.

## Use the tools in canonical order

The server registers these 26 tools in this order. Use the phases below as the default call order.

1. **Discover:** `discover_capabilities`.
2. **Resolve documents:** `inspect_documents`, `new_document`, `open_document`, `import_model`, `save_document`, `close_document`, `reload_document`.
3. **Resolve objects:** `inspect_objects`.
4. **Mutate objects:** `create_object`, `create_objects`, `edit_object`, `edit_objects`, `delete_object`.
5. **Prove geometry:** `validate_geometry`, `measure`, `inspect_topology`.
6. **Control parameters:** `edit_parameters`.
7. **Control sketches:** `inspect_sketch`, `edit_sketch`.
8. **Control PartDesign:** `create_feature`, `edit_feature`.
9. **Deliver or analyze:** `export`, `capture_view`, `run_fem`.
10. **Escape only when required:** `run_script`.

This list covers every registered tool. Read [the exact tool matrix](references/mcp-tools.md#tool-matrix) before the first unfamiliar group call.

The protocol also provides `server/discover`, `tools/list`, `tasks/get`, `tasks/update`, `tasks/cancel`, `resources/list`, `resources/read`, and `subscriptions/listen`. Use `tools/list` for the live schemas. Poll detached `measure`, `export`, `run_fem`, or `run_script` calls with `tasks/get` until terminal. Treat cancellation as cooperative.

## Apply tool-use rules

- Use structured tools before `run_script`.
- Use `run_script` only for a named unsupported operation.
- Keep each script short, deterministic, and idempotent when possible.
- Never send unverified native `Sketcher.Constraint` constructor forms.
- Use `create_objects` only for independent entries.
- Read `nameMapping` before you create links between batch-created objects.
- Use `edit_objects` for one atomic multi-object change.
- Write spreadsheet cells only through `properties.cells` with address or alias keys.
- Require `cellContentsPersisted: true` after a spreadsheet write.
- Pass `expected_generation` on sketch or feature edits after an earlier inspection.
- Pass `expected_solids` and `expected_bounds` when the design determines them.
- Use canonical links: `{"object":"Name","subelement":"Face1"}`.
- Use document-space bounds: `[xmin,ymin,zmin,xmax,ymax,zmax]`.
- Use plain numbers for mapped quantity properties.
- Use quantity strings only where the schema requires strings, such as sketch datums or FEM material maps.
- Treat `close_document` as discard, never save.
- Re-read persistence-sensitive changes after recompute or save.
- Do not delete objects with unresolved dependents.
- Do not issue more GUI calls after `GUI_DISPATCH_STUCK`.

## Build with checkpoints

Build in this dependency order:

1. Create the document and base form.
2. Create stable references and profiles.
3. Add additive features.
4. Add subtractive features.
5. Add patterns and transforms.
6. Add thickness, fillets, and chamfers late.
7. Set final placement and visibility.
8. Validate and save the stage.

Use one transaction-sized tool call per coherent stage. Do not place the whole design in one long script. Save the last valid FCStd state before high-cost features.

Prefer sketch arcs for stable corner radii. Generated-edge fillets and chamfers can break after topology-changing upstream edits.

## Validate before delivery

Require all applicable evidence:

- The final object has no `Invalid`, `Error`, or `Touched` state.
- `validate_geometry` reports a valid shape.
- The solid count matches the design.
- The volume is positive when a solid is required.
- The bounds match the design within a stated tolerance.
- `measure(mode="distance")` confirms clearance magnitude.
- `measure(mode="interference")` confirms no volumetric overlap.
- `capture_view` confirms the requested form and placement.
- The export readback matches the source bounds and solid intent.

A positive distance alone does not prove separation. A zero common volume does not prove clearance. Use both modes for mating parts.

For FFF output, read `references/printability.md` before final geometry. Choose the build orientation before overhang-sensitive features. Mark unsourced fit values and uncalibrated clearances as assumptions.

## Handle failures

Read `details.reason` first. If `details.nextTool` exists, call that tool. Never call a `details.nextAction` value as a tool.

After an uncertain timeout, inspect state before retrying. A timed-out GUI operation can still complete and duplicate a repeated mutation. After `GUI_DISPATCH_STUCK`, call only GUI-independent `discover_capabilities` until health returns.

`run_script` is opt-in arbitrary Python with the FreeCAD user’s privileges. Never place credentials or untrusted code in it. Never print the bearer token.

## Finish with a compact report

Report:

- The document and final internal object names.
- The chosen representation and important named parameters.
- The validation result, solid count, volume, and bounds.
- The fit measurements and tolerance when applicable.
- The saved FCStd path and export readback.
- The print orientation and assumptions when applicable.
- Any unverified behavior or blocked operation.
