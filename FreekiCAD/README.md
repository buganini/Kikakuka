# FreekiCAD

<img src="freecad/FreekiCAD/resources/icons/FreekiCAD.png" alt="Logo" width="64" height="64">

FreekiCAD bridges KiCad and FreeCAD, providing a FreeCAD-based workflow for
PCB editing and mechanical assembly.

## Features

- Linking external KiCad `.kicad_pcb` files
- Editing board outlines
- Importing solid stiffeners from annotated `F.Stiffener` and `B.Stiffener` layers
- Bending flexible PCBs from a KiCad user layer named `FreekiCAD`
- Assembling multiple linked PCBs and STEP models
- Importing and exporting `.kkkk_asm` assembly files

Both `.kicad_pcb` boards and STEP models remain linked to their external source
files. A FreeCAD document caches the generated objects and geometry, while
retaining each source path so the linked object can be reloaded. `AutoReload`
is enabled by default for both object types and can be disabled independently
on each linked object.

The `.kkkk_asm` format therefore stores the linked source paths, object settings,
and placements rather than the cached objects or generated geometry. This also
makes FreekiCAD useful for assembling multiple STEP models without KiCad.

The [FPC assembly example][fpc-assembly-example] shows a `.kkkk_asm` manifest
containing linked KiCad PCB files.

## Manual Installation

FreekiCAD requires FreeCAD 1.0 or later. Importing STEP files and working with
`.kkkk_asm` assemblies that contain only STEP objects require no additional
Python dependencies. KiCad PCB integration requires KiCad 9.0 or later plus
`kicad-python` and `shapely`.

After copying the `FreekiCAD` folder into FreeCAD's `Mod` folder, open **View >
Panels > Python Console** and paste this single line if you need KiCad
integration. It waits for pip to finish, then prints its output:

```python
import subprocess,os,sys; print(subprocess.run([os.path.join(os.path.dirname(sys.executable),"python"),"-m","pip","install","kicad-python>=0.8,<0.9","shapely"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True).stdout)
```

Restart FreeCAD after installation.

## Headless Assembly Export

Use `kkkk_export.py` with FreeCAD's command-line executable to convert a
`.kkkk_asm` assembly to STEP without opening the FreeCAD GUI:

```text
freecadcmd kkkk_export.py input.kkkk_asm output.step
```

The exporter synchronously loads every object and component model before it
writes the STEP file. Assemblies containing only STEP objects need no extra
Python dependencies. For assemblies containing KiCad PCB objects, install
`kicad-python` and `shapely` and keep the Kikakuka Workspace Manager running;
it resolves or starts the matching KiCad instance and waits for its IPC API.

Run the command from the directory containing `kkkk_export.py`. The actual
FreeCAD user-data directory can be printed from FreeCAD's Python console with
`print(App.getUserAppDataDir())`.

### macOS

```sh
"/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd" kkkk_export.py input.kkkk_asm output.step
```

### Windows PowerShell

```powershell
& "C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe" kkkk_export.py input.kkkk_asm output.step
```

For `cmd.exe`, use the same command without the leading `&`.

### Linux

```sh
/usr/bin/freecadcmd kkkk_export.py input.kkkk_asm output.step
```

When `freecadcmd` is already in `PATH`, its full path can be omitted. For an
AppImage, use its `--console` mode:

```sh
./FreeCAD_1.1-Linux-x86_64.AppImage --console kkkk_export.py input.kkkk_asm output.step
```

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

Stiffener thickness is additive outside the finished PCB; it is not included
in the board thickness and is never subtracted from the board body.

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
