import os
import sys
import re
import sexpr

VERSION = "5.3"

WORKSPACE_SUFFIX = ".kkkk"
PNL_SUFFIX = ".kikit_pnl"
PCB_SUFFIX = ".kicad_pcb"
SCH_SUFFIX = ".kicad_sch"
STEP_SUFFIX = ".step"

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath("resources")

    return os.path.join(base_path, relative_path)

def indexOf(list, item):
    try:
        return list.index(item) + 1
    except ValueError:
        return -1

def relpath(path, base, allow_outside=False):
    try:
        path = os.path.abspath(path)
    except:
        pass
    try:
        relpath = os.path.relpath(path, base)
        if not allow_outside and relpath.startswith(".."):
            return path
        return relpath
    except ValueError:
        return path
