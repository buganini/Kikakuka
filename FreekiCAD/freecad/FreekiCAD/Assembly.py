"""Import and export FreekiCAD assembly manifests.

``.kkkk_asm`` files are deliberately plain JSON.  They reference KiCad boards
and external models instead of embedding generated FreeCAD geometry, which keeps
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
MODEL_OBJECT_SETTINGS = {
    "StepObject": ("AutoReload",),
    "StlObject": ("AutoReload", "LengthPerUnit"),
}
CAMERA_TYPES = ("Orthographic", "Perspective")


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


def _field_components(field):
    """Return a Coin vector or rotation field as plain JSON numbers."""
    value = field.getValue()
    if hasattr(value, "getValue"):
        value = value.getValue()
    return [float(component) for component in value]


def _capture_viewport():
    """Return the active 3D camera state, or None outside the GUI."""
    if not getattr(FreeCAD, "GuiUp", False):
        return None
    try:
        import FreeCADGui

        gui_document = FreeCADGui.activeDocument()
        if gui_document is None:
            return None
        view = gui_document.activeView()
        if view is None:
            return None
        camera_type = str(view.getCameraType())
        if camera_type not in CAMERA_TYPES:
            return None
        camera = view.getCameraNode()
        data = {
            "camera_type": camera_type,
            "position": _field_components(camera.position),
            "orientation": _field_components(camera.orientation),
            "near_distance": float(camera.nearDistance.getValue()),
            "far_distance": float(camera.farDistance.getValue()),
            "aspect_ratio": float(camera.aspectRatio.getValue()),
            "focal_distance": float(camera.focalDistance.getValue()),
        }
        if camera_type == "Orthographic":
            data["height"] = float(camera.height.getValue())
        else:
            data["height_angle"] = float(camera.heightAngle.getValue())
        _validate_viewport(data)
        return data
    except Exception as ex:
        console = getattr(FreeCAD, "Console", None)
        if console is not None and hasattr(console, "PrintWarning"):
            console.PrintWarning(
                "FreekiCAD: Could not capture assembly viewport: {}\n".format(
                    ex))
        return None


def _finite_number(value, description, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{} must be a number".format(description))
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("{} must be finite".format(description))
    if positive and value <= 0:
        raise ValueError("{} must be positive".format(description))
    return value


def _number_array(data, name, length):
    value = data.get(name)
    if not isinstance(value, list) or len(value) != length:
        raise ValueError("viewport {} must have {} values".format(
            name, length))
    return [
        _finite_number(component, "viewport {} value".format(name))
        for component in value
    ]


def _validate_viewport(data):
    """Validate and normalize the optional portable camera block."""
    if not isinstance(data, dict):
        raise ValueError("viewport must be an object")
    camera_type = data.get("camera_type")
    if camera_type not in CAMERA_TYPES:
        raise ValueError(
            "viewport camera_type must be Orthographic or Perspective")
    position = _number_array(data, "position", 3)
    orientation = _number_array(data, "orientation", 4)
    if not any(orientation):
        raise ValueError("viewport orientation must not be zero")
    near_distance = _finite_number(
        data.get("near_distance"), "viewport near_distance", positive=True)
    far_distance = _finite_number(
        data.get("far_distance"), "viewport far_distance", positive=True)
    if far_distance <= near_distance:
        raise ValueError(
            "viewport far_distance must be greater than near_distance")
    normalized = {
        "camera_type": camera_type,
        "position": position,
        "orientation": orientation,
        "near_distance": near_distance,
        "far_distance": far_distance,
        "aspect_ratio": _finite_number(
            data.get("aspect_ratio"), "viewport aspect_ratio", positive=True),
        "focal_distance": _finite_number(
            data.get("focal_distance"), "viewport focal_distance",
            positive=True),
    }
    dimension_name = (
        "height" if camera_type == "Orthographic" else "height_angle")
    normalized[dimension_name] = _finite_number(
        data.get(dimension_name), "viewport {}".format(dimension_name),
        positive=True)
    return normalized


def _restore_viewport(document, data):
    """Best-effort restore of viewport fields without affecting geometry."""
    if data is None or not getattr(FreeCAD, "GuiUp", False):
        return False
    if not isinstance(data, dict):
        return False

    def warn(name, ex):
        console = getattr(FreeCAD, "Console", None)
        if console is not None and hasattr(console, "PrintWarning"):
            console.PrintWarning(
                "FreekiCAD: Could not restore viewport {}: {}\n".format(
                    name, ex))

    try:
        import FreeCADGui

        gui_document = FreeCADGui.getDocument(document.Name)
        view = gui_document.activeView()
    except Exception as ex:
        warn("view", ex)
        return False

    applied = False

    def apply(name, callback):
        nonlocal applied
        if name not in data:
            return
        try:
            callback()
            applied = True
        except Exception as ex:
            warn(name, ex)

    def set_camera_type():
        camera_type = data["camera_type"]
        if camera_type not in CAMERA_TYPES:
            raise ValueError("must be Orthographic or Perspective")
        view.setCameraType(camera_type)

    apply("camera_type", set_camera_type)
    try:
        camera = view.getCameraNode()
    except Exception as ex:
        warn("camera", ex)
        return applied

    apply("position", lambda: camera.position.setValue(
        *_number_array(data, "position", 3)))

    def set_orientation():
        orientation = _number_array(data, "orientation", 4)
        if not any(orientation):
            raise ValueError("must not be zero")
        camera.orientation.setValue(*orientation)

    apply("orientation", set_orientation)

    scalar_fields = (
        ("near_distance", "nearDistance"),
        ("far_distance", "farDistance"),
        ("aspect_ratio", "aspectRatio"),
        ("focal_distance", "focalDistance"),
        ("height", "height"),
        ("height_angle", "heightAngle"),
    )
    for source_name, field_name in scalar_fields:
        apply(source_name, lambda source_name=source_name,
              field_name=field_name: getattr(camera, field_name).setValue(
                  _finite_number(
                      data[source_name], "viewport {}".format(source_name),
                      positive=True)))

    if applied and hasattr(view, "redraw"):
        try:
            view.redraw()
        except Exception as ex:
            warn("redraw", ex)
    return applied


def _object_type(obj):
    proxy = getattr(obj, "Proxy", None)
    object_type = getattr(proxy, "Type", None)
    return "PcbObject" if object_type == "LinkedObject" else object_type


def _is_supported_object(obj):
    if str(getattr(obj, "TypeId", "") or "") in (
            "App::Link", "App::LinkElement"):
        return False
    return _object_type(obj) in ("PcbObject", *MODEL_OBJECT_SETTINGS)


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
                     else MODEL_OBJECT_SETTINGS[object_type])
    for name in setting_names:
        if not hasattr(obj, name):
            continue
        value = getattr(obj, name)
        if name == "WedgeMode":
            settings[name] = str(value)
        elif name == "LengthPerUnit":
            settings["LengthPerUnitMM"] = float(
                value.getValueAs("mm").Value)
        else:
            settings[name] = bool(value)
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
            or object_type in MODEL_OBJECT_SETTINGS
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
    viewport = _capture_viewport()
    if viewport is not None:
        data["viewport"] = viewport
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
                "PcbObject", "LinkedObject", *MODEL_OBJECT_SETTINGS):
            raise ValueError("object {} has an unsupported type".format(index))
        source = item.get("file")
        if not isinstance(source, str) or not source:
            raise ValueError("object {} has no file path".format(index))

        if item["type"] in ("PcbObject", "LinkedObject"):
            from .PcbObject import create_pcb_object

            obj = create_pcb_object(document=document, recompute=False)
            setting_names = PCB_OBJECT_SETTINGS
        elif item["type"] == "StepObject":
            from .StepObject import create_step_object

            obj = create_step_object(document=document, recompute=False)
            setting_names = MODEL_OBJECT_SETTINGS["StepObject"]
        else:
            from .StlObject import create_stl_object

            obj = create_stl_object(document=document, recompute=False)
            setting_names = MODEL_OBJECT_SETTINGS["StlObject"]
        settings = item.get("settings", {})
        if not isinstance(settings, dict):
            raise ValueError("object {} settings must be an object".format(index))
        if item["type"] == "StlObject" and "UnitScale" in settings:
            raise ValueError(
                "object {} uses obsolete UnitScale; use "
                "LengthPerUnitMM".format(index))
        for name in setting_names:
            setting_name = (
                "LengthPerUnitMM" if name == "LengthPerUnit" else name)
            if setting_name not in settings or not hasattr(obj, name):
                continue
            value = settings[setting_name]
            if name == "LengthPerUnit":
                if isinstance(value, bool) or not isinstance(
                        value, (int, float)) or value <= 0:
                    raise ValueError(
                        "object {} LengthPerUnitMM must be a positive "
                        "number".format(index))
            setattr(obj, name, value)
        if "placement" in item:
            obj.Placement = _placement_from_json(item["placement"])
        # Set the filename last so PcbObject sees all restored settings when
        # its normal first-load callback runs.
        obj.FileName = _loaded_path(source, filename)
        imported.append(obj)

    if recompute:
        document.recompute()
        _restore_viewport(document, data.get("viewport"))
    return imported


def open(filename):
    """Open a manifest in a newly created FreeCAD document."""
    stem = os.path.splitext(os.path.basename(filename))[0] or "Assembly"
    document = FreeCAD.newDocument(stem)
    insert(filename, document.Name)
    from .cadhoc_client import register_document_source
    register_document_source(document, filename)
    if getattr(FreeCAD, "GuiUp", False):
        from .cadhoc_client import activate_gui_document
        activate_gui_document(document)
    return document
