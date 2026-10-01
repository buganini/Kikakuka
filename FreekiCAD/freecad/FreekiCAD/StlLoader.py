"""Shared STL mesh loading and optional faceted B-Rep conversion."""

import os

import FreeCAD


DEFAULT_SHAPE_TOLERANCE = 0.01


def _mesh_is_empty(mesh):
    """Return whether *mesh* contains no triangles."""
    try:
        return int(mesh.CountFacets) == 0
    except (AttributeError, TypeError, ValueError):
        return False


def _load_stl_mesh(stl_path, scale=1.0):
    """Load an STL as a native FreeCAD mesh.

    STL does not carry units.  FreekiCAD treats one STL coordinate unit as one
    millimetre and applies *scale* explicitly when a different interpretation
    is needed.
    """
    import Mesh

    scale = float(scale)
    if scale <= 0:
        raise ValueError("STL unit scale must be greater than zero")

    mesh = Mesh.Mesh(stl_path)
    if _mesh_is_empty(mesh):
        raise ValueError("STL contains no triangles")
    if scale != 1.0:
        matrix = FreeCAD.Matrix()
        matrix.scale(scale, scale, scale)
        mesh.transform(matrix)
    return mesh


def _mesh_to_shape(mesh, tolerance=DEFAULT_SHAPE_TOLERANCE,
                   require_solid=False):
    """Convert a mesh to a faceted ``Part.Shape``.

    When *require_solid* is true, reject open/non-manifold meshes and build a
    solid for every closed shell.  This is used for STEP export so a broken
    STL cannot silently become an empty or open STEP model.
    """
    import Part

    if _mesh_is_empty(mesh):
        raise ValueError("STL contains no triangles")
    if require_solid and not mesh.isSolid():
        raise ValueError("STL mesh is not closed and manifold")

    shape = Part.Shape()
    shape.makeShapeFromMesh(mesh.Topology, float(tolerance))
    if shape.isNull():
        raise ValueError("STL mesh could not be converted to a Part shape")
    if not require_solid:
        return shape

    shells = list(getattr(shape, "Shells", []) or [])
    if not shells:
        raise ValueError("STL mesh did not produce a closed shell")
    solids = []
    for shell in shells:
        solid = (Part.makeSolid(shell) if hasattr(Part, "makeSolid")
                 else Part.Solid(shell))
        if solid.isNull():
            raise ValueError("STL shell could not be converted to a solid")
        try:
            if solid.Volume < 0:
                solid.complement()
        except (AttributeError, TypeError):
            pass
        solids.append(solid)
    return solids[0] if len(solids) == 1 else Part.makeCompound(solids)


def _load_stl_shape(stl_path, doc=None, cache=None):
    """Load STL as a faceted Part shape for the PCB component pipeline."""
    del doc  # Kept for the same loader signature as ``_load_step``.
    canonical = os.path.realpath(stl_path)
    if cache is not None and canonical in cache:
        shape, colors = cache[canonical]
        return [(shape.copy(), list(colors) if colors else None)]

    try:
        shape = _mesh_to_shape(_load_stl_mesh(stl_path))
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD:   Could not read STL {stl_path}: {exc}\n")
        return []

    if cache is not None:
        cache[canonical] = (shape.copy(), None)
    return [(shape, None)]
