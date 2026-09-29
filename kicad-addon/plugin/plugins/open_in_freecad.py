#!/usr/bin/env python3
"""KiCad action for updating or opening the current PCB in FreeCAD."""

import os
import sys

from kipy import KiCad

from im.freecad_open import open_board
from im.kicad_compat import get_kicad_compat


def main():
    try:
        kicad = KiCad()
        board = kicad.get_board()
        compatibility = get_kicad_compat(kicad.get_version())
        filepath = compatibility.board_path(board, os.path.abspath)
        pid = open_board(filepath, os.environ.get("KICAD_API_SOCKET", ""))
    except Exception as error:
        print(f"Kikakuka Open in FreeCAD: {error}", file=sys.stderr)
        return 1
    print("Kikakuka: Open in FreeCAD is already running." if pid is None
          else f"Kikakuka: opened PCB in FreeCAD (PID {pid}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
