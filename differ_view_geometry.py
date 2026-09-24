"""Geometry shared by the schematic and PCB difference views."""


DEFAULT_OVERLAP_PERCENT = 13.0


def canvas_priority_point(cursor_position, canvas_size):
    """Use the live cursor only while it is inside the canvas."""
    width, height = canvas_size
    if cursor_position is not None:
        x, y = cursor_position
        if 0 <= x < width and 0 <= y < height:
            return x, y
    return width / 2, height / 2


def viewport_center_splitter_fraction(
        page_width, viewport_width, view_offset_x, view_scale):
    """Return the page fraction displayed at the viewport's horizontal center."""
    if page_width <= 0 or view_scale <= 0:
        return 0.5
    source_x = (viewport_width / 2.0 - view_offset_x) / view_scale
    return max(0.0, min(1.0, source_x / page_width))


def clipped_view_transform(previous, page_size, canvas_size, zoom_limit):
    """Keep the current pan/zoom within the new page and canvas bounds."""
    page_width, page_height = page_size
    canvas_width, canvas_height = canvas_size
    if min(page_width, page_height, canvas_width, canvas_height) <= 0:
        return None

    fit_scale = min(
        canvas_width / page_width, canvas_height / page_height
    ) * 0.75
    if previous is None:
        scale = fit_scale
        offx = (canvas_width - page_width * scale) / 2
        offy = (canvas_height - page_height * scale) / 2
    else:
        old_offx, old_offy, old_scale = previous
        scale = min(fit_scale * zoom_limit, max(fit_scale / 8, old_scale))

        def clip_offset(old_offset, page_extent, canvas_extent):
            gap = canvas_extent - page_extent * scale
            return min(max(old_offset, min(0.0, gap)), max(0.0, gap))

        offx = clip_offset(old_offx, page_width, canvas_width)
        offy = clip_offset(old_offy, page_height, canvas_height)

    return (offx, offy, scale), fit_scale


def adjust_overlap_percent(current, wheel_delta):
    """Move one percentage point per wheel notch, within the 0–30% range."""
    return max(0.0, min(30.0, current + wheel_delta / 120.0))


def overlap_bounds(page_width, splitter_fraction, viewport_width, scale,
                   overlap_percent):
    """Return source-space overlap edges for a viewport-relative width.

    The percentage denotes the *total* overlap width, centered on the
    splitter, rather than a margin on each side.
    """
    center = page_width * splitter_fraction
    half_width = viewport_width * overlap_percent / (200.0 * scale)
    return (
        max(0.0, min(page_width, center - half_width)),
        max(0.0, min(page_width, center + half_width)),
    )
