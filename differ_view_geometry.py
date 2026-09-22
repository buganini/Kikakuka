"""Geometry shared by the schematic and PCB difference views."""


DEFAULT_OVERLAP_PERCENT = 13.0


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
