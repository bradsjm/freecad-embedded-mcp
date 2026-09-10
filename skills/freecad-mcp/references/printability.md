# Design for fused-filament fabrication

Use this reference only when the part will be made by fused-filament fabrication. Start with the function, interfaces, loads, and fit requirements. Apply manufacturing constraints only where they change the geometry or delivery advice.

Values in this reference are starting heuristics. Printer condition, nozzle, material, layer height, extrusion width, cooling, and slicer settings can change them. Prefer a measured calibration part or an explicit machine profile over a generic value.

## Collect process inputs only when they matter

Do not require a printer model before all CAD work. Ask only for inputs that affect the requested part:

| Input | Use it to decide |
|---|---|
| Nozzle diameter and extrusion width | Minimum walls, slots, pins, text, and other small features |
| Layer height | Vertical resolution, shallow slopes, stair-stepping, and layer count |
| Material | Stiffness, heat resistance, creep, shrinkage, cooling, and warp risk |
| Build volume | Whether the part fits, must rotate, or must split |
| Enclosed or open machine | Cooling and warp strategy for temperature-sensitive materials |
| Calibrated fit or hole data | Clearances and dimensional compensation |
| Slicer profile | Perimeters, support settings, bridge behavior, and extrusion assumptions |

If these inputs are unavailable, keep geometry-driving values as named parameters and state the assumptions. A common 0.4 mm nozzle and 0.2 mm layer height can seed an early concept, but they are not universal defaults.

### Example machine profile: Bambu Lab P1S

Use the P1S only when the user names it, provides its profile, or asks for an example. Do not make P1S-specific constraints the general rule.

A starting P1S example can include:

- enclosed CoreXY printer;
- 256 × 256 × 256 mm nominal build volume;
- 0.4 mm nozzle;
- 0.2 mm layer height;
- material and slicer preset selected for the part.

Verify the current machine and filament documentation before treating these values as requirements. User modifications, a different nozzle, build-plate exclusions, and the selected preset can change the usable limits.

## Choose the build orientation with the geometry

Choose an orientation before you finalize features that depend on overhangs, surface finish, or layer direction.

- Put a suitable flat face on the bed when this improves stability and adhesion.
- Align the principal tensile or bending load with layer planes when practical. Loads across layers can expose weaker interlayer bonding.
- Keep mating, sealing, and cosmetic faces away from supports when possible.
- Use a bottom chamfer where a rounded lower edge would begin as a severe overhang.
- Use teardrop, diamond, or relieved profiles for horizontal holes when a round roof would sag.
- Split or reorient a part when this gives a better load path or removes inaccessible supports.

Do not force a large flat-bed orientation when another orientation better protects a critical fit, load path, or surface.

## Design overhangs and bridges deliberately

Treat 45° from vertical as an initial overhang screen, not a machine limit. Test the selected material and profile when an unsupported surface matters.

- Replace an unsupported shelf with a chamfer, arch, rib, or split where the function permits.
- Shorten bridge spans or support them at both ends.
- Add a root fillet where it reduces stress and remains printable.
- Use a root chamfer when the same location would otherwise create an unsupported lower curve.
- Accept supports when redesign would harm function, strength, accuracy, or maintainability. Identify the supported faces in the delivery.

Supports on a mating or cosmetic face require explicit review because removal can change fit and finish.

## Size printable features from the process

Express small-feature checks relative to the intended extrusion width and layer height.

- Use two or more extrusion-width paths for a structural wall unless a validated thin-wall strategy applies.
- Widen or remove a feature that is too narrow for the slicer to form consistently.
- Add gussets or increase section size for slender pins, tabs, and freestanding walls.
- Break the bottom outside edge when first-layer expansion could tighten a fit.
- Treat small and horizontal holes as calibration-sensitive. Use a measured compensation or plan a finishing operation when diameter is critical.
- Check embossed and recessed text against actual line width, layer height, and viewing distance.

A slicer preview is useful evidence for path generation. It does not replace geometric validation in FreeCAD.

## Parameterize clearances and compensation

Base fits on a calibration made with the intended machine, material, orientation, and profile when possible.

- Store clearance per side as a named parameter.
- Separate sliding clearance, captured clearance, press fit, snap fit, and thread compensation.
- Keep hole compensation separate from general mating clearance.
- Mark an untested value as uncalibrated.
- Use 0.30 mm per side only as a conservative concept-stage example, not as a universal fit.

## Consider material behavior

Use material behavior to guide the shape, not to make unsupported strength claims.

- PLA is stiff and dimensionally stable, but sustained heat can reduce stiffness.
- PETG is tougher and more heat tolerant than PLA, but sustained preload can expose creep.
- ABS and ASA tolerate more heat and can have higher warp risk; an enclosure and balanced sections can help.
- TPU needs geometry and profile choices appropriate to its flexibility and extrusion behavior.

Use a datasheet, a measured specimen, or FEM with explicit assumptions for load-critical claims. A successful print does not establish a safe working load.

## Deliver applicable manufacturing guidance

For a printed part, report only the guidance that affects use or reproduction:

- build orientation and its reason;
- support requirement and affected faces;
- material assumption;
- nozzle, layer height, and profile assumptions when they drove geometry;
- uncalibrated clearances, hole offsets, or fits;
- required post-processing or test coupons.

## Verify the design before export

1. Confirm that the chosen orientation fits the intended build volume.
2. Confirm that the bed contact is stable or specify an adhesion strategy.
3. Check load-critical features against layer direction.
4. Check walls and small features against the intended extrusion width and layer height.
5. Identify unsupported regions, bridges, and support-contact faces.
6. Check fit values against calibration evidence or mark them as assumptions.
7. Run the geometry checks in [Geometry validation](validation.md).
8. Review the exported mesh and, when available, the slicer preview.

## Sources

- [Part Fillet](https://wiki.freecad.org/Part_Fillet) and [Part Chamfer](https://wiki.freecad.org/Part_Chamfer) for FreeCAD dress-up features
- [Export to STL or OBJ](https://wiki.freecad.org/Export_to_STL_or_OBJ) for mesh export and unit assumptions
- [Bambu Lab P1S specifications](https://bambulab.com/en/p1/tech-specs) for the example machine profile

Confirm machine specifications against the current manufacturer documentation. Confirm manufacturing heuristics with the intended process and a representative test print.