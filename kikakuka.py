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
    elif len(inputs) == 2 and (is_gerber_dir(inputs[0]) or is_gerber_zip(inputs[0]) or is_gerber_file(inputs[0])) and inputs[1].endswith(PCB_SUFFIX):
        errors = convert_to_kicad(inputs[0], inputs[1], required_edge_cuts=False)
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
