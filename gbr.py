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
        if "EdgeCut" in fn:
            return fn
        if os.path.splitext(fn)[1].lower() in (".gm1", ".gm3", ".gko"):
            return fn
    return None

def find_silk_top(filenames):
    for fn in filenames:
        if "SilkTop" in fn:
            return fn
    return None

def find_silk_bottom(filenames):
    for fn in filenames:
        if "SilkBottom" in fn:
            return fn
    return None

def find_cu_top(filenames):
    for fn in filenames:
        if "CuTop" in fn:
            return fn
    return None

def find_cu_bottom(filenames):
    for fn in filenames:
        if "CuBottom" in fn:
            return fn
    return None

def find_mask_top(filenames):
    for fn in filenames:
        if "MaskTop" in fn:
            return fn
    return None

def find_mask_bottom(filenames):
    for fn in filenames:
        if "MaskBottom" in fn:
            return fn
    return None

def find_PTH(filenames):
    for fn in filenames:
        if "PTH" in fn and not fn.lower().endswith(".pdf"):
            return fn
    return None

def find_NPTH(filenames):
    for fn in filenames:
        if "NPTH" in fn and not fn.lower().endswith(".pdf"):
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

def populate_kicad(board, gbr, layer, optimize=True):
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
        if isinstance(p, gerber.primitives.Arc):
            # print(p.__class__.__name__, p.__dict__)
            # print(dir(p))

            arc = pcbnew.PCB_SHAPE()
            arc.SetShape(pcbnew.SHAPE_T_ARC)

            arc.SetStart(pcbnew.VECTOR2I(
                fromUnit(p.start[0]),
                -fromUnit(p.start[1])
            ))
            arc.SetCenter(pcbnew.VECTOR2I(
                fromUnit(p.center[0]),
                -fromUnit(p.center[1])
            ))
            arc.SetArcAngleAndEnd(pcbnew.EDA_ANGLE(p.sweep_angle, pcbnew.RADIANS_T))

            arc.SetLayer(layer)
            arc.SetWidth(fromUnit(p.aperture.radius * 2))

            board.Add(arc)
        elif isinstance(p, gerber.primitives.Line):
            if isinstance(p.aperture, gerber.primitives.Circle):
                # print(p.__class__.__name__, p.__dict__)
                # print(dir(p))

                line = pcbnew.PCB_SHAPE()

                line.SetShape(pcbnew.SHAPE_T_SEGMENT)

                line.SetStart(pcbnew.VECTOR2I(
                    fromUnit(p.start[0]),
                    -fromUnit(p.start[1])
                ))

                line.SetEnd(pcbnew.VECTOR2I(
                    fromUnit(p.end[0]),
                    -fromUnit(p.end[1])
                ))

                line.SetLayer(layer)
                line.SetWidth(fromUnit(p.aperture.radius * 2))

                board.Add(line)
            else:
                print(p.__class__.__name__, p.__dict__)
                print(dir(p))
        elif isinstance(p, gerber.primitives.Rectangle):
            # print(p.__class__.__name__, p.__dict__)
            # print(dir(p))

            rectangle = pcbnew.PCB_SHAPE()
            rectangle.SetShape(pcbnew.SHAPE_T_RECTANGLE)

            rectangle.SetStart(pcbnew.VECTOR2I(
                fromUnit(p.position[0] - p.width / 2),
                -fromUnit(p.position[1] - p.height / 2)
            ))
            rectangle.SetEnd(pcbnew.VECTOR2I(
                fromUnit(p.position[0] + p.width / 2),
                -fromUnit(p.position[1] + p.height / 2)
            ))

            rectangle.SetLayer(layer)
            rectangle.SetFilled(True)
            board.Add(rectangle)
        elif isinstance(p, gerber.primitives.Circle):
            # print(p.__class__.__name__, p.__dict__)
            # print(dir(p))

            circle = pcbnew.PCB_SHAPE()
            circle.SetShape(pcbnew.SHAPE_T_CIRCLE)
            circle.SetCenter(pcbnew.VECTOR2I(
                fromUnit(p.position[0]),
                -fromUnit(p.position[1])
            ))
            circle.SetRadius(fromUnit(p.radius))
            circle.SetLayer(layer)
            circle.SetFilled(True)
            board.Add(circle)
        elif isinstance(p, gerber.primitives.AMGroup):
            print(p.__class__.__name__, p.__dict__)
            print(dir(p))

            r = None
            circles = []
            circles_center = set()
            outlines_center = set()
            for primitive in p.primitives:
                if isinstance(primitive, gerber.primitives.Circle):
                    circles.append(primitive)
                    circles_center.add(primitive.position)
                    r = primitive.radius

                    # """ BEGIN OF DEBUG """
                    # circle = pcbnew.PCB_SHAPE()
                    # circle.SetShape(pcbnew.SHAPE_T_CIRCLE)
                    # circle.SetCenter(pcbnew.VECTOR2I(
                    #     fromUnit(primitive.position[0]),
                    #     -fromUnit(primitive.position[1])
                    # ))
                    # circle.SetRadius(fromUnit(primitive.radius))
                    # circle.SetLayer(layer)
                    # circle.SetFilled(False)
                    # board.Add(circle)
                    # """ END OF DEBUG """
                elif isinstance(primitive, gerber.primitives.Outline):
                    for outline_line in primitive.primitives:
                        outlines_center.add(outline_line.start)
                        outlines_center.add(outline_line.end)

                        # """ BEGIN OF DEBUG """
                        # line = pcbnew.PCB_SHAPE()
                        # line.SetShape(pcbnew.SHAPE_T_SEGMENT)
                        # line.SetStart(pcbnew.VECTOR2I(
                        #     fromUnit(outline_line.start[0]),
                        #     -fromUnit(outline_line.start[1])
                        # ))
                        # line.SetEnd(pcbnew.VECTOR2I(
                        #     fromUnit(outline_line.end[0]),
                        #     -fromUnit(outline_line.end[1])
                        # ))
                        # line.SetLayer(layer)
                        # line.SetWidth(fromUnit(0.01))
                        # board.Add(line)
                        # """ END OF DEBUG """
            if outlines_center.issuperset(circles_center):
                rectangle = pcbnew.PCB_SHAPE()
                rectangle.SetShape(pcbnew.SHAPE_T_RECTANGLE)

                rectangle.SetStart(pcbnew.VECTOR2I(
                    fromUnit(circles[0].position[0]),
                    -fromUnit(circles[0].position[1])
                ))
                rectangle.SetEnd(pcbnew.VECTOR2I(
                    fromUnit(circles[2].position[0]),
                    -fromUnit(circles[2].position[1])
                ))

                rectangle.SetLayer(layer)
                rectangle.SetFilled(True)
                rectangle.SetWidth(fromUnit(r * 2))
                board.Add(rectangle)
            else:
                print("Unhandled case: not a rounded rectangle")

                for primitive in p.primitives:
                    if isinstance(primitive, gerber.primitives.Circle):
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
                    elif isinstance(primitive, gerber.primitives.Outline):
                        for outline_line in primitive.primitives:
                            line = pcbnew.PCB_SHAPE()
                            line.SetShape(pcbnew.SHAPE_T_SEGMENT)
                            line.SetStart(pcbnew.VECTOR2I(
                                fromUnit(outline_line.start[0]),
                                -fromUnit(outline_line.start[1])
                            ))
                            line.SetEnd(pcbnew.VECTOR2I(
                                fromUnit(outline_line.end[0]),
                                -fromUnit(outline_line.end[1])
                            ))
                            line.SetLayer(layer)
                            line.SetWidth(fromUnit(outline_line.aperture.radius * 2))
                            board.Add(line)
        elif isinstance(p, gerber.primitives.Region):
            # print(p.__class__.__name__, p.__dict__)
            # print(dir(p))

            poly = pcbnew.PCB_SHAPE()
            poly.SetShape(pcbnew.SHAPE_T_POLY)

            poly.SetLayer(layer)

            poly_set = poly.GetPolyShape()
            outline = poly_set.NewOutline()

            for line in p.primitives:
                poly_set.Append(
                    fromUnit(line.start[0]),
                    -fromUnit(line.start[1]),
                    outline
                )

            poly.SetFilled(True)
            board.Add(poly)
        elif isinstance(p, gerber.primitives.Drill):
            # print(p.__class__.__name__, p.__dict__)
            # print(dir(p))

            if layer: # plated
                via = pcbnew.PCB_VIA(board)

                via.SetPosition(pcbnew.VECTOR2I(
                    fromUnit(p.position[0]),
                    -fromUnit(p.position[1])
                ))
                via.SetWidth(fromUnit(p.diameter))
                via.SetDrill(fromUnit(p.diameter))
                via.SetViaType(pcbnew.VIATYPE_THROUGH)

                board.Add(via)
            else:
                footprint = pcbnew.FootprintLoad(kikit.common.KIKIT_LIB, "NPTH")
                footprint.SetPosition(pcbnew.VECTOR2I(
                    fromUnit(p.position[0]),
                    -fromUnit(p.position[1])
                ))
                for pad in footprint.Pads():
                    pad.SetDrillSizeX(fromUnit(p.diameter))
                    pad.SetDrillSizeY(fromUnit(p.diameter))
                    pad.SetSizeX(fromUnit(p.diameter))
                    pad.SetSizeY(fromUnit(p.diameter))
                board.Add(footprint)
        else:
            print(p.__class__.__name__, p.__dict__)
            # print(dir(p))

def convert_to_kicad(input, output, required_edge_cuts=True, outline_only=False):
    filenames = list_gerber_files(input)
    print("filenames", filenames)


    edge_cuts_file = find_edge_cuts(filenames)
    if edge_cuts_file is None and required_edge_cuts:
        raise ValueError(f"Edge cuts not found in {input}")

    board = pcbnew.BOARD()

    if edge_cuts_file:
        filenames.remove(edge_cuts_file)
        edge_cuts_data = read_gbr_file(input, edge_cuts_file)
        gbr = gerber.loads(edge_cuts_data)

        populate_kicad(board, gbr, pcbnew.Edge_Cuts)

    if not outline_only:
        cu_top_file = find_cu_top(filenames)
        if cu_top_file is not None:
            filenames.remove(cu_top_file)
            cu_top_data = read_gbr_file(input, cu_top_file)
            gbr = gerber.loads(cu_top_data)
            populate_kicad(board, gbr, pcbnew.F_Cu)

        cu_bottom_file = find_cu_bottom(filenames)
        if cu_bottom_file is not None:
            filenames.remove(cu_bottom_file)
            cu_bottom_data = read_gbr_file(input, cu_bottom_file)
            gbr = gerber.loads(cu_bottom_data)
            populate_kicad(board, gbr, pcbnew.B_Cu)

        silk_top_file = find_silk_top(filenames)
        if silk_top_file is not None:
            filenames.remove(silk_top_file)
            silk_top_data = read_gbr_file(input, silk_top_file)
            gbr = gerber.loads(silk_top_data)
            populate_kicad(board, gbr, pcbnew.F_SilkS)

        silk_bottom_file = find_silk_bottom(filenames)
        if silk_bottom_file is not None:
            filenames.remove(silk_bottom_file)
            silk_bottom_data = read_gbr_file(input, silk_bottom_file)
            gbr = gerber.loads(silk_bottom_data)
            populate_kicad(board, gbr, pcbnew.B_SilkS)

        mask_top_file = find_mask_top(filenames)
        if mask_top_file is not None:
            filenames.remove(mask_top_file)
            mask_top_data = read_gbr_file(input, mask_top_file)
            gbr = gerber.loads(mask_top_data)
            populate_kicad(board, gbr, pcbnew.F_Mask)

        mask_bottom_file = find_mask_bottom(filenames)
        if mask_bottom_file is not None:
            filenames.remove(mask_bottom_file)
            mask_bottom_data = read_gbr_file(input, mask_bottom_file)
            gbr = gerber.loads(mask_bottom_data)
            populate_kicad(board, gbr, pcbnew.B_Mask)

        pth_file = find_PTH(filenames)
        if pth_file is not None:
            filenames.remove(pth_file)
            pth_data = read_gbr_file(input, pth_file)
            gbr = gerber.loads(pth_data)
            populate_kicad(board, gbr, True)

        npth_file = find_NPTH(filenames)
        if npth_file is not None:
            filenames.remove(npth_file)
            npth_data = read_gbr_file(input, npth_file)
            gbr = gerber.loads(npth_data)
            populate_kicad(board, gbr, False)

        print(filenames)

    board.Save(output)

if __name__ == "__main__":
    import sys
    convert_to_kicad(sys.argv[1], sys.argv[2], required_edge_cuts=False)