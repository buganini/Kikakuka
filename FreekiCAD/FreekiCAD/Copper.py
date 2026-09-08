"""Build display geometry for KiCad copper layers.

Copper is represented as planar faces.  The stackup thickness is metadata;
the faces themselves stay zero-thickness so importing copper cannot change
component or coupler placement and does not double-count board thickness.
"""

from dataclasses import dataclass
import math

import FreeCAD
import Part


NM_PER_MM = 1_000_000.0
DEFAULT_COPPER_THICKNESS_MM = 0.035
COPPER_DISPLAY_OFFSET_MM = 0.020
COPPER_COLOR = (0.72, 0.45, 0.12)


@dataclass(frozen=True)
class CopperLayerInfo:
    layer: int
    name: str
    z: float
    thickness: float
    is_outer: bool


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


def copper_stackup_layers(stackup, board_layer):
    """Return enabled copper layers with their board-local display Z.

    KiCad returns stackup entries from top to bottom.  The board body used by
    FreekiCAD spans z=0..total_thickness.  Outer copper is displayed 20 um
    outside that body to avoid z-fighting; inner copper stays at its physical
    layer centre and becomes visible when the board is hidden/transparent.
    """
    entries = list(stackup.layers)
    total_mm = sum(max(0, getattr(entry, "thickness", 0))
                   for entry in entries) / NM_PER_MM
    consumed_mm = 0.0
    result = []
    for entry in entries:
        thickness_mm = max(0, getattr(entry, "thickness", 0)) / NM_PER_MM
        if (getattr(entry, "enabled", True)
                and is_copper_layer(board_layer, entry.layer)):
            name = board_layer_name(board_layer, entry.layer)
            physical_thickness = thickness_mm or DEFAULT_COPPER_THICKNESS_MM
            if name == "F.Cu":
                z = total_mm + COPPER_DISPLAY_OFFSET_MM
            elif name == "B.Cu":
                z = -COPPER_DISPLAY_OFFSET_MM
            else:
                z = total_mm - consumed_mm - thickness_mm / 2.0
            result.append(CopperLayerInfo(
                layer=entry.layer,
                name=name,
                z=z,
                thickness=physical_thickness,
                is_outer=name in ("F.Cu", "B.Cu"),
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


def board_graphic_shape(graphic):
    """Build filled/stroked geometry for a copper BoardShape."""
    kind = type(graphic).__name__
    width = max(
        float(getattr(getattr(graphic.attributes, "stroke", None), "width", 0))
        / NM_PER_MM,
        0.001,
    )
    if kind == "BoardSegment":
        return _capsule(_v(graphic.start), _v(graphic.end), width)
    if kind == "BoardArc":
        return _arc_stroke(_v(graphic.start), _v(graphic.mid),
                           _v(graphic.end), width)
    if kind == "BoardCircle":
        radius = float(graphic.radius()) / NM_PER_MM
        if getattr(graphic.attributes.fill, "filled", False):
            return _circle_face(radius, _v(graphic.center))
        outer = _circle_face(radius + width / 2.0, _v(graphic.center))
        inner = _circle_face(max(0, radius - width / 2.0), _v(graphic.center))
        return outer.cut(inner) if inner is not None else outer
    if kind == "BoardRectangle":
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
    if kind == "BoardPolygon":
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


def build_copper_layers(board, stackup, board_layer, board_shapes=None,
                        warn=None, include_outer=True, include_inner=True):
    """Read KiCad copper items and return layer descriptors with Shapes."""
    infos = copper_stackup_layers(stackup, board_layer)
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
        shape = Part.makeCompound(item_shapes)
        shape.translate(FreeCAD.Vector(0, 0, info.z))
        result.append({
            "layer": info.layer,
            "name": info.name,
            "z": info.z,
            "thickness": info.thickness,
            "is_outer": info.is_outer,
            "item_count": counts[info.layer],
            "item_counts": kind_counts[info.layer],
            "face_count": len(getattr(shape, "Faces", [])),
            "area": float(getattr(shape, "Area", 0.0)),
            "shape": shape,
        })
    return result
