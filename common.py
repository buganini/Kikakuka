import os
import sys
import re

VERSION = "4.2.1"

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

def relpath(path, base):
    try:
        path = os.path.abspath(path)
    except:
        pass
    try:
        relpath = os.path.relpath(path, base)
        if relpath.startswith(".."):
            return path
        return relpath
    except ValueError:
        return path

def findFiles(workspace, root, types=None):
    if types is None:
        types = [SCH_SUFFIX, PCB_SUFFIX, STEP_SUFFIX]
    for project in workspace["projects"]:
        project["files"] = []
        project["parent"] = None
        project["project_path"] = project["path"]
        if project["path"].endswith(".kikit_pnl"):
            continue
        if project["path"].endswith(".kicad_pro"):
            for ext in types:
                fpath = re.sub(r"\.kicad_pro$", ext, project["path"])
                if not os.path.isabs(fpath):
                    fpath = os.path.join(root, fpath)
                if os.path.exists(fpath):
                    project["files"].append({
                        "project_path": project["path"],
                        "path": fpath,
                        "parent": project,
                        "files": [],
                    })
