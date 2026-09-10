# FreeCAD MCP tool contract

Use this file for exact tool behavior. The authoritative implementation is the [FreeCAD MCP repository](https://github.com/bradsjm/freecad-embedded-mcp) under [`addon/FreeCADMCP/mcp_server/`](https://github.com/bradsjm/freecad-embedded-mcp/tree/main/addon/FreeCADMCP/mcp_server): `server.py` (registry, dispatch, deadlines, consent), `protocol.py` (wire contract), `http_server.py` (transport and auth), `gui_dispatch.py` (GUI-thread bridge), and `tools/` (one module per tool domain).

## Architecture

The add-on embeds the MCP server inside FreeCAD's GUI process. There is no separate server package and no second bridge process.

1. The server speaks JSON-RPC over streamable HTTP with SSE at `http://127.0.0.1:9876/mcp` (port from settings). Current-protocol clients send the `MCP-Protocol-Version` header (`2026-07-28`) and the `Mcp-Method`/`Mcp-Name` request-metadata headers. Clients that speak the 2025 revisions (`2025-03-26`, `2025-06-18`, `2025-11-25`) connect without changes and address the session with the returned `MCP-Session-Id`; an unsupported offered version negotiates `2025-11-25`.
2. HTTP worker threads never touch FreeCAD. Every document or GUI operation is dispatched onto FreeCAD's main GUI thread through a queue drained by the Qt loop. One long GUI operation delays later GUI operations; there is one shared dispatch.

`run_script` executes arbitrary Python inside the FreeCAD process with the user's privileges. It is deliberately not sandboxed.

## Tool matrix

The server exposes 24 tools in a fixed order. Document tools return the actual sanitized `name`, `label`, and `objectCount`; use the returned `name` as the `document` argument in later calls.

| Tool | Use | Important arguments |
|---|---|---|
| `discover_capabilities` | Versions, workbenches, supported types, exporter and FEM availability, GUI dispatch health | optional `refresh` (default `false`; `true` re-captures through the GUI path) and `detail` (`compact` default or `full`); GUI-independent without `refresh` |
| `inspect_documents` | Open-document inventory with generation, dirty/active flags, and transaction state | none |
| `new_document` | Create an empty document | `name` |
| `open_document` | Open an `.FCStd` from an allowed root | `path`; `untrusted` defaults true and requires consent |
| `import_model` | Import STEP or STL behind file consent | `document`, `path`, `format`; optional `name` (STL mesh feature) |
| `save_document` | Save to the existing path, or save-as | `document`; optional `path` (consent to overwrite a different existing file) |
| `close_document` | Close one document | `document`; consent when dirty or unsaved nonempty |
| `reload_document` | Close and reopen the saved file | `document`; consent to discard unsaved changes |
| `inspect_objects` | List objects sorted by Name, or a 1–64 object selection; signed-cursor pagination | `document`; optional `objects`, `cursor`, `detail` (`compact`/`full`), `property_filter`, `limit` (default 32, max 500), `property_offset`, `property_limit` |
| `create_object` | Create a supported Part/App type or a FEM object | `document`, `type`, `name`; optional `properties`, `expected_solids`, `expected_bounds`, `bounds_tolerance` |
| `edit_object` | Assign properties with full prevalidation; returns before/after deltas | `document`, `object`, `properties`; optional `expected_solids`, `expected_bounds`, `bounds_tolerance` |
| `edit_objects` | Edit 1–32 objects atomically | `document`, `edits`; optional `expectations` per object |
| `delete_object` | Delete one object; refuses objects with dependents | `document`, `object` |
| `validate_geometry` | State, validity, solid count, volume, bounds, tolerance | `document`, `objects` (max 100); optional `expected_solids`, `expected_bounds`, `bounds_tolerance` |
| `measure` | Distance, interference, section, or face measurement | `document`, `a`, `mode`; optional `b`, `plane`; selectors accept names, bbox objects, or signed `{object, subelement}` references |
| `inspect_topology` | Page through faces or edges with signed references | `document`, `object`, `role`; optional `cursor`, `limit` (default 50, max 100) |
| `edit_parameters` | Add/rename dynamic properties, bind expressions, clear expressions; reports `document`, `generation`, and `applied` | `document`, `object`; optional `add`, `rename`, `expressions`, `clear_expressions` |
| `inspect_sketch` | Sketch geometry/constraint rows, solver summary, `state`, `statusText`, and `solver.solverStatus` | `document`, `sketch` |
| `edit_sketch` | Atomic sketch batch: geometry, constraints, datums, deletes | `document`, `sketch`; optional `addGeometry`, `addConstraints`, `setDatums`, `deleteGeometry`, `deleteConstraints`, `expected_generation` |
| `create_feature` | Datum plane, sketch, pad, pocket, or hole inside a Body | `document`, `body`, `kind`, `name`; optional `properties`, `profile`, `support`, `expected_solids`, `expected_bounds`, `bounds_tolerance` |
| `export` | STL/STEP/3MF or native FCStd copy with readback verification | `document`, `objects`, `format`, `path`; optional `linear_deflection`, `angular_deflection`, `bed_align` |
| `capture_view` | PNG of the 3D view with an explicit orientation | `document`, `focus_object`, `view_name`; optional `width`, `height` |
| `run_fem` | Modern CalculiX solve; returns a VTK result summary | `document`, `analysis`; optional `timeout_s` (default 600) |
| `run_script` | Arbitrary Python on the GUI thread in a persistent session namespace | `code`; optional `session_id` (default `"default"`), `timeout_s` (default 90) |

`discover_capabilities` with `detail: "compact"` (the default) returns `freecad`, `occ`, `exporters`, `fem`, `supportedTypesCount`, and `supportedTypesDocument`; `detail: "full"` returns the complete snapshot. `refresh: true` re-captures the snapshot through the GUI path.

`inspect_documents` takes no arguments. It returns `documents[]` rows with `name`, `label`, `fileName`, `objectCount`, `generation`, `dirty`, `active`, `transactionOpen`, and `editObject`, plus `activeDocument`. Use it when the document name is unknown; it replaces document discovery through `run_script`.

The tools cover CAD-side modeling, inspection, validation, export, and FEM operations only. Report anything outside these operations as outside this skill's boundary.

Valid `view_name` values are `Isometric`, `Front`, `Top`, `Right`, `Back`, `Left`, `Bottom`, `Dimetric`, and `Trimetric`. `capture_view` returns base64 PNG content. An omitted size is clamped to a 1024 px longest edge; an explicit size up to 4096 px is honored. The caller's selection and active document are preserved.

## Standard sequence

1. Call `discover_capabilities`. Read `gui.state`, exporter/FEM availability, and the supported-type inventory; add `detail: "full"` when the complete `supportedTypes` list is needed.
2. Address the target document by the `name` returned by `new_document` or `open_document`. When the name is unknown, call `inspect_documents` and read its rows before choosing the target.
3. Call `inspect_objects(document)` and read the compact rows before editing.
4. Create or edit one dependency stage at a time; inspect after each recompute.
5. Run `validate_geometry` and `measure` on the final solid.
6. Call `export`, then `capture_view` from the most informative orientation when useful.

## Property mapping

`create_object` and `edit_object` map JSON-like values onto native FreeCAD property types. Every property is prevalidated before the transaction opens, so a later invalid property leaves earlier ones unchanged.

- Placement properties take `{"position": [x, y, z], "axis": [x, y, z], "angle_deg": n}`.
- Vector properties take `{"x": n, "y": n, "z": n}` or `[x, y, z]`.
- Link and link-sub properties take only the canonical form `{"object": "<Name>", "subelement": ""}` or `{"object": "<Name>", "subelement": "Face1"}`; link lists take arrays of these values.
- Color properties take `[r, g, b]` or `[r, g, b, a]`.
- Quantity and float properties (`App::PropertyQuantity`, `Distance`, `Length`, `Angle`, `Speed`, `Area`, `Volume`, `Percent`) take plain JSON numbers; the value lands in the property's internal unit.
- Enumeration properties take the exact string; validation reports the allowed values.
- Prefix a key with `ViewObject.` to target a view property explicitly; an unprefixed key resolves against the document object first and the ViewObject as fallback.

A failure during the transaction aborts the whole operation, recomputes the restored document, and reports rollback failure separately. Change summaries from `create_object`, `edit_object`, and `edit_objects` report `dependentCountBefore` alongside `dependentCount`, the post-mutation count. Feature-specific assignments the mapper cannot express go through `run_script`.

## Inspection response

Compact `inspect_objects` rows carry `name`, `label`, `typeId`, `state`, `bounds`, `shape_valid`, `solid_count`, `tip`, and `links`. `detail: "full"` adds `placement`, `globalPlacement`, and the property pages: `properties` for the requested `property_filter` (or all names), plus `propertyMetadata` (type, read-only, enumeration), `propertyCount`, `nextPropertyOffset`, and `truncatedProperties`. The row `limit` defaults to 32 (max 500). Use `property_offset` and `property_limit` (default 64) to page large property sets. Bounds are document-space millimetres. Pagination uses an opaque signed cursor bound to the document generation and filters; a stale cursor returns a restart-pagination error.

Use `typeId` and internal `name` for automation. Use `label` only for human presentation.

## `inspect_sketch` and `edit_sketch`

Both tools report `state` (a list of state strings), `statusText` (a string or `null`), and `solver.solverStatus` (an integer or `null`, the native `solve()` code) alongside the geometry and constraint rows.

`edit_sketch` accepts optional `expected_generation`. A mismatch fails with `VALIDATION_FAILED` and no transaction opens, so nothing changes; the details carry `expectedGeneration`, `actualGeneration`, and `nextAction: inspect_sketch`.

A constraint `(type, argument-count)` shape with no recorded native acceptance is refused before execution with `VALIDATION_FAILED` and no transaction; the details carry `reason: unrecorded_constraint_shape`, the `acceptedArgumentCounts` for the requested type (`null` when the type has no recorded form), and `nextAction: inspect_sketch`. The refusal is a process-safety measure: a malformed `Sketcher.Constraint` constructor call can raise an unhandled C++ exception that terminates the whole FreeCAD process.

## `edit_parameters` results

`edit_parameters` results carry `document`, `generation`, and `applied`. Each `applied` entry is an operation label: `add:NAME`, `rename:OLD->NEW`, `expression:PROP`, or `clear:PROP`. The list ends with the mutated object's internal `Name`.

## `create_feature` details

`create_feature` creates the feature through `body.newObject`, so Body membership and the Body Tip are native. The `pad`, `pocket`, and `hole` kinds require a `profile` object that already belongs to the same Body. A `support` reference requires an explicit `properties.MapMode`. The tool never invents an attachment mode.

Shapeless and null-shape objects are valid on FreeCAD 1.1.3: `create_object` creates `PartDesign::Body` and `Part::Feature` successfully, and the report shows `solid_count: 0` until the object holds a solid. A positive `expected_solids` on such an object fails with `has no geometry; expected_solids=N cannot be satisfied`.

`support` is applied through the `Support` property when the target type exposes it and through `AttachmentSupport` otherwise. `MapMode` is required either way. A target that exposes neither property fails with `feature '...' exposes neither Support nor AttachmentSupport`.

An enumeration property such as `PartDesign::Pocket.Type` takes the exact string (`"Length"`), never an index.

## `create_object` details

Generic Part/App types go through `doc.addObject(type, name)`. FEM types use an explicit factory mapping through `ObjectsFem`: `Fem::FemAnalysis` (and the legacy alias `Fem::AnalysisPython`) to `makeAnalysis`, `Fem::SolverCalculiX` to `makeSolverCalculiX`, `Fem::MaterialCommon` to `makeMaterialSolid`, plus materials, element definitions, and `Fem::Constraint*` names. An unsupported or ambiguous type is an explicit error, not a guess.

Shapeless types are created successfully and report `solid_count: 0`. The attachment property routing for `create_feature` is in its section above.

The result carries the actual internal name and a post-recompute geometry report. FreeCAD sanitizes and de-duplicates names (`Box` may become `Box001`). Always use the returned name in later calls.

New volumetric geometry defaults to one solid; pass `expected_solids` to require a different count. Existing valid dependent solid counts are preserved when their inputs change.

Compact `inspect_objects` rows carry `name`, `label`, `typeId`, `state`, `bounds`, `shape_valid`, `solid_count`, `tip`, and `links`. `detail: "full"` adds `placement`, `globalPlacement`, and the property pages: `properties` for the requested `property_filter` (or all names), plus `propertyMetadata` (type, read-only, enumeration), `propertyCount`, `nextPropertyOffset`, and `truncatedProperties`. The row `limit` defaults to 32 (max 500). Use `property_offset` and `property_limit` (default 64) to page large property sets. Bounds are document-space millimetres. Pagination uses an opaque signed cursor bound to the document generation and filters; a stale cursor returns a restart-pagination error.

`run_script` executes on the GUI thread in a namespace seeded with `FreeCAD`/`App` and `Gui`. Variables persist per `session_id` for the server's lifetime. At most 32 sessions are kept; new sessions are refused instead of evicting live state. stdout, stderr, and the traceback are captured even when the code raises. `timeout_s` is a cooperative server deadline (1–3600 s, default 90); execution cannot be preempted, and the tool result says so truthfully. The tool is refused with `SERVER_BUSY` while a FEM solve is active.

Use `run_script` for operations outside the structured tools: `FreeCADGui` calls, selection, imports of formats `import_model` does not support (it covers STEP and STL behind file consent), Parts Library access, mesh routes, and specialized property assignments.

`run_script` reaches the native bindings directly and is not covered by the `edit_sketch` guard: malformed native constructor calls, such as an unsupported `Sketcher.Constraint` argument form, can raise an unhandled C++ exception that terminates the whole FreeCAD process.

## Consent

Consent-gated operations: opening an untrusted FCStd file (the default), saving over a different existing file, closing a dirty or unsaved nonempty document, reloading a dirty document, and overwriting an export destination. A client that declares `elicitation.form` receives an MRTR elicitation with a fixed `confirm` boolean form and must answer it; the signed consent state is a single-use nonce bound to the operation target. Declining, cancelling, or tampering aborts the operation without effect (`CONSENT_DENIED`). A client without form support proceeds without the prompt; each bypass is noted in the Report view.

## Long-running operations and cancellation

`run_fem`, `run_script`, `export`, and `measure` may detach as tasks under the `io.modelcontextprotocol/tasks` extension: the call returns a task id immediately, `tasks/get` polls for the terminal result, and `tasks/cancel` requests cooperative cancellation. Cancellation is honest about its limits: a running CalculiX solve is never killed, and a `run_script` deadline that already started does not stop the code. Clients without the tasks extension receive blocking final results.

## Timeouts and stuck state

Per-tool deadlines: 60 s default; `export` and `measure` 600 s; `run_fem` and `run_script` accept `timeout_s` (1–3600). Consent preflight gets 30 s. At most 32 operations run concurrently. If a GUI operation that already started exceeds its deadline, the server reports `GUI_DISPATCH_STUCK` and rejects later GUI operations immediately. `discover_capabilities` is GUI-independent; call it to inspect health. Do not attempt force-cancellation and do not pile on more operations. Allow the operation to finish; if health does not return to healthy, restart FreeCAD.

## Errors

Application failures are complete tool results with `isError: true` and a structured `{code, message, details}` payload. Stable codes: `DOCUMENT_NOT_FOUND`, `OBJECT_NOT_FOUND`, `VALIDATION_FAILED`, `GUI_DISPATCH_FAILED`, `CONSENT_DENIED`, `PATH_NOT_ALLOWED`, `UNSUPPORTED_VIEW`, `SOLVER_FAILED`, `SERVER_BUSY`. Only protocol-level violations become JSON-RPC errors. Output-schema violations are infrastructure errors (`-32603`).

## Security and network boundary

There are exactly two modes. Local (default): the server binds `127.0.0.1`, accepts only loopback peers, checks Host/Origin, and needs no token. Network access (opt-in in MCP Settings): the server binds `0.0.0.0`, the bearer token becomes mandatory on every request, and `allowed_ips` (empty means any host) restricts peers further. There is no TLS in either mode; never forward or tunnel the endpoint to an untrusted network.

The bearer token is full local code-execution authority: `run_script` is not restricted by `allowed_roots`. Treat the token like a shell on this machine; never log or print it. `allowed_roots` (default: the user's home directory) contains the file-touching tools only — document open/save paths, export destinations, and FEM working directories. It is path containment inside this server, not a sandbox.

## Sources

- [FreeCAD MCP README](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/README.md)
- [Server orchestrator](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/addon/FreeCADMCP/mcp_server/server.py)
- [Tool modules](https://github.com/bradsjm/freecad-embedded-mcp/tree/main/addon/FreeCADMCP/mcp_server/tools)
- [GUI dispatch](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/addon/FreeCADMCP/mcp_server/gui_dispatch.py)
- [Dependency-free client example (FEM)](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/examples/cantilever_fem.py)
