# FreekiCAD

<img src="Resources/icons/FreekiCAD.png" alt="Logo" width="64" height="64">

FreekiCAD bridges KiCad and FreeCAD, providing a FreeCAD-based workflow for
PCB editing and mechanical assembly.

## Features

- Importing KiCad PCB files
- Editing board outlines
- Bending flexible PCBs
- Assembling multiple PCBs and STEP models
- Importing and exporting `.kkkk_asm` assembly files

The `.kkkk_asm` format stores only file paths and placement information. STEP
models are referenced rather than embedded, so imported models remain
reloadable. This also makes FreekiCAD useful for assembling multiple STEP models
without KiCad.

## Upstream

This repository is a release mirror. Development takes place in the
[FreekiCAD directory of the Kikakuka repository][upstream].

[upstream]: https://github.com/buganini/Kikakuka/tree/main/FreekiCAD
