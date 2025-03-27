import os
import sys
import json
import pywinctl as pwc
from PUI.PySide6 import *
from PUI.interfaces import BaseTreeAdapter
import PUI
import re
from common import resource_path

WORKSPACE_SUFFIX = ".kikakuka"

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

class UI(Application):
    def __init__(self):
        super().__init__(icon=resource_path("icon.ico"))
        self.state = State()
        self.state.workspace = {
            "projects": [
                {
                    "path": "/Users/buganini/repo/cf/esp-dimmer-hw/esp-dimmer-AC/esp-dimmer-AC.kicad_pro",
                    "description": "AC Dimmer",
                },
                {
                    "path": "/Users/buganini/repo/cf/esp-dimmer-hw/esp-dimmer-DC/esp-dimmer-DC.kicad_pro",
                    "description": "DC Dimmer",
                },
                {
                    "path": "/Users/buganini/repo/buganini/usb-dl/panel.kikit_pnl",
                    "description": "DC Dimmer",
                },
            ],
        }
        self.findFiles()

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

    def loadFile(self, path):
        if path.endswith(WORKSPACE_SUFFIX):
            with open(path, "r") as f:
                self.state.workspace = json.load(f)


ui = UI()

inputs = sys.argv[1:]
if inputs:
    if inputs[0].endswith(WORKSPACE_SUFFIX):
        ui.File(inputs[0])

ui.run()
