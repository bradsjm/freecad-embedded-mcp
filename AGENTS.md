# Repository Guidelines

## Project Overview

FreeCAD add-on that embeds a Model Context Protocol (MCP) server inside FreeCAD's GUI. MCP clients drive FreeCAD over streamable HTTP (JSON-RPC + SSE) at `POST http://127.0.0.1:9876/mcp`, protocol version `2026-07-28`; a legacy adapter (`legacy_protocol.py`) also accepts Streamable HTTP revisions `2025-03-26` (batches), `2025-06-18`, and `2025-11-25`. Security model: local-only mode (default) binds loopback and needs no token; `remote_enabled` rebinds `0.0.0.0` and makes a bearer token mandatory on every request (`allowed_ips` defaults to empty = any peer; a populated list restricts peers further). The add-on runs inside FreeCAD's bundled Python (3.11+) and requires FreeCAD 1.1.3 or later; startup fails closed outside `1.1.3 <= v < 1.2`. MIT licensed.

The server registers 26 tools in a fixed order: `discover_capabilities`, `inspect_documents`, `new_document`, `open_document`, `import_model`, `save_document`, `close_document`, `reload_document`, `inspect_objects`, `create_object`, `create_objects`, `edit_object`, `edit_objects`, `delete_object`, `validate_geometry`, `measure`, `inspect_topology`, `edit_parameters`, `inspect_sketch`, `edit_sketch`, `create_feature`, `edit_feature`, `export`, `capture_view`, `run_fem`, and `run_script`. `run_script` is registered but appears in `tools/list` only when the `allow_scripts` setting is enabled, so the default surface has 25 tools; a disabled call answers `METHOD_NOT_FOUND` before schema validation or dispatch.

The repository also ships an installable agent skill (`skills/freecad-mcp/`) that teaches the tool contract, and a native-contract probe (`examples/native_contract_probe.py`) that records live FreeCAD behavior into `tests/native_contract.json`.

## Architecture & Data Flow

**Startup (GUI only):** `addon/FreeCADMCP/InitGui.py` (`Init.py` is deliberately empty — console mode gets nothing) puts the add-on on `sys.path` and registers the "MCP Addon" workbench from `mcp_server/workbench.py` (toolbar "FreeCAD MCP": `Toggle_MCP_Server`; menu adds `Connection_Details`, `MCP_Settings` from `mcp_server/commands.py`). Auto-start is deferred via `QtCore.QTimer.singleShot(0, ...)` and starts the server only when the `auto_start` setting is true; failures become a console warning plus a status-bar message. `server.start_server()`: FreeCAD version guard → `load_settings()` (fail closed; missing file bootstraps local defaults; `remote_enabled` without a token generates and persists one) → capture a capabilities snapshot on the GUI thread → `gui_dispatch.initialize()` → construct `McpHTTPServer` (remote without token raises in the constructor) → `http.start()` → document observer → running. Any failure unwinds everything. `server.py` holds a module-level `_server` singleton; restart is refused while operations or GUI work are still pending; `stop_server` is nonblocking.

**Request path (end to end):**

1. `http_server.py` — ThreadingHTTPServer, never imports FreeCAD. Order: header-structure checks (duplicate single-value headers, any `Transfer-Encoding` → `-32600`) → digits-only `Content-Length`, body cap 8 MiB (413) → peer gate → Host and Origin checks (local mode only) → constant-time bearer compare (401 + `WWW-Authenticate`). POST requires `Content-Type: application/json` and an `Accept` with q>0 for both `application/json` and `text/event-stream`. JSON parsing rejects NaN/Infinity. Era selection: a message carrying the `_meta` protocol marker, or a bare JSON array with no session id, takes the modern path; everything else routes through `legacy_protocol.LegacyProtocol`.

2. `protocol.py` `validate_request` — precedence envelope (`-32600`) → metadata (`-32602`) → routing-header mirroring (`-32020`: `MCP-Protocol-Version` must equal the meta value, `Mcp-Method` must equal the method, `Mcp-Name`/`Mcp-Param-*` must match the body, base64 sentinels decoded strictly) → version check last (`-32022` unless `2026-07-28`). Valid notifications return 202 and are never dispatched.
3. `server.py` `Server.dispatch` — routes 9 methods (`server/discover`, `tools/list`, `tools/call`, `tasks/get`, `tasks/update`, `tasks/cancel`, `subscriptions/listen`, `resources/list`, `resources/read`). `tools/call`: validate arguments against the tool's `inputSchema` (unknown or disabled tool → `-32601`), honor a pre-set cancel event, run consent preflight on the GUI thread (30 s), then take the task path (client declared the tasks capability and the tool is task-eligible) or the blocking path. Results are validated against `outputSchema` before image conversion; violations are `-32603`. `resources/list` exposes `freecad://documents`; `resources/read` returns the live document inventory; `subscriptions/listen` streams acknowledged, task, and resource-update notifications through bounded queues.
4. The handler runs **on the FreeCAD GUI thread** through `gui_dispatch.py` (a `queue.Queue` woken by a Qt signal, 500 ms heartbeat fallback; ticks skip while the mouse is held or a modal is open). Application failures are complete results with `isError: true` and structured `{code, message, details}` — never JSON-RPC errors. Only protocol-level violations become JSON-RPC errors.
5. Task-eligible tools (`run_script`, `run_fem`, `export`, `measure`) may detach under the `io.modelcontextprotocol/tasks` extension (`tasks.py`): the call returns a task id immediately; clients poll `tasks/get` and request `tasks/cancel`.

**Hard rules an editor must preserve:**

- HTTP worker threads never touch FreeCAD. All FreeCAD work goes through `gui_dispatch`.
- Cancellation is cooperative (`threading.Event`). A running CalculiX solve is never killed; a timed-out GUI operation poisons dispatch health (later GUI ops are rejected until healthy — inspect via `discover_capabilities`). Never report a stuck operation as stopped.
- Two security modes only: local (loopback bind, loopback peers, Host/Origin checks, no token) and remote (`remote_enabled`: bind `0.0.0.0`, token mandatory on every request, `allowed_ips` empty = open). Never add TLS or CORS handling. Remote start without a token must fail. The bearer token is full local code-execution authority (`run_script` is intentionally unsandboxed); never log or print it.
- `allowed_roots` (defaults to `~`) is path containment for file-touching tools and the FEM working directory — not a sandbox, and it never constrains `run_script`. An absolute `recovery_directory` is implicitly allowed for reads and writes and needs no `allowed_roots` entry.
- Mutations go through `object_validation.mutation()`: refuses FEM-locked documents and nesting inside a user transaction, opens/commits/aborts its own transaction, recomputes, validates targets and every recomputed dependent against solid-count baselines, and reports rollback separately (`operationState`: `rolled_back` / `rollback_failed` / `may_have_changed`). `create_objects` applies 1–32 entries in one transaction and returns requested-to-actual name mappings. A dependent closure over 256 objects is refused before any effect.
- When `recovery_enabled` is true, expensive feature creation or editing first writes and verifies an FCStd recovery copy in `recovery_directory` before the mutation transaction opens. Recovery failures refuse the mutation and never overwrite an existing checkpoint.
- FEM uses the modern `Fem::SolverCalculiX` pipeline only; legacy `SolverCcxTools`, mixed, or duplicate solvers produce explicit errors; missing CalculiX produces an actionable error, never an auto-install.
- Consent (MRTR): `InputRequired` elicitation with an HMAC-signed single-use `requestState` nonce (300 s TTL, fresh in-memory key per server start). Retries carry `requestState` + `inputResponses`; handlers re-verify the approved target (`documents._require_approved`), so consent binds principal, method, args, and target fingerprints. Consent targets are untrusted document open, STEP/STL import, overwrite-style save, dirty or unsaved document close, dirty reload, and export overwrite.
- All output floats are finite; `canonical_json`/fingerprint sign consent targets. No asyncio anywhere. Locks are fine-grained, per-domain, and never held across a GUI dispatch. Detached task calls use the Tasks extension; the native asynchronous path is `tools/fem.py` (QProcess signals on the GUI thread plus a retained `Future` the server flattens).

## Key Directories

- `addon/FreeCADMCP/` — the shipped add-on. `mcp_server/` holds the core server; `mcp_server/tools/` holds one module per tool domain, including `recovery.py` for verified FCStd checkpoints.
- `tests/` — pytest suite mirroring the source; runs headless with stubs (no FreeCAD install needed).
- `examples/cantilever_fem.py` — manual, stdlib-only MCP client demo (not a test). Its checked-in `EXPECTED_TOOLS = 23` guard is stale against the current 25-tool default surface, so update that guard before running it against this revision.
- `examples/native_contract_probe.py` — stdlib-only probe client that drives a live FreeCAD through the server and rewrites `tests/native_contract.json` (modes: default verification, `--dev` resume-past-crashes, `--sweep` constraint-forms merge).
- `skills/freecad-mcp/` — the agent skill (`SKILL.md` + 16 `references/` files); distributed from the repo via `npx skills add bradsjm/freecad-embedded-mcp`, not packaged with the add-on.
- `.native-contract/` — generated probe journal and artifacts; gitignored, never commit.
- `.github/workflows/test.yml` — CI gate.
- `assets/` — demo images referenced by README.

## Development Commands

```bash
uv sync                                                  # env (dev group = pytest + ruff)
uv run pytest -q                                         # full suite, headless
uv run pytest tests/test_settings.py -q                  # one file
uv run pytest tests/test_settings.py::test_invalid_port_fails_closed   # one test
uv run pytest -k "consent" -q                            # keyword filter
uv run ruff check .                                      # lint gate
uv run ruff check --fix .                                # apply safe lint fixes
uv run ruff format .                                     # format gate
uv run python -m compileall addon                        # CI syntax gate
uv lock --check                                          # lockfile sync gate
```

Live-server tools (need FreeCAD 1.1.3+ with the add-on running):

```bash
# Update examples/cantilever_fem.py EXPECTED_TOOLS to 25 before running.
FREECAD_MCP_TOKEN=... python3 examples/cantilever_fem.py
python3 examples/native_contract_probe.py [--dev] [--sweep]   # FREECAD_MCP_URL/FREECAD_MCP_TOKEN optional
```

Manual run: symlink `addon/FreeCADMCP` into FreeCAD's `Mod/` directory (per-OS paths in README), start FreeCAD, select the "MCP Addon" workbench, then use the **Start MCP Server** toolbar action. The token is shown in **Connection Details…** or stored in `freecad_mcp_settings.json` under FreeCAD's user app-data directory.

Lint and format rules live in `pyproject.toml` under `[tool.ruff]`. Read that file before you change a rule. The configuration selects defect-focused rule families and ignores `SIM105` deliberately: the mutation gate and transport layers attach per-branch diagnostics, and `contextlib.suppress` would hide the failing native call. Per-file-ignores: `examples/*.py` get `T20`/`SIM115` (print-based demo, long-lived journal handle); `tests/*.py` get `E402`/`RUF012`/`PLW1641` for the `sys.path` import preamble and FreeCAD-shaped stub doubles. Do not add `# noqa` to silence a finding; fix the code, or change the rule in `pyproject.toml` when the rule conflicts with a documented design decision. The only inline `# noqa` cases are host-injected names (`Gui` in `InitGui.py`) and a deliberate availability-probe import (`fem.py` `calculixtools`), each with a reason comment.

Every completion MUST pass both ruff gates. Run `uv run ruff check .` and `uv run ruff format --check .` before you report a task as complete. CI enforces the same commands.

## Code Conventions & Common Patterns

- **Tool module pattern** — every `mcp_server/tools/*.py` ends with:
  ```python
  TOOL_DEFINITIONS = [
      {
          "name": "measure",
          "description": ...,
          "inputSchema": _MEASURE_INPUT,
          "outputSchema": _MEASURE_OUTPUT,
      }
  ]
  HANDLERS = {"measure": _handle_measure}
  check_schema(_MEASURE_INPUT)  # every schema validated at registration
  ```
  `documents`, `export`, and `import_model` also export `preflight(ctx, name, args)` for consent targets. `server._register_tools()` consumes the triples and enforces the exact 26-tool `PLAN_TOOL_ORDER`; a missing, extra, or duplicate tool name raises `RuntimeError`. Handler signature: `def handler(ctx, arguments: dict) -> dict`. `ctx` is an `_OpContext` over the `Server` exposing `require_document`, `require_object`, `App`, `Gui`, `settings`, `signer`, `cancel_event`, `approved_target`, and friends. Module map: `documents` = new/open/save/close/reload; `objects` = inspect/create/create_objects/edit/edit_objects/delete; `geometry` = validate_geometry/measure/inspect_topology; `sketch` = inspect/edit; plus `parameters`, `features` (create_feature + edit_feature), `export`, `view`, `fem`, `script`, `import_model`; `discover_capabilities` and `inspect_documents` are built into `server.py`.
- **Schemas** use a finite JSON-Schema subset: `additionalProperties: false`, `$ref` only into local `$defs`, no NaN/infinity (`oneOf`/`allOf`/conditionals/patterns/remote refs are rejected at registration). Schemas are checked at registration and per call; an output-schema violation is an infrastructure error (`-32603`).
- **Errors:** raise `ToolError(code, message, details)` with stable codes (`DOCUMENT_NOT_FOUND`, `OBJECT_NOT_FOUND`, `VALIDATION_FAILED`, `GUI_DISPATCH_FAILED`, `CONSENT_DENIED`, `PATH_NOT_ALLOWED`, `UNSUPPORTED_VIEW`, `SOLVER_FAILED`, `SERVER_BUSY` — the last defined locally in `server`/`fem`/`script`/`tasks`/`legacy_protocol`, not a `protocol.py` constant). `ProtocolError` maps to a JSON-RPC error (`-32700`/`-32600`/`-32602` → 400, `-32601` → 404, `-32603` → 500, `-32020`/`-32021`/`-32022` → 400) on the modern era; the legacy adapter returns JSON-RPC errors in the body with HTTP 200 (deliberate era difference — legacy clients key on the body). A `ToolError` raised inside a consent preflight is re-raised with its own code by `server._run_preflight` — never re-wrap preflight refusals (`PATH_NOT_ALLOWED`, missing file) as `GUI_DISPATCH_FAILED`. `details.nextTool` is a tool name that clients can call; `details.nextAction` is not a tool name. `InputRequired` starts a consent round trip.
- **Consent preflight targets:** `open_document` (untrusted load, default untrusted), `save_document` (overwrite-style save-as only), `close_document` and `reload_document` (dirty discard), `export` (existing-file overwrite), `import_model` (untrusted input).
- **Import discipline (what keeps tests headless):** `protocol`, `http_server`, `legacy_protocol`, `tasks`, `subscriptions`, `ip_parse`, `object_validation`, and `dispatch_health` must not import FreeCAD or Qt; `settings` imports FreeCAD only lazily inside `default_settings_path`. Module-level FreeCAD/Qt imports live in `workbench`, `commands`, `server`, `gui_dispatch`, and `tools/{export, objects, view}`; every other tool module imports FreeCAD/Part/Import lazily inside handlers. Tool modules import siblings lazily to stay registration-order independent (known deviation: `import_model` imports `documents._require_approved` at module level).
- **Concurrency:** no asyncio. Locks are per-domain and never held across a GUI dispatch. Blocking responses are produced by short-lived daemon threads that only wait on the dispatcher. Detached task calls use the Tasks extension; the native asynchronous path is `tools/fem.py` (QProcess signals plus a retained `Future`).
- **Numeric limits** (`server.py` unless noted): default deadline 60 s; export/measure 600 s; `run_script` default 90 s; `run_fem` default 600 s; `timeout_s` clamped 1–3600; preflight 30 s; resource read 10 s; 32 concurrent operations shared across blocking + tasks (`SERVER_BUSY` beyond); 32 active subscriptions; 32 script sessions (a 33rd is refused, never evicted); tasks 32 active / 1024 retained / 1 h TTL / 500 ms poll; HTTP requests and responses 8 MiB, stream events 1 MiB, 64 HTTP connections, and 32 streams; subscription queues 256 events and 4 MiB (overflow closes the stream); legacy adapter 32 sessions / 32 inflight / 32 batch members / 3600 s idle timeout.
- **JSON safety:** all output floats are finite; `canonical_json` (sorted keys, `allow_nan=False`) and SHA-256 fingerprints sign consent targets. Consent signatures use a fresh in-memory HMAC key per server start (they expire on restart).
- **Naming:** snake_case modules; `_UPPER_SNAKE` module constants; tests use `make_*` factories, `Fake*`/`Stub*` doubles, and `load_*` module loaders.

## Important Files

- `addon/FreeCADMCP/InitGui.py` — GUI entry point; `Init.py` stays empty.
- `mcp_server/workbench.py` — workbench, toolbar/menu, deferred auto-start. `mcp_server/commands.py` — the three UI commands and status mapping.
- `mcp_server/server.py` — orchestrator: tool registry, dispatch, consent choreography, operation registry, deadlines, script-session cap, recovery checkpoints, resources, subscriptions, and lifecycle.
- `mcp_server/http_server.py` — transport, auth, SSE, era routing. `mcp_server/protocol.py` — wire contract, error codes, schema checks, `ConsentSigner`. `mcp_server/legacy_protocol.py` — legacy-revision Streamable HTTP adapter.
- `mcp_server/gui_dispatch.py` — GUI-thread bridge and dispatch health (`dispatch_health.py`: stuck jobs poison the dispatcher until the callable truly returns).
- `mcp_server/object_validation.py` — mutation gate and geometry reports. `mcp_server/tasks.py` — task store. `mcp_server/subscriptions.py` — connection-scoped bounded queues. `mcp_server/settings.py` — atomic, fail-closed settings (`port`, `token`, `auto_start`, `remote_enabled`, `allowed_ips`, `allowed_roots`, `recovery_enabled`, `recovery_directory`, `allow_scripts`). `mcp_server/ip_parse.py` — strict `allowed_ips` parsing.
- `mcp_server/tools/` — `documents`, `import_model`, `objects`, `geometry`, `sketch`, `features`, `parameters`, `export`, `view`, `fem`, `script`, and `recovery`.
- `pyproject.toml` — pytest config plus the ruff lint/format rules. `package.xml` — Addon Manager manifest (repo `bradsjm/freecad-embedded-mcp`, branch `v2.0-embedded-mcp`, freecad 1.1.3–1.1.99). `README.md` — install, handshake, and tool docs. `skills/freecad-mcp/SKILL.md` — agent-skill entry point.

## Runtime/Tooling Preferences

- Package manager: **uv** (lockfile committed; keep `uv lock --check` green).
- Python `>= 3.11`; pinned `3.11` locally (`.python-version`) and in CI.
- **Zero runtime dependencies** — the add-on runs inside FreeCAD's bundled Python. Do not add any. The dev group is pytest and ruff only.
- Distribution: the add-on ships via git and the FreeCAD Addon Manager (`package.xml`); no wheel is built or published — `pyproject.toml` is a virtual package with no build system.

## Testing & QA

- pytest only; `testpaths = ["tests"]`; **no conftest.py**. Self-contained test files (own stubs, own `ADDON_DIR` `sys.path` preamble) are the convention, with two documented exceptions:
  - `tests/_native_contract.py` — non-collected shared loader for `tests/native_contract.json` (its docstring records the deviation).
  - `tests/test_server.py` doubles as the shared harness, imported as `ts` by `test_commands.py`, `test_legacy_protocol.py`, and `test_mcp_integration.py`, because `mcp_server.server` binds one set of stub tool modules at first import; `test_mcp_integration.py` removes the fake tool package from `sys.modules` afterward.
- Tests never import real FreeCAD. Stubbing tiers: pure modules imported directly with `FakeClock`; lazy-import tool modules exercised through `FakeCtx`/`FakeDoc` doubles; FreeCAD/Qt-importing modules loaded via `importlib` under unique names with `sys.modules` stubs (`load_*` context managers); `test_http_server.py` runs a real HTTP server on port 0 through `http.client`/raw sockets; `test_server.py` drives the real `Server.dispatch` over contract-shaped fake tool modules.
- Zero-sleep discipline: use `FakeClock` and `threading.Event` barriers, never `time.sleep`. One sanctioned exception: the shared `wait_until` predicate poll in `test_server.py`, a bounded condition-variable wait for cross-thread completion that has no event surface.
- Native contract: after FreeCAD version or native-behavior changes, run `examples/native_contract_probe.py` against a live FreeCAD to regenerate `tests/native_contract.json`; a crash in default mode is terminal evidence (journal + `.ips` copy under gitignored `.native-contract/`). `tests/test_native_contract.py` validates the committed record headlessly (24 required probes, schema 1, freecad 1.1.*) with no skips.
- Real sockets appear only in `test_http_server.py` and `test_mcp_integration.py`, both on OS-assigned port 0. No external binaries are required (the CalculiX path is stubbed to `sys.executable`).
- CI gates to keep green (`.github/workflows/test.yml`, single job, Python 3.11): `uv lock --check`; `uv run ruff check .`; `uv run ruff format --check .`; `uv run python -m compileall addon`; `uv run pytest -q`.
- Known thin spots: `InitGui.py`/`workbench.py` are untested; `test_script.py` (279 lines), `test_dispatch_health.py` (79), and `test_native_contract.py` (30) are small; `test_mcp_integration.py` exercises the real transport but through the shared stub tool modules, so real tool registration is covered only by the per-file schema tests.

## Verified Behaviors (live probe, 2026-09-11)

Verified end to end against a live FreeCAD 1.1.3 / OCC 7.8.1 server (add-on v2.0.0, local mode) using the structured tools plus raw JSON-RPC over both protocol eras. These pass; re-check an item only after changing the code area it covers.

**Transport and protocol.**

- Header-structure checks: duplicate single-value headers → 400; any `Transfer-Encoding` → 400; non-numeric or negative `Content-Length` → 400; NaN/Infinity body → `-32700`; body cap and `Content-Length` digits-only checks hold.
- `Content-Type` missing or wrong → 415; `Accept` without both media types → 406; bad `Host` or `Origin` (local mode) → 403; `GET /mcp` → 405 with `Allow: POST, DELETE`; unknown path and non-POST verbs on `/mcp` → 404 with JSON-RPC `-32601` (pinned contract — keep it).
- Unknown tool → `-32601` with HTTP 404 on the modern era and HTTP 200 on the legacy era (deliberate era difference).
- Routing-header mirroring holds: `MCP-Protocol-Version` must equal the `_meta` value, `Mcp-Method` must equal the method, and `tasks/get`/`tasks/update`/`tasks/cancel` mirror `params.taskId` in `Mcp-Name`; `MCP-Session-Id` is refused together with modern metadata.
- Argument limits return `-32602` with exact paths: `inspect_objects` selection ≤ 64, `edit_objects` ≤ 32 entries, `validate_geometry` ≤ 100 objects, `additionalProperties: false` everywhere.
- Tasks: `export` and `measure` detach when the client declares `io.modelcontextprotocol/tasks`; `tasks/get` returns the terminal result; unknown or expired id → `-32602`; `tasks/cancel` on a finished task answers benignly.
- Caching stamps: `tools/list` and the `discover_capabilities` tool carry `ttlMs: 0, cacheScope: private`; only `server/discover` without `refresh` carries `ttlMs: 3600000, public`.

**Consent (MRTR).** With a form-elicitation-capable legacy client: `elicitation/create` carries the fixed `confirmed` boolean form with target-specific text for open, import, overwrite-save, dirty close, dirty reload, and export overwrite; `accept` returns the final result on the same stream; `decline`, `cancel`, and a malformed response each end in `CONSENT_DENIED` with distinct detail reasons. A client without form support bypasses the prompt silently (documented 1.0 fallback).

**Documents and objects.** Name sanitization and dedupe; save-as and save-to-own-file without consent; consent for dirty close/reload; `create_objects` creates 1–32 entries atomically and returns requested-to-actual `nameMapping`; `edit_objects` rolls the whole batch when a later entry is invalid; `response_detail: "compact"` keeps post-state reports while omitting before-state deltas; `delete_object` refuses dependents and names them; `expected_solids`/`expected_bounds` gates report exact deviations and `operationState: rolled_back`; null-shape bodies are valid with `solid_count: null`. `open_document` reports `alreadyOpen` when the path was already open (FreeCAD returns the live in-memory document instead of re-reading the file).

**PartDesign and Sketcher.** `rectangle` composite expands to 4 segments + 8 auto constraints; explicit datums reach DoF 0; a padded 40×30×10 rectangle verifies at 12000 mm³; `fillet` from a signed edge reference; `gear_profile` wire; `hole` counterbore and thread parameters; `helix`, the eight `primitive` shapes, `subshape_binder`, `multi_transform`, `scaled`, and `datum_point`; `properties`/`parameters` exclusivity. Crash-input point positions (≥ 3), unrecorded `(type, arity)` shapes, invalid datum strings, `expected_generation` mismatches, non-sketch targets, empty batches, negative radii, 2-sided polygons, and duplicate polyline points are all refused before any native call. Signed topology references and pagination cursors refuse stale generations and tampered signatures (`stale_generation`, `stale_cursor`, `malformed_cursor`); topology pages report native 1-based indices and can select an explicit index list.

**Query, export, view, import.** `measure` distance/interference/section/faces plus signed-reference selectors; a `box` selector selects the one face or edge whose document-space bounds equal it within tolerance (enclosing boxes deliberately match nothing — take bounds from `inspect_topology`). `export` STL/STEP/3MF/FCStd verify by readback; paths outside `allowed_roots` (and outside the implicit absolute `recovery_directory`) are refused in preflight before any file effect; a missing parent directory produces an explicit error. `capture_view` honors explicit sizes and `focus_subelement` and rejects invalid `FaceN` with the face count. `import_model` STEP and STL report created objects with bounds, validity, solid count, geometry kind, and units. Forty concurrent exports complete without reaching the 32-operation cap (they serialize on the GUI thread).

**Fixes from the same probe — preserve these behaviors:**

- Dress-up binding normalizes the base first: `_apply_base_list` (fillet/chamfer/thickness/draft) touches the base feature and recomputes before assigning `Base`. A base feature that has executed only once (its creation) carries an element map that does not survive its next re-execution, so a dress-up bound to that map failed with `Invalid edge link` on the first length edit of any tool. Verified live: pad → fillet → first-write `edit_feature` and `edit_object` both succeed after the fix and both failed before it.
- `_apply_semantic_parameters` and `_apply_semantic_attachment` call `setExpression(prop, None)` only when a binding exists, so a numeric write touches exactly the property it changes.
- `server._run_preflight` re-raises the preflight `ToolError` with its own code and details (no traceback) instead of `GUI_DISPATCH_FAILED`.
- `_compact_capabilities` reports `supportedTypesUnavailable` when the snapshot was captured with no document open, instead of bare nulls for `supportedTypesCount`/`supportedTypesDocument`.
- `edit_sketch` prevalidates that `setDatums`/`setExpressions` targets are datum-capable constraints instead of surfacing a native `ValueError`/`IndexError` after rollback.
- `_convert_value` refuses JSON objects for unmapped property types with a domain error naming the property and its native type.
- `protocol.validate_schema` anyOf failures list the per-branch reasons (bounded) so union mismatches name the offending field.
- `_consent_denied_result` messages match the actual reason (timeout, invalid response, session closed).

Known remaining rough edges, accepted for now: creating a feature or importing leaves the object open in the GUI edit session (`inspect_documents.editObject` non-null).
