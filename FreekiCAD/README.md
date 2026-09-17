# FreekiCAD

<img src="freecad/FreekiCAD/resources/icons/FreekiCAD.png" alt="Logo" width="64" height="64">

FreekiCAD bridges KiCad and FreeCAD, providing a FreeCAD-based workflow for
PCB editing and mechanical assembly.

## Features

- Importing KiCad PCB files
- Editing board outlines
- Importing solid stiffeners from annotated `F.Stiffener` and `B.Stiffener` layers
- Bending flexible PCBs from a KiCad user layer named `FreekiCAD`
- Assembling multiple PCBs and STEP models
- Importing and exporting `.kkkk_asm` assembly files

The `.kkkk_asm` format stores only file paths and placement information. STEP
models are referenced rather than embedded, so imported models remain
reloadable. This also makes FreekiCAD useful for assembling multiple STEP models
without KiCad. See the [FPC assembly example][fpc-assembly-example] for a
complete `.kkkk_asm` file.

## Flexible PCB Stiffener

Rename KiCad user layers to `F.Stiffener` and/or `B.Stiffener`
(case-insensitive). Each closed rectangle, circle, polygon, or connected
line/arc outline is imported as one stiffener area, regardless of its KiCad
display-fill setting. Put one text annotation inside each area and separate
properties with `/` or newlines:

```text
Name=Tail reinforcement
Material=Polyimide
Color=#C87518
Opacity=0.65
Thickness=250 um
```

`Material` and `Thickness` are required. `Name`, `Color`, and `Opacity` are
optional and property names are case-insensitive. A non-empty `Name` becomes
the suffix of the FreeCAD child name and label (`F_Stiffener_<Name>` or
`B_Stiffener_<Name>`) and is included in warning messages. Thickness accepts
`mm`, `in`, `mil` (`0.001 in`), and `um`; a unitless value is interpreted as
millimetres.

| Material | Default color | Default opacity |
| --- | --- | ---: |
| `FR4` | `#C8B45A` | `0.90` |
| `Polyimide` | `#C87518` | `0.65` |
| `Stainless_Steel` | `#A7ADB4` | `1.00` |
| `3M468` | `#F2E3BD` | `0.28` |
| `tesa8854` | `#EEE8D8` | `0.45` |
| `3M9077` | `#DDE8EE` | `0.30` |

Front stiffeners extend outward beyond the front silkscreen plane; back
stiffeners extend outward beyond the back silkscreen plane. Pad, via, and
explicit mask-graphic openings on `F.Mask` are cut through front stiffeners,
while `B.Mask` openings are cut through back stiffeners. This still applies
when `ImportSolderMask` is disabled.

Stiffeners remain rigid during flexible-PCB bending. If a stiffener overlaps a
full bend band, FreekiCAD prints a warning containing its name, the bend name,
and the overlap area. If an area contains multiple property-text objects, it is
reported as an error and skipped. Invalid annotations and annotations outside
an area are also errors; unannotated areas are skipped with a warning. The
[FPC sample board][fpc-sample] contains a complete stiffener example.

## Flexible PCB Bending

Rename an unused KiCad user layer to `FreekiCAD` (case-insensitive). This is
the layer name used for flexible-PCB bending. Draw each bend as a line segment
on that layer, then add parameter text on the same layer within `0.1 mm` of
either endpoint, for example:

```text
a=-70 r=0.5
```

Alternatively, specify the complete bend span instead of the radius:

```text
a=-70 s=0.61
```

- `a` is the bend angle in degrees.
- `r` is the bend radius in millimetres.
- `s` is the full bend span in millimetres; when `r` is omitted, FreekiCAD
  derives the radius from `s`, the angle, and the KiCad stackup thickness.

Each imported bend line becomes a FreeCAD child object with editable `Angle`,
`Radius`, and `Active` properties. A line without parameter text still loads
and can be configured in FreeCAD. Use the linked PCB object's `EnableBending`
property to toggle all deformation.

The board solid, components, copper, solder mask, silkscreen, and coupler
markers follow the resulting bend geometry. Stiffeners stay rigid and generate
a warning when they overlap a bend band. The [FPC sample board][fpc-sample]
contains both bending and stiffener examples.

## Upstream

This repository is a release mirror. Development takes place in the
[FreekiCAD directory of the Kikakuka repository][upstream].

[upstream]: https://github.com/buganini/Kikakuka/tree/main/FreekiCAD
[fpc-assembly-example]: https://github.com/buganini/Kikakuka/blob/main/samples/fpc-assembly.kkkk_asm
[fpc-sample]: https://github.com/buganini/Kikakuka/blob/main/samples/fpc.kicad_pcb
