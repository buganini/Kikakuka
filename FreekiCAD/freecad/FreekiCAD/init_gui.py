import os
import FreeCAD
import FreeCADGui


WORKBENCH_DIR = os.path.dirname(os.path.abspath(__file__))
WORKBENCH_ICON = os.path.join(
    WORKBENCH_DIR, "resources", "icons", "FreekiCAD.png")


class CreatePcbObjectCommand:
    """Command to create a new PcbObject."""

    def GetResources(self):
        return {
            "MenuText": "Add KiCad PCB",
            "ToolTip": "Add a linked KiCad PCB object",
        }

    def IsActive(self):
        return FreeCAD.ActiveDocument is not None

    def Activated(self):
        from PySide import QtWidgets
        from .PcbObject import create_pcb_object
        filepath, _ = QtWidgets.QFileDialog.getOpenFileName(
            None, "Select file to link", "", "KiCad PCB (*.kicad_pcb)"
        )
        if filepath:
            create_pcb_object(filepath)


class CreateStepObjectCommand:
    """Command to create a reloadable STEP object."""

    def GetResources(self):
        return {
            "MenuText": "Add STEP",
            "ToolTip": "Add a reloadable linked STEP object",
        }

    def IsActive(self):
        return FreeCAD.ActiveDocument is not None

    def Activated(self):
        from PySide import QtWidgets
        from .StepObject import create_step_object

        filepath, _ = QtWidgets.QFileDialog.getOpenFileName(
            None,
            "Select STEP file to link",
            "",
            "STEP (*.step *.stp *.STEP *.STP)",
        )
        if filepath:
            create_step_object(filepath)


class ReloadAllObjectsCommand:
    """Command to reload all linked objects in the document."""

    def GetResources(self):
        return {
            "MenuText": "Reload All",
            "ToolTip": "Reload all linked KiCad PCB and STEP objects",
        }

    def IsActive(self):
        return FreeCAD.ActiveDocument is not None

    def Activated(self):
        doc = FreeCAD.ActiveDocument
        for obj in list(doc.Objects):
            try:
                if hasattr(obj, "Proxy") and hasattr(obj.Proxy, "reload"):
                    if hasattr(obj, "FileName") and obj.FileName:
                        obj.Proxy.reload(obj, force=True)
            except ReferenceError:
                continue


class FreekiCADWorkbench(FreeCADGui.Workbench):
    MenuText = "FreekiCAD"
    ToolTip = "Addon for linking external files to objects"

    def Initialize(self):
        self.appendMenu(
            "FreekiCAD",
            ["CreatePcbObject", "CreateStepObject", "ReloadAllObjects"],
        )

    def Activated(self):
        pass

    def Deactivated(self):
        pass


FreekiCADWorkbench.Icon = WORKBENCH_ICON
FreeCADGui.addWorkbench(FreekiCADWorkbench)
FreeCADGui.addCommand("CreatePcbObject", CreatePcbObjectCommand())
FreeCADGui.addCommand("CreateStepObject", CreateStepObjectCommand())
FreeCADGui.addCommand("ReloadAllObjects", ReloadAllObjectsCommand())
