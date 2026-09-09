# FreeCAD MCP tool contract

Use this file for exact tool behavior. The authoritative implementation is the [FreeCAD MCP repository](https://github.com/neka-nat/freecad-mcp) under [`addon/FreeCADMCP/mcp_server/`](https://github.com/neka-nat/freecad-mcp/tree/main/addon/FreeCADMCP/mcp_server): `server.py` (registry, dispatch, deadlines, consent), `protocol.py` (wire contract), `http_server.py` (transport and auth), `gui_dispatch.py` (GUI-thread bridge), and `tools/` (one module per tool domain).

## Architecture

The add-on embeds the MCP server inside FreeCAD's GUI process. There is no separate server package and no second bridge process.

1. The server speaks JSON-RPC over streamable HTTP with SSE at `http://127.0.0.1:9876/mcp` (port from settings). Current-protocol clients send the `MCP-Protocol-Version` header (`2026-07-28`) and the `Mcp-Method`/`Mcp-Name` request-metadata headers. Clients that speak the 2025 revisions (`2025-03-26`, `2025-06-18`, `2025-11-25`) connect without changes and address the session with the returned `MCP-Session-Id`; an unsupported offered version negotiates `2025-11-25`.
2. HTTP worker threads never touch FreeCAD. Every document or GUI operation is dispatched onto FreeCAD's main GUI thread through a queue drained by the Qt loop. One long GUI operation delays later GUI operations; there is one shared dispatch.

`run_script` executes arbitrary Python inside the FreeCAD process with the user's privileges. It is deliberately not sandboxed.

## Tool matrix

The server exposes 17 tools in a fixed order. Document tools return the actual sanitized `name`, `label`, and `objectCount`; use the returned `name` as the `document` argument in later calls.

| Tool | Use | Important arguments |
|---|---|---|
| `discover_capabilities` | Versions, workbenches, full `supportedTypes`, exporter and FEM availability, GUI dispatch health | none; GUI-independent |
| `new_document` | Create an empty document | `name` |
| `open_document` | Open an `.FCStd` from an allowed root | `path`; `untrusted` defaults true and requires consent |
| `save_document` | Save to the existing path, or save-as | `document`; optional `path` (consent to overwrite a different existing file) |
| `close_document` | Close one document | `document`; consent when dirty or unsaved nonempty |
| `reload_document` | Close and reopen the saved file | `document`; consent to discard unsaved changes |
| `inspect_objects` | List objects sorted by Name; signed-cursor pagination | `document`; `cursor`, `detail` (`compact`/`full`), `property_filter`, `limit` (default 100, max 500), `property_offset`, `property_limit` |
| `create_object` | Create a supported Part/App type or a FEM object | `document`, `type`, `name`; optional `properties`, `expected_solids` |
| `edit_object` | Assign properties with full prevalidation | `document`, `object`, `properties`; optional `expected_solids` |
| `delete_object` | Delete one object; refuses objects with dependents | `document`, `object` |
| `edit_parameters` | Add/rename dynamic properties and bind expressions | `document`, `object`; optional `add`, `rename`, `expressions` |
| `validate_geometry` | State, validity, solid count, volume, bounds, tolerance | `document`, `objects` (max 100); optional `expected_solids`, `expected_bounds`, `bounds_tolerance` |
| `measure` | Distance, interference, section, or face measurement | `document`, `a`, `mode`; optional `b`, `plane` |
| `export` | STL/STEP/3MF or native FCStd copy with readback verification | `document`, `objects`, `format`, `path`; optional `linear_deflection`, `angular_deflection`, `bed_align` |
| `capture_view` | PNG of the 3D view with an explicit orientation | `document`, `focus_object`, `view_name`; optional `width`, `height` |
| `run_fem` | Modern CalculiX solve; returns a VTK result summary | `document`, `analysis`; optional `timeout_s` (default 600) |
| `run_script` | Arbitrary Python on the GUI thread in a persistent session namespace | `code`; optional `session_id` (default `"default"`), `timeout_s` (default 90) |

The tools cover CAD-side modeling, inspection, validation, export, and FEM operations only. Report anything outside these operations as outside this skill's boundary.

Valid `view_name` values are `Isometric`, `Front`, `Top`, `Right`, `Back`, `Left`, `Bottom`, `Dimetric`, and `Trimetric`. `capture_view` returns base64 PNG content. An omitted size is clamped to a 1024 px longest edge; an explicit size up to 4096 px is honored. The caller's selection and active document are preserved.

## Standard sequence

1. Call `discover_capabilities`. Read `gui.state`, `capabilities.supportedTypes`, and exporter/FEM availability.
2. Address the target document by the `name` returned by `new_document` or `open_document`. There is no list-documents tool; when the name is unknown, call `run_script` with `App.listDocuments()`.
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

A failure during the transaction aborts the whole operation, recomputes the restored document, and reports rollback failure separately. Feature-specific assignments the mapper cannot express go through `run_script`.

## Inspection response

Compact `inspect_objects` rows carry `name`, `label`, `typeId`, `state`, `placement`, `globalPlacement`, `boundsCoordinateSystem`, `bounds`, `shape_valid`, `solid_count`, `tip`, `links`, and an empty/absent `properties` map. `detail: "full"` fills `properties` for the requested `property_filter` (or all names), plus `propertyMetadata` (type, read-only, enumeration), `propertyCount`, `nextPropertyOffset`, and `truncatedProperties`. Use `property_offset` and `property_limit` (default 64) to page large property sets. Bounds are document-space millimetres. Pagination uses an opaque signed cursor bound to the document generation and filters; a stale cursor returns a restart-pagination error.

Use `typeId` and internal `name` for automation. Use `label` only for human presentation.

## `create_object` details

Generic Part/App types go through `doc.addObject(type, name)`. FEM types use an explicit factory mapping through `ObjectsFem`: `Fem::FemAnalysis` (and the legacy alias `Fem::AnalysisPython`) to `makeAnalysis`, `Fem::SolverCalculiX` to `makeSolverCalculiX`, `Fem::MaterialCommon` to `makeMaterialSolid`, plus materials, element definitions, and `Fem::Constraint*` names. An unsupported or ambiguous type is an explicit error, not a guess.

The result carries the actual internal name and a post-recompute geometry report. FreeCAD sanitizes and de-duplicates names (`Box` may become `Box001`). Always use the returned name in later calls.

New volumetric geometry defaults to one solid; pass `expected_solids` to require a different count. Existing valid dependent solid counts are preserved when their inputs change.

Compact `inspect_objects` rows carry `name`, `label`, `typeId`, `state`, `placement`, `globalPlacement`, `boundsCoordinateSystem`, `bounds`, `shape_valid`, `solid_count`, `tip`, `links`, and an empty `properties` map. `detail: "full"` fills `properties` for the requested `property_filter` (or all names), plus `propertyMetadata` (type, read-only, enumeration), `propertyCount`, `nextPropertyOffset`, and `truncatedProperties`. Use `property_offset` and `property_limit` (default 64) to page large property sets. Bounds are document-space millimetres. Pagination uses an opaque signed cursor bound to the document generation and filters; a stale cursor returns a restart-pagination error.

`run_script` executes on the GUI thread in a namespace seeded with `FreeCAD`/`App` and `Gui`. Variables persist per `session_id` for the server's lifetime. At most 32 sessions are kept; new sessions are refused instead of evicting live state. stdout, stderr, and the traceback are captured even when the code raises. `timeout_s` is a cooperative server deadline (1–3600 s, default 90); execution cannot be preempted, and the tool result says so truthfully. The tool is refused with `SERVER_BUSY` while a FEM solve is active.

Use `run_script` for operations outside the structured tools: `FreeCADGui` calls, selection, imports of neutral formats, Parts Library access, mesh routes, and specialized property assignments.

## Consent

Consent-gated operations: opening an untrusted FCStd file (the default), saving over a different existing file, closing a dirty or unsaved nonempty document, reloading a dirty document, and overwriting an export destination. A client that declares `elicitation.form` receives an MRTR elicitation with a fixed `confirm` boolean form and must answer it; the signed consent state is a single-use nonce bound to the operation target. Declining, cancelling, or tampering aborts the operation without effect (`CONSENT_DENIED`). A client without form support proceeds without the prompt; each bypass is noted in the Report view.

## Long-running operations and cancellation

`run_fem`, `run_script`, `export`, and `measure` may detach as tasks under the `io.modelcontextprotocol/tasks` extension: the call returns a task id immediately, `tasks/get` polls for the terminal result, and `tasks/cancel` requests cooperative cancellation. Cancellation is honest about its limits: a running CalculiX solve is never killed, and a `run_script` deadline that already started does not stop the code. Clients without the tasks extension receive blocking final results.

## Timeouts and stuck state

Per-tool deadlines: 60 s default; `export` and `measure` 600 s; `run_fem` and `run_script` accept `timeout_s` (1–3600). Consent preflight gets 30 s. At most 32 operations run concurrently. If a GUI operation that already started exceeds its deadline, the server reports `GUI_DISPATCH_STUCK` and rejects later GUI operations immediately. `discover_capabilities` is GUI-independent; call it to inspect health. Do not attempt force-cancellation and do not pile on more operations. Allow the operation to finish; if health does not return to healthy, restart FreeCAD.

## Errors

Application failures are complete tool results with `isError: true` and a structured `{code, message, details}` payload. Stable codes: `DOCUMENT_NOT_FOUND`, `OBJECT_NOT_FOUND`, `VALIDATION_FAILED`, `GUI_DISPATCH_FAILED`, `CONSENT_DENIED`, `PATH_NOT_ALLOWED`, `UNSUPPORTED_VIEW`, `SOLVER_FAILED`, `SERVER_BUSY`. Only protocol-level violations become JSON-RPC errors. Output-schema violations are infrastructure errors (`-32603`).

## Security and network boundary

There are exactly two modes. Local (default): the server binds `127.0.0.1`, accepts only loopback peers, checks Host/Origin, and needs no token. Remote (opt-in via the Remote Connections toggle): the server binds `0.0.0.0`, the bearer token becomes mandatory on every request, and `allowed_ips` (empty means any host) restricts peers further. There is no TLS in either mode; never forward or tunnel the endpoint to an untrusted network.

The bearer token is full local code-execution authority: `run_script` is not restricted by `allowed_roots`. Treat the token like a shell on this machine; never log or print it. `allowed_roots` (default: the user's home directory) contains the file-touching tools only — document open/save paths, export destinations, and FEM working directories. It is path containment inside this server, not a sandbox.

## Sources

- [FreeCAD MCP README](https://github.com/neka-nat/freecad-mcp/blob/main/README.md)
- [Server orchestrator](https://github.com/neka-nat/freecad-mcp/blob/main/addon/FreeCADMCP/mcp_server/server.py)
- [Tool modules](https://github.com/neka-nat/freecad-mcp/tree/main/addon/FreeCADMCP/mcp_server/tools)
- [GUI dispatch](https://github.com/neka-nat/freecad-mcp/blob/main/addon/FreeCADMCP/mcp_server/gui_dispatch.py)
- [Dependency-free client example (FEM)](https://github.com/neka-nat/freecad-mcp/blob/main/examples/cantilever_fem.py)
