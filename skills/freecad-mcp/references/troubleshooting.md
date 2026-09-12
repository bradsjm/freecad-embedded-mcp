# FreeCAD MCP troubleshooting

Use the smallest recovery step that matches the observed failure. Preserve user work. Never hide a failure by deleting or rescaling geometry.

Read `details.reason` first. When `details.nextTool` exists, call that tool next; never call a `details.nextAction` value as a tool. Error codes and payload shape: [mcp-tools.md](mcp-tools.md#errors).

## Contents

| Symptom | First action |
|---|---|
| [Connection unavailable](#connection-unavailable) | Call `discover_capabilities` off the GUI thread |
| [Wrong document or object](#wrong-document-or-object) | `inspect_documents` without arguments, then `inspect_objects` |
| [Unsupported type](#unsupported-type) | Read `supportedTypes` from `discover_capabilities(detail="full")` |
| [Property assignment failed](#property-assignment-failed) | `inspect_objects(detail="full")` for names, types, and metadata |
| [Mutation succeeded but the effect is wrong](#mutation-succeeded-but-the-effect-is-wrong) | Re-read the exact property, cell, link, or file effect |
| [Atomic batch failed](#atomic-batch-failed) | Treat as fully rolled back and correct the invalid entry |
| [Sketch edit failed](#sketch-edit-failed) | Read `details.reason`; treat the batch as rolled back |
| [Invalid or Touched object](#invalid-or-touched-object) | Inspect `state`, then the support and tool links |
| [Deadline exceeded with unknown outcome](#deadline-exceeded-with-unknown-outcome) | Stop GUI calls, then inspect actual document state |
| [Recovery checkpoint failed](#recovery-checkpoint-failed) | Inspect the recovery directory before any retry |
| [GUI dispatch stuck](#gui-dispatch-stuck) | Stop all GUI and document calls |
| [FEM failure](#fem-failure) | Confirm the analysis graph in `fem.md` |
| [Export mismatch](#export-mismatch) | Read the `export` readback, not hand-derived values |
| [Tool mount unavailable](#tool-mount-unavailable) | Recheck the client mount before diagnosing the model |

## Connection unavailable

1. Call `discover_capabilities`. It does not use the GUI thread. Treat one transport error as transient and re-query before concluding failure.
2. If the client still cannot connect, the embedded server is not running. It lives inside FreeCAD's GUI process, so enable auto-start in MCP Settings or enable the add-on's server start for unattended runs.
3. Match the client to the configured port, mode, and token: local mode needs no token, network mode requires the bearer token on every request.
4. `PATH_NOT_ALLOWED` means the path is outside `allowed_roots`. The configured absolute `recovery_directory` is allowed automatically.

Endpoint, modes, token, and path contract: [protocol-security.md](protocol-security.md#connection), [protocol-security.md](protocol-security.md#settings-and-path-access).

## Wrong document or object

1. Call `inspect_documents` with no arguments when the document name is unknown, and read its rows.
2. Call `inspect_objects(document)` on the intended document and reuse the internal `name` values it returns.
3. Never use a display `label` as an edit or link target.

## Unsupported type

1. Call `discover_capabilities` with `detail: "full"` and read `supportedTypes`.
2. Choose a registered type, load the workbench it needs, or build the shape deterministically through `run_script`.
3. Do not retry the same rejected type. Type resolution: [mcp-tools.md](mcp-tools.md#create_object-details).

## Property assignment failed

`edit_object` and `edit_objects` prevalidate every property before the transaction opens, so a failed call assigns nothing.

1. Inspect names, types, and metadata with `inspect_objects(document, detail="full")`.
2. Use plain numbers for quantity properties and canonical `{"object", "subelement"}` links for references.
3. For a `Spreadsheet::Sheet`, read the row's `spreadsheet` inventory and write address or alias keys through `edit_object` `properties.cells`.
4. Send feature-specific assignments the mapper cannot express through `run_script`.

Mapping rules: [mcp-tools.md](mcp-tools.md#property-mapping).

## Mutation succeeded but the effect is wrong

1. Re-read the exact property, cell, link, bounds, or file readback that the call should change.
2. For spreadsheet writes, require `cellContentsPersisted: true` and inspect the cell after recompute.
3. For export, compare the returned bounds and objects with the requested orientation and selection.
4. For save, require the `save_document` acknowledgement. Reopen only when independent persistence proof is required.
5. If the effect differs, stop dependent work and choose the correct tool or payload from `tools/list`.

Current tool schemas reject unknown arguments. Do not probe schemas with deliberate invalid calls when `tools/list` is available.

## Atomic batch failed

1. Treat the whole 1–32-entry `create_objects` or `edit_objects` batch as rolled back; the document is unchanged.
2. Read the error details, correct the invalid entry, and retry from the original document state.
3. `create_objects` does not resolve links inside the batch: create the entries, read `nameMapping`, then link them with `edit_objects`.

## Sketch edit failed

1. Treat the batch as rolled back. The document is unchanged and needs no cleanup.
2. `reason: stale_generation` means the target changed since your last read. Re-inspect the sketch, then retry with the current `expected_generation`.
3. A `VALIDATION_FAILED` refusal happened before any native call. Correct the datum or constraint form and retry; accepted constraint forms are in [sketcher.md](sketcher.md).
4. Add geometry and read `addedGeometry` before you constrain it in the same batch.
5. After a delete, re-read the indices; never reuse a pre-delete index.
6. An `Invalid` sketch after an accepted batch means a redundant or conflicting constraint that the native call does not reject. Delete the redundant constraint, then re-inspect.

Sketch call contract: [mcp-tools.md](mcp-tools.md#inspect_sketch-and-edit_sketch).

## Invalid or Touched object

Mutation tools reject `Invalid`, `Error`, `Touched`, and `isValid() == False` after recompute, and roll the document back on failure. Find the first failed dependency:

1. Inspect the feature's `state` and status text.
2. Inspect its support, profile, base, and tool links.
3. Check dimensions, placement, and subelement references.
4. Recompute after the smallest correction.
5. Validate the final Body Tip or Part feature with `validate_geometry`.

[Check Geometry](https://wiki.freecad.org/Part_CheckGeometry) diagnoses BRep faults and never repairs them; fix the modeling history instead. Gates: [validation.md](validation.md).

## Deadline exceeded with unknown outcome

A deadline does not prove that the operation failed or rolled back; it may have completed.

1. Stop GUI-thread requests and call `discover_capabilities`. Re-query health separately after one transport error.
2. Poll `tasks/get` for the terminal result of a detached call instead of assuming an outcome.
3. Inspect the target document's actual names, states, Tip, and shape validity, plus output-file existence. Do not trust variables assigned by the interrupted call.
4. Resume only the missing stage. Never replay creation, save, or export after an unknown outcome.
5. Keep mutation, expensive audits, export, and capture in separate bounded calls, and save a valid milestone with `save_document` before expensive work.

Deadlines and detached tasks: [protocol-security.md](protocol-security.md#limits-and-deadlines), [protocol-security.md](protocol-security.md#tasks-and-cancellation).

## Recovery checkpoint failed

A failed checkpoint refuses the mutation with `VALIDATION_FAILED`, `reason: checkpoint_failed`, and `nextAction: inspect_recovery_directory`, and removes only its staging file.

1. Inspect the recovery directory before retrying; never replay the refused mutation blind.
2. Save each validated milestone with `save_document`; do not wait for export, and preserve the last known-good source and exports.
3. After a relaunch, enumerate documents again: recovery can rename documents, restore older values, or leave none open.
4. Inspect Body membership, Tip, states, and parameter values, discard stale Python object references, and save the recovered valid state before further work.

Which operations checkpoint: [mcp-tools.md](mcp-tools.md#recovery-checkpoints).

## GUI dispatch stuck

1. If `gui.state` is `busy`, wait before dependent GUI calls.
2. If `gui.state` is `stuck`, stop all GUI and document calls.
3. Call `discover_capabilities` to identify the running operation and health; it is GUI-independent.
4. Wait for the running operation. It cannot be safely cancelled, and no parallel FEM or GUI call may start meanwhile.
5. Restart FreeCAD only if health does not return.

Dispatch health and limits: [protocol-security.md](protocol-security.md#gui-dispatch-health).

## FEM failure

1. Confirm the analysis graph: Part solid, material, generated Gmsh mesh, fixed and force/pressure constraints, and a modern `Fem::SolverCalculiX`. See [fem.md](fem.md).
2. Let `run_fem` select or create the modern solver. Legacy `Fem::SolverCcxTools` and ambiguous setups are explicit errors, never silent conversions.
3. On `SOLVER_FAILED`, report the actual solver error and its working directory.
4. A missing CalculiX installation is an actionable error; never auto-install it.
5. Cancellation is cooperative: a running solve is never killed.

## Export mismatch

Verify CAD-side facts only.

1. Read the `export` readback (mesh facets and bounds, STEP validity/solid count/volume, FCStd identity/placements) instead of re-deriving values by hand.
2. Confirm the millimetre assumption and compare the reported downstream dimensions with the `validate_geometry` global bounds.
3. Recheck which objects were exported: the readback echoes `objects`.
4. Revise orientation or geometry deliberately only when CAD evidence supports it.

Mechanics: [export-print.md](export-print.md), [validation.md](validation.md).

## Tool mount unavailable

Treat `No such tool` as a client or mount failure, not as evidence about the FreeCAD model.

1. Check that the FreeCAD MCP server remains mounted in the client session.
2. Call `discover_capabilities` after the mount returns.
3. Reinspect documents and objects before you resume mutation.
4. Do not infer model state from calls that never reached the server.
