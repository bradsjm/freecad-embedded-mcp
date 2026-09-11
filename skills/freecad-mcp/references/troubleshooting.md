# FreeCAD MCP troubleshooting

Use the smallest recovery step that addresses the observed failure. Preserve user work and do not hide errors with arbitrary deletion or scaling.

## Tool and document failures

### Connection or server is unavailable

Call `discover_capabilities`. If the client cannot connect, the embedded server may not be running: start it with the **Start MCP Server** toolbar action in the MCP Addon workbench, or enable auto-start in **MCP Settings**. The default endpoint is `http://127.0.0.1:9876/mcp` (port from settings). Local mode needs no token; network-access mode requires the bearer token on every request, and `PATH_NOT_ALLOWED` on file tools means the path is outside `allowed_roots`.

### Wrong document or object

Call `inspect_objects(document)` on the intended document. Use the actual internal `Name` returned by create/inspection calls. Do not use a display `Label` as a link target. When the document name is unknown, call `inspect_documents` (no arguments) and read its rows.

### `not a document object type`

The requested type is not registered in the current session. `discover_capabilities` reports the complete `supportedTypes` list.

Then choose a registered type, load the relevant workbench/module if appropriate, or construct a deterministic `Part::Feature` shape through `run_script`. Do not repeatedly retry the same unknown type.

### Property assignment failed

Inspect property names, types, and metadata with `inspect_objects(document, detail="full")`. `edit_object` prevalidates every property before the transaction opens, so a failed call assigns nothing. Use plain numbers for quantity properties (internal units apply) and canonical `{"object", "subelement"}` links for references. Feature-specific assignments the mapper cannot express go through `run_script`.


## Sketch edit failed

`edit_sketch` is atomic: a rejected entry rolls back the whole batch and reports `nextTool: inspect_sketch`. The document is unchanged, so no cleanup is needed. Pass `expected_generation` to refuse a batch whose target changed since you last read it; a mismatch fails before the transaction opens and the details carry `reason: stale_generation`, `expectedGeneration`, `actualGeneration`, and `nextTool: inspect_sketch`.

- A `setDatums` datum that is not a valid quantity fails with `VALIDATION_FAILED` before the transaction opens. Correct the datum string; the native call always receives a `FreeCAD.Units.Quantity`.
- `VALIDATION_FAILED` naming the accepted forms means the server rejected the constraint entry before any native call. `Collinear`, `InternalAlignment`, `SnellsLaw`, `AngleViaPoint`, and `Weight` are rejected without a native call, because the native 1.1.3 constructor accepted no verified form of them. Use `Tangent` between two lines, or use `run_script` with a form recorded in `tests/native_contract.json`.
- A batch that used geometry indices for constraints added in the same batch fails or constrains the wrong element. Add the geometry, read the returned `addedGeometry` indices, then add the constraints.
- Deleting geometry or constraints renumbers the remaining rows. Never reuse a pre-delete index after a delete.

A sketch that reports `Invalid` after a batch accepted a conflicting or redundant constraint. The native `addConstraint` does not reject it. Delete the redundant constraint, then re-read the sketch. A conflicting pair and a redundant pair each produced a negative `solve()` result and state `['Touched', 'Invalid']` on FreeCAD 1.1.3.

## FreeCAD crash during a live session

The client connection dropping mid-call may mean FreeCAD died. A dropped connection alone does not prove it: check for a live process first.

```bash
pgrep -f FreeCAD
```

When FreeCAD died, read the newest crash report and the triggered thread frames:

```bash
ls -t ~/Library/Logs/DiagnosticReports/freecad-*.ips | head -1
```

The `.ips` file holds two JSON documents: the first line is one object, the remainder is a second. Read `exception`, `termination`, and the frames of the thread whose `triggered` key is true.

When you verify work, stop with the reason. Record the step, the journal path, and the crash-report path. Do not restart FreeCAD and do not retry the step.

When you develop tests, restart FreeCAD and resume only with the `--dev` mode of `examples/native_contract_probe.py`:

```bash
osascript -e 'quit app "FreeCAD"'
osascript -e 'tell application "FreeCAD" to activate'
```

When `osascript activate` fails, use `open -a FreeCAD`. Never resume by replaying the interrupted call. Recreate the probe document, confirm `discover_capabilities` reports `gui.state == "healthy"` with `queuedJobs == 0`, and complete one trivial GUI-thread call before the next step.

## Invalid or touched object

The mutation tools validate after recompute and reject states such as `Invalid`, `Error`, and `Touched`, as well as `isValid()==False`; a failed mutation rolls the document back and reports rollback failures separately. Find the first failed dependency:

1. Inspect the feature's `state` and status string.
2. Inspect its support/profile/base/tool links.
3. Check dimensions, placement, and subelement references.
4. Recompute after the smallest correction.
5. Validate the final Body Tip or Part feature with `validate_geometry`.

[Check Geometry](https://wiki.freecad.org/Part_CheckGeometry) diagnoses BRep issues but does not repair them automatically.

## Deadline exceeded with unknown outcome

A tool deadline (60 s default) does not prove that the operation failed or rolled back. In an observed session, a timed-out script had completed its feature creation; replaying it would have duplicated objects.

1. Stop GUI-thread requests and call `discover_capabilities`; it never waits for the GUI thread. Treat one transport error as transient and re-query health separately.
2. If the call detached as a task, poll `tasks/get` for its terminal result instead of assuming an outcome.
3. Once healthy, inspect the target document's actual object names, states, Body Tip, and shape validity with `inspect_objects` and `validate_geometry`, plus output-file existence where relevant. Do not rely solely on variables assigned by the interrupted call.
4. Resume only the missing stage. Never blindly replay document creation, feature additions, save, or export after an unknown outcome.
5. Keep geometry mutation, expensive boolean audits, export, and screenshot capture in separate bounded calls. Save a valid milestone with `save_document` before expensive checks; this does not authorize exporting unvalidated geometry.

## Checkpoint and crash recovery

Save each validated milestone with `save_document`; do not wait until export. Preserve the last known-good source and exports during experiments. A transaction or `finally` block cannot guarantee restoration after a process crash.

1. Create a separate validation document before parameter sweeps or expensive geometry checks.
2. Separate each mutation, recompute, inspection, and restoration into bounded calls.
3. Start with the smallest functional probe. Avoid large GUI-thread loops of booleans or point-in-solid queries.
4. After an aborted call, check `discover_capabilities` before issuing another document request. A busy server is not proof of a crash.
5. After relaunch, enumerate documents again. Recovery can change document names, restore older values, or leave no documents open.
6. Inspect Body membership, Tip, states, and parameter values before resuming. Discard stale Python object references from the previous process.
7. Save the recovered valid state before further experiments. Resume only stages whose completion is confirmed.

Observed on 2026-09-09: crashes followed a combined parameter/boolean batch and a large `isInside()` surface-travel loop. The operation preceding each crash was known, but no crash-log diagnosis established the cause. Do not claim either API is inherently unsafe. Do not repeat the same workload unchanged after a crash. Isolated height-change, point-probe, and restoration calls later completed successfully; this is limited evidence, not a general stability guarantee.

Deleting a Body did not remove its old feature chains in this session. Inspect dependencies before cleanup. A clean document made with `copyObject([active bodies], recursive=True)` retained the active dependency graphs without those obsolete chains. Verify copied names, links, states, and external workbench dependencies before using this approach. Preserve the source until the clean checkpoint is saved and checked.

## GUI dispatch stuck

If a call fails with `GUI_DISPATCH_STUCK`, or `discover_capabilities` reports a non-healthy `gui.state`:

1. Stop sending document/GUI requests.
2. Call `discover_capabilities` to identify the running operation and health; it is GUI-independent.
3. Wait for the running operation to finish; it cannot be safely cancelled.
4. If the state does not return to healthy, restart FreeCAD.

Do not force-cancel a running GUI operation or start parallel FEM/GUI calls.

## FEM failure

Confirm the analysis contains the solid, material, generated Gmsh mesh, fixed constraint, force/pressure constraint, and a modern `Fem::SolverCalculiX` solver; `run_fem` selects or creates the modern solver and refuses legacy `Fem::SolverCcxTools` or ambiguous setups. Missing CalculiX produces an actionable error, never an auto-install. A solver failure reports `SOLVER_FAILED`; report the actual error and working directory. Cancellation is cooperative: a running solve is never killed. Use the dependency-free [client example](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/examples/cantilever_fem.py) as the working reference.

## Export mismatch

If a downstream consumer of an exported file reports a scale, placement, hole, or geometry problem, verify only the CAD-side facts:

- Use the `export` readback (mesh facets/bounds, STEP validity/solid count/volume, FCStd identity/placements) instead of re-deriving values by hand.
- Verify millimetre assumptions and compare reported downstream dimensions with the `validate_geometry` global bounds.
- Recheck which objects were exported; the result echoes the `objects` list.
- Revise CAD orientation or geometry deliberately when CAD evidence supports it.

## Sources

- [FreeCAD MCP README](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/README.md)
- [Server orchestrator](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/addon/FreeCADMCP/mcp_server/server.py)
- [GUI dispatch](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/addon/FreeCADMCP/mcp_server/gui_dispatch.py)
- [Object validation](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/addon/FreeCADMCP/mcp_server/object_validation.py)
- [Part Check Geometry](https://wiki.freecad.org/Part_CheckGeometry)
