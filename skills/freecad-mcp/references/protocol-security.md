# Protocol and security

Read this file for connection, authentication, path access, consent, limits, tasks, resources, and GUI dispatch behavior. Do not load it for ordinary modeling calls.

## Contents

- [Connection](#connection)
- [Security modes](#security-modes)
- [Settings and path access](#settings-and-path-access)
- [Consent](#consent)
- [Tasks and cancellation](#tasks-and-cancellation)
- [Resources and subscriptions](#resources-and-subscriptions)
- [Limits and deadlines](#limits-and-deadlines)
- [GUI dispatch health](#gui-dispatch-health)

## Connection

Connect to the embedded Streamable HTTP endpoint at `http://127.0.0.1:9876/mcp` by default. The server runs inside FreeCAD's GUI process.

Use protocol version `2026-07-28` for the current contract. The legacy adapter accepts `2025-03-26`, `2025-06-18`, and `2025-11-25`.

HTTP worker threads never touch FreeCAD. The server sends every document and GUI operation to FreeCAD's GUI thread.

## Security modes

Use local mode by default. It binds loopback, accepts loopback peers, checks Host and Origin, and needs no token.

Use network mode only when the task requires it. It binds `0.0.0.0` and requires the bearer token on every request. An empty `allowed_ips` list accepts any peer with the token.

The server does not provide TLS or CORS. Never forward, proxy, or tunnel the endpoint to an untrusted network.

Treat the token as full local code-execution authority. Never log or print it. `run_script` is not sandboxed.

## Settings and path access

The settings file accepts:

- `port`
- `token`
- `auto_start`
- `remote_enabled`
- `allowed_ips`
- `allowed_roots`
- `recovery_enabled`
- `recovery_directory`
- `allow_scripts`

Invalid settings fail closed. Network mode without a token creates and stores one before startup.

Use absolute paths under `allowed_roots` for document, import, export, and FEM file operations. The absolute `recovery_directory` is allowed automatically. `allowed_roots` does not restrict `run_script`.

Settings changes take effect after a server restart.

## Consent

Expect consent for these calls:

- `open_document` with untrusted input, which is the default.
- Every `import_model` call.
- `save_document` over a different existing file.
- `close_document` for a dirty or unsaved nonempty document.
- `reload_document` for a dirty document.
- `export` over an existing destination.

A form-capable client must answer the fixed confirmation form. The signed `requestState` is single-use, target-bound, and valid for 300 seconds.

Treat decline, cancel, expiry, and tampering as no-effect failures. A client without form support proceeds under the legacy fallback.

## Tasks and cancellation

The server can detach `measure`, `export`, `run_fem`, and `run_script` when the client declares Tasks support.

1. Read the returned task ID.
2. Poll `tasks/get` at the advertised interval.
3. Treat only a terminal task result as completion.
4. Use `tasks/cancel` to request cooperative cancellation.

Use `tasks/update` only to acknowledge a task. Consent completes before task creation, so this method does not supply consent input.

Cancellation does not kill a running CalculiX solve. Cancellation and deadline expiry do not stop a `run_script` call that already started.

The store allows 32 active tasks. It retains at most 1024 records for one hour and advertises a 500 ms poll interval.

## Resources and subscriptions

Use `resources/list` to discover `freecad://documents`. Use `resources/read` to get the live document inventory.

Use `subscriptions/listen` for acknowledged events, document-resource changes, and same-principal task updates. The server does not honor list-changed boolean filters.

The server allows 32 active subscriptions. Each queue allows 256 events and 4 MiB. One event can be at most 1 MiB.

## Limits and deadlines

The transport allows 64 HTTP connections and 32 active SSE streams. Request and response bodies can be at most 8 MiB.

The default tool deadline is 60 seconds. `measure` and `export` use 600 seconds. `run_fem` and `run_script` accept `timeout_s` from 1 through 3600 seconds.

The server allows 32 concurrent operations across blocking and detached calls. Do not use concurrency for dependent document mutations.

## GUI dispatch health

Call `discover_capabilities` without `refresh` to inspect health without using the GUI thread.

If `gui.state` is `busy`, wait before dependent GUI calls. If it is `stuck`, stop GUI-thread calls.

If a started GUI operation exceeds its deadline, the server reports `GUI_DISPATCH_STUCK` and rejects later GUI work.

Do not force-cancel a stuck GUI operation. Wait for it to finish. Restart FreeCAD only if health does not return.
