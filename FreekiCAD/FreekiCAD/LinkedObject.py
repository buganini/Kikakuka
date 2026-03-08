import os
import FreeCAD
import Part


class LinkedObject:
    """A FeaturePython object that maps an external file to a FreeCAD object.

    Acts like a symbolic link: stores a file path and can be reloaded
    to update the object when the file changes.
    """

    def __init__(self, obj):
        obj.addProperty(
            "App::PropertyFile", "FileName", "LinkedFile",
            "Path to the external file"
        )
        obj.Proxy = self
        self.Type = "LinkedObject"

    def onChanged(self, obj, prop):
        if prop == "FileName":
            self.execute(obj)

    def execute(self, obj):
        """Generate geometry. Currently returns a dummy cube."""
        obj.Shape = Part.makeBox(10, 10, 10)

    def reload(self, obj):
        """Force reload from the linked file."""
        obj.touch()
        obj.Document.recompute()

    def dumps(self):
        return {"Type": self.Type}

    def loads(self, state):
        if state:
            self.Type = state.get("Type", "LinkedObject")


class LinkedObjectViewProvider:
    """ViewProvider for LinkedObject."""

    def __init__(self, vobj):
        vobj.Proxy = self

    def attach(self, vobj):
        self.Object = vobj.Object

    def getIcon(self):
        return ":/icons/Tree_Part.svg"

    def updateData(self, obj, prop):
        pass

    def dumps(self):
        return None

    def loads(self, state):
        return None


def create_linked_object(filename=""):
    doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument()

    obj = doc.addObject("Part::FeaturePython", "LinkedObject")
    LinkedObject(obj)
    LinkedObjectViewProvider(obj.ViewObject)

    if filename:
        obj.FileName = filename

    doc.recompute()
    return obj
