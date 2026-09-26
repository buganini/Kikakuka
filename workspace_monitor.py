"""Best-effort inventory of running Kikakuka-supported editors."""

import os

import psutil

from im.im_mesh import owned_process_iter


FILE_SUFFIXES = {
    "KiCad": (".kicad_pcb", ".kicad_sch", ".kicad_pro"),
    "FreeCAD": (".fcstd", ".step", ".stp", ".kkkk_asm"),
    "Fabrication Planner": (".kkkk_fab", ".kikit_pnl"),
}


def update_pidmap_entry(pidmap, filepath, pid):
    """Keep all FreeCAD documents; track one active file for other editors."""
    filepath = os.path.abspath(filepath)
    for program in ("KiCad", "Fabrication Planner"):
        if filepath.lower().endswith(FILE_SUFFIXES[program]):
            for existing_path, existing_pid in list(pidmap.items()):
                if (existing_pid == pid and existing_path != filepath and
                        existing_path.lower().endswith(FILE_SUFFIXES[program])):
                    pidmap.pop(existing_path, None)
            break
    pidmap[filepath] = pid


def replace_freecad_documents(pidmap, pid, filepaths):
    """Reconcile one FreeCAD process from an authoritative document scan."""
    current = {os.path.normcase(os.path.realpath(os.path.abspath(path)))
               for path in filepaths}
    for filepath, mapped_pid in list(pidmap.items()):
        if mapped_pid == pid and os.path.isabs(filepath) and filepath not in current:
            pidmap.pop(filepath, None)
    for filepath in current:
        pidmap[filepath] = pid


def program_for_process(name):
    """Classify GUI editor process names without including CLI tools."""
    name = (name or "").casefold().removesuffix(".exe")
    if (name in ("kicad", "pcbnew", "eeschema", "pcb editor") or
            (name.startswith("kicad-") and
             not name.startswith("kicad-cli"))):
        return "KiCad"
    if name.startswith("freecad") and not name.startswith("freecadcmd"):
        return "FreeCAD"
    return None


def _file_paths(arguments, program, cwd=None):
    paths = set()
    for argument in arguments:
        if not isinstance(argument, str) or not argument.lower().endswith(
            FILE_SUFFIXES[program]
        ):
            continue
        if os.path.isabs(argument):
            paths.add(os.path.abspath(argument))
        elif cwd:
            paths.add(os.path.abspath(os.path.join(cwd, argument)))
    return paths


def snapshot_editor_processes(pidmap):
    """Return sorted (PID, program, path) rows for running GUI editors.

    A blank path means the process was found but its active document could not
    be verified from the workspace map or process command line.
    """
    tracked_by_pid = {}
    for filepath, pid in pidmap.copy().items():
        if isinstance(pid, int) and isinstance(filepath, str):
            tracked_by_pid.setdefault(pid, []).append(filepath)

    rows = []
    try:
        processes = owned_process_iter(["pid", "name"])
        for process in processes:
            try:
                pid = process.info["pid"]
                name = process.info.get("name")
                tracked = tracked_by_pid.get(pid, ())
                panel_paths = _file_paths(
                    tracked, "Fabrication Planner")
                program = (
                    "Fabrication Planner" if panel_paths
                    else program_for_process(name)
                )
                if program is None:
                    continue
                paths = panel_paths or _file_paths(tracked, program)
                if not paths:
                    try:
                        arguments = process.cmdline()
                    except (psutil.Error, OSError):
                        arguments = ()
                    try:
                        cwd = process.cwd()
                    except (psutil.Error, OSError):
                        cwd = None
                    paths = _file_paths((arguments or ())[1:], program, cwd)
                for path in sorted(paths) if paths else ("",):
                    rows.append((pid, program, path))
            except (psutil.Error, OSError, KeyError, TypeError):
                continue
    except (psutil.Error, OSError):
        pass
    return tuple(sorted(rows))
