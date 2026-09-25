import os
import sys
import json
from PUI.PySide6 import *
from PUI.interfaces import BaseTreeAdapter
import PUI
import re
import subprocess
import platform
import psutil
from importlib.metadata import PackageNotFoundError, version as package_version
from threading import Thread
from common import *
from pcb_open import system_open_command
from workspace_monitor import (replace_freecad_documents,
                               snapshot_editor_processes, update_pidmap_entry)

FREECAD_SUFFIXES = (ASSEMBLY_SUFFIX, FREECAD_SUFFIX, STEP_SUFFIX)
FILE_ORDER = [*PNL_SUFFIXES, ASSEMBLY_SUFFIX, FREECAD_SUFFIX, ".kicad_pro"]
WINDOWS_FREECAD_EXE = r"C:\Program Files\FreeCAD 1.0\bin\FreeCAD.exe"

try:
    KIPY_VERSION = package_version("kicad-python")
except PackageNotFoundError:
    KIPY_VERSION = "unknown"

try:
    base_path = sys._MEIPASS
    ARGV0 = [sys.argv[0]]
except Exception:
    ARGV0 = [sys.executable, sys.argv[0]]

if platform.system() == 'Windows':
    import win32gui
    import win32process
    import win32con

def windows_associated_executable(extension):
    """Return the executable registered for a Windows file extension."""
    import ctypes
    from ctypes import wintypes

    assoc_query_string = ctypes.windll.shlwapi.AssocQueryStringW
    assoc_query_string.argtypes = (
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    assoc_query_string.restype = wintypes.LONG

    assocstr_executable = 2
    length = wintypes.DWORD()
    assoc_query_string(
        0, assocstr_executable, extension, None, None, ctypes.byref(length)
    )
    if not length.value:
        return None

    executable = ctypes.create_unicode_buffer(length.value)
    result = assoc_query_string(
        0,
        assocstr_executable,
        extension,
        None,
        executable,
        ctypes.byref(length),
    )
    if result != 0:
        return None
    return executable.value or None

def windows_open_file(file_path, filters):
    """
    Opens a file with its default application and returns the PID
    of the launched process.

    Args:
        file_path (str): Path to the file to be opened
        filters ([str]): List of process filter keyword

    Returns:
        int: PID of the opened application, or None if unsuccessful
    """
    # Get initial set of PIDs before launching
    initial_pids = set(psutil.pids())

    # Open the file with the default application (non-blocking)
    os.startfile(file_path)

    # Wait a moment for the application to launch
    time.sleep(3)

    # Get new set of PIDs after launching
    new_pids = set(psutil.pids())

    # Find newly created processes
    new_processes = new_pids - initial_pids

    # If no new process was created, return None
    if not new_processes:
        print("No new process detected")
        return None

    # If multiple processes were created, find the most likely parent process
    if len(new_processes) > 1:
        # Get process info for all new processes
        processes = []
        for pid in new_processes:
            try:
                proc = psutil.Process(pid)
                processes.append((pid, proc.name(), proc.create_time()))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        processes = [p for p in processes if any([f in psutil.Process(p[0]).name().lower() for f in filters])]

        if processes:
            pid = processes[0][0]
            print(f"Multiple processes created. Using newest: PID {pid} ({processes[0][1]})")
            print(f"All new processes: {processes}")
            return pid
    else:
        # Only one new process, return its PID
        pid = list(new_processes)[0]
        try:
            proc_name = psutil.Process(pid).name()
            print(f"File opened with: {proc_name} (PID: {pid})")
            return pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            print(f"Process with PID {pid} was created but can't access its info")
            return pid

    return None


def open_folder(location):
    if platform.system() == 'Darwin':
        subprocess.run(["open", location])
    elif platform.system() == 'Windows':
        subprocess.run(["explorer", location])
    else:
        subprocess.run(["xdg-open", location])


def windows_bring_pid_to_front(pid):
    """
    Brings the main window of a process with the specified PID to the foreground.

    Args:
        pid (int): Process ID of the window to bring to front

    Returns:
        bool: True if successful, False otherwise
    """
    def enum_windows_callback(hwnd, result):
        # Get the process ID for the current window
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)

        # Check if this window belongs to the PID we're looking for and is visible
        if window_pid == pid and win32gui.IsWindowVisible(hwnd):
            # Store the window handle in our result list
            result.append(hwnd)

    window_handles = []
    win32gui.EnumWindows(enum_windows_callback, window_handles)

    if not window_handles:
        print(f"No visible windows found for PID {pid}")
        return False

    # Bring the first window found to the front
    # You might want to modify this to find the main window if there are multiple
    hwnd = window_handles[0]

    # Check if the window is minimized
    if win32gui.IsIconic(hwnd):
        # Restore the window if it's minimized
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    # Set the window to foreground
    win32gui.SetForegroundWindow(hwnd)
    print(f"Successfully brought window for PID {pid} to front")
    return True

def posix_open_file(filepath, filters, *open_args):
    """
    Opens a file with its default application and finds the launched PID.
    macOS accepts *open_args*; Linux uses xdg-open instead.

    Args:
        file_path (str): Path to the file to be opened

    Returns:
        int: PID of the opened application, or None if unsuccessful
    """
    # Get initial set of PIDs before launching
    initial_pids = set(psutil.pids())

    # Open the file with the default application
    open_command = system_open_command(filepath, mac_args=open_args)
    subprocess.Popen(open_command)

    # Wait a moment for the application to launch
    time.sleep(3)

    # Get new set of PIDs after launching
    new_pids = set(psutil.pids())

    # Find newly created processes
    new_processes = new_pids - initial_pids

    # If no new process was created, return None
    if not new_processes:
        print("No new process detected")
        return None

    # If multiple processes were created, find the most likely parent process
    if len(new_processes) > 1:
        # Get process info for all new processes
        processes = []
        for pid in new_processes:
            try:
                proc = psutil.Process(pid)
                processes.append((pid, proc.name(), proc.create_time()))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        processes = [p for p in processes if any([f in psutil.Process(p[0]).name().lower() for f in filters])]

        if processes:
            pid = processes[0][0]
            print(f"Multiple processes created. Using newest: PID {pid} ({processes[0][1]})")
            print(f"All new processes: {processes}")
            return pid
    else:
        # Only one new process, return its PID
        pid = list(new_processes)[0]
        try:
            proc_name = psutil.Process(pid).name()
            print(f"File opened with: {proc_name} (PID: {pid})")
            return pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            print(f"Process with PID {pid} was created but can't access its info")
            return pid

    return None

def macos_bring_pid_to_front(pid):
    applescript = f'''
    tell application "System Events"
        set frontApp to name of first application process whose unix id is {pid}
        if frontApp is not "" then
            set frontmost of every process whose unix id is {pid} to true
            return true
        else
            return false
        end if
    end tell
    '''

    # Run the AppleScript
    result = subprocess.run(
        ['osascript', '-e', applescript],
        capture_output=True,
        text=True
    )

    if "true" in result.stdout.lower():
        print(f"Successfully brought application PID: {pid} to front")
        return True
    else:
        print(f"Failed to bring application to front. Process with PID {pid} may not have a GUI window")
        return False

def bringToFront(pid):
    if not pid:
        return False
    import platform
    if platform.system() == 'Darwin':
        return macos_bring_pid_to_front(pid)
    elif platform.system() == 'Windows':
        return windows_bring_pid_to_front(pid)
    else:
        return False


def populateProject(project, root, types=None):
    if types is None:
        types = [SCH_SUFFIX, PCB_SUFFIX, STEP_SUFFIX]
    project["files"] = []
    project["parent"] = None
    project["project_path"] = project["path"]
    if project["path"].lower().endswith(PNL_SUFFIXES):
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

        project["fp_lib_table"] = {
            "version": None,
            "lib": [],
        }
        project["sym_lib_table"] = {
            "version": None,
            "lib": [],
        }

        KIPRJMOD = os.path.dirname(project["path"])
        fp_lib_table_path = os.path.join(KIPRJMOD, "fp-lib-table")
        if os.path.exists(fp_lib_table_path):
            # print("fp_lib_table_path", fp_lib_table_path)
            try:
                fp_lib_table = sexpr.parse(open(fp_lib_table_path).read())
                project["fp_lib_table"]["version"] = fp_lib_table.get("version").value
                # print(fp_lib_table)
                for libnode in fp_lib_table.get_all("lib"):
                    # print(libnode)
                    lib = StateDict({
                        "name": libnode.get("name").value,
                        "uri": libnode.get("uri").value,
                        "type": libnode.get("type").value,
                        "options": libnode.get("options").value,
                        "descr": libnode.get("descr").value,
                    })
                    # print(lib)
                    project["fp_lib_table"]["lib"].append(lib)
            except:
                import traceback
                traceback.print_exc()

        sym_lib_table_path = os.path.join(KIPRJMOD, "sym-lib-table")
        if os.path.exists(sym_lib_table_path):
            # print("sym_lib_table_path", sym_lib_table_path)
            try:
                sym_lib_table = sexpr.parse(open(sym_lib_table_path).read())
                project["sym_lib_table"]["version"] = sym_lib_table.get("version").value
                # print(sym_lib_table)
                for libnode in sym_lib_table.get_all("lib"):
                    # print(libnode)
                    lib = StateDict({
                        "name": libnode.get("name").value,
                        "uri": libnode.get("uri").value,
                        "type": libnode.get("type").value,
                        "options": libnode.get("options").value,
                        "descr": libnode.get("descr").value,
                    })
                    # print(lib)
                    project["sym_lib_table"]["lib"].append(lib)
            except:
                import traceback
                traceback.print_exc()

def populateWorkspace(workspace, root, types=None):
    for project in workspace["projects"]:
        populateProject(project, root, types)

def commitLibTable(project, root):
    with open(os.path.join(os.path.dirname(project["project_path"]), "fp-lib-table"), "w") as f:
        f.write("(fp_lib_table\n")
        f.write(f"  (version {project['fp_lib_table']['version']})\n")
        for lib in project["fp_lib_table"]["lib"]:
            f.write(f"  (lib (name {json.dumps(lib['name'])})(type {json.dumps(lib['type'])})(uri {json.dumps(lib['uri'])})(options {json.dumps(lib['options'])})(descr {json.dumps(lib['descr'])}))\n")
        f.write(")\n")
    with open(os.path.join(os.path.dirname(project["project_path"]), "sym-lib-table"), "w") as f:
        f.write("(sym_lib_table\n")
        f.write(f"  (version {project['sym_lib_table']['version']})\n")
        for lib in project["sym_lib_table"]["lib"]:
            f.write(f"  (lib (name {json.dumps(lib['name'])})(type {json.dumps(lib['type'])})(uri {json.dumps(lib['uri'])})(options {json.dumps(lib['options'])})(descr {json.dumps(lib['descr'])}))\n")
        f.write(")\n")

    populateProject(project, root)

def convertToRelativePath(project, root):
    for lib in project["sym_lib_table"]["lib"]:
        if os.path.isabs(lib["uri"]) and os.path.exists(lib["uri"]):
            # print(lib, os.path.dirname(project["project_path"]))
            reluri = relpath(lib["uri"], os.path.dirname(project["project_path"]), allow_outside=True)
            reluri = "${KIPRJMOD}/" + reluri
            print(lib["uri"], "->", reluri)
            lib["uri"] = reluri
    for lib in project["fp_lib_table"]["lib"]:
        if os.path.isabs(lib["uri"]) and os.path.exists(lib["uri"]):
            # print(lib, os.path.dirname(project["project_path"]))
            reluri = relpath(lib["uri"], os.path.dirname(project["project_path"]), allow_outside=True)
            reluri = "${KIPRJMOD}/" + reluri
            print(lib["uri"], "->", reluri)
            lib["uri"] = reluri
    commitLibTable(project, root)



class WorkspaceUI(PUIView):
    def __init__(self, main, filepath):
        super().__init__()
        self.main = main
        self.filepath = filepath

    def setup(self):
        self.state = State()
        self.state.filepath = self.filepath
        self.state.focus = None
        self.state.workspace = {"projects": []}
        self.state.editingDesc = False
        self.state.edit = ""

        if os.path.exists(self.state.filepath):
            self.loadFile()
        else:
            self.state.root = os.path.dirname(os.path.abspath(self.state.filepath))
            self.state.workspace = {"projects": []}
            populateWorkspace(self.state.workspace, self.state.root)
            self.saveFile()

    def loadFile(self):
        if not os.path.exists(self.state.filepath):
            return
        with open(self.state.filepath, "r") as f:
            self.state.root = os.path.dirname(os.path.abspath(self.state.filepath))
            self.state.workspace = json.load(f)
            projects = []
            for proj in self.state.workspace["projects"]:
                if not proj["path"] in [p["path"] for p in projects]:
                    projects.append(proj)
            self.state.workspace["projects"] = projects
            for project in self.state.workspace["projects"]:
                if not os.path.isabs(project["path"]):
                    project["path"] = os.path.join(self.state.root, project["path"])
        populateWorkspace(self.state.workspace, self.state.root)

    def saveFile(self):
        if self.state.filepath is None:
            return
        projects = []
        for project in self.state.workspace["projects"]:
            projects.append({
                "path": relpath(project["path"], os.path.dirname(self.state.filepath)),
                "description": project["description"],
            })
        workspace = {
            "projects": projects
        }
        with open(self.state.filepath, "w") as f:
            json.dump(workspace, f, indent=4, ensure_ascii=False)

    def content(self):
        with VBox():
            with HBox():
                Button("Import KiCad/FabPlan/Assembly").click(lambda e: self.addFileDialog())
                Button("New FabPlan").click(lambda e: self.newPanelization())
                Button("Differ for this workspace").click(lambda e: self.openDiffer())
                Spacer()
                Button("Close").click(lambda e: self.close())

            with HBox():
                with (Tree().layout(weight=1).expandAll().expandable(False)
                    .dragEnter(self.handleDragEnter).drop(self.handleDrop)):
                    for project in self.state.workspace["projects"]:
                        folder = os.path.basename(os.path.dirname(project["path"]))
                        file = os.path.basename(project["path"])
                        folder_file = f"{folder}/{file}"
                        with (TreeNode(folder_file)
                                .click(lambda e, project: self.selectFile(project), project)
                                .dblclick(lambda e, project: self.openFile(project["path"], bring_to_front=True), project)):
                            for file in project["files"]:
                                (TreeNode(os.path.basename(file["path"]))
                                    .click(lambda e, file: self.selectFile(file), file)
                                    .dblclick(lambda e, file: self.openFile(file["path"], bring_to_front=True), file))

                with VBox().layout(weight=1):
                    with HBox():
                        Label("File:")
                        if self.state.focus is not None:
                            Label(os.path.basename(self.state.focus["path"]), selectable=True)
                            Button("Open File Location").click(lambda e, location: self.openFolder(location), os.path.dirname(self.state.focus["project_path"]))
                            Spacer()
                            Button("Remove").click(lambda e: self.removeFile())
                        else:
                            Spacer()

                    with HBox():
                        Label("Description:")
                        if self.state.focus is not None:
                            if self.state.editingDesc:
                                TextField(self.state("edit")).layout(weight=1)
                                Button("Save").click(lambda e: self.saveDescription())
                            else:
                                if "description" in self.state.focus:
                                    desc = self.state.focus["description"]
                                else:
                                    desc = self.state.focus["parent"]["description"]
                                Label(desc, selectable=True)
                                Button("Edit").click(lambda e: self.editDescription())
                        Spacer()

                    focus_project = self.state.focus
                    if focus_project and focus_project["parent"]:
                        focus_project = focus_project["parent"]
                    if focus_project is not None and focus_project["path"].endswith(".kicad_pro"):
                        with Scroll().layout(weight=1):
                            with VBox():

                                with HBox():
                                    Label("Project Specific Libraries:")
                                    Button("Refresh").click(lambda e: populateProject(focus_project, self.state.root))
                                    Spacer()
                                    Button("Convert to relative path").click(lambda e: convertToRelativePath(focus_project, self.state.root))

                                with Grid():
                                    r = 0

                                    Label("Symbol:").grid(row=r, column=0)
                                    # Label(f"Version={focus_project['sym_lib_table']['version']}").grid(row=r, column=1)
                                    r += 1

                                    Label("Name").grid(row=r, column=0)
                                    Label("Path").grid(row=r, column=1)
                                    r += 1

                                    for lib in focus_project["sym_lib_table"]["lib"]:
                                        Label(lib["name"], selectable=True).grid(row=r, column=0)
                                        Label(lib["uri"], selectable=True).grid(row=r, column=1)
                                        r += 1

                                    Label("").grid(row=r, column=0)
                                    r += 1

                                    Label("Footprint:").grid(row=r, column=0)
                                    # Label(f"Version={focus_project['fp_lib_table']['version']}").grid(row=r, column=1)
                                    r += 1

                                    Label("Name").grid(row=r, column=0)
                                    Label("Path").grid(row=r, column=1)
                                    r += 1

                                    for lib in focus_project["fp_lib_table"]["lib"]:
                                        Label(lib["name"], selectable=True).grid(row=r, column=0)
                                        Label(lib["uri"], selectable=True).grid(row=r, column=1)
                                        r += 1

                                Spacer()
                    else:
                        Spacer()

    def editDescription(self):
        if "description" in self.state.focus:
            desc = self.state.focus["description"]
        else:
            desc = self.state.focus["parent"]["description"]
        self.state.edit = desc
        self.state.editingDesc = True

    def saveDescription(self):
        if "description" in self.state.focus:
            self.state.focus["description"] = self.state.edit
        else:
            self.state.focus["parent"]["description"] = self.state.edit
        self.state.editingDesc = False
        self.saveFile()

    def handleDragEnter(self, event):
        if event.mimeData().hasUrls():
            event.accept()
            return True
        event.ignore()
        return False

    def handleDrop(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                filepath = os.path.abspath(url.toLocalFile())
                lower = filepath.lower()
                for suffix in (
                        ".kicad_pro", ".kicad_sch", ".kicad_pcb",
                        ".kicad_prl"):
                    if lower.endswith(suffix):
                        filepath = filepath[:-len(suffix)] + ".kicad_pro"
                        break
                else:
                    if not lower.endswith((*PNL_SUFFIXES,
                                           *FREECAD_SUFFIXES)):
                        continue
                if os.path.exists(filepath):
                    self.addFile(filepath)
            event.accept()
            return True
        event.ignore()
        return False

    def openDiffer(self):
        if bringToFront(self.main.pidmap.get(":differ")):
            return
        Thread(target=self._openDiffer, args=[self.state.filepath], daemon=True).start()

    def _openDiffer(self, filepath):
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.Popen([*ARGV0, "--differ", *([filepath] if filepath else [])], **kwargs)
        if not filepath:
            self.quit()
            return
        pid = p.pid
        self.main.pidmap[":differ"] = pid
        p.wait()
        self.main.pidmap.pop(":differ", None)

    def selectFile(self, node):
        Thread(target=self._selectFile, args=[node], daemon=True).start()

    def _selectFile(self, node):
        time.sleep(0.5)
        self.state.editingDesc = False
        self.state.focus = node

    def addFileDialog(self):
        dir = None
        if self.state.filepath:
            dir = os.path.dirname(self.state.filepath)
        filepath = OpenFile(
            "Open KiCad/FabPlan/Assembly",
            dir=dir,
            types="KiCad/FabPlan/Assembly (*.kicad_pro *.kkkk_fab *.kikit_pnl *.kkkk_asm *.FCStd *.step)|*.kicad_pro;*.kkkk_fab;*.kikit_pnl;*.kkkk_asm;*.FCStd;*.step",
        )
        if filepath:
            self.addFile(filepath)

    def addFile(self, filepath):
        filepath = os.path.abspath(filepath)
        if not os.path.exists(filepath):
            return
        if filepath in [project["path"] for project in self.state.workspace["projects"]]:
            return
        self.state.workspace["projects"].append({
            "path": filepath,
            "description": "",
        })
        self.state.workspace["projects"].sort(key=lambda x: (-indexOf(FILE_ORDER, os.path.splitext(x["path"])[1].lower()), os.path.basename(x["path"])))
        populateWorkspace(self.state.workspace, self.state.root)
        self.saveFile()
        self.state()

    def removeFile(self):
        if Confirm("Are you sure you want to remove this file from the workspace?", "Remove file"):
            self.state.workspace["projects"] = [p for p in self.state.workspace["projects"] if p["project_path"] != self.state.focus["project_path"]]
            populateWorkspace(self.state.workspace, self.state.root)
            self.saveFile()
            self.state()

    def newPanelization(self):
        if self.state.filepath:
            dir = os.path.dirname(self.state.filepath)
        filepath = SaveFile("New FabPlan", dir=dir, types=f"Kikakuka FabPlan (*.kkkk_fab)|*.kkkk_fab")
        if filepath:
            if not filepath.endswith(".kkkk_fab"):
                filepath = filepath + ".kkkk_fab"
            if self.state.filepath:
                self.state.workspace["projects"].append({
                    "path": filepath,
                    "description": "",
                })
                self.state.workspace["projects"].sort(key=lambda x: (-indexOf(FILE_ORDER, os.path.splitext(x["path"])[1].lower()), os.path.basename(x["path"])))
                populateWorkspace(self.state.workspace, self.state.root)
                self.saveFile()
                self.state()
            self.openPanelizer(filepath)

    def openFile(self, path, bring_to_front=False):
        if path.lower().endswith(PNL_SUFFIXES):
            self.openPanelizer(path)
            return
        if path.lower().endswith(FREECAD_SUFFIXES):
            self.openFreeCAD(path)
            return
        if path.lower().endswith((".kicad_pcb", ".kicad_sch", ".kicad_pro")):
            from pcb_open import open_kicad_file
            Thread(target=open_kicad_file, args=[path], daemon=True).start()
            return
        pid = self.main.pidmap.get(path)
        if pid is not None:
            if bring_to_front:
                if bringToFront(pid):
                    return
            else:
                if psutil.pid_exists(pid):
                    return
        from pcb_open import open_with_system
        Thread(target=open_with_system, args=[path], daemon=True).start()

    def openFolder(self, location):
        open_folder(location)

    def openPanelizer(self, filepath):
        self.main.openPanelizer(filepath)

    def openFreeCAD(self, filepath):
        Thread(target=self._openFreeCAD, args=[filepath], daemon=True).start()

    def _openFreeCAD(self, filepath):
        from im import im_mesh
        from pcb_open import open_with_system
        try:
            reply = im_mesh.request({"action": "open-file", "filepath": filepath})
            if reply.get("status") == "error":
                print(f"Instance mesh: {reply.get('message', 'could not open FreeCAD file')}")
        except ConnectionError:
            from im.instance_backend import _editors
            if _editors("freecad"):
                print("Instance mesh unavailable: FreeCAD is running but its "
                      "FreekiCAD instance node cannot be reached")
                return
            open_with_system(filepath)

    def close(self):
        if Confirm("Are you sure you want to close this workspace?", "Close workspace"):
            self.main.state.workspaces = [f for f in self.main.state.workspaces if f != self.filepath]
            self.main.commit()

class MainUI(Application):
    def __init__(self, filepaths=None):
        workspaces = []

        from pathlib import Path
        self.cfgfile = Path.home() / ".kikakuka"
        if os.path.exists(self.cfgfile):
            try:
                cfg = json.load(open(self.cfgfile))
                for workspace in cfg["workspaces"]:
                    if os.path.exists(workspace):
                        workspaces.append(workspace)
            except Exception:
                pass

        if filepaths is not None:
            workspaces.extend([os.path.abspath(filepath) for filepath in filepaths])
        dedup = []
        for l in workspaces:
            if l not in dedup:
                dedup.append(l)
        workspaces = dedup
        super().__init__(icon=resource_path("icon.ico"))
        self.state = State()
        self.state.workspaces = workspaces
        self.commit()
        self.pidmap = StateDict({})

        # Host a symmetric instance node. No workspace-owned socket or
        # permanent leader is required for FreekiCAD to resolve KiCad IPC.
        self._bus = None
        try:
            from im.im_mesh import start_node
            from im.instance_backend import handle
            self._bus = start_node(handle, self._mesh_mapping_changed)
            import atexit
            atexit.register(self._shutdown_bus)
        except Exception as e:
            print(f"Instance mesh: Could not start: {e}")

        self.refresh_monitor()

    def refresh_monitor(self, _event=None):
        from im.instance_backend import scan_open_kicad_boards
        boards = scan_open_kicad_boards()
        if self._bus:
            from im.im_mesh import scan_freecad_documents
            self._bus.refresh()
            scans = scan_freecad_documents()
        else:
            scans = []
        with self.pidmap:
            for pid, paths in scans:
                replace_freecad_documents(self.pidmap, pid, paths)
            for pid, filepath in boards:
                update_pidmap_entry(self.pidmap, filepath, pid)
        # Include editors that were started outside the mesh as well.
        self.pidmap()

    def go_to_monitor_row(self, _event, pid, program, filepath):
        Thread(target=self._go_to_monitor_row,
               args=(pid, program, filepath), daemon=True).start()

    def _go_to_monitor_row(self, pid, program, filepath):
        if not psutil.pid_exists(pid):
            return
        bringToFront(pid)
        if program == "FreeCAD" and filepath:
            try:
                from im.im_mesh import activate_open_freecad_document
                activate_open_freecad_document(filepath, target_pid=pid)
            except Exception as exc:
                print(f"Instance Manager: Could not activate {filepath} in PID {pid}: {exc}")

    def _update_pidmap_entry(self, filepath, pid):
        with self.pidmap:
            update_pidmap_entry(self.pidmap, filepath, pid)

    def _mesh_mapping_changed(self, filepath, pid):
        if pid is None:
            self.pidmap.pop(filepath, None)
        else:
            self._update_pidmap_entry(filepath, pid)

    def _open_kicad_file(self, filepath, bring_to_front=False):
        """Open a KiCad board or schematic in a new editor instance.

        Called from WorkspaceBus when a resolve request arrives for a
        file not yet in the pidmap.  Returns the PID on success, or None.

        When *bring_to_front* is False (the default for WorkspaceBus callers),
        macOS uses ``-g`` so KiCad launches in the background.
        """
        print(f"MainUI: opening KiCad for {filepath}")
        if platform.system() in ['Darwin', 'Linux']:
            open_args = ["-n"]
            if not bring_to_front:
                open_args.append("-g")
            pid = posix_open_file(filepath, ["kicad", "pcbnew", "eeschema"], *open_args)
            if pid and bring_to_front:
                bringToFront(pid)
        elif platform.system() == 'Windows':
            pid = windows_open_file(filepath, ["kicad", "pcbnew", "eeschema"])
            if pid and bring_to_front:
                bringToFront(pid)
        else:
            subprocess.Popen(('xdg-open', filepath))
            pid = None
        if pid:
            self.pidmap[filepath] = pid
            print(f"MainUI: KiCad started, PID {pid}")
        else:
            print(f"MainUI: could not determine KiCad PID")
        return pid

    def _shutdown_bus(self):
        if self._bus:
            self._bus.close()
            self._bus = None

    def commit(self):
        f = open(self.cfgfile, "w")
        json.dump({
            "workspaces": list(self.state.workspaces)
        }, f)
        f.close()

    def content(self):
        title = (
            f"Kikakuka v{VERSION} Workspace "
            f"(PUI {PUI.__version__} {PUI_BACKEND}, kipy {KIPY_VERSION})"
        )
        with Window(size=(1300, 768), title=title, icon=resource_path("icon.ico")).keypress(self.keypress):
            with VBox():
                with HBox():
                    Button("New Workspace").click(lambda e: self.newWorkspace())
                    Button("Open Workspace").click(lambda e: self.openWorkspace())
                    Button("New FabPlan").click(lambda e: self.newPanelization())
                    Button("Open FabPlan").click(lambda e: self.openPanelizationAndClose())
                    Spacer()
                    Button("Differ").click(lambda e: self.openDiffer())

                with Tabs().layout(weight=1):
                    for workspace in self.state.workspaces:
                        with Tab(os.path.splitext(os.path.basename(workspace))[0]):
                            WorkspaceUI(self, workspace).id(workspace)

                    with Tab("Instance Manager"):
                        with VBox():
                            with HBox():
                                Label("KiCad and FreeCAD processes")
                                Button("Refresh").click(self.refresh_monitor)
                                Spacer()
                            with Scroll().layout(weight=1):
                                with VBox():
                                    with Grid():
                                        Label("ProcessID").grid(row=0, column=0)
                                        Label("Program").grid(row=0, column=1)
                                        Label("File Path").grid(row=0, column=2)
                                        Label("Action").grid(row=0, column=3)
                                        rows = snapshot_editor_processes(self.pidmap)
                                        if not rows:
                                            Label("No KiCad or FreeCAD processes found").grid(row=1, column=0)
                                        else:
                                            for row, (pid, program, filepath) in enumerate(rows, start=1):
                                                Label(str(pid)).grid(row=row, column=0)
                                                Label(program).grid(row=row, column=1)
                                                Label(filepath or "Unknown", selectable=True).grid(row=row, column=2)
                                                with HBox().grid(row=row, column=3):
                                                    Button("Go to").click(
                                                        self.go_to_monitor_row, pid, program, filepath
                                                    )
                                                    if filepath:
                                                        Button("Open File Location").click(
                                                            lambda e, path: open_folder(os.path.dirname(path)), filepath
                                                        )
                                    Spacer()

    def newWorkspace(self):
        filepath = SaveFile("New Workspace", types=f"Kikakuka Workspace (*.kkkk)|*.kkkk")
        if filepath:
            if not filepath.endswith(".kkkk"):
                filepath = filepath + ".kkkk"
            filepath = os.path.abspath(filepath)
            if filepath not in self.state.workspaces:
                self.state.workspaces.append(filepath)
                self.commit()

    def openWorkspace(self):
        filepath = OpenFile("Open Workspace", types=f"Kikakuka Workspace (*.kkkk)|*.kkkk")
        if filepath:
            filepath = os.path.abspath(filepath)
            if filepath not in self.state.workspaces:
                self.state.workspaces.append(filepath)
                self.commit()


    def newPanelization(self):
        filepath = SaveFile("New FabPlan", types=f"Kikakuka FabPlan (*.kkkk_fab)|*.kkkk_fab")
        if filepath:
            if not filepath.endswith(".kkkk_fab"):
                filepath = filepath + ".kkkk_fab"
            self.openPanelizer(filepath)

    def openPanelizationAndClose(self):
        filepath = OpenFile("Open FabPlan", types=f"Kikakuka FabPlan (*.kkkk_fab *.kikit_pnl)|*.kkkk_fab;*.kikit_pnl")
        if filepath:
            self.openPanelizer(filepath)
            self.quit()

    def openPanelizer(self, filepath):
        Thread(target=self._openPanelizer, args=[filepath], daemon=True).start()

    def _openPanelizer(self, filepath):
        from im.im_mesh import activate_or_open_published_file

        filepath = os.path.normcase(
            os.path.realpath(os.path.abspath(filepath))
        )
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        launched = {}

        def opener(path):
            process = subprocess.Popen([*ARGV0, path], **kwargs)
            launched["process"] = process
            return process.pid

        pid, opened = activate_or_open_published_file(
            filepath, opener, bringToFront
        )
        if pid is None or not opened:
            return

        process = launched["process"]
        self.pidmap[filepath] = pid
        process.wait()
        if self._bus and self._bus.snapshot().get(filepath) == pid:
            self._bus.publish(filepath, None)
        if self.pidmap.get(filepath) == pid:
            self.pidmap.pop(filepath, None)

    def openDiffer(self):
        if bringToFront(self.pidmap.get(":differ")):
            return
        Thread(target=self._openDiffer, daemon=True).start()

    def _openDiffer(self):
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.Popen([*ARGV0, "--differ"], **kwargs)
        pid = p.pid
        self.pidmap[":differ"] = pid
        p.wait()
        self.pidmap.pop(":differ", None)
