"""Client for querying the Kikakuka workspace manager.

FreekiCAD uses this to resolve which KiCad IPC socket to connect to
for a given .kicad_pcb file.  The workspace manager runs a JSON
daemon on a Unix domain socket (or named pipe on Windows).

Request-response is decoupled: requests are fire-and-forget, and
responses arrive asynchronously on the same connection.  A background
listener thread reads responses and dispatches them to a registered
handler callback on the Qt main thread.
"""

import json
import os
import platform
import socket
import threading
import FreeCAD
from PySide import QtCore

RECV_TIMEOUT = 30.0     # per-request recv timeout (seconds)

# Global response handler callback.
# Set via set_response_handler(callback).
# Called on the Qt main thread with the response dict.
_response_handler = None
_response_handler_lock = threading.Lock()


class _Dispatcher(QtCore.QObject):
    """Helper QObject that lives on the main thread.
    Emitting ``dispatch`` from any thread queues the callback
    for execution on the main thread via Qt's signal/slot mechanism."""
    dispatch = QtCore.Signal(object)

    def __init__(self):
        super().__init__()
        self.dispatch.connect(self._run)

    def _run(self, callback):
        callback()


# Created lazily on the main thread by set_response_handler().
_dispatcher = None


def set_response_handler(handler):
    """Register a callback to handle workspace bus responses.

    *handler* is called on the Qt main thread with a single dict argument
    containing at least ``action``, ``object``, and ``socket`` keys.
    ``component`` is present when the request included one.

    Must be called from the Qt main thread (creates the dispatcher QObject).
    """
    global _response_handler, _dispatcher
    with _response_handler_lock:
        _response_handler = handler
    if _dispatcher is None:
        _dispatcher = _Dispatcher()


def _socket_path(action=None):
    """Return the platform-specific path for the workspace manager socket."""
    if platform.system() == 'Windows':
        return r'\\.\pipe\kikakuka_workspace'
    return '/tmp/kikakuka.sock'


def _connect(action=None):
    """Connect to the workspace manager and return the socket, or None."""
    path = _socket_path(action)
    if platform.system() == 'Windows':
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(RECV_TIMEOUT)
            s.connect(path)
            return s
        except (AttributeError, OSError):
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
    """Send a length-prefixed JSON message."""
    data = json.dumps(msg).encode('utf-8')
    s.sendall(len(data).to_bytes(4, 'big') + data)


def _recv(s):
    """Read a length-prefixed JSON message. Returns dict or None."""
    header = b''
    while len(header) < 4:
        chunk = s.recv(4 - len(header))
        if not chunk:
            return None
        header += chunk
    resp_len = int.from_bytes(header, 'big')
    resp_data = b''
    while len(resp_data) < resp_len:
        chunk = s.recv(resp_len - len(resp_data))
        if not chunk:
            return None
        resp_data += chunk
    return json.loads(resp_data.decode('utf-8'))


def _listener_thread(s):
    """Background thread: read response from server, dispatch to handler."""
    try:
        reply = _recv(s)
        if reply is None:
            return
        FreeCAD.Console.PrintMessage(
            f"FreekiCAD: RESP {reply}\n"
        )
        status = reply.get("status")
        if status == "error":
            FreeCAD.Console.PrintError(
                f"FreekiCAD: Workspace manager error: "
                f"{reply.get('message', 'unknown error')}\n"
            )
            return
        with _response_handler_lock:
            handler = _response_handler
        if handler is not None:
            if _dispatcher is not None:
                _dispatcher.dispatch.emit(lambda: handler(reply))
            else:
                handler(reply)
    except Exception as e:
        FreeCAD.Console.PrintError(
            f"FreekiCAD: Workspace bus listener error: {e}\n"
        )
    finally:
        s.close()


def send_request(action, filepath, object_label="", component=""):
    """Send a request to the workspace manager (fire-and-forget).

    The response will be delivered asynchronously to the handler
    registered via set_response_handler().

    *action*: ``"reload"``, ``"open-sketch"``, or ``"move-component"``
    *filepath*: path to the .kicad_pcb file
    *object_label*: the FreeCAD object label
    *component*: component designator (for move-component)
    """
    msg = {"action": action, "object": object_label, "filepath": filepath}
    if component:
        msg["component"] = component

    FreeCAD.Console.PrintMessage(
        f"FreekiCAD: REQ  {msg}\n"
    )
    s = _connect(action=action)
    if s is None:
        FreeCAD.Console.PrintError(
            "FreekiCAD: Could not connect to Kikakuka workspace manager. "
            "Start the workspace manager first.\n"
        )
        return

    try:
        _send(s, msg)
    except Exception as e:
        FreeCAD.Console.PrintError(
            f"FreekiCAD: Workspace manager send error: {e}\n"
        )
        s.close()
        return

    # Hand off the socket to a listener thread for the response
    threading.Thread(target=_listener_thread, args=(s,), daemon=True).start()


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
