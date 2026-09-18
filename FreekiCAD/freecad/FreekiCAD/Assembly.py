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


class _ExportInstance:
    """One source object at the placement selected for manifest export."""

    __slots__ = ("source", "placement", "snapshot")

    def __init__(self, source, placement, snapshot=False):
        self.source = source
        self.placement = placement
        self.snapshot = bool(snapshot)


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
    if str(getattr(obj, "TypeId", "") or "") in (
            "App::Link", "App::LinkElement"):
        return False
    return _object_type(obj) in ("PcbObject", "StepObject")


def _is_link(obj):
    type_id = str(getattr(obj, "TypeId", "") or "")
    if type_id in ("App::Link", "App::LinkElement"):
        return True
    try:
        return bool(obj.isDerivedFrom("App::Link"))
    except (AttributeError, TypeError, RuntimeError):
        return False


def _linked_object(link):
    target = getattr(link, "LinkedObject", None)
    if isinstance(target, (tuple, list)):
        target = target[0] if target else None
    if target is not None:
        return target
    try:
        return link.getLinkedObject(False)
    except (AttributeError, TypeError, RuntimeError):
        try:
            return link.getLinkedObject()
        except (AttributeError, TypeError, RuntimeError):
            return None


def _global_placement(obj):
    try:
        return obj.getGlobalPlacement()
    except (AttributeError, TypeError, RuntimeError):
        return obj.Placement


def _mapped_placement(rebase, placement):
    return placement if rebase is None else rebase.multiply(placement)


def _container_children(obj):
    try:
        return list(obj.Group)
    except (AttributeError, TypeError, RuntimeError):
        return []


def _collect_instance_node(obj, instances, rebase=None, path=None):
    """Flatten links and containers into positioned source instances.

    ``rebase`` maps an object's native document-global placement into the
    selected link instance.  This is needed when an App::Link points at an
    Assembly or App::Part whose children live in another hierarchy.
    """
    if obj is None:
        return
    path = set() if path is None else set(path)
    identity = id(obj)
    if identity in path:
        return
    path.add(identity)

    if _is_link(obj):
        target = _linked_object(obj)
        if target is None:
            return
        link_global = _mapped_placement(rebase, _global_placement(obj))
        if _is_supported_object(target):
            instances.append(_ExportInstance(target, link_global, True))
            return

        try:
            target_rebase = link_global.multiply(
                _global_placement(target).inverse())
        except (AttributeError, TypeError, RuntimeError):
            return
        _collect_instance_node(
            target, instances, rebase=target_rebase, path=path)
        return

    if _is_supported_object(obj):
        instances.append(_ExportInstance(
            obj, _mapped_placement(rebase, _global_placement(obj)), True))
        return

    for child in _container_children(obj):
        _collect_instance_node(child, instances, rebase=rebase, path=path)


def _collect_export_instances(export_list):
    """Resolve selected sources, links, and assemblies for export.

    Selecting a source object preserves its own Placement and dynamic coupler
    semantics.  Selecting a link or container creates flattened placement
    snapshots of the selected instances instead.
    """
    instances = []
    for obj in export_list:
        if _is_link(obj):
            _collect_instance_node(obj, instances)
        elif _is_supported_object(obj):
            instances.append(_ExportInstance(
                obj, obj.Placement, snapshot=False))
        else:
            _collect_instance_node(obj, instances)
    return instances


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
    absolute_targets = _coupler_poses(obj, "CouplerAt")
    if len(moving) != 1 or absolute_targets:
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


def _object_to_json(instance, filename, assembly_objects):
    obj = instance.source
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
    if instance.snapshot and object_type == "PcbObject":
        # An Assembly/App::Link export is a flattened pose snapshot.  Do not
        # let coupler positioning overwrite that solved instance placement
        # when the manifest is imported.
        settings["SnapToCoupler"] = False
    data = {
        "type": object_type,
        "file": _stored_path(source_path, filename),
        "settings": settings,
    }
    # CouplerMoving placement is derived state.  Omitting it avoids briefly
    # restoring a stale transform before the linked boards finish loading.
    if (instance.snapshot
            or object_type == "StepObject"
            or not _uses_moving_coupler(obj, assembly_objects)):
        data["placement"] = _placement_to_json(instance.placement)
    return data


def export(export_list, filename):
    """Write selected FreekiCAD sources or instances to a manifest."""
    instances = _collect_export_instances(export_list)
    if not instances:
        raise ValueError(
            "select a FreekiCAD linked object, App::Link, or Assembly "
            "containing one to export")

    # Coupler-derived placement omission applies only when source objects are
    # exported directly.  Flattened link instances always carry an explicit
    # placement and disable SnapToCoupler in their own manifest entry.
    assembly_objects = [
        instance.source for instance in instances if not instance.snapshot
    ]

    data = {
        "objects": [
            _object_to_json(instance, filename, assembly_objects)
            for instance in instances
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
