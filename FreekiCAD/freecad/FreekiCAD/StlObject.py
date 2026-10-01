"""Reloadable native STL mesh objects for the FreekiCAD workbench."""

import os

import FreeCAD

from .StepObject import _resolved_filename


class StlObject:
    """A mesh feature whose geometry can be refreshed from an STL file."""

    def __init__(self, obj):
        obj.addProperty(
            "App::PropertyFile", "FileName", "LinkedFile",
            "Path to the STL file")
        obj.addProperty(
            "App::PropertyBool", "AutoReload", "LinkedFile",
            "Automatically reload when the file changes")
        obj.AutoReload = True
        obj.addProperty(
            "App::PropertyLength", "LengthPerUnit", "LinkedFile",
            "Physical length represented by one STL coordinate unit")
        obj.LengthPerUnit = "1 mm"
        obj.addProperty(
            "App::PropertyString", "FileMtime", "LinkedFile",
            "Stored mtime of the linked STL file")
        obj.setPropertyStatus("FileMtime", "Hidden")
        obj.Proxy = self
        self.Type = "StlObject"
        self._reloading = False
        self._last_filename = ""

    def onDocumentRestored(self, obj):
        if not hasattr(obj, "AutoReload"):
            obj.addProperty(
                "App::PropertyBool", "AutoReload", "LinkedFile",
                "Automatically reload when the file changes")
            obj.AutoReload = True
        if not hasattr(obj, "LengthPerUnit"):
            obj.addProperty(
                "App::PropertyLength", "LengthPerUnit", "LinkedFile",
                "Physical length represented by one STL coordinate unit")
            obj.LengthPerUnit = "1 mm"
        if not hasattr(obj, "FileMtime"):
            obj.addProperty(
                "App::PropertyString", "FileMtime", "LinkedFile",
                "Stored mtime of the linked STL file")
            obj.setPropertyStatus("FileMtime", "Hidden")
        self._reloading = False
        self._last_filename = _resolved_filename(obj)

    def onChanged(self, obj, prop):
        if prop not in ("FileName", "LengthPerUnit") or self._reloading:
            return
        if getattr(getattr(obj, "Document", None), "Restoring", False):
            return
        filename = _resolved_filename(obj)
        if prop == "FileName" and filename == self._last_filename:
            return
        self._last_filename = filename
        if prop == "FileName" and obj.FileName:
            obj.Label = os.path.splitext(os.path.basename(obj.FileName))[0]
        if hasattr(obj, "FileMtime"):
            obj.FileMtime = ""

    def _check_file_changed(self, obj):
        filename = _resolved_filename(obj)
        if not filename:
            return False
        try:
            mtime = os.path.getmtime(filename)
        except OSError:
            return False
        try:
            stored_mtime = float(getattr(obj, "FileMtime", ""))
        except (TypeError, ValueError):
            return True
        return mtime != stored_mtime

    def execute(self, obj):
        # Match StepObject: explicit reload/watcher owns external I/O.
        pass

    def reload(self, obj, force=False):
        if self._reloading:
            return False
        filename = _resolved_filename(obj)
        if not filename:
            return False
        if not force and not self._check_file_changed(obj):
            return False

        try:
            mtime = os.path.getmtime(filename)
        except OSError as exc:
            FreeCAD.Console.PrintWarning(
                f"FreekiCAD: Cannot reload STL '{filename}': {exc}\n")
            return False

        self._reloading = True
        try:
            from .StlLoader import _load_stl_mesh

            try:
                mesh = _load_stl_mesh(
                    filename,
                    obj.LengthPerUnit.getValueAs("mm").Value)
            except Exception as exc:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: STL load failed for '{filename}': {exc}\n")
                return False
            # FreeCAD resets a Mesh::Feature's Placement when its Mesh is
            # replaced.  Preserve the linked/assembly transform across a
            # source reload just as StepObject does when replacing Shape.
            placement = getattr(obj, "Placement", None)
            if hasattr(placement, "copy"):
                placement = placement.copy()
            obj.Mesh = mesh
            if placement is not None:
                obj.Placement = placement
            obj.FileMtime = str(mtime)
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Reloaded STL '{obj.Label}'.\n")
            return True
        finally:
            self._reloading = False

    def dumps(self):
        return {"Type": self.Type}

    def loads(self, state):
        self.Type = state.get("Type", "StlObject") if state else "StlObject"
        self._reloading = False


class StlObjectViewProvider:
    def __init__(self, view_object):
        view_object.Proxy = self

    def attach(self, view_object):
        from PySide import QtCore

        self.Object = view_object.Object
        self._auto_reload_timer = QtCore.QTimer()
        self._auto_reload_timer.timeout.connect(
            lambda: self._auto_reload(view_object))
        self._auto_reload_timer.start(2000)

    def _auto_reload(self, view_object):
        try:
            obj = view_object.Object
        except ReferenceError:
            self._auto_reload_timer.stop()
            return
        if getattr(obj.Document, "Restoring", False) or not obj.FileName:
            return
        first_load = not str(getattr(obj, "FileMtime", "") or "")
        if ((first_load or obj.AutoReload)
                and obj.Proxy._check_file_changed(obj)):
            obj.Proxy.reload(obj, force=first_load)

    def setupContextMenu(self, view_object, menu):
        action = menu.addAction("Reload STL")
        action.triggered.connect(
            lambda: view_object.Object.Proxy.reload(
                view_object.Object, force=True))

    def getIcon(self):
        return ":/icons/Tree_Mesh.svg"

    def dumps(self):
        return None

    def loads(self, state):
        return None


def create_stl_object(filename="", document=None, recompute=True):
    doc = document or FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument()
    label = (os.path.splitext(os.path.basename(filename))[0]
             if filename else "StlObject")
    obj = doc.addObject("Mesh::FeaturePython", label)
    StlObject(obj)
    view_object = getattr(obj, "ViewObject", None)
    if getattr(FreeCAD, "GuiUp", False) and view_object is not None:
        StlObjectViewProvider(view_object)
    if filename:
        obj.FileName = filename
    if recompute:
        doc.recompute()
    return obj
