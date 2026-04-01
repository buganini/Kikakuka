import os
import math
import FreeCAD
import Part



DEFAULT_PCB_THICKNESS = 1.6  # mm fallback
GEOMETRY_TOLERANCE = 0.001  # mm (1 µm)


def _kipy_retry(func, max_retries=15, delay_s=1.0):
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


def _kicad_config_bases():
    """Return platform-specific KiCad configuration base directories."""
    bases = []
    if os.name == 'nt':
        bases.append(os.path.join(
            os.environ.get('APPDATA', ''), 'kicad'))
    else:
        bases.append(os.path.expanduser(
            '~/Library/Preferences/kicad'))
        bases.append(os.path.expanduser('~/.config/kicad'))
    return bases


def _kicad_data_bases():
    """Return platform-specific KiCad user data base directories."""
    bases = []
    if os.name == 'nt':
        bases.append(os.path.join(
            os.environ.get('USERPROFILE', ''), 'Documents', 'KiCad'))
    else:
        bases.append(os.path.expanduser('~/Documents/KiCad'))
        bases.append(os.path.expanduser('~/.local/share/kicad'))
    return bases


def _discover_kicad_versions(base_dirs):
    """Scan *base_dirs* for versioned sub-directories (e.g. '9.0', '10.0').
    Returns a sorted list of (major_int, 'X.0') tuples found on disk,
    newest first."""
    import re
    found = set()
    for base in base_dirs:
        if not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                m = re.match(r'^(\d+)\.0$', name)
                if m and os.path.isdir(os.path.join(base, name)):
                    found.add((int(m.group(1)), name))
        except OSError:
            pass
    return sorted(found, reverse=True)


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

        # 3D models — set KICAD<V>_3DMODEL_DIR for every version
        # whose config directory exists, plus the running version.
        model_dir = None
        for d in [os.path.join(parent, 'SharedSupport', '3dmodels'),
                  os.path.join(parent, 'share', 'kicad', '3dmodels')]:
            if os.path.isdir(d):
                model_dir = d
                break
        if model_dir:
            # Discover installed config versions so we cover all of them
            config_bases = _kicad_config_bases()
            versions = _discover_kicad_versions(config_bases)
            # Always include a reasonable range in case no config dirs
            # exist yet (fresh install).
            major_set = {v for v, _ in versions} | {6, 7, 8, 9}
            for v in major_set:
                env[f'KICAD{v}_3DMODEL_DIR'] = model_dir
    except Exception:
        pass

    # 2. Read user-defined variables from kicad_common.json
    try:
        config_bases = _kicad_config_bases()
        versions = _discover_kicad_versions(config_bases)
        for _major, ver_dir in versions:
            for base in config_bases:
                cfg = os.path.join(base, ver_dir, 'kicad_common.json')
                if os.path.isfile(cfg):
                    with open(cfg, 'r') as f:
                        data = json.load(f)
                    user_vars = (data.get('environment', {})
                                 or {}).get('vars', {})
                    if user_vars:
                        env.update(user_vars)
    except Exception:
        pass

    # 3. Derive KICAD*_3RD_PARTY from the user data directory
    #    (set by PCM / Plugin Content Manager).
    try:
        data_bases = _kicad_data_bases()
        versions = _discover_kicad_versions(data_bases)
        thirdparty_path = None
        for _major, ver_dir in versions:
            for base in data_bases:
                candidate = os.path.join(base, ver_dir, '3rdparty')
                if os.path.isdir(candidate):
                    thirdparty_path = candidate
                    break
            if thirdparty_path:
                break

        if thirdparty_path:
            majors = {v for v, _ in versions} | {6, 7, 8, 9}
            for v in sorted(majors, reverse=True):
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
    Returns (board_shape, footprints_data, color, outline_edges, thickness,
    bend_lines) where footprints_data is a list of dicts with ref/position/
    models info, color is (r,g,b) or None, outline_edges is a list of sorted
    Part edges, thickness is the board thickness in mm, and bend_lines is a
    list of dicts with uuid/start/end for each valid line on the User.4
    layer."""
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

        # --- Bend lines (User.4 layer) ---
        bend_lines = []
        for s in all_shapes:
            if s.layer != BoardLayer.BL_User_4:
                continue
            try:
                concrete = to_concrete_board_shape(s)
                if concrete is None:
                    continue
                if isinstance(concrete, BoardSegment):
                    bend_lines.append({
                        'uuid': s.id.value,
                        'start': _vec(concrete.start.x, concrete.start.y),
                        'end': _vec(concrete.end.x, concrete.end.y),
                    })
            except Exception:
                continue
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: User.4 bend lines: {len(bend_lines)}\n")

        # --- Parse text on User.4 for bend parameters ---
        if bend_lines:
            try:
                from kipy.board_types import BoardText as KiPyBoardText
                import re
                all_text = board.get_text()
                u4_text_count = 0
                for t in all_text:
                    if not isinstance(t, KiPyBoardText):
                        continue
                    if t.layer != BoardLayer.BL_User_4:
                        continue
                    u4_text_count += 1
                    text_val = t.value.strip()
                    tx = t.position.x / 1e6
                    ty = -t.position.y / 1e6
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: User.4 text '{text_val}' "
                        f"at ({tx:.3f},{ty:.3f})\n")
                    if not text_val:
                        continue
                    # Parse "a=-70 r=0.5" style text
                    angle = 0.0
                    radius = 0.0
                    m_a = re.search(r'a\s*=\s*([+-]?\d+(?:\.\d+)?)',
                                    text_val)
                    if m_a:
                        angle = float(m_a.group(1))
                    m_r = re.search(r'r\s*=\s*([+-]?\d+(?:\.\d+)?)',
                                    text_val)
                    if m_r:
                        radius = float(m_r.group(1))
                    # Match text position to nearest bend line endpoint
                    best_dist = float('inf')
                    best_bl = None
                    best_ep = None
                    for bl in bend_lines:
                        for ep in (bl['start'], bl['end']):
                            d = math.hypot(tx - ep.x, ty - ep.y)
                            if d < best_dist:
                                best_dist = d
                                best_bl = bl
                                best_ep = ep
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   nearest bend ep "
                        f"({best_ep.x:.3f},{best_ep.y:.3f}) "
                        f"d={best_dist:.3f}mm\n")
                    if best_bl is not None and best_dist < GEOMETRY_TOLERANCE:
                        best_bl['angle'] = angle
                        best_bl['radius'] = radius
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD:   matched → "
                            f"angle={angle}° "
                            f"radius={radius}mm\n")
                    else:
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD:   not matched "
                            f"(d={best_dist:.6f}mm > 0.001mm)\n")
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: User.4 text items: "
                    f"{u4_text_count}\n")
            except Exception as ex:
                import traceback
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: Failed to parse User.4 "
                    f"text: {ex}\n")
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: {traceback.format_exc()}\n")

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
        board_face = None
        if edges:
            sorted_groups = Part.sortEdges(edges)
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: sortEdges produced {len(sorted_groups)}"
                f" group(s): "
                + ", ".join(f"{len(g)} edges" for g in sorted_groups)
                + "\n")
            outline_edges = sorted_groups[0]
            wires = []
            for g in sorted_groups:
                w = Part.Wire(g)
                if not w.isClosed():
                    # sortEdges may leave a micro-gap at the
                    # closure point.  Fix topology to close it.
                    try:
                        w.fixWire(None, GEOMETRY_TOLERANCE)
                    except Exception:
                        pass
                    if not w.isClosed():
                        # Last resort: add closing edge
                        verts = w.Vertexes
                        gap = verts[-1].Point.distanceToPoint(
                            verts[0].Point)
                        if gap < GEOMETRY_TOLERANCE:
                            closing = Part.makeLine(
                                verts[-1].Point, verts[0].Point)
                            w = Part.Wire(list(w.Edges) + [closing])
                wires.append(w)
            for wi, w in enumerate(wires):
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: wire {wi}: closed={w.isClosed()}"
                    f" edges={len(w.Edges)}"
                    f" verts={len(w.Vertexes)}\n")
            if len(wires) > 1:
                face = Part.Face(wires, "Part::FaceMakerBullseye")
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: Board outline has {len(wires) - 1} "
                    "hole(s)\n")
            else:
                face = Part.Face(wires[0])
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Board face area={face.Area:.4f}"
                f" valid={face.isValid()}\n")
            board_solid = face.extrude(FreeCAD.Vector(0, 0, thickness))

            board_face = face

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
                margin = GEOMETRY_TOLERANCE
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

                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   {ref}: {len(models)} model(s) in definition\n"
                )
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
                else:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   {ref}: skipped (no visible/resolved 3D models)\n"
                    )

            except Exception as ex:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD:   {ref}: footprint error: {ex}\n"
                )

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Found {len(footprints_data)} footprints with 3D models\n"
        )

        # Filter bend lines: keep only those with segments inside
        # the board (trimmed by expanded outline).
        if bend_lines and board_face is not None:
            valid_bends = []
            for bl in bend_lines:
                p0 = FreeCAD.Vector(bl['start'].x, bl['start'].y, 0)
                p1 = FreeCAD.Vector(bl['end'].x, bl['end'].y, 0)
                segs = LinkedObject._trim_line_to_outline(
                    None, p0, p1, board_face)
                if not segs:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: reject bend {bl.get('uuid', '?')[:8]}"
                        f" no segments cross board"
                        f" ({p0.x:.2f},{p0.y:.2f})"
                        f"-({p1.x:.2f},{p1.y:.2f})\n")
                    continue
                valid_bends.append(bl)
            bend_lines = valid_bends
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Valid bend lines: {len(bend_lines)}\n")

        # Filter out bend lines that overlap or cross each other
        if len(bend_lines) > 1:
            skip = set()
            for i in range(len(bend_lines)):
                if i in skip:
                    continue
                a0x, a0y = bend_lines[i]['start'].x, bend_lines[i]['start'].y
                a1x, a1y = bend_lines[i]['end'].x, bend_lines[i]['end'].y
                for j in range(i + 1, len(bend_lines)):
                    if j in skip:
                        continue
                    b0x, b0y = bend_lines[j]['start'].x, bend_lines[j]['start'].y
                    b1x, b1y = bend_lines[j]['end'].x, bend_lines[j]['end'].y
                    dx, dy = a1x - a0x, a1y - a0y
                    ex, ey = b1x - b0x, b1y - b0y
                    denom = dx * ey - dy * ex
                    if abs(denom) > 1e-12:
                        # Non-parallel: check if segments cross
                        t = ((b0x - a0x) * ey - (b0y - a0y) * ex) / denom
                        u = ((b0x - a0x) * dy - (b0y - a0y) * dx) / denom
                        if 0 <= t <= 1 and 0 <= u <= 1:
                            skip.add(i)
                            skip.add(j)
                            FreeCAD.Console.PrintWarning(
                                f"FreekiCAD: Bend lines "
                                f"{bend_lines[i]['uuid'][:8]} and "
                                f"{bend_lines[j]['uuid'][:8]} cross, "
                                f"ignoring both\n")
                            break
                    else:
                        # Parallel: check colinear overlap
                        la = (dx * dx + dy * dy) ** 0.5
                        if la < 1e-9:
                            continue
                        dnx, dny = dx / la, dy / la
                        # Perpendicular distance
                        rx, ry = b0x - a0x, b0y - a0y
                        proj = rx * dnx + ry * dny
                        px, py = rx - dnx * proj, ry - dny * proj
                        perp_dist = (px * px + py * py) ** 0.5
                        if perp_dist > 0.5:
                            continue
                        # Projection overlap
                        b_lo = rx * dnx + ry * dny
                        b_hi = (b1x - a0x) * dnx + (b1y - a0y) * dny
                        if b_lo > b_hi:
                            b_lo, b_hi = b_hi, b_lo
                        if la > b_lo + 0.5 and b_hi > 0.5:
                            skip.add(i)
                            skip.add(j)
                            FreeCAD.Console.PrintWarning(
                                f"FreekiCAD: Bend lines "
                                f"{bend_lines[i]['uuid'][:8]} and "
                                f"{bend_lines[j]['uuid'][:8]} overlap, "
                                f"ignoring both\n")
                            break
            if skip:
                bend_lines = [bl for k, bl in enumerate(bend_lines)
                              if k not in skip]

        return (board_solid, footprints_data, board_color, outline_edges,
                thickness, bend_lines, board_face)

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
    return None, [], None, [], DEFAULT_PCB_THICKNESS, [], None


def _fit_view(obj):
    """Fit the 3D viewport to show the given object."""
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
        FreeCADGui.SendMsgToActiveView("ViewFit")
    except Exception:
        pass


class BendLine:
    """FeaturePython proxy for a bend-line child object."""

    Type = "BendLine"

    def __init__(self, obj, uuid=""):
        obj.Proxy = self
        obj.addProperty(
            "App::PropertyString", "UUID",
            "Bending", "KiCad UUID of the bend line")
        obj.UUID = uuid
        try:
            obj.setPropertyStatus("UUID", "Hidden")
        except Exception:
            pass
        obj.addProperty(
            "App::PropertyLength", "Radius",
            "Bending", "Bend radius")
        obj.Radius = 0.0
        obj.addProperty(
            "App::PropertyAngle", "Angle",
            "Bending", "Bend angle")
        obj.Angle = 0.0
        obj.addProperty(
            "App::PropertyBool", "Active",
            "Bending", "Enable bending for this line")
        obj.Active = True

    def execute(self, obj):
        pass

    def onChanged(self, obj, prop):
        if prop not in ("Radius", "Angle", "Active"):
            return
        if prop in ("Radius", "Angle"):
            try:
                angle = obj.Angle.Value
                radius = obj.Radius.Value
                obj.Label2 = f"a={angle:.4g}° r={radius:.4g}"
            except Exception:
                pass
        if not obj.InList:
            return
        for parent in obj.InList:
            proxy = getattr(parent, "Proxy", None)
            if proxy and getattr(proxy, 'Type', None) == 'LinkedObject':
                proxy._schedule_rebend(parent)
                break

    def dumps(self):
        return None

    def loads(self, state):
        return None

    def onDocumentRestored(self, obj):
        if not hasattr(obj, "Active"):
            obj.addProperty(
                "App::PropertyBool", "Active",
                "Bending", "Enable bending for this line")
            obj.Active = True


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

    def _is_bending_active(self, parent):
        """True when EnableBending is on and at least one bend child
        has Active=True and a non-zero Angle."""
        if not getattr(parent, 'EnableBending', False):
            return False
        for c in parent.Group:
            proxy = getattr(c, 'Proxy', None)
            if proxy and getattr(proxy, 'Type', None) == 'BendLine':
                if c.Active and c.Angle.Value != 0:
                    return True
        return False

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
                # Skip when bending is active — placement changes are
                # cosmetic (applied by the bend transform).
                proxy = getattr(parent, "Proxy", None)
                if (proxy and getattr(proxy, '_bending', False)) \
                        or self._is_bending_active(parent):
                    return
                self._constrain_placement(obj)
                self._schedule_move_component(obj, parent)
                return
            elif hasattr(obj, 'TypeId') and obj.TypeId == "Part::Feature":
                # Skip debug piece objects (created by DebugBoard)
                if '_DebugPieces_' not in obj.Name:
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
            if not hasattr(obj, 'X'):
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
            new_kicad_x = float(obj.X) + delta_x
            new_kicad_y = float(obj.Y) + delta_y
            new_kicad_angle = float(obj.Rotation) + delta_yaw

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
        pass


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
        obj.Label2 = "AutoReload=On"
        obj.addProperty(
            "App::PropertyBool", "EnableBending", "LinkedFile",
            "Enable flex PCB bending deformation"
        )
        obj.EnableBending = True
        obj.addProperty(
            "App::PropertyBool", "BuildDebugObjects", "LinkedFile",
            "Build debug arrows and cut lines"
        )
        obj.BuildDebugObjects = False
        obj.addProperty(
            "App::PropertyBool", "DebugBoard", "LinkedFile",
            "Show each board piece as a separate child object"
        )
        obj.DebugBoard = False
        obj.addProperty(
            "App::PropertyBool", "SmoothWedge", "LinkedFile",
            "Use B-Spline loft for wedges (slower but smoother)"
        )
        obj.SmoothWedge = False
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
        if prop in ("EnableBending", "BuildDebugObjects", "DebugBoard",
                    "SmoothWedge"):
            if not obj.Document.Restoring:
                self._rebend(obj)
            return
        if prop == "AutoReload":
            try:
                obj.Label2 = f"AutoReload={'On' if obj.AutoReload else 'Off'}"
            except Exception:
                pass
            return
        if prop not in ("FileName",):
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
        """Remove outline sketch and board shape children, keep components
        and bend lines.
        Returns (existing_components, existing_bends) where
        existing_components is {ref: child_obj} and
        existing_bends is {uuid: child_obj}."""
        doc = obj.Document
        prefix = obj.Name + "_"
        existing_components = {}
        existing_bends = {}
        for child in list(obj.Group):
            if child.Name.endswith("_Outline") or child.Name.endswith("_Board"):
                try:
                    doc.removeObject(child.Name)
                except (ReferenceError, Exception) as e:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD: Failed to remove child: {e}\n"
                    )
            elif getattr(getattr(child, 'Proxy', None),
                         'Type', None) == 'BendLine':
                existing_bends[child.UUID] = child
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
        return existing_components, existing_bends

    def execute(self, obj):
        """Called by FreeCAD recompute.  Only ensures properties exist.
        Actual KiCad loading is done by reload()."""
        self._ensure_properties(obj)

    def _do_execute(self, obj, socket_path, existing_components=None,
                    existing_bends=None):
        """Internal execute implementation.
        NOTE: board/outline children must be removed BEFORE this method,
        outside of FreeCAD's recompute cycle.
        *socket_path*: resolved KiCad IPC socket path.
        *existing_components*: optional dict {label: child_obj} of component
        Part::Feature objects to reuse by designator match.
        *existing_bends*: optional dict {uuid: child_obj} of bend line
        objects to preserve radius/angle on reload."""
        import json
        import time as _time

        _t_load = _time.time()
        board_solid, footprints_data, board_color, outline_edges, \
            thickness, bend_lines, board_face = \
            load_board(obj.FileName, socket_path)
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] load_board: "
            f"{_time.time() - _t_load:.3f}s\n")

        # Freeze the main window to prevent viewport flashing
        # as children are added one by one.
        _mw = None
        try:
            import FreeCADGui
            _mw = FreeCADGui.getMainWindow()
            _mw.setUpdatesEnabled(False)
        except Exception:
            _mw = None

        _t_body = _time.time()
        try:
            self.__do_execute_body(obj, board_solid, footprints_data,
                                   board_color, outline_edges, thickness,
                                   bend_lines, existing_components,
                                   existing_bends,
                                   board_face)
        finally:
            if _mw is not None:
                _mw.setUpdatesEnabled(True)
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] __do_execute_body: "
            f"{_time.time() - _t_body:.3f}s\n")
        _fit_view(obj)

    def __do_execute_body(self, obj, board_solid, footprints_data,
                          board_color, outline_edges, thickness,
                          bend_lines, existing_components,
                          existing_bends,
                          board_face=None):
        import json
        import time as _time
        _t0_body = _time.time()
        doc = obj.Document

        self._board_color = board_color
        self._outline_edges = outline_edges or []
        self._board_face = board_face

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
            self._unbent_board_shape = board_solid.copy()
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

        # Add / update bend line children
        if existing_bends is None:
            existing_bends = {}
        seen_uuids = set()
        half_z = thickness / 2.0
        for bl in bend_lines:
            uuid = bl['uuid']
            seen_uuids.add(uuid)
            p0 = FreeCAD.Vector(bl['start'].x, bl['start'].y, half_z)
            p1 = FreeCAD.Vector(bl['end'].x, bl['end'].y, half_z)
            if uuid in existing_bends:
                bend_obj = existing_bends[uuid]
                bend_obj.Shape = Part.makeLine(p0, p1)
            else:
                bend_obj = doc.addObject(
                    "Part::FeaturePython", obj.Name + "_Bend")
                BendLine(bend_obj, uuid)
                bend_obj.Shape = Part.makeLine(p0, p1)
                obj.addObject(bend_obj)
                bend_obj.ViewObject.Proxy = 0
            # Apply angle/radius from KiCad text annotation
            if 'angle' in bl:
                bend_obj.Angle = bl['angle']
            if 'radius' in bl:
                bend_obj.Radius = bl['radius']
            try:
                angle = bend_obj.Angle.Value
                radius = bend_obj.Radius.Value
                bend_obj.Label2 = f"a={angle:.4g}° r={radius:.4g}"
            except Exception:
                pass
        # Remove stale bend lines no longer in KiCad
        for uuid, bend_obj in existing_bends.items():
            if uuid not in seen_uuids:
                try:
                    doc.removeObject(bend_obj.Name)
                except Exception:
                    pass

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] board+outline+bends setup: "
            f"{_time.time() - _t0_body:.3f}s\n")
        # Load component 3D models on demand, reusing where possible
        _t_comps = _time.time()
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
                for pname, ptype in (
                        ('X', 'App::PropertyDistance'),
                        ('Y', 'App::PropertyDistance'),
                        ('Rotation', 'App::PropertyAngle')):
                    if not hasattr(comp_obj, pname):
                        comp_obj.addProperty(
                            ptype, pname,
                            "KiCad",
                            "KiCad coordinate")
                        try:
                            comp_obj.setPropertyStatus(
                                pname, "ReadOnly")
                        except Exception:
                            pass
                comp_obj.X = kc[0]
                comp_obj.Y = kc[1]
                comp_obj.Rotation = kc[2]
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

        self._board_thickness = thickness

        # Sort children: sketch, board, bend lines, components
        def _child_sort_key(c):
            if c.Name.endswith("_Outline"):
                return (0, c.Label)
            if c.Name.endswith("_Board"):
                return (1, c.Label)
            if getattr(getattr(c, 'Proxy', None),
                       'Type', None) == 'BendLine':
                return (2, c.Label)
            return (3, c.Label)
        obj.Group = sorted(obj.Group, key=_child_sort_key)

        # Store unbent placements for bend lines and components.
        self._unbent_placements = {}
        for c in obj.Group:
            if getattr(getattr(c, 'Proxy', None),
                       'Type', None) == 'BendLine':
                self._unbent_placements[c.Name] = c.Placement.copy()
            elif hasattr(c, 'X'):
                init_p = getattr(c, 'FreekiCAD_InitPlacement', None)
                if init_p is not None:
                    self._unbent_placements[c.Name] = init_p.copy()
                    c.Placement = init_p.copy()
                else:
                    self._unbent_placements[c.Name] = \
                        c.Placement.copy()

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] component loading + sorting: "
            f"{_time.time() - _t_comps:.3f}s\n")
        # Apply bending deformation for active bend lines
        bend_children = [c for c in obj.Group
                         if getattr(getattr(c, 'Proxy', None),
                                    'Type', None) == 'BendLine']
        enable = getattr(obj, 'EnableBending', True)
        active_bends = [c for c in bend_children
                        if c.Active and c.Radius.Value >= 0]
        board_obj = None
        for c in obj.Group:
            if c.Name.endswith("_Board"):
                board_obj = c
                break
        if board_obj and bend_children:
            self._apply_bends(obj, board_obj, bend_children,
                              thickness, enable_bending=enable)
        # Cancel any pending rebend timer — bending was already
        # handled by _apply_bends above.
        if hasattr(self, '_rebend_timer'):
            self._rebend_timer.stop()

    def _update_reused_component(self, comp_obj, kc, thickness, fp_info):
        """Update placement and KiCad coords for a reused component
        whose 3D model hasn't changed but whose KiCad position may have.
        *kc* is (new_kicad_x_mm, new_kicad_y_mm, new_kicad_angle_deg)
        in FreeCAD coordinates (Y already negated)."""
        if not hasattr(comp_obj, 'X'):
            return
        old_x = float(comp_obj.X)
        old_y = float(comp_obj.Y)
        old_angle = float(comp_obj.Rotation)
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
        comp_obj.X = new_x
        comp_obj.Y = new_y
        comp_obj.Rotation = new_angle

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

    def _schedule_rebend(self, obj):
        """Schedule a deferred rebend, coalescing changes from multiple
        bend lines into a single rebend call."""
        from PySide import QtCore, QtWidgets

        if hasattr(self, '_rebend_timer'):
            self._rebend_timer.stop()

        def _do_rebend():
            # Check if a property editor widget is still focused
            app = QtWidgets.QApplication.instance()
            if app:
                w = app.focusWidget()
                if w and isinstance(
                        w, (QtWidgets.QDoubleSpinBox,
                            QtWidgets.QSpinBox,
                            QtWidgets.QLineEdit)):
                    # Still editing — retry later
                    self._rebend_timer.start(1000)
                    return
            self._rebend(obj)

        self._rebend_timer = QtCore.QTimer()
        self._rebend_timer.setSingleShot(True)
        self._rebend_timer.timeout.connect(_do_rebend)
        self._rebend_timer.start(1000)

    def _rebend(self, obj):
        """Re-apply bending after Radius/Angle/Active or EnableBending
        changes on a bend line."""
        if not hasattr(self, '_unbent_board_shape'):
            return
        import time as _time
        _t0_rebend = _time.time()
        self._bending = True
        try:
            thickness = getattr(self, '_board_thickness',
                                DEFAULT_PCB_THICKNESS)

            board_obj = None
            for c in obj.Group:
                if c.Name.endswith("_Board"):
                    board_obj = c
                    break

            # Restore original board shape
            _t_restore = _time.time()
            if board_obj and hasattr(self, '_unbent_board_shape'):
                board_obj.Shape = self._unbent_board_shape.copy()

            # Restore unbent placements for bend lines and components.
            if not hasattr(self, '_unbent_placements'):
                self._unbent_placements = {}
                for c in obj.Group:
                    if getattr(getattr(c, 'Proxy', None),
                               'Type', None) == 'BendLine':
                        self._unbent_placements[c.Name] = \
                            c.Placement.copy()
                    elif hasattr(c, 'X'):
                        init_p = getattr(
                            c, 'FreekiCAD_InitPlacement', None)
                        if init_p is not None:
                            self._unbent_placements[c.Name] = \
                                init_p.copy()
                        else:
                            self._unbent_placements[c.Name] = \
                                c.Placement.copy()
            for c in obj.Group:
                if c.Name in self._unbent_placements:
                    c.Placement = \
                        self._unbent_placements[c.Name].copy()
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: [profile] _rebend restore shapes+placements: "
                f"{_time.time() - _t_restore:.3f}s\n")

            # Re-apply all active bends
            bend_children = [
                c for c in obj.Group
                if getattr(getattr(c, 'Proxy', None),
                           'Type', None) == 'BendLine']
            enable = getattr(obj, 'EnableBending', True)
            active_bends = [c for c in bend_children
                            if c.Active and c.Angle.Value != 0
                            and c.Radius.Value >= 0]
            if enable and active_bends and board_obj:
                self._apply_bends(obj, board_obj, active_bends,
                                  thickness)
        finally:
            self._bending = False
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: [profile] TOTAL _rebend: "
                f"{_time.time() - _t0_rebend:.3f}s\n")

    def _apply_bends(self, obj, board_obj, bend_children, thickness,
                     enable_bending=True):
        """Apply bending deformation to the board shape."""
        self._bending = True
        try:
            self.__apply_bends_impl(obj, board_obj, bend_children,
                                    thickness, enable_bending)
        finally:
            self._bending = False

    def __apply_bends_impl(self, obj, board_obj, bend_children,
                           thickness, enable_bending=True):
        import time as _time
        _t0_total = _time.time()
        unbent = getattr(self, '_unbent_board_shape', board_obj.Shape)
        # Use the bounding box center as the reference point for
        # bend normal orientation and stationary piece selection.
        # BoundBox.Center is always well-defined, even for shapes
        # with holes (unlike CenterOfMass which can fall outside).
        mass_center = unbent.BoundBox.Center
        half_t = thickness / 2.0
        up = FreeCAD.Vector(0, 0, 1)

        # --- Phase 1: collect bend info from flat positions ---
        _t_phase1 = _time.time()
        board_face = getattr(self, '_board_face', None)
        bend_info = []
        for bend_obj in bend_children:
            angle_deg = bend_obj.Angle.Value
            radius = bend_obj.Radius.Value
            if radius < 0:
                continue

            verts = bend_obj.Shape.Vertexes
            p0 = FreeCAD.Vector(verts[0].Point.x, verts[0].Point.y, 0)
            p1 = FreeCAD.Vector(verts[1].Point.x, verts[1].Point.y, 0)
            line_dir = p1 - p0
            line_dir.normalize()
            normal = FreeCAD.Vector(-line_dir.y, line_dir.x, 0)

            mc_dist = (FreeCAD.Vector(mass_center.x, mass_center.y, 0)
                       - p0).dot(normal)
            if mc_dist > 0:
                normal = normal * -1

            bend_info.append((bend_obj, p0, p1, line_dir, normal,
                              math.radians(angle_deg), radius))

        if not bend_info:
            return

        # --- Phase 2: cut flat board with all bend faces, classify
        #     pieces via BFS to find which bends each piece crosses ---
        bb = unbent.BoundBox
        diag = bb.DiagonalLength + 50
        thickness = half_t * 2

        # Compute inset for each bend using r_eff = R + T/θ
        # inset = r_eff * |θ| / 2 = R*|θ|/2 + T/2
        insets = []
        for _, _, _, _, _, angle_rad, radius in bend_info:
            abs_a = abs(angle_rad)
            if abs_a < 1e-9:
                insets.append(0.0)
            else:
                r_eff = radius + half_t
                insets.append(r_eff * abs_a / 2.0)

        for bi, ins in enumerate(insets):
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: bend {bi} inset={ins:.4f}mm\n")
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Phase 1 (bend info): "
            f"{_time.time() - _t_phase1:.3f}s\n")
        # --- Phase 2a: 2D cut plan ---
        _t_phase2a = _time.time()
        # Each cut carries full bend info + moving_normal flag.
        # Format: (seg_p0, seg_p1, side, bi, angle_rad, radius,
        #          p0, normal, bend_obj, moving_normal)
        cut_plan = []
        trimmed_bend_segs = []  # per bend: list of (sp0, sp1)
        for bi, (bend_obj_ref, p0, p1, line_dir, normal,
                 angle_rad, radius) in enumerate(bend_info):
            ins = insets[bi]
            bl_segs = self._trim_line_to_outline(
                p0, p1, board_face)
            if not bl_segs:
                bl_segs = [(p0, p1)]
            trimmed_bend_segs.append(bl_segs)

            # All bends have inset > 0 (r_eff always > 0)
            a_p0 = p0 - normal * ins
            a_p1 = p1 - normal * ins
            b_p0 = p0 + normal * ins
            b_p1 = p1 + normal * ins
            a_segs = self._trim_line_to_outline(
                a_p0, a_p1, board_face)
            if not a_segs:
                a_segs = [(a_p0, a_p1)]
            b_segs = self._trim_line_to_outline(
                b_p0, b_p1, board_face)
            if not b_segs:
                b_segs = [(b_p0, b_p1)]
            for sp0, sp1 in a_segs:
                cut_plan.append((sp0, sp1, 'A', bi,
                                 angle_rad, radius, p0, normal,
                                 bend_obj_ref, normal))
            for mp0, mp1 in b_segs:
                cut_plan.append((mp0, mp1, 'B', bi,
                                 angle_rad, radius, p0, normal,
                                 bend_obj_ref, normal))

        # Validate: discard virtual cuts whose midpoint is too
        # far from any trimmed bend CENTER segment.
        # Uses 2D distance (not parameter projection) so that
        # angled outlines don't discard valid cuts (bend 8 fix),
        # while phantom segments far from the center line are
        # still caught (bend 4 phantom fix).
        validated_plan = []
        for entry in cut_plan:
            sp0, sp1, side, bi = entry[0], entry[1], entry[2], entry[3]
            ins_bi = insets[bi]
            mid = (sp0 + sp1) * 0.5
            mid_2d = FreeCAD.Vector(mid.x, mid.y, 0)
            # 2D distance from cut midpoint to nearest center
            # line segment.  Valid cuts are at ~ins distance
            # (perpendicular offset).  Phantom segments are
            # much further.
            min_dist = float('inf')
            for bl_sp0, bl_sp1 in trimmed_bend_segs[bi]:
                dx = bl_sp1.x - bl_sp0.x
                dy = bl_sp1.y - bl_sp0.y
                len2 = dx * dx + dy * dy
                if len2 < 1e-12:
                    d = math.sqrt((mid_2d.x - bl_sp0.x) ** 2
                                  + (mid_2d.y - bl_sp0.y) ** 2)
                else:
                    t = max(0.0, min(1.0,
                        ((mid_2d.x - bl_sp0.x) * dx
                         + (mid_2d.y - bl_sp0.y) * dy)
                        / len2))
                    px = bl_sp0.x + t * dx
                    py = bl_sp0.y + t * dy
                    d = math.sqrt((mid_2d.x - px) ** 2
                                  + (mid_2d.y - py) ** 2)
                if d < min_dist:
                    min_dist = d
            on_bend = min_dist < ins_bi + GEOMETRY_TOLERANCE
            if on_bend:
                validated_plan.append(entry)
            else:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: discard cut side={side}"
                    f" dist={min_dist:.3f}"
                    f" ({sp0.x:.2f},{sp0.y:.2f})"
                    f"-({sp1.x:.2f},{sp1.y:.2f})\n")
        cut_plan = validated_plan

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Phase 2a (2D cut plan): "
            f"{_time.time() - _t_phase2a:.3f}s\n")
        # --- Phase 2b: create 3D cutting faces from 2D plan ---
        _t_phase2b = _time.time()
        # Each stationary-side cut segment → independent micro-bend.
        # Moving-side cuts → geometry only (no micro-bend, no rotation).
        # After 2D planning, everything is per cut line.
        cut_faces = []
        micro_bend_info = []  # per micro-bend: (angle, bend_obj,
                              #   cut_mid, normal, radius, orig_bi)
        # --- Phase 2b-1: create cut faces with generic bend labels ---
        # Both geometric sides get the same label (bi) initially.
        # Stationary/moving role is determined per crossing via BFS.
        face_to_micro = {}  # fi → label (initially all bi)
        face_topo_side = {}  # fi → topological side ('A' or 'B')
        face_bend = {}      # fi → bi
        cut_plan_data = {}  # fi → (angle_rad, bend_obj, cut_mid,
                            #       normal, radius, bi)

        for entry in cut_plan:
            sp0, sp1 = entry[0], entry[1]
            side, bi = entry[2], entry[3]
            angle_rad = entry[4]
            radius = entry[5]
            p0_ref = entry[6]
            normal_ref = entry[7]
            bend_obj_ref = entry[8]

            fi = len(cut_faces)
            c1 = sp0 - up * diag
            c2 = sp1 - up * diag
            c3 = sp1 + up * diag
            c4 = sp0 + up * diag
            cut_faces.append(
                Part.Face(Part.makePolygon([c1, c2, c3, c4, c1])))

            cut_mid = (sp0 + sp1) * 0.5
            face_topo_side[fi] = side
            face_bend[fi] = bi
            # All faces labelled with bi
            face_to_micro[fi] = bi
            cut_plan_data[fi] = (angle_rad, bend_obj_ref,
                                 FreeCAD.Vector(cut_mid),
                                 normal_ref, radius, bi)

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Phase 2b (3D cut faces): "
            f"{_time.time() - _t_phase2b:.3f}s\n")
        # --- Phase 2c: cut board, assign stationary/moving ---
        _t_phase2c = _time.time()
        _t_fuse = _time.time()
        try:
            # NOTE: generalFuse returns a map of input→output face
            # images, but we don't use it for adjacency because:
            # (1) the map only tracks faces, missing edge/vertex
            #     adjacency between pieces;
            # (2) pieces filtered by Volume don't correspond 1:1
            #     to compound solids, making face ownership fragile.
            # Instead we slice pieces to 2D and use distToShape on
            # the lightweight 2D wires.
            fused, _map = unbent.generalFuse(cut_faces)
            pieces = [s for s in fused.Solids if s.Volume > 1e-6]
        except Exception:
            pieces = []
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] generalFuse: "
            f"{_time.time() - _t_fuse:.3f}s "
            f"({len(pieces)} pieces)\n")

        # Build 2D slices of pieces for fast adjacency checks.
        # The board is flat — slicing at z=half_t gives 2D wires
        # that are orders of magnitude cheaper than 3D distToShape.
        _t_slices = _time.time()
        piece_slices = []
        for piece in pieces:
            wires = piece.slice(FreeCAD.Vector(0, 0, 1), half_t)
            if wires:
                piece_slices.append(Part.Compound(wires))
            else:
                piece_slices.append(piece)  # fallback to 3D
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] 2D piece slices: "
            f"{_time.time() - _t_slices:.3f}s\n")

        # Build 2D cut segments for adjacency face matching.
        cut_segments = []
        for fi in range(len(cut_faces)):
            sp0, sp1 = cut_plan[fi][0], cut_plan[fi][1]
            edge_2d = Part.makeLine(
                FreeCAD.Vector(sp0.x, sp0.y, half_t),
                FreeCAD.Vector(sp1.x, sp1.y, half_t))
            cut_segments.append(edge_2d)

        # Build joints.  Each trimmed center segment
        # is one joint containing: the center seg, zero-or-more A
        # faces, zero-or-more B faces, and zero-or-more wedge pieces.
        # This replaces both the old A/B pairing and strip_pieces
        # identification with a single centre-segment-centric pass.
        #
        # joints: list of dicts, indexed by sid:
        #   {'bi', 'center', 'a_faces', 'b_faces', 'wedges'}
        joints = []  # indexed by sid
        seg_to_bend = {}      # sid → bi
        face_to_seg = {}      # fi → sid
        strip_pieces = set()
        strip_to_bend = {}
        for bi, (_, p0, p1, line_dir, normal,
                 angle_rad, radius) in enumerate(bend_info):
            ins = insets[bi]
            bl_segs = trimmed_bend_segs[bi]
            for bl_sp0, bl_sp1 in bl_segs:
                sid = len(joints)
                seg_to_bend[sid] = bi
                joint = {
                    'bi': bi,
                    'center': (bl_sp0, bl_sp1),
                    'a_faces': [],
                    'b_faces': [],
                    'wedges': [],
                }
                joints.append(joint)
                # Assign A/B faces whose midpoint projects onto
                # this center segment (within tolerance).
                sx = bl_sp1.x - bl_sp0.x
                sy = bl_sp1.y - bl_sp0.y
                sl2 = sx * sx + sy * sy
                for fi in range(len(cut_faces)):
                    if face_bend.get(fi) != bi:
                        continue
                    if fi in face_to_seg:
                        continue
                    mid = (cut_plan[fi][0] + cut_plan[fi][1]) * 0.5
                    if sl2 < 1e-12:
                        d = math.sqrt((mid.x - bl_sp0.x) ** 2
                                      + (mid.y - bl_sp0.y) ** 2)
                    else:
                        t = max(0.0, min(1.0,
                            ((mid.x - bl_sp0.x) * sx
                             + (mid.y - bl_sp0.y) * sy)
                            / sl2))
                        px = bl_sp0.x + t * sx
                        py = bl_sp0.y + t * sy
                        d = math.sqrt((mid.x - px) ** 2
                                      + (mid.y - py) ** 2)
                    if d < ins + GEOMETRY_TOLERANCE:
                        face_to_seg[fi] = sid
                        side = face_topo_side[fi]
                        if side == 'A':
                            joint['a_faces'].append(fi)
                        else:
                            joint['b_faces'].append(fi)
                # Assign wedge pieces whose center of mass is
                # within ins of this center segment.
                if ins < 1e-6:
                    continue
                for pi, piece in enumerate(pieces):
                    if pi in strip_pieces:
                        continue
                    cm = piece.CenterOfMass
                    if sl2 < 1e-12:
                        d = math.sqrt((cm.x - bl_sp0.x) ** 2
                                      + (cm.y - bl_sp0.y) ** 2)
                    else:
                        t = max(0.0, min(1.0,
                            ((cm.x - bl_sp0.x) * sx
                             + (cm.y - bl_sp0.y) * sy)
                            / sl2))
                        px = bl_sp0.x + t * sx
                        py = bl_sp0.y + t * sy
                        d = math.sqrt((cm.x - px) ** 2
                                      + (cm.y - py) ** 2)
                    if d < ins + GEOMETRY_TOLERANCE:
                        joint['wedges'].append(pi)
                        strip_pieces.add(pi)
                        strip_to_bend[pi] = bi

        # Build geometric crossings and BFS for s/m assignment.
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Phase 2c (pairing): "
            f"{_time.time() - _t_phase2c:.3f}s\n")
        _t_bfs1 = _time.time()
        geo_crossings = self._build_geometric_adjacency(
            pieces, cut_faces, cut_plan,
            piece_slices=piece_slices,
            cut_segments=cut_segments,
            joints=joints)

        # Stationary piece = closest non-wedge piece to board
        # outline center of mass.
        n_pieces = len(pieces)
        mc_2d = FreeCAD.Vector(
            mass_center.x, mass_center.y, half_t)
        _sp_cands = [pi for pi in range(n_pieces)
                     if pi not in strip_pieces]
        if not _sp_cands:
            _sp_cands = list(range(n_pieces))
        stationary_idx = min(
            _sp_cands,
            key=lambda pi: pieces[pi].CenterOfMass.distanceToPoint(
                FreeCAD.Vector(mc_2d.x, mc_2d.y,
                               pieces[pi].CenterOfMass.z)))

        # BFS from stationary piece, skipping wedges.
        # fi_parent[fi] = non-wedge parent piece for crossing face fi.
        # bfs_parent[pi] = non-wedge parent for non-wedge piece pi.
        _adj = [[] for _ in range(n_pieces)]
        for c_i, c_j, c_fi in geo_crossings:
            _adj[c_i].append((c_j, c_fi))
            _adj[c_j].append((c_i, c_fi))
        fi_parent = {}       # fi → non-wedge parent piece index
        bfs_parent = {stationary_idx: None}
        _bfs_visited = {stationary_idx}
        _bfs_q = [stationary_idx]
        while _bfs_q:
            _cur = _bfs_q.pop(0)  # always non-wedge
            for _nbr, _fi in _adj[_cur]:
                if _nbr not in strip_pieces:
                    # Direct non-wedge neighbor
                    if _fi not in fi_parent:
                        fi_parent[_fi] = _cur
                    if _nbr not in _bfs_visited:
                        _bfs_visited.add(_nbr)
                        bfs_parent[_nbr] = _cur
                        _bfs_q.append(_nbr)
                else:
                    # Wedge neighbor — walk through wedge chain
                    _w_visited = {_cur}
                    _w_q = [(_nbr, _fi)]
                    while _w_q:
                        _w, _wfi = _w_q.pop(0)
                        if _w in _w_visited:
                            continue
                        _w_visited.add(_w)
                        if _wfi not in fi_parent:
                            fi_parent[_wfi] = _cur
                        if _w not in strip_pieces:
                            # Reached non-wedge on the other side
                            if _w not in _bfs_visited:
                                _bfs_visited.add(_w)
                                bfs_parent[_w] = _cur
                                _bfs_q.append(_w)
                            continue
                        # _w is a wedge, continue through it
                        for _w_nbr, _w_fi in _adj[_w]:
                            if _w_nbr not in _w_visited:
                                _w_q.append((_w_nbr, _w_fi))
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] BFS (piece parents): "
            f"{_time.time() - _t_bfs1:.3f}s\n")

        # --- Phase 2b-2: assign stationary/moving and create micro-bends ---
        _t_phase2b2 = _time.time()
        # Log per-crossing stationary/moving assignment.
        # (per-bend logging removed — per-cut makes it irrelevant)
        s_group = {}
        bend_seg_mids = {}
        bend_s_mis = {}  # bi → list of mi's (one per stationary segment)
        # Per-cut labeling: each A and B face gets a unique ID
        # with a segment index within its bend, displayed as
        # "9.0A", "9.1B" etc. in adjacency/path logs.
        m_face_counter = 0
        m_face_to_bend = {}  # unique_m_id → (bi, seg_idx)
        mi_seg_idx = {}  # mi → seg_idx (for stationary faces)
        # Derive segment index from face_to_seg pairing:
        # paired A and B faces share the same sid, so they
        # get the same seg_idx within their bend.
        sid_to_seg_idx = {}  # sid → seg_idx
        bend_seg_count = {}  # bi → next seg_idx
        for fi in range(len(cut_faces)):
            bi = face_bend.get(fi)
            if bi is None:
                continue
            topo_side = face_topo_side[fi]
            # Per-crossing stationary side from BFS direction
            sid = face_to_seg.get(fi)
            # Assign seg_idx from pairing: same sid → same idx
            if sid is not None and sid in sid_to_seg_idx:
                seg_idx = sid_to_seg_idx[sid]
            else:
                seg_idx = bend_seg_count.get(bi, 0)
                bend_seg_count[bi] = seg_idx + 1
                if sid is not None:
                    sid_to_seg_idx[sid] = seg_idx
            # Determine stationary/moving: non-wedge BFS parent
            # touches stationary-side face (distToShape < tol).
            parent_pi = fi_parent.get(fi)
            if parent_pi is not None:
                is_stationary = (
                    pieces[parent_pi].distToShape(
                        cut_faces[fi])[0] < GEOMETRY_TOLERANCE)
            else:
                is_stationary = (topo_side == 'A')
            if not is_stationary:
                # Per-cut: each moving-side face gets a unique negative ID
                m_id = -(m_face_counter + len(bend_info) + 2)
                m_face_to_bend[m_id] = (bi, seg_idx)
                face_to_micro[fi] = m_id
                m_face_counter += 1
            else:
                # Per-cut: each stationary-side face gets its own micro-bend
                mi = len(micro_bend_info)
                s_group[(bi, fi)] = mi
                mi_seg_idx[mi] = seg_idx
                data = cut_plan_data[fi]
                angle_rad, bend_obj_ref, cut_mid, normal_ref, \
                    radius, _ = data
                # Per-cut: orient normal away from BFS parent
                # (from stationary side into wedge) using parent piece position.
                if parent_pi is not None:
                    parent_cm = pieces[parent_pi].CenterOfMass
                    dot_val = (parent_cm - cut_mid).dot(normal_ref)
                    flipped = dot_val > 0
                    if flipped:
                        normal_ref = normal_ref * -1
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: mi={mi} sid={sid}"
                        f" parent_pi={parent_pi}"
                        f" parent_cm=({parent_cm.x:.2f},"
                        f"{parent_cm.y:.2f},{parent_cm.z:.2f})"
                        f" cut_mid=({cut_mid.x:.2f},"
                        f"{cut_mid.y:.2f},{cut_mid.z:.2f})"
                        f" dot={dot_val:.4f}"
                        f" flipped={flipped}\n")
                else:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: mi={mi} sid={sid}"
                        f" NO parent_pi from bfs_parent\n")
                micro_bend_info.append((angle_rad, bend_obj_ref,
                                        cut_mid, normal_ref,
                                        radius, bi))
                face_to_micro[fi] = mi
                bend_seg_mids.setdefault(bi, []).append(
                    FreeCAD.Vector(cut_mid))
                bend_s_mis.setdefault(bi, []).append(mi)
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: mi={mi} bend={bi}"
                    f" seg={seg_idx}"
                    f" angle={math.degrees(angle_rad):.1f}°"
                    f" normal=({normal_ref.x:.3f},{normal_ref.y:.3f},{normal_ref.z:.3f})"
                    f"\n")

        # BFS with stationary/moving labels
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Phase 2b-2 (stationary/moving assign): "
            f"{_time.time() - _t_phase2b2:.3f}s\n")
        _t_bfs2 = _time.time()
        face_to_bend = face_to_micro

        piece_bend_sets, bfs_tree, adjacency, _ = \
            self._classify_pieces_bfs(
                pieces, cut_faces, face_to_bend, mass_center,
                half_t, bend_info, cut_plan, micro_bend_info,
                m_face_to_bend=m_face_to_bend,
                mi_seg_idx=mi_seg_idx,
                cached_geo_crossings=geo_crossings,
                strip_pieces=strip_pieces,
                face_to_seg=face_to_seg,
                joints=joints)
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] BFS (final): "
            f"{_time.time() - _t_bfs2:.3f}s\n")

        # Map components to pieces using flat (X, Y) in 2D
        comp_piece_idx = {}  # child.Name → pi
        for child in obj.Group:
            if not hasattr(child, 'X'):
                continue
            pt = FreeCAD.Vector(
                float(child.X), float(child.Y), half_t)
            for pi, piece in enumerate(pieces):
                if piece.isInside(pt, 0.5, True):
                    comp_piece_idx[child.Name] = pi
                    break

        # Map bend lines to pieces.
        # For each bend line, find its own micro-bend(s) and
        # assign it to the piece on the STATIONARY side (the
        # piece that does NOT have the bend's own micro-bend).
        # This ensures the bend line gets the correct rotations.
        bendline_bend_sets = {}
        bendline_piece_idx = {}  # child.Name → piece index

        # Build mapping: bend_obj.Name → set of own micro indices
        bendline_own_micros = {}
        for mi, (_, bobj, _, _, _, _) in enumerate(
                micro_bend_info):
            bendline_own_micros.setdefault(
                bobj.Name, set()).add(mi)

        for child in obj.Group:
            if (getattr(getattr(child, 'Proxy', None),
                        'Type', None) != 'BendLine'):
                continue
            bl_verts = child.Shape.Vertexes
            if len(bl_verts) < 2:
                continue
            pt = FreeCAD.Vector(
                (bl_verts[0].Point.x + bl_verts[1].Point.x) / 2,
                (bl_verts[0].Point.y + bl_verts[1].Point.y) / 2,
                half_t)

            own_mis = bendline_own_micros.get(child.Name, set())

            # Find the piece containing this point
            found_pi = None
            for pi, piece in enumerate(pieces):
                if piece.isInside(pt, 0.5, True):
                    found_pi = pi
                    break

            # Use adjacency to find the piece on the stationary
            # side of the bend's own micro-bend.  This is the
            # piece adjacent to the own cut that does NOT have
            # the own micro-bend in its multiplier.
            if own_mis:
                stat_pi = None
                for mi_own in own_mis:
                    for pi_a in range(len(pieces)):
                        for nbr, mi_adj, _fi in adjacency[pi_a]:
                            if mi_adj != mi_own:
                                continue
                            # pi_a and nbr separated by mi_own
                            if mi_own not in \
                                    piece_bend_sets[pi_a]:
                                stat_pi = pi_a
                            elif mi_own not in \
                                    piece_bend_sets[nbr]:
                                stat_pi = nbr
                            if stat_pi is not None:
                                break
                        if stat_pi is not None:
                            break
                    if stat_pi is not None:
                        break
                if stat_pi is not None:
                    found_pi = stat_pi

            if found_pi is not None:
                bendline_bend_sets[child.Name] = \
                    piece_bend_sets[found_pi]
                bendline_piece_idx[child.Name] = found_pi
            else:
                pt_2d = FreeCAD.Vector(pt.x, pt.y, 0)
                fb = set()
                for bi, (_, p0, _, _, normal, _, _) in enumerate(
                        bend_info):
                    if (pt_2d - p0).dot(normal) > 0:
                        fb.add(bi)
                bendline_bend_sets[child.Name] = fb

        # Log bend line piece assignments
        for child in obj.Group:
            if (getattr(getattr(child, 'Proxy', None),
                        'Type', None) != 'BendLine'):
                continue
            bl_pi = bendline_piece_idx.get(child.Name)
            bl_set = bendline_bend_sets.get(child.Name, set())
            if bl_pi is not None:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: bendline {child.Name}"
                    f" in piece {bl_pi}"
                    f" set={sorted(bl_set)}\n")

        # --- Phase 3: apply bends sequentially using pre-cut pieces ---
        _t_phase3 = _time.time()
        up = FreeCAD.Vector(0, 0, 1)
        piece_shapes = [p.copy() for p in pieces]

        # strip_pieces and strip_to_bend already computed before BFS.

        # Map each wedge piece to its specific S-cut mi
        # (from BFS tree: the mi through which BFS first reached the wedge).
        strip_to_mi = {}
        for wpi in strip_pieces:
            if wpi in bfs_tree:
                _parent, mis_crossed, _ = bfs_tree[wpi]
                for mi_val in mis_crossed:
                    if mi_val >= 0 and micro_bend_info[mi_val][5] == strip_to_bend[wpi]:
                        strip_to_mi[wpi] = mi_val
                        break

        # Compute bend processing order from BFS traversal.
        # Stationary piece = piece closest to the board outline's
        # center of mass.
        mc_2d = FreeCAD.Vector(mass_center.x, mass_center.y, 0)
        stationary_idx = min(
            range(len(pieces)),
            key=lambda pi: pieces[pi].CenterOfMass.distanceToPoint(
                FreeCAD.Vector(mc_2d.x, mc_2d.y,
                               pieces[pi].CenterOfMass.z)))
        bend_order = []
        seen_bends = set()
        bfs_visit = {stationary_idx}
        bfs_q = [stationary_idx]
        while bfs_q:
            cur = bfs_q.pop(0)
            entry = bfs_tree.get(cur)
            if entry:
                for bi in sorted(entry[1]):
                    if bi >= 0 and bi not in seen_bends:
                        bend_order.append(bi)
                        seen_bends.add(bi)
            for pi in range(len(pieces)):
                if pi in bfs_visit:
                    continue
                e = bfs_tree.get(pi)
                if e and e[0] == cur:
                    bfs_visit.add(pi)
                    bfs_q.append(pi)
        # Add any bends not in BFS tree
        for bi in range(len(bend_info)):
            if bi not in seen_bends:
                bend_order.append(bi)
                seen_bends.add(bi)

        # Per-cut: ordered chain of mi's from root to each piece.
        # Duplicates are preserved (a piece can cross the same mi twice).
        # bfs_tree entries are (parent, mis_crossed_set, wedge_pi).
        piece_mi_list = [[] for _ in range(len(pieces))]
        for pi in range(len(pieces)):
            if pi in strip_pieces:
                continue  # handle wedges separately below
            chain = []
            cur = pi
            while cur is not None:
                entry = bfs_tree.get(cur)
                if entry is None:
                    break
                parent = entry[0]
                mis_crossed = entry[1]  # set of mi indices
                if parent is not None:
                    for mi in sorted(mis_crossed):
                        if mi >= 0:
                            chain.append(mi)
                cur = parent
            chain.reverse()  # root-to-piece order
            piece_mi_list[pi] = chain

        # Wedge chains: stationary neighbor's chain + own mi.
        for wpi in strip_pieces:
            mi_w = strip_to_mi.get(wpi)
            if mi_w is not None:
                for nbr, mi_adj, _fi in adjacency[wpi]:
                    if mi_adj == mi_w and nbr not in strip_pieces:
                        piece_mi_list[wpi] = \
                            piece_mi_list[nbr] + [mi_w]
                        break
                else:
                    # Fallback: copy from dest piece through wedge
                    for dest_pi, entry in bfs_tree.items():
                        if entry[2] == wpi:
                            piece_mi_list[wpi] = \
                                list(piece_mi_list[dest_pi])
                            break

        # Helper: does piece pi rotate at this step?
        def _at_step(pi, step_pos, mi):
            return (step_pos < len(piece_mi_list[pi])
                    and piece_mi_list[pi][step_pos] == mi)

        max_chain_len = max(
            (len(lst) for lst in piece_mi_list), default=0)

        # Log piece_mi_list for neighbours of stationary piece
        for pi in range(len(pieces)):
            entry = bfs_tree.get(pi)
            if entry is not None and entry[0] == stationary_idx:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: piece_mi_list[{pi}]"
                    f" (neighbour of fixed p{stationary_idx})"
                    f" = {piece_mi_list[pi]}"
                    f" strip={pi in strip_pieces}\n")
            # Also log if entry[2] is wedge that connects to fixed
            if (entry is not None and entry[2] is not None
                    and entry[0] == stationary_idx):
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: p{pi} reaches fixed"
                    f" via wedge p{entry[2]}"
                    f" crossed={sorted(entry[1])}\n")

        # Track accumulated transform per piece (for virtual_plc).
        piece_plc = [FreeCAD.Placement() for _ in range(len(pieces))]
        # Save original bend placements before Phase 3 modifies them.
        bend_plc_original = {}
        for child in obj.Group:
            proxy = getattr(child, 'Proxy', None)
            if proxy and getattr(proxy, 'Type', None) == 'BendLine':
                bend_plc_original[child.Name] = child.Placement.copy()

        micro_pivots = {}  # mi → saved pivot data for wedge loft
        wedge_pre_shapes = {}  # pi → shape copy before this bend's rotation
        wedge_post_mi_plc = {}  # wpi → piece_plc[wpi] after own mi rotation
        mi_wedge_processed = set()  # track first occurrence per mi
        # Build processing schedule: iterate chain positions,
        # collect distinct mi's at each position.
        _schedule = []  # list of (step_pos, mi)
        for _step in range(max_chain_len):
            _mis = sorted(set(
                piece_mi_list[pi][_step]
                for pi in range(len(pieces))
                if _step < len(piece_mi_list[pi])))
            for _mi in _mis:
                _schedule.append((_step, _mi))
        for step_pos, mi in _schedule:
            micro_angle, bend_obj, cut_mid, normal, radius, orig_bi = \
                micro_bend_info[mi]
            if abs(micro_angle) < 1e-9 or not enable_bending \
                    or not bend_obj.Active:
                continue
            first_mi_occurrence = mi not in mi_wedge_processed
            mi_wedge_processed.add(mi)

            plc = bend_obj.Placement

            # Find the stationary-side parent piece of this mi's wedge.
            # Its accumulated piece_plc will be used to build
            # virtual_plc for the cut geometry transform.
            # With per-crossing s/m, the BFS parent side is always
            # stationary, so there is no moving-side entry case.
            s_parent_pi = None
            mi_wpi = None
            for wpi in strip_pieces:
                if strip_to_mi.get(wpi) == mi:
                    mi_wpi = wpi
                    # stationary-parent = neighbor connected via
                    # stationary-side cut mi
                    for nbr, mi_adj, _fi in adjacency[wpi]:
                        if mi_adj == mi:
                            s_parent_pi = nbr
                            break
                    break

            # Build virtual_plc from the stationary-parent's accumulated
            # transform composed with the bend's original placement.
            # This correctly interleaves same-bend and cross-bend
            # rotations (chain composition gets the order wrong
            # because 3D rotations don't commute).
            plc_original = bend_plc_original.get(
                bend_obj.Name, plc)
            if s_parent_pi is not None:
                virtual_plc = piece_plc[s_parent_pi].multiply(
                    plc_original)
            else:
                virtual_plc = plc_original

            cur_normal = virtual_plc.Rotation.multVec(normal)
            cur_up = virtual_plc.Rotation.multVec(up)
            cur_p0 = virtual_plc.multVec(cut_mid)

            bend_axis = cur_up.cross(cur_normal)
            bend_axis.normalize()

            # CoC: inner radius of the fold.
            r_eff_bi = radius + half_t
            bend_sign = -1.0 if micro_angle > 0 else 1.0
            stat_edge_mid = cur_p0 + cur_up * half_t
            pivot = stat_edge_mid + cur_up * (r_eff_bi * bend_sign)
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: mi {mi} CoC:"
                f" pivot=({pivot.x:.4f},{pivot.y:.4f},"
                f"{pivot.z:.4f})\n")

            # Save pivot data for wedge loft (first occurrence only)
            if first_mi_occurrence:
                micro_pivots[mi] = (
                    virtual_plc.copy(),
                    FreeCAD.Vector(cur_p0),
                    FreeCAD.Vector(cur_normal),
                    FreeCAD.Vector(cur_up),
                    FreeCAD.Vector(bend_axis),
                    FreeCAD.Vector(pivot))

            # Save wedge piece shapes NOW (before rotation),
            # so the loft uses shapes in the same space as
            # micro_pivots.  Subsequent micro-bends will rotate
            # piece_shapes further, creating a mismatch.
            # Only on first occurrence of this mi (subsequent
            # occurrences are rotation-only).
            if first_mi_occurrence:
                for wpi in strip_pieces:
                    if strip_to_mi.get(wpi) == mi:
                        wedge_pre_shapes[wpi] = \
                            piece_shapes[wpi].copy()

            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: micro {mi}:"
                f" angle={math.degrees(micro_angle):.1f}°,"
                f" orig_bi={orig_bi},"
                f" pivot={pivot}, axis={bend_axis}\n")

            # Rotate pieces by full angle around CoC.
            rot = FreeCAD.Rotation(
                bend_axis, math.degrees(micro_angle))
            plc_rot = FreeCAD.Placement(
                FreeCAD.Vector(0, 0, 0), rot, pivot)
            rotated_pis = []
            for pi in range(len(piece_shapes)):
                if not _at_step(pi, step_pos, mi):
                    continue
                pre_cm = piece_shapes[pi].CenterOfMass
                piece_shapes[pi].transformShape(
                    plc_rot.toMatrix())
                piece_plc[pi] = plc_rot.multiply(piece_plc[pi])
                post_cm = piece_shapes[pi].CenterOfMass
                rotated_pis.append(pi)
                # Log z-change for pieces near fixed
                entry_dbg = bfs_tree.get(pi)
                if (entry_dbg is not None
                        and entry_dbg[0] == stationary_idx):
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   mi {mi} rotated"
                        f" p{pi} (fixed-nbr):"
                        f" z {pre_cm.z:.4f}"
                        f" → {post_cm.z:.4f}\n")
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD:   mi {mi} rotated"
                f" {len(rotated_pis)} pieces:"
                f" {rotated_pis[:10]}"
                f"{'...' if len(rotated_pis) > 10 else ''}\n")

            # Save wedge's piece_plc right after its own mi rotation
            # (only on first occurrence of this mi)
            if first_mi_occurrence:
                for wpi in strip_pieces:
                    if strip_to_mi.get(wpi) == mi:
                        wedge_post_mi_plc[wpi] = \
                            piece_plc[wpi].copy()

            # Move bend lines using piece multiplier.
            # Skip the bend line's OWN micro-bend.
            mi_bend_obj_name = bend_obj.Name
            for child in obj.Group:
                if (getattr(getattr(child, 'Proxy', None),
                            'Type', None) != 'BendLine'):
                    continue
                if child.Name not in bendline_bend_sets:
                    continue
                if child.Name == mi_bend_obj_name:
                    continue
                bl_pi = bendline_piece_idx.get(child.Name)
                if bl_pi is not None:
                    bl_mult = int(_at_step(bl_pi, step_pos, mi))
                else:
                    bl_mult = 1 if orig_bi in \
                        bendline_bend_sets[child.Name] else 0
                if bl_mult == 0:
                    continue
                eff_angle = micro_angle
                rot_bl = FreeCAD.Rotation(
                    bend_axis, math.degrees(eff_angle))
                plc_bl = FreeCAD.Placement(
                    FreeCAD.Vector(0, 0, 0), rot_bl, pivot)
                child.Placement = plc_bl.multiply(
                    child.Placement)

            # Move components: same rotation as their piece
            for child in obj.Group:
                cpi = comp_piece_idx.get(child.Name)
                if cpi is None:
                    continue
                if not _at_step(cpi, step_pos, mi):
                    continue
                rot = FreeCAD.Rotation(
                    bend_axis, math.degrees(micro_angle))
                rot_plc = FreeCAD.Placement(
                    FreeCAD.Vector(0, 0, 0), rot, pivot)
                child.Placement = rot_plc.multiply(
                    child.Placement)

            # Inset correction: the wedge wraps around CoC
            # carrying the moving part, but pieces were rotated
            # from their flat M-edge position.  Apply correction
            # NOW (inside Phase 3 loop) so subsequent rotations
            # carry it along in the correct frame.
            ins_bi = insets[orig_bi]
            if ins_bi > 1e-6:
                _, s_p0, s_normal, s_up, s_axis, s_pivot = \
                    micro_pivots[mi]
                coc_corr = s_pivot
                mi_angle_corr = micro_angle
                r_eff_corr = radius + half_t

                mid_stat = s_p0 + s_up * half_t
                mid_flat = s_p0 + s_normal * (2 * ins_bi) \
                    + s_up * half_t

                rot_full = FreeCAD.Rotation(
                    s_axis, math.degrees(mi_angle_corr))
                plc_full = FreeCAD.Placement(
                    FreeCAD.Vector(0, 0, 0), rot_full,
                    coc_corr)
                mid_expected = plc_full.multVec(mid_stat)
                mid_actual = plc_full.multVec(mid_flat)
                correction = mid_expected - mid_actual

                if correction.Length > 1e-6:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: correction mi {mi}"
                        f" (bend {orig_bi}):"
                        f" ins={ins_bi:.4f}"
                        f" r_eff={r_eff_corr:.4f}"
                        f" angle="
                        f"{math.degrees(mi_angle_corr):.1f}°"
                        f" |corr|="
                        f"{correction.Length:.4f}"
                        f" vec=({correction.x:.4f},"
                        f"{correction.y:.4f},"
                        f"{correction.z:.4f})\n")

                    corr_plc = FreeCAD.Placement(
                        correction, FreeCAD.Rotation())

                    for pi in range(len(piece_shapes)):
                        is_own_wedge = (
                            pi in strip_pieces
                            and strip_to_mi.get(pi) == mi)
                        if is_own_wedge:
                            continue
                        if not _at_step(pi, step_pos, mi):
                            continue
                        piece_shapes[pi].translate(correction)
                        piece_plc[pi] = corr_plc.multiply(
                            piece_plc[pi])

                    # Correct bend lines
                    mi_bend_obj_name2 = bend_obj.Name
                    for child in obj.Group:
                        if (getattr(getattr(
                                child, 'Proxy', None),
                                'Type', None) != 'BendLine'):
                            continue
                        if child.Name == mi_bend_obj_name2:
                            continue
                        bl_pi = bendline_piece_idx.get(
                            child.Name)
                        if bl_pi is None:
                            continue
                        bl_mult = int(
                            _at_step(bl_pi, step_pos, mi))
                        if bl_mult > 0:
                            child.Placement.Base = \
                                child.Placement.Base \
                                + correction

                    # Correct components
                    for child in obj.Group:
                        cpi = comp_piece_idx.get(child.Name)
                        if cpi is None:
                            continue
                        if not _at_step(cpi, step_pos, mi):
                            continue
                        child.Placement.Base = \
                            child.Placement.Base + correction

        # Log final positions after all transforms
        for pi in range(len(piece_shapes)):
            s = piece_shapes[pi]
            if s.isValid() and s.Volume > 1e-6:
                orig = pieces[pi].CenterOfMass
                final = s.CenterOfMass
                dist = orig.distanceToPoint(final)
                if dist > GEOMETRY_TOLERANCE:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: piece {pi} moved"
                        f" {dist:.3f}mm to"
                        f" ({final.x:.2f},{final.y:.2f},"
                        f"{final.z:.2f})\n")

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Phase 3 (rotation): "
            f"{_time.time() - _t_phase3:.3f}s\n")
        # Bend wedge pieces into arcs via loft between
        # rotated cross-sections.
        _t_loft = _time.time()
        smooth_wedge = getattr(obj, 'SmoothWedge', False)
        # N_SLICES per wedge: at least 16, or 1 per degree
        # (computed per wedge below)
        coc_offsets = {}  # bi → (bend_obj, first_s_mi)
        for pi in sorted(strip_to_bend):
            _t_loft_one = _time.time()
            bi = strip_to_bend[pi]
            _, p0_bi, p1_bi, line_dir_bi, normal_bi, \
                angle_rad_bi, radius_bi = bend_info[bi]
            ins = insets[bi]
            abs_a = abs(angle_rad_bi)
            r_eff = radius_bi + half_t
            N_SLICES = max(
                int(math.ceil(
                    abs(math.degrees(angle_rad_bi)) / 4)),
                8)

            # Find the S micro-bend for this specific wedge piece
            s_mi = strip_to_mi.get(pi)
            if s_mi is None:
                continue
            micro_angle_s = micro_bend_info[s_mi][0]

            # Use saved pivot data from Phase 3
            saved = micro_pivots.get(s_mi)
            if saved is None:
                continue
            saved_plc, cur_p0, cur_normal, cur_up, bend_axis, \
                saved_pivot = saved
            # Phase 3 already built the chain-based virtual_plc.
            # Use saved values directly — no re-composition needed.
            pivot = saved_pivot
            bend_obj_bi = bend_info[bi][0]

            # Per-segment CoC: find nearest segment for multi-seg
            seg_mids = bend_seg_mids.get(bi, [])
            if len(seg_mids) > 1:
                piece_cm = pieces[pi].CenterOfMass
                best_mid = None
                best_d = float('inf')
                for sm in seg_mids:
                    d = piece_cm.distanceToPoint(sm)
                    if d < best_d:
                        best_d = d
                        best_mid = sm
                cur_p0 = saved_plc.multVec(best_mid)
                bs = -1.0 if micro_angle_s > 0 else 1.0
                stat_edge_mid = cur_p0 + cur_up * half_t
                pivot = stat_edge_mid + cur_up * (r_eff * bs)

            coc = saved_pivot

            sweep_angle = micro_angle_s

            # Save CoC offset for bend line (applied after lofts)
            if bi not in coc_offsets:
                coc_offsets[bi] = (bend_obj_bi, s_mi)

            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: wedge pi={pi} bi={bi}"
                f" sweep={math.degrees(sweep_angle):.1f}°"
                f" s_mi={s_mi}"
                f" ins={ins:.4f}"
                f" r_eff={r_eff:.4f}"
                f" segs={len(seg_mids)}"
                f" pre_vol={piece_shapes[pi].Volume:.4f}"
                f" cur_p0=({cur_p0.x:.2f},{cur_p0.y:.2f},{cur_p0.z:.2f})"
                f" normal=({cur_normal.x:.3f},{cur_normal.y:.3f},{cur_normal.z:.3f})"
                f" up=({cur_up.x:.3f},{cur_up.y:.3f},{cur_up.z:.3f})"
                f" axis=({bend_axis.x:.3f},{bend_axis.y:.3f},{bend_axis.z:.3f})"
                f" coc=({coc.x:.2f},{coc.y:.2f},{coc.z:.2f})"
                f"\n")

            # Count how many distinct mi's of this bend the piece crosses
            piece_mi_set_pi = set(piece_mi_list[pi])
            s_mult = sum(1 for mi_chk in bend_s_mis.get(bi, [])
                         if mi_chk in piece_mi_set_pi)
            positioned_flat = wedge_pre_shapes[pi].copy() \
                if pi in wedge_pre_shapes \
                else piece_shapes[pi].copy()

            FreeCAD.Console.PrintMessage(
                f"FreekiCAD:   s_mult={s_mult}"
                f" micro_angle={math.degrees(micro_angle_s):.1f}°"
                f" pre_shape={'Y' if pi in wedge_pre_shapes else 'N'}"
                f" piece_cm=({piece_shapes[pi].CenterOfMass.x:.2f}"
                f",{piece_shapes[pi].CenterOfMass.y:.2f}"
                f",{piece_shapes[pi].CenterOfMass.z:.2f})"
                f"\n")

            # Slice the flat wedge at each position, then
            # move each slice to its arc position.
            # For each slice at fraction frac:
            #   1. Slice positioned_flat at d along cur_normal
            #   2. Compute arc position at angle frac*sweep
            #   3. Translate slice center to arc position
            #   4. Rotate slice to be tangent to arc
            # Build sorted list of d-values: uniform slices +
            # vertex projections (ensures loft captures all edges
            # of the flat wedge piece).
            gt = GEOMETRY_TOLERANCE
            # Build uniform d-values
            d_uniform = []
            for si in range(N_SLICES + 1):
                frac = si / float(N_SLICES)
                d_uniform.append(
                    gt + frac * (2 * ins - 2 * gt))
            # Collect vertex projection d-values (split points)
            vertex_proj_ds = []
            for v in positioned_flat.Vertexes:
                vd = (v.Point - cur_p0).dot(cur_normal)
                if gt < vd < 2 * ins - gt:
                    vertex_proj_ds.append(vd)
            vertex_proj_ds = sorted(set(vertex_proj_ds))

            # Split the d-range at vertex projection planes
            # so each sub-range has consistent cross-section
            # topology.  We slice the original solid for each
            # sub-range (no half-space cutting needed).
            split_ds = vertex_proj_ds  # d-values to split at
            d_lo = gt
            d_hi = 2 * ins - gt
            # Dedup split_ds within tolerance
            if split_ds:
                deduped = [split_ds[0]]
                for sd in split_ds[1:]:
                    if sd - deduped[-1] > gt:
                        deduped.append(sd)
                split_ds = deduped
            sub_ranges = []  # (d_start, d_end) for each sub
            if split_ds:
                bounds = [d_lo] + list(split_ds) + [d_hi]
                for k in range(len(bounds) - 1):
                    if bounds[k + 1] - bounds[k] > gt:
                        sub_ranges.append(
                            (bounds[k], bounds[k + 1]))
            else:
                sub_ranges = [(d_lo, d_hi)]

            FreeCAD.Console.PrintMessage(
                f"FreekiCAD:   {len(sub_ranges)} sub-range(s)"
                f" at split ds="
                f"{[f'{d:.6f}' for d in split_ds]}\n")

            # For each sub-range, generate d-values, slice
            # the original solid, transform, and collect wires.
            min_sep = max(gt * 2, (2 * ins) / 1000)
            segments = []
            all_wires_flat = []  # for debug logging

            for si_sub, (sr_lo, sr_hi) in \
                    enumerate(sub_ranges):
                # Collect d-values in this sub-range
                seg_ds = []
                for du in d_uniform:
                    if sr_lo - gt <= du <= sr_hi + gt:
                        seg_ds.append(du)
                # Add boundary d-values
                if not seg_ds or seg_ds[0] > sr_lo + min_sep:
                    seg_ds.insert(0, sr_lo + gt * 0.5)
                if not seg_ds or seg_ds[-1] < sr_hi - min_sep:
                    seg_ds.append(sr_hi - gt * 0.5)
                # Deduplicate
                seg_ds_dedup = [seg_ds[0]]
                for sd in seg_ds[1:]:
                    if sd - seg_ds_dedup[-1] >= min_sep:
                        seg_ds_dedup.append(sd)
                seg_ds = seg_ds_dedup

                seg_wires = []
                for d in seg_ds:
                    frac = (d - gt) / (2 * ins - 2 * gt) \
                        if abs(2 * ins - 2 * gt) > 1e-9 \
                        else 0.0
                    slice_pt = cur_p0 + cur_normal * d
                    plane_dist = (
                        slice_pt.x * cur_normal.x
                        + slice_pt.y * cur_normal.y
                        + slice_pt.z * cur_normal.z)
                    try:
                        wires = positioned_flat.slice(
                            cur_normal, plane_dist)
                    except Exception:
                        wires = []
                    if not wires:
                        continue

                    w = wires[0].copy()
                    slice_angle = frac * sweep_angle

                    # Translate back then rotate to arc
                    trans_vec = cur_normal * (-d)
                    w.translate(trans_vec)
                    if abs(slice_angle) > 1e-9:
                        rot_s = FreeCAD.Rotation(
                            bend_axis,
                            math.degrees(slice_angle))
                        plc_s = FreeCAD.Placement(
                            FreeCAD.Vector(0, 0, 0),
                            rot_s, coc)
                        w.transformShape(
                            plc_s.toMatrix())

                    seg_wires.append(w)
                    all_wires_flat.append(w)

                if len(seg_wires) < 2:
                    continue

                # Reorder vertices within this segment
                ref_pts = [v.Point
                           for v in seg_wires[0].Vertexes]
                n_v = len(ref_pts)
                for wi in range(1, len(seg_wires)):
                    w = seg_wires[wi]
                    cur_pts = [v.Point
                               for v in w.Vertexes]
                    if len(cur_pts) != n_v:
                        continue
                    best_dist = None
                    best_pts = cur_pts
                    for pts_order in (
                            cur_pts,
                            list(reversed(cur_pts))):
                        for rot in range(n_v):
                            rotated = (
                                pts_order[rot:]
                                + pts_order[:rot])
                            total = sum(
                                (rotated[k]
                                 - ref_pts[k]).Length
                                for k in range(n_v))
                            if (best_dist is None
                                    or total
                                    < best_dist):
                                best_dist = total
                                best_pts = rotated
                    if any(
                        (best_pts[k]
                         - cur_pts[k]).Length > 1e-9
                            for k in range(n_v)):
                        poly_pts = (list(best_pts)
                                    + [best_pts[0]])
                        seg_wires[wi] = Part.makePolygon(
                            poly_pts)
                    ref_pts = best_pts
                segments.append(seg_wires)

            wire_edges = [len(w.Edges)
                          for w in all_wires_flat]
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD:   slices={len(all_wires_flat)}"
                f" edges={wire_edges}\n")

            if segments:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   loft segments="
                    f"{len(segments)} splits_at="
                    f"{[f'{d:.6f}' for d in split_ds]}\n")

                def _try_loft(segs, label):
                    """Try to build a loft from segments.
                    Returns the loft Shape or None."""
                    loft_parts = []
                    for seg in segs:
                        if len(seg) < 2:
                            continue
                        part = Part.makeLoft(
                            seg, True, not smooth_wedge)
                        if abs(part.Volume) > 1e-9:
                            if part.Volume < 0:
                                part = part.reversed()
                            loft_parts.append(part)
                    if not loft_parts:
                        return None
                    if len(loft_parts) == 1:
                        loft = loft_parts[0]
                    else:
                        loft = loft_parts[0].fuse(
                            loft_parts[1:])
                    vol = loft.Volume
                    if abs(vol) <= 1e-9:
                        return None
                    if vol < 0:
                        loft = loft.reversed()
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   {label} loft ok"
                        f" vol={loft.Volume:.4f}\n")
                    return loft

                loft = None
                try:
                    loft = _try_loft(segments, "segmented")
                except Exception as e:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD: segmented loft failed"
                        f" for piece {pi}: {e}\n")

                if loft is not None:
                    # Apply remaining Phase 3
                    # rotations: the loft was built
                    # in the pre-mi frame; use the
                    # wedge's own absolute chain to
                    # catch subsequent rotations.
                    if pi in wedge_post_mi_plc:
                        remaining_plc = piece_plc[
                            pi].multiply(
                            wedge_post_mi_plc[
                                pi].inverse())
                        ra = remaining_plc.Rotation.Angle
                        rb = remaining_plc.Base.Length
                        if ra > 1e-6 or rb > 1e-6:
                            loft.transformShape(
                                remaining_plc.toMatrix())
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD: wedge pi={pi}"
                                f" remaining_rot="
                                f"{math.degrees(ra):.1f}°"
                                f" remaining_trans="
                                f"{rb:.3f}\n")
                    piece_shapes[pi] = loft
                    # Compute CenterOfMass safely:
                    # fuse() may return a Compound
                    # instead of a Solid.
                    try:
                        cm = loft.CenterOfMass
                    except AttributeError:
                        solids = loft.Solids
                        if solids:
                            tv = sum(
                                abs(s.Volume)
                                for s in solids)
                            if tv > 1e-12:
                                cm = FreeCAD.Vector()
                                for s in solids:
                                    w = abs(s.Volume) / tv
                                    cm += s.CenterOfMass * w
                            else:
                                cm = FreeCAD.Vector()
                        else:
                            cm = FreeCAD.Vector()
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: wedge loft solid:"
                        f" post_vol={loft.Volume:.4f}"
                        f" wedge_cm="
                        f"({cm.x:.2f}"
                        f",{cm.y:.2f}"
                        f",{cm.z:.2f})"
                        f"\n")
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: [profile] wedge p{pi}"
                f" loft: {_time.time() - _t_loft_one:.3f}s\n")

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Wedge loft: "
            f"{_time.time() - _t_loft:.3f}s\n")

        # Move bend lines to first stationary cut line's CoC (#9,
        # visual only, no effect on other geometry).
        for bi, (bl_obj, first_mi) in coc_offsets.items():
            saved = micro_pivots.get(first_mi)
            if saved is None:
                continue

            # Find the first wedge piece for this bend
            wedge_pi = None
            for wpi in strip_pieces:
                if strip_to_mi.get(wpi) == first_mi:
                    wedge_pi = wpi
                    break

            if wedge_pi is None:
                continue

            # Move bend line to center of first wedge at half thickness
            ws = piece_shapes[wedge_pi]
            if not hasattr(ws, 'CenterOfMass'):
                continue
            wedge_cm = ws.CenterOfMass
            _, p0_bi, _, _, normal_bi, _, _ = bend_info[bi]
            final_plc = bl_obj.Placement
            final_bend_p0 = final_plc.multVec(p0_bi)
            final_up = final_plc.Rotation.multVec(up)
            final_center = final_bend_p0 + final_up * half_t

            # Project offset perpendicular to bend line direction
            # so the line moves to the wedge's x/z position but
            # stays centered along its own length.
            target = FreeCAD.Vector(wedge_cm.x, wedge_cm.y,
                                    wedge_cm.z)
            offset_vec = target - final_center
            _, _, p1_bi, _, _, _, _ = bend_info[bi]
            world_dir = final_plc.Rotation.multVec(
                p1_bi - p0_bi)
            dl = world_dir.Length
            if dl > 1e-9:
                world_dir = world_dir * (1.0 / dl)
                along = offset_vec.dot(world_dir)
                offset_vec = offset_vec - world_dir * along
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: bendline {bl_obj.Name} (mi={first_mi})"
                f" wedge_cm=({target.x:.2f},{target.y:.2f},"
                f"{target.z:.2f})"
                f" off=({offset_vec.x:.3f},{offset_vec.y:.3f},"
                f"{offset_vec.z:.3f})\n")
            new_base = bl_obj.Placement.Base + offset_vec
            bl_obj.Placement = FreeCAD.Placement(
                new_base, bl_obj.Placement.Rotation)

        # Draw debug visualizations if enabled
        show_debug = getattr(obj, 'BuildDebugObjects', False)
        if show_debug and pieces:
            self._draw_debug_arrows(
                obj, pieces, piece_bend_sets, bfs_tree,
                strip_pieces, strip_to_bend,
                bend_info, insets, half_t,
                micro_bend_info, bendline_piece_idx,
                mi_seg_idx=mi_seg_idx,
                m_face_to_bend=m_face_to_bend,
                piece_shapes=piece_shapes)
            self._draw_debug_cuts(
                obj, cut_plan, thickness,
                bend_plc_original=bend_plc_original,
                face_to_seg=face_to_seg)
        else:
            # Log piece classification even without debug shapes
            for pi in range(len(pieces)):
                cm = pieces[pi].CenterOfMass
                if pi in strip_pieces:
                    label = f"p{pi}_W{strip_to_bend[pi]}"
                elif not piece_bend_sets[pi]:
                    label = f"p{pi}_F"
                else:
                    bends = sorted(piece_bend_sets[pi])
                    label = f"p{pi}_M" + ",".join(
                        str(b) for b in bends)
                # Build path notation
                path = []
                cur = pi
                while cur is not None:
                    entry = bfs_tree.get(cur)
                    if entry is None:
                        break
                    parent = entry[0]
                    mis_crossed = entry[1]  # set of mi indices
                    if parent is not None:
                        for bi_crossed in sorted(mis_crossed):
                            if bi_crossed >= 0:
                                orig_bi = micro_bend_info[
                                    bi_crossed][5]
                                seg = mi_seg_idx.get(
                                    bi_crossed, 0)
                                path.append(
                                    f"{orig_bi}.{seg}A")
                            elif (bi_crossed <= -2
                                    and m_face_to_bend):
                                b, s = m_face_to_bend.get(
                                    bi_crossed,
                                    (-bi_crossed-2, 0))
                                path.append(f"{b}.{s}B")
                            elif bi_crossed <= -2:
                                path.append(
                                    f"{-bi_crossed-2}B")
                    cur = parent
                path.reverse()
                # Collapse consecutive same-bend B crossings.
                cleaned = []
                for p in path:
                    if (cleaned and cleaned[-1] == p
                            and p.endswith('B')):
                        cleaned.pop()
                    else:
                        cleaned.append(p)
                path_str = "/".join(cleaned) if cleaned \
                    else "(root)"
                # Find source bend line name
                bl_name = ""
                for child in obj.Group:
                    if (getattr(getattr(child, 'Proxy', None),
                                'Type', None) != 'BendLine'):
                        continue
                    bl_pi = bendline_piece_idx.get(child.Name)
                    if bl_pi == pi:
                        bl_name = f" bl={child.Name}"
                        break
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: piece {pi}: {label}"
                    f" path=[{path_str}]"
                    f" vol={pieces[pi].Volume:.4f}"
                    f" cm=({cm.x:.2f},{cm.y:.2f},{cm.z:.2f})"
                    f"{bl_name}\n")

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Correction + assembly: "
            f"{_time.time() - _t_loft:.3f}s\n")
        # Update board shape with all pieces (including bent wedges)
        _t_final = _time.time()
        if enable_bending:
            saved_color = None
            try:
                saved_color = board_obj.ViewObject.ShapeColor
            except Exception:
                pass

            board_obj.Shape = Part.makeCompound(
                [s for s in piece_shapes if s.isValid()])

            if saved_color:
                try:
                    board_obj.ViewObject.ShapeColor = saved_color
                except Exception:
                    pass

        # Hide assembled board when DebugBoard is enabled
        debug_board = getattr(obj, 'DebugBoard', False)
        if hasattr(board_obj, 'ViewObject') \
                and board_obj.ViewObject is not None:
            board_obj.ViewObject.Visibility = not debug_board

        # Debug board: create child objects for each piece
        debug_grp_name = obj.Name + "_DebugPieces"
        doc = obj.Document
        # Remove old debug pieces
        old_grp = doc.getObject(debug_grp_name)
        if old_grp:
            for child in old_grp.Group:
                doc.removeObject(child.Name)
            doc.removeObject(debug_grp_name)
        if debug_board and pieces:
            grp = doc.addObject(
                "App::DocumentObjectGroup", debug_grp_name)
            if grp not in obj.Group:
                obj.addObject(grp)
            for pi in range(len(piece_shapes)):
                s = piece_shapes[pi]
                if not s.isValid():
                    continue
                # Build label and path
                if pi in strip_pieces:
                    lbl = f"W{strip_to_bend[pi]}"
                elif not piece_bend_sets[pi]:
                    lbl = "F"
                else:
                    bends = sorted(piece_bend_sets[pi])
                    lbl = "M" + ",".join(str(b) for b in bends)
                path = []
                cur = pi
                while cur is not None:
                    entry = bfs_tree.get(cur)
                    if entry is None:
                        break
                    parent = entry[0]
                    mis_crossed = entry[1]  # set of mi indices
                    if parent is not None:
                        for bi_crossed in sorted(mis_crossed):
                            if bi_crossed >= 0:
                                obi = micro_bend_info[
                                    bi_crossed][5]
                                seg = mi_seg_idx.get(
                                    bi_crossed, 0)
                                path.append(
                                    f"{obi}.{seg}A")
                            elif (bi_crossed <= -2
                                    and m_face_to_bend):
                                mb, ms = m_face_to_bend.get(
                                    bi_crossed,
                                    (-bi_crossed-2, 0))
                                path.append(
                                    f"{mb}.{ms}B")
                            elif bi_crossed <= -2:
                                path.append(
                                    f"{-bi_crossed-2}B")
                    cur = parent
                path.reverse()
                path_str = "/".join(path) if path \
                    else "(root)"
                pname = f"{debug_grp_name}_{pi}"
                pobj = doc.addObject("Part::Feature", pname)
                pobj.Shape = s
                pobj.Label = f"p{pi}_{lbl}"
                # Add readonly properties
                if not hasattr(pobj, 'PieceName'):
                    pobj.addProperty(
                        "App::PropertyString", "PieceName",
                        "Debug", "Piece classification")
                    pobj.setPropertyStatus("PieceName",
                                           "ReadOnly")
                pobj.PieceName = f"p{pi}_{lbl}"
                if not hasattr(pobj, 'Chain'):
                    pobj.addProperty(
                        "App::PropertyString", "Chain",
                        "Debug", "BFS path from root")
                    pobj.setPropertyStatus("Chain",
                                           "ReadOnly")
                pobj.Chain = path_str
                if not hasattr(pobj, 'Parent'):
                    pobj.addProperty(
                        "App::PropertyString", "Parent",
                        "Debug", "BFS parent piece index")
                    pobj.setPropertyStatus("Parent",
                                           "ReadOnly")
                entry_pi = bfs_tree.get(pi)
                if entry_pi is not None and entry_pi[0] is not None:
                    pobj.Parent = f"p{entry_pi[0]}"
                else:
                    pobj.Parent = "(root)"
                if pi in strip_pieces:
                    # Wedge: also show cut segment id
                    bi_w = strip_to_bend[pi]
                    if not hasattr(pobj, 'Cut'):
                        pobj.addProperty(
                            "App::PropertyString", "Cut",
                            "Debug",
                            "Cut segment this wedge sits on")
                        pobj.setPropertyStatus("Cut",
                                               "ReadOnly")
                    seg = mi_seg_idx.get(
                        strip_to_mi.get(pi, -1), 0)
                    pobj.Cut = f"{bi_w}.{seg}"
                grp.addObject(pobj)

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Final assembly: "
            f"{_time.time() - _t_final:.3f}s\n")
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] TOTAL __apply_bends_impl: "
            f"{_time.time() - _t0_total:.3f}s\n")

    def _draw_debug_cuts(self, obj, debug_cut_segs, thickness,
                          bend_plc_original=None,
                          face_to_seg=None):
        """Draw trimmed cutting segments as individual objects.

        Each cut segment becomes its own Part::Feature with
        properties: Side (A/B/C), Bend (index), Segment (sid),
        and a label like 'cut0_3.1A' (cut index, bend.segment, side).
        Colors: green=A-side, blue=B-side, cyan=center.
        Group hidden by default; individual objects visible.
        """
        doc = obj.Document
        debug_grp_name = obj.Name + "_DebugCuts"

        # Remove old group and children
        old_grp = doc.getObject(debug_grp_name)
        if old_grp:
            for child in old_grp.Group:
                doc.removeObject(child.Name)
            doc.removeObject(debug_grp_name)

        if not debug_cut_segs:
            return

        grp = doc.addObject(
            "App::DocumentObjectGroup", debug_grp_name)
        if grp not in obj.Group:
            obj.addObject(grp)
        try:
            grp.ViewObject.Visibility = False
        except Exception:
            pass

        side_colors = {
            'A': (0.0, 1.0, 0.0),   # green
            'B': (0.0, 0.0, 1.0),   # blue
            'C': (0.0, 1.0, 1.0),   # cyan
        }

        for ci, entry in enumerate(debug_cut_segs):
            sp0, sp1, side = entry[0], entry[1], entry[2]
            bi = entry[3] if len(entry) > 3 else -1
            # Transform cut endpoints using bend placement chain
            if bend_plc_original is not None and len(entry) > 8:
                bend_obj = entry[8]
                orig_plc = bend_plc_original.get(
                    bend_obj.Name)
                if orig_plc is not None:
                    xform = bend_obj.Placement.multiply(
                        orig_plc.inverse())
                    sp0 = xform.multVec(sp0)
                    sp1 = xform.multVec(sp1)
            start = FreeCAD.Vector(
                sp0.x, sp0.y, sp0.z + thickness)
            end = FreeCAD.Vector(
                sp1.x, sp1.y, sp1.z + thickness)
            if start.distanceToPoint(end) <= GEOMETRY_TOLERANCE:
                continue

            edge = Part.makeLine(start, end)
            pname = f"{debug_grp_name}_{ci}"
            pobj = doc.addObject("Part::Feature", pname)
            pobj.Shape = edge
            sid = (face_to_seg.get(ci, -1)
                   if face_to_seg else -1)
            pobj.Label = f"cut{ci}_{bi}.{sid}{side}"

            if not hasattr(pobj, 'Side'):
                pobj.addProperty(
                    "App::PropertyString", "Side",
                    "Debug", "Cut side (A/B/C)")
                pobj.setPropertyStatus("Side", "ReadOnly")
            pobj.Side = side

            if not hasattr(pobj, 'Bend'):
                pobj.addProperty(
                    "App::PropertyInteger", "Bend",
                    "Debug", "Bend index")
                pobj.setPropertyStatus("Bend", "ReadOnly")
            pobj.Bend = bi
            if not hasattr(pobj, 'Segment'):
                pobj.addProperty(
                    "App::PropertyInteger", "Segment",
                    "Debug", "Segment ID (sid)")
                pobj.setPropertyStatus("Segment", "ReadOnly")
            pobj.Segment = sid

            try:
                color = side_colors.get(side, (1.0, 1.0, 1.0))
                pobj.ViewObject.LineColor = color
                pobj.ViewObject.LineWidth = 3.0
                pobj.ViewObject.Visibility = True
            except Exception:
                pass

            grp.addObject(pobj)

    def _trim_line_to_outline(self, p0, p1, board_face):
        """Trim a 2D line to the board face using BRep section.

        Uses FreeCAD's geometry kernel to compute exact intersection
        of the line with the board face — no ray-casting or even/odd
        pairing issues.

        Returns a list of (seg_p0, seg_p1) pairs.  Each segment is
        extended by 1 µm at both ends for numerical safety.
        """
        line_dir = p1 - p0
        line_len = line_dir.Length
        if line_len < 1e-9:
            return []

        if board_face is None:
            return []

        # Create an edge from p0 to p1 at z=0
        edge = Part.makeLine(
            FreeCAD.Vector(p0.x, p0.y, 0),
            FreeCAD.Vector(p1.x, p1.y, 0))

        # Section: intersection of line with board face gives
        # edges where the line is inside the face.
        try:
            common = edge.common(board_face)
        except Exception:
            return []

        if not common.Edges:
            return []

        # Extract segments from the result edges
        segments = []
        unit = line_dir * (1.0 / line_len)
        for e in common.Edges:
            vs = e.Vertexes
            if len(vs) < 2:
                continue
            sp = FreeCAD.Vector(vs[0].Point.x, vs[0].Point.y, 0)
            ep = FreeCAD.Vector(vs[1].Point.x, vs[1].Point.y, 0)
            seg_dir = ep - sp
            seg_len = seg_dir.Length
            if seg_len < 1e-6:
                continue
            # Ensure segment direction matches line direction
            if seg_dir.dot(line_dir) < 0:
                sp, ep = ep, sp
                seg_dir = ep - sp
            ext = seg_dir * (GEOMETRY_TOLERANCE / seg_len)
            segments.append((sp - ext, ep + ext))

        # Sort by position along the line
        segments.sort(key=lambda s: (s[0] - p0).dot(unit))

        # Keep only segments that cross the board: both endpoints
        # must lie on (or very near) the outline wire.
        wire = board_face.OuterWire
        crossing = []
        for sp, ep in segments:
            sv = Part.Vertex(FreeCAD.Vector(sp.x, sp.y, 0))
            ev = Part.Vertex(FreeCAD.Vector(ep.x, ep.y, 0))
            if sv.distToShape(wire)[0] < 0.1 \
                    and ev.distToShape(wire)[0] < 0.1:
                crossing.append((sp, ep))
        return crossing

    def _draw_debug_arrows(self, obj, pieces, piece_bend_sets,
                            bfs_tree, strip_pieces, strip_to_bend,
                            bend_info, insets, half_t,
                            micro_bend_info=None,
                            bendline_piece_idx=None,
                            mi_seg_idx=None,
                            m_face_to_bend=None,
                            piece_shapes=None):
        """Draw debug arrows showing the BFS tree from fixed to
        moving pieces.  Each piece is labeled fixed/moving/wedge."""
        doc = obj.Document
        up = FreeCAD.Vector(0, 0, 1)
        thickness = half_t * 2

        edges = []
        labels = []  # (center, label_text)

        def _get_centroid(pi_idx):
            """Get a representative point inside the piece.

            Uses CenterOfMass if it lies inside the shape;
            otherwise returns the closest point on the shape
            to the CenterOfMass."""
            if (piece_shapes is not None
                    and 0 <= pi_idx < len(piece_shapes)):
                s = piece_shapes[pi_idx]
                if hasattr(s, 'CenterOfMass'):
                    cm = s.CenterOfMass
                    if s.isInside(cm, GEOMETRY_TOLERANCE, True):
                        return cm
                    try:
                        d, pts, _ = s.distToShape(
                            Part.Vertex(cm))
                        return pts[0][0]
                    except Exception:
                        return cm
            cm = pieces[pi_idx].CenterOfMass
            s = pieces[pi_idx]
            if s.isInside(cm, GEOMETRY_TOLERANCE, True):
                return cm
            try:
                d, pts, _ = s.distToShape(Part.Vertex(cm))
                return pts[0][0]
            except Exception:
                return cm

        def _single_arrow(start, end):
            """Draw line with single arrowhead at end."""
            dist = start.distanceToPoint(end)
            if dist < GEOMETRY_TOLERANCE:
                return
            edges.append(Part.makeLine(start, end))
            d = (end - start) * (1.0 / dist)
            hl = min(0.3, dist * 0.2)
            perp = FreeCAD.Vector(-d.y, d.x, 0)
            base = end - d * hl
            edges.append(Part.makeLine(
                end, base + perp * hl * 0.4))
            edges.append(Part.makeLine(
                end, base - perp * hl * 0.4))

        def _double_arrow(start, end):
            """Draw line with double arrowhead at end."""
            dist = start.distanceToPoint(end)
            if dist < GEOMETRY_TOLERANCE:
                return
            edges.append(Part.makeLine(start, end))
            d = (end - start) * (1.0 / dist)
            hl = min(0.3, dist * 0.2)
            perp = FreeCAD.Vector(-d.y, d.x, 0)
            # First arrowhead at tip
            base1 = end - d * hl
            edges.append(Part.makeLine(
                end, base1 + perp * hl * 0.4))
            edges.append(Part.makeLine(
                end, base1 - perp * hl * 0.4))
            # Second arrowhead behind first
            tip2 = end - d * hl * 0.8
            base2 = tip2 - d * hl
            edges.append(Part.makeLine(
                tip2, base2 + perp * hl * 0.4))
            edges.append(Part.makeLine(
                tip2, base2 - perp * hl * 0.4))

        for pi, piece in enumerate(pieces):
            cm = _get_centroid(pi)
            # Classify piece
            if pi in strip_pieces:
                label = f"W{strip_to_bend[pi]}"
            elif not piece_bend_sets[pi]:
                label = "F"
            else:
                bends = sorted(piece_bend_sets[pi])
                label = "M" + ",".join(str(b) for b in bends)
            labels.append((cm, label))

            # Draw arrows for BFS tree edges
            entry = bfs_tree.get(pi)
            if entry and entry[0] is not None:
                parent_idx = entry[0]
                wedge_pi = entry[2]
                parent_cm = _get_centroid(parent_idx)
                src = FreeCAD.Vector(
                    parent_cm.x, parent_cm.y,
                    parent_cm.z + thickness)
                dst = FreeCAD.Vector(
                    cm.x, cm.y, cm.z + thickness)
                if wedge_pi is not None:
                    # src → wedge (single), wedge →→ dest (double)
                    w_cm = _get_centroid(wedge_pi)
                    mid = FreeCAD.Vector(
                        w_cm.x, w_cm.y,
                        w_cm.z + thickness)
                    _single_arrow(src, mid)
                    _double_arrow(mid, dst)
                else:
                    _single_arrow(src, dst)

        # Circle at BFS root (stationary/fixed piece)
        for pi, piece in enumerate(pieces):
            entry = bfs_tree.get(pi)
            if entry and entry[0] is None:
                root_cm = _get_centroid(pi)
                root_pt = FreeCAD.Vector(
                    root_cm.x, root_cm.y,
                    root_cm.z + thickness)
                try:
                    circ = Part.makeCircle(
                        0.5, root_pt, FreeCAD.Vector(0, 0, 1))
                    edges.append(circ)
                except Exception:
                    pass
                break

        # Reuse existing debug arrows object or create new one.
        debug_name = obj.Name + "_DebugArrows"
        debug_obj = doc.getObject(debug_name)
        if edges:
            if debug_obj is None:
                debug_obj = doc.addObject(
                    "Part::Feature", debug_name)
                obj.addObject(debug_obj)
                try:
                    debug_obj.ViewObject.LineColor = (
                        1.0, 0.0, 0.0)
                    debug_obj.ViewObject.LineWidth = 2.0
                    debug_obj.ViewObject.Visibility = False
                except Exception:
                    pass
            debug_obj.Shape = Part.makeCompound(edges)
        elif debug_obj is not None:
            debug_obj.Shape = Part.Shape()  # empty

        # Log the classification
        for pi, (cm, label) in enumerate(labels):
            # Build path from stationary in notation like 5A/4B/0A
            path = []
            if micro_bend_info is not None:
                cur = pi
                while cur is not None:
                    entry = bfs_tree.get(cur)
                    if entry is None:
                        break
                    parent = entry[0]
                    mis_crossed = entry[1]  # set of mi indices
                    if parent is not None:
                        for bi_crossed in sorted(mis_crossed):
                            if bi_crossed >= 0:
                                orig_bi = micro_bend_info[
                                    bi_crossed][5]
                                seg = mi_seg_idx.get(
                                    bi_crossed, 0)
                                path.append(
                                    f"{orig_bi}.{seg}A")
                            elif (bi_crossed <= -2
                                    and m_face_to_bend):
                                b, s = m_face_to_bend.get(
                                    bi_crossed,
                                    (-bi_crossed-2, 0))
                                path.append(f"{b}.{s}B")
                            elif bi_crossed <= -2:
                                path.append(
                                    f"{-bi_crossed-2}B")
                    cur = parent
                path.reverse()
            path_str = "/".join(path) if path else "(root)"

            # Find source bend line name
            bl_name = ""
            if bendline_piece_idx is not None:
                for child in obj.Group:
                    if (getattr(getattr(child, 'Proxy', None),
                                'Type', None) != 'BendLine'):
                        continue
                    bl_pi = bendline_piece_idx.get(child.Name)
                    if bl_pi == pi:
                        bl_name = f" bl={child.Name}"
                        break

            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: piece {pi}: {label}"
                f" path=[{path_str}]"
                f" vol={pieces[pi].Volume:.4f}"
                f" cm=({cm.x:.2f},{cm.y:.2f},{cm.z:.2f})"
                f"{bl_name}\n")

    def _build_geometric_adjacency(self, pieces, cut_faces, cut_plan,
                                    piece_slices=None, cut_segments=None,
                                    joints=None):
        """Build geometric adjacency: which pieces touch and via which cut face.

        Returns a list of (i, j, fi) tuples.

        Builds adjacency from the joint structure (pieces adjacent to
        each A/B face).

        When *piece_slices* and *cut_segments* are provided, uses 2D
        geometry for distance checks instead of 3D solids.
        """
        n = len(pieces)
        tol = GEOMETRY_TOLERANCE
        shapes = piece_slices if piece_slices is not None else pieces

        # Group-based adjacency: for each cut face, find all
        # touching pieces; adjacent pairs share the face.
        face_pieces = {}  # fi → set of pi
        for grp in joints:
            for fi in grp['a_faces'] + grp['b_faces']:
                cf_shape = (cut_segments[fi]
                            if cut_segments is not None
                            else cut_faces[fi])
                adj = set()
                for pi in range(n):
                    if shapes[pi].distToShape(cf_shape)[0] < tol:
                        adj.add(pi)
                face_pieces[fi] = adj
        # Build crossings: pairs of pieces that share a cut face.
        crossing_set = set()
        geo_crossings = []
        for fi, pis in face_pieces.items():
            pis_list = sorted(pis)
            for idx_a in range(len(pis_list)):
                for idx_b in range(idx_a + 1, len(pis_list)):
                    i, j = pis_list[idx_a], pis_list[idx_b]
                    key = (i, j)
                    if key in crossing_set:
                        continue
                    # Only connect pieces on opposite sides
                    # of the cut face.
                    sp0_e = cut_plan[fi][0]
                    sp1_e = cut_plan[fi][1]
                    sdx = sp1_e.x - sp0_e.x
                    sdy = sp1_e.y - sp0_e.y
                    cm_i = pieces[i].CenterOfMass
                    cm_j = pieces[j].CenterOfMass
                    ci = sdx * (cm_i.y - sp0_e.y) \
                        - sdy * (cm_i.x - sp0_e.x)
                    cj = sdx * (cm_j.y - sp0_e.y) \
                        - sdy * (cm_j.x - sp0_e.x)
                    if ci * cj >= 0:
                        continue
                    crossing_set.add(key)
                    geo_crossings.append((i, j, fi))
        return geo_crossings

    def _classify_pieces_bfs(self, pieces, cut_faces, face_to_bend,
                             mass_center, half_t, bend_info, cut_plan,
                             micro_bend_info=None, log=True,
                             m_face_to_bend=None, mi_seg_idx=None,
                             cached_geo_crossings=None,
                             piece_slices=None, cut_segments=None,
                             strip_pieces=None,
                             joints=None,
                             face_to_seg=None):
        """BFS from the stationary piece with maximum-set preference.

        All crossings ADD the bend (union, sets only grow).

        When *strip_pieces* is set, BFS builds piece-to-piece arrows
        that skip through wedges.  Each bfs_tree entry is
        ``(parent_pi, mis_crossed, wedge_pi)`` where *mis_crossed*
        is a set of mi indices.  Without *strip_pieces*, entries are
        ``(parent_pi, mi_crossed)`` (2-tuple, legacy path).

        Returns (piece_bend_sets, bfs_tree, adjacency, cached_geo_crossings)."""
        n = len(pieces)
        if n == 0:
            return [], {}, [], []

        # Stationary piece = closest non-wedge piece to board outline
        # center of mass.  Wedge (strip) pieces are excluded so that BFS
        # always roots on a real panel piece.
        mc_2d = FreeCAD.Vector(mass_center.x, mass_center.y, half_t)
        _sp = strip_pieces or set()
        candidates = [pi for pi in range(n) if pi not in _sp]
        if not candidates:
            candidates = list(range(n))
        stationary_idx = min(
            candidates,
            key=lambda pi: pieces[pi].CenterOfMass.distanceToPoint(
                FreeCAD.Vector(mc_2d.x, mc_2d.y,
                               pieces[pi].CenterOfMass.z)))

        # Build adjacency graph from geometric crossings.
        # Uses topological data from generalFuse when available.
        if cached_geo_crossings is None:
            cached_geo_crossings = self._build_geometric_adjacency(
                pieces, cut_faces, cut_plan,
                piece_slices=piece_slices,
                cut_segments=cut_segments,
                joints=joints)

        # Label crossings using face_to_bend mapping (cheap).
        # Each entry: (neighbor, mi_label, face_index_or_None)
        adjacency = [[] for _ in range(n)]
        for i, j, best_touch_fi in cached_geo_crossings:
            mi = face_to_bend.get(best_touch_fi, -1)
            adjacency[i].append((j, mi, best_touch_fi))
            adjacency[j].append((i, mi, best_touch_fi))

        # Helper to decode crossing label for logging.
        # Per-cut labels: "9.0A", "9.1B" etc.
        def _crossing_label(bi):
            if bi >= 0 and micro_bend_info is not None:
                orig_bi = micro_bend_info[bi][5]
                seg = mi_seg_idx.get(bi, 0) if mi_seg_idx else 0
                return f"{orig_bi}.{seg}A"
            elif bi <= -2 and m_face_to_bend:
                bend_idx, seg = m_face_to_bend.get(
                    bi, (-bi - 2, 0))
                return f"{bend_idx}.{seg}B"
            elif bi <= -2:
                return f"{-bi - 2}B"
            elif bi >= 0:
                return f"b{bi}"
            else:
                return "-"

        # Log adjacency graph
        if log:
            for pi in range(n):
                crossings = []
                for nbr, bi, _fi in adjacency[pi]:
                    crossings.append(
                        f"{nbr}({_crossing_label(bi)})")
                if crossings:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: adjacent {pi} → "
                        f"{', '.join(crossings)}\n")

        # BFS: strict first-visit, all crossings add the bend.
        # No re-visiting, no re-queuing — first path wins.
        _sp = strip_pieces or set()

        def _side_test(pi_a, pi_b, fi_cut):
            """Return True if pieces a and b are on different sides
            of the bend's center segment identified by *fi_cut*."""
            if fi_cut is None:
                return True
            # Use the center segment (bend line) rather than the
            # offset A/B face line for a more robust side test.
            sid = face_to_seg.get(fi_cut) if face_to_seg else None
            if sid is not None and joints is not None:
                sp0, sp1 = joints[sid]['center']
            else:
                sp0, sp1 = cut_plan[fi_cut][0], cut_plan[fi_cut][1]
            sdx = sp1.x - sp0.x
            sdy = sp1.y - sp0.y
            cm_a = pieces[pi_a].CenterOfMass
            cm_b = pieces[pi_b].CenterOfMass
            ca = sdx * (cm_a.y - sp0.y) - sdy * (cm_a.x - sp0.x)
            cb = sdx * (cm_b.y - sp0.y) - sdy * (cm_b.x - sp0.x)
            return ca * cb < 0

        def _get_bend_idx(bi):
            if bi >= 0 and micro_bend_info is not None:
                return micro_bend_info[bi][5]
            elif bi >= 0:
                return bi
            elif bi <= -2 and m_face_to_bend:
                return m_face_to_bend.get(bi, (-bi - 2, 0))[0]
            elif bi <= -2:
                return -bi - 2
            return None

        piece_bend_sets = [None] * n
        piece_bend_sets[stationary_idx] = set()
        if _sp:
            # Piece-to-piece BFS: skip through wedges.
            bfs_tree = {stationary_idx: (None, set(), None)}
            queue = [stationary_idx]
            while queue:
                cur = queue.pop(0)
                for nbr, bi, fi in adjacency[cur]:
                    bend_idx = _get_bend_idx(bi)

                    if nbr in _sp:
                        # Wedge — mark visited if first time,
                        # then always look through (multiple
                        # pieces may need to traverse the same
                        # wedge to reach different neighbors).
                        if piece_bend_sets[nbr] is None:
                            piece_bend_sets[nbr] = \
                                piece_bend_sets[cur] | (
                                    {bend_idx} if bend_idx
                                    is not None else set())
                            bfs_tree[nbr] = (
                                cur, {bi}, None)
                        if log:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD: BFS p{cur} → "
                                f"wedge p{nbr} "
                                f"(entry={_crossing_label(bi)})"
                                f"\n")
                        for nbr2, bi2, fi2 in adjacency[nbr]:
                            if nbr2 == cur:
                                continue
                            if piece_bend_sets[nbr2] is not None:
                                continue
                            bend_idx2 = _get_bend_idx(bi2)
                            crossed = {bi}
                            crossed.add(bi2)
                            piece_bend_sets[nbr2] = \
                                piece_bend_sets[cur] | (
                                    {bend_idx2} if bend_idx2
                                    is not None else set())
                            bfs_tree[nbr2] = (
                                cur, crossed, nbr)
                            # Also record exit crossing in wedge's
                            # own crossed set so strip_to_mi
                            # can find the positive mi even when
                            # the entry crossing was a B-side face.
                            if nbr in bfs_tree:
                                bfs_tree[nbr][1].add(bi2)
                            if log:
                                FreeCAD.Console.PrintMessage(
                                    f"FreekiCAD: BFS   "
                                    f"p{cur} →[{_crossing_label(bi)}"
                                    f"]→ p{nbr}(W) →["
                                    f"{_crossing_label(bi2)}]→ "
                                    f"p{nbr2}\n")
                            queue.append(nbr2)
                    else:
                        # Regular piece — skip if visited.
                        if piece_bend_sets[nbr] is not None:
                            continue
                        if not _side_test(cur, nbr, fi):
                            if log:
                                FreeCAD.Console.PrintMessage(
                                    f"FreekiCAD: BFS "
                                    f"side_test("
                                    f"p{cur}, p{nbr}, "
                                    f"fi={fi}) FAIL "
                                    f"(cut="
                                    f"{_crossing_label(bi)})\n")
                            continue
                        piece_bend_sets[nbr] = \
                            piece_bend_sets[cur] | (
                                {bend_idx} if bend_idx is not None
                                else set())
                        bfs_tree[nbr] = (cur, {bi}, None)
                        if log:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD: BFS p{cur} →"
                                f"[{_crossing_label(bi)}]→ "
                                f"p{nbr}\n")
                        queue.append(nbr)

        else:
            # Legacy 3-tuple BFS (preliminary, no wedges).
            bfs_tree = {stationary_idx: (None, None, None)}
            queue = [stationary_idx]
            while queue:
                cur = queue.pop(0)
                for nbr, bi, _fi in adjacency[cur]:
                    if piece_bend_sets[nbr] is not None:
                        continue  # already visited
                    if bi >= 0:
                        bend_idx = micro_bend_info[bi][5] \
                            if micro_bend_info is not None \
                            else bi
                        piece_bend_sets[nbr] = \
                            piece_bend_sets[cur] | {bend_idx}
                    else:
                        piece_bend_sets[nbr] = \
                            piece_bend_sets[cur].copy()
                    bfs_tree[nbr] = (cur, bi, _fi)
                    queue.append(nbr)

        return piece_bend_sets, bfs_tree, adjacency, cached_geo_crossings

    def _bend_board(self, board_obj, p0, p1, line_dir, normal,
                    radius, max_angle, half_thickness):
        """Cut the board at the bend line segment, rotate the moving half.

        Uses generalFuse with a cutting face (the bend line extruded in Z)
        to split the board exactly where the segment crosses it.  Concave
        regions that don't cross the bend line are never touched.
        """
        shape = board_obj.Shape
        bb = shape.BoundBox
        up = FreeCAD.Vector(0, 0, 1)

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: _bend_board: p0={p0}, p1={p1}, "
            f"normal={normal}, angle={math.degrees(max_angle):.1f}°, "
            f"radius={radius}\n")
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD:   board bb: "
            f"({bb.XMin:.2f},{bb.YMin:.2f},{bb.ZMin:.2f})-"
            f"({bb.XMax:.2f},{bb.YMax:.2f},{bb.ZMax:.2f}), "
            f"n_solids={len(shape.Solids)}\n")

        # Build a cutting face from the bend line segment, extended in Z.
        diag = bb.DiagonalLength + 50
        c1 = p0 - up * diag
        c2 = p1 - up * diag
        c3 = p1 + up * diag
        c4 = p0 + up * diag
        cut_face = Part.Face(Part.makePolygon([c1, c2, c3, c4, c1]))

        # Split the board at the cutting face
        try:
            fused, _map = shape.generalFuse([cut_face])
        except Exception as e:
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD:   generalFuse failed: {e}\n")
            return None

        # Classify resulting solids by which side of the bend line
        # their center of mass falls on.
        stationary = []
        moving = []
        for s in fused.Solids:
            if s.Volume < 1e-6:
                continue
            cm = s.CenterOfMass
            cm_2d = FreeCAD.Vector(cm.x, cm.y, 0)
            d = (cm_2d - p0).dot(normal)
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD:   solid vol={s.Volume:.2f}, "
                f"cm=({cm.x:.2f},{cm.y:.2f},{cm.z:.2f}), "
                f"d={d:.2f} -> {'MOVE' if d > 0 else 'STAY'}\n")
            if d > 0:
                moving.append(s)
            else:
                stationary.append(s)

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD:   stationary={len(stationary)}, "
            f"moving={len(moving)}\n")

        if not moving:
            FreeCAD.Console.PrintWarning(
                "FreekiCAD:   no moving solids found!\n")
            return None

        bend_axis = up.cross(normal)
        bend_axis.normalize()
        rot = FreeCAD.Rotation(bend_axis, math.degrees(max_angle))
        pivot = p0 + up * half_thickness
        rot_placement = FreeCAD.Placement(
            FreeCAD.Vector(0, 0, 0), rot, pivot)

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD:   bend_axis={bend_axis}, "
            f"pivot={pivot}\n")

        bent_moving = []
        for s in moving:
            moved = s.copy()
            moved.transformShape(rot_placement.toMatrix())
            bent_moving.append(moved)

        # Preserve board color
        saved_color = None
        try:
            saved_color = board_obj.ViewObject.ShapeColor
        except Exception:
            pass

        all_solids = stationary + bent_moving
        board_obj.Shape = Part.makeCompound(all_solids)

        if saved_color:
            try:
                board_obj.ViewObject.ShapeColor = saved_color
            except Exception:
                pass

        return rot_placement

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
        pass

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
        import time as _time
        _t0_reload = _time.time()
        outline_name = obj.Name + "_Outline"
        if _sketch_observer is not None:
            _sketch_observer.suppress(outline_name)
        try:
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Reloading '{obj.Name}'...\n")
            self._suppress_execute = True
            if hasattr(obj, 'FileMtime'):
                obj.FileMtime = ""
            _t_remove = _time.time()
            existing_comps, existing_bends = self._remove_board_children(obj)
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: [profile] _remove_board_children: "
                f"{_time.time() - _t_remove:.3f}s\n")
            self._in_execute = True
            self._do_execute(obj, socket_path,
                             existing_components=existing_comps,
                             existing_bends=existing_bends)
        finally:
            self._in_execute = False
            self._suppress_execute = False
            if _sketch_observer is not None:
                _sketch_observer.unsuppress(outline_name)
            self._reloading = False
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: [profile] TOTAL _handle_reload_response: "
                f"{_time.time() - _t0_reload:.3f}s\n")

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

    # Properties that belong to this class (group "LinkedFile").
    # Anything in this group not listed here is obsolete and removed
    # on load by _ensure_properties().
    _KNOWN_PROPERTIES = {
        "FileName", "AutoReload", "EnableBending",
        "BuildDebugObjects", "DebugBoard", "SmoothWedge",
        "ComponentMtimes", "FileMtime",
    }

    def _ensure_properties(self, obj):
        """Add missing properties and remove obsolete ones (migration)."""
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
        if not hasattr(obj, 'BuildDebugObjects'):
            obj.addProperty(
                "App::PropertyBool", "BuildDebugObjects", "LinkedFile",
                "Build debug arrows and cut lines")
            obj.BuildDebugObjects = False
        if not hasattr(obj, 'DebugBoard'):
            obj.addProperty(
                "App::PropertyBool", "DebugBoard", "LinkedFile",
                "Show each board piece as a separate child object")
            obj.DebugBoard = False
        if not hasattr(obj, 'SmoothWedge'):
            obj.addProperty(
                "App::PropertyBool", "SmoothWedge", "LinkedFile",
                "Use B-Spline loft for wedges (slower but smoother)")
            obj.SmoothWedge = False
        # Remove obsolete properties from older saved files.
        for prop in list(obj.PropertiesList):
            if obj.getGroupOfProperty(prop) == "LinkedFile" \
                    and prop not in self._KNOWN_PROPERTIES:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: removing obsolete property "
                    f"'{prop}'\n")
                obj.removeProperty(prop)


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
        try:
            obj = vobj.Object
        except ReferenceError:
            self._auto_reload_timer.stop()
            return
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
