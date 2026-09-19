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


def build_pair_metadata(cache_a, cache_b, layers):
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
                         render_scale, tile_size=TILE_SIZE):
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

    center_x = (left + right) / (2.0 * tile_points)
    center_y = (top + bottom) / (2.0 * tile_points)
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
    darker = cv2.min(image_a, image_b)
    darker[:, :, 3] = cv2.max(image_a[:, :, 3], image_b[:, :, 3])
    return darker


def binary_layer_occupancy(image, alpha_threshold=LAYER_ALPHA_THRESHOLD):
    _, occupancy = cv2.threshold(
        image[:, :, 3], alpha_threshold, 255, cv2.THRESH_BINARY
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


def alpha_composite(destination, source, opacity=1.0):
    """Composite straight-alpha BGRA images, matching QPainter source-over."""
    destination_alpha = (
        destination[:, :, 3:4].astype(np.float32) / 255.0
    )
    premultiplied = (
        destination[:, :, :3].astype(np.float32) * destination_alpha
    )
    _accumulate_alpha(premultiplied, destination_alpha, source, opacity)
    return _finish_alpha(premultiplied, destination_alpha)


def _accumulate_alpha(premultiplied, destination_alpha, source,
                      opacity=1.0):
    source_alpha = (
        source[:, :, 3:4].astype(np.float32) * (opacity / 255.0)
    )
    inverse_source_alpha = 1.0 - source_alpha
    premultiplied *= inverse_source_alpha
    premultiplied += source[:, :, :3].astype(np.float32) * source_alpha
    destination_alpha *= inverse_source_alpha
    destination_alpha += source_alpha


def _finish_alpha(premultiplied, alpha):
    output = np.empty((*alpha.shape[:2], 4), dtype=np.uint8)
    output[:, :, :3] = np.divide(
        premultiplied,
        alpha,
        out=np.full_like(premultiplied, 255.0),
        where=alpha > 0,
    ).clip(0, 255).astype(np.uint8)
    output[:, :, 3] = (
        alpha[:, :, 0] * 255.0
    ).clip(0, 255).astype(np.uint8)
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
    def _blank(width, height):
        image = np.zeros((height, width, 4), dtype=np.uint8)
        image[:, :, :3] = 255
        return image

    def _render_layer(self, path, page_size, canvas_size, bounds,
                      render_scale):
        x, y, width, height = bounds
        request_left = int(round(x * render_scale))
        request_top = int(round(y * render_scale))
        request_right = int(round((x + width) * render_scale))
        request_bottom = int(round((y + height) * render_scale))
        pixel_width = max(1, request_right - request_left)
        pixel_height = max(1, request_bottom - request_top)
        output = self._blank(pixel_width, pixel_height)
        if not path:
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
        bitmap = self._page(path).render(
            fill_color=(255, 255, 255, 0),
            scale=render_scale,
            rotation=0,
            crop=crop,
        ).to_numpy()

        target_x = intersection_left - request_left
        target_y = intersection_top - request_top
        copy_width = min(bitmap.shape[1], output.shape[1] - target_x)
        copy_height = min(bitmap.shape[0], output.shape[0] - target_y)
        if copy_width > 0 and copy_height > 0:
            output[
                target_y:target_y + copy_height,
                target_x:target_x + copy_width,
            ] = bitmap[:copy_height, :copy_width]
        return output

    def render_tile(self, metadata, layers, render_scale, tile_x, tile_y,
                    output_root, gutter=TILE_GUTTER):
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

        os.makedirs(output_root, exist_ok=True)
        extended_pixel_width = extended_pixel_right - extended_pixel_x
        extended_pixel_height = extended_pixel_bottom - extended_pixel_y
        composites = {
            name: (
                np.zeros(
                    (extended_pixel_height, extended_pixel_width, 3),
                    dtype=np.float32,
                ),
                np.zeros(
                    (extended_pixel_height, extended_pixel_width, 1),
                    dtype=np.float32,
                ),
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
            if merged_binary_mask is None:
                merged_binary_mask = binary_mask
            else:
                merged_binary_mask = cv2.max(
                    merged_binary_mask, binary_mask
                )
            _accumulate_alpha(*composites["a"], image_a, opacity=0.8)
            _accumulate_alpha(*composites["b"], image_b, opacity=0.8)
            _accumulate_alpha(
                *composites["darker"], darker, opacity=0.8
            )

        image_paths = {}
        for name, accumulator in composites.items():
            image = _finish_alpha(*accumulator)
            path = os.path.join(output_root, f"{name}.png")
            image = image[
                crop_y:crop_y + crop_height,
                crop_x:crop_x + crop_width,
            ]
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
            mask_path = os.path.join(output_root, "mask.png")
            if not cv2.imwrite(mask_path, mask):
                raise RuntimeError(f"Could not write PCB tile {mask_path}")

        return {
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
