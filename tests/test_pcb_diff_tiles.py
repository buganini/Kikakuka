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
    _accumulate_coverage,
    _accumulate_bounded_coverage,
    _coverage_bounds,
    _finish_coverage,
    _union_bounds,
    binary_layer_occupancy,
    choose_coarse_render_scale,
    choose_render_scale,
    choose_fallback_scale,
    clipped_tile_geometry,
    combine_layer_images,
    finish_merged_mask,
    pixel_aligned_page_layout,
    select_fallback_results,
    standard_layer_style,
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
        self.assertEqual(tiles[0], (0, 0))

    def test_center_tile_precedes_surrounding_tiles(self):
        tiles = visible_tile_indices(
            canvas_size=(1536.0, 1536.0),
            viewport_size=(1536, 1536),
            view_transform=(0.0, 0.0, 1.0),
            render_scale=1.0,
        )

        self.assertEqual(tiles[0], (1, 1))

    def test_cursor_tile_precedes_viewport_center(self):
        tiles = visible_tile_indices(
            canvas_size=(1536.0, 1536.0),
            viewport_size=(1536, 1536),
            view_transform=(0.0, 0.0, 1.0),
            render_scale=1.0,
            priority_point=(1300.0, 200.0),
        )

        self.assertEqual(tiles[0], (2, 0))

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
    def test_grayscale_coverage_reapplies_color_with_uint32(self):
        premultiplied = np.zeros((1, 2), dtype=np.uint32)
        grayscale = np.array([[255, 0]], dtype=np.uint8)

        _accumulate_coverage(
            premultiplied, grayscale, (10, 20, 30), opacity=0.8
        )
        output = _finish_coverage(premultiplied)

        self.assertEqual(premultiplied.dtype, np.uint32)
        np.testing.assert_array_equal(
            output[:, :, :3],
            np.array([[[0, 0, 0], [10, 20, 30]]], dtype=np.uint8),
        )
        np.testing.assert_array_equal(
            output[:, :, 3], np.array([[0, 204]], dtype=np.uint8)
        )

    def test_bounded_compositing_matches_full_array(self):
        grayscale = np.full((8, 8), 255, dtype=np.uint8)
        grayscale[2:6, 3:5] = np.array(
            [[0, 64], [128, 192], [32, 96], [160, 224]],
            dtype=np.uint8,
        )
        expected = np.zeros((8, 8), dtype=np.uint32)
        actual = expected.copy()

        _accumulate_coverage(expected, grayscale, (10, 20, 30), 0.8)
        _accumulate_bounded_coverage(
            actual, grayscale, (10, 20, 30), 0.8
        )

        np.testing.assert_array_equal(actual, expected)

    def test_reused_compositing_work_buffers_match_allocating_path(self):
        random = np.random.default_rng(7)
        expected = random.integers(
            0, np.iinfo(np.uint32).max, (8, 9), dtype=np.uint32
        )
        actual = expected.copy()
        work_buffers = tuple(
            np.empty((8, 9), dtype=np.uint32) for _index in range(6)
        )

        for opacity in (0.25, 0.8, 1.0):
            grayscale = random.integers(
                0, 256, (8, 9), dtype=np.uint8
            )
            _accumulate_coverage(
                expected, grayscale, (10, 120, 240), opacity
            )
            _accumulate_coverage(
                actual, grayscale, (10, 120, 240), opacity, work_buffers
            )

        np.testing.assert_array_equal(actual, expected)

    def test_darker_bounds_are_union_of_source_bounds(self):
        image_a = np.full((8, 8), 255, dtype=np.uint8)
        image_b = image_a.copy()
        image_a[1:3, 2:4] = 0
        image_b[5:7, 4:7] = 0
        darker = np.minimum(image_a, image_b)
        scratch = np.empty_like(image_a)

        bounds = _union_bounds(
            _coverage_bounds(image_a, scratch),
            _coverage_bounds(image_b, scratch),
        )

        self.assertEqual(bounds, _coverage_bounds(darker, scratch))

    def test_standard_theme_uses_canonical_layer_colors(self):
        self.assertEqual(standard_layer_style("F.Cu"), ((52, 52, 200), 1.0))
        self.assertEqual(
            standard_layer_style("F.Mask"), ((255, 100, 216), 0.4)
        )
        self.assertEqual(
            standard_layer_style("User.2"), ((220, 148, 89), 1.0)
        )

    def test_darker_uses_minimum_grayscale_coverage(self):
        image_a = np.array([[10]], dtype=np.uint8)
        image_b = np.array([[20]], dtype=np.uint8)

        darker, binary_mask = combine_layer_images(image_a, image_b)

        np.testing.assert_array_equal(darker, np.array([[10]], dtype=np.uint8))
        self.assertEqual(int(binary_mask[0, 0]), 0)

    def test_alpha_only_difference_is_detected(self):
        image_a = np.array([[255]], dtype=np.uint8)
        image_b = np.array([[0]], dtype=np.uint8)

        _, binary_mask = combine_layer_images(image_a, image_b)

        self.assertEqual(int(binary_mask[0, 0]), 255)

    def test_binary_coverage_matches_the_former_alpha_threshold(self):
        grayscale = np.array([[126, 127, 128, 129]], dtype=np.uint8)

        occupancy = binary_layer_occupancy(grayscale)

        np.testing.assert_array_equal(
            occupancy,
            np.array([[255, 255, 0, 0]], dtype=np.uint8),
        )

    def test_merged_mask_keeps_shape_and_expands_change(self):
        binary_mask = np.zeros((64, 64), dtype=np.uint8)
        binary_mask[32, 32] = 255

        mask = finish_merged_mask(binary_mask)

        self.assertEqual(mask.shape, binary_mask.shape)
        self.assertGreater(np.count_nonzero(mask), 1)

    def test_merged_mask_can_reuse_scratch_buffer(self):
        binary_mask = np.zeros((64, 64), dtype=np.uint8)
        binary_mask[32, 32] = 255
        expected = finish_merged_mask(binary_mask.copy())
        scratch = np.empty_like(binary_mask)

        actual = finish_merged_mask(binary_mask.copy(), scratch)

        self.assertIs(actual, scratch)
        np.testing.assert_array_equal(actual, expected)


class PcbDiffRendererBlockTests(unittest.TestCase):
    def setUp(self):
        self.legacy_image_a = np.zeros((4, 4, 4), dtype=np.uint8)
        self.legacy_image_a[:, :, :3] = 255
        self.legacy_image_b = self.legacy_image_a.copy()
        self.legacy_image_b[1, 1] = [10, 20, 30, 255]
        self.image_a = np.full((4, 4), 255, dtype=np.uint8)
        self.image_b = self.image_a.copy()
        self.image_b[1, 1] = 0
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
                self.legacy_image_a.copy() if path == "a.pdf"
                else self.legacy_image_b.copy()
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
            side_effect=lambda path, *_args, **_kwargs: (
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

    def test_viewport_block_can_return_images_without_disk_artifacts(self):
        renderer = PcbTileRenderer()
        renderer._render_layer = mock.Mock(
            side_effect=lambda path, *_args, **_kwargs: (
                self.image_a.copy() if path == "a.pdf"
                else self.image_b.copy()
            )
        )

        result = renderer.render_tile(
            self.metadata,
            ["F.Cu"],
            1.0,
            0,
            0,
            return_image_data=True,
        )

        self.assertEqual(result["images"], {})
        self.assertIsNone(result["mask"])
        self.assertEqual(set(result["image_data"]), {"a", "b", "darker"})
        for image in result["image_data"].values():
            self.assertEqual(image.shape, (4, 4, 4))
        self.assertEqual(result["mask_data"].shape, (4, 4, 4))

    def test_viewport_block_can_composite_into_owned_buffers(self):
        renderer = PcbTileRenderer()
        renderer._render_layer = mock.Mock(
            side_effect=lambda path, *_args, **_kwargs: (
                self.image_a.copy() if path == "a.pdf"
                else self.image_b.copy()
            )
        )
        composite_buffers = {
            name: np.empty((4, 4), dtype=np.uint32)
            for name in ("a", "b", "darker")
        }
        mask_buffer = np.empty((4, 4, 4), dtype=np.uint8)

        expected = renderer.render_tile(
            self.metadata,
            ["F.Cu"],
            1.0,
            0,
            0,
            return_image_data=True,
        )
        result = renderer.render_tile(
            self.metadata,
            ["F.Cu"],
            1.0,
            0,
            0,
            composite_buffers=composite_buffers,
            mask_buffer=mask_buffer,
        )

        self.assertTrue(result["has_mask"])
        self.assertEqual(result["images"], {})
        self.assertIsNone(result["mask"])
        self.assertGreater(np.count_nonzero(composite_buffers["b"]), 0)
        self.assertGreater(np.count_nonzero(mask_buffer), 0)
        for name, buffer in composite_buffers.items():
            np.testing.assert_array_equal(
                _finish_coverage(buffer),
                expected["image_data"][name],
            )
        np.testing.assert_array_equal(mask_buffer, expected["mask_data"])

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

    def test_full_pdf_crop_borrows_pdfium_numpy_buffer(self):
        renderer = PcbTileRenderer()
        bitmap = np.full((4, 4), 255, dtype=np.uint8)
        page = mock.MagicMock()
        page.render.return_value.to_numpy.return_value = bitmap
        renderer._page = mock.Mock(return_value=page)

        result = renderer._render_layer(
            "board.pdf", (4.0, 4.0), (4.0, 4.0),
            (0.0, 0.0, 4.0, 4.0), 1.0,
        )

        self.assertTrue(np.shares_memory(result, bitmap))

    def test_full_pdf_crop_renders_into_caller_buffer(self):
        renderer = PcbTileRenderer()
        output = np.empty((4, 4), dtype=np.uint8)
        page = mock.MagicMock()

        def render(**options):
            bitmap = options["bitmap_maker"](4, 4, 1, False)
            bitmap.to_numpy()[:] = 42
            return bitmap

        page.render.side_effect = render
        renderer._page = mock.Mock(return_value=page)

        result = renderer._render_layer(
            "board.pdf", (4.0, 4.0), (4.0, 4.0),
            (0.0, 0.0, 4.0, 4.0), 1.0, output=output,
        )

        self.assertIs(result, output)
        np.testing.assert_array_equal(
            result, np.full((4, 4), 42, dtype=np.uint8)
        )

    def test_partial_pdf_crop_is_copied_into_caller_buffer(self):
        renderer = PcbTileRenderer()
        output = np.empty((4, 4), dtype=np.uint8)
        bitmap = np.full((2, 2), 42, dtype=np.uint8)
        page = mock.MagicMock()
        page.render.return_value.to_numpy.return_value = bitmap
        renderer._page = mock.Mock(return_value=page)

        result = renderer._render_layer(
            "board.pdf", (2.0, 2.0), (4.0, 4.0),
            (0.0, 0.0, 4.0, 4.0), 1.0, output=output,
        )

        expected = np.full((4, 4), 255, dtype=np.uint8)
        expected[1:3, 1:3] = 42
        self.assertIs(result, output)
        np.testing.assert_array_equal(result, expected)

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
