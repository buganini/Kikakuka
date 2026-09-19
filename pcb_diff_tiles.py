import glob
import math
import os

import cv2
import numpy as np
import pypdfium2 as pdfium


TILE_SIZE = 512
TILE_GUTTER = 24
RENDER_SCALES = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
LAYER_ALPHA_THRESHOLD = 127


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


def build_pair_metadata(cache_a, cache_b, layers, canonical_layers=None):
    canonical_layers = canonical_layers or {}
    layer_pdfs = {}
    page_size_a = None
    page_size_b = None

    for layer in layers:
        path_a = find_layer_pdf(cache_a, layer)
        path_b = find_layer_pdf(cache_b, layer)
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
    return RENDER_SCALES[-1]


def choose_fallback_scale(available_scales, render_scale):
    available_scales = tuple(available_scales)
    if not available_scales:
        return None
    return min(
        available_scales,
        key=lambda scale: abs(math.log2(scale / render_scale)),
    )


def select_fallback_results(coarse_results, candidates_by_scale,
                            render_scale):
    results = list(coarse_results)
    fallback_scale = choose_fallback_scale(
        candidates_by_scale, render_scale
    )
    if fallback_scale is not None:
        results.extend(candidates_by_scale[fallback_scale])
    return results


def choose_coarse_render_scale(canvas_size, tile_size=TILE_SIZE):
    width, height = canvas_size
    if width <= 0 or height <= 0:
        raise ValueError("PCB canvas must have a positive size")
    fit_scale = min(tile_size / width, tile_size / height)
    standard_scales = [
        scale for scale in RENDER_SCALES
        if scale <= 0.5 and scale <= fit_scale
    ]
    if standard_scales:
        return standard_scales[-1]
    return min(0.5, fit_scale * (1.0 - 1e-9))


def visible_tile_indices(canvas_size, viewport_size, view_transform,
                         render_scale, tile_size=TILE_SIZE,
                         priority_point=None):
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
    else:
        priority_x = (priority_point[0] - offx) / view_scale
        priority_y = (priority_point[1] - offy) / view_scale
        center_x = math.floor(priority_x / tile_points) + 0.5
        center_y = math.floor(priority_y / tile_points) + 0.5
    tiles = [
        (tx, ty)
        for ty in range(first_y, last_y + 1)
        for tx in range(first_x, last_x + 1)
    ]
    tiles.sort(key=lambda tile: (
        (tile[0] + 0.5 - center_x) ** 2 +
        (tile[1] + 0.5 - center_y) ** 2
    ))
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
        raise ValueError("Tile lies outside the PCB canvas")
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


def finish_merged_mask(binary_mask):
    if binary_mask is None:
        return None
    blurred = cv2.GaussianBlur(binary_mask, (21, 21), 10)
    _, extended_mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY)
    return cv2.GaussianBlur(extended_mask, (21, 21), 10)


def _divide_packed_pairs_by_255(value):
    """Round and divide two independent uint16 lanes packed in uint32."""
    value += np.uint32(0x00800080)
    value += (value >> 8) & np.uint32(0x00FF00FF)
    return (value >> 8) & np.uint32(0x00FF00FF)


def _accumulate_coverage(destination, grayscale, color, opacity=1.0):
    """Source-over colored coverage in packed premultiplied BGRA uint32."""
    opacity_alpha = np.uint32(round(min(1.0, max(0.0, opacity)) * 255.0))
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

    def close(self):
        for page in self._pages.values():
            page.close()
        self._pages.clear()
        for document in self._documents.values():
            document.close()
        self._documents.clear()

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
                      render_scale, grayscale=True):
        x, y, width, height = bounds
        request_left = int(round(x * render_scale))
        request_top = int(round(y * render_scale))
        request_right = int(round((x + width) * render_scale))
        request_bottom = int(round((y + height) * render_scale))
        pixel_width = max(1, request_right - request_left)
        pixel_height = max(1, request_bottom - request_top)
        if not path:
            return self._blank(pixel_width, pixel_height, grayscale)

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
            return self._blank(pixel_width, pixel_height, grayscale)

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
        bitmap = self._page(path).render(**render_options).to_numpy()

        target_x = intersection_left - request_left
        target_y = intersection_top - request_top
        copy_width = min(bitmap.shape[1], pixel_width - target_x)
        copy_height = min(bitmap.shape[0], pixel_height - target_y)
        if (target_x == 0 and target_y == 0 and
                copy_width == pixel_width and copy_height == pixel_height):
            return bitmap[:copy_height, :copy_width].copy()

        output = self._blank(pixel_width, pixel_height, grayscale)
        if copy_width > 0 and copy_height > 0:
            output[
                target_y:target_y + copy_height,
                target_x:target_x + copy_width,
            ] = bitmap[:copy_height, :copy_width]
        return output

    def render_tile(self, metadata, layers, render_scale, tile_x, tile_y,
                    output_root=None, gutter=TILE_GUTTER,
                    return_image_data=False):
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
        composites = {
            name: np.zeros(
                (extended_pixel_height, extended_pixel_width),
                dtype=np.uint32,
            )
            for name in ("a", "b", "darker")
        }
        merged_binary_mask = None
        for layer in reversed(layers):
            path_a, path_b = metadata["layer_pdfs"].get(
                layer, (None, None)
            )
            image_a = self._render_layer(
                path_a,
                metadata["page_size_a"],
                canvas_size,
                extended_bounds,
                render_scale,
            )
            image_b = self._render_layer(
                path_b,
                metadata["page_size_b"],
                canvas_size,
                extended_bounds,
                render_scale,
            )
            darker, binary_mask = combine_layer_images(image_a, image_b)
            color, layer_opacity = metadata.get("layer_styles", {}).get(
                layer, standard_layer_style(layer)
            )
            if merged_binary_mask is None:
                merged_binary_mask = binary_mask
            else:
                merged_binary_mask = cv2.max(
                    merged_binary_mask, binary_mask
                )
            opacity = 0.8 * layer_opacity
            _accumulate_coverage(
                composites["a"], image_a, color, opacity=opacity
            )
            _accumulate_coverage(
                composites["b"], image_b, color, opacity=opacity
            )
            _accumulate_coverage(
                composites["darker"], darker, color, opacity=opacity
            )

        image_paths = {}
        image_data = {}
        for name, accumulator in composites.items():
            image = _finish_coverage(accumulator)
            image = image[
                crop_y:crop_y + crop_height,
                crop_x:crop_x + crop_width,
            ]
            if return_image_data:
                image_data[name] = image
            if output_root is not None:
                path = os.path.join(output_root, f"{name}.png")
                if not cv2.imwrite(path, image):
                    raise RuntimeError(f"Could not write PCB tile {path}")
                image_paths[name] = path

        mask_path = None
        mask = finish_merged_mask(merged_binary_mask)
        if mask is not None:
            mask = mask[
                crop_y:crop_y + crop_height,
                crop_x:crop_x + crop_width,
            ]
            mask = cv2.merge([mask, mask, mask, mask])
            if output_root is not None:
                mask_path = os.path.join(output_root, "mask.png")
                if not cv2.imwrite(mask_path, mask):
                    raise RuntimeError(f"Could not write PCB tile {mask_path}")

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
        }
        if return_image_data:
            result["image_data"] = image_data
            result["mask_data"] = mask
        return result
