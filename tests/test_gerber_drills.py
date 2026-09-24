import os
import tempfile
import unittest

import pcbnew
from pcb_tools import gerber

from gerber import convert_to_kicad, excellon_tool_functions


class GerberDrillTests(unittest.TestCase):
    def test_reads_xnc_aperture_functions(self):
        with open("samples/gerber/export/gerber-PTH.drl") as source:
            drill = gerber.loads(source.read())

        self.assertEqual(excellon_tool_functions(drill), {
            1: "ViaDrill",
            2: "ComponentDrill",
            3: "ComponentDrill",
            4: "ComponentDrill",
        })

    def test_component_drills_become_mask_opening_pth_pads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "converted.kicad_pcb")
            errors = convert_to_kicad(
                "samples/gerber/export", output, required_edge_cuts=False)
            board = pcbnew.LoadBoard(output)

        vias = [
            track for track in board.GetTracks()
            if isinstance(track, pcbnew.PCB_VIA)
        ]
        pads = [
            pad for footprint in board.GetFootprints()
            for pad in footprint.Pads()
        ]
        pth_pads = [
            pad for pad in pads
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
        ]
        pth_footprints = [
            footprint for footprint in board.GetFootprints()
            if any(
                pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                for pad in footprint.Pads()
            )
        ]
        npth_pads = [
            pad for pad in pads
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
        ]
        npth_footprints = [
            footprint for footprint in board.GetFootprints()
            if any(
                pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
                for pad in footprint.Pads()
            )
        ]

        self.assertEqual(errors, [])
        self.assertEqual(len(vias), 2)
        self.assertEqual(len(pth_pads), 17)
        self.assertEqual(len(pth_footprints), 1)
        self.assertEqual(pth_footprints[0].GetFPIDAsString(), "PTH")
        self.assertTrue(pth_footprints[0].IsExcludedFromPosFiles())
        self.assertTrue(pth_footprints[0].IsExcludedFromBOM())
        self.assertEqual(len(npth_pads), 7)
        self.assertEqual(len(npth_footprints), 7)
        self.assertTrue(all(
            footprint.IsExcludedFromPosFiles()
            and footprint.IsExcludedFromBOM()
            for footprint in npth_footprints
        ))
        self.assertTrue(all(
            pad.GetLayerSet().Contains(pcbnew.F_Mask)
            and pad.GetLayerSet().Contains(pcbnew.B_Mask)
            for pad in pth_pads
        ))
        self.assertTrue(all(
            pad.GetSize().x == pad.GetDrillSize().x + 1
            and pad.GetSize().y == pad.GetDrillSize().y + 1
            for pad in pth_pads
        ))


if __name__ == "__main__":
    unittest.main()
