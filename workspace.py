import os
import sys
import json
from PUI.PySide6 import *
from PUI.interfaces import BaseTreeAdapter
import PUI
import re
import subprocess
from threading import Thread
from common import *

FILE_ORDER = [PNL_SUFFIX, ".kicad_pro"]

class WorkspaceUI(Application):
    def __init__(self, filepath=None):
        super().__init__(icon=resource_path("icon.ico"))
        self.state = State()
        self.state.filepath = filepath
        self.state.focus = None
        self.state.workspace = {"projects": []}
        self.state.editingDesc = False
        self.state.edit = ""

        self.pidmap = {}

    def setup(self):
        if self.state.filepath:
            self.loadFile()

    def loadFile(self):
        if not os.path.exists(self.state.filepath):
            return
        with open(self.state.filepath, "r") as f:
            self.state.root = os.path.dirname(os.path.abspath(self.state.filepath))
            self.state.workspace = json.load(f)
            for project in self.state.workspace["projects"]:
                project["path"] = os.path.abspath(project["path"])
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
        with Window(size=(1300, 768), title=f"Kikakuka (PUI {PUI.__version__} {PUI_BACKEND}))", icon=resource_path("icon.ico")).keypress(self.keypress):
            with VBox():
                if self.state.filepath is None:
                    with HBox():
                        Button("New Workspace").click(lambda e: self.newWorkspace())
                        Button("Open Workspace").click(lambda e: self.openWorkspace())
                        Button("New Panelization").click(lambda e: self.newPanelization())
                        Spacer()

                    Spacer()
                    return

                with HBox():
                    Button("Add Project/Panelization").click(lambda e: self.addFileDialog())
                    Button("New Panelization").click(lambda e: self.newPanelization())
                    Spacer()

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

    def newWorkspace(self):
        filepath = SaveFile("New Workspace", types=f"Kikakuka Workspace (*.kkkk)|*.kkkk")
        if filepath:
            self.state.filepath = filepath
            self.state.workspace = {"projects": []}
            findFiles(self.state.workspace, self.state.root)
            self.saveFile()

    def openWorkspace(self):
        filepath = OpenFile("Open Workspace", types=f"Kikakuka Workspace (*.kkkk)|*.kkkk")
        if filepath:
            self.state.filepath = filepath
            self.loadFile()

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
        pid = self.pidmap.get(path)
        if pid:
            self.bringToFront(pid)
            return
        Thread(target=self._openFile, args=[path], daemon=True).start()

    def _openFile(self, filepath):
        import subprocess, platform
        if platform.system() == 'Darwin':
            cmd = ["open", "-n", "-W", filepath]
            p = subprocess.Popen(cmd)
            pid = p.pid + 1
            self.pidmap[filepath] = pid
            p.wait()
            self.pidmap.pop(filepath, None)
        elif platform.system() == 'Windows':
            # XXX untested
            p = subprocess.Popen(['cmd', '/c', 'start', '/wait', filepath], shell=True)
            pid = p.pid + 1
            self.pidmap[filepath] = pid
            p.wait()
            self.pidmap.pop(filepath, None)
        else:
            # XXX untested
            p = subprocess.Popen(('xdg-open', filepath)) # XXX wait for process to finish
            pid = p.pid + 1
            self.pidmap[filepath] = pid
            p.wait()
            self.pidmap.pop(filepath, None)

    def openFolder(self, location):
        import subprocess, platform
        if platform.system() == 'Darwin':
            subprocess.run(["open", location])
        elif platform.system() == 'Windows':
            subprocess.run(["explorer", location])
        else:
            subprocess.run(["xdg-open", location])

    def openPanelizer(self, filepath):
        pid = self.pidmap.get(filepath)
        if pid:
            self.bringToFront(pid)
            return
        Thread(target=self._openPanelizer, args=[filepath], daemon=True).start()

    def _openPanelizer(self, filepath):
        p = subprocess.Popen([sys.executable, sys.argv[0], filepath])
        pid = p.pid
        self.pidmap[filepath] = pid
        p.wait()
        self.pidmap.pop(filepath, None)

    def openStep(self, filepath):
        pid = self.pidmap.get(filepath)
        if pid:
            self.bringToFront(pid)
            return
        Thread(target=self._openStep, args=[filepath], daemon=True).start()

    def _openStep(self, filepath):
        cmd = ["open", "-a", "FreeCAD", "-n", "-W", "--args", filepath]
        p = subprocess.Popen(cmd)
        pid = p.pid + 1
        self.pidmap[filepath] = pid
        p.wait()
        self.pidmap.pop(filepath, None)

    def bringToFront(self, pid):
        import platform
        if platform.system() == 'Darwin':
            applescript = f'''
            tell application "System Events"
                set frontmost of every process whose unix id is {pid} to true
            end tell
            '''
            subprocess.run(["osascript", "-e", applescript])
        elif platform.system() == 'Windows':
            subprocess.call(["taskkill", "/PID", str(pid), "/F"])
        else:
            subprocess.call(["xdotool", "windowactivate", "--sync", str(pid)])