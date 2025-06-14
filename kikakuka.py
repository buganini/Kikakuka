import sys
from differ import *
from workspace import *
from panelizer import *

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
    else:
        ui = PanelizerUI()
        for boardfile in inputs:
            if boardfile.endswith(PCB_SUFFIX):
                ui._addPCB(PCB(boardfile))

        ui.build()
        ui.run()
else:
    if "PANELIZER" in os.environ:
        ui = PanelizerUI()
        ui.run()
    else:
        ui = MainUI()
        ui.run()
