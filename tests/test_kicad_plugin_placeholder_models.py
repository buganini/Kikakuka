import importlib.util
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "kicad-plugin"
    / "plugins"
    / "placeholder_models.py"
)
SPEC = importlib.util.spec_from_file_location("placeholder_models", MODULE_PATH)
placeholder_models = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = placeholder_models
SPEC.loader.exec_module(placeholder_models)


class FakeBoard:
    def __init__(self, footprints=()):
        self._footprints = list(footprints)
        self.updated = []
        self.pushed = False
        self.dropped = False
        self.document = type(
            "Document",
            (),
            {"project": type("Project", (), {"path": ""})()},
        )()

    def expand_text_variables(self, text):
        return text

    def get_footprints(self):
        return self._footprints

    def begin_commit(self):
        return object()

    def update_items(self, items):
        self.updated = list(items)
        return list(items)

    def push_commit(self, _commit, _message):
        self.pushed = True

    def drop_commit(self, _commit):
        self.dropped = True


class FakeKiCad:
    def get_kicad_binary_path(self, _name):
        raise FileNotFoundError


def make_footprint(properties, models=()):
    from kipy.board_types import Field, FootprintInstance

    footprint = FootprintInstance()
    for name, value in properties.items():
        item = Field()
        item.name = name
        item.text.value = value
        footprint.definition.add_item(item)
    for model in models:
        footprint.definition.add_item(model)
    return footprint


class PlaceholderModelsTest(unittest.TestCase):
    def test_parse_length_mm(self):
        for text, expected in (
            ("2", 2.0),
            ("2 mm", 2.0),
            ("0.5in", 12.7),
            ("100 mil", 2.54),
        ):
            with self.subTest(text=text):
                self.assertTrue(math.isclose(
                    placeholder_models.parse_length_mm(text), expected))

    def test_parse_length_mm_rejects_invalid_or_nonpositive_values(self):
        for text in (None, "", "1cm", "0", "-1mm"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                placeholder_models.parse_length_mm(text)

    def test_add_placeholder_model_with_unit_converted_scale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cube = Path(temp_dir) / "unit-cube.step"
            cube.write_text("STEP", encoding="ascii")
            footprint = make_footprint({
                "SizeX": "2mm",
                "SizeY": "0.5 in",
                "SizeZ": "100mil",
            })
            board = FakeBoard([footprint])
            original_resolver = placeholder_models.resolve_model_path

            def resolve(filename, board_arg, variables):
                if filename == placeholder_models.PLACEHOLDER_MODEL:
                    return cube
                return original_resolver(filename, board_arg, variables)

            with mock.patch.object(
                    placeholder_models, "resolve_model_path", resolve):
                result = placeholder_models.add_placeholder_models(
                    FakeKiCad(), board)

            self.assertEqual(result.added, 1)
            self.assertTrue(board.pushed)
            self.assertFalse(board.dropped)
            model = footprint.definition.models[0]
            self.assertEqual(
                model.filename, placeholder_models.PLACEHOLDER_MODEL)
            self.assertTrue(math.isclose(model.scale.x, 2.0))
            self.assertTrue(math.isclose(model.scale.y, 12.7))
            self.assertTrue(math.isclose(model.scale.z, 2.54))
            self.assertTrue(model.visible)
            self.assertTrue(math.isclose(model.opacity, 1.0))

    def test_existing_resolvable_model_is_preserved(self):
        from kipy.board_types import Footprint3DModel

        with tempfile.TemporaryDirectory() as temp_dir:
            cube = Path(temp_dir) / "unit-cube.step"
            cube.write_text("STEP", encoding="ascii")
            existing = Path(temp_dir) / "existing.step"
            existing.write_text("STEP", encoding="ascii")
            model = Footprint3DModel()
            model.filename = str(existing)
            footprint = make_footprint({
                "SizeX": "2",
                "SizeY": "3",
                "SizeZ": "4",
            }, [model])
            board = FakeBoard([footprint])
            original_resolver = placeholder_models.resolve_model_path

            def resolve(filename, board_arg, variables):
                if filename == placeholder_models.PLACEHOLDER_MODEL:
                    return cube
                return original_resolver(filename, board_arg, variables)

            with mock.patch.object(
                    placeholder_models, "resolve_model_path", resolve):
                result = placeholder_models.add_placeholder_models(
                    FakeKiCad(), board)

            self.assertEqual(result.already_valid, 1)
            self.assertEqual(result.added, 0)
            self.assertEqual(footprint.definition.models, [model])
            self.assertFalse(board.pushed)

    def test_invalid_model_is_replaced(self):
        from kipy.board_types import Footprint3DModel

        with tempfile.TemporaryDirectory() as temp_dir:
            cube = Path(temp_dir) / "unit-cube.step"
            cube.write_text("STEP", encoding="ascii")
            invalid = Footprint3DModel()
            invalid.filename = str(Path(temp_dir) / "missing.step")
            footprint = make_footprint({
                "SizeX": "2",
                "SizeY": "3",
                "SizeZ": "4",
            }, [invalid])
            board = FakeBoard([footprint])

            def resolve(filename, _board, _variables):
                if filename == placeholder_models.PLACEHOLDER_MODEL:
                    return cube
                return None

            with mock.patch.object(
                    placeholder_models, "resolve_model_path", resolve):
                result = placeholder_models.add_placeholder_models(
                    FakeKiCad(), board)

            self.assertEqual(result.added, 1)
            self.assertEqual(len(footprint.definition.models), 1)
            self.assertEqual(
                footprint.definition.models[0].filename,
                placeholder_models.PLACEHOLDER_MODEL,
            )

    def test_missing_size_property_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cube = Path(temp_dir) / "unit-cube.step"
            cube.write_text("STEP", encoding="ascii")
            footprint = make_footprint({"SizeX": "2", "SizeY": "3"})
            board = FakeBoard([footprint])

            def resolve(filename, _board, _variables):
                if filename == placeholder_models.PLACEHOLDER_MODEL:
                    return cube
                return None

            with mock.patch.object(
                    placeholder_models, "resolve_model_path", resolve):
                result = placeholder_models.add_placeholder_models(
                    FakeKiCad(), board)

            self.assertEqual(result.missing_size, 1)
            self.assertEqual(result.added, 0)
            self.assertFalse(footprint.definition.models)


if __name__ == "__main__":
    unittest.main()
