"""KiCad editor operations shared by Kikakuka and FreekiCAD mesh nodes."""

import os
import platform
import re
import stat
import subprocess
import tempfile
import time

import psutil

from .im_mesh import (activate_open_freecad_document, bind_freecad_source,
                      launch_lock, local_node, open_in_freecad_node,
                      owned_pid_exists, owned_process, owned_process_iter)
from workspace_monitor import is_kicad_editor_process


FREECAD_SUFFIXES = (".fcstd", ".step", ".stp", ".kkkk_asm")
FRESH_READY_RETRIES = 12
FRESH_READY_DELAY_S = 0.25
SOCKET_OWNER_RETRIES = 4
SOCKET_OWNER_RETRY_DELAY_S = 0.1
WINDOWS_KICAD_API_SENTINEL = (
    b"Kikakuka KiCad IPC compatibility sentinel for issue #23994.\r\n"
)


def _normal(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _kicad_socket_directory():
    return os.path.join(tempfile.gettempdir(), "kicad")


def ensure_windows_kicad_api_sentinel():
    """Force affected Windows builds to choose PID-specific named pipes."""
    if platform.system() != "Windows":
        return None
    directory = _kicad_socket_directory()
    sentinel = os.path.join(directory, "api.sock")
    try:
        os.makedirs(directory, exist_ok=True)
        try:
            mode = os.lstat(sentinel).st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None:
            return sentinel if stat.S_ISREG(mode) else None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(sentinel, flags, 0o600)
        try:
            os.write(descriptor, WINDOWS_KICAD_API_SENTINEL)
        finally:
            os.close(descriptor)
        return sentinel
    except FileExistsError:
        try:
            return sentinel if stat.S_ISREG(os.lstat(sentinel).st_mode) else None
        except OSError:
            return None
    except OSError:
        return None


if platform.system() == "Windows":
    ensure_windows_kicad_api_sentinel()


def _editors(program="kicad"):
    editors = {}
    try:
        for process in owned_process_iter(["pid", "name", "create_time"]):
            try:
                name = (process.info["name"] or "").lower()
                match = (
                    name.startswith("freecad") and not name.startswith("freecadcmd")
                    if program == "freecad"
                    else is_kicad_editor_process(name)
                )
                if match:
                    editors[process.pid] = process.info.get("create_time") or 0
            except (psutil.Error, OSError):
                pass
    except (psutil.Error, OSError):
        pass
    return editors


def _unix_socket_owner(
        socket_path, editors, excluded_pids=(),
        max_retries=SOCKET_OWNER_RETRIES,
        delay_s=SOCKET_OWNER_RETRY_DELAY_S):
    """Return the same-user editor PID that owns a Unix socket path."""
    if platform.system() == "Windows" or not hasattr(os, "geteuid"):
        return None

    target = _normal(socket_path)
    excluded_pids = set(excluded_pids)
    candidates = [
        pair for pair in sorted(editors.items(), key=lambda pair: pair[1])
        if pair[0] not in excluded_pids
    ]
    for attempt in range(max_retries + 1):
        for pid, expected_create_time in candidates:
            try:
                process = owned_process(pid, expected_create_time)
                if process is None:
                    continue
                connections = process.net_connections(kind="unix")
            except (psutil.Error, OSError, RuntimeError, NotImplementedError):
                continue
            for connection in connections:
                local_address = connection.laddr
                if (isinstance(local_address, str) and local_address
                        and _normal(local_address) == target):
                    return pid
        if candidates and attempt < max_retries:
            time.sleep(delay_s)
    return None


def _windows_named_pipe_server_pid(pipe_path):
    """Return a named pipe's server PID via Kernel32, or None on failure."""
    if platform.system() != "Windows" or not pipe_path:
        return None

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        get_server_pid = kernel32.GetNamedPipeServerProcessId
        get_server_pid.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.ULONG),
        ]
        get_server_pid.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_file(pipe_path, 0, 0, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            return None
        try:
            pid = wintypes.ULONG()
            if not get_server_pid(handle, ctypes.byref(pid)):
                return None
            return int(pid.value)
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _windows_named_pipe_owner(pipe_path, editors, excluded_pids=()):
    """Return the validated same-user editor PID serving a named pipe."""
    if platform.system() != "Windows" or not pipe_path:
        return None

    try:
        pid = _windows_named_pipe_server_pid(pipe_path)
        if pid is None or pid in excluded_pids or pid not in editors:
            return None
        expected_create_time = editors[pid]
        if owned_process(pid, expected_create_time) is None:
            return None
        return pid
    except (psutil.Error, OSError, RuntimeError):
        return None


def _sockets():
    is_windows = platform.system() == "Windows"
    directory = _kicad_socket_directory() if is_windows else "/tmp/kicad"
    if is_windows:
        ensure_windows_kicad_api_sentinel()
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    windows_pipe_paths = {}
    if is_windows:
        # KiCad/nng exposes endpoints as named pipes instead of directory
        # entries. Keep the canonical ipc:// filesystem-style path for kipy.
        try:
            for pipe in os.listdir(r"\\.\pipe"):
                for candidate in re.findall(r"api(?:-\d+)?\.sock", pipe):
                    names.append(candidate)
                    windows_pipe_paths.setdefault(
                        candidate, rf"\\.\pipe\{pipe}"
                    )
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
        if is_windows:
            pipe_path = windows_pipe_paths.get("api.sock")
            candidate = (
                _windows_named_pipe_owner(
                    pipe_path, editors, set(explicit)
                ) if pipe_path else None
            )
        else:
            candidate = _unix_socket_owner(
                generic, editors, set(explicit)
            )
        if candidate is None:
            candidate = next((pid for pid, _ in sorted(editors.items(), key=lambda pair: pair[1])
                              if pid not in explicit), None)
        if candidate is not None:
            explicit[candidate] = generic
    return list(explicit.items())


def _ready_board(socket_path, max_retries=0, delay_s=1.0):
    from kipy.kicad import KiCad
    from .kicad_api_retry import get_ready_kicad_board

    return get_ready_kicad_board(
        KiCad(socket_path=f"ipc://{socket_path}", timeout_ms=1000),
        max_retries=max_retries,
        delay_s=delay_s,
        retry_connection_timeout=True,
    )


def _board_path_from_ready_board(board):
    name = getattr(board, "name", "") or getattr(getattr(board, "document", None), "board_filename", "")
    if not name:
        return None
    if os.path.isabs(name):
        return _normal(name)
    project = board.get_project()
    project_path = getattr(project, "path", "")
    return _normal(os.path.join(project_path, name)) if project_path else None


def _board_path(socket_path):
    return _board_path_from_ready_board(_ready_board(socket_path))


def _find_board(filepath, wait_until_ready=False):
    for pid, socket_path in _sockets():
        try:
            board = _ready_board(
                socket_path,
                max_retries=(FRESH_READY_RETRIES if wait_until_ready else 0),
                delay_s=(FRESH_READY_DELAY_S if wait_until_ready else 1.0),
            )
            if _board_path_from_ready_board(board) == filepath:
                return pid, socket_path, board
        except Exception:
            continue
    return None, None, None


def scan_open_kicad_boards():
    """Read open PCB paths and sockets from live KiCad IPC endpoints."""
    boards = []
    for pid, socket_path in _sockets():
        try:
            filepath = _board_path(socket_path)
        except Exception:
            continue
        if filepath:
            boards.append((pid, filepath, socket_path))
    return boards


def _focus(pid):
    if not owned_pid_exists(pid):
        return
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
    if platform.system() == "Windows" and program == "kicad":
        ensure_windows_kicad_api_sentinel()
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
        pid, socket_path, _board = _find_board(filepath)
        if pid is not None:
            return pid, socket_path
        time.sleep(0.5)
    return None, None


def _revert_ready_board(board):
    """Reload an already-ready board using its existing KiCad IPC client."""
    from .kicad_api_retry import probe_kicad_board_ready, retry_kicad_call

    retry_kicad_call(board.revert, retry_connection_timeout=True)
    retry_kicad_call(
        lambda: probe_kicad_board_ready(board),
        retry_connection_timeout=True,
    )


def _open_new(filepath, program, is_board, node, ensure_fresh=False):
    """Serialize launches for different files as well as for the same file."""
    with launch_lock(program):
        # The previous launch may have completed while this request waited
        # for the global slot. Recheck before creating another editor.
        if is_board:
            pid, socket_path, board = _find_board(
                filepath, wait_until_ready=ensure_fresh)
            if pid is not None:
                if ensure_fresh:
                    _revert_ready_board(board)
                return pid, socket_path
        elif program == "freecad":
            pid = activate_open_freecad_document(
                filepath, before_activate=_focus)
            if pid is not None:
                return pid, None
            pid = open_in_freecad_node(filepath, before_open=_focus)
            if pid is not None:
                return pid, None
        elif node is not None:
            pid = node.snapshot().get(filepath)
            if pid is not None and owned_pid_exists(pid):
                return pid, None

        if program == "freecad" and _editors("freecad"):
            raise RuntimeError(
                "FreeCAD is running but its FreekiCAD instance node is unavailable")
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
    if request.get("ensure_fresh") and (action != "open-file" or not is_board):
        return {"status": "error",
                "message": "ensure_fresh requires an open-file PCB request"}
    ensure_fresh = bool(request.get("ensure_fresh"))
    pid, socket_path, board = (
        _find_board(filepath, wait_until_ready=ensure_fresh)
        if is_board else (None, None, None)
    )
    if is_board and mapped and pid != mapped and node:
        # A live PID alone does not prove that the editor still has this
        # board open. The KiCad API's filename is authoritative.
        node.publish(filepath, None)
    if (pid is None and mapped and owned_pid_exists(mapped)
            and not is_board and not is_freecad):
        pid = mapped
    if pid is None and action == "monitor-couplers":
        return {"status": "error", "message": "file is not open in KiCad"}
    if pid is None:
        pid, socket_path = _open_new(
            filepath, "freecad" if is_freecad else "kicad", is_board, node,
            ensure_fresh=ensure_fresh)
        if pid is None:
            message = ("KiCad IPC did not report the requested board within 30 seconds"
                       if is_board else "could not determine editor PID")
            return {"status": "error", "message": message}
    if action == "open-file":
        if ensure_fresh and board is not None:
            _revert_ready_board(board)
        _focus(pid)
        return {"status": "ok", "action": action, "filepath": filepath, "pid": pid}

    if not is_board:
        return {"status": "error", "message": "KiCad IPC requires a PCB file"}
    deadline = time.monotonic() + 30
    while socket_path is None and time.monotonic() < deadline:
        if not owned_pid_exists(pid):
            return {"status": "error", "message": "KiCad editor exited before IPC was ready"}
        time.sleep(0.5)
        verified_pid, socket_path, _board = _find_board(filepath)
        if verified_pid is not None:
            pid = verified_pid
    if not socket_path:
        return {"status": "error", "message": "KiCad IPC did not report the requested board within 30 seconds"}
    reply = {"status": "ok", "action": action, "object": request.get("object", ""),
             "socket": socket_path, "pid": pid}
    if request.get("component"):
        reply["component"] = request["component"]
    return reply
