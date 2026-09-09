# FreeCAD MCP

FreeCAD add-on that embeds a Model Context Protocol server inside FreeCAD,
letting MCP clients drive FreeCAD directly over a local HTTP connection.

This version is a full rewrite of the original freecad-mcp as an embedded
MCP server, built on the great work in the original project.

## Rewrite summary

The original [freecad-mcp](https://github.com/neka-nat/freecad-mcp) ran as
two processes: an MCP proxy installed from PyPI (`uvx freecad-mcp`) that an
MCP client such as Claude Desktop launched over stdio, and an XML-RPC
server inside FreeCAD on port `9875` that the proxy relayed tool calls to.
This rewrite embeds the MCP server in FreeCAD's own process, so clients
connect to FreeCAD directly. The GUI-thread architecture and the workflow
concepts listed below carry over from the original.

### What was rewritten

* **Architecture.** The PyPI proxy package (`src/freecad_mcp`, FastMCP over
  stdio) and the in-FreeCAD XML-RPC server are gone. One embedded server
  speaks MCP over Streamable HTTP (JSON-RPC + SSE) at
  `http://127.0.0.1:9876/mcp`; no pip or uvx install and no client config
  file are needed.
* **Protocol.** XML-RPC with ad-hoc dictionaries became the MCP JSON-RPC
  wire protocol, version `2026-07-28`, with request-metadata headers,
  capability negotiation, and session-based support for the 2025
  Streamable HTTP revisions.
* **Tools.** Fifteen loosely typed tools became 17 tools validated against
  JSON input and output schemas, with structured error codes and paginated
  results. `execute_code` became `run_script`; `get_view` became
  `capture_view`; `get_rpc_status` became `discover_capabilities`;
  `insert_part_from_library` and `get_parts_list` were dropped — the parts
  library is reachable through `run_script`.
* **Security.** The IP allow-list alone became two explicit modes: local
  (loopback bind, Host/Origin checks, no token) and remote (bind to all
  interfaces, mandatory bearer token, optional CIDR allow-list), plus
  `allowed_roots` path containment for the file-touching tools.
* **Document safety.** Unvalidated success/error dictionaries became
  MCP-owned transactions with prevalidation, rollback, dependent-object
  checks and solid-count baselines.
* **Long-running work.** Blocking calls with client-side timeouts became
  detached tasks under the `io.modelcontextprotocol/tasks` extension, with
  polling and cooperative cancellation.
* **Consent.** Operations that touch untrusted or existing data — opening
  files, saving over paths, closing dirty documents — became explicit
  elicitation round trips instead of implicit effects.
* **FEM.** The legacy `SolverCcxTools` auto-create became the modern
  `Fem::SolverCalculiX` pipeline returning a VTK result summary; legacy
  solvers are refused with an explicit error.
* **Testing and packaging.** The original's proxy and RPC tests were
  replaced with a larger headless suite that runs without a FreeCAD
  install; CI dropped the MCP SDK compatibility job and now asserts the
  wheel ships the add-on and nothing else.

### Carried over from the original

* **GUI-thread dispatch.** A queue still ferries every FreeCAD operation to
  the main thread, woken by a Qt signal with a 500 ms heartbeat fallback,
  and ticks are still skipped while a mouse button is held so MCP work
  never interrupts 3D navigation.
* **Dispatch health.** A GUI operation that overruns its timeout still
  fails fast and blocks later GUI operations until healthy —
  `dispatch_health.py` moved across almost unchanged, now reported through
  `discover_capabilities`.
* **Persistent script namespace.** `execute_code`'s shared namespace with
  `FreeCAD`/`App` and `Gui` aliases survives as `run_script` sessions.
* **Add-on UX.** The "MCP Addon" workbench, the "FreeCAD MCP" toolbar and
  menu, the Auto-Start toggle, and settings in `freecad_mcp_settings.json`
  under FreeCAD's user app-data directory.
* **Remote connections and allowed IPs.** The CIDR allow-list carried over
  (`ip_parse.py`); enabling remote now additionally requires the bearer
  token, and the allow-list defaults to "any host" instead of `127.0.0.1`.
* **CalculiX FEM with a summary result, view screenshots with named
  orientations, and the cantilever FEM example** all survive in their new
  tool forms.
* **The demos below** were produced with the original add-on and still show
  the workflow this rewrite serves.

## Demo

### Design a flange

![demo](./assets/freecad_mcp4.gif)

### Design a toy car

![demo](./assets/make_toycar4.gif)

### Design a part from 2D drawing

#### Input 2D drawing

![input](./assets/b9-1.png)

#### Demo

![demo](./assets/from_2ddrawing.gif)

## Install the add-on

The add-on is installed into FreeCAD's `Mod` directory — either by copying
`addon/FreeCADMCP` there or by creating a symlink to it. It is not installed
with pip and does not run in a separate Python environment; the server executes
inside FreeCAD's own bundled Python (3.11 or newer).

FreeCAD user addon directories:

* Windows: `%APPDATA%\FreeCAD\Mod\`
* Mac:
  * FreeCAD 1.1: `~/Library/Application\ Support/FreeCAD/v1-1/Mod/`
  * FreeCAD 1.0: `~/Library/Application\ Support/FreeCAD/v1-0/Mod/`
* Linux:
  * Ubuntu: `~/.FreeCAD/Mod/` or `~/snap/freecad/common/Mod/` (if you install FreeCAD from snap)
  * Debian: `~/.local/share/FreeCAD/Mod`
  * Arch / CachyOS (FreeCAD 1.1 from `extra/freecad`): `~/.local/share/FreeCAD/v1-1/Mod/`
  * Flatpak: `~/.var/app/org.freecad.FreeCAD/data/FreeCAD/v1-1/Mod/`

Copy or symlink the add-on directory:

```bash
git clone https://github.com/neka-nat/freecad-mcp.git
cd freecad-mcp

# Copy (Linux, Ubuntu/Debian)
mkdir -p ~/.FreeCAD/Mod/
cp -r addon/FreeCADMCP ~/.FreeCAD/Mod/

# Symlink (macOS, FreeCAD 1.1) — updates automatically with the repository
mkdir -p ~/Library/Application\ Support/FreeCAD/v1-1/Mod/
ln -s "$(pwd)/addon/FreeCADMCP" ~/Library/Application\ Support/FreeCAD/v1-1/Mod/FreeCADMCP
```

Restart FreeCAD after installing. The server supports FreeCAD 1.1.3 <= version < 1.2; bundled Python 3.11 or newer.

Select "MCP Addon" from the workbench list to see the add-on UI.

![workbench_list](./assets/workbench_list.png)

The "FreeCAD MCP" toolbar and menu contain:

* **Start MCP Server** — bind the embedded HTTP server
* **Stop MCP Server** — stop it cleanly
* **Auto-Start Server** — persist a setting so the server starts on every
  FreeCAD launch (disabled by default; auto-start is never inherited from the
  legacy add-on's settings)
* **Remote Connections** — checkable opt-in that rebinds the server to all
  interfaces (`0.0.0.0`) and requires a bearer token; takes effect on the
  next server start
* **Configure Allowed IPs** — dialog editing the optional comma-separated
  peer allow-list (addresses or CIDR subnets); empty means any host may
  connect
* **Show Auth Token** — local dialog showing the endpoint and, in remote
  mode, the bearer token with a copy button

![start_rpc_server](./assets/start_rpc_server.png)

## Endpoint and security

Two access modes:

**Local only (default).** The server listens on `127.0.0.1` (port `9876`
by default) and no token is required — local tools just connect. Only
loopback peers are accepted, and the loopback Host/Origin checks reject
browser-borne cross-origin requests. There is no TLS and no CORS handling.

**Remote (opt-in via Remote Connections).** The server rebinds to all
interfaces (`0.0.0.0`) and a bearer token becomes mandatory on every
request; it is generated automatically when remote mode is first enabled.
`allowed_ips` defaults to empty — any host that has the token may connect.
To lock down beyond the token, populate the list with addresses or CIDR
subnets via **Configure Allowed IPs**. There is still no TLS — traffic is
plaintext — so enable remote access only on networks you trust, and never
forward, proxy, or tunnel the endpoint to untrusted networks.

Settings live in `freecad_mcp_settings.json` inside FreeCAD's user
application data directory (`port`, `token`, `auto_start`, `remote_enabled`,
`allowed_ips`, `allowed_roots`). The **Connection Details** dialog displays the
active state, endpoint, bind address, allowed IPs and, in remote mode, the
masked token with copy buttons.

**The bearer token is full local code-execution authority.** The
`run_script` tool executes arbitrary Python with the FreeCAD user's
privileges and is deliberately not restricted by `allowed_roots`. Treat the
token like a shell on this machine: never put it in logs, URLs, or shared
documents, and do not install this add-on on a machine you would not give
shell access to.

`allowed_roots` (default: your home directory) limits which filesystem paths
the file-taking tools (open/save/export and the working directories of FEM
runs) may touch. These are path-containment checks inside this server — they
are not a sandbox, and they never restrict what `run_script` can do.

## Connecting an MCP client

The server speaks the JSON-RPC MCP protocol over streamable HTTP. A client
must:

1. POST to `http://127.0.0.1:9876/mcp` with `Content-Type: application/json`
   and an `Accept` offering both `application/json` and
   `text/event-stream`. In remote mode every request must additionally
   carry `Authorization: Bearer <token>`; in local-only mode no
   Authorization header is needed.
2. Send the `MCP-Protocol-Version` header (`2026-07-28`) and the
   `Mcp-Method`/`Mcp-Name` request-metadata headers, with the matching
   protocol version and client information in each request's `_meta`.
3. Declare the `elicitation.form` capability to receive consent prompts,
   and the `io.modelcontextprotocol/tasks` extension to receive
   long-running operations as tasks. Clients without these capabilities
   are never blocked: consent-gated operations proceed without the
   prompt, and long-running operations return final results.

Clients that speak the 2025 Streamable HTTP revisions connect without any
client-side changes. Legacy sessions degrade deliberately:

* Tools always return final results; tasks are never offered, so
  long-running operations simply block until they finish.
* Consent prompts travel as native `elicitation/create` requests on the
  request-scoped SSE stream when the client declares form support. A
  `2025-03-26` client (or any client without form support) falls back to
  1.0 behavior: consent-required operations proceed without the prompt,
  and each bypass is noted in the Report view.
* Resource subscriptions and task methods are not advertised and return
  "unknown method" errors.
* JSON-RPC batches are accepted only for the `2025-03-26` revision.

[`examples/cantilever_fem.py`](examples/cantilever_fem.py) is a complete,
dependency-free client example that shows this handshake and a full FEM run.

## Agent skill

The repository ships an agent skill in [`skills/freecad-mcp/`](skills/freecad-mcp/SKILL.md). The
skill teaches coding agents how to drive this server: the 17-tool contract, FreeCAD modeling
patterns, geometry validation, FEM, and export. It complements the MCP connection — the agent
still talks to `http://127.0.0.1:9876/mcp`; the skill tells it how to use the tools well.

[`npx skills`](https://github.com/vercel-labs/skills) is the official installer for the open
agent skills ecosystem. It requires Node.js and supports Claude Code, Codex, Cursor, and 75+
other agents:

```bash
# List the skill without installing
npx skills add bradsjm/freecad-mcp --list

# Install interactively (auto-detects installed agents; symlinks by default)
npx skills add bradsjm/freecad-mcp

# Install globally to specific agents, non-interactive
npx skills add bradsjm/freecad-mcp --skill freecad-mcp -g -a claude-code -a codex -y
```

Project installs land in `./<agent>/skills/` (for example `.claude/skills/`); `-g` installs to
`~/<agent>/skills/` for all projects. The CLI symlinks to one canonical copy by default; pass
`--copy` when symlinks are not available. To use the skill once without installing:

```bash
npx skills use bradsjm/freecad-mcp --skill freecad-mcp --agent claude-code
```

Manage an installed skill with `npx skills list`, `npx skills update freecad-mcp`, and
`npx skills remove freecad-mcp`. Start the server (**Start MCP Server**), connect the agent to
the endpoint as described above, and the skill supplies the operating procedure.

## Tools

The server exposes 17 tools:

* `discover_capabilities`: report the FreeCAD/OCC versions, workbenches,
  supported types, exporter and FEM availability, plus GUI dispatch health.
* `new_document`: create an empty document and return its actual sanitized
  Name, Label and object count.
* `open_document`: open an FCStd file after consent (opening an untrusted
  file is never implicit).
* `save_document`: save to the existing path, or save-as to a new one (saving
  over an existing target requires consent).
* `close_document`: close a document; a dirty or unsaved nonempty document
  requires consent first.
* `reload_document`: close and reopen a saved document from its file,
  discarding unsaved changes only after explicit consent.
* `inspect_objects`: list a document's objects with placement, bounds, shape
  validity and solid counts; paginated with a signed cursor.
* `create_object`: create a supported Part/App type or a FEM object through
  an explicit factory mapping (modern analysis, `Fem::SolverCalculiX`,
  materials and constraints).
* `edit_object`: assign properties with prevalidation so an invalid property
  leaves earlier ones unchanged; canonical `{object, subelement}` links only.
* `delete_object`: remove an object, refusing objects that still have
  dependents instead of cascading silently.
* `edit_parameters`: add, rename and bind expressions on dynamic properties
  with full validation and rollback.
* `validate_geometry`: per-object state and shape validity, solid count,
  volume, bounds and tolerance diagnostics, optionally against expected
  bounds.
* `measure`: distance, interference, section and face measurements between
  objects or their subshapes.
* `export`: write STL, STEP, 3MF or a native FCStd copy, verifying every
  file by reading it back.
* `capture_view`: capture a PNG of a document's 3D view with an explicit
  orientation (Isometric, Front, Top, ...) framed on one existing object,
  preserving the caller's selection and active document.
* `run_fem`: run a FEM analysis through the modern `Fem::SolverCalculiX`
  pipeline and return the loaded VTK result summary (`.vtm` and `.vtu`
  blocks, point/cell counts and finite result ranges).
* `run_script`: execute arbitrary FreeCAD Python code in a persistent
  per-session namespace — the escape hatch for workflows the structured
  tools do not cover (e.g. meshing or the parts library).

Document creation, opening and reloading return `name`, `label` and
`objectCount`; use the returned `name` as the `document` argument in later calls.

Structured object and parameter edits use MCP-owned transactions and refuse to
nest inside a user's active transaction. A failed edit aborts and recomputes the
restored document; rollback failures are reported separately. New volumetric
geometry defaults to one solid unless `expected_solids` specifies otherwise.
Existing valid dependent solid counts are preserved when their inputs change.

### Consent

Destructive or untrusted operations — opening an untrusted FCStd file,
saving over an existing path, closing or reloading a dirty document — offer
an MRTR elicitation round trip before taking effect. A client that
declares `elicitation.form` must answer the fixed `confirm` boolean form;
declining, cancelling, or tampering with the signed consent state aborts
the operation without any effect. A client without form support falls back
to 1.0 behavior and proceeds without the prompt; each bypass is noted in
the Report view.

### Long-running operations and cancellation

`run_fem`, `run_script`, `export` and `measure` can run as detached tasks
under the `io.modelcontextprotocol/tasks` extension: the tool call returns a
task ID immediately, `tasks/get` polls for the terminal result, and
`tasks/cancel` requests cooperative cancellation. Cancellation is honest
about its limits: a running CalculiX solver is never killed by a timeout or
cancel request; the tool result reports whether cancellation was requested
but the process keeps running until it finishes. Likewise, a `run_script`
deadline that has already started is reported while the code continues
running to completion.

### GUI dispatch timeouts

FreeCAD GUI-thread operations cannot be force-cancelled safely. If a
GUI-thread operation exceeds its timeout after starting, the server reports
a stuck dispatcher and rejects later GUI operations immediately. Use
`discover_capabilities` to inspect the reported dispatch health; document
queries run on the GUI thread alongside modelling operations. If the health
does not return to healthy after the operation finishes, restart FreeCAD.

### `run_script` sessions

`run_script` executes on the GUI thread inside a namespace seeded with
`FreeCAD`/`App` and `Gui` aliases, and variables survive between calls to the
same `session_id` for the server's lifetime. At most 32 sessions are kept;
new sessions are refused rather than evicting live state. An exception in
executed code returns captured stdout, stderr and the traceback. Code
execution still has FreeCAD's full privileges — it is not sandboxed.

## Development

The Python code lives in `addon/FreeCADMCP/mcp_server` and `tests/`. Tests
use stubs so they run without a FreeCAD installation:

```bash
uv run pytest -q
```

The project targets Python 3.11+ and has no runtime dependencies.
