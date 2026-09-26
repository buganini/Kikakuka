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
from im.socket_discovery_test import (enumerated_kicad_sockets,
                                      owner_kicad_sockets)


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


def _mapping_changed(filepath, pid, socket_path=None):
    LOGGER.info("mapping filepath=%s pid=%s socket=%s",
                filepath, pid or "-", socket_path or "-")


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
    parser.add_argument(
        "command",
        choices=("run", "test"),
        default="run",
        nargs="?",
        help="run a mesh node (default), or test KiCad socket discovery",
    )
    return parser.parse_args(arguments)


def _print_socket_results(label, sockets):
    print(f"{label}:")
    if not sockets:
        print("  (none)")
        return
    for pid, socket_path in sorted(
            sockets, key=lambda item: (item[0] is None, item[0] or 0, item[1])):
        print(f"  {pid if pid is not None else '?'}\t{socket_path}")


def _test_socket_discovery():
    _print_socket_results("enumerate", enumerated_kicad_sockets())
    owner_method = (
        "GetNamedPipeServerProcessId"
        if sys.platform == "win32"
        else 'psutil.Process.net_connections(kind="unix")'
    )
    _print_socket_results(owner_method, owner_kicad_sockets())
    return 0


def main(arguments=None):
    options = _parse_arguments(
        sys.argv[1:] if arguments is None else arguments
    )
    logging.basicConfig(
        level=getattr(logging, options.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if options.command == "test":
        return _test_socket_discovery()
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
