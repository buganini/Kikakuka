"""Open KiCad files through the on-demand instance mesh."""

import os
import platform
import subprocess

from FreekiCAD.freecad.FreekiCAD import im_mesh

WORKSPACE_OPEN_TIMEOUT = 120.0


def system_open_command(filepath, mac_args=()):
    """Return the POSIX associated-file opener command."""
    system = platform.system()
    if system == "Darwin":
        return ["open", *mac_args, filepath]
    if system == "Windows":
        raise ValueError("Windows uses os.startfile instead")
    return ["xdg-open", filepath]


def open_with_system(filepath):
    """Open a KiCad file using its OS file association."""
    if platform.system() == "Windows":
        os.startfile(filepath)
    else:
        subprocess.Popen(system_open_command(filepath, mac_args=("-n",)))


def request_workspace_open(filepath, timeout=WORKSPACE_OPEN_TIMEOUT):
    """Ask the lowest-PID live node to open/focus this exact file."""
    reply = im_mesh.request({"action": "open-file", "filepath": filepath}, timeout=timeout)
    if reply.get("status") == "error":
        raise RuntimeError(reply.get("message", "instance open failed"))
    if reply.get("status") != "ok" or reply.get("filepath") != filepath:
        raise RuntimeError("instance node returned a mismatched open result")
    return True


def open_kicad_file(filepath):
    """Open a KiCad board, schematic, or project via the instance mesh.

    Returns ``"workspace"`` (mesh) or ``"system"`` to identify the path used.
    This can run in a background thread because the workspace request may
    wait for an editor launch.
    """
    filepath = os.path.abspath(os.fspath(filepath))
    if not filepath.lower().endswith((".kicad_pcb", ".kicad_sch", ".kicad_pro")):
        raise ValueError("expected a KiCad board, schematic, or project file")
    if not os.path.isfile(filepath):
        raise FileNotFoundError(filepath)
    try:
        if request_workspace_open(filepath):
            return "workspace"
    except ConnectionError as exc:
        print(f"Instance mesh unavailable for {filepath}: {exc}")
    open_with_system(filepath)
    return "system"


def open_pcb_file(filepath):
    """PCB-only entry point used by the panelizer."""
    if not os.fspath(filepath).lower().endswith(".kicad_pcb"):
        raise ValueError("expected a .kicad_pcb file")
    return open_kicad_file(filepath)
