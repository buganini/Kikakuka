import threading
import unittest
from unittest import mock

import workspace_bus
from FreekiCAD.FreekiCAD.kicad_api_retry import retry_kicad_call
from kipy.errors import ApiError
from kipy.proto.common import ApiStatusCode


class RetryKicadCallTests(unittest.TestCase):
    def test_reuses_busy_retry_policy(self):
        attempts = {"count": 0}

        def func():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ApiError("busy", code=ApiStatusCode.AS_BUSY)
            return "ok"

        with mock.patch(
            "FreekiCAD.FreekiCAD.kicad_api_retry.time.sleep", return_value=None
        ):
            result = retry_kicad_call(func, max_retries=5, delay_s=1.0)

        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 3)


class WorkspaceBusResolveSocketTests(unittest.TestCase):
    def _make_bus(self, pidmap):
        bus = workspace_bus.WorkspaceBus.__new__(workspace_bus.WorkspaceBus)
        bus._get_pidmap = lambda: dict(pidmap)
        bus._open_file = None
        bus._remove_pid = None
        bus._update_pid = None
        bus._opening = set()
        bus._opening_lock = threading.Lock()
        return bus

    def test_wait_for_ready_socket_retries_until_api_is_ready(self):
        bus = self._make_bus({})

        with mock.patch(
            "workspace_bus._kicad_socket_for_pid", return_value="/tmp/api.sock"
        ):
            with mock.patch(
                "workspace_bus._socket_board_filepath_state",
                side_effect=[
                    ("not_ready", None, "busy"),
                    ("ready", "/boards/fpc.kicad_pcb", None),
                ],
            ):
                with mock.patch("workspace_bus.time.sleep", return_value=None):
                    socket_path, state, actual_filepath, error = (
                        bus._wait_for_ready_socket(123, timeout=1.0, interval=0.0)
                    )

        self.assertEqual(socket_path, "/tmp/api.sock")
        self.assertEqual(state, "ready")
        self.assertEqual(actual_filepath, "/boards/fpc.kicad_pcb")
        self.assertIsNone(error)

    def test_resolve_socket_returns_error_when_api_never_becomes_ready(self):
        requested = "/boards/fpc.kicad_pcb"
        pidmap = {requested: 111}
        bus = self._make_bus(pidmap)

        with mock.patch("workspace_bus.psutil.pid_exists", return_value=True):
            with mock.patch.object(
                bus,
                "_wait_for_ready_socket",
                return_value=("/tmp/api.sock", "not_ready", None, "KiCad busy"),
            ):
                reply = bus._resolve_socket(
                    {"action": "reload", "object": "fpc", "filepath": requested},
                    dict(pidmap),
                )

        self.assertEqual(reply["status"], "error")
        self.assertIn("IPC API was not ready after 30s", reply["message"])


if __name__ == "__main__":
    unittest.main()
