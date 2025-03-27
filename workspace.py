import os
import sys
import json
from PUI.PySide6 import *
from PUI.interfaces import BaseTreeAdapter
import PUI
import re
from common import *

class TreeAdapter(BaseTreeAdapter):
    def __init__(self, main, data):
        self.main = main
        self._data = data

    def parent(self, node):
        if node is None:
            return None
        return node["parent"]

    def child(self, parent, index):
        if parent is None:
            return self._data.value["projects"][index]
        else:
            return parent["files"][index]

    def data(self, node):
        return os.path.basename(node["path"])

    def rowCount(self, parent):
        if parent is None:
            return len(self._data.value["projects"])
        return len(parent["files"])

    def clicked(self, node):
        self.main.selectProject(node)

    def dblclicked(self, node):
        self.main.openProject(node["path"])

class WorkspaceUI(Application):
    def __init__(self, filepath=None):
        super().__init__(icon=resource_path("icon.ico"))
        self.filepath = filepath
        self.state = State()
        self.state.focus = None
        self.state.workspace = {"projects": []}
        self.state.adapter = TreeAdapter(self, self.state("workspace"))

    def setup(self):
        if self.filepath is not None:
            if os.path.exists(self.filepath):
                self.loadFile()
            else:
                self.saveFile()
        else:
            filepath = OpenFile("Open/Create Workspace", types=f"Kikakuka Workspace (*.kkkk)|*.kkkk|*.kkkk")
            if filepath:
                self.filepath = filepath
                if os.path.exists(self.filepath):
                    self.loadFile()
                else:
                    self.saveFile()

    def loadFile(self):
        with open(self.filepath, "r") as f:
            self.state.workspace = json.load(f)
        self.findFiles()

    def saveFile(self):
        if self.filepath is None:
            return
        projects = []
        for project in self.state.workspace["projects"]:
            projects.append({
                "path": relpath(project["path"], os.path.dirname(self.filepath)),
                "description": project["description"],
            })
        workspace = {
            "projects": projects
        }
        with open(self.filepath, "w") as f:
            json.dump(workspace, f, indent=4)

    def findFiles(self):
        for project in self.state.workspace["projects"]:
            project["files"] = []
            project["parent"] = None
            if project["path"].endswith(".kikit_pnl"):
                continue
            if project["path"].endswith(".kicad_pro"):
                sch = re.sub(r"\.kicad_pro$", ".kicad_sch", project["path"])
                pcb = re.sub(r"\.kicad_pro$", ".kicad_pcb", project["path"])
                if os.path.exists(sch):
                    project["files"].append({
                        "path": sch,
                        "parent": project,
                        "files": [],
                    })
                if os.path.exists(pcb):
                    project["files"].append({
                        "path": pcb,
                        "parent": project,
                        "files": [],
                    })
        self.state.adapter = TreeAdapter(self, self.state("workspace"))

    def content(self):
        with Window(size=(1300, 768), title=f"Kikakuka (PUI {PUI.__version__} {PUI_BACKEND}))", icon=resource_path("icon.ico")).keypress(self.keypress):
            with VBox():
                with HBox():
                    Button("Add Project/Panelization").click(lambda e: self.addFile())
                    Button("New Panelization")
                    Spacer()

                with HBox():
                    Tree(self.state.adapter).layout(weight=1)

                    with VBox().layout(weight=1):
                        if self.state.focus is not None:
                            with HBox():
                                Label(os.path.basename(self.state.focus["path"]))
                                Spacer()
                                Button("Remove").click(lambda e: self.removeFile())

                        desc = ""
                        if self.state.focus is not None:
                            if "description" in self.state.focus:
                                desc = self.state.focus["description"]
                            else:
                                desc = self.state.focus["parent"]["description"]
                        Text(desc).layout(weight=1)

    def openProject(self, path, new_window=True):
        import subprocess, os, platform
        if platform.system() == 'Darwin':
            cmd = ["open"]
            if new_window:
                cmd.append("-n")
            cmd.append(path)
            subprocess.call(cmd)
        elif platform.system() == 'Windows':
            os.startfile(path)
        else:
            subprocess.call(('xdg-open', path))

    def selectProject(self, node):
        self.state.focus = node

    def addFile(self):
        filepath = OpenFile("Open Project/Panelization", types=f"KiCad Project/Panelization (*.kicad_pro *.kikit_pnl)|*.kicad_pro|*.kikit_pnl")
        if filepath:
            self.state.workspace["projects"].append({
                "path": filepath,
                "description": "",
            })
            orders = [PNL_SUFFIX, ".kicad_pro"]
            self.state.workspace["projects"].sort(key=lambda x: (-indexOf(orders, os.path.splitext(x["path"])[1]), os.path.basename(x["path"])))
            self.findFiles()
            self.saveFile()

    def removeFile(self):
        if Confirm("Are you sure you want to remove this file from the workspace?", "Remove file"):
            self.state.workspace["projects"] = [p for p in self.state.workspace["projects"] if p["path"] != self.state.focus["path"]]
            self.findFiles()
            self.saveFile()
