[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/neka-nat-freecad-mcp-badge.png)](https://mseep.ai/app/neka-nat-freecad-mcp)

# FreeCAD MCP

FreeCAD add-on that embeds a Model Context Protocol server inside FreeCAD,
letting MCP clients drive FreeCAD directly over a local HTTP connection.

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

Restart FreeCAD after installing. The server requires FreeCAD 1.1.3 or later.

Select "MCP Addon" from the workbench list to see the add-on UI.

![workbench_list](./assets/workbench_list.png)

The "FreeCAD MCP" toolbar and menu contain:

* **Start MCP Server** — bind the embedded HTTP server
* **Stop MCP Server** — stop it cleanly
* **Auto-Start Server** — persist a setting so the server starts on every
  FreeCAD launch (disabled by default; auto-start is never inherited from the
  legacy add-on's settings)
* **Show Auth Token** — local dialog showing the endpoint and bearer token
  with a copy button

![start_rpc_server](./assets/start_rpc_server.png)

## Endpoint and security

The server listens only on the loopback interface — `127.0.0.1` on the
configured port (default `9876`) — with the single JSON-RPC endpoint
`POST /mcp`. There is no remote binding, no TLS, and no CORS handling: the
endpoint is not designed to be reachable from other machines and must never be
forwarded, proxied, or exposed through a tunnel.

Every request must present the bearer token. The token is generated on first
start and stored with the rest of the server settings in
`freecad_mcp_settings.json` inside FreeCAD's user application data directory
(together with `port`, `auto_start`, `allowed_ips` and `allowed_roots`). Use
the **Show Auth Token** dialog to display and copy it.

**The bearer token is full local code-execution authority.** The
`run_script` tool executes arbitrary Python with the FreeCAD user's
privileges and is deliberately not restricted by `allowed_roots`. Do not put
the token in logs, URLs, or shared documents, and do not install this add-on
on a machine you would not give shell access to.

`allowed_roots` (default: your home directory) limits which filesystem paths
the file-taking tools (open/save/export and the working directories of FEM
runs) may touch. These are path-containment checks inside this server — they
are not a sandbox, and they never restrict what `run_script` can do.

## Connecting an MCP client

The server speaks the JSON-RPC MCP protocol over streamable HTTP. A client
must:

1. POST to `http://127.0.0.1:9876/mcp` with
   `Authorization: Bearer <token>`, `Content-Type: application/json`, and an
   `Accept` offering both `application/json` and `text/event-stream`.
2. Send the `MCP-Protocol-Version` header (`2026-07-28`) and the
   `Mcp-Method`/`Mcp-Name` request-metadata headers, with the matching
   protocol version and client information in each request's `_meta`.
3. Declare the `elicitation.form` capability to support consent prompts, and
   the `io.modelcontextprotocol/tasks` extension to receive long-running
   operations as tasks.

[`examples/cantilever_fem.py`](examples/cantilever_fem.py) is a complete,
dependency-free client example that shows this handshake and a full FEM run.

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
saving over an existing path, closing or reloading a dirty document — issue
an MRTR elicitation round trip before taking effect. The client must answer
the fixed `confirm` boolean form; declining, cancelling, or tampering with
the signed consent state aborts the operation without any effect.

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

## Contributors

<a href="https://github.com/neka-nat/freecad-mcp/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=neka-nat/freecad-mcp" />
</a>

Made with [contrib.rocks](https://contrib.rocks).
