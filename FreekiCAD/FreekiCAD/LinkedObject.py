import os
import math
import re
import FreeCAD
import Part



DEFAULT_PCB_THICKNESS = 1.6  # mm fallback
GEOMETRY_TOLERANCE = 0.001  # mm (1 µm)
DEBUG_BENDING_BFS = True


def _log_bending_bfs(message):
    if DEBUG_BENDING_BFS:
        FreeCAD.Console.PrintMessage(message)


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


def _signed_line_side_2d(point, seg_p0, seg_p1):
    """Signed 2D side value of *point* relative to line *seg_p0*→*seg_p1*."""
    return ((seg_p1.x - seg_p0.x) * (point.y - seg_p0.y)
            - (seg_p1.y - seg_p0.y) * (point.x - seg_p0.x))


def _piece_local_side_point(piece, seg_p0, seg_p1):
    """Pick a piece point that best represents its local side near a bend.

    Uses the nearest off-line vertex to the segment midpoint and falls back
    to the piece center of mass when no such vertex exists.
    """
    sdx = seg_p1.x - seg_p0.x
    sdy = seg_p1.y - seg_p0.y
    seg_len = math.hypot(sdx, sdy)
    if seg_len < 1e-12:
        cm = piece.CenterOfMass
        return FreeCAD.Vector(cm.x, cm.y, cm.z)

    mid_x = (seg_p0.x + seg_p1.x) * 0.5
    mid_y = (seg_p0.y + seg_p1.y) * 0.5
    best = None
    best_d2 = float('inf')
    for v in getattr(piece, 'Vertexes', []):
        p = v.Point
        cross = _signed_line_side_2d(p, seg_p0, seg_p1)
        # Ignore points that lie essentially on the bend line.
        if abs(cross) < 1e-3 * seg_len:
            continue
        d2 = ((p.x - mid_x) ** 2 + (p.y - mid_y) ** 2)
        if d2 < best_d2:
            best_d2 = d2
            best = FreeCAD.Vector(p.x, p.y, p.z)
    if best is not None:
        return best

    cm = piece.CenterOfMass
    return FreeCAD.Vector(cm.x, cm.y, cm.z)


def _project_point_to_line_xy(pt, seg_p0, seg_p1):
    """Project *pt* to the infinite 2D line through *seg_p0*→*seg_p1*."""
    sx = seg_p1.x - seg_p0.x
    sy = seg_p1.y - seg_p0.y
    sl2 = sx * sx + sy * sy
    if sl2 < 1e-12:
        dx = pt.x - seg_p0.x
        dy = pt.y - seg_p0.y
        return 0.0, math.sqrt(dx * dx + dy * dy)
    t_raw = (
        ((pt.x - seg_p0.x) * sx
         + (pt.y - seg_p0.y) * sy)
        / sl2)
    px = seg_p0.x + t_raw * sx
    py = seg_p0.y + t_raw * sy
    dx = pt.x - px
    dy = pt.y - py
    return t_raw, math.sqrt(dx * dx + dy * dy)


_BEND_ANNOTATION_NUMBER_RE = (
    r'([+-]?(?:\d+(?:\.\d+)?|\.\d+))'
)


def _parse_bend_annotation(text_val, thickness):
    """Parse User.4 bend text and return ``(angle_deg, radius_mm, span_mm)``.

    ``r=...`` remains the explicit radius input. When ``r`` is omitted,
    ``s=...`` is interpreted as bend spanning (the full inset band width)
    and converted into a radius via:

    ``s / 2 = (r + thickness / 2) * |angle_rad| / 2``
    """
    angle = 0.0
    radius = 0.0
    span = None

    m_a = re.search(r'a\s*=\s*' + _BEND_ANNOTATION_NUMBER_RE, text_val)
    if m_a:
        angle = float(m_a.group(1))

    m_r = re.search(r'r\s*=\s*' + _BEND_ANNOTATION_NUMBER_RE, text_val)
    if m_r:
        radius = float(m_r.group(1))
        return angle, radius, span

    m_s = re.search(r's\s*=\s*' + _BEND_ANNOTATION_NUMBER_RE, text_val)
    if not m_s:
        return angle, radius, span

    span = float(m_s.group(1))
    angle_rad = math.radians(angle)
    if abs(angle_rad) <= 1e-9:
        FreeCAD.Console.PrintWarning(
            "FreekiCAD:   bend text has s=... but angle is 0°, "
            "cannot derive radius\n")
        return angle, radius, span

    radius = span / abs(angle_rad) - thickness / 2.0
    return angle, radius, span


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


def _component_transform_cache_value(fp_info, thickness):
    """Return a stable JSON string describing baked model transforms."""
    import json

    is_back = bool(fp_info.get('is_back', False))
    fp_z = thickness if not is_back else 0.0
    models = []
    for model in fp_info.get('models', []):
        models.append({
            'path': os.path.realpath(model['path']),
            'offset': [float(v) for v in model['offset']],
            'rotation': [float(v) for v in model['rotation']],
            'scale': [float(v) for v in model['scale']],
        })
    return json.dumps(
        {
            'is_back': is_back,
            'fp_z': float(fp_z),
            'models': models,
        },
        sort_keys=True,
        separators=(',', ':'),
    )


def _ensure_component_transform_cache_property(comp_obj):
    """Ensure the hidden cached-transform property exists on *comp_obj*."""
    if hasattr(comp_obj, 'FreekiCAD_ModelTransformCache'):
        return
    comp_obj.addProperty(
        "App::PropertyString", "FreekiCAD_ModelTransformCache",
        "FreekiCAD", "Cached model transform settings for reuse"
    )
    try:
        comp_obj.setPropertyStatus(
            "FreekiCAD_ModelTransformCache", "Hidden")
    except Exception:
        pass


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

    color = None

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
                color = c.red, c.green, c.blue, c.alpha
                if all([x==0 for x in color]):
                    color = None
                break
    except Exception as ex:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Could not read board color from API: {ex}\n"
        )

    # 2. Fallback: parse the .kicad_pcb file directly
    if color is None and filepath:
        color = _get_board_color_from_file(filepath)
        if color:
            color = [x*255 for x in color]
            if len(color) == 3:
                color.append(255)

    if color and (all([x==0 for x in color]) or color == [0x80, 0x80, 0x80, 0xFF]):
        FreeCAD.Console.PrintMessage(
            "FreekiCAD: Stackup F.Mask color from API is transparent black, use default color"
        )
        color = None

    if color:
        ret = tuple([x/255.0 for x in color[:3]])
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Use color {ret}"
        )
        return ret
    else:
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

        # --- Parse text on User.4 for bend parameters ---
        if bend_lines:
            try:
                from kipy.board_types import BoardText as KiPyBoardText
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
                    # Parse "a=-70 r=0.5" or "a=-70 s=0.61" style text.
                    angle, radius, span = _parse_bend_annotation(
                        text_val, thickness)
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
                        msg = (
                            f"FreekiCAD:   matched → "
                            f"angle={angle}° "
                            f"radius={radius}mm"
                        )
                        if span is not None:
                            msg += (
                                f" (from s={span}mm, t={thickness}mm)"
                            )
                        FreeCAD.Console.PrintMessage(msg + "\n")
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
        self._move_timer_parents = {}  # obj.Name → parent.Name

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

    def _is_component_move_blocked(self, parent):
        proxy = getattr(parent, "Proxy", None)
        if proxy and hasattr(proxy, "_is_component_move_blocked"):
            try:
                return proxy._is_component_move_blocked(parent)
            except Exception:
                pass
        return self._is_bending_active(parent)

    def cancel_component_moves(self, parent):
        parent_name = getattr(parent, "Name", parent)
        for name, owner in list(self._move_timer_parents.items()):
            if owner != parent_name:
                continue
            timer = self._move_timers.pop(name, None)
            self._move_timer_parents.pop(name, None)
            if timer is not None:
                timer.stop()

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
                if self._is_component_move_blocked(parent):
                    return
                self._constrain_placement(obj)
                if self._is_component_move_blocked(parent):
                    return
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
            self._move_timers.pop(name, None)
            self._move_timer_parents.pop(name, None)

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
        self._move_timer_parents[name] = parent.Name
        delay_ms = getattr(parent.Proxy, "_COMPONENT_MOVE_DEBOUNCE_MS", 200)
        timer.start(delay_ms)

    def _send_move_component(self, obj, parent, ref):
        """Send move-component request to workspace bus."""
        self._move_timers.pop(obj.Name, None)
        self._move_timer_parents.pop(obj.Name, None)
        try:
            proxy = getattr(parent, "Proxy", None)
            if proxy and proxy._is_component_move_blocked(parent):
                return
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

    _WEDGE_MODE_OPTIONS = [
        "Smooth", "Wireframe"]
    _REBEND_DEBOUNCE_MS = 1000
    _COMPONENT_MOVE_DEBOUNCE_MS = 200
    _COMPONENT_SYNC_GRACE_MS = 200


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
            "App::PropertyEnumeration", "WedgeMode", "LinkedFile",
            "Wedge rendering mode: Smooth or Wireframe"
        )
        obj.WedgeMode = list(self._WEDGE_MODE_OPTIONS)
        obj.WedgeMode = "Smooth"
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
        self._ensure_rebend_timer_state()
        self._ensure_component_sync_state()

    def onChanged(self, obj, prop):
        if prop in ("EnableBending", "BuildDebugObjects", "DebugBoard",
                    "WedgeMode"):
            if not obj.Document.Restoring:
                self._schedule_rebend(obj)
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

    def _ensure_component_sync_state(self):
        if not hasattr(self, '_component_sync_suspended'):
            self._component_sync_suspended = False
        if not hasattr(self, '_component_sync_generation'):
            self._component_sync_generation = 0

    def _is_component_move_blocked(self, obj=None):
        self._ensure_component_sync_state()
        if self._component_sync_suspended or getattr(self, '_bending', False):
            return True
        if obj is None or not getattr(obj, 'EnableBending', False):
            return False
        for c in getattr(obj, 'Group', []):
            proxy = getattr(c, 'Proxy', None)
            if proxy and getattr(proxy, 'Type', None) == 'BendLine':
                if c.Active and c.Angle.Value != 0:
                    return True
        return False

    def _suspend_component_move_sync(self, obj=None):
        self._ensure_component_sync_state()
        self._component_sync_suspended = True
        self._component_sync_generation += 1
        self._pending_move = None
        if obj is not None and _sketch_observer is not None:
            _sketch_observer.cancel_component_moves(obj)

    def _resume_component_move_sync(self, delay_ms=None):
        self._ensure_component_sync_state()
        token = self._component_sync_generation
        if delay_ms is None:
            delay_ms = self._COMPONENT_SYNC_GRACE_MS
        if delay_ms <= 0:
            self._component_sync_suspended = False
            return
        from PySide import QtCore

        def _resume():
            if self._component_sync_generation == token:
                self._component_sync_suspended = False

        QtCore.QTimer.singleShot(delay_ms, _resume)

    def _remove_children(self, obj):
        """Remove all child objects from this group."""
        doc = obj.Document
        children = list(obj.Group)
        for child in children:
            try:
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
                # Reused bend lines may still carry the previous reload's
                # bent placement. Reset to the flat pose before rebuilding
                # the base line so a new reload starts from identity.
                bend_obj.Placement = FreeCAD.Placement()
                bend_obj.Shape = Part.makeLine(p0, p1)
            else:
                bend_obj = doc.addObject(
                    "Part::FeaturePython", obj.Name + "_Bend")
                BendLine(bend_obj, uuid)
                bend_obj.Placement = FreeCAD.Placement()
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
        footprint_info_by_ref = {}

        for fp_info in footprints_data:
            ref = fp_info['ref']
            footprint_info_by_ref[ref] = fp_info
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
                    reuse_reason = None
                    for path, old_mt in stored.items():
                        try:
                            cur_mt = os.path.getmtime(path)
                        except OSError:
                            can_reuse = False
                            changed_file = path
                            reuse_reason = "missing model file"
                            break
                        if cur_mt != old_mt:
                            can_reuse = False
                            changed_file = path
                            reuse_reason = "mtime changed"
                            break
                    child = existing_components[ref]
                    current_transform_cache = _component_transform_cache_value(
                        fp_info, thickness)
                    cached_transform_cache = ""
                    if can_reuse:
                        if hasattr(child, 'FreekiCAD_ModelTransformCache'):
                            cached_transform_cache = \
                                child.FreekiCAD_ModelTransformCache or ""
                        if not cached_transform_cache:
                            can_reuse = False
                            reuse_reason = "no cached transform settings"
                        elif cached_transform_cache != current_transform_cache:
                            can_reuse = False
                            reuse_reason = "transform settings changed"
                    if can_reuse:
                        matched.add(ref)
                        all_mtimes[ref] = stored_mtimes[ref]
                        # Ensure prefixed name for migration
                        expected = obj.Name + "_" + ref
                        if child.Label != expected:
                            child.Label = expected
                        _ensure_component_transform_cache_property(child)
                        child.FreekiCAD_ModelTransformCache = \
                            current_transform_cache
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
                        if changed_file:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD:   {ref}: {reuse_reason} "
                                f"({os.path.basename(changed_file)}), "
                                f"reloading\n")
                        else:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD:   {ref}: {reuse_reason}, "
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
            fp_info = footprint_info_by_ref.get(label)
            if fp_info is not None:
                _ensure_component_transform_cache_property(comp_obj)
                comp_obj.FreekiCAD_ModelTransformCache = \
                    _component_transform_cache_value(fp_info, thickness)
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
        elif board_obj:
            self._update_conflicts_debug_object(obj, None, thickness)
        # Cancel any pending rebend timer — bending was already
        # handled by _apply_bends above.
        timer = getattr(self, '_rebend_timer', None)
        if timer is not None:
            timer.stop()

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
        bend lines and LinkedObject properties into a single rebend call."""
        from PySide import QtCore, QtWidgets

        self._ensure_rebend_timer_state()
        self._suspend_component_move_sync(obj)
        self._rebend_target = obj
        delay_ms = self._get_rebend_debounce_ms(obj)

        if self._rebend_timer is None:
            self._rebend_timer = QtCore.QTimer()
            self._rebend_timer.setSingleShot(True)

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
                        self._rebend_timer.start(
                            self._get_rebend_debounce_ms(obj))
                        return
                target = self._rebend_target
                if target is not None:
                    self._rebend(target)

            self._rebend_timer.timeout.connect(_do_rebend)

        if self._rebend_timer.isActive():
            self._rebend_timer.stop()
        self._rebend_timer.start(delay_ms)

    def _rebend(self, obj):
        """Re-apply bending after Radius/Angle/Active or EnableBending
        changes on a bend line."""
        if not hasattr(self, '_unbent_board_shape'):
            self._resume_component_move_sync(delay_ms=0)
            return
        import time as _time
        _t0_rebend = _time.time()
        self._suspend_component_move_sync(obj)
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
            self._resume_component_move_sync()
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: [profile] TOTAL _rebend: "
                f"{_time.time() - _t0_rebend:.3f}s\n")

    def _apply_bends(self, obj, board_obj, bend_children, thickness,
                     enable_bending=True):
        """Apply bending deformation to the board shape."""
        self._suspend_component_move_sync(obj)
        self._bending = True
        try:
            self.__apply_bends_impl(obj, board_obj, bend_children,
                                    thickness, enable_bending)
        finally:
            self._bending = False
            self._resume_component_move_sync()

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
            self._update_conflicts_debug_object(obj, None, thickness)
            return

        # --- Phase 2: cut flat board with all bend faces, classify
        #     pieces via BFS to find which bends each piece crosses ---
        bb = unbent.BoundBox
        diag = bb.DiagonalLength + 50
        thickness = half_t * 2

        # Compute inset for each bend using r_eff = R + T/2
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

        bend_span_shapes = []
        overlap_area_tol = max(
            GEOMETRY_TOLERANCE * GEOMETRY_TOLERANCE, 1e-4)
        conflict_pairs = []
        conflict_bends = set()
        for bi, (_, p0, p1, _, normal, _, _) in enumerate(bend_info):
            bend_span_shapes.append(
                self._build_bend_span_shape(
                    p0, p1, normal, insets[bi], board_face))
        for i in range(len(bend_span_shapes)):
            span_i = bend_span_shapes[i]
            if span_i is None:
                continue
            for j in range(i + 1, len(bend_span_shapes)):
                span_j = bend_span_shapes[j]
                if span_j is None:
                    continue
                try:
                    overlap = span_i.common(span_j)
                    overlap_area = float(getattr(overlap, "Area", 0.0))
                except Exception:
                    continue
                if overlap_area <= overlap_area_tol:
                    continue
                conflict_pairs.append((i, j, overlap_area))
                conflict_bends.add(i)
                conflict_bends.add(j)
                bend_i = bend_info[i][0]
                bend_j = bend_info[j][0]
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: Bend span conflict "
                    f"{i} ({bend_i.Name}) <-> {j} ({bend_j.Name}) "
                    f"area={overlap_area:.4f} mm^2\n")

        if conflict_bends:
            conflict_shapes = []
            for bi in sorted(conflict_bends):
                span_shape = bend_span_shapes[bi]
                if span_shape is None:
                    continue
                try:
                    conflict_shapes.append(span_shape.copy())
                except Exception:
                    conflict_shapes.append(span_shape)
            conflict_shape = None
            if conflict_shapes:
                try:
                    conflict_shape = Part.makeCompound(conflict_shapes)
                except Exception:
                    conflict_shape = conflict_shapes[0]
            self._clear_bend_debug_artifacts(
                obj, board_obj=board_obj)
            self._update_conflicts_debug_object(
                obj, conflict_shape, thickness)
            state = "disabling bending" if enable_bending \
                else "keeping board flat"
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: Found {len(conflict_pairs)} bend span "
                f"conflict(s), {state}\n")
            return

        self._update_conflicts_debug_object(obj, None, thickness)
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

        def _project_point_to_segment_xy(pt, seg_p0, seg_p1):
            sx = seg_p1.x - seg_p0.x
            sy = seg_p1.y - seg_p0.y
            sl2 = sx * sx + sy * sy
            if sl2 < 1e-12:
                dx = pt.x - seg_p0.x
                dy = pt.y - seg_p0.y
                return 0.0, 0.0, math.sqrt(dx * dx + dy * dy)
            t_raw = (
                ((pt.x - seg_p0.x) * sx
                 + (pt.y - seg_p0.y) * sy)
                / sl2)
            t = max(0.0, min(1.0, t_raw))
            px = seg_p0.x + t * sx
            py = seg_p0.y + t * sy
            dx = pt.x - px
            dy = pt.y - py
            return t_raw, t, math.sqrt(dx * dx + dy * dy)

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
        wedge_assign_diag = getattr(obj, 'BuildDebugObjects', False)

        def _piece_segment_debug_metrics(piece, seg_p0, seg_p1):
            cm = piece.CenterOfMass
            cm_t_raw, cm_t, cm_d = _project_point_to_segment_xy(
                cm, seg_p0, seg_p1)
            vertex_line_d = []
            vertex_t_raw = []
            vertex_t = []
            vertex_d = []
            for vertex in getattr(piece, 'Vertexes', []):
                _, d_line_v = _project_point_to_line_xy(
                    vertex.Point, seg_p0, seg_p1)
                t_raw_v, t_v, d_v = _project_point_to_segment_xy(
                    vertex.Point, seg_p0, seg_p1)
                vertex_line_d.append(d_line_v)
                vertex_t_raw.append(t_raw_v)
                vertex_t.append(t_v)
                vertex_d.append(d_v)
            bbox = getattr(piece, 'BoundBox', None)
            return {
                'cm_t_raw': cm_t_raw,
                'cm_t': cm_t,
                'cm_d': cm_d,
                'line_d_max': (
                    max(vertex_line_d)
                    if vertex_line_d else float('nan')),
                't_raw_min': min(vertex_t_raw) if vertex_t_raw else float('nan'),
                't_raw_max': max(vertex_t_raw) if vertex_t_raw else float('nan'),
                't_min': min(vertex_t) if vertex_t else float('nan'),
                't_max': max(vertex_t) if vertex_t else float('nan'),
                'd_min': min(vertex_d) if vertex_d else float('nan'),
                'd_max': max(vertex_d) if vertex_d else float('nan'),
                'bbox': bbox,
            }

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
        strip_to_seg = {}
        strip_seed_mi = {}
        strip_seed_parent = {}
        strip_seed_source = {}
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
                for fi in range(len(cut_faces)):
                    if face_bend.get(fi) != bi:
                        continue
                    if fi in face_to_seg:
                        continue
                    mid = (cut_plan[fi][0] + cut_plan[fi][1]) * 0.5
                    _, _, d = _project_point_to_segment_xy(
                        mid, bl_sp0, bl_sp1)
                    if d < ins + GEOMETRY_TOLERANCE:
                        face_to_seg[fi] = sid
                        side = face_topo_side[fi]
                        if side == 'A':
                            joint['a_faces'].append(fi)
                        else:
                            joint['b_faces'].append(fi)
                # Seed obvious wedge pieces whose farthest sampled point
                # still stays inside the inset band for this center segment.
                # Give this first pass a slightly looser tolerance because it
                # only samples vertices; the touch-based rescue pass below
                # still does the precise geometric cleanup.
                if ins < 1e-6:
                    continue
                seg_dx = bl_sp1.x - bl_sp0.x
                seg_dy = bl_sp1.y - bl_sp0.y
                seg_len = math.hypot(seg_dx, seg_dy)
                if seg_len < 1e-12:
                    continue
                seed_tol = max(GEOMETRY_TOLERANCE, min(0.05, ins * 0.05))
                tol_t = seed_tol / seg_len
                for pi, piece in enumerate(pieces):
                    if pi in strip_pieces:
                        continue
                    metrics = _piece_segment_debug_metrics(
                        piece, bl_sp0, bl_sp1)
                    if math.isnan(metrics['t_raw_min']):
                        continue
                    if math.isnan(metrics['t_raw_max']):
                        continue
                    if math.isnan(metrics['d_max']):
                        continue
                    if (metrics['t_raw_max'] < -tol_t
                            or metrics['t_raw_min'] > 1.0 + tol_t):
                        continue
                    if metrics['d_max'] > ins + seed_tol:
                        continue
                    joint['wedges'].append(pi)
                    strip_pieces.add(pi)
                    strip_to_bend[pi] = bi
                    strip_to_seg[pi] = sid

        # Rescue any still-unmatched A/B cut faces using the whole 2D cut
        # segment instead of only the cut midpoint. This catches branch/
        # concavity cases where the face is visibly part of a trimmed bend
        # segment but its midpoint falls outside the inset band.
        center_segments_2d = []
        for joint in joints:
            seg_p0, seg_p1 = joint['center']
            center_segments_2d.append(Part.makeLine(
                FreeCAD.Vector(seg_p0.x, seg_p0.y, half_t),
                FreeCAD.Vector(seg_p1.x, seg_p1.y, half_t)))

        def _match_unassigned_face_to_sid(fi):
            bi = face_bend.get(fi)
            if bi is None:
                return None
            ins = insets[bi]
            face_seg = cut_segments[fi]
            face_sp0, face_sp1 = cut_plan[fi][0], cut_plan[fi][1]
            face_mid = (face_sp0 + face_sp1) * 0.5
            best_sid = None
            best_score = None
            for sid, joint in enumerate(joints):
                if joint['bi'] != bi:
                    continue
                seg_p0, seg_p1 = joint['center']
                seg_dx = seg_p1.x - seg_p0.x
                seg_dy = seg_p1.y - seg_p0.y
                seg_len = math.sqrt(seg_dx * seg_dx + seg_dy * seg_dy)
                if seg_len < 1e-12:
                    continue
                try:
                    whole_d = face_seg.distToShape(
                        center_segments_2d[sid])[0]
                except Exception:
                    continue
                if whole_d > ins + GEOMETRY_TOLERANCE:
                    continue

                t0_raw, _, _ = _project_point_to_segment_xy(
                    face_sp0, seg_p0, seg_p1)
                t1_raw, _, _ = _project_point_to_segment_xy(
                    face_sp1, seg_p0, seg_p1)
                t_min_raw = min(t0_raw, t1_raw)
                t_max_raw = max(t0_raw, t1_raw)
                tol_t = GEOMETRY_TOLERANCE / seg_len
                if t_max_raw < -tol_t or t_min_raw > 1.0 + tol_t:
                    continue

                # Prefer candidates whose full cut segment overlaps the
                # trimmed center segment, then fall back to smallest whole-
                # segment distance and midpoint closeness as tie-breakers.
                overlap = max(0.0, min(1.0, t_max_raw) - max(0.0, t_min_raw))
                mid_t_raw, _, mid_d = _project_point_to_segment_xy(
                    face_mid, seg_p0, seg_p1)
                score = (
                    overlap > 0.0,
                    overlap,
                    -whole_d,
                    -mid_d,
                    -abs(mid_t_raw - 0.5),
                )
                if best_score is None or score > best_score:
                    best_score = score
                    best_sid = sid
            return best_sid

        for fi in range(len(cut_faces)):
            if fi in face_to_seg:
                continue
            sid = _match_unassigned_face_to_sid(fi)
            if sid is None:
                continue
            face_to_seg[fi] = sid
            side = face_topo_side.get(fi)
            if side == 'A':
                if fi not in joints[sid]['a_faces']:
                    joints[sid]['a_faces'].append(fi)
            else:
                if fi not in joints[sid]['b_faces']:
                    joints[sid]['b_faces'].append(fi)
            if wedge_assign_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: rescued face fi={fi}"
                    f" bend={face_bend.get(fi)}"
                    f" side={side}"
                    f" sid={sid}\n")

        # Rescue any still-unassigned strip pieces using the matched cut
        # faces they actually touch, instead of only their center of mass.
        # A true strip piece must touch both topo sides of at least one bend:
        # either both A/B faces of a single trimmed segment, or neighboring
        # trimmed segments on opposite topo sides (for branched strips such as
        # p162 in maze_radius_skewed).
        piece_bend_touch = {}  # pi -> bi -> {'sids', 'sides', 'sid_sides'}
        for fi, sid in face_to_seg.items():
            bi = face_bend.get(fi)
            if bi is None:
                continue
            side = face_topo_side.get(fi)
            if side not in ('A', 'B'):
                continue
            cut_shape = cut_segments[fi]
            for pi in range(len(pieces)):
                if pi in strip_pieces:
                    continue
                try:
                    d_touch = piece_slices[pi].distToShape(cut_shape)[0]
                except Exception:
                    d_touch = float('inf')
                if d_touch >= GEOMETRY_TOLERANCE:
                    continue
                bend_touch = piece_bend_touch.setdefault(pi, {})
                touch = bend_touch.setdefault(bi, {
                    'sids': set(),
                    'sides': set(),
                    'sid_sides': {},
                })
                touch['sids'].add(sid)
                touch['sides'].add(side)
                touch['sid_sides'].setdefault(sid, set()).add(side)

        if wedge_assign_diag:
            for pi in sorted(piece_bend_touch):
                if pieces[pi].Volume > 0.15:
                    continue
                bend_touch = piece_bend_touch[pi]
                for bi in sorted(bend_touch):
                    touch = bend_touch[bi]
                    sid_sides = ", ".join(
                        f"{sid}:{''.join(sorted(sides))}"
                        for sid, sides in sorted(
                            touch['sid_sides'].items()))
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: strip-touch p{pi}"
                        f" bend={bi}"
                        f" sides={sorted(touch['sides'])}"
                        f" sids={sorted(touch['sids'])}"
                        f" sid_sides=[{sid_sides}]\n")

        def _match_unassigned_piece_to_sid(pi):
            bend_touch = piece_bend_touch.get(pi)
            if not bend_touch:
                if wedge_assign_diag and pieces[pi].Volume <= 0.15:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: strip-touch reject p{pi}"
                        f" reason=no-touch-map"
                        f" vol={pieces[pi].Volume:.6f}\n")
                return None
            best_match = None
            for bi, touch in bend_touch.items():
                if len(touch['sides']) < 2:
                    if wedge_assign_diag and pieces[pi].Volume <= 0.15:
                        sid_sides = ", ".join(
                            f"{sid}:{''.join(sorted(sides))}"
                            for sid, sides in sorted(
                                touch['sid_sides'].items()))
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD: strip-touch reject p{pi}"
                            f" bend={bi}"
                            f" reason=single-side"
                            f" sides={sorted(touch['sides'])}"
                            f" sids={sorted(touch['sids'])}"
                            f" sid_sides=[{sid_sides}]\n")
                    continue
                ins = insets[bi]
                if ins < 1e-6:
                    continue
                for sid in sorted(touch['sids']):
                    joint = joints[sid]
                    if joint['bi'] != bi:
                        continue
                    seg_p0, seg_p1 = joint['center']
                    seg_dx = seg_p1.x - seg_p0.x
                    seg_dy = seg_p1.y - seg_p0.y
                    seg_len = math.sqrt(seg_dx * seg_dx + seg_dy * seg_dy)
                    if seg_len < 1e-12:
                        continue
                    try:
                        whole_d = piece_slices[pi].distToShape(
                            center_segments_2d[sid])[0]
                    except Exception:
                        continue
                    if whole_d > ins + GEOMETRY_TOLERANCE:
                        if wedge_assign_diag and pieces[pi].Volume <= 0.15:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD: strip-touch reject p{pi}"
                                f" bend={bi}"
                                f" sid={sid}"
                                f" reason=outside-band"
                                f" whole_d={whole_d:.6f}"
                                f" limit={ins + GEOMETRY_TOLERANCE:.6f}\n")
                        continue
                    metrics = _piece_segment_debug_metrics(
                        pieces[pi], seg_p0, seg_p1)
                    tol_t = GEOMETRY_TOLERANCE / seg_len
                    if (metrics['t_raw_max'] < -tol_t
                            or metrics['t_raw_min'] > 1.0 + tol_t):
                        if wedge_assign_diag and pieces[pi].Volume <= 0.15:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD: strip-touch reject p{pi}"
                                f" bend={bi}"
                                f" sid={sid}"
                                f" reason=no-segment-overlap"
                                f" t_raw=[{metrics['t_raw_min']:.6f},"
                                f"{metrics['t_raw_max']:.6f}]"
                                f" tol_t={tol_t:.6f}\n")
                        continue
                    overlap = max(
                        0.0,
                        min(1.0, metrics['t_raw_max'])
                        - max(0.0, metrics['t_raw_min']))
                    score = (
                        len(touch['sid_sides'].get(sid, ())) == 2,
                        overlap > 0.0,
                        overlap,
                        -whole_d,
                        -metrics['cm_d'],
                        -abs(metrics['cm_t_raw'] - 0.5),
                        -sid,
                    )
                    if wedge_assign_diag and pieces[pi].Volume <= 0.15:
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD: strip-touch candidate p{pi}"
                            f" bend={bi}"
                            f" sid={sid}"
                            f" overlap={overlap:.6f}"
                            f" whole_d={whole_d:.6f}"
                            f" cm_d={metrics['cm_d']:.6f}"
                            f" line_d_max={metrics['line_d_max']:.6f}"
                            f" cm_t_raw={metrics['cm_t_raw']:.6f}\n")
                    if best_match is None or score > best_match[0]:
                        best_match = (score, sid, bi, touch)
            return best_match

        for pi in range(len(pieces)):
            if pi in strip_pieces:
                continue
            match = _match_unassigned_piece_to_sid(pi)
            if match is None:
                continue
            _score, sid, bi, touch = match
            joints[sid]['wedges'].append(pi)
            strip_pieces.add(pi)
            strip_to_bend[pi] = bi
            strip_to_seg[pi] = sid
            if wedge_assign_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: rescued strip piece p{pi}"
                    f" bend={bi}"
                    f" sid={sid}"
                    f" touch_sids={sorted(touch['sids'])}"
                    f" touch_sides={sorted(touch['sides'])}\n")

        if wedge_assign_diag:
            tol = GEOMETRY_TOLERANCE
            for sid, joint in enumerate(joints):
                seg_p0, seg_p1 = joint['center']
                sx = seg_p1.x - seg_p0.x
                sy = seg_p1.y - seg_p0.y
                seg_len = math.sqrt(sx * sx + sy * sy)
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: joint sid={sid} bi={joint['bi']}"
                    f" len={seg_len:.6f}"
                    f" A={joint['a_faces']}"
                    f" B={joint['b_faces']}"
                    f" wedges={joint['wedges']}\n")
                for pi in joint['wedges']:
                    piece = pieces[pi]
                    metrics = _piece_segment_debug_metrics(
                        piece, seg_p0, seg_p1)
                    touch_faces = []
                    for fi in range(len(cut_faces)):
                        try:
                            d_touch = piece_slices[pi].distToShape(
                                cut_segments[fi])[0]
                        except Exception:
                            d_touch = float('inf')
                        if d_touch < tol:
                            touch_faces.append(fi)
                    touch_sids = sorted(set(
                        face_to_seg[fi]
                        for fi in touch_faces
                        if fi in face_to_seg))
                    touch_ab = [
                        f"{fi}:{face_topo_side.get(fi, '?')}"
                        for fi in touch_faces
                        if face_bend.get(fi) == joint['bi']
                    ]
                    bbox = metrics['bbox']
                    bbox_str = "-"
                    if bbox is not None:
                        bbox_str = (
                            f"({bbox.XMin:.3f},{bbox.YMin:.3f},{bbox.ZMin:.3f})"
                            f"->({bbox.XMax:.3f},{bbox.YMax:.3f},{bbox.ZMax:.3f})")
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   wedge-src p{pi}"
                        f" cm=({piece.CenterOfMass.x:.6f},{piece.CenterOfMass.y:.6f},{piece.CenterOfMass.z:.6f})"
                        f" vol={piece.Volume:.6f}"
                        f" cm_t={metrics['cm_t']:.6f}"
                        f" cm_t_raw={metrics['cm_t_raw']:.6f}"
                        f" cm_d={metrics['cm_d']:.6f}"
                        f" t_raw=[{metrics['t_raw_min']:.6f},{metrics['t_raw_max']:.6f}]"
                        f" t=[{metrics['t_min']:.6f},{metrics['t_max']:.6f}]"
                        f" d=[{metrics['d_min']:.6f},{metrics['d_max']:.6f}]"
                        f" touch={touch_ab if touch_ab else '-'}"
                        f" touch_sid={touch_sids if touch_sids else '-'}"
                        f" bbox={bbox_str}\n")
                    if touch_sids and touch_sids != [sid]:
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD:   wedge-src p{pi}"
                            f" assigned_sid={sid}"
                            f" but touches_sid={touch_sids}\n")

        # Map each debug cut to the rigid piece that owns that edge.
        # The final debug line should follow the same rigid transform as
        # the piece adjacent to that cut, rather than approximating the
        # result from the bend line placement alone.
        cut_owner_piece = {}
        for fi, cut_seg in enumerate(cut_segments):
            touching = []
            for pi in range(len(pieces)):
                if piece_slices[pi].distToShape(
                        cut_seg)[0] < GEOMETRY_TOLERANCE:
                    touching.append(pi)
            rigid_touching = [pi for pi in touching
                              if pi not in strip_pieces]
            if len(rigid_touching) == 1:
                cut_owner_piece[fi] = rigid_touching[0]
            elif len(rigid_touching) > 1:
                bi = face_bend.get(fi)
                if bi is not None:
                    p0_ref = bend_info[bi][1]
                    normal_ref = bend_info[bi][4]
                    side = face_topo_side.get(fi)
                    if side == 'A':
                        owner_pi = min(
                            rigid_touching,
                            key=lambda pi: (
                                pieces[pi].CenterOfMass
                                - p0_ref).dot(normal_ref))
                    else:
                        owner_pi = max(
                            rigid_touching,
                            key=lambda pi: (
                                pieces[pi].CenterOfMass
                                - p0_ref).dot(normal_ref))
                    cut_owner_piece[fi] = owner_pi

        # Build geometric crossings and BFS for s/m assignment.
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Phase 2c (pairing): "
            f"{_time.time() - _t_phase2c:.3f}s\n")
        _t_bfs1 = _time.time()
        geo_crossings = self._build_geometric_adjacency(
            pieces, cut_faces, cut_plan,
            piece_slices=piece_slices,
            cut_segments=cut_segments,
            joints=joints,
            face_to_seg=face_to_seg)

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
        bend_s_mis = {}  # bi → list of mi's (one per cut segment)
        mi_seg_idx = {}  # mi → seg_idx
        # Derive segment index from face_to_seg pairing:
        # paired A and B faces share the same sid, so they
        # get the same seg_idx within their bend.
        sid_to_seg_idx = {}  # sid → seg_idx
        sid_to_mi = {}  # sid → positive mi (paired A/B faces share one mi)
        mi_to_sid = {}  # mi → sid
        mi_has_stationary_face = {}  # mi → geometry chosen from stationary face
        bend_seg_count = {}  # bi → next seg_idx
        for fi in range(len(cut_faces)):
            bi = face_bend.get(fi)
            if bi is None:
                continue
            sid = face_to_seg.get(fi)
            if sid is None:
                # Unmatched cut faces can still split the solid when the
                # offset line passes through a concavity. Keep the geometry,
                # but treat those cuts as rigid/no-bend links so BFS can
                # traverse across them without inventing a micro-bend.
                face_to_micro[fi] = -1
                continue
            parent_pi = fi_parent.get(fi)
            is_stationary = False
            if parent_pi is not None:
                if sid is not None and joints is not None:
                    seg_p0, seg_p1 = joints[sid]['center']
                else:
                    seg_p0, seg_p1 = cut_plan[fi][0], cut_plan[fi][1]
                parent_pt = _piece_local_side_point(
                    pieces[parent_pi], seg_p0, seg_p1)
                seg_mid = (seg_p0 + seg_p1) * 0.5
                normal_ref = cut_plan_data[fi][3]
                side_dot = (parent_pt - seg_mid).dot(normal_ref)
                side_tol = max(GEOMETRY_TOLERANCE * 10.0, 1e-4)
                side_label = face_topo_side.get(fi)
                if side_label == 'A':
                    is_stationary = side_dot < -side_tol
                elif side_label == 'B':
                    is_stationary = side_dot > side_tol
                if not is_stationary and abs(side_dot) <= side_tol:
                    # Local geometry is too close to the bend line; fall back
                    # to direct face contact as a tie-breaker.
                    is_stationary = (
                        pieces[parent_pi].distToShape(
                            cut_faces[fi])[0] < GEOMETRY_TOLERANCE)
            # Reuse mi if partner face already processed
            if sid is not None and sid in sid_to_mi:
                mi = sid_to_mi[sid]
                face_to_micro[fi] = mi
                # Keep the canonical segment geometry anchored on the
                # stationary-side face. Wedge build slices from cut_mid
                # along normal, so picking the moving-side partner can
                # leave every slice plane outside the wedge.
                if is_stationary and not mi_has_stationary_face.get(mi, False):
                    data = cut_plan_data[fi]
                    angle_rad, bend_obj_ref, cut_mid, normal_ref, \
                        radius, _ = data
                    micro_bend_info[mi] = (angle_rad, bend_obj_ref,
                                           cut_mid, normal_ref,
                                           radius, bi)
                    mi_has_stationary_face[mi] = True
                    mids = bend_seg_mids.get(bi)
                    mis = bend_s_mis.get(bi)
                    if mids is not None and mis is not None:
                        try:
                            mid_idx = mis.index(mi)
                            mids[mid_idx] = FreeCAD.Vector(cut_mid)
                        except ValueError:
                            pass
                continue
            # Assign seg_idx from pairing: same sid → same idx
            if sid is not None and sid in sid_to_seg_idx:
                seg_idx = sid_to_seg_idx[sid]
            else:
                seg_idx = bend_seg_count.get(bi, 0)
                bend_seg_count[bi] = seg_idx + 1
                if sid is not None:
                    sid_to_seg_idx[sid] = seg_idx
            # Create micro-bend entry for this cut segment
            mi = len(micro_bend_info)
            s_group[(bi, fi)] = mi
            mi_seg_idx[mi] = seg_idx
            if sid is not None:
                sid_to_mi[sid] = mi
                mi_to_sid[mi] = sid
            mi_has_stationary_face[mi] = is_stationary
            data = cut_plan_data[fi]
            angle_rad, bend_obj_ref, cut_mid, normal_ref, \
                radius, _ = data
            # Store initial normal (pointing away from board center).
            # Crossing direction sign is determined later from BFS.
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
                mi_seg_idx=mi_seg_idx,
                cached_geo_crossings=geo_crossings,
                strip_pieces=strip_pieces,
                face_to_seg=face_to_seg,
                joints=joints)
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] BFS (final): "
            f"{_time.time() - _t_bfs2:.3f}s\n")

        # Promote tiny same-set leaf slivers to strip pieces. These can be
        # created by generalFuse as terminal crumbs that stay inside a bend
        # band but never got caught by the first strip pass because their
        # center of mass or touched cut topology is skewed by the split.
        mi_to_sid = {mi: sid for sid, mi in sid_to_mi.items()}
        child_count = {}
        for _pi, _entry in bfs_tree.items():
            parent_pi = _entry[0]
            if parent_pi is not None:
                child_count[parent_pi] = child_count.get(parent_pi, 0) + 1

        def _resolve_leaf_strip_source(parent_pi, promote_mi, candidate_mode):
            # Leaf strips should reuse the immediate parent as their rigid
            # source frame. Trailing exit leaves already inherit the moving-side
            # orientation from that parent; trying to source them from an
            # earlier stationary ancestor detaches them from the parent instead.
            return parent_pi

        for pi in range(len(pieces)):
            if pi in strip_pieces:
                continue
            entry = bfs_tree.get(pi)
            if entry is None or entry[0] is None:
                continue
            parent_pi = entry[0]
            if child_count.get(pi, 0) != 0:
                continue
            if len(adjacency[pi]) != 1:
                continue
            bends_pi = piece_bend_sets[pi]
            bends_parent = piece_bend_sets[parent_pi]
            if (bends_pi is None or bends_parent is None
                    or bends_pi != bends_parent):
                continue
            # Keep this heuristic narrowly targeted at tiny terminal slivers.
            if pieces[pi].Volume > 0.15:
                continue

            candidate_infos = []
            seen_candidates = set()

            def _append_candidate(mode, bi_val, sid_val, mi_val=None):
                if sid_val is None:
                    return
                key = (mode, bi_val, sid_val, mi_val)
                if key in seen_candidates:
                    return
                seen_candidates.add(key)
                candidate_infos.append((mode, bi_val, sid_val, mi_val))

            for mi_val in sorted(entry[1]):
                if (mi_val >= 0
                        and micro_bend_info[mi_val][5] in bends_parent):
                    _append_candidate(
                        'own-entry',
                        micro_bend_info[mi_val][5],
                        mi_to_sid.get(mi_val),
                        mi_val)
                    break

            shared_edge_is_unlabeled = adjacency[pi][0][1] < 0

            if not candidate_infos and entry[2] is None:
                parent_entry = bfs_tree.get(parent_pi)
                if parent_entry is not None:
                    parent_mis = parent_entry[1]
                    for mi_val in sorted(parent_mis, reverse=True):
                        if mi_val < 0:
                            continue
                        if micro_bend_info[mi_val][5] not in bends_parent:
                            continue
                        if -(mi_val + 2) not in parent_mis:
                            continue
                        _append_candidate(
                            'parent-balance',
                            micro_bend_info[mi_val][5],
                            mi_to_sid.get(mi_val),
                            mi_val)
                        break
                    parent_wedge_pi = parent_entry[2]
                    parent_wedge_bi = strip_to_bend.get(parent_wedge_pi)
                    if not candidate_infos and parent_wedge_bi is not None:
                        for mi_val in sorted(parent_mis):
                            if mi_val > -2:
                                continue
                            pos_mi = -(mi_val + 2)
                            if pos_mi < 0:
                                continue
                            if micro_bend_info[pos_mi][5] != parent_wedge_bi:
                                continue
                            _append_candidate(
                                'parent-exit',
                                parent_wedge_bi,
                                mi_to_sid.get(pos_mi),
                                pos_mi)
                    if parent_wedge_bi is not None:
                        wedge_entry = bfs_tree.get(parent_wedge_pi)
                        if wedge_entry is not None:
                            # Branched wedges can spill into a same-side leaf
                            # through a neighboring sid that is still part of
                            # the parent wedge's own local crossing set.
                            for wedge_mi in sorted(wedge_entry[1]):
                                if wedge_mi >= 0:
                                    pos_mi = wedge_mi
                                else:
                                    pos_mi = -(wedge_mi + 2)
                                if pos_mi < 0:
                                    continue
                                if micro_bend_info[pos_mi][5] != parent_wedge_bi:
                                    continue
                                _append_candidate(
                                    'parent-wedge-mis',
                                    parent_wedge_bi,
                                    mi_to_sid.get(pos_mi),
                                    pos_mi)

            if not candidate_infos:
                continue

            promoted = False
            trailing_candidate = None
            for (candidate_mode,
                 promote_bi,
                 promote_sid,
                 promote_mi) in candidate_infos:
                if (candidate_mode in ('parent-balance', 'parent-exit',
                                       'parent-wedge-mis')
                        and pieces[pi].Volume > 0.02):
                    continue
                seg_p0, seg_p1 = joints[promote_sid]['center']
                seg_dx = seg_p1.x - seg_p0.x
                seg_dy = seg_p1.y - seg_p0.y
                seg_len = math.sqrt(seg_dx * seg_dx + seg_dy * seg_dy)
                if seg_len < 1e-12:
                    continue
                metrics = _piece_segment_debug_metrics(
                    pieces[pi], seg_p0, seg_p1)
                band_margin = max(
                    GEOMETRY_TOLERANCE,
                    min(insets[promote_bi] * 0.15, 0.05))
                band_limit = insets[promote_bi] + band_margin
                within_line_band = (
                    math.isfinite(metrics['line_d_max'])
                    and metrics['line_d_max']
                    <= band_limit)
                if wedge_assign_diag:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: leaf-strip candidate p{pi}"
                        f" mode={candidate_mode}"
                        f" bend={promote_bi}"
                        f" sid={promote_sid}"
                        f" vol={pieces[pi].Volume:.6f}"
                        f" line_d_max={metrics['line_d_max']:.6f}"
                        f" limit={band_limit:.6f}"
                        f" pass={within_line_band}\n")
                if not within_line_band:
                    if (trailing_candidate is None
                            and shared_edge_is_unlabeled
                            and pieces[pi].Volume <= 0.02
                            and candidate_mode in (
                                'parent-exit',
                                'parent-wedge-mis')
                            and promote_mi is not None):
                        trailing_candidate = (
                            candidate_mode,
                            promote_bi,
                            promote_sid,
                            promote_mi)
                    continue
                strip_pieces.add(pi)
                strip_to_bend[pi] = promote_bi
                strip_to_seg[pi] = promote_sid
                source_pi = None
                if promote_mi is not None:
                    strip_seed_mi[pi] = promote_mi
                    strip_seed_parent[pi] = parent_pi
                    source_pi = _resolve_leaf_strip_source(
                        parent_pi, promote_mi, candidate_mode)
                    strip_seed_source[pi] = source_pi
                if pi not in joints[promote_sid]['wedges']:
                    joints[promote_sid]['wedges'].append(pi)
                if wedge_assign_diag:
                    source_msg = ""
                    if promote_mi is not None:
                        source_msg = (
                            f" parent={parent_pi}"
                            f" source={source_pi}")
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: promoted leaf strip p{pi}"
                        f" via {candidate_mode}"
                        f" bend={promote_bi}"
                        f" sid={promote_sid}"
                        f"{source_msg}\n")
                promoted = True
                break
            if not promoted and trailing_candidate is not None:
                (candidate_mode,
                 promote_bi,
                 promote_sid,
                 promote_mi) = trailing_candidate
                strip_pieces.add(pi)
                strip_to_bend[pi] = promote_bi
                strip_to_seg[pi] = promote_sid
                strip_seed_mi[pi] = promote_mi
                strip_seed_parent[pi] = parent_pi
                source_pi = _resolve_leaf_strip_source(
                    parent_pi, promote_mi, candidate_mode)
                strip_seed_source[pi] = source_pi
                if pi not in joints[promote_sid]['wedges']:
                    joints[promote_sid]['wedges'].append(pi)
                if wedge_assign_diag:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: promoted leaf strip p{pi}"
                        f" via {candidate_mode}-trailing"
                        f" bend={promote_bi}"
                        f" sid={promote_sid}"
                        f" mi={promote_mi}"
                        f" parent={parent_pi}"
                        f" source={source_pi}\n")
                promoted = True
            if not promoted:
                continue

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

        def _classified_piece_bends(pi):
            if pi is None or pi < 0 or pi >= len(piece_bend_sets):
                return None
            bends = piece_bend_sets[pi]
            if bends is None:
                return None
            return bends

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
                        bends_a = _classified_piece_bends(pi_a)
                        if bends_a is None:
                            continue
                        for nbr, mi_adj, _fi in adjacency[pi_a]:
                            if mi_adj != mi_own:
                                continue
                            bends_nbr = _classified_piece_bends(nbr)
                            if bends_nbr is None:
                                continue
                            # pi_a and nbr separated by mi_own
                            if mi_own not in bends_a:
                                stat_pi = pi_a
                            elif mi_own not in bends_nbr:
                                stat_pi = nbr
                            if stat_pi is not None:
                                break
                        if stat_pi is not None:
                            break
                    if stat_pi is not None:
                        break
                if stat_pi is not None:
                    found_pi = stat_pi

            assigned_bends = _classified_piece_bends(found_pi)
            if assigned_bends is not None:
                bendline_bend_sets[child.Name] = \
                    assigned_bends.copy()
                bendline_piece_idx[child.Name] = found_pi
            else:
                if found_pi is not None:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD: bendline {child.Name}"
                        f" maps to unclassified piece {found_pi};"
                        f" using geometric bend-set fallback\n")
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
        wedge_diag = getattr(obj, 'BuildDebugObjects', False)

        # strip_pieces and strip_to_bend already computed before BFS.

        # Map each wedge piece to its canonical entry S-cut mi
        # (the mi through which BFS first reached the wedge).  Also keep
        # reverse maps for every positive mi:
        # - mi_to_wpis: which wedges this mi traverses, if any
        # - mi_to_stationary_pi: the stationary-side/source piece for the
        #   specific positive crossing. On multi-exit wedges this is not
        #   necessarily the wedge's original BFS parent.
        strip_to_mi = {}
        mi_to_wpis = {}
        mi_to_stationary_pi = {}
        for wpi in sorted(strip_seed_mi):
            mi_val = strip_seed_mi[wpi]
            if (mi_val < 0
                    or micro_bend_info[mi_val][5] != strip_to_bend[wpi]):
                continue
            strip_to_mi[wpi] = mi_val
            mi_to_wpis.setdefault(mi_val, set()).add(wpi)
            source_parent = strip_seed_source.get(
                wpi, strip_seed_parent.get(wpi))
            if source_parent is not None:
                mi_to_stationary_pi.setdefault(mi_val, source_parent)
        for wpi in strip_pieces:
            if wpi in bfs_tree:
                parent_pi, mis_crossed, _ = bfs_tree[wpi]
                for mi_val in mis_crossed:
                    if (mi_val >= 0
                            and micro_bend_info[mi_val][5]
                            == strip_to_bend[wpi]):
                        strip_to_mi[wpi] = mi_val
                        mi_to_wpis.setdefault(mi_val, set()).add(wpi)
                        if parent_pi is not None:
                            mi_to_stationary_pi.setdefault(
                                mi_val, parent_pi)
                        break

        def _find_parent_chain_mi(pi, bend_idx):
            """Find the nearest positive mi for *bend_idx* in pi's parent chain.

            Some sliver wedges enter BFS through a phantom split face, so their
            own entry crossing is unlabeled even though they belong to a real
            bend. Fall back to the nearest ancestor crossing for the same bend
            so the wedge can still reuse the correct micro-bend geometry.
            """
            seen = set()
            cur = pi
            while cur is not None and cur not in seen:
                seen.add(cur)
                entry = bfs_tree.get(cur)
                if entry is None:
                    break
                parent_pi = entry[0]
                for mi_val in sorted(entry[1], reverse=True):
                    if (mi_val >= 0
                            and micro_bend_info[mi_val][5] == bend_idx):
                        return mi_val, parent_pi
                cur = parent_pi
            return None, None

        for wpi in sorted(strip_pieces):
            if wpi in strip_to_mi:
                continue
            entry = bfs_tree.get(wpi)
            if entry is None or entry[0] is None:
                continue
            mi_val, source_parent = _find_parent_chain_mi(
                entry[0], strip_to_bend[wpi])
            if mi_val is None:
                continue
            strip_to_mi[wpi] = mi_val
            mi_to_wpis.setdefault(mi_val, set()).add(wpi)
            if source_parent is not None:
                mi_to_stationary_pi.setdefault(mi_val, source_parent)
        for pi, entry in bfs_tree.items():
            if pi in strip_pieces:
                continue
            parent_pi = entry[0]
            wedge_pi = entry[2]
            if wedge_pi is None:
                for mi_val in entry[1]:
                    if mi_val >= 0 and parent_pi is not None:
                        mi_to_stationary_pi.setdefault(
                            mi_val, parent_pi)
                continue
            for mi_val in entry[1]:
                if (mi_val >= 0
                        and micro_bend_info[mi_val][5]
                        == strip_to_bend[wedge_pi]):
                    mi_to_wpis.setdefault(mi_val, set()).add(wedge_pi)
                    if parent_pi is not None:
                        mi_to_stationary_pi.setdefault(
                            mi_val, parent_pi)

        wedge_to_moving_pis = {}
        if wedge_diag:
            for pi, entry in bfs_tree.items():
                if pi in strip_pieces:
                    continue
                wedge_pi = entry[2]
                if wedge_pi is None:
                    continue
                wedge_to_moving_pis.setdefault(
                    wedge_pi, []).append(pi)
            for wpi in wedge_to_moving_pis:
                wedge_to_moving_pis[wpi].sort()

        # Compute per-mi crossing direction sign.
        # +1 if crossing goes in initial normal direction, -1 otherwise.
        mi_sign = {}
        for pi, entry in bfs_tree.items():
            parent = entry[0]
            if parent is None:
                continue
            mis_crossed = entry[1]
            for mi in mis_crossed:
                if mi < 0 or mi in mi_sign:
                    continue
                _, _, cut_mid, normal, _, _ = micro_bend_info[mi]
                sid = mi_to_sid.get(mi)
                if sid is not None and joints is not None:
                    seg_p0, seg_p1 = joints[sid]['center']
                    parent_pt = _piece_local_side_point(
                        pieces[parent], seg_p0, seg_p1)
                    ref_pt = (seg_p0 + seg_p1) * 0.5
                else:
                    parent_pt = pieces[parent].CenterOfMass
                    ref_pt = cut_mid
                dot_val = (parent_pt - ref_pt).dot(normal)
                if abs(dot_val) <= max(GEOMETRY_TOLERANCE * 10.0, 1e-4):
                    parent_cm = pieces[parent].CenterOfMass
                    dot_val = (parent_cm - cut_mid).dot(normal)
                # Parent on +normal side → crossing goes in -normal
                mi_sign[mi] = -1 if dot_val > 0 else 1

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
        # Only positive mi values (stationary-side crossings) are
        # included; negative values (wedge exit crossings) are skipped.
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

        # Wedge chains: BFS parent's chain + own mi.
        # The BFS parent is always the stationary-side piece.
        for wpi in strip_pieces:
            mi_w = strip_to_mi.get(wpi)
            if mi_w is not None and wpi in bfs_tree:
                wpi_parent = bfs_tree[wpi][0]
                if wpi_parent is not None:
                    parent_chain = piece_mi_list[wpi_parent]
                    parent_entry = bfs_tree.get(wpi_parent)
                    parent_crossed_exit = (
                        parent_entry is not None
                        and (-(mi_w + 2)) in parent_entry[1])
                    # Borrowed terminal slivers can inherit a bend entry that
                    # already terminates the parent's chain, or they may hang
                    # off the parent's moving-side exit for the same segment.
                    # In both cases, don't append the positive mi again or the
                    # strip gets an extra rigid rotation that detaches it from
                    # its parent.
                    if ((parent_chain and parent_chain[-1] == mi_w)
                            or parent_crossed_exit):
                        piece_mi_list[wpi] = list(parent_chain)
                    else:
                        piece_mi_list[wpi] = parent_chain + [mi_w]

        for pi in range(len(pieces)):
            angles = [f"{math.degrees(micro_bend_info[mi][0]):.1f}°"
                      for mi in piece_mi_list[pi]]
            _log_bending_bfs(
                f"FreekiCAD: piece_mi_list[{pi}]"
                f" = {piece_mi_list[pi]}"
                f" angles={angles}"
                f" strip={pi in strip_pieces}\n")

        # Helper: does piece pi rotate at this step?
        def _at_step(pi, step_pos, mi):
            return (step_pos < len(piece_mi_list[pi])
                    and piece_mi_list[pi][step_pos] == mi)

        max_chain_len = max(
            (len(lst) for lst in piece_mi_list), default=0)

        def _piece_path_labels(pi):
            # Wedges accumulate extra exit crossings in bfs_tree when
            # later traversals pass through them, so their display path
            # must use the canonical entry mi instead of the full set.
            path = []
            cur = pi
            while cur is not None:
                entry = bfs_tree.get(cur)
                if entry is None:
                    break
                parent = entry[0]
                mis_crossed = entry[1]
                if parent is not None:
                    if cur in strip_pieces:
                        mi_w = strip_to_mi.get(cur)
                        crossings = [mi_w] if mi_w is not None else \
                            sorted(mis_crossed)
                    else:
                        crossings = sorted(mis_crossed)
                    for bi_crossed in crossings:
                        pos = -(bi_crossed + 2) \
                            if bi_crossed <= -2 else bi_crossed
                        if pos < 0:
                            continue
                        orig_bi = micro_bend_info[pos][5]
                        seg = mi_seg_idx.get(pos, 0)
                        prefix = "-" if bi_crossed <= -2 else ""
                        path.append(f"{prefix}{orig_bi}.{seg}")
                cur = parent
            path.reverse()
            return path

        # Log piece_mi_list for neighbours of stationary piece
        for pi in range(len(pieces)):
            entry = bfs_tree.get(pi)
            if entry is not None and entry[0] == stationary_idx:
                _log_bending_bfs(
                    f"FreekiCAD: piece_mi_list[{pi}]"
                    f" (neighbour of fixed p{stationary_idx})"
                    f" = {piece_mi_list[pi]}"
                    f" strip={pi in strip_pieces}\n")
            # Also log if entry[2] is wedge that connects to fixed
            if (entry is not None and entry[2] is not None
                    and entry[0] == stationary_idx):
                _log_bending_bfs(
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
            normal = normal * mi_sign.get(mi, 1)
            if abs(micro_angle) < 1e-9 or not enable_bending \
                    or not bend_obj.Active:
                continue
            first_mi_occurrence = mi not in mi_wedge_processed
            mi_wedge_processed.add(mi)

            plc = bend_obj.Placement

            # Find the stationary/source piece for this specific
            # positive-mi crossing. Its accumulated piece_plc will be
            # used to build virtual_plc for the cut geometry transform.
            # On multi-exit wedges this can differ from the wedge's
            # original BFS parent, but the parent side of the crossing
            # is always the stationary one.
            s_parent_pi = mi_to_stationary_pi.get(mi)

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
            _log_bending_bfs(
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

            _log_bending_bfs(
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
                    _log_bending_bfs(
                        f"FreekiCAD:   mi {mi} rotated"
                        f" p{pi} (fixed-nbr):"
                        f" z {pre_cm.z:.4f}"
                        f" → {post_cm.z:.4f}\n")
            _log_bending_bfs(
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
                    _log_bending_bfs(
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
                        if not _at_step(pi, step_pos, mi):
                            continue
                        # Wedge geometry is rebuilt later from the
                        # pre-mi snapshot, but their final rigid
                        # target still needs the same inset
                        # correction as neighboring pieces.  Because
                        # wedge_post_mi_plc was saved before this
                        # translation, the remaining_plc applied after
                        # loft reconstruction will carry the same
                        # correction back onto the rebuilt wedge.
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

        # Debug wedge nodes should follow the accumulated rigid
        # transform chain from Phase 3 regardless of whether the later
        # loft succeeds or changes the wedge geometry centroid.
        debug_piece_centers = {}
        if wedge_diag:
            for wpi in strip_pieces:
                try:
                    debug_piece_centers[wpi] = piece_plc[wpi].multVec(
                        pieces[wpi].CenterOfMass)
                except Exception:
                    pass

        # Build wedge shapes using explicit `Smooth`
        # or `Wireframe`.
        _t_loft = _time.time()
        wedge_mode = self._get_wedge_mode(obj)
        is_wireframe_wedge = (wedge_mode == "Wireframe")
        wedge_target_edge_splits = self._get_wedge_target_edge_splits(
            wedge_mode)
        # N_SLICES per wedge: at least 16, or 1 per degree
        # (computed per wedge below)
        coc_offsets = {}  # bi → (bend_obj, first_s_mi)

        def _shape_volume(shape):
            try:
                return float(shape.Volume)
            except Exception:
                return 0.0

        def _shape_center(shape):
            try:
                return shape.CenterOfMass
            except Exception:
                pass
            try:
                bb = shape.BoundBox
                if getattr(bb, "isValid", lambda: False)():
                    return bb.Center
            except Exception:
                pass
            pts = [v.Point for v in getattr(shape, "Vertexes", [])]
            if pts:
                center = FreeCAD.Vector()
                for pt in pts:
                    center += pt
                return center * (1.0 / len(pts))
            return FreeCAD.Vector()

        def _vec_length(vec):
            try:
                return float(vec.Length)
            except Exception:
                return math.sqrt(
                    vec.x * vec.x + vec.y * vec.y + vec.z * vec.z)

        def _fmt_vec(vec, prec=6):
            return (
                f"({vec.x:.{prec}f},"
                f"{vec.y:.{prec}f},"
                f"{vec.z:.{prec}f})")

        def _closest_point_on_shape(shape, ref_pt):
            try:
                _, pts, _ = shape.distToShape(Part.Vertex(ref_pt))
                if pts and pts[0]:
                    return pts[0][0]
            except Exception:
                pass
            best = None
            best_d = float('inf')
            for v in getattr(shape, "Vertexes", []):
                try:
                    d = v.Point.distanceToPoint(ref_pt)
                except Exception:
                    continue
                if d < best_d:
                    best_d = d
                    best = v.Point
            return best if best is not None else _shape_center(shape)

        def _shape_distance(shape_a, shape_b):
            try:
                return float(shape_a.distToShape(shape_b)[0])
            except Exception:
                return float('nan')

        def _normalize_wire_sequence(seq):
            if len(seq) < 2:
                return seq
            ref_pts = [v.Point for v in seq[0].Vertexes]
            n_v = len(ref_pts)
            for wi in range(1, len(seq)):
                w = seq[wi]
                cur_pts = [v.Point for v in w.Vertexes]
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
                                or total < best_dist):
                            best_dist = total
                            best_pts = rotated
                if any(
                    (best_pts[k]
                     - cur_pts[k]).Length > 1e-9
                        for k in range(n_v)):
                    poly_pts = list(best_pts) + [best_pts[0]]
                    seq[wi] = Part.makePolygon(poly_pts)
                ref_pts = best_pts
            return seq

        def _match_wires_by_center(ref_wires, cand_wires):
            remaining = list(cand_wires)
            ordered = []
            for ref_wire in ref_wires:
                if not remaining:
                    break
                ref_center = _shape_center(ref_wire)
                best_idx = min(
                    range(len(remaining)),
                    key=lambda idx: _shape_center(
                        remaining[idx]
                    ).distanceToPoint(ref_center))
                ordered.append(remaining.pop(best_idx))
            return ordered

        def _harmonize_boundary_slices(seg_slices, si_sub):
            counts = [len(slice_wires) for _d, slice_wires in seg_slices]
            if len(set(counts)) <= 1 or len(seg_slices) < 2:
                return seg_slices

            interior_counts = counts[1:-1]
            if interior_counts and len(set(interior_counts)) == 1:
                target_count = interior_counts[0]
            else:
                freq = {}
                for count in counts:
                    freq[count] = freq.get(count, 0) + 1
                target_count = max(
                    freq.items(),
                    key=lambda item: (item[1], -item[0]))[0]

            adjusted = list(seg_slices)
            changed = False

            if (len(adjusted[0][1]) > target_count
                    and len(adjusted[1][1]) == target_count):
                keep = _match_wires_by_center(
                    adjusted[1][1], adjusted[0][1])
                if len(keep) == target_count:
                    adjusted[0] = (adjusted[0][0], keep)
                    changed = True

            if (len(adjusted[-1][1]) > target_count
                    and len(adjusted[-2][1]) == target_count):
                keep = _match_wires_by_center(
                    adjusted[-2][1], adjusted[-1][1])
                if len(keep) == target_count:
                    adjusted[-1] = (adjusted[-1][0], keep)
                    changed = True

            if changed:
                new_counts = [
                    len(slice_wires)
                    for _d, slice_wires in adjusted
                ]
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   boundary-adjust sub={si_sub}"
                    f" target={target_count}"
                    f" counts={counts} -> {new_counts}\n")
            return adjusted

        def _shape_is_same(shape_a, shape_b):
            try:
                return shape_a.isSame(shape_b)
            except Exception:
                return False

        def _shape_in_list(shape, shapes):
            for cand in shapes:
                if _shape_is_same(shape, cand):
                    return True
            return False

        def _append_unique_shape(seq, shape):
            if not _shape_in_list(shape, seq):
                seq.append(shape)

        def _collect_unique_edges(faces):
            edges = []
            for face in faces:
                for edge in getattr(face, 'Edges', []):
                    _append_unique_shape(edges, edge)
            return edges

        def _collect_unique_wires(faces):
            wires = []
            for face in faces:
                for wire in getattr(face, 'Wires', []):
                    _append_unique_shape(wires, wire)
            return wires

        def _face_normal(face):
            try:
                u0, u1, v0, v1 = face.ParameterRange
                n = face.normalAt(
                    0.5 * (u0 + u1), 0.5 * (v0 + v1))
                if getattr(n, 'Length', 0.0) > 1e-9:
                    n.normalize()
                    return n
            except Exception:
                pass
            pts = [v.Point for v in getattr(face, 'Vertexes', [])]
            if len(pts) >= 3:
                for idx in range(1, len(pts) - 1):
                    n = (pts[idx] - pts[0]).cross(
                        pts[idx + 1] - pts[0])
                    if getattr(n, 'Length', 0.0) > 1e-9:
                        n.normalize()
                        return n
            return None

        def _dedupe_point_sequence(points, tol=1e-7):
            deduped = []
            for pt in points:
                pt = FreeCAD.Vector(pt)
                if (not deduped
                        or pt.distanceToPoint(deduped[-1]) > tol):
                    deduped.append(pt)
            return deduped

        def _weld_point(pt, welded_points, tol):
            pt = FreeCAD.Vector(pt)
            for existing in welded_points:
                try:
                    if pt.distanceToPoint(existing) <= tol:
                        return existing
                except Exception:
                    continue
            welded_points.append(pt)
            return welded_points[-1]

        def _lookup_welded_source_vertex(
                source_vertex, wedge_ctx, vertex_cache,
                welded_points, weld_tol):
            for cached_vertex, cached_pt in vertex_cache:
                if _shape_is_same(source_vertex, cached_vertex):
                    return cached_pt
            bent_pt = _bend_wedge_point(source_vertex.Point, wedge_ctx)
            canonical_pt = _weld_point(
                bent_pt, welded_points, weld_tol)
            vertex_cache.append((source_vertex, canonical_pt))
            return canonical_pt

        def _extract_flat_wedge_profile(wedge_ctx):
            flat_shape = wedge_ctx['positioned_flat']
            cur_up = FreeCAD.Vector(wedge_ctx['cur_up'])
            faces = list(getattr(flat_shape, 'Faces', []))
            top_faces = []
            bottom_faces = []
            side_faces = []

            for face in faces:
                n = _face_normal(face)
                dot_up = n.dot(cur_up) if n is not None else 0.0
                if dot_up > 0.5:
                    top_faces.append(face)
                elif dot_up < -0.5:
                    bottom_faces.append(face)
                else:
                    side_faces.append(face)

            if (faces
                    and (not top_faces or not bottom_faces)):
                by_height = sorted(
                    faces,
                    key=lambda face: _shape_center(face).dot(cur_up))
                top_faces = [by_height[-1]]
                bottom_faces = [by_height[0]]
                side_faces = []
                for face in faces:
                    if (_shape_is_same(face, top_faces[0])
                            or _shape_is_same(face, bottom_faces[0])):
                        continue
                    side_faces.append(face)

            top_wires = _collect_unique_wires(top_faces)
            bottom_wires = _collect_unique_wires(bottom_faces)
            top_edges = _collect_unique_edges(top_faces)
            bottom_edges = _collect_unique_edges(bottom_faces)

            side_pairs = []
            connector_edges = []
            for side_idx, side_face in enumerate(side_faces):
                face_top_edges = []
                face_bottom_edges = []
                for edge in getattr(side_face, 'Edges', []):
                    if _shape_in_list(edge, top_edges):
                        _append_unique_shape(face_top_edges, edge)
                    elif _shape_in_list(edge, bottom_edges):
                        _append_unique_shape(face_bottom_edges, edge)
                    else:
                        _append_unique_shape(connector_edges, edge)

                remaining_bottom = list(face_bottom_edges)
                for top_edge in face_top_edges:
                    if not remaining_bottom:
                        break
                    top_center = _shape_center(top_edge)
                    best_idx = min(
                        range(len(remaining_bottom)),
                        key=lambda idx: _shape_center(
                            remaining_bottom[idx]
                        ).distanceToPoint(top_center))
                    bottom_edge = remaining_bottom.pop(best_idx)
                    side_pairs.append({
                        'face': side_face,
                        'face_index': side_idx,
                        'top_edge': top_edge,
                        'bottom_edge': bottom_edge,
                    })

            wireframe_edges = []
            for edge in top_edges + bottom_edges + connector_edges:
                _append_unique_shape(wireframe_edges, edge)

            profile = {
                'top_faces': top_faces,
                'bottom_faces': bottom_faces,
                'side_faces': side_faces,
                'top_wires': top_wires,
                'bottom_wires': bottom_wires,
                'top_edges': top_edges,
                'bottom_edges': bottom_edges,
                'connector_edges': connector_edges,
                'side_pairs': side_pairs,
                'wireframe_edges': wireframe_edges,
            }
            if wedge_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   profile"
                    f" top_faces={len(top_faces)}"
                    f" bottom_faces={len(bottom_faces)}"
                    f" side_faces={len(side_faces)}"
                    f" top_wires={len(top_wires)}"
                    f" bottom_wires={len(bottom_wires)}"
                    f" pairs={len(side_pairs)}"
                    f" connectors={len(connector_edges)}\n")
            return profile

        def _make_wedge_build_context(
                pi, bi, s_mi, positioned_flat, target_shape,
                flat_cm, target_cm, target_vol, cur_p0,
                cur_normal, cur_up, bend_axis, coc, sweep_angle,
                ins, near_d, far_d, far_span, near_ref, far_ref,
                slice_count, target_edge_splits,
                anchor_post_plc=None,
                anchor_ref_near=None, anchor_ref_far=None,
                anchor_target_near=None, anchor_target_far=None):
            angle_deg = abs(math.degrees(sweep_angle))
            feature_candidates = []
            try:
                bbox = positioned_flat.BoundBox
                for length in (
                        float(getattr(bbox, 'XLength', 0.0)),
                        float(getattr(bbox, 'YLength', 0.0)),
                        float(getattr(bbox, 'ZLength', 0.0))):
                    if length > 1e-6:
                        feature_candidates.append(length)
            except Exception:
                pass
            for candidate in (abs(far_span), abs(ins) * 2.0):
                if candidate > 1e-6:
                    feature_candidates.append(candidate)
            for edge in getattr(positioned_flat, 'Edges', []):
                try:
                    edge_len = float(edge.Length)
                except Exception:
                    edge_len = 0.0
                if edge_len > 1e-6:
                    feature_candidates.append(edge_len)
            feature_size = (
                min(feature_candidates)
                if feature_candidates
                else GEOMETRY_TOLERANCE * 50.0)
            close_tol = max(
                1e-6,
                min(GEOMETRY_TOLERANCE, feature_size * 0.02))
            weld_tol = max(
                1e-6,
                min(GEOMETRY_TOLERANCE * 0.1, close_tol * 0.25))
            face_area_tol = max(1e-12, close_tol * close_tol * 0.25)
            fix_tol = close_tol
            fix_max_tol = max(
                fix_tol,
                min(GEOMETRY_TOLERANCE * 10.0,
                    max(feature_size * 0.05, fix_tol * 4.0)))
            anchor_tol = max(
                0.02,
                min(0.25, max(feature_size * 0.05, close_tol * 10.0)))
            sweep_ref_d = 0.0
            sweep_span = abs(ins) * 2.0
            if sweep_span <= 1e-6:
                sweep_span = abs(far_span)
            return {
                'pi': pi,
                'bi': bi,
                's_mi': s_mi,
                'positioned_flat': positioned_flat,
                'target_shape': target_shape,
                'flat_cm': flat_cm,
                'target_cm': target_cm,
                'target_vol': target_vol,
                'cur_p0': FreeCAD.Vector(cur_p0),
                'cur_normal': FreeCAD.Vector(cur_normal),
                'cur_up': FreeCAD.Vector(cur_up),
                'bend_axis': FreeCAD.Vector(bend_axis),
                'coc': FreeCAD.Vector(coc),
                'sweep_angle': sweep_angle,
                'ins': ins,
                'near_d': near_d,
                'far_d': far_d,
                'far_span': far_span,
                'sweep_ref_d': sweep_ref_d,
                'sweep_span': sweep_span,
                'near_ref': FreeCAD.Vector(near_ref),
                'far_ref': FreeCAD.Vector(far_ref),
                'anchor_post_plc': (
                    anchor_post_plc.copy()
                    if anchor_post_plc is not None
                    else None),
                'anchor_ref_near': FreeCAD.Vector(
                    anchor_ref_near
                    if anchor_ref_near is not None
                    else near_ref),
                'anchor_ref_far': FreeCAD.Vector(
                    anchor_ref_far
                    if anchor_ref_far is not None
                    else far_ref),
                'anchor_target_near': (
                    FreeCAD.Vector(anchor_target_near)
                    if anchor_target_near is not None
                    else None),
                'anchor_target_far': (
                    FreeCAD.Vector(anchor_target_far)
                    if anchor_target_far is not None
                    else None),
                'anchor_tolerance': anchor_tol,
                'slice_count': max(int(slice_count), 1),
                'target_edge_splits': max(int(target_edge_splits), 1),
                'edge_samples': max(
                    6,
                    int(math.ceil(angle_deg / 8.0)) + 2),
                'tolerances': {
                    'feature_size': feature_size,
                    'weld': weld_tol,
                    'close': close_tol,
                    'face_area': face_area_tol,
                    'fix': fix_tol,
                    'fix_max': fix_max_tol,
                },
            }

        def _bend_wedge_point(pt, wedge_ctx):
            pt = FreeCAD.Vector(pt)
            cur_p0 = wedge_ctx['cur_p0']
            cur_normal = wedge_ctx['cur_normal']
            bend_axis = wedge_ctx['bend_axis']
            coc = wedge_ctx['coc']
            sweep_angle = wedge_ctx['sweep_angle']
            d = (pt - cur_p0).dot(cur_normal)
            frac = _wedge_sweep_fraction(d, wedge_ctx)
            flat_pt = pt + cur_normal * (-d)
            angle_deg = math.degrees(frac * sweep_angle)
            if abs(angle_deg) <= 1e-9:
                return flat_pt
            rot = FreeCAD.Rotation(bend_axis, angle_deg)
            return coc + rot.multVec(flat_pt - coc)

        def _constant_d_tolerance(wedge_ctx):
            tol_cfg = wedge_ctx.get('tolerances') or {}
            far_span = abs(float(wedge_ctx.get('far_span', 0.0) or 0.0))
            return max(
                1e-6,
                float(tol_cfg.get('close', GEOMETRY_TOLERANCE)),
                far_span * 1e-5)

        def _wedge_sweep_fraction(d, wedge_ctx):
            sweep_ref_d = float(wedge_ctx.get('sweep_ref_d', 0.0) or 0.0)
            sweep_span = abs(float(wedge_ctx.get('sweep_span', 0.0) or 0.0))
            if sweep_span <= 1e-9:
                sweep_ref_d = float(wedge_ctx.get('near_d', 0.0) or 0.0)
                sweep_span = abs(
                    float(wedge_ctx.get('far_span', 0.0) or 0.0))
            frac = abs(d - sweep_ref_d) / sweep_span if sweep_span > 1e-9 \
                else 0.0
            return max(0.0, min(1.0, frac))

        def _shape_constant_projection_info(
                shape, wedge_ctx, direction, tol=None):
            pts = [
                FreeCAD.Vector(v.Point)
                for v in getattr(shape, 'Vertexes', [])
            ]
            if not pts:
                return False, None, 0.0
            if tol is None:
                tol = _constant_d_tolerance(wedge_ctx)
            cur_p0 = wedge_ctx['cur_p0']
            direction = FreeCAD.Vector(direction)
            ds = [(pt - cur_p0).dot(direction) for pt in pts]
            d_min = min(ds)
            d_max = max(ds)
            d_mid = sum(ds) / float(len(ds))
            return ((d_max - d_min) <= tol, d_mid, d_max - d_min)

        def _shape_constant_d_info(shape, wedge_ctx, tol=None):
            return _shape_constant_projection_info(
                shape, wedge_ctx, wedge_ctx['cur_normal'], tol=tol)

        def _shape_constant_s_info(shape, wedge_ctx, tol=None):
            return _shape_constant_projection_info(
                shape, wedge_ctx, wedge_ctx['bend_axis'], tol=tol)

        def _project_point_to_constant_axis_value(
                pt, wedge_ctx, axis, target_value):
            pt = FreeCAD.Vector(pt)
            axis = FreeCAD.Vector(axis)
            axis_len = getattr(axis, 'Length', 0.0)
            if axis_len <= 1e-12:
                return pt
            if abs(axis_len - 1.0) > 1e-9:
                axis = axis * (1.0 / axis_len)
            cur_p0 = wedge_ctx['cur_p0']
            cur_value = (pt - cur_p0).dot(axis)
            return pt + axis * (target_value - cur_value)

        def _transform_shape_for_wedge_d(shape, wedge_ctx, d):
            if shape is None:
                return None
            try:
                bent_shape = shape.copy()
            except Exception:
                bent_shape = shape

            cur_normal = wedge_ctx['cur_normal']
            bend_axis = wedge_ctx['bend_axis']
            coc = wedge_ctx['coc']
            sweep_angle = wedge_ctx['sweep_angle']
            frac = _wedge_sweep_fraction(d, wedge_ctx)
            angle_deg = math.degrees(frac * sweep_angle)

            try:
                bent_shape.translate(cur_normal * (-d))
            except Exception:
                return None
            if abs(angle_deg) > 1e-9:
                try:
                    rot = FreeCAD.Rotation(bend_axis, angle_deg)
                    plc = FreeCAD.Placement(
                        FreeCAD.Vector(0, 0, 0), rot, coc)
                    bent_shape.transformShape(plc.toMatrix())
                except Exception:
                    return None
            return bent_shape

        def _build_anchored_sweep_face(
                source_face, wedge_ctx, label, area_tol=1e-9):
            is_constant_d, face_d, face_span = _shape_constant_d_info(
                source_face, wedge_ctx)
            if not is_constant_d:
                return None

            bent_shape = _transform_shape_for_wedge_d(
                source_face, wedge_ctx, face_d)
            faces = _collect_shape_faces(bent_shape)
            if len(faces) != 1:
                return None
            face = faces[0]
            try:
                if getattr(face, 'Area', 0.0) <= area_tol:
                    return None
            except Exception:
                pass
            if wedge_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   curved {label}"
                    f" surface=anchored-sweep"
                    f" d={face_d:.6f}"
                    f" span={face_span:.6f}\n")
            return face

        def _build_constant_sweep_boundary_face(
                source_face, bent_pairs, wedge_ctx, label,
                close_tol=None, area_tol=1e-9):
            is_constant_s, face_s, face_span = _shape_constant_s_info(
                source_face, wedge_ctx)
            if not is_constant_s:
                return None

            bent_wires = []
            boundary_source = "exact"
            for source_wire in getattr(source_face, 'Wires', []):
                bent_wire = _build_constant_axis_closed_wire_from_source_edges(
                    source_wire,
                    bent_pairs,
                    wedge_ctx,
                    wedge_ctx['bend_axis'],
                    face_s,
                    close_tol=close_tol)
                if bent_wire is None:
                    bent_wire = _build_projected_closed_wire_from_source_edges(
                        source_wire,
                        bent_pairs,
                        wedge_ctx,
                        wedge_ctx['bend_axis'],
                        face_s,
                        close_tol=close_tol)
                    if bent_wire is not None:
                        boundary_source = "sampled"
                if bent_wire is None:
                    return None
                bent_wires.append(bent_wire)
            if not bent_wires:
                return None

            face = None
            if len(bent_wires) == 1:
                try:
                    face = Part.Face(bent_wires[0])
                except Exception:
                    face = None
            if face is None:
                face = _make_filled_face_from_wires(
                    bent_wires, area_tol=area_tol)
            if face is None:
                return None
            try:
                if getattr(face, 'Area', 0.0) <= area_tol:
                    return None
            except Exception:
                pass
            if wedge_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   curved {label}"
                    f" surface=sweep-boundary-plane"
                    f" s={face_s:.6f}"
                    f" span={face_span:.6f}"
                    f"{'' if boundary_source == 'exact' else ' source=sampled'}\n")
            return face

        def _build_exact_side_face(
                source_face, bent_pairs, wedge_ctx, label,
                close_tol=None, area_tol=1e-9):
            if close_tol is None:
                close_tol = GEOMETRY_TOLERANCE

            bent_wires = []
            for source_wire in getattr(source_face, 'Wires', []):
                bent_wire = _build_closed_wire_from_bent_edges(
                    source_wire, bent_pairs, close_tol=close_tol)
                if bent_wire is None:
                    bent_wire = _build_dense_closed_wire_from_bent_edges(
                        source_wire, bent_pairs, close_tol=close_tol)
                if bent_wire is None:
                    return None
                bent_wires.append(bent_wire)
            if not bent_wires:
                return None

            face = _make_filled_face_from_wires(
                bent_wires, area_tol=area_tol)
            if face is None and len(bent_wires) == 1:
                try:
                    face = Part.Face(bent_wires[0])
                except Exception:
                    face = None
            if face is None:
                return None
            try:
                if getattr(face, 'Area', 0.0) <= area_tol:
                    return None
            except Exception:
                pass
            if wedge_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   curved {label}"
                    f" surface=exact\n")
            return face

        def _make_line_edge(p0, p1):
            try:
                if p0.distanceToPoint(p1) <= 1e-9:
                    return None
            except Exception:
                return None
            try:
                return Part.LineSegment(p0, p1).toShape()
            except Exception:
                try:
                    poly = Part.makePolygon([p0, p1])
                    edges = list(getattr(poly, 'Edges', []))
                    return edges[0] if edges else None
                except Exception:
                    return None

        def _make_arc_edge(points):
            if len(points) < 3:
                return None
            p0 = FreeCAD.Vector(points[0])
            pm = FreeCAD.Vector(points[len(points) // 2])
            p1 = FreeCAD.Vector(points[-1])
            area = (pm - p0).cross(p1 - p0)
            if getattr(area, 'Length', 0.0) <= 1e-9:
                return _make_line_edge(p0, p1)
            try:
                return Part.Arc(p0, pm, p1).toShape()
            except Exception:
                return None

        def _make_bspline_edge(points):
            deduped = _dedupe_point_sequence(points)
            if len(deduped) < 2:
                return None
            if len(deduped) == 2:
                return _make_line_edge(deduped[0], deduped[1])
            try:
                curve = Part.BSplineCurve()
                try:
                    curve.interpolate(Points=deduped)
                except TypeError:
                    curve.interpolate(deduped)
                return curve.toShape()
            except Exception:
                return _make_line_edge(deduped[0], deduped[-1])

        def _discretize_wedge_edge(edge, sample_count):
            try:
                pts = edge.discretize(Number=sample_count)
            except Exception:
                pts = [v.Point for v in getattr(edge, 'Vertexes', [])]
            deduped = _dedupe_point_sequence(pts)
            if len(deduped) >= 2:
                return deduped
            verts = [FreeCAD.Vector(v.Point)
                     for v in getattr(edge, 'Vertexes', [])]
            return _dedupe_point_sequence(verts)

        def _bend_wedge_curve_from_samples(
                flat_pts, wedge_ctx, source_edge=None,
                snap_start=None, snap_end=None):
            flat_pts = [FreeCAD.Vector(pt) for pt in flat_pts]
            if len(flat_pts) < 2:
                return None
            cur_p0 = wedge_ctx['cur_p0']
            cur_normal = wedge_ctx['cur_normal']
            bend_axis = wedge_ctx['bend_axis']
            coc = wedge_ctx['coc']
            near_d = wedge_ctx['near_d']
            far_span = wedge_ctx['far_span']
            sweep_angle = wedge_ctx['sweep_angle']

            ds = [(pt - cur_p0).dot(cur_normal) for pt in flat_pts]
            d_min = min(ds)
            d_max = max(ds)
            d_tol = max(1e-6, far_span * 1e-5)

            if d_max - d_min <= d_tol:
                d = sum(ds) / float(len(ds))
                if source_edge is not None:
                    try:
                        exact_edge = _transform_shape_for_wedge_d(
                            source_edge, wedge_ctx, d)
                        exact_verts = list(
                            getattr(exact_edge, 'Vertexes', []))
                        start_ok = True
                        end_ok = True
                        if len(exact_verts) >= 2:
                            if snap_start is not None:
                                start_ok = (
                                    exact_verts[0].Point.distanceToPoint(
                                        snap_start)
                                    <= GEOMETRY_TOLERANCE)
                            if snap_end is not None:
                                end_ok = (
                                    exact_verts[-1].Point.distanceToPoint(
                                        snap_end)
                                    <= GEOMETRY_TOLERANCE)
                        if start_ok and end_ok:
                            return exact_edge
                    except Exception:
                        pass
                bent_pts = [
                    _bend_wedge_point(flat_pts[0], wedge_ctx),
                    _bend_wedge_point(flat_pts[-1], wedge_ctx),
                ]
                if snap_start is not None:
                    bent_pts[0] = FreeCAD.Vector(snap_start)
                if snap_end is not None:
                    bent_pts[-1] = FreeCAD.Vector(snap_end)
                bent_pts = _dedupe_point_sequence(bent_pts)
                if len(bent_pts) < 2:
                    return None
                return _make_line_edge(bent_pts[0], bent_pts[1])

            base_pts = []
            base_ref = None
            base_constant = True
            for pt, d in zip(flat_pts, ds):
                base_pt = pt + cur_normal * (-d)
                base_pts.append(base_pt)
                if base_ref is None:
                    base_ref = base_pt
                    continue
                if base_pt.distanceToPoint(base_ref) > d_tol:
                    base_constant = False
                    break

            bent_pts = _dedupe_point_sequence(
                _bend_wedge_point(pt, wedge_ctx) for pt in flat_pts)
            if bent_pts and snap_start is not None:
                bent_pts[0] = FreeCAD.Vector(snap_start)
            if bent_pts and snap_end is not None:
                bent_pts[-1] = FreeCAD.Vector(snap_end)
            bent_pts = _dedupe_point_sequence(bent_pts)
            if len(bent_pts) < 2:
                return None

            if base_constant:
                arc_edge = _make_arc_edge(bent_pts)
                if arc_edge is not None:
                    return arc_edge

            return _make_bspline_edge(bent_pts)

        def _sample_flat_segment_points(p0, p1, wedge_ctx):
            p0 = FreeCAD.Vector(p0)
            p1 = FreeCAD.Vector(p1)
            cur_p0 = wedge_ctx['cur_p0']
            cur_normal = wedge_ctx['cur_normal']
            sweep_span = abs(float(
                wedge_ctx.get('sweep_span', wedge_ctx.get('far_span', 0.0))
                or 0.0))
            slice_count = max(
                int(wedge_ctx.get('slice_count', 1) or 1), 1)
            d0 = (p0 - cur_p0).dot(cur_normal)
            d1 = (p1 - cur_p0).dot(cur_normal)
            frac_span = abs(d1 - d0) / sweep_span if sweep_span > 1e-9 \
                else 0.0
            sample_count = max(
                3,
                int(math.ceil(
                    max(frac_span, 0.25)
                    * max(wedge_ctx['edge_samples'],
                          slice_count + 1, 8))) + 1)
            sample_count = min(sample_count, 64)
            pts = []
            for idx in range(sample_count):
                frac = idx / float(sample_count - 1)
                pt = p0 + (p1 - p0) * frac
                if (not pts
                        or pt.distanceToPoint(pts[-1]) > 1e-7):
                    pts.append(pt)
            return pts

        def _build_wedge_wireframe_analytic(wedge_ctx):
            profile = wedge_ctx.get('profile') or {}
            source_edges = profile.get('wireframe_edges')
            if not source_edges:
                source_edges = list(wedge_ctx['positioned_flat'].Edges)
            curves = []
            for edge, bent_curve in _build_bent_wedge_edges(
                    source_edges, wedge_ctx):
                if bent_curve is None:
                    continue
                curves.append(bent_curve)
            if not curves:
                return None
            if wedge_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   analytic wireframe ok"
                    f" edges={len(curves)}\n")
            try:
                return Part.makeCompound(curves)
            except Exception:
                return None

        def _build_bent_wedge_edges(
                source_edges, wedge_ctx, vertex_cache=None,
                welded_points=None, weld_tol=None):
            if vertex_cache is None:
                vertex_cache = []
            if welded_points is None:
                welded_points = []
            if weld_tol is None:
                weld_tol = GEOMETRY_TOLERANCE
            bent_pairs = []
            for edge in source_edges:
                edge_len = 0.0
                try:
                    edge_len = float(edge.Length)
                except Exception:
                    pass
                sample_count = max(
                    wedge_ctx['edge_samples'],
                    int(math.ceil(edge_len / 2.0)) + 1)
                sample_count = min(max(sample_count, 2), 96)
                pts = _discretize_wedge_edge(edge, sample_count)
                edge_verts = list(getattr(edge, 'Vertexes', []))
                snap_start = None
                snap_end = None
                if edge_verts:
                    snap_start = _lookup_welded_source_vertex(
                        edge_verts[0], wedge_ctx, vertex_cache,
                        welded_points, weld_tol)
                if len(edge_verts) >= 2:
                    snap_end = _lookup_welded_source_vertex(
                        edge_verts[-1], wedge_ctx, vertex_cache,
                        welded_points, weld_tol)
                bent_curve = _bend_wedge_curve_from_samples(
                    pts, wedge_ctx, source_edge=edge,
                    snap_start=snap_start, snap_end=snap_end)
                bent_pairs.append((edge, bent_curve))
            return bent_pairs

        def _lookup_bent_wedge_edge(source_edge, bent_pairs):
            for src_edge, bent_edge in bent_pairs:
                if _shape_is_same(src_edge, source_edge):
                    return bent_edge
            return None

        def _edge_d_span(edge, wedge_ctx):
            pts = _discretize_wedge_edge(edge, 8)
            if not pts:
                pts = [v.Point for v in getattr(edge, 'Vertexes', [])]
            if not pts:
                return 0.0
            cur_p0 = wedge_ctx['cur_p0']
            cur_normal = wedge_ctx['cur_normal']
            ds = [
                (FreeCAD.Vector(pt) - cur_p0).dot(cur_normal)
                for pt in pts
            ]
            return max(ds) - min(ds)

        def _collect_shape_faces(shape):
            if shape is None:
                return []
            faces = list(getattr(shape, 'Faces', []))
            if faces:
                return faces
            try:
                if getattr(shape, 'ShapeType', None) == 'Face':
                    return [shape]
            except Exception:
                pass
            return []

        def _edge_endpoints(edge):
            verts = list(getattr(edge, 'Vertexes', []))
            if len(verts) < 2:
                return None, None
            return verts[0].Point, verts[-1].Point

        def _close_wire_if_near_closed(wire, close_tol=None):
            if wire is None:
                return None
            if close_tol is None:
                close_tol = GEOMETRY_TOLERANCE
            try:
                is_closed = wire.isClosed()
            except Exception:
                is_closed = False
            if not is_closed:
                try:
                    wire.fixWire(None, close_tol)
                except Exception:
                    pass
            try:
                is_closed = wire.isClosed()
            except Exception:
                is_closed = False
            if not is_closed:
                verts = list(getattr(wire, 'Vertexes', []))
                if len(verts) >= 2:
                    try:
                        gap = verts[-1].Point.distanceToPoint(
                            verts[0].Point)
                    except Exception:
                        gap = float('inf')
                    if gap < close_tol:
                        try:
                            closing = Part.makeLine(
                                verts[-1].Point, verts[0].Point)
                            wire = Part.Wire(
                                list(wire.Edges) + [closing])
                        except Exception:
                            pass
            try:
                return wire if wire.isClosed() else None
            except Exception:
                return None

        def _build_closed_wire_from_bent_edges(
                source_wire, bent_pairs, close_tol=None):
            source_edges = list(getattr(source_wire, 'Edges', []))
            bent_edges = []
            for edge in source_edges:
                bent_edge = _lookup_bent_wedge_edge(
                    edge, bent_pairs)
                if bent_edge is None:
                    return None
                try:
                    bent_edges.append(bent_edge.copy())
                except Exception:
                    bent_edges.append(bent_edge)
            if not bent_edges:
                return None

            if len(bent_edges) == 1:
                try:
                    return Part.Wire(bent_edges)
                except Exception:
                    return None

            ordered_edges = []
            try:
                first_edge = bent_edges[0]
                first_start, first_end = _edge_endpoints(first_edge)
                if len(bent_edges) > 1:
                    next_start, next_end = _edge_endpoints(
                        bent_edges[1])
                    if (first_start is not None and first_end is not None
                            and next_start is not None
                            and next_end is not None):
                        keep_dist = min(
                            first_end.distanceToPoint(next_start),
                            first_end.distanceToPoint(next_end))
                        rev_dist = min(
                            first_start.distanceToPoint(next_start),
                            first_start.distanceToPoint(next_end))
                        if rev_dist + 1e-9 < keep_dist:
                            first_edge = first_edge.reversed()
                            first_start, first_end = (
                                first_end, first_start)
                ordered_edges.append(first_edge)
                prev_end = first_end
                for edge in bent_edges[1:]:
                    cur_edge = edge
                    cur_start, cur_end = _edge_endpoints(cur_edge)
                    if (prev_end is not None
                            and cur_start is not None
                            and cur_end is not None):
                        keep_dist = prev_end.distanceToPoint(cur_start)
                        rev_dist = prev_end.distanceToPoint(cur_end)
                        if rev_dist + 1e-9 < keep_dist:
                            cur_edge = cur_edge.reversed()
                            cur_start, cur_end = (
                                cur_end, cur_start)
                    ordered_edges.append(cur_edge)
                    prev_end = cur_end
                wire = _close_wire_if_near_closed(
                    Part.Wire(ordered_edges), close_tol=close_tol)
                if wire is not None:
                    return wire
            except Exception:
                pass

            try:
                sorted_groups = Part.sortEdges(bent_edges)
            except Exception:
                sorted_groups = [bent_edges]
            if len(sorted_groups) != 1:
                return None
            try:
                wire = Part.Wire(sorted_groups[0])
            except Exception:
                return None
            return _close_wire_if_near_closed(
                wire, close_tol=close_tol)

        def _build_dense_closed_wire_from_bent_edges(
                source_wire, bent_pairs, close_tol=None):
            if close_tol is None:
                close_tol = GEOMETRY_TOLERANCE
            source_edges = list(getattr(source_wire, 'Edges', []))
            if not source_edges:
                return None

            pts = []
            point_tol = max(close_tol * 0.5, 1e-7)
            for source_edge in source_edges:
                bent_edge = _lookup_bent_wedge_edge(
                    source_edge, bent_pairs)
                if bent_edge is None:
                    return None
                edge_len = 0.0
                try:
                    edge_len = float(getattr(bent_edge, 'Length', 0.0))
                except Exception:
                    edge_len = 0.0
                sample_count = max(
                    8,
                    min(48, int(math.ceil(edge_len / 0.05)) + 1))
                edge_pts = _discretize_wedge_edge(
                    bent_edge, sample_count)
                if len(edge_pts) < 2:
                    continue
                edge_pts = [FreeCAD.Vector(pt) for pt in edge_pts]
                if pts:
                    keep_gap = pts[-1].distanceToPoint(edge_pts[0])
                    rev_gap = pts[-1].distanceToPoint(edge_pts[-1])
                    if rev_gap + point_tol < keep_gap:
                        edge_pts.reverse()
                for pt in edge_pts:
                    if (not pts
                            or pts[-1].distanceToPoint(pt) > point_tol):
                        pts.append(pt)
            pts = _dedupe_point_sequence(pts)
            if len(pts) < 3:
                return None

            gap_tol = max(close_tol * 20.0, 1e-4)
            if pts[-1].distanceToPoint(pts[0]) <= gap_tol:
                pts[-1] = FreeCAD.Vector(pts[0])

            poly_pts = list(pts)
            if poly_pts[-1].distanceToPoint(poly_pts[0]) > point_tol:
                poly_pts.append(FreeCAD.Vector(poly_pts[0]))
            if len(poly_pts) < 4:
                return None

            try:
                poly = Part.makePolygon(poly_pts)
                wire = Part.Wire(poly.Edges)
            except Exception:
                return None
            return _close_wire_if_near_closed(
                wire, close_tol=gap_tol)

        def _build_constant_axis_edge_from_source_edge(
                source_edge, bent_pairs, wedge_ctx,
                axis, target_value, close_tol=None):
            if close_tol is None:
                close_tol = GEOMETRY_TOLERANCE
            point_tol = max(close_tol * 0.5, 1e-7)

            bent_edge = _lookup_bent_wedge_edge(
                source_edge, bent_pairs)
            if bent_edge is not None:
                try:
                    return bent_edge.copy()
                except Exception:
                    return bent_edge

            edge_verts = list(getattr(source_edge, 'Vertexes', []))
            if len(edge_verts) < 2:
                return None
            p0 = _project_point_to_constant_axis_value(
                _bend_wedge_point(edge_verts[0].Point, wedge_ctx),
                wedge_ctx, axis, target_value)
            p1 = _project_point_to_constant_axis_value(
                _bend_wedge_point(edge_verts[-1].Point, wedge_ctx),
                wedge_ctx, axis, target_value)
            if p0.distanceToPoint(p1) <= point_tol:
                return None
            return _make_line_edge(p0, p1)

        def _build_constant_axis_closed_wire_from_source_edges(
                source_wire, bent_pairs, wedge_ctx,
                axis, target_value, close_tol=None):
            if close_tol is None:
                close_tol = GEOMETRY_TOLERANCE
            source_edges = list(getattr(source_wire, 'Edges', []))
            if not source_edges:
                return None

            bent_edges = []
            for source_edge in source_edges:
                bent_edge = _build_constant_axis_edge_from_source_edge(
                    source_edge,
                    bent_pairs,
                    wedge_ctx,
                    axis,
                    target_value,
                    close_tol=close_tol)
                if bent_edge is None:
                    continue
                bent_edges.append(bent_edge)
            if not bent_edges:
                return None
            if len(bent_edges) == 1:
                return None

            ordered_edges = []
            try:
                first_edge = bent_edges[0]
                first_start, first_end = _edge_endpoints(first_edge)
                if len(bent_edges) > 1:
                    next_start, next_end = _edge_endpoints(
                        bent_edges[1])
                    if (first_start is not None and first_end is not None
                            and next_start is not None
                            and next_end is not None):
                        keep_dist = min(
                            first_end.distanceToPoint(next_start),
                            first_end.distanceToPoint(next_end))
                        rev_dist = min(
                            first_start.distanceToPoint(next_start),
                            first_start.distanceToPoint(next_end))
                        if rev_dist + 1e-9 < keep_dist:
                            first_edge = first_edge.reversed()
                            first_start, first_end = (
                                first_end, first_start)
                ordered_edges.append(first_edge)
                prev_end = first_end
                for edge in bent_edges[1:]:
                    cur_edge = edge
                    cur_start, cur_end = _edge_endpoints(cur_edge)
                    if (prev_end is not None
                            and cur_start is not None
                            and cur_end is not None):
                        keep_dist = prev_end.distanceToPoint(cur_start)
                        rev_dist = prev_end.distanceToPoint(cur_end)
                        if rev_dist + 1e-9 < keep_dist:
                            cur_edge = cur_edge.reversed()
                            cur_start, cur_end = (
                                cur_end, cur_start)
                    ordered_edges.append(cur_edge)
                    prev_end = cur_end
                wire = _close_wire_if_near_closed(
                    Part.Wire(ordered_edges), close_tol=close_tol)
                if wire is not None:
                    return wire
            except Exception:
                pass

            try:
                sorted_groups = Part.sortEdges(bent_edges)
            except Exception:
                sorted_groups = [bent_edges]
            if len(sorted_groups) != 1:
                return None
            try:
                wire = Part.Wire(sorted_groups[0])
            except Exception:
                return None
            return _close_wire_if_near_closed(
                wire, close_tol=close_tol)

        def _build_projected_closed_wire_from_source_edges(
                source_wire, bent_pairs, wedge_ctx,
                axis, target_value, close_tol=None):
            if close_tol is None:
                close_tol = GEOMETRY_TOLERANCE
            source_edges = list(getattr(source_wire, 'Edges', []))
            if not source_edges:
                return None

            pts = []
            point_tol = max(close_tol * 0.5, 1e-7)
            gap_tol = max(close_tol * 20.0, 1e-4)
            for source_edge in source_edges:
                bent_edge = _lookup_bent_wedge_edge(
                    source_edge, bent_pairs)
                edge_len = 0.0
                edge_pts = []
                if bent_edge is not None:
                    try:
                        edge_len = float(getattr(bent_edge, 'Length', 0.0))
                    except Exception:
                        edge_len = 0.0
                    sample_count = max(
                        8,
                        min(48, int(math.ceil(edge_len / 0.05)) + 1))
                    edge_pts = _discretize_wedge_edge(
                        bent_edge, sample_count)
                if len(edge_pts) < 2:
                    try:
                        edge_len = float(getattr(source_edge, 'Length', 0.0))
                    except Exception:
                        edge_len = 0.0
                    sample_count = max(
                        8,
                        min(48, int(math.ceil(edge_len / 0.05)) + 1))
                    flat_edge_pts = _discretize_wedge_edge(
                        source_edge, sample_count)
                    edge_pts = [
                        _bend_wedge_point(pt, wedge_ctx)
                        for pt in flat_edge_pts
                    ]
                edge_pts = [
                    _project_point_to_constant_axis_value(
                        pt, wedge_ctx, axis, target_value)
                    for pt in edge_pts
                ]
                edge_pts = _dedupe_point_sequence(edge_pts, tol=point_tol)
                if not edge_pts:
                    continue
                if pts:
                    keep_gap = pts[-1].distanceToPoint(edge_pts[0])
                    rev_gap = pts[-1].distanceToPoint(edge_pts[-1])
                    if rev_gap + point_tol < keep_gap:
                        edge_pts.reverse()
                for pt in edge_pts:
                    if (not pts
                            or pts[-1].distanceToPoint(pt) > point_tol):
                        pts.append(pt)

            pts = _dedupe_point_sequence(pts, tol=point_tol)
            if len(pts) < 3:
                return None
            if pts[-1].distanceToPoint(pts[0]) <= gap_tol:
                pts[-1] = FreeCAD.Vector(pts[0])

            poly_pts = list(pts)
            if poly_pts[-1].distanceToPoint(poly_pts[0]) > point_tol:
                poly_pts.append(FreeCAD.Vector(poly_pts[0]))
            if len(poly_pts) < 4:
                return None

            try:
                poly = Part.makePolygon(poly_pts)
                wire = Part.Wire(poly.Edges)
            except Exception:
                return None
            return _close_wire_if_near_closed(
                wire, close_tol=gap_tol)

        def _points_collapse_to_line(points, point_tol, line_tol=None):
            pts = _dedupe_point_sequence(points, tol=point_tol)
            if len(pts) < 3:
                return True
            axis_p0 = None
            axis_p1 = None
            axis_len = 0.0
            for idx, p0 in enumerate(pts):
                for p1 in pts[idx + 1:]:
                    dist = p0.distanceToPoint(p1)
                    if dist > axis_len:
                        axis_len = dist
                        axis_p0 = p0
                        axis_p1 = p1
            if axis_p0 is None or axis_p1 is None or axis_len <= point_tol:
                return True
            if line_tol is None:
                line_tol = point_tol * 2.0
            axis = axis_p1 - axis_p0
            for pt in pts:
                perp = ((pt - axis_p0).cross(axis)).Length / axis_len
                if perp > line_tol:
                    return False
            return True

        def _wire_collapses_after_bending(
                source_wire, bent_pairs, wedge_ctx=None):
            close_tol = GEOMETRY_TOLERANCE
            if wedge_ctx is not None:
                tol_cfg = wedge_ctx.get('tolerances') or {}
                close_tol = float(
                    tol_cfg.get('close', GEOMETRY_TOLERANCE))
            point_tol = max(close_tol * 0.5, 1e-7)
            line_tol = max(close_tol * 2.0, point_tol * 2.0)
            bent_pts = []
            for source_edge in getattr(source_wire, 'Edges', []):
                bent_edge = _lookup_bent_wedge_edge(
                    source_edge, bent_pairs)
                if bent_edge is None:
                    continue
                edge_len = 0.0
                try:
                    edge_len = float(getattr(bent_edge, 'Length', 0.0))
                except Exception:
                    edge_len = 0.0
                sample_count = max(
                    8,
                    min(48, int(math.ceil(edge_len / 0.05)) + 1))
                edge_pts = _discretize_wedge_edge(
                    bent_edge, sample_count)
                if len(edge_pts) < 2:
                    edge_pts = [
                        FreeCAD.Vector(vertex.Point)
                        for vertex in getattr(bent_edge, 'Vertexes', [])
                    ]
                for pt in edge_pts:
                    pt = FreeCAD.Vector(pt)
                    if any(
                            pt.distanceToPoint(existing) <= point_tol
                            for existing in bent_pts):
                        continue
                    bent_pts.append(pt)
            return _points_collapse_to_line(
                bent_pts, point_tol, line_tol=line_tol)

        def _make_filled_face_from_wire(wire, area_tol=1e-9):
            if wire is None:
                return None
            make_filled_face = getattr(Part, 'makeFilledFace', None)
            if callable(make_filled_face):
                for edge_arg in (list(wire.Edges), wire.Edges):
                    try:
                        face = make_filled_face(edge_arg)
                    except Exception:
                        continue
                    if getattr(face, 'Area', 0.0) > area_tol:
                        return face
            try:
                face = Part.Face(wire)
                if getattr(face, 'Area', 0.0) > area_tol:
                    return face
            except Exception:
                pass
            return None

        def _build_surface_from_span_edges(
                source_wire, bent_pairs, wedge_ctx, label):
            source_edges = list(getattr(source_wire, 'Edges', []))
            if len(source_edges) < 2:
                return None
            far_span = max(float(wedge_ctx.get('far_span', 0.0)), 0.0)
            axis_tol = _constant_d_tolerance(wedge_ctx)
            sweep_boundary_candidates = []
            span_entries = []
            for edge in source_edges:
                d_span = _edge_d_span(edge, wedge_ctx)
                edge_len = float(getattr(edge, 'Length', 0.0))
                span_entries.append((d_span, edge_len, edge))
                is_constant_s, s_mid, s_span = (
                    _shape_constant_projection_info(
                        edge, wedge_ctx, wedge_ctx['bend_axis'],
                        tol=axis_tol))
                if is_constant_s:
                    sweep_boundary_candidates.append({
                        'edge': edge,
                        's_mid': s_mid,
                        's_span': s_span,
                        'd_span': d_span,
                        'length': edge_len,
                    })
            span_entries.sort(key=lambda item: (item[0], item[1]),
                              reverse=True)
            span_tol = max(far_span * 0.5, GEOMETRY_TOLERANCE * 10)
            sweep_boundary_candidates = [
                entry for entry in sweep_boundary_candidates
                if entry['d_span'] >= span_tol]
            span_candidates = []
            if len(sweep_boundary_candidates) >= 2:
                sweep_boundary_candidates.sort(
                    key=lambda entry: (
                        entry['s_mid'],
                        -entry['d_span'],
                        -entry['length']))
                first_entry = sweep_boundary_candidates[0]
                last_entry = sweep_boundary_candidates[-1]
                if not _shape_is_same(
                        first_entry['edge'], last_entry['edge']):
                    span_candidates = [
                        first_entry['edge'],
                        last_entry['edge']]
                    if wedge_diag:
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD:   curved {label}"
                            f" sweep-boundaries="
                            f"[{first_entry['s_mid']:.6f},"
                            f"{last_entry['s_mid']:.6f}]"
                            f" d=[{first_entry['d_span']:.6f},"
                            f"{last_entry['d_span']:.6f}]\n")
            if not span_candidates:
                if wedge_diag:
                    labels = []
                    for d_span, edge_len, edge in span_entries[:4]:
                        is_constant_s, s_mid, s_span = (
                            _shape_constant_projection_info(
                                edge, wedge_ctx,
                                wedge_ctx['bend_axis'],
                                tol=axis_tol))
                        labels.append(
                            f"d={d_span:.6f}"
                            f"/len={edge_len:.6f}"
                            f"/s={'const' if is_constant_s else 'var'}"
                            f"{'' if not is_constant_s else f'@{s_mid:.6f}'}")
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   curved {label}"
                        f" sweep-boundaries=fallback"
                        f" candidates=[{'; '.join(labels)}]\n")
                return None
            if len(span_candidates) < 2:
                return None

            edge_a = _lookup_bent_wedge_edge(
                span_candidates[0], bent_pairs)
            edge_b = _lookup_bent_wedge_edge(
                span_candidates[1], bent_pairs)
            if edge_a is None or edge_b is None:
                return None

            if wedge_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   curved {label} spans="
                    f"{_edge_d_span(span_candidates[0], wedge_ctx):.6f},"
                    f"{_edge_d_span(span_candidates[1], wedge_ctx):.6f}\n")

            loft_variants = [
                (edge_a, edge_b),
                (edge_a, edge_b.reversed()),
            ]
            for loft_a, loft_b in loft_variants:
                try:
                    patch = Part.makeLoft(
                        [loft_a, loft_b], False, False)
                except Exception:
                    patch = None
                faces = _collect_shape_faces(patch)
                if faces:
                    if wedge_diag:
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD:   curved {label}"
                            f" surface=span-loft"
                            f" faces={len(faces)}\n")
                    return faces
                make_ruled = getattr(
                    Part, 'makeRuledSurface', None)
                if callable(make_ruled):
                    try:
                        patch = make_ruled(loft_a, loft_b)
                    except Exception:
                        patch = None
                    faces = _collect_shape_faces(patch)
                    if faces:
                        if wedge_diag:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD:   curved {label}"
                                f" surface=span-ruled"
                                f" faces={len(faces)}\n")
                        return faces
            return None

        def _build_cap_surface_faces(
                source_wire, bent_pairs, wedge_ctx, label,
                prefer_span=None):
            edge_count = len(getattr(source_wire, 'Edges', []))
            if _wire_collapses_after_bending(
                    source_wire, bent_pairs, wedge_ctx):
                if wedge_diag:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   curved {label}"
                        f" collapsed-to-line"
                        f" for piece {wedge_ctx['pi']}\n")
                return []
            if prefer_span is None:
                prefer_span = (edge_count <= 4)
            if wedge_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   curved {label}"
                    f" strategy={'span-first' if prefer_span else 'fill-first'}"
                    f" edges={edge_count}\n")

            strategies = (
                ('span', 'fill') if prefer_span else ('fill', 'span'))
            for strategy in strategies:
                if strategy == 'span':
                    faces = _build_surface_from_span_edges(
                        source_wire, bent_pairs, wedge_ctx, label)
                    if faces:
                        return faces
                    continue

                wire = _build_closed_wire_from_bent_edges(
                    source_wire, bent_pairs)
                face = _make_filled_face_from_wire(wire)
                if face is not None:
                    if wedge_diag:
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD:   curved {label}"
                            f" surface=filled-face"
                            f" faces=1\n")
                    return [face]
                dense_wire = _build_dense_closed_wire_from_bent_edges(
                    source_wire, bent_pairs)
                dense_face = _make_filled_face_from_wire(dense_wire)
                if dense_face is not None:
                    if wedge_diag:
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD:   curved {label}"
                            f" surface=dense-filled-face"
                            f" faces=1\n")
                    return [dense_face]
            return None

        def _build_side_surface_faces(
                source_face, bent_pairs, wedge_ctx, label,
                pair_entries=None, close_tol=None, area_tol=1e-9):
            anchored_face = _build_anchored_sweep_face(
                source_face, wedge_ctx, label, area_tol=area_tol)
            if anchored_face is not None:
                return [anchored_face], "anchored"

            boundary_face = _build_constant_sweep_boundary_face(
                source_face, bent_pairs, wedge_ctx, label,
                close_tol=close_tol, area_tol=area_tol)
            if boundary_face is not None:
                return [boundary_face], "boundary-plane"

            sweep_deg = abs(math.degrees(float(
                wedge_ctx.get('sweep_angle', 0.0) or 0.0)))
            source_edge_count = len(getattr(source_face, 'Edges', []))
            if sweep_deg >= 179.0 and source_edge_count <= 4:
                exact_side_face = _build_exact_side_face(
                    source_face,
                    bent_pairs,
                    wedge_ctx,
                    label,
                    close_tol=close_tol,
                    area_tol=area_tol)
                if exact_side_face is not None:
                    return [exact_side_face], "exact"

            if not pair_entries:
                if wedge_diag:
                    FreeCAD.Console.PrintWarning(
                        f"FreekiCAD:   curved {label}"
                        f" surface=side-pairs-missing\n")
                return None, None

            rebuilt_faces = []
            make_ruled = getattr(Part, 'makeRuledSurface', None)
            for pair_idx, pair in enumerate(pair_entries):
                top_edge = _lookup_bent_wedge_edge(
                    pair.get('top_edge'), bent_pairs)
                bottom_edge = _lookup_bent_wedge_edge(
                    pair.get('bottom_edge'), bent_pairs)
                if top_edge is None or bottom_edge is None:
                    continue

                pair_label = (
                    label if len(pair_entries) == 1
                    else f"{label}:{pair_idx}")
                variants = [
                    (top_edge, bottom_edge),
                    (top_edge, bottom_edge.reversed()),
                ]
                faces = None
                for top_variant, bottom_variant in variants:
                    if callable(make_ruled):
                        try:
                            patch = make_ruled(
                                top_variant, bottom_variant)
                        except Exception:
                            patch = None
                        faces = _collect_shape_faces(patch)
                        if faces:
                            if wedge_diag:
                                FreeCAD.Console.PrintMessage(
                                    f"FreekiCAD:   curved {pair_label}"
                                    f" surface=side-ruled"
                                    f" faces={len(faces)}\n")
                            break

                    try:
                        patch = Part.makeLoft(
                            [top_variant, bottom_variant],
                            False, False)
                    except Exception:
                        patch = None
                    faces = _collect_shape_faces(patch)
                    if faces:
                        if wedge_diag:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD:   curved {pair_label}"
                                f" surface=side-loft"
                                f" faces={len(faces)}\n")
                        break

                if not faces:
                    continue
                for face in faces:
                    _append_unique_shape(rebuilt_faces, face)

            if not rebuilt_faces and wedge_diag:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD:   curved {label}"
                    f" surface=side-pairs-failed"
                    f" pairs={len(pair_entries)}\n")
            if not rebuilt_faces:
                return None, None
            return rebuilt_faces, "pairs"

        def _make_filled_face_from_wires(wires, area_tol=1e-9):
            if not wires:
                return None
            if len(wires) == 1:
                return _make_filled_face_from_wire(
                    wires[0], area_tol=area_tol)

            make_filled_face = getattr(Part, 'makeFilledFace', None)
            if callable(make_filled_face):
                edge_list = []
                for wire in wires:
                    edge_list.extend(list(getattr(wire, 'Edges', [])))
                if edge_list:
                    for edge_arg in (edge_list, tuple(edge_list)):
                        try:
                            face = make_filled_face(edge_arg)
                        except Exception:
                            continue
                        if getattr(face, 'Area', 0.0) > area_tol:
                            return face
            return None

        def _build_face_from_source_face(
                source_face, bent_pairs, wedge_ctx,
                close_tol=None, area_tol=1e-9,
                face_label=None, allow_filled=True):
            anchored_face = _build_anchored_sweep_face(
                source_face, wedge_ctx,
                face_label if face_label is not None else "face",
                area_tol=area_tol)
            if anchored_face is not None:
                return anchored_face
            if not allow_filled:
                return None
            bent_wires = []
            for source_wire in getattr(source_face, 'Wires', []):
                bent_wire = _build_closed_wire_from_bent_edges(
                    source_wire, bent_pairs, close_tol=close_tol)
                if bent_wire is None:
                    return None
                bent_wires.append(bent_wire)
            if not bent_wires:
                return None

            face = _make_filled_face_from_wires(
                bent_wires, area_tol=area_tol)
            if face is None:
                try:
                    face = Part.Face(bent_wires[0])
                except Exception:
                    face = None
            if face is None:
                return None
            try:
                if getattr(face, 'Area', 0.0) <= area_tol:
                    return None
            except Exception:
                pass
            return face

        def _collect_shape_shells(shape):
            if shape is None:
                return []
            shells = list(getattr(shape, 'Shells', []))
            if shells:
                return shells
            try:
                if getattr(shape, 'ShapeType', None) == 'Shell':
                    return [shape]
            except Exception:
                pass
            return []

        def _repair_valid_solid_candidate(
                shape, fix_tol=None, fix_max_tol=None):
            if shape is None:
                return None, None, 0.0

            attempts = [("raw", shape)]

            def _add_attempt(label, candidate):
                if candidate is not None:
                    attempts.append((label, candidate))

            if fix_tol is None:
                fix_tol = GEOMETRY_TOLERANCE
            if fix_max_tol is None:
                fix_max_tol = max(
                    GEOMETRY_TOLERANCE, GEOMETRY_TOLERANCE * 10.0)

            try:
                fixed = shape.copy()
                fixed.fix(fix_tol, fix_tol, fix_max_tol)
                _add_attempt("fix", fixed)
            except Exception:
                pass

            try:
                split = shape.copy().removeSplitter()
                _add_attempt("removeSplitter", split)
            except Exception:
                pass

            try:
                fixed_split = shape.copy()
                fixed_split.fix(fix_tol, fix_tol, fix_max_tol)
                fixed_split = fixed_split.removeSplitter()
                fixed_split.fix(fix_tol, fix_tol, fix_max_tol)
                _add_attempt("fix+removeSplitter+fix", fixed_split)
            except Exception:
                pass

            for label, candidate in attempts:
                try:
                    vol = float(candidate.Volume)
                except Exception:
                    vol = 0.0
                if abs(vol) <= 1e-9:
                    continue
                if vol < 0:
                    try:
                        candidate = candidate.reversed()
                        vol = abs(float(candidate.Volume))
                    except Exception:
                        continue
                try:
                    valid = candidate.isValid()
                except Exception:
                    valid = True
                if not valid:
                    continue
                return candidate, label, vol
            return None, None, 0.0

        def _orient_face_outward(face, solid_center):
            if face is None:
                return None, False
            try:
                face_center = FreeCAD.Vector(face.CenterOfMass)
            except Exception:
                face_center = None
            face_normal = _face_normal(face)
            if face_center is None or face_normal is None:
                return face, False
            outward = face_center - FreeCAD.Vector(solid_center)
            if getattr(outward, 'Length', 0.0) <= 1e-9:
                return face, False
            if outward.dot(face_normal) >= 0.0:
                return face, False
            try:
                return face.reversed(), True
            except Exception:
                return face, False

        def _measure_wedge_anchor_alignment(shape, wedge_ctx):
            if shape is None:
                return None
            anchor_ref_near = wedge_ctx.get('anchor_ref_near')
            anchor_ref_far = wedge_ctx.get('anchor_ref_far')
            anchor_target_near = wedge_ctx.get('anchor_target_near')
            anchor_target_far = wedge_ctx.get('anchor_target_far')
            if (anchor_ref_near is None or anchor_ref_far is None
                    or anchor_target_near is None
                    or anchor_target_far is None):
                return None

            score_shape = shape
            anchor_post_plc = wedge_ctx.get('anchor_post_plc')
            if anchor_post_plc is not None:
                try:
                    rot_angle = anchor_post_plc.Rotation.Angle
                    base_len = anchor_post_plc.Base.Length
                    needs_post = (
                        base_len > 1e-9
                        or rot_angle > 1e-9)
                except Exception:
                    rot_angle = None
                    base_len = None
                    needs_post = True
                if (rot_angle is not None and base_len is not None
                        and rot_angle <= 1e-6
                        and base_len > 1e-9):
                    # Translation-only post placement is refined later by
                    # adaptive translation scoring; don't reject a solid here
                    # against a full-translation anchor that may not be used.
                    return None
                if needs_post:
                    try:
                        score_shape = shape.copy()
                        score_shape.transformShape(
                            anchor_post_plc.toMatrix())
                    except Exception:
                        return None

            cand_anchor_near = _closest_point_on_shape(
                score_shape, anchor_ref_near)
            cand_anchor_far = _closest_point_on_shape(
                score_shape, anchor_ref_far)
            near_err = cand_anchor_near.distanceToPoint(
                anchor_target_near)
            far_err = cand_anchor_far.distanceToPoint(
                anchor_target_far)
            center_err = (
                _shape_center(score_shape)
                - wedge_ctx.get('target_cm', FreeCAD.Vector())
            ).Length
            return {
                'near': cand_anchor_near,
                'far': cand_anchor_far,
                'near_err': near_err,
                'far_err': far_err,
                'score': (
                    float(max(near_err, far_err)),
                    float(near_err + far_err),
                    float(center_err)),
            }

        def _solidify_surface_faces(
                surface_faces, wedge_ctx, strategy_name,
                side_face_count=None):
            unique_faces = []
            for face in surface_faces:
                _append_unique_shape(unique_faces, face)
            if not unique_faces:
                return None
            target_center = FreeCAD.Vector(
                wedge_ctx.get('target_cm', FreeCAD.Vector()))
            oriented_faces = []
            flipped_faces = 0
            for face in unique_faces:
                oriented_face, flipped = _orient_face_outward(
                    face, target_center)
                if oriented_face is None:
                    continue
                if flipped:
                    flipped_faces += 1
                oriented_faces.append(oriented_face)
            unique_faces = oriented_faces
            if not unique_faces:
                return None

            if wedge_diag:
                msg = (
                    f"FreekiCAD:   curved solid"
                    f" strategy={strategy_name}")
                if side_face_count is not None:
                    msg += f" side_faces={side_face_count}"
                msg += f" total_faces={len(unique_faces)}\n"
                FreeCAD.Console.PrintMessage(msg)
                if flipped_faces:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   curved face orientation"
                        f" flipped={flipped_faces}"
                        f" strategy={strategy_name}\n")

            target_vol = abs(float(
                wedge_ctx.get('target_vol', 0.0) or 0.0))
            tol_cfg = wedge_ctx.get('tolerances') or {}
            fix_tol = float(tol_cfg.get('fix', GEOMETRY_TOLERANCE))
            fix_max_tol = float(tol_cfg.get(
                'fix_max',
                max(GEOMETRY_TOLERANCE, GEOMETRY_TOLERANCE * 10.0)))
            anchor_tol = float(
                wedge_ctx.get('anchor_tolerance', 0.0) or 0.0)

            def _build_shell_candidates(
                    candidate_faces,
                    candidate_fix_tol,
                    candidate_fix_max_tol):
                shell_candidates = []

                def _add_shell_candidate(label, shape):
                    for shell in _collect_shape_shells(shape):
                        shell_candidates.append((label, shell))

                try:
                    _add_shell_candidate(
                        "makeShell", Part.makeShell(candidate_faces))
                except Exception:
                    pass
                try:
                    _add_shell_candidate(
                        "Shell", Part.Shell(candidate_faces))
                except Exception:
                    pass

                compound = None
                try:
                    compound = Part.makeCompound(
                        [face.copy() for face in candidate_faces])
                except Exception:
                    compound = None
                if compound is not None:
                    _add_shell_candidate("compound", compound)
                    try:
                        sewn = compound.copy()
                    except Exception:
                        sewn = compound
                    try:
                        sew_result = sewn.sewShape()
                    except Exception:
                        sew_result = None
                    _add_shell_candidate("sewShape", sew_result)
                    _add_shell_candidate("sewShape", sewn)
                    try:
                        sewn_fixed = sewn.copy()
                        sewn_fixed.fix(
                            candidate_fix_tol,
                            candidate_fix_tol,
                            candidate_fix_max_tol)
                        _add_shell_candidate("sewShape+fix", sewn_fixed)
                    except Exception:
                        pass
                return shell_candidates

            def _try_shell_candidates(
                    shell_candidates,
                    candidate_target_vol,
                    candidate_fix_tol,
                    candidate_fix_max_tol):
                best = None
                best_score = None
                for shell_label, shell in shell_candidates:
                    display_label = shell_label
                    shell_to_use = shell
                    try:
                        if not shell_to_use.isValid():
                            shell_fixed = shell_to_use.copy()
                            shell_fixed.fix(
                                candidate_fix_tol,
                                candidate_fix_tol,
                                candidate_fix_max_tol)
                            shell_to_use = shell_fixed
                    except Exception:
                        pass

                    solid = None
                    try:
                        solid = Part.makeSolid(shell_to_use)
                    except Exception:
                        try:
                            solid = Part.Solid(shell_to_use)
                        except Exception:
                            solid = None
                    if solid is None:
                        continue

                    solid, repair_label, vol = _repair_valid_solid_candidate(
                        solid,
                        fix_tol=candidate_fix_tol,
                        fix_max_tol=candidate_fix_max_tol)
                    if solid is None:
                        if wedge_diag:
                            FreeCAD.Console.PrintWarning(
                                f"FreekiCAD:   curved solid invalid"
                                f" for piece {wedge_ctx['pi']}"
                                f" strategy={strategy_name}"
                                f" shell={display_label}\n")
                        continue

                    if (wedge_diag
                            and repair_label not in (None, "raw")):
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD:   curved solid repaired"
                            f" strategy={strategy_name}"
                            f" shell={display_label}"
                            f" via={repair_label}\n")

                    anchor_metrics = _measure_wedge_anchor_alignment(
                        solid, wedge_ctx)
                    if anchor_metrics is not None:
                        if wedge_diag:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD:   curved solid anchor"
                                f" strategy={strategy_name}"
                                f" shell={display_label}"
                                f" near={anchor_metrics['near_err']:.6f}"
                                f" far={anchor_metrics['far_err']:.6f}\n")
                        if (anchor_tol > 1e-9
                                and anchor_metrics['score'][0]
                                > anchor_tol):
                            if wedge_diag:
                                FreeCAD.Console.PrintWarning(
                                    f"FreekiCAD:   curved solid off-anchor"
                                    f" for piece {wedge_ctx['pi']}"
                                    f" strategy={strategy_name}"
                                    f" shell={display_label}"
                                    f" max_err={anchor_metrics['score'][0]:.6f}"
                                    f" tol={anchor_tol:.6f}\n")
                            continue

                    vol_rel = 0.0
                    if candidate_target_vol > 1e-9:
                        vol_rel = abs(vol - candidate_target_vol) / (
                            candidate_target_vol)
                    if vol_rel > 0.20 and wedge_diag:
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD:   curved solid suspicious"
                            f" for piece {wedge_ctx['pi']}"
                            f" strategy={strategy_name}"
                            f" vol_rel={vol_rel:.3f}"
                            f" vol={vol:.4f}"
                            f" target={candidate_target_vol:.4f}\n")
                    if anchor_metrics is not None:
                        cand_score = (
                            float(vol_rel),
                            float(anchor_metrics['score'][0]),
                            float(anchor_metrics['score'][1]),
                            float(anchor_metrics['score'][2]),
                        )
                    else:
                        cand_score = (
                            float(vol_rel),
                            float('inf'),
                            float('inf'),
                            float('inf'),
                        )
                    better = best_score is None
                    if not better:
                        for axis, (cand_v, best_v) in enumerate(
                                zip(cand_score, best_score)):
                            tol = 1e-6 if axis < 3 else 1e-9
                            if cand_v < best_v - tol:
                                better = True
                                break
                            if cand_v > best_v + tol:
                                break
                    if better:
                        best = solid
                        best_score = cand_score

                return best

            shell_candidates = _build_shell_candidates(
                unique_faces, fix_tol, fix_max_tol)
            solid = _try_shell_candidates(
                shell_candidates, target_vol, fix_tol, fix_max_tol)
            if solid is not None:
                return solid

            if wedge_diag:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD:   curved solidify failed"
                    f" for piece {wedge_ctx['pi']}"
                    f" strategy={strategy_name}\n")
            return None

        def _build_solid_from_source_faces(
                source_face_entries, bent_pairs, wedge_ctx):
            if not source_face_entries:
                return None

            tol_cfg = wedge_ctx.get('tolerances') or {}
            close_tol = float(tol_cfg.get('close', GEOMETRY_TOLERANCE))
            area_tol = float(tol_cfg.get('face_area', 1e-9))
            cap_face_cache = {}
            side_surface_cache = {}
            exact_face_cache = {}
            tri_face_cache = {}

            def _cached_cap_surface_faces(
                    source_wire, face_label, prefer_span):
                key = (face_label, bool(prefer_span))
                if key not in cap_face_cache:
                    cap_face_cache[key] = _build_cap_surface_faces(
                        source_wire, bent_pairs, wedge_ctx,
                        face_label, prefer_span=prefer_span)
                return cap_face_cache[key]

            def _cached_side_surface_faces(
                    source_face, face_label, pair_entries):
                key = face_label
                if key not in side_surface_cache:
                    side_surface_cache[key] = _build_side_surface_faces(
                        source_face,
                        bent_pairs,
                        wedge_ctx,
                        face_label,
                        pair_entries,
                        close_tol=close_tol,
                        area_tol=area_tol)
                return side_surface_cache[key]

            def _cached_exact_face(
                    source_face, face_label, allow_filled):
                key = (face_label, bool(allow_filled))
                if key not in exact_face_cache:
                    exact_face_cache[key] = _build_face_from_source_face(
                        source_face,
                        bent_pairs,
                        wedge_ctx,
                        close_tol=close_tol,
                        area_tol=area_tol,
                        face_label=face_label,
                        allow_filled=allow_filled)
                return exact_face_cache[key]

            def _cached_tri_faces(source_face, face_label):
                key = face_label
                if key not in tri_face_cache:
                    tri_face_cache[key] = _build_bent_source_face_triangle_patches(
                        source_face, wedge_ctx)
                return tri_face_cache[key]

            def _build_rebuilt_faces():
                rebuilt_faces = []
                tri_fallback_faces = 0
                dropped_faces = 0
                collapsed_faces = 0
                for face_idx, source_face_entry in enumerate(
                        source_face_entries):
                    if isinstance(source_face_entry, dict):
                        source_face = source_face_entry.get('face')
                        face_role = source_face_entry.get('role') or "face"
                        face_label = (
                            source_face_entry.get('label')
                            or f"{face_role}{face_idx}")
                    else:
                        source_face = source_face_entry
                        face_role = "face"
                        face_label = f"face{face_idx}"
                    if source_face is None:
                        continue

                    source_wires = list(getattr(source_face, 'Wires', []))
                    if (face_role in ("top", "bottom")
                            and len(source_wires) == 1):
                        cap_prefer_span = (
                            len(getattr(source_wires[0], 'Edges', [])) <= 4)
                        cap_faces = _cached_cap_surface_faces(
                            source_wires[0], face_label, cap_prefer_span)
                        if cap_faces is not None:
                            if cap_faces:
                                rebuilt_faces.extend(cap_faces)
                            else:
                                collapsed_faces += 1
                                if wedge_diag:
                                    FreeCAD.Console.PrintMessage(
                                        f"FreekiCAD:   curved source-face"
                                        f" {face_label}"
                                        f" collapsed-to-line\n")
                            continue

                    if face_role == "side":
                        side_faces, _side_mode = _cached_side_surface_faces(
                            source_face, face_label,
                            source_face_entry.get('pairs'))
                        if side_faces:
                            rebuilt_faces.extend(side_faces)
                            continue

                    rebuilt_face = _cached_exact_face(
                        source_face, face_label,
                        allow_filled=(face_role != "side"))
                    if rebuilt_face is not None:
                        if wedge_diag:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD:   curved source-face"
                                f" {face_label}"
                                f" surface=exact\n")
                        rebuilt_faces.append(rebuilt_face)
                        continue

                    tri_faces = _cached_tri_faces(source_face, face_label)
                    if not tri_faces:
                        dropped_faces += 1
                        if wedge_diag:
                            FreeCAD.Console.PrintWarning(
                                f"FreekiCAD:   curved source-face"
                                f" {face_label}"
                                f" dropped\n")
                        continue
                    if wedge_diag:
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD:   curved source-face"
                            f" {face_label}"
                            f" tri-fallback"
                            f" faces={len(tri_faces)}\n")
                    tri_fallback_faces += len(tri_faces)
                    rebuilt_faces.extend(tri_faces)

                return (
                    rebuilt_faces,
                    tri_fallback_faces,
                    collapsed_faces,
                    dropped_faces,
                )

            def _try_source_topology():
                (rebuilt_faces,
                 tri_fallback_faces,
                 collapsed_faces,
                 dropped_faces) = _build_rebuilt_faces()
                if not rebuilt_faces:
                    return None
                if wedge_diag:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   curved source-topology"
                        f" faces={len(rebuilt_faces)}"
                        f" tri_fallback={tri_fallback_faces}"
                        f" collapsed={collapsed_faces}"
                        f" dropped={dropped_faces}\n")
                solid = _solidify_surface_faces(
                    rebuilt_faces, wedge_ctx, "source-topology")
                if solid is None:
                    return None
                target_vol = abs(float(
                    wedge_ctx.get('target_vol', 0.0) or 0.0))
                vol_rel = 0.0
                if target_vol > 1e-9:
                    try:
                        vol_rel = abs(
                            float(getattr(solid, 'Volume', 0.0)) - target_vol
                        ) / target_vol
                    except Exception:
                        vol_rel = float('inf')
                if ((dropped_faces > 0
                        or collapsed_faces > 0
                        or tri_fallback_faces > 0)
                        and vol_rel > 0.35):
                    if wedge_diag:
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD:   curved source-topology"
                            f" rejected after fallback faces"
                            f" vol_rel={vol_rel:.3f}"
                            f" tri_fallback={tri_fallback_faces}"
                            f" collapsed={collapsed_faces}"
                            f" dropped={dropped_faces}\n")
                    return None
                return solid

            source_topology_solid = _try_source_topology()
            if source_topology_solid is not None:
                return source_topology_solid

            return None

        def _build_wedge_solid_hybrid(wedge_ctx):
            profile = wedge_ctx.get('profile') or {}
            side_pairs = profile.get('side_pairs') or []
            source_face_entries = []
            seen_source_faces = []
            for role, face_group in (
                    ('top', profile.get('top_faces') or []),
                    ('bottom', profile.get('bottom_faces') or []),
                    ('side', profile.get('side_faces') or [])):
                for face_group_idx, face in enumerate(face_group):
                    if _shape_in_list(face, seen_source_faces):
                        continue
                    seen_source_faces.append(face)
                    source_face_entry = {
                        'face': face,
                        'role': role,
                        'label': f"{role}{len(source_face_entries)}",
                    }
                    if role == 'side':
                        face_pairs = []
                        for pair in side_pairs:
                            if pair.get('face_index') == face_group_idx:
                                face_pairs.append(pair)
                                continue
                            if _shape_is_same(pair.get('face'), face):
                                face_pairs.append(pair)
                        if face_pairs:
                            source_face_entry['pairs'] = face_pairs
                    source_face_entries.append(source_face_entry)
            top_wires = profile.get('top_wires') or []
            bottom_wires = profile.get('bottom_wires') or []
            source_edges = profile.get('wireframe_edges') or []

            welded_vertex_cache = []
            welded_points = []
            tol_cfg = wedge_ctx.get('tolerances') or {}
            weld_tol = float(
                tol_cfg.get('weld', max(1e-6, GEOMETRY_TOLERANCE * 0.1)))
            bent_pairs = _build_bent_wedge_edges(
                source_edges, wedge_ctx,
                vertex_cache=welded_vertex_cache,
                welded_points=welded_points,
                weld_tol=weld_tol)
            if not bent_pairs:
                return None

            if wedge_diag:
                src_vertex_count = 0
                for edge in source_edges:
                    src_vertex_count += len(
                        getattr(edge, 'Vertexes', []))
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   curved vertex-weld"
                    f" source={src_vertex_count}"
                    f" unique={len(welded_points)}"
                    f" tol={weld_tol:.6f}"
                    f" close={float(tol_cfg.get('close', GEOMETRY_TOLERANCE)):.6f}"
                    f" fix={float(tol_cfg.get('fix', GEOMETRY_TOLERANCE)):.6f}"
                    f" fix_max={float(tol_cfg.get('fix_max', GEOMETRY_TOLERANCE * 10.0)):.6f}\n")

            source_topology_solid = _build_solid_from_source_faces(
                source_face_entries, bent_pairs, wedge_ctx)
            if source_topology_solid is not None:
                return source_topology_solid
            if wedge_diag:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD:   surface-based solid failed"
                    f" for piece {wedge_ctx['pi']}\n")
            return None

        def _make_triangle_face(p0, p1, p2):
            try:
                area_vec = (p1 - p0).cross(p2 - p0)
                if getattr(area_vec, 'Length', 0.0) <= 1e-9:
                    return None
                return Part.Face(Part.makePolygon([p0, p1, p2, p0]))
            except Exception:
                return None

        def _shape_feature_size(shape, fallback=None):
            candidates = []
            try:
                area = float(getattr(shape, 'Area', 0.0))
            except Exception:
                area = 0.0
            if area > 1e-12:
                try:
                    candidates.append(math.sqrt(area))
                except Exception:
                    pass
            try:
                bbox = getattr(shape, 'BoundBox', None)
                if bbox is not None:
                    for length in (
                            float(getattr(bbox, 'XLength', 0.0)),
                            float(getattr(bbox, 'YLength', 0.0)),
                            float(getattr(bbox, 'ZLength', 0.0))):
                        if length > 1e-6:
                            candidates.append(length)
            except Exception:
                pass
            for edge in getattr(shape, 'Edges', []):
                try:
                    edge_len = float(getattr(edge, 'Length', 0.0))
                except Exception:
                    edge_len = 0.0
                if edge_len > 1e-6:
                    candidates.append(edge_len)
            if candidates:
                return min(candidates)
            if fallback is not None:
                return float(fallback)
            return GEOMETRY_TOLERANCE * 50.0

        def _tessellate_face_triangles(face, deflection):
            try:
                verts, tris = face.tessellate(deflection)
            except Exception:
                return []
            tri_pts = []
            for tri in tris:
                if len(tri) < 3:
                    continue
                try:
                    pts = [FreeCAD.Vector(verts[idx]) for idx in tri[:3]]
                except Exception:
                    continue
                tri_pts.append(pts)
            return tri_pts

        def _source_face_tessellation_deflections(source_face, wedge_ctx):
            tol_cfg = wedge_ctx.get('tolerances') or {}
            close_tol = float(tol_cfg.get('close', GEOMETRY_TOLERANCE))
            feature_size = float(
                tol_cfg.get(
                    'feature_size',
                    max(
                        GEOMETRY_TOLERANCE * 50.0,
                        abs(float(wedge_ctx.get('ins', 0.0) or 0.0)) * 2.0)))
            local_size = _shape_feature_size(
                source_face, fallback=feature_size)
            global_deflection = max(
                0.05,
                min(0.25, max(wedge_ctx['ins'], 0.05) / 8.0))
            base_deflection = max(
                close_tol * 2.0,
                min(global_deflection, local_size / 8.0))
            min_deflection = max(
                close_tol * 0.5,
                min(base_deflection, local_size / 64.0))
            deflections = []
            cur_deflection = base_deflection
            while cur_deflection >= min_deflection - 1e-12:
                if not any(
                        abs(cur_deflection - existing) <= 1e-9
                        for existing in deflections):
                    deflections.append(cur_deflection)
                if cur_deflection <= min_deflection * 1.25:
                    break
                cur_deflection *= 0.5
            if not deflections:
                deflections.append(base_deflection)
            if not any(
                    abs(min_deflection - existing) <= 1e-9
                    for existing in deflections):
                deflections.append(min_deflection)
            return deflections

        def _subdivide_triangle_uniform(tri, max_depth):
            pending = [([FreeCAD.Vector(pt) for pt in tri], 0)]
            out = []
            area_tol = 1e-10
            while pending:
                cur_tri, depth = pending.pop()
                area_vec = (
                    (cur_tri[1] - cur_tri[0]).cross(
                        cur_tri[2] - cur_tri[0]))
                if (depth >= max_depth
                        or getattr(area_vec, 'Length', 0.0) <= area_tol):
                    out.append(cur_tri)
                    continue

                p0 = FreeCAD.Vector(cur_tri[0])
                p1 = FreeCAD.Vector(cur_tri[1])
                p2 = FreeCAD.Vector(cur_tri[2])
                m01 = (p0 + p1) * 0.5
                m12 = (p1 + p2) * 0.5
                m20 = (p2 + p0) * 0.5

                sub_tris = [
                    [p0, m01, m20],
                    [m01, p1, m12],
                    [m20, m12, p2],
                    [m01, m12, m20],
                ]
                for sub_tri in sub_tris:
                    pending.append((sub_tri, depth + 1))
            return out

        def _triangle_fallback_subdivision_info(
                wedge_ctx, reduction_levels=1):
            target_edge_splits = max(
                int(wedge_ctx.get('target_edge_splits', 8) or 8), 1)
            base_depth = 0
            if target_edge_splits > 1:
                base_depth = max(
                    0,
                    int(round(math.log(target_edge_splits, 2))))
            max_depth = max(0, base_depth - max(int(reduction_levels), 0))
            effective_splits = max(1, 1 << max_depth)
            return target_edge_splits, effective_splits, max_depth

        def _build_bent_source_face_triangle_patches(
                source_face, wedge_ctx):
            tri_faces = []
            (target_edge_splits,
             effective_splits,
             max_depth) = _triangle_fallback_subdivision_info(
                wedge_ctx, reduction_levels=1)
            used_deflection = None
            deflection_attempts = _source_face_tessellation_deflections(
                source_face, wedge_ctx)

            for deflection in deflection_attempts:
                tri_faces = []
                for tri in _tessellate_face_triangles(source_face, deflection):
                    sub_tris = _subdivide_triangle_uniform(
                        tri, max_depth)
                    for sub_tri in sub_tris:
                        bent = [
                            _bend_wedge_point(pt, wedge_ctx)
                            for pt in sub_tri
                        ]
                        tri_face = _make_triangle_face(
                            bent[0], bent[1], bent[2])
                        if tri_face is not None:
                            tri_faces.append(tri_face)
                if tri_faces:
                    used_deflection = deflection
                    break

            if tri_faces and wedge_diag:
                attempt_msg = ""
                if len(deflection_attempts) > 1:
                    attempt_msg = (
                        f" tries={len(deflection_attempts)}")
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   curved face tri-fallback"
                    f" patches={len(tri_faces)}"
                    f" splits={effective_splits}"
                    f" defl={used_deflection:.4f}"
                    f" depth={max_depth}"
                    f"{attempt_msg}\n")
            return tri_faces

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
            wedge_stationary_pi = mi_to_stationary_pi.get(s_mi)
            micro_angle_s = micro_bend_info[s_mi][0]

            # Use saved pivot data from Phase 3
            saved = micro_pivots.get(s_mi)
            if saved is None:
                continue
            saved_plc, cur_p0, cur_normal, cur_up, bend_axis, \
                saved_pivot = saved
            # Each s_mi already identifies a specific cut segment and its
            # stationary-side frame. Use that saved frame directly for every
            # wedge instead of re-picking a "nearest" segment for multi-seg
            # bends, which can select the wrong local CoC.
            pivot = saved_pivot
            bend_obj_bi = bend_info[bi][0]
            seg_mids = bend_seg_mids.get(bi, [])

            coc = pivot

            sweep_angle = micro_angle_s

            # Save CoC offset for bend line (applied after lofts)
            if bi not in coc_offsets:
                coc_offsets[bi] = (bend_obj_bi, s_mi)

            if wedge_diag:
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
                sid_dbg = strip_to_seg.get(pi)
                if sid_dbg is not None and 0 <= sid_dbg < len(joints):
                    joint_dbg = joints[sid_dbg]
                    seg_p0_dbg, seg_p1_dbg = joint_dbg['center']
                    metrics_dbg = _piece_segment_debug_metrics(
                        pieces[pi], seg_p0_dbg, seg_p1_dbg)
                    touch_faces_dbg = []
                    for fi_dbg in range(len(cut_faces)):
                        try:
                            d_touch_dbg = piece_slices[pi].distToShape(
                                cut_segments[fi_dbg])[0]
                        except Exception:
                            d_touch_dbg = float('inf')
                        if d_touch_dbg < GEOMETRY_TOLERANCE:
                            touch_faces_dbg.append(fi_dbg)
                    touch_sids_dbg = sorted(set(
                        face_to_seg[fi_dbg]
                        for fi_dbg in touch_faces_dbg
                        if fi_dbg in face_to_seg))
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   wedge-src"
                        f" sid={sid_dbg}"
                        f" faces=A{joint_dbg['a_faces']}/B{joint_dbg['b_faces']}"
                        f" cm_t_raw={metrics_dbg['cm_t_raw']:.6f}"
                        f" t_raw=[{metrics_dbg['t_raw_min']:.6f},{metrics_dbg['t_raw_max']:.6f}]"
                        f" d=[{metrics_dbg['d_min']:.6f},{metrics_dbg['d_max']:.6f}]"
                        f" touch_sid={touch_sids_dbg if touch_sids_dbg else '-'}"
                        f"\n")
                    if (metrics_dbg['t_raw_min'] < -GEOMETRY_TOLERANCE
                            or metrics_dbg['t_raw_max'] > 1.0 + GEOMETRY_TOLERANCE):
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD:   wedge-src p{pi}"
                            f" exceeds assigned sid={sid_dbg}"
                            f" t_raw=[{metrics_dbg['t_raw_min']:.6f},"
                            f"{metrics_dbg['t_raw_max']:.6f}]\n")
                    if touch_sids_dbg and touch_sids_dbg != [sid_dbg]:
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD:   wedge-src p{pi}"
                            f" assigned_sid={sid_dbg}"
                            f" touch_sid={touch_sids_dbg}\n")

            # Count how many distinct mi's of this bend the piece crosses
            piece_mi_set_pi = set(piece_mi_list[pi])
            s_mult = sum(1 for mi_chk in bend_s_mis.get(bi, [])
                         if mi_chk in piece_mi_set_pi)
            positioned_flat = wedge_pre_shapes[pi].copy() \
                if pi in wedge_pre_shapes \
                else piece_shapes[pi].copy()
            flat_cm = _shape_center(positioned_flat)
            target_shape = piece_shapes[pi]
            target_cm = _shape_center(target_shape)
            target_vol = _shape_volume(target_shape) if wedge_diag else 0.0
            flat_to_target = target_cm - flat_cm

            if wedge_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   s_mult={s_mult}"
                    f" micro_angle={math.degrees(micro_angle_s):.1f}°"
                    f" pre_shape={'Y' if pi in wedge_pre_shapes else 'N'}"
                    f" flat_cm={_fmt_vec(flat_cm)}"
                    f" target_cm={_fmt_vec(target_cm)}"
                    f" target_vol={target_vol:.6f}"
                    f" flat_to_target={_fmt_vec(flat_to_target)}"
                    f" |flat_to_target|={_vec_length(flat_to_target):.6f}"
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
            # Collect vertex projection d-values so we can both split on
            # topology changes and diagnose wedges whose geometry falls
            # outside the nominal [0, 2*ins] slice window.
            proj_ds_all = sorted(
                (v.Point - cur_p0).dot(cur_normal)
                for v in positioned_flat.Vertexes)
            proj_min = proj_ds_all[0] if proj_ds_all else float('nan')
            proj_max = proj_ds_all[-1] if proj_ds_all else float('nan')
            if proj_ds_all:
                d_lo = proj_min + gt * 0.5
                d_hi = proj_max - gt * 0.5
                if d_hi <= d_lo:
                    mid_d = 0.5 * (proj_min + proj_max)
                    d_lo = mid_d - gt * 0.25
                    d_hi = mid_d + gt * 0.25
                near_d = proj_max if abs(proj_max) <= abs(proj_min) \
                    else proj_min
                far_d = proj_min if near_d == proj_max else proj_max
            else:
                d_lo = gt
                d_hi = 2 * ins - gt
                near_d = gt
                far_d = 2 * ins - gt
            d_span = d_hi - d_lo
            far_span = abs(far_d - near_d)
            near_ref = cur_p0 + cur_normal * near_d + cur_up * half_t
            far_ref = cur_p0 + cur_normal * far_d + cur_up * half_t
            sweep_span_dbg = abs(ins) * 2.0
            if sweep_span_dbg <= 1e-6:
                sweep_span_dbg = far_span
            if wedge_diag:
                flat_anchor_near = _closest_point_on_shape(
                    positioned_flat, near_ref)
                flat_anchor_far = _closest_point_on_shape(
                    positioned_flat, far_ref)

                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   anchor-flat"
                    f" near_ref={_fmt_vec(near_ref)}"
                    f" near_pt={_fmt_vec(flat_anchor_near)}"
                    f" far_ref={_fmt_vec(far_ref)}"
                    f" far_pt={_fmt_vec(flat_anchor_far)}"
                    f"\n")
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   sweep-window"
                    f" ref=0.000000"
                    f" span={sweep_span_dbg:.6f}"
                    f" actual_d=[{near_d:.6f},{far_d:.6f}]"
                    f"\n")

            anchor_post_plc = None
            anchor_ref_near = FreeCAD.Vector(near_ref)
            anchor_ref_far = FreeCAD.Vector(far_ref)
            if pi in wedge_post_mi_plc:
                anchor_post_plc = piece_plc[
                    pi].multiply(
                    wedge_post_mi_plc[
                        pi].inverse())
                try:
                    anchor_ref_near = anchor_post_plc.multVec(
                        near_ref)
                    anchor_ref_far = anchor_post_plc.multVec(
                        far_ref)
                except Exception:
                    anchor_ref_near = FreeCAD.Vector(near_ref)
                    anchor_ref_far = FreeCAD.Vector(far_ref)
            anchor_target_near = _closest_point_on_shape(
                target_shape, anchor_ref_near)
            anchor_target_far = _closest_point_on_shape(
                target_shape, anchor_ref_far)

            wedge_ctx = _make_wedge_build_context(
                pi=pi,
                bi=bi,
                s_mi=s_mi,
                positioned_flat=positioned_flat,
                target_shape=target_shape,
                flat_cm=flat_cm,
                target_cm=target_cm,
                target_vol=target_vol,
                cur_p0=cur_p0,
                cur_normal=cur_normal,
                cur_up=cur_up,
                bend_axis=bend_axis,
                coc=coc,
                sweep_angle=sweep_angle,
                ins=ins,
                near_d=near_d,
                far_d=far_d,
                far_span=far_span,
                near_ref=near_ref,
                far_ref=far_ref,
                slice_count=N_SLICES,
                target_edge_splits=wedge_target_edge_splits,
                anchor_post_plc=anchor_post_plc,
                anchor_ref_near=anchor_ref_near,
                anchor_ref_far=anchor_ref_far,
                anchor_target_near=anchor_target_near,
                anchor_target_far=anchor_target_far)
            wedge_ctx['profile'] = _extract_flat_wedge_profile(
                wedge_ctx)

            # Build uniform d-values over the wedge's actual projected span.
            d_uniform = []
            for si in range(N_SLICES + 1):
                frac = si / float(N_SLICES)
                d_uniform.append(d_lo + frac * d_span)
            vertex_proj_ds = []
            for vd in proj_ds_all:
                if d_lo < vd < d_hi:
                    vertex_proj_ds.append(vd)
            vertex_proj_ds = sorted(set(vertex_proj_ds))

            min_sep = max(gt * 2, abs(d_span) / 1000)
            segments = []
            all_wires_flat = []  # for debug logging
            attempted_ds = []
            split_ds = []
            if not is_wireframe_wedge:
                # Split the d-range at vertex projection planes
                # so each sub-range has consistent cross-section
                # topology.  We slice the original solid for each
                # sub-range (no half-space cutting needed).
                split_ds = vertex_proj_ds  # d-values to split at
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

                if wedge_diag:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD:   {len(sub_ranges)} sub-range(s)"
                        f" at split ds="
                        f"{[f'{d:.6f}' for d in split_ds]}\n")

                # For each sub-range, generate d-values, slice
                # the original solid, transform, and collect wires.
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
                    seg_ds = sorted(
                        seg_ds_dedup,
                        key=lambda sd: abs(sd - near_d))

                    seg_slices = []
                    seg_wire_counts = []
                    for d in seg_ds:
                        attempted_ds.append(d)
                        frac = _wedge_sweep_fraction(d, wedge_ctx)
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

                        slice_angle = frac * sweep_angle
                        trans_vec = cur_normal * (-d)
                        slice_wires = []
                        for wire in wires:
                            w = wire.copy()
                            # Translate back then rotate to arc
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
                            slice_wires.append(w)
                            all_wires_flat.append(w)
                        if not slice_wires:
                            continue
                        seg_slices.append((d, slice_wires))
                        seg_wire_counts.append(len(slice_wires))

                    if len(seg_slices) < 2:
                        continue

                    seg_slices = _harmonize_boundary_slices(
                        seg_slices, si_sub)
                    seg_wire_counts = [
                        len(slice_wires)
                        for _d, slice_wires in seg_slices
                    ]

                    wire_count_set = {
                        len(slice_wires)
                        for _d, slice_wires in seg_slices
                    }
                    if len(wire_count_set) != 1:
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD:   inconsistent slice wire counts"
                            f" sub={si_sub}"
                            f" ds={[f'{d:.6f}' for d, _ in seg_slices]}"
                            f" counts={seg_wire_counts}"
                            f" -> fallback first-wire\n")
                        seg_wires = [
                            slice_wires[0]
                            for _d, slice_wires in seg_slices
                        ]
                        segments.append(
                            _normalize_wire_sequence(seg_wires))
                        continue

                    wire_count = next(iter(wire_count_set))
                    if wire_count > 1 and wedge_diag:
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD:   multi-wire sub={si_sub}"
                            f" count={wire_count}"
                            f" ds={[f'{d:.6f}' for d, _ in seg_slices]}\n")

                    first_wires = sorted(
                        seg_slices[0][1],
                        key=lambda wire: (
                            _shape_center(wire).dot(bend_axis),
                            _shape_center(wire).dot(cur_up),
                            _shape_center(wire).dot(cur_normal)))
                    branch_sequences = [[w] for w in first_wires]
                    prev_wires = first_wires

                    for _d, slice_wires in seg_slices[1:]:
                        if wire_count == 1:
                            ordered_wires = [slice_wires[0]]
                        else:
                            ordered_wires = _match_wires_by_center(
                                prev_wires, slice_wires)
                        for branch_idx, wire in enumerate(ordered_wires):
                            branch_sequences[branch_idx].append(wire)
                        prev_wires = ordered_wires

                    for branch_seq in branch_sequences:
                        if len(branch_seq) < 2:
                            continue
                        segments.append(
                            _normalize_wire_sequence(branch_seq))

            if wedge_diag and not is_wireframe_wedge:
                wire_edges = [len(w.Edges)
                              for w in all_wires_flat]
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD:   slices={len(all_wires_flat)}"
                    f" edges={wire_edges}\n")

            if not is_wireframe_wedge and not all_wires_flat:
                bbox = positioned_flat.BoundBox
                attempted_ds = sorted(set(attempted_ds))
                attempted_side_counts = []
                for d in attempted_ds:
                    lt = sum(1 for vd in proj_ds_all if vd < d - gt)
                    eq = sum(1 for vd in proj_ds_all
                             if abs(vd - d) <= gt)
                    gt_count = sum(1 for vd in proj_ds_all if vd > d + gt)
                    attempted_side_counts.append(
                        f"{d:.6f}:{lt}/{eq}/{gt_count}")

                retry_ds = []
                retry_hits = []
                if proj_ds_all:
                    diag_lo = proj_min + gt * 0.5
                    diag_hi = proj_max - gt * 0.5
                    if diag_hi <= diag_lo:
                        retry_ds = [0.5 * (proj_min + proj_max)]
                    else:
                        n_retry = 5
                        if n_retry == 1:
                            retry_ds = [0.5 * (diag_lo + diag_hi)]
                        else:
                            for ri in range(n_retry):
                                frac_r = ri / float(n_retry - 1)
                                retry_ds.append(
                                    diag_lo
                                    + frac_r * (diag_hi - diag_lo))
                    for rd in retry_ds:
                        plane_dist = (
                            cur_p0.x * cur_normal.x
                            + cur_p0.y * cur_normal.y
                            + cur_p0.z * cur_normal.z
                            + rd)
                        try:
                            retry_wires = positioned_flat.slice(
                                cur_normal, plane_dist)
                            retry_hits.append(len(retry_wires))
                        except Exception:
                            retry_hits.append(-1)

                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD:   slice-miss"
                    f" proj=[{proj_min:.6f},{proj_max:.6f}]"
                    f" nominal=[{d_lo:.6f},{d_hi:.6f}]"
                    f" overlap="
                    f"{'Y' if proj_ds_all and not (proj_max < d_lo or proj_min > d_hi) else 'N'}"
                    f" valid={positioned_flat.isValid()}"
                    f" verts={len(positioned_flat.Vertexes)}"
                    f" edges={len(positioned_flat.Edges)}"
                    f" faces={len(positioned_flat.Faces)}"
                    f" solids={len(positioned_flat.Solids)}"
                    f" bbox=({bbox.XMin:.3f},{bbox.YMin:.3f},{bbox.ZMin:.3f})"
                    f"→({bbox.XMax:.3f},{bbox.YMax:.3f},{bbox.ZMax:.3f})"
                    f" tried={attempted_side_counts}"
                    f" retry={list(zip([round(d, 6) for d in retry_ds], retry_hits))}"
                    f"\n")

            def _build_wedge_shape(mode, ctx):
                if mode == "Wireframe":
                    return _build_wedge_wireframe_analytic(ctx)
                if mode == "Smooth":
                    curved_solid = _build_wedge_solid_hybrid(ctx)
                    if curved_solid is not None:
                        if wedge_diag:
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD:   curved solid ok"
                                f" vol={_shape_volume(curved_solid):.4f}\n")
                        return curved_solid
                    if wedge_diag:
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD: curved solid failed"
                            f" for piece {ctx['pi']},"
                            f" fallback=wireframe\n")
                    return _build_wedge_wireframe_analytic(ctx)
                return _build_wedge_wireframe_analytic(ctx)

            loft = _build_wedge_shape(wedge_mode, wedge_ctx)

            if loft is not None:
                    loft_pre_cm = _shape_center(loft)
                    remaining_plc = None
                    remaining_axis = FreeCAD.Vector() if wedge_diag else None
                    target_cm_pre = FreeCAD.Vector(target_cm)
                    target_near_ref = FreeCAD.Vector(near_ref)
                    target_far_ref = FreeCAD.Vector(far_ref)
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
                        if wedge_diag:
                            try:
                                remaining_axis = \
                                    remaining_plc.Rotation.Axis
                            except Exception:
                                remaining_axis = FreeCAD.Vector()
                        try:
                            target_cm_pre = remaining_plc.inverse(
                            ).multVec(target_cm)
                        except Exception:
                            target_cm_pre = FreeCAD.Vector(target_cm)
                        try:
                            target_near_ref = remaining_plc.multVec(
                                near_ref)
                            target_far_ref = remaining_plc.multVec(
                                far_ref)
                        except Exception:
                            target_near_ref = FreeCAD.Vector(near_ref)
                            target_far_ref = FreeCAD.Vector(far_ref)
                        ra = remaining_plc.Rotation.Angle
                        rb = remaining_plc.Base.Length
                        if wedge_diag:
                            loft_anchor_near_pre = _closest_point_on_shape(
                                loft, near_ref)
                            loft_anchor_far_pre = _closest_point_on_shape(
                                loft, far_ref)
                            delta_anchor_near_flat = (
                                loft_anchor_near_pre - flat_anchor_near)
                            delta_anchor_far_flat = (
                                loft_anchor_far_pre - flat_anchor_far)
                            delta_pre_target = loft_pre_cm - target_cm_pre
                            delta_pre_flat = loft_pre_cm - flat_cm
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD: wedge pi={pi}"
                                f" pre-remain"
                                f" loft_cm={_fmt_vec(loft_pre_cm)}"
                                f" target_pre={_fmt_vec(target_cm_pre)}"
                                f" flat_cm={_fmt_vec(flat_cm)}"
                                f" delta_target={_fmt_vec(delta_pre_target)}"
                                f" |delta_target|={_vec_length(delta_pre_target):.6f}"
                                f" delta_flat={_fmt_vec(delta_pre_flat)}"
                                f" |delta_flat|={_vec_length(delta_pre_flat):.6f}"
                                f"\n")
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD: wedge pi={pi}"
                                f" anchor-pre"
                                f" near={_fmt_vec(loft_anchor_near_pre)}"
                                f" delta_near_flat={_fmt_vec(delta_anchor_near_flat)}"
                                f" |delta_near_flat|={_vec_length(delta_anchor_near_flat):.6f}"
                                f" far={_fmt_vec(loft_anchor_far_pre)}"
                                f" delta_far_flat={_fmt_vec(delta_anchor_far_flat)}"
                                f" |delta_far_flat|={_vec_length(delta_anchor_far_flat):.6f}"
                                f"\n")
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD: wedge pi={pi}"
                                f" remaining_rot="
                                f"{math.degrees(ra):.6f}°"
                                f" remaining_axis={_fmt_vec(remaining_axis)}"
                                f" remaining_base={_fmt_vec(remaining_plc.Base)}"
                                f" remaining_trans={rb:.6f}"
                                f"\n")
                        applied_plc = remaining_plc
                        applied_frac = 1.0
                        if ra <= 1e-6 and rb > 1e-6:
                            neighbor_shapes = []
                            for nbr, _bi, _fi in adjacency[pi]:
                                if nbr in strip_pieces:
                                    continue
                                if nbr < 0 or nbr >= len(piece_shapes):
                                    continue
                                neighbor_shapes.append(
                                    (nbr, piece_shapes[nbr]))
                            if neighbor_shapes:
                                target_anchor_near = _closest_point_on_shape(
                                    target_shape, target_near_ref)
                                target_anchor_far = _closest_point_on_shape(
                                    target_shape, target_far_ref)
                                best_score = None
                                best_plc = None
                                best_frac = 1.0
                                adaptive_scores = []
                                for frac in (0.0, 0.5, 1.0):
                                    cand_plc = FreeCAD.Placement()
                                    cand_plc.Base = FreeCAD.Vector(
                                        remaining_plc.Base.x * frac,
                                        remaining_plc.Base.y * frac,
                                        remaining_plc.Base.z * frac)
                                    cand_shape = loft.copy()
                                    if cand_plc.Base.Length > 1e-12:
                                        cand_shape.transformShape(
                                            cand_plc.toMatrix())
                                    cand_dists = []
                                    cand_labels = [] if wedge_diag else None
                                    for nbr, nbr_shape in neighbor_shapes:
                                        d_adj = _shape_distance(
                                            cand_shape, nbr_shape)
                                        if math.isnan(d_adj):
                                            continue
                                        cand_dists.append(d_adj)
                                        if wedge_diag:
                                            cand_labels.append(
                                                f"p{nbr}:{d_adj:.6f}")
                                    if not cand_dists:
                                        if wedge_diag:
                                            adaptive_scores.append(
                                                f"{frac:.2f}:nan")
                                        continue
                                    cand_center = _shape_center(cand_shape)
                                    cand_center_err = (
                                        cand_center - target_cm).Length
                                    cand_anchor_near = _closest_point_on_shape(
                                        cand_shape, target_near_ref)
                                    cand_anchor_far = _closest_point_on_shape(
                                        cand_shape, target_far_ref)
                                    cand_anchor_err_near = (
                                        cand_anchor_near.distanceToPoint(
                                            target_anchor_near))
                                    cand_anchor_err_far = (
                                        cand_anchor_far.distanceToPoint(
                                            target_anchor_far))
                                    cand_score = (
                                        float(max(cand_dists)),
                                        float(sum(cand_dists)),
                                        float(max(
                                            cand_anchor_err_near,
                                            cand_anchor_err_far)),
                                        float(
                                            cand_anchor_err_near
                                            + cand_anchor_err_far),
                                        float(cand_center_err))
                                    if wedge_diag:
                                        adaptive_scores.append(
                                            f"{frac:.2f}:"
                                            f"max={cand_score[0]:.6f}"
                                            f" sum={cand_score[1]:.6f}"
                                            f" anchor={cand_score[2]:.6f}"
                                            f" anchor_sum={cand_score[3]:.6f}"
                                            f" center={cand_score[4]:.6f}"
                                            f" [{' '.join(cand_labels)}]")
                                    better = best_score is None
                                    if not better:
                                        # Treat sub-micron adjacency deltas as
                                        # ties so the center match can decide.
                                        for axis, (cand_v, best_v) in enumerate(
                                                zip(cand_score, best_score)):
                                            tol = 1e-6 if axis < 4 else 1e-9
                                            if cand_v < best_v - tol:
                                                better = True
                                                break
                                            if cand_v > best_v + tol:
                                                break
                                    if better:
                                        best_score = cand_score
                                        best_plc = cand_plc
                                        best_frac = frac
                                if best_plc is not None:
                                    applied_plc = best_plc
                                    applied_frac = best_frac
                                    if wedge_diag:
                                        FreeCAD.Console.PrintMessage(
                                            f"FreekiCAD: wedge pi={pi}"
                                            f" adaptive-translate"
                                            f" choose={applied_frac:.2f}"
                                            f" scores=["
                                            f"{'; '.join(adaptive_scores)}"
                                            f"]\n")
                            elif is_wireframe_wedge:
                                base = remaining_plc.Base
                                base_len2 = (
                                    base.x * base.x
                                    + base.y * base.y
                                    + base.z * base.z)
                                if base_len2 > 1e-18:
                                    center_delta = target_cm - loft_pre_cm
                                    proj_frac = center_delta.dot(base) / base_len2
                                    proj_frac = max(0.0, min(1.0, proj_frac))
                                    applied_plc = FreeCAD.Placement()
                                    applied_plc.Base = FreeCAD.Vector(
                                        base.x * proj_frac,
                                        base.y * proj_frac,
                                        base.z * proj_frac)
                                    applied_frac = proj_frac
                                    if wedge_diag:
                                        FreeCAD.Console.PrintMessage(
                                            f"FreekiCAD: wedge pi={pi}"
                                            f" center-translate"
                                            f" choose={applied_frac:.6f}"
                                            f" center_delta={_fmt_vec(center_delta)}"
                                            f" base={_fmt_vec(base)}"
                                            f" fallback=no-neighbors\n")
                        if (ra > 1e-6
                                or applied_plc.Base.Length > 1e-6):
                            if ra <= 1e-6 and abs(applied_frac - 1.0) > 1e-9:
                                if wedge_diag:
                                    FreeCAD.Console.PrintMessage(
                                        f"FreekiCAD: wedge pi={pi}"
                                        f" applying adaptive translation"
                                        f" base={_fmt_vec(applied_plc.Base)}"
                                        f" frac={applied_frac:.2f}\n")
                            loft.transformShape(
                                applied_plc.toMatrix())
                    else:
                        if wedge_diag:
                            loft_anchor_near_pre = _closest_point_on_shape(
                                loft, near_ref)
                            loft_anchor_far_pre = _closest_point_on_shape(
                                loft, far_ref)
                            delta_anchor_near_flat = (
                                loft_anchor_near_pre - flat_anchor_near)
                            delta_anchor_far_flat = (
                                loft_anchor_far_pre - flat_anchor_far)
                            delta_pre_target = loft_pre_cm - target_cm
                            delta_pre_flat = loft_pre_cm - flat_cm
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD: wedge pi={pi}"
                                f" pre-remain"
                                f" loft_cm={_fmt_vec(loft_pre_cm)}"
                                f" target_pre={_fmt_vec(target_cm)}"
                                f" flat_cm={_fmt_vec(flat_cm)}"
                                f" delta_target={_fmt_vec(delta_pre_target)}"
                                f" |delta_target|={_vec_length(delta_pre_target):.6f}"
                                f" delta_flat={_fmt_vec(delta_pre_flat)}"
                                f" |delta_flat|={_vec_length(delta_pre_flat):.6f}"
                                f" no_post_mi_plc=Y"
                                f"\n")
                            FreeCAD.Console.PrintMessage(
                                f"FreekiCAD: wedge pi={pi}"
                                f" anchor-pre"
                                f" near={_fmt_vec(loft_anchor_near_pre)}"
                                f" delta_near_flat={_fmt_vec(delta_anchor_near_flat)}"
                                f" |delta_near_flat|={_vec_length(delta_anchor_near_flat):.6f}"
                                f" far={_fmt_vec(loft_anchor_far_pre)}"
                                f" delta_far_flat={_fmt_vec(delta_anchor_far_flat)}"
                                f" |delta_far_flat|={_vec_length(delta_anchor_far_flat):.6f}"
                                f" no_post_mi_plc=Y"
                                f"\n")
                    piece_shapes[pi] = loft
                    if wedge_diag:
                        cm = _shape_center(loft)
                        post_vol = _shape_volume(loft)
                        wedge_anchor_near = _closest_point_on_shape(
                            loft, target_near_ref)
                        wedge_anchor_far = _closest_point_on_shape(
                            loft, target_far_ref)
                        target_anchor_near = _closest_point_on_shape(
                            target_shape, target_near_ref)
                        target_anchor_far = _closest_point_on_shape(
                            target_shape, target_far_ref)
                        delta_post_target = cm - target_cm
                        delta_post_flat = cm - flat_cm
                        delta_anchor_near_target = (
                            wedge_anchor_near - target_anchor_near)
                        delta_anchor_far_target = (
                            wedge_anchor_far - target_anchor_far)
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD: wedge {wedge_mode.lower()}:"
                            f" post_vol={post_vol:.6f}"
                            f" target_vol={target_vol:.6f}"
                            f" target_cm={_fmt_vec(target_cm)}"
                            f" wedge_cm={_fmt_vec(cm)}"
                            f" delta_target={_fmt_vec(delta_post_target)}"
                            f" |delta_target|={_vec_length(delta_post_target):.6f}"
                            f" delta_flat={_fmt_vec(delta_post_flat)}"
                            f" |delta_flat|={_vec_length(delta_post_flat):.6f}"
                            f"\n")
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD: wedge {wedge_mode.lower()} anchor:"
                            f" near_ref={_fmt_vec(target_near_ref)}"
                            f" target_near={_fmt_vec(target_anchor_near)}"
                            f" wedge_near={_fmt_vec(wedge_anchor_near)}"
                            f" delta_near={_fmt_vec(delta_anchor_near_target)}"
                            f" |delta_near|={_vec_length(delta_anchor_near_target):.6f}"
                            f" far_ref={_fmt_vec(target_far_ref)}"
                            f" target_far={_fmt_vec(target_anchor_far)}"
                            f" wedge_far={_fmt_vec(wedge_anchor_far)}"
                            f" delta_far={_fmt_vec(delta_anchor_far_target)}"
                            f" |delta_far|={_vec_length(delta_anchor_far_target):.6f}"
                            f"\n")
                        stat_bridge = "-"
                        if (wedge_stationary_pi is not None
                                and 0 <= wedge_stationary_pi
                                < len(piece_shapes)):
                            d_stat = _shape_distance(
                                loft, piece_shapes[wedge_stationary_pi])
                            stat_bridge = (
                                f"p{wedge_stationary_pi}:{d_stat:.6f}")
                        moving_bridge = []
                        for mpi in wedge_to_moving_pis.get(pi, ()):
                            if mpi < 0 or mpi >= len(piece_shapes):
                                continue
                            d_move = _shape_distance(
                                loft, piece_shapes[mpi])
                            moving_bridge.append(
                                f"p{mpi}:{d_move:.6f}")
                        adjacent_bridge = []
                        for nbr, _bi, _fi in adjacency[pi]:
                            if nbr in strip_pieces:
                                continue
                            if nbr < 0 or nbr >= len(piece_shapes):
                                continue
                            d_adj = _shape_distance(
                                loft, piece_shapes[nbr])
                            adjacent_bridge.append(
                                f"p{nbr}:{d_adj:.6f}")
                        adjacent_bridge.sort()
                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD: wedge bridge"
                            f" p{pi}"
                            f" entry_mi={s_mi}"
                            f" stationary={stat_bridge}"
                            f" moving=["
                            f"{', '.join(moving_bridge) if moving_bridge else '-'}"
                            f"]"
                            f" adjacent=["
                            f"{', '.join(adjacent_bridge) if adjacent_bridge else '-'}"
                            f"]\n")
            if wedge_diag:
                FreeCAD.Console.PrintMessage(
                    f"FreekiCAD: [profile] wedge p{pi}"
                    f" loft: {_time.time() - _t_loft_one:.3f}s\n")

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: [profile] Wedge build: "
            f"{_time.time() - _t_loft:.3f}s\n")

        bend_plc_debug = {}
        for child in obj.Group:
            proxy = getattr(child, 'Proxy', None)
            if proxy and getattr(proxy, 'Type', None) == 'BendLine':
                bend_plc_debug[child.Name] = child.Placement.copy()

        # Bend lines represent the bend center. Their placements have
        # already been updated by the same rotation chain as the board
        # pieces, so don't add any extra visual offset here.
        for bi, (bl_obj, first_mi) in coc_offsets.items():
            _, p0_bi, _, _, _, _, _ = bend_info[bi]
            final_plc = bl_obj.Placement
            final_bend_p0 = final_plc.multVec(p0_bi)
            final_up = final_plc.Rotation.multVec(up)
            final_center = final_bend_p0 + final_up * half_t
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: bendline {bl_obj.Name} (mi={first_mi})"
                f" center=({final_center.x:.2f},{final_center.y:.2f},"
                f"{final_center.z:.2f})"
                f" off=(0.000,0.000,0.000)\n")

        # Draw debug visualizations if enabled
        show_debug = getattr(obj, 'BuildDebugObjects', False)
        if show_debug and pieces:
            self._draw_debug_arrows(
                obj, pieces, piece_bend_sets, bfs_tree,
                strip_pieces, strip_to_bend,
                bend_info, insets, half_t,
                micro_bend_info, bendline_piece_idx,
                mi_seg_idx=mi_seg_idx,
                piece_shapes=piece_shapes,
                piece_center_overrides=debug_piece_centers)
            self._draw_debug_cuts(
                obj, cut_plan, thickness,
                bend_plc_original=bend_plc_original,
                bend_plc_debug=bend_plc_debug,
                cut_owner_piece=cut_owner_piece,
                piece_plc=piece_plc,
                face_to_seg=face_to_seg)
        elif show_debug:
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
                path = _piece_path_labels(pi)
                path_str = "/".join(path) if path \
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

        def _build_piece_wireframe_fallback(shape, pi):
            if shape is None:
                return None

            edges = []
            try:
                source_edges = list(getattr(shape, 'Edges', []))
            except Exception:
                source_edges = []

            for edge in source_edges:
                try:
                    edge_copy = edge.copy()
                except Exception:
                    continue
                try:
                    if edge_copy.isNull():
                        continue
                except Exception:
                    pass
                try:
                    if abs(float(edge_copy.Length)) <= 1e-9:
                        continue
                except Exception:
                    pass
                edges.append(edge_copy)

            if not edges:
                return None

            try:
                fallback = Part.makeCompound(edges)
            except Exception:
                return None

            try:
                if fallback.isNull():
                    return None
            except Exception:
                pass

            if wedge_diag:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: piece {pi} fallback to wireframe"
                    f" edges={len(edges)}\n")
            return fallback

        def _shape_should_display(shape):
            if shape is None:
                return False
            try:
                if shape.isValid():
                    return True
            except Exception:
                pass
            try:
                has_edges = len(getattr(shape, 'Edges', [])) > 0
                has_faces = len(getattr(shape, 'Faces', [])) > 0
                has_solids = len(getattr(shape, 'Solids', [])) > 0
                return has_edges and not has_faces and not has_solids
            except Exception:
                return False

        def _repair_piece_shape_for_display(shape, pi):
            if shape is None:
                return None
            try:
                if shape.isValid():
                    return shape
            except Exception:
                pass

            if wedge_diag:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: piece {pi} invalid after bending,"
                    f" attempting repair\n")

            fix_tol = GEOMETRY_TOLERANCE
            fix_max_tol = max(GEOMETRY_TOLERANCE, GEOMETRY_TOLERANCE * 10.0)
            attempts = []

            def _add_attempt(label, candidate):
                if candidate is not None:
                    attempts.append((label, candidate))

            try:
                fixed = shape.copy()
                fixed.fix(fix_tol, fix_tol, fix_max_tol)
                _add_attempt("fix", fixed)
            except Exception:
                pass

            try:
                split = shape.copy().removeSplitter()
                _add_attempt("removeSplitter", split)
            except Exception:
                pass

            try:
                fixed_split = shape.copy()
                fixed_split.fix(fix_tol, fix_tol, fix_max_tol)
                fixed_split = fixed_split.removeSplitter()
                fixed_split.fix(fix_tol, fix_tol, fix_max_tol)
                _add_attempt("fix+removeSplitter+fix", fixed_split)
            except Exception:
                pass

            for label, candidate in attempts:
                try:
                    cand_valid = candidate.isValid()
                except Exception:
                    cand_valid = False
                if not cand_valid:
                    continue
                try:
                    cand_vol = abs(float(candidate.Volume))
                except Exception:
                    cand_vol = 0.0
                if cand_vol <= 1e-9:
                    continue
                if wedge_diag:
                    FreeCAD.Console.PrintMessage(
                        f"FreekiCAD: piece {pi} repaired"
                        f" via {label} vol={cand_vol:.4f}\n")
                return candidate

            if wedge_mode == "Smooth" and pi in strip_pieces:
                fallback = _build_piece_wireframe_fallback(shape, pi)
                if fallback is not None:
                    return fallback

            if wedge_diag:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: piece {pi} repair failed,"
                    f" dropping from display\n")
            return None

        if enable_bending:
            saved_color = None
            try:
                saved_color = board_obj.ViewObject.ShapeColor
            except Exception:
                pass

            piece_shapes = [
                _repair_piece_shape_for_display(s, pi)
                for pi, s in enumerate(piece_shapes)
            ]
            board_obj.Shape = Part.makeCompound(
                [s for s in piece_shapes if _shape_should_display(s)])

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
                path = _piece_path_labels(pi)
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
                    wedge_color = (1.0, 0.0, 0.0, 0.0)
                    _write_face_colors(
                        pobj.ViewObject,
                        [wedge_color] * max(1, len(s.Faces)))
                    try:
                        pobj.ViewObject.LineColor = (0.6, 0.0, 0.0)
                    except Exception:
                        pass
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
                          bend_plc_debug=None,
                          cut_owner_piece=None,
                          piece_plc=None,
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
            start_flat = FreeCAD.Vector(
                sp0.x, sp0.y, sp0.z + thickness)
            end_flat = FreeCAD.Vector(
                sp1.x, sp1.y, sp1.z + thickness)
            owner_pi = (cut_owner_piece.get(ci)
                        if cut_owner_piece else None)
            if (owner_pi is not None and piece_plc is not None
                    and 0 <= owner_pi < len(piece_plc)):
                start = piece_plc[owner_pi].multVec(start_flat)
                end = piece_plc[owner_pi].multVec(end_flat)
            # Fallback: transform using the physical bend placement.
            # The live bend line may later receive a visual-only
            # offset so it sits on the wedge center.
            elif bend_plc_original is not None and len(entry) > 8:
                bend_obj = entry[8]
                orig_plc = bend_plc_original.get(
                    bend_obj.Name)
                if orig_plc is not None:
                    bend_plc = bend_obj.Placement
                    if bend_plc_debug is not None:
                        bend_plc = bend_plc_debug.get(
                            bend_obj.Name, bend_plc)
                    xform = bend_plc.multiply(
                        orig_plc.inverse())
                    start = xform.multVec(start_flat)
                    end = xform.multVec(end_flat)
                else:
                    start = start_flat
                    end = end_flat
            else:
                start = start_flat
                end = end_flat
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

    def _clear_bend_debug_artifacts(self, obj, board_obj=None):
        """Remove transient bend debug objects from a previous solve."""
        doc = obj.Document
        for grp_name in (obj.Name + "_DebugCuts",
                         obj.Name + "_DebugPieces"):
            grp = doc.getObject(grp_name)
            if grp is None:
                continue
            for child in list(getattr(grp, "Group", [])):
                try:
                    doc.removeObject(child.Name)
                except Exception:
                    pass
            try:
                doc.removeObject(grp.Name)
            except Exception:
                pass

        debug_obj = doc.getObject(obj.Name + "_DebugArrows")
        if debug_obj is not None:
            try:
                doc.removeObject(debug_obj.Name)
            except Exception:
                try:
                    debug_obj.Shape = Part.Shape()
                except Exception:
                    pass

        if (board_obj is not None
                and hasattr(board_obj, 'ViewObject')
                and board_obj.ViewObject is not None):
            try:
                board_obj.ViewObject.Visibility = True
            except Exception:
                pass

    def _build_bend_span_shape(self, p0, p1, normal, inset, board_face):
        """Build the 2D board-clipped span area for one bend."""
        if board_face is None or inset <= GEOMETRY_TOLERANCE:
            return None

        line_dir = p1 - p0
        if line_dir.Length <= GEOMETRY_TOLERANCE:
            return None

        try:
            band = Part.Face(Part.makePolygon([
                p0 - normal * inset,
                p1 - normal * inset,
                p1 + normal * inset,
                p0 + normal * inset,
                p0 - normal * inset,
            ]))
        except Exception:
            return None

        try:
            span = band.common(board_face)
        except Exception:
            return None

        try:
            if span.isNull() or float(getattr(span, "Area", 0.0)) <= 0.0:
                return None
        except Exception:
            pass
        return span

    def _update_conflicts_debug_object(self, obj, conflict_shape, thickness):
        """Show or remove the red conflict area overlay."""
        doc = obj.Document
        conflict_name = obj.Name + "_Conflicts"
        debug_obj = doc.getObject(conflict_name)

        has_conflicts = False
        if conflict_shape is not None:
            try:
                has_conflicts = (not conflict_shape.isNull()
                                 and float(getattr(
                                     conflict_shape, "Area", 0.0)) > 0.0)
            except Exception:
                has_conflicts = True

        if not has_conflicts:
            if debug_obj is not None:
                try:
                    doc.removeObject(debug_obj.Name)
                except Exception:
                    try:
                        debug_obj.Shape = Part.Shape()
                        debug_obj.ViewObject.Visibility = False
                    except Exception:
                        pass
            return

        try:
            display_shape = conflict_shape.copy()
        except Exception:
            display_shape = conflict_shape
        try:
            display_shape.translate(FreeCAD.Vector(
                0, 0, thickness + max(0.05, thickness * 0.02)))
        except Exception:
            pass

        if debug_obj is None:
            debug_obj = doc.addObject("Part::Feature", conflict_name)
            obj.addObject(debug_obj)
        debug_obj.Label = "Conflicts"
        debug_obj.Shape = display_shape

        try:
            debug_obj.ViewObject.ShapeColor = (1.0, 0.0, 0.0)
            debug_obj.ViewObject.LineColor = (0.6, 0.0, 0.0)
            debug_obj.ViewObject.Transparency = 75
            debug_obj.ViewObject.Visibility = True
        except Exception:
            pass

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
                            piece_shapes=None,
                            piece_center_overrides=None):
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
            if (piece_center_overrides is not None
                    and pi_idx in piece_center_overrides):
                return piece_center_overrides[pi_idx]
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
                            pos = -(bi_crossed + 2) \
                                if bi_crossed <= -2 \
                                else bi_crossed
                            if pos < 0:
                                continue
                            orig_bi = micro_bend_info[pos][5]
                            seg = mi_seg_idx.get(pos, 0)
                            prefix = "-" \
                                if bi_crossed <= -2 else ""
                            path.append(
                                f"{prefix}{orig_bi}.{seg}")
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
                                    joints=None, face_to_seg=None):
        """Build geometric adjacency: which pieces touch and via which cut face.

        Returns a list of (i, j, fi) tuples.

        Builds adjacency from the joint structure (pieces adjacent to
        each A/B face). Faces that do not map back to a trimmed bend
        segment are treated as rigid/no-bend crossings so phantom
        generalFuse splits do not disconnect the BFS tree.

        When *piece_slices* and *cut_segments* are provided, uses 2D
        geometry for distance checks instead of 3D solids.
        """
        n = len(pieces)
        tol = GEOMETRY_TOLERANCE
        shapes = piece_slices if piece_slices is not None else pieces

        # Group-based adjacency: for each cut face, find all
        # touching pieces; adjacent pairs share the face.
        face_pieces = {}  # fi → set of pi
        matched_faces = set(face_to_seg) if face_to_seg is not None else set()
        if joints is not None:
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
                    matched_faces.add(fi)

        for fi in range(len(cut_faces)):
            if fi in matched_faces:
                continue
            cf_shape = (cut_segments[fi]
                        if cut_segments is not None
                        else cut_faces[fi])
            adj = set()
            for pi in range(n):
                if shapes[pi].distToShape(cf_shape)[0] < tol:
                    adj.add(pi)
            if len(adj) >= 2:
                face_pieces[fi] = adj
        # Build crossings: pairs of pieces that share a cut face.
        #
        # Do not pre-filter by center-of-mass side here. Wedge/rigid
        # adjacency near a bend can be locally valid even when the two
        # piece centers happen to lie on the same side of the cut face.
        # The BFS stage performs the more robust, local side test against
        # the bend center segment and can prefer true cross-bend traversals
        # over same-side branch hops.
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
                    crossing_set.add(key)
                    geo_crossings.append((i, j, fi))
        return geo_crossings

    def _classify_pieces_bfs(self, pieces, cut_faces, face_to_bend,
                             mass_center, half_t, bend_info, cut_plan,
                             micro_bend_info=None, log=True,
                             mi_seg_idx=None,
                             cached_geo_crossings=None,
                             piece_slices=None, cut_segments=None,
                             strip_pieces=None,
                             joints=None,
                             face_to_seg=None):
        """BFS from the stationary piece with maximum-set preference.

        All crossings ADD the bend (union, sets only grow).

        BFS returns a normalized tree entry shape for both wedge and
        non-wedge traversals:
        ``(parent_pi, mis_crossed, wedge_pi)`` where *mis_crossed*
        is a set of mi indices and *wedge_pi* is ``None`` unless the
        piece was reached through a wedge strip.

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
                joints=joints,
                face_to_seg=face_to_seg)

        # Label crossings using face_to_bend mapping (cheap).
        # Each entry: (neighbor, mi_label, face_index_or_None)
        adjacency = [[] for _ in range(n)]
        for i, j, best_touch_fi in cached_geo_crossings:
            mi = face_to_bend.get(best_touch_fi, -1)
            adjacency[i].append((j, mi, best_touch_fi))
            adjacency[j].append((i, mi, best_touch_fi))

        # Helper to decode crossing label for logging.
        # Per-cut labels: "9.0", "9.0M" (M = moving-side crossing).
        def _crossing_label(bi):
            pos = -(bi + 2) if bi <= -2 else bi
            if pos >= 0 and micro_bend_info is not None:
                orig_bi = micro_bend_info[pos][5]
                seg = mi_seg_idx.get(pos, 0) \
                    if mi_seg_idx else 0
                prefix = "-" if bi <= -2 else ""
                return f"{prefix}{orig_bi}.{seg}"
            elif pos >= 0:
                return f"b{pos}"
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
                    _log_bending_bfs(
                        f"FreekiCAD: adjacent {pi} → "
                        f"{', '.join(crossings)}\n")

        # BFS: strict first-visit, all crossings add the bend.
        # No re-visiting, no re-queuing — first path wins.
        _sp = strip_pieces or set()

        def _side_test(pi_a, pi_b, fi_cut):
            """Return True if pieces a and b are on different sides
            of the bend's center segment identified by *fi_cut*.

            Uses the nearest vertex to the cut midpoint (that is not
            on the cut line itself) rather than CenterOfMass, so that
            curl-back pieces are classified by their local side near
            the cut rather than by a distant center of mass.  If that
            local heuristic is inconclusive or disagrees with the older
            center-of-mass test, fall back to the center-of-mass result
            so the traversal graph does not collapse.
            """
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
            seg_len = (sdx * sdx + sdy * sdy) ** 0.5
            if seg_len < 1e-12:
                return True

            def _near_cut_point(pi):
                """Pick the vertex of *pi* closest to the cut midpoint
                whose signed distance from the cut line exceeds a
                threshold.  Falls back to CenterOfMass when no vertex
                qualifies (e.g. degenerate sliver)."""
                mid_x = (sp0.x + sp1.x) * 0.5
                mid_y = (sp0.y + sp1.y) * 0.5
                best = None
                best_d2 = float('inf')
                for v in pieces[pi].Vertexes:
                    p = v.Point
                    cross = (sdx * (p.y - sp0.y)
                             - sdy * (p.x - sp0.x))
                    # Skip vertices that are (nearly) on the cut line.
                    if abs(cross) < 1e-3 * seg_len:
                        continue
                    d2 = ((p.x - mid_x) ** 2
                          + (p.y - mid_y) ** 2)
                    if d2 < best_d2:
                        best_d2 = d2
                        best = p
                return best if best is not None \
                    else pieces[pi].CenterOfMass

            pt_a = _near_cut_point(pi_a)
            pt_b = _near_cut_point(pi_b)
            ca = sdx * (pt_a.y - sp0.y) - sdy * (pt_a.x - sp0.x)
            cb = sdx * (pt_b.y - sp0.y) - sdy * (pt_b.x - sp0.x)
            if ca * cb < 0:
                return True

            # Fallback to the older center-of-mass classification when
            # the local nearest-vertex heuristic keeps both pieces on
            # the same side.  This preserves traversal through broad or
            # highly segmented pieces where the nearest local vertex can
            # be misleading.
            cm_a = pieces[pi_a].CenterOfMass
            cm_b = pieces[pi_b].CenterOfMass
            cm_ca = sdx * (cm_a.y - sp0.y) - sdy * (cm_a.x - sp0.x)
            cm_cb = sdx * (cm_b.y - sp0.y) - sdy * (cm_b.x - sp0.x)
            return cm_ca * cm_cb < 0

        def _get_bend_idx(bi):
            pos = -(bi + 2) if bi <= -2 else bi
            if pos >= 0 and micro_bend_info is not None:
                return micro_bend_info[pos][5]
            elif pos >= 0:
                return pos
            return None

        def _ordered_wedge_neighbors(src_pi, wedge_pi, entry_fi):
            """Prefer destinations on the opposite side of the bend.

            Same-side wedge branches are still allowed, but they should not
            win first-visit BFS over a real cross-bend traversal.
            """
            ordered = []
            for nbr2, bi2, fi2 in adjacency[wedge_pi]:
                if nbr2 == src_pi:
                    continue
                ordered.append((
                    0 if _side_test(src_pi, nbr2, entry_fi) else 1,
                    nbr2,
                    bi2,
                    fi2,
                ))
            ordered.sort(key=lambda item: (item[0], item[1]))
            return [(nbr2, bi2, fi2) for _, nbr2, bi2, fi2 in ordered]

        piece_bend_sets = [None] * n
        piece_bend_sets[stationary_idx] = set()
        if _sp:
            # Piece-to-piece BFS: skip through wedges.
            # Per-crossing sign convention:
            #   source → wedge:  positive mi (stationary side)
            #   wedge  → dest:   -(mi+2)    (moving side)
            #   regular (no wedge): positive mi
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
                        # Entry crossing: source → wedge = positive
                        if piece_bend_sets[nbr] is None:
                            piece_bend_sets[nbr] = \
                                piece_bend_sets[cur] | (
                                    {bend_idx} if bend_idx
                                    is not None else set())
                            bfs_tree[nbr] = (
                                cur, {bi}, None)
                        if log:
                            _log_bending_bfs(
                                f"FreekiCAD: BFS p{cur} → "
                                f"wedge p{nbr} "
                                f"(entry={_crossing_label(bi)})"
                                f"\n")
                        for nbr2, bi2, fi2 in _ordered_wedge_neighbors(
                                cur, nbr, fi):
                            if piece_bend_sets[nbr2] is not None:
                                continue
                            if not _side_test(cur, nbr2, fi):
                                # Branched wedges can feed pieces that stay on
                                # the source side of the entry cut. Keep the
                                # wedge hop for parent/adjacency purposes, but
                                # do not record any bend crossing for those
                                # same-side pieces.
                                piece_bend_sets[nbr2] = \
                                    piece_bend_sets[cur].copy()
                                bfs_tree[nbr2] = (cur, set(), nbr)
                                if log:
                                    _log_bending_bfs(
                                        f"FreekiCAD: BFS "
                                        f"wedge same_side("
                                        f"p{cur}, p{nbr2}, "
                                        f"fi={fi}) "
                                        f"(entry="
                                        f"{_crossing_label(bi)}"
                                        f")\n")
                                queue.append(nbr2)
                                continue
                            bend_idx2 = _get_bend_idx(bi2)
                            # Exit crossing: wedge → dest = negative
                            sbi2 = -(bi2 + 2) if bi2 >= 0 \
                                else bi2
                            crossed = {bi}
                            crossed.add(sbi2)
                            piece_bend_sets[nbr2] = \
                                piece_bend_sets[cur] | (
                                    {bend_idx2} if bend_idx2
                                    is not None else set())
                            bfs_tree[nbr2] = (
                                cur, crossed, nbr)
                            # Also record exit crossing in wedge's
                            # own crossed set so strip_to_mi
                            # can find the mi for the wedge.
                            if nbr in bfs_tree:
                                bfs_tree[nbr][1].add(sbi2)
                            if log:
                                _log_bending_bfs(
                                    f"FreekiCAD: BFS   "
                                    f"p{cur} →[{_crossing_label(bi)}"
                                    f"]→ p{nbr}(W) →["
                                    f"{_crossing_label(sbi2)}]→ "
                                    f"p{nbr2}\n")
                            queue.append(nbr2)
                    else:
                        # Regular piece (no wedge) — positive mi.
                        if piece_bend_sets[nbr] is not None:
                            continue
                        if not _side_test(cur, nbr, fi):
                            if log:
                                _log_bending_bfs(
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
                            _log_bending_bfs(
                                f"FreekiCAD: BFS p{cur} →"
                                f"[{_crossing_label(bi)}]→ "
                                f"p{nbr}\n")
                        queue.append(nbr)

        else:
            # Non-wedge BFS uses the same normalized tuple shape as the
            # wedge-aware path so later Phase 3 code can consume one
            # structure regardless of whether strip pieces exist.
            bfs_tree = {stationary_idx: (None, set(), None)}
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
                            piece_bend_sets[cur] | (
                                {bend_idx} if bend_idx is not None
                                else set())
                        crossed = {bi}
                    else:
                        piece_bend_sets[nbr] = \
                            piece_bend_sets[cur].copy()
                        crossed = set()
                    bfs_tree[nbr] = (cur, crossed, None)
                    queue.append(nbr)

        if log:
            for pi in sorted(bfs_tree):
                parent, mis_crossed, wedge_pi = bfs_tree[pi]
                if mis_crossed:
                    labels = ", ".join(
                        _crossing_label(m)
                        for m in sorted(mis_crossed))
                    raw = sorted(mis_crossed)
                else:
                    labels = "-"
                    raw = []
                _log_bending_bfs(
                    f"FreekiCAD: bfs_tree[{pi}] ="
                    f" parent={parent}"
                    f" mis_crossed=[{labels}]"
                    f" raw={raw}"
                    f" wedge={wedge_pi}\n")

        classified_count = sum(
            1 for bends in piece_bend_sets if bends is not None)
        crossed_count = sum(
            1 for bends in piece_bend_sets if bends)
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: BFS summary"
            f" classified={classified_count}/{n}"
            f" crossed={crossed_count}/{n}"
            f" root={stationary_idx}\n")

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
        if self._is_component_move_blocked(obj):
            self._pending_move = None
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Dropping pending move for '{component}' "
                "(component sync blocked)\n")
            return
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
        self._ensure_rebend_timer_state()
        self._ensure_component_sync_state()

    def _ensure_rebend_timer_state(self):
        if not hasattr(self, '_rebend_timer'):
            self._rebend_timer = None
        if not hasattr(self, '_rebend_target'):
            self._rebend_target = None

    def _get_wedge_mode(self, obj):
        return self._normalize_wedge_mode_value(
            getattr(obj, "WedgeMode", None))

    def _normalize_wedge_mode_value(self, mode_value):
        if mode_value in self._WEDGE_MODE_OPTIONS:
            return mode_value
        return "Smooth"

    def _get_wedge_target_edge_splits(self, mode_value):
        self._normalize_wedge_mode_value(mode_value)
        return 8

    def _get_rebend_debounce_ms(self, obj=None):
        return max(0, int(self._REBEND_DEBOUNCE_MS))

    # Properties that belong to this class (group "LinkedFile").
    # Anything in this group not listed here is obsolete and removed
    # on load by _ensure_properties().
    _KNOWN_PROPERTIES = {
        "FileName", "AutoReload", "EnableBending",
        "BuildDebugObjects", "DebugBoard", "WedgeMode",
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
        mode_value = self._normalize_wedge_mode_value(
            getattr(obj, 'WedgeMode', None))

        if not hasattr(obj, 'WedgeMode'):
            obj.addProperty(
                "App::PropertyEnumeration", "WedgeMode", "LinkedFile",
                "Wedge rendering mode: Smooth or Wireframe")
        obj.WedgeMode = list(self._WEDGE_MODE_OPTIONS)
        obj.WedgeMode = mode_value
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
