import os
import zipfile
from pcb_tools import gerber
import pcbnew
import math
import kikit.common

def is_gerber_file(filename):
    if os.path.splitext(filename)[1].lower() in (".gbr", ".gm1", ".gm3", ".gko", ".g1"):
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
    return None

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
    return None

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
    return None

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
    return None

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
    return None

def find_paste_top(filenames):
    for fn in filenames:
        if "F_Paste" in fn: # KiCAD
            return fn
        if "F.Paste" in fn: # KiCAD
            return fn
        if fn.lower().endswith(".gtp"): # Altium
            return fn
    return None

def find_paste_bottom(filenames):
    for fn in filenames:
        if "B_Paste" in fn: # KiCAD
            return fn
        if "B.Paste" in fn: # KiCAD
            return fn
        if fn.lower().endswith(".gbp"): # Altium
            return fn
    return None

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
    return None

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
    return None

def find_PTH(filenames):
    for fn in filenames:
        if fn.lower().endswith(".pdf"):
            continue
        if "PTH" in fn:
            return fn
    return None

def find_NPTH(filenames):
    for fn in filenames:
        if fn.lower().endswith(".pdf"):
            continue
        if "NPTH" in fn:
            return fn
    return None

def read_gbr_file(path, filename):
    if is_gerber_dir(path):
        return open(os.path.join(path, filename), "r").read()
    if is_gerber_zip(path):
        with zipfile.ZipFile(path) as zf:
            path = zipfile.Path(zf, at=filename)
            return path.read_text(encoding='UTF-8')
    return None

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

    for p in gbr.primitives:
        populate_kicad_by_primitive(board, p, fromUnit, layer, errors)

def populate_kicad_by_primitive(board, primitive, fromUnit, layer, errors):
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

        if layer: # plated
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

def convert_to_kicad(input, output, required_edge_cuts=True, outline_only=False):
    filenames = list_gerber_files(input)
    # print("filenames", filenames)

    edge_cuts_file = find_edge_cuts(filenames)
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
        cu_top_file = find_cu_top(filenames)
        if cu_top_file is not None:
            print("cu_top_file", cu_top_file)
            filenames.remove(cu_top_file)
            cu_top_data = read_gbr_file(input, cu_top_file)
            gbr = gerber.loads(cu_top_data)
            populate_kicad(board, gbr, pcbnew.F_Cu, errors)

        found_inner_layer = 0
        inner_layers = [pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.In3_Cu, pcbnew.In4_Cu, pcbnew.In5_Cu, pcbnew.In6_Cu, pcbnew.In7_Cu, pcbnew.In8_Cu, pcbnew.In9_Cu, pcbnew.In10_Cu, pcbnew.In11_Cu, pcbnew.In12_Cu, pcbnew.In13_Cu, pcbnew.In14_Cu, pcbnew.In15_Cu, pcbnew.In16_Cu, pcbnew.In17_Cu, pcbnew.In18_Cu, pcbnew.In19_Cu, pcbnew.In20_Cu, pcbnew.In21_Cu, pcbnew.In22_Cu, pcbnew.In23_Cu, pcbnew.In24_Cu, pcbnew.In25_Cu, pcbnew.In26_Cu, pcbnew.In27_Cu, pcbnew.In28_Cu, pcbnew.In29_Cu, pcbnew.In30_Cu]
        for i in range(len(inner_layers)):
            cu_inner_file = find_cu_inner(filenames, i+1)
            if cu_inner_file is not None:
                print("cu_inner_file[{}]".format(i+1), cu_inner_file)
                filenames.remove(cu_inner_file)
                cu_inner_data = read_gbr_file(input, cu_inner_file)
                gbr = gerber.loads(cu_inner_data)
                populate_kicad(board, gbr, inner_layers[found_inner_layer], errors)
                found_inner_layer += 1

        cu_bottom_file = find_cu_bottom(filenames)
        if cu_bottom_file is not None:
            print("cu_bottom_file", cu_bottom_file)
            filenames.remove(cu_bottom_file)
            cu_bottom_data = read_gbr_file(input, cu_bottom_file)
            gbr = gerber.loads(cu_bottom_data)
            populate_kicad(board, gbr, pcbnew.B_Cu, errors)

        board.SetCopperLayerCount(found_inner_layer + 2)

        silk_top_file = find_silk_top(filenames)
        if silk_top_file is not None:
            print("silk_top_file", silk_top_file)
            filenames.remove(silk_top_file)
            silk_top_data = read_gbr_file(input, silk_top_file)
            gbr = gerber.loads(silk_top_data)
            populate_kicad(board, gbr, pcbnew.F_SilkS, errors)

        silk_bottom_file = find_silk_bottom(filenames)
        if silk_bottom_file is not None:
            print("silk_bottom_file", silk_bottom_file)
            filenames.remove(silk_bottom_file)
            silk_bottom_data = read_gbr_file(input, silk_bottom_file)
            gbr = gerber.loads(silk_bottom_data)
            populate_kicad(board, gbr, pcbnew.B_SilkS, errors)

        mask_top_file = find_mask_top(filenames)
        if mask_top_file is not None:
            print("mask_top_file", mask_top_file)
            filenames.remove(mask_top_file)
            mask_top_data = read_gbr_file(input, mask_top_file)
            gbr = gerber.loads(mask_top_data)
            populate_kicad(board, gbr, pcbnew.F_Mask, errors)

        mask_bottom_file = find_mask_bottom(filenames)
        if mask_bottom_file is not None:
            print("mask_bottom_file", mask_bottom_file)
            filenames.remove(mask_bottom_file)
            mask_bottom_data = read_gbr_file(input, mask_bottom_file)
            gbr = gerber.loads(mask_bottom_data)
            populate_kicad(board, gbr, pcbnew.B_Mask, errors)

        paste_top_file = find_paste_top(filenames)
        if paste_top_file is not None:
            print("paste_top_file", paste_top_file)
            filenames.remove(paste_top_file)
            paste_top_data = read_gbr_file(input, paste_top_file)
            gbr = gerber.loads(paste_top_data)
            populate_kicad(board, gbr, pcbnew.F_Paste, errors)

        paste_bottom_file = find_paste_bottom(filenames)
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

        print(filenames)

    board.Save(output)

    return errors

if __name__ == "__main__":
    import sys
    errors = convert_to_kicad(sys.argv[1], sys.argv[2], required_edge_cuts=False)
    if errors:
        print("Errors:")
        for error in errors:
            print(error)
