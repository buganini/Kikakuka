import importlib.util
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGINS_DIR = (
    Path(__file__).resolve().parents[1]
    / "kicad-plugin"
    / "plugins"
)
sys.path.insert(0, str(PLUGINS_DIR))
MODULE_PATH = PLUGINS_DIR / "coupler_helpers.py"
SPEC = importlib.util.spec_from_file_location("coupler_helpers", MODULE_PATH)
coupler_helpers = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coupler_helpers
SPEC.loader.exec_module(coupler_helpers)


class FakeBoard:
    def __init__(self, footprints=()):
        self._footprints = list(footprints)
        self.updated = []
        self.commit_message = None
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

    def push_commit(self, _commit, message):
        self.commit_message = message

    def drop_commit(self, _commit):
        self.dropped = True


class FakeKiCad:
    def get_kicad_binary_path(self, _name):
        raise FileNotFoundError


def make_model(filename, offset=(0, 0, 0), rotation=(0, 0, 0)):
    from kipy.board_types import Footprint3DModel
    from kipy.geometry import Vector3D

    model = Footprint3DModel()
    model.filename = filename
    model.scale = Vector3D.from_xyz(1, 1, 1)
    model.offset = Vector3D.from_xyz(*offset)
    model.rotation = Vector3D.from_xyz(*rotation)
    model.visible = True
    model.opacity = 1.0
    return model


def make_footprint(coupler_type, properties=None, models=()):
    from kipy.board_types import Field, FootprintInstance

    footprint = FootprintInstance()
    footprint.definition.id.name = coupler_type
    for name, value in (properties or {}).items():
        item = Field()
        item.name = name
        item.text.value = value
        footprint.definition.add_item(item)
    for model in models:
        footprint.definition.add_item(model)
    return footprint


class CouplerHelpersTest(unittest.TestCase):
    def test_parse_signed_lengths_and_tilt(self):
        for text, expected in (
            ("-2", -2.0),
            ("2 mm", 2.0),
            ("-0.5in", -12.7),
            ("100 mil", 2.54),
            ("250 um", 0.25),
            ("250 µm", 0.25),
            (None, 0.0),
        ):
            with self.subTest(text=text):
                self.assertTrue(math.isclose(
                    coupler_helpers.parse_signed_length_mm(text, "Z"),
                    expected,
                ))
        self.assertEqual(coupler_helpers.parse_tilt_degrees("-12.5°"), -12.5)
        self.assertEqual(coupler_helpers.parse_tilt_degrees(None), 0.0)

    def test_invalid_values_are_rejected(self):
        for text in ("1cm", "nan", "1 mm extra"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                coupler_helpers.parse_signed_length_mm(text, "Offset")
        with self.assertRaises(ValueError):
            coupler_helpers.parse_tilt_degrees("12 rad")

    def test_enable_adds_transformed_model_and_preserves_other_models(self):
        other = make_model("other.step")
        footprint = make_footprint("CouplerMoving", {
            "Z": "2.4 mm",
            "Tilt": "-12.5 deg",
            "Offset": "100 mil",
        }, [other])
        board = FakeBoard([footprint])

        with tempfile.TemporaryDirectory() as temp_dir:
            model_file = Path(temp_dir) / "coupler-moving.step"
            model_file.write_text("STEP", encoding="ascii")
            with mock.patch.object(
                coupler_helpers,
                "resolve_model_path",
                return_value=model_file,
            ):
                result = coupler_helpers.enable_update_coupler_helpers(
                    FakeKiCad(), board)

        self.assertEqual(result.added, 1)
        self.assertEqual(result.updated, 0)
        self.assertEqual(board.updated, [footprint])
        self.assertIn("Coupler 3D Viewer", board.commit_message)
        self.assertEqual(len(footprint.definition.models), 2)
        self.assertEqual(footprint.definition.models[0].filename, "other.step")
        helper = footprint.definition.models[1]
        self.assertEqual(
            helper.filename,
            coupler_helpers.COUPLER_MODELS["CouplerMoving"],
        )
        self.assertEqual((helper.offset.x, helper.offset.y, helper.offset.z),
                         (0.0, -2.54, 2.4))
        self.assertEqual(
            (helper.rotation.x, helper.rotation.y, helper.rotation.z),
            (-12.5, 0.0, 0.0),
        )

    def test_enable_is_idempotent(self):
        helper = make_model(
            coupler_helpers.COUPLER_MODELS["CouplerFixed"],
            offset=(0, -3, 2),
            rotation=(10, 0, 0),
        )
        footprint = make_footprint("CouplerFixed", {
            "Z": "2",
            "Tilt": "10",
            "Offset": "3",
        }, [helper])
        board = FakeBoard([footprint])

        with mock.patch.object(
            coupler_helpers,
            "resolve_model_path",
            return_value=Path("/installed/coupler-fixed.step"),
        ):
            result = coupler_helpers.enable_update_coupler_helpers(
                FakeKiCad(), board)

        self.assertEqual(result.unchanged, 1)
        self.assertEqual(result.added, 0)
        self.assertEqual(result.updated, 0)
        self.assertEqual(board.updated, [])
        self.assertIsNone(board.commit_message)

    def test_enable_replaces_wrong_helper_type_and_transform(self):
        wrong = make_model(
            coupler_helpers.COUPLER_MODELS["CouplerMoving"],
            offset=(4, 5, 6),
        )
        footprint = make_footprint("CouplerFixed", {
            "Z": "-1",
            "Tilt": "5",
            "Offset": "-2",
        }, [wrong])
        board = FakeBoard([footprint])

        with mock.patch.object(
            coupler_helpers,
            "resolve_model_path",
            return_value=Path("/installed/coupler-fixed.step"),
        ):
            result = coupler_helpers.enable_update_coupler_helpers(
                FakeKiCad(), board)

        self.assertEqual(result.updated, 1)
        self.assertEqual(len(footprint.definition.models), 1)
        helper = footprint.definition.models[0]
        self.assertEqual(
            helper.filename,
            coupler_helpers.COUPLER_MODELS["CouplerFixed"],
        )
        self.assertEqual((helper.offset.x, helper.offset.y, helper.offset.z),
                         (0.0, 2.0, -1.0))

    def test_enable_reports_invalid_property_without_mutating(self):
        footprint = make_footprint("CouplerFixed", {
            "Z": "nope",
            "Tilt": "0",
            "Offset": "0",
        })
        board = FakeBoard([footprint])
        with mock.patch.object(
            coupler_helpers,
            "resolve_model_path",
            return_value=Path("/installed/coupler-fixed.step"),
        ):
            result = coupler_helpers.enable_update_coupler_helpers(
                FakeKiCad(), board)

        self.assertEqual(result.invalid, 1)
        self.assertEqual(len(result.details), 1)
        self.assertFalse(footprint.definition.models)
        self.assertEqual(board.updated, [])

    def test_missing_library_is_reported_before_mutation(self):
        footprint = make_footprint("CouplerFixed")
        board = FakeBoard([footprint])
        with mock.patch.object(
            coupler_helpers,
            "resolve_model_path",
            return_value=None,
        ), self.assertRaises(FileNotFoundError):
            coupler_helpers.enable_update_coupler_helpers(FakeKiCad(), board)

        self.assertFalse(footprint.definition.models)
        self.assertEqual(board.updated, [])

    def test_hide_preserves_models_and_hides_only_visible_helpers(self):
        other = make_model("/my/models/coupler-fixed.step")
        fixed = make_model(coupler_helpers.COUPLER_MODELS["CouplerFixed"])
        expanded = make_model(
            "/tmp/3dmodels/com_github_buganini_kikakuka-footprints/"
            "Kikakuka.3dshapes/coupler-moving.step"
        )
        footprint = make_footprint("ordinary", models=[other, fixed, expanded])
        board = FakeBoard([footprint])

        result = coupler_helpers.hide_coupler_helpers(FakeKiCad(), board)

        self.assertEqual(result.changed, 1)
        self.assertEqual(result.hidden, 2)
        self.assertEqual(footprint.definition.models, [other, fixed, expanded])
        self.assertTrue(other.visible)
        self.assertFalse(fixed.visible)
        self.assertFalse(expanded.visible)
        self.assertIn("Hide", board.commit_message)

    def test_hide_is_idempotent(self):
        helper = make_model(coupler_helpers.COUPLER_MODELS["CouplerMoving"])
        helper.visible = False
        footprint = make_footprint("CouplerMoving", models=[helper])
        board = FakeBoard([footprint])

        result = coupler_helpers.hide_coupler_helpers(FakeKiCad(), board)

        self.assertEqual(result.changed, 0)
        self.assertEqual(result.hidden, 0)
        self.assertEqual(board.updated, [])
        self.assertIsNone(board.commit_message)


if __name__ == "__main__":
    unittest.main()
