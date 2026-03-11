import os
import math
import FreeCAD
import Part


DEFAULT_PCB_THICKNESS = 1.6  # mm fallback


def _kipy_retry(func, max_retries=10, delay_s=1.0):
    """Call *func* and retry up to *max_retries* times when KiCad reports
    AS_NOT_READY or AS_BUSY.  Sleeps *delay_s* seconds between attempts."""
    import time
    from kipy.errors import ApiError
    from kipy.proto.common import ApiStatusCode
    _RETRYABLE = (ApiStatusCode.AS_NOT_READY, ApiStatusCode.AS_BUSY)
    for attempt in range(max_retries + 1):
        try:
            return func()
        except ApiError as e:
            if e.code in _RETRYABLE and attempt < max_retries:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: KiCad not ready, retrying "
                    f"({attempt + 1}/{max_retries})...\n")
                time.sleep(delay_s)
                continue
            raise


def _vec(x_nm, y_nm, z=0):
    """Convert KiCad nanometres to FreeCAD mm, flipping Y."""
    return FreeCAD.Vector(x_nm / 1e6, -y_nm / 1e6, z)


def _polyline_to_edges(polyline):
    """Convert a kipy PolyLine to Part edges (lines and arcs)."""
    result = []
    nodes = polyline.nodes
    if not nodes:
        return result
    for i in range(len(nodes)):
        n0 = nodes[i]
        n1 = nodes[(i + 1) % len(nodes)]
        p0 = _vec(n0.point.x, n0.point.y)
        p1 = _vec(n1.point.x, n1.point.y)
        # Check for a real arc mid-point; protobuf defaults to (0,0)
        # which is not a valid mid-point for board outline arcs.
        has_arc = (hasattr(n1, 'arc') and n1.arc is not None
                   and hasattr(n1.arc, 'mid')
                   and (n1.arc.mid.x != 0 or n1.arc.mid.y != 0))
        if has_arc:
            pm = _vec(n1.arc.mid.x, n1.arc.mid.y)
            result.append(Part.Arc(p0, pm, p1).toShape())
        else:
            result.append(Part.makeLine(p0, p1))
    return result


def _load_kicad_env_vars(kicad):
    """Load KiCad path variables from the running KiCad instance and
    its configuration files.  Returns a dict of variable name -> value."""
    import json
    import re
    env = {}

    # 1. Derive built-in paths from kicad-cli binary location
    try:
        bin_path = kicad.get_kicad_binary_path('kicad-cli')
        bin_dir = os.path.dirname(bin_path)
        parent = os.path.dirname(bin_dir)

        # 3D models
        for d in [os.path.join(parent, 'SharedSupport', '3dmodels'),
                  os.path.join(parent, 'share', 'kicad', '3dmodels')]:
            if os.path.isdir(d):
                env['KICAD9_3DMODEL_DIR'] = d
                env['KICAD8_3DMODEL_DIR'] = d
                env['KICAD7_3DMODEL_DIR'] = d
                env['KICAD6_3DMODEL_DIR'] = d
                break
    except Exception:
        pass

    # 2. Read user-defined variables from kicad_common.json
    try:
        config_bases = []
        if os.name == 'nt':
            config_bases.append(os.path.join(os.environ.get('APPDATA', ''), 'kicad'))
        else:
            config_bases.append(os.path.expanduser('~/Library/Preferences/kicad'))
            config_bases.append(os.path.expanduser('~/.config/kicad'))

        for base in config_bases:
            for ver in ['6.0', '7.0', '8.0', '9.0']:
                cfg = os.path.join(base, ver, 'kicad_common.json')
                if os.path.isfile(cfg):
                    with open(cfg, 'r') as f:
                        data = json.load(f)
                    user_vars = (data.get('environment', {}) or {}).get('vars', {})
                    if user_vars:
                        env.update(user_vars)
    except Exception:
        pass

    # 3. Derive KICAD*_3RD_PARTY from the current KiCad version's
    #    user data directory (set by PCM / Plugin Content Manager).
    try:
        data_bases = []
        if os.name == 'nt':
            data_bases.append(os.path.join(
                os.environ.get('USERPROFILE', ''), 'Documents', 'KiCad'))
        else:
            data_bases.append(os.path.expanduser('~/Documents/KiCad'))
            data_bases.append(os.path.expanduser('~/.local/share/kicad'))

        thirdparty_path = None
        for base in data_bases:
            for ver in ['9.0', '8.0', '7.0', '6.0']:
                candidate = os.path.join(base, ver, '3rdparty')
                if os.path.isdir(candidate):
                    thirdparty_path = candidate
                    break
            if thirdparty_path:
                break

        if thirdparty_path:
            for v in range(9, 5, -1):
                key = f'KICAD{v}_3RD_PARTY'
                if key not in env:
                    env[key] = thirdparty_path
    except Exception:
        pass

    # 4. Environment variables from the OS (highest priority)
    for key in list(env.keys()):
        os_val = os.environ.get(key)
        if os_val:
            env[key] = os_val

    FreeCAD.Console.PrintMessage(
        f"FreekiCAD: Loaded path variables: {env}\n"
    )
    return env


def _resolve_model_path(filename, board, kicad_vars):
    """Resolve a KiCad 3D model filename, expanding variables like
    ${KICAD9_3DMODEL_DIR}.  Returns an absolute path or None."""
    import re

    try:
        resolved = board.expand_text_variables(filename)
    except Exception:
        resolved = filename

    # Substitute ${VAR} using our collected variables, then OS env
    def _var_sub(m):
        var = m.group(1)
        val = kicad_vars.get(var) or os.environ.get(var)
        if not val:
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: Unresolved variable '${{{var}}}', "
                f"kicad_vars keys: {list(kicad_vars.keys())}\n"
            )
        return val if val else m.group(0)
    resolved = re.sub(r'\$\{(\w+)\}', _var_sub, resolved)

    # Prefer .step over .wrl – FreeCAD handles STEP reliably but
    # may fail silently on VRML (.wrl) files.
    base, ext = os.path.splitext(resolved)
    if ext.lower() == '.wrl':
        for alt in [base + '.step', base + '.stp', base + '.STEP', base + '.STP']:
            if os.path.isfile(alt):
                return alt
        # No STEP sibling found; fall through to return .wrl as-is
    if os.path.isfile(resolved):
        return resolved

    return None


_DEFAULT_COLOR = (0.8, 0.8, 0.8, 0.0)


def _read_face_colors(vobj, n_faces):
    """Read per-face colors from a ViewObject.

    Tries ShapeAppearance (FreeCAD 1.0+), then DiffuseColor (older),
    then ShapeColor (single colour fallback).
    Returns a list of n_faces (r, g, b, a) tuples.
    """
    if vobj is None:
        return [_DEFAULT_COLOR] * n_faces

    # FreeCAD 1.0+: ShapeAppearance is a list of App.Material per face
    try:
        sa = vobj.ShapeAppearance
        if sa and len(sa) > 0:
            colors = [tuple(m.DiffuseColor) for m in sa]
            if len(colors) == n_faces:
                return colors
            if len(colors) == 1:
                return colors * n_faces
    except (AttributeError, Exception):
        pass

    # Legacy: DiffuseColor
    try:
        dc = list(vobj.DiffuseColor)
        if len(dc) == n_faces:
            return dc
        if dc:
            return dc[:1] * n_faces
    except (AttributeError, Exception):
        pass

    # Single-colour fallback
    try:
        sc = vobj.ShapeColor
        return [tuple(sc) + (0.0,) if len(sc) == 3 else tuple(sc)] * n_faces
    except (AttributeError, Exception):
        pass

    return [_DEFAULT_COLOR] * n_faces


def _write_face_colors(vobj, colors):
    """Write per-face colors to a ViewObject.

    Tries ShapeAppearance (FreeCAD 1.0+), then DiffuseColor,
    then ShapeColor (single-colour fallback).
    """
    if vobj is None or not colors:
        return

    # FreeCAD 1.0+: ShapeAppearance
    try:
        mats = []
        for c in colors:
            m = FreeCAD.Material()
            m.DiffuseColor = c
            mats.append(m)
        vobj.ShapeAppearance = mats
        return
    except (AttributeError, Exception):
        pass

    # Legacy: DiffuseColor
    try:
        vobj.DiffuseColor = colors
        return
    except (AttributeError, Exception):
        pass

    # Single-colour fallback
    try:
        vobj.ShapeColor = colors[0][:3]
    except (AttributeError, Exception):
        pass


def _collect_leaf_colors(obj):
    """Recursively collect per-face colors from leaf children.

    Walks the Group tree in order.  Leaf objects (with Shape, no Group)
    contribute their per-face colours.  Container objects recurse into
    their Group children.  The face order matches the compound shape
    built by FreeCAD for the top-level container.
    """
    _skip = {'App::Origin', 'App::Plane', 'App::Line'}
    colors = []
    if hasattr(obj, 'Group') and obj.Group:
        for child in obj.Group:
            if child.TypeId in _skip:
                continue
            colors.extend(_collect_leaf_colors(child))
    elif hasattr(obj, 'Shape') and not obj.Shape.isNull():
        n = len(obj.Shape.Faces)
        vobj = getattr(obj, 'ViewObject', None)
        colors.extend(_read_face_colors(vobj, n))
    return colors


def _obj_colors(obj):
    """Get per-face colors for a single (non-container) object."""
    n = len(obj.Shape.Faces)
    vobj = getattr(obj, 'ViewObject', None)
    return _read_face_colors(vobj, n)


def _load_step(step_path, doc, cache=None):
    """Load a STEP file and return ``[(shape, colors)]`` or ``[]``.

    Uses ImportGui in a temporary document to get both shape and
    per-face DiffuseColor.  Falls back to Part.read() (no colors)
    on failure.

    If *cache* is provided, results are keyed by canonical path.
    """
    canonical = os.path.realpath(step_path)
    if cache is not None and canonical in cache:
        shape, colors = cache[canonical]
        return [(shape.copy(), list(colors) if colors else None)]

    # --- strategy 1: ImportGui (shape + colours) ---
    try:
        import ImportGui
        from PySide import QtCore
        tmp_doc = FreeCAD.newDocument("__FreekiCAD_tmp__")
        try:
            ImportGui.insert(step_path, tmp_doc.Name)
            tmp_doc.recompute()
            # Flush pending events so ViewObjects get their
            # DiffuseColor populated from the STEP colour data.
            QtCore.QCoreApplication.processEvents()

            _skip = {'App::Origin', 'App::Plane', 'App::Line'}
            child_names = set()
            for obj in tmp_doc.Objects:
                if hasattr(obj, 'Group'):
                    for child in obj.Group:
                        child_names.add(child.Name)

            shapes = []
            colors = []
            for obj in tmp_doc.Objects:
                if obj.TypeId in _skip or obj.Name in child_names:
                    continue
                if not hasattr(obj, 'Shape') or obj.Shape.isNull():
                    continue
                s = obj.Shape.copy()
                shapes.append(s)
                n = len(s.Faces)
                if hasattr(obj, 'Group') and obj.Group:
                    # Container: use parent shape (correct placement)
                    # but collect colors from leaf children.
                    leaf_colors = _collect_leaf_colors(obj)
                    if len(leaf_colors) == n:
                        colors.extend(leaf_colors)
                    else:
                        colors.extend([_DEFAULT_COLOR] * n)
                else:
                    colors.extend(_obj_colors(obj))

            if shapes:
                shape = (shapes[0] if len(shapes) == 1
                         else Part.makeCompound(shapes))
                result_colors = colors if colors else None
                if cache is not None:
                    cache[canonical] = (
                        shape.copy(),
                        list(result_colors) if result_colors else None)
                return [(shape, result_colors)]
        finally:
            FreeCAD.closeDocument(tmp_doc.Name)
    except Exception as ex:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD:   ImportGui failed for {step_path}: {ex}\n")
        try:
            FreeCAD.closeDocument("__FreekiCAD_tmp__")
        except Exception:
            pass

    # --- strategy 2: Part.read (shape only, no colours) ---
    try:
        shape = Part.read(step_path)
        if shape and not shape.isNull():
            if cache is not None:
                cache[canonical] = (shape.copy(), None)
            return [(shape, None)]
    except Exception as ex:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD:   Could not read STEP {step_path}: {ex}\n")
    return []


def _load_footprint_models(fp_info, thickness, doc, step_cache=None):
    """Load and transform 3D models for a single footprint.
    Returns (components, model_mtimes) where components is a list of
    (ref, shape, colors) tuples and model_mtimes is {canonical_path: mtime}."""
    ref = fp_info['ref']
    fp_x = fp_info['x']
    fp_y = fp_info['y']
    fp_angle = fp_info['angle']
    is_back = fp_info['is_back']

    components = []
    mtimes = {}

    for model_info in fp_info['models']:
        model_path = model_info['path']
        offset = model_info['offset']
        rotation = model_info['rotation']
        scale = model_info['scale']

        # Record model file mtime
        canonical = os.path.realpath(model_path)
        try:
            mt = os.path.getmtime(canonical)
        except OSError:
            mt = None
        mtimes[canonical] = mt

        # Load STEP (check cache status before call)
        was_cached = step_cache is not None and canonical in step_cache
        parts = _load_step(model_path, doc, cache=step_cache)
        if not parts:
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD:   {ref}: STEP load returned "
                f"no shapes: {model_path}\n"
            )
            continue

        status = "cache hit" if was_cached else "loaded"
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD:   {ref}: {status} "
            f"{os.path.basename(model_path)}\n"
        )

        for part_shape, part_colors in parts:
            # Apply model scale
            sx = scale[0] if scale[0] != 0 else 1.0
            sy = scale[1] if scale[1] != 0 else 1.0
            sz = scale[2] if scale[2] != 0 else 1.0
            if sx != 1.0 or sy != 1.0 or sz != 1.0:
                mat = FreeCAD.Matrix()
                mat.scale(sx, sy, sz)
                part_shape = part_shape.transformGeometry(mat)

            # Apply model rotation (degrees, X then Y then Z)
            # KiCad negates all rotation angles when applying 3D models
            origin = FreeCAD.Vector(0, 0, 0)
            if rotation[0] != 0:
                part_shape.rotate(
                    origin, FreeCAD.Vector(1, 0, 0), -rotation[0])
            if rotation[1] != 0:
                part_shape.rotate(
                    origin, FreeCAD.Vector(0, 1, 0), -rotation[1])
            if rotation[2] != 0:
                part_shape.rotate(
                    origin, FreeCAD.Vector(0, 0, 1), -rotation[2])

            # Apply model offset (mm in KiCad)
            part_shape.translate(FreeCAD.Vector(
                offset[0], offset[1], offset[2]))

            # Flip for back-side components
            # KiCad does Ry(180)*Rz(180): (x,y,z)→(x,-y,-z)
            if is_back:
                part_shape.rotate(
                    origin, FreeCAD.Vector(0, 0, 1), 180)
                part_shape.rotate(
                    origin, FreeCAD.Vector(0, 1, 0), 180)

            # Apply footprint rotation around origin
            if fp_angle != 0:
                part_shape.rotate(
                    origin, FreeCAD.Vector(0, 0, 1), fp_angle)

            # Move to footprint position
            # Front on top of board, back at bottom
            fp_z = thickness if not is_back else 0.0
            part_shape.translate(
                FreeCAD.Vector(fp_x, fp_y, fp_z))

            components.append((ref, part_shape, part_colors, is_back))

    return components, mtimes


# Default solder mask color when stackup has no color set.
# Matches KiCad g_DefaultSolderMask: COLOR4D(0.08, 0.20, 0.14, 0.83)
_DEFAULT_SOLDER_MASK_COLOR = (0.08, 0.20, 0.14)

# KiCad g_MaskColors – maps solder mask color names to (r, g, b) in 0‑1 range.
# Values taken from KiCad source: 3d-viewer/3d_canvas/board_adapter.cpp
_KICAD_COLOR_NAMES = {
    "green":          (0.078, 0.200, 0.141),
    "light green":    (0.357, 0.659, 0.047),
    "saturated green":(0.051, 0.408, 0.043),
    "red":            (0.710, 0.075, 0.082),
    "light red":      (0.824, 0.157, 0.055),
    "red/orange":     (0.937, 0.208, 0.161),
    "blue":           (0.008, 0.231, 0.635),
    "light blue 1":   (0.212, 0.310, 0.455),
    "light blue 2":   (0.239, 0.333, 0.510),
    "green/blue":     (0.082, 0.275, 0.314),
    "black":          (0.043, 0.043, 0.043),
    "white":          (0.961, 0.961, 0.961),
    "purple":         (0.125, 0.008, 0.208),
    "light purple":   (0.467, 0.122, 0.357),
    "yellow":         (0.761, 0.765, 0.000),
}


def _parse_color_string(color_str):
    """Convert a KiCad color string to (r, g, b) 0‑1 floats.
    Accepts named colors (\"Green\"), hex (\"#rrggbb\"), or
    comma/space-separated floats (\"0.0 0.51 0.13 1.0\")."""
    if not color_str:
        return None
    s = color_str.strip().strip('"')
    if not s:
        return None

    # Named color
    lower = s.lower()
    if lower in _KICAD_COLOR_NAMES:
        return _KICAD_COLOR_NAMES[lower]

    # Hex color  #rrggbb or #rrggbbaa
    if s.startswith("#") and len(s) in (7, 9):
        try:
            r = int(s[1:3], 16) / 255.0
            g = int(s[3:5], 16) / 255.0
            b = int(s[5:7], 16) / 255.0
            return (r, g, b)
        except ValueError:
            pass

    # Float components  "r g b" or "r g b a" (space or comma separated)
    import re
    parts = re.split(r'[,\s]+', s)
    if len(parts) >= 3:
        try:
            vals = [float(p) for p in parts[:3]]
            # If any value > 1 assume 0‑255 range
            if any(v > 1.0 for v in vals):
                vals = [v / 255.0 for v in vals]
            return tuple(vals)
        except ValueError:
            pass

    return None


def _get_board_color_from_file(filepath):
    """Parse the .kicad_pcb file directly and extract the front solder mask
    color from the stackup section.  Returns (r, g, b) 0‑1 or None."""
    import re
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Find stackup section
        stackup_match = re.search(r'\(stackup\b', content)
        if not stackup_match:
            return None

        # Find F.Mask layer inside stackup
        # Use [\s\S]*? to skip nested parens like (type "...")
        mask_pattern = re.compile(
            r'\(layer\s+"F\.Mask"[\s\S]*?\(color\s+"([^"]+)"\)',
        )
        m = mask_pattern.search(content, stackup_match.start())
        if m:
            color = _parse_color_string(m.group(1))
            if color:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: Board color from file F.Mask: {m.group(1)} → {color}\n"
                )
                return color

        # Fallback: any layer with a color in the stackup
        any_color = re.compile(
            r'\(layer\s+"[^"]*"[\s\S]*?\(color\s+"([^"]+)"\)',
        )
        for cm in any_color.finditer(content, stackup_match.start()):
            color = _parse_color_string(cm.group(1))
            if color:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: Board color from file (fallback): {cm.group(1)} → {color}\n"
                )
                return color
    except Exception as ex:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Could not parse board color from file: {ex}\n"
        )
    return None


def _get_board_color(board, filepath):
    """Read solder mask color from the board.

    Resolution order (mirrors the KiCad 3D viewer when
    ``use_stackup_colors`` is enabled, which is the default):
      1. kipy API – stackup layer F.Mask colour (r,g,b floats)
      2. .kicad_pcb file – stackup F.Mask named colour string
      3. KiCad default solder mask green

    Returns (r, g, b) floats 0‑1."""
    # 1. Try kipy API
    try:
        from kipy.proto.board.board_types_pb2 import BoardLayer
        stackup = board.get_stackup()
        for layer in stackup.layers:
            if layer.layer == BoardLayer.BL_F_Mask:
                c = layer.color
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: Stackup F.Mask color from API: "
                    f"r={c.red} g={c.green} b={c.blue} a={c.alpha}\n"
                )
                # Normalize – if values look like 0‑255 range, scale down
                r, g, b = c.red, c.green, c.blue
                if any(v > 1.0 for v in (r, g, b)):
                    r, g, b = r / 255.0, g / 255.0, b / 255.0
                if r > 0 or g > 0 or b > 0:
                    return (r, g, b)
    except Exception as ex:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Could not read board color from API: {ex}\n"
        )

    # 2. Fallback: parse the .kicad_pcb file directly
    if filepath:
        color = _get_board_color_from_file(filepath)
        if color:
            return color

    # 3. Ultimate fallback: KiCad default solder mask green
    FreeCAD.Console.PrintMessage(
        f"FreekiCAD: Using default solder mask color {_DEFAULT_SOLDER_MASK_COLOR}\n"
    )
    return _DEFAULT_SOLDER_MASK_COLOR


def load_board(filepath, socket_path):
    """Connect to a running KiCad instance via kipy and build the board
    solid + footprint metadata.
    Returns (board_shape, footprints_data, color, outline_edges, thickness)
    where footprints_data is a list of dicts with ref/position/models info,
    color is (r,g,b) or None, outline_edges is a list of sorted Part edges,
    and thickness is the board thickness in mm."""
    try:
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Loading board {filepath}\n")

        from kipy.kicad import KiCad
        from kipy.proto.board.board_types_pb2 import BoardLayer
        from kipy.board_types import (
            BoardSegment, BoardCircle, BoardArc, BoardRectangle,
            BoardPolygon, to_concrete_board_shape,
        )

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Connecting to KiCad at {socket_path}\n")
        kicad = KiCad(socket_path=f"ipc://{socket_path}")
        board = _kipy_retry(kicad.get_board)

        # Load KiCad path variables
        kicad_vars = _load_kicad_env_vars(kicad)
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: KiCad path variables: {kicad_vars}\n"
        )

        # --- Board outline ---
        edges = []
        all_shapes = board.get_shapes()
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Total board shapes: {len(all_shapes)}\n"
        )

        for s in all_shapes:
            if s.layer != BoardLayer.BL_Edge_Cuts:
                continue
            try:
                concrete = to_concrete_board_shape(s)
                if concrete is None:
                    continue
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   Edge.Cuts: {type(concrete).__name__}\n"
                )
                if isinstance(concrete, BoardSegment):
                    p1 = _vec(concrete.start.x, concrete.start.y)
                    p2 = _vec(concrete.end.x, concrete.end.y)
                    edges.append(Part.makeLine(p1, p2))
                elif isinstance(concrete, BoardArc):
                    p1 = _vec(concrete.start.x, concrete.start.y)
                    pm = _vec(concrete.mid.x, concrete.mid.y)
                    p2 = _vec(concrete.end.x, concrete.end.y)
                    edges.append(Part.Arc(p1, pm, p2).toShape())
                elif isinstance(concrete, BoardCircle):
                    center = _vec(concrete.center.x, concrete.center.y)
                    dx = concrete.end.x - concrete.center.x
                    dy = concrete.end.y - concrete.center.y
                    radius = math.hypot(dx, dy) / 1e6
                    edges.append(Part.makeCircle(radius, center))
                elif isinstance(concrete, BoardRectangle):
                    p1 = _vec(concrete.top_left.x, concrete.top_left.y)
                    p2 = _vec(concrete.bottom_right.x,
                              concrete.bottom_right.y)
                    x0, x1 = min(p1.x, p2.x), max(p1.x, p2.x)
                    y0, y1 = min(p1.y, p2.y), max(p1.y, p2.y)
                    c1 = FreeCAD.Vector(x0, y0, 0)
                    c2 = FreeCAD.Vector(x1, y0, 0)
                    c3 = FreeCAD.Vector(x1, y1, 0)
                    c4 = FreeCAD.Vector(x0, y1, 0)
                    edges.append(Part.makeLine(c1, c2))
                    edges.append(Part.makeLine(c2, c3))
                    edges.append(Part.makeLine(c3, c4))
                    edges.append(Part.makeLine(c4, c1))
                elif isinstance(concrete, BoardPolygon):
                    for pwh in concrete.polygons:
                        edges.extend(
                            _polyline_to_edges(pwh.outline))
                        for hole in pwh.holes:
                            edges.extend(
                                _polyline_to_edges(hole))
                else:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD:   Unhandled Edge.Cuts shape: "
                        f"{type(concrete).__name__}\n")
            except Exception as ex:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD:   shape exception: {ex}\n"
                )
                continue

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Edge.Cuts edges collected: {len(edges)}\n"
        )

        # Get board thickness from stackup
        thickness = DEFAULT_PCB_THICKNESS
        try:
            stackup = board.get_stackup()
            total_nm = sum(layer.thickness for layer in stackup.layers)
            if total_nm > 0:
                thickness = total_nm / 1e6
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: Board thickness from stackup: {thickness}mm\n"
                )
        except Exception as ex:
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: Could not read stackup, using default {DEFAULT_PCB_THICKNESS}mm: {ex}\n"
            )

        board_solid = None
        outline_edges = []
        if edges:
            sorted_groups = Part.sortEdges(edges)
            outline_edges = sorted_groups[0]
            wires = [Part.Wire(g) for g in sorted_groups]
            if len(wires) > 1:
                face = Part.Face(wires, "Part::FaceMakerBullseye")
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: Board outline has {len(wires) - 1} "
                    "hole(s)\n")
            else:
                face = Part.Face(wires[0])
            board_solid = face.extrude(FreeCAD.Vector(0, 0, thickness))

            # --- Drill holes ---
            drill_holes = []

            # Vias
            try:
                vias = board.get_vias()
                for via in vias:
                    vx = via.position.x / 1e6
                    vy = -via.position.y / 1e6
                    try:
                        d = via.drill_diameter / 1e6
                    except Exception:
                        d = via.padstack.drill.diameter / 1e6
                    if d > 0:
                        drill_holes.append((vx, vy, d / 2.0))
            except Exception as ex:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: Could not read vias: {ex}\n"
                )

            # Through-hole pads
            try:
                pads = board.get_pads()
                for pad in pads:
                    pt = str(pad.pad_type) if hasattr(pad, 'pad_type') else ""
                    if 'SMD' in pt.upper():
                        continue
                    try:
                        drill = pad.padstack.drill
                        # diameter may be a scalar or vector
                        try:
                            d = drill.diameter / 1e6
                        except TypeError:
                            d = drill.diameter.x / 1e6
                        if d <= 0:
                            continue
                        px = pad.position.x / 1e6
                        py = -pad.position.y / 1e6
                        drill_holes.append((px, py, d / 2.0))
                    except Exception:
                        continue
            except Exception as ex:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: Could not read pads: {ex}\n"
                )

            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Drill holes: {len(drill_holes)}\n"
            )

            if drill_holes and board_solid:
                # Build all drill cylinders and cut from board
                margin = 0.1  # extra height to ensure clean cut
                drill_shapes = []
                for hx, hy, radius in drill_holes:
                    cyl = Part.makeCylinder(
                        radius,
                        thickness + 2 * margin,
                        FreeCAD.Vector(hx, hy, -margin),
                        FreeCAD.Vector(0, 0, 1),
                    )
                    drill_shapes.append(cyl)
                if drill_shapes:
                    try:
                        drill_compound = drill_shapes[0]
                        for ds in drill_shapes[1:]:
                            drill_compound = drill_compound.fuse(ds)
                        board_solid = board_solid.cut(drill_compound)
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD: Cut {len(drill_holes)} drill holes\n"
                        )
                    except Exception as ex:
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD: Boolean cut failed: {ex}\n"
                        )

        # --- Board color ---
        board_color = _get_board_color(board, filepath)

        # --- Footprint metadata (no STEP loading) ---
        footprints_data = []
        footprints = board.get_footprints()
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Total footprints: {len(footprints)}\n"
        )

        for fp in footprints:
            try:
                ref = fp.reference_field.text.value if fp.reference_field else "?"
            except Exception:
                ref = "?"

            try:
                pos = fp.position
                fp_x = pos.x / 1e6
                fp_y = -pos.y / 1e6

                # Footprint orientation (kipy returns Angle)
                try:
                    fp_angle = float(fp.orientation.degrees)
                except Exception:
                    fp_angle = 0.0

                is_back = (fp.layer == BoardLayer.BL_B_Cu)

                try:
                    models = fp.definition.models
                except Exception as ex:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD:   {ref}: could not access models: {ex}\n"
                    )
                    continue

                models_info = []
                for model in models:
                    try:
                        if not model.visible:
                            continue
                        model_path = _resolve_model_path(
                            model.filename, board, kicad_vars)
                        if model_path is None:
                            FreeCAD.Console.PrintWarning(
                                f"FreekiCAD:   {ref}: 3D model not found: "
                                f"{model.filename}\n"
                            )
                            continue
                        models_info.append({
                            'path': model_path,
                            'offset': (model.offset.x, model.offset.y,
                                       model.offset.z),
                            'rotation': (model.rotation.x, model.rotation.y,
                                         model.rotation.z),
                            'scale': (model.scale.x, model.scale.y,
                                      model.scale.z),
                        })
                    except Exception as ex:
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD:   {ref}: model error: {ex}\n"
                        )
                        continue

                if models_info:
                    footprints_data.append({
                        'ref': ref,
                        'x': fp_x,
                        'y': fp_y,
                        'angle': fp_angle,
                        'is_back': is_back,
                        'models': models_info,
                    })

            except Exception as ex:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD:   {ref}: footprint error: {ex}\n"
                )

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Found {len(footprints_data)} footprints with 3D models\n"
        )
        return board_solid, footprints_data, board_color, outline_edges, thickness

    except Exception as e:
        import traceback
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Could not load board via kipy: {e}\n"
        )
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Traceback:\n{traceback.format_exc()}\n"
        )
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: KiCad API socket was: {socket_path}\n"
        )
        from FreekiCAD.workspace_bus import report_error
        report_error(socket_path, e)
    return None, [], None, [], DEFAULT_PCB_THICKNESS


def _fit_view(obj):
    """Fit the 3D viewport to show the given object."""
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
        FreeCADGui.SendMsgToActiveView("ViewFit")
    except Exception:
        pass


_sketch_observer = None


class _OutlineSketchObserver:
    """Document observer that detects when an outline sketch is modified
    and constrains component Placement to X/Y movement + Z rotation only."""

    def __init__(self):
        self._suppressed = set()
        self._constraining = False  # re-entrancy guard
        self._move_timers = {}  # obj.Name → QTimer for debounce

    def suppress(self, name):
        self._suppressed.add(name)

    def unsuppress(self, name):
        self._suppressed.discard(name)

    def _find_linked_parent(self, obj):
        """Find the LinkedObject parent of an outline sketch."""
        if not hasattr(obj, 'TypeId') or obj.TypeId != "Sketcher::SketchObject":
            return None
        for parent in obj.InList:
            proxy = getattr(parent, "Proxy", None)
            if proxy and hasattr(proxy, '_on_outline_changed'):
                if obj.Name == parent.Name + "_Outline":
                    return parent
        return None

    def _find_component_parent(self, obj):
        """Find the LinkedObject parent of a component Part::Feature."""
        if not hasattr(obj, 'TypeId') or obj.TypeId != "Part::Feature":
            return None
        for parent in obj.InList:
            proxy = getattr(parent, "Proxy", None)
            if proxy and getattr(proxy, 'Type', None) == 'LinkedObject':
                # Skip board shape and outline sketch
                if (obj.Name.endswith("_Board")
                        or obj.Name.endswith("_Outline")):
                    return None
                return parent
        return None

    def slotInEdit(self, vobj):
        """Called when an object enters edit mode (sketch editor opened).
        Note: Gui observer passes the ViewProvider, not the App object."""
        obj = vobj.Object
        if getattr(obj.Document, 'Restoring', False):
            return
        parent = self._find_linked_parent(obj)
        if parent and hasattr(parent, "Proxy"):
            parent.Proxy._on_outline_edit_start(parent)

    def slotChangedObject(self, obj, prop):
        try:
            doc = obj.Document
        except Exception:
            return
        if getattr(doc, 'Restoring', False):
            return

        # Constrain component Placement: only X/Y move + Z rotation
        if prop == "Placement" and not self._constraining:
            parent = self._find_component_parent(obj)
            if parent is not None:
                self._constrain_placement(obj)
                self._schedule_move_component(obj, parent)
                return
            elif hasattr(obj, 'TypeId') and obj.TypeId == "Part::Feature":
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: Placement changed on '{obj.Name}' "
                    f"(Label='{obj.Label}') but no parent found "
                    f"(InList={[p.Name for p in obj.InList]})\n")

        if prop not in ("Shape", "Geometry"):
            return
        if not hasattr(obj, 'TypeId') or obj.TypeId != "Sketcher::SketchObject":
            return
        if not obj.Name.endswith("_Outline"):
            return
        if obj.Name in self._suppressed:
            return
        parent = self._find_linked_parent(obj)
        if parent:
            proxy = parent.Proxy
            # Ensure KiCad connection if slotInEdit didn't fire (Windows)
            if getattr(proxy, '_cached_socket_path', None) is None:
                proxy._on_outline_edit_start(parent)
            proxy._on_outline_changed(parent)

    def _constrain_placement(self, obj):
        """Constrain component Placement: allow X/Y move + Z rotation only.
        Z position, pitch, and roll are locked to the initial placement."""
        init_p = getattr(obj, 'FreekiCAD_InitPlacement', None)
        if init_p is None:
            return

        p = obj.Placement
        pos = p.Base
        rot = p.Rotation

        init_z = init_p.Base.z
        init_yaw, init_pitch, init_roll = init_p.Rotation.getYawPitchRoll()

        # Extract current yaw (Z rotation) — this is the only free rotation
        yaw, pitch, roll = rot.getYawPitchRoll()
        needs_fix = False

        if abs(pos.z - init_z) > 1e-6:
            needs_fix = True
        if abs(pitch - init_pitch) > 1e-6 or abs(roll - init_roll) > 1e-6:
            needs_fix = True

        if needs_fix:
            self._constraining = True
            try:
                obj.Placement = FreeCAD.Placement(
                    FreeCAD.Vector(pos.x, pos.y, init_z),
                    FreeCAD.Rotation(yaw, init_pitch, init_roll))
            finally:
                self._constraining = False

    def _schedule_move_component(self, obj, parent):
        """Debounce move-component: schedule a KiCad push after 200ms.
        Cancels any pending timer for the same component so only the
        final position during a drag is sent."""
        name = obj.Name
        if name in self._move_timers:
            self._move_timers[name].stop()

        # Extract designator from component label: parentName_REF
        # Use Label (which we explicitly set) rather than Name
        # (which FreeCAD may auto-rename to avoid conflicts).
        label = obj.Label
        prefix = parent.Name + "_"
        if not label.startswith(prefix):
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: Cannot extract ref from '{label}' "
                f"(expected prefix '{prefix}')\n")
            return
        ref = label[len(prefix):]
        if not ref:
            return

        from PySide import QtCore
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda: self._send_move_component(obj, parent, ref))
        self._move_timers[name] = timer
        timer.start(200)

    def _send_move_component(self, obj, parent, ref):
        """Send move-component request to workspace bus."""
        self._move_timers.pop(obj.Name, None)
        try:
            if not hasattr(obj, 'FreekiCAD_KiCadX'):
                return
            init_p = getattr(obj, 'FreekiCAD_InitPlacement', None)
            if init_p is None:
                return

            p = obj.Placement
            # Compute delta from initial FreeCAD placement
            delta_x = p.Base.x - init_p.Base.x
            delta_y = p.Base.y - init_p.Base.y
            yaw, _, _ = p.Rotation.getYawPitchRoll()
            init_yaw, _, _ = init_p.Rotation.getYawPitchRoll()
            delta_yaw = yaw - init_yaw

            # Apply delta to original KiCad coordinates
            # FreeCAD coords: x_mm, y_mm (Y already negated from KiCad)
            # KiCad API via kipy Vector2.from_xy_mm takes mm with
            # KiCad sign convention (Y negated back)
            new_kicad_x = obj.FreekiCAD_KiCadX + delta_x
            new_kicad_y = obj.FreekiCAD_KiCadY + delta_y
            new_kicad_angle = obj.FreekiCAD_KiCadAngle + delta_yaw

            from FreekiCAD.workspace_bus import send_request
            send_request("move-component", parent.FileName,
                         object_label=parent.Label, component=ref)
            # Stash computed coordinates on the proxy for the response
            # handler to use.
            parent.Proxy._pending_move = {
                'ref': ref,
                'x': new_kicad_x,
                'y': new_kicad_y,
                'angle': new_kicad_angle,
            }
        except Exception as e:
            FreeCAD.Console.PrintError(
                f"FreekiCAD: _send_move_component error: {e}\n")

    def slotResetEdit(self, vobj):
        """Called when an object exits edit mode (sketch/transform closed).
        Note: Gui observer passes the ViewProvider, not the App object."""
        obj = vobj.Object
        if getattr(obj.Document, 'Restoring', False):
            return
        parent = self._find_linked_parent(obj)
        if parent:
            parent.Proxy._on_outline_edit_done(parent)
            return
        # Component transform tool closed — trigger KiCad save
        parent = self._find_component_parent(obj)
        if parent is not None:
            from PySide import QtCore
            QtCore.QTimer.singleShot(
                0, lambda: self._deferred_component_save(parent))

    def _deferred_component_save(self, parent):
        """Save KiCad board after component transform tool is closed."""
        proxy = parent.Proxy
        try:
            board = proxy._get_kicad_board(parent)
            if board is None:
                return
            board.save()
            FreeCAD.Console.PrintMessage(
                "FreekiCAD: Board file saved after component move\n")
        except Exception as e:
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: Failed to save board after move: {e}\n")


def _find_obj_by_label(label):
    """Find a LinkedObject FreeCAD object by its Label across all documents."""
    for doc in FreeCAD.listDocuments().values():
        for obj in doc.Objects:
            if obj.Label == label:
                proxy = getattr(obj, 'Proxy', None)
                if proxy and getattr(proxy, 'Type', None) == 'LinkedObject':
                    return obj
    return None


def _handle_bus_response(reply):
    """Global response handler for workspace bus messages.
    Dispatches to the appropriate LinkedObject method based on
    action/object/component."""
    action = reply.get("action")
    obj_label = reply.get("object", "")
    socket_path = reply.get("socket")
    component = reply.get("component", "")

    if not socket_path:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Bus response has no socket: {reply}\n")
        return

    obj = _find_obj_by_label(obj_label)
    if obj is None:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Bus response for unknown object '{obj_label}'\n")
        return

    proxy = obj.Proxy

    if action == "reload":
        proxy._handle_reload_response(obj, socket_path)
    elif action == "open-sketch":
        proxy._handle_open_sketch_response(obj, socket_path)
    elif action == "move-component":
        proxy._handle_move_component_response(obj, socket_path, component)
    else:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Unknown bus action '{action}'\n")


def _ensure_sketch_observer():
    global _sketch_observer
    if _sketch_observer is None:
        _sketch_observer = _OutlineSketchObserver()
        # App observer for slotChangedObject (geometry changes)
        FreeCAD.addDocumentObserver(_sketch_observer)
        # Gui observer for slotInEdit / slotResetEdit (edit mode)
        import FreeCADGui
        FreeCADGui.addDocumentObserver(_sketch_observer)
        # Register the global workspace bus response handler
        from FreekiCAD.workspace_bus import set_response_handler
        set_response_handler(_handle_bus_response)
    return _sketch_observer


class LinkedObject:
    """A Part object with group extension that maps an external .kicad_pcb
    file to FreeCAD objects via kipy (KiCad IPC API).

    Uses Part::FeaturePython + App::GroupExtensionPython so the object
    has its own Shape and can hold component children in the tree view.
    """


    def __init__(self, obj):
        obj.addExtension("App::GeoFeatureGroupExtensionPython")
        obj.addProperty(
            "App::PropertyFile", "FileName", "LinkedFile",
            "Path to the .kicad_pcb file"
        )
        obj.addProperty(
            "App::PropertyBool", "AutoReload", "LinkedFile",
            "Automatically reload when the file changes"
        )
        obj.AutoReload = True
        obj.addProperty(
            "App::PropertyString", "ComponentMtimes", "LinkedFile",
            "JSON: per-component model file mtimes for reuse"
        )
        obj.setPropertyStatus("ComponentMtimes", "Hidden")
        obj.addProperty(
            "App::PropertyString", "FileMtime", "LinkedFile",
            "Stored mtime of the linked .kicad_pcb file"
        )
        obj.setPropertyStatus("FileMtime", "Hidden")
        obj.Proxy = self
        self.Type = "LinkedObject"
        self._board_color = None

    def onChanged(self, obj, prop):
        if prop not in ("FileName", "AutoReload"):
            return
        if prop == "FileName":
            # Skip during document restore — shapes are already saved
            if obj.Document.Restoring:
                return
            if obj.FileName:
                obj.Label = os.path.splitext(os.path.basename(obj.FileName))[0]
            self._suppress_execute = True
            if hasattr(obj, 'FileMtime'):
                obj.FileMtime = ""
            self._remove_children(obj)
            self._suppress_execute = False
            # mtime watcher (always running) will detect the
            # cleared FileMtime and trigger reload

    def _remove_children(self, obj):
        """Remove all child objects from this group."""
        doc = obj.Document
        children = list(obj.Group)
        if children:
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Removing {len(children)} children from '{obj.Name}'\n"
            )
        for child in children:
            try:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: Removing child '{child.Name}'\n"
                )
                doc.removeObject(child.Name)
            except (ReferenceError, Exception) as e:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: Failed to remove child: {e}\n"
                )

    def _remove_board_children(self, obj):
        """Remove outline sketch and board shape children, keep components.
        Returns dict of {ref: child_obj} for existing component objects,
        where ref is the designator with the parent name prefix stripped."""
        doc = obj.Document
        prefix = obj.Name + "_"
        existing_components = {}
        for child in list(obj.Group):
            if child.Name.endswith("_Outline") or child.Name.endswith("_Board"):
                try:
                    doc.removeObject(child.Name)
                except (ReferenceError, Exception) as e:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD: Failed to remove child: {e}\n"
                    )
            else:
                # Strip parent name prefix to get the designator ref
                label = child.Label
                if label.startswith(prefix):
                    ref = label[len(prefix):]
                else:
                    ref = label
                existing_components[ref] = child
        if existing_components:
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Existing components: "
                f"{', '.join(existing_components.keys())}\n")
        return existing_components

    def execute(self, obj):
        """Called by FreeCAD recompute.  Only ensures properties exist.
        Actual KiCad loading is done by reload()."""
        self._ensure_properties(obj)

    def _do_execute(self, obj, socket_path, existing_components=None):
        """Internal execute implementation.
        NOTE: board/outline children must be removed BEFORE this method,
        outside of FreeCAD's recompute cycle.
        *socket_path*: resolved KiCad IPC socket path.
        *existing_components*: optional dict {label: child_obj} of component
        Part::Feature objects to reuse by designator match."""
        import json

        board_solid, footprints_data, board_color, outline_edges, thickness = \
            load_board(obj.FileName, socket_path)

        # Freeze the main window to prevent viewport flashing
        # as children are added one by one.
        _mw = None
        try:
            import FreeCADGui
            _mw = FreeCADGui.getMainWindow()
            _mw.setUpdatesEnabled(False)
        except Exception:
            _mw = None

        try:
            self.__do_execute_body(obj, board_solid, footprints_data,
                                   board_color, outline_edges, thickness,
                                   existing_components)
        finally:
            if _mw is not None:
                _mw.setUpdatesEnabled(True)

    def __do_execute_body(self, obj, board_solid, footprints_data,
                          board_color, outline_edges, thickness,
                          existing_components):
        import json
        doc = obj.Document

        self._board_color = board_color

        # Record file modification time
        try:
            mt = os.path.getmtime(obj.FileName)
            if hasattr(obj, 'FileMtime'):
                obj.FileMtime = str(mt)
        except OSError:
            if hasattr(obj, 'FileMtime'):
                obj.FileMtime = ""

        # Add board outline sketch as a child
        if outline_edges:
            sketch = doc.addObject("Sketcher::SketchObject",
                                   obj.Name + "_Outline")
            obj.addObject(sketch)
            self._build_outline_sketch(sketch, outline_edges)

        # Add board shape as a child
        if board_solid:
            board_obj = doc.addObject("Part::Feature", obj.Name + "_Board")
            board_obj.Shape = board_solid
            if board_color:
                try:
                    board_obj.ViewObject.ShapeColor = board_color
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: Applied board color {board_color}\n"
                    )
                except Exception:
                    pass
            obj.addObject(board_obj)

        # Load component 3D models on demand, reusing where possible
        if existing_components is None:
            existing_components = {}
        stored_mtimes = {}
        if existing_components and hasattr(obj, 'ComponentMtimes') \
                and obj.ComponentMtimes:
            try:
                stored_mtimes = json.loads(obj.ComponentMtimes)
            except Exception:
                pass

        step_cache = {}
        all_mtimes = {}
        matched = set()
        components = []
        # Map ref → (kicad_x_mm, kicad_y_mm, kicad_angle_deg)
        # for storing original KiCad coordinates on component objects.
        kicad_coords = {}

        for fp_info in footprints_data:
            ref = fp_info['ref']
            kicad_coords[ref] = (fp_info['x'], fp_info['y'],
                                 fp_info['angle'])

            # Check if this component can be reused from existing objects
            in_existing = ref in existing_components
            in_stored = ref in stored_mtimes
            if in_existing and ref not in matched and in_stored:
                fp_paths = {os.path.realpath(m['path'])
                            for m in fp_info['models']}
                stored = stored_mtimes[ref]
                if set(stored.keys()) == fp_paths:
                    can_reuse = True
                    changed_file = None
                    for path, old_mt in stored.items():
                        try:
                            cur_mt = os.path.getmtime(path)
                        except OSError:
                            can_reuse = False
                            changed_file = path
                            break
                        if cur_mt != old_mt:
                            can_reuse = False
                            changed_file = path
                            break
                    if can_reuse:
                        matched.add(ref)
                        all_mtimes[ref] = stored_mtimes[ref]
                        # Ensure prefixed name for migration
                        expected = obj.Name + "_" + ref
                        child = existing_components[ref]
                        if child.Label != expected:
                            child.Label = expected
                        # Update placement from new KiCad position
                        kc = kicad_coords.get(ref)
                        if kc is not None:
                            self._update_reused_component(
                                child, kc, thickness, fp_info)
                        for m in fp_info['models']:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD:   {ref}: reused "
                                f"{os.path.basename(m['path'])}\n")
                        continue
                    else:
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD:   {ref}: mtime changed "
                            f"({os.path.basename(changed_file)}), "
                            f"reloading\n")
                else:
                    added = fp_paths - set(stored.keys())
                    removed = set(stored.keys()) - fp_paths
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   {ref}: model paths changed"
                        f"{' +' + ','.join(os.path.basename(p) for p in added) if added else ''}"
                        f"{' -' + ','.join(os.path.basename(p) for p in removed) if removed else ''}"
                        f", reloading\n")
            elif not in_existing:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   {ref}: new component\n")
            elif not in_stored:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   {ref}: no stored mtimes\n")

            # Load models on demand
            fp_components, fp_mtimes = _load_footprint_models(
                fp_info, thickness, doc, step_cache=step_cache)
            components.extend(fp_components)
            if ref not in all_mtimes:
                all_mtimes[ref] = {}
            all_mtimes[ref].update(fp_mtimes)

        # Create/update component FreeCAD objects
        for label, comp_shape, comp_colors, comp_is_back in components:
            if label in existing_components and label not in matched:
                comp_obj = existing_components[label]
                comp_obj.Shape = comp_shape
                # Ensure prefixed name for migration
                expected = obj.Name + "_" + label
                if comp_obj.Label != expected:
                    comp_obj.Label = expected
                matched.add(label)
            else:
                comp_obj = doc.addObject(
                    "Part::Feature", obj.Name + "_" + label)
                comp_obj.Shape = comp_shape
                obj.addObject(comp_obj)
            # Store initial placement and board side for constraint
            if not hasattr(comp_obj, 'FreekiCAD_InitPlacement'):
                comp_obj.addProperty(
                    "App::PropertyPlacement", "FreekiCAD_InitPlacement",
                    "FreekiCAD", "Initial placement for constraint")
                try:
                    comp_obj.setPropertyStatus(
                        "FreekiCAD_InitPlacement", "Hidden")
                except Exception:
                    pass
                # First load: use full placement as init
                comp_obj.FreekiCAD_InitPlacement = comp_obj.Placement
            else:
                # Reload: only update the constrained axes (Z, pitch, roll);
                # preserve the user's X/Y and yaw.
                cur = comp_obj.Placement
                old_init = comp_obj.FreekiCAD_InitPlacement
                yaw_old, _, _ = old_init.Rotation.getYawPitchRoll()
                _, pitch_new, roll_new = cur.Rotation.getYawPitchRoll()
                comp_obj.FreekiCAD_InitPlacement = FreeCAD.Placement(
                    FreeCAD.Vector(old_init.Base.x, old_init.Base.y, cur.Base.z),
                    FreeCAD.Rotation(yaw_old, pitch_new, roll_new))
            if not hasattr(comp_obj, 'FreekiCAD_BackSide'):
                comp_obj.addProperty(
                    "App::PropertyBool", "FreekiCAD_BackSide",
                    "FreekiCAD", "Component is on back side of board")
                try:
                    comp_obj.setPropertyStatus(
                        "FreekiCAD_BackSide", "Hidden")
                except Exception:
                    pass
            comp_obj.FreekiCAD_BackSide = comp_is_back
            # Store original KiCad coordinates for move-component
            kc = kicad_coords.get(label)
            if kc is not None:
                for pname in ('FreekiCAD_KiCadX', 'FreekiCAD_KiCadY',
                              'FreekiCAD_KiCadAngle'):
                    if not hasattr(comp_obj, pname):
                        comp_obj.addProperty(
                            "App::PropertyFloat", pname,
                            "FreekiCAD",
                            "Original KiCad coordinate")
                        try:
                            comp_obj.setPropertyStatus(pname, "Hidden")
                        except Exception:
                            pass
                comp_obj.FreekiCAD_KiCadX = kc[0]
                comp_obj.FreekiCAD_KiCadY = kc[1]
                comp_obj.FreekiCAD_KiCadAngle = kc[2]
            if comp_colors and hasattr(comp_obj, 'ViewObject') \
                    and comp_obj.ViewObject:
                _write_face_colors(comp_obj.ViewObject, comp_colors)

        # Remove unmatched old components
        for label, child in existing_components.items():
            if label not in matched:
                try:
                    doc.removeObject(child.Name)
                except (ReferenceError, Exception) as e:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD: Failed to remove old component "
                        f"'{label}': {e}\n"
                    )

        # Persist model mtimes
        if hasattr(obj, 'ComponentMtimes'):
            obj.ComponentMtimes = json.dumps(all_mtimes)

    def _update_reused_component(self, comp_obj, kc, thickness, fp_info):
        """Update placement and KiCad coords for a reused component
        whose 3D model hasn't changed but whose KiCad position may have.
        *kc* is (new_kicad_x_mm, new_kicad_y_mm, new_kicad_angle_deg)
        in FreeCAD coordinates (Y already negated)."""
        if not hasattr(comp_obj, 'FreekiCAD_KiCadX'):
            return
        old_x = comp_obj.FreekiCAD_KiCadX
        old_y = comp_obj.FreekiCAD_KiCadY
        old_angle = comp_obj.FreekiCAD_KiCadAngle
        new_x, new_y, new_angle = kc

        dx = new_x - old_x
        dy = new_y - old_y
        da = new_angle - old_angle

        if abs(dx) < 1e-6 and abs(dy) < 1e-6 and abs(da) < 1e-4:
            return  # No change

        # The shape is baked at the old position. Apply a Placement
        # delta so the component appears at the new position.
        # Rotation delta around Z at the old footprint position,
        # then translate by (dx, dy).
        p = comp_obj.Placement
        if abs(da) > 1e-4:
            # Rotate around the old footprint center
            rot_center = FreeCAD.Vector(old_x, old_y, 0)
            delta_rot = FreeCAD.Placement(
                FreeCAD.Vector(0, 0, 0),
                FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), da),
                rot_center)
            p = delta_rot.multiply(p)
        p.Base = FreeCAD.Vector(p.Base.x + dx, p.Base.y + dy, p.Base.z)
        comp_obj.Placement = p

        # Update stored KiCad coordinates
        comp_obj.FreekiCAD_KiCadX = new_x
        comp_obj.FreekiCAD_KiCadY = new_y
        comp_obj.FreekiCAD_KiCadAngle = new_angle

        # Update InitPlacement constrained axes
        if hasattr(comp_obj, 'FreekiCAD_InitPlacement'):
            init_p = comp_obj.FreekiCAD_InitPlacement
            is_back = getattr(comp_obj, 'FreekiCAD_BackSide', False)
            fp_z = thickness if not is_back else 0.0
            yaw_old, _, _ = init_p.Rotation.getYawPitchRoll()
            comp_obj.FreekiCAD_InitPlacement = FreeCAD.Placement(
                FreeCAD.Vector(init_p.Base.x + dx, init_p.Base.y + dy,
                               fp_z),
                FreeCAD.Rotation(yaw_old - da, 0, 0))

        # Update BackSide
        is_back = fp_info.get('is_back', False)
        if hasattr(comp_obj, 'FreekiCAD_BackSide'):
            comp_obj.FreekiCAD_BackSide = is_back

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD:   {comp_obj.Label}: updated placement "
            f"(Δx={dx:.3f}, Δy={dy:.3f}, Δangle={da:.1f}°)\n")

    def _on_outline_edit_start(self, obj):
        """Called when the outline sketch enters edit mode.
        Suppresses execute() and sends a fire-and-forget request to
        resolve the KiCad socket path."""
        self._suppress_execute = True
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Outline sketch opened for '{obj.Name}'\n")
        from FreekiCAD.workspace_bus import send_request
        send_request("open-sketch", obj.FileName,
                     object_label=obj.Label)

    def _handle_open_sketch_response(self, obj, socket_path):
        """Called when the workspace bus responds to an open-sketch request."""
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Resolved KiCad socket for '{obj.Name}': "
            f"{socket_path}\n")
        self._cached_socket_path = socket_path
        self._ensure_kicad_connection(obj)

    def _ensure_kicad_connection(self, obj):
        """Establish KiCad connection in the background with retries."""
        from kipy.kicad import KiCad
        socket_path = getattr(self, '_cached_socket_path', None)
        if socket_path is None:
            return
        try:
            kicad = KiCad(socket_path=f"ipc://{socket_path}")
            _kipy_retry(kicad.get_board)
            self._kicad = kicad
            FreeCAD.Console.PrintMessage(
                "FreekiCAD: KiCad connection ready\n")
        except Exception as e:
            import traceback
            self._kicad = None
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: Could not pre-connect to KiCad: "
                f"{type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}\n")

    def _get_kicad_board(self, obj):
        """Connect to KiCad and return the board proxy, or None."""
        from kipy.kicad import KiCad
        socket_path = getattr(self, '_cached_socket_path', None)
        if socket_path is None:
            FreeCAD.Console.PrintError(
                "FreekiCAD: Could not resolve KiCad socket\n")
            return None
        try:
            kicad = getattr(self, '_kicad', None)
            if kicad is None:
                kicad = KiCad(socket_path=f"ipc://{socket_path}")
                self._kicad = kicad
            return _kipy_retry(kicad.get_board)
        except Exception as e:
            self._kicad = None
            import traceback
            FreeCAD.Console.PrintError(
                f"FreekiCAD: Failed to connect to KiCad: "
                f"{type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}\n")
            from FreekiCAD.workspace_bus import report_error
            report_error(socket_path, e)
            return None

    def _find_outline_sketch(self, obj):
        """Find the outline sketch child, or None."""
        for child in obj.Group:
            if child.Name == obj.Name + "_Outline":
                return child
        return None

    def _on_outline_changed(self, obj):
        """Called by the sketch observer when the outline sketch is modified.
        Defers the KiCad update to avoid blocking the observer callback."""
        from PySide import QtCore
        QtCore.QTimer.singleShot(0, lambda: self._deferred_send_outline(obj))

    def _deferred_send_outline(self, obj):
        """Rebuild the Edge.Cuts layer in KiCad (runs outside observer)."""
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Outline sketch changed for '{obj.Name}', "
            "sending to KiCad...\n")
        sketch = self._find_outline_sketch(obj)
        if sketch is None:
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: No outline sketch found for '{obj.Name}'\n")
            return
        try:
            from kipy.proto.board.board_types_pb2 import BoardLayer
            from kipy.board_types import BoardSegment, BoardArc, BoardCircle
            from kipy.geometry import Vector2

            board = self._get_kicad_board(obj)
            if board is None:
                return

            commit = board.begin_commit()

            # Remove existing Edge.Cuts
            existing = board.get_shapes()
            edge_cuts = [s for s in existing
                         if s.layer == BoardLayer.BL_Edge_Cuts]
            if edge_cuts:
                board.remove_items(edge_cuts)

            # Build new Edge.Cuts from sketch geometry
            new_items = []
            for i in range(sketch.GeometryCount):
                geo = sketch.Geometry[i]
                try:
                    if isinstance(geo, Part.LineSegment):
                        seg = BoardSegment()
                        seg.start = Vector2.from_xy_mm(
                            geo.StartPoint.x, -geo.StartPoint.y)
                        seg.end = Vector2.from_xy_mm(
                            geo.EndPoint.x, -geo.EndPoint.y)
                        seg.layer = BoardLayer.BL_Edge_Cuts
                        new_items.append(seg)
                    elif isinstance(geo, Part.ArcOfCircle):
                        mid_angle = (geo.FirstParameter
                                     + geo.LastParameter) / 2
                        mid_x = (geo.Center.x
                                 + geo.Radius * math.cos(mid_angle))
                        mid_y = (geo.Center.y
                                 + geo.Radius * math.sin(mid_angle))
                        arc = BoardArc()
                        arc.start = Vector2.from_xy_mm(
                            geo.StartPoint.x, -geo.StartPoint.y)
                        arc.mid = Vector2.from_xy_mm(mid_x, -mid_y)
                        arc.end = Vector2.from_xy_mm(
                            geo.EndPoint.x, -geo.EndPoint.y)
                        arc.layer = BoardLayer.BL_Edge_Cuts
                        new_items.append(arc)
                    elif isinstance(geo, Part.Circle):
                        circle = BoardCircle()
                        circle.center = Vector2.from_xy_mm(
                            geo.Center.x, -geo.Center.y)
                        circle.radius_point = Vector2.from_xy_mm(
                            geo.Center.x + geo.Radius, -geo.Center.y)
                        circle.layer = BoardLayer.BL_Edge_Cuts
                        new_items.append(circle)
                except Exception as ex:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD: Outline geo {i} error: {ex}\n")

            if new_items:
                board.create_items(new_items)

            board.push_commit(commit,
                              "Update board outline from FreeCAD")

            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Sent {len(new_items)} outline shapes to KiCad "
                f"(removed {len(edge_cuts)} old)\n")

        except Exception as ex:
            import traceback
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: Failed to send outline to KiCad: {ex}\n")
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: {traceback.format_exc()}\n")

    def _on_outline_edit_done(self, obj):
        """Called when the outline sketch editor is closed.
        Defers save to the next event loop iteration to avoid
        modifying the document inside a document observer callback."""
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Outline sketch editor closed for '{obj.Name}', "
            "deferring save...\n")
        from PySide import QtCore
        QtCore.QTimer.singleShot(0, lambda: self._deferred_save(obj))

    def _deferred_save(self, obj):
        """Save board file via kipy (runs outside observer callback).
        The mtime watcher will trigger reload if AutoReload is enabled."""
        try:
            board = self._get_kicad_board(obj)
            if board is None:
                FreeCAD.Console.PrintWarning(
                    "FreekiCAD: _deferred_save: board is None, "
                    "cannot save\n")
                return
            board.save()
            FreeCAD.Console.PrintMessage("FreekiCAD: Board file saved\n")
            # Clear stored mtime so the auto-reload watcher detects the
            # newly saved file and triggers a reload.
            if hasattr(obj, 'FileMtime'):
                obj.FileMtime = ""
        except Exception as ex:
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: Failed to save board: {ex}\n")
        finally:
            self._kicad = None
            self._cached_socket_path = None
            self._suppress_execute = False

    def _build_outline_sketch(self, sketch, edges):
        """Populate a Sketcher::SketchObject with geometry and coincident
        constraints from the sorted outline edges."""
        import Sketcher

        obs = _ensure_sketch_observer()
        obs.suppress(sketch.Name)
        geo_indices = []
        for edge in edges:
            curve = edge.Curve
            try:
                if isinstance(curve, Part.Line) or isinstance(curve, Part.LineSegment):
                    p1 = edge.Vertexes[0].Point
                    p2 = edge.Vertexes[1].Point
                    seg = Part.LineSegment(
                        FreeCAD.Vector(p1.x, p1.y, 0),
                        FreeCAD.Vector(p2.x, p2.y, 0),
                    )
                    idx = sketch.addGeometry(seg, False)
                    geo_indices.append(idx)
                elif isinstance(curve, Part.Circle):
                    if edge.isClosed():
                        # Full circle
                        circle = Part.Circle(
                            FreeCAD.Vector(curve.Center.x, curve.Center.y, 0),
                            FreeCAD.Vector(0, 0, 1),
                            curve.Radius,
                        )
                        idx = sketch.addGeometry(circle, False)
                        geo_indices.append(idx)
                    else:
                        # Arc
                        arc = Part.ArcOfCircle(
                            Part.Circle(
                                FreeCAD.Vector(curve.Center.x, curve.Center.y, 0),
                                FreeCAD.Vector(0, 0, 1),
                                curve.Radius,
                            ),
                            edge.FirstParameter,
                            edge.LastParameter,
                        )
                        idx = sketch.addGeometry(arc, False)
                        geo_indices.append(idx)
                else:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD: Unsupported outline curve type: "
                        f"{type(curve).__name__}\n"
                    )
            except Exception as ex:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: Failed to add outline geometry: {ex}\n"
                )

        # Add coincident constraints between consecutive edges
        if len(geo_indices) >= 2:
            for i in range(len(geo_indices)):
                curr = geo_indices[i]
                nxt = geo_indices[(i + 1) % len(geo_indices)]
                try:
                    sketch.addConstraint(
                        Sketcher.Constraint("Coincident",
                                            curr, 2, nxt, 1))
                except Exception as ex:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD: Failed to add coincident constraint "
                        f"between geo {curr} and {nxt}: {ex}\n"
                    )

        obs.unsuppress(sketch.Name)

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Built outline sketch with {len(geo_indices)} "
            f"elements and {len(geo_indices)} constraints\n"
        )

    def _check_file_changed(self, obj):
        """Return True if the file's mtime has changed since last load."""
        if not obj.FileName:
            return False
        try:
            mtime = os.path.getmtime(obj.FileName)
        except OSError:
            return False
        stored = ""
        if hasattr(obj, 'FileMtime'):
            stored = obj.FileMtime
        if not stored:
            if obj.Document.Restoring:
                # During restore — record mtime, skip reload
                if hasattr(obj, 'FileMtime'):
                    obj.FileMtime = str(mtime)
                return False
            # First load — need to load
            return True
        try:
            stored_mt = float(stored)
        except (ValueError, TypeError):
            return True
        if mtime != stored_mt:
            return True
        return False

    def reload(self, obj, force=False):
        """Reload from KiCad.  Unless force=True, skips if file mtime
        hasn't changed (prevents double-scheduled reloads).
        Sends a fire-and-forget request; the actual loading happens
        when the response arrives via _handle_reload_response."""
        if getattr(self, '_reloading', False):
            return
        if not force and not self._check_file_changed(obj):
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Skipping reload of '{obj.Name}' "
                "(file unchanged)\n")
            return
        self._reloading = True
        self._ensure_properties(obj)
        from FreekiCAD.workspace_bus import send_request
        send_request("reload", obj.FileName, object_label=obj.Label)

    def _handle_reload_response(self, obj, socket_path):
        """Called when the workspace bus responds to a reload request."""
        outline_name = obj.Name + "_Outline"
        if _sketch_observer is not None:
            _sketch_observer.suppress(outline_name)
        try:
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Reloading '{obj.Name}'...\n")
            self._suppress_execute = True
            if hasattr(obj, 'FileMtime'):
                obj.FileMtime = ""
            existing = self._remove_board_children(obj)
            self._in_execute = True
            self._do_execute(obj, socket_path,
                             existing_components=existing)
        finally:
            self._in_execute = False
            self._suppress_execute = False
            if _sketch_observer is not None:
                _sketch_observer.unsuppress(outline_name)
            self._reloading = False

    def _handle_move_component_response(self, obj, socket_path, component):
        """Called when the workspace bus responds to a move-component request.
        Pushes the component's new position/angle to KiCad via kipy."""
        move = getattr(self, '_pending_move', None)
        if move is None or move.get('ref') != component:
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: No pending move for '{component}'\n")
            return
        self._pending_move = None

        try:
            from kipy.kicad import KiCad
            from kipy.geometry import Vector2, Angle

            kicad = KiCad(socket_path=f"ipc://{socket_path}")
            board = _kipy_retry(kicad.get_board)

            # Find the footprint by reference designator
            target_fp = None
            for fp in board.get_footprints():
                try:
                    ref = fp.reference_field.text.value
                except Exception:
                    continue
                if ref == component:
                    target_fp = fp
                    break

            if target_fp is None:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: Footprint '{component}' not found "
                    f"in KiCad board\n")
                return

            # Apply new position (FreeCAD mm → KiCad mm, negate Y back)
            new_x = move['x']
            new_y = -move['y']  # negate Y back to KiCad convention
            new_angle = move['angle']

            commit = board.begin_commit()
            target_fp.position = Vector2.from_xy_mm(new_x, new_y)
            target_fp.orientation = Angle.from_degrees(new_angle)
            board.update_items([target_fp])
            board.push_commit(commit,
                              f"Move {component} from FreeCAD")

            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Moved '{component}' in KiCad to "
                f"({new_x:.3f}, {new_y:.3f}) mm, "
                f"angle {new_angle:.1f}°\n")

            # Update stored mtime so auto-reload doesn't trigger a
            # redundant full reload after we just pushed this change.
            try:
                mt = os.path.getmtime(obj.FileName)
                if hasattr(obj, 'FileMtime'):
                    obj.FileMtime = str(mt)
            except OSError:
                pass

        except Exception as e:
            import traceback
            FreeCAD.Console.PrintError(
                f"FreekiCAD: Failed to move '{component}' in KiCad: "
                f"{e}\n{traceback.format_exc()}\n")

    def dumps(self):
        return {"Type": self.Type}

    def loads(self, state):
        if state:
            self.Type = state.get("Type", "LinkedObject")
        self._board_color = None

    def _ensure_properties(self, obj):
        """Add hidden properties if they don't exist yet (migration)."""
        if not hasattr(obj, 'ComponentMtimes'):
            obj.addProperty(
                "App::PropertyString", "ComponentMtimes", "LinkedFile",
                "JSON: per-component model file mtimes for reuse")
            obj.setPropertyStatus("ComponentMtimes", "Hidden")
        if not hasattr(obj, 'FileMtime'):
            obj.addProperty(
                "App::PropertyString", "FileMtime", "LinkedFile",
                "Stored mtime of the linked .kicad_pcb file")
            obj.setPropertyStatus("FileMtime", "Hidden")


class LinkedObjectViewProvider:
    """ViewProvider for LinkedObject."""

    def __init__(self, vobj):
        vobj.addExtension("Gui::ViewProviderGeoFeatureGroupExtensionPython")
        vobj.Proxy = self

    def attach(self, vobj):
        self.Object = vobj.Object
        _ensure_sketch_observer()
        from PySide import QtCore
        self._auto_reload_timer = QtCore.QTimer()
        self._auto_reload_timer.timeout.connect(lambda: self._auto_reload(vobj))
        self._auto_reload_timer.start(2000)

    def _auto_reload(self, vobj):
        """Called by the timer — reload when file changed.
        First load (FileMtime empty) always reloads; subsequent
        changes only reload if AutoReload is enabled."""
        obj = vobj.Object
        if obj.Document.Restoring:
            return
        if not obj.FileName:
            return
        if not hasattr(obj, "Proxy") or not hasattr(obj.Proxy, "_check_file_changed"):
            return
        stored = getattr(obj, "FileMtime", "") if hasattr(obj, "FileMtime") else ""
        first_load = not stored
        if not first_load and not getattr(obj, "AutoReload", False):
            return
        if obj.Proxy._check_file_changed(obj):
            obj.Proxy.reload(obj)

    def getIcon(self):
        return ":/icons/Tree_Part.svg"

    def setupContextMenu(self, vobj, menu):
        from PySide import QtGui
        action = menu.addAction("Reload KiCad PCB")
        action.triggered.connect(lambda: self._reload(vobj))

    def _reload(self, vobj):
        obj = vobj.Object
        if hasattr(obj, "Proxy") and hasattr(obj.Proxy, "reload"):
            obj.Proxy.reload(obj, force=True)

    def dumps(self):
        return None

    def loads(self, state):
        return None


def create_linked_object(filename=""):
    doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument()

    label = os.path.splitext(os.path.basename(filename))[0] if filename else "LinkedObject"
    obj = doc.addObject("Part::FeaturePython", label)
    LinkedObject(obj)
    LinkedObjectViewProvider(obj.ViewObject)

    if filename:
        obj.FileName = filename

    doc.recompute()
    return obj
