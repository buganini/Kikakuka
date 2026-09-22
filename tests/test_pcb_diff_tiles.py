import math
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
    PCB_LAYER_PRESETS,
    PcbTileRenderer,
    _accumulate_coverage,
    _accumulate_bounded_coverage,
    _coverage_bounds,
    _finish_coverage,
    _union_bounds,
    binary_layer_occupancy,
    build_pair_metadata,
    choose_coarse_render_scale,
    choose_render_scale,
    choose_fallback_scale,
    clipped_tile_geometry,
    comparison_regions,
    combine_layer_images,
    display_layer_label,
    finish_merged_mask,
    layers_for_preset,
    layer_label_color,
    mirrored_view_transform,
    paired_layer_label,
    pixel_aligned_page_layout,
    prioritize_selected_layer,
    select_fallback_results,
    sort_layers_in_kicad_ui_order,
    toggle_selected_layer,
    standard_layer_style,
    tile_bounds,
    visible_tile_indices,
)


class PcbDiffTileGeometryTests(unittest.TestCase):
    def test_flipped_view_keeps_a_on_left_and_b_on_right(self):
        self.assertEqual(
            comparison_regions(100.0, 25.0, 35.0),
            ((0.0, 25.0), (35.0, 100.0), (25.0, 35.0)),
        )
        self.assertEqual(
            comparison_regions(100.0, 25.0, 35.0, flipped=True),
            ((75.0, 100.0), (0.0, 65.0), (65.0, 75.0)),
        )

    def test_flipped_view_requests_tiles_at_the_opposite_page_edge(self):
        transform = mirrored_view_transform(
            512, 2048.0, (0.0, 0.0, 1.0)
        )

        self.assertEqual(transform, (-1536.0, 0.0, 1.0))
        self.assertEqual(
            visible_tile_indices(
                (2048.0, 512.0), (512, 512), transform, 1.0
            ),
            [(3, 0)],
        )

    def test_same_user_layer_in_both_boards_has_one_combined_label(self):
        self.assertEqual(
            paired_layer_label("User.2", "User.2", "F.Stiffener"),
            "F.Stiffener (User.2)",
        )
        self.assertEqual(
            paired_layer_label("User.3", "Front Stiffener", "Back Stiffener"),
            "Front Stiffener / Back Stiffener (User.3)",
        )

    def test_layer_label_color_matches_renderer_theme_rgb(self):
        self.assertEqual(layer_label_color("F.Cu"), 0xC83434)
        self.assertEqual(layer_label_color("B.Cu"), 0x4D7FC4)
        self.assertEqual(layer_label_color("User.2"), 0x5994DC)

    def test_pair_metadata_matches_renamed_layers_by_canonical_id(self):
        layer_names_a = {"User.2": "User.2"}
        layer_names_b = {"User.2": "F.Stiffener"}
        with mock.patch("pcb_diff_tiles.find_layer_pdf") as find_pdf, mock.patch(
            "pcb_diff_tiles.pdf_page_size", return_value=(100.0, 80.0)
        ):
            find_pdf.side_effect = lambda cache, name: f"{cache}/{name}.pdf"
            metadata = build_pair_metadata(
                "cache_a", "cache_b", ["User.2"],
                layer_names_a=layer_names_a,
                layer_names_b=layer_names_b,
            )

        self.assertEqual(
            metadata["layer_pdfs"]["User.2"],
            ("cache_a/User.2.pdf", "cache_b/F.Stiffener.pdf"),
        )
        self.assertEqual(
            find_pdf.call_args_list,
            [
                mock.call("cache_a", "User.2"),
                mock.call("cache_b", "F.Stiffener"),
            ],
        )

    def test_pair_metadata_skips_layer_missing_from_one_board(self):
        with mock.patch("pcb_diff_tiles.find_layer_pdf") as find_pdf, mock.patch(
            "pcb_diff_tiles.pdf_page_size", return_value=(100.0, 80.0)
        ):
            find_pdf.return_value = "cache_a/User.2.pdf"
            metadata = build_pair_metadata(
                "cache_a", "cache_b", ["User.2"],
                layer_names_a={"User.2": "User.2"},
                layer_names_b={},
            )

        self.assertEqual(
            metadata["layer_pdfs"]["User.2"],
            ("cache_a/User.2.pdf", None),
        )
        find_pdf.assert_called_once_with("cache_a", "User.2")

    def test_renamed_user_layer_label_shows_canonical_number(self):
        canonical_layers = {
            "Notes": "User.2",
            "Top Copper": "F.Cu",
            "Assembly Notes": "User.Drawings",
        }

        self.assertEqual(
            display_layer_label("Notes", canonical_layers),
            "Notes (User.2)",
        )
        self.assertEqual(
            display_layer_label("User.1", {"User.1": "User.1"}),
            "User.1",
        )
        self.assertEqual(
            display_layer_label("Top Copper", canonical_layers),
            "Top Copper",
        )
        self.assertEqual(
            display_layer_label("Assembly Notes", canonical_layers),
            "Assembly Notes",
        )

    def test_layers_follow_kicad_ui_order(self):
        layers = (
            "User.2", "B.Fab", "In2.Cu", "Edge.Cuts", "F.Mask",
            "F.Cu", "B.Silkscreen", "User.Drawings", "B.Cu",
            "F.Adhesive", "In1.Cu", "F.Fab", "User.1", "B.Mask",
            "F.Silkscreen", "Margin",
        )

        self.assertEqual(
            sort_layers_in_kicad_ui_order(layers),
            [
                "F.Cu", "In1.Cu", "In2.Cu", "B.Cu", "F.Adhesive",
                "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask",
                "User.Drawings", "Edge.Cuts", "Margin", "F.Fab",
                "B.Fab", "User.1", "User.2",
            ],
        )

    def test_layer_order_uses_canonical_names_and_keeps_unknowns_stable(self):
        layers = ("Notes B", "Bottom Copper", "Notes A", "Top Copper")
        canonical_layers = {
            "Bottom Copper": "B.Cu",
            "Top Copper": "F.Cu",
        }

        self.assertEqual(
            sort_layers_in_kicad_ui_order(layers, canonical_layers),
            ["Top Copper", "Bottom Copper", "Notes B", "Notes A"],
        )

    def test_layer_presets_match_kicad_layer_groups(self):
        layers = (
            "F.Cu", "In1.Cu", "B.Cu", "F.Silkscreen", "B.Silkscreen",
            "F.Mask", "B.Mask", "F.Fab", "B.Fab", "F.Courtyard",
            "B.Courtyard", "User.1", "Edge.Cuts",
        )

        self.assertEqual(
            set(layers_for_preset(layers, "All Copper Layers")),
            {"F.Cu", "In1.Cu", "B.Cu", "Edge.Cuts"},
        )
        self.assertEqual(
            set(layers_for_preset(layers, "Inner Copper Layers")),
            {"In1.Cu", "Edge.Cuts"},
        )
        self.assertEqual(
            set(layers_for_preset(layers, "Front Assembly View")),
            {
                "F.Silkscreen", "F.Mask", "F.Fab", "F.Courtyard",
                "Edge.Cuts",
            },
        )
        self.assertEqual(
            set(layers_for_preset(layers, "Back Layers")),
            {
                "B.Cu", "B.Silkscreen", "B.Mask", "B.Fab",
                "B.Courtyard", "Edge.Cuts",
            },
        )
        self.assertIn("All Layers", PCB_LAYER_PRESETS)

    def test_no_layers_is_the_last_preset_and_hides_every_layer(self):
        self.assertEqual(PCB_LAYER_PRESETS[-1], "No Layers")
        self.assertEqual(
            layers_for_preset(("F.Cu", "B.Cu", "Edge.Cuts"), "No Layers"),
            (),
        )

    def test_layer_presets_use_canonical_names_for_renamed_layers(self):
        self.assertEqual(
            layers_for_preset(
                ("Top Copper", "Board Outline", "Notes"),
                "All Copper Layers",
                {
                    "Top Copper": "F.Cu",
                    "Board Outline": "Edge.Cuts",
                    "Notes": "User.Comments",
                },
            ),
            ("Top Copper", "Board Outline"),
        )

    def test_selected_layer_is_prioritized_for_topmost_rendering(self):
        self.assertEqual(
            prioritize_selected_layer(
                ("F.Cu", "B.Cu", "F.Silkscreen"), "B.Cu"
            ),
            ("B.Cu", "F.Cu", "F.Silkscreen"),
        )

    def test_missing_selected_layer_preserves_render_order(self):
        self.assertEqual(
            prioritize_selected_layer(("F.Cu", "B.Cu"), "Edge.Cuts"),
            ("F.Cu", "B.Cu"),
        )

    def test_clicking_selected_layer_again_restores_normal_order(self):
        layers = ("F.Cu", "B.Cu", "F.Silkscreen")
        selected = toggle_selected_layer(None, "B.Cu")
        self.assertEqual(
            prioritize_selected_layer(layers, selected),
            ("B.Cu", "F.Cu", "F.Silkscreen"),
        )
        selected = toggle_selected_layer(selected, "B.Cu")
        self.assertIsNone(selected)
        self.assertEqual(prioritize_selected_layer(layers, selected), layers)

    def test_render_scale_uses_ceiling_discrete_lod(self):
        self.assertEqual(choose_render_scale(0.2), 0.25)
        self.assertEqual(choose_render_scale(0.5), 0.5)
        self.assertEqual(choose_render_scale(0.51), 1.0)
        self.assertEqual(choose_render_scale(0.8), 1.0)
        self.assertEqual(choose_render_scale(1.01), 2.0)
        self.assertEqual(choose_render_scale(8.0), 8.0)
        self.assertAlmostEqual(choose_render_scale(8.01), 8 * math.sqrt(2))
        self.assertEqual(choose_render_scale(16.0), 16.0)
        self.assertAlmostEqual(choose_render_scale(20.0), 16 * math.sqrt(2))
        self.assertEqual(choose_render_scale(32.0), 32.0)

    def test_render_scale_accounts_for_physical_pixel_density(self):
        self.assertEqual(choose_render_scale(0.5, pixel_density=2.0), 1.0)
        self.assertEqual(choose_render_scale(0.8, pixel_density=2.0), 2.0)
        self.assertAlmostEqual(
            choose_render_scale(20.0, pixel_density=2.0),
            32 * math.sqrt(2),
        )

    def test_high_zoom_lod_never_upscales_or_oversamples_by_two(self):
        for effective_scale in (8.01, 12.0, 20.0, 40.0, 64.51, 100.0):
            render_scale = choose_render_scale(effective_scale)
            self.assertGreaterEqual(render_scale, effective_scale)
            self.assertLess(render_scale, effective_scale * math.sqrt(2))

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

    def test_cursor_neighborhood_then_splitter_then_original_order(self):
        tiles = visible_tile_indices(
            canvas_size=(2560.0, 2560.0),
            viewport_size=(2560, 2560),
            view_transform=(0.0, 0.0, 1.0),
            render_scale=1.0,
            priority_point=(100.0, 100.0),
            priority_lines=(1800.0,),
        )

        self.assertEqual(
            set(tiles[:4]), {(0, 0), (1, 0), (0, 1), (1, 1)}
        )
        self.assertEqual(
            tiles[4:9], [(3, 0), (3, 1), (3, 2), (3, 3), (3, 4)]
        )
        self.assertEqual(tiles[9], (2, 0))

    def test_splitter_on_tile_boundary_prioritizes_both_sides(self):
        tiles = visible_tile_indices(
            canvas_size=(2048.0, 512.0),
            viewport_size=(2048, 512),
            view_transform=(0.0, 0.0, 1.0),
            render_scale=1.0,
            priority_lines=(1024.0,),
        )

        self.assertEqual(set(tiles[:2]), {(1, 0), (2, 0)})

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

    def test_reusing_alpha_for_identical_layers_preserves_each_output(self):
        random = np.random.default_rng(17)
        grayscale = random.integers(0, 256, (8, 9), dtype=np.uint8)
        initial = [
            random.integers(
                0, np.iinfo(np.uint32).max, (8, 9), dtype=np.uint32
            )
            for _index in range(3)
        ]
        expected = [image.copy() for image in initial]
        actual = [image.copy() for image in initial]
        work_buffers = tuple(
            np.empty((8, 9), dtype=np.uint32) for _index in range(6)
        )

        for image in expected:
            _accumulate_coverage(image, grayscale, (10, 120, 240), 0.8)
        for index, image in enumerate(actual):
            _accumulate_coverage(
                image, grayscale, (10, 120, 240), 0.8,
                work_buffers, reuse_alpha=index > 0,
            )

        for result, reference in zip(actual, expected):
            np.testing.assert_array_equal(result, reference)

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
        self.assertGreaterEqual(result["pdf_render_ms"], 0.0)
        self.assertGreaterEqual(result["composite_ms"], 0.0)

    def test_viewport_block_renders_empty_layer_selection(self):
        result = PcbTileRenderer().render_tile(
            self.metadata, [], 1.0, 0, 0, return_image_data=True
        )

        self.assertFalse(result["has_mask"])
        self.assertIsNone(result["mask_data"])
        for image in result["image_data"].values():
            self.assertFalse(np.any(image))

    def test_equal_bottom_layer_is_shared_until_top_layer_differs(self):
        bottom = np.full((4, 4), 255, dtype=np.uint8)
        bottom[0, 0] = 0
        top_a = np.full((4, 4), 255, dtype=np.uint8)
        top_a[1, 1] = 0
        top_b = np.full((4, 4), 255, dtype=np.uint8)
        images = {
            "bottom_a.pdf": bottom,
            "bottom_b.pdf": bottom,
            "top_a.pdf": top_a,
            "top_b.pdf": top_b,
        }
        metadata = {
            **self.metadata,
            "layer_pdfs": {
                "F.Cu": ("top_a.pdf", "top_b.pdf"),
                "B.Cu": ("bottom_a.pdf", "bottom_b.pdf"),
            },
        }
        renderer = PcbTileRenderer()
        renderer._render_layer = mock.Mock(
            side_effect=lambda path, *_args, **_kwargs: images[path].copy()
        )

        result = renderer.render_tile(
            metadata, ["F.Cu", "B.Cu"], 1.0, 0, 0,
            return_image_data=True,
        )
        image_a = result["image_data"]["a"]
        image_b = result["image_data"]["b"]
        darker = result["image_data"]["darker"]

        np.testing.assert_array_equal(image_a[0, 0], image_b[0, 0])
        np.testing.assert_array_equal(image_a[0, 0], darker[0, 0])
        self.assertGreater(int(image_a[0, 0, 3]), 0)
        np.testing.assert_array_equal(image_a[1, 1], darker[1, 1])
        self.assertGreater(int(image_a[1, 1, 3]), 0)
        self.assertEqual(int(image_b[1, 1, 3]), 0)

    def test_equal_layers_produce_identical_composites(self):
        renderer = PcbTileRenderer()
        renderer._render_layer = mock.Mock(
            side_effect=lambda _path, *_args, **_kwargs: self.image_b.copy()
        )
        result = renderer.render_tile(
            self.metadata, ["F.Cu"], 1.0, 0, 0,
            return_image_data=True,
        )

        np.testing.assert_array_equal(
            result["image_data"]["a"], result["image_data"]["b"]
        )
        np.testing.assert_array_equal(
            result["image_data"]["a"], result["image_data"]["darker"]
        )

    def test_later_difference_expands_dirty_composite_region(self):
        blank = np.full((8, 8), 255, dtype=np.uint8)
        bottom_a = blank.copy()
        bottom_a[1, 1] = 0
        middle = blank.copy()
        middle[1, 1] = 100
        middle[6, 6] = 0
        top_a = blank.copy()
        top_a[6, 6] = 0
        top_a[0, 7] = 80
        images = {
            "bottom_a.pdf": bottom_a,
            "bottom_b.pdf": blank,
            "middle_a.pdf": middle,
            "middle_b.pdf": middle,
            "top_a.pdf": top_a,
            "top_b.pdf": blank,
        }
        layers = ["F.Cu", "B.Cu", "Edge.Cuts"]
        metadata = {
            "canvas_size": (8.0, 8.0),
            "page_size_a": (8.0, 8.0),
            "page_size_b": (8.0, 8.0),
            "layer_pdfs": {
                "F.Cu": ("top_a.pdf", "top_b.pdf"),
                "B.Cu": ("middle_a.pdf", "middle_b.pdf"),
                "Edge.Cuts": ("bottom_a.pdf", "bottom_b.pdf"),
            },
        }
        renderer = PcbTileRenderer()
        renderer._render_layer = mock.Mock(
            side_effect=lambda path, *_args, **_kwargs: images[path].copy()
        )

        actual = renderer.render_tile(
            metadata, layers, 1.0, 0, 0,
            gutter=0, return_image_data=True,
        )["image_data"]

        expected = {
            name: np.zeros((8, 8), dtype=np.uint32)
            for name in ("a", "b", "darker")
        }
        for layer in reversed(layers):
            path_a, path_b = metadata["layer_pdfs"][layer]
            image_a, image_b = images[path_a], images[path_b]
            color, layer_opacity = standard_layer_style(layer)
            for name, grayscale in (
                ("a", image_a),
                ("b", image_b),
                ("darker", np.minimum(image_a, image_b)),
            ):
                _accumulate_coverage(
                    expected[name], grayscale, color,
                    0.8 * layer_opacity,
                )
        for name, accumulator in expected.items():
            np.testing.assert_array_equal(
                actual[name], _finish_coverage(accumulator),
            )

    def test_dirty_composite_copies_shared_pixels_through_tile_gutter(self):
        blank = np.full((100, 112), 255, dtype=np.uint8)
        bottom_a = blank.copy()
        bottom_a[50, 30] = 0
        top = blank.copy()
        top[50, 100] = 0
        images = {
            "bottom_a.pdf": bottom_a,
            "bottom_b.pdf": blank,
            "top_a.pdf": top,
            "top_b.pdf": top,
        }
        metadata = {
            "canvas_size": (600.0, 100.0),
            "page_size_a": (600.0, 100.0),
            "page_size_b": (600.0, 100.0),
            "layer_pdfs": {
                "F.Cu": ("top_a.pdf", "top_b.pdf"),
                "B.Cu": ("bottom_a.pdf", "bottom_b.pdf"),
            },
        }
        renderer = PcbTileRenderer()
        renderer._render_layer = mock.Mock(
            side_effect=lambda path, *_args, **_kwargs: images[path].copy()
        )

        result = renderer.render_tile(
            metadata, ["F.Cu", "B.Cu"], 1.0, 1, 0,
            return_image_data=True,
        )

        images = result["image_data"]
        self.assertEqual(images["a"].shape, (100, 88, 4))
        self.assertGreater(int(images["a"][50, 76, 3]), 0)
        np.testing.assert_array_equal(
            images["a"][50, 76], images["b"][50, 76],
        )
        np.testing.assert_array_equal(
            images["a"][50, 76], images["darker"][50, 76],
        )

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

    def test_fractional_pdf_crop_falls_back_when_bitmap_size_differs(self):
        renderer = PcbTileRenderer()
        output = np.empty((4, 4), dtype=np.uint8)
        page = mock.MagicMock()
        bitmap = np.full((4, 5), 42, dtype=np.uint8)

        def render(**options):
            if "bitmap_maker" in options:
                options["bitmap_maker"](5, 4, 1, False)
            result = mock.MagicMock()
            result.to_numpy.return_value = bitmap
            return result

        page.render.side_effect = render
        renderer._page = mock.Mock(return_value=page)

        result = renderer._render_layer(
            "board.pdf", (4.0, 4.0), (4.0, 4.0),
            (0.0, 0.0, 4.0, 4.0), 1.0, output=output,
        )

        self.assertIs(result, output)
        np.testing.assert_array_equal(result, np.full((4, 4), 42))
        self.assertEqual(page.render.call_count, 2)

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
