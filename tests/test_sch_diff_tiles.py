import unittest
from unittest import mock

import numpy as np

from sch_diff_tiles import (
    SchematicTileRenderer,
    corresponding_schematic_page,
    matched_page_shift,
    schematic_page_index,
    synchronized_page_shift,
)


class SchematicTileRendererTests(unittest.TestCase):
    def test_thumbnail_name_carries_pdf_page_index(self):
        self.assertEqual(schematic_page_index("sch_00.png"), 0)
        self.assertEqual(schematic_page_index("sch_12.png"), 12)

    def test_sync_page_matches_index_and_clamps_shorter_side(self):
        pages = ["sch_00.png", "sch_01.png"]
        self.assertEqual(
            corresponding_schematic_page(pages, "sch_01.png"), "sch_01.png"
        )
        self.assertEqual(
            corresponding_schematic_page(pages, "sch_04.png"), "sch_01.png"
        )
        self.assertIsNone(corresponding_schematic_page([], "sch_00.png"))

    def test_sync_page_arrows_align_b_with_a(self):
        pages_a = ["sch_00.png", "sch_01.png", "sch_02.png"]
        pages_b = ["sch_00.png", "sch_01.png"]
        self.assertEqual(
            matched_page_shift(pages_a, "sch_00.png", pages_b, 1),
            ("sch_01.png", "sch_01.png"),
        )
        self.assertEqual(
            matched_page_shift(pages_a, "sch_01.png", pages_b, 1),
            ("sch_02.png", "sch_01.png"),
        )
        self.assertIsNone(matched_page_shift(pages_a, "sch_02.png", pages_b, 1))

    def test_synchronized_page_shift_moves_both_sides(self):
        self.assertEqual(
            synchronized_page_shift(
                ["a0", "a1", "a2"], "a1",
                ["b0", "b1", "b2"], "b1", 1,
            ),
            ("a2", "b2"),
        )

    def test_synchronized_page_shift_preserves_page_offset(self):
        self.assertEqual(
            synchronized_page_shift(
                ["a0", "a1", "a2"], "a0",
                ["b0", "b1", "b2", "b3"], "b1", 1,
            ),
            ("a1", "b2"),
        )

    def test_synchronized_page_shift_stops_if_either_side_is_at_edge(self):
        self.assertIsNone(synchronized_page_shift(
            ["a0", "a1", "a2"], "a1",
            ["b0", "b1"], "b1", 1,
        ))
        self.assertIsNone(synchronized_page_shift(
            ["a0", "a1"], "a0",
            ["b0", "b1", "b2"], "b1", -1,
        ))

    def test_tile_builds_darker_image_and_highlight_mask(self):
        renderer = SchematicTileRenderer()
        images = {
            name: np.empty((4, 4, 4), dtype=np.uint8)
            for name in ("a", "b", "darker")
        }
        mask = np.empty((4, 4, 4), dtype=np.uint8)

        def render(path, _page_index, _page_size, _canvas_size,
                   _bounds, _scale, output):
            output.fill(255)
            if path == "a.pdf":
                output[1, 1, :3] = 0
            else:
                output[2, 2, :3] = 0
            return output

        renderer._render_page = mock.Mock(side_effect=render)
        metadata = {
            "canvas_size": (4.0, 4.0),
            "page_size_a": (4.0, 4.0),
            "page_size_b": (4.0, 4.0),
            "pdf_a": "a.pdf",
            "pdf_b": "b.pdf",
            "page_index_a": 0,
            "page_index_b": 1,
        }

        result = renderer.render_tile(
            metadata, 1.0, 0, 0,
            image_buffers=images,
            mask_buffer=mask,
            gutter=0,
        )

        self.assertEqual(result["pixel_size"], (4, 4))
        self.assertTrue(result["has_mask"])
        np.testing.assert_array_equal(
            images["darker"], np.minimum(images["a"], images["b"])
        )
        self.assertGreater(np.count_nonzero(mask), 0)
        np.testing.assert_array_equal(mask[:, :, 0], 0)
        np.testing.assert_array_equal(mask[:, :, 1], 0)
        np.testing.assert_array_equal(mask[:, :, 2], mask[:, :, 3])

    def test_pdfium_renders_directly_into_reusable_color_buffer(self):
        renderer = SchematicTileRenderer()
        output = np.empty((4, 4, 4), dtype=np.uint8)
        page = mock.MagicMock()

        def render(**options):
            bitmap = options["bitmap_maker"](4, 4, 4, False)
            bitmap.to_numpy()[:] = 42
            return bitmap

        page.render.side_effect = render
        renderer._page = mock.Mock(return_value=page)

        result = renderer._render_page(
            "sch.pdf", 2, (4.0, 4.0), (4.0, 4.0),
            (0.0, 0.0, 4.0, 4.0), 1.0, output,
        )

        self.assertIs(result, output)
        np.testing.assert_array_equal(
            result, np.full((4, 4, 4), 42, dtype=np.uint8)
        )
        renderer._page.assert_called_once_with("sch.pdf", 2)

    def test_unchanged_tile_skips_highlight_blur_and_mask_resource(self):
        renderer = SchematicTileRenderer()
        images = {
            name: np.empty((4, 4, 4), dtype=np.uint8)
            for name in ("a", "b", "darker")
        }
        mask = np.empty((4, 4, 4), dtype=np.uint8)
        renderer._render_page = mock.Mock(
            side_effect=lambda *_args: _args[-1].fill(255) or _args[-1]
        )
        metadata = {
            "canvas_size": (4.0, 4.0),
            "page_size_a": (4.0, 4.0),
            "page_size_b": (4.0, 4.0),
            "pdf_a": "a.pdf",
            "pdf_b": "b.pdf",
            "page_index_a": 0,
            "page_index_b": 0,
        }

        with mock.patch("sch_diff_tiles.finish_merged_mask") as finish:
            result = renderer.render_tile(
                metadata, 1.0, 0, 0,
                image_buffers=images,
                mask_buffer=mask,
                gutter=0,
            )

        self.assertFalse(result["has_mask"])
        finish.assert_not_called()

    def test_centered_partial_page_uses_padded_fallback(self):
        renderer = SchematicTileRenderer()
        output = np.empty((6, 6, 4), dtype=np.uint8)
        page = mock.MagicMock()

        def render(**options):
            self.assertNotIn("bitmap_maker", options)
            bitmap = mock.MagicMock()
            bitmap.to_numpy.return_value = np.full(
                (2, 2, 4), 42, dtype=np.uint8
            )
            return bitmap

        page.render.side_effect = render
        renderer._page = mock.Mock(return_value=page)

        renderer._render_page(
            "sch.pdf", 0, (2.0, 2.0), (6.0, 6.0),
            (0.0, 0.0, 6.0, 6.0), 1.0, output,
        )

        expected = np.full((6, 6, 4), 255, dtype=np.uint8)
        expected[2:4, 2:4] = 42
        np.testing.assert_array_equal(output, expected)


if __name__ == "__main__":
    unittest.main()
