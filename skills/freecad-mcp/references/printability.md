# Design for fused-filament fabrication

Use this reference only when the part will be made by fused-filament fabrication (FDM/FFF). Start with the function, interfaces, loads, and fit requirements. Apply manufacturing constraints only where they change the geometry or the delivery advice.

Treat every number in this reference as a starting heuristic for an unknown printer, and mark it as such in the report. Printer condition, nozzle, material, layer height, extrusion width, cooling, and slicer settings change the usable values. Values marked "synthesized" come from process logic in this skill, not from a printer or filament maker. Prefer a measured coupon or an explicit machine profile over a generic value.

## Contents

- [Collect process inputs only when they matter](#collect-process-inputs-only-when-they-matter)
- [Scale features to extrusion lines and layers](#scale-features-to-extrusion-lines-and-layers)
- [Choose the build orientation first](#choose-the-build-orientation-first)
- [Measure overhangs from the vertical](#measure-overhangs-from-the-vertical)
- [Choose chamfers and fillets by orientation](#choose-chamfers-and-fillets-by-orientation)
- [Design bridges and supports deliberately](#design-bridges-and-supports-deliberately)
- [Size walls, ribs, bosses, holes, and text](#size-walls-ribs-bosses-holes-and-text)
- [Add sacrificial features](#add-sacrificial-features)
- [Help the slicer](#help-the-slicer)
- [Parameterize fits per side](#parameterize-fits-per-side)
- [Choose the material](#choose-the-material)
- [Apply the rules with FreeCAD MCP tools](#apply-the-rules-with-freecad-mcp-tools)
- [Deliver applicable manufacturing guidance](#deliver-applicable-manufacturing-guidance)
- [Verify the design before export](#verify-the-design-before-export)
- [Sources](#sources)

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

If these inputs are unavailable, keep geometry-driving values as named parameters and state the assumptions. Use a 0.4 mm nozzle, a 0.45 mm line width (synthesized as 1.125 × nozzle), and 0.2 mm layers only when nothing better is known. Keep a maximum layer height near 70–80 % of nozzle diameter.

### Example machine profile: Bambu Lab P1S

Use the P1S only when the user names it, provides its profile, or asks for an example. Do not make P1S-specific constraints the general rule.

A starting P1S example can include:

- enclosed CoreXY printer;
- 256 × 256 × 256 mm nominal build volume;
- 0.4 mm nozzle;
- 0.2 mm layer height;
- material and slicer preset selected for the part.

Verify the current machine and filament documentation before treating these values as requirements. User modifications, a different nozzle, build-plate exclusions, and the selected preset can change the usable limits.

## Scale features to extrusion lines and layers

Design in extrusion lines and layers, not in millimetres alone. Two dimensions drive printable geometry: the deposited line width in XY and the layer height in Z. A motion system positions more finely than either, but every wall, stroke, and pin must become a bead of material.

Line widths below are synthesized from the 1.125 × nozzle rule. Maximum layer heights follow the 70–80 % rule. Wall columns are multiples of the line width.

| Nozzle | Line width | Maximum layer height | One-line wall | Two-line wall | Structural shell, three to four lines |
|---:|---:|---:|---:|---:|---:|
| 0.25 mm | 0.28 mm | ≤0.20 mm | 0.28 mm | 0.56 mm | 0.84–1.13 mm |
| 0.40 mm | 0.45 mm | ≤0.32 mm | 0.45 mm | 0.90 mm | 1.35–1.80 mm |
| 0.60 mm | 0.68 mm | ≤0.48 mm | 0.68 mm | 1.35 mm | 2.0–2.7 mm |
| 0.80 mm | 0.90 mm | ≤0.64 mm | 0.90 mm | 1.80 mm | 2.7–3.6 mm |

- Treat a one-line wall as a printability threshold, not as structure. It survives, but it has no redundancy.
- Treat two lines as the floor for a housing, boss, bracket, or snap feature, and three or four as the robust target.
- Scale vertical relief in whole layers. Use at least two layers and prefer three: 0.4–0.6 mm at 0.2 mm layers (synthesized).
- Check small features against the line width before detailing. A slicer preview shows generated paths; it does not replace geometric validation in FreeCAD.

## Choose the build orientation first

Choose an orientation before finalizing overhangs, surfaces, fits, or text. Orientation is a multicriteria decision, not a search for the largest face. Use this priority order: load path, unsupported geometry, critical dimensions, bed stability, cosmetic faces, seam placement, print time.

- Keep a principal tensile or bending load in the XY plane, so the load runs along layers. A load that peels layers apart uses the weaker interlayer bond.
- Put a suitable flat face on the bed when this improves stability and adhesion.
- Broaden the base of a tall or narrow part, and round plan-view corners on large warp-prone bases.
- Keep mating, sealing, and cosmetic faces away from supports when orientation permits.
- Split a part when the split gives each piece a better orientation, a support-free build, or a measurable interface. Add locating features to the split; see [Parameterize fits per side](#parameterize-fits-per-side).
- Model in the print orientation with document +Z as the build-up direction when practical. Then `<Z` and `-Z` selectors map to the bed. If the model is not in print orientation, record the rotation and convert every direction before comparing it with a tool result.

Do not force a large flat-bed orientation when another orientation better protects a critical fit, load path, or surface.

## Measure overhangs from the vertical

Measure the overhang angle θ from the vertical. θ = 0° is a vertical wall. θ = 90° is a horizontal ceiling. Some guides measure from the horizontal instead; convert before you compare their numbers: θ_vertical = 90° − α_horizontal.

- Use θ ≤ 45° as the conservative self-support baseline for an unknown printer.
- Expect 50–60° from vertical on a well-cooled, tuned machine. Prusa quotes a 45–60° range in this vertical convention.
- Print an overhang coupon before using any value above 45°. The safe angle depends on material, nozzle, layer height, cooling, and speed.
- Convert an unsupported horizontal ledge into a 45° ramp where the function permits. The ramp removes the support requirement with little change to the envelope.
- Read a planar face from its outward normal. The limit for an allowed angle θ_max is a normal z-component of `−sin(θ_max)`: −0.7071 at 45°. A face below that limit, with a small numerical tolerance, is more horizontal than the baseline. A normal of about `nz ≈ −1` is a horizontal ceiling: it must bridge or be supported, unless it lies wholly on the bed.

Check with tools: use [Audit overhangs and horizontal ceilings](recipes.md#audit-overhangs-and-horizontal-ceilings).

## Choose chamfers and fillets by orientation

Use the rule "fillet for stress, chamfer for unsupported manufacturability".

- Use a chamfer where a downward-facing convex fillet would start at the bed or open into air. Each successive layer of that fillet extends farther out, so the overhang gets worse with height.
- Use a 45° chamfer for a bed-facing edge, an unsupported shoulder, a counterbore underside, and the underside of a projecting boss.
- Use a fillet where the surface faces upward or sideways: boss roots, rib junctions, vertical XY-plane corners, and cantilever roots.
- Break the bottom outside edge of a fit-critical part with a small 45° chamfer against elephant-foot interference. UltiMaker recommends the chamfer but not a size. Start with 0.2–0.4 mm for a 0.4 mm nozzle at 0.2 mm layers (synthesized), and tune it from the first-layer squish of the actual profile.
- Revalidate dependent dress-up features after a topology change. A failed fillet is a stop condition, not a cosmetic loss.

Check with tools: use [Chamfer bed-facing edges](recipes.md#chamfer-bed-facing-edges).

## Design bridges and supports deliberately

A bridge spans free space between two anchored regions. It is not an overhang, and it has its own limits.

- Treat about 10 mm as a validation threshold for an unknown printer and material, not as a maximum. Above it, test the pair or redesign the span. UltiMaker reports longer validated spans on its own hardware with optimized material.
- Span the shortest dimension of a rectangular opening, and rotate a rectangular hole so the strands cross its narrow side.
- Give every bridge two substantial anchor lands. Do not land the first bridge layer on a point or a thin wall.
- Replace the crown of a horizontal round hole with a teardrop, diamond, or 45° roof profile.
- Design for the weakest support system likely to be used. Soluble or breakaway support relaxes constraints only when the manufacturing specification guarantees it.
- Keep support contact off cosmetic, sealing, and fit-critical faces. Support marks change fit and finish, and removal adds work.
- Accept supports when redesign would harm function, strength, accuracy, or maintainability. Name the supported faces in the delivery.

Prefer these redesigns before adding support:

- Replace a horizontal roof with two 45° roofs.
- Split the part and rejoin it with locating pins, tongues, or keys.
- Replace a shelf with a gusset.

## Size walls, ribs, bosses, holes, and text

Use this wall table for a 0.4 mm nozzle at about 0.45 mm line width:

| Feature | Starting thickness | Reason |
|---|---:|---|
| Cosmetic membrane | 0.45 mm | One line; vulnerable |
| General enclosure wall | 0.9–1.35 mm | Two to three lines |
| Functional wall | 1.35–1.8 mm | Three to four lines; robust |
| Local load-bearing region | ≥1.8 mm or locally reinforced | Add ribs or bosses rather than infill |

- Keep walls uniform in thickness. Uneven walls shrink unevenly, especially with engineering materials.
- Leave the interior solid volume to the slicer. Model a lattice only when the internal architecture is a functional requirement.
- Make a rib two or three lines thick: about 0.9–1.35 mm at a 0.4 mm nozzle. A 0.6–0.7 mm rib can slice into an unstable mix of paths. Keep a gusset underside at 45° or less from vertical; a more horizontal face is an overhang beyond the baseline.

Holes:

- Keep a hole diameter at 2 mm or more unless a calibrated process proves a smaller feature. UltiMaker and Protolabs both quote 2 mm as a practical floor.
- Prefer a vertical-axis hole. It is cut as XY perimeter curves and holds circularity and diameter better. Give a horizontal-axis hole a teardrop, diamond, or 45° roof, or leave drilling stock and machine it.
- Do not offset every hole by one blanket value. Print a coupon with the relevant diameters in the print orientation, measure it, and parameterize the compensation.

Bosses:

- Give a screw boss three or four lines of radial wall: about 1.35–1.8 mm at a 0.4 mm nozzle.
- Connect the boss to a wall, or add a fillet or gusset at its root. Avoid a flat horizontal shelf under a projecting boss: start the boss at a wall, support it with a 45° gusset, or move it to another component.
- Design a pilot and drill it for a critical screw. Do not expect an as-printed small bore to be metrological.

Text and markings, for a 0.4 mm nozzle:

| Text feature | Starting value |
|---|---:|
| Stroke floor | 0.45 mm |
| Preferred stroke width | 0.6–0.9 mm |
| Emboss or deboss depth at 0.2 mm layers | ≥0.4 mm; 0.6 mm is safer |
| Gap between strokes | ≥0.4–0.5 mm |
| Cap height | 4–5 mm or larger |

The stroke and depth values are synthesized: preferred strokes are about 1.5–2 lines, and relief spans two to three layers. Prefer top-facing text. Vertical-side text can work, but its smallest horizontal strokes depend on layer height. Choose the orientation before finalizing text.

Check with tools: measure a wall with `measure` in section or faces mode, as in [Check wall thickness against line width](recipes.md#check-wall-thickness-against-line-width). A parameter value or a view panel is not thickness evidence.

## Add sacrificial features

Add material that makes the printing state stable, then remove it to reach the functional state.

- Add a thin membrane under a counterbore so the roof bridges instead of stepping outward. Size it as a few layers from the intended layer height, and keep it accessible to a knife or drill.
- Add a bridge roof across a horizontal hole when a teardrop shape is not acceptable.
- Add a mouse ear at a warp-prone corner of a large flat base, and a breakaway tab between tall slender features that would otherwise sway.
- Add machining stock on a critical bore or face that will be drilled or milled.
- Report every sacrificial feature in the delivery, with its removal method.

## Help the slicer

- Leave a rear rib, groove, flat, or concave corner on a cosmetic curved part so the seam has a natural place. A smooth cylinder gives the slicer nowhere to hide a seam.
- Reduce disconnected islands on the same layer, and join neighboring features where the function permits. Islands force more travel and retraction, and they raise stringing risk, especially with PETG.
- Reserve build-area margin for brim, skirt, raft, support base, and purge structures. The nominal envelope is not the usable area.
- Do not prescribe a universal retraction distance. The value depends on the feed system, hotend, filament, and profile. Change the geometry instead.
- Slice the model and inspect the generated perimeters, bridges, supports, seam, and first layer.

## Parameterize fits per side

State every fit as a gap between two surfaces, per side. Diametral clearance is twice the per-side gap. Prusa and UltiMaker state the gap between parts; report it per side in this skill so the value matches the modeled offset.

Clearance is the design gap. Tolerance is the process variation. Keep them separate: 0.3 mm per side with ±0.15 mm process variation is a different design from 0.3 mm with ±0.05 mm.

| Relationship | Per-side gap | Status |
|---|---:|---|
| Locating or slip fit | 0.10–0.15 mm | Synthesized; coupon required |
| Movable parts printed separately | 0.3–0.5 mm; at least 0.3 mm | Prusa minimum; validate the upper value |
| Print-in-place moving joint | 0.5–0.6 mm | UltiMaker quotes 0.6 mm between components |
| Snap-fit running clearance | Start in the movable range and test | Material and orientation specific |
| Press fit | No universal interference | Calibrate with a coupon, or drill a pilot |

Rules:

- Store clearance per side as a named parameter.
- Separate sliding, captured, press, snap, and thread compensation, and keep hole compensation separate from mating clearance.
- Mark every untested value as uncalibrated in the report.
- Never model a nominal zero-clearance sliding joint.

Press fits:

- Do not apply a generic negative allowance. Printed bore bias varies by printer, material, diameter, and orientation. Make one component sacrificial, or leave a pilot bore and drill or ream it to the final interference.

Snap fits:

- Satisfy two independent conditions: the assembly needs clearance, and the flexible beam needs to survive its deflection.
- Use a long tapered cantilever instead of a short thick hook, and round its root.
- Orient the beam so flexural tensile stress runs within layers, not across layer interfaces.
- Add a hard stop so the beam cannot be over-deflected during assembly.
- Reorient the part, or split it so the snap prints flat, when the root would peel layers apart.

Assembly features:

- Add lead-in chamfers on pins and sockets, and a relief at the bottom of a blind socket so elephant foot or debris does not bottom the part early.
- Use an asymmetric key when orientation matters.
- Join split parts with pins, tongues, dovetails, lap joints, or a stepped interface. Do not rely on two flat butt faces for location.
- Control location with isolated datum pads, pins, or shoulders instead of two broad as-printed faces.

Check with tools: record a fit as named checks, as in [Measure fit and clearance with both modes](validation.md#5-measure-fit-and-clearance-with-both-modes).

## Choose the material

Use material behavior to guide shape and delivery. Do not make unsupported strength claims.

Heat values are Prusament heat-deflection temperatures at 0.45 MPa: PLA 55 °C, PETG 68 °C, ASA 93 °C. They describe that filament, not every filament of the polymer.

| Consideration | PLA | PETG | ASA |
|---|---|---|---|
| Warp | Low | Low | High |
| Enclosure | Usually unnecessary | Usually unnecessary | Strongly recommended |
| Layer adhesion | Adequate but brittle | Good | Good, with warp-driven delamination risk |
| Bridges and overhangs | Generally favorable | Poorer than PLA | Good detail, limited cooling |
| Stringing | Possible | Characteristic concern | Lower concern |
| Heat resistance | Poor | Moderate | High among these three |
| UV and outdoor | Poor | Formulation dependent | Primary strength |
| Chemical resistance | Poor against aggressive service | Better; check the substance | Good except acetone |
| Post-processing | Wet sanding | Sanding and machining | Sanding, acetone welding, vapor smoothing |

- PLA: design for detail, low warp, and large flat geometry. Keep it away from hot vehicles, warm machinery, sustained UV, and aggressive chemicals. Do not design thin snap beams or press-loaded features as if PLA were ductile.
- PETG: use it for brackets, housings, clips, and wet environments where toughness and layer adhesion matter. Design conservatively around unsupported roofs, stringing, and support removal.
- ASA: design around shrinkage from the start. Keep walls uniform, round plan-view corners, avoid long thin flat plates, give large parts generous bed contact, and split large assemblies into thermally balanced pieces.

For other common filaments: ABS tolerates more heat than PLA and PETG and warps like ASA; an enclosure and balanced sections help both. TPU needs geometry and profile choices appropriate to its flexibility and extrusion behavior.

Take print and bed temperatures from the exact filament datasheet or a validated slicer profile, not from this skill. Profiles differ even within one polymer: Prusa's knowledge-base PETG profile and the Prusament PETG datasheet disagree on nozzle and bed temperature.

Use a datasheet, a measured specimen, or FEM with explicit assumptions for load-critical claims. A successful print does not establish a safe working load.

## Apply the rules with FreeCAD MCP tools

Keep the build-direction convention from [Choose the build orientation first](#choose-the-build-orientation-first). Never use a view name as a bed reference. Topology selector aliases keep CadQuery meanings (`front` is >Z, `top` is >Y), while `capture_view` panels use FreeCAD camera views, so the same word means different directions. Establish orientation from geometry and axes, not from object labels; when you read a capture image, use the camera directions in its `views` manifest.

| Rule | Tool | Recipe |
|---|---|---|
| Store process parameters | `edit_parameters` on an `App::VarSet`; bind expressions | [Store FFF process parameters](recipes.md#store-fff-process-parameters) |
| Audit overhangs and ceilings | `inspect_topology` with `detail: "full"` | [Audit overhangs and horizontal ceilings](recipes.md#audit-overhangs-and-horizontal-ceilings) |
| Chamfer bed edges | `create_feature` kind `chamfer` | [Chamfer bed-facing edges](recipes.md#chamfer-bed-facing-edges) |
| Cut a teardrop hole | `create_feature` kind `pocket` on a vertical sketch plane | [Cut a teardrop horizontal hole](recipes.md#cut-a-teardrop-horizontal-hole) |
| Check wall thickness | `measure` in section or faces mode | [Check wall thickness against line width](recipes.md#check-wall-thickness-against-line-width) |
| Check the envelope | `validate_geometry` bounds against the usable envelope | [Check build volume fit](recipes.md#check-build-volume-fit) |

## Deliver applicable manufacturing guidance

For a printed part, report only the guidance that affects use or reproduction:

- build orientation and its reason;
- support requirement and affected faces;
- material assumption;
- nozzle, line width, layer height, and profile assumptions when they drove geometry;
- every per-side clearance and its calibrated or uncalibrated status, including hole compensation;
- sacrificial features and their removal method;
- required post-processing, machining stock, or test coupons.

## Verify the design before export

1. Confirm that the orientation fits the usable build volume, including brim, skirt, support, and purge margin.
2. Confirm that the bed contact is stable, or specify an adhesion strategy.
3. Check load-critical features against layer direction.
4. List every downward planar face and every ceiling beyond the chosen overhang angle. Record a bridge, support, or redesign decision for each one.
5. Check each bridge span against the validation threshold and each anchor land for substance.
6. Check walls against line width, and ribs, bosses, holes, text, and vertical relief against the extrusion-scale tables.
7. Check each fit against a per-side parameter and calibration evidence, or mark it uncalibrated.
8. Check the snap-fit root, lead-in, and hard stop.
9. Confirm that no support contact falls on a cosmetic, sealing, or fit-critical face.
10. Review seam placement on a cosmetic curved part. Add a seam-hiding feature only when the design permits it.
11. Run the geometry checks in [Geometry validation](validation.md).
12. Slice the model, inspect the toolpaths, and review the exported mesh.

## Sources

- [Prusa Knowledge Base, Modeling with 3D printing in mind](https://help.prusa3d.com/article/modeling-with-3d-printing-in-mind_164135)
- [UltiMaker, Design for FDM 3D printing](https://ultimaker.com/learn/design-for-fff-3d-printing-maximize-your-success/)
- [UltiMaker, How to design for FFF 3D printing (PDF)](https://ultimaker.com/wp-content/uploads/2024/06/How-to-design-for-FFF-1.pdf)
- [Part Fillet](https://wiki.freecad.org/Part_Fillet) and [Part Chamfer](https://wiki.freecad.org/Part_Chamfer) for FreeCAD dress-up features
- [Export to STL or OBJ](https://wiki.freecad.org/Export_to_STL_or_OBJ) for mesh export and unit assumptions
- [Bambu Lab P1S specifications](https://bambulab.com/en/p1/tech-specs) for the example machine profile

Cited by name only, without a link: Prusa's extrusion-width article and PrusaSlicer documentation, Prusament technical datasheets for PLA, PETG, and ASA, UltiMaker Cura settings documentation, and Protolabs FDM design guidelines.

Confirm machine specifications against the current manufacturer documentation. Confirm manufacturing heuristics with the intended process and a representative test print.
