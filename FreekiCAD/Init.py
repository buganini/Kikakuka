import FreeCAD

FreeCAD.addImportType(
    "FreekiCAD Assembly (*.kkkk_asm)", "FreekiCAD.Assembly")
FreeCAD.addExportType(
    "FreekiCAD Assembly (*.kkkk_asm)", "FreekiCAD.Assembly")
