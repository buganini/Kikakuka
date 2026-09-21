"""KiCad editor operations shared by Kikakuka and FreekiCAD mesh nodes."""

import os
import platform
import re
import subprocess
import tempfile
import time

import psutil

from .im_mesh import (activate_open_freecad_document, bind_freecad_source, launch_lock,
                      local_node, open_in_freecad_node)


EDITOR_NAMES = ("kicad", "pcbnew", "eeschema", "pcb editor")
FREECAD_SUFFIXES = (".fcstd", ".step", ".stp", ".kkkk_asm")


def _normal(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _editors(program="kicad"):
    editors = {}
    try:
        for process in psutil.process_iter(["pid", "name", "create_time"]):
            try:
                name = (process.info["name"] or "").lower()
                match = (name.startswith("freecad") and not name.startswith("freecadcmd")) if program == "freecad" else any(token in name for token in EDITOR_NAMES)
                if match:
                    editors[process.pid] = process.info.get("create_time") or 0
            except (psutil.Error, OSError):
                pass
    except (psutil.Error, OSError):
        pass
    return editors


def _sockets():
    directory = os.path.join(tempfile.gettempdir(), "kicad") if os.name == "nt" else "/tmp/kicad"
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    if os.name == "nt":
        # KiCad/nng may expose the endpoint as a named pipe instead of a
        # directory entry. Keep the canonical ipc:// path for kipy.
        try:
            for pipe in os.listdir(r"\\.\pipe"):
                for candidate in re.findall(r"api(?:-\d+)?\.sock", pipe):
                    names.append(candidate)
        except OSError:
            pass
    editors = _editors()
    explicit = {}
    generic = None
    for name in names:
        match = re.fullmatch(r"api-(\d+)\.sock", name)
        if match and int(match.group(1)) in editors:
            explicit[int(match.group(1))] = os.path.join(directory, name)
        elif name == "api.sock":
            generic = os.path.join(directory, name)
    if generic:
        candidate = next((pid for pid, _ in sorted(editors.items(), key=lambda pair: pair[1])
                          if pid not in explicit), None)
        if candidate is not None:
            explicit[candidate] = generic
    return list(explicit.items())


def _board_path(socket_path):
    from kipy.kicad import KiCad
    from .kicad_api_retry import get_ready_kicad_board

    board = get_ready_kicad_board(KiCad(socket_path=f"ipc://{socket_path}", timeout_ms=1000),
                                  max_retries=0, retry_connection_timeout=True)
    name = getattr(board, "name", "") or getattr(getattr(board, "document", None), "board_filename", "")
    if not name:
        return None
    if os.path.isabs(name):
        return _normal(name)
    project = board.get_project()
    project_path = getattr(project, "path", "")
    return _normal(os.path.join(project_path, name)) if project_path else None


def _find_board(filepath):
    for pid, socket_path in _sockets():
        try:
            if _board_path(socket_path) == filepath:
                return pid, socket_path
        except Exception:
            continue
    return None, None


def _focus(pid):
    if platform.system() == "Darwin":
        try:
            subprocess.run(["osascript", "-e", f'tell application "System Events" to set frontmost of (first process whose unix id is {int(pid)}) to true'],
                           check=False, capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif platform.system() == "Windows":
        # Focus is best-effort; Windows prohibits background foreground-stealing.
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            found = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            @callback_type
            def visit(hwnd, _):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value == pid and user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                    return False
                return True

            user32.EnumWindows(visit, 0)
            if found:
                user32.ShowWindow(found[0], 9)
                user32.SetForegroundWindow(found[0])
        except (OSError, AttributeError):
            pass


def _launch(filepath, program="kicad"):
    before = _editors(program)
    if platform.system() == "Darwin":
        if program == "freecad":
            # FreeCAD on macOS does not reliably handle Finder's open-file
            # event. Pass the path as an application argument, as the former
            # Workspace Manager launcher did.
            subprocess.Popen(["open", "-a", "FreeCAD", "-n", "-W", "--args", filepath])
        else:
            subprocess.Popen(["open", "-n", "-g", filepath])
    elif platform.system() == "Windows":
        os.startfile(filepath)
    else:
        subprocess.Popen(["xdg-open", filepath])
    deadline = time.monotonic() + (20 if program == "freecad" else 8)
    while time.monotonic() < deadline:
        after = _editors(program)
        new = [(pid, start) for pid, start in after.items() if pid not in before]
        if new:
            return max(new, key=lambda item: item[1])[0]
        time.sleep(0.2)
    return None


def _wait_for_board(filepath, timeout=30):
    """Do not release the KiCad launch slot until its board is identifiable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid, socket_path = _find_board(filepath)
        if pid is not None:
            return pid, socket_path
        time.sleep(0.5)
    return None, None


def _open_new(filepath, program, is_board, node):
    """Serialize launches for different files as well as for the same file."""
    with launch_lock(program):
        # The previous launch may have completed while this request waited
        # for the global slot. Recheck before creating another editor.
        if is_board:
            pid, socket_path = _find_board(filepath)
            if pid is not None:
                return pid, socket_path
        elif program == "freecad":
            pid = activate_open_freecad_document(filepath)
            if pid is not None:
                return pid, None
            pid = open_in_freecad_node(filepath)
            if pid is not None:
                return pid, None
        elif node is not None:
            pid = node.snapshot().get(filepath)
            if pid is not None and psutil.pid_exists(pid):
                return pid, None

        pid = _launch(filepath, program)
        if is_board:
            return _wait_for_board(filepath)
        if program == "freecad" and pid is not None:
            if not bind_freecad_source(pid, filepath):
                raise RuntimeError(
                    f"FreeCAD started but did not report the opened document: {filepath}")
        if pid is not None and program == "kicad":
            # Schematics/projects have no board IPC path to probe. Allow the
            # editor's initial socket/process setup to settle before another
            # KiCad launch begins.
            time.sleep(3)
        return pid, None


def handle(request):
    """Handle a legacy editor action; mesh transport is deliberately separate."""
    action = request.get("action")
    filepath = _normal(request.get("filepath", ""))
    if action == "log":
        return {"status": "ok"}
    if action == "list":
        return {"status": "ok", "instances": (local_node().snapshot() if local_node() else {})}
    if action not in {"open-file", "reload", "open-sketch", "move-component",
                      "update-coupler", "monitor-couplers"}:
        return {"status": "error", "message": f"unknown action: {action}"}
    is_freecad = filepath.lower().endswith(FREECAD_SUFFIXES)
    if not filepath.lower().endswith((".kicad_pcb", ".kicad_sch", ".kicad_pro", *FREECAD_SUFFIXES)) or not os.path.isfile(filepath):
        return {"status": "error", "message": f"editor file not found: {filepath}"}
    if is_freecad and action != "open-file":
        return {"status": "error", "message": "FreeCAD file does not expose KiCad IPC"}

    node = local_node()
    mapped = node.snapshot().get(filepath) if node else None
    is_board = filepath.lower().endswith(".kicad_pcb")
    pid, socket_path = _find_board(filepath) if is_board else (None, None)
    if is_board and mapped and pid != mapped and node:
        # A live PID alone does not prove that the editor still has this
        # board open. The KiCad API's filename is authoritative.
        node.publish(filepath, None)
    if pid is None and mapped and psutil.pid_exists(mapped) and not is_board and not is_freecad:
        pid = mapped
    if pid is None and action == "monitor-couplers":
        return {"status": "error", "message": "file is not open in KiCad"}
    if pid is None:
        pid, socket_path = _open_new(
            filepath, "freecad" if is_freecad else "kicad", is_board, node)
        if pid is None:
            message = ("KiCad IPC did not report the requested board within 30 seconds"
                       if is_board else "could not determine editor PID")
            return {"status": "error", "message": message}
    if action == "open-file":
        _focus(pid)
        return {"status": "ok", "action": action, "filepath": filepath, "pid": pid}

    if not is_board:
        return {"status": "error", "message": "KiCad IPC requires a PCB file"}
    deadline = time.monotonic() + 30
    while socket_path is None and time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return {"status": "error", "message": "KiCad editor exited before IPC was ready"}
        time.sleep(0.5)
        verified_pid, socket_path = _find_board(filepath)
        if verified_pid is not None:
            pid = verified_pid
    if not socket_path:
        return {"status": "error", "message": "KiCad IPC did not report the requested board within 30 seconds"}
    reply = {"status": "ok", "action": action, "object": request.get("object", ""),
             "socket": socket_path, "pid": pid}
    if request.get("component"):
        reply["component"] = request["component"]
    return reply
