# FreeCAD Embedded MCP

FreeCAD add-on that embeds a Model Context Protocol server inside FreeCAD. MCP clients can drive FreeCAD over a local or remote HTTP connection. Remote connections are optional and require a token.

This project is a full rewrite of the original [freecad-mcp](https://github.com/neka-nat/freecad-mcp), built on the original project's work.

## Contents

- [Demo](#demo)
- [Install the add-on](#install-the-add-on)
- [Endpoint and security](#endpoint-and-security)
- [Protocol surface](#protocol-surface)
- [Connecting an MCP client](#connecting-an-mcp-client)
- [Agent skill](#agent-skill)
- [Tools](#tools)
- [Development](#development)
- [Rewrite summary](#rewrite-summary)

## Rewrite summary

The original [freecad-mcp](https://github.com/neka-nat/freecad-mcp) used two processes: an MCP proxy installed from PyPI (`uvx freecad-mcp`) that an MCP client such as Claude Desktop launched over stdio, and an XML-RPC server inside FreeCAD that handled tool calls.

This rewrite **embeds the MCP server in FreeCAD's own process**, so clients connect to FreeCAD directly. The GUI-thread architecture and workflow concepts from the original project carry over.

## Original Demos

### Design a flange

![FreeCAD flange design demo](./assets/freecad_mcp4.gif)

### Design a toy car

![FreeCAD toy car design demo](./assets/make_toycar4.gif)

### Design a part from a 2D drawing

#### Input drawing

![Input 2D drawing](./assets/b9-1.png)

#### Demo

![2D drawing workflow demo](./assets/from_2ddrawing.gif)

## Install the add-on

Install through FreeCAD's Add-on Manager (recommended), or copy the add-on folder into FreeCAD's `Mod` directory. The add-on runs inside FreeCAD's bundled Python (3.11 or newer); it does not use a separate Python environment.

### Install with the Add-on Manager

1. Open **Edit → Preferences → Addon Manager**.
2. Find **Custom repositories**.
3. Click **Add**.
4. Enter the following values:

   | Field | Value |
   | --- | --- |
   | Repository URL | `https://github.com/bradsjm/freecad-embedded-mcp` |
   | Branch | `v2.0-embedded-mcp` |

5. Confirm with **OK** and close Preferences.
6. Open **Tools → Addon Manager**.
7. Search for `FreeCAD MCP` and click **Install**.
8. If it does not appear, select your custom repository in the source filter.

### Install manually

FreeCAD user add-on directories:

| Platform | Directory |
| --- | --- |
| Windows | `%APPDATA%\FreeCAD\Mod\` |
| macOS, FreeCAD 1.1 | `~/Library/Application Support/FreeCAD/v1-1/Mod/` |
| Ubuntu | `~/.FreeCAD/Mod/` |
| Ubuntu snap | `~/snap/freecad/common/Mod/` |
| Debian | `~/.local/share/FreeCAD/Mod` |
| Arch / CachyOS (FreeCAD 1.1 from `extra/freecad`) | `~/.local/share/FreeCAD/v1-1/Mod/` |
| Flatpak | `~/.var/app/org.freecad.FreeCAD/data/FreeCAD/v1-1/Mod/` |

Copy or symlink the add-on directory:

```bash
git clone https://github.com/bradsjm/freecad-embedded-mcp.git
cd freecad-embedded-mcp

# Copy (Linux, Ubuntu/Debian)
mkdir -p ~/.FreeCAD/Mod/
cp -r addon/FreeCADMCP ~/.FreeCAD/Mod/

# Symlink (macOS, FreeCAD 1.1) — updates automatically with the repository
mkdir -p ~/Library/Application\ Support/FreeCAD/v1-1/Mod/
ln -s "$(pwd)/addon/FreeCADMCP" ~/Library/Application\ Support/FreeCAD/v1-1/Mod/FreeCADMCP
```

Restart FreeCAD after installing. The server supports `1.1.3 <= version < 1.2` and bundled Python 3.11 or newer.

Select **MCP Addon** from the workbench list to see the add-on UI.

![FreeCAD workbench list](./assets/workbench_list.png)

The **FreeCAD MCP** toolbar has one contextual action. Its label, icon, and availability follow the confirmed server state:

| Server state | Action |
| --- | --- |
| Fully stopped | **Start MCP Server** — bind the embedded HTTP server |
| Running | **Stop MCP Server** — stop the server cleanly |
| Starting or stopping or draining | The action is disabled and shows the current transition |

The menu also provides:

- **Connection Details…** — endpoint, access mode, bind address, allowed IPs, and, when network access is enabled, the masked bearer token with copy buttons.
- **MCP Settings…** — port, auto-start, network access, allowed IPs, allowed roots, verified recovery copies, and unrestricted Python access in one dialog. Changes take effect on the next server start and never restart a running server.

A status-bar indicator shows the confirmed state, such as **MCP: Running (Local only)**, **MCP: Running (Network enabled)**, **MCP: Running — GUI blocked**, **MCP: Starting**, **MCP: Stopping (N operations)**, or **MCP: Stopped**, and opens **Connection Details…** when clicked.

## Endpoint and security

The server has two access modes:

| Mode | Bind address | Authentication | Peer restrictions |
| --- | --- | --- | --- |
| **Local only** (default) | `127.0.0.1` on port `9876` by default | No token | Loopback peers only; loopback Host/Origin checks reject browser-borne cross-origin requests |
| **Network access** (opt-in) | `0.0.0.0` | Bearer token required on every request | `allowed_ips` is empty by default (any host with the token); populated addresses and CIDR subnets restrict peers further |

There is no TLS or CORS handling. Network access sends plaintext traffic, so enable it only on networks you trust. Never forward, proxy, or tunnel the endpoint to an untrusted network.

Settings live in `freecad_mcp_settings.json` inside FreeCAD's user application data directory:

- `port`
- `token`
- `auto_start`
- `remote_enabled`
- `allowed_ips`
- `allowed_roots`
- `recovery_enabled`
- `recovery_directory`
- `allow_scripts`

When `remote_enabled` is true and no token exists, the server generates and persists one before it starts. **Security warning:** The bearer token grants full local code-execution authority. The `run_script` tool executes arbitrary Python with the FreeCAD user's privileges and is deliberately not restricted by `allowed_roots`.

`allowed_roots` defaults to the user's home directory. It contains document paths, export destinations, and FEM working directories; it is not a sandbox for `run_script`.

Recovery copies require an absolute `recovery_directory`. The configured directory is allowed automatically for reads and writes, so it does not have to be listed in `allowed_roots`.

## Protocol surface

Current-protocol clients use MCP version `2026-07-28` and send the request-metadata headers `MCP-Protocol-Version` and `Mcp-Method`, plus the mirrored `Mcp-Name` or `Mcp-Param-*` headers required by the request. The server validates the headers against the JSON-RPC body.

The legacy adapter accepts Streamable HTTP revisions `2025-03-26`, `2025-06-18`, and `2025-11-25`. Revision `2025-03-26` also accepts JSON-RPC batches. Legacy clients initialize a session and send its `MCP-Session-Id` on later requests.

The embedded server routes these MCP methods:

- `server/discover` — return cached or refreshed server capabilities.
- `tools/list` — return the active tool surface and JSON schemas.
- `tools/call` — validate and run one structured tool.
- `tasks/get`, `tasks/update`, and `tasks/cancel` — manage detached operations when the client declares `io.modelcontextprotocol/tasks`.
- `subscriptions/listen` — open an SSE response stream for acknowledged, task, or document-resource notifications.
- `resources/list` and `resources/read` — expose the live document resource `freecad://documents`.

Subscription filters can select `freecad://documents` updates and task IDs. Task-ID filters require the Tasks extension and same-principal task ownership. The server does not currently honor tools-, prompts-, or resources-list-changed boolean filters.

The server does not provide a standalone GET-based SSE endpoint. `subscriptions/listen` is a POST request that returns an SSE stream.

Protocol failures are JSON-RPC errors. Tool failures are complete results with `isError: true` and a structured error code, message, and details.

The transport accepts up to 64 HTTP connections and 32 active SSE streams. It limits request bodies and responses to 8 MiB and one SSE event to 1 MiB. The server allows up to 32 active subscriptions, with a 1024-character subscription id, and each subscription queue is limited to 256 events and 4 MiB.

## Connecting an MCP client

The server speaks the JSON-RPC MCP protocol over Streamable HTTP at `http://127.0.0.1:9876/mcp` by default. Start FreeCAD and start the MCP server before connecting a client. If you enable **Network access**, replace the loopback URL with the configured address and send `Authorization: Bearer <token>` on every request.

### Claude Code

Add the server with the Claude Code CLI:

```bash
claude mcp add --transport http freecad http://127.0.0.1:9876/mcp
```

For Network access, include the bearer token:

```bash
claude mcp add --transport http freecad http://HOST:9876/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

Run `claude mcp list` to check the connection.

### Codex

Add a Streamable HTTP server to Codex with `config.toml` (`~/.codex/config.toml` or a project-scoped `.codex/config.toml`):

```toml
[mcp_servers.freecad]
url = "http://127.0.0.1:9876/mcp"
```

For Network access, store the token in an environment variable and add:

```toml
[mcp_servers.freecad]
url = "http://HOST:9876/mcp"
bearer_token_env_var = "FREECAD_MCP_TOKEN"
```

Then run `codex mcp list` to verify the configuration. Codex also shares this configuration with its desktop app and IDE extension.

### Other MCP clients with HTTP support

Choose **Streamable HTTP** (sometimes named `HTTP` or `streamable-http`) and use:

```text
http://127.0.0.1:9876/mcp
```

For Network access, configure the request header `Authorization: Bearer YOUR_TOKEN`. A generic client configuration commonly looks like this:

```json
{
  "mcpServers": {
    "freecad": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:9876/mcp"
    }
  }
}
```

Use the client’s HTTP or Streamable HTTP setting when it provides one. Do not configure this URL as a local command or an SSE endpoint.

### Clients without HTTP support

Use `mcp-remote` to bridge a STDIO-only client to the embedded HTTP server. For example, a client that accepts an `mcpServers` configuration can use:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:9876/mcp"]
    }
  }
}
```

For Network access, add the bearer header to the arguments. Keep the token out of the configuration when possible by using an environment variable or `--header-file`:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "http://HOST:9876/mcp",
        "--header", "Authorization: Bearer ${FREECAD_MCP_TOKEN}"
      ],
      "env": {
        "FREECAD_MCP_TOKEN": "YOUR_TOKEN"
      }
    }
  }
}
```

`mcp-remote` is the HTTP-to-STDIO bridge required for this use case. The similarly named `mcp-proxy` package provides the opposite direction: it starts a STDIO server and exposes it over HTTP.

## Tools

The server registers 26 tools. `run_script` is hidden unless the
`allow_scripts` setting is enabled, so a default server exposes 25.

| Tool | Purpose |
| --- | --- |
| `discover_capabilities` | Report FreeCAD/OCC versions, workbenches, supported types, exporter and FEM availability, and GUI dispatch health. |
| `inspect_documents` | List open documents with `name`, `label`, `fileName`, `objectCount`, `generation`, `dirty`, `active`, `transactionOpen`, and `editObject`, plus the `activeDocument`. |
| `new_document` | Create an empty document and return its actual sanitized `Name`, `Label`, and object count. |
| `open_document` | Open an FCStd file. |
| `import_model` | Import a STEP or STL file behind file consent, reporting created objects, bounds, validity, and units. |
| `save_document` | Save to the existing path or save as a new path. |
| `close_document` | Close a document. |
| `reload_document` | Close and reopen a saved document from its file. |
| `inspect_objects` | List document objects, or a 1–64 object selection, with placement, bounds, shape validity, and solid counts. Results are paginated with a signed cursor and support compact/full detail and property pages. |
| `create_object` | Create a supported Part/App type or FEM object through an explicit factory mapping, including modern analysis, `Fem::SolverCalculiX`, materials, and constraints. |
| `create_objects` | Create 1–32 objects of supported types atomically in one transaction with optional per-object expectations and a requested-to-actual `nameMapping`; sibling links use a later edit because names are resolved after creation. |
| `edit_object` | Assign properties with prevalidation so an invalid property leaves earlier properties unchanged. Supports canonical `{object, subelement}` links only, optional commit-time bounds expectations, and `response_detail: "compact"` or `"full"` change reports. |
| `edit_objects` | Edit 1–32 existing objects atomically in one transaction with optional per-object expectations and compact/full change reports. |
| `delete_object` | Remove an object, refusing objects that still have dependents instead of cascading silently. |
| `validate_geometry` | Report per-object state and shape validity, solid count, volume, bounds, and tolerance diagnostics, optionally against expected bounds. |
| `measure` | Measure distance, interference, section, and face relationships between objects, bbox-selected subshapes, or signed topology references. |
| `inspect_topology` | Page through an object's faces or edges with native 1-based indices, bounds, sampled centers/normals, native type names, and signed references; request an explicit index list when needed. |
| `edit_parameters` | Add, rename, bind expressions on, and clear expressions from dynamic properties with full validation and rollback. |
| `inspect_sketch` | Report sketch geometry and constraint rows in native index order with the solver degree-of-freedom summary and expression bindings. |
| `edit_sketch` | Apply one atomic batch of sketch operations: add geometry (including `rectangle`, `polyline`, and `regularPolygon` profiles), add constraints, set datums, bind constraint expressions, and delete geometry or constraints. |
| `create_feature` | Create one of 24 PartDesign feature kinds inside a Body — `datum_plane`, `datum_line`, `sketch`, `pad`, `pocket`, `hole`, `revolve`, `groove`, `fillet`, `chamfer`, `thickness`, `draft`, `linear_pattern`, `polar_pattern`, `mirrored`, `loft`, `pipe`, the involute `gear_profile`, `helix` (additive or subtractive, pitch/height/turns driven), the eight `primitive` shapes (additive or subtractive), a same-document `subshape_binder` reference holder, an atomic `multi_transform` composite, `scaled` features, and `datum_point`s — wiring profile and support and validating the Body's final geometry. Semantic `parameters` map onto native properties; lengths are mm and angles are degrees. |
| `edit_feature` | Edit the native parameters of one existing pad, pocket, hole, or gear feature that belongs to the named Body — for the hole this includes its cut, depth-type and thread parameters — returning actual before/after values and Body validation. |
| `export` | Write STL, STEP, 3MF, or a native FCStd copy, verifying every file by reading it back. |
| `capture_view` | Capture a PNG of a document's 3D view with one of the named orientations, framed on one existing object or validated face/edge while preserving the caller's selection and active document. |
| `run_fem` | Run a FEM analysis through the modern `Fem::SolverCalculiX` pipeline and return the loaded VTK result summary (`.vtm` and `.vtu` blocks, point/cell counts, and finite result ranges). |
| `run_script` (opt-in) | Execute arbitrary FreeCAD Python code in a persistent per-session namespace. This is the escape hatch for workflows that structured tools do not cover, such as meshing or the parts library. |

`capture_view` accepts `Isometric`, `Front`, `Top`, `Right`, `Back`, `Left`, `Bottom`, `Dimetric`, and `Trimetric`. An omitted size follows the active viewport and scales to a maximum 768-pixel longest edge. An explicit width or height can be up to 4096 pixels.

### Placement and bounds conventions

`App::PropertyPlacement` values use `{"position": [x, y, z], "axis": [x, y, z], "angle_deg": n}`. The legacy `{"Base": ..., "Rotation": ...}` form is also accepted. Object bounds and `expected_bounds` use document-space `[xmin, ymin, zmin, xmax, ymax, zmax]` order.

Document creation, opening, and reloading return `name`, `label`, and `objectCount`. Use the returned `name` as the `document` argument in later calls.

Structured object, parameter, sketch, and feature edits use MCP-owned transactions and refuse to nest inside a user's active transaction. A failed edit aborts and recomputes the restored document; rollback failures are reported separately. New volumetric geometry defaults to one solid unless `expected_solids` specifies otherwise. Existing valid dependent solid counts are preserved when their inputs change.

`create_objects` applies 1–32 entries in one transaction and one recompute. It returns a `nameMapping` because FreeCAD sanitizes and de-duplicates names. Sibling links inside the same batch are not supported because the actual names do not exist until creation finishes; use `edit_objects` after the batch.

`response_detail` accepts `"full"` (the default) or `"compact"` on object and feature mutations. Compact changes omit before-state property and geometry deltas and dependent counts, but keep the post-state validation report.

### Recovery checkpoints

Set `recovery_enabled` and an absolute `recovery_directory` to enable verified recovery copies. The directory is allowed automatically, so it does not have to appear in `allowed_roots`. The server checks the document is idle, writes a temporary FCStd copy, reopens it, compares it with the live document, and publishes it without overwriting an existing file before an expensive feature mutation starts.

Recovery covers `create_feature` kinds `fillet`, `chamfer`, `thickness`, `draft`, `linear_pattern`, `polar_pattern`, `mirrored`, `loft`, `pipe`, `helix`, `multi_transform`, and `scaled`. It also covers an `edit_feature` operation when the affected Body's feature chain contains an expensive feature.

The mutation is refused when the checkpoint cannot be verified. The failure reports `VALIDATION_FAILED` with `reason: checkpoint_failed` and `nextAction: inspect_recovery_directory`. Previous recovery copies are never pruned.

### Consent

Destructive or untrusted operations—opening an untrusted FCStd file, importing an untrusted STEP or STL file, saving over a different existing path, overwriting an export destination, closing a dirty or unsaved nonempty document, and reloading a dirty document—offer an MRTR elicitation round trip before taking effect.

A client that declares `elicitation.form` must answer the fixed `confirm` boolean form. The signed, single-use `requestState` binds the principal, method, arguments, and target fingerprint, and expires after 300 seconds or a server restart. Declining, cancelling, or tampering with the consent state aborts the operation without any effect. A client without form support falls back to 1.0 behavior and proceeds without the prompt; each bypass is noted in the Report view.

### Long-running operations and cancellation

`run_fem`, `run_script`, `export`, and `measure` can run as detached tasks under the `io.modelcontextprotocol/tasks` extension:

1. The tool call returns a task ID immediately.
2. `tasks/get` polls for the terminal result.
3. `tasks/cancel` requests cooperative cancellation.

The task store allows 32 active tasks and retains up to 1024 task records for one hour. The advertised polling interval is 500 ms. A client without the Tasks extension receives a blocking final result.

Cancellation is honest about its limits. A running CalculiX solver is never killed by a timeout or cancel request; the task result reports whether cancellation was requested, but the process keeps running until it finishes. Likewise, a `run_script` deadline that has already started is reported while the code continues running to completion. `tasks/update` acknowledges the task but does not grant input keys because all consent happens before task creation.

### GUI dispatch timeouts

FreeCAD GUI-thread operations cannot be force-cancelled safely. If a GUI-thread operation exceeds its timeout after starting, the server reports `GUI_DISPATCH_STUCK` and rejects later GUI operations immediately.

Use `discover_capabilities` to inspect dispatch health. Document queries run on the GUI thread alongside modelling operations. If health does not return to normal after the operation finishes, restart FreeCAD.

### `run_script` sessions

`run_script` executes on the GUI thread inside a namespace seeded with `FreeCAD`/`App` and `Gui` aliases. Variables survive between calls to the same `session_id` for the server's lifetime.

At most 32 sessions are kept. New sessions are refused rather than evicting live state. An exception in executed code returns captured stdout, stderr, and the traceback. Code execution still has FreeCAD's full privileges; it is not sandboxed.

## Agent skill

The repository ships an [agent skill](skills/freecad-mcp/SKILL.md). It teaches coding agents how to drive this server: the 26-tool contract, FreeCAD modeling patterns, geometry validation, FEM, and export. It complements the MCP connection: the agent still talks to `http://127.0.0.1:9876/mcp`, while the skill explains how to use the tools effectively.

[`npx skills`](https://github.com/vercel-labs/skills) is the official installer for the open agent skills ecosystem. It requires Node.js and supports Claude Code, Codex, Cursor, and more than 75 other agents.

```bash
# List the skill without installing
npx skills add bradsjm/freecad-embedded-mcp --list

# Install interactively (auto-detects installed agents; symlinks by default)
npx skills add bradsjm/freecad-embedded-mcp

# Install globally to specific agents, non-interactive
npx skills add bradsjm/freecad-embedded-mcp --skill freecad-mcp -g -a claude-code -a codex -y
```

Project installs land in `./<agent>/skills/` (for example `.claude/skills/`). Global installs use `~/<agent>/skills/` for all projects when `-g` is set. The CLI symlinks to one canonical copy by default; pass `--copy` when symlinks are not available.

To use the skill once without installing:

```bash
npx skills use bradsjm/freecad-embedded-mcp --skill freecad-mcp --agent claude-code
```

Manage an installed skill with:

```bash
npx skills list
npx skills update freecad-mcp
npx skills remove freecad-mcp
```

Start the server then connect the agent to the endpoint described above, and the skill supplies the operating procedure.

## Development

The Python code lives in `addon/FreeCADMCP/mcp_server` and `tests/`. Tests use stubs so they run without a FreeCAD installation.

```bash
uv sync
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall addon
uv run pytest -q
uv run pytest tests/test_settings.py -q
```

The project targets Python 3.11 and newer and has no runtime dependencies. The add-on runs with FreeCAD's bundled Python; the `uv` environment is for development checks only.

The CI workflow runs the lock check, Ruff lint, Ruff format check, add-on compile check, and the full headless pytest suite on Python 3.11. The tests use FreeCAD-shaped stubs and do not require a FreeCAD installation.

[`examples/cantilever_fem.py`](examples/cantilever_fem.py) is a dependency-free client example for this handshake and a full FEM run. Its checked-in `EXPECTED_TOOLS = 23` guard is stale against the current 25-tool default surface, so update that guard before running it against this revision.

[`examples/native_contract_probe.py`](examples/native_contract_probe.py) requires a live FreeCAD server and rewrites `tests/native_contract.json`. Run it without flags for the default verification, with `--dev` to resume after recorded crashes, or with `--sweep` to merge the full constraint-form sweep.

### What was carried over

- **GUI-thread dispatch:** A queue ferries every FreeCAD operation to the main thread. A Qt signal wakes the queue with a 500 ms heartbeat fallback. Ticks are skipped while a mouse button is held, so MCP work never interrupts 3D navigation.
- **Dispatch health:** A GUI operation that overruns its timeout fails fast and blocks later GUI operations until the dispatcher is healthy. `dispatch_health.py` moved across almost unchanged and is now reported through `discover_capabilities`.
- **Persistent script namespace:** `run_script` keeps a namespace with `FreeCAD`/`App` and `Gui` aliases for each `session_id`.
- **Add-on UX:** The “MCP Addon” workbench, the “FreeCAD MCP” toolbar and menu, status indicator, connection details, and settings in `freecad_mcp_settings.json` under FreeCAD's user app-data directory.
- **Remote connections and allowed IPs:** The CIDR allow-list carried over (`ip_parse.py`). Enabling remote access now requires a bearer token, and the allow-list defaults to “any host” instead of `127.0.0.1`.
- **Recovery copies:** Optional verified FCStd checkpoints protect expensive feature mutations before their transactions open.
- **Resources and subscriptions:** The server exposes a live documents resource and bounded SSE notification subscriptions for document updates and detached tasks.
- **CalculiX FEM, view screenshots, and the cantilever FEM example:** CalculiX FEM with a summary result, view screenshots with named orientations, and the cantilever FEM example all survive in their new tool forms.
- **Demos:** The demos below were produced with the original add-on and still show the workflow this rewrite serves.

### What was rewritten

| Area | Changes |
| --- | --- |
| **Architecture** | The PyPI proxy package (`src/freecad_mcp`, FastMCP over stdio) and the in-FreeCAD XML-RPC server are gone. One embedded server speaks MCP over Streamable HTTP (JSON-RPC + SSE) at `http://127.0.0.1:9876/mcp`. No pip or uvx install and no client config file are needed. |
| **Protocol** | XML-RPC with ad-hoc dictionaries became the MCP JSON-RPC wire protocol, version `2026-07-28`, with request-metadata headers, capability negotiation, and session-based support for the 2025 Streamable HTTP revisions. |
| **Tools** | Fifteen loosely typed tools became a 26-tool registered surface validated against JSON input and output schemas, with structured error codes and paginated results. `run_script` is opt-in through the `allow_scripts` setting. `execute_code` became `run_script`; `get_view` became `capture_view`; `get_rpc_status` became `discover_capabilities`; `inspect_documents` was added for live document inventory; `create_objects` adds atomic 1–32 object creation. |
| **Security** | The IP allow-list alone became two explicit modes: local (loopback bind, Host/Origin checks, no token) and remote (bind to all interfaces, mandatory bearer token, optional CIDR allow-list), plus `allowed_roots` path containment for file-touching tools and a dedicated absolute `recovery_directory` for recovery copies. |
| **Document safety** | Unvalidated success/error dictionaries became MCP-owned transactions with prevalidation, rollback, dependent-object checks, and solid-count baselines. |
| **Long-running work** | Blocking calls with client-side timeouts became detached tasks under the `io.modelcontextprotocol/tasks` extension, with polling and cooperative cancellation. |
| **Consent** | Operations that touch untrusted or existing data—opening or importing files, saving over paths, exporting over existing files, and closing or reloading dirty documents—became explicit elicitation round trips instead of implicit effects. |
| **FEM** | The legacy `SolverCcxTools` auto-create became the modern `Fem::SolverCalculiX` pipeline, which returns a VTK result summary. Legacy solvers are refused with an explicit error. |
| **Testing and packaging** | The original proxy and RPC tests were replaced with a larger headless suite that runs without a FreeCAD install. CI dropped the MCP SDK compatibility job and now runs the lock check, ruff lint and format gates, a compile check of the add-on, and the pytest suite on Python 3.11. No wheel is built or published; the add-on ships from this repository through the Addon Manager. |
