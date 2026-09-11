---
name: freecad-mcp
description: "Automate creation, inspection, editing, validation, and export of FreeCAD models through the FreeCAD MCP tools. Use for new or existing mechanical parts, parametric CAD, boolean geometry, geometry validation, FEM-assisted design, STL/STEP/3MF export, and electronics enclosures. Also use when a part must fit a real device or connector, or when print orientation and printability shape the design."
---

# FreeCAD MCP automation

Use this skill only when a language model is driving a live FreeCAD 1.1 installation through the `freecad-mcp` MCP server. All operations in this skill must be performed through the embedded server's MCP tools; `run_script` is the arbitrary-Python path and runs synchronously on the FreeCAD GUI thread. Do not instruct a human to operate FreeCAD or a website.

Prefer a small, valid, inspectable model over a long unverified script. Keep dimensions in millimetres unless FreeCAD explicitly reports another unit.

When the user will manufacture the part by fused-filament fabrication, design for the manufacturing process and the real product the part must fit. Source each fit dimension from a primary reference. Choose the build orientation with the geometry, not after it.

Honor this user's preference: handle complex CAD design and model mutation directly, without subagents unless explicitly requested. Keep one owner of the live FreeCAD document and the persistent `run_script` sessions.

## Navigate the references

- [MCP tools and runtime contract](references/mcp-tools.md): exact tools, arguments, return data, consent, tasks, deadlines, failures, and security.
- [FreeCAD fundamentals](references/fundamentals.md): documents, object identity, properties, units, recompute, and App/GUI boundaries.
- [Part topology](references/part-topsolids.md): deterministic Part geometry, booleans, topology, validity, and refinement.
- [Modeling patterns](references/modeling.md): intake, dimension research, representation choice, dependency order, staged review, placement.
- [Parametric workflows](references/workflows.md): PartDesign, Sketcher, Draft, topological naming, and editing an existing model.
- [Sketcher profiles](references/sketcher.md): `inspect_sketch`/`edit_sketch` index model, the enforced constraint forms, and the rectangle recipe.
- [Placement and attachment](references/placement-attachment.md): transforms, supports, attachment offsets, links, and FreeCAD 1.1 orientation changes.
- [Geometry validation](references/validation.md): FreeCAD validity, topology, bounds, solid checks, the visual review checklist, and the final inspection gate.
- [Printability](references/printability.md): process constraints, build orientation, overhangs, minimum features, clearances, and machine-profile examples.
- [Python and export](references/python-export.md): safe `run_script` patterns, persistent script sessions, shape construction, and the structured export tool.
- [Mesh export, import, and repair](references/export-print.md): tessellation control, mesh formats, mesh import and repair routes, and the export report.
- [FEM through MCP](references/fem.md): analysis setup, Gmsh/CalculiX prerequisites, constraints, results, and known property limitations.
- [Troubleshooting](references/troubleshooting.md): server health, consent prompts, invalid objects, FEM failures, GUI dispatch, and export mismatches.
- [Electronic component models](references/electronic-components.md): use web research for source metadata, import neutral CAD through `import_model`, and validate dimensions/clearances.
- [Community support and project status](references/community-support.md): research-only access to FreeCAD Forum, GitHub Issues, Discussions, and Releases.
- [English FreeCAD wiki index](references/wiki-index.md): durable `https://wiki.freecad.org/` links grouped by task.

## Default operating procedure

1. **Check server health.** Call `discover_capabilities` first; it never waits for the GUI thread. Read `gui.state`. If it is not healthy, do not issue more GUI-thread operations. Call `discover_capabilities` again after a suspected timeout.
2. **Establish the requirements.** Resolve what the object is, its non-negotiable fit dimensions, and how it attaches. When the part will be printed, also resolve the process, material, nozzle, layer height, and relevant machine limits. Research each real-world interface dimension from a primary source, then record it as a named parameter with its source. Never guess a fit-critical value. See [requirements intake](references/modeling.md#establish-the-requirements-before-the-geometry) and [printability](references/printability.md#collect-process-inputs-only-when-they-matter).
3. **Inspect before mutating.** Track the actual document `name` returned by `new_document` or `open_document`. Call `inspect_documents` when the name is unknown; it takes no arguments, and its `documents[]` rows carry `generation`, `dirty`, `active`, `transactionOpen`, and `editObject`, plus the `activeDocument`. Call `inspect_objects(document)` for the target document. Preserve unrelated user work. Do not assume the active document is intended.
4. **Choose the modeling strategy.** Use PartDesign for a coherent single solid with a feature history. Use Part primitives, booleans, or scripted Part shapes for deterministic CSG. Use `run_script` for operations the structured tools cannot express reliably.
5. **Shapeless and null-shape objects are valid.** `create_object` creates `PartDesign::Body` and `Part::Feature` successfully and reports `solid_count: 0` until the object holds a solid. A positive `expected_solids` on a null-shape object fails with `has no geometry; expected_solids=N cannot be satisfied`; drop the contract or fill the object first. Do not bootstrap bodies through `run_script`.
6. **Build in reviewable stages.** Build the base form, verify it, and save a checkpoint before you add the functional features. Finish with fillets, chamfers, and edge cleanup. Capture a view and validate at each stage. See [build in reviewable stages](references/modeling.md#build-in-reviewable-stages).
7. **Build in dependency order.** Create base geometry, references/sketches/features, cuts/dress-ups/patterns. `create_object` and `edit_object` recompute inside an MCP-owned transaction and validate dependents and solid counts. Inspect returned names and states after each meaningful stage.
   Save a checkpoint after each validated modeling stage, before expensive checks or cleanup. Run parameter experiments in a separate validation copy. Change, check, and restore one parameter at a time. See [checkpoint and crash recovery](references/troubleshooting.md#checkpoint-and-crash-recovery).
8. **Use returned names.** FreeCAD sanitizes and de-duplicates names. Use actual internal `Name` values for all later edits, references, and deletes.
9. **Keep document and GUI work on the GUI thread.** Use the structured tools for their covered operations: `discover_capabilities`, `inspect_documents`, `new_document`, `open_document`, `import_model`, `save_document`, `close_document`, `reload_document`, `inspect_objects`, `create_object`, `edit_object`, `edit_objects`, `edit_parameters`, `delete_object`, `validate_geometry`, `measure`, `inspect_topology`, `inspect_sketch`, `edit_sketch`, `create_feature`, `edit_feature`, `export`, `capture_view`, and `run_fem`. Use `run_script` for `FreeCADGui`, selection, mesh formats `import_model` does not support, and anything else the structured tools do not cover. There is no asynchronous execution tool. `run_fem`, `run_script`, `export`, and `measure` may detach as tasks. Poll `tasks/get`, and treat the terminal task result as the only completion signal.
10. **Validate before export.** Require valid recomputed geometry, positive-volume solids where intended, and correct topology. Run `validate_geometry(document, objects=[...])` for state, validity, solid count, volume, bounds, and tolerance. Assert `expected_bounds` when the intended placement is known. Run `measure` for distance, interference, section, and face screens. Do not list a zero-solid sketch in a call that carries `expected_solids`. See [Geometry validation](references/validation.md) for the full gate.
11. **Export the result.** Save the editable source with `save_document`. Use the `export` tool for STL, STEP, 3MF, or a native FCStd copy. It writes to a temporary sibling file, verifies the result by readback, and requires consent to overwrite an existing destination. Report the readback values.
12. **Report manufacturing recommendations when applicable.** For a printed part, state the build orientation, support requirement, starting process profile, and material. Distinguish source facts, computed results, and heuristics. Mark each uncalibrated clearance or fit value as an assumption. State when work is outside the available MCP boundary.

## Fast decision tree

- **Electronic component in an enclosure:** read [Electronic component models](references/electronic-components.md), research exact part metadata, import neutral CAD through `import_model`, and check keep-outs separately from the visible shell.
- **Anything that wraps, clips onto, or mates with a real product:** source every fit dimension from a primary reference. Record the source next to the parameter. Never round a non-negotiable fit. See [source the fit dimensions](references/modeling.md#source-the-fit-dimensions-of-real-products).
- **Part intended for fused-filament fabrication:** confirm the process inputs that affect the geometry. Derive wall thickness, minimum features, clearances, and build orientation from those inputs. Use a known machine profile only as an example or explicit user constraint. See [Printability](references/printability.md).
- **Parametric mechanical part:** create the `PartDesign::Body` with `create_object`. Create the sketch and its attachment with `create_feature` (`support` plus `properties.MapMode`). Draw and constrain the profile with `edit_sketch`. Create the Pad with `create_feature` and typed `parameters` (for example `{"extent": "distance", "length": 10}`). Validate the Body tip after every feature.
- **Constrained profile:** read the sketch with `inspect_sketch`, then change it with `edit_sketch` in one atomic batch. Datum strings convert to native quantities; a string that is not a valid quantity fails before the transaction opens. Confirm the returned geometry and constraint indices before you add dependents. See [Sketcher profiles](references/sketcher.md).
- **Null shape object:** a null-shape object is valid and reports zero solids. A positive `expected_solids` on it fails; drop the contract or give the object geometry first.
- **Unregistered generic type:** `discover_capabilities` with `detail: "full"` returns the complete `supportedTypes` list. If the type is absent, build the shape with `Part` and assign it to `Part::Feature` through `run_script`.
- Treat `run_script` as arbitrary code execution inside FreeCAD. Never paste untrusted code, credentials, or destructive filesystem operations without a clear user request. Malformed native constructor calls, such as an unsupported `Sketcher.Constraint` argument form, can raise an unhandled C++ exception that terminates the whole FreeCAD process; `edit_sketch` refuses constraint shapes without a recorded native acceptance before execution for this reason, so scripted constraint construction must use only the forms the structured tools accept or forms verified against the native contract.
- Keep remote mode disabled unless necessary. Remote mode rebinds the server to all interfaces and makes the bearer token mandatory on every request; the token is full local code-execution authority. Never log or print it.
- Run dependent document operations sequentially. When a tool detaches as a task, poll `tasks/get`; cancellation through `tasks/cancel` is cooperative and never kills a running CalculiX solve.
- If a tool call fails with `GUI_DISPATCH_STUCK`, or `discover_capabilities` reports a non-healthy `gui.state`, stop GUI-thread calls. Later GUI operations are rejected until the running operation finishes; restart FreeCAD if health does not return.
- Do not continue building on an object whose state is `Invalid`, `Error`, or `Touched`, or whose shape validity check fails.
- Do not delete user objects or close/reload documents unless the requested task requires it; closing or reloading a dirty document discards unsaved changes after consent.
- Do not describe manual FreeCAD actions as if they were MCP capabilities.

## Version and source scope

Target FreeCAD release is 1.1; the add-in fails closed outside `1.1.3 <= version < 1.2`. The MCP server is embedded in the add-on at `addon/FreeCADMCP/mcp_server/` in the freecad-embedded-mcp repository; the default endpoint is `http://127.0.0.1:9876/mcp`. Use English FreeCAD wiki pages only, linked through `https://wiki.freecad.org/`.
