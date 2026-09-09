# Placement and attachment

Use this reference when positioning a part, orienting a build, placing a sketch, or editing an attached object.

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

Always inspect the global `Shape.BoundBox` after rotating or placing an object. Primitive dimensions describe local geometry and may not describe the world-space envelope.

## Placement versus shape definition

A shape can be defined with vertices already offset from the origin, or it can be placed with `Placement.Base`; both affect the final location. Do not double-translate by applying an offset in the shape constructor and the same offset in Placement unless that is intentional.

Orient and place the final object deliberately, and confirm the result from its actual global bounds. Use `capture_view` with the `Bottom`, `Front`, `Top`, and `Isometric` orientations to catch accidental rotations.

## Attachment

Attachment maps an object to support geometry using an attachment engine/mode and an attachment offset. For attached sketches/features, the derived Placement is not the primary control. Edit support, `MapMode`, and `AttachmentOffset` rather than trying to force a global Placement that the attachment system will recompute away.

The [Part Attachment](https://wiki.freecad.org/Part_EditAttachment) page documents attachment to faces, edges, vertices, and datum geometry. For robust parametric models:

- Attach critical sketches to Body Origin planes or stable datum geometry.
- Avoid generated faces for long-lived supports when upstream edits can renumber faces.
- Keep attachment offsets explicit and inspect them after edits.
- Recompute and check the resulting feature state before adding dependents.

## References and links

The MCP mapper accepts link values only in the canonical form. `References` take arrays of `{"object", "subelement"}` values:

```json
{
  "References": [
    {"object": "Base", "subelement": "Face1"}
  ]
}
```

Use internal `Name`, not `Label`, and verify that the subelement is the intended face/edge. For complex `PropertyLinkSub` and attachment assignments the mapper rejects, use `run_script` with the exact FreeCAD property type.

## FreeCAD 1.1 note

FreeCAD 1.1 changed PartDesign Body Origin datum orientation. Older files may need conversion when opened, and 1.1-created files may not be safe to reopen in older FreeCAD versions. Keep the target version explicit in the model handoff.

## Sources

- [Placement](https://wiki.freecad.org/Placement)
- [Placement API](https://wiki.freecad.org/Placement_API)
- [Part Attachment](https://wiki.freecad.org/Part_EditAttachment)
- [Attachment](https://wiki.freecad.org/Attachment)
- [PartDesign Body](https://wiki.freecad.org/PartDesign_Body)
- [Topological naming problem](https://wiki.freecad.org/Topological_naming_problem)
- [FreeCAD 1.1 release notes](https://wiki.freecad.org/Release_notes_1.1)
