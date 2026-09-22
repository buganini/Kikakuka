import unittest

import numpy as np

from differ_overlap import OVERLAP_COLOR_SHIFT, apply_overlap_color_shift


class OverlapColorTests(unittest.TestCase):
    def test_equal_pixels_keep_original_color_even_at_channel_limits(self):
        pixels = np.array([[[0, 0, 0, 255], [255, 255, 255, 255],
                            [20, 40, 200, 255], [10, 20, 40, 128]]],
                          dtype=np.uint8)
        for premultiplied in (False, True):
            overlap = pixels.copy()
            apply_overlap_color_shift(
                pixels, pixels, overlap, premultiplied=premultiplied
            )
            np.testing.assert_array_equal(overlap, pixels)

    def test_schematic_only_a_or_b_ink_gets_red_or_blue_tint(self):
        white = np.array([[[255, 255, 255, 255]]], dtype=np.uint8)
        black = np.array([[[0, 0, 0, 255]]], dtype=np.uint8)
        a_only = black.copy()
        b_only = black.copy()

        apply_overlap_color_shift(black, white, a_only, premultiplied=False)
        apply_overlap_color_shift(white, black, b_only, premultiplied=False)

        np.testing.assert_array_equal(
            a_only[0, 0], [0, 0, OVERLAP_COLOR_SHIFT, 255]
        )
        np.testing.assert_array_equal(
            b_only[0, 0], [OVERLAP_COLOR_SHIFT, 0, 0, 255]
        )

    def test_pcb_only_a_or_b_coverage_gets_red_or_blue_tint(self):
        color = np.array([[[20, 40, 100, 255]]], dtype=np.uint8)
        transparent = np.zeros_like(color)
        a_only = color.copy()
        b_only = color.copy()

        apply_overlap_color_shift(
            color, transparent, a_only, premultiplied=True
        )
        apply_overlap_color_shift(
            transparent, color, b_only, premultiplied=True
        )

        np.testing.assert_array_equal(
            a_only[0, 0], [20, 40, 100 + OVERLAP_COLOR_SHIFT, 255]
        )
        np.testing.assert_array_equal(
            b_only[0, 0], [20 + OVERLAP_COLOR_SHIFT, 40, 100, 255]
        )

    def test_pcb_equal_alpha_different_colors_do_not_cancel_tint(self):
        image_a = np.array([[[100, 100, 10, 255]]], dtype=np.uint8)
        image_b = np.array([[[10, 10, 100, 255]]], dtype=np.uint8)
        overlap = np.zeros_like(image_a)

        apply_overlap_color_shift(
            image_a, image_b, overlap, premultiplied=True
        )

        np.testing.assert_array_equal(overlap[0, 0], [100, 10, 100, 255])

    def test_pcb_partial_alpha_stays_premultiplied(self):
        color = np.array([[[10, 20, 40, 128]]], dtype=np.uint8)
        transparent = np.zeros_like(color)
        overlap = color.copy()

        apply_overlap_color_shift(
            color, transparent, overlap, premultiplied=True
        )

        self.assertTrue(np.all(overlap[:, :, :3] <= overlap[:, :, 3:4]))
        self.assertEqual(int(overlap[0, 0, 3]), 128)

    def test_channel_shift_clips_at_eight_bit_limits(self):
        color = np.array([[[30, 5, 250, 255]]], dtype=np.uint8)
        transparent = np.zeros_like(color)
        overlap = color.copy()

        apply_overlap_color_shift(
            color, transparent, overlap, premultiplied=True
        )

        np.testing.assert_array_equal(overlap[0, 0], [30, 5, 255, 255])


if __name__ == "__main__":
    unittest.main()
