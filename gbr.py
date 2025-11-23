import os
import zipfile
import gerber
import pcbnew
import math

def is_gerber_file(filename):
    if filename.lower().endswith(".gbr"):
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

def read_gbr_file(path, filename):
    if is_gerber_dir(path):
        return open(os.path.join(path, filename), "r").read()
    if is_gerber_zip(path):
        with zipfile.ZipFile(path) as z:
            return z.open(filename).read()
    return None

def populate_kicad(board, gbr, layer):
    for p in gbr.primitives:
        if isinstance(p, gerber.primitives.Arc):
            # print(p.__class__.__name__, p.__dict__)
            # print(dir(p))

            arc = pcbnew.PCB_SHAPE()
            arc.SetShape(pcbnew.SHAPE_T_ARC)

            arc.SetStart(pcbnew.VECTOR2I(
                pcbnew.FromMM(p.start[0]),
                pcbnew.FromMM(-p.start[1])
            ))
            arc.SetCenter(pcbnew.VECTOR2I(
                pcbnew.FromMM(p.center[0]),
                pcbnew.FromMM(-p.center[1])
            ))
            arc.SetArcAngleAndEnd(pcbnew.EDA_ANGLE(p.sweep_angle, pcbnew.RADIANS_T))

            arc.SetLayer(layer)
            arc.SetWidth(pcbnew.FromMM(p.aperture.radius * 2))

            board.Add(arc)
        elif isinstance(p, gerber.primitives.Line):
            if isinstance(p.aperture, gerber.primitives.Circle):
                # print(p.__class__.__name__, p.__dict__)
                # print(dir(p))

                line = pcbnew.PCB_SHAPE()

                line.SetShape(pcbnew.SHAPE_T_SEGMENT)

                line.SetStart(pcbnew.VECTOR2I(
                    pcbnew.FromMM(p.start[0]),
                    pcbnew.FromMM(-p.start[1])
                ))

                line.SetEnd(pcbnew.VECTOR2I(
                    pcbnew.FromMM(p.end[0]),
                    pcbnew.FromMM(-p.end[1])
                ))

                line.SetLayer(layer)
                line.SetWidth(pcbnew.FromMM(p.aperture.radius * 2))

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
                pcbnew.FromMM(p.position[0] - p.width / 2),
                pcbnew.FromMM(-p.position[1] - p.height / 2)
            ))
            rectangle.SetEnd(pcbnew.VECTOR2I(
                pcbnew.FromMM(p.position[0] + p.width / 2),
                pcbnew.FromMM(-p.position[1] + p.height / 2)
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
                pcbnew.FromMM(p.position[0]),
                pcbnew.FromMM(-p.position[1])
            ))
            circle.SetRadius(pcbnew.FromMM(p.radius))
            circle.SetLayer(layer)
            circle.SetFilled(True)
            board.Add(circle)
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
                    pcbnew.FromMM(line.start[0]),
                    pcbnew.FromMM(-line.start[1]),
                    outline
                )

            poly.SetFilled(True)
            board.Add(poly)
        else:
            print(p.__class__.__name__, p.__dict__)
            # print(dir(p))

def convert_to_kicad(input, output, outline_only=False):
    filenames = list_gerber_files(input)
    edge_cuts_file = find_edge_cuts(filenames)
    if edge_cuts_file is None:
        raise ValueError(f"Edge cuts not found in {input}")
    filenames.remove(edge_cuts_file)
    edge_cuts_data = read_gbr_file(input, edge_cuts_file)
    gbr = gerber.loads(edge_cuts_data)

    board = pcbnew.BOARD()
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

        print(filenames)

    board.Save(output)

if __name__ == "__main__":
    import sys
    convert_to_kicad(sys.argv[1], sys.argv[2])