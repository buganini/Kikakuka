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


class _SurfaceFace:
    def __init__(self, name, area, z):
        self.name = name
        self.Area = area
        self.CenterOfMass = _Vector(0, 0, z)

    def isSame(self, other):
        return self is other


class _Arc:
    def __init__(self, start, mid, end):
        self.points = (start, mid, end)

    def toShape(self):
        return ("arc", self.points)


class CopperStackupTests(unittest.TestCase):
    def _zone_polygon(self, coordinates, holes=()):
        def ring(points):
            return types.SimpleNamespace(nodes=[types.SimpleNamespace(
                point=_Vector(x * 1_000_000, y * 1_000_000)) for x, y in points])
        return types.SimpleNamespace(outline=ring(coordinates), holes=[ring(h) for h in holes])

    def test_direct_zone_preserves_explicit_holes_and_coordinate_transform(self):
        from shapely.geometry import Polygon
        copper = self._import_copper()
        zone = self._zone_polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                                  [[(2, 2), (8, 2), (8, 8), (2, 8)]])
        result = copper._straight_zone_polygon(zone)
        expected = Polygon([(0, 0), (10, 0), (10, -10), (0, -10)],
                           [[(2, -2), (8, -2), (8, -8), (2, -8)]])
        self.assertTrue(result.equals(expected))
        self.assertEqual(result.area, 64)

    def test_direct_zone_repairs_stitched_hole_without_filling_it(self):
        from shapely.geometry import Polygon
        copper = self._import_copper()
        zone = self._zone_polygon([
            (0, 0), (10, 0), (10, 10), (0, 10), (0, 0),
            (2, 2), (2, 8), (8, 8), (8, 2), (2, 2), (0, 0)])
        result = copper._straight_zone_polygon(zone)
        polygons = list(copper._polygon_geometries(result))
        self.assertTrue(result.is_valid)
        self.assertEqual(result.area, 64)
        self.assertEqual(len(polygons), 1)
        self.assertEqual(len(polygons[0].interiors), 1)

    def test_direct_zone_falls_back_for_arcs_degenerate_rings_or_missing_shapely(self):
        copper = self._import_copper()
        zone = self._zone_polygon([(0, 0), (10, 0), (0, 10)])
        zone.outline.nodes[0].has_arc = True
        self.assertIsNone(copper._straight_zone_polygon(zone))
        self.assertIsNone(copper._straight_zone_polygon(self._zone_polygon([(0, 0), (1, 0)])))
        with mock.patch.object(copper, "shapely", None):
            self.assertIsNone(copper._straight_zone_polygon(zone))

    def test_direct_zone_is_not_lost_in_brep_union_fallback(self):
        from shapely.geometry import Polygon
        copper = self._import_copper()
        zone = Polygon([(0, 0), (1, 0), (0, 1)])
        existing = mock.Mock()
        zone_shape = mock.Mock()
        result = existing.multiFuse.return_value
        result.isNull.return_value = False
        with mock.patch.object(copper.shapely, "union_all", side_effect=RuntimeError("union failed")), \
                mock.patch.object(copper, "_shape_to_polygons", return_value=[]), \
                mock.patch.object(copper, "_polygons_to_part_shape", return_value=zone_shape) as build:
            copper.union_planar_profiles([existing], seed_polygons=[(0, zone)])
        build.assert_called_once_with(zone)
        existing.multiFuse.assert_called_once_with([zone_shape])

    def test_direct_zone_union_keeps_source_order_for_precision_grid(self):
        from shapely.geometry import Polygon
        copper = self._import_copper()
        track, pad = object(), object()
        track_polygon = Polygon([(0, 0), (1, 0), (0, 1)])
        zone_polygon = Polygon([(0, 0), (2, 0), (0, 2)])
        pad_polygon = Polygon([(0, 0), (3, 0), (0, 3)])
        with mock.patch.object(copper, "_shape_to_polygons",
                               side_effect=[[track_polygon], [pad_polygon]]), \
                mock.patch.object(copper.shapely, "union_all", return_value=zone_polygon) as union, \
                mock.patch.object(copper, "_polygons_to_part_shape", return_value="face"):
            self.assertEqual(copper.union_planar_profiles(
                [track, pad], seed_polygons=[(1, zone_polygon)]), "face")
        self.assertEqual(union.call_args.args[0], [track_polygon, zone_polygon, pad_polygon])

    def test_single_direct_zone_does_not_snap_coordinates_to_union_grid(self):
        from shapely.geometry import Polygon
        copper = self._import_copper()
        zone = Polygon([(0, 0), (0.001234, 0), (0, 0.001234)])
        with mock.patch.object(copper.shapely, "union_all") as union, \
                mock.patch.object(copper, "_polygons_to_part_shape", return_value="face") as build:
            self.assertEqual(copper.union_planar_profiles([], seed_polygons=[(0, zone)]), "face")
        union.assert_not_called()
        build.assert_called_once_with(zone)

    def test_straight_polyline_wire_closes_and_removes_consecutive_duplicates(self):
        copper = self._import_copper(with_geometry=True)
        copper.Part.makePolygon = mock.Mock(return_value="wire")
        polyline = types.SimpleNamespace(nodes=[
            types.SimpleNamespace(point=_Vector(x, y))
            for x, y in [(0, 0), (1_000_000, 0), (1_000_000, 0),
                         (1_000_000, 1_000_000), (0, 1_000_000)]])
        self.assertEqual(copper._polyline_wire(polyline), "wire")
        points = copper.Part.makePolygon.call_args.args[0]
        self.assertEqual([(p.x, p.y) for p in points],
                         [(0, 0), (1, 0), (1, -1), (0, -1), (0, 0)])

    def test_polyline_wire_preserves_open_and_already_closed_paths(self):
        copper = self._import_copper(with_geometry=True)
        copper.Part.makePolygon = lambda points: points
        for closed, coordinates in (
                (False, [(0, 0), (1_000_000, 0), (1_000_000, 1_000_000)]),
                (True, [(0, 0), (1_000_000, 0), (1_000_000, 1_000_000), (0, 0)])):
            with self.subTest(closed=closed):
                polyline = types.SimpleNamespace(closed=closed, nodes=[
                    types.SimpleNamespace(point=_Vector(x, y)) for x, y in coordinates])
                points = copper._polyline_wire(polyline)
                self.assertEqual(len(points), len(coordinates))

    def test_polyline_wire_keeps_arcs_as_edges(self):
        copper = self._import_copper(with_geometry=True)
        polyline = types.SimpleNamespace(nodes=[
            types.SimpleNamespace(has_arc=True), types.SimpleNamespace(has_arc=False)])
        with mock.patch.object(copper, "_polyline_edges", return_value=["arc", "line"]) as edges:
            self.assertEqual(copper._polyline_wire(polyline), ["arc", "line"])
        edges.assert_called_once_with(polyline)

    def test_degenerate_polyline_wire_returns_none(self):
        copper = self._import_copper(with_geometry=True)
        for count in (0, 1, 3):
            with self.subTest(count=count):
                polyline = types.SimpleNamespace(nodes=[
                    types.SimpleNamespace(point=_Vector()) for _ in range(count)])
                self.assertIsNone(copper._polyline_wire(polyline))

    def test_polygon_rebuild_preserves_holes_and_islands_with_reversed_rings(self):
        try:
            from shapely.geometry import MultiPolygon, Polygon
        except ImportError:
            self.skipTest("Shapely is required for planar copper union")
        copper = self._import_copper(with_geometry=True)
        if copper.shapely is None:
            self.skipTest("Shapely is required for planar copper union")

        # A clockwise exterior and counterclockwise hole must be reoriented
        # for OCC's explicit-plane constructor. The island stays a separate face.
        outer = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)],
                        [[(2, 2), (8, 2), (8, 8), (2, 8)]])
        island = Polygon([(4, 4), (4, 6), (6, 6), (6, 4)])
        plane = object()
        copper.Part.Plane = lambda: plane
        copper.Part.makePolygon = lambda points: [(p.x, p.y) for p in points]

        def make_face(surface, wires):
            self.assertIs(surface, plane)
            polygon = Polygon(wires[0], wires[1:])
            self.assertTrue(polygon.exterior.is_ccw)
            self.assertTrue(all(not ring.is_ccw for ring in polygon.interiors))
            self.assertTrue(polygon.is_valid)
            return polygon

        copper.Part.Face = make_face
        copper.Part.makeCompound = MultiPolygon
        result = copper._polygons_to_part_shape(MultiPolygon([outer, island]))
        self.assertAlmostEqual(result.area, 68.0)
        self.assertEqual(len(result.geoms), 2)
        self.assertEqual(len(result.geoms[0].interiors), 1)
        self.assertTrue(result.equals(MultiPolygon([outer, island])))

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
        module_name = "FreekiCAD.freecad.FreekiCAD.Copper"
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
        self.assertAlmostEqual(layers[0].z, 1.573)
        self.assertAlmostEqual(layers[1].z, 0.845)
        self.assertAlmostEqual(layers[2].z, 0.035)
        self.assertAlmostEqual(layers[1].thickness, 0.018)
        self.assertAlmostEqual(layers[0].direction, 0.035)
        self.assertAlmostEqual(layers[1].direction, 0.018)
        self.assertAlmostEqual(layers[2].direction, -0.035)

    def test_outer_copper_sits_inside_imported_mask_volume(self):
        copper = self._import_copper()
        stackup = types.SimpleNamespace(layers=[
            _StackEntry(1, 10_000),
            _StackEntry(2, 35_000),
            _StackEntry(3, 200_000),
            _StackEntry(5, 35_000),
            _StackEntry(6, 10_000),
        ])

        layers = copper.copper_stackup_layers(
            stackup, _BoardLayer,
            outer_offsets={"F.Cu": 0.010, "B.Cu": 0.010})

        self.assertAlmostEqual(layers[0].z, 0.245)
        self.assertAlmostEqual(layers[1].z, 0.045)

    def test_outer_stackup_thicknesses_use_physical_defaults(self):
        copper = self._import_copper()
        stackup = types.SimpleNamespace(layers=[
            _StackEntry(1, 10_000),
            _StackEntry(2, 35_000),
            _StackEntry(5, 0),
            _StackEntry(6, 0),
        ])

        result = copper.outer_stackup_thicknesses(stackup, _BoardLayer)

        self.assertEqual(result["F.Mask"], 0.010)
        self.assertEqual(result["F.Cu"], 0.035)
        self.assertEqual(result["B.Cu"], 0.035)
        self.assertEqual(result["B.Mask"], 0.010)

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

    def test_outer_shell_keeps_walls_and_end_cap_only(self):
        copper = self._import_copper()
        base = _SurfaceFace("base", 10.0, 0.0)
        end = _SurfaceFace("end", 10.0, 0.035)
        wall = _SurfaceFace("wall", 1.0, 0.0175)
        solid = types.SimpleNamespace(Faces=[base, end, wall])

        result = copper.extrusion_display_faces(
            solid, base, include_end_cap=True, include_base_cap=False)

        self.assertEqual([face.name for face in result], ["wall", "end"])

    def test_inner_copper_shell_keeps_walls_only(self):
        copper = self._import_copper()
        base = _SurfaceFace("base", 10.0, 0.0)
        end = _SurfaceFace("end", 10.0, 0.018)
        wall = _SurfaceFace("wall", 0.5, 0.009)
        solid = types.SimpleNamespace(Faces=[base, end, wall])

        result = copper.extrusion_display_faces(
            solid, base, include_end_cap=False, include_base_cap=False)

        self.assertEqual([face.name for face in result], ["wall"])

if __name__ == "__main__":
    unittest.main()
