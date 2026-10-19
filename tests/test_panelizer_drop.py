import ast
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import traceback
import unittest
from unittest.mock import Mock


class PanelizerDropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'kikakuka/panelizer.py'
        tree = ast.parse(source.read_text())
        ui = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                  and n.name == 'PanelizerUI')
        names = {'handleDrop', 'addFile', '_addPCB', 'fromCanvas', 'toCanvas'}
        methods = [n for n in ui.body if isinstance(n, ast.FunctionDef)
                   and n.name in names]
        namespace = dict(os=os, traceback=traceback, PNL_SUFFIXES=('.kkkk_pnl',),
                         PCB_SUFFIX='.kicad_pcb', is_gerber_dir=lambda p: False,
                         is_gerber_zip=lambda p: False,
                         is_gerber_file=lambda p: False,
                         Critical=Mock())
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), 'exec'), namespace)
        cls.UI = type('UI', (), {name: namespace[name] for name in names})

    def drop(self, x, y, scale=(100, -40, 0.00002)):
        ui = self.UI()
        ui.unit = 1000000
        ui.off_x, ui.off_y = 50000000, 70000000
        ui.state = SimpleNamespace(scale=scale, pcb=[], frame_top=5, spacing=2)
        pcb = SimpleNamespace(x=0, y=0, pcb_file=SimpleNamespace(subboards=1))
        ui.makePanelCell = Mock(return_value=pcb)
        ui.build = Mock()
        # Loading a PCB must not change the transform used to restore the drop.
        def make_cell(*args):
            ui.state.scale = (0, 0, 1)
            return pcb
        ui.makePanelCell.side_effect = make_cell
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'board.kicad_pcb'
            path.touch()
            event = SimpleNamespace(files=[SimpleNamespace(path=str(path))],
                                    x=x, y=y, accept=Mock(), reject=Mock())
            self.assertTrue(ui.handleDrop(event))
            event.accept.assert_called_once()
            event.reject.assert_not_called()
        self.assertEqual(ui.state.pcb, [pcb])
        ui.build.assert_called_once()
        return pcb

    def test_restores_zoom_and_pan_in_internal_units(self):
        pcb = self.drop(300, 160)
        self.assertEqual((pcb.x, pcb.y), (10000000, 10000000))

    def test_single_zero_axis_is_a_valid_position(self):
        pcb = self.drop(0, 160)
        self.assertEqual((pcb.x, pcb.y), (-5000000, 10000000))

    def test_origin_or_missing_coordinates_keep_automatic_placement(self):
        for x, y in [(0, 0), (None, None), (None, 100)]:
            with self.subTest(x=x, y=y):
                pcb = self.drop(x, y)
                self.assertEqual((pcb.x, pcb.y), (0, 7000000))

    def test_uninitialized_canvas_keeps_automatic_placement(self):
        pcb = self.drop(300, 160, scale=None)
        self.assertEqual((pcb.x, pcb.y), (0, 7000000))


if __name__ == '__main__':
    unittest.main()
