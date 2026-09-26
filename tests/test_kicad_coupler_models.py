"""Checks for the prebuilt KiCad coupler helper STEP geometry."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL_DIRECTORY = (
    ROOT / "kicad-addon" / "library" / "3dmodels" /
    "Kikakuka.3dshapes"
)


def _step_vertices(path):
    text = path.read_text(encoding="ascii")
    points = {
        identifier: tuple(float(value) for value in values.split(","))
        for identifier, values in re.findall(
            r"#(\d+)\s*=\s*CARTESIAN_POINT\('',\(([^()]*)\)\);",
            text.replace("\n", ""),
        )
    }
    point_ids = re.findall(
        r"#\d+\s*=\s*VERTEX_POINT\('',#(\d+)\);", text
    )
    return {points[identifier] for identifier in point_ids}


class KiCadCouplerModelTests(unittest.TestCase):
    def test_step_y_is_mirrored_to_match_footprint_triangle(self):
        expected = {
            (0.0, 0.0, 0.0),
            (-2.0, 2.0, -0.075),
            (-2.0, 2.0, 0.075),
            (2.0, 2.0, -0.075),
            (2.0, 2.0, 0.075),
        }
        for filename in ("coupler-fixed.step", "coupler-moving.step"):
            with self.subTest(filename=filename):
                self.assertEqual(
                    _step_vertices(MODEL_DIRECTORY / filename), expected
                )

    def test_step_colors_encode_fixed_red_and_moving_orange(self):
        expected = {
            "coupler-fixed.step": (1.0, 0.1, 0.1),
            "coupler-moving.step": (1.0, 0.4, 0.1),
        }
        for filename, color in expected.items():
            with self.subTest(filename=filename):
                text = (MODEL_DIRECTORY / filename).read_text(encoding="ascii")
                values = re.search(
                    r"COLOUR_RGB\('',([^,]+),([^,]+),([^\)]+)\)", text
                ).groups()
                for actual, wanted in zip(
                        (float(value) for value in values), color):
                    self.assertAlmostEqual(actual, wanted)


if __name__ == "__main__":
    unittest.main()
