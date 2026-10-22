"""KiCad and FreeCAD open requests, validation, and offline fallback."""

import os
import sys

from . import mesh

WORKSPACE_OPEN_TIMEOUT = 120.0


def _ensure_instance_node():
    """Make standalone Kikakuka tools participate in the instance mesh."""
    from .instance_backend import handle

    return mesh.start_node(handle)


def open_with_system(filepath, program="kicad"):
    """Delegate fallback file opening to the shared CADhoc backend."""
    from .instance_backend import open_with_system as open_editor

    return open_editor(filepath, program=program)


def request_workspace_open(
    filepath,
    timeout=WORKSPACE_OPEN_TIMEOUT,
    ensure_fresh=False,
    activation_error_handler=None,
):
    """Ask the lowest-PID live node to open/focus this exact file."""
    request = {"action": "open-file", "filepath": filepath}
    if ensure_fresh:
        request["ensure_fresh"] = True
    reply = mesh.request(request, timeout=timeout)
    if reply.get("status") == "error":
        raise RuntimeError(reply.get("message", "instance open failed"))
    if reply.get("status") != "ok" or reply.get("filepath") != filepath:
        raise RuntimeError("instance node returned a mismatched open result")
    activation_error = reply.get("activation_error")
    if activation_error:
        if activation_error_handler is not None:
            activation_error_handler(activation_error)
        else:
            print(f"Could not bring editor to front: {activation_error}",
                  file=sys.stderr)
    return True


def open_kicad_file(filepath, ensure_fresh=False,
                    activation_error_handler=None):
    """Open a KiCad board, schematic, or project via the instance mesh.

    Returns ``"workspace"`` (mesh) or ``"system"`` to identify the path used.
    When *ensure_fresh* is true, an already-open PCB is reloaded from disk
    through the same KiCad IPC connection used to verify it is ready.  This
    can run in a background thread because the workspace request may wait for
    an editor launch.
    """
    filepath = os.path.abspath(os.fspath(filepath))
    if not filepath.lower().endswith((".kicad_pcb", ".kicad_sch", ".kicad_pro")):
        raise ValueError("expected a KiCad board, schematic, or project file")
    if ensure_fresh and not filepath.lower().endswith(".kicad_pcb"):
        raise ValueError("ensure_fresh requires a .kicad_pcb file")
    if not os.path.isfile(filepath):
        raise FileNotFoundError(filepath)
    try:
        _ensure_instance_node()
    except Exception as exc:
        print(f"Instance mesh node unavailable: {exc}")
    try:
        request_options = {"ensure_fresh": ensure_fresh}
        if activation_error_handler is not None:
            request_options["activation_error_handler"] = (
                activation_error_handler)
        if request_workspace_open(filepath, **request_options):
            return "workspace"
    except ConnectionError as exc:
        print(f"Instance mesh unavailable for {filepath}: {exc}")
    open_with_system(filepath)
    return "system"


def open_freecad_file(filepath, activation_error_handler=None):
    """Open through the mesh; never duplicate an unreachable running FreeCAD."""
    from .instance_backend import FREECAD_SUFFIXES, _editors

    filepath = os.path.abspath(os.fspath(filepath))
    if not filepath.lower().endswith(FREECAD_SUFFIXES):
        raise ValueError("expected a FreeCAD document or importable file")
    if not os.path.isfile(filepath):
        raise FileNotFoundError(filepath)
    try:
        _ensure_instance_node()
    except Exception as exc:
        print(f"Instance mesh node unavailable: {exc}")
    try:
        request_workspace_open(filepath, activation_error_handler=activation_error_handler)
        return "workspace"
    except ConnectionError:
        if _editors("freecad"):
            raise ConnectionError("FreeCAD is running but its FreekiCAD instance node cannot be reached")
        open_with_system(filepath, program="freecad")
        return "system"
