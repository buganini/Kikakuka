"""ZMQ client for querying the Kikakuka workspace manager.

FreekiCAD uses this to resolve which KiCad IPC socket to connect to
for a given .kicad_pcb file.  The workspace manager runs a ZMQ REP
daemon on tcp://127.0.0.1:19780.
"""

import json
import time
import FreeCAD

WORKSPACE_PORT = 19780
TIMEOUT_MS = 5000       # per-request recv timeout (ms)
RETRY_INTERVAL = 1.0    # seconds between retries on "pending"
RESOLVE_TIMEOUT = 60.0  # overall deadline (seconds)


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
    try:
        import zmq
    except ImportError:
        FreeCAD.Console.PrintWarning(
            "FreekiCAD: pyzmq not installed, "
            "cannot connect to workspace manager\n"
        )
        return None

    ctx = zmq.Context.instance()
    req = ctx.socket(zmq.REQ)
    req.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
    req.setsockopt(zmq.LINGER, 0)
    req.connect(f"tcp://127.0.0.1:{WORKSPACE_PORT}")

    msg = json.dumps({
        "type": "resolve",
        "filepath": filepath,
    }).encode("utf-8")

    deadline = time.time() + RESOLVE_TIMEOUT

    try:
        while True:
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: ZMQ REQ  resolve {filepath}\n"
            )
            req.send(msg)
            reply = json.loads(req.recv().decode("utf-8"))
            FreeCAD.Console.PrintMessage(
                f"FreekiCAD: ZMQ RESP {reply}\n"
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

    except zmq.Again:
        FreeCAD.Console.PrintError(
            "FreekiCAD: Could not connect to Kikakuka workspace manager. "
            "Start the workspace manager first.\n"
        )
        return None
    except Exception as e:
        FreeCAD.Console.PrintError(
            f"FreekiCAD: Workspace manager error: {e}\n"
        )
        return None
    finally:
        req.close()
