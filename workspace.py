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

    def dblclicked(self, node):
        self.main.openProject(node["path"])

class WorkspaceUI(Application):
    def __init__(self, filepath=None):
        super().__init__(icon=resource_path("icon.ico"))
        self.filepath = filepath
        self.state = State()


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
                "path": project["path"],
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

    def content(self):
        with Window(size=(1300, 768), title=f"Kikakuka (PUI {PUI.__version__} {PUI_BACKEND}))", icon=resource_path("icon.ico")).keypress(self.keypress):
            Tree(TreeAdapter(self, self.state("workspace")))

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
