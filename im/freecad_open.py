"""Open a live KiCad board in a FreekiCAD GUI process."""

import os
import time
import uuid

from . import im_mesh
from .instance_backend import _editors, _focus, _launch


OPEN_TIMEOUT = 300


def _gui_peers():
    return [peer for peer in im_mesh.discover() if peer.get("freecad_documents")]


def _open_pcb(peer, filepath, socket_path, *, create=False, active_only=False,
              probe=False, document_name=None):
    if peer.get("freecad_pcb", 0) != 2:
        raise RuntimeError("Update FreekiCAD in the running FreeCAD instance, then restart it")
    request_id = uuid.uuid4().hex
    ack = im_mesh._exchange(peer["endpoint"], {
        "mesh_action": "freecad-open-pcb", "id": request_id,
        "filepath": filepath, "socket": socket_path, "create": create,
        "active_only": active_only, "probe": probe, "document": document_name,
    })
    if ack.get("status") != "accepted":
        raise RuntimeError(ack.get("message", "FreeCAD rejected the PCB request"))
    # Once accepted, never retry in a different process: an import may still
    # be running even if its result cannot be retrieved.
    deadline = time.monotonic() + OPEN_TIMEOUT
    while time.monotonic() < deadline:
        reply = im_mesh._exchange(peer["endpoint"], {
            "mesh_action": "result", "id": ack["id"],
        })
        if reply.get("status") == "pending":
            time.sleep(im_mesh.POLL_INTERVAL)
            continue
        if reply.get("status") != "ok" or reply.get("pid") != peer["pid"]:
            raise RuntimeError(reply.get("message", "FreeCAD could not open the PCB"))
        if probe:
            return reply.get("document")
        return reply.get("found") is True
    raise TimeoutError("FreeCAD did not finish opening the PCB; it may still be loading")


def open_board(filepath, socket_path):
    """Update the first linked document, or create one; coalesce repeated clicks."""
    if not filepath or not os.path.isabs(filepath) or not os.path.isfile(filepath):
        raise ValueError("Save the PCB once before using Open in FreeCAD")
    filepath = os.path.normcase(os.path.realpath(filepath))
    if not filepath.lower().endswith(".kicad_pcb"):
        raise ValueError("Open in FreeCAD requires a .kicad_pcb file")
    if not socket_path:
        raise ValueError("The invoking KiCad API socket is unavailable")
    socket_path = socket_path.removeprefix("ipc://")
    with im_mesh._file_lock("open-in-freecad:" + filepath, blocking=False) as acquired:
        if not acquired:
            return None
        with im_mesh.launch_lock("freecad"):
            peers = _gui_peers()
            if not peers:
                running = _editors("freecad")
                pid = None if running else _launch(None, "freecad")
                if not running and pid is None:
                    raise RuntimeError("FreeCAD did not start")
                deadline = time.monotonic() + 45
                while not peers and time.monotonic() < deadline:
                    time.sleep(0.25)
                    peers = _gui_peers()
                if not peers:
                    raise RuntimeError("FreeCAD did not connect; install or enable FreekiCAD")
            # Prefer active documents across all GUI instances before falling
            # back to each process's document order.
            for active_only in (True, False):
                for peer in peers:
                    document_name = _open_pcb(
                        peer, filepath, socket_path, active_only=active_only, probe=True)
                    if document_name:
                        _focus(peer["pid"])
                        if not _open_pcb(peer, filepath, socket_path,
                                         document_name=document_name):
                            raise RuntimeError("The selected FreeCAD document is no longer available")
                        return peer["pid"]
            peer = peers[0]
            _focus(peer["pid"])
            if not _open_pcb(peer, filepath, socket_path, create=True):
                raise RuntimeError("FreeCAD did not create a PCB document")
            return peer["pid"]
