# Python scripting with `run_script`

Use `run_script` only for a named operation that no structured tool covers. `run_script` is opt-in: it is registered but hidden unless `allow_scripts: true` is saved in `freecad_mcp_settings.json` and the server restarted. A disabled tool answers `tools/call` with `METHOD_NOT_FOUND` and never reaches schema validation, consent, or the GUI dispatch. Discovery reports `capabilities.scriptingEnabled`. Read [the tool contract](mcp-tools.md) for exact schemas.

## Contents

- [When to use it](#when-to-use-it)
- [Session and call contract](#session-and-call-contract)
- [Deadlines and the GUI thread](#deadlines-and-the-gui-thread)
- [Safe script shape](#safe-script-shape)
- [Validate after the script](#validate-after-the-script)
- [Safety boundary](#safety-boundary)
- [Sources](#sources)

## When to use it

Use `run_script` for:

- `FreeCADGui` calls and the live selection.
- Imports of formats `import_model` does not support.
- Geometry or mesh operations the structured tools do not cover. See [Part geometry and topology](part-topsolids.md) and [Export, save, and mesh work](export-print.md).
- Property assignments the property mapper cannot express. `edit_parameters` covers dynamic properties and expressions first.

Use a structured tool, not `run_script`, whenever one covers the operation. Call `tools/list` for the live schemas.

## Session and call contract

Arguments: `code`, optional `session_id` (default `"default"`), optional `timeout_s`.

- Variables persist per `session_id` for the server's lifetime, in a namespace pre-seeded with `FreeCAD`/`App` and `Gui`.
- At most 32 sessions are kept. A new session is refused instead of evicting live state.
- `stdout`, `stderr`, and the traceback are captured even when the code raises.
- The tool is refused with `SERVER_BUSY` while a FEM solve is active.
- A client that declares the Tasks extension may detach the call. Poll with `tasks/get` until terminal. Cancellation is cooperative and does not stop running code.

## Deadlines and the GUI thread

`run_script` executes on FreeCAD's main GUI thread through one shared dispatch queue, so a long script delays every later GUI operation. Execution cannot be preempted after it starts; `timeout_s` is a cooperative server deadline with a range of 1–3600 s and a default of 90 s, and the tool result says so truthfully.

- Keep each script inside the deadline and split long work into stages.
- A GUI operation that exceeds its deadline leaves the server reporting `GUI_DISPATCH_STUCK`. Read [troubleshooting](troubleshooting.md) before you retry.

## Safe script shape

- Make the script short, deterministic, and idempotent. Get or create the document instead of assuming an empty session.
- Keep model state in document objects. A session namespace is not a place for hidden state.
- Inspect an existing object before you overwrite it. Never replace a parametric feature with a raw shape silently.
- Print one compact result and assert the invariants the script depends on.
- Import only the modules the script uses.

```python
import FreeCAD as App

doc = App.ActiveDocument or App.newDocument("Scratch")
obj = doc.getObject("Final")
assert obj is not None, "Final is missing"

doc.recompute()
print({"name": obj.Name, "valid": obj.Shape.isValid(), "solids": len(obj.Shape.Solids)})
```

## Validate after the script

Script output is not proof. Re-run the structured checks after the script and report their result:

- `inspect_objects` for internal names, state, and bounds.
- `validate_geometry` for shape validity, solid count, volume, and bounds.
- `measure` for fit decisions.
- The written-file readback for delivered files. See [Export, save, and mesh work](export-print.md).

See [validation](validation.md) for the full gate.

## Safety boundary

`run_script` is arbitrary Python inside the FreeCAD process with the user's privileges. It is deliberately not sandboxed and is not restricted by `allowed_roots`. Never place credentials or untrusted code in it, and never print the bearer token.

It reaches the native bindings directly and is not covered by the `edit_sketch` guard: a malformed native constructor call, such as an unsupported `Sketcher.Constraint` argument form, can raise an unhandled C++ exception that terminates the whole FreeCAD process.

## Sources

- [FreeCAD Scripting Basics](https://wiki.freecad.org/FreeCAD_Scripting_Basics)
- [Scripting and macros](https://wiki.freecad.org/Scripting_and_macros)
