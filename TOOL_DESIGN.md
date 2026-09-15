# Tool Design

This document records why the FreeCAD MCP server exposes the tools it does, in the shapes it does. It is the design rationale for translating the FreeCAD Python API into a tool set for language models. Use it when you add a tool, change a schema, or wonder why a refusal exists.

The live `tools/list` response is the source of truth for the enabled tool surface. `run_script` appears there only when the `allow_scripts` setting is enabled. The companion documents describe behavior: `README.md` for transport and install, `skills/freecad-mcp/SKILL.md` for the agent-facing contract, and `AGENTS.md` for code conventions. This document describes intent.

## The client model

Every decision below follows from six assumptions about the consumer. The consumer is a language model, not a person and not a GUI client.

1. It has no memory between calls except its own context window. Anything a result does not state is unknown one turn later.
2. Its context is finite and lossy. A long result costs more than tokens; it crowds out design reasoning.
3. It cannot watch the screen or share a pointer. It observes the application only through tool results, and each observation is a round trip.
4. It will mistype names, keep stale facts, and trust results literally. Silence reads as success.
5. It is capable, not careful. It will attempt a plausible but wrong action unless the schema, the validation, or the refusal stops it.
6. It arrives pre-trained. Its weights already hold CadQuery and its docs, FreeCAD tutorials, machining handbooks, and gear design texts. Terms it knows are free to send and right on the first attempt; invented terms cost a schema description on every page and still mistrigger.

These assumptions invert the usual API design priorities. A client application wants flexibility and raw access. A human wants a GUI. A model wants closed shapes, self-describing results, early refusals, and vocabulary it already holds. The tool set is therefore an interface with opinions, not a thin binding over `FreeCAD`.

## Principles

### 1. Verbs over wrappers

The tool set groups by model workflow phase, not by native class hierarchy. FreeCAD organizes itself around document, object, feature, sketch, and solver classes. The tools organize around what a model is trying to do: discover, resolve documents, resolve objects, mutate, prove geometry, deliver.

Each tool is one verb on one domain: `create_object`, `edit_object`, `delete_object`; `create_feature`, `edit_feature`; `inspect_documents`, `inspect_objects`, `inspect_topology`. The model composes verbs into workflows. It never assembles constructor calls.

The recommended call order mirrors the client workflow: discover, documents, objects, mutations, validation, parameters, sketches, features, delivery, and script escape. Use `tools/list` for the live schemas; do not infer the workflow from list ordering.

### 2. Priors are leverage

The model arrives pre-trained, and it naturally reaches for what it already knows. Its weights hold the CadQuery documentation and source, FreeCAD tutorials and forum threads, machining handbooks, gear design texts, and thousands of Python CAD scripts. The design works with that reflex, not against it: borrow the terms and shapes the corpus already teaches, because known vocabulary is right on the first attempt and costs nothing to send, while invented vocabulary must be re-taught in every schema and still mistriggers under pressure. Every borrowed term removes a round trip of learning and one more way to be wrong.

The flagship adoption is the topology selector grammar. Instead of minting a filter language, the tools implement CadQuery's string selector DSL, specified under *Reads carry proofs of freshness*, down to reusing CadQuery's numerical tolerance, and `discover_capabilities` announces the dialect as `cadquery-string-v1`. A model that once wrote `edges("%CIRCLE")` in CadQuery writes the same filter here on the first try.

The borrowing is deliberate, not wholesale:

- **Stable vocabulary is adopted.** Machining terms pair exactly as the handbooks pair them: `counterbore` takes `counterbore_diameter` and `counterbore_depth`; `countersink` takes `countersink_diameter` and `countersink_angle`; `counterdrill` takes all three; a countersink angle validates over (0, 180] degrees. Gears take integer `teeth`, `module` in millimetres, and `pressure_angle` over 14.5–25 degrees — the classic and standard tooth systems — so handbook knowledge passes validation unchanged. Feature kinds keep the PartDesign names the corpus teaches (`pad`, `pocket`, `revolve`) while typed parameters translate them into portable terms (`extent`, `diameter`). Named views resolve exactly as CadQuery's do — deliberately not the way FreeCAD camera names point.
- **Unstable vocabulary is refused, even though the model will reach for it.** Native `Face7`/`EdgeN` labels are in the corpus too, but their referent shifts under upstream edits, so prior fluency there is a trap. Indices are refused as durable input in favor of whole objects, signed references, and declarative queries.
- **Divergences are stated where the term is used, never discovered by failing.** The grammar says extrema rank centers of mass rather than bounding boxes and that a cylinder's axis is not its face normal; CadQuery's bare `IndexError` past the last cluster becomes `selector_index_out_of_range` with the index and group count; CadQuery's silent drop of unreadable geometry becomes `selector_geometry_unavailable` instead of a false zero-match.

### 3. Liberal in what you accept, precise in what you emit

Postel's law holds at the tool boundary. A model that means the PartDesign pad will send `extrude` (the word CadQuery, Fusion 360, and SolidWorks all teach), `Extrude`, or `extruded boss`, and an error in return buys nothing, because the meaning was unambiguous. Where a value admits exactly one interpretation, the tools accept it: cross-tool synonyms, abbreviations (`stp` for `step`), case variants (`front` for `Front`), and separator or style variants (`thru all`, `thruAll`, and `through_all` are one value). Where a value is ambiguous or unknown, it passes through untouched and the closed schema refuses it: the error names the parameter path and the expected values, and the caller's untouched spelling is what was checked.

Two rules keep the liberality honest:

- **Normalization is a table, not a guess.** Each accepted surface has a closed alias table built from the same constants as the schema enum it serves, so an alias can never normalize to a value the schema refuses. A value must match exactly one member after folding to a comparison key; there is no fuzzy matching, no nearest-match, and no inference between meanings. Table construction refuses an alias aimed at a non-member and two members that fold together, so vocabulary drift fails at import, not at call time.
- **Normalization runs before validation, consent, and storage.** The canonicalized request is what the schema validates, what the consent fingerprint signs, what a detached task stores, and what the handler executes, so a retry that spells a value differently binds to the same target and every result reports the same canonical value.

The result side is strictly canonical: `kind` comes back `pad`, never `extrude`; `view_name` comes back `Front`, never `front`. Downstream arguments, signed references, and comparisons are built on one spelling. The boundary is deliberate: the CadQuery selector grammar keeps CadQuery's own case rules instead of accepting folds, because that grammar is a ported prior (see *Priors are leverage*), not server vocabulary — liberal acceptance applies to parameter values, never to a faithfully reused language.

### 4. Results are contracts

Every tool has an `outputSchema`, checked at registration and again per call. A result that violates its schema is an infrastructure error (`-32603`), never a quietly odd payload. A model can build `nextTool` recovery logic once and trust it for every tool.

All output floats are finite. JSON contains no `NaN` or `Infinity`. A model cannot represent or reason about non-finite numbers, so the server never produces them.

### 5. Reads carry proofs of freshness

A model that read a document three turns ago may still believe what it read. The FreeCAD API would silently apply an edit against changed state. The tools refuse.

Every mutation-sensitive read and write carries a document `generation` stamp. `edit_object`, `edit_objects`, `edit_sketch`, and `edit_feature` accept `expected_generation`; a mismatch fails with `VALIDATION_FAILED`, `reason: stale_generation`, `expectedGeneration`, `actualGeneration`, and the matching inspector (`inspect_objects` or `inspect_sketch`) as `nextTool` before any transaction opens.

Topology selection is one closed vocabulary shared by `measure`, `inspect_topology`, `capture_view`, feature reference positions, and link-valued properties. A target is exactly one of three forms, and the form is the meaning:

- **Whole object** — `{"object": "Pad"}`. Identity: the entire shape, and the only form accepted where a whole object is required (Body, profile, axis, analysis, export list).
- **Signed reference** — `{"object": "Pad", "subelement": "<token>"}`. A fresh subshape identity minted by an inspection result; the opaque HMAC token binds document identity, generation, object name, role, and index. A stale or tampered token is rejected with a named reason (`stale_generation`, `stale_cursor`, `malformed_cursor`). An empty `subelement` string is refused rather than read as the whole object — the absence of the key is what names the whole object.
- **Declarative query** — `{"object": "Pad", "query": [{"role": "face", "selector": ">Z"}, {"role": "edge", "selector": "%CIRCLE"}], "expected_generation": 12}`. A selection recipe evaluated at call time, so "the top perimeter" survives a regeneration without a re-inspection round trip; `expected_generation` refuses a document that changed since the target was written.

The grammar is CadQuery's string selector DSL, bounded to 1–256 characters and 1–3 steps. `%TYPE` filters by analytic type; `+D`/`-D`/`|D`/`#D` match planar-face normals and linear-edge tangents against a direction; `>D`/`<D`/`>>D`/`<<D` rank centers of mass projected on the direction, with an optional `[n]` indexing the tied clusters; `and`/`or`/`exc`/`not` combine sets within one candidate universe (`not` binds weakest, so `not >X and >Y` is `not (>X and >Y)`). Named views keep their CadQuery meanings (`front` is `>Z`, `top` is `>Y`), not FreeCAD camera names. Extrema rank centers, never bounding boxes, and a cylinder's axis is not its face normal.

Consumers declare their cardinality. Singleton readers such as `measure` and `capture_view` accept a query only when it resolves to exactly one subshape and refuse otherwise (`selection_empty`, `selection_ambiguous`); set consumers such as a fillet's subelement list expand a query within a bounded budget. Empty matches are data for readers and refusals for mutations, and unreadable geometry evidence refuses with `selector_geometry_unavailable` rather than reporting a false empty match.

Pagination cursors are signed the same way and bound to identity, generation, object, role, page size, and the canonical target (the `queryHash` of a query target). A cursor from an older generation, or one reused after its selector changed, answers `stale_cursor` with `nextTool: inspect_topology`, which is exactly how a model recovers: re-inspect, re-select.

### 6. Writes predeclare their intent

A model usually knows what success looks like: one solid of roughly these dimensions. The tools let it say so before the write happens.

`create_object`, `create_objects`, `edit_object`, `edit_objects`, `create_feature`, `edit_feature`, and `validate_geometry` accept `expected_solids` and `expected_bounds` with `bounds_tolerance`. The server recomputes, compares, and refuses on deviation, reporting the exact mismatch and `operationState`. **Without expectations, a wrong boolean or a failed cut would return success-shaped output with the wrong geometry, and the model would build the next stage on a false premise.**

Declarative checks in `validate_geometry` extend the same predeclared intent to acceptance: `volume_range`, `clearance_min`, and `interference_max` state the condition in the request and answer with per-check `pass`/`fail`/`indeterminate` rows. `clearance_min` passes only when the measured minimum distance reaches the requested margin AND the common volume is zero — a positive distance alone does not prove separation and a zero common volume does not prove clearance — and a non-volumetric or unreadable target reports `indeterminate` instead of passing.

### 7. Refuse before harm, explain after

Validation runs before any native call. A batch entry that names an unknown property fails before the first entry is written. A constraint form `(type, argument-count)` with no recorded native acceptance is refused before execution, because a malformed `Sketcher.Constraint` constructor can raise an unhandled C++ exception that terminates the whole FreeCAD process. A dependent closure over 256 objects is refused before any effect. Recovery checkpoints are written and verified before the mutation transaction opens.

Refusals carry the reason a model needs to proceed: `details.reason` as a stable token (`stale_generation`, `unrecorded_constraint_shape`, `checkpoint_failed`), plus the evidence (`acceptedArgumentCounts`, `expectedGeneration`, `actualGeneration`, `path`) and often the recovery step (`nextTool: inspect_sketch`).

### 8. Failures are data

Application failures are complete tool results with `isError: true` and a structured `{code, message, details}` payload. Only protocol-level violations become JSON-RPC errors. A model never parses a traceback; it reads `code`, `reason`, and `details`.

The codes are a closed set: `DOCUMENT_NOT_FOUND`, `OBJECT_NOT_FOUND`, `VALIDATION_FAILED`, `GUI_DISPATCH_FAILED`, `GUI_DISPATCH_STUCK`, `CONSENT_DENIED`, `PATH_NOT_ALLOWED`, `UNSUPPORTED_VIEW`, `SOLVER_FAILED`, `SERVER_BUSY`.

**Two fields make errors recoverable rather than terminal**:

- `details.nextTool` is always the name of a tool this server exposes. The model can call it directly.
- `details.nextAction` is a plain instruction such as `retry_from_original_state` or `inspect_recovery_directory`. It is deliberately not a tool name, so the model cannot confuse instruction with invocation.

`details.suggestions` lists close matches when a name or type was rejected, which **turns a typo into one retry instead of a re-inspection**.

### 9. Context is a budget

**Every payload is bounded by design**, because an unbounded result is a context-window hazard.

| Mechanism | Bound | Why |
|---|---|---|
| `inspect_objects` selection | 64 objects | One call answers one question. |
| Batch entries (`create_objects`, `edit_objects`) | 1–32 | One transaction per coherent stage. |
| `validate_geometry` objects | 100 | A report per object multiplies fast. |
| `inspect_topology` page | 50 default, 100 max | Faces and edges number in the hundreds. |
| Feature `originals` | 1–8 | Patterns beyond this are a design smell. |
| `subshape_binder` references | 1–16 | Binders beyond this are fragile. |
| Document and object detail | `compact` default | Full detail is opt-in per call. |
| Property listings | `property_filter`, `property_offset`, `property_limit` | Spreadsheets have hundreds of properties. |

Discovery carries caching hints: `tools/list` and the `discover_capabilities` tool are `ttlMs: 0, cacheScope: private`, while `server/discover` without `refresh` is `ttlMs: 3600000, public`. Clients that cache can skip a round trip; clients that cannot, lose nothing.

`response_detail: "compact"` keeps post-state reports and drops before-state deltas. A model that wants the delta asks for it; most edits do not need to re-read what was already known.

### 10. Side effects are visible and reversible

Mutations run through one gate (`object_validation.mutation`): refuse FEM-locked documents, refuse nesting inside a user transaction, open one transaction, recompute, validate targets and dependents against solid-count baselines, and report the outcome.

The outcome vocabulary is honest about uncertainty: `rolled_back` means the transaction aborted cleanly; `rollback_failed` means the rollback itself failed; `may_have_changed` means the model must re-inspect before trusting anything. **A model that treats every failure as "nothing happened" will corrupt its own plan**. The vocabulary prevents that.

Batches raise the stakes and the payoff together. `create_objects` applies 1–32 entries in one transaction and returns a requested-to-actual `nameMapping`, because FreeCAD sanitizes and de-duplicates names (`Box` may become `Box001`) and a link to a guessed name is a latent failure. `edit_objects` rolls the whole batch when any entry is invalid. Atomicity converts "thirty calls, maybe consistent" into "one call, definitely consistent".

Expensive feature operations first write a verified recovery copy (`checkpoint`) into the recovery directory, so the worst outcome of a bad edit is a known-good file one step back. The checkpoint failure refuses the mutation and removes only the staging file it created.

### 11. Danger is explicit and consented

Some operations touch the world outside the document graph: reading an untrusted file, overwriting a file, discarding unsaved work. The tools make these operations possible but never silent when the client supports the consent form contract.

- Consent targets: untrusted document open, STEP/STL import, overwrite-style save, dirty or unsaved close, dirty reload, export overwrite. Consent is a round trip: the server returns `InputRequired`, the client answers, and the retry carries an HMAC-signed single-use `requestState` that binds principal, method, arguments, and target fingerprints. A model cannot obtain consent for target A and spend it on target B.
- Clients without `elicitation.form` use the documented 1.0 compatibility fallback and proceed without the prompt. This is an intentional capability downgrade, not a second consent protocol; the host records each bypass in the Report view.
- `allowed_roots` contains every file-touching tool and the FEM working directory. A path outside containment fails in preflight, before any file effect. `run_script` is the documented exception: it is full local code execution with the user's privileges, which is why it is opt-in, hidden by default, and answered with `METHOD_NOT_FOUND` before schema validation when disabled.
- Discovery reports `scriptingEnabled` and `recoveryEnabled`, so a model reads the active policy instead of probing for it.

### 12. One escape hatch, deliberately gated

`run_script` exists because a closed tool set cannot anticipate every CAD operation. It is the only unstructured surface, and the design quarantines it:

- Off by default; visible in `tools/list` only when enabled.
- A persistent per-session namespace, capped at 32 sessions; a 33rd is refused, never evicted, because silent eviction would destroy a model's working state.
- stdout, stderr, and traceback are captured even when code raises exceptions, so failure is still data.
- The deadline is cooperative and the result says so truthfully: execution cannot be preempted, and the tool does not pretend otherwise.
- Refused with `SERVER_BUSY` while a FEM solve is active.

The skill text repeats the rule the design enforces: use structured tools first, script only a named unsupported operation.

## Parameter shape rules

The schemas use a finite JSON-Schema subset: `additionalProperties: false`, `$ref` only into local `$defs`, no `oneOf`/`allOf`/conditionals/patterns/remote references. Each rule serves the client model.

- **Closed objects.** `additionalProperties: false` turns a typo into a named validation error at `-32602` with the exact path, instead of a silently ignored key and a mysterious default.
- **No free-form unions.** Unions are allowed only inside `$defs` with bounded per-branch failure reasons, so a mismatch names the offending field instead of returning "does not match any branch".
- **Typed semantic parameters.** `create_feature` maps a `kind` plus typed `parameters` onto native properties with closed per-kind schemas: `pad` takes `extent`, `length`, `symmetric`, `reversed`; `hole` takes `diameter`, `depth_type`, and a `cut` form whose sub-parameters are required together, with counterdrill using `countersink_diameter`, `counterbore_depth`, and `countersink_angle`. An incomplete request fails wire validation before a transaction opens. The model states design intent; the tool performs native bookkeeping. The names are machining and gear vocabulary rather than FreeCAD property names, so the request a model writes from handbook knowledge is already valid input (see *Priors are leverage*).
- **Liberal inputs normalize at one seam.** Enum-valued parameters may declare an argument normalizer; it runs once at dispatch, before schema validation, consent, and task storage. Tables are built through `input_aliases.build_table` from the same constants as the schema enums, so an alias aimed at a refused value fails at import (see *Liberal in what you accept, precise in what you emit*).
- **Topology targets are one closed union.** Every geometric position — `measure` `a`/`b`, `inspect_topology` `target`, `capture_view` `focus`/`a`/`b`, feature subelement lists, link-valued properties — accepts the same three-form `topologyTarget` union, kept under root `$defs`. A query is 1–3 steps, each a required `role` (`face`/`edge`) plus an optional selector string and optional `radius`/`axis` predicates (`axis` matches analytic cylinder, cone, and torus axes and is deliberately separate from the normal/tangent operators). Chaining steps narrows the candidate set; a `face` step followed by an `edge` step expands to the selected faces' edges, and the reverse is refused because there is no implicit ancestor query.
- **Raw properties as a bounded exception.** `properties` (raw assignment) exists only for the five kinds where the typed surface was original (`sketch`, `pad`, `pocket`, `hole`, `datum_plane`), and `properties` and `parameters` are mutually exclusive. One path per intent; no precedence rules to discover.
- **Composites expand.** `rectangle` becomes 4 segments plus 8 constraints; `regularPolygon` becomes its vertex and constraint set. The tool does the bookkeeping a model would get wrong, and returns the committed ids (`addedGeometryIds`) so later constraint arguments can reference `{"geometry": "<id>"}` instead of guessing indices.
- **Identifiers are internal names.** Every mutation returns the actual sanitized name, and every reference takes that name. `label` is display text. The distinction is enforced by documentation and by returning both, so a model never has to guess which one a parameter wants.
- **Units are explicit and minimal.** Lengths and angles are plain numbers in millimetres and degrees. Quantity strings appear only where the native API demands them (sketch datums, FEM material maps). Results carry a `units` block (`{"length": "mm", "volume": "mm3", "tolerance": "mm"}`) so the model never converts from memory.
- **Bounds are positional arrays.** `[xmin, ymin, zmin, xmax, ymax, zmax]` in document space — the bounding-box shape every geometry toolkit uses: compact, orderable, comparable, and the same shape in requests (`expected_bounds`), results, and selectors.
- **Enums are closed at registration.** Every schema is checked when the server starts. A native enumeration change fails loudly at startup, not quietly at call time.
- **Counts have ranges.** `teeth` is 8–80; `module` is 0.1–10 mm with pitch diameter capped at 200 mm; pattern `count` is 2–32; `helix` angle is −80–80 degrees. The ranges encode plausible design space, not native minima, and keep degenerate geometry out of the recompute.

## Result shape rules

Results follow one template: identify the state, report the effect, provide the next step.

- **Identity first.** Results that depend on document state carry `document` and `generation`. A model comparing two results can detect that they describe different worlds without an extra call.
- **Geometry as evidence.** Validation and mutation results carry `solid_count`, `volume`, `bounds`, validity flags, and `all_valid` summaries. Shapeless objects are valid results with `solid_count: 0` or `null`, so "no geometry yet" is data, not an error.
- **Naming as fact.** `nameMapping` (requested → actual), `addedGeometryIds`, and the mutated object `Name` in `applied` entries all report what the server actually did, because name transformation is where intent and effect diverge.
- **Deltas when asked.** `edit_object` returns before/after deltas; `response_detail: "compact"` keeps post-state and drops before-state. The default is the cheaper read.
- **Selections are receipted.** A mutation that consumed a query returns `resolvedSelections`: the input parameter path, document, generation, and canonical signed references, capped at 64 receipts and 64 referenced subshapes per operation and stamped with the selection-time generation.
- **Verification by readback.** `export` reopens the written file and compares it with the source before reporting success. `saveCopy`-based recovery checkpoints are verified the same way. Success claims are observations, never inferences.
- **Image publication is single-copy.** `capture_view` validates its raw PNG payload, publishes the bytes once in the image content block, and keeps only metadata in structuredContent.
- **Long operations detach.** Task-eligible tools (`run_script`, `run_fem`, `export`, `measure`) return a task id when the client declares the tasks capability. The model polls `tasks/get` and can request `tasks/cancel`. Cancellation is cooperative and documented as such: a running CalculiX solve is never killed, and the results say what actually stopped.
- **Health is queryable.** `discover_capabilities` reports `gui.state` and dispatch health, and is GUI-independent without `refresh`, so a model can always ask "is the server usable" even when every other tool would block.

## How the tools compose

The tool set assumes one loop, and every tool is a step in it:

1. `discover_capabilities` — read versions, exporter/FEM availability, `gui.state`, active policy.
2. `inspect_documents` / `new_document` / `open_document` — name the world.
3. `inspect_objects` — read compact rows: identity, state, bounds, validity, links.
4. `inspect_topology` — when a specific face or edge matters; collect signed references.
5. Mutate one stage: `create_object` / `create_objects` / `create_feature` / `edit_sketch` / `edit_feature` / `edit_parameters`, with expectations attached.
6. Prove the stage: `validate_geometry`, then `measure` for fit decisions.
7. Repeat 5–6 per dependency stage; `export` and `capture_view` when the design is done.

The composition rules that matter:

- **One stage, one call.** A dependency stage fits in one transaction-sized tool call. Independent entries batch (`create_objects`); anything whose name a later entry needs goes in its own call after `nameMapping` comes back.
- **Read before write; write then prove.** The loop pairs every mutation with a validation read. A positive distance alone does not prove separation and a zero common volume does not prove clearance, so fit decisions pair `measure(mode="distance")` with `measure(mode="interference")`. Two cheap reads replace one wrong assembly.
- **Failure follows the arrows.** `nextTool` values form recovery edges in the graph: `stale_generation` points back to the inspector, unsupported `edit_feature` kinds point to `edit_object`, checkpoint failures point to `inspect_recovery_directory` (an instruction, not a tool). A model can follow `details` without knowing the map in advance.
- **After an uncertain timeout, re-read.** A timed-out GUI operation may still complete; a retried mutation could duplicate. The failure guidance says `retry_from_original_state`, and the generation stamps make "original state" checkable.
- **After a stuck dispatch, stop.** `GUI_DISPATCH_STUCK` poisons later GUI operations until health returns; only `discover_capabilities` stays callable. The tools do not queue more work into a wedged dispatcher and report it as progress.

## What is deliberately not surfaced

Omissions are design statements. Each one removes a failure mode the client model would hit.

- **No raw API surface.** There is no "call arbitrary FreeCAD method" tool. `run_script` is the only escape, and it is opt-in. Every structured tool prevalidates; a raw surface would bypass all of it.
- **No numeric topology selectors.** Native `Face7`/`EdgeN` labels appear only as descriptive diagnostics inside results. Input is a whole object, a signed reference, or a declarative query — never an index — because native indices shift under upstream edits and a model cannot see it happen. The corpus teaches these labels too; the refusal is deliberate prior-resistance where prior fluency would be a trap (see *Priors are leverage*).
- **No guessed attachments.** A `support` requires an explicit `MapMode`. The tool never invents an attachment mode, because a wrong default attaches silently and fails later, far from the cause.
- **No legacy or auto-rescued FEM paths.** Only the modern `Fem::SolverCalculiX` pipeline; legacy solvers produce explicit errors; missing CalculiX produces an actionable error, never an auto-install. Installing software is outside a CAD tool's authority.
- **No GUI emulation.** No selection events, no viewport control beyond `capture_view`, no dialog automation. The model's "eyes" are inspection results and one PNG composed per inspection intent with a self-describing panel manifest.
- **No silent compatibility.** Protocol eras differ on purpose (unknown tools answer 404 on the modern era, 200 on the legacy era) because clients of each era key on different signals. Nothing falls back quietly.
- **No unbounded file access.** `allowed_roots` contains file effects in preflight. The recovery directory is implicitly allowed because checkpoints must work without configuration ceremony; it is an absolute path, not a widening of the root.

## Extending the tool set

Apply these tests before adding a tool, a parameter, or a behavior. A capability that fails a test stays out or changes shape.

1. **Model need, not API surface.** Add it because a model needs it to reach a goal the current set cannot, not because the API exposes it.
2. **One verb, one domain.** A new operation joins an existing tool as a mode or parameter when it shares the verb and the risk. It becomes a new tool when its lifecycle, ownership, or blast radius differs (`run_fem` is not a parameter of `export`).
3. **Reuse a prior or name the divergence.** Prefer the vocabulary and shapes the corpus already teaches — CadQuery selectors, machining pairings, gear terms, PartDesign kind names — over invented ones; they are right on the first attempt. If the familiar term must mean something different here, say so at the point of use (`front` is `>Z`; extrema rank centers of mass). Never adopt a prior whose referent is unstable.
4. **Accept the synonym, emit the canonical.** Every enum-valued input surface gets an alias table for unambiguous cross-tool terms, abbreviations, and case or separator variants; results and errors carry only canonical values. The table is closed and import-checked — liberal acceptance never means fuzzy matching, and an ambiguous value passes through and is refused by the closed schema.
5. **Closed schema or no schema.** If the parameters cannot form a closed, registered schema, the capability belongs in `run_script`, where the lack of structure is at least explicit.
6. **Failures must be nameable.** Every refusal needs a stable `code`, a `reason` token, evidence in `details`, and where possible a `nextTool`. If you cannot name the failure, you cannot prevalidate the input.
7. **Writes carry expectations.** Any tool that changes geometry accepts `expected_solids` / `expected_bounds` / `expected_generation` as applicable, and reports `operationState` honestly.
8. **Results self-identify.** New results carry `document` and `generation` where state matters, report actual names, and validate against an `outputSchema`.
9. **Bound the payload.** Choose a limit from the design space, not the native maximum, and add pagination or a `detail` switch before the payload can crowd a context window.
10. **Make danger explicit.** New file effects join the consent targets and the `allowed_roots` preflight. New expensive operations join the checkpoint list. Nothing risky ships silent when the client supports the consent contract; documented legacy fallback remains explicit.

The one-sentence version: surface the model's intent as closed parameters, return the server's effect as self-describing evidence, refuse what cannot be proven safe before it runs, make every failure the first step of recovery, speak the vocabulary the model already holds, and accept the synonym while emitting the canonical form.
