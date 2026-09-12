# FEM through FreeCAD MCP

Use FEM to estimate structural behavior before committing to a design; do not treat a successful solve as proof of real-world strength. A simple isotropic bulk-material model does not represent manufacturing defects, assembly effects, or load paths that differ from the defined constraints.

## Contents

- [Required analysis graph](#required-analysis-graph)
- [Recommended MCP sequence](#recommended-mcp-sequence)
- [Material values](#material-values)
- [Constraint properties](#constraint-properties)
- [Mesh and solver limits](#mesh-and-solver-limits)
- [Results](#results)
- [Interpretation](#interpretation)
- [Sources](#sources)

## Required analysis graph

`run_fem` runs the modern `Fem::SolverCalculiX` pipeline. It expects an existing `Fem::FemAnalysis` container (the legacy `Fem::AnalysisPython` TypeId is accepted as an alias when creating) with:

1. A Part-derived solid, such as `Part::Box` or a PartDesign Body.
2. A `Fem::MaterialCommon` assigned to the geometry and added to the analysis.
3. A `Fem::FemMeshGmsh` referencing the geometry and added to the analysis. Create and mesh it through `run_script`.
4. At least one `Fem::ConstraintFixed` and one `Fem::ConstraintForce`, or a `Fem::ConstraintPressure`, bound to geometry faces and added to the analysis.
5. A usable CalculiX installation. `run_fem` selects the single modern solver or creates one. A legacy `Fem::SolverCcxTools` or ambiguous solver setup is an explicit error, never a silent conversion. Missing CalculiX produces an actionable error, not an auto-install.

Read [FEM Analysis](https://wiki.freecad.org/FEM_Analysis), [FEM Mesh Gmsh From Shape](https://wiki.freecad.org/FEM_MeshGmshFromShape), [FEM Material Solid](https://wiki.freecad.org/FEM_MaterialSolid), and [FEM Solver CalculiX](https://wiki.freecad.org/FEM_SolverCalculixCcxtools) before building a nontrivial case.

## Recommended MCP sequence

1. Call `discover_capabilities` and read the FEM availability snapshot.
2. Create or identify the solid with `create_object` and confirm its faces with `inspect_objects`.
3. `create_object` with type `Fem::FemAnalysis`.
4. `create_object` with type `Fem::MaterialCommon`; set its `Material` map values with `edit_object` and add it to the analysis.
5. Create the `Fem::FemMeshGmsh` through `run_script`: add the object, link its `Shape` to the solid, set `CharacteristicLengthMax`/`CharacteristicLengthMin`, add it to the analysis, and recompute so Gmsh generates elements.
6. `create_object` with the `Fem::ConstraintFixed`, `Fem::ConstraintForce`, or `Fem::ConstraintPressure` types; pass canonical `References` and add each constraint to the analysis group.
7. Inspect all FEM objects and their `References` with `inspect_objects(detail="full")`.
8. Use `run_script` for any feature-specific assignment the structured tools reject.
9. Call `run_fem(document, analysis, timeout_s)` with an appropriate timeout; keep no parallel document work while it runs.
10. Read the returned VTK result summary.

## Material values

The `Material` property is a string map; give quantities explicit units, for example:

```json
{
  "Material": {
    "Name": "Generic PLA",
    "Density": "1240 kg/m^3",
    "YoungsModulus": "3500 MPa",
    "PoissonRatio": "0.36"
  }
}
```

Key spellings have differed between server versions. Inspect the created object with `inspect_objects(detail="full")` and follow the current repository example (`examples/cantilever_fem.py`) rather than assuming a key. A material model should be chosen from measured or manufacturer data for the intended application.

## Constraint properties

`edit_object` handles the common cases directly:

- `Fem::ConstraintForce.Force` is a quantity property; assign a plain number (internal unit, newtons for force) through `edit_object`.
- `Fem::ConstraintForce.Direction` is a link/subelement property; assign the canonical form `{"object": "<Name>", "subelement": "Edge1"}`.
- `References` take arrays of canonical `{"object", "subelement"}` values.

Use internal object names and actual face/edge identifiers. Resolve faces deliberately, for example by inspecting face centers or bounding boxes through `run_script`; do not guess that `Face1` is the correct load face. When a property assignment is still rejected, inspect its type in `propertyMetadata` and assign it through `run_script`.

## Mesh and solver limits

- Mesh size is a convergence and runtime tradeoff. Refine around holes, fillets, load application, and supports.
- Avoid tiny sliver faces and invalid geometry before meshing; run `validate_geometry` on the solid first.
- Check that the mesh generated real elements and that all constraints reference valid geometry.
- A solver failure reports `SOLVER_FAILED`; it may indicate missing CalculiX, invalid prerequisites, an unsuitable mesh, or an ill-posed model. Report the actual error and working directory.
- `run_fem` accepts `timeout_s` (1–3600, default 600). The solve runs on the solver process while the GUI thread stays free; the result arrives when the solver and the native result loading finish. Do not fan out document work while it runs.
- Cancellation through `tasks/cancel` is cooperative: the result reports `cancellation_requested`, but a running CalculiX process is never killed.
- `run_script` is refused with `SERVER_BUSY` while a solve is active.

## Results

The result schema reports the loaded VTK multiblock summary, not pre-chewed scalars:

- `pipeline` (the `Fem::FemPostPipeline` in the document), `analysis`, `solver`, `working_dir`, `vtk_path` (a `.vtm` file), and `vtu_files`.
- `blocks`: per-block `points`, `cells`, and finite min/max ranges for each scalar and vector-magnitude array.
- `aggregates`: block count and point/cell sums over the returned blocks (not deduplicated nodes).
- `cancellation_requested`.

Units are solver/report units, not a safety certification. Convert named array ranges into design decisions yourself; do not assume MPa or mm without reading the array names.

## Interpretation

Treat FEM as comparative evidence:

- load direction relative to the modeled supports and constraints;
- stress concentrations at fillets, holes, and abrupt section changes;
- walls, ribs, and inserts that the mesh may not resolve;
- material data quality and temperature dependence;
- deflections that exceed the small-displacement assumptions of a linear solve.

A structurally adequate bulk-solid simulation does not prove real-world strength. Use a prototype or measured coupon for load-critical designs.

## Sources

- [FEM Workbench](https://wiki.freecad.org/FEM_Workbench)
- [FEM Analysis](https://wiki.freecad.org/FEM_Analysis)
- [FEM Material Solid](https://wiki.freecad.org/FEM_MaterialSolid)
- [FEM Mesh](https://wiki.freecad.org/FEM_Mesh)
- [FEM Mesh Gmsh From Shape](https://wiki.freecad.org/FEM_MeshGmshFromShape)
- [FEM Constraint Fixed](https://wiki.freecad.org/FEM_ConstraintFixed)
- [FEM Constraint Force](https://wiki.freecad.org/FEM_ConstraintForce)
- [FEM Constraint Pressure](https://wiki.freecad.org/FEM_ConstraintPressure)
- [FEM Solver CalculiX](https://wiki.freecad.org/FEM_SolverCalculixCcxtools)
- [FEM Solver Run](https://wiki.freecad.org/FEM_SolverRun)
- [CalculiX cantilever example](https://wiki.freecad.org/FEM_CalculiX_Cantilever_3D)
- [FreeCAD MCP FEM example](https://github.com/bradsjm/freecad-embedded-mcp/blob/main/examples/cantilever_fem.py)
