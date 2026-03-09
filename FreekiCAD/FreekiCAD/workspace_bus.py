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

RETRY_INTERVAL = 1.0    # seconds between retries on "pending"
RESOLVE_TIMEOUT = 60.0  # overall deadline (seconds)
RECV_TIMEOUT = 5.0      # per-request recv timeout (seconds)


def _socket_path():
    """Return the platform-specific path for the workspace manager socket."""
    if platform.system() == 'Windows':
        return r'\\.\pipe\kikakuka_workspace'
    return '/tmp/kikakuka.sock'


def _connect():
    """Connect to the workspace manager and return the socket, or None."""
    path = _socket_path()
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


def _send_recv(s, msg):
    """Send a JSON message and receive a JSON response."""
    data = json.dumps(msg).encode('utf-8')
    # Length-prefixed framing: 4-byte big-endian length + payload
    s.sendall(len(data).to_bytes(4, 'big') + data)
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


def resolve_kicad_socket(filepath):
    """Query the Kikakuka workspace manager for the KiCad IPC socket
    path that owns *filepath*.

    If the workspace manager needs to start a new KiCad instance it
    will respond with ``status: pending``.  This function automatically
    retries until the socket is ready or ``RESOLVE_TIMEOUT`` elapses.

    Returns the socket path string (e.g. ``/tmp/kicad/api-12345.sock``)
    on success, or ``None`` on failure.  Progress, errors and warnings
    are printed to the FreeCAD console.
    """
    deadline = time.time() + RESOLVE_TIMEOUT
    msg = {"type": "resolve", "filepath": filepath}

    sock_path = _socket_path()
    FreeCAD.Console.PrintMessage(
        f"FreekiCAD: Workspace bus socket: {sock_path}\n"
    )

    while True:
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: REQ  resolve {filepath}\n"
        )
        s = _connect()
        if s is None:
            FreeCAD.Console.PrintError(
                "FreekiCAD: Could not connect to Kikakuka workspace manager. "
                "Start the workspace manager first.\n"
            )
            return None

        try:
            reply = _send_recv(s, msg)
        except Exception as e:
            FreeCAD.Console.PrintError(
                f"FreekiCAD: Workspace manager error: {e}\n"
            )
            return None
        finally:
            s.close()

        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: RESP {reply}\n"
        )

        status = reply.get("status")

        if status == "ok":
            socket_path = reply["socket"]
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Resolved KiCad socket: {socket_path}\n"
            )
            return socket_path

        if status == "pending":
            action = reply.get("action", "")
            message = reply.get("message", "")
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: Workspace manager: "
                f"{action} – {message}, retrying...\n"
            )
            if time.time() >= deadline:
                FreeCAD.Console.PrintError(
                    "FreekiCAD: Timeout waiting for KiCad socket.\n"
                )
                return None
            time.sleep(RETRY_INTERVAL)
            continue

        # status == "error" or unknown
        FreeCAD.Console.PrintWarning(
            f"FreekiCAD: Workspace manager: "
            f"{reply.get('message', 'unknown error')}\n"
        )
        return None
