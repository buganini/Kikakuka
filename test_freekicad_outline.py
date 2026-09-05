import importlib
import sys
import types
import unittest
from unittest import mock


class _FakeFace:
    def __init__(self, wire):
        self.Area = wire.area


class _FakeWire:
    def __init__(self, area):
        self.area = area


class _CurrentKipyCircle:
    """BoardCircle API shape used by current kicad-python/kipy."""

    def radius(self):
        return 20_000_000


class OutlineWireOrderTests(unittest.TestCase):
    def test_largest_profile_is_selected_before_hole(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_part = types.ModuleType("Part")
        fake_part.Face = _FakeFace

        module_name = "FreekiCAD.FreekiCAD.LinkedObject"
        self.addCleanup(sys.modules.pop, module_name, None)
        with mock.patch.dict(
            sys.modules,
            {"FreeCAD": fake_freecad, "Part": fake_part},
        ):
            sys.modules.pop(module_name, None)
            linked_object = importlib.import_module(module_name)

        # bulb-1ch.kicad_pcb declares its 15 x 4.35 mm cutout before its
        # radius-20 mm circular outline.
        cutout = _FakeWire(15.0 * 4.35)
        outer_circle = _FakeWire(3.141592653589793 * 20.0 * 20.0)

        order = linked_object._outline_wire_order([cutout, outer_circle])

        self.assertEqual(order, [1, 0])

    def test_board_circle_uses_radius_method_without_end_attribute(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_part = types.ModuleType("Part")
        module_name = "FreekiCAD.FreekiCAD.LinkedObject"
        self.addCleanup(sys.modules.pop, module_name, None)

        with mock.patch.dict(
            sys.modules,
            {"FreeCAD": fake_freecad, "Part": fake_part},
        ):
            sys.modules.pop(module_name, None)
            linked_object = importlib.import_module(module_name)

        self.assertEqual(
            linked_object._board_circle_radius_mm(_CurrentKipyCircle()),
            20.0,
        )


if __name__ == "__main__":
    unittest.main()
