import os
import sys

WORKSPACE_SUFFIX = ".kkkk"
PNL_SUFFIX = ".kikit_pnl"
PCB_SUFFIX = ".kicad_pcb"

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath("resources")

    return os.path.join(base_path, relative_path)
