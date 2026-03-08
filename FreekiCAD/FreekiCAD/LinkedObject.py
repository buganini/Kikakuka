import os
import math
import FreeCAD
import Part


DEFAULT_PCB_THICKNESS = 1.6  # mm fallback


def _vec(x_nm, y_nm, z=0):
    """Convert KiCad nanometres to FreeCAD mm, flipping Y."""
    return FreeCAD.Vector(x_nm / 1e6, -y_nm / 1e6, z)


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


def _load_step(step_path, doc):
    """Load a STEP file as a single compound shape with colors.
    Uses ImportGui in a temporary document to get both shape and
    per-face DiffuseColor without polluting the main document.
    Falls back to Part.read() (no colors) on failure.
    Returns a list with one (shape, colors_or_None) entry, or [] on failure."""
    # Try ImportGui in a temporary document for shape + colors
    try:
        import ImportGui
        tmp_doc = FreeCAD.newDocument("__FreekiCAD_tmp__")
        ImportGui.insert(step_path, tmp_doc.Name)
        tmp_doc.recompute()
        # Find the top-level object (compound parent or single part)
        top = None
        for obj in tmp_doc.Objects:
            if hasattr(obj, 'Shape') and not obj.Shape.isNull():
                if top is None:
                    top = obj
                if hasattr(obj, 'Group') and obj.Group:
                    top = obj
                    break
        if top and hasattr(top, 'Shape') and not top.Shape.isNull():
            shape = top.Shape.copy()
            colors = None
            if hasattr(top, 'ViewObject') and top.ViewObject:
                try:
                    colors = list(top.ViewObject.DiffuseColor)
                except Exception:
                    try:
                        colors = [top.ViewObject.ShapeColor]
                    except Exception:
                        pass
            FreeCAD.closeDocument(tmp_doc.Name)
            return [(shape, colors)]
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD:   ImportGui produced no shape for {step_path}\n"
        )
        FreeCAD.closeDocument(tmp_doc.Name)
    except Exception as ex:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD:   ImportGui failed for {step_path}: {ex}\n"
        )
        try:
            FreeCAD.closeDocument("__FreekiCAD_tmp__")
        except Exception:
            pass

    # Fallback to Part.read (no colors)
    try:
        shape = Part.read(step_path)
        if shape and not shape.isNull():
            return [(shape, None)]
    except Exception as ex:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD:   Could not read STEP {step_path}: {ex}\n"
        )
    return []


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


def load_board(filepath):
    """Connect to a running KiCad instance via kipy and build the board
    solid + component 3D models.  Returns (board_shape, components, color)
    where components is a list of (label, shape, color) and color is (r,g,b) or None."""
    try:
        from kipy.kicad import KiCad
        from kipy.proto.board.board_types_pb2 import BoardLayer
        from kipy.board_types import (
            BoardSegment, BoardCircle, BoardArc, BoardRectangle,
            to_concrete_board_shape,
        )

        # Resolve the KiCad IPC socket for this file via the
        # Kikakuka workspace manager (ZMQ).  The workspace manager
        # will start KiCad automatically if needed (async pending).
        from FreekiCAD.zmq_bus import resolve_kicad_socket
        socket_path = resolve_kicad_socket(filepath)
        if socket_path is None:
            FreeCAD.Console.PrintError(
                "FreekiCAD: Could not resolve KiCad socket for "
                f"{filepath}. Is the workspace manager running?\n"
            )
            return None, [], None
        kicad = KiCad(socket_path=f"ipc://{socket_path}")
        board = kicad.get_board()

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
                    p1 = _vec(concrete.start.x, concrete.start.y)
                    p2 = _vec(concrete.end.x, concrete.end.y)
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
            except Exception as ex:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD:   shape exception: {ex}\n"
                )
                continue

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Edge.Cuts edges collected: {len(edges)}\n"
        )

        board_solid = None
        if edges:
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

            wire = Part.Wire(Part.sortEdges(edges)[0])
            face = Part.Face(wire)
            board_solid = face.extrude(FreeCAD.Vector(0, 0, thickness))
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Board solid created ({thickness}mm thick)\n"
            )

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

        # --- 3D models for footprints ---
        components = []
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

                        fc_doc = FreeCAD.ActiveDocument
                        parts = _load_step(model_path, fc_doc)
                        if not parts:
                            FreeCAD.Console.PrintWarning(
                                f"FreekiCAD:   {ref}: STEP load returned "
                                f"no shapes: {model_path}\n"
                            )
                            continue

                        FreeCAD.Console.PrintMessage(
                            f"FreekiCAD:   {ref}: loaded "
                            f"{os.path.basename(model_path)} "
                            f"({len(parts)} parts)"
                            f"  offset=({model.offset.x}, {model.offset.y}, "
                            f"{model.offset.z})"
                            f"  rot=({model.rotation.x}, {model.rotation.y}, "
                            f"{model.rotation.z})"
                            f"  scale=({model.scale.x}, {model.scale.y}, "
                            f"{model.scale.z})"
                            f"  fp_angle={fp_angle} is_back={is_back}\n"
                        )

                        for part_shape, part_colors in parts:
                            # Apply model scale
                            sx = model.scale.x if model.scale.x != 0 else 1.0
                            sy = model.scale.y if model.scale.y != 0 else 1.0
                            sz = model.scale.z if model.scale.z != 0 else 1.0
                            if sx != 1.0 or sy != 1.0 or sz != 1.0:
                                mat = FreeCAD.Matrix()
                                mat.scale(sx, sy, sz)
                                part_shape = part_shape.transformGeometry(mat)

                            # Apply model rotation (degrees, X then Y then Z)
                            origin = FreeCAD.Vector(0, 0, 0)
                            if model.rotation.x != 0:
                                part_shape.rotate(
                                    origin, FreeCAD.Vector(1, 0, 0),
                                    model.rotation.x)
                            if model.rotation.y != 0:
                                part_shape.rotate(
                                    origin, FreeCAD.Vector(0, 1, 0),
                                    model.rotation.y)
                            if model.rotation.z != 0:
                                part_shape.rotate(
                                    origin, FreeCAD.Vector(0, 0, 1),
                                    model.rotation.z)

                            # Apply model offset (mm in KiCad)
                            part_shape.translate(FreeCAD.Vector(
                                model.offset.x,
                                model.offset.y,
                                model.offset.z,
                            ))

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

                            components.append(
                                (ref, part_shape, part_colors))

                    except Exception as ex:
                        FreeCAD.Console.PrintWarning(
                            f"FreekiCAD:   {ref}: model error: {ex}\n"
                        )
                        continue

            except Exception as ex:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD:   {ref}: footprint error: {ex}\n"
                )

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Loaded {len(components)} component models\n"
        )
        return board_solid, components, board_color

    except Exception as e:
        import traceback
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Could not load board via kipy: {e}\n"
        )
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Traceback:\n{traceback.format_exc()}\n"
        )
    return None, [], None


def _fit_view(obj):
    """Fit the 3D viewport to show the given object."""
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
        FreeCADGui.SendMsgToActiveView("ViewFit")
    except Exception:
        pass


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
        obj.AutoReload = False
        obj.Proxy = self
        self.Type = "LinkedObject"
        self._file_mtime = None
        self._board_color = None

    def onChanged(self, obj, prop):
        if prop == "FileName":
            if obj.FileName:
                obj.Label = os.path.splitext(os.path.basename(obj.FileName))[0]
            self._file_mtime = None
            self.execute(obj)
        elif prop == "AutoReload":
            if hasattr(obj, "ViewObject") and obj.ViewObject and hasattr(obj.ViewObject, "Proxy"):
                vp = obj.ViewObject.Proxy
                if hasattr(vp, '_auto_reload_timer'):
                    if obj.AutoReload:
                        vp._auto_reload_timer.start(2000)
                    else:
                        vp._auto_reload_timer.stop()

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

    def execute(self, obj):
        """Load board data from KiCad via kipy, or fall back to a dummy cube."""
        self._board_color = None
        if not obj.FileName:
            return

        board_solid, components, board_color = load_board(obj.FileName)
        self._board_color = board_color

        # Record file modification time
        try:
            self._file_mtime = os.path.getmtime(obj.FileName)
        except OSError:
            self._file_mtime = None

        # Remove old children
        self._remove_children(obj)

        doc = obj.Document

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

        # Add component objects as children
        if components:
            for label, comp_shape, comp_colors in components:
                comp_obj = doc.addObject("Part::Feature", label)
                comp_obj.Shape = comp_shape
                if comp_colors and hasattr(comp_obj, 'ViewObject') and comp_obj.ViewObject:
                    try:
                        comp_obj.ViewObject.DiffuseColor = comp_colors
                    except Exception:
                        try:
                            comp_obj.ViewObject.ShapeColor = comp_colors[0][:3]
                        except Exception:
                            pass
                obj.addObject(comp_obj)

    def _check_file_changed(self, obj):
        """Return True if the file's mtime has changed since last load."""
        if not obj.FileName:
            return False
        try:
            mtime = os.path.getmtime(obj.FileName)
        except OSError:
            return False
        if not hasattr(self, '_file_mtime') or self._file_mtime is None or mtime != self._file_mtime:
            return True
        return False

    def reload(self, obj):
        """Force reload from KiCad."""
        self._remove_children(obj)
        obj.touch()
        obj.Document.recompute()
        _fit_view(obj)

    def dumps(self):
        return {"Type": self.Type}

    def loads(self, state):
        if state:
            self.Type = state.get("Type", "LinkedObject")
        self._file_mtime = None


class LinkedObjectViewProvider:
    """ViewProvider for LinkedObject."""

    def __init__(self, vobj):
        vobj.addExtension("Gui::ViewProviderGeoFeatureGroupExtensionPython")
        vobj.Proxy = self

    def attach(self, vobj):
        self.Object = vobj.Object
        from PySide import QtCore
        self._auto_reload_timer = QtCore.QTimer()
        self._auto_reload_timer.timeout.connect(lambda: self._auto_reload(vobj))
        if hasattr(vobj.Object, "AutoReload") and vobj.Object.AutoReload:
            self._auto_reload_timer.start(2000)

    def _auto_reload(self, vobj):
        """Called by the timer — reload only if the file has changed."""
        obj = vobj.Object
        if hasattr(obj, "Proxy") and hasattr(obj.Proxy, "_check_file_changed"):
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
            obj.Proxy.reload(obj)

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
    _fit_view(obj)
    return obj
