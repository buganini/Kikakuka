"""Compatibility wrapper for documents saved before PcbObject was renamed."""

if __package__ == "FreekiCAD":
    # The old package name is an alias for freecad.FreekiCAD. Import the
    # implementation under its canonical name so a re-saved document records
    # freecad.FreekiCAD.PcbObject rather than another legacy module path.
    from freecad.FreekiCAD import PcbObject as _implementation
else:
    from . import PcbObject as _implementation


# Old FCStd files reference this module and its former class names.  Re-export
# the complete implementation (including private helpers used by older proxy
# code) while new code imports freecad.FreekiCAD.PcbObject directly.
for _name in dir(_implementation):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_implementation, _name)

LinkedObject = _implementation.PcbObject
LinkedObjectViewProvider = _implementation.PcbObjectViewProvider
create_linked_object = _implementation.create_pcb_object
