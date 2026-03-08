import os
import FreeCAD
import FreeCADGui


class CreateLinkedObjectCommand:
    """Command to create a new LinkedObject."""

    def GetResources(self):
        return {
            "MenuText": "Create Linked File",
            "ToolTip": "Create an object linked to an external file",
        }

    def IsActive(self):
        return FreeCAD.ActiveDocument is not None

    def Activated(self):
        from PySide import QtGui
        from FreekiCAD.LinkedObject import create_linked_object
        filepath, _ = QtGui.QFileDialog.getOpenFileName(
            None, "Select file to link", "", "All files (*.*)"
        )
        if filepath:
            create_linked_object(filepath)


class ReloadLinkedObjectCommand:
    """Command to reload a LinkedObject from its file."""

    def GetResources(self):
        return {
            "MenuText": "Reload Linked File",
            "ToolTip": "Reload the selected linked object from its file",
        }

    def IsActive(self):
        sel = FreeCADGui.Selection.getSelection()
        if sel and hasattr(sel[0], "FileName"):
            return True
        return False

    def Activated(self):
        sel = FreeCADGui.Selection.getSelection()
        for obj in sel:
            if hasattr(obj, "Proxy") and hasattr(obj.Proxy, "reload"):
                obj.Proxy.reload(obj)


class FreekiCADWorkbench(FreeCADGui.Workbench):
    MenuText = "FreekiCAD"
    ToolTip = "Addon for linking external files to objects"

    def Initialize(self):
        self.appendMenu("FreekiCAD", ["CreateLinkedObject", "ReloadLinkedObject"])

    def Activated(self):
        pass

    def Deactivated(self):
        pass


FreeCADGui.addWorkbench(FreekiCADWorkbench)
FreeCADGui.addCommand("CreateLinkedObject", CreateLinkedObjectCommand())
FreeCADGui.addCommand("ReloadLinkedObject", ReloadLinkedObjectCommand())
