import os
import sys
import re
import sexpr

VERSION = "4.6.1"

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

def populateProject(project, root, types=None):
    if types is None:
        types = [SCH_SUFFIX, PCB_SUFFIX, STEP_SUFFIX]
    project["files"] = []
    project["parent"] = None
    project["project_path"] = project["path"]
    if project["path"].endswith(".kikit_pnl"):
        return
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

        project["fp_lib_table"] = []
        project["sym_lib_table"] = []

        KIPRJMOD = os.path.dirname(project["path"])
        fp_lib_table_path = os.path.join(KIPRJMOD, "fp-lib-table")
        if os.path.exists(fp_lib_table_path):
            # print("fp_lib_table_path", fp_lib_table_path)
            try:
                fp_lib_table = sexpr.parse(open(fp_lib_table_path).read())
                # print(fp_lib_table)
                for libnode in fp_lib_table.get_all("lib"):
                    # print(libnode)
                    lib = {
                        "name": libnode.get("name").value,
                        "path": libnode.get("uri").value,
                    }
                    # print(lib)
                    project["fp_lib_table"].append(lib)
            except:
                import traceback
                traceback.print_exc()

        sym_lib_table_path = os.path.join(KIPRJMOD, "sym-lib-table")
        if os.path.exists(sym_lib_table_path):
            # print("sym_lib_table_path", sym_lib_table_path)
            try:
                sym_lib_table = sexpr.parse(open(sym_lib_table_path).read())
                # print(sym_lib_table)
                for libnode in sym_lib_table.get_all("lib"):
                    # print(libnode)
                    lib = {
                        "name": libnode.get("name").value,
                        "path": libnode.get("uri").value,
                    }
                    # print(lib)
                    project["sym_lib_table"].append(lib)
            except:
                import traceback
                traceback.print_exc()

def populateWorkspace(workspace, root, types=None):
    for project in workspace["projects"]:
        populateProject(project, root, types)
