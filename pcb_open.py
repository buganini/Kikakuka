"""Open KiCad files through the on-demand instance mesh."""

import os
import platform
import subprocess
import sys

from im import im_mesh

WORKSPACE_OPEN_TIMEOUT = 120.0


def _ensure_instance_node():
    """Make standalone Kikakuka tools participate in the instance mesh."""
    from im.instance_backend import handle

    return im_mesh.start_node(handle)


def system_open_command(filepath, mac_args=()):
    """Return the macOS associated-file opener command."""
    system = platform.system()
    if system == "Darwin":
        return ["open", *mac_args, filepath]
    if system == "Windows":
        raise ValueError("Windows uses os.startfile instead")
    raise ValueError("Linux KiCad files are launched directly")


def open_with_system(filepath, program="kicad"):
    """Open an editor file through the platform-specific launcher."""
    if program not in {"kicad", "freecad"}:
        raise ValueError(f"unsupported editor program: {program}")
    system = platform.system()
    if system == "Windows":
        os.startfile(filepath)
    elif system == "Linux":
        if program == "freecad":
            from im.instance_backend import _launch

            _launch(filepath, "freecad")
        else:
            from im.instance_backend import launch_linux_kicad

            launch_linux_kicad(filepath)
    else:
        subprocess.Popen(system_open_command(filepath, mac_args=("-n",)))


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
    reply = im_mesh.request(request, timeout=timeout)
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


def open_pcb_file(filepath, ensure_fresh=False):
    """PCB-only entry point used by the panelizer."""
    if not os.fspath(filepath).lower().endswith(".kicad_pcb"):
        raise ValueError("expected a .kicad_pcb file")
    return open_kicad_file(filepath, ensure_fresh=ensure_fresh)


def parse_open_arguments(arguments):
    """Parse the top-level ``--open``/``--fresh`` command-line mode.

    Return ``None`` when this is not an open request, otherwise return a
    ``(filepaths, ensure_fresh)`` tuple.  ``--open`` is a mode selector and
    must therefore be the first argument.
    """
    arguments = list(arguments)
    if not arguments or arguments[0] != "--open":
        if "--open" in arguments:
            raise ValueError("--open must be the first argument")
        if "--fresh" in arguments:
            raise ValueError("--fresh requires --open")
        return None
    if "--open" in arguments[1:]:
        raise ValueError("--open may only be specified once")

    unknown = [
        argument for argument in arguments
        if argument.startswith("-")
        and argument not in {"--open", "--fresh"}
    ]
    if unknown:
        raise ValueError(
            f"unknown option for --open: {unknown[0]}")

    filepaths = [
        argument for argument in arguments
        if argument not in {"--open", "--fresh"}
    ]
    if not filepaths:
        raise ValueError("--open requires at least one KiCad file")
    return filepaths, "--fresh" in arguments


def open_requested_kicad_files(arguments):
    """Handle the CLI open mode, returning whether it was selected."""
    request = parse_open_arguments(arguments)
    if request is None:
        return False
    filepaths, ensure_fresh = request
    pcb_paths = {
        filepath for filepath in filepaths
        if os.fspath(filepath).lower().endswith(".kicad_pcb")
    }
    if ensure_fresh and not pcb_paths:
        raise ValueError("--fresh requires at least one .kicad_pcb file")
    for filepath in filepaths:
        open_kicad_file(
            filepath,
            ensure_fresh=ensure_fresh and filepath in pcb_paths,
        )
    return True
