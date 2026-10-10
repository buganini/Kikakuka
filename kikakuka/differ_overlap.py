"""Color accents for the overlap band in PCB and schematic diffs."""

import numpy as np


OVERLAP_COLOR_SHIFT = 128


def apply_overlap_color_shift(image_a, image_b, overlap, *, premultiplied):
    """Raise A's red and B's blue before taking the channel-wise minimum.

    Identical pixels keep their original color: each raised channel is paired
    with the unchanged channel from the other image. PCB tiles are
    premultiplied BGRA, so compare straight colors and premultiply the result.
    """
    if premultiplied:
        alpha_a = image_a[:, :, 3].astype(np.uint16)
        alpha_b = image_b[:, :, 3].astype(np.uint16)
        alpha_out = np.maximum(alpha_a, alpha_b)
        overlap[:, :, 3] = alpha_out
        for channel in range(3):
            straight_a = np.full(alpha_a.shape, 255, dtype=np.uint16)
            straight_b = np.full(alpha_b.shape, 255, dtype=np.uint16)
            np.floor_divide(
                image_a[:, :, channel].astype(np.uint16) * 255 + alpha_a // 2,
                alpha_a, out=straight_a, where=alpha_a > 0,
            )
            np.floor_divide(
                image_b[:, :, channel].astype(np.uint16) * 255 + alpha_b // 2,
                alpha_b, out=straight_b, where=alpha_b > 0,
            )
            np.minimum(straight_a, 255, out=straight_a)
            np.minimum(straight_b, 255, out=straight_b)
            if channel == 2:  # Red in BGRA: tint A.
                np.minimum(straight_a + OVERLAP_COLOR_SHIFT, 255,
                           out=straight_a)
            elif channel == 0:  # Blue in BGRA: tint B.
                np.minimum(straight_b + OVERLAP_COLOR_SHIFT, 255,
                           out=straight_b)
            overlap[:, :, channel] = (
                np.minimum(straight_a, straight_b) * alpha_out + 127
            ) // 255
        equal_pixels = np.all(image_a == image_b, axis=2)
        overlap[equal_pixels] = image_a[equal_pixels]
    else:
        overlap[:, :, 0] = np.minimum(
            image_a[:, :, 0],
            np.minimum(image_b[:, :, 0].astype(np.int16) + OVERLAP_COLOR_SHIFT,
                       255),
        )
        overlap[:, :, 1] = np.minimum(image_a[:, :, 1], image_b[:, :, 1])
        overlap[:, :, 2] = np.minimum(
            np.minimum(image_a[:, :, 2].astype(np.int16) + OVERLAP_COLOR_SHIFT,
                       255),
            image_b[:, :, 2],
        )
        overlap[:, :, 3] = np.minimum(image_a[:, :, 3], image_b[:, :, 3])
