# FreeCAD modeling strategy

Use this file to choose a representation and sequence a build. Payloads, property mapping, units, and version behavior live in the linked references.

## Contents

- [Choose the representation](#choose-the-representation)
- [Establish the requirements before the geometry](#establish-the-requirements-before-the-geometry)
- [Source the fit dimensions of real products](#source-the-fit-dimensions-of-real-products)
- [Design decisions before detailing](#design-decisions-before-detailing)
- [Build in dependency order](#build-in-dependency-order)
- [Build in reviewable stages](#build-in-reviewable-stages)

## Choose the representation

Prefer one primary representation per component.

| Representation | Choose it when | Read |
|---|---|---|
| PartDesign `PartDesign::Body` | The result is one coherent parametric component that must stay editable as a feature history. | [recipes.md](recipes.md), [sketcher.md](sketcher.md) |
| Scripted Part | Independent solids combine by boolean, exact shape construction outranks feature history, or a short script reproduces the result more reliably. | [part-topsolids.md](part-topsolids.md) |
| Structured primitives and objects | The model is a simple primitive or a supported object type without a feature history. | [recipes.md](recipes.md) |
| Draft | The work is planar construction, annotation, arrays, or shape strings. | [Draft Workbench](https://wiki.freecad.org/Draft_Workbench) |

Rules:

- Use one Body per component unless the design needs several independent solids; a second Body beside a completed history is a poor default, not a prohibition.
- Keep scripted Part shapes out of a component that must remain natively editable. Use the native feature or declare the scripted shape as the final artifact.
- Constrained profiles belong to Sketcher. Read [sketcher.md](sketcher.md) for the profile and constraint contract.
- Prefer Origin planes and stable datum geometry for critical sketches. Avoid generated faces when an upstream edit can renumber them (the [topological naming problem](https://wiki.freecad.org/Topological_naming_problem)).
- Keep one dependency chain. Prefer named parameters and a master sketch over duplicated dimensions.

Object type registration, name safety, property names, units, and object identity belong to [fundamentals.md](fundamentals.md); placement, attachment, and link payloads belong to [placement-attachment.md](placement-attachment.md).

## Establish the requirements before the geometry

Resolve these before the first feature. Ask only the questions that change the geometry, a few at a time. State the default you use when the user has no preference.

1. **What is it, and what does it hold or attach to?** Fix a concrete model: a bracket for a named motor, a case for a named phone, a tray for a named slot.
2. **Which dimensions are non-negotiable?** Board outline, screw spacing, the diameter it wraps, the device footprint.
3. **How does it attach?** Bolted, threaded insert, self-tapping screw, snap fit, adhesive, magnet, or freestanding.
4. **How will it be made?** For fused-filament fabrication, collect the material, nozzle, layer height, build volume, and calibration data that change the geometry. Read [printability.md](printability.md).
5. **Which functional requirements matter?** Airflow, cable routing, water resistance, an access panel, a visibility window, stacking, or a weight limit.
6. **Any aesthetic direction?** Ask briefly. Function outranks form.

A non-negotiable fit dimension is a correctness input, not a preference. Never guess one, never round it silently, and never present it as measured. See [validation.md](validation.md) for the acceptance gate on those dimensions.

## Source the fit dimensions of real products

When the part interfaces with an existing product, a connector, or a device, obtain the real dimensions before writing geometry. A small error is invisible in a render and makes the part unusable.

1. Search for the exact product or component with an explicit unit: `"<product> dimensions mm"`, `"<component> mechanical drawing"`, or `"<component> datasheet"`.
2. Prefer a primary source: the manufacturer datasheet, the mechanical drawing, or a published standard.
3. Cross-check two independent sources when the fit is tight, and record both.
4. Verify the exact variant, revision, and generation.
5. Convert and check the unit. Many drawings publish inches.
6. Record each value as a named parameter next to its source, date, and uncertainty.
7. Add a per-side clearance and mark it uncalibrated until a test print confirms it.

A number without a named source, variant, and date is a guess: state the value, its source, and its uncertainty, or leave it as an explicitly named parameter for the user to confirm. Never present a researched dimension as measured. Distinguish a datasheet value, a third-party measurement, a user measurement, and your own estimate.

For electronic components, read [electronic-components.md](electronic-components.md). Use the manufacturer datasheet, and treat an imported model as an envelope rather than a complete keep-out. Model connector openings, mating travel, and cable bend separately from the visible shell.

## Design decisions before detailing

Decide these before creating detailed features:

- The intended orientation and the principal load direction.
- Wall-width candidates, as a small named set of parameters rather than arbitrary thin values.
- Fit-clearance parameters, per side, each named and each marked calibrated or uncalibrated.

Keep load paths continuous. Replace a flat unsupported shelf or roof with a chamfer, arch, teardrop roof, or rib. Add root fillets to hooks, snap arms, bosses, and cantilevers. Add lead-in chamfers where insertion matters.

Read [printability.md](printability.md) for wall, clearance, and overhang sizing values, and [validation.md](validation.md) for the checks that close the design.

## Build in dependency order

Use this order unless the requested model requires a different graph:

1. Create or identify the target document. Call `inspect_documents` first; never assume names or active-document state.
2. Create the base solid or Body.
3. Create stable profiles, sketches, datum references, or helper solids.
4. Add additive features.
5. Add subtractive features such as holes and pockets.
6. Add booleans, patterns, fillets, chamfers, shell and thickness, and cosmetic features late.
7. Add placement, attachment, and visibility. Read [placement-attachment.md](placement-attachment.md).
8. Recompute, inspect, validate, and export. Read [validation.md](validation.md).

Rules:

- Name intermediate helpers by role, and keep the final object distinct from its helpers.
- Hide helpers instead of deleting them until the final result is verified.
- Add finishing features largest first. Apply fillets after the shell or thickness operation, and revalidate after each dress-up.
- Build stable corner radii into a sketch when upstream dimensions can change the corner topology.
- After a topology-changing edit, reinspect dependent fillets and chamfers. Rebuild or retarget each invalid edge link through a supported route.
- Export exactly the intended final object, never the whole document.

For an edit to an existing model, call `inspect_objects` and reuse the returned internal `name` before every dependent change; the object, property, and recompute rules are in [fundamentals.md](fundamentals.md).

## Build in reviewable stages

Build a shape, verify it, and save a checkpoint before adding the next layer of detail. Never write the whole model in one script and validate only at the end: a late failure hides its cause, and the user cannot steer a design they have not seen.

| Stage | Build | Verify before continuing |
|---|---|---|
| 1. Base form | Outer envelope, walls, base plate. No cutouts, no fillets. | Overall bounds match the requirements. A flat face lies on the bed. |
| 2. Features | Holes, cutouts, bosses, slots, vents, internal structure. | Each feature is present and correctly placed. Booleans are clean. |
| 3. Finish | Fillets, chamfers, edge cleanup, cosmetic detail. | Final geometry validation, view review, printability checks. |

After each stage:

1. Run `capture_view` from at least `Isometric`, `Top`, and `Front`.
2. Run `validate_geometry` for bounds, solid count, and validity.
3. Save the milestone with `save_document` before the next stage.

Show the user the stage result and the key dimensions, then continue. Treat design approval as a real decision point, because a wrong envelope or mounting layout is expensive to change after detail work. Do not stall on a stage the user already specified or approved, and do not request approval for a detail the requirements already decide.
