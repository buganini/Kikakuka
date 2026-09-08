import importlib
import sys
import types
import unittest
from unittest import mock


class _BoardLayer:
    names = {
        1: "BL_F_Mask",
        2: "BL_F_Cu",
        3: "BL_Dielectric",
        4: "BL_B_Cu",
        5: "BL_B_Mask",
    }

    @classmethod
    def Name(cls, value):
        return cls.names[value]


class _Color:
    def __init__(self, red, green, blue, alpha=1.0):
        self.red = red
        self.green = green
        self.blue = blue
        self.alpha = alpha


class _StackEntry:
    def __init__(self, layer, thickness, color=None, dielectric=None):
        self.layer = layer
        self.thickness = thickness
        self.color = color
        self.dielectric = dielectric


class MaskTests(unittest.TestCase):
    def _import_mask(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_part = types.ModuleType("Part")
        module_name = "FreekiCAD.FreekiCAD.Mask"
        self.addCleanup(sys.modules.pop, module_name, None)
        with mock.patch.dict(
            sys.modules, {"FreeCAD": fake_freecad, "Part": fake_part}
        ):
            sys.modules.pop(module_name, None)
            return importlib.import_module(module_name)

    def test_substrate_uses_first_configured_dielectric_color(self):
        mask = self._import_mask()
        stackup = types.SimpleNamespace(layers=[
            _StackEntry(2, 35_000),
            _StackEntry(3, 200_000, _Color(32, 64, 128, 255), object()),
        ])

        self.assertEqual(mask.substrate_color(stackup),
                         (32 / 255, 64 / 255, 128 / 255))

    def test_substrate_falls_back_to_translucent_white_color(self):
        mask = self._import_mask()
        stackup = types.SimpleNamespace(layers=[
            _StackEntry(3, 200_000, _Color(128, 128, 128, 255), object()),
        ])

        self.assertEqual(mask.substrate_color(stackup),
                         mask.DEFAULT_SUBSTRATE_COLOR)

    def test_substrate_uses_kicad_alpha_as_freecad_transparency(self):
        mask = self._import_mask()
        stackup = types.SimpleNamespace(layers=[
            _StackEntry(
                3, 200_000,
                _Color(205 / 255, 130 / 255, 0, 0.68), object()),
        ])

        color, transparency = mask.substrate_appearance(stackup)

        self.assertEqual(color, (205 / 255, 130 / 255, 0))
        # KiCad boosts the accumulated body alpha after adding each
        # dielectric: 0.68 + (1 - 0.68) * 0.68 / 2 = 0.7888.
        self.assertEqual(transparency, 45)

    def test_substrate_combines_dielectrics_like_kicad(self):
        mask = self._import_mask()
        stackup = types.SimpleNamespace(layers=[
            _StackEntry(3, 100_000, _Color(1.0, 0.0, 0.0, 0.5), object()),
            _StackEntry(3, 100_000, _Color(0.0, 0.0, 1.0, 0.5), object()),
        ])

        color, transparency = mask.substrate_appearance(stackup)

        self.assertEqual(color, (0.5, 0.0, 0.5))
        self.assertEqual(transparency, 50)

    def test_substrate_fallback_uses_kicad_board_body_opacity(self):
        mask = self._import_mask()
        stackup = types.SimpleNamespace(layers=[])

        _color, transparency = mask.substrate_appearance(stackup)

        self.assertEqual(transparency, 37)

    def test_mask_layers_are_twenty_um_outside_copper_display(self):
        mask = self._import_mask()
        stackup = types.SimpleNamespace(layers=[
            _StackEntry(1, 10_000, _Color(0, 128, 0, 255)),
            _StackEntry(2, 35_000),
            _StackEntry(3, 200_000, dielectric=object()),
            _StackEntry(4, 35_000),
            _StackEntry(5, 10_000, _Color(0, 128, 0, 255)),
        ])

        layers = mask.mask_stackup_layers(stackup, _BoardLayer)

        self.assertEqual([layer.name for layer in layers],
                         ["F.Mask", "B.Mask"])
        self.assertAlmostEqual(layers[0].z, 0.330)
        self.assertAlmostEqual(layers[1].z, -0.040)
        self.assertAlmostEqual(layers[0].thickness, 0.010)
        self.assertEqual(layers[0].color, (0, 128 / 255, 0))
        self.assertEqual(layers[0].transparency, 30)

    def test_opening_reader_uses_kicad_final_layer_polygons(self):
        mask = self._import_mask()
        polygon = object()
        board = types.SimpleNamespace(
            get_pad_shapes_as_polygons=mock.Mock(
                return_value=[polygon, None]))
        items = [object(), object()]

        result = mask.read_mask_opening_polygons(board, items, 1)

        self.assertEqual(result, [polygon])
        board.get_pad_shapes_as_polygons.assert_called_once_with(items, 1)

    def test_pad_is_only_opened_on_declared_mask_layer(self):
        mask = self._import_mask()
        front_pad = types.SimpleNamespace(
            padstack=types.SimpleNamespace(layers=[1, 2]))

        self.assertTrue(mask.padstack_item_exists_on_layer(front_pad, 1))
        self.assertFalse(mask.padstack_item_exists_on_layer(front_pad, 5))

    def test_unreadable_padstack_does_not_create_fallback_opening(self):
        mask = self._import_mask()

        self.assertFalse(mask.padstack_item_exists_on_layer(object(), 1))


if __name__ == "__main__":
    unittest.main()
