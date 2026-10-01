import os
import tempfile
import unittest
from pathlib import Path

import pcbnew

from gerber import (
    convert_to_kicad,
    find_cu_bottom,
    find_cu_inner,
    find_cu_top,
    find_mask_bottom,
    find_mask_top,
    find_paste_bottom,
    find_paste_top,
    find_silk_bottom,
    find_silk_top,
    is_gerber_dir,
    is_gerber_file,
)


class GerberDetectionTests(unittest.TestCase):
    def test_gbx_is_recognized_as_gerber(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            filename = os.path.join(temp_dir, "copper.GBX")
            Path(filename).touch()

            self.assertTrue(is_gerber_file(filename))
            self.assertTrue(is_gerber_dir(temp_dir))

    def test_cam350_gbx_four_layer_stackup(self):
        filenames = [
            "job/P7301L1.GBX",
            "job/P7301L1LQ.GBX",
            "job/P7301L1P.GBX",
            "job/P7301L1T.GBX",
            "job/P7301L2.GBX",
            "job/P7301L3.GBX",
            "job/P7301L4.GBX",
            "job/P7301L4LQ.GBX",
            "job/P7301L4P.GBX",
            "job/P7301L4T.GBX",
        ]

        self.assertEqual(find_cu_top(filenames), "job/P7301L1.GBX")
        self.assertEqual(find_cu_inner(filenames, 1), "job/P7301L2.GBX")
        self.assertEqual(find_cu_inner(filenames, 2), "job/P7301L3.GBX")
        self.assertIsNone(find_cu_inner(filenames, 3))
        self.assertEqual(find_cu_bottom(filenames), "job/P7301L4.GBX")
        self.assertEqual(find_mask_top(filenames), "job/P7301L1LQ.GBX")
        self.assertEqual(find_mask_bottom(filenames), "job/P7301L4LQ.GBX")
        self.assertEqual(find_silk_top(filenames), "job/P7301L1T.GBX")
        self.assertEqual(find_silk_bottom(filenames), "job/P7301L4T.GBX")
        self.assertEqual(find_paste_top(filenames), "job/P7301L1P.GBX")
        self.assertEqual(find_paste_bottom(filenames), "job/P7301L4P.GBX")

    def test_cam350_gbx_detection_is_case_insensitive(self):
        filenames = ["boardl1.gbx", "boardl2.gbx"]

        self.assertEqual(find_cu_top(filenames), "boardl1.gbx")
        self.assertEqual(find_cu_bottom(filenames), "boardl2.gbx")

    def test_single_gbx_filename_does_not_imply_a_stackup(self):
        filenames = ["MODEL1.GBX"]

        self.assertIsNone(find_cu_top(filenames))
        self.assertIsNone(find_cu_bottom(filenames))

    def test_conversion_keeps_complete_stackup_during_detection(self):
        gerber_data = """G04 test layer *
%FSLAX24Y24*%
%MOMM*%
%ADD10C,0.1000*%
D10*
X000000Y000000D02*
X010000Y000000D01*
M02*
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            for number in range(1, 5):
                Path(temp_dir, f"boardL{number}.GBX").write_text(
                    gerber_data, encoding="ascii")
            output = os.path.join(temp_dir, "board.kicad_pcb")

            errors = convert_to_kicad(
                temp_dir, output, required_edge_cuts=False)
            board = pcbnew.LoadBoard(output)

        self.assertEqual(errors, [])
        self.assertEqual(board.GetCopperLayerCount(), 4)
        self.assertEqual(
            {board.GetLayerName(item.GetLayer()) for item in board.GetDrawings()},
            {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"},
        )


if __name__ == "__main__":
    unittest.main()
