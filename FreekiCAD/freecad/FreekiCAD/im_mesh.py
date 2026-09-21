"""Per-user, on-demand local IPC mesh for editor instance coordination.

Every host process owns one node. Unix sockets or Windows named pipes provide
discovery and request/reply transport. There is no permanent leader, heartbeat,
or privileged socket scan.
"""

from contextlib import contextmanager
import getpass
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import tempfile
import threading
import time
import uuid

import psutil

from . import im_transport


ACK_TIMEOUT_MS = 1000
RESULT_TIMEOUT = 120.0
POLL_INTERVAL = 0.2
_nodes_lock = threading.RLock()
_local_node = None
_secret_cache = {}
_active_endpoints = set()


def _has_kicad_api():
    try:
        return importlib.util.find_spec("kipy") is not None
    except (ImportError, ValueError):
        return False


def runtime_dir():
    if os.name != "nt":
        # Keep the socket path short enough for macOS's sockaddr_un limit.
        return Path("/tmp") / f"kikakuka-{os.getuid()}"
    # LOCALAPPDATA is stable across independently launched Python runtimes,
    # unlike a process-specific temporary-directory override.
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Kikakuka" / "instances"
    user = getpass.getuser()
    suffix = hashlib.sha256(user.encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"kikakuka-{suffix}"


def _ensure_runtime_dir():
    directory = runtime_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"invalid instance directory: {directory}")
    if os.name != "nt" and info.st_mode & 0o077:
        raise PermissionError(f"insecure instance directory: {directory}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError(f"instance directory has another owner: {directory}")
    return directory


def _started_ms(pid):
    return round(psutil.Process(pid).create_time() * 1000)


def _endpoint_name(pid, started_ms, suffix=""):
    if os.name == "nt":
        scope = hashlib.sha256(
            os.path.normcase(os.path.realpath(str(runtime_dir()))).encode()
        ).hexdigest()[:16]
        return f"kikakuka-{scope}-{pid}-{started_ms}{suffix}"
    return f"{pid}-{started_ms}{suffix}.sock"


def _endpoint(pid, started_ms, suffix=""):
    name = _endpoint_name(pid, started_ms, suffix)
    if os.name == "nt":
        return "\\\\.\\pipe\\" + name
    return str(_ensure_runtime_dir() / name)


def _parse_endpoint(endpoint):
    name = endpoint.rsplit("\\", 1)[-1] if os.name == "nt" else Path(endpoint).name
    if os.name == "nt":
        scope = hashlib.sha256(
            os.path.normcase(os.path.realpath(str(runtime_dir()))).encode()
        ).hexdigest()[:16]
        pattern = rf"kikakuka-{scope}-(\d+)-(\d+)(?:-[0-9a-f]{{8}})?"
    else:
        pattern = r"(\d+)-(\d+)(?:-[0-9a-f]{8})?\.sock"
    match = re.fullmatch(pattern, name)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _alive(endpoint):
    identity = _parse_endpoint(endpoint)
    if identity is None:
        return False
    try:
        return _started_ms(identity[0]) == identity[1]
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError):
        return None


def _remove_dead_unix_socket(endpoint):
    if os.name == "nt":
        return
    path = Path(endpoint)
    try:
        before = path.lstat()
        if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid():
            return
        after = path.lstat()
        if (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino):
            path.unlink()
    except OSError:
        pass


def _candidate_endpoints():
    if os.name != "nt":
        return [str(path) for path in _ensure_runtime_dir().glob("*.sock")]
    try:
        names = os.listdir(r"\\.\pipe")
        enumerated = True
    except OSError:
        names = []
        enumerated = False
    endpoints = ["\\\\.\\pipe\\" + name for name in names
                 if _parse_endpoint(name) is not None]
    with _nodes_lock:
        endpoints.extend(_active_endpoints)
    if enumerated and endpoints:
        return list(dict.fromkeys(endpoints))
    # Pipe namespace enumeration is not guaranteed by the public Win32 API.
    # Fall back to deterministic names derived from ordinary process data.
    for process in psutil.process_iter(["pid", "create_time"]):
        try:
            endpoints.append(_endpoint(process.pid,
                                       round(process.info["create_time"] * 1000)))
        except (psutil.Error, OSError, TypeError, ValueError):
            pass
    return list(dict.fromkeys(endpoints))


def _shared_token():
    path = _ensure_runtime_dir() / "mesh-token"
    cached = _secret_cache.get(path)
    if cached is not None:
        return cached
    with _file_lock("mesh-token"):
        if path.exists():
            token = path.read_text(encoding="ascii")
        else:
            token = secrets.token_hex(32)
            with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                           "w", encoding="ascii") as stream:
                stream.write(token)
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ValueError(f"invalid instance token file: {path}")
        _secret_cache[path] = token
        return token


def discover():
    """Probe local IPC endpoints on demand, ordered by PID and node ID."""
    result = []
    for endpoint in _candidate_endpoints():
        if _alive(endpoint) is False:
            _remove_dead_unix_socket(endpoint)
            continue
        try:
            reply = _exchange(endpoint, {"mesh_action": "hello"}, 300)
        except (OSError, ConnectionError, TimeoutError, ValueError):
            # A timeout may mean a live but busy node. Only a refused Unix
            # connection is sufficient evidence to unlink a socket.
            if os.name != "nt" and _unix_refused(endpoint):
                _remove_dead_unix_socket(endpoint)
            continue
        identity = _parse_endpoint(endpoint)
        if (isinstance(reply, dict) and reply.get("status") == "ok" and
                reply.get("version") == 3 and
                reply.get("pid") == identity[0] and
                reply.get("started_ms") == identity[1]):
            result.append(dict(reply, endpoint=endpoint))
    return sorted(result, key=lambda item: (item["pid"], item["id"]))


def _unix_refused(endpoint):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            probe.connect(endpoint)
            return False
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


def _exchange(endpoint, message, timeout_ms=ACK_TIMEOUT_MS, token=None):
    """One bounded JSON request on a fresh local connection."""
    with im_transport.connect(endpoint, timeout_ms) as connection:
        connection.send(dict(message, token=_shared_token() if token is None else token))
        return connection.receive(timeout_ms)


@contextmanager
def _file_lock(filepath):
    """Cross-process lock, retained across elected-node failover.

    Lock files are deliberately not unlinked: unlinking a lock with waiters
    can create two distinct locks for the same path.
    """
    name = hashlib.sha256(filepath.encode("utf-8")).hexdigest() + ".lock"
    path = _ensure_runtime_dir() / name
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def launch_lock(program):
    """Serialize process discovery and editor startup across all mesh nodes.

    Per-file locks prevent opening one file twice; this separate lock prevents
    two *different* files from racing through process-list PID discovery and
    KiCad's initial IPC socket setup.
    """
    with _file_lock(f"launch:{program}"):
        yield


class InstanceNode:
    def __init__(self, handle, on_change=None, kicad_api=None):
        self.handle = handle
        self.on_change = on_change
        self.id = uuid.uuid4().hex
        self.token = _shared_token()
        self.pid = os.getpid()
        self.started_ms = _started_ms(self.pid)
        self.endpoint = _endpoint(self.pid, self.started_ms)
        if os.name != "nt" and Path(self.endpoint).exists():
            if _unix_refused(self.endpoint):
                _remove_dead_unix_socket(self.endpoint)
            else:
                self.endpoint = _endpoint(self.pid, self.started_ms,
                                          "-" + self.id[:8])
        with _nodes_lock:
            if self.endpoint in _active_endpoints:
                # Tests can host multiple nodes in one process. Production
                # uses one node per process and the deterministic base name.
                self.endpoint = _endpoint(self.pid, self.started_ms,
                                          "-" + self.id[:8])
            _active_endpoints.add(self.endpoint)
        self.kicad_api = _has_kicad_api() if kicad_api is None else bool(kicad_api)
        self._lock = threading.RLock()
        self._results = {}
        self._result_finished = {}
        self._mappings = {}
        self._running = True
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            with _nodes_lock:
                _active_endpoints.discard(self.endpoint)
            raise RuntimeError("instance node failed to start")
        if getattr(self, "_error", None):
            with _nodes_lock:
                _active_endpoints.discard(self.endpoint)
            raise self._error
        try:
            self.refresh()
        except Exception:
            self.close()
            raise

    def _serve(self):
        listener = None
        try:
            listener = im_transport.Listener(self.endpoint)
            self._ready.set()
            while self._running:
                connection = listener.accept()
                if connection is None:
                    continue
                with connection:
                    try:
                        request = connection.receive(1000)
                        reply = self._receive(request)
                    except Exception as exc:
                        reply = {"status": "error", "message": str(exc)}
                    try:
                        connection.send(reply)
                    except (OSError, ConnectionError):
                        pass
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            if listener is not None:
                listener.close()

    def _receive(self, request):
        if request.get("token") != self.token:
            return {"status": "error", "message": "unauthorized instance request"}
        action = request.get("mesh_action")
        if action == "hello":
            return {"status": "ok", "version": 3, "pid": self.pid,
                    "started_ms": self.started_ms, "id": self.id,
                    "kicad_api": self.kicad_api}
        if action == "snapshot":
            with self._lock:
                return {"status": "ok", "mappings": dict(self._mappings)}
        if action == "event":
            self._apply_event(request["event"])
            return {"status": "ok"}
        if action == "result":
            with self._lock:
                return self._results.get(request["id"], {"status": "unknown"})
        if action == "dispatch":
            request_id = request["id"]
            with self._lock:
                expired = [key for key, finished in self._result_finished.items()
                           if time.monotonic() - finished > 300]
                for key in expired:
                    self._result_finished.pop(key, None)
                    self._results.pop(key, None)
                if request_id not in self._results:
                    self._results[request_id] = {"status": "pending"}
                    threading.Thread(target=self._work, args=(request_id, request["request"]), daemon=True).start()
            return {"status": "accepted", "id": request_id}
        return {"status": "error", "message": "unknown mesh action"}

    def _work(self, request_id, request):
        try:
            filepath = request.get("filepath", "")
            if filepath:
                filepath = os.path.normcase(os.path.realpath(os.path.abspath(filepath)))
                request = dict(request, filepath=filepath)
                with _file_lock(filepath):
                    self.refresh()
                    reply = self.handle(request)
                    if (isinstance(reply, dict) and reply.get("pid") and
                            reply.get("status") != "error"):
                        # Publish while still holding the file lock so the
                        # next executor sees the mapping before it can open.
                        self.publish(filepath, reply["pid"])
            else:
                reply = self.handle(request)
            if not isinstance(reply, dict):
                reply = {"status": "ok"}
            if "status" not in reply:
                reply["status"] = "ok"
        except Exception as exc:
            reply = {"status": "error", "message": str(exc)}
        with self._lock:
            self._results[request_id] = reply
            self._result_finished[request_id] = time.monotonic()

    def _apply_event(self, event):
        path = event["filepath"]
        with self._lock:
            previous = self._mappings.get(path)
            if previous and previous["stamp"] >= event["stamp"]:
                return
            # Retain tombstones, otherwise an offline peer's old snapshot can
            # resurrect a mapping after an editor closes.
            self._mappings[path] = event
        if self.on_change:
            self.on_change(path, event.get("pid"))

    def publish(self, filepath, pid):
        filepath = os.path.normcase(os.path.realpath(os.path.abspath(filepath)))
        event = {"filepath": filepath, "pid": pid,
                 "stamp": [time.time_ns(), self.pid, self.id]}
        self._apply_event(event)
        for peer in discover():
            if peer["id"] == self.id:
                continue
            try:
                _exchange(peer["endpoint"], {"mesh_action": "event", "event": event},
                          300)
            except (OSError, ConnectionError, TimeoutError, ValueError):
                pass

    def refresh(self):
        """Merge snapshots only on demand; remove dead process mappings."""
        for peer in discover():
            if peer["id"] == self.id:
                continue
            try:
                reply = _exchange(peer["endpoint"], {"mesh_action": "snapshot"},
                                  300)
                for event in reply.get("mappings", {}).values():
                    self._apply_event(event)
            except (OSError, ConnectionError, TimeoutError, ValueError):
                continue
        with self._lock:
            stale = [(path, value["pid"]) for path, value in self._mappings.items()
                     if value.get("pid") and not psutil.pid_exists(value["pid"])]
        for path, _ in stale:
            self.publish(path, None)

    def snapshot(self):
        self.refresh()
        with self._lock:
            return {path: value["pid"] for path, value in self._mappings.items()
                    if value.get("pid")}

    def close(self):
        global _local_node
        if not self._running:
            return
        self._running = False
        if os.name == "nt":
            try:
                _exchange(self.endpoint, {"mesh_action": "hello"}, 300)
            except (OSError, ConnectionError, TimeoutError, ValueError):
                pass
        self._thread.join(timeout=1)
        with _nodes_lock:
            _active_endpoints.discard(self.endpoint)
            if _local_node is self:
                _local_node = None


def start_node(handle, on_change=None):
    global _local_node
    with _nodes_lock:
        if _local_node is None:
            _local_node = InstanceNode(handle, on_change)
        return _local_node


def local_node():
    return _local_node


def request(message, timeout=RESULT_TIMEOUT):
    """Elect lowest responding PID; retry next node on missing ACK."""
    request_id = uuid.uuid4().hex
    candidates = discover()
    if str(message.get("filepath", "")).lower().endswith(".kicad_pcb"):
        candidates = [peer for peer in candidates if peer.get("kicad_api")]
    if not candidates:
        raise ConnectionError("no capable Kikakuka or FreekiCAD instance node is running")
    last_error = None
    for peer in candidates:
        try:
            ack = _exchange(peer["endpoint"],
                            {"mesh_action": "dispatch", "id": request_id,
                             "request": message})
            if ack.get("status") != "accepted":
                raise RuntimeError(ack.get("message", "instance request rejected"))
        except (OSError, ConnectionError, TimeoutError, RuntimeError, ValueError) as exc:
            last_error = exc
            continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = _exchange(peer["endpoint"],
                                   {"mesh_action": "result", "id": request_id})
            except (OSError, ConnectionError, TimeoutError, ValueError) as exc:
                last_error = exc
                break
            if result.get("status") != "pending":
                return result
            time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"instance request failed: {last_error or 'timed out'}")
