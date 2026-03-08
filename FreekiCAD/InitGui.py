import os
import FreeCAD
import FreeCADGui


class CreateLinkedObjectCommand:
    """Command to create a new LinkedObject."""

    def GetResources(self):
        return {
            "MenuText": "Add KiCad PCB",
            "ToolTip": "Add a linked KiCad PCB object",
        }

    def IsActive(self):
        return FreeCAD.ActiveDocument is not None

    def Activated(self):
        from PySide import QtGui
        from FreekiCAD.LinkedObject import create_linked_object
        filepath, _ = QtGui.QFileDialog.getOpenFileName(
            None, "Select file to link", "", "KiCad PCB (*.kicad_pcb)"
        )
        if filepath:
            create_linked_object(filepath)


class ReloadAllLinkedObjectsCommand:
    """Command to reload all LinkedObjects in the document."""

    def GetResources(self):
        return {
            "MenuText": "Reload All",
            "ToolTip": "Reload all linked KiCad PCB objects",
        }

    def IsActive(self):
        return FreeCAD.ActiveDocument is not None

    def Activated(self):
        doc = FreeCAD.ActiveDocument
        for obj in doc.Objects:
            if hasattr(obj, "Proxy") and hasattr(obj.Proxy, "reload"):
                if hasattr(obj, "FileName") and obj.FileName:
                    obj.Proxy.reload(obj)


class FreekiCADWorkbench(FreeCADGui.Workbench):
    MenuText = "FreekiCAD"
    ToolTip = "Addon for linking external files to objects"

    def Initialize(self):
        self.appendMenu("FreekiCAD", ["CreateLinkedObject", "ReloadAllLinkedObjects"])

    def Activated(self):
        pass

    def Deactivated(self):
        pass


FreeCADGui.addWorkbench(FreekiCADWorkbench)
FreeCADGui.addCommand("CreateLinkedObject", CreateLinkedObjectCommand())
FreeCADGui.addCommand("ReloadAllLinkedObjects", ReloadAllLinkedObjectsCommand())
