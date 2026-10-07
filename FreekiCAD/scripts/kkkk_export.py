"""Convert a FreekiCAD assembly or KiCad PCB to STEP or STL."""

import sys
from pathlib import Path


# The script is also bundled outside FreeCAD's normal Addon Manager paths.
# Put its FreekiCAD root on sys.path so the adjacent ``freecad`` package is
# importable in both a source checkout and the packaged Kikakuka application.
addon_root = str(Path(__file__).resolve().parents[1])
if addon_root not in sys.path:
    sys.path.insert(0, addon_root)

from freecad.FreekiCAD.HeadlessExport import main


raise SystemExit(main())
