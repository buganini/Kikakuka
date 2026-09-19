import os
import tempfile
import unittest
from unittest import mock

import numpy as np

from legacy_pcb_diff import (
    PcbLegacyRenderer,
    legacy_compare_layer,
    legacy_finish_layer_mask,
)
from pcb_diff_tiles import (
    PcbTileRenderer,
    _accumulate_alpha,
    _finish_alpha,
    alpha_composite,
    choose_coarse_render_scale,
    choose_render_scale,
    choose_fallback_scale,
    clipped_tile_geometry,
    combine_layer_images,
    finish_merged_mask,
    pixel_aligned_page_layout,
    select_fallback_results,
    tile_bounds,
    visible_tile_indices,
)


class PcbDiffTileGeometryTests(unittest.TestCase):
    def test_render_scale_uses_ceiling_discrete_lod(self):
        self.assertEqual(choose_render_scale(0.2), 0.25)
        self.assertEqual(choose_render_scale(0.5), 0.5)
        self.assertEqual(choose_render_scale(0.51), 1.0)
        self.assertEqual(choose_render_scale(0.8), 1.0)
        self.assertEqual(choose_render_scale(1.01), 2.0)
        self.assertEqual(choose_render_scale(20.0), 8.0)

    def test_render_scale_accounts_for_physical_pixel_density(self):
        self.assertEqual(choose_render_scale(0.5, pixel_density=2.0), 1.0)
        self.assertEqual(choose_render_scale(0.8, pixel_density=2.0), 2.0)

    def test_fallback_uses_nearest_cached_lod(self):
        self.assertEqual(choose_fallback_scale((0.5, 2.0), 1.0), 0.5)
        self.assertEqual(choose_fallback_scale((1.0, 4.0), 2.0), 1.0)
        self.assertIsNone(choose_fallback_scale((), 2.0))

    def test_coarse_fallback_remains_below_nearest_cached_lod(self):
        coarse = {"name": "coarse"}
        near = {"name": "near"}
        far = {"name": "far"}
        self.assertEqual(
            select_fallback_results(
                [coarse], {0.5: [far], 2.0: [near]}, 2.5
            ),
            [coarse, near],
        )

    def test_coarse_lod_fits_complete_page_in_one_tile(self):
        self.assertEqual(
            choose_coarse_render_scale((841.896, 595.296)), 0.5
        )
        large_scale = choose_coarse_render_scale((5000.0, 3000.0))
        self.assertLess(large_scale, 0.25)
        self.assertLessEqual(round(5000.0 * large_scale), 512)
        self.assertLessEqual(round(3000.0 * large_scale), 512)

    def test_visible_tiles_are_clipped_and_center_first(self):
        tiles = visible_tile_indices(
            canvas_size=(1000.0, 800.0),
            viewport_size=(500, 400),
            view_transform=(-250.0, -200.0, 1.0),
            render_scale=1.0,
        )
        self.assertEqual(set(tiles), {(0, 0), (1, 0), (0, 1), (1, 1)})
        self.assertIn(tiles[0], {(0, 0), (1, 0), (0, 1), (1, 1)})

    def test_edge_tile_is_shorter_than_regular_tile(self):
        self.assertEqual(
            tile_bounds((600.0, 550.0), 1.0, 1, 1),
            (512.0, 512.0, 88.0, 38.0),
        )

    def test_fractional_page_size_uses_one_global_pixel_grid(self):
        first = tile_bounds((841.896, 595.296), 1.0, 0, 0)
        second = tile_bounds((841.896, 595.296), 1.0, 1, 0)

        self.assertEqual(first, (0.0, 0.0, 512.0, 512.0))
        self.assertEqual(second, (512.0, 0.0, 330.0, 512.0))
        self.assertEqual(first[0] + first[2], second[0])
        self.assertEqual(second[0] + second[2], 842.0)

    def test_smaller_page_is_centered_with_integer_pixel_offset(self):
        canvas_pixels, page_pixels, offset = pixel_aligned_page_layout(
            (90.1, 80.1), (100.3, 90.3), 2.0
        )

        self.assertEqual(canvas_pixels, (201, 181))
        self.assertEqual(page_pixels, (180, 160))
        self.assertEqual(offset, (10, 10))

    def test_cursor_clip_never_changes_tile_sampling_geometry(self):
        first = clipped_tile_geometry(
            (0.0, 0.0, 512.0, 512.0),
            (512, 512),
            (11.25, 7.75, 1.2),
            0.0,
            100.1,
        )
        second = clipped_tile_geometry(
            (0.0, 0.0, 512.0, 512.0),
            (512, 512),
            (11.25, 7.75, 1.2),
            0.0,
            100.2,
        )

        self.assertEqual(first["source"], (0, 0, 512, 512))
        self.assertEqual(first["source"], second["source"])
        self.assertEqual(first["destination"], second["destination"])
        self.assertEqual(first["clip"], second["clip"])


class PcbDiffTileImageTests(unittest.TestCase):
    def test_uint32_accumulator_composites_without_float_buffers(self):
        premultiplied = np.zeros((1, 1, 3), dtype=np.uint32)
        alpha = np.zeros((1, 1, 1), dtype=np.uint32)
        source = np.array([[[10, 20, 30, 255]]], dtype=np.uint8)

        _accumulate_alpha(premultiplied, alpha, source, opacity=0.8)
        output = _finish_alpha(premultiplied, alpha)

        self.assertEqual(premultiplied.dtype, np.uint32)
        self.assertEqual(alpha.dtype, np.uint32)
        np.testing.assert_array_equal(output[:, :, :3], source[:, :, :3])
        self.assertEqual(int(output[0, 0, 3]), 204)

    def test_alpha_composite_applies_layer_opacity(self):
        destination = np.array([[[255, 255, 255, 0]]], dtype=np.uint8)
        source = np.array([[[10, 20, 30, 255]]], dtype=np.uint8)

        output = alpha_composite(destination, source, opacity=0.8)

        np.testing.assert_array_equal(output[:, :, :3], source[:, :, :3])
        self.assertEqual(int(output[0, 0, 3]), 204)

    def test_darker_uses_minimum_color_and_maximum_alpha(self):
        image_a = np.array([[[10, 80, 30, 0]]], dtype=np.uint8)
        image_b = np.array([[[20, 40, 60, 255]]], dtype=np.uint8)

        darker, binary_mask = combine_layer_images(image_a, image_b)

        np.testing.assert_array_equal(
            darker, np.array([[[10, 40, 30, 255]]], dtype=np.uint8)
        )
        self.assertEqual(int(binary_mask[0, 0]), 255)

    def test_alpha_only_difference_is_detected(self):
        image_a = np.array([[[255, 255, 255, 0]]], dtype=np.uint8)
        image_b = np.array([[[255, 255, 255, 255]]], dtype=np.uint8)

        _, binary_mask = combine_layer_images(image_a, image_b)

        self.assertEqual(int(binary_mask[0, 0]), 255)

    def test_merged_mask_keeps_shape_and_expands_change(self):
        binary_mask = np.zeros((64, 64), dtype=np.uint8)
        binary_mask[32, 32] = 255

        mask = finish_merged_mask(binary_mask)

        self.assertEqual(mask.shape, binary_mask.shape)
        self.assertGreater(np.count_nonzero(mask), 1)


class PcbDiffRendererBlockTests(unittest.TestCase):
    def setUp(self):
        self.image_a = np.zeros((4, 4, 4), dtype=np.uint8)
        self.image_a[:, :, :3] = 255
        self.image_b = self.image_a.copy()
        self.image_b[1, 1] = [10, 20, 30, 255]
        self.metadata = {
            "canvas_size": (4.0, 4.0),
            "page_size_a": (4.0, 4.0),
            "page_size_b": (4.0, 4.0),
            "layer_pdfs": {"F.Cu": ("a.pdf", "b.pdf")},
        }

    def test_legacy_block_renders_full_page_artifacts(self):
        renderer = PcbLegacyRenderer()
        renderer._render_layer = mock.Mock(
            side_effect=lambda path, *_args: (
                self.image_a.copy() if path == "a.pdf"
                else self.image_b.copy()
            )
        )
        with tempfile.TemporaryDirectory() as output_root:
            result = renderer.render_full_page(
                self.metadata, ["F.Cu"], output_root, render_scale=1.0
            )

            self.assertEqual(result["pixel_size"], (4, 4))
            self.assertTrue(os.path.exists(result["mask"]))
            self.assertTrue(os.path.exists(
                result["layers"]["F.Cu"]["darker"]
            ))

    def test_viewport_block_renders_composited_tile_artifacts(self):
        renderer = PcbTileRenderer()
        renderer._render_layer = mock.Mock(
            side_effect=lambda path, *_args: (
                self.image_a.copy() if path == "a.pdf"
                else self.image_b.copy()
            )
        )
        with tempfile.TemporaryDirectory() as output_root:
            result = renderer.render_tile(
                self.metadata, ["F.Cu"], 1.0, 0, 0, output_root
            )

            self.assertEqual(result["pixel_size"], (4, 4))
            self.assertTrue(os.path.exists(result["mask"]))
            for path in result["images"].values():
                self.assertTrue(os.path.exists(path))

    def test_viewport_renderer_reuses_and_closes_pdf_page(self):
        renderer = PcbTileRenderer()
        document = mock.MagicMock()
        page = mock.MagicMock()
        document.__getitem__.return_value = page
        close_order = []
        page.close.side_effect = lambda: close_order.append("page")
        document.close.side_effect = lambda: close_order.append("document")

        with mock.patch(
                "pcb_diff_tiles.pdfium.PdfDocument",
                return_value=document) as pdf_document:
            self.assertIs(renderer._page("board.pdf"), page)
            self.assertIs(renderer._page("board.pdf"), page)
            renderer.close()

        pdf_document.assert_called_once_with("board.pdf")
        document.__getitem__.assert_called_once_with(0)
        self.assertEqual(close_order, ["page", "document"])

    def test_legacy_block_preserves_alpha_blind_difference(self):
        transparent = np.array([[[255, 255, 255, 0]]], dtype=np.uint8)
        opaque = np.array([[[255, 255, 255, 255]]], dtype=np.uint8)

        _, binary_mask = legacy_compare_layer(transparent, opaque)

        self.assertEqual(int(binary_mask[0, 0]), 0)

    def test_legacy_mask_block_can_be_called_independently(self):
        binary_mask = np.zeros((64, 64), dtype=np.uint8)
        binary_mask[32, 32] = 255

        mask = legacy_finish_layer_mask(binary_mask)

        self.assertEqual(mask.shape, binary_mask.shape)
        self.assertGreater(np.count_nonzero(mask), 1)


if __name__ == "__main__":
    unittest.main()
