"""Client for querying the Kikakuka workspace manager.

FreekiCAD uses this to resolve which KiCad IPC socket to connect to
for a given .kicad_pcb file.  The workspace manager runs a JSON
daemon on a Unix domain socket (or named pipe on Windows).
"""

import json
import os
import platform
import socket
import time
import FreeCAD

RECV_TIMEOUT = 30.0     # per-request recv timeout (seconds)


def _socket_path(action=None):
    """Return the platform-specific path for the workspace manager socket."""
    if platform.system() == 'Windows':
        return r'\\.\pipe\kikakuka_workspace'
    return '/tmp/kikakuka.sock'


def _connect(action=None):
    """Connect to the workspace manager and return the socket, or None."""
    path = _socket_path(action)
    if platform.system() == 'Windows':
        # Named pipe on Windows – open as a regular file-like socket
        # Python's socket module doesn't support named pipes directly,
        # so we use AF_UNIX which is available on Windows 10 1803+.
        # Fall back to a TCP localhost connection if AF_UNIX is not available.
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(RECV_TIMEOUT)
            s.connect(path)
            return s
        except (AttributeError, OSError):
            # AF_UNIX not available on older Windows; try TCP fallback
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(RECV_TIMEOUT)
                s.connect(('127.0.0.1', 19780))
                return s
            except OSError:
                return None
    else:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(RECV_TIMEOUT)
            s.connect(path)
            return s
        except OSError:
            return None


def _send(s, msg):
    """Send a JSON message (fire-and-forget)."""
    data = json.dumps(msg).encode('utf-8')
    s.sendall(len(data).to_bytes(4, 'big') + data)


def _send_recv(s, msg):
    """Send a JSON message and receive a JSON response."""
    _send(s, msg)
    # Read response length
    header = b''
    while len(header) < 4:
        chunk = s.recv(4 - len(header))
        if not chunk:
            raise ConnectionError("connection closed")
        header += chunk
    resp_len = int.from_bytes(header, 'big')
    # Read response payload
    resp_data = b''
    while len(resp_data) < resp_len:
        chunk = s.recv(resp_len - len(resp_data))
        if not chunk:
            raise ConnectionError("connection closed")
        resp_data += chunk
    return json.loads(resp_data.decode('utf-8'))


def resolve_kicad_socket(filepath, action="reload"):
    """Query the Kikakuka workspace manager for the KiCad IPC socket
    path that owns *filepath*.

    *action* is ``"reload"`` (launch KiCad if needed, sync board) or
    ``"open-sketch"`` (resolve only, no launch).

    Returns ``(socket_path, None)`` on success,
    ``(None, pending)`` if still pending, or
    ``(None, None)`` on error.
    """
    msg = {"action": action, "filepath": filepath}

    FreeCAD.Console.PrintMessage(
        f"FreekiCAD: REQ  {msg}\n"
    )
    s = _connect(action=action)
    if s is None:
        FreeCAD.Console.PrintError(
            "FreekiCAD: Could not connect to Kikakuka workspace manager. "
            "Start the workspace manager first.\n"
        )
        return None, None

    try:
        reply = _send_recv(s, msg)
    except Exception as e:
        FreeCAD.Console.PrintError(
            f"FreekiCAD: Workspace manager error: {e}\n"
        )
        return None, None
    finally:
        s.close()

    FreeCAD.Console.PrintMessage(
        f"FreekiCAD: RESP {reply}\n"
    )

    status = reply.get("status")
    if status == "error":
        FreeCAD.Console.PrintError(
            f"FreekiCAD: Workspace manager error: "
            f"{reply.get('message', 'unknown error')}\n"
        )
        return None, None

    socket_path = reply.get("socket")
    pending = reply.get("pending")

    if socket_path:
        if pending:
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Socket ready, pending: {pending}\n"
            )
        else:
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Resolved KiCad socket: {socket_path}\n"
            )
        return socket_path, pending

    # No socket in response
    FreeCAD.Console.PrintWarning(
        f"FreekiCAD: Workspace manager: unexpected response: {reply}\n"
    )
    return None, None


def report_error(socket_path, error):
    """Send an error report to the workspace manager when FreekiCAD
    fails to connect to a KiCad API socket.
    Best-effort: silently ignores failures."""
    msg = {
        "action": "log",
        "level": "error",
        "source": "FreekiCAD",
        "message": f"Failed to connect to KiCad API socket: {socket_path}: {error}",
    }
    try:
        s = _connect(action="log")
        if s is None:
            return
        try:
            _send(s, msg)
        finally:
            s.close()
    except Exception:
        pass
