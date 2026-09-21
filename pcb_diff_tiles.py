import ctypes
import glob
import math
import os
import time

import cv2
import numpy as np
import pypdfium2 as pdfium

from pdf_tile_scheduler import (
    choose_coarse_render_scale,
    choose_fallback_scale,
    select_fallback_results,
)


TILE_SIZE = 512
TILE_GUTTER = 24
RENDER_SCALES = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
LAYER_ALPHA_THRESHOLD = 127


class _UnexpectedPdfBitmapSize(ValueError):
    pass


# KiCad's built-in default board theme, stored as OpenCV BGR plus alpha.
_STANDARD_COPPER_RGB = (
    (200, 52, 52), (127, 200, 127), (206, 125, 44),
    (79, 203, 203), (219, 98, 139), (167, 165, 198),
    (40, 204, 217), (232, 178, 167), (242, 237, 161),
    (141, 203, 129), (237, 124, 51), (91, 195, 235),
    (247, 111, 142), (167, 165, 198), (40, 204, 217),
    (232, 178, 167), (242, 237, 161), (237, 124, 51),
    (91, 195, 235), (247, 111, 142), (167, 165, 198),
    (40, 204, 217), (232, 178, 167), (242, 237, 161),
    (237, 124, 51), (91, 195, 235), (247, 111, 142),
    (167, 165, 198), (40, 204, 217), (232, 178, 167),
    (242, 237, 161),
)
_STANDARD_LAYER_RGB = {
    "F.Cu": (200, 52, 52),
    "B.Cu": (77, 127, 196),
    "F.Adhesive": (132, 0, 132),
    "B.Adhesive": (0, 0, 132),
    "F.Paste": (180, 160, 154),
    "B.Paste": (0, 194, 194),
    "F.Silkscreen": (242, 237, 161),
    "B.Silkscreen": (232, 178, 167),
    "F.Mask": (216, 100, 255),
    "B.Mask": (2, 255, 238),
    "User.Drawings": (194, 194, 194),
    "User.Comments": (89, 148, 220),
    "User.Eco1": (180, 219, 210),
    "User.Eco2": (216, 200, 82),
    "Edge.Cuts": (208, 210, 205),
    "Margin": (255, 38, 226),
    "F.Courtyard": (255, 38, 226),
    "B.Courtyard": (38, 233, 255),
    "F.Fab": (175, 175, 175),
    "B.Fab": (88, 93, 132),
}
_STANDARD_LAYER_ALPHA = {
    "F.Paste": 0.902,
    "B.Paste": 0.902,
    "F.Mask": 0.4,
    "B.Mask": 0.4,
}
_STANDARD_USER_RGB = (
    (194, 194, 194),
    (89, 148, 220),
    (180, 219, 210),
    (216, 200, 82),
)

PCB_LAYER_PRESETS = (
    "All Copper Layers",
    "All Layers",
    "Back Assembly View",
    "Back Layers",
    "Front Assembly View",
    "Front Layers",
    "Inner Copper Layers",
    "No Layers",
)

_FRONT_ASSEMBLY_LAYERS = {
    "F.Silkscreen", "F.Mask", "F.Fab", "F.Courtyard", "Edge.Cuts",
}
_BACK_ASSEMBLY_LAYERS = {
    "B.Silkscreen", "B.Mask", "B.Fab", "B.Courtyard", "Edge.Cuts",
}

_KICAD_TECH_USER_UI_ORDER = (
    "F.Adhesive",
    "B.Adhesive",
    "F.Paste",
    "B.Paste",
    "F.Silkscreen",
    "B.Silkscreen",
    "F.Mask",
    "B.Mask",
    "User.Drawings",
    "User.Comments",
    "User.Eco1",
    "User.Eco2",
    "Edge.Cuts",
    "Margin",
    "F.Courtyard",
    "B.Courtyard",
    "F.Fab",
    "B.Fab",
)
_KICAD_TECH_USER_UI_RANK = {
    layer: index for index, layer in enumerate(_KICAD_TECH_USER_UI_ORDER)
}


def standard_layer_style(layer):
    """Return KiCad's standard theme color as (BGR, alpha)."""
    rgb = _STANDARD_LAYER_RGB.get(layer)
    if rgb is None and layer.startswith("In") and layer.endswith(".Cu"):
        try:
            index = int(layer[2:-3])
        except ValueError:
            index = 0
        if 1 <= index <= len(_STANDARD_COPPER_RGB):
            rgb = _STANDARD_COPPER_RGB[index]
    if rgb is None and layer.startswith("User."):
        try:
            index = int(layer[5:])
        except ValueError:
            index = 0
        if index > 0:
            rgb = _STANDARD_USER_RGB[(index - 1) % len(_STANDARD_USER_RGB)]
    if rgb is None:
        rgb = (194, 194, 194)
    return (rgb[2], rgb[1], rgb[0]), _STANDARD_LAYER_ALPHA.get(layer, 1.0)


def find_layer_pdf(cache_dir, layer):
    pattern = os.path.join(
        cache_dir,
        "pcb_pdf",
        f"*{layer.replace('.', '_')}.pdf",
    )
    matches = sorted(glob.glob(pattern))
    return matches[0] if matches else None


def pdf_page_size(path):
    if not path:
        return None
    document = pdfium.PdfDocument(path)
    try:
        width, height = document[0].get_size()
        return float(width), float(height)
    finally:
        document.close()


def build_pair_metadata(cache_a, cache_b, layers, canonical_layers=None,
                        layer_names_a=None, layer_names_b=None):
    canonical_layers = canonical_layers or {}
    layer_pdfs = {}
    page_size_a = None
    page_size_b = None

    for layer in layers:
        name_a = layer if layer_names_a is None else layer_names_a.get(layer)
        name_b = layer if layer_names_b is None else layer_names_b.get(layer)
        path_a = find_layer_pdf(cache_a, name_a) if name_a else None
        path_b = find_layer_pdf(cache_b, name_b) if name_b else None
        layer_pdfs[layer] = (path_a, path_b)
        if page_size_a is None and path_a:
            page_size_a = pdf_page_size(path_a)
        if page_size_b is None and path_b:
            page_size_b = pdf_page_size(path_b)

    if page_size_a is None and page_size_b is None:
        raise RuntimeError("KiCad did not produce any PCB layer PDFs")
    if page_size_a is None:
        page_size_a = page_size_b
    if page_size_b is None:
        page_size_b = page_size_a

    canvas_size = (
        max(page_size_a[0], page_size_b[0]),
        max(page_size_a[1], page_size_b[1]),
    )
    return {
        "canvas_size": canvas_size,
        "page_size_a": page_size_a,
        "page_size_b": page_size_b,
        "layer_pdfs": layer_pdfs,
        "layer_styles": {
            layer: standard_layer_style(canonical_layers.get(layer, layer))
            for layer in layers
        },
    }


def choose_render_scale(view_scale, pixel_density=1.0):
    effective_scale = view_scale * pixel_density
    if effective_scale <= 0:
        return RENDER_SCALES[0]
    for render_scale in RENDER_SCALES:
        if render_scale >= effective_scale:
            return render_scale
    render_scale = RENDER_SCALES[-1]
    while True:
        intermediate_scale = render_scale * math.sqrt(2)
        if intermediate_scale >= effective_scale:
            return intermediate_scale
        render_scale *= 2
        if render_scale >= effective_scale:
            return render_scale


def prioritize_selected_layer(layers, selected_layer):
    """Put the selected layer first, which is the renderer's top layer."""
    ordered_layers = list(layers)
    if selected_layer in ordered_layers:
        ordered_layers.remove(selected_layer)
        ordered_layers.insert(0, selected_layer)
    return tuple(ordered_layers)


def display_layer_label(layer, canonical_layers=None):
    """Show the original User.N ID beside a renamed user layer."""
    canonical = (canonical_layers or {}).get(layer, layer)
    if (canonical != layer and canonical.startswith("User.") and
            canonical[5:].isdigit() and int(canonical[5:]) > 0):
        return f"{layer} ({canonical})"
    return layer


def paired_layer_label(canonical, name_a=None, name_b=None):
    """Show one row per canonical layer, including names from both boards."""
    custom_names = list(dict.fromkeys(
        name for name in (name_a, name_b)
        if name and name != canonical
    ))
    display_name = " / ".join(custom_names) if custom_names else canonical
    return display_layer_label(display_name, {display_name: canonical})


def sort_layers_in_kicad_ui_order(layers, canonical_layers=None):
    """Sort board layers like KiCad's LSET::UIOrder()."""
    canonical_layers = canonical_layers or {}

    def sort_key(layer):
        canonical = canonical_layers.get(layer, layer)
        if canonical == "F.Cu":
            return 0, 0
        if canonical.startswith("In") and canonical.endswith(".Cu"):
            try:
                return 0, int(canonical[2:-3])
            except ValueError:
                pass
        if canonical == "B.Cu":
            return 0, 10_000
        if canonical in _KICAD_TECH_USER_UI_RANK:
            return 1, _KICAD_TECH_USER_UI_RANK[canonical]
        if canonical.startswith("User."):
            try:
                return 2, int(canonical[5:])
            except ValueError:
                pass
        return 3, 0

    return sorted(layers, key=sort_key)


def layers_for_preset(layers, preset, canonical_layers=None):
    """Return the available layers included in a KiCad-style preset."""
    if preset == "No Layers":
        return ()
    canonical_layers = canonical_layers or {}

    def canonical(layer):
        return canonical_layers.get(layer, layer)

    def included(layer):
        layer = canonical(layer)
        if preset == "All Layers":
            return True
        if preset == "All Copper Layers":
            return layer.endswith(".Cu") or layer == "Edge.Cuts"
        if preset == "Inner Copper Layers":
            return (
                layer.startswith("In") and layer.endswith(".Cu")
            ) or layer == "Edge.Cuts"
        if preset == "Front Layers":
            return layer.startswith("F.") or layer == "Edge.Cuts"
        if preset == "Back Layers":
            return layer.startswith("B.") or layer == "Edge.Cuts"
        if preset == "Front Assembly View":
            return layer in _FRONT_ASSEMBLY_LAYERS
        if preset == "Back Assembly View":
            return layer in _BACK_ASSEMBLY_LAYERS
        raise ValueError(f"Unknown PCB layer preset: {preset}")

    return tuple(layer for layer in layers if included(layer))


def visible_tile_indices(canvas_size, viewport_size, view_transform,
                         render_scale, tile_size=TILE_SIZE,
                         priority_point=None, priority_lines=()):
    canvas_width, canvas_height = canvas_size
    viewport_width, viewport_height = viewport_size
    offx, offy, view_scale = view_transform
    tile_points = tile_size / render_scale

    left = max(0.0, (0.0 - offx) / view_scale)
    top = max(0.0, (0.0 - offy) / view_scale)
    right = min(canvas_width, (viewport_width - offx) / view_scale)
    bottom = min(canvas_height, (viewport_height - offy) / view_scale)
    if right <= left or bottom <= top:
        return []

    first_x = max(0, int(math.floor(left / tile_points)))
    first_y = max(0, int(math.floor(top / tile_points)))
    last_x = int(math.floor(math.nextafter(right, -math.inf) / tile_points))
    last_y = int(math.floor(math.nextafter(bottom, -math.inf) / tile_points))

    if priority_point is None:
        center_x = (left + right) / (2.0 * tile_points)
        center_y = (top + bottom) / (2.0 * tile_points)
        cursor_tile = None
    else:
        priority_x = (priority_point[0] - offx) / view_scale
        priority_y = (priority_point[1] - offy) / view_scale
        center_x = math.floor(priority_x / tile_points) + 0.5
        center_y = math.floor(priority_y / tile_points) + 0.5
        cursor_tile = (
            int(math.floor(priority_x / tile_points)),
            int(math.floor(priority_y / tile_points)),
        )
    tiles = [
        (tx, ty)
        for ty in range(first_y, last_y + 1)
        for tx in range(first_x, last_x + 1)
    ]
    line_columns = set()
    for line_x in priority_lines:
        if not 0.0 <= line_x <= canvas_width:
            continue
        line_columns.add(int(math.floor(line_x / tile_points)))
        if line_x > 0.0:
            line_columns.add(int(math.floor(
                math.nextafter(line_x, -math.inf) / tile_points
            )))

    def priority(tile):
        tile_x, tile_y = tile
        distance = (
            (tile_x + 0.5 - center_x) ** 2 +
            (tile_y + 0.5 - center_y) ** 2
        )
        if (cursor_tile is not None and
                abs(tile_x - cursor_tile[0]) <= 1 and
                abs(tile_y - cursor_tile[1]) <= 1):
            return 0, distance
        if tile_x in line_columns:
            return 1, (tile_y + 0.5 - center_y) ** 2, distance
        return 2, distance

    tiles.sort(key=priority)
    return tiles


def tile_bounds(canvas_size, render_scale, tile_x, tile_y,
                tile_size=TILE_SIZE):
    pixel_x, pixel_y, pixel_width, pixel_height = tile_pixel_bounds(
        canvas_size, render_scale, tile_x, tile_y, tile_size
    )
    return (
        pixel_x / render_scale,
        pixel_y / render_scale,
        pixel_width / render_scale,
        pixel_height / render_scale,
    )


def tile_pixel_bounds(canvas_size, render_scale, tile_x, tile_y,
                      tile_size=TILE_SIZE):
    canvas_pixel_width = max(1, int(round(canvas_size[0] * render_scale)))
    canvas_pixel_height = max(1, int(round(canvas_size[1] * render_scale)))
    pixel_x = tile_x * tile_size
    pixel_y = tile_y * tile_size
    pixel_width = min(tile_size, canvas_pixel_width - pixel_x)
    pixel_height = min(tile_size, canvas_pixel_height - pixel_y)
    if pixel_width <= 0 or pixel_height <= 0:
        raise ValueError("Tile lies outside the PDF canvas")
    return pixel_x, pixel_y, pixel_width, pixel_height


def pixel_aligned_page_layout(page_size, canvas_size, render_scale):
    canvas_pixel_size = (
        max(1, int(round(canvas_size[0] * render_scale))),
        max(1, int(round(canvas_size[1] * render_scale))),
    )
    page_pixel_size = (
        max(1, int(round(page_size[0] * render_scale))),
        max(1, int(round(page_size[1] * render_scale))),
    )
    page_offset = (
        (canvas_pixel_size[0] - page_pixel_size[0]) // 2,
        (canvas_pixel_size[1] - page_pixel_size[1]) // 2,
    )
    return canvas_pixel_size, page_pixel_size, page_offset


def clipped_tile_geometry(bounds, pixel_size, view_transform,
                          region_left, region_right):
    """Map a complete tile once and return a screen-space clip rectangle."""
    tile_x, tile_y, tile_width, tile_height = bounds
    left = max(tile_x, region_left)
    right = min(tile_x + tile_width, region_right)
    if right <= left:
        return None

    offx, offy, view_scale = view_transform
    dest_left = round(offx + tile_x * view_scale)
    dest_top = round(offy + tile_y * view_scale)
    dest_right = round(offx + (tile_x + tile_width) * view_scale)
    dest_bottom = round(offy + (tile_y + tile_height) * view_scale)
    clip_left = round(offx + left * view_scale)
    clip_right = round(offx + right * view_scale)
    if clip_right <= clip_left or dest_bottom <= dest_top:
        return None
    return {
        "source": (0, 0, pixel_size[0], pixel_size[1]),
        "destination": (
            dest_left,
            dest_top,
            max(1, dest_right - dest_left),
            max(1, dest_bottom - dest_top),
        ),
        "clip": (
            clip_left,
            dest_top,
            clip_right - clip_left,
            dest_bottom - dest_top,
        ),
    }


def mirrored_view_transform(canvas_width, page_width, view_transform):
    """Map source tiles into a canvas mirrored around its vertical center."""
    offx, offy, scale = view_transform
    return canvas_width - offx - page_width * scale, offy, scale


def comparison_regions(page_width, x_left, x_right, flipped=False):
    """Return source-space A, B and overlap regions in display order."""
    if flipped:
        return (
            (page_width - x_left, page_width),
            (0.0, page_width - x_right),
            (page_width - x_right, page_width - x_left),
        )
    return (0.0, x_left), (x_right, page_width), (x_left, x_right)


def combine_layer_images(image_a, image_b):
    """Return the darker image and binary geometry difference for a layer."""
    darker = darker_layer_image(image_a, image_b)
    return darker, binary_layer_difference(image_a, image_b)


def darker_layer_image(image_a, image_b):
    return cv2.min(image_a, image_b)


def binary_layer_occupancy(image, alpha_threshold=LAYER_ALPHA_THRESHOLD):
    _, occupancy = cv2.threshold(
        image, 254 - alpha_threshold, 255, cv2.THRESH_BINARY_INV
    )
    return occupancy


def binary_layer_difference(image_a, image_b,
                            alpha_threshold=LAYER_ALPHA_THRESHOLD):
    occupancy_a = binary_layer_occupancy(image_a, alpha_threshold)
    occupancy_b = binary_layer_occupancy(image_b, alpha_threshold)
    return cv2.bitwise_xor(occupancy_a, occupancy_b)


def finish_merged_mask(binary_mask, scratch=None):
    if binary_mask is None:
        return None
    if scratch is not None:
        cv2.GaussianBlur(binary_mask, (21, 21), 10, dst=scratch)
        cv2.threshold(scratch, 0, 255, cv2.THRESH_BINARY, dst=binary_mask)
        cv2.GaussianBlur(binary_mask, (21, 21), 10, dst=scratch)
        return scratch
    blurred = cv2.GaussianBlur(binary_mask, (21, 21), 10)
    _, extended_mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY)
    return cv2.GaussianBlur(extended_mask, (21, 21), 10)


def _divide_packed_pairs_by_255(value):
    """Round and divide two independent uint16 lanes packed in uint32."""
    value += np.uint32(0x00800080)
    value += (value >> 8) & np.uint32(0x00FF00FF)
    return (value >> 8) & np.uint32(0x00FF00FF)


def _divide_packed_pairs_by_255_inplace(value, scratch):
    """In-place variant using a caller-owned uint32 scratch array."""
    value += np.uint32(0x00800080)
    np.right_shift(value, 8, out=scratch)
    np.bitwise_and(scratch, np.uint32(0x00FF00FF), out=scratch)
    value += scratch
    np.right_shift(value, 8, out=value)
    np.bitwise_and(value, np.uint32(0x00FF00FF), out=value)


def _accumulate_coverage(destination, grayscale, color, opacity=1.0,
                         work_buffers=None, reuse_alpha=False):
    """Source-over colored coverage in packed premultiplied BGRA uint32."""
    opacity_alpha = np.uint32(round(min(1.0, max(0.0, opacity)) * 255.0))
    if work_buffers is not None:
        source_alpha, inverse_source_alpha, output_br, output_ga, temp, shift = (
            work_buffers
        )
        if not reuse_alpha:
            np.subtract(255, grayscale, out=source_alpha, casting="unsafe")
            source_alpha *= opacity_alpha
            source_alpha += 127
            source_alpha //= 255
            np.subtract(255, source_alpha, out=inverse_source_alpha)

        blue, green, red = color
        source_br = np.uint32(blue | (red << 16))
        source_ga = np.uint32(green | (255 << 16))
        np.bitwise_and(
            destination, np.uint32(0x00FF00FF), out=output_br
        )
        np.right_shift(destination, 8, out=output_ga)
        np.bitwise_and(
            output_ga, np.uint32(0x00FF00FF), out=output_ga
        )

        output_br *= inverse_source_alpha
        np.multiply(source_alpha, source_br, out=temp)
        output_br += temp
        _divide_packed_pairs_by_255_inplace(output_br, shift)

        output_ga *= inverse_source_alpha
        np.multiply(source_alpha, source_ga, out=temp)
        output_ga += temp
        _divide_packed_pairs_by_255_inplace(output_ga, shift)

        np.left_shift(output_ga, 8, out=temp)
        np.bitwise_or(output_br, temp, out=destination)
        return

    source_alpha = 255 - grayscale.astype(np.uint32)
    source_alpha *= opacity_alpha
    source_alpha += 127
    source_alpha //= 255
    inverse_source_alpha = 255 - source_alpha

    blue, green, red = color
    source_br = np.uint32(blue | (red << 16))
    source_ga = np.uint32(green | (255 << 16))
    destination_br = destination & np.uint32(0x00FF00FF)
    destination_ga = (destination >> 8) & np.uint32(0x00FF00FF)
    output_br = _divide_packed_pairs_by_255(
        destination_br * inverse_source_alpha + source_br * source_alpha
    )
    output_ga = _divide_packed_pairs_by_255(
        destination_ga * inverse_source_alpha + source_ga * source_alpha
    )
    destination[:] = output_br | (output_ga << 8)


def _coverage_bounds(grayscale, scratch=None):
    """Return the bounding rectangle containing non-white pixels."""
    if scratch is None:
        coverage = cv2.bitwise_not(grayscale)
    else:
        cv2.bitwise_not(grayscale, dst=scratch)
        coverage = scratch
    return cv2.boundingRect(coverage)


def _union_bounds(bounds_a, bounds_b):
    """Return the smallest rectangle containing both non-empty bounds."""
    x_a, y_a, width_a, height_a = bounds_a
    x_b, y_b, width_b, height_b = bounds_b
    if width_a == 0 or height_a == 0:
        return bounds_b
    if width_b == 0 or height_b == 0:
        return bounds_a
    left = min(x_a, x_b)
    top = min(y_a, y_b)
    right = max(x_a + width_a, x_b + width_b)
    bottom = max(y_a + height_a, y_b + height_b)
    return left, top, right - left, bottom - top


def _intersect_bounds(bounds_a, bounds_b):
    left = max(bounds_a[0], bounds_b[0])
    top = max(bounds_a[1], bounds_b[1])
    right = min(bounds_a[0] + bounds_a[2], bounds_b[0] + bounds_b[2])
    bottom = min(bounds_a[1] + bounds_a[3], bounds_b[1] + bounds_b[3])
    return left, top, max(0, right - left), max(0, bottom - top)


def _copy_rectangle_difference(source, destination, outer, inner):
    """Copy outer except inner, which already has its own composite."""
    x, y, width, height = outer
    if width == 0 or height == 0:
        return
    inner_x, inner_y, inner_width, inner_height = inner
    if inner_width == 0 or inner_height == 0:
        destination[y:y + height, x:x + width] = source[
            y:y + height, x:x + width
        ]
        return
    right = x + width
    bottom = y + height
    inner_right = inner_x + inner_width
    inner_bottom = inner_y + inner_height
    destination[y:inner_y, x:right] = source[y:inner_y, x:right]
    destination[inner_bottom:bottom, x:right] = source[
        inner_bottom:bottom, x:right
    ]
    destination[inner_y:inner_bottom, x:inner_x] = source[
        inner_y:inner_bottom, x:inner_x
    ]
    destination[inner_y:inner_bottom, inner_right:right] = source[
        inner_y:inner_bottom, inner_right:right
    ]


def _accumulate_coverage_bounds(destination, grayscale, color, opacity,
                                bounds, work_buffers=None, reuse_alpha=False):
    x, y, width, height = bounds
    if width == 0 or height == 0:
        return
    bounded_work_buffers = None
    if work_buffers is not None:
        pixel_count = width * height
        bounded_work_buffers = tuple(
            buffer.reshape(-1)[:pixel_count].reshape(height, width)
            for buffer in work_buffers
        )
    _accumulate_coverage(
        destination[y:y + height, x:x + width],
        grayscale[y:y + height, x:x + width],
        color,
        opacity,
        bounded_work_buffers,
        reuse_alpha=reuse_alpha,
    )


def _accumulate_bounded_coverage(destination, grayscale, color,
                                 opacity=1.0):
    """Composite only the bounding rectangle containing non-white pixels."""
    _accumulate_coverage_bounds(
        destination,
        grayscale,
        color,
        opacity,
        _coverage_bounds(grayscale),
    )


def _finish_coverage(premultiplied):
    """Convert fixed-point colored coverage to a straight-alpha BGRA image."""
    output = np.empty((*premultiplied.shape, 4), dtype=np.uint8)
    alpha = premultiplied >> 24
    nonzero = alpha > 0
    for channel, shift in enumerate((0, 8, 16)):
        value = (premultiplied >> shift) & 255
        straight = np.zeros_like(value, dtype=np.uint32)
        np.floor_divide(
            value * 255,
            alpha,
            out=straight,
            where=nonzero,
        )
        output[:, :, channel] = straight.clip(0, 255).astype(np.uint8)
    output[:, :, 3] = alpha.astype(np.uint8)
    return output


class PcbTileRenderer:
    def __init__(self):
        self._documents = {}
        self._pages = {}
        self._uint8_workspace = None
        self._uint32_workspace = None

    def _uint8_buffers(self, count, height, width):
        """Borrow reusable contiguous uint8 image buffers for one tile."""
        pixel_count = height * width
        if (self._uint8_workspace is None or
                self._uint8_workspace.shape[0] < count or
                self._uint8_workspace.shape[1] < pixel_count):
            self._uint8_workspace = np.empty(
                (count, pixel_count), dtype=np.uint8
            )
        return tuple(
            self._uint8_workspace[index, :pixel_count].reshape(height, width)
            for index in range(count)
        )

    def _uint32_buffers(self, count, height, width):
        """Borrow reusable contiguous uint32 compositing work buffers."""
        pixel_count = height * width
        if (self._uint32_workspace is None or
                self._uint32_workspace.shape[0] < count or
                self._uint32_workspace.shape[1] < pixel_count):
            self._uint32_workspace = np.empty(
                (count, pixel_count), dtype=np.uint32
            )
        return tuple(
            self._uint32_workspace[index, :pixel_count].reshape(height, width)
            for index in range(count)
        )

    def close(self):
        for page in self._pages.values():
            page.close()
        self._pages.clear()
        for document in self._documents.values():
            document.close()
        self._documents.clear()
        self._uint8_workspace = None
        self._uint32_workspace = None

    def _document(self, path):
        document = self._documents.get(path)
        if document is None:
            document = pdfium.PdfDocument(path)
            self._documents[path] = document
        return document

    def _page(self, path):
        page = self._pages.get(path)
        if page is None:
            page = self._document(path)[0]
            self._pages[path] = page
        return page

    @staticmethod
    def _blank(width, height, grayscale=True):
        if grayscale:
            return np.full((height, width), 255, dtype=np.uint8)
        image = np.zeros((height, width, 4), dtype=np.uint8)
        image[:, :, :3] = 255
        return image

    def _render_layer(self, path, page_size, canvas_size, bounds,
                      render_scale, grayscale=True, output=None):
        x, y, width, height = bounds
        request_left = int(round(x * render_scale))
        request_top = int(round(y * render_scale))
        request_right = int(round((x + width) * render_scale))
        request_bottom = int(round((y + height) * render_scale))
        pixel_width = max(1, request_right - request_left)
        pixel_height = max(1, request_bottom - request_top)
        if output is not None:
            if not grayscale:
                raise ValueError("Direct PDF rendering requires grayscale")
            if output.shape != (pixel_height, pixel_width):
                raise ValueError("PDF render buffer has the wrong shape")
        if not path:
            if output is None:
                return self._blank(pixel_width, pixel_height, grayscale)
            output.fill(255)
            return output

        page_width, page_height = page_size
        _canvas_pixel_size, page_pixel_size, page_offset = (
            pixel_aligned_page_layout(page_size, canvas_size, render_scale)
        )
        page_left, page_top = page_offset
        page_right = page_left + page_pixel_size[0]
        page_bottom = page_top + page_pixel_size[1]

        intersection_left = max(request_left, page_left)
        intersection_top = max(request_top, page_top)
        intersection_right = min(request_right, page_right)
        intersection_bottom = min(request_bottom, page_bottom)
        if (intersection_right <= intersection_left or
                intersection_bottom <= intersection_top):
            if output is None:
                return self._blank(pixel_width, pixel_height, grayscale)
            output.fill(255)
            return output

        local_left = intersection_left - page_left
        local_top = intersection_top - page_top
        local_right = intersection_right - page_left
        local_bottom = intersection_bottom - page_top
        crop = (
            local_left / render_scale,
            max(0.0, page_height - local_bottom / render_scale),
            max(0.0, page_width - local_right / render_scale),
            local_top / render_scale,
        )
        render_options = {
            "scale": render_scale,
            "rotation": 0,
            "crop": crop,
        }
        if grayscale:
            render_options.update({
                "fill_color": (255, 255, 255, 255),
                "grayscale": True,
            })
        else:
            render_options["fill_color"] = (255, 255, 255, 0)

        target_x = intersection_left - request_left
        target_y = intersection_top - request_top
        render_width = intersection_right - intersection_left
        render_height = intersection_bottom - intersection_top
        if (output is not None and target_x == 0 and target_y == 0 and
                render_width == pixel_width and render_height == pixel_height):
            ctypes_buffer = (ctypes.c_ubyte * output.nbytes).from_buffer(
                output
            )

            def bitmap_maker(width, height, format, rev_byteorder=False):
                if width != pixel_width or height != pixel_height:
                    raise _UnexpectedPdfBitmapSize(
                        "PDFium requested an unexpected bitmap"
                    )
                return pdfium.PdfBitmap.new_native(
                    width,
                    height,
                    format,
                    rev_byteorder,
                    buffer=ctypes_buffer,
                    stride=output.strides[0],
                )

            try:
                self._page(path).render(
                    bitmap_maker=bitmap_maker, **render_options
                )
            except _UnexpectedPdfBitmapSize:
                # Fractional scales can round PDFium's crop by one pixel.
                pass
            else:
                return output

        bitmap = self._page(path).render(**render_options).to_numpy()
        copy_width = min(bitmap.shape[1], pixel_width - target_x)
        copy_height = min(bitmap.shape[0], pixel_height - target_y)
        if (output is None and target_x == 0 and target_y == 0 and
                copy_width == pixel_width and copy_height == pixel_height):
            # pypdfium2's NumPy view retains the bitmap buffer owner. Borrow it
            # until this layer has been composited instead of copying the crop.
            return bitmap[:copy_height, :copy_width]

        if output is None:
            output = self._blank(pixel_width, pixel_height, grayscale)
        elif grayscale:
            output.fill(255)
        else:
            output.fill(0)
            output[:, :, :3] = 255
        if copy_width > 0 and copy_height > 0:
            output[
                target_y:target_y + copy_height,
                target_x:target_x + copy_width,
            ] = bitmap[:copy_height, :copy_width]
        return output

    def render_tile(self, metadata, layers, render_scale, tile_x, tile_y,
                    output_root=None, gutter=TILE_GUTTER,
                    return_image_data=False, composite_buffers=None,
                    mask_buffer=None):
        canvas_size = metadata["canvas_size"]
        pixel_x, pixel_y, pixel_width, pixel_height = tile_pixel_bounds(
            canvas_size, render_scale, tile_x, tile_y
        )
        canvas_pixel_width = max(
            1, int(round(canvas_size[0] * render_scale))
        )
        canvas_pixel_height = max(
            1, int(round(canvas_size[1] * render_scale))
        )
        extended_pixel_x = max(0, pixel_x - gutter)
        extended_pixel_y = max(0, pixel_y - gutter)
        extended_pixel_right = min(
            canvas_pixel_width, pixel_x + pixel_width + gutter
        )
        extended_pixel_bottom = min(
            canvas_pixel_height, pixel_y + pixel_height + gutter
        )
        extended_bounds = (
            extended_pixel_x / render_scale,
            extended_pixel_y / render_scale,
            (extended_pixel_right - extended_pixel_x) / render_scale,
            (extended_pixel_bottom - extended_pixel_y) / render_scale,
        )
        crop_x = pixel_x - extended_pixel_x
        crop_y = pixel_y - extended_pixel_y
        crop_width = pixel_width
        crop_height = pixel_height

        if output_root is not None:
            os.makedirs(output_root, exist_ok=True)
        extended_pixel_width = extended_pixel_right - extended_pixel_x
        extended_pixel_height = extended_pixel_bottom - extended_pixel_y
        external_composites = composite_buffers is not None
        if external_composites:
            composites = composite_buffers
            for accumulator in composites.values():
                accumulator.fill(0)
        else:
            composites = {
                name: np.zeros(
                    (extended_pixel_height, extended_pixel_width),
                    dtype=np.uint32,
                )
                for name in ("a", "b", "darker")
            }
        (image_a_buffer, image_b_buffer, darker_buffer, merged_binary_mask,
         mask_scratch, coverage_scratch) = self._uint8_buffers(
            6, extended_pixel_height, extended_pixel_width
        )
        composite_work_buffers = self._uint32_buffers(
            6, extended_pixel_height, extended_pixel_width
        )
        merged_binary_mask.fill(0)
        has_layers = False
        dirty_bounds = (0, 0, 0, 0)
        pdf_render_seconds = 0.0
        composite_seconds = 0.0
        for layer in reversed(layers):
            has_layers = True
            path_a, path_b = metadata["layer_pdfs"].get(
                layer, (None, None)
            )
            pdf_started = time.perf_counter()
            image_a = self._render_layer(
                path_a,
                metadata["page_size_a"],
                canvas_size,
                extended_bounds,
                render_scale,
                output=image_a_buffer,
            )
            image_b = self._render_layer(
                path_b,
                metadata["page_size_b"],
                canvas_size,
                extended_bounds,
                render_scale,
                output=image_b_buffer,
            )
            pdf_render_seconds += time.perf_counter() - pdf_started
            composite_started = time.perf_counter()
            layer_equal = np.array_equal(image_a, image_b)
            if not layer_equal:
                cv2.min(image_a, image_b, dst=darker_buffer)
            color, layer_opacity = metadata.get("layer_styles", {}).get(
                layer, standard_layer_style(layer)
            )
            opacity = 0.8 * layer_opacity
            composite_a = image_a
            composite_b = image_a if layer_equal else image_b
            composite_darker = image_a if layer_equal else darker_buffer
            if external_composites:
                composite_a = composite_a[
                    crop_y:crop_y + crop_height,
                    crop_x:crop_x + crop_width,
                ]
                composite_b = composite_b[
                    crop_y:crop_y + crop_height,
                    crop_x:crop_x + crop_width,
                ]
                composite_darker = composite_darker[
                    crop_y:crop_y + crop_height,
                    crop_x:crop_x + crop_width,
                ]
            bounds_scratch = coverage_scratch.reshape(-1)[
                :composite_a.size
            ].reshape(composite_a.shape)
            if not layer_equal:
                cv2.compare(
                    composite_a, composite_b, cv2.CMP_NE,
                    dst=bounds_scratch,
                )
                difference_bounds = cv2.boundingRect(bounds_scratch)
                expanded_bounds = _union_bounds(
                    dirty_bounds, difference_bounds
                )
                if expanded_bounds != dirty_bounds:
                    # Outside the old dirty region, B and darker still match
                    # A's value from before this layer is composited.
                    for name in ("b", "darker"):
                        _copy_rectangle_difference(
                            composites["a"], composites[name],
                            expanded_bounds, dirty_bounds,
                        )
                    dirty_bounds = expanded_bounds
            bounds_a = _coverage_bounds(composite_a, bounds_scratch)
            _accumulate_coverage_bounds(
                composites["a"], composite_a, color, opacity, bounds_a,
                composite_work_buffers,
            )
            if dirty_bounds[2] and dirty_bounds[3]:
                bounds_b = (
                    bounds_a if layer_equal else
                    _coverage_bounds(composite_b, bounds_scratch)
                )
                # min(A, B) is non-white wherever either image is non-white.
                bounds_darker = _union_bounds(bounds_a, bounds_b)
                bounds_b = _intersect_bounds(bounds_b, dirty_bounds)
                bounds_darker = _intersect_bounds(
                    bounds_darker, dirty_bounds
                )
                _accumulate_coverage_bounds(
                    composites["b"], composite_b, color, opacity, bounds_b,
                    composite_work_buffers,
                    reuse_alpha=layer_equal and bounds_b == bounds_a,
                )
                _accumulate_coverage_bounds(
                    composites["darker"], composite_darker, color, opacity,
                    bounds_darker, composite_work_buffers,
                    reuse_alpha=layer_equal and bounds_darker == bounds_b,
                )
            if not layer_equal:
                threshold = 254 - LAYER_ALPHA_THRESHOLD
                cv2.threshold(
                    image_a, threshold, 255, cv2.THRESH_BINARY_INV,
                    dst=image_a
                )
                cv2.threshold(
                    image_b, threshold, 255, cv2.THRESH_BINARY_INV,
                    dst=image_b
                )
                cv2.bitwise_xor(image_a, image_b, dst=image_a)
                cv2.max(
                    merged_binary_mask, image_a, dst=merged_binary_mask
                )
            composite_seconds += time.perf_counter() - composite_started

        if not has_layers:
            merged_binary_mask = None
        composite_height, composite_width = composites["a"].shape
        full_bounds = (0, 0, composite_width, composite_height)
        for name in ("b", "darker"):
            _copy_rectangle_difference(
                composites["a"], composites[name], full_bounds,
                dirty_bounds,
            )

        image_paths = {}
        image_data = {}
        if not external_composites:
            for name, accumulator in composites.items():
                accumulator = accumulator[
                    crop_y:crop_y + crop_height,
                    crop_x:crop_x + crop_width,
                ]
                image = _finish_coverage(accumulator)
                if return_image_data:
                    image_data[name] = image
                if output_root is not None:
                    path = os.path.join(output_root, f"{name}.png")
                    if not cv2.imwrite(path, image):
                        raise RuntimeError(f"Could not write PCB tile {path}")
                    image_paths[name] = path

        mask_path = None
        mask = finish_merged_mask(merged_binary_mask, mask_scratch)
        if mask is not None:
            mask = mask[
                crop_y:crop_y + crop_height,
                crop_x:crop_x + crop_width,
            ]
            if mask_buffer is not None:
                mask_buffer[:] = mask[:, :, np.newaxis]
            else:
                mask = cv2.merge([mask, mask, mask, mask])
                if output_root is not None:
                    mask_path = os.path.join(output_root, "mask.png")
                    if not cv2.imwrite(mask_path, mask):
                        raise RuntimeError(
                            f"Could not write PCB tile {mask_path}"
                        )

        result = {
            "bounds": (
                pixel_x / render_scale,
                pixel_y / render_scale,
                pixel_width / render_scale,
                pixel_height / render_scale,
            ),
            "pixel_size": (crop_width, crop_height),
            "images": image_paths,
            "mask": mask_path,
            "has_mask": mask is not None,
            "pdf_render_ms": pdf_render_seconds * 1000,
            "composite_ms": composite_seconds * 1000,
        }
        if return_image_data:
            result["image_data"] = image_data
            result["mask_data"] = mask
        return result
