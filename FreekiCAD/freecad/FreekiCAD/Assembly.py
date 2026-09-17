"""Import and export FreekiCAD assembly manifests.

``.kkkk_asm`` files are deliberately plain JSON.  They reference KiCad boards
and STEP models instead of embedding generated FreeCAD geometry, which keeps
the files small and lets each linked object rebuild through its normal path.
"""

import builtins
import json
import math
import os

import FreeCAD


# Runtime/cache properties are intentionally omitted.  They are regenerated
# when a linked PCB is loaded.
PCB_OBJECT_SETTINGS = (
    "AutoReload",
    "SnapToCoupler",
    "EnableBending",
    "ImportOuterCopper",
    "ImportInnerCopper",
    "ImportSolderMask",
    "ImportSilkscreen",
    "BuildDebugObjects",
    "DebugBoard",
    "WedgeMode",
)


def _component(value, lower_name, upper_name):
    if hasattr(value, lower_name):
        return float(getattr(value, lower_name))
    return float(getattr(value, upper_name))


def _resolved_object_path(obj):
    """Resolve a linked path using its FreeCAD document as the base."""
    path = os.path.expanduser(str(getattr(obj, "FileName", "") or ""))
    if not path:
        return ""
    if os.path.isabs(path):
        return os.path.normpath(path)

    document = getattr(obj, "Document", None)
    document_path = str(getattr(document, "FileName", "") or "")
    if document_path:
        return os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(document_path)), path)
        )
    return os.path.abspath(path)


def _stored_path(path, assembly_filename):
    """Prefer a path relative to the manifest, falling back across drives."""
    absolute = os.path.abspath(path)
    base = os.path.dirname(os.path.abspath(assembly_filename))
    try:
        return os.path.relpath(absolute, base)
    except ValueError:
        # relpath raises for paths on different Windows drives.
        return absolute


def _loaded_path(path, assembly_filename):
    path = os.path.expanduser(str(path))
    if os.path.isabs(path):
        return os.path.normpath(path)
    base = os.path.dirname(os.path.abspath(assembly_filename))
    return os.path.normpath(os.path.join(base, path))


def _placement_to_json(placement):
    base = placement.Base
    rotation = placement.Rotation
    axis = rotation.Axis
    return {
        "base": [
            _component(base, "x", "X"),
            _component(base, "y", "Y"),
            _component(base, "z", "Z"),
        ],
        "rotation": {
            "axis": [
                _component(axis, "x", "X"),
                _component(axis, "y", "Y"),
                _component(axis, "z", "Z"),
            ],
            "angle_degrees": math.degrees(float(rotation.Angle)),
        },
    }


def _placement_from_json(data):
    if not isinstance(data, dict):
        raise ValueError("placement must be an object")
    base = data.get("base", [0, 0, 0])
    rotation = data.get("rotation", {})
    axis = rotation.get("axis", [0, 0, 1])
    angle = rotation.get("angle_degrees", 0)
    if len(base) != 3 or len(axis) != 3:
        raise ValueError("placement base and rotation axis must have 3 values")
    return FreeCAD.Placement(
        FreeCAD.Vector(*(float(value) for value in base)),
        FreeCAD.Rotation(
            FreeCAD.Vector(*(float(value) for value in axis)), float(angle)
        ),
    )


def _object_type(obj):
    proxy = getattr(obj, "Proxy", None)
    object_type = getattr(proxy, "Type", None)
    return "PcbObject" if object_type == "LinkedObject" else object_type


def _is_supported_object(obj):
    return _object_type(obj) in ("PcbObject", "StepObject")


def _coupler_poses(obj, coupler_type):
    try:
        poses = json.loads(str(getattr(obj, "CouplerPoses", "") or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(poses, list):
        return []
    return [
        pose
        for pose in poses
        if isinstance(pose, dict) and pose.get("type") == coupler_type
    ]


def _uses_moving_coupler(obj, assembly_objects):
    """Return whether *obj* is positioned by an exported fixed coupler."""
    if not getattr(obj, "SnapToCoupler", True):
        return False
    moving = _coupler_poses(obj, "CouplerMoving")
    origins = _coupler_poses(obj, "CouplerOrigin")
    if len(moving) != 1 or origins:
        return False
    reference = moving[0].get("ref")
    if not reference:
        return False
    return any(
        candidate is not obj
        and any(
            pose.get("ref") == reference
            for pose in _coupler_poses(candidate, "CouplerFixed")
        )
        for candidate in assembly_objects
    )


def _object_to_json(obj, filename, assembly_objects):
    object_type = _object_type(obj)
    source_path = _resolved_object_path(obj)
    if not source_path:
        raise ValueError(
            "cannot export {} {!r} without a FileName".format(
                object_type,
                getattr(obj, "Label", getattr(obj, "Name", ""))
            )
        )
    settings = {}
    setting_names = (PCB_OBJECT_SETTINGS if object_type == "PcbObject"
                     else ("AutoReload",))
    for name in setting_names:
        if not hasattr(obj, name):
            continue
        value = getattr(obj, name)
        settings[name] = str(value) if name == "WedgeMode" else bool(value)
    data = {
        "type": object_type,
        "file": _stored_path(source_path, filename),
        "settings": settings,
    }
    # CouplerMoving placement is derived state.  Omitting it avoids briefly
    # restoring a stale transform before the linked boards finish loading.
    if (object_type == "StepObject"
            or not _uses_moving_coupler(obj, assembly_objects)):
        data["placement"] = _placement_to_json(obj.Placement)
    return data


def export(export_list, filename):
    """Write selected FreekiCAD linked objects to a ``.kkkk_asm`` file."""
    objects = [obj for obj in export_list if _is_supported_object(obj)]
    if not objects:
        raise ValueError("select at least one FreekiCAD linked object to export")

    data = {
        "objects": [
            _object_to_json(obj, filename, objects)
            for obj in objects
        ],
    }
    with builtins.open(filename, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _read(filename):
    with builtins.open(filename, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("FreekiCAD assembly must be a JSON object")
    if not isinstance(data.get("objects"), list):
        raise ValueError("assembly objects must be an array")
    return data


def insert(filename, document_name, recompute=True):
    """Insert all objects from a manifest into an existing document."""
    data = _read(filename)
    document = FreeCAD.getDocument(document_name)
    imported = []
    for index, item in enumerate(data["objects"]):
        if not isinstance(item, dict) or item.get("type") not in (
                "PcbObject", "LinkedObject", "StepObject"):
            raise ValueError("object {} has an unsupported type".format(index))
        source = item.get("file")
        if not isinstance(source, str) or not source:
            raise ValueError("object {} has no file path".format(index))

        if item["type"] in ("PcbObject", "LinkedObject"):
            from .PcbObject import create_pcb_object

            obj = create_pcb_object(document=document, recompute=False)
            setting_names = PCB_OBJECT_SETTINGS
        else:
            from .StepObject import create_step_object

            obj = create_step_object(document=document, recompute=False)
            setting_names = ("AutoReload",)
        settings = item.get("settings", {})
        if not isinstance(settings, dict):
            raise ValueError("object {} settings must be an object".format(index))
        for name in setting_names:
            if name in settings and hasattr(obj, name):
                setattr(obj, name, settings[name])
        if "placement" in item:
            obj.Placement = _placement_from_json(item["placement"])
        # Set the filename last so PcbObject sees all restored settings when
        # its normal first-load callback runs.
        obj.FileName = _loaded_path(source, filename)
        imported.append(obj)

    if recompute:
        document.recompute()
    return imported


def open(filename):
    """Open a manifest in a newly created FreeCAD document."""
    stem = os.path.splitext(os.path.basename(filename))[0] or "Assembly"
    document = FreeCAD.newDocument(stem)
    insert(filename, document.Name)
    return document
