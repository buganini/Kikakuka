"""Build zero-thickness KiCad silkscreen display geometry."""

from dataclasses import dataclass
import warnings

import FreeCAD
import Part

from .Copper import board_graphic_shape


NM_PER_MM = 1_000_000.0
DEFAULT_SILKSCREEN_THICKNESS_MM = 0.010
DEFAULT_SILKSCREEN_COLOR = (0.94, 0.94, 0.94)


@dataclass(frozen=True)
class SilkscreenLayerInfo:
    layer: int
    name: str
    z: float
    thickness: float
    color: tuple


def _color_tuple(color, fallback=DEFAULT_SILKSCREEN_COLOR):
    try:
        channels = tuple(float(value) for value in (
            color.red, color.green, color.blue))
    except Exception:
        return fallback
    if any(value > 1.0 for value in channels):
        channels = tuple(value / 255.0 for value in channels)
    if all(value == 0.0 for value in channels):
        return fallback
    unset_gray = (128 / 255.0,) * 3
    if all(abs(value - sentinel) <= 1e-9
           for value, sentinel in zip(channels, unset_gray)):
        return fallback
    return channels


def silkscreen_stackup_layers(stackup, board_layer,
                               total_thickness=None, outer_inset=0.0):
    """Return F/B silkscreen layer display metadata.

    Both sides are described so silk graphics still import when an older
    KiCad file omits explicit silkscreen stackup entries.
    """
    total_mm = (float(total_thickness)
                if total_thickness is not None
                else sum(max(0, getattr(entry, "thickness", 0))
                         for entry in stackup.layers) / NM_PER_MM)
    targets = [
        (board_layer.BL_F_SilkS, "F.SilkS", True),
        (board_layer.BL_B_SilkS, "B.SilkS", False),
    ]
    entries = {getattr(entry, "layer", None): entry
               for entry in stackup.layers}
    inset = max(0.0, float(outer_inset))
    result = []
    for layer, name, is_front in targets:
        entry = entries.get(layer)
        if entry is not None and not getattr(entry, "enabled", True):
            continue
        thickness = (max(0, getattr(entry, "thickness", 0)) / NM_PER_MM
                     if entry is not None else 0.0)
        result.append(SilkscreenLayerInfo(
            layer=layer,
            name=name,
            z=(total_mm - inset if is_front else inset),
            thickness=thickness or DEFAULT_SILKSCREEN_THICKNESS_MM,
            color=_color_tuple(getattr(entry, "color", None)),
        ))
    return result


def _text_is_visible(item):
    try:
        return bool(item.visible)
    except Exception:
        pass
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return bool(item.attributes.visible)
    except Exception:
        return True


def _text_base_shape(item):
    if hasattr(item, "as_text"):
        return item.as_text()
    if hasattr(item, "as_textbox"):
        return item.as_textbox()
    nested = getattr(item, "text", None)
    if nested is not None and hasattr(nested, "as_text"):
        return nested.as_text()
    return None


def build_silkscreen_layers(kicad, board, stackup, board_layer,
                             board_shapes=None, footprints=None, warn=None,
                             total_thickness=None, outer_inset=0.0):
    """Return one planar display shape for each non-empty silk layer."""
    infos = silkscreen_stackup_layers(
        stackup, board_layer, total_thickness=total_thickness,
        outer_inset=outer_inset)
    try:
        board_text = list(board.get_text())
    except Exception as ex:
        board_text = []
        if warn:
            warn(f"Could not read silkscreen text: {ex}")
    if footprints is None:
        try:
            footprints = list(board.get_footprints())
        except Exception as ex:
            footprints = []
            if warn:
                warn(f"Could not read footprint silkscreen: {ex}")

    footprint_shapes = []
    footprint_text = []
    for footprint in footprints or []:
        try:
            footprint_shapes.extend(footprint.definition.shapes)
        except Exception as ex:
            if warn:
                warn(f"Could not read footprint silk graphics: {ex}")
        try:
            footprint_text.extend(footprint.texts_and_fields)
        except Exception as ex:
            if warn:
                warn(f"Could not read footprint silk text: {ex}")

    all_graphics = list(board_shapes or []) + footprint_shapes
    all_text = board_text + footprint_text

    result = []
    for info in infos:
        shapes = []
        graphic_count = 0
        for graphic in all_graphics:
            if getattr(graphic, "layer", None) != info.layer:
                continue
            try:
                shape = board_graphic_shape(graphic)
                if shape is not None:
                    shapes.append(shape)
                    graphic_count += 1
            except Exception as ex:
                if warn:
                    warn(f"Could not build {info.name} graphic: {ex}")

        layer_text = [item for item in all_text
                      if getattr(item, "layer", None) == info.layer
                      and _text_is_visible(item)]
        text_inputs = [base for base in (
            _text_base_shape(item) for item in layer_text)
                       if base is not None]
        text_count = 0
        if text_inputs:
            try:
                compounds = kicad.get_text_as_shapes(text_inputs)
                for compound in compounds:
                    built_any = False
                    for graphic in compound:
                        shape = board_graphic_shape(graphic)
                        if shape is not None:
                            shapes.append(shape)
                            built_any = True
                    if built_any:
                        text_count += 1
            except Exception as ex:
                if warn:
                    warn(f"Could not polygonize {info.name} text: {ex}")

        if not shapes:
            continue
        shape = shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)
        shape.translate(FreeCAD.Vector(0, 0, info.z))
        result.append({
            "layer": info.layer,
            "name": info.name,
            "z": info.z,
            "thickness": info.thickness,
            "color": info.color,
            "graphic_count": graphic_count,
            "text_count": text_count,
            "face_count": len(getattr(shape, "Faces", [])),
            "area": float(getattr(shape, "Area", 0.0)),
            "shape": shape,
        })
    return result
