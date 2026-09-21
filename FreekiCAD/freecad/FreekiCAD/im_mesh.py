"""Per-user, on-demand local IPC mesh for editor instance coordination.

Every host process owns one node. Unix sockets or Windows named pipes provide
discovery and request/reply transport. There is no permanent leader, heartbeat,
or privileged socket scan.
"""

from contextlib import contextmanager
from functools import lru_cache
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
import uuid

import psutil

from . import im_transport


ACK_TIMEOUT_MS = 1000
RESULT_TIMEOUT = 300.0
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


@lru_cache(maxsize=1)
def _windows_user_sid():
    """Return the account SID from this process's primary access token."""
    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE))
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD))
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_void_p))
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                     ctypes.byref(token)):  # TOKEN_QUERY
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0,
                                     ctypes.byref(size))  # TokenUser
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, size,
                                            ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents.user.sid
        sid_string = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(sid_string.value)
        finally:
            kernel32.LocalFree(sid_string)
    finally:
        kernel32.CloseHandle(token)


def _windows_user_scope():
    return hashlib.sha256(_windows_user_sid().encode("ascii")).hexdigest()[:16]


def runtime_dir():
    if os.name != "nt":
        # Keep the socket path short enough for macOS's sockaddr_un limit.
        return Path("/tmp") / f"kikakuka-{os.getuid()}"
    # LOCALAPPDATA is stable across independently launched Python runtimes,
    # unlike a process-specific temporary-directory override.
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Kikakuka" / "instances"
    return Path(tempfile.gettempdir()) / f"kikakuka-{_windows_user_scope()}"


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
        return f"kikakuka-{_windows_user_scope()}-{pid}-{started_ms}{suffix}"
    return f"{pid}-{started_ms}{suffix}.sock"


def _endpoint(pid, started_ms, suffix=""):
    name = _endpoint_name(pid, started_ms, suffix)
    if os.name == "nt":
        return "\\\\.\\pipe\\" + name
    return str(_ensure_runtime_dir() / name)


def _parse_endpoint(endpoint):
    name = endpoint.rsplit("\\", 1)[-1] if os.name == "nt" else Path(endpoint).name
    if os.name == "nt":
        pattern = rf"kikakuka-{_windows_user_scope()}-(\d+)-(\d+)(?:-[0-9a-f]{{8}})?"
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
            # A live process may have bound its socket but not started
            # listening yet. A refusal is not proof that the socket is stale.
            continue
        identity = _parse_endpoint(endpoint)
        if (isinstance(reply, dict) and reply.get("status") == "ok" and
                reply.get("version") == 3 and
                reply.get("pid") == identity[0] and
                reply.get("started_ms") == identity[1]):
            result.append(dict(reply, endpoint=endpoint))
    return sorted(result, key=lambda item: (item["pid"], item["id"]))


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
        self.document_provider = None
        self.document_activator = None
        self.document_opener = None
        self.source_registrar = None
        self.id = uuid.uuid4().hex
        self.token = _shared_token()
        self.pid = os.getpid()
        self.started_ms = _started_ms(self.pid)
        self.endpoint = _endpoint(self.pid, self.started_ms)
        if os.name != "nt" and Path(self.endpoint).exists():
            # Another node in this process may still be binding/listening.
            # Never unlink its socket just because a connection is refused.
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
        self._freecad_open_inflight = {}
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
                    "kicad_api": self.kicad_api,
                    "freecad_documents": self.document_provider is not None}
        if action == "freecad-list-documents":
            if self.document_provider is None:
                return {"status": "error", "message": "FreeCAD GUI documents unavailable"}
            return {"status": "ok", "pid": self.pid,
                    "documents": list(self.document_provider())}
        if action == "freecad-activate-document":
            if self.document_activator is None:
                return {"status": "error", "message": "FreeCAD GUI activation unavailable"}
            filepath = request.get("filepath")
            if not isinstance(filepath, str) or not os.path.isabs(filepath):
                return {"status": "error", "message": "absolute file path required"}
            return {"status": "ok", "pid": self.pid,
                    "found": bool(self.document_activator(filepath))}
        if action == "freecad-open-document":
            if self.document_opener is None:
                return {"status": "error", "message": "FreeCAD GUI opening unavailable"}
            filepath = request.get("filepath")
            if not isinstance(filepath, str) or not os.path.isabs(filepath):
                return {"status": "error", "message": "absolute file path required"}
            request_id = request.get("id")
            if not isinstance(request_id, str) or not request_id:
                return {"status": "error", "message": "request ID required"}
            with self._lock:
                existing_id = self._freecad_open_inflight.get(filepath)
                if existing_id is not None:
                    return {"status": "accepted", "id": existing_id}
                if request_id not in self._results:
                    self._results[request_id] = {"status": "pending"}
                    self._freecad_open_inflight[filepath] = request_id
                    threading.Thread(target=self._work_freecad_open,
                                     args=(request_id, filepath), daemon=True).start()
            return {"status": "accepted", "id": request_id}
        if action == "freecad-bind-source":
            if self.source_registrar is None:
                return {"status": "error", "message": "FreeCAD GUI source binding unavailable"}
            filepath = request.get("filepath")
            if not isinstance(filepath, str) or not os.path.isabs(filepath):
                return {"status": "error", "message": "absolute file path required"}
            return {"status": "ok", "pid": self.pid,
                    "bound": bool(self.source_registrar(filepath))}
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

    def _work_freecad_open(self, request_id, filepath):
        # The caller already holds the per-file lock. Acquiring it again here
        # would deadlock when that caller lives in another mesh process.
        try:
            opened = self.document_opener(filepath)
            reply = ({"status": "ok", "pid": self.pid} if opened else
                     {"status": "error", "message": f"FreeCAD did not open: {filepath}"})
        except Exception as exc:
            reply = {"status": "error", "message": str(exc)}
        with self._lock:
            self._results[request_id] = reply
            self._result_finished[request_id] = time.monotonic()
            if self._freecad_open_inflight.get(filepath) == request_id:
                self._freecad_open_inflight.pop(filepath, None)

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

    def set_document_provider(self, provider):
        self.document_provider = provider

    def set_document_activator(self, activator):
        self.document_activator = activator

    def set_document_opener(self, opener):
        self.document_opener = opener

    def set_source_registrar(self, registrar):
        self.source_registrar = registrar

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


def scan_freecad_documents():
    """Query every GUI FreeCAD node; omit nodes that cannot answer now."""
    documents_by_pid = {}
    for peer in discover():
        if not peer.get("freecad_documents"):
            continue
        try:
            reply = _exchange(peer["endpoint"],
                              {"mesh_action": "freecad-list-documents"}, 2500)
        except (OSError, ConnectionError, TimeoutError, ValueError):
            continue
        documents = reply.get("documents") if isinstance(reply, dict) else None
        if (isinstance(reply, dict) and reply.get("status") == "ok" and
                reply.get("pid") == peer["pid"] and
                isinstance(documents, list) and
                all(isinstance(path, str) and path for path in documents)):
            documents_by_pid.setdefault(peer["pid"], set()).update(documents)
    return [(pid, tuple(sorted(paths)))
            for pid, paths in sorted(documents_by_pid.items())]


def activate_open_freecad_document(filepath, target_pid=None, before_activate=None):
    """Select an open document, focusing its process before changing MDI tabs."""
    filepath = os.path.normcase(os.path.realpath(os.path.abspath(filepath)))
    for peer in discover():
        if (not peer.get("freecad_documents") or
                (target_pid is not None and peer["pid"] != target_pid)):
            continue
        try:
            if before_activate is not None:
                listing = _exchange(peer["endpoint"],
                                    {"mesh_action": "freecad-list-documents"}, 2500)
                if (listing.get("status") != "ok" or
                        listing.get("pid") != peer["pid"] or
                        filepath not in listing.get("documents", ())):
                    continue
                before_activate(peer["pid"])
            reply = _exchange(peer["endpoint"],
                              {"mesh_action": "freecad-activate-document",
                               "filepath": filepath}, 2500)
        except (OSError, ConnectionError, TimeoutError, ValueError):
            continue
        if (isinstance(reply, dict) and reply.get("status") == "ok" and
                reply.get("pid") == peer["pid"] and reply.get("found") is True and
                _alive(peer["endpoint"]) is True):
            return peer["pid"]
    return None


def open_in_freecad_node(filepath, before_open=None):
    """Focus a GUI node before handing it a new document to open."""
    filepath = os.path.normcase(os.path.realpath(os.path.abspath(filepath)))
    candidates = [peer for peer in discover() if peer.get("freecad_documents")]
    if not candidates:
        return None
    request_id = uuid.uuid4().hex
    rejected = False
    for peer in candidates:
        try:
            if before_open is not None:
                before_open(peer["pid"])
            ack = _exchange(peer["endpoint"],
                            {"mesh_action": "freecad-open-document",
                             "id": request_id, "filepath": filepath})
        except (OSError, ConnectionError, TimeoutError, ValueError):
            continue
        if ack.get("status") != "accepted":
            rejected = rejected or _alive(peer["endpoint"]) is True
            continue
        # The node has queued the GUI operation. Treat that as delivered so a
        # slow import cannot cause the caller to launch a second FreeCAD.
        return peer["pid"]
    if rejected:
        raise RuntimeError("Running FreeCAD node cannot open a new document")
    return None


def bind_freecad_source(pid, filepath, timeout=45):
    """Wait for a newly launched GUI node to identify its imported file."""
    filepath = os.path.normcase(os.path.realpath(os.path.abspath(filepath)))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and psutil.pid_exists(pid):
        for peer in discover():
            if peer["pid"] != pid or not peer.get("freecad_documents"):
                continue
            try:
                reply = _exchange(peer["endpoint"],
                                  {"mesh_action": "freecad-bind-source",
                                   "filepath": filepath}, 2500)
            except (OSError, ConnectionError, TimeoutError, ValueError):
                continue
            if (reply.get("status") == "ok" and reply.get("pid") == pid and
                    reply.get("bound") is True and _alive(peer["endpoint"]) is True):
                return True
        time.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
    return False


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
