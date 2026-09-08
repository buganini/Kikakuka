"""Build zero-thickness solder-mask display geometry from KiCad data."""

from dataclasses import dataclass

import FreeCAD
import Part

from .Copper import board_graphic_shape, polygon_with_holes_face


NM_PER_MM = 1_000_000.0
MASK_DISPLAY_GAP_MM = 0.020
DEFAULT_MASK_THICKNESS_MM = 0.010
DEFAULT_MASK_COLOR = (0.08, 0.20, 0.14)
DEFAULT_SUBSTRATE_COLOR = (0.92, 0.92, 0.92)
DEFAULT_SUBSTRATE_OPACITY = 0.90
DEFAULT_MASK_OPACITY = 0.83
DEFAULT_SUBSTRATE_TRANSPARENCY = 10
DEFAULT_MASK_TRANSPARENCY = 17


@dataclass(frozen=True)
class MaskLayerInfo:
    layer: int
    name: str
    z: float
    thickness: float
    color: tuple
    transparency: int


def _color_rgba(color, fallback=None):
    try:
        channels = tuple(float(value) for value in (
            color.red, color.green, color.blue, color.alpha))
    except Exception:
        return fallback
    if any(value > 1.0 for value in channels):
        channels = tuple(value / 255.0 for value in channels)
    if all(value == 0.0 for value in channels):
        return fallback
    unset_gray = (128 / 255.0, 128 / 255.0, 128 / 255.0, 1.0)
    if all(abs(value - sentinel) <= 1e-9
           for value, sentinel in zip(channels, unset_gray)):
        return fallback
    return channels


def _color_tuple(color, fallback=None):
    rgba_fallback = None if fallback is None else tuple(fallback) + (1.0,)
    rgba = _color_rgba(color, rgba_fallback)
    return None if rgba is None else rgba[:3]


def _transparency(opacity):
    return int(round((1.0 - min(1.0, max(0.0, opacity))) * 100.0))


def substrate_appearance(stackup):
    """Return dielectric appearance using KiCad's 3D body-color mixing."""
    body_rgba = None
    for entry in stackup.layers:
        if getattr(entry, "dielectric", None) is None:
            continue
        layer_rgba = _color_rgba(getattr(entry, "color", None))
        if layer_rgba is None:
            continue
        if body_rgba is None:
            body_rgba = layer_rgba
        else:
            layer_alpha = layer_rgba[3]
            body_rgba = (
                layer_rgba[0] * layer_alpha
                + body_rgba[0] * (1.0 - layer_alpha),
                layer_rgba[1] * layer_alpha
                + body_rgba[1] * (1.0 - layer_alpha),
                layer_rgba[2] * layer_alpha
                + body_rgba[2] * (1.0 - layer_alpha),
                body_rgba[3],
            )
        body_alpha = (body_rgba[3]
                      + (1.0 - body_rgba[3]) * layer_rgba[3] / 2.0)
        body_rgba = body_rgba[:3] + (body_alpha,)
    if body_rgba is not None:
        return body_rgba[:3], _transparency(body_rgba[3])
    return DEFAULT_SUBSTRATE_COLOR, DEFAULT_SUBSTRATE_TRANSPARENCY


def substrate_color(stackup):
    """Return the first configured dielectric color, or translucent white."""
    return substrate_appearance(stackup)[0]


def mask_stackup_layers(stackup, board_layer):
    total_mm = sum(max(0, getattr(entry, "thickness", 0))
                   for entry in stackup.layers) / NM_PER_MM
    result = []
    for entry in stackup.layers:
        try:
            name = board_layer.Name(entry.layer)
        except Exception:
            continue
        if name not in ("BL_F_Mask", "BL_B_Mask"):
            continue
        thickness = (max(0, getattr(entry, "thickness", 0)) / NM_PER_MM
                     or DEFAULT_MASK_THICKNESS_MM)
        rgba = _color_rgba(
            getattr(entry, "color", None),
            DEFAULT_MASK_COLOR + (DEFAULT_MASK_OPACITY,))
        is_front = name == "BL_F_Mask"
        result.append(MaskLayerInfo(
            layer=entry.layer,
            name="F.Mask" if is_front else "B.Mask",
            z=(total_mm + 2 * MASK_DISPLAY_GAP_MM
               if is_front else -2 * MASK_DISPLAY_GAP_MM),
            thickness=thickness,
            color=rgba[:3],
            transparency=_transparency(rgba[3]),
        ))
    return result


def read_mask_opening_polygons(board, items, layer, warn=None):
    """Ask KiCad for final padstack polygons on one solder-mask layer."""
    if not items:
        return []
    try:
        polygons = board.get_pad_shapes_as_polygons(items, layer)
        if polygons is None:
            return []
        if not isinstance(polygons, list):
            polygons = [polygons]
        return [polygon for polygon in polygons if polygon is not None]
    except Exception as ex:
        if warn:
            warn(f"Could not read mask opening polygons: {ex}")
        return []


def padstack_item_exists_on_layer(item, layer):
    """Return whether KiCad declares this pad/via on the target layer.

    The polygon endpoint can return an anchor/fallback shape when asked for a
    technical layer absent from a padstack.  Filtering on ``padstack.layers``
    keeps items from one side out of the opposite mask.
    """
    try:
        return layer in item.padstack.layers
    except Exception:
        return False


def build_solder_mask_layers(board, stackup, board_layer, board_face,
                             board_shapes=None, warn=None):
    """Return F.Mask/B.Mask faces using KiCad-computed padstack openings."""
    infos = mask_stackup_layers(stackup, board_layer)
    try:
        pads = list(board.get_pads())
    except Exception as ex:
        pads = []
        if warn:
            warn(f"Could not read pads for solder mask: {ex}")
    try:
        vias = list(board.get_vias())
    except Exception as ex:
        vias = []
        if warn:
            warn(f"Could not read vias for solder mask: {ex}")
    result = []
    for info in infos:
        layer_pads = [pad for pad in pads
                      if padstack_item_exists_on_layer(pad, info.layer)]
        layer_vias = [via for via in vias
                      if padstack_item_exists_on_layer(via, info.layer)]
        polygons = read_mask_opening_polygons(
            board, layer_pads, info.layer, warn)
        polygons.extend(read_mask_opening_polygons(
            board, layer_vias, info.layer, warn))
        openings = []
        for polygon in polygons:
            try:
                face = polygon_with_holes_face(polygon)
                if face is not None:
                    openings.append(face)
            except Exception as ex:
                if warn:
                    warn(f"Could not build mask opening: {ex}")
        for graphic in board_shapes or []:
            if getattr(graphic, "layer", None) != info.layer:
                continue
            try:
                face = board_graphic_shape(graphic)
                if face is not None:
                    openings.append(face)
            except Exception as ex:
                if warn:
                    warn(f"Could not build mask graphic opening: {ex}")

        mask = board_face.copy()
        if openings:
            try:
                mask = mask.cut(Part.makeCompound(openings))
            except Exception as ex:
                if warn:
                    warn(f"Could not subtract {info.name} openings: {ex}")
        mask.translate(FreeCAD.Vector(0, 0, info.z))
        result.append({
            "layer": info.layer,
            "name": info.name,
            "z": info.z,
            "thickness": info.thickness,
            "color": info.color,
            "transparency": info.transparency,
            "pad_count": len(layer_pads),
            "via_count": len(layer_vias),
            "opening_count": len(openings),
            "face_count": len(getattr(mask, "Faces", [])),
            "area": float(getattr(mask, "Area", 0.0)),
            "shape": mask,
        })
    return result
