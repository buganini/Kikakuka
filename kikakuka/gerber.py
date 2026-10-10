import os
import re
import sys
import zipfile
from itertools import groupby

from .pcb_tools import gerber
import pcbnew
import math
import kikit.common
import shapely
from shapely.geometry import LineString, Point, Polygon

from .tableloader import TableLoader

PKG_BASE = os.path.dirname(os.path.dirname(__file__))
KIKAKUKA_LIB = os.path.join(
    PKG_BASE, "resources/kikakuka-internal.pretty")

if getattr(sys, 'frozen', False):
    import kikit.common
    kikit.common.KIKIT_LIB = os.path.join(sys._MEIPASS, "kikit.pretty")
    KIKAKUKA_LIB = os.path.join(
        sys._MEIPASS, "kikakuka-internal.pretty")


def get_footprint_field(footprint, name):
    if hasattr(footprint, "GetFieldByName"):
        return footprint.GetFieldByName(name)
    if hasattr(footprint, "HasField") and footprint.HasField(name):
        return footprint.GetField(name)
    return None


def is_gerber_file(filename):
    if os.path.splitext(filename)[1].lower() in (".gbr", ".gbx", ".gm1", ".gm3", ".gko", ".g1"):
        return True
    return False

def is_gerber_dir(path):
    if not os.path.isdir(path):
        return False
    for file in os.listdir(path):
        if is_gerber_file(file):
            return True
    return False

def is_gerber_zip(path):
    if not zipfile.is_zipfile(path):
        return False
    with zipfile.ZipFile(path) as z:
        for file in z.namelist():
            if is_gerber_file(file):
                return True
    return False

def is_gerber(path):
    if is_gerber_dir(path):
        return True
    if is_gerber_zip(path):
        return True
    return False

def list_gerber_files(path):
    if is_gerber_dir(path):
        return os.listdir(path)
    if is_gerber_zip(path):
        with zipfile.ZipFile(path) as z:
            return z.namelist()
    return []


CAM350_GBX_LAYER_RE = re.compile(
    r"^(?P<prefix>.+)L(?P<number>[1-9][0-9]*)(?P<kind>LQ|T|P)?\.GBX$",
    re.IGNORECASE,
)


def find_cam350_gbx_layer(filenames, position, kind=None):
    """Find a layer using CAM350's L<n>[LQ|T|P].GBX convention.

    A valid file family must contain L1 and at least one further bare copper
    layer.  This keeps an unrelated file whose name merely ends in L1.GBX from
    being classified as a PCB stackup.  L1 is the top copper layer and the
    highest numbered bare layer is the bottom; intermediate numbers are inner
    copper layers.  LQ, T, and P denote mask, silkscreen, and paste.
    """
    families = {}
    for filename in filenames:
        match = CAM350_GBX_LAYER_RE.match(os.path.basename(filename))
        if match is None:
            continue
        prefix = match.group("prefix").lower()
        number = int(match.group("number"))
        layer_kind = match.group("kind")
        if layer_kind is not None:
            layer_kind = layer_kind.upper()
        families.setdefault(prefix, []).append(
            (filename, number, layer_kind))

    for layers in families.values():
        copper_numbers = {
            number for _, number, layer_kind in layers
            if layer_kind is None
        }
        if 1 not in copper_numbers or len(copper_numbers) < 2:
            continue
        bottom_number = max(copper_numbers)

        if position == "top":
            target_number = 1
        elif position == "bottom":
            target_number = bottom_number
        else:
            target_number = position + 1
            if target_number >= bottom_number:
                continue

        for filename, number, layer_kind in layers:
            if number == target_number and layer_kind == kind:
                return filename
    return None

def find_edge_cuts(filenames):
    for fn in filenames:
        if "EdgeCut" in fn: # Bouni/kicad-jlcpcb-tools
            return fn
        if "Edge_Cuts" in fn: # KiCAD
            return fn
        if "Edge.Cuts" in fn: # KiCAD
            return fn
        if os.path.splitext(fn)[1].lower() in (".gm1", ".gm3", ".gko"):
            return fn
    return None

def find_silk_top(filenames):
    for fn in filenames:
        if "SilkTop" in fn: # Bouni/kicad-jlcpcb-tools
            return fn
        if "F_Silk" in fn: # KiCAD
            return fn
        if "F.Silk" in fn: # KiCAD
            return fn
        if fn.lower().endswith(".gto"): # Altium
            return fn
    return find_cam350_gbx_layer(filenames, "top", "T")

def find_silk_bottom(filenames):
    for fn in filenames:
        if "SilkBottom" in fn: # Bouni/kicad-jlcpcb-tools
            return fn
        if "B_Silk" in fn: # KiCAD
            return fn
        if "B.Silk" in fn: # KiCAD
            return fn
        if fn.lower().endswith(".gbo"): # Altium
            return fn
    return find_cam350_gbx_layer(filenames, "bottom", "T")

def find_cu_top(filenames):
    for fn in filenames:
        if "CuTop" in fn: # Bouni/kicad-jlcpcb-tools
            return fn
        if "F_Cu" in fn: # KiCAD
            return fn
        if "F.Cu" in fn: # KiCAD
            return fn
        if fn.lower().endswith(".gtl"): # Altium
            return fn
    return find_cam350_gbx_layer(filenames, "top")

def find_cu_bottom(filenames):
    for fn in filenames:
        if "CuBottom" in fn: # Bouni/kicad-jlcpcb-tools
            return fn
        if "B_Cu" in fn: # KiCAD
            return fn
        if "B.Cu" in fn: # KiCAD
            return fn
        if fn.lower().endswith(".gbl"): # Altium
            return fn
    return find_cam350_gbx_layer(filenames, "bottom")

def find_cu_inner(filenames, i):
    for fn in filenames:
        if f"CuIn{i}" in fn: # Bouni/kicad-jlcpcb-tools
            return fn
        if f"In{i}_Cu" in fn: # KiCAD
            return fn
        if f"In{i}.Cu" in fn: # KiCAD
            return fn
        if fn.endswith(f".G{i}"):
            return fn
    return find_cam350_gbx_layer(filenames, i)

def find_paste_top(filenames):
    for fn in filenames:
        if "F_Paste" in fn: # KiCAD
            return fn
        if "F.Paste" in fn: # KiCAD
            return fn
        if fn.lower().endswith(".gtp"): # Altium
            return fn
    return find_cam350_gbx_layer(filenames, "top", "P")

def find_paste_bottom(filenames):
    for fn in filenames:
        if "B_Paste" in fn: # KiCAD
            return fn
        if "B.Paste" in fn: # KiCAD
            return fn
        if fn.lower().endswith(".gbp"): # Altium
            return fn
    return find_cam350_gbx_layer(filenames, "bottom", "P")

def find_mask_top(filenames):
    for fn in filenames:
        if "MaskTop" in fn: # Bouni/kicad-jlcpcb-tools
            return fn
        if "F_Mask" in fn: # KiCAD
            return fn
        if "F.Mask" in fn: # KiCAD
            return fn
        if fn.lower().endswith(".gts"): # Altium
            return fn
    return find_cam350_gbx_layer(filenames, "top", "LQ")

def find_mask_bottom(filenames):
    for fn in filenames:
        if "MaskBottom" in fn: # Bouni/kicad-jlcpcb-tools
            return fn
        if "B_Mask" in fn: # KiCAD
            return fn
        if "B.Mask" in fn: # KiCAD
            return fn
        if fn.lower().endswith(".gbs"): # Altium
            return fn
    return find_cam350_gbx_layer(filenames, "bottom", "LQ")

def find_PTH(filenames):
    for fn in filenames:
        if fn.lower().endswith(".pdf"):
            continue
        if "PTH" in fn and "NPTH" not in fn:
            return fn
    return None

def find_NPTH(filenames):
    for fn in filenames:
        if fn.lower().endswith(".pdf"):
            continue
        if "NPTH" in fn:
            return fn
    return None

def find_BOM(filenames):
    for fn in filenames:
        filename = os.path.basename(fn).lower()
        if filename.endswith((".csv", ".xlsx")):
            if "bom" in filename:
                return fn
    return None

def find_CPL(filenames):
    for fn in filenames:
        filename = os.path.basename(fn).lower()
        if filename.endswith((".csv", ".xlsx")):
            if any(marker in filename for marker in ("cpl", "pos", "smt")):
                return fn
    return None


CPL_HEADER_ALIASES = {
    "designator": ("Designator", "Ref", "位置"),
    "x": ("Mid X", "PosX", "X", "Center-X"),
    "y": ("Mid Y", "PosY", "Y", "Center-Y"),
    "rotation": ("Rotation", "Rot", "角度"),
    "layer": ("Layer", "Side", "面向"),
}


def find_table_header(headers, aliases):
    return next((header for header in aliases if header in headers), None)


def table_value_is_blank(value):
    return value is None or (
        isinstance(value, str) and not value.strip())


def resolve_table_file(input_path, filename):
    if os.path.isfile(filename):
        return filename
    if is_gerber_dir(input_path):
        return os.path.join(input_path, filename)
    return filename

def read_gbr_file(path, filename):
    if is_gerber_dir(path):
        with open(os.path.join(path, filename), "r") as source:
            return source.read()
    if is_gerber_zip(path):
        with zipfile.ZipFile(path) as zf:
            path = zipfile.Path(zf, at=filename)
            return path.read_text(encoding='UTF-8')
    return None


def excellon_tool_functions(gbr):
    """Return XNC aperture functions keyed by Excellon tool number."""
    functions = {}
    pending_function = None

    for statement in getattr(gbr, "statements", []):
        if statement.__class__.__name__ == "CommentStmt":
            comment = getattr(statement, "comment", "")
            marker = "TA.AperFunction,"
            if marker in comment:
                pending_function = comment.split(marker, 1)[1].split(",")[-1]
                pending_function = pending_function.strip().rstrip("*")
        elif statement.__class__.__name__ == "ExcellonTool":
            if pending_function:
                functions[statement.number] = pending_function
            pending_function = None

    return functions


def new_pth_footprint(board, position):
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetFPIDAsString("PTH")
    footprint.SetReference("")
    footprint.SetValue("PTH")
    footprint.Reference().SetVisible(False)
    footprint.Value().SetVisible(False)
    footprint.SetExcludedFromPosFiles(True)
    footprint.SetExcludedFromBOM(True)
    footprint.SetPosition(position)
    board.Add(footprint)
    return footprint


def prepare_differ_paste_drills(board, position_tolerance=1000):
    """Make drilled Paste flashes use KiCad's native pad plotting.

    Gerber Paste flashes are normally imported as board graphics.  KiCad's PDF
    plotter only applies its drill overlay to pad-owned Paste geometry, so a
    converted Gerber and its source board otherwise render differently.  This
    normalization is intentionally reserved for Differ's temporary boards.
    """
    pth_pads = [
        pad
        for footprint in board.GetFootprints()
        for pad in footprint.Pads()
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
    ]

    matches = [(pad, []) for pad in pth_pads]
    for shape in list(board.GetDrawings()):
        if (shape.GetLayer() not in (pcbnew.F_Paste, pcbnew.B_Paste)
                or shape.GetShape() != pcbnew.SHAPE_T_CIRCLE
                or not shape.IsSolidFill()):
            continue

        center = shape.GetCenter()
        match = next((
            entry for entry in matches
            if abs(entry[0].GetPosition().x - center.x)
            <= position_tolerance
            and abs(entry[0].GetPosition().y - center.y)
            <= position_tolerance
        ), None)
        if match is None:
            continue
        match[1].append(shape)

    for pad, shapes in matches:
        if not shapes:
            continue
        diameters = [shape.GetRadius() * 2 for shape in shapes]
        if max(diameters) - min(diameters) > position_tolerance:
            continue

        diameter = diameters[0]
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(diameter, diameter))
        layers = pad.GetLayerSet()
        for shape in shapes:
            layers.AddLayer(shape.GetLayer())
        pad.SetLayerSet(layers)
        for shape in shapes:
            board.Remove(shape)


def arc_path_points(arc, max_error):
    """Approximate a Gerber arc with points no farther than max_error away."""
    start_angle = math.atan2(
        arc.start[1] - arc.center[1], arc.start[0] - arc.center[0])
    end_angle = math.atan2(
        arc.end[1] - arc.center[1], arc.end[0] - arc.center[0])
    if arc.direction == "counterclockwise":
        sweep = (end_angle - start_angle) % (2 * math.pi)
    else:
        sweep = -((start_angle - end_angle) % (2 * math.pi))
    if abs(sweep) < 1e-12 and arc.start == arc.end:
        sweep = (2 * math.pi if arc.direction == "counterclockwise"
                 else -2 * math.pi)

    radius = arc.radius
    if radius <= max_error:
        max_step = math.pi / 12
    else:
        max_step = 2 * math.acos(max(-1, 1 - max_error / radius))
        max_step = min(max_step, math.pi / 12)
    steps = max(1, math.ceil(abs(sweep) / max_step))
    points = [
        (
            arc.center[0] + radius * math.cos(start_angle + sweep * i / steps),
            arc.center[1] + radius * math.sin(start_angle + sweep * i / steps),
        )
        for i in range(1, steps)
    ]
    points.append(arc.end)
    return points


def curve_approximation_error(units):
    return 0.001 if units != "inch" else 0.001 / 25.4


def circle_quad_segs(radius, max_error):
    """Return a Shapely circle resolution within the requested chord error."""
    if radius <= max_error:
        max_step = math.pi / 12
    else:
        max_step = 2 * math.acos(max(-1, 1 - max_error / radius))
        max_step = min(max_step, math.pi / 12)
    return max(1, math.ceil(math.pi / 2 / max_step))


def path_geometry(path, units=None):
    """Convert a closed Gerber path, including curved edges, to geometry."""
    if not path.primitives:
        return Polygon()
    units = path.units or units
    max_error = curve_approximation_error(units)
    points = [path.primitives[0].start]
    for edge in path.primitives:
        if isinstance(edge, gerber.primitives.Arc):
            points.extend(arc_path_points(edge, max_error))
        else:
            points.append(edge.end)
    geometry = Polygon(points)
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    return geometry


def rectangle_geometry(position, width, height, rotation=0):
    """Create a rectangle rotated about its center."""
    angle = math.radians(rotation)
    cos_angle = math.cos(angle)
    sin_angle = math.sin(angle)
    geometry = []
    for x, y in (
            (-width / 2, -height / 2),
            (-width / 2, height / 2),
            (width / 2, height / 2),
            (width / 2, -height / 2)):
        geometry.append((
            position[0] + x * cos_angle - y * sin_angle,
            position[1] + x * sin_angle + y * cos_angle,
        ))
    return Polygon(geometry)


def subtract_flash_holes(geometry, primitive, units=None):
    """Subtract circular or rectangular holes from a flashed aperture."""
    max_error = curve_approximation_error(primitive.units or units)
    hole_diameter = getattr(primitive, "hole_diameter", 0) or 0
    if hole_diameter > 0:
        radius = hole_diameter / 2
        hole = Point(primitive.position).buffer(
            radius, quad_segs=circle_quad_segs(radius, max_error))
        geometry = geometry.difference(hole)

    hole_width = getattr(primitive, "hole_width", 0) or 0
    hole_height = getattr(primitive, "hole_height", 0) or 0
    if hole_width > 0 and hole_height > 0:
        hole = rectangle_geometry(
            primitive.position,
            hole_width,
            hole_height,
            getattr(primitive, "rotation", 0),
        )
        geometry = geometry.difference(hole)
    return geometry


def composite_geometry(primitives, units=None):
    """Apply the ordered polarities in a sequence of Gerber primitives."""
    geometry = Polygon()
    for polarity, grouped in groupby(
            primitives,
            key=lambda primitive: getattr(
                primitive, "level_polarity", "dark") or "dark"):
        operands = []
        for primitive in grouped:
            operand = primitive_geometry(primitive, units)
            if operand is None:
                return None, primitive
            operands.append(operand)
        operand = shapely.union_all(operands)
        if polarity == "clear":
            geometry = geometry.difference(operand)
        else:
            geometry = geometry.union(operand)
    return geometry, None


def primitive_geometry(primitive, units=None):
    """Convert a drawable Gerber primitive to polarity-composable geometry."""
    units = primitive.units or units
    max_error = curve_approximation_error(units)

    if isinstance(primitive, (gerber.primitives.Region,
                              gerber.primitives.Outline)):
        return path_geometry(primitive, units)

    if isinstance(primitive, gerber.primitives.AMGroup):
        geometry, unsupported = composite_geometry(
            primitive.primitives, units)
        return None if unsupported is not None else geometry

    if isinstance(primitive, gerber.primitives.Circle):
        geometry = Point(primitive.position).buffer(
            primitive.radius,
            quad_segs=circle_quad_segs(primitive.radius, max_error),
        )
        return subtract_flash_holes(geometry, primitive, units)

    if isinstance(primitive, gerber.primitives.Rectangle):
        geometry = rectangle_geometry(
            primitive.position,
            primitive.width,
            primitive.height,
            primitive.rotation,
        )
        return subtract_flash_holes(geometry, primitive, units)

    if isinstance(primitive, gerber.primitives.Obround):
        radius = min(primitive.width, primitive.height) / 2
        length = abs(primitive.width - primitive.height)
        if length == 0:
            geometry = Point(primitive.position).buffer(
                radius, quad_segs=circle_quad_segs(radius, max_error))
        else:
            if primitive.width > primitive.height:
                delta = (length / 2, 0)
            else:
                delta = (0, length / 2)
            angle = math.radians(primitive.rotation)
            dx = delta[0] * math.cos(angle) - delta[1] * math.sin(angle)
            dy = delta[0] * math.sin(angle) + delta[1] * math.cos(angle)
            geometry = LineString([
                (primitive.position[0] - dx, primitive.position[1] - dy),
                (primitive.position[0] + dx, primitive.position[1] + dy),
            ]).buffer(
                radius,
                quad_segs=circle_quad_segs(radius, max_error),
                cap_style="round",
            )
        return subtract_flash_holes(geometry, primitive, units)

    if isinstance(primitive, gerber.primitives.Polygon):
        geometry = Polygon(primitive.vertices)
        return subtract_flash_holes(geometry, primitive, units)

    if isinstance(primitive, gerber.primitives.Line):
        if isinstance(primitive.aperture, gerber.primitives.Circle):
            radius = primitive.aperture.radius
            return LineString([primitive.start, primitive.end]).buffer(
                radius,
                quad_segs=circle_quad_segs(radius, max_error),
                cap_style="round",
            )
        if primitive.vertices is not None:
            return Polygon(primitive.vertices)
        return None

    if isinstance(primitive, gerber.primitives.Arc):
        if not isinstance(primitive.aperture, gerber.primitives.Circle):
            return None
        radius = primitive.aperture.radius
        points = [primitive.start]
        points.extend(arc_path_points(primitive, max_error))
        return LineString(points).buffer(
            radius,
            quad_segs=circle_quad_segs(radius, max_error),
            cap_style="round",
            join_style="round",
        )

    vertices = getattr(primitive, "vertices", None)
    if vertices is not None:
        return subtract_flash_holes(Polygon(vertices), primitive, units)
    return None


def iter_polygons(geometry):
    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
        return
    for child in getattr(geometry, "geoms", []):
        yield from iter_polygons(child)


def append_ring(poly_set, coordinates, fromUnit, outline, hole=-1):
    points = []
    for x, y in list(coordinates)[:-1]:
        point = (fromUnit(x), -fromUnit(y))
        if not points or point != points[-1]:
            points.append(point)
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    for x, y in points:
        poly_set.Append(x, y, outline, hole)


def populate_kicad_by_composited_regions(
        board, primitives, fromUnit, layer, errors):
    """Apply ordered dark/clear Gerber primitives and add the result."""
    geometry, unsupported = composite_geometry(primitives)
    if unsupported is not None:
        errors.append(
            "Cannot composite Gerber polarity containing "
            f"{unsupported.__class__.__name__}")
        return False

    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    for polygon in iter_polygons(geometry):
        poly_set = pcbnew.SHAPE_POLY_SET()
        outline = poly_set.NewOutline()
        append_ring(poly_set, polygon.exterior.coords, fromUnit, outline)
        for interior in polygon.interiors:
            hole = poly_set.NewHole(outline)
            append_ring(
                poly_set, interior.coords, fromUnit, outline, hole)

        shape = pcbnew.PCB_SHAPE()
        shape.SetShape(pcbnew.SHAPE_T_POLY)
        shape.SetPolyShape(poly_set)
        shape.SetLayer(layer)
        shape.SetFilled(True)
        shape.SetWidth(0)
        board.Add(shape)
    return True


def populate_kicad(board, gbr, layer, errors):
    # print(gbr, dir(gbr))
    # print(gbr.__dict__)

    def fromMM(value):
        return int(value * pcbnew.PCB_IU_PER_MM)

    def fromInch(value):
        return int(value * pcbnew.PCB_IU_PER_MM * 25.4)

    fromUnit = {
        "inch": fromInch,
        "metric": fromMM,
    }.get(gbr.units)

    primitives = gbr.primitives
    hits = getattr(gbr, "hits", None)
    tool_functions = excellon_tool_functions(gbr) if hits is not None else {}

    annotated_primitives = []
    for index, primitive in enumerate(primitives):
        drill_function = None
        if hits is not None and index < len(hits):
            drill_function = tool_functions.get(hits[index].tool.number)
        annotated_primitives.append((primitive, drill_function))

    clear_indexes = [
        index for index, (primitive, _) in enumerate(annotated_primitives)
        if getattr(primitive, "level_polarity", "dark") == "clear"
    ]
    if clear_indexes:
        last_clear = clear_indexes[-1]
        composited = populate_kicad_by_composited_regions(
            board,
            [primitive for primitive, _ in annotated_primitives[:last_clear + 1]],
            fromUnit,
            layer,
            errors,
        )
        if composited:
            annotated_primitives = annotated_primitives[last_clear + 1:]

    pth_footprint = None
    for primitive, drill_function in annotated_primitives:
        if (layer is True and drill_function == "ComponentDrill"
                and isinstance(primitive, gerber.primitives.Drill)):
            pth_footprint = new_pth_footprint(board, pcbnew.VECTOR2I(
                fromUnit(primitive.position[0]),
                -fromUnit(primitive.position[1])
            ))
            break

    for primitive, drill_function in annotated_primitives:
        populate_kicad_by_primitive(
            board, primitive, fromUnit, layer, errors, drill_function,
            pth_footprint)

def populate_kicad_by_primitive(
        board, primitive, fromUnit, layer, errors, drill_function=None,
        pth_footprint=None):
    if isinstance(primitive, gerber.primitives.Arc):
        # print(primitive.__class__.__name__, primitive.__dict__)
        # print(dir(primitive))

        if isinstance(primitive.aperture, gerber.primitives.Circle):
            start = primitive.start if primitive.direction == "clockwise" else primitive.end
            sweep = (primitive.start_angle - primitive.end_angle) if primitive.direction == "clockwise" else (primitive.end_angle - primitive.start_angle)

            arc = pcbnew.PCB_SHAPE()
            arc.SetShape(pcbnew.SHAPE_T_ARC)

            arc.SetStart(pcbnew.VECTOR2I(
                fromUnit(start[0]),
                -fromUnit(start[1])
            ))
            arc.SetCenter(pcbnew.VECTOR2I(
                fromUnit(primitive.center[0]),
                -fromUnit(primitive.center[1])
            ))
            arc.SetArcAngleAndEnd(pcbnew.EDA_ANGLE(sweep, pcbnew.RADIANS_T))

            arc.SetLayer(layer)
            arc.SetWidth(fromUnit(primitive.aperture.radius * 2))

            board.Add(arc)
        else:
            errors.append(f"Unhandled aperture type {primitive.aperture.__class__.__name__} for Arc primitive")
    elif isinstance(primitive, gerber.primitives.Line):
        if isinstance(primitive.aperture, gerber.primitives.Circle):
            # print(primitive.__class__.__name__, primitive.__dict__)
            # print(dir(primitive))

            line = pcbnew.PCB_SHAPE()

            line.SetShape(pcbnew.SHAPE_T_SEGMENT)

            line.SetStart(pcbnew.VECTOR2I(
                fromUnit(primitive.start[0]),
                -fromUnit(primitive.start[1])
            ))

            line.SetEnd(pcbnew.VECTOR2I(
                fromUnit(primitive.end[0]),
                -fromUnit(primitive.end[1])
            ))

            line.SetLayer(layer)
            line.SetWidth(fromUnit(primitive.aperture.radius * 2))

            board.Add(line)
        else:
            errors.append(f"Unhandled aperture type {primitive.aperture.__class__.__name__} for Line primitive")
    elif isinstance(primitive, gerber.primitives.Rectangle):
        # print(primitive.__class__.__name__, primitive.__dict__)
        # print(dir(primitive))

        rectangle = pcbnew.PCB_SHAPE()
        rectangle.SetShape(pcbnew.SHAPE_T_RECTANGLE)

        rectangle.SetStart(pcbnew.VECTOR2I(
            fromUnit(primitive.position[0] - primitive.width / 2),
            -fromUnit(primitive.position[1] - primitive.height / 2)
        ))
        rectangle.SetEnd(pcbnew.VECTOR2I(
            fromUnit(primitive.position[0] + primitive.width / 2),
            -fromUnit(primitive.position[1] + primitive.height / 2)
        ))

        rectangle.SetLayer(layer)
        rectangle.SetWidth(fromUnit(0.0))
        rectangle.SetFilled(True)
        board.Add(rectangle)
    elif isinstance(primitive, gerber.primitives.Circle):
        # print(primitive.__class__.__name__, primitive.__dict__)
        # print(dir(primitive))

        circle = pcbnew.PCB_SHAPE()
        circle.SetShape(pcbnew.SHAPE_T_CIRCLE)
        circle.SetCenter(pcbnew.VECTOR2I(
            fromUnit(primitive.position[0]),
            -fromUnit(primitive.position[1])
        ))
        circle.SetRadius(fromUnit(primitive.radius))
        circle.SetLayer(layer)
        circle.SetFilled(True)
        circle.SetWidth(fromUnit(0.0))
        board.Add(circle)
    elif isinstance(primitive, gerber.primitives.AMGroup):
        for amp in primitive.primitives:
            populate_kicad_by_primitive(board, amp, fromUnit, layer, errors)
    elif isinstance(primitive, gerber.primitives.Obround):
        # print(primitive.__class__.__name__, primitive.__dict__)
        # print(dir(primitive))
        if primitive.hole_diameter == 0:
            if primitive.width > primitive.height: # horizontal obround
                line = pcbnew.PCB_SHAPE()

                line.SetShape(pcbnew.SHAPE_T_SEGMENT)

                line.SetStart(pcbnew.VECTOR2I(
                    fromUnit(primitive.position[0] - primitive.width / 2 + primitive.height / 2),
                    -fromUnit(primitive.position[1])
                ))

                line.SetEnd(pcbnew.VECTOR2I(
                    fromUnit(primitive.position[0] + primitive.width / 2 - primitive.height / 2),
                    -fromUnit(primitive.position[1])
                ))

                line.SetLayer(layer)
                line.SetWidth(fromUnit(primitive.height))

                board.Add(line)
            else: # vertical obround
                line = pcbnew.PCB_SHAPE()

                line.SetShape(pcbnew.SHAPE_T_SEGMENT)

                line.SetStart(pcbnew.VECTOR2I(
                    fromUnit(primitive.position[0]),
                    -fromUnit(primitive.position[1] - primitive.height / 2 + primitive.width / 2)
                ))

                line.SetEnd(pcbnew.VECTOR2I(
                    fromUnit(primitive.position[0]),
                    -fromUnit(primitive.position[1] + primitive.height / 2 - primitive.width / 2)
                ))

                line.SetLayer(layer)
                line.SetWidth(fromUnit(primitive.width))

                board.Add(line)

        else:
            errors.append("Unhandled Obround primitive with hole")

    elif isinstance(primitive, gerber.primitives.Outline):
        poly = pcbnew.PCB_SHAPE()
        poly.SetShape(pcbnew.SHAPE_T_POLY)

        poly.SetLayer(layer)

        poly_set = poly.GetPolyShape()
        outline = poly_set.NewOutline()

        for line in primitive.primitives:
            poly_set.Append(
                fromUnit(line.start[0]),
                -fromUnit(line.start[1]),
                outline
            )

        poly.SetFilled(True)
        poly.SetWidth(fromUnit(0.0))
        board.Add(poly)
    elif isinstance(primitive, gerber.primitives.Slot):
        # print(primitive.__class__.__name__, primitive.__dict__)
        # print(dir(primitive))
        if layer is True: # PTH
            errors.append("Unhandled PTH slot")
        elif layer is False: # NPTH
            footprint = pcbnew.FootprintLoad(kikit.common.KIKIT_LIB, "NPTH")
            footprint.SetPosition(pcbnew.VECTOR2I(
                fromUnit((primitive.start[0] + primitive.end[0]) / 2),
                -fromUnit((primitive.start[1] + primitive.end[1]) / 2)
            ))
            footprint.SetExcludedFromPosFiles(True)
            footprint.SetExcludedFromBOM(True)
            for pad in footprint.Pads():
                pad.SetShape(pcbnew.PAD_SHAPE_OVAL)
                pad.SetDrillShape(pcbnew.PAD_DRILL_SHAPE_OBLONG)
                if primitive.start[0] == primitive.end[0]: # vertical slot
                    w = fromUnit(primitive.diameter)
                    h = fromUnit(abs(primitive.start[1] - primitive.end[1]) + primitive.diameter)
                    pad.SetSize(pcbnew.VECTOR2I(w, h))
                    pad.SetDrillSize(pcbnew.VECTOR2I(w, h))
                elif primitive.start[1] == primitive.end[1]: # horizontal slot
                    w = fromUnit(abs(primitive.start[0] - primitive.end[0]) + primitive.diameter)
                    h = fromUnit(primitive.diameter)
                    pad.SetSize(pcbnew.VECTOR2I(w, h))
                    pad.SetDrillSize(pcbnew.VECTOR2I(w, h))
                else:
                    left, right = (primitive.start, primitive.end) if primitive.start[0] < primitive.end[0] else (primitive.end, primitive.start)
                    rotation = math.atan2(right[1] - left[1], right[0] - left[0])
                    footprint.SetOrientation(pcbnew.EDA_ANGLE(rotation, pcbnew.RADIANS_T))
                    distance = math.sqrt((right[0] - left[0])**2 + (right[1] - left[1])**2)
                    w = fromUnit(distance + primitive.diameter)
                    h = fromUnit(primitive.diameter)
                    footprint.SetPosition(pcbnew.VECTOR2I(
                        fromUnit((left[0] + right[0]) / 2),
                        -fromUnit((left[1] + right[1]) / 2)
                    ))
                    pad.SetSize(pcbnew.VECTOR2I(w, h))
                    pad.SetDrillSize(pcbnew.VECTOR2I(w, h))
            board.Add(footprint)
        else:
            errors.append("Unhandled slot on layer", layer)
    elif isinstance(primitive, gerber.primitives.Region):
        # print(primitive.__class__.__name__, primitive.__dict__)
        # print(dir(primitive))

        poly = pcbnew.PCB_SHAPE()
        poly.SetShape(pcbnew.SHAPE_T_POLY)

        poly.SetLayer(layer)

        poly_set = poly.GetPolyShape()
        outline = poly_set.NewOutline()

        for line in primitive.primitives:
            poly_set.Append(
                fromUnit(line.start[0]),
                -fromUnit(line.start[1]),
                outline
            )

        poly.SetFilled(True)
        poly.SetWidth(fromUnit(0.0))
        board.Add(poly)
    elif isinstance(primitive, gerber.primitives.Drill):
        # print(primitive.__class__.__name__, primitive.__dict__)
        # print(dir(primitive))

        if layer is True and drill_function == "ComponentDrill":
            pad = pcbnew.PAD(pth_footprint)
            pad.SetPosition(pcbnew.VECTOR2I(
                fromUnit(primitive.position[0]),
                -fromUnit(primitive.position[1])
            ))
            diameter = fromUnit(primitive.diameter)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
            pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
            pad.SetDrillSize(pcbnew.VECTOR2I(diameter, diameter))
            # Keep the synthetic annulus below Gerber precision.  A non-zero
            # annulus is needed for KiCad to plot this as a PTH pad; the
            # imported copper and mask layers remain authoritative.
            pad.SetSize(pcbnew.VECTOR2I(diameter + 1, diameter + 1))
            pth_footprint.Add(pad)
        elif layer: # plated via or an unclassified plated drill
            via = pcbnew.PCB_VIA(board)

            via.SetPosition(pcbnew.VECTOR2I(
                fromUnit(primitive.position[0]),
                -fromUnit(primitive.position[1])
            ))
            via.SetWidth(fromUnit(primitive.diameter))
            via.SetDrill(fromUnit(primitive.diameter))
            via.SetViaType(pcbnew.VIATYPE_THROUGH)

            board.Add(via)
        else:
            footprint = pcbnew.FootprintLoad(kikit.common.KIKIT_LIB, "NPTH")
            footprint.SetExcludedFromPosFiles(True)
            footprint.SetExcludedFromBOM(True)
            footprint.SetPosition(pcbnew.VECTOR2I(
                fromUnit(primitive.position[0]),
                -fromUnit(primitive.position[1])
            ))
            for pad in footprint.Pads():
                pad.SetDrillSizeX(fromUnit(primitive.diameter))
                pad.SetDrillSizeY(fromUnit(primitive.diameter))
                pad.SetSizeX(fromUnit(primitive.diameter))
                pad.SetSizeY(fromUnit(primitive.diameter))
            board.Add(footprint)
    else:
        # print(primitive.__class__.__name__, primitive.__dict__)
        # print(dir(primitive))
        errors.append(f"Unhandled primitive {primitive.__class__.__name__}")

def convert_to_kicad(
        input, output, required_edge_cuts=True, outline_only=False,
        bom_file=None, cpl_file=None, extra_files=None, differ_mode=False):
    filenames = list_gerber_files(input)
    if extra_files:
        filenames.extend(extra_files)
    # Layer detection must see the complete stackup.  The mutable list below
    # is consumed as files are imported, which would otherwise make numbered
    # CAM350 layers impossible to classify after L1 has been removed.
    layer_filenames = list(filenames)
    # print("filenames", filenames)

    edge_cuts_file = find_edge_cuts(layer_filenames)
    if edge_cuts_file is None and required_edge_cuts:
        raise ValueError(f"Edge cuts not found in {input}")

    board = pcbnew.BOARD()

    errors = []

    if edge_cuts_file:
        print("edge_cuts_file", edge_cuts_file)
        filenames.remove(edge_cuts_file)
        edge_cuts_data = read_gbr_file(input, edge_cuts_file)
        gbr = gerber.loads(edge_cuts_data)

        populate_kicad(board, gbr, pcbnew.Edge_Cuts, errors)

    if not outline_only:
        cu_top_file = find_cu_top(layer_filenames)
        if cu_top_file is not None:
            print("cu_top_file", cu_top_file)
            filenames.remove(cu_top_file)
            cu_top_data = read_gbr_file(input, cu_top_file)
            gbr = gerber.loads(cu_top_data)
            populate_kicad(board, gbr, pcbnew.F_Cu, errors)

        found_inner_layer = 0
        inner_layers = [pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.In3_Cu, pcbnew.In4_Cu, pcbnew.In5_Cu, pcbnew.In6_Cu, pcbnew.In7_Cu, pcbnew.In8_Cu, pcbnew.In9_Cu, pcbnew.In10_Cu, pcbnew.In11_Cu, pcbnew.In12_Cu, pcbnew.In13_Cu, pcbnew.In14_Cu, pcbnew.In15_Cu, pcbnew.In16_Cu, pcbnew.In17_Cu, pcbnew.In18_Cu, pcbnew.In19_Cu, pcbnew.In20_Cu, pcbnew.In21_Cu, pcbnew.In22_Cu, pcbnew.In23_Cu, pcbnew.In24_Cu, pcbnew.In25_Cu, pcbnew.In26_Cu, pcbnew.In27_Cu, pcbnew.In28_Cu, pcbnew.In29_Cu, pcbnew.In30_Cu]
        for i in range(len(inner_layers)):
            cu_inner_file = find_cu_inner(layer_filenames, i+1)
            if cu_inner_file is not None:
                print("cu_inner_file[{}]".format(i+1), cu_inner_file)
                filenames.remove(cu_inner_file)
                cu_inner_data = read_gbr_file(input, cu_inner_file)
                gbr = gerber.loads(cu_inner_data)
                populate_kicad(board, gbr, inner_layers[found_inner_layer], errors)
                found_inner_layer += 1

        cu_bottom_file = find_cu_bottom(layer_filenames)
        if cu_bottom_file is not None:
            print("cu_bottom_file", cu_bottom_file)
            filenames.remove(cu_bottom_file)
            cu_bottom_data = read_gbr_file(input, cu_bottom_file)
            gbr = gerber.loads(cu_bottom_data)
            populate_kicad(board, gbr, pcbnew.B_Cu, errors)

        board.SetCopperLayerCount(found_inner_layer + 2)

        silk_top_file = find_silk_top(layer_filenames)
        if silk_top_file is not None:
            print("silk_top_file", silk_top_file)
            filenames.remove(silk_top_file)
            silk_top_data = read_gbr_file(input, silk_top_file)
            gbr = gerber.loads(silk_top_data)
            populate_kicad(board, gbr, pcbnew.F_SilkS, errors)

        silk_bottom_file = find_silk_bottom(layer_filenames)
        if silk_bottom_file is not None:
            print("silk_bottom_file", silk_bottom_file)
            filenames.remove(silk_bottom_file)
            silk_bottom_data = read_gbr_file(input, silk_bottom_file)
            gbr = gerber.loads(silk_bottom_data)
            populate_kicad(board, gbr, pcbnew.B_SilkS, errors)

        mask_top_file = find_mask_top(layer_filenames)
        if mask_top_file is not None:
            print("mask_top_file", mask_top_file)
            filenames.remove(mask_top_file)
            mask_top_data = read_gbr_file(input, mask_top_file)
            gbr = gerber.loads(mask_top_data)
            populate_kicad(board, gbr, pcbnew.F_Mask, errors)

        mask_bottom_file = find_mask_bottom(layer_filenames)
        if mask_bottom_file is not None:
            print("mask_bottom_file", mask_bottom_file)
            filenames.remove(mask_bottom_file)
            mask_bottom_data = read_gbr_file(input, mask_bottom_file)
            gbr = gerber.loads(mask_bottom_data)
            populate_kicad(board, gbr, pcbnew.B_Mask, errors)

        paste_top_file = find_paste_top(layer_filenames)
        if paste_top_file is not None:
            print("paste_top_file", paste_top_file)
            filenames.remove(paste_top_file)
            paste_top_data = read_gbr_file(input, paste_top_file)
            gbr = gerber.loads(paste_top_data)
            populate_kicad(board, gbr, pcbnew.F_Paste, errors)

        paste_bottom_file = find_paste_bottom(layer_filenames)
        if paste_bottom_file is not None:
            print("paste_bottom_file", paste_bottom_file)
            filenames.remove(paste_bottom_file)
            paste_bottom_data = read_gbr_file(input, paste_bottom_file)
            gbr = gerber.loads(paste_bottom_data)
            populate_kicad(board, gbr, pcbnew.B_Paste, errors)

        pth_file = find_PTH(filenames)
        if pth_file is not None:
            print("pth_file", pth_file)
            filenames.remove(pth_file)
            pth_data = read_gbr_file(input, pth_file)
            gbr = gerber.loads(pth_data)
            populate_kicad(board, gbr, True, errors)

        npth_file = find_NPTH(filenames)
        if npth_file is not None:
            print("npth_file", npth_file)
            filenames.remove(npth_file)
            npth_data = read_gbr_file(input, npth_file)
            gbr = gerber.loads(npth_data)
            populate_kicad(board, gbr, False, errors)

        if bom_file is None:
            bom_file = find_BOM(filenames)
        if cpl_file is None:
            cpl_file = find_CPL(filenames)

        if cpl_file:
            if bom_file:
                print("bom_file", bom_file)
            print("cpl_file", cpl_file)
            bom_entries = {}
            bom_comment_header = None
            bom_footprint_header = None
            bom_ignore_headers = ["Quantity", "Qty", "Item #", "Id"]

            if bom_file:
                bom_loader = TableLoader(resolve_table_file(input, bom_file))
                try:
                    bom_rows = bom_loader.rows()
                    bom_header = next(bom_rows)
                    bom_designator_header = find_table_header(
                        bom_header, ("Designator",))
                    bom_comment_header = find_table_header(
                        bom_header, ("Comment",))
                    bom_footprint_header = find_table_header(
                        bom_header, ("Footprint",))
                    if bom_designator_header:
                        for row_number, row in enumerate(bom_rows, start=2):
                            if all(table_value_is_blank(value)
                                   for value in row):
                                continue
                            entry = {k: v for k, v in zip(bom_header, row)}
                            designators = entry.pop(bom_designator_header)
                            if table_value_is_blank(designators):
                                errors.append(
                                    f"BOM row {row_number} is missing "
                                    "Designator")
                                continue
                            for designator in str(designators).split(","):
                                bom_entries[designator.strip()] = entry
                finally:
                    bom_loader.close()

            cpl_loader = TableLoader(resolve_table_file(input, cpl_file))
            cpl_rows = cpl_loader.rows()
            cpl_header = next(cpl_rows)
            cpl_designator_header = find_table_header(
                cpl_header, CPL_HEADER_ALIASES["designator"])
            cpl_x_header = find_table_header(
                cpl_header, CPL_HEADER_ALIASES["x"])
            cpl_y_header = find_table_header(
                cpl_header, CPL_HEADER_ALIASES["y"])
            cpl_rotation_header = find_table_header(
                cpl_header, CPL_HEADER_ALIASES["rotation"])
            cpl_layer_header = find_table_header(
                cpl_header, CPL_HEADER_ALIASES["layer"])
            layer_map = {
                "top": pcbnew.F_Cu,
                "t": pcbnew.F_Cu,
                "bottom": pcbnew.B_Cu,
                "b": pcbnew.B_Cu,
            }
            required_headers = {
                "designator": cpl_designator_header,
                "x": cpl_x_header,
                "y": cpl_y_header,
                "rotation": cpl_rotation_header,
                "layer": cpl_layer_header,
            }
            missing_headers = [
                name for name, header in required_headers.items()
                if header is None
            ]

            try:
                if missing_headers:
                    errors.append(
                        "CPL is missing required columns: "
                        + ", ".join(missing_headers))
                else:
                    unit = pcbnew.PCB_IU_PER_MM
                    for row_number, row in enumerate(cpl_rows, start=2):
                        if all(table_value_is_blank(value) for value in row):
                            continue
                        entry = {k: v for k, v in zip(cpl_header, row)}
                        required_values = {
                            "designator": entry.get(cpl_designator_header),
                            "x": entry.get(cpl_x_header),
                            "y": entry.get(cpl_y_header),
                            "rotation": entry.get(cpl_rotation_header),
                            "layer": entry.get(cpl_layer_header),
                        }
                        missing_values = [
                            name for name, value in required_values.items()
                            if table_value_is_blank(value)
                        ]
                        if missing_values:
                            errors.append(
                                f"CPL row {row_number} is missing required "
                                "values: " + ", ".join(missing_values))
                            continue

                        designator = str(required_values["designator"]).strip()
                        try:
                            mid_x = float(required_values["x"]) * unit
                            mid_y = -float(required_values["y"]) * unit
                            rotation = float(required_values["rotation"])
                        except (TypeError, ValueError):
                            errors.append(
                                f"CPL row {row_number} has invalid numeric "
                                "values")
                            continue
                        layer = str(required_values["layer"]).strip().lower()
                        if layer not in layer_map:
                            errors.append(
                                f"CPL row {row_number} has unknown layer: "
                                f"{required_values['layer']}")
                            continue

                        # print(designator, mid_x/mm, -mid_y/mm, rotation, layer)
                        footprint = pcbnew.FootprintLoad(
                            KIKAKUKA_LIB, "Footprint")
                        bom_entry = bom_entries.get(designator, {})
                        footprint.SetFPIDAsString(
                            bom_entry.get(bom_footprint_header, ""))
                        footprint.SetPosition(
                            pcbnew.VECTOR2I(round(mid_x), round(mid_y)))
                        footprint.SetOrientation(
                            pcbnew.EDA_ANGLE(rotation, pcbnew.DEGREES_T))
                        footprint.SetLayer(layer_map[layer])
                        for k, v in bom_entry.items():
                            if not v:
                                continue
                            if k in [bom_comment_header, bom_footprint_header]:
                                continue
                            if k in bom_ignore_headers:
                                continue
                            footprint.SetField(k, v)
                            text = get_footprint_field(footprint, k)
                            if text:
                                text.SetVisible(False)
                        footprint.SetReference(designator)
                        footprint.SetValue(
                            bom_entry.get(bom_comment_header, ""))
                        ref = footprint.Reference()
                        ref.SetVisible(True)
                        board.Add(footprint)
            finally:
                cpl_loader.close()
        print(filenames)

    if differ_mode:
        prepare_differ_paste_drills(board)

    board.Save(output)

    return errors

if __name__ == "__main__":
    import sys
    errors = convert_to_kicad(sys.argv[1], sys.argv[2], required_edge_cuts=False, extra_files=sys.argv[3:])
    if errors:
        print("Errors:")
        for error in errors:
            print(error)
