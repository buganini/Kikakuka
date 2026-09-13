"""Reloadable STEP objects for the FreekiCAD workbench."""

import os

import FreeCAD


def _resolved_filename(obj):
    filename = os.path.expanduser(str(getattr(obj, "FileName", "") or ""))
    if not filename:
        return ""
    if os.path.isabs(filename):
        return os.path.normpath(filename)
    document_filename = str(
        getattr(getattr(obj, "Document", None), "FileName", "") or ""
    )
    if document_filename:
        return os.path.normpath(
            os.path.join(
                os.path.dirname(os.path.abspath(document_filename)), filename
            )
        )
    return os.path.abspath(filename)


class StepObject:
    """A Part feature whose shape can be refreshed from a STEP file."""

    def __init__(self, obj):
        obj.addProperty(
            "App::PropertyFile", "FileName", "LinkedFile",
            "Path to the STEP file")
        obj.addProperty(
            "App::PropertyBool", "AutoReload", "LinkedFile",
            "Automatically reload when the file changes")
        obj.AutoReload = True
        obj.addProperty(
            "App::PropertyString", "FileMtime", "LinkedFile",
            "Stored mtime of the linked STEP file")
        obj.setPropertyStatus("FileMtime", "Hidden")
        obj.Proxy = self
        self.Type = "StepObject"
        self._reloading = False

    def onDocumentRestored(self, obj):
        if not hasattr(obj, "AutoReload"):
            obj.addProperty(
                "App::PropertyBool", "AutoReload", "LinkedFile",
                "Automatically reload when the file changes")
            obj.AutoReload = True
        if not hasattr(obj, "FileMtime"):
            obj.addProperty(
                "App::PropertyString", "FileMtime", "LinkedFile",
                "Stored mtime of the linked STEP file")
            obj.setPropertyStatus("FileMtime", "Hidden")
        self._reloading = False

    def onChanged(self, obj, prop):
        if prop != "FileName" or getattr(self, "_reloading", False):
            return
        if getattr(getattr(obj, "Document", None), "Restoring", False):
            return
        if obj.FileName:
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
        if not obj.FileName:
            return
        first_load = not str(getattr(obj, "FileMtime", "") or "")
        if first_load or (obj.AutoReload and self._check_file_changed(obj)):
            self.reload(obj, force=first_load)

    def reload(self, obj, force=False):
        if getattr(self, "_reloading", False):
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
                f"FreekiCAD: Cannot reload STEP '{filename}': {exc}\n")
            return False

        self._reloading = True
        try:
            from FreekiCAD.PcbObject import _load_step, _write_face_colors

            parts = _load_step(filename, obj.Document)
            if not parts:
                FreeCAD.Console.PrintWarning(
                    f"FreekiCAD: STEP load returned no shape: {filename}\n")
                return False
            shape, colors = parts[0]
            obj.Shape = shape
            if colors and len(colors) == len(shape.Faces):
                _write_face_colors(obj.ViewObject, list(colors))
            obj.FileMtime = str(mtime)
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Reloaded STEP '{obj.Label}'.\n")
            return True
        finally:
            self._reloading = False

    def dumps(self):
        return {"Type": self.Type}

    def loads(self, state):
        self.Type = state.get("Type", "StepObject") if state else "StepObject"
        self._reloading = False


class StepObjectViewProvider:
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
        if obj.AutoReload and obj.Proxy._check_file_changed(obj):
            obj.Proxy.reload(obj)

    def setupContextMenu(self, view_object, menu):
        action = menu.addAction("Reload STEP")
        action.triggered.connect(
            lambda: view_object.Object.Proxy.reload(
                view_object.Object, force=True))

    def getIcon(self):
        return ":/icons/Tree_Part.svg"

    def dumps(self):
        return None

    def loads(self, state):
        return None


def create_step_object(filename="", document=None):
    doc = document or FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument()
    label = (os.path.splitext(os.path.basename(filename))[0]
             if filename else "StepObject")
    obj = doc.addObject("Part::FeaturePython", label)
    StepObject(obj)
    StepObjectViewProvider(obj.ViewObject)
    if filename:
        obj.FileName = filename
    doc.recompute()
    return obj
