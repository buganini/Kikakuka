"""Socket daemon for the Kikakuka workspace manager.

Listens on a Unix domain socket (or TCP on Windows) and answers
queries from FreekiCAD (running inside FreeCAD) about which KiCad
IPC socket to use for a given ``.kicad_pcb`` file.
"""

import json
import math
import os
import platform
import socket
import tempfile
import threading
import time

import psutil

try:
    from FreekiCAD.FreekiCAD.kicad_api_retry import is_kicad_retryable_error
except ImportError:
    from FreekiCAD.kicad_api_retry import is_kicad_retryable_error

WORKSPACE_PORT = 19780  # TCP fallback port for Windows


def _timestamp():
    """Return a local timestamp for log lines."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(message):
    print(f"[{_timestamp()}] WorkspaceBus: {message}")


def _normalize_filepath(path):
    """Return a comparable normalized absolute file path."""
    if not path:
        return ""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _socket_board_filepath_state(socket_path, timeout_ms=1000):
    """Probe a KiCad IPC socket and report readiness plus board filepath.

    Returns ``(state, filepath, error_message)`` where:
      - ``state == "ready"`` means KiCad answered and the socket is usable
      - ``state == "not_ready"`` means KiCad exists but is still busy/loading
      - ``state == "unverified"`` means readiness could not be checked and the
        caller should fall back to PID-only resolution
    """
    try:
        from kipy.errors import ApiError, ConnectionError
        from kipy.kicad import KiCad
    except Exception as e:
        _log(f"could not import kipy for socket verification: {e}")
        return "unverified", None, str(e)

    try:
        kicad = KiCad(socket_path=f"ipc://{socket_path}", timeout_ms=timeout_ms)
        board = kicad.get_board()
        board_name = getattr(board, "name", "") or getattr(
            getattr(board, "document", None), "board_filename", ""
        )
        if not board_name:
            return "ready", None, None

        if os.path.isabs(board_name):
            return "ready", os.path.abspath(board_name), None

        project = board.get_project()
        project_path = getattr(project, "path", "")
        if project_path:
            return "ready", os.path.abspath(os.path.join(project_path, board_name)), None

        return "ready", None, None
    except (ConnectionError, ApiError) as e:
        message = str(e)
        if is_kicad_retryable_error(e, retry_connection_timeout=True):
            return "not_ready", None, message
        lowered = message.lower()
        if "expected to be able to retrieve at least one board" in lowered:
            return "not_ready", None, message
        _log(f"could not verify socket {socket_path}: {message}")
        return "unverified", None, message
    except Exception as e:
        message = str(e)
        _log(f"could not verify socket {socket_path}: {message}")
        return "unverified", None, message


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
        if os.path.exists(path):
            return path

    # On Windows, KiCad/nng uses named pipes
    if platform.system() == 'Windows':
        pipes = os.listdir(r'\\.\pipe')
        for name in candidates:
            pipe = os.path.join(sock_dir, name)
            if pipe in pipes:
                return pipe

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
    should return a PID (or ``None``).

    *update_pid* (optional) is called when socket verification discovers
    that a PID is serving a different board path than the cached pidmap key.
    """

    def __init__(self, get_pidmap, open_file=None, remove_pid=None, update_pid=None):
        self._get_pidmap = get_pidmap
        self._open_file = open_file
        self._remove_pid = remove_pid
        self._update_pid = update_pid
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
            _log(f"listening on {sock_path}")
        else:
            # TCP fallback (Windows without AF_UNIX)
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.settimeout(0.5)
            self._server.bind(('127.0.0.1', WORKSPACE_PORT))
            self._server.listen(5)
            self._sock_path = None
            _log(f"listening on tcp://127.0.0.1:{WORKSPACE_PORT}")

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
            _log(f"REQ  {msg}")
            reply = self._handle(msg)
            if reply is not None:
                _log(f"RESP {reply}")
                _send_msg(conn, reply)
        except Exception as e:
            _log(f"ERR  {e}")
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

    def _wait_for_ready_socket(self, pid, timeout=30.0, interval=1.0):
        """Wait for a KiCad IPC socket to appear and answer API requests.

        Returns ``(socket_path, state, filepath, error_message)``.
        """
        deadline = time.time() + timeout
        last_socket_path = None
        last_error = None
        attempt = 0
        max_attempts = max(1, math.ceil(timeout / interval)) if interval > 0 else 1

        while time.time() < deadline:
            attempt += 1
            socket_path = _kicad_socket_for_pid(pid)
            if socket_path is None:
                _log(
                    f"waiting for KiCad IPC socket for PID {pid} "
                    f"(retry {attempt}/{max_attempts}): socket not found yet"
                )
                sleep_s = min(interval, max(0.0, deadline - time.time()))
                if sleep_s > 0:
                    time.sleep(sleep_s)
                continue

            last_socket_path = socket_path
            remaining_s = max(0.25, deadline - time.time())
            timeout_ms = max(250, min(1000, int(remaining_s * 1000)))
            state, actual_filepath, error_message = _socket_board_filepath_state(
                socket_path, timeout_ms=timeout_ms
            )
            if state != "not_ready":
                return socket_path, state, actual_filepath, error_message

            last_error = error_message
            _log(
                f"waiting for KiCad IPC API for PID {pid} at {socket_path} "
                f"(retry {attempt}/{max_attempts}): "
                f"{error_message or 'KiCad not ready'}"
            )
            sleep_s = min(interval, max(0.0, deadline - time.time()))
            if sleep_s > 0:
                time.sleep(sleep_s)

        if last_socket_path is None:
            _log(f"timed out waiting for KiCad IPC socket for PID {pid} after {timeout:.0f}s")
            return None, "missing", None, None
        _log(
            f"timed out waiting for KiCad IPC API for PID {pid} after {timeout:.0f}s: "
            f"{last_error or 'KiCad not ready'}"
        )
        return last_socket_path, "not_ready", None, last_error

    def _verify_socket_filepath(self, requested_filepath, pid, actual_filepath):
        """Verify that the resolved board filepath matches *requested_filepath*.

        Returns ``True`` when the file matches or cannot be verified.
        Returns ``False`` when a mismatch is detected and the pidmap was
        repaired so the caller should retry resolution.
        """
        if not actual_filepath:
            return True

        requested_norm = _normalize_filepath(requested_filepath)
        actual_norm = _normalize_filepath(actual_filepath)
        if requested_norm == actual_norm:
            return True

        _log(
            "socket/path mismatch for PID "
            f"{pid}: requested={requested_filepath}, actual={actual_filepath}"
        )
        if self._remove_pid:
            self._remove_pid(requested_filepath)
        if self._update_pid:
            self._update_pid(actual_filepath, pid)
        return False

    # -- action handlers ------------------------------------------------

    def _resolve_socket(self, msg, pidmap):
        """Resolve the KiCad IPC socket for a file, launching KiCad if needed.

        Returns a response dict with 'action', 'object', 'socket', 'pid',
        and optionally 'component'.
        """
        action = msg.get("action", "reload")
        obj_label = msg.get("object", "")
        component = msg.get("component", "")
        filepath = msg.get("filepath", "")
        for _ in range(3):
            pid = pidmap.get(filepath)

            # Discard stale PID if the process is no longer running
            if pid is not None and not psutil.pid_exists(pid):
                _log(
                    f"PID {pid} is no longer running, removing stale entry for {filepath}"
                )
                if self._remove_pid:
                    self._remove_pid(filepath)
                pid = None
                pidmap = self._get_pidmap()

            if pid is None and self._open_file:
                with self._opening_lock:
                    is_opening = filepath in self._opening

                if is_opening:
                    # Another thread is already opening this file,
                    # wait for it to finish
                    _log(f"waiting for ongoing open: {filepath}")
                    for _ in range(30):  # up to 15 seconds
                        time.sleep(0.5)
                        with self._opening_lock:
                            if filepath not in self._opening:
                                break
                else:
                    # Launch KiCad synchronously
                    _log(f"file not in pidmap, starting KiCad: {filepath}")
                    with self._opening_lock:
                        self._opening.add(filepath)
                    self._do_open_file(filepath)

                # Re-check pidmap after launch/wait
                pidmap = self._get_pidmap()
                pid = pidmap.get(filepath)

            if pid is None:
                return {
                    "status": "error",
                    "message": "file not in workspace",
                }

            # PID is known – find a ready IPC socket
            socket_path, socket_state, actual_filepath, socket_error = (
                self._wait_for_ready_socket(pid)
            )

            if socket_path is None:
                return {
                    "status": "error",
                    "message": f"KiCad running (PID {pid}) but "
                               f"IPC socket not found",
                }

            if socket_state == "not_ready":
                return {
                    "status": "error",
                    "message": (
                        f"KiCad running (PID {pid}) but IPC API was not ready "
                        f"after 30s"
                        + (f": {socket_error}" if socket_error else "")
                    ),
                }

            if not self._verify_socket_filepath(filepath, pid, actual_filepath):
                pidmap = self._get_pidmap()
                continue

            resp = {
                "action": action,
                "object": obj_label,
                "socket": socket_path,
                "pid": pid,
            }
            if component:
                resp["component"] = component
            return resp

        return {
            "status": "error",
            "message": f"could not resolve a matching KiCad socket for {filepath}",
        }

    # -- message handler ------------------------------------------------

    def _handle(self, msg):
        action = msg.get("action")
        pidmap = self._get_pidmap()

        if action in ("reload", "open-sketch", "move-component"):
            return self._resolve_socket(msg, pidmap)

        elif action == "log":
            # Log message from FreekiCAD (fire-and-forget, no response)
            level = msg.get("level", "info")
            source = msg.get("source", "unknown")
            message = msg.get("message", "")
            _log(f"[{source}] {level}: {message}")
            return None

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
