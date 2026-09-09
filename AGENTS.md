# Repository Guidelines

## Project Overview

FreeCAD add-on that embeds a Model Context Protocol (MCP) server inside FreeCAD's GUI. MCP clients drive FreeCAD over streamable HTTP (JSON-RPC + SSE) at `POST http://127.0.0.1:9876/mcp`, protocol version `2026-07-28`. Security model: local-only mode (default) binds loopback and needs no token; `remote_enabled` rebinds `0.0.0.0` and makes a bearer token mandatory (`allowed_ips` defaults to empty = any host; a populated list restricts peers further). The add-on runs inside FreeCAD's bundled Python (3.11+) and requires FreeCAD 1.1.3 or later; startup fails closed outside `1.1.3 <= v < 1.2`. MIT licensed.

The server exposes 23 tools in a fixed order: `discover_capabilities`, `new_document`, `open_document`, `import_model`, `save_document`, `close_document`, `reload_document`, `inspect_objects`, `create_object`, `edit_object`, `edit_objects`, `delete_object`, `validate_geometry`, `measure`, `inspect_topology`, `edit_parameters`, `inspect_sketch`, `edit_sketch`, `create_feature`, `export`, `capture_view`, `run_fem`, `run_script`.

## Architecture & Data Flow

**Startup (GUI only):** `addon/FreeCADMCP/InitGui.py` (`Init.py` is deliberately empty — console mode gets nothing) registers the "MCP Addon" workbench plus three commands from `mcp_server/commands.py` (`Toggle_MCP_Server`, `Connection_Details`, `MCP_Settings`), then optionally auto-starts via `server.start_server()`: FreeCAD version guard → `load_settings()` (fail closed) → construct `Server` → `gui_dispatch.initialize()` → `ThreadingHTTPServer` on a daemon thread → document observer. `server.py` holds a module-level `_server` singleton; restart is refused while GUI operations are still pending.

**Request path (end to end):**

1. `http_server.py` — ThreadingHTTPServer: peer gate (local = loopback only; remote = `allowed_ips` list or open when empty), Host/Origin checks in local mode only, constant-time bearer compare when a token is set, body-size and transfer-encoding limits. Never imports FreeCAD.
2. `protocol.py` `validate_request` — JSON-RPC envelope, header mirroring (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`, `Mcp-Param-*`), client-capability checks.
3. `server.py` `Server.dispatch` — routes 9 methods. `tools/call` validates arguments against the tool's `inputSchema`, runs consent preflight (MRTR elicitation via `InputRequired`; HMAC-signed `requestState` consumed as a single-use nonce), then executes blocking or detached.
4. The handler runs **on the FreeCAD GUI thread** through `gui_dispatch.py` (a `queue.Queue` woken by a Qt signal; FreeCAD's main loop drains it). The result is validated against `outputSchema` and returned as a complete tool result. Application failures are complete results with `isError: true` and structured `{code, message, details}` — never JSON-RPC errors. Only protocol-level violations become JSON-RPC errors.
5. Task-eligible tools (`run_script`, `run_fem`, `export`, `measure`) may detach under the `io.modelcontextprotocol/tasks` extension (`tasks.py`): the call returns a task id immediately; clients poll `tasks/get` and request `tasks/cancel`.

**Hard rules an editor must preserve:**

- HTTP worker threads never touch FreeCAD. All FreeCAD work goes through `gui_dispatch`.
- Cancellation is cooperative (`threading.Event`). A running CalculiX solve is never killed; a timed-out GUI operation poisons dispatch health (later GUI ops are rejected until healthy — inspect via `discover_capabilities`). Never report a stuck operation as stopped.
- Two security modes only: local (loopback bind, loopback peers, Host/Origin checks, no token) and remote (`remote_enabled`: bind `0.0.0.0`, token mandatory on every request, `allowed_ips` empty = open). Never add TLS or CORS handling. Remote start without a token must fail. The bearer token is full local code-execution authority (`run_script` is intentionally unsandboxed); never log or print it.
- `allowed_roots` is path containment for file-touching tools only — not a sandbox, and it never constrains `run_script`.
- FEM uses the modern `Fem::SolverCalculiX` pipeline only; legacy `CcxTools` or ambiguous solvers produce explicit errors. Missing CalculiX produces an actionable error, never an auto-install.

## Key Directories

- `addon/FreeCADMCP/` — the shipped add-on. `mcp_server/` holds the core server; `mcp_server/tools/` holds one module per tool domain.
- `tests/` — pytest suite mirroring the source; runs headless with stubs (no FreeCAD install needed).
- `examples/cantilever_fem.py` — manual, stdlib-only MCP client demo (not a test; run with `FREECAD_MCP_TOKEN=... python3 examples/cantilever_fem.py`).
- `.github/workflows/test.yml` — CI gates.
- `assets/` — demo images only.

## Development Commands

```bash
uv sync                                                  # env (dev group = pytest + ruff)
uv run pytest -q                                         # full suite, headless
uv run pytest tests/test_settings.py -q                  # one file
uv run pytest tests/test_settings.py::test_invalid_port_fails_closed   # one test
uv run pytest tests/test_protocol.py::TestErrorPrecedence -q           # one class
uv run pytest -k "consent" -q                            # keyword filter
uv run ruff check .                                      # lint gate
uv run ruff check --fix .                                # apply safe lint fixes
uv run ruff format .                                     # format gate
uv run python -m compileall addon                        # CI syntax gate
uv lock --check                                          # lockfile sync gate
```

Lint and format rules live in `pyproject.toml` under `[tool.ruff]`. Read that file before you change a rule. The configuration selects defect-focused rule families and ignores `SIM105` deliberately: the mutation gate and transport layers attach per-branch diagnostics, and `contextlib.suppress` would hide the failing native call. Test files add `E402` and `RUF012` exemptions for the `sys.path` import preamble and FreeCAD-shaped mutable stub state.

Every completion MUST pass both ruff gates. Run `uv run ruff check .` and `uv run ruff format --check .` before you report a task as complete. CI enforces the same commands.

Manual run: symlink `addon/FreeCADMCP` into FreeCAD's `Mod/` directory (paths in README), start FreeCAD, select the "MCP Addon" workbench, then use the **Start MCP Server** toolbar action. The token is shown in **Connection Details…** or stored in `freecad_mcp_settings.json` under FreeCAD's user app-data directory.

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
  `server._register_tools()` consumes `(definitions, handlers, preflight)` triples and enforces the exact 23-tool `PLAN_TOOL_ORDER`; a missing, extra, or duplicate tool name raises `RuntimeError`. Handler signature: `def handler(ctx, arguments: dict) -> dict`. `ctx` is an `_OpContext` over the `Server` exposing `require_document`, `require_object`, `App`, `Gui`, `settings`, `signer`, `cancel_event`, and friends.
- **Schemas** use a finite JSON-Schema subset: `additionalProperties: false`, `$ref` only into local `$defs`, no NaN/infinity. Schemas are checked at registration and per call; an output-schema violation is an infrastructure error (`-32603`).
- **Errors:** raise `ToolError(code, message, details)` with stable codes (`DOCUMENT_NOT_FOUND`, `OBJECT_NOT_FOUND`, `VALIDATION_FAILED`, `GUI_DISPATCH_FAILED`, `CONSENT_DENIED`, `PATH_NOT_ALLOWED`, `UNSUPPORTED_VIEW`, `SOLVER_FAILED`, `SERVER_BUSY`). `ProtocolError` maps to a JSON-RPC error; `InputRequired` starts a consent round trip.
- **Import discipline (what keeps tests headless):** `protocol`, `http_server`, `tasks`, `subscriptions`, `settings`, `ip_parse`, `object_validation`, and `dispatch_health` must not import FreeCAD or Qt. FreeCAD/Part imports live inside handlers or only in GUI-facing modules (`InitGui`, `commands`, `gui_dispatch`, `server`, and the tool modules that need host types). Tool modules import siblings lazily to stay registration-order independent.
- **Mutations** go through `object_validation.mutation()`: refuses to nest inside a user transaction, opens/commits/aborts a FreeCAD transaction, recomputes, validates dependents and solid-count baselines, and reports rollback failures separately. A dependent closure over 256 objects is refused before any effect.
- **Concurrency:** no asyncio. Locks are fine-grained and per-domain and are never held across a GUI dispatch. The only async path is `tools/fem.py` (QProcess signals on the GUI thread plus a retained `Future` the server flattens).
- **JSON safety:** all output floats are finite; `canonical_json`/fingerprint sign consent targets. Consent signatures use a fresh in-memory HMAC key per server start (they expire on restart).
- **Naming:** snake_case modules; `_UPPER_SNAKE` module constants; tests use `make_*` factories, `Fake*`/`Stub*` doubles, and `load_*` module loaders.
- **Lint and format:** run `uv run ruff check .` and `uv run ruff format --check .` before you report a task as complete. Do not add `# noqa` to silence a finding; fix the code, or change the rule in `pyproject.toml` when the rule conflicts with a documented design decision. FreeCAD-injected globals (for example `Gui` in `InitGui.py`) are the only accepted inline `# noqa` case, and they MUST carry a reason comment.

## Important Files

- `addon/FreeCADMCP/InitGui.py` — GUI entry point and auto-start. `Init.py` stays empty.
- `addon/FreeCADMCP/mcp_server/server.py` (~2200 lines) — orchestrator: tool registry, dispatch, consent choreography, deadlines (default 60 s; export/measure 600 s; `timeout_s` clamped 1–3600; preflight 30 s), the 32-concurrent-operation cap, and the 32 script-session cap (new sessions are refused, never evicted).
- `mcp_server/http_server.py` — transport, auth, SSE. `protocol.py` — wire contract, error codes, `ConsentSigner`. `gui_dispatch.py` — GUI-thread bridge and dispatch health. `object_validation.py` — mutation gate and geometry reports. `tasks.py` — task store (1024 retained / 32 nonterminal). `subscriptions.py` — connection-scoped 256-event queues. `settings.py` — atomic, fail-closed settings (`port`, `token`, `auto_start`, `remote_enabled`, `allowed_ips`, `allowed_roots`).
- `mcp_server/tools/` — `documents`, `import_model`, `objects`, `geometry`, `sketch`, `features`, `parameters`, `export`, `view`, `fem`, `script`.
- `pyproject.toml` — pytest config plus the ruff lint/format rules. `README.md` — install, handshake, and tool docs.

## Runtime/Tooling Preferences

- Package manager: **uv** (lockfile committed; keep `uv lock --check` green).
- Python `>= 3.11` (local pin 3.11; CI matrix 3.11/3.12/3.13).
- **Zero runtime dependencies** — the add-on runs inside FreeCAD's bundled Python. Do not add any. The dev group is pytest and ruff only.
- Distribution: the add-on ships via git and the FreeCAD Addon Manager (`package.xml`); no wheel is built or published.

## Testing & QA

- pytest only; `testpaths = ["tests"]`; **no conftest.py** — each test file is self-contained, with its own stubs and its own `ADDON_DIR` sys.path preamble. Follow that convention for new tests.
- Tests never import real FreeCAD. Stubbing tiers used across the suite: pure modules imported directly (with `FakeClock`); lazy-import tool modules exercised through `FakeCtx`/`FakeDoc` doubles; FreeCAD/Qt-importing modules loaded via `importlib` under unique names with `sys.modules` stubs; `test_http_server.py` runs a real HTTP server on port 0 through `http.client`/raw sockets; `test_server.py` drives the real `Server.dispatch` over contract-shaped fake tool modules.
- Zero-sleep discipline: use `FakeClock` and `threading.Event` barriers, never `time.sleep`.
- CI gates to keep green (`.github/workflows/test.yml`): `uv lock --check`; `uv run ruff check .`; `uv run ruff format --check .`; `uv run python -m compileall addon` on 3.11/3.12/3.13; `uv run pytest -q`.
- Known thin spots: `InitGui.py` is untested; `test_script.py` and `test_dispatch_health.py` are small; there is no end-to-end test wiring the real HTTP server into the real `Server.dispatch`.
