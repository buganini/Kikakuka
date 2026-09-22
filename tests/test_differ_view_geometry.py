import unittest

from differ_view_geometry import (
    DEFAULT_OVERLAP_PERCENT, adjust_overlap_percent, overlap_bounds,
)


class OverlapGeometryTests(unittest.TestCase):
    def test_default_overlap_is_thirteen_percent(self):
        self.assertEqual(DEFAULT_OVERLAP_PERCENT, 13.0)

    def test_wheel_adjustment_is_linear_and_clamped(self):
        self.assertEqual(adjust_overlap_percent(0.0, 120), 1.0)
        self.assertEqual(adjust_overlap_percent(1.0, 60), 1.5)
        self.assertEqual(adjust_overlap_percent(0.0, -120), 0.0)
        self.assertEqual(adjust_overlap_percent(29.5, 120), 30.0)

    def test_overlap_is_percent_of_viewport_not_page(self):
        self.assertEqual(
            overlap_bounds(1000.0, 0.5, 400.0, 2.0, 30.0),
            (470.0, 530.0),
        )
        self.assertEqual(
            overlap_bounds(1000.0, 0.5, 400.0, 4.0, 30.0),
            (485.0, 515.0),
        )
        self.assertEqual(
            overlap_bounds(1000.0, 0.5, 400.0, 2.0, 0.0),
            (500.0, 500.0),
        )

    def test_overlap_clips_to_page(self):
        self.assertEqual(
            overlap_bounds(100.0, 0.05, 400.0, 2.0, 30.0),
            (0.0, 35.0),
        )


if __name__ == "__main__":
    unittest.main()
