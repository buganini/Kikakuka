"""Build unioned planar copper geometry at physical stackup Z."""

from dataclasses import dataclass
import math
import time

import FreeCAD
import Part

try:
    import shapely
    from shapely.geometry import Polygon
except Exception:
    shapely = None
    Polygon = None


NM_PER_MM = 1_000_000.0
DEFAULT_COPPER_THICKNESS_MM = 0.035
COPPER_2D_DEFLECTION_MM = 0.002
COPPER_2D_GRID_MM = 0.001
COPPER_COLOR = (0.72, 0.45, 0.12)


@dataclass(frozen=True)
class CopperLayerInfo:
    layer: int
    name: str
    z: float
    thickness: float
    is_outer: bool
    direction: float


@dataclass(frozen=True)
class CopperItem:
    """One KiCad copper item assigned to one concrete copper layer."""

    layer: int
    kind: str
    source: object
    geometry: object = None


def board_layer_name(board_layer, layer):
    """Return a KiCad spelling such as ``F.Cu`` for a layer enum value."""
    name = board_layer.Name(layer)
    if name.startswith("BL_"):
        name = name[3:]
    return name.replace("_Cu", ".Cu")


def is_copper_layer(board_layer, layer):
    try:
        name = board_layer.Name(layer)
    except Exception:
        return False
    return name in ("BL_F_Cu", "BL_B_Cu") or (
        name.startswith("BL_In") and name.endswith("_Cu")
    )


def outer_stackup_thicknesses(stackup, board_layer):
    """Return physical thicknesses for imported outer copper/mask layers."""
    result = {name: 0.0 for name in (
        "F.Mask", "F.Cu", "B.Cu", "B.Mask")}
    name_map = {
        "BL_F_Mask": "F.Mask",
        "BL_F_Cu": "F.Cu",
        "BL_B_Cu": "B.Cu",
        "BL_B_Mask": "B.Mask",
    }
    for entry in stackup.layers:
        if not getattr(entry, "enabled", True):
            continue
        try:
            name = name_map.get(board_layer.Name(entry.layer))
        except Exception:
            continue
        if name is None:
            continue
        thickness = max(0, getattr(entry, "thickness", 0)) / NM_PER_MM
        if not thickness:
            thickness = (0.010 if name.endswith(".Mask")
                         else DEFAULT_COPPER_THICKNESS_MM)
        result[name] = thickness
    return result


def expanded_padstack_layers(padstack, target_layers):
    """Yield (target layer, geometry descriptor) pairs for a padstack.

    A normal KiCad padstack stores one F.Cu descriptor as the common
    geometry for every layer in ``padstack.layers``.  Custom/per-layer
    stacks instead provide one descriptor for each target layer.
    """
    targets_set = set(target_layers)
    descriptors = list(padstack.copper_layers)
    descriptor_by_layer = {item.layer: item for item in descriptors}
    targets = [layer for layer in padstack.layers if layer in targets_set]
    if not targets:
        targets = [item.layer for item in descriptors
                   if item.layer in targets_set]
    common = descriptors[0] if len(descriptors) == 1 else None
    for layer in targets:
        descriptor = descriptor_by_layer.get(layer, common)
        if descriptor is not None:
            yield layer, descriptor


def copper_stackup_layers(stackup, board_layer, outer_offsets=None,
                           total_thickness=None):
    """Return enabled copper layers with their physical stackup origin.

    KiCad returns stackup entries from top to bottom.  The board body used by
    FreekiCAD spans z=0..total_thickness. ``outer_offsets`` is the imported
    material outside F.Cu/B.Cu, normally solder mask. ``z`` is the inner face
    for outer copper and the lower face for inner copper. ``direction`` points
    through the physical copper volume used to locate the planar display face.
    """
    entries = list(stackup.layers)
    total_mm = (float(total_thickness)
                if total_thickness is not None
                else sum(max(0, getattr(entry, "thickness", 0))
                         for entry in entries) / NM_PER_MM)
    consumed_mm = 0.0
    outer_offsets = outer_offsets or {}
    result = []
    for entry in entries:
        thickness_mm = max(0, getattr(entry, "thickness", 0)) / NM_PER_MM
        if (getattr(entry, "enabled", True)
                and is_copper_layer(board_layer, entry.layer)):
            name = board_layer_name(board_layer, entry.layer)
            physical_thickness = thickness_mm or DEFAULT_COPPER_THICKNESS_MM
            if name == "F.Cu":
                z = (total_mm - max(
                    0.0, float(outer_offsets.get(name, 0.0)))
                    - physical_thickness)
                direction = physical_thickness
            elif name == "B.Cu":
                z = (max(0.0, float(outer_offsets.get(name, 0.0)))
                     + physical_thickness)
                direction = -physical_thickness
            else:
                center_z = total_mm - consumed_mm - thickness_mm / 2.0
                z = center_z - physical_thickness / 2.0
                direction = physical_thickness
            result.append(CopperLayerInfo(
                layer=entry.layer,
                name=name,
                z=z,
                thickness=physical_thickness,
                is_outer=name in ("F.Cu", "B.Cu"),
                direction=direction,
            ))
        consumed_mm += thickness_mm
    return result


def _v(point, z=0.0):
    return FreeCAD.Vector(point.x / NM_PER_MM, -point.y / NM_PER_MM, z)


def _circle_face(radius, center=None):
    center = center or FreeCAD.Vector()
    if radius <= 0:
        return None
    edge = Part.makeCircle(radius, center)
    return Part.Face(Part.Wire([edge]))


def _polygon_face(points):
    if len(points) < 3:
        return None
    pts = list(points)
    if pts[0].distanceToPoint(pts[-1]) > 1e-9:
        pts.append(pts[0])
    return Part.Face(Part.makePolygon(pts))


def _capsule(p0, p1, width):
    radius = width / 2.0
    dx = p1.x - p0.x
    dy = p1.y - p0.y
    length = math.hypot(dx, dy)
    if radius <= 0:
        return None
    if length <= 1e-12:
        return _circle_face(radius, p0)
    ux, uy = dx / length, dy / length
    nx, ny = -uy * radius, ux * radius
    a_top = FreeCAD.Vector(p0.x + nx, p0.y + ny, 0)
    b_top = FreeCAD.Vector(p1.x + nx, p1.y + ny, 0)
    b_bottom = FreeCAD.Vector(p1.x - nx, p1.y - ny, 0)
    a_bottom = FreeCAD.Vector(p0.x - nx, p0.y - ny, 0)
    edges = [
        Part.makeLine(a_top, b_top),
        Part.Arc(
            b_top,
            FreeCAD.Vector(p1.x + ux * radius,
                           p1.y + uy * radius, 0),
            b_bottom,
        ).toShape(),
        Part.makeLine(b_bottom, a_bottom),
        Part.Arc(
            a_bottom,
            FreeCAD.Vector(p0.x - ux * radius,
                           p0.y - uy * radius, 0),
            a_top,
        ).toShape(),
    ]
    return Part.Face(Part.Wire(edges))


def _arc_stroke(start, mid, end, width):
    try:
        edge = Part.Arc(start, mid, end).toShape()
        points = edge.discretize(Deflection=max(width / 8.0, 0.01))
    except Exception:
        return _capsule(start, end, width)
    strokes = [_capsule(a, b, width) for a, b in zip(points, points[1:])]
    return Part.makeCompound([shape for shape in strokes if shape is not None])


def _bezier_stroke(start, control1, control2, end, width, segments=24):
    """Approximate a cubic Bezier as filled-width display strokes."""
    points = []
    for index in range(segments + 1):
        t = index / segments
        u = 1.0 - t
        points.append(FreeCAD.Vector(
            u ** 3 * start.x + 3 * u * u * t * control1.x
            + 3 * u * t * t * control2.x + t ** 3 * end.x,
            u ** 3 * start.y + 3 * u * u * t * control1.y
            + 3 * u * t * t * control2.y + t ** 3 * end.y,
            0,
        ))
    strokes = [_capsule(a, b, width) for a, b in zip(points, points[1:])]
    return Part.makeCompound([shape for shape in strokes if shape is not None])


def _polyline_edges(polyline):
    nodes = list(polyline.nodes)
    result = []
    if len(nodes) < 2:
        return result

    def node_start(node):
        if getattr(node, "has_arc", False):
            return _v(node.arc.start)
        return _v(node.point)

    first = node_start(nodes[0])
    current = first
    for index, node in enumerate(nodes):
        if index == 0 and not getattr(node, "has_arc", False):
            continue
        if getattr(node, "has_arc", False):
            arc_start = _v(node.arc.start)
            if current.distanceToPoint(arc_start) > 1e-9:
                result.append(Part.makeLine(current, arc_start))
            result.append(Part.Arc(
                arc_start, _v(node.arc.mid), _v(node.arc.end)).toShape())
            current = _v(node.arc.end)
        else:
            point = _v(node.point)
            if current.distanceToPoint(point) > 1e-9:
                result.append(Part.makeLine(current, point))
            current = point
    if (getattr(polyline, "closed", True)
            and current.distanceToPoint(first) > 1e-9):
        result.append(Part.makeLine(current, first))
    return result


def polygon_with_holes_face(polygon):
    outline = _polyline_edges(polygon.outline)
    if not outline:
        return None
    wires = [Part.Wire(outline)]
    for hole in polygon.holes:
        edges = _polyline_edges(hole)
        if edges:
            wires.append(Part.Wire(edges))
    if len(wires) == 1:
        return Part.Face(wires[0])
    return Part.Face(wires, "Part::FaceMakerBullseye")


def _rect_face(width, height):
    return _polygon_face([
        FreeCAD.Vector(-width / 2, -height / 2, 0),
        FreeCAD.Vector(width / 2, -height / 2, 0),
        FreeCAD.Vector(width / 2, height / 2, 0),
        FreeCAD.Vector(-width / 2, height / 2, 0),
    ])


def _rounded_rect_face(width, height, radius):
    radius = min(max(0.0, radius), width / 2.0, height / 2.0)
    if radius <= 1e-9:
        return _rect_face(width, height)
    x0, x1 = -width / 2.0, width / 2.0
    y0, y1 = -height / 2.0, height / 2.0
    diagonal = radius / math.sqrt(2.0)
    points = [
        FreeCAD.Vector(x0 + radius, y0, 0),
        FreeCAD.Vector(x1 - radius, y0, 0),
        FreeCAD.Vector(x1, y0 + radius, 0),
        FreeCAD.Vector(x1, y1 - radius, 0),
        FreeCAD.Vector(x1 - radius, y1, 0),
        FreeCAD.Vector(x0 + radius, y1, 0),
        FreeCAD.Vector(x0, y1 - radius, 0),
        FreeCAD.Vector(x0, y0 + radius, 0),
    ]
    arc_mids = [
        FreeCAD.Vector(x1 - radius + diagonal,
                       y0 + radius - diagonal, 0),
        FreeCAD.Vector(x1 - radius + diagonal,
                       y1 - radius + diagonal, 0),
        FreeCAD.Vector(x0 + radius - diagonal,
                       y1 - radius + diagonal, 0),
        FreeCAD.Vector(x0 + radius - diagonal,
                       y0 + radius - diagonal, 0),
    ]
    edges = []

    def line(start, end):
        if start.distanceToPoint(end) > 1e-9:
            edges.append(Part.makeLine(start, end))

    line(points[0], points[1])
    edges.append(Part.Arc(points[1], arc_mids[0], points[2]).toShape())
    line(points[2], points[3])
    edges.append(Part.Arc(points[3], arc_mids[1], points[4]).toShape())
    line(points[4], points[5])
    edges.append(Part.Arc(points[5], arc_mids[2], points[6]).toShape())
    line(points[6], points[7])
    edges.append(Part.Arc(points[7], arc_mids[3], points[0]).toShape())
    return Part.Face(Part.Wire(edges))


def _oval_face(width, height):
    if abs(width - height) <= 1e-9:
        return _circle_face(width / 2.0)
    if width > height:
        delta = (width - height) / 2.0
        return _capsule(FreeCAD.Vector(-delta, 0, 0),
                        FreeCAD.Vector(delta, 0, 0), height)
    delta = (height - width) / 2.0
    return _capsule(FreeCAD.Vector(0, -delta, 0),
                    FreeCAD.Vector(0, delta, 0), width)


def _chamfered_rect_face(width, height, ratio, corners):
    chamfer = min(width, height) * max(0.0, min(float(ratio), 0.5))
    if chamfer <= 1e-9:
        return _rect_face(width, height)
    enabled = {
        "tl": bool(getattr(corners, "top_left", False)),
        "tr": bool(getattr(corners, "top_right", False)),
        "br": bool(getattr(corners, "bottom_right", False)),
        "bl": bool(getattr(corners, "bottom_left", False)),
    }
    x0, x1 = -width / 2.0, width / 2.0
    y0, y1 = -height / 2.0, height / 2.0
    points = []
    points.extend([
        FreeCAD.Vector(x0, y0 + chamfer, 0),
        FreeCAD.Vector(x0 + chamfer, y0, 0),
    ] if enabled["bl"] else [FreeCAD.Vector(x0, y0, 0)])
    points.extend([
        FreeCAD.Vector(x1 - chamfer, y0, 0),
        FreeCAD.Vector(x1, y0 + chamfer, 0),
    ] if enabled["br"] else [FreeCAD.Vector(x1, y0, 0)])
    points.extend([
        FreeCAD.Vector(x1, y1 - chamfer, 0),
        FreeCAD.Vector(x1 - chamfer, y1, 0),
    ] if enabled["tr"] else [FreeCAD.Vector(x1, y1, 0)])
    points.extend([
        FreeCAD.Vector(x0 + chamfer, y1, 0),
        FreeCAD.Vector(x0, y1 - chamfer, 0),
    ] if enabled["tl"] else [FreeCAD.Vector(x0, y1, 0)])
    return _polygon_face(points)


def _transform(shape, x, y, angle_deg=0.0):
    if shape is None:
        return None
    result = shape.copy()
    if abs(angle_deg) > 1e-12:
        result.rotate(FreeCAD.Vector(), FreeCAD.Vector(0, 0, 1), angle_deg)
    result.translate(FreeCAD.Vector(x, y, 0))
    return result


def pad_layer_shape(pad, pad_layer, pad_shape_enum):
    """Build a board-coordinate face for one padstack layer."""
    width = pad_layer.size.x / NM_PER_MM
    height = pad_layer.size.y / NM_PER_MM
    if width <= 0 or height <= 0:
        return None
    shape_kind = pad_layer.shape
    if shape_kind == pad_shape_enum.PSS_CIRCLE:
        shape = _circle_face(width / 2.0)
    elif shape_kind == pad_shape_enum.PSS_OVAL:
        shape = _oval_face(width, height)
    elif shape_kind == pad_shape_enum.PSS_ROUNDRECT:
        shape = _rounded_rect_face(
            width, height, min(width, height) * pad_layer.corner_rounding_ratio)
    elif shape_kind == pad_shape_enum.PSS_TRAPEZOID:
        dx = pad_layer.trapezoid_delta.x / NM_PER_MM / 2.0
        dy = pad_layer.trapezoid_delta.y / NM_PER_MM / 2.0
        shape = _polygon_face([
            FreeCAD.Vector(-width / 2 - dy, -height / 2 - dx, 0),
            FreeCAD.Vector(width / 2 + dy, -height / 2 + dx, 0),
            FreeCAD.Vector(width / 2 - dy, height / 2 + dx, 0),
            FreeCAD.Vector(-width / 2 + dy, height / 2 - dx, 0),
        ])
    elif shape_kind == pad_shape_enum.PSS_CHAMFEREDRECT:
        shape = _chamfered_rect_face(
            width, height, pad_layer.chamfer_ratio,
            pad_layer.chamfered_corners)
    elif shape_kind == pad_shape_enum.PSS_CUSTOM:
        custom = [board_graphic_shape(s) for s in pad_layer.custom_shapes]
        shape = Part.makeCompound([s for s in custom if s is not None]) \
            if custom else _rect_face(width, height)
    else:
        # Rectangle and unknown future pad shapes retain their full extent.
        shape = _rect_face(width, height)

    offset_x = pad_layer.offset.x / NM_PER_MM
    offset_y = -pad_layer.offset.y / NM_PER_MM
    angle = -float(getattr(pad.padstack.angle, "degrees", 0.0))
    shape = _transform(shape, offset_x, offset_y, angle)
    shape = _transform(
        shape, pad.position.x / NM_PER_MM, -pad.position.y / NM_PER_MM, 0.0)
    try:
        drill = pad.padstack.drill.diameter
        drill_width = drill.x / NM_PER_MM
        drill_height = drill.y / NM_PER_MM
        if drill_width > 0 and drill_height > 0:
            drill_shape = _oval_face(drill_width, drill_height)
            drill_shape = _transform(
                drill_shape,
                pad.position.x / NM_PER_MM,
                -pad.position.y / NM_PER_MM,
                angle)
            shape = shape.cut(drill_shape)
    except Exception:
        pass
    return shape


def board_graphic_area_shape(graphic):
    """Build the enclosed area of a closed board graphic.

    Unlike :func:`board_graphic_shape`, this intentionally ignores KiCad's
    display fill flag.  It is used when the closed outline itself defines a
    mechanical area, such as a stiffener.
    """
    kind = type(graphic).__name__
    if kind in ("BoardCircle", "Circle"):
        return _circle_face(
            float(graphic.radius()) / NM_PER_MM, _v(graphic.center))
    if kind in ("BoardRectangle", "Rectangle"):
        p0, p1 = _v(graphic.top_left), _v(graphic.bottom_right)
        face = _rect_face(abs(p1.x - p0.x), abs(p1.y - p0.y))
        return _transform(face, (p0.x + p1.x) / 2, (p0.y + p1.y) / 2)
    if kind in ("BoardPolygon", "Polygon"):
        faces = [polygon_with_holes_face(p) for p in graphic.polygons]
        faces = [face for face in faces if face is not None]
        if not faces:
            return None
        return faces[0] if len(faces) == 1 else Part.makeCompound(faces)
    return None


def board_graphic_path_edge(graphic):
    """Return the center-line edge of a line or arc board graphic."""
    kind = type(graphic).__name__
    if kind in ("BoardSegment", "Segment"):
        return Part.makeLine(_v(graphic.start), _v(graphic.end))
    if kind in ("BoardArc", "Arc"):
        return Part.Arc(
            _v(graphic.start), _v(graphic.mid), _v(graphic.end)).toShape()
    return None


def board_graphic_shape(graphic):
    """Build filled/stroked geometry for a copper BoardShape."""
    kind = type(graphic).__name__
    width = max(
        float(getattr(getattr(graphic.attributes, "stroke", None), "width", 0))
        / NM_PER_MM,
        0.001,
    )
    if kind in ("BoardSegment", "Segment"):
        return _capsule(_v(graphic.start), _v(graphic.end), width)
    if kind in ("BoardArc", "Arc"):
        return _arc_stroke(_v(graphic.start), _v(graphic.mid),
                           _v(graphic.end), width)
    if kind in ("BoardCircle", "Circle"):
        radius = float(graphic.radius()) / NM_PER_MM
        if getattr(graphic.attributes.fill, "filled", False):
            return _circle_face(radius, _v(graphic.center))
        outer = _circle_face(radius + width / 2.0, _v(graphic.center))
        inner = _circle_face(max(0, radius - width / 2.0), _v(graphic.center))
        return outer.cut(inner) if inner is not None else outer
    if kind in ("BoardRectangle", "Rectangle"):
        p0, p1 = _v(graphic.top_left), _v(graphic.bottom_right)
        if getattr(graphic.attributes.fill, "filled", False):
            face = _rect_face(abs(p1.x - p0.x), abs(p1.y - p0.y))
            return _transform(
                face, (p0.x + p1.x) / 2, (p0.y + p1.y) / 2)
        corners = [
            FreeCAD.Vector(p0.x, p0.y, 0),
            FreeCAD.Vector(p1.x, p0.y, 0),
            FreeCAD.Vector(p1.x, p1.y, 0),
            FreeCAD.Vector(p0.x, p1.y, 0),
        ]
        strokes = [_capsule(corners[i], corners[(i + 1) % 4], width)
                   for i in range(4)]
        return Part.makeCompound(strokes)
    if kind in ("BoardPolygon", "Polygon"):
        if getattr(graphic.attributes.fill, "filled", False):
            faces = [polygon_with_holes_face(p) for p in graphic.polygons]
            return Part.makeCompound(
                [f for f in faces if f is not None]) if faces else None
        strokes = []
        for polygon in graphic.polygons:
            for polyline in [polygon.outline] + list(polygon.holes):
                for edge in _polyline_edges(polyline):
                    points = edge.discretize(Deflection=max(width / 8, 0.01))
                    strokes.extend(_capsule(a, b, width)
                                   for a, b in zip(points, points[1:]))
        return Part.makeCompound(strokes) if strokes else None
    if kind in ("BoardBezier", "Bezier"):
        return _bezier_stroke(
            _v(graphic.start), _v(graphic.control1),
            _v(graphic.control2), _v(graphic.end), width)
    return None


def read_copper_items(board, target_layers, board_shapes=None, warn=None):
    """Read KiCad copper objects and expand them onto concrete layers.

    This stage intentionally has no FreeCAD geometry operations, making API
    reading and padstack/layer expansion independently unit-testable.
    Returns ``{layer: [CopperItem, ...]}`` for every requested layer.
    """
    by_layer = {layer: [] for layer in target_layers}

    def add(layer, kind, source, geometry=None):
        if layer in by_layer:
            by_layer[layer].append(CopperItem(
                layer=layer, kind=kind, source=source, geometry=geometry))

    try:
        for track in board.get_tracks():
            add(track.layer, "track", track)
    except Exception as ex:
        if warn:
            warn(f"Could not read copper tracks: {ex}")

    try:
        for zone in board.get_zones():
            try:
                if zone.is_rule_area():
                    continue
                for layer, polygons in zone.filled_polygons.items():
                    for polygon in polygons:
                        add(layer, "zone_polygon", polygon)
            except Exception as ex:
                if warn:
                    warn(f"Could not read copper zone: {ex}")
    except Exception as ex:
        if warn:
            warn(f"Could not read copper zones: {ex}")

    try:
        for pad in board.get_pads():
            try:
                for target_layer, pad_layer in expanded_padstack_layers(
                        pad.padstack, by_layer):
                    add(target_layer, "pad", pad, pad_layer)
            except Exception as ex:
                if warn:
                    warn(f"Could not read copper pad: {ex}")
    except Exception as ex:
        if warn:
            warn(f"Could not read copper pads: {ex}")

    try:
        for via in board.get_vias():
            try:
                for target_layer, via_layer in expanded_padstack_layers(
                        via.padstack, by_layer):
                    add(target_layer, "via", via, via_layer)
            except Exception as ex:
                if warn:
                    warn(f"Could not read copper via: {ex}")
    except Exception as ex:
        if warn:
            warn(f"Could not read copper vias: {ex}")

    for graphic in board_shapes or []:
        add(graphic.layer, "graphic", graphic)

    return by_layer


def _copper_item_shape(item, pad_shape_enum):
    if item.kind == "track":
        track = item.source
        if type(track).__name__ == "ArcTrack":
            return _arc_stroke(
                _v(track.start), _v(track.mid), _v(track.end),
                track.width / NM_PER_MM)
        return _capsule(
            _v(track.start), _v(track.end), track.width / NM_PER_MM)
    if item.kind == "zone_polygon":
        return polygon_with_holes_face(item.source)
    if item.kind == "pad":
        return pad_layer_shape(item.source, item.geometry, pad_shape_enum)
    if item.kind == "via":
        via = item.source
        diameter = item.geometry.size.x / NM_PER_MM
        center = FreeCAD.Vector(
            via.position.x / NM_PER_MM, -via.position.y / NM_PER_MM, 0)
        shape = _circle_face(diameter / 2.0, center)
        try:
            drill_diameter = via.drill_diameter / NM_PER_MM
            if drill_diameter > 0:
                shape = shape.cut(_circle_face(drill_diameter / 2.0, center))
        except Exception:
            pass
        return shape
    if item.kind == "graphic":
        return board_graphic_shape(item.source)
    return None


def _wire_coordinates(wire, deflection=COPPER_2D_DEFLECTION_MM):
    """Return one closed XY coordinate ring from an ordered FreeCAD wire."""
    segments = []
    for edge in wire.Edges:
        points = edge.discretize(Deflection=deflection)
        coordinates = [(float(point.x), float(point.y)) for point in points]
        if len(coordinates) >= 2:
            segments.append(coordinates)
    if not segments:
        return None

    tolerance_squared = 1e-12

    def distance_squared(a, b):
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    for reverse_first in (False, True):
        coordinates = list(reversed(segments[0])) \
            if reverse_first else list(segments[0])
        valid = True
        for segment in segments[1:]:
            forward_distance = distance_squared(coordinates[-1], segment[0])
            reverse_distance = distance_squared(coordinates[-1], segment[-1])
            if reverse_distance < forward_distance:
                segment = list(reversed(segment))
                forward_distance = reverse_distance
            if forward_distance > tolerance_squared:
                valid = False
                break
            coordinates.extend(segment[1:])
        if valid and distance_squared(
                coordinates[-1], coordinates[0]) <= tolerance_squared:
            coordinates[-1] = coordinates[0]
            return coordinates
    return None


def _shape_to_polygons(shape):
    polygons = []
    for face in shape.Faces:
        exterior = _wire_coordinates(face.OuterWire)
        if exterior is None:
            continue
        holes = []
        for wire in face.Wires:
            try:
                if wire.isSame(face.OuterWire):
                    continue
            except Exception:
                if wire == face.OuterWire:
                    continue
            ring = _wire_coordinates(wire)
            if ring is not None:
                holes.append(ring)
        polygon = Polygon(exterior, holes)
        if not polygon.is_valid:
            polygon = shapely.make_valid(polygon)
        if not polygon.is_empty:
            polygons.append(polygon)
    return polygons


def _polygon_geometries(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry
        return
    for child in getattr(geometry, "geoms", []):
        yield from _polygon_geometries(child)


def _ring_wire(coordinates):
    points = [FreeCAD.Vector(float(x), float(y), 0)
              for x, y, *_rest in coordinates]
    if len(points) < 4:
        return None
    return Part.makePolygon(points)


def _polygons_to_part_shape(geometry):
    faces = []
    for polygon in _polygon_geometries(geometry):
        exterior = _ring_wire(polygon.exterior.coords)
        if exterior is None:
            continue
        wires = [exterior]
        wires.extend(wire for wire in (
            _ring_wire(interior.coords) for interior in polygon.interiors)
                     if wire is not None)
        face = (Part.Face(wires[0]) if len(wires) == 1
                else Part.Face(wires, "Part::FaceMakerBullseye"))
        faces.append(face)
    if not faces:
        raise RuntimeError("2D union returned no polygon faces")
    return faces[0] if len(faces) == 1 else Part.makeCompound(faces)


def union_planar_profiles(shapes, warn=None, layer_name=""):
    """Union overlapping coplanar profiles before physical extrusion."""
    shapes = [shape for shape in shapes if shape is not None]
    if not shapes:
        return None
    if len(shapes) == 1:
        return shapes[0]
    prefix = f" {layer_name}" if layer_name else ""
    if shapely is not None:
        try:
            polygons = []
            for shape in shapes:
                polygons.extend(_shape_to_polygons(shape))
            if not polygons:
                raise RuntimeError("no polygon faces were produced")
            merged = shapely.union_all(
                polygons, grid_size=COPPER_2D_GRID_MM)
            if merged.is_empty:
                raise RuntimeError("union returned an empty geometry")
            return _polygons_to_part_shape(merged)
        except Exception as ex:
            if warn:
                warn(f"Could not perform fast 2D union for{prefix}; "
                     f"trying BRep fallback: {ex}")
    elif warn:
        warn(f"Shapely is unavailable for{prefix}; trying BRep fallback")

    try:
        profile = shapes[0].multiFuse(shapes[1:])
        if profile is None or profile.isNull():
            raise RuntimeError("BRep union returned an empty shape")
        try:
            profile = profile.removeSplitter()
        except Exception:
            pass
        return profile
    except Exception as ex:
        if warn:
            warn(f"Could not union{prefix}; using separate profiles: {ex}")
        return Part.makeCompound(shapes)


def _same_face(left, right):
    try:
        return bool(left.isSame(right))
    except Exception:
        return left == right


def _face_mid_normal(face):
    try:
        u_min, u_max, v_min, v_max = face.ParameterRange
        normal = face.normalAt(
            (u_min + u_max) / 2.0,
            (v_min + v_max) / 2.0)
        normal.normalize()
        return normal
    except Exception:
        return None


def extrusion_display_faces(solid, base_face, include_end_cap=True,
                            include_base_cap=False):
    """Select extrusion walls and requested caps from one solid.

    The base cap is the source profile. The end cap is the physical surface
    reached by extrusion. Omitting interface caps entirely prevents two
    touching layer objects from submitting coincident triangles to Coin3D.
    """
    faces = list(getattr(solid, "Faces", []))
    if len(faces) <= 2:
        return faces
    base_cap = next(
        (face for face in faces if _same_face(face, base_face)), None)
    if base_cap is None:
        try:
            center = base_face.CenterOfMass
            base_cap = min(
                faces,
                key=lambda face: face.CenterOfMass.distanceToPoint(center))
        except Exception:
            base_cap = max(
                faces, key=lambda face: float(getattr(face, "Area", 0.0)))

    remaining = [face for face in faces if not _same_face(face, base_cap)]
    base_normal = _face_mid_normal(base_cap)
    cap_candidates = []
    if base_normal is not None:
        for face in remaining:
            normal = _face_mid_normal(face)
            if normal is not None and abs(normal.dot(base_normal)) >= 0.8:
                cap_candidates.append(face)
    if not cap_candidates:
        cap_candidates = sorted(
            remaining,
            key=lambda face: float(getattr(face, "Area", 0.0)),
            reverse=True,
        )[:1]
    try:
        base_center = base_cap.CenterOfMass
        end_cap = max(
            cap_candidates,
            key=lambda face: face.CenterOfMass.distanceToPoint(base_center))
    except Exception:
        end_cap = cap_candidates[0] if cap_candidates else None
    selected = [face for face in faces
                if not _same_face(face, base_cap)
                and (end_cap is None or not _same_face(face, end_cap))]
    if include_base_cap:
        selected.append(base_cap)
    if include_end_cap and end_cap is not None:
        selected.append(end_cap)
    return selected


def extrude_profile_for_display(profile, direction, cap_mode="outer"):
    """Return ``(display_shell, subtraction_solid)`` for a planar profile.

    ``outer`` shows walls plus the extrusion end cap. ``walls`` shows only
    walls. The source/base cap is never displayed.
    """
    display_faces = []
    solids = []
    vector = FreeCAD.Vector(0, 0, float(direction))
    for base_face in getattr(profile, "Faces", []):
        solid = base_face.extrude(vector)
        if solid is None or getattr(solid, "isNull", lambda: False)():
            continue
        solids.append(solid)
        display_faces.extend(extrusion_display_faces(
            solid, base_face,
            include_end_cap=cap_mode == "outer",
            include_base_cap=False))
    if not solids:
        raise RuntimeError("profile extrusion returned no solids")
    display = Part.makeCompound(display_faces)
    subtraction = solids[0] if len(solids) == 1 else Part.makeCompound(solids)
    return display, subtraction


def build_copper_layers(board, stackup, board_layer, board_shapes=None,
                        warn=None, include_outer=True, include_inner=True,
                        outer_offsets=None, total_thickness=None):
    """Read KiCad copper items and return layer descriptors with Shapes."""
    infos = copper_stackup_layers(
        stackup, board_layer, outer_offsets=outer_offsets,
        total_thickness=total_thickness)
    if not include_outer:
        infos = [info for info in infos if not info.is_outer]
    if not include_inner:
        infos = [info for info in infos if info.is_outer]
    items_by_layer = read_copper_items(
        board, [info.layer for info in infos], board_shapes, warn)
    by_layer = {info.layer: [] for info in infos}
    counts = {info.layer: 0 for info in infos}
    kind_counts = {info.layer: {} for info in infos}
    try:
        from kipy.proto.board.board_types_pb2 import PadStackShape
    except Exception:
        PadStackShape = None
    for layer, items in items_by_layer.items():
        for item in items:
            try:
                shape = _copper_item_shape(item, PadStackShape)
                if shape is None:
                    if warn:
                        warn(f"Unsupported copper {item.kind}: "
                             f"{type(item.source).__name__}")
                    continue
                by_layer[layer].append(shape)
                counts[layer] += 1
                kind_counts[layer][item.kind] = (
                    kind_counts[layer].get(item.kind, 0) + 1)
            except Exception as ex:
                if warn:
                    warn(f"Could not build copper {item.kind}: {ex}")

    try:
        copper_text = [item for item in board.get_text()
                       if item.layer in by_layer]
        if copper_text and warn:
            warn(f"Skipped {len(copper_text)} copper text item(s)")
    except Exception:
        pass

    try:
        copper_barcodes = [item for item in board.get_barcodes()
                           if item.layer in by_layer]
        if copper_barcodes and warn:
            warn(f"Skipped {len(copper_barcodes)} copper barcode(s)")
    except Exception:
        pass

    result = []
    for info in infos:
        item_shapes = by_layer[info.layer]
        if not item_shapes:
            continue
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] 2D union {info.name} start: "
            f"items={len(item_shapes)}\n")
        profile_started = time.perf_counter()
        profile = union_planar_profiles(
            item_shapes, warn=warn, layer_name=info.name)
        profile_seconds = time.perf_counter() - profile_started
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] 2D union {info.name}: "
            f"{profile_seconds:.3f}s\n")
        display_z = (info.z + info.direction
                     if info.is_outer
                     else info.z + info.direction / 2.0)
        profile.translate(FreeCAD.Vector(0, 0, display_z))
        shape = profile
        result.append({
            "layer": info.layer,
            "name": info.name,
            "z": info.z,
            "display_z": display_z,
            "thickness": info.thickness,
            "is_outer": info.is_outer,
            "direction": 0.0,
            "body_direction": info.direction,
            "item_count": counts[info.layer],
            "item_counts": kind_counts[info.layer],
            "profile_seconds": profile_seconds,
            "face_count": len(getattr(shape, "Faces", [])),
            "area": float(getattr(shape, "Area", 0.0)),
            "volume": 0.0,
            "shape": shape,
            "profile_shape": profile,
        })
    return result
