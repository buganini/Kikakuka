"""Local JSON request/reply transport for the instance mesh."""

import json
import os
import socket
import time


MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def _encode(message):
    data = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("instance message is too large")
    return data


def _read_exact(connection, size, deadline):
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("instance node did not answer")
        connection.settimeout(remaining)
        try:
            chunk = connection.recv(size - len(data))
        except socket.timeout as exc:
            raise TimeoutError("instance node did not answer") from exc
        if not chunk:
            raise ConnectionError("instance connection closed")
        data.extend(chunk)
    return bytes(data)


class Connection:
    def __init__(self, connection, pipe=False):
        self.connection = connection
        self.pipe = pipe

    def send(self, message):
        data = _encode(message)
        if self.pipe:
            self.connection.send_bytes(data)
        else:
            self.connection.sendall(len(data).to_bytes(4, "big") + data)

    def receive(self, timeout_ms):
        if self.pipe:
            if not self.connection.poll(timeout_ms / 1000):
                raise TimeoutError("instance node did not answer")
            data = self.connection.recv_bytes(MAX_MESSAGE_BYTES)
        else:
            deadline = time.monotonic() + timeout_ms / 1000
            size = int.from_bytes(_read_exact(self.connection, 4, deadline), "big")
            if size > MAX_MESSAGE_BYTES:
                raise ValueError("instance message is too large")
            data = _read_exact(self.connection, size, deadline)
        return json.loads(data.decode("utf-8"))

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Listener:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.pipe = os.name == "nt"
        if self.pipe:
            from multiprocessing.connection import Listener as PipeListener
            self.listener = PipeListener(endpoint, family="AF_PIPE")
        else:
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self.listener.bind(endpoint)
                self._bound_inode = os.stat(endpoint, follow_symlinks=False).st_ino
                os.chmod(endpoint, 0o600)
                self.listener.listen(16)
                self.listener.settimeout(0.2)
            except Exception:
                self.listener.close()
                raise

    def accept(self):
        try:
            connection = self.listener.accept()
        except socket.timeout:
            return None
        if not self.pipe:
            connection = connection[0]
            connection.settimeout(1)
        return Connection(connection, self.pipe)

    def close(self):
        self.listener.close()
        if not self.pipe:
            try:
                if os.stat(self.endpoint, follow_symlinks=False).st_ino == self._bound_inode:
                    os.unlink(self.endpoint)
            except FileNotFoundError:
                pass


def _connect_pipe(endpoint, timeout_ms):
    """Use a bounded Windows pipe connect instead of Client's 20-second retry."""
    import _winapi
    from multiprocessing.connection import PipeConnection

    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        if time.monotonic() >= deadline:
            raise TimeoutError(f"instance node did not answer: {endpoint}")
        try:
            _winapi.WaitNamedPipe(endpoint, remaining)
            handle = _winapi.CreateFile(
                endpoint, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                0, _winapi.NULL, _winapi.OPEN_EXISTING,
                _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
            try:
                _winapi.SetNamedPipeHandleState(
                    handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
                return PipeConnection(handle)
            except Exception:
                _winapi.CloseHandle(handle)
                raise
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (
                    _winapi.ERROR_PIPE_BUSY, _winapi.ERROR_SEM_TIMEOUT):
                raise


def connect(endpoint, timeout_ms):
    if os.name == "nt":
        return Connection(_connect_pipe(endpoint, timeout_ms), pipe=True)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout_ms / 1000)
        connection.connect(endpoint)
        return Connection(connection)
    except Exception:
        connection.close()
        raise
