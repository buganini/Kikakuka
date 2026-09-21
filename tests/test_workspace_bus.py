import threading
import unittest
from unittest import mock

import workspace_bus
from FreekiCAD.freecad.FreekiCAD.kicad_api_retry import retry_kicad_call
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
            "FreekiCAD.freecad.FreekiCAD.kicad_api_retry.time.sleep",
            return_value=None,
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
        bus._pending_open_pids = {}
        bus._opening_lock = threading.Lock()
        bus._launch_lock = threading.Lock()
        bus._pidmap_rebuild_done = threading.Event()
        bus._pidmap_rebuild_done.set()
        bus._running = True
        return bus

    def test_open_file_request_focuses_existing_editor(self):
        path = "/boards/panel.kicad_pcb"
        bus = self._make_bus({path: 111})
        bus._open_file = mock.Mock()
        bus._bring_to_front = mock.Mock(return_value=True)

        with mock.patch("workspace_bus.os.path.isfile", return_value=True):
            with mock.patch("workspace_bus.psutil.pid_exists", return_value=True):
                reply = bus._handle({"action": "open-file", "filepath": path})

        self.assertEqual(reply, {
            "status": "ok", "action": "open-file",
            "filepath": path, "pid": 111,
        })
        bus._open_file.assert_not_called()
        bus._bring_to_front.assert_called_once_with(111)

    def test_open_file_request_launches_missing_editor(self):
        path = "/boards/panel.kicad_pcb"
        bus = self._make_bus({})
        bus._open_file = mock.Mock(return_value=222)
        bus._bring_to_front = mock.Mock(return_value=True)

        with mock.patch("workspace_bus.os.path.isfile", return_value=True):
            with mock.patch("workspace_bus._existing_kicad_sockets", return_value=[]):
                reply = bus._handle({"action": "open-file", "filepath": path})

        self.assertEqual(reply["status"], "ok")
        self.assertEqual(reply["pid"], 222)
        bus._open_file.assert_called_once_with(path)
        bus._bring_to_front.assert_called_once_with(222)

    def test_open_file_request_accepts_schematic(self):
        path = "/boards/main.kicad_sch"
        bus = self._make_bus({})
        bus._open_file = mock.Mock(return_value=333)

        with mock.patch("workspace_bus.os.path.isfile", return_value=True):
            with mock.patch("workspace_bus._existing_kicad_sockets", return_value=[]):
                reply = bus._handle({"action": "open-file", "filepath": path})

        self.assertEqual(reply["status"], "ok")
        self.assertEqual(reply["filepath"], path)
        bus._open_file.assert_called_once_with(path)

    def test_open_file_request_returns_error_when_launch_fails(self):
        path = "/boards/panel.kicad_pcb"
        bus = self._make_bus({})
        bus._open_file = mock.Mock(return_value=None)

        with mock.patch("workspace_bus.os.path.isfile", return_value=True):
            with mock.patch("workspace_bus._existing_kicad_sockets", return_value=[]):
                reply = bus._handle({"action": "open-file", "filepath": path})

        self.assertEqual(reply["status"], "error")

    def test_repeated_open_request_reuses_pending_editor(self):
        path = "/boards/panel.kicad_pcb"
        bus = self._make_bus({})
        bus._open_file = mock.Mock(return_value=222)

        with mock.patch("workspace_bus._existing_kicad_sockets", return_value=[]):
            with mock.patch("workspace_bus.psutil.pid_exists", return_value=True):
                self.assertEqual(bus._do_open_file(path), 222)
                self.assertEqual(bus._do_open_file(path), 222)

        bus._open_file.assert_called_once_with(path)

    def test_opening_different_files_serializes_kicad_launches(self):
        bus = self._make_bus({})
        first_started = threading.Event()
        second_attempted = threading.Event()
        release_first = threading.Event()
        opened = []

        def open_file(filepath):
            opened.append(filepath)
            if filepath.endswith("fpc.kicad_pcb"):
                first_started.set()
                release_first.wait(1.0)
                return 111
            return 222

        bus._open_file = open_file
        bus._opening.update({
            "/boards/fpc.kicad_pcb",
            "/boards/fpc2.kicad_pcb",
        })

        first = threading.Thread(
            target=bus._do_open_file, args=("/boards/fpc.kicad_pcb",)
        )

        def open_second():
            second_attempted.set()
            bus._do_open_file("/boards/fpc2.kicad_pcb")

        second = threading.Thread(target=open_second)
        with mock.patch("workspace_bus._existing_kicad_sockets", return_value=[]):
            first.start()
            self.assertTrue(first_started.wait(1.0))
            second.start()
            self.assertTrue(second_attempted.wait(1.0))

            self.assertEqual(opened, ["/boards/fpc.kicad_pcb"])
            release_first.set()
            first.join(1.0)
            second.join(1.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(
            opened,
            ["/boards/fpc.kicad_pcb", "/boards/fpc2.kicad_pcb"],
        )
        self.assertEqual(
            bus._pending_open_pids,
            {
                "/boards/fpc.kicad_pcb": 111,
                "/boards/fpc2.kicad_pcb": 222,
            },
        )

    def test_existing_kicad_sockets_maps_pid_named_and_generic_sockets(self):
        with mock.patch(
            "workspace_bus.os.listdir",
            return_value=["api.sock", "api-222.sock", "api.lock", "other"],
        ):
            with mock.patch("workspace_bus._kicad_socket_dir", return_value="/ipc"):
                with mock.patch("workspace_bus._kicad_pid_state", return_value=True):
                    with mock.patch(
                        "workspace_bus._socket_owner_pid", return_value=111
                    ):
                        with mock.patch(
                            "workspace_bus._kicad_process_pids", return_value=[111, 222]
                        ):
                            sockets = workspace_bus._existing_kicad_sockets()

        self.assertCountEqual(
            sockets,
            [("/ipc/api.sock", 111), ("/ipc/api-222.sock", 222)],
        )

    def test_existing_kicad_sockets_removes_dead_pid_named_socket(self):
        with mock.patch(
            "workspace_bus.os.listdir", return_value=["api-222.sock"]
        ):
            with mock.patch("workspace_bus._kicad_socket_dir", return_value="/ipc"):
                with mock.patch(
                    "workspace_bus._kicad_pid_state", return_value=False
                ):
                    with mock.patch(
                        "workspace_bus._unix_socket_connectable", return_value=False
                    ):
                        with mock.patch(
                            "workspace_bus._remove_dead_socket", return_value=True
                        ) as remove:
                            sockets = workspace_bus._existing_kicad_sockets()

        self.assertEqual(sockets, [])
        remove.assert_called_once_with(
            "/ipc/api-222.sock", "PID 222 is not a running KiCad process"
        )

    def test_existing_kicad_sockets_removes_generic_without_kicad_process(self):
        with mock.patch("workspace_bus.os.listdir", return_value=["api.sock"]):
            with mock.patch("workspace_bus._kicad_socket_dir", return_value="/ipc"):
                with mock.patch("workspace_bus._socket_owner_pid", return_value=None):
                    with mock.patch(
                        "workspace_bus._kicad_process_pids", return_value=[]
                    ):
                        with mock.patch(
                            "workspace_bus._unix_socket_connectable",
                            return_value=False,
                        ):
                            with mock.patch(
                                "workspace_bus._remove_dead_socket", return_value=True
                            ) as remove:
                                sockets = workspace_bus._existing_kicad_sockets()

        self.assertEqual(sockets, [])
        remove.assert_called_once_with(
            "/ipc/api.sock", "no listener or running KiCad process"
        )

    def test_existing_kicad_sockets_keeps_socket_when_pid_state_unknown(self):
        with mock.patch(
            "workspace_bus.os.listdir", return_value=["api-222.sock"]
        ):
            with mock.patch("workspace_bus._kicad_socket_dir", return_value="/ipc"):
                with mock.patch(
                    "workspace_bus._kicad_pid_state", return_value=None
                ):
                    with mock.patch(
                        "workspace_bus._remove_dead_socket"
                    ) as remove:
                        sockets = workspace_bus._existing_kicad_sockets()

        self.assertEqual(sockets, [("/ipc/api-222.sock", 222)])
        remove.assert_not_called()

    def test_existing_kicad_sockets_does_not_remove_socket_with_listener(self):
        with mock.patch(
            "workspace_bus.os.listdir", return_value=["api-222.sock"]
        ):
            with mock.patch("workspace_bus._kicad_socket_dir", return_value="/ipc"):
                with mock.patch(
                    "workspace_bus._kicad_pid_state", return_value=False
                ):
                    with mock.patch(
                        "workspace_bus._unix_socket_connectable", return_value=True
                    ):
                        with mock.patch(
                            "workspace_bus._remove_dead_socket"
                        ) as remove:
                            sockets = workspace_bus._existing_kicad_sockets()

        self.assertEqual(sockets, [])
        remove.assert_not_called()

    def test_dead_socket_cleanup_skips_windows_named_pipes(self):
        with mock.patch("workspace_bus.platform.system", return_value="Windows"):
            with mock.patch("workspace_bus.os.stat") as socket_stat:
                with mock.patch("workspace_bus.os.unlink") as unlink:
                    removed = workspace_bus._remove_dead_socket(
                        r"C:\Temp\kicad\api-222.sock", "dead test socket"
                    )

        self.assertFalse(removed)
        socket_stat.assert_not_called()
        unlink.assert_not_called()

    def test_socket_connectability_is_inconclusive_on_windows(self):
        with mock.patch("workspace_bus.platform.system", return_value="Windows"):
            self.assertIsNone(
                workspace_bus._unix_socket_connectable(
                    r"C:\Temp\kicad\api-222.sock"
                )
            )

    def test_monitor_couplers_action_resolves_existing_socket(self):
        bus = self._make_bus({"/boards/fpc.kicad_pcb": 111})
        expected = {
            "status": "ok", "action": "monitor-couplers",
            "socket": "/tmp/api.sock",
        }
        bus._resolve_socket = mock.Mock(return_value=expected)
        message = {
            "action": "monitor-couplers", "object": "fpc",
            "filepath": "/boards/fpc.kicad_pcb",
        }

        reply = bus._handle(message)

        self.assertEqual(reply, expected)
        bus._resolve_socket.assert_called_once_with(
            message, {"/boards/fpc.kicad_pcb": 111})

    def test_update_coupler_action_resolves_existing_socket(self):
        bus = self._make_bus({"/boards/fpc.kicad_pcb": 111})
        expected = {
            "status": "ok", "action": "update-coupler",
            "socket": "/tmp/api.sock", "component": "J1",
        }
        bus._resolve_socket = mock.Mock(return_value=expected)
        message = {
            "action": "update-coupler", "object": "fpc",
            "filepath": "/boards/fpc.kicad_pcb", "component": "J1",
        }

        reply = bus._handle(message)

        self.assertEqual(reply, expected)
        bus._resolve_socket.assert_called_once_with(
            message, {"/boards/fpc.kicad_pcb": 111})

    def test_monitor_couplers_does_not_launch_kicad(self):
        bus = self._make_bus({})
        bus._open_file = mock.Mock(return_value=111)

        reply = bus._resolve_socket({
            "action": "monitor-couplers", "object": "fpc",
            "filepath": "/boards/fpc.kicad_pcb",
        }, {})

        self.assertEqual(reply["status"], "error")
        self.assertEqual(reply["message"], "file is not open in KiCad")
        bus._open_file.assert_not_called()

    def test_rebuild_pidmap_retries_busy_socket_then_restores_mapping(self):
        bus = self._make_bus({})
        restored = []
        bus._update_pid = lambda filepath, pid: restored.append((filepath, pid))
        bus._pidmap_rebuild_done.clear()

        with mock.patch(
            "workspace_bus._existing_kicad_sockets",
            return_value=[("/tmp/kicad/api-111.sock", 111)],
        ):
            with mock.patch("workspace_bus.psutil.pid_exists", return_value=True):
                with mock.patch(
                    "workspace_bus._socket_board_filepath_state",
                    side_effect=[
                        ("not_ready", None, "busy"),
                        ("ready", "/boards/fpc.kicad_pcb", None),
                    ],
                ):
                    with mock.patch("workspace_bus.time.sleep", return_value=None):
                        bus._rebuild_pidmap(interval=0.0)

        self.assertEqual(restored, [("/boards/fpc.kicad_pcb", 111)])
        self.assertTrue(bus._pidmap_rebuild_done.is_set())

    def test_resolve_waits_for_startup_rebuild_before_opening_new_instance(self):
        requested = "/boards/fpc.kicad_pcb"
        pidmap = {}
        bus = self._make_bus(pidmap)
        bus._open_file = mock.Mock(return_value=333)

        class RebuildEvent:
            def is_set(self):
                return False

            def wait(self, timeout):
                pidmap[requested] = 222
                return True

        bus._pidmap_rebuild_done = RebuildEvent()

        with mock.patch("workspace_bus.psutil.pid_exists", return_value=True):
            with mock.patch.object(
                bus,
                "_wait_for_ready_socket",
                return_value=("/tmp/api-222.sock", "ready", requested, None),
            ):
                reply = bus._resolve_socket(
                    {"action": "reload", "object": "fpc", "filepath": requested},
                    {},
                )

        self.assertEqual(reply["pid"], 222)
        bus._open_file.assert_not_called()

    def test_resolve_scans_manual_instance_before_opening_new_instance(self):
        requested = "/boards/fpc.kicad_pcb"
        pidmap = {}
        bus = self._make_bus(pidmap)
        bus._update_pid = lambda filepath, pid: pidmap.__setitem__(filepath, pid)
        bus._open_file = mock.Mock(return_value=333)

        with mock.patch(
            "workspace_bus._existing_kicad_sockets",
            return_value=[("/tmp/kicad/api-222.sock", 222)],
        ):
            with mock.patch("workspace_bus.psutil.pid_exists", return_value=True):
                with mock.patch(
                    "workspace_bus._socket_board_filepath_state",
                    return_value=("ready", requested, None),
                ):
                    with mock.patch.object(
                        bus,
                        "_wait_for_ready_socket",
                        return_value=(
                            "/tmp/kicad/api-222.sock", "ready", requested, None
                        ),
                    ):
                        reply = bus._resolve_socket(
                            {
                                "action": "reload",
                                "object": "fpc",
                                "filepath": requested,
                            },
                            {},
                        )

        self.assertEqual(reply["pid"], 222)
        self.assertEqual(pidmap, {requested: 222})
        bus._open_file.assert_not_called()

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

    def test_wait_for_ready_socket_ignores_other_instance_until_board_loads(self):
        bus = self._make_bus({})
        requested = "/boards/fpc.kicad_pcb"

        with mock.patch(
            "workspace_bus._kicad_socket_for_pid", return_value="/tmp/api.sock"
        ):
            with mock.patch(
                "workspace_bus._socket_board_filepath_state",
                side_effect=[
                    ("ready", "/boards/other.kicad_pcb", None),
                    ("ready", requested, None),
                ],
            ):
                with mock.patch("workspace_bus.time.sleep", return_value=None):
                    socket_path, state, actual_filepath, error = (
                        bus._wait_for_ready_socket(
                            123,
                            timeout=1.0,
                            interval=0.0,
                            expected_filepath=requested,
                        )
                    )

        self.assertEqual(socket_path, "/tmp/api.sock")
        self.assertEqual(state, "ready")
        self.assertEqual(actual_filepath, requested)
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

    def test_resolve_socket_does_not_reuse_unverified_board_path(self):
        requested = "/boards/fpc.kicad_pcb"
        removed = []
        pidmap = {requested: 111}
        bus = self._make_bus(pidmap)
        bus._remove_pid = removed.append
        bus._get_pidmap = lambda: {}

        with mock.patch("workspace_bus.psutil.pid_exists", return_value=True):
            with mock.patch.object(
                bus,
                "_wait_for_ready_socket",
                return_value=("/tmp/api.sock", "ready", None, None),
            ):
                reply = bus._resolve_socket(
                    {"action": "reload", "object": "fpc", "filepath": requested},
                    dict(pidmap),
                )

        self.assertEqual(reply["status"], "error")
        self.assertEqual(reply["message"], "file not in workspace")
        self.assertEqual(removed, [requested])

    def test_resolve_retry_reuses_pending_instance_while_modal_blocks_load(self):
        requested = "/boards/fpc.kicad_pcb"
        pidmap = {}
        open_file = mock.Mock(side_effect=lambda path: pidmap.setdefault(path, 111))
        bus = self._make_bus(pidmap)
        bus._open_file = open_file

        with mock.patch("workspace_bus._existing_kicad_sockets", return_value=[]):
            with mock.patch("workspace_bus.psutil.pid_exists", return_value=True):
                with mock.patch.object(
                    bus,
                    "_wait_for_ready_socket",
                    return_value=(
                        "/tmp/api.sock",
                        "not_ready",
                        None,
                        "waiting for requested board to load",
                    ),
                ) as wait_for_ready:
                    first = bus._resolve_socket(
                        {"action": "reload", "object": "fpc", "filepath": requested},
                        {},
                    )
                    second = bus._resolve_socket(
                        {"action": "reload", "object": "fpc", "filepath": requested},
                        dict(pidmap),
                    )

        self.assertEqual(first["status"], "error")
        self.assertEqual(second["status"], "error")
        open_file.assert_called_once_with(requested)
        self.assertEqual(bus._pending_open_pids, {requested: 111})
        self.assertEqual(wait_for_ready.call_count, 2)
        for call in wait_for_ready.call_args_list:
            self.assertEqual(call.args, (111,))
            self.assertEqual(
                call.kwargs,
                {"timeout": None, "expected_filepath": requested},
            )

    def test_pending_instance_is_cleared_after_requested_board_is_ready(self):
        requested = "/boards/fpc.kicad_pcb"
        pidmap = {requested: 111}
        bus = self._make_bus(pidmap)
        bus._pending_open_pids[requested] = 111

        with mock.patch("workspace_bus.psutil.pid_exists", return_value=True):
            with mock.patch.object(
                bus,
                "_wait_for_ready_socket",
                return_value=("/tmp/api-111.sock", "ready", requested, None),
            ):
                reply = bus._resolve_socket(
                    {"action": "reload", "object": "fpc", "filepath": requested},
                    dict(pidmap),
                )

        self.assertEqual(reply["pid"], 111)
        self.assertEqual(bus._pending_open_pids, {})

    def test_pending_instance_wait_stops_if_process_exits(self):
        bus = self._make_bus({})

        with mock.patch("workspace_bus.psutil.pid_exists", return_value=False):
            result = bus._wait_for_ready_socket(111, timeout=None, interval=0.0)

        self.assertEqual(
            result,
            (None, "exited", None, "KiCad process exited"),
        )


if __name__ == "__main__":
    unittest.main()
