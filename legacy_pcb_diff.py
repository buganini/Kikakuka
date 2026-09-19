import hashlib
import os

import cv2
import numpy as np

from pcb_diff_tiles import PcbTileRenderer


def legacy_compare_layer(image_a, image_b):
    """Reproduce the pre-viewport per-layer darker and mask inputs."""
    if image_a is None and image_b is None:
        raise ValueError("At least one layer image is required")
    if image_a is None:
        darker = image_b.copy()
        difference = cv2.cvtColor(image_b, cv2.COLOR_BGRA2GRAY)
    elif image_b is None:
        darker = image_a.copy()
        difference = cv2.cvtColor(image_a, cv2.COLOR_BGRA2GRAY)
    else:
        darker = cv2.min(image_a, image_b)
        darker[:, :, 3] = cv2.max(
            image_a[:, :, 3], image_b[:, :, 3]
        )
        difference = cv2.cvtColor(
            cv2.absdiff(image_a, image_b), cv2.COLOR_BGRA2GRAY
        )
    _, binary_mask = cv2.threshold(
        difference, 0, 255, cv2.THRESH_BINARY
    )
    return darker, binary_mask


def legacy_finish_layer_mask(binary_mask):
    blurred = cv2.GaussianBlur(binary_mask, (21, 21), 10)
    _, extended_mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY)
    return cv2.GaussianBlur(extended_mask, (21, 21), 10)


class PcbLegacyRenderer(PcbTileRenderer):
    """Reference implementation of the full-page PCB differ pipeline."""

    def render_full_page(self, metadata, layers, output_root,
                         render_scale=7.0):
        canvas_width, canvas_height = metadata["canvas_size"]
        bounds = (0.0, 0.0, canvas_width, canvas_height)
        pixel_size = (
            max(1, int(round(canvas_width * render_scale))),
            max(1, int(round(canvas_height * render_scale))),
        )
        directories = {
            name: os.path.join(output_root, name)
            for name in ("a", "b", "darker")
        }
        for directory in directories.values():
            os.makedirs(directory, exist_ok=True)

        merged_mask = None
        result_layers = {}
        for layer in layers:
            path_a, path_b = metadata["layer_pdfs"].get(
                layer, (None, None)
            )
            image_a = None
            image_b = None
            if path_a:
                image_a = self._render_layer(
                    path_a,
                    metadata["page_size_a"],
                    metadata["canvas_size"],
                    bounds,
                    render_scale,
                )
            if path_b:
                image_b = self._render_layer(
                    path_b,
                    metadata["page_size_b"],
                    metadata["canvas_size"],
                    bounds,
                    render_scale,
                )
            if image_a is None and image_b is None:
                continue

            safe_layer = hashlib.sha1(
                layer.encode("utf-8")
            ).hexdigest()[:12]
            paths = {}
            for name, image in (("a", image_a), ("b", image_b)):
                if image is None:
                    continue
                path = os.path.join(directories[name], f"{safe_layer}.png")
                if not cv2.imwrite(path, image):
                    raise RuntimeError(f"Could not write legacy PCB image {path}")
                paths[name] = path

            darker, binary_mask = legacy_compare_layer(image_a, image_b)
            darker_path = os.path.join(
                directories["darker"], f"{safe_layer}.png"
            )
            if not cv2.imwrite(darker_path, darker):
                raise RuntimeError(
                    f"Could not write legacy PCB image {darker_path}"
                )
            paths["darker"] = darker_path
            result_layers[layer] = paths

            mask = legacy_finish_layer_mask(binary_mask)
            if merged_mask is None:
                merged_mask = mask
            else:
                merged_mask = cv2.max(merged_mask, mask)

        mask_path = None
        if merged_mask is not None:
            mask_path = os.path.join(output_root, "mask.png")
            mask_rgba = cv2.merge([merged_mask] * 4)
            if not cv2.imwrite(mask_path, mask_rgba):
                raise RuntimeError(
                    f"Could not write legacy PCB mask {mask_path}"
                )

        return {
            "bounds": bounds,
            "pixel_size": pixel_size,
            "layers": result_layers,
            "mask": mask_path,
        }
