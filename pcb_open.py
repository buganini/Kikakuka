"""Open KiCad boards and schematics through the workspace manager."""

import json
import os
import platform
import socket
import subprocess


WORKSPACE_SOCKET_PATH = "/tmp/kikakuka.sock"
WORKSPACE_PORT = 19780
WORKSPACE_OPEN_TIMEOUT = 30.0
MAX_WORKSPACE_REPLY_BYTES = 1024 * 1024


def system_open_command(filepath, mac_args=()):
    """Return the POSIX associated-file opener command."""
    system = platform.system()
    if system == "Darwin":
        return ["open", *mac_args, filepath]
    if system == "Windows":
        raise ValueError("Windows uses os.startfile instead")
    return ["xdg-open", filepath]


def open_with_system(filepath):
    """Open a KiCad file using its OS file association."""
    if platform.system() == "Windows":
        os.startfile(filepath)
    else:
        subprocess.Popen(system_open_command(filepath, mac_args=("-n",)))


def _connect_workspace(timeout):
    if platform.system() == "Windows":
        return socket.create_connection(("127.0.0.1", WORKSPACE_PORT), timeout)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(WORKSPACE_SOCKET_PATH)
        return connection
    except Exception:
        connection.close()
        raise


def _recv_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError("workspace manager closed the connection")
        data.extend(chunk)
    return bytes(data)


def request_workspace_open(filepath, timeout=WORKSPACE_OPEN_TIMEOUT):
    """Return True only when the manager accepts opening this exact file."""
    request = {"action": "open-file", "filepath": filepath}
    payload = json.dumps(request).encode("utf-8")
    with _connect_workspace(timeout) as connection:
        connection.sendall(len(payload).to_bytes(4, "big") + payload)
        reply_size = int.from_bytes(_recv_exact(connection, 4), "big")
        if not 0 < reply_size <= MAX_WORKSPACE_REPLY_BYTES:
            raise ValueError("invalid workspace manager response size")
        reply = json.loads(_recv_exact(connection, reply_size).decode("utf-8"))
    return (
        isinstance(reply, dict) and
        reply.get("status") == "ok" and
        reply.get("action") == "open-file" and
        reply.get("filepath") == filepath
    )


def open_kicad_file(filepath):
    """Open a PCB or schematic via the manager, falling back to the OS.

    Returns ``"workspace"`` or ``"system"`` to identify the path used.
    This can run in a background thread because the workspace request may
    wait for an editor launch.
    """
    filepath = os.path.abspath(os.fspath(filepath))
    if not filepath.lower().endswith((".kicad_pcb", ".kicad_sch")):
        raise ValueError("expected a .kicad_pcb or .kicad_sch file")
    if not os.path.isfile(filepath):
        raise FileNotFoundError(filepath)
    try:
        if request_workspace_open(filepath):
            return "workspace"
    except (OSError, ValueError) as exc:
        print(f"Workspace manager could not open {filepath}: {exc}")
    open_with_system(filepath)
    return "system"


def open_pcb_file(filepath):
    """PCB-only entry point used by the panelizer."""
    if not os.fspath(filepath).lower().endswith(".kicad_pcb"):
        raise ValueError("expected a .kicad_pcb file")
    return open_kicad_file(filepath)
