"""Build solid stiffeners from named KiCad user layers."""

from dataclasses import dataclass
import re

import FreeCAD
import Part

from .Copper import (
    board_graphic_area_shape,
    board_graphic_path_edge,
)
from .Units import parse_length_mm


MATERIAL_DEFAULTS = {
    "FR4": ((0xC8 / 255.0, 0xB4 / 255.0, 0x5A / 255.0), 0.90),
    "Polyimide": ((0xC8 / 255.0, 0x75 / 255.0, 0x18 / 255.0), 0.65),
    "Stainless_Steel": ((0xA7 / 255.0, 0xAD / 255.0, 0xB4 / 255.0), 1.00),
    "3M468": ((0xF2 / 255.0, 0xE3 / 255.0, 0xBD / 255.0), 0.28),
    "tesa8854": ((0xEE / 255.0, 0xE8 / 255.0, 0xD8 / 255.0), 0.45),
    "3M9077": ((0xDD / 255.0, 0xE8 / 255.0, 0xEE / 255.0), 0.30),
}
_MATERIAL_NAMES = {name.lower(): name for name in MATERIAL_DEFAULTS}
_COLOR_RE = re.compile(r"#[0-9a-f]{6}", re.IGNORECASE)
_PROPERTY_RE = re.compile(
    r"(?:^|[/\r\n])\s*(name|material|color|opacity|thickness)\s*=",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StiffenerSpec:
    name: str
    material: str
    color: tuple
    opacity: float
    thickness: float


def _parse_color(value):
    value = str(value).strip()
    if _COLOR_RE.fullmatch(value) is None:
        raise ValueError(f"invalid Color value {value!r}; expected #RRGGBB")
    return tuple(int(value[index:index + 2], 16) / 255.0
                 for index in (1, 3, 5))


def parse_stiffener_annotation(value):
    """Parse a slash- or newline-separated stiffener annotation."""
    properties = {}
    for field in re.split(r"[/\r\n]+", str(value or "")):
        if not field.strip():
            continue
        if "=" not in field:
            raise ValueError(f"invalid stiffener field {field.strip()!r}")
        key, field_value = field.split("=", 1)
        key = key.strip().lower()
        if key not in ("name", "material", "color", "opacity", "thickness"):
            raise ValueError(f"unknown stiffener property {key!r}")
        if key in properties:
            raise ValueError(f"duplicate stiffener property {key!r}")
        properties[key] = field_value.strip()

    material_value = properties.get("material")
    material = _MATERIAL_NAMES.get(str(material_value or "").lower())
    if material is None:
        choices = ", ".join(MATERIAL_DEFAULTS)
        raise ValueError(
            f"invalid or missing Material {material_value!r}; expected {choices}"
        )
    if "thickness" not in properties:
        raise ValueError("missing Thickness; expected mm, in, mil, or um")
    thickness = parse_length_mm(properties["thickness"], "Thickness")
    if thickness <= 0:
        raise ValueError("Thickness must be greater than zero")

    default_color, default_opacity = MATERIAL_DEFAULTS[material]
    color = (_parse_color(properties["color"])
             if "color" in properties else default_color)
    if "opacity" in properties:
        try:
            opacity = float(properties["opacity"])
        except (TypeError, ValueError):
            raise ValueError(
                f"invalid Opacity value {properties['opacity']!r}; expected 0..1"
            )
        if not 0.0 <= opacity <= 1.0:
            raise ValueError("Opacity must be between 0 and 1")
    else:
        opacity = default_opacity
    name = properties.get("name", "").strip()
    return StiffenerSpec(name, material, color, opacity, thickness)


def _text_value(item):
    value = getattr(item, "value", None)
    if value is not None:
        return str(value)
    nested = getattr(item, "text", None)
    value = getattr(nested, "value", None)
    return str(value) if value is not None else ""


def _text_point(item):
    position = getattr(item, "position", None)
    if position is None:
        return None
    try:
        return FreeCAD.Vector(position.x / 1e6, -position.y / 1e6, 0)
    except Exception:
        return None


def _area_shape(graphic):
    """Return the enclosed area of a rectangle, circle, or polygon."""
    shape = board_graphic_area_shape(graphic)
    if shape is None or not getattr(shape, "Faces", []):
        return None
    if float(getattr(shape, "Area", 0.0)) <= 0.0:
        return None
    return shape


def _contains(shape, point):
    try:
        return bool(shape.isInside(point, 0.001, True))
    except Exception:
        try:
            return any(face.isInside(point, 0.001, True)
                       for face in shape.Faces)
        except Exception:
            return False


def build_stiffener_layers(board_shapes, board_text, layers,
                           total_thickness, to_concrete=None, warn=None,
                           error=None, mask_openings=None):
    """Return one solid per annotated closed area on F/B.Stiffener.

    ``layers`` contains ``(layer_id, displayed_name, is_front)`` tuples.
    Annotation text is assigned to the smallest containing area, which makes
    nested drawings deterministic.
    """
    report_error = error or warn
    result = []
    for layer_id, layer_name, is_front in layers:
        areas = []
        path_edges = []
        path_sources = []
        for source in board_shapes or []:
            if getattr(source, "layer", None) != layer_id:
                continue
            try:
                graphic = to_concrete(source) if to_concrete else source
                if graphic is None:
                    continue
                shape = _area_shape(graphic)
                if shape is not None:
                    areas.append((source, shape))
                    continue
                edge = board_graphic_path_edge(graphic)
                if edge is not None:
                    path_edges.append(edge)
                    path_sources.append(source)
            except Exception as ex:
                if report_error:
                    report_error(f"Could not build {layer_name} area: {ex}")

        if path_edges:
            try:
                for edge_group in Part.sortEdges(path_edges):
                    wire = Part.Wire(edge_group)
                    if not wire.isClosed():
                        try:
                            wire.fixWire(None, 0.001)
                        except Exception:
                            pass
                    if not wire.isClosed():
                        continue
                    shape = Part.Face(wire)
                    if (getattr(shape, "Faces", [])
                            and float(getattr(shape, "Area", 0.0)) > 0.0):
                        areas.append((path_sources[0], shape))
            except Exception as ex:
                if report_error:
                    report_error(
                        f"Could not join {layer_name} outline: {ex}")

        assignments = {index: [] for index in range(len(areas))}
        for item in board_text or []:
            if getattr(item, "layer", None) != layer_id:
                continue
            value = _text_value(item).strip()
            if not _PROPERTY_RE.search(value):
                continue
            point = _text_point(item)
            if point is None:
                if report_error:
                    report_error(
                        f"Could not locate {layer_name} annotation {value!r}")
                continue
            candidates = [
                index for index, (_source, shape) in enumerate(areas)
                if _contains(shape, point)
            ]
            if not candidates:
                if report_error:
                    report_error(
                        f"{layer_name} annotation is outside any area: "
                        f"{value!r}")
                continue
            selected = min(
                candidates,
                key=lambda index: float(getattr(areas[index][1], "Area", 0.0)),
            )
            assignments[selected].append(value)

        for index, (source, area) in enumerate(areas):
            annotations = assignments[index]
            if not annotations:
                if warn:
                    warn(f"Ignoring unannotated area on {layer_name}")
                continue
            if len(annotations) > 1:
                if report_error:
                    report_error(
                        f"Ignoring {layer_name} area with multiple annotations")
                continue
            try:
                spec = parse_stiffener_annotation(annotations[0])
                cut_area = area.copy()
                side_openings = list(
                    (mask_openings or {}).get(is_front, []))
                if side_openings:
                    cut_area = cut_area.cut(Part.makeCompound(side_openings))
                if (cut_area is None
                        or not getattr(cut_area, "Faces", [])
                        or float(getattr(cut_area, "Area", 0.0)) <= 0.0):
                    raise ValueError(
                        "area is empty after applying solder-mask openings")
                placed = cut_area.copy()
                base_z = float(total_thickness) if is_front else 0.0
                placed.translate(FreeCAD.Vector(0, 0, base_z))
                direction = spec.thickness if is_front else -spec.thickness
                solid = placed.extrude(FreeCAD.Vector(0, 0, direction))
                center = cut_area.CenterOfMass
                source_id = getattr(getattr(source, "id", None), "value", "")
                result.append({
                    "layer": layer_id,
                    "layer_name": layer_name,
                    "is_front": is_front,
                    "source_id": str(source_id or index),
                    "name": spec.name,
                    "material": spec.material,
                    "color": spec.color,
                    "opacity": spec.opacity,
                    "transparency": int(round((1.0 - spec.opacity) * 100.0)),
                    "thickness": spec.thickness,
                    "mask_opening_count": len(side_openings),
                    "x": float(center.x),
                    "y": float(center.y),
                    "area_shape": cut_area,
                    "shape": solid,
                })
            except Exception as ex:
                if report_error:
                    report_error(
                        f"Ignoring invalid {layer_name} area: {ex}")
    return result
