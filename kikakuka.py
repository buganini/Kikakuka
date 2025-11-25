#!/usr/bin/env python3
import sys
from differ import *
from workspace import *
from panelizer import *
from gerber import *

inputs = sys.argv[1:]
if inputs:
    if inputs[0] == "--differ":
        ui = DifferUI(*inputs[1:])
        ui.run()
    elif inputs[0] == "--help" or inputs[0] == "-h":
        print("Usage:")
        print("  # Just open it")
        print(f"  {sys.argv[0]}")
        print()
        print("  # Start with PCB files")
        print(f"  {sys.argv[0]} a.kicad_pcb b.kicad_pcb...")
        print()
        print("  # Load file (.kkkk or .kikit_pnl)")
        print(f"  {sys.argv[0]} a.kikit_pnl")
        print()
        print("  # Headless export for panelization or build variants")
        print(f"  {sys.argv[0]} a.kikit_pnl out.kicad_pcb")
        print()
        print("  # Differ")
        print(f"  {sys.argv[0]} --differ a.kicad_sch b.kicad_sch")
        print()
        print("  # Gerber to KiCAD Conversion")
        print(f"  {sys.argv[0]} gerber.gbr out.kicad_pcb")
        print(f"  {sys.argv[0]} gerber.zip out.kicad_pcb")
        print(f"  {sys.argv[0]} gerber_folder out.kicad_pcb # BOM/CPL will be detected if they are in the folder")
        print()
        print("  # Gerber to KiCAD Conversion with extra BOM/CPL files")
        print(f"  {sys.argv[0]} gerber.zip out.kicad_pcb bom_or_cpl_1.csv bom_or_cpl_2.csv # BOM/CPL files are determined by filename regardless of argument order")
    elif inputs[0] == "--version" or inputs[0] == "-v":
        print(f"Kikakuka v{VERSION}")
        print(f"KiCad {pcbnew.Version()}")
        print(f"KiKit {kikit.__version__}")
        print(f"Shapely {shapely.__version__}")
        print(f"PUI {PUI.__version__} ({PUI_BACKEND})")
    elif all([input.endswith(WORKSPACE_SUFFIX) for input in inputs]):
        ui = MainUI(inputs)
        ui.run()
    elif inputs[0].endswith(PNL_SUFFIX):
        ui = PanelizerUI()
        ui.load(None, inputs[0])
        if len(inputs) > 1:
            ui.build(export=inputs[1])
            sys.exit(0)
        else:
            ui.build()
            ui.run()
    elif len(inputs) >= 2 and (is_gerber_dir(inputs[0]) or is_gerber_zip(inputs[0]) or is_gerber_file(inputs[0])) and inputs[1].endswith(PCB_SUFFIX):
        errors = convert_to_kicad(inputs[0], inputs[1], required_edge_cuts=False, extra_files=inputs[2:])
        if errors:
            print("Errors:")
            for error in errors:
                print(error)
    else:
        ui = PanelizerUI()
        for path in inputs:
            if path.endswith(PCB_SUFFIX) or is_gerber_dir(path) or is_gerber_zip(path) or is_gerber_file(path):
                ui._addPCB(PCB(ui, path))

        ui.build()
        ui.run()
else:
    if "PANELIZER" in os.environ:
        ui = PanelizerUI()
        ui.run()
    else:
        ui = MainUI()
        ui.run()
