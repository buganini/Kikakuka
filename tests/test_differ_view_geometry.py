import unittest

from differ_view_geometry import (
    DEFAULT_OVERLAP_PERCENT, adjust_overlap_percent, clipped_view_transform,
    overlap_bounds,
)


class OverlapGeometryTests(unittest.TestCase):
    def test_default_overlap_is_thirteen_percent(self):
        self.assertEqual(DEFAULT_OVERLAP_PERCENT, 13.0)

    def test_initial_view_fits_and_centers_page(self):
        self.assertEqual(
            clipped_view_transform(None, (100, 80), (400, 300), 64),
            ((59.375, 37.5, 2.8125), 2.8125),
        )

    def test_new_generation_preserves_valid_pan_and_zoom(self):
        self.assertEqual(
            clipped_view_transform((-40, -25, 4), (100, 100),
                                   (300, 300), 64),
            ((-40, -25, 4), 2.25),
        )

    def test_new_bounds_clip_only_out_of_range_values(self):
        self.assertEqual(
            clipped_view_transform((-1000, 1000, 4), (100, 100),
                                   (300, 300), 64),
            ((-100, 0.0, 4), 2.25),
        )
        self.assertEqual(
            clipped_view_transform((-40, -25, 4), (50, 50),
                                   (300, 300), 64),
            ((0.0, 0.0, 4), 4.5),
        )

    def test_zoom_is_clipped_to_new_page_range(self):
        self.assertEqual(
            clipped_view_transform((10, 10, 100), (100, 100),
                                   (300, 300), 4),
            ((0.0, 0.0, 9.0), 2.25),
        )
        self.assertEqual(
            clipped_view_transform((10, 10, 0.01), (100, 100),
                                   (300, 300), 4),
            ((10, 10, 0.28125), 2.25),
        )

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
