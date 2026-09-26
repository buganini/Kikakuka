"""Standalone probes for evaluating KiCad IPC discovery approaches.

This module intentionally does not import the production Instance Manager
discovery code.  It exists only for ``python -m im test`` so the two operating
system techniques can be compared independently across platforms.
"""

import os
import platform
import re
import tempfile

import psutil


KICAD_EDITOR_NAMES = {"kicad", "pcbnew", "eeschema", "pcb editor"}
SOCKET_NAME = re.compile(r"api(?:-(\d+))?\.sock")


def _is_kicad_editor(name):
    basename = os.path.basename(name or "")
    executable, _extension = os.path.splitext(basename)
    return executable.casefold() in KICAD_EDITOR_NAMES


def _same_user_editors():
    """Return current-user KiCad editor processes without inspecting others."""
    editors = {}
    try:
        current = psutil.Process()
        if platform.system() == "Windows":
            owner_field = "username"
            owner = current.username().casefold()
        else:
            owner_field = "uids"
            owner = os.geteuid()
        for process in psutil.process_iter(["pid", owner_field]):
            try:
                process_owner = process.info.get(owner_field)
                matches = (
                    bool(process_owner)
                    and process_owner.casefold() == owner
                    if owner_field == "username"
                    else process_owner is not None
                    and process_owner.effective == owner
                )
                if not matches:
                    continue
                details = process.as_dict(
                    attrs=["pid", "name", "create_time", owner_field]
                )
                if _is_kicad_editor(details.get("name")):
                    editors[process.pid] = process
            except (psutil.Error, OSError, AttributeError, TypeError,
                    ValueError):
                continue
    except (psutil.Error, OSError, AttributeError, TypeError, ValueError):
        pass
    return editors


def _endpoint_inventory():
    """Return ``(name, canonical path, native pipe path)`` endpoint rows."""
    if platform.system() == "Windows":
        directory = os.path.join(tempfile.gettempdir(), "kicad")
        rows = []
        try:
            for pipe in os.listdir(r"\\.\pipe"):
                for match in SOCKET_NAME.finditer(pipe):
                    name = match.group(0)
                    rows.append((name, os.path.join(directory, name),
                                 rf"\\.\pipe\{pipe}"))
        except OSError:
            pass
        return rows

    directory = "/tmp/kicad"
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    return [
        (name, os.path.join(directory, name), None)
        for name in names if SOCKET_NAME.fullmatch(name)
    ]


def enumerated_kicad_sockets():
    """Find PID/socket pairs using endpoint enumeration and names only."""
    editor_pids = set(_same_user_editors())
    found = []
    for name, socket_path, _pipe_path in _endpoint_inventory():
        match = SOCKET_NAME.fullmatch(name)
        encoded_pid = int(match.group(1)) if match and match.group(1) else None
        if encoded_pid is None or encoded_pid in editor_pids:
            found.append((encoded_pid, socket_path))
    return found


def _windows_server_pid(pipe_path):
    """Query a named pipe's server PID without production IM helpers."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        get_server_pid = kernel32.GetNamedPipeServerProcessId
        get_server_pid.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG),
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
            return int(pid.value) if get_server_pid(
                handle, ctypes.byref(pid)
            ) else None
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def owner_kicad_sockets():
    """Find PID/socket pairs using an independent OS ownership query."""
    editors = _same_user_editors()
    found = {}
    if platform.system() == "Windows":
        for _name, socket_path, pipe_path in _endpoint_inventory():
            pid = _windows_server_pid(pipe_path)
            if pid in editors:
                found[pid] = socket_path
        return list(found.items())

    for pid, process in editors.items():
        try:
            connections = process.net_connections(kind="unix")
        except (psutil.Error, OSError, RuntimeError, NotImplementedError,
                ValueError):
            continue
        for connection in connections:
            socket_path = connection.laddr
            if (isinstance(socket_path, str) and socket_path
                    and SOCKET_NAME.fullmatch(os.path.basename(socket_path))):
                found[pid] = socket_path
    return list(found.items())
