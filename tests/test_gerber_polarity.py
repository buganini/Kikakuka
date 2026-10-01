import unittest

import pcbnew
from pcb_tools import gerber

from gerber import populate_kicad


class GerberPolarityTests(unittest.TestCase):
    def test_clear_region_becomes_polygon_hole(self):
        source = """G04 dark square with clear center *
%FSLAX24Y24*%
%MOMM*%
%ADD10C,0.1000*%
D10*
%LPD*%
G36*
X000000Y000000D02*
X100000Y000000D01*
X100000Y100000D01*
X000000Y100000D01*
X000000Y000000D01*
G37*
%LPC*%
G36*
X040000Y040000D02*
X060000Y040000D01*
X060000Y060000D01*
X040000Y060000D01*
X040000Y040000D01*
G37*
M02*
"""
        board = pcbnew.BOARD()
        errors = []

        populate_kicad(
            board, gerber.loads(source), pcbnew.F_Cu, errors)

        drawings = list(board.GetDrawings())
        self.assertEqual(errors, [])
        self.assertEqual(len(drawings), 1)
        self.assertEqual(drawings[0].GetShape(), pcbnew.SHAPE_T_POLY)
        self.assertEqual(drawings[0].GetPolyShape().OutlineCount(), 1)
        # PCB_SHAPE fractures holes into its outline, so verify the resulting
        # 10 x 10 mm square has the 2 x 2 mm clear region subtracted.
        self.assertEqual(
            drawings[0].GetPolyShape().Area(),
            96 * pcbnew.PCB_IU_PER_MM ** 2,
        )


if __name__ == "__main__":
    unittest.main()
