"""Native OCC tests for direct copper polygon paths; run in FreeCAD Python."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest

try:
    import FreeCAD
    import Part
    import shapely
except ImportError:
    FreeCAD = None


@unittest.skipIf(FreeCAD is None, 'requires FreeCAD Python and Shapely')
class CopperPolygonNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'FreekiCAD/freecad/FreekiCAD/Copper.py'
        spec = importlib.util.spec_from_file_location('native_copper_regression', source)
        cls.c = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.c
        spec.loader.exec_module(cls.c)

    def point(self, x, y):
        return types.SimpleNamespace(x=x*1e6, y=y*1e6)

    def test_partition_preserves_holes_islands_and_exact_occ_clipping(self):
        outer = shapely.box(-7, -6, 13, 11)
        holes = shapely.union_all([
            shapely.box(-5, -4, 0, 1),
            shapely.box(3, 3, 8, 8)])
        geometry = outer.difference(holes).union(shapely.box(-3, -2, -2, -1))
        regions = list(self.c._partition_planar_polygons(geometry, 5.0))
        self.assertGreater(len(regions), 1)
        self.assertLess(geometry.symmetric_difference(shapely.union_all(regions)).area, 1e-9)
        self.assertAlmostEqual(sum(p.area for p in regions), geometry.area, places=9)
        original = self.c._polygons_to_part_shape(geometry)
        partitioned = self.c._polygons_to_part_shape(geometry, 5.0)
        self.assertTrue(partitioned.isValid())
        self.assertAlmostEqual(original.Area, partitioned.Area, places=8)
        # Curved clipping boundaries must still be handled by exact OCC.
        cutter = Part.makeCylinder(6, 2, FreeCAD.Vector(1, 2, -1))
        exact = original.common(cutter)
        split = partitioned.common(cutter)
        self.assertAlmostEqual(exact.Area, split.Area, places=7)
        self.assertLess(exact.cut(split).Area, 1e-7)
        self.assertLess(split.cut(exact).Area, 1e-7)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'copper.step')
            partitioned.exportStep(path)
            restored = Part.Shape()
            restored.read(path)
            self.assertAlmostEqual(restored.Area, original.Area, places=7)

    def test_partition_keeps_tiny_regions_and_bounds_grid_growth(self):
        tiny = shapely.box(4.999999, 4.999999, 5.000001, 5.000001)
        regions = list(self.c._partition_planar_polygons(tiny, 5.0))
        self.assertEqual(len(regions), 4)
        self.assertLess(tiny.symmetric_difference(shapely.union_all(regions)).area, 1e-20)
        huge = shapely.box(0, 0, 100000, 100000)
        regions = list(self.c._partition_planar_polygons(huge, 5.0))
        self.assertEqual(len(regions), 1)
        self.assertTrue(regions[0].equals(huge))

    def test_straight_arc_and_zero_length_tracks_match_brep_sampling(self):
        for name, start, end, mid in [
                ('Track', (0,0), (5,0), None),
                ('Track', (3,-2), (-4,5), None),
                ('Track', (2,3), (2,3), None),
                ('ArcTrack', (0,0), (4,0), (2,2)),
                ('ArcTrack', (4,0), (0,0), (2,2)),
                ('ArcTrack', (0,0), (4,0), (2,0))]:
            for width in (.001, .2, 2):
                with self.subTest(name=name, start=start, width=width):
                    track = type(name, (), {})()
                    track.start, track.end = self.point(*start), self.point(*end)
                    track.width = width*1e6
                    if mid:
                        track.mid = self.point(*mid)
                    direct = self.c._track_polygons(track)
                    shape = self.c._copper_item_shape(
                        types.SimpleNamespace(kind='track', source=track), None)
                    old = self.c._shape_to_polygons(shape)
                    self.assertEqual(len(old), len(direct))
                    for a, b in zip(old, direct):
                        self.assertTrue(a.equals(b))

    def test_rotated_offset_straight_pads_match_brep(self):
        from kipy.proto.board.board_types_pb2 import PadStackShape as E
        for kind in (E.PSS_RECTANGLE, E.PSS_TRAPEZOID):
            for angle in (0, 90, 37, -135):
                with self.subTest(kind=kind, angle=angle):
                    pad = types.SimpleNamespace(
                        position=self.point(10,20),
                        padstack=types.SimpleNamespace(
                            angle=types.SimpleNamespace(degrees=angle),
                            drill=types.SimpleNamespace(diameter=self.point(0,0))))
                    layer = types.SimpleNamespace(shape=kind, size=self.point(2,1),
                        offset=self.point(.3,-.4), trapezoid_delta=self.point(.2,.1))
                    direct = self.c._straight_pad_polygon(pad, layer, E)
                    old = self.c._shape_to_polygons(self.c.pad_layer_shape(pad, layer, E))[0]
                    self.assertLess(direct.symmetric_difference(old).area, 1e-12)
                    self.assertLess(direct.hausdorff_distance(old), 1e-12)
                    pad.padstack.drill.diameter = self.point(.3,.3)
                    self.assertIsNone(self.c._straight_pad_polygon(pad, layer, E))

    def test_missing_shapely_and_nonpositive_tracks_use_fallback(self):
        from unittest.mock import patch
        track = types.SimpleNamespace(start=self.point(0,0), end=self.point(1,1), width=0)
        self.assertIsNone(self.c._track_polygons(track))
        with patch.object(self.c, 'shapely', None):
            self.assertIsNone(self.c._track_polygons(track))

    def test_graphic_strokes_match_brep_with_minimum_width(self):
        for kind in ('BoardSegment', 'Segment', 'BoardArc', 'Arc'):
            for width in (0, -100, 120_000):
                with self.subTest(kind=kind, width=width):
                    graphic = type(kind, (), {})()
                    graphic.start, graphic.end = self.point(0,0), self.point(4,0)
                    graphic.mid = self.point(2,2)
                    graphic.attributes = types.SimpleNamespace(
                        stroke=types.SimpleNamespace(width=width))
                    direct = self.c.board_graphic_polygons(graphic)
                    old = self.c._shape_to_polygons(self.c.board_graphic_shape(graphic))
                    self.assertEqual(len(old), len(direct))
                    self.assertTrue(all(a.equals(b) for a, b in zip(old, direct)))
        self.assertIsNone(self.c.board_graphic_polygons(object()))

    def test_single_surviving_track_keeps_exact_brep_curves(self):
        from unittest.mock import patch
        info = types.SimpleNamespace(layer=1, is_outer=True, z=0, direction=0,
                                     thickness=.035, name='F.Cu')
        track = types.SimpleNamespace(start=self.point(0,0), end=self.point(4,0),
                                      width=.2*1e6)
        items = [types.SimpleNamespace(kind='track', source=track),
                 types.SimpleNamespace(kind='unsupported', source=None)]
        board = types.SimpleNamespace(get_text=lambda: [], get_barcodes=lambda: [])
        with patch.object(self.c, 'copper_stackup_layers', return_value=[info]), \
                patch.object(self.c, 'read_copper_items', return_value={1: items}):
            layers = self.c.build_copper_layers(board, None, None)
        edges = layers[0]['shape'].Edges
        self.assertEqual(len(edges), 4)
        self.assertEqual(sum(isinstance(e.Curve, Part.Circle) for e in edges), 2)


if __name__ == '__main__':
    unittest.main()
