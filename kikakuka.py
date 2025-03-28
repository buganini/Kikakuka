import sys
from workspace import *
from panelizer import *

inputs = sys.argv[1:]
if inputs:
    if inputs[0].endswith(WORKSPACE_SUFFIX):
        ui = WorkspaceUI(inputs[0])
        ui.run()
    elif inputs[0].endswith(PNL_SUFFIX):
        ui = PanelizerUI()
        ui.load(None, inputs[0])
        if len(inputs) > 1:
            ui.build(export=inputs[1])
            sys.exit(0)
        else:
            ui.autoScale()
            ui.build()
            ui.run()
    else:
        ui = PanelizerUI()
        for boardfile in inputs:
            if boardfile.endswith(PCB_SUFFIX):
                ui._addPCB(PCB(boardfile))

        ui.autoScale()
        ui.build()
        ui.run()
else:
    if "PANELIZER" in os.environ:
        ui = PanelizerUI()
        ui.run()
    else:
        ui = WorkspaceUI()
        ui.run()
