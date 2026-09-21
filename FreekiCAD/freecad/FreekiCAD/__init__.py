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

# Register this FreeCAD process before any document is opened, including when
# the workbench has not been activated or FreeCADCmd is used. A missing or
# broken dependency should be reported without breaking FreeCAD's startup.
if FreeCAD is not None:
    try:
        from .im_client import ensure_node
        ensure_node()
    except ImportError as exc:
        if hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintError(
                f"FreekiCAD instance dependencies unavailable: {exc}\n")
    except Exception as exc:
        if hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintError(
                f"FreekiCAD instance node could not start: {exc}\n")
