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
from threading import Thread
from common import *

FILE_ORDER = [PNL_SUFFIX, ".kicad_pro"]

try:
    base_path = sys._MEIPASS
    ARGV0 = [sys.argv[0]]
except Exception:
    ARGV0 = [sys.executable, sys.argv[0]]

if platform.system() == 'Windows':
    import win32gui
    import win32process
    import win32con

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
    time.sleep(2)

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

def macos_open_file(filepath, filters, *open_args):
    """
    Opens a file with its default application on macOS and returns the PID
    of the launched process.

    Args:
        file_path (str): Path to the file to be opened

    Returns:
        int: PID of the opened application, or None if unsuccessful
    """
    # Get initial set of PIDs before launching
    initial_pids = set(psutil.pids())

    # Open the file with the default application
    open_command = ["open", *open_args, filepath]
    subprocess.Popen(open_command)

    # Wait a moment for the application to launch
    time.sleep(2)

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

        self.pidmap = {}
        if os.path.exists(self.state.filepath):
            self.loadFile()
        else:
            self.state.root = os.path.dirname(os.path.abspath(self.state.filepath))
            self.state.workspace = {"projects": []}
            findFiles(self.state.workspace, self.state.root)
            self.saveFile()

    def loadFile(self):
        if not os.path.exists(self.state.filepath):
            return
        with open(self.state.filepath, "r") as f:
            self.state.root = os.path.dirname(os.path.abspath(self.state.filepath))
            self.state.workspace = json.load(f)
            for project in self.state.workspace["projects"]:
                if not os.path.isabs(project["path"]):
                    project["path"] = os.path.join(self.state.root, project["path"])
        findFiles(self.state.workspace, self.state.root)

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
            json.dump(workspace, f, indent=4)

    def content(self):
        with VBox():
            with HBox():
                Button("Add Project/Panelization").click(lambda e: self.addFileDialog())
                Button("New Panelization").click(lambda e: self.newPanelization())
                Button("Differ").click(lambda e: self.openDiffer())
                Spacer()
                Button("Close").click(lambda e: self.close())

            with HBox():
                with (Tree().layout(weight=1).expandAll().expandable(False)
                    .dragEnter(self.handleDragEnter).drop(self.handleDrop)):
                    for project in self.state.workspace["projects"]:
                        with (TreeNode(os.path.basename(project["path"]))
                                .click(lambda e, project: self.selectFile(project), project)
                                .dblclick(lambda e, project: self.openFile(project["path"]), project)):
                            for file in project["files"]:
                                (TreeNode(os.path.basename(file["path"]))
                                    .click(lambda e, file: self.selectFile(file), file)
                                    .dblclick(lambda e, file: self.openFile(file["path"]), file))

                with VBox().layout(weight=1):
                    with HBox():
                        Label("File:")
                        if self.state.focus is not None:
                            Label(os.path.basename(self.state.focus["project_path"]))
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
                                Label(desc)
                                Button("Edit").click(lambda e: self.editDescription())
                        Spacer()

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
            print("Drag enter", "accent", event)
            event.accept()
        else:
            print("Drag enter", "ignore", event)
            event.ignore()

    def handleDrop(self, event):
        print("Dropped", event)
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                filepath = url.toLocalFile()
                if re.match(r".+?\.kicad_(pro|sch|pcb|prl)$", filepath):
                    filepath = re.sub(r"\.kicad_(pro|sch|pcb|prl)$", ".kicad_pro", filepath)
                    self.addFile(filepath)
                else:
                    print("Dropped unknown file", filepath)
            event.accept()
        else:
            event.ignore()

    def openDiffer(self):
        if bringToFront(self.pidmap.get(":differ")):
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
        self.pidmap[":differ"] = pid
        p.wait()
        self.pidmap.pop(":differ", None)

    def selectFile(self, node):
        Thread(target=self._selectFile, args=[node], daemon=True).start()

    def _selectFile(self, node):
        time.sleep(0.5)
        self.state.editingDesc = False
        self.state.focus = node

    def addFileDialog(self):
        filepath = OpenFile("Open Project/Panelization", types=f"KiCad Project/Panelization (*.kicad_pro *.kikit_pnl)|*.kicad_pro|*.kikit_pnl")
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
        self.state.workspace["projects"].sort(key=lambda x: (-indexOf(FILE_ORDER, os.path.splitext(x["path"])[1]), os.path.basename(x["path"])))
        findFiles(self.state.workspace, self.state.root)
        self.saveFile()
        self.state()

    def removeFile(self):
        if Confirm("Are you sure you want to remove this file from the workspace?", "Remove file"):
            self.state.workspace["projects"] = [p for p in self.state.workspace["projects"] if p["project_path"] != self.state.focus["project_path"]]
            findFiles(self.state.workspace, self.state.root)
            self.saveFile()
            self.state()

    def newPanelization(self):
        filepath = SaveFile("New Panelization", types=f"KiCad Panelization (*.kikit_pnl)|*.kikit_pnl")
        if filepath:
            if not filepath.endswith(".kikit_pnl"):
                filepath = filepath + ".kikit_pnl"
            if self.state.filepath:
                self.state.workspace["projects"].append({
                    "path": filepath,
                    "description": "",
                })
                self.state.workspace["projects"].sort(key=lambda x: (-indexOf(FILE_ORDER, os.path.splitext(x["path"])[1]), os.path.basename(x["path"])))
                findFiles(self.state.workspace, self.state.root)
                self.saveFile()
            self.openPanelizer(filepath)

    def openFile(self, path):
        if path.lower().endswith(PNL_SUFFIX):
            self.openPanelizer(path)
            return
        if path.lower().endswith(STEP_SUFFIX):
            self.openStep(path)
            return
        if bringToFront(self.pidmap.get(path)):
            return
        Thread(target=self._openFile, args=[path], daemon=True).start()

    def _openFile(self, filepath):
        import subprocess, platform
        if platform.system() == 'Darwin':
            pid = macos_open_file(filepath, ["pcbnew", "eeschema"], "-n", "-W")
            if pid:
                self.pidmap[filepath] = pid
        elif platform.system() == 'Windows':
            pid = windows_open_file(filepath, ["pcbnew", "eeschema", "freecad"])
            if pid:
                self.pidmap[filepath] = pid
        else:
            # XXX window recalling is not implemented
            subprocess.Popen(('xdg-open', filepath)) # XXX wait for process to finish

    def openFolder(self, location):
        if platform.system() == 'Darwin':
            subprocess.run(["open", location])
        elif platform.system() == 'Windows':
            subprocess.run(["explorer", location])
        else:
            subprocess.run(["xdg-open", location])

    def openPanelizer(self, filepath):
        if bringToFront(self.pidmap.get(filepath)):
            return
        Thread(target=self._openPanelizer, args=[filepath], daemon=True).start()

    def _openPanelizer(self, filepath):
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.Popen([*ARGV0, filepath], **kwargs)
        pid = p.pid
        self.pidmap[filepath] = pid
        p.wait()
        self.pidmap.pop(filepath, None)

    def openStep(self, filepath):
        if bringToFront(self.pidmap.get(filepath)):
            return
        Thread(target=self._openStep, args=[filepath], daemon=True).start()

    def _openStep(self, filepath):
        if platform.system() == 'Darwin':
            pid = macos_open_file(filepath, ["freecad"], "-a", "FreeCAD", "-n", "-W", "--args")
            if pid:
                self.pidmap[filepath] = pid
        elif platform.system() == 'Windows':
            pid = windows_open_file(filepath, ["freecad"])
            if pid:
                self.pidmap[filepath] = pid

    def close(self):
        self.main.state.workspaces = [f for f in self.main.state.workspaces if f != self.filepath]
        self.main.commit()

class MainUI(Application):
    def __init__(self, filepaths=None):
        workspaces = []

        from pathlib import Path
        cfgfile = Path.home() / ".kikakuka"
        if os.path.exists(cfgfile):
            try:
                cfg = json.load(open(cfgfile))
                workspaces.extend(cfg["workspaces"])
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
        self.pidmap = {}

    def commit(self):
        from pathlib import Path
        f = open(Path.home() / ".kikakuka", "w")
        json.dump({
            "workspaces": list(self.state.workspaces)
        }, f)
        f.close()

    def content(self):
        title = f"Kikakuka v{VERSION} Workspace (PUI {PUI.__version__} {PUI_BACKEND})"
        with Window(size=(1300, 768), title=title, icon=resource_path("icon.ico")).keypress(self.keypress):
            with VBox():
                if not self.state.workspaces:
                    with HBox():
                        Label("Workspace")
                        Button("New").click(lambda e: self.newWorkspace())
                        Button("Open").click(lambda e: self.openWorkspace())
                        Spacer()
                    with HBox():
                        Label("Panelization")
                        Button("New").click(lambda e: self.newPanelization())
                        Button("Open").click(lambda e: self.openPanelizationAndClose())
                        Spacer()
                    with HBox():
                        Label("Differ")
                        Button("Open").click(lambda e: self.openDiffer())
                        Spacer()

                    Spacer()
                    return

                with HBox():
                    Button("New Workspace").click(lambda e: self.newWorkspace())
                    Button("Open Workspace").click(lambda e: self.openWorkspace())
                    Spacer()
                    Button("New Panelization").click(lambda e: self.newPanelization())
                    Button("Open Panelization").click(lambda e: self.openPanelizationAndClose())

                with Tabs():
                    for workspace in self.state.workspaces:
                        with Tab(os.path.splitext(os.path.basename(workspace))[0]):
                            WorkspaceUI(self, workspace).id(workspace)

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
        filepath = SaveFile("New Panelization", types=f"KiCad Panelization (*.kikit_pnl)|*.kikit_pnl")
        if filepath:
            if not filepath.endswith(".kikit_pnl"):
                filepath = filepath + ".kikit_pnl"
            self.openPanelizer(filepath)

    def openPanelizationAndClose(self):
        filepath = OpenFile("Open Panelization", types=f"KiCad Panelization (*.kikit_pnl)|*.kikit_pnl")
        if filepath:
            self.openPanelizer(filepath)
            self.quit()

    def openPanelizer(self, filepath):
        if bringToFront(self.pidmap.get(filepath)):
            return
        Thread(target=self._openPanelizer, args=[filepath], daemon=True).start()

    def _openPanelizer(self, filepath):
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.Popen([*ARGV0, filepath], **kwargs)
        pid = p.pid
        self.pidmap[filepath] = pid
        p.wait()
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