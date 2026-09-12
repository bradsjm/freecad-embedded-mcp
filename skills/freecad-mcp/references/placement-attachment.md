# Placement and attachment

Use this reference when positioning an object, orienting a build, placing a sketch, or editing an attached object. This file owns the `Placement` payload, support and `MapMode`, `AttachmentOffset`, canonical link values, and the FreeCAD 1.1 orientation change.

Property-type and unit tables belong to [FreeCAD fundamentals](fundamentals.md). Common multi-call payloads belong to [Common recipes](recipes.md). Sketch profiles belong to [Sketcher profiles](sketcher.md), and scripted shape transforms belong to [Part geometry and topology](part-topsolids.md).

## Contents

- [Placement payload](#placement-payload)
- [Placement versus shape definition](#placement-versus-shape-definition)
- [Attachment](#attachment)
- [Attachment offset](#attachment-offset)
- [Canonical link values](#canonical-link-values)
- [FreeCAD 1.1 orientation](#freecad-11-orientation)
- [Sources](#sources)

## Placement payload

FreeCAD Placement combines translation and rotation. The MCP property mapper accepts a JSON-friendly dictionary:

```json
{
  "Placement": {
    "position": [10, 5, 0],
    "axis": [0, 0, 1],
    "angle_deg": 90
  }
}
```

`position` carries the translation vector. `axis` plus `angle_deg` (degrees) carry the rotation; FreeCAD stores the underlying rotation internally in quaternion form. The [Placement](https://wiki.freecad.org/Placement) page also documents Euler yaw/pitch/roll and matrix forms.

For direct Python:

```python
import FreeCAD as App
obj.Placement = App.Placement(
    App.Vector(10, 5, 0),
    App.Rotation(App.Vector(0, 0, 1), 90),
)
```

Length properties take plain numbers in the internal unit; read [Units and quantities](fundamentals.md#units-and-quantities) before mixing units.

## Placement versus shape definition

A shape can be defined with vertices already offset from the origin, or it can be placed with `Placement.Base`; both affect the final location. Do not double-translate by applying an offset in the shape constructor and the same offset in Placement unless that is intentional.

Always inspect the global `Shape.BoundBox` after rotating or placing an object. Primitive dimensions describe local geometry and may not describe the world-space envelope. Orient and place the final object deliberately, and confirm the result from its actual global bounds. Use `capture_view` with the `Bottom`, `Front`, `Top`, and `Isometric` orientations to catch accidental rotations.

## Attachment

Attachment maps an object to support geometry using an attachment engine/mode and an attachment offset. For attached sketches/features, the derived Placement is not the primary control. Edit support, `MapMode`, and `AttachmentOffset` rather than trying to force a global Placement that the attachment system will recompute away.

- `MapMode` names how the support geometry defines the attachment frame. It takes the exact enumeration string, never an index.
- Attach critical sketches to Body Origin planes or stable datum geometry.
- Avoid generated faces for long-lived supports when upstream edits can renumber faces.
- Recompute and check the resulting feature state before adding dependents.

An explicit attachment payload names the support and its mode. Verified on FreeCAD 1.1.3, a sketch attaches to a Body origin plane with:

```json
{
  "support": {"object": "XY_Plane"},
  "properties": {"MapMode": "FlatFace"}
}
```

The [Part Attachment](https://wiki.freecad.org/Part_EditAttachment) page documents attachment to faces, edges, vertices, and datum geometry. The typed `parameters` route that names a plane without `support` or `properties` is in [Common recipes](recipes.md); a `support` reference and that typed route are mutually exclusive attachment targets. [mcp-tools.md](mcp-tools.md) carries the exact `create_feature` attachment routing and its error.

## Attachment offset

`AttachmentOffset` is an `App::PropertyPlacement` that positions the attached object relative to its support geometry, so it takes the same dictionary as `Placement`. It is parametric: it survives an upstream edit that the attachment recomputes, while a forced global Placement does not.

Keep attachment offsets explicit and inspect them after edits. Where the attachment system defines the location, change the offset instead of the derived Placement.

## Canonical link values

The MCP mapper accepts link values only in the canonical form. `References` take arrays of `{"object", "subelement"}` values:

```json
{
  "References": [
    {"object": "Base", "subelement": "Face1"}
  ]
}
```

Use the internal `Name`, not the display `Label`, and verify that the subelement is the intended face or edge. Every other link form is rejected before the transaction opens; the complete property-type table is in [Property types](fundamentals.md#property-types). For complex `PropertyLinkSub` and attachment assignments the mapper rejects, use `run_script` with the exact FreeCAD property type.

## FreeCAD 1.1 orientation

FreeCAD 1.1 changed PartDesign Body Origin datum orientation. Older files may need conversion when opened, and 1.1-created files may not be safe to reopen in older FreeCAD versions. Keep the target version explicit in the model handoff.

## Sources

- [Placement](https://wiki.freecad.org/Placement)
- [Placement API](https://wiki.freecad.org/Placement_API)
- [Part Attachment](https://wiki.freecad.org/Part_EditAttachment)
- [Attachment](https://wiki.freecad.org/Attachment)
- [PartDesign Body](https://wiki.freecad.org/PartDesign_Body)
- [Topological naming problem](https://wiki.freecad.org/Topological_naming_problem)
- [FreeCAD 1.1 release notes](https://wiki.freecad.org/Release_notes_1.1)
