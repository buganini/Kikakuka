"""Socket daemon for the Kikakuka workspace manager.

Listens on a Unix domain socket (or TCP on Windows) and answers
queries from FreekiCAD (running inside FreeCAD) about which KiCad
IPC socket to use for a given ``.kicad_pcb`` file.
"""

import json
import os
import platform
import socket
import tempfile
import threading
import time

WORKSPACE_PORT = 19780  # TCP fallback port for Windows


def _is_pid_running(pid):
    """Return True if a process with *pid* is still alive."""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        if platform.system() == 'Windows':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
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
    candidates = [f"api-{pid}.sock", "api.sock"]

    for name in candidates:
        path = os.path.join(sock_dir, name)
        print(f"WorkspaceBus: checking socket: {path} "
              f"exists={os.path.exists(path)}")
        if os.path.exists(path):
            return path

    return None


def _socket_path(action=None):
    """Return the platform-specific path for the workspace manager socket."""
    if platform.system() == 'Windows':
        return None  # Use TCP fallback
    return '/tmp/kikakuka.sock'


def _recv_msg(conn):
    """Read a length-prefixed JSON message from *conn*.
    Returns the decoded dict, or None on connection close."""
    header = b''
    while len(header) < 4:
        chunk = conn.recv(4 - len(header))
        if not chunk:
            return None
        header += chunk
    msg_len = int.from_bytes(header, 'big')
    data = b''
    while len(data) < msg_len:
        chunk = conn.recv(msg_len - len(data))
        if not chunk:
            return None
        data += chunk
    return json.loads(data.decode('utf-8'))


def _send_msg(conn, msg):
    """Send a length-prefixed JSON message over *conn*."""
    data = json.dumps(msg).encode('utf-8')
    conn.sendall(len(data).to_bytes(4, 'big') + data)


class WorkspaceBus:
    """Socket daemon that resolves .kicad_pcb file paths to KiCad
    IPC socket paths.

    Runs a blocking accept loop in a daemon thread.

    *get_pidmap* must be a callable returning ``{filepath: pid, ...}``
    aggregated across all open workspaces.

    *open_file* (optional) is called when a ``resolve`` request arrives
    for a file not yet in the pidmap.  It receives the filepath and
    should return a PID (or ``None``).  It is run in a background thread
    so the server can respond immediately with ``status: pending``.
    """

    def __init__(self, get_pidmap, open_file=None, remove_pid=None):
        self._get_pidmap = get_pidmap
        self._open_file = open_file
        self._remove_pid = remove_pid
        self._opening = set()
        self._opening_lock = threading.Lock()
        self._running = True
        self._server = None

        sock_path = _socket_path()
        if sock_path is not None:
            # Unix domain socket
            # Remove stale socket file
            try:
                os.unlink(sock_path)
            except OSError:
                pass
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.settimeout(0.5)
            self._server.bind(sock_path)
            self._server.listen(5)
            self._sock_path = sock_path
            print(f"WorkspaceBus: listening on {sock_path}")
        else:
            # TCP fallback (Windows without AF_UNIX)
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.settimeout(0.5)
            self._server.bind(('127.0.0.1', WORKSPACE_PORT))
            self._server.listen(5)
            self._sock_path = None
            print(f"WorkspaceBus: listening on tcp://127.0.0.1:{WORKSPACE_PORT}")

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_conn, args=(conn,), daemon=True
            ).start()

    def _handle_conn(self, conn):
        """Handle a single client connection."""
        try:
            conn.settimeout(5.0)
            msg = _recv_msg(conn)
            if msg is None:
                return
            print(f"WorkspaceBus: REQ  {msg}")
            reply = self._handle(msg)
            print(f"WorkspaceBus: RESP {reply}")
            _send_msg(conn, reply)
        except Exception as e:
            print(f"WorkspaceBus: ERR  {e}")
            try:
                _send_msg(conn, {"status": "error", "message": str(e)})
            except Exception:
                pass
        finally:
            conn.close()

    # -- synchronous file opener ----------------------------------------

    def _do_open_file(self, filepath):
        """Open KiCad synchronously (blocks the handler thread).
        Clears the _opening flag when done."""
        try:
            self._open_file(filepath)
        finally:
            with self._opening_lock:
                self._opening.discard(filepath)

    def _wait_for_socket(self, pid, timeout=10.0):
        """Poll for the KiCad IPC socket to appear.
        Returns the socket path or None on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            sock = _kicad_socket_for_pid(pid)
            if sock:
                return sock
            time.sleep(0.5)
        return None

    # -- action handlers ------------------------------------------------

    def _handle_reload(self, msg, pidmap):
        """Handle action=reload: resolve socket, launch KiCad if needed."""
        filepath = msg.get("filepath", "")
        pid = pidmap.get(filepath)

        # Discard stale PID if the process is no longer running
        if pid is not None and not _is_pid_running(pid):
            print(f"WorkspaceBus: PID {pid} is no longer running, "
                  f"removing stale entry for {filepath}")
            if self._remove_pid:
                self._remove_pid(filepath)
            pid = None

        launched = False
        if pid is None and self._open_file:
            with self._opening_lock:
                is_opening = filepath in self._opening

            if is_opening:
                # Another thread is already opening this file,
                # wait for it to finish
                print(f"WorkspaceBus: waiting for ongoing open: "
                      f"{filepath}")
                for _ in range(30):  # up to 15 seconds
                    time.sleep(0.5)
                    with self._opening_lock:
                        if filepath not in self._opening:
                            break
            else:
                # Launch KiCad synchronously
                print(f"WorkspaceBus: file not in pidmap, "
                      f"starting KiCad: {filepath}")
                with self._opening_lock:
                    self._opening.add(filepath)
                self._do_open_file(filepath)
                launched = True

            # Re-check pidmap after launch/wait
            pid = self._get_pidmap().get(filepath)

        if pid is None:
            return {
                "status": "error",
                "message": "file not in workspace",
            }

        # PID is known – find the IPC socket
        socket_path = _kicad_socket_for_pid(pid)
        if socket_path is None:
            socket_path = self._wait_for_socket(pid)

        if socket_path is None:
            return {
                "status": "error",
                "message": f"KiCad running (PID {pid}) but "
                           f"IPC socket not found",
            }

        resp = {
            "action": "reload",
            "socket": socket_path,
            "pid": pid,
        }
        if launched:
            resp["pending"] = True
        return resp

    # -- message handler ------------------------------------------------

    def _handle(self, msg):
        print("_handle", msg)
        action = msg.get("action")
        pidmap = self._get_pidmap()

        if action == "reload":
            return self._handle_reload(msg, pidmap)

        elif action == "log":
            # Log message from FreekiCAD
            level = msg.get("level", "info")
            source = msg.get("source", "unknown")
            message = msg.get("message", "")
            print(f"WorkspaceBus: [{source}] {level}: {message}")
            return {"status": "ok"}

        elif action == "list":
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
            "message": f"unknown action: {action}",
        }

    def shutdown(self):
        self._running = False
        if self._server:
            self._server.close()
            self._server = None
        # Clean up socket file
        if self._sock_path:
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass
