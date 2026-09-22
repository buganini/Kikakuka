import ctypes
import os

import cv2
import numpy as np
import pypdfium2 as pdfium

from pcb_diff_tiles import (
    TILE_GUTTER,
    finish_merged_mask,
    pixel_aligned_page_layout,
    tile_pixel_bounds,
)


def schematic_page_index(thumbnail_name):
    stem = os.path.splitext(os.path.basename(thumbnail_name))[0]
    return int(stem.rsplit("_", 1)[1])


def corresponding_schematic_page(pages, selected_page):
    """Match a page number, clamping to the final page when necessary."""
    if not pages:
        return None
    return pages[min(schematic_page_index(selected_page), len(pages) - 1)]


def matched_page_shift(pages_a, current_a, pages_b, offset):
    """Shift A and select the corresponding page on B when sync is enabled."""
    try:
        index = pages_a.index(current_a) + offset
    except ValueError:
        return None
    if not 0 <= index < len(pages_a):
        return None
    page_a = pages_a[index]
    page_b = corresponding_schematic_page(pages_b, page_a)
    return (page_a, page_b) if page_b is not None else None


def synchronized_page_shift(pages_a, current_a, pages_b, current_b, offset):
    """Return both shifted pages, or None if either side cannot move."""
    try:
        target_a = pages_a.index(current_a) + offset
        target_b = pages_b.index(current_b) + offset
    except ValueError:
        return None
    if (not 0 <= target_a < len(pages_a) or
            not 0 <= target_b < len(pages_b)):
        return None
    return pages_a[target_a], pages_b[target_b]


def pdf_page_size(path, page_index):
    document = pdfium.PdfDocument(path)
    try:
        page = document[page_index]
        try:
            width, height = page.get_size()
            return float(width), float(height)
        finally:
            page.close()
    finally:
        document.close()


def build_schematic_pair_metadata(cache_a, cache_b, page_a, page_b):
    path_a = os.path.join(cache_a, "sch.pdf")
    path_b = os.path.join(cache_b, "sch.pdf")
    page_index_a = schematic_page_index(page_a)
    page_index_b = schematic_page_index(page_b)
    page_size_a = pdf_page_size(path_a, page_index_a)
    page_size_b = pdf_page_size(path_b, page_index_b)
    return {
        "canvas_size": (
            max(page_size_a[0], page_size_b[0]),
            max(page_size_a[1], page_size_b[1]),
        ),
        "page_size_a": page_size_a,
        "page_size_b": page_size_b,
        "pdf_a": path_a,
        "pdf_b": path_b,
        "page_index_a": page_index_a,
        "page_index_b": page_index_b,
    }


class SchematicTileRenderer:
    """Render a pair of schematic PDF pages into viewport-sized tiles."""

    def __init__(self):
        self._documents = {}
        self._pages = {}
        self._color_workspace = None
        self._gray_workspace = None

    def close(self):
        for page in self._pages.values():
            page.close()
        self._pages.clear()
        for document in self._documents.values():
            document.close()
        self._documents.clear()
        self._color_workspace = None
        self._gray_workspace = None

    def _document(self, path):
        document = self._documents.get(path)
        if document is None:
            document = pdfium.PdfDocument(path)
            self._documents[path] = document
        return document

    def _page(self, path, page_index):
        key = (path, page_index)
        page = self._pages.get(key)
        if page is None:
            page = self._document(path)[page_index]
            self._pages[key] = page
        return page

    def _color_buffers(self, count, height, width):
        pixel_count = height * width
        required = pixel_count * 4
        if (self._color_workspace is None or
                self._color_workspace.shape[0] < count or
                self._color_workspace.shape[1] < required):
            self._color_workspace = np.empty(
                (count, required), dtype=np.uint8
            )
        return tuple(
            self._color_workspace[index, :required].reshape(
                height, width, 4
            )
            for index in range(count)
        )

    def _gray_buffers(self, count, height, width):
        pixel_count = height * width
        if (self._gray_workspace is None or
                self._gray_workspace.shape[0] < count or
                self._gray_workspace.shape[1] < pixel_count):
            self._gray_workspace = np.empty(
                (count, pixel_count), dtype=np.uint8
            )
        return tuple(
            self._gray_workspace[index, :pixel_count].reshape(height, width)
            for index in range(count)
        )

    def _render_page(self, path, page_index, page_size, canvas_size,
                     bounds, render_scale, output):
        x, y, width, height = bounds
        request_left = int(round(x * render_scale))
        request_top = int(round(y * render_scale))
        request_right = int(round((x + width) * render_scale))
        request_bottom = int(round((y + height) * render_scale))
        pixel_width = max(1, request_right - request_left)
        pixel_height = max(1, request_bottom - request_top)
        if output.shape != (pixel_height, pixel_width, 4):
            raise ValueError("PDF render buffer has the wrong shape")
        output.fill(255)

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
        target_x = intersection_left - request_left
        target_y = intersection_top - request_top
        render_width = intersection_right - intersection_left
        render_height = intersection_bottom - intersection_top
        render_options = dict(
            scale=render_scale,
            rotation=0,
            crop=crop,
            fill_color=(255, 255, 255, 255),
            prefer_bgrx=True,
        )
        if (target_x == 0 and target_y == 0 and
                render_width == pixel_width and
                render_height == pixel_height):
            ctypes_buffer = (ctypes.c_ubyte * output.nbytes).from_buffer(
                output
            )

            def bitmap_maker(width, height, format, rev_byteorder=False):
                if width != pixel_width or height != pixel_height:
                    raise ValueError("PDFium requested an unexpected bitmap")
                return pdfium.PdfBitmap.new_native(
                    width,
                    height,
                    format,
                    rev_byteorder,
                    buffer=ctypes_buffer,
                    stride=output.strides[0],
                )

            self._page(path, page_index).render(
                bitmap_maker=bitmap_maker, **render_options
            )
            return output

        bitmap = self._page(path, page_index).render(
            **render_options
        ).to_numpy()
        copy_width = min(bitmap.shape[1], pixel_width - target_x)
        copy_height = min(bitmap.shape[0], pixel_height - target_y)
        if copy_width > 0 and copy_height > 0:
            output[
                target_y:target_y + copy_height,
                target_x:target_x + copy_width,
            ] = bitmap[:copy_height, :copy_width]
        return output

    def render_tile(self, metadata, render_scale, tile_x, tile_y, *,
                    image_buffers, mask_buffer, gutter=TILE_GUTTER):
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
        extended_width = extended_pixel_right - extended_pixel_x
        extended_height = extended_pixel_bottom - extended_pixel_y
        extended_bounds = (
            extended_pixel_x / render_scale,
            extended_pixel_y / render_scale,
            extended_width / render_scale,
            extended_height / render_scale,
        )
        crop_x = pixel_x - extended_pixel_x
        crop_y = pixel_y - extended_pixel_y
        crop = np.s_[
            crop_y:crop_y + pixel_height,
            crop_x:crop_x + pixel_width,
        ]

        image_a, image_b, darker, difference = self._color_buffers(
            4, extended_height, extended_width
        )
        binary_mask, mask_scratch = self._gray_buffers(
            2, extended_height, extended_width
        )
        self._render_page(
            metadata["pdf_a"], metadata["page_index_a"],
            metadata["page_size_a"], canvas_size, extended_bounds,
            render_scale, image_a,
        )
        self._render_page(
            metadata["pdf_b"], metadata["page_index_b"],
            metadata["page_size_b"], canvas_size, extended_bounds,
            render_scale, image_b,
        )
        cv2.min(image_a, image_b, dst=darker)
        cv2.absdiff(image_a, image_b, dst=difference)
        cv2.cvtColor(difference, cv2.COLOR_BGRA2GRAY, dst=binary_mask)
        cv2.threshold(
            binary_mask, 0, 255, cv2.THRESH_BINARY, dst=binary_mask
        )
        has_mask = cv2.countNonZero(binary_mask) > 0
        mask = (
            finish_merged_mask(binary_mask, mask_scratch)
            if has_mask else None
        )

        image_buffers["a"][:] = image_a[crop]
        image_buffers["b"][:] = image_b[crop]
        image_buffers["darker"][:] = darker[crop]
        if mask is not None:
            mask = mask[crop]
            mask_buffer[:, :, :2] = 0
            mask_buffer[:, :, 2] = mask
            mask_buffer[:, :, 3] = mask
        return {
            "bounds": (
                pixel_x / render_scale,
                pixel_y / render_scale,
                pixel_width / render_scale,
                pixel_height / render_scale,
            ),
            "pixel_size": (pixel_width, pixel_height),
            "images": {},
            "mask": None,
            "has_mask": has_mask,
        }
