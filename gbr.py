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

def find_edge_cuts(path):
    if is_gerber_dir(path):
        filenames = os.listdir(path)
    elif is_gerber_zip(path):
        with zipfile.ZipFile(path) as z:
            filenames = z.namelist()
    else:
        raise ValueError(f"Invalid path: {path}")
    for fn in filenames:
        if "EdgeCut" in fn:
            return fn
        if os.path.splitext(fn)[1].lower() in (".gm1", ".gm3", ".gko"):
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
        print(p.__class__.__name__, p.__dict__)
        # print(dir(p))

        if isinstance(p, gerber.primitives.Arc):
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
                print(f"Line Start: {p.start}, End: {p.end}, Aperture: {p.aperture}")

def convert_to_kicad(input, output):
    edge_cuts_file = find_edge_cuts(input)
    if edge_cuts_file is None:
        raise ValueError(f"Edge cuts not found in {input}")
    edge_cuts_data = read_gbr_file(input, edge_cuts_file)
    gbr = gerber.loads(edge_cuts_data)

    board = pcbnew.BOARD()

    populate_kicad(board, gbr, pcbnew.Edge_Cuts)

    board.Save(output)

if __name__ == "__main__":
    import sys
    convert_to_kicad(sys.argv[1], sys.argv[2])