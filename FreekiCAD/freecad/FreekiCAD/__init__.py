"""Headless entry point for the FreekiCAD addon."""

try:
    import FreeCAD
except ImportError:
    FreeCAD = None


if FreeCAD is not None and hasattr(FreeCAD, "addImportType"):
    FreeCAD.addImportType(
        "FreekiCAD linked KiCad PCB (*.kicad_pcb)",
        "freecad.FreekiCAD.PcbImport",
    )
    FreeCAD.addImportType(
        "FreekiCAD Assembly (*.kkkk_asm)",
        "freecad.FreekiCAD.Assembly",
    )
if FreeCAD is not None and hasattr(FreeCAD, "addExportType"):
    FreeCAD.addExportType(
        "FreekiCAD Assembly (*.kkkk_asm)",
        "freecad.FreekiCAD.Assembly",
    )
