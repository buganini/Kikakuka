import importlib
import sys
import types
import unittest
from unittest import mock


class _BoardLayer:
    names = {
        1: "BL_F_Mask",
        2: "BL_F_Cu",
        3: "BL_UNDEFINED",
        4: "BL_In1_Cu",
        5: "BL_B_Cu",
        6: "BL_B_Mask",
        7: "BL_User_4",
    }

    @classmethod
    def Name(cls, value):
        return cls.names[value]


class _StackEntry:
    def __init__(self, layer, thickness, enabled=True):
        self.layer = layer
        self.thickness = thickness
        self.enabled = enabled


class _Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = x
        self.y = y
        self.z = z

    def distanceToPoint(self, other):
        return ((self.x - other.x) ** 2
                + (self.y - other.y) ** 2
                + (self.z - other.z) ** 2) ** 0.5


class _Arc:
    def __init__(self, start, mid, end):
        self.points = (start, mid, end)

    def toShape(self):
        return ("arc", self.points)


class CopperStackupTests(unittest.TestCase):
    def _import_copper(self, with_geometry=False):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_part = types.ModuleType("Part")
        if with_geometry:
            fake_freecad.Vector = _Vector
            fake_part.makeLine = lambda start, end: ("line", start, end)
            fake_part.Arc = _Arc
            fake_part.Wire = lambda edges: list(edges)
            fake_part.Face = lambda wire, *args: ("face", wire)
            fake_part.makeCompound = mock.Mock(
                side_effect=AssertionError("compound geometry is not expected"))
        module_name = "FreekiCAD.FreekiCAD.Copper"
        self.addCleanup(sys.modules.pop, module_name, None)
        with mock.patch.dict(
            sys.modules, {"FreeCAD": fake_freecad, "Part": fake_part}
        ):
            sys.modules.pop(module_name, None)
            return importlib.import_module(module_name)

    def test_all_enabled_copper_layers_keep_stackup_order_and_z(self):
        copper = self._import_copper()
        stackup = types.SimpleNamespace(layers=[
            _StackEntry(1, 10_000),
            _StackEntry(2, 35_000),
            _StackEntry(3, 700_000),
            _StackEntry(4, 18_000),
            _StackEntry(3, 800_000),
            _StackEntry(5, 35_000),
            _StackEntry(6, 10_000),
        ])

        layers = copper.copper_stackup_layers(stackup, _BoardLayer)

        self.assertEqual([layer.name for layer in layers],
                         ["F.Cu", "In1.Cu", "B.Cu"])
        self.assertAlmostEqual(layers[0].z, 1.628)
        self.assertAlmostEqual(layers[1].z, 0.854)
        self.assertAlmostEqual(layers[2].z, -0.020)
        self.assertAlmostEqual(layers[1].thickness, 0.018)

    def test_disabled_inner_layer_is_not_imported(self):
        copper = self._import_copper()
        stackup = types.SimpleNamespace(layers=[
            _StackEntry(2, 35_000),
            _StackEntry(4, 0, enabled=False),
            _StackEntry(5, 0),
        ])

        layers = copper.copper_stackup_layers(stackup, _BoardLayer)

        self.assertEqual([layer.name for layer in layers], ["F.Cu", "B.Cu"])
        self.assertEqual(layers[1].thickness,
                         copper.DEFAULT_COPPER_THICKNESS_MM)

    def test_non_copper_layer_is_rejected(self):
        copper = self._import_copper()

        self.assertFalse(copper.is_copper_layer(_BoardLayer, 7))
        self.assertTrue(copper.is_copper_layer(_BoardLayer, 4))

    def test_normal_padstack_descriptor_expands_to_all_target_layers(self):
        copper = self._import_copper()
        common = types.SimpleNamespace(layer=2, shape="circle")
        padstack = types.SimpleNamespace(
            layers=[2, 4, 5], copper_layers=[common])

        expanded = list(copper.expanded_padstack_layers(
            padstack, [2, 4, 5]))

        self.assertEqual([layer for layer, _shape in expanded], [2, 4, 5])
        self.assertTrue(all(shape is common for _layer, shape in expanded))

    def test_custom_padstack_keeps_per_layer_descriptors(self):
        front = types.SimpleNamespace(layer=2, shape="circle")
        inner = types.SimpleNamespace(layer=4, shape="oval")
        copper = self._import_copper()
        padstack = types.SimpleNamespace(
            layers=[2, 4], copper_layers=[front, inner])

        expanded = list(copper.expanded_padstack_layers(
            padstack, [2, 4, 5]))

        self.assertEqual(expanded, [(2, front), (4, inner)])

    def test_read_copper_items_collects_every_kind_on_f_cu(self):
        copper = self._import_copper()
        common_pad = types.SimpleNamespace(layer=2, shape="roundrect")
        common_via = types.SimpleNamespace(layer=2, shape="circle")
        pad = types.SimpleNamespace(padstack=types.SimpleNamespace(
            layers=[2, 5], copper_layers=[common_pad]))
        via = types.SimpleNamespace(padstack=types.SimpleNamespace(
            layers=[2, 4, 5], copper_layers=[common_via]))
        front_track = types.SimpleNamespace(layer=2)
        inner_track = types.SimpleNamespace(layer=4)
        front_polygon = object()
        bottom_polygon = object()
        zone = types.SimpleNamespace(
            is_rule_area=lambda: False,
            filled_polygons={2: [front_polygon], 5: [bottom_polygon]})
        rule_area = types.SimpleNamespace(
            is_rule_area=lambda: True, filled_polygons={2: [object()]})
        front_graphic = types.SimpleNamespace(layer=2)

        board = types.SimpleNamespace(
            get_tracks=lambda: [front_track, inner_track],
            get_zones=lambda: [zone, rule_area],
            get_pads=lambda: [pad],
            get_vias=lambda: [via],
        )

        items = copper.read_copper_items(
            board, [2, 4, 5], [front_graphic])

        self.assertEqual(
            [item.kind for item in items[2]],
            ["track", "zone_polygon", "pad", "via", "graphic"])
        self.assertEqual(
            [item.kind for item in items[4]], ["track", "via"])
        self.assertEqual(
            [item.kind for item in items[5]],
            ["zone_polygon", "pad", "via"])
        self.assertIs(items[2][1].source, front_polygon)
        self.assertIs(items[2][2].geometry, common_pad)

    def test_read_copper_items_reports_api_getter_failure(self):
        copper = self._import_copper()
        warnings = []

        def fail_tracks():
            raise RuntimeError("IPC failure")

        board = types.SimpleNamespace(
            get_tracks=fail_tracks,
            get_zones=lambda: [],
            get_pads=lambda: [],
            get_vias=lambda: [],
        )

        items = copper.read_copper_items(board, [2], warn=warnings.append)

        self.assertEqual(items, {2: []})
        self.assertIn("Could not read copper tracks", warnings[0])

    def test_capsule_is_one_closed_face_without_overlapping_primitives(self):
        copper = self._import_copper(with_geometry=True)

        shape = copper._capsule(_Vector(0, 0), _Vector(4, 0), 2)

        self.assertEqual(shape[0], "face")
        self.assertEqual([edge[0] for edge in shape[1]],
                         ["line", "arc", "line", "arc"])

    def test_roundrect_is_one_closed_face_without_internal_edges(self):
        copper = self._import_copper(with_geometry=True)

        shape = copper._rounded_rect_face(4, 3, 0.5)

        self.assertEqual(shape[0], "face")
        self.assertEqual(len(shape[1]), 8)
        self.assertEqual(sum(edge[0] == "arc" for edge in shape[1]), 4)

    def test_maximum_roundrect_radius_skips_zero_length_sides(self):
        copper = self._import_copper(with_geometry=True)

        shape = copper._rounded_rect_face(2, 2, 1)

        self.assertEqual(shape[0], "face")
        self.assertEqual(len(shape[1]), 4)
        self.assertTrue(all(edge[0] == "arc" for edge in shape[1]))

if __name__ == "__main__":
    unittest.main()
