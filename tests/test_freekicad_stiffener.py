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


class _AreaShape:
    def __init__(self, x0, y0, x1, y1, z=0.0):
        self.bounds = (x0, y0, x1, y1)
        self.Area = (x1 - x0) * (y1 - y0)
        self.Faces = [self]
        self.CenterOfMass = _Vector((x0 + x1) / 2, (y0 + y1) / 2, z)
        self.z = z
        self.cut_openings = []

    def isInside(self, point, _tolerance, _include_boundary):
        x0, y0, x1, y1 = self.bounds
        return x0 <= point.x <= x1 and y0 <= point.y <= y1

    def copy(self):
        result = _AreaShape(*self.bounds, z=self.z)
        result.cut_openings = list(self.cut_openings)
        return result

    def cut(self, openings):
        self.cut_openings = list(openings)
        return self

    def translate(self, vector):
        self.z += vector.z
        self.CenterOfMass.z += vector.z

    def extrude(self, vector):
        return types.SimpleNamespace(
            base_z=self.z, extrusion_z=vector.z,
            cut_openings=list(self.cut_openings))


class StiffenerTests(unittest.TestCase):
    def _import_stiffener(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_freecad.Vector = _Vector
        fake_part = types.ModuleType("Part")
        fake_part.makeCompound = lambda shapes: list(shapes)
        fake_copper = types.ModuleType(
            "FreekiCAD.freecad.FreekiCAD.Copper")
        fake_copper.board_graphic_area_shape = lambda graphic: graphic.shape
        fake_copper.board_graphic_path_edge = lambda graphic: getattr(
            graphic, "edge", None)
        fake_copper.extrude_profile_for_display = lambda profile, direction, cap_mode: (
            profile.extrude(_Vector(0, 0, direction)),
            profile.extrude(_Vector(0, 0, direction)),
        )
        module_name = "FreekiCAD.freecad.FreekiCAD.Stiffener"
        self.addCleanup(sys.modules.pop, module_name, None)
        with mock.patch.dict(sys.modules, {
                "FreeCAD": fake_freecad,
                "Part": fake_part,
                "FreekiCAD.freecad.FreekiCAD.Copper": fake_copper}):
            sys.modules.pop(module_name, None)
            return importlib.import_module(module_name)

    def test_material_defaults_and_case_insensitive_names(self):
        stiffener = self._import_stiffener()

        spec = stiffener.parse_stiffener_annotation(
            "Name=Tail reinforcement\nmaterial=polyimide\nThickness=12.5 um")

        self.assertEqual(spec.name, "Tail reinforcement")
        self.assertEqual(spec.material, "Polyimide")
        self.assertEqual(spec.color, (0xC8 / 255, 0x75 / 255, 0x18 / 255))
        self.assertEqual(spec.opacity, 0.65)
        self.assertAlmostEqual(spec.thickness, 0.0125)

    def test_explicit_color_opacity_and_inch_thickness_override_defaults(self):
        stiffener = self._import_stiffener()

        spec = stiffener.parse_stiffener_annotation(
            "Material=FR4/Color=#123aBc/Opacity=0.4/Thickness=.01 in")

        self.assertEqual(spec.color, (0x12 / 255, 0x3A / 255, 0xBC / 255))
        self.assertEqual(spec.opacity, 0.4)
        self.assertAlmostEqual(spec.thickness, 0.254)

    def test_mil_thickness(self):
        stiffener = self._import_stiffener()

        spec = stiffener.parse_stiffener_annotation(
            "Material=FR4/Thickness=10 mil")

        self.assertAlmostEqual(spec.thickness, 0.254)

    def test_required_and_bounded_values_are_validated(self):
        stiffener = self._import_stiffener()

        for annotation in (
                "Thickness=1mm",
                "Material=FR4",
                "Material=wood/Thickness=1mm",
                "Material=FR4/Thickness=0mm",
                "Material=FR4/Opacity=1.1/Thickness=1mm",
                "Material=FR4/Color=red/Thickness=1mm"):
            with self.subTest(annotation=annotation):
                with self.assertRaises(ValueError):
                    stiffener.parse_stiffener_annotation(annotation)

    def test_builder_extrudes_front_and_back_away_from_board(self):
        stiffener = self._import_stiffener()
        front = types.SimpleNamespace(
            layer=10, shape=_AreaShape(0, 0, 4, 2),
            id=types.SimpleNamespace(value="front-id"))
        back = types.SimpleNamespace(
            layer=11, shape=_AreaShape(5, 0, 7, 2),
            id=types.SimpleNamespace(value="back-id"))
        front_text = types.SimpleNamespace(
            layer=10,
            value="Name=Front brace/Material=FR4/Thickness=200um",
            position=types.SimpleNamespace(x=2_000_000, y=-1_000_000))
        back_text = types.SimpleNamespace(
            layer=11,
            value="Material=3M9077/Thickness=.01in",
            position=types.SimpleNamespace(x=6_000_000, y=-1_000_000))

        result = stiffener.build_stiffener_layers(
            [front, back], [front_text, back_text],
            [(10, "f.STIFFENER", True), (11, "B.Stiffener", False)],
            total_thickness=1.6,
            mask_openings={True: ["front-pad"], False: ["back-pad"]},
            surface_offsets={True: 0.01, False: 0.02})

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["shape"].base_z, 1.61)
        self.assertAlmostEqual(result[0]["shape"].extrusion_z, 0.2)
        self.assertEqual(result[0]["source_id"], "front-id")
        self.assertEqual(result[0]["name"], "Front brace")
        self.assertEqual(result[0]["shape"].cut_openings, ["front-pad"])
        self.assertEqual(result[0]["mask_opening_count"], 1)
        self.assertEqual(result[1]["shape"].base_z, -0.02)
        self.assertAlmostEqual(result[1]["shape"].extrusion_z, -0.254)
        self.assertEqual(result[1]["name"], "")
        self.assertEqual(result[1]["shape"].cut_openings, ["back-pad"])
        self.assertEqual(result[1]["mask_opening_count"], 1)
        self.assertEqual(result[1]["transparency"], 70)

    def test_unsupported_or_unannotated_areas_are_ignored_with_warning(self):
        stiffener = self._import_stiffener()
        warnings = []
        area = types.SimpleNamespace(
            layer=10, shape=_AreaShape(0, 0, 2, 2))
        unsupported = types.SimpleNamespace(layer=10, shape=None)

        result = stiffener.build_stiffener_layers(
            [area, unsupported], [], [(10, "F.Stiffener", True)], 1.6,
            warn=warnings.append)

        self.assertEqual(result, [])
        self.assertEqual(warnings,
                         ["Ignoring unannotated area on F.Stiffener"])

    def test_invalid_annotation_is_reported_as_error(self):
        stiffener = self._import_stiffener()
        warnings = []
        errors = []
        area = types.SimpleNamespace(
            layer=10, shape=_AreaShape(0, 0, 2, 2))
        text = types.SimpleNamespace(
            layer=10,
            value="Material=Polymide\nThickness=12.5um",
            position=types.SimpleNamespace(x=1_000_000, y=-1_000_000))

        result = stiffener.build_stiffener_layers(
            [area], [text], [(10, "F.Stiffener", True)], 1.6,
            warn=warnings.append, error=errors.append)

        self.assertEqual(result, [])
        self.assertEqual(warnings, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Ignoring invalid F.Stiffener area", errors[0])
        self.assertIn("Polymide", errors[0])

    def test_closed_line_chain_builds_stiffener_area(self):
        stiffener = self._import_stiffener()
        edges = [object() for _index in range(4)]
        graphics = [
            types.SimpleNamespace(layer=10, shape=None, edge=edge)
            for edge in edges]
        text = types.SimpleNamespace(
            layer=10,
            value="Material=Polyimide\nThickness=12.5um",
            position=types.SimpleNamespace(x=1_000_000, y=-1_000_000))
        wire = types.SimpleNamespace(
            isClosed=lambda: True,
            fixWire=lambda *_args: None)
        stiffener.Part.sortEdges = lambda values: [values]
        stiffener.Part.Wire = lambda values: wire
        stiffener.Part.Face = lambda value: _AreaShape(0, 0, 2, 2)

        result = stiffener.build_stiffener_layers(
            graphics, [text], [(10, "B.Stiffener", False)], 1.6)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["material"], "Polyimide")
        self.assertAlmostEqual(result[0]["thickness"], 0.0125)
        self.assertLess(result[0]["shape"].extrusion_z, 0.0)


if __name__ == "__main__":
    unittest.main()
