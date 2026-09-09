# FreeCAD MCP troubleshooting

Use the smallest recovery step that addresses the observed failure. Preserve user work and do not hide errors with arbitrary deletion or scaling.

## Tool and document failures

### Connection or server is unavailable

Call `discover_capabilities`. If the client cannot connect, the embedded server may not be running: start it with **Start MCP Server** from the MCP Addon workbench, or enable auto-start. The default endpoint is `http://127.0.0.1:9876/mcp` (port from settings). Local mode needs no token; remote mode requires the bearer token on every request, and `PATH_NOT_ALLOWED` on file tools means the path is outside `allowed_roots`.

### Wrong document or object

Call `inspect_objects(document)` on the intended document. Use the actual internal `Name` returned by create/inspection calls. Do not use a display `Label` as a link target. When the document name is unknown, enumerate `App.listDocuments()` through `run_script`.

### `not a document object type`

The requested type is not registered in the current session. `discover_capabilities` reports the complete `supportedTypes` list; this query through `run_script` also works:

```python
print(App.ActiveDocument.supportedTypes())
```

Then choose a registered type, load the relevant workbench/module if appropriate, or construct a deterministic `Part::Feature` shape through `run_script`. Do not repeatedly retry the same unknown type.

### Property assignment failed

Inspect property names, types, and metadata with `inspect_objects(document, detail="full")`. `edit_object` prevalidates every property before the transaction opens, so a failed call assigns nothing. Use plain numbers for quantity properties (internal units apply) and canonical `{"object", "subelement"}` links for references. Feature-specific assignments the mapper cannot express go through `run_script`.

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

## GUI dispatch stuck

If a call fails with `GUI_DISPATCH_STUCK`, or `discover_capabilities` reports a non-healthy `gui.state`:

1. Stop sending document/GUI requests.
2. Call `discover_capabilities` to identify the running operation and health; it is GUI-independent.
3. Wait for the running operation to finish; it cannot be safely cancelled.
4. If the state does not return to healthy, restart FreeCAD.

Do not force-cancel a running GUI operation or start parallel FEM/GUI calls.

## FEM failure

Confirm the analysis contains the solid, material, generated Gmsh mesh, fixed constraint, force/pressure constraint, and a modern `Fem::SolverCalculiX` solver; `run_fem` selects or creates the modern solver and refuses legacy `Fem::SolverCcxTools` or ambiguous setups. Missing CalculiX produces an actionable error, never an auto-install. A solver failure reports `SOLVER_FAILED`; report the actual error and working directory. Cancellation is cooperative: a running solve is never killed. Use the dependency-free [client example](https://github.com/neka-nat/freecad-mcp/blob/main/examples/cantilever_fem.py) as the working reference.

## Export mismatch

If a downstream consumer of an exported file reports a scale, placement, hole, or geometry problem, verify only the CAD-side facts:

- Use the `export` readback (mesh facets/bounds, STEP validity/solid count/volume, FCStd identity/placements) instead of re-deriving values by hand.
- Verify millimetre assumptions and compare reported downstream dimensions with the `validate_geometry` global bounds.
- Recheck which objects were exported; the result echoes the `objects` list.
- Revise CAD orientation or geometry deliberately when CAD evidence supports it.

## Sources

- [FreeCAD MCP README](https://github.com/neka-nat/freecad-mcp/blob/main/README.md)
- [Server orchestrator](https://github.com/neka-nat/freecad-mcp/blob/main/addon/FreeCADMCP/mcp_server/server.py)
- [GUI dispatch](https://github.com/neka-nat/freecad-mcp/blob/main/addon/FreeCADMCP/mcp_server/gui_dispatch.py)
- [Object validation](https://github.com/neka-nat/freecad-mcp/blob/main/addon/FreeCADMCP/mcp_server/object_validation.py)
- [Part Check Geometry](https://wiki.freecad.org/Part_CheckGeometry)
