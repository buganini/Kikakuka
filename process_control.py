"""Process controls shared by Kikakuka's command-line entry point."""

from __future__ import annotations

import os
import sys
from typing import Optional

import psutil

from im.im_mesh import is_kicad_editor_process, owned_process_iter


def _application_for_process(name: str) -> Optional[str]:
    """Classify processes that belong to a supported CAD application."""
    if is_kicad_editor_process(name):
        return "KiCad"
    basename = os.path.basename((name or "").replace("\\", "/"))
    stem, _extension = os.path.splitext(basename)
    stem = stem.casefold()
    if stem.startswith("freecad") and not stem.startswith("freecadcmd"):
        return "FreeCAD"
    return None


def _wait_for_processes(processes, timeout: float):
    if not processes:
        return [], []
    try:
        return psutil.wait_procs(processes, timeout=timeout)
    except (psutil.Error, OSError):
        return [], list(processes)


def kill_all_cad_instances(timeout: float = 3.0) -> int:
    """Stop all same-user KiCad and FreeCAD GUI editor processes.

    Processes first receive a normal termination request. Any that remain after
    *timeout* seconds are killed. Return zero only when every target is gone.
    """
    targets = []
    applications = {}
    for process in owned_process_iter(["pid", "name"]):
        try:
            application = _application_for_process(
                process.info.get("name") or ""
            )
            if application is None:
                continue
            targets.append(process)
            applications[process.pid] = application
        except (psutil.Error, OSError, AttributeError, KeyError, TypeError):
            continue

    if not targets:
        print("No running KiCad or FreeCAD instances found.")
        return 0

    failed = []
    terminating = []
    for process in targets:
        try:
            process.terminate()
            terminating.append(process)
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError) as exc:
            failed.append((process, exc))

    _gone, alive = _wait_for_processes(terminating, timeout)
    killing = []
    for process in alive:
        try:
            process.kill()
            killing.append(process)
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError) as exc:
            failed.append((process, exc))

    _gone, still_alive = _wait_for_processes(killing, timeout)
    for process in still_alive:
        failed.append((process, RuntimeError("process is still running")))

    failed_pids = {process.pid for process, _error in failed}
    stopped = [process for process in targets if process.pid not in failed_pids]
    counts = {
        application: sum(
            applications[process.pid] == application for process in stopped
        )
        for application in ("KiCad", "FreeCAD")
    }
    print(
        "Stopped "
        f"{counts['KiCad']} KiCad and {counts['FreeCAD']} FreeCAD instance(s)."
    )
    for process, error in failed:
        application = applications.get(process.pid, "CAD")
        print(
            f"Could not stop {application} PID {process.pid}: {error}",
            file=sys.stderr,
        )
    return 1 if failed else 0
