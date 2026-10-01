"""Headless FreekiCAD assembly or KiCad PCB to STEP/STL export support."""

import os
import sys

import FreeCAD
import Import

from . import Assembly


PCB_OBJECT_TYPES = ("PcbObject", "LinkedObject")
MODEL_OBJECT_TYPES = ("StepObject", "StlObject")


def _proxy_type(obj):
    return getattr(getattr(obj, "Proxy", None), "Type", None)


def _has_shape(obj):
    shape = getattr(obj, "Shape", None)
    if shape is None:
        return False
    try:
        if shape.isNull():
            return False
    except AttributeError:
        pass
    faces = getattr(shape, "Faces", None)
    if faces is not None and len(faces) == 0:
        return False
    return True


def _has_mesh(obj):
    mesh = getattr(obj, "Mesh", None)
    if mesh is None:
        return False
    try:
        return int(mesh.CountFacets) > 0
    except (AttributeError, TypeError, ValueError):
        return True


def _pcb_export_children(obj):
    """Return physical PCB children, excluding editor/debug markers."""
    export_objects = []
    for child in getattr(obj, "Group", []):
        name = str(getattr(child, "Name", ""))
        child_type = _proxy_type(child)
        if child_type in ("BendLine", "CouplerMarker"):
            continue
        if hasattr(child, "CouplerType"):
            continue
        if (name.endswith("_Outline") or "_Debug" in name
                or name.endswith("_Conflicts")):
            continue
        if _has_shape(child):
            export_objects.append(child)
    return export_objects


def _load_all_objects(objects, document):
    """Synchronously load every manifest object and its linked models."""
    pcb_objects = []
    total = len(objects)
    for index, obj in enumerate(objects, 1):
        object_type = _proxy_type(obj)
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Loading {index}/{total}: {obj.Label}\n")
        if object_type in MODEL_OBJECT_TYPES:
            if not obj.Proxy.reload(obj, force=True):
                raise RuntimeError(
                    f"Could not freshly load {object_type} '{obj.Label}'")
        elif object_type in PCB_OBJECT_TYPES:
            obj.Proxy.reload_sync(obj, reposition=False)
            pcb_objects.append(obj)
        else:
            raise ValueError(
                f"Unsupported assembly object type: {object_type!r}")

    # CouplerMoving placement can depend on a board that appears later in the
    # manifest, so position only after every board and model is ready.
    if pcb_objects:
        pcb_objects[0].Proxy._reposition_all_coupled_objects(document)
    document.recompute()


def _collect_export_objects(objects):
    export_objects = []
    for obj in objects:
        object_type = _proxy_type(obj)
        if object_type == "StepObject":
            if _has_shape(obj):
                export_objects.append(obj)
        elif object_type == "StlObject":
            if _has_mesh(obj):
                export_objects.append(obj)
        elif object_type in PCB_OBJECT_TYPES:
            export_objects.extend(_pcb_export_children(obj))
    return export_objects


def _export_colors(obj, owner=None):
    """Return valid per-face colors retained during synchronous loading."""
    colors = None
    if owner is not None:
        colors = getattr(
            owner.Proxy, "_export_face_colors", {}).get(obj.Name)
    elif _proxy_type(obj) == "StepObject":
        colors = getattr(obj.Proxy, "_export_face_colors", None)
    if not colors:
        return None
    if len(colors) == 1:
        return list(colors) * len(obj.Shape.Faces)
    if len(colors) != len(obj.Shape.Faces):
        return None
    return list(colors)


def _collect_export_sources(objects):
    sources = []
    for obj in objects:
        object_type = _proxy_type(obj)
        if object_type == "StepObject" and _has_shape(obj):
            sources.append((obj, _export_colors(obj)))
        elif object_type == "StlObject" and _has_mesh(obj):
            sources.append((obj, None))
        elif object_type in PCB_OBJECT_TYPES:
            for child in _pcb_export_children(obj):
                sources.append((child, _export_colors(child, owner=obj)))
    return sources


def _source_placement(source):
    if hasattr(source, "getGlobalPlacement"):
        return source.getGlobalPlacement()
    return getattr(source, "Placement", None)


def _flatten_export_items(sources, document, target_suffix=".step"):
    """Create root-level copies with source global placements and colors."""
    export_items = []
    for index, (source, colors) in enumerate(sources, 1):
        placement = _source_placement(source)
        if _has_mesh(source):
            mesh = source.Mesh.copy()
            # Mesh::Feature exposes its transform through both the object and
            # the assigned Mesh.  Normalize the copied mesh before applying
            # the source's global placement, otherwise mesh-to-shape export
            # applies a root object's transform twice.
            if hasattr(mesh, "Placement") and hasattr(FreeCAD, "Placement"):
                mesh.Placement = FreeCAD.Placement()
            if target_suffix in (".step", ".stp"):
                from .StlLoader import _mesh_to_shape

                try:
                    shape = _mesh_to_shape(mesh, require_solid=True)
                except ValueError as exc:
                    raise ValueError(
                        f"STL object '{source.Label}' cannot be exported "
                        f"to STEP: {exc}") from exc
                if placement is not None:
                    shape.Placement = placement
                flat = document.addObject(
                    "Part::Feature", f"Export_{index}_{source.Name}")
                flat.Label = source.Label
                flat.Shape = shape
            else:
                flat = document.addObject(
                    "Mesh::Feature", f"Export_{index}_{source.Name}")
                flat.Label = source.Label
                flat.Mesh = mesh
                if placement is not None:
                    flat.Placement = placement
        else:
            shape = source.Shape.copy()
            if placement is not None:
                shape.Placement = placement
            flat = document.addObject(
                "Part::Feature", f"Export_{index}_{source.Name}")
            flat.Label = source.Label
            flat.Shape = shape
        export_items.append(
            (flat, colors)
            if colors and target_suffix in (".step", ".stp") else flat)
    document.recompute()
    return export_items


def _insert_source_objects(source, document):
    """Create linked objects for a supported headless input file."""
    lower_source = source.lower()
    if lower_source.endswith(".kkkk_asm"):
        # Do not recompute here: that would trigger an implicit linked-file
        # load before the explicit synchronous loading pass below.
        return Assembly.insert(source, document.Name, recompute=False)
    if lower_source.endswith(".kicad_pcb"):
        from .PcbObject import create_pcb_object

        return [create_pcb_object(
            filename=source, document=document, recompute=False)]
    raise ValueError("input must be a .kkkk_asm or .kicad_pcb file")


def export_assembly(source, target):
    """Freshly load *source* and export its complete geometry to *target*."""
    source = os.path.abspath(os.path.expanduser(source))
    target = os.path.abspath(os.path.expanduser(target))
    if not os.path.isfile(source):
        raise FileNotFoundError(source)
    if not source.lower().endswith((".kkkk_asm", ".kicad_pcb")):
        raise ValueError("input must be a .kkkk_asm or .kicad_pcb file")
    target_suffix = os.path.splitext(target)[1].lower()
    if target_suffix not in (".step", ".stp", ".stl"):
        raise ValueError("output must be a .step, .stp, or .stl file")

    document = FreeCAD.newDocument("FreekiCADExport")
    try:
        objects = _insert_source_objects(source, document)
        _load_all_objects(objects, document)
        export_sources = _collect_export_sources(objects)
        if not export_sources:
            raise RuntimeError("input produced no exportable geometry")
        export_items = _flatten_export_items(
            export_sources, document, target_suffix=target_suffix)

        output_directory = os.path.dirname(target)
        if output_directory and not os.path.isdir(output_directory):
            raise FileNotFoundError(output_directory)
        if target_suffix == ".stl":
            import Mesh
            Mesh.export(export_items, target)
        else:
            Import.export(
                export_items, target, legacy=False, keepPlacement=True)
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: Exported {len(export_items)} object(s) to "
            f"'{target}'\n")
    finally:
        FreeCAD.closeDocument(document.Name)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 3:
        FreeCAD.Console.PrintError(
            "Usage: freecadcmd scripts/kkkk_export.py "
            "input.kkkk_asm|input.kicad_pcb output.step|output.stl\n")
        return 2
    source, target = argv[-2:]
    try:
        export_assembly(source, target)
    except Exception as ex:
        FreeCAD.Console.PrintError(f"FreekiCAD: Export failed: {ex}\n")
        return 1
    return 0
