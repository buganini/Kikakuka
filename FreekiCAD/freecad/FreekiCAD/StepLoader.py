"""Shared STEP import and per-face colour helpers."""

import os

import FreeCAD
import Part


_DEFAULT_COLOR = (0.8, 0.8, 0.8, 0.0)


def _read_face_colors(vobj, n_faces):
    """Read per-face colors from a ViewObject.

    Tries ShapeAppearance (FreeCAD 1.0+), then DiffuseColor (older),
    then ShapeColor (single colour fallback).
    Returns a list of n_faces (r, g, b, a) tuples.
    """
    if vobj is None:
        return [_DEFAULT_COLOR] * n_faces

    # FreeCAD 1.0+: ShapeAppearance is a list of App.Material per face
    try:
        sa = vobj.ShapeAppearance
        if sa and len(sa) > 0:
            colors = [tuple(m.DiffuseColor) for m in sa]
            if len(colors) == n_faces:
                return colors
            if len(colors) == 1:
                return colors * n_faces
    except (AttributeError, Exception):
        pass

    # Legacy: DiffuseColor
    try:
        dc = list(vobj.DiffuseColor)
        if len(dc) == n_faces:
            return dc
        if dc:
            return dc[:1] * n_faces
    except (AttributeError, Exception):
        pass

    # Single-colour fallback
    try:
        sc = vobj.ShapeColor
        return [tuple(sc) + (0.0,) if len(sc) == 3 else tuple(sc)] * n_faces
    except (AttributeError, Exception):
        pass

    return [_DEFAULT_COLOR] * n_faces


def _write_face_colors(vobj, colors):
    """Write per-face colors to a ViewObject.

    Tries ShapeAppearance (FreeCAD 1.0+), then DiffuseColor,
    then ShapeColor (single-colour fallback).
    """
    if vobj is None or not colors:
        return

    # FreeCAD 1.0+: ShapeAppearance
    try:
        mats = []
        for color in colors:
            material = FreeCAD.Material()
            material.DiffuseColor = color
            mats.append(material)
        vobj.ShapeAppearance = mats
        return
    except (AttributeError, Exception):
        pass

    # Legacy: DiffuseColor
    try:
        vobj.DiffuseColor = colors
        return
    except (AttributeError, Exception):
        pass

    # Single-colour fallback
    try:
        vobj.ShapeColor = colors[0][:3]
    except (AttributeError, Exception):
        pass


def _collect_leaf_colors(obj):
    """Recursively collect per-face colors from leaf children.

    Walks the Group tree in order. Leaf objects (with Shape, no Group)
    contribute their per-face colours. Container objects recurse into
    their Group children. The face order matches the compound shape
    built by FreeCAD for the top-level container.
    """
    skip = {'App::Origin', 'App::Plane', 'App::Line'}
    colors = []
    if hasattr(obj, 'Group') and obj.Group:
        for child in obj.Group:
            if child.TypeId in skip:
                continue
            colors.extend(_collect_leaf_colors(child))
    elif hasattr(obj, 'Shape') and not obj.Shape.isNull():
        n_faces = len(obj.Shape.Faces)
        colors.extend(_read_face_colors(
            getattr(obj, 'ViewObject', None), n_faces))
    return colors


def _obj_colors(obj):
    """Get per-face colors for a single (non-container) object."""
    return _read_face_colors(
        getattr(obj, 'ViewObject', None), len(obj.Shape.Faces))


def _insert_step_merged(import_gui, step_path, document_name):
    """Import STEP independently of the user's global FreeCAD preferences."""
    import_gui.insert(
        name=step_path,
        docName=document_name,
        merge=True,
        useLinkGroup=False,
    )


def _load_step(step_path, doc, cache=None):
    """Load a STEP file and return ``[(shape, colors)]`` or ``[]``.

    Uses ImportGui in a temporary document to get both shape and
    per-face DiffuseColor. Falls back to Part.read() (no colors)
    on failure.

    If *cache* is provided, results are keyed by canonical path.
    """
    canonical = os.path.realpath(step_path)
    if cache is not None and canonical in cache:
        shape, colors = cache[canonical]
        return [(shape.copy(), list(colors) if colors else None)]

    # Strategy 1: ImportGui (shape + colours)
    try:
        import ImportGui
        from PySide import QtCore
        tmp_doc = FreeCAD.newDocument("__FreekiCAD_tmp__")
        try:
            _insert_step_merged(ImportGui, step_path, tmp_doc.Name)
            tmp_doc.recompute()
            # Flush pending events so ViewObjects get their
            # DiffuseColor populated from the STEP colour data.
            QtCore.QCoreApplication.processEvents()

            skip = {'App::Origin', 'App::Plane', 'App::Line'}
            child_names = set()
            for obj in tmp_doc.Objects:
                if hasattr(obj, 'Group'):
                    for child in obj.Group:
                        child_names.add(child.Name)

            shapes = []
            colors = []
            for obj in tmp_doc.Objects:
                if obj.TypeId in skip or obj.Name in child_names:
                    continue
                if not hasattr(obj, 'Shape') or obj.Shape.isNull():
                    continue
                shape = obj.Shape.copy()
                shapes.append(shape)
                n_faces = len(shape.Faces)
                if hasattr(obj, 'Group') and obj.Group:
                    # Container: use parent shape (correct placement)
                    # but collect colors from leaf children.
                    leaf_colors = _collect_leaf_colors(obj)
                    if len(leaf_colors) == n_faces:
                        colors.extend(leaf_colors)
                    else:
                        colors.extend([_DEFAULT_COLOR] * n_faces)
                else:
                    colors.extend(_obj_colors(obj))

            if shapes:
                shape = (shapes[0] if len(shapes) == 1
                         else Part.makeCompound(shapes))
                result_colors = colors if colors else None
                if cache is not None:
                    cache[canonical] = (
                        shape.copy(),
                        list(result_colors) if result_colors else None)
                return [(shape, result_colors)]
        finally:
            FreeCAD.closeDocument(tmp_doc.Name)
    except Exception as ex:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD:   ImportGui failed for {step_path}: {ex}\n")
        try:
            FreeCAD.closeDocument("__FreekiCAD_tmp__")
        except Exception:
            pass

    # Strategy 2: Part.read (shape only, no colours)
    try:
        shape = Part.read(step_path)
        if shape and not shape.isNull():
            if cache is not None:
                cache[canonical] = (shape.copy(), None)
            return [(shape, None)]
    except Exception as ex:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD:   Could not read STEP {step_path}: {ex}\n")
    return []
