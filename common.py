import os
import sys
import sexpr

VERSION = "8.2"

WORKSPACE_SUFFIX = ".kkkk"
PNL_SUFFIX = ".kkkk_fab"
LEGACY_PNL_SUFFIX = ".kikit_pnl"
PNL_SUFFIXES = (PNL_SUFFIX, LEGACY_PNL_SUFFIX)
PCB_SUFFIX = ".kicad_pcb"
SCH_SUFFIX = ".kicad_sch"
STEP_SUFFIX = ".step"
STL_SUFFIX = ".stl"
FREECAD_SUFFIX = ".fcstd"
ASSEMBLY_SUFFIX = ".kkkk_asm"
KICAD_PROJECT_SUFFIX = ".kicad_pro"
KICAD_PROJECT_MEMBER_SUFFIXES = (
    KICAD_PROJECT_SUFFIX, PCB_SUFFIX, SCH_SUFFIX, ".kicad_prl")


def kicad_project_path(path):
    """Return the sibling .kicad_pro represented by a KiCad project file."""
    path = os.fspath(path)
    lower = path.lower()
    if lower.endswith(KICAD_PROJECT_SUFFIX):
        return path
    for suffix in KICAD_PROJECT_MEMBER_SUFFIXES[1:]:
        if lower.endswith(suffix):
            return path[:-len(suffix)] + KICAD_PROJECT_SUFFIX
    return path


def workspace_entry_path(path):
    """Normalize an existing selected file into its workspace entry path."""
    path = os.path.abspath(os.fspath(path))
    if not os.path.exists(path):
        return None
    return kicad_project_path(path)


def workspace_filename(path):
    """Hide .kicad_pro only for a virtual entry without a project file."""
    path = os.fspath(path)
    filename = os.path.basename(path)
    if (filename.lower().endswith(KICAD_PROJECT_SUFFIX)
            and not os.path.isfile(path)):
        return filename[:-len(KICAD_PROJECT_SUFFIX)]
    return filename

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

def findFiles(workspace, root, types=None):
    if types is None:
        types = [SCH_SUFFIX, PCB_SUFFIX, STEP_SUFFIX, STL_SUFFIX]
    for project in workspace["projects"]:
        project["files"] = []
        project["parent"] = None
        project["project_path"] = project["path"]
        if project["path"].lower().endswith(PNL_SUFFIXES):
            continue
        if project["path"].lower().endswith(KICAD_PROJECT_SUFFIX):
            for ext in types:
                fpath = project["path"][:-len(KICAD_PROJECT_SUFFIX)] + ext
                if not os.path.isabs(fpath):
                    fpath = os.path.join(root, fpath)
                if os.path.exists(fpath):
                    project["files"].append({
                        "project_path": project["path"],
                        "path": fpath,
                        "parent": project,
                        "files": [],
                    })
