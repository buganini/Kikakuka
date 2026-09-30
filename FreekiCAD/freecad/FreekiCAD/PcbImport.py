"""FreeCAD importer for linked KiCad PCB files."""

import os

import FreeCAD
from .PcbObject import create_pcb_object


def insert(filename, document_name):
    """Insert a linked PCB object into an existing FreeCAD document."""
    document = FreeCAD.getDocument(document_name)
    obj = create_pcb_object(filename, document=document)
    if getattr(FreeCAD, "GuiUp", False):
        from PySide import QtCore
        view = obj.ViewObject
        provider = view.Proxy
        # Start after the importer returns, without waiting for the 2 s poll.
        # Reuse its guards for deleted objects and already-started reloads.
        QtCore.QTimer.singleShot(0, lambda: provider._auto_reload(view))
    return obj


def open(filename):
    """Open a KiCad PCB as a linked object in a new FreeCAD document."""
    stem = os.path.splitext(os.path.basename(filename))[0] or "PcbObject"
    document = FreeCAD.newDocument(stem)
    insert(filename, document.Name)
    return document
