"""Best-effort inventory of running KiCad and FreeCAD editors."""

import os

import psutil


FILE_SUFFIXES = {
    "KiCad": (".kicad_pcb", ".kicad_sch", ".kicad_pro"),
    "FreeCAD": (".fcstd", ".step", ".stp", ".kkkk_asm"),
}


def update_pidmap_entry(pidmap, filepath, pid):
    """Keep all FreeCAD documents; KiCad's editor tracks one active file."""
    filepath = os.path.abspath(filepath)
    if filepath.lower().endswith(FILE_SUFFIXES["KiCad"]):
        for existing_path, existing_pid in list(pidmap.items()):
            if (existing_pid == pid and existing_path != filepath and
                    existing_path.lower().endswith(FILE_SUFFIXES["KiCad"])):
                pidmap.pop(existing_path, None)
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
        processes = psutil.process_iter(["pid", "name"])
        for process in processes:
            try:
                pid = process.info["pid"]
                name = process.info.get("name")
                program = program_for_process(name)
                if program is None:
                    continue
                paths = _file_paths(tracked_by_pid.get(pid, ()), program)
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
