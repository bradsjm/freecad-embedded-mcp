# Electronic component 3D models

Use [Component Search Engine](https://componentsearchengine.com/) as an external research source when a FreeCAD enclosure, bracket, panel, PCB carrier, or assembly needs a realistic electronic-component envelope. The available FreeCAD MCP tools do not provide a Component Search Engine API or a dedicated CAD-import tool. Use `read` for public page research, browser automation only when an authenticated interactive action is authorized, and `run_script` for FreeCAD import and document mutation.

## Source and identify the component

1. Use `read` on `https://componentsearchengine.com/` or a known part page.
2. Search by the exact manufacturer part number from the bill of materials or datasheet. If no exact result exists, research partial part number, manufacturer, package designation, or functional keyword matches.
3. Verify manufacturer, complete ordering code, package/category, pin count, footprint name, and description against the manufacturer datasheet.
4. Inspect available 3D-model metadata/previews when returned by the page. Do not mistake a schematic symbol or PCB footprint for a mechanical 3D model.
5. Capture the part-page URL, manufacturer part number, download date, available formats, and any model revision/update information.
6. Prefer a neutral solid format offered by the source, commonly STEP. Preserve the downloaded file and its license/terms in the project workspace.

Component Search Engine documents free account access to ECAD models, symbols, footprints, and 3D models, plus a Library Loader helper and Build Wizard/part-request services. These are external services: do not claim access, download, authentication, or a completed part request unless the corresponding `read`/browser operation actually succeeded. Library Loader is not a FreeCAD MCP capability and must not be assumed necessary.

Sources: [Component Search Engine](https://componentsearchengine.com/), [Learn more](https://componentsearchengine.com/learn-more), [PCB component library examples](https://componentsearchengine.com/examples), and [Library Loader](https://componentsearchengine.com/LibraryLoader).

## Import through run_script

Use the known local path supplied by the task. Import and document mutation must run through `run_script`; it executes synchronously on the GUI thread, and there is no asynchronous execution path.

```python
import os
import FreeCAD as App

path = "/absolute/path/to/component.step"
assert os.path.isfile(path), path

# Import into a dedicated document so importer-created objects are isolated.
import_doc = App.openDocument("/absolute/path/to/ElectronicsAssembly.FCStd")
App.setActiveDocument(import_doc.Name)
import Part
Part.insert(path, import_doc.Name)
import_doc.recompute()
print([(obj.Name, obj.Label, obj.TypeId) for obj in import_doc.Objects])
```

The exact importer can vary by format and FreeCAD 1.1 build. For STEP, `Part.insert(path, doc.Name)` is a common synchronous route; for other formats, use the registered importer documented by the running installation. If an import API fails, inspect the exception and available modules rather than retrying guessed calls. Do not assume the imported object name, document, units, or solid count.

After import, call `inspect_objects(document)` to identify the imported object. Use returned internal `Name` values in later edits and links. If the imported file opened a separate document, preserve that fact and copy/link only the intended object through explicit `run_script` code.

If the source offers a mesh only, treat it as a visual envelope unless it passes mesh evaluation/repair and conversion checks. Native ECAD-library files are not automatically FreeCAD-readable; use a neutral export or build a simplified parametric envelope from the datasheet.

## Normalize and position the model

After import, use `run_script` to:

- assign a semantic `Label` without losing the manufacturer part number;
- inspect `TypeId`, `ShapeType`, solid count, volume, bounds, and validity;
- preserve the original and create a simplified reference copy when collision calculations do not need cosmetic detail;
- identify the coordinate origin, pin-1/keying direction, board-facing side, and unit convention;
- set Placement or attachment deliberately, recompute, and recheck global bounds;
- create separate keep-out solids for leads, connector insertion, cable bend radius, mounting hardware, thermal clearance, and assembly-tool access;
- avoid unstable generated faces as long-lived attachment supports.

Do not use a component model’s visible shell as the complete keep-out. A model can omit pins, contacts, cable access, mating travel, or manufacturing clearances.

## Validate before designing around it

Require these machine-checkable records before using the component in surrounding geometry:

1. Exact manufacturer ordering code match.
2. Critical dimensions compared with the datasheet/mechanical drawing.
3. Known units and orientation.
4. Global bounds and board reference plane.
5. Connector, pin, mounting-hole, and mating clearances.
6. Documented missing or simplified features.
7. Valid surrounding FreeCAD geometry after boolean/attachment operations.
8. Expected global bounds asserted for the final enclosure/bracket.

Do not use a visually similar model for a tight-fit connector, switch, display, heatsink, socket, or mounting interface without mechanical-drawing comparison. Mark approximate envelopes explicitly in object labels and output reports.

## No model available

If no verified model exists, use the datasheet to create an explicitly labeled parametric envelope through `run_script`. Component Search Engine documents a Build Wizard and part-request service, but this skill can only research or report those external options; it cannot assume they have completed.

## Sources

- [Component Search Engine](https://componentsearchengine.com/)
- [Component Search Engine learn-more](https://componentsearchengine.com/learn-more)
- [PCB Component Library Examples](https://componentsearchengine.com/examples)
- [Component Search Engine Library Loader](https://componentsearchengine.com/LibraryLoader)
- [FreeCAD Import/Export](https://wiki.freecad.org/Import_Export)
- [FreeCAD Part CheckGeometry](https://wiki.freecad.org/Part_CheckGeometry)
- [FreeCAD Placement](https://wiki.freecad.org/Placement)
