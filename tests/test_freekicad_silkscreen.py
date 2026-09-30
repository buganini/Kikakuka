import importlib
import sys
import types
import unittest
from unittest import mock


class _Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = x
        self.y = y
        self.z = z


class _Shape:
    def __init__(self, area=1.0, face_count=1):
        self.Area = area
        self.Faces = [object()] * face_count
        self.z = 0.0

    def translate(self, vector):
        self.z += vector.z

    def copy(self):
        result = _Shape(self.Area, len(self.Faces))
        result.z = self.z
        return result


class _BoardLayer:
    BL_F_SilkS = 1
    BL_B_SilkS = 2


class _Color:
    def __init__(self, red, green, blue):
        self.red = red
        self.green = green
        self.blue = blue


class _Entry:
    def __init__(self, layer, thickness=0, color=None, enabled=True):
        self.layer = layer
        self.thickness = thickness
        self.color = color
        self.enabled = enabled


class SilkscreenTests(unittest.TestCase):
    def _import_silkscreen(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_freecad.Vector = _Vector
        fake_part = types.ModuleType("Part")

        def make_compound(shapes):
            return _Shape(
                sum(shape.Area for shape in shapes),
                sum(len(shape.Faces) for shape in shapes),
            )

        fake_part.makeCompound = make_compound
        module_name = "FreekiCAD.freecad.FreekiCAD.Silkscreen"
        self.addCleanup(sys.modules.pop, module_name, None)
        with mock.patch.dict(
            sys.modules, {"FreeCAD": fake_freecad, "Part": fake_part}
        ):
            sys.modules.pop(module_name, None)
            return importlib.import_module(module_name)

    def test_stackup_uses_finished_boundaries_and_fallback_side(self):
        silk = self._import_silkscreen()
        stackup = types.SimpleNamespace(layers=[
            _Entry(1, 12_000, _Color(255, 200, 100)),
        ])

        layers = silk.silkscreen_stackup_layers(
            stackup, _BoardLayer, total_thickness=1.6)

        self.assertEqual([layer.name for layer in layers],
                         ["F.SilkS", "B.SilkS"])
        self.assertAlmostEqual(layers[0].z, 1.61)
        self.assertAlmostEqual(layers[1].z, -0.01)
        self.assertAlmostEqual(layers[0].thickness, 0.0)
        self.assertAlmostEqual(layers[1].thickness, 0.0)
        self.assertEqual(layers[0].color, (1.0, 200 / 255, 100 / 255))

    def test_explicitly_disabled_side_is_skipped(self):
        silk = self._import_silkscreen()
        stackup = types.SimpleNamespace(layers=[
            _Entry(1, enabled=False),
            _Entry(2),
        ])

        layers = silk.silkscreen_stackup_layers(
            stackup, _BoardLayer, total_thickness=1.6)

        self.assertEqual([layer.name for layer in layers], ["B.SilkS"])

    def test_direct_graphics_and_text_preserve_mixed_union_order(self):
        silk = self._import_silkscreen()
        direct = types.SimpleNamespace(layer=1)
        fallback = types.SimpleNamespace(layer=1)
        text_stroke = object()
        text = types.SimpleNamespace(layer=1, visible=True, as_text=lambda: 'text')
        board = types.SimpleNamespace(get_text=lambda: [text])
        kicad = types.SimpleNamespace(get_text_as_shapes=lambda _: [[text_stroke]])
        def polygons(item):
            if item is direct:
                return ['first']
            if item is text_stroke:
                return ['text-a', 'text-b']
            return None
        with mock.patch.object(silk, 'board_graphic_polygons', side_effect=polygons), \
                mock.patch.object(silk, 'board_graphic_shape', return_value=_Shape()) as build, \
                mock.patch.object(silk, 'union_planar_profiles', return_value=_Shape()) as union:
            layers = silk.build_silkscreen_layers(kicad, board,
                types.SimpleNamespace(layers=[]), _BoardLayer,
                board_shapes=[direct, fallback], footprints=[])
        build.assert_called_once_with(fallback)
        self.assertEqual(union.call_args.kwargs['seed_polygons'],
                         [(0, 'first'), (1, 'text-a'), (1, 'text-b')])
        self.assertEqual(layers[0]['graphic_count'], 2)
        self.assertEqual(layers[0]['text_count'], 1)

    def test_single_direct_graphic_keeps_original_brep(self):
        silk = self._import_silkscreen()
        graphic = types.SimpleNamespace(layer=1)
        original = _Shape()
        with mock.patch.object(silk, 'board_graphic_polygons', return_value=['polygon']), \
                mock.patch.object(silk, 'board_graphic_shape', return_value=original) as build, \
                mock.patch.object(silk, 'union_planar_profiles', return_value=original) as union:
            silk.build_silkscreen_layers(None, types.SimpleNamespace(get_text=lambda: []),
                types.SimpleNamespace(layers=[]), _BoardLayer,
                board_shapes=[graphic], footprints=[])
        build.assert_called_once_with(graphic)
        self.assertEqual(union.call_args.args[0], [original])
        self.assertNotIn('seed_polygons', union.call_args.kwargs)

    def test_builder_includes_board_and_footprint_graphics_and_text(self):
        silk = self._import_silkscreen()
        board_graphic = types.SimpleNamespace(layer=1)
        footprint_graphic = types.SimpleNamespace(layer=1)
        board_text = types.SimpleNamespace(
            layer=1,
            attributes=types.SimpleNamespace(visible=True),
            as_text=lambda: "board text",
        )
        field_text = types.SimpleNamespace(
            layer=1,
            visible=True,
            text=types.SimpleNamespace(as_text=lambda: "reference text"),
        )
        footprint = types.SimpleNamespace(
            definition=types.SimpleNamespace(shapes=[footprint_graphic]),
            texts_and_fields=[field_text],
        )
        board = types.SimpleNamespace(get_text=lambda: [board_text])
        kicad = types.SimpleNamespace(
            get_text_as_shapes=mock.Mock(
                return_value=[[object()], [object()]])
        )
        stackup = types.SimpleNamespace(layers=[_Entry(1, 10_000)])

        with mock.patch.object(
                silk, "board_graphic_shape",
                side_effect=lambda _item: _Shape()), mock.patch.object(
                silk, "union_planar_profiles",
                side_effect=lambda shapes, **_kwargs: silk.Part.makeCompound(
                    shapes)):
            layers = silk.build_silkscreen_layers(
                kicad, board, stackup, _BoardLayer,
                board_shapes=[board_graphic], footprints=[footprint],
                total_thickness=1.6,
            )

        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]["name"], "F.SilkS")
        self.assertEqual(layers[0]["graphic_count"], 2)
        self.assertEqual(layers[0]["text_count"], 2)
        self.assertEqual(layers[0]["face_count"], 4)
        self.assertAlmostEqual(layers[0]["area"], 4.0)
        self.assertAlmostEqual(layers[0]["shape"].z, 1.61)
        self.assertAlmostEqual(layers[0]["direction"], 0.0)
        self.assertEqual(layers[0]["volume"], 0.0)
        self.assertIs(layers[0]["shape"], layers[0]["profile_shape"])
        kicad.get_text_as_shapes.assert_called_once_with(
            ["board text", "reference text"])


if __name__ == "__main__":
    unittest.main()
