"""ZMQ REP daemon for the Kikakuka workspace manager.

Listens on ``tcp://127.0.0.1:19780`` and answers queries from
FreekiCAD (running inside FreeCAD) about which KiCad IPC socket
to use for a given ``.kicad_pcb`` file.
"""

import json
import os
import platform
import tempfile
import threading

WORKSPACE_PORT = 19780


def _is_pid_running(pid):
    """Return True if a process with *pid* is still alive."""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _kicad_socket_dir():
    """Return the platform-specific directory where KiCad creates IPC sockets.

    - Linux/macOS: ``/tmp/kicad/``
    - Windows:     ``{tempdir}\\kicad\\``  (e.g. ``C:\\Users\\foo\\AppData\\Local\\Temp\\kicad\\``)
    """
    if platform.system() == 'Windows':
        return os.path.join(tempfile.gettempdir(), 'kicad')
    return '/tmp/kicad'


def _kicad_socket_for_pid(pid):
    """Derive the KiCad IPC API socket path from a process *pid*.

    KiCad naming convention:
      - First instance:      ``api.sock``
      - Subsequent instances: ``api-{PID}.sock``

    Returns the path (str) if the socket file exists, else ``None``.
    """
    sock_dir = _kicad_socket_dir()
    specific = os.path.join(sock_dir, f"api-{pid}.sock")
    if os.path.exists(specific):
        return specific
    default = os.path.join(sock_dir, "api.sock")
    if os.path.exists(default):
        return default
    return None


class WorkspaceBus:
    """ZMQ REP daemon that resolves .kicad_pcb file paths to KiCad
    IPC socket paths.

    Runs a blocking recv loop in a daemon thread.

    *get_pidmap* must be a callable returning ``{filepath: pid, ...}``
    aggregated across all open workspaces.

    *open_file* (optional) is called when a ``resolve`` request arrives
    for a file not yet in the pidmap.  It receives the filepath and
    should return a PID (or ``None``).  It is run in a background thread
    so the REP socket can respond immediately with ``status: pending``.
    """

    def __init__(self, get_pidmap, open_file=None, remove_pid=None):
        import zmq

        self._get_pidmap = get_pidmap
        self._open_file = open_file
        self._remove_pid = remove_pid
        self._opening = set()           # filepaths currently being opened
        self._opening_lock = threading.Lock()
        self._ctx = zmq.Context.instance()
        self._rep = self._ctx.socket(zmq.REP)
        self._rep.setsockopt(zmq.RCVTIMEO, 500)  # wake up every 500ms to check _running
        try:
            self._rep.bind(f"tcp://127.0.0.1:{WORKSPACE_PORT}")
            print(f"WorkspaceBus: ZMQ REP listening on port {WORKSPACE_PORT}")
        except zmq.ZMQError as e:
            print(f"WorkspaceBus: Could not bind port {WORKSPACE_PORT}: {e}")
            self._rep.close()
            self._rep = None
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        import zmq
        while self._running:
            try:
                data = self._rep.recv()
            except zmq.Again:
                continue  # timeout, check _running
            except zmq.ZMQError:
                break  # socket closed
            try:
                msg = json.loads(data.decode("utf-8"))
                print(f"WorkspaceBus: REQ  {msg}")
                reply = self._handle(msg)
                print(f"WorkspaceBus: RESP {reply}")
            except Exception as e:
                reply = {"status": "error", "message": str(e)}
                print(f"WorkspaceBus: ERR  {e}")
            try:
                self._rep.send(json.dumps(reply).encode("utf-8"))
            except zmq.ZMQError:
                break

    # -- background file opener -----------------------------------------

    def _do_open_file(self, filepath):
        """Run *open_file* in a background thread and clear the
        _opening flag when done."""
        try:
            self._open_file(filepath)
        finally:
            with self._opening_lock:
                self._opening.discard(filepath)

    # -- message handler ------------------------------------------------

    def _handle(self, msg):
        msg_type = msg.get("type")
        pidmap = self._get_pidmap()

        if msg_type == "resolve":
            filepath = msg.get("filepath", "")
            pid = pidmap.get(filepath)

            # Discard stale PID if the process is no longer running
            if pid is not None and not _is_pid_running(pid):
                print(f"WorkspaceBus: PID {pid} is no longer running, "
                      f"removing stale entry for {filepath}")
                if self._remove_pid:
                    self._remove_pid(filepath)
                pid = None

            if pid is None:
                # Check if we are already opening this file
                with self._opening_lock:
                    is_opening = filepath in self._opening

                if is_opening:
                    return {
                        "status": "pending",
                        "action": "opening",
                        "message": "KiCad is starting",
                    }

                # Try to start a new KiCad instance
                if self._open_file:
                    print(f"WorkspaceBus: file not in pidmap, "
                          f"starting KiCad: {filepath}")
                    with self._opening_lock:
                        self._opening.add(filepath)
                    threading.Thread(
                        target=self._do_open_file,
                        args=(filepath,),
                        daemon=True,
                    ).start()
                    return {
                        "status": "pending",
                        "action": "opening",
                        "message": "starting KiCad instance",
                    }

                return {
                    "status": "error",
                    "message": "file not in workspace",
                }

            # PID is known – check if the IPC socket is ready
            socket_path = _kicad_socket_for_pid(pid)
            if socket_path is None:
                return {
                    "status": "pending",
                    "action": "resolving",
                    "message": f"KiCad running (PID {pid}), "
                               f"resolving IPC socket",
                    "pid": pid,
                }
            return {
                "status": "ok",
                "socket": socket_path,
                "pid": pid,
            }

        elif msg_type == "list":
            instances = {}
            for filepath, pid in pidmap.items():
                sock = _kicad_socket_for_pid(pid)
                if pid not in instances:
                    instances[pid] = {
                        "pid": pid,
                        "socket": sock,
                        "files": [],
                    }
                instances[pid]["files"].append(filepath)
            return {
                "status": "ok",
                "instances": list(instances.values()),
            }

        return {
            "status": "error",
            "message": f"unknown type: {msg_type}",
        }

    def shutdown(self):
        self._running = False
        if self._rep:
            self._rep.close(linger=0)
            self._rep = None
