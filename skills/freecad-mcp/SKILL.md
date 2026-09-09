---
name: freecad-mcp
description: "Automate creation, inspection, editing, validation, and export of FreeCAD models through the FreeCAD MCP tools. Use for new or existing mechanical parts, parametric CAD, boolean geometry, geometry validation, FEM-assisted design, STL/STEP/3MF export, and electronics enclosures."
---

# FreeCAD MCP automation

Use this skill only when a language model is driving a live FreeCAD 1.1 installation through the `freecad-mcp` MCP server. All operations in this skill must be performed through the embedded server's MCP tools; `run_script` is the arbitrary-Python path and runs synchronously on the FreeCAD GUI thread. Do not instruct a human to operate FreeCAD or a website.

Prefer a small, valid, inspectable model over a long unverified script. Keep dimensions in millimetres unless FreeCAD explicitly reports another unit.

Honor this user's preference: handle complex CAD design and model mutation directly, without subagents unless explicitly requested. Keep one owner of the live FreeCAD document and the persistent `run_script` sessions.

## Navigate the references

- [MCP tools and runtime contract](references/mcp-tools.md): exact tools, arguments, return data, consent, tasks, deadlines, failures, and security.
- [FreeCAD fundamentals](references/fundamentals.md): documents, object identity, properties, units, recompute, and App/GUI boundaries.
- [Part topology](references/part-topsolids.md): deterministic Part geometry, booleans, topology, validity, and refinement.
- [Modeling patterns](references/modeling.md): choosing Part vs PartDesign vs scripted geometry, dependency order, property payloads, placement, and robust edits.
- [Parametric workflows](references/workflows.md): PartDesign, Sketcher, Draft, topological naming, and editing an existing model.
- [Placement and attachment](references/placement-attachment.md): transforms, supports, attachment offsets, links, and FreeCAD 1.1 orientation changes.
- [Geometry validation](references/validation.md): FreeCAD validity, topology, bounds, solid checks, and the final inspection gate.
- [Python and export](references/python-export.md): safe `run_script` patterns, persistent script sessions, shape construction, and the structured export tool.
- [Mesh export, import, and repair](references/export-print.md): tessellation control, mesh formats, mesh import and repair routes, and the export report.
- [FEM through MCP](references/fem.md): analysis setup, Gmsh/CalculiX prerequisites, constraints, results, and known property limitations.
- [Troubleshooting](references/troubleshooting.md): server health, consent prompts, invalid objects, FEM failures, GUI dispatch, and export mismatches.
- [Electronic component models](references/electronic-components.md): use web research for source metadata, import neutral CAD through `run_script`, and validate dimensions/clearances.
- [Community support and project status](references/community-support.md): research-only access to FreeCAD Forum, GitHub Issues, Discussions, and Releases.
- [English FreeCAD wiki index](references/wiki-index.md): durable `https://wiki.freecad.org/` links grouped by task.

## Default operating procedure

1. **Check server health.** Call `discover_capabilities` first; it never waits for the GUI thread. Read `gui.state`. If it is not healthy, do not issue more GUI-thread operations. Call `discover_capabilities` again after a suspected timeout.
2. **Inspect before mutating.** Track the actual document `name` returned by `new_document` or `open_document`. There is no list-documents tool; when the name is unknown, call `run_script` with `App.listDocuments()`. Call `inspect_objects(document)` for the target document. Preserve unrelated user work; do not assume the active document is intended.
3. **Choose the modeling strategy.** Use PartDesign for a coherent single solid with a feature history; use Part primitives/booleans or scripted Part shapes for deterministic CSG; use `run_script` for operations not expressible reliably through the structured tools.
4. **Build in dependency order.** Create base geometry, references/sketches/features, cuts/dress-ups/patterns. `create_object` and `edit_object` recompute inside an MCP-owned transaction and validate dependents and solid counts. Inspect returned names and states after each meaningful stage.
5. **Use returned names.** FreeCAD sanitizes and de-duplicates names. Use actual internal `Name` values for all later edits, references, and deletes.
6. **Keep document and GUI work on the GUI thread.** Use the structured tools for their covered operations: `new_document`, `open_document`, `save_document`, `close_document`, `reload_document`, `create_object`, `edit_object`, `edit_parameters`, `delete_object`, `export`, `validate_geometry`, `measure`, `capture_view`, and `run_fem`. Use `run_script` for `FreeCADGui`, selection, imports, and anything else the structured tools do not cover. There is no asynchronous execution tool. `run_fem`, `run_script`, `export`, and `measure` may detach as tasks; poll `tasks/get`, and treat the terminal task result as the only completion signal.
7. **Validate before export.** Require valid recomputed geometry, positive-volume solids where intended, and correct topology. Run `validate_geometry(document, objects=[...])` for state, validity, solid count, volume, bounds, and tolerance; assert `expected_bounds` when the intended placement is known; run `measure` for distance, interference, section, and face screens. See [Geometry validation](references/validation.md) for the full gate.
8. **Export the result.** Save the editable source with `save_document`. Use the `export` tool for STL, STEP, 3MF, or a native FCStd copy; it writes to a temporary sibling file, verifies the result by readback, and requires consent to overwrite an existing destination. Report the readback values.
9. **Report limits honestly.** Distinguish source facts, computed results, and heuristics. State when work is outside the available MCP boundary.

## Fast decision tree

- **Electronic component in an enclosure:** read [Electronic component models](references/electronic-components.md), research exact part metadata, import neutral CAD through `run_script`, and check keep-outs separately from the visible shell.
- **Parametric mechanical part:** create a `PartDesign::Body`, then use registered sketch/feature types or scripted PartDesign operations; validate the Body tip after every feature.
- **Unregistered generic type:** `discover_capabilities` returns the complete `supportedTypes` list. If the type is absent, build the shape with `Part` and assign it to `Part::Feature` through `run_script`.
- Treat `run_script` as arbitrary code execution inside FreeCAD. Never paste untrusted code, credentials, or destructive filesystem operations without a clear user request.
- Keep remote mode disabled unless necessary. Remote mode rebinds the server to all interfaces and makes the bearer token mandatory on every request; the token is full local code-execution authority. Never log or print it.
- Run dependent document operations sequentially. When a tool detaches as a task, poll `tasks/get`; cancellation through `tasks/cancel` is cooperative and never kills a running CalculiX solve.
- If a tool call fails with `GUI_DISPATCH_STUCK`, or `discover_capabilities` reports a non-healthy `gui.state`, stop GUI-thread calls. Later GUI operations are rejected until the running operation finishes; restart FreeCAD if health does not return.
- Do not continue building on an object whose state is `Invalid`, `Error`, or `Touched`, or whose shape validity check fails.
- Do not delete user objects or close/reload documents unless the requested task requires it; closing or reloading a dirty document discards unsaved changes after consent.
- Do not describe manual FreeCAD actions as if they were MCP capabilities.

## Version and source scope

Target FreeCAD release is 1.1; the add-in fails closed outside `1.1.3 <= version < 1.2`, and the live environment previously reported 1.1.3. The MCP server is embedded in the add-on at `addon/FreeCADMCP/mcp_server/` in the freecad-embedded-mcp repository; the default endpoint is `http://127.0.0.1:9876/mcp`. Use English FreeCAD wiki pages only, linked through `https://wiki.freecad.org/`.
