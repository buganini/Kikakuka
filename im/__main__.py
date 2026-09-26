"""Run a foreground Instance Manager mesh node."""

import argparse
import logging
import os
import signal
import sys
import threading


# ``python im`` executes this file as a directory entry point without package
# context. Add the repository root so it shares the same imports as
# ``python -m im``.
if __package__ in (None, ""):
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if package_root not in sys.path:
        sys.path.insert(0, package_root)

from im.im_mesh import start_node
from im.instance_backend import handle as instance_handle


LOGGER = logging.getLogger("im")


def _handle(request):
    action = request.get("action")
    filepath = request.get("filepath")
    LOGGER.info("request action=%s filepath=%s", action, filepath or "-")
    try:
        reply = instance_handle(request)
    except Exception:
        LOGGER.exception("request failed action=%s filepath=%s",
                         action, filepath or "-")
        raise
    LOGGER.info(
        "reply action=%s status=%s pid=%s socket=%s message=%s",
        action,
        reply.get("status", "-") if isinstance(reply, dict) else "-",
        reply.get("pid", "-") if isinstance(reply, dict) else "-",
        reply.get("socket", "-") if isinstance(reply, dict) else "-",
        reply.get("message", "-") if isinstance(reply, dict) else "-",
    )
    return reply


def _mapping_changed(filepath, pid):
    LOGGER.info("mapping filepath=%s pid=%s", filepath, pid or "-")


def _wait_for_shutdown():
    stopped = threading.Event()

    def stop(_signum=None, _frame=None):
        stopped.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        shutdown_signal = getattr(signal, signal_name, None)
        if shutdown_signal is not None:
            signal.signal(shutdown_signal, stop)
    try:
        stopped.wait()
    except KeyboardInterrupt:
        pass


def _parse_arguments(arguments):
    parser = argparse.ArgumentParser(
        description="Run a foreground Kikakuka Instance Manager node.",
    )
    parser.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error"),
        default="info",
    )
    return parser.parse_args(arguments)


def main(arguments=None):
    options = _parse_arguments(
        sys.argv[1:] if arguments is None else arguments
    )
    logging.basicConfig(
        level=getattr(logging, options.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        node = start_node(_handle, _mapping_changed)
    except Exception:
        LOGGER.exception("node failed to start")
        return 1

    LOGGER.info(
        "node started pid=%s endpoint=%s kicad_api=%s",
        node.pid,
        node.endpoint,
        node.kicad_api,
    )
    try:
        _wait_for_shutdown()
    finally:
        node.close()
        LOGGER.info("node stopped pid=%s", node.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
