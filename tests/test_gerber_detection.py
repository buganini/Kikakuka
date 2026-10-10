import os
import tempfile
import unittest
from pathlib import Path

import pcbnew

from kikakuka.gerber import (
    convert_to_kicad,
    find_CPL,
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
    def test_smt_xlsx_is_recognized_as_cpl(self):
        self.assertEqual(
            find_CPL(["job/P7301_SMT.xlsx"]),
            "job/P7301_SMT.xlsx",
        )

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

    def test_xlsx_cpl_without_bom_creates_reference_only_footprints(self):
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "unclassified.gbr").touch()
            cpl_path = Path(temp_dir, "P7301_SMT.xlsx")
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(
                ["位置", "num", "Center-X", "Center-Y", "角度", "面向"])
            sheet.append(["R1", 1, 1.25, 2.5, 90, "T"])
            sheet.append([None, None, None, None, None, None])
            sheet.append(["C1", 2, 3.75, 4.5, 180, "B"])
            workbook.save(cpl_path)
            workbook.close()

            output = os.path.join(temp_dir, "board.kicad_pcb")
            errors = convert_to_kicad(
                temp_dir, output, required_edge_cuts=False)
            board = pcbnew.LoadBoard(output)
            footprints = {
                footprint.GetReference(): footprint
                for footprint in board.GetFootprints()
            }

        self.assertEqual(errors, [])
        self.assertEqual(set(footprints), {"R1", "C1"})
        self.assertEqual(footprints["R1"].GetValue(), "")
        self.assertEqual(footprints["C1"].GetValue(), "")
        self.assertEqual(footprints["R1"].GetFPIDAsString(), "")
        self.assertEqual(list(footprints["R1"].Pads()), [])
        self.assertEqual(board.GetLayerName(footprints["R1"].GetLayer()), "F.Cu")
        self.assertEqual(board.GetLayerName(footprints["C1"].GetLayer()), "B.Cu")
        self.assertEqual(
            footprints["R1"].GetPosition(),
            pcbnew.VECTOR2I(1250000, -2500000),
        )

    def test_xlsx_cpl_invalid_nonempty_row_reports_and_continues(self):
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "unclassified.gbr").touch()
            cpl_path = Path(temp_dir, "placements.xlsx")
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(
                ["Designator", "Mid X", "Mid Y", "Rotation", "Layer"])
            sheet.append(["R1", 1.25, 2.5, 90, "T"])
            sheet.append(["C1", None, 4.5, 180, "T"])
            sheet.append(["R2", 5.25, 6.5, 0, "B"])
            workbook.save(cpl_path)
            workbook.close()

            output = os.path.join(temp_dir, "board.kicad_pcb")
            errors = convert_to_kicad(
                temp_dir, output, required_edge_cuts=False,
                cpl_file=str(cpl_path))
            board = pcbnew.LoadBoard(output)

        self.assertEqual(errors, [
            "CPL row 3 is missing required values: x",
        ])
        self.assertEqual(
            {footprint.GetReference() for footprint in board.GetFootprints()},
            {"R1", "R2"},
        )

    def test_blank_bom_and_cpl_rows_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "unclassified.gbr").touch()
            bom_path = Path(temp_dir, "bom.csv")
            bom_path.write_text(
                "Designator,Comment,Footprint\n"
                "R1,10k,Resistor_SMD:R_0603\n"
                ",,\n"
                "C1,1u,Capacitor_SMD:C_0603\n",
                encoding="utf-8",
            )
            cpl_path = Path(temp_dir, "cpl.csv")
            cpl_path.write_text(
                "Designator,Mid X,Mid Y,Rotation,Layer\n"
                "R1,1,2,0,top\n"
                ",,,,\n"
                "C1,3,4,90,bottom\n",
                encoding="utf-8",
            )

            output = os.path.join(temp_dir, "board.kicad_pcb")
            errors = convert_to_kicad(
                temp_dir, output, required_edge_cuts=False,
                bom_file=str(bom_path), cpl_file=str(cpl_path))
            board = pcbnew.LoadBoard(output)
            footprints = {
                footprint.GetReference(): footprint
                for footprint in board.GetFootprints()
            }

        self.assertEqual(errors, [])
        self.assertEqual(set(footprints), {"R1", "C1"})
        self.assertEqual(footprints["R1"].GetValue(), "10k")
        self.assertEqual(footprints["C1"].GetValue(), "1u")


if __name__ == "__main__":
    unittest.main()
