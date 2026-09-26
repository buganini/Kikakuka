"""Tests for the editor logic executed by each mesh node."""

import os
import unittest
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from im import im_mesh
from im import instance_backend as backend
from im.kicad_api_retry import retry_kicad_call
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

        with mock.patch("im.kicad_api_retry.time.sleep"):
            self.assertEqual(retry_kicad_call(func, max_retries=5), "ok")
        self.assertEqual(attempts["count"], 3)


class InstanceBackendTests(unittest.TestCase):
    def test_editors_excludes_processes_owned_by_other_users(self):
        own = mock.Mock(
            pid=111,
            info={
                "pid": 111,
                "name": "pcbnew",
                "create_time": 1,
                "uids": mock.Mock(effective=1000),
            },
        )
        other = mock.Mock(
            pid=222,
            info={
                "pid": 222,
                "name": "pcbnew",
                "create_time": 2,
                "uids": mock.Mock(effective=2000),
            },
        )

        with mock.patch.object(
                    backend, "owned_process_iter", return_value=[own]
                ) as process_iter:
            self.assertEqual(backend._editors(), {111: 1})

        process_iter.assert_called_once_with(
            ["pid", "name", "create_time"]
        )

    def test_windows_editors_excludes_processes_owned_by_other_users(self):
        own = mock.Mock(
            pid=111,
            info={
                "pid": 111,
                "name": "pcbnew.exe",
                "create_time": 1,
                "username": r"WORKSTATION\alice",
            },
        )
        other = mock.Mock(
            pid=222,
            info={
                "pid": 222,
                "name": "pcbnew.exe",
                "create_time": 2,
                "username": r"WORKSTATION\bob",
            },
        )
        with mock.patch.object(
                    backend, "owned_process_iter", return_value=[own]
                ) as process_iter:
            self.assertEqual(backend._editors(), {111: 1})

        process_iter.assert_called_once_with(
            ["pid", "name", "create_time"]
        )

    def test_editors_excludes_non_editor_kicad_processes(self):
        processes = [
            mock.Mock(
                pid=pid,
                info={"pid": pid, "name": name, "create_time": pid},
            )
            for pid, name in enumerate(
                ("kicad", "pcbnew", "eeschema", "PCB Editor",
                 "kicad-api", "kicad-api.exe", "kicad-cli"),
                start=1,
            )
        ]
        with mock.patch.object(
            backend, "owned_process_iter", return_value=processes
        ):
            self.assertEqual(
                backend._editors(),
                {pid: pid for pid in range(1, 5)},
            )

    def test_windows_kicad_api_sentinel_is_created_once(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_kicad_socket_directory", return_value=directory
                ):
            sentinel = backend.ensure_windows_kicad_api_sentinel()
            self.assertEqual(sentinel, str(Path(directory) / "api.sock"))
            self.assertEqual(
                Path(sentinel).read_bytes(),
                backend.WINDOWS_KICAD_API_SENTINEL,
            )
            Path(sentinel).write_bytes(b"existing")
            self.assertEqual(
                backend.ensure_windows_kicad_api_sentinel(), sentinel
            )
            self.assertEqual(Path(sentinel).read_bytes(), b"existing")

    def test_windows_kicad_api_sentinel_does_not_replace_directory(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_kicad_socket_directory", return_value=directory
                ):
            sentinel = Path(directory) / "api.sock"
            sentinel.mkdir()
            self.assertIsNone(
                backend.ensure_windows_kicad_api_sentinel()
            )
            self.assertTrue(sentinel.is_dir())

    def test_windows_socket_candidates_come_from_named_pipe_enumeration(self):
        directory = r"C:\Temp\kicad"
        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_kicad_socket_directory", return_value=directory
                ), \
                mock.patch.object(
                    backend, "ensure_windows_kicad_api_sentinel"
                ), \
                mock.patch.object(
                    backend.os, "listdir", side_effect=[
                        ["api.sock"],
                        [r"C:\Temp\kicad\api-222.sock"],
                    ]
                ), \
                mock.patch.object(
                    backend, "_editors", return_value={111: 1, 222: 2}
                ), \
                mock.patch.object(
                    backend, "_windows_named_pipe_owner"
                ) as owner:
            self.assertEqual(backend._sockets(), [
                (222, os.path.join(directory, "api-222.sock")),
                (111, os.path.join(directory, "api.sock")),
            ])
        owner.assert_not_called()

    def test_windows_generic_pipe_uses_server_pid(self):
        directory = r"C:\Temp\kicad"
        editors = {111: 1, 222: 2}
        pipe_name = r"C:\Temp\kicad\api.sock"
        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_kicad_socket_directory", return_value=directory
                ), \
                mock.patch.object(
                    backend, "ensure_windows_kicad_api_sentinel"
                ), \
                mock.patch.object(
                    backend.os, "listdir", side_effect=[
                        ["api.sock"],
                        [pipe_name],
                    ]
                ), \
                mock.patch.object(backend, "_editors", return_value=editors), \
                mock.patch.object(
                    backend, "_windows_named_pipe_owner", return_value=222
                ) as owner:
            self.assertEqual(backend._sockets(), [
                (222, os.path.join(directory, "api.sock")),
            ])

        owner.assert_called_once_with(
            rf"\\.\pipe\{pipe_name}", editors, set()
        )

    def test_windows_named_pipe_owner_validates_live_editor(self):
        process = mock.Mock()
        process.create_time.return_value = 2
        pipe_path = r"\\.\pipe\C:\Temp\kicad\api.sock"

        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_windows_named_pipe_server_pid",
                    return_value=222,
                ), \
                mock.patch.object(
                    backend, "owned_process", return_value=process
                ):
            self.assertEqual(
                backend._windows_named_pipe_owner(
                    pipe_path, {111: 1, 222: 2}, excluded_pids={111}
                ),
                222,
            )

    def test_windows_named_pipe_server_pid_closes_handle(self):
        create_file = mock.Mock(return_value=123)
        get_server_pid = mock.Mock()

        def return_pid(_handle, pid_pointer):
            pid_pointer._obj.value = 222
            return True

        get_server_pid.side_effect = return_pid
        close_handle = mock.Mock(return_value=True)
        kernel32 = mock.Mock(
            CreateFileW=create_file,
            GetNamedPipeServerProcessId=get_server_pid,
            CloseHandle=close_handle,
        )
        pipe_path = r"\\.\pipe\C:\Temp\kicad\api.sock"

        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch(
                    "ctypes.WinDLL", return_value=kernel32, create=True
                ):
            self.assertEqual(
                backend._windows_named_pipe_server_pid(pipe_path), 222
            )

        create_file.assert_called_once_with(
            pipe_path, 0, 0, None, 3, 0, None
        )
        get_server_pid.assert_called_once()
        close_handle.assert_called_once_with(123)

    def test_windows_named_pipe_owner_ignores_unknown_or_reused_pid(self):
        pipe_path = r"\\.\pipe\C:\Temp\kicad\api.sock"

        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_windows_named_pipe_server_pid",
                    side_effect=[333, 222],
                ), \
                mock.patch.object(
                    backend, "owned_process", return_value=None
                ) as owned_process:
            self.assertIsNone(
                backend._windows_named_pipe_owner(pipe_path, {222: 2})
            )
            owned_process.assert_not_called()
            self.assertIsNone(
                backend._windows_named_pipe_owner(pipe_path, {222: 2})
            )
            owned_process.assert_called_once_with(222, 2)

    def test_unix_socket_owner_scans_only_same_user_unmapped_editors(self):
        owner = mock.Mock()
        owner.net_connections.return_value = [
            mock.Mock(laddr="/tmp/kicad/api.sock"),
        ]
        processes = {111: None, 333: owner}

        with mock.patch.object(backend.platform, "system", return_value="Linux"), \
                mock.patch.object(
                    backend, "owned_process",
                    side_effect=lambda pid, _created: processes[pid],
                ) as process:
            self.assertEqual(
                backend._unix_socket_owner(
                    "/tmp/kicad/api.sock",
                    {111: 1, 222: 2, 333: 3},
                    excluded_pids={222},
                ),
                333,
            )

        self.assertEqual(process.call_args_list, [mock.call(111, 1),
                                                  mock.call(333, 3)])
        owner.net_connections.assert_called_once_with(kind="unix")

    def test_unix_socket_owner_continues_after_process_error(self):
        inaccessible = mock.Mock()
        inaccessible.uids.return_value = mock.Mock(effective=1000)
        inaccessible.create_time.return_value = 1
        inaccessible.net_connections.side_effect = backend.psutil.AccessDenied(
            pid=111
        )
        owner = mock.Mock()
        owner.net_connections.return_value = [
            mock.Mock(laddr="/tmp/kicad/api.sock"),
        ]

        with mock.patch.object(backend.platform, "system", return_value="Darwin"), \
                mock.patch.object(
                    backend, "owned_process",
                    side_effect=lambda pid, _created: {
                        111: inaccessible, 222: owner
                    }[pid],
                ):
            self.assertEqual(
                backend._unix_socket_owner(
                    "/tmp/kicad/api.sock", {111: 1, 222: 2}
                ),
                222,
            )

    def test_unix_socket_owner_retries_before_fallback(self):
        owner = mock.Mock()
        owner.net_connections.side_effect = [
            [],
            [mock.Mock(laddr="/tmp/kicad/api.sock")],
        ]

        with mock.patch.object(backend.platform, "system", return_value="Linux"), \
                mock.patch.object(
                    backend, "owned_process", return_value=owner
                ) as owned_process, \
                mock.patch.object(backend.time, "sleep") as sleep:
            self.assertEqual(
                backend._unix_socket_owner(
                    "/tmp/kicad/api.sock",
                    {111: 1},
                    max_retries=2,
                    delay_s=0.05,
                ),
                111,
            )

        self.assertEqual(owned_process.call_count, 2)
        self.assertEqual(owner.net_connections.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_scan_open_boards_reads_each_reachable_kicad_endpoint(self):
        with mock.patch.object(backend, "_sockets", return_value=[
                (111, "/tmp/kicad/api.sock"),
                (222, "/tmp/kicad/api-222.sock"),
                (333, "/tmp/kicad/api-333.sock"),
        ]), mock.patch.object(backend, "_board_path", side_effect=[
                "/boards/one.kicad_pcb", RuntimeError("busy"),
                "/boards/three.kicad_pcb",
        ]):
            self.assertEqual(backend.scan_open_kicad_boards(), [
                (111, "/boards/one.kicad_pcb", "/tmp/kicad/api.sock"),
                (333, "/boards/three.kicad_pcb", "/tmp/kicad/api-333.sock"),
            ])

    def test_mac_freecad_launch_uses_explicit_app_and_file_argument(self):
        with mock.patch.object(backend.platform, "system", return_value="Darwin"), \
                mock.patch.object(backend, "_editors", side_effect=[{}, {321: 1}]), \
                mock.patch.object(backend.subprocess, "Popen") as popen:
            self.assertEqual(backend._launch("/models/part.FCStd", "freecad"), 321)
        popen.assert_called_once_with([
            "open", "-a", "FreeCAD", "-n", "-W", "--args", "/models/part.FCStd"])

    def test_windows_kicad_launch_ensures_api_sentinel(self):
        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "ensure_windows_kicad_api_sentinel"
                ) as ensure, \
                mock.patch.object(
                    backend, "_editors", side_effect=[{}, {321: 1}]
                ), \
                mock.patch.object(
                    backend.os, "startfile", create=True
                ) as startfile:
            self.assertEqual(
                backend._launch("C:/boards/test.kicad_pcb"), 321
            )
        ensure.assert_called_once_with()
        startfile.assert_called_once_with("C:/boards/test.kicad_pcb")

    def test_different_board_files_do_not_overlap_editor_launch(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def launch(_filepath, _program):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.1)
            with guard:
                active -= 1
            return 123

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(backend, "_find_board", return_value=(None, None, None)), \
                    mock.patch.object(backend, "_launch", side_effect=launch), \
                    mock.patch.object(backend, "_wait_for_board", return_value=(123, "/ipc/api.sock")):
                threads = [threading.Thread(target=backend._open_new,
                                            args=(f"/boards/{name}.kicad_pcb", "kicad", True, None))
                           for name in ("first", "second")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(maximum, 1)

    def test_board_launch_waits_for_verified_path_before_releasing_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(backend, "_find_board", return_value=(None, None, None)), \
                    mock.patch.object(backend, "_launch", return_value=111), \
                    mock.patch.object(backend, "_wait_for_board", return_value=(222, "/ipc/api-222.sock")) as wait:
                pid, socket_path = backend._open_new(
                    "/boards/main.kicad_pcb", "kicad", True, None)
        self.assertEqual((pid, socket_path), (222, "/ipc/api-222.sock"))
        wait.assert_called_once_with("/boards/main.kicad_pcb")

    def test_board_reuses_only_verified_matching_socket(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/boards/main.kicad_pcb": 111}
        board = mock.Mock()
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_find_board",
                    return_value=(111, "/ipc/api.sock", board),
                ), \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus"):
            reply = backend.handle({"action": "open-file", "filepath": "/boards/main.kicad_pcb"})
        self.assertEqual(reply["pid"], 111)
        self.assertEqual(reply["socket"], "/ipc/api.sock")
        board.revert.assert_not_called()
        launch.assert_not_called()
        node.publish.assert_not_called()

    def test_ensure_fresh_reuses_ready_probe_connection_for_revert(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/boards/main.kicad_pcb": 111}
        board = mock.Mock()
        board.name = "/boards/main.kicad_pcb"
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_sockets",
                    return_value=[(111, "/ipc/api.sock")],
                ), \
                mock.patch.object(
                    backend, "_ready_board", return_value=board,
                ) as ready, \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus"):
            reply = backend.handle({
                "action": "open-file",
                "filepath": "/boards/main.kicad_pcb",
                "ensure_fresh": True,
            })
        self.assertEqual(reply["pid"], 111)
        ready.assert_called_once_with(
            "/ipc/api.sock",
            max_retries=backend.FRESH_READY_RETRIES,
            delay_s=backend.FRESH_READY_DELAY_S,
        )
        board.revert.assert_called_once_with()
        board.get_shapes.assert_called_once_with()
        launch.assert_not_called()

    def test_ensure_fresh_handles_board_opened_while_waiting_for_launch_lock(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        board = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(backend, "local_node", return_value=node), \
                    mock.patch.object(backend.os.path, "isfile", return_value=True), \
                    mock.patch.object(backend, "_find_board", side_effect=[
                        (None, None, None),
                        (111, "/ipc/api.sock", board),
                    ]), \
                    mock.patch.object(backend, "_launch") as launch, \
                    mock.patch.object(backend, "_focus"):
                reply = backend.handle({
                    "action": "open-file",
                    "filepath": "/boards/main.kicad_pcb",
                    "ensure_fresh": True,
                })
        self.assertEqual(reply["pid"], 111)
        board.revert.assert_called_once_with()
        board.get_shapes.assert_called_once_with()
        launch.assert_not_called()

    def test_mismatched_board_mapping_is_invalidated(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/boards/main.kicad_pcb": 111}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_find_board",
                    return_value=(222, "/ipc/api-222.sock", mock.Mock()),
                ), \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus"):
            reply = backend.handle({"action": "open-file", "filepath": "/boards/main.kicad_pcb"})
        self.assertEqual(reply["pid"], 222)
        node.publish.assert_called_once_with("/boards/main.kicad_pcb", None)
        launch.assert_not_called()

    def test_monitor_does_not_launch_unopened_board(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "_find_board", return_value=(None, None, None)), \
                mock.patch.object(backend, "_launch") as launch:
            reply = backend.handle({"action": "monitor-couplers", "filepath": "/boards/main.kicad_pcb"})
        self.assertEqual(reply["status"], "error")
        launch.assert_not_called()

    def test_resolve_includes_caller_fields_and_verified_socket(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_find_board",
                    return_value=(123, "/ipc/api.sock", mock.Mock()),
                ):
            reply = backend.handle({"action": "move-component", "filepath": "/boards/main.kicad_pcb",
                                    "object": "Board", "component": "U1"})
        self.assertEqual(reply["socket"], "/ipc/api.sock")
        self.assertEqual(reply["object"], "Board")
        self.assertEqual(reply["component"], "U1")

    def test_freecad_file_uses_same_open_and_focus_path(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
                mock.patch.object(backend, "_editors", return_value={}), \
                mock.patch.object(backend, "bind_freecad_source", return_value=True), \
                mock.patch.object(backend, "_launch", return_value=321) as launch, \
                mock.patch.object(backend, "_focus") as focus:
            reply = backend.handle({"action": "open-file", "filepath": "/models/part.FCStd"})
        self.assertEqual(reply["pid"], 321)
        launch.assert_called_once_with("/models/part.FCStd", "freecad")
        focus.assert_called_once_with(321)

    def test_freecad_live_document_overrides_stale_pidmap(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/models/part.FCStd": 111}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "owned_pid_exists", return_value=True), \
                mock.patch.object(backend, "activate_open_freecad_document",
                                  return_value=222) as activate, \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus") as focus:
            reply = backend.handle({"action": "open-file", "filepath": "/models/part.FCStd"})
        self.assertEqual(reply["pid"], 222)
        activate.assert_called_once_with(
            "/models/part.FCStd", before_activate=focus)
        launch.assert_not_called()
        focus.assert_called_once_with(222)

    def test_freecad_closed_document_is_not_reused_from_pidmap(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/models/part.FCStd": 111}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "owned_pid_exists", return_value=True), \
                mock.patch.object(backend, "activate_open_freecad_document",
                                  return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
                mock.patch.object(backend, "_editors", return_value={}), \
                mock.patch.object(backend, "bind_freecad_source", return_value=True), \
                mock.patch.object(backend, "_launch", return_value=321) as launch, \
                mock.patch.object(backend, "_focus"):
            reply = backend.handle({"action": "open-file", "filepath": "/models/part.FCStd"})
        self.assertEqual(reply["pid"], 321)
        launch.assert_called_once_with("/models/part.FCStd", "freecad")

    def test_new_freecad_file_opens_in_existing_gui_node(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "activate_open_freecad_document",
                                  return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node",
                                  return_value=222) as open_in_node, \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus") as focus:
            reply = backend.handle({"action": "open-file", "filepath": "/models/new.step"})
        self.assertEqual(reply["pid"], 222)
        open_in_node.assert_called_once_with(
            "/models/new.step", before_open=focus)
        launch.assert_not_called()
        focus.assert_called_once_with(222)

    def test_freecad_node_import_failure_does_not_launch_duplicate(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "activate_open_freecad_document",
                                  return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node",
                                  side_effect=RuntimeError("import failed")), \
                mock.patch.object(backend, "_launch") as launch:
            with self.assertRaisesRegex(RuntimeError, "import failed"):
                backend.handle({"action": "open-file", "filepath": "/models/new.step"})
        launch.assert_not_called()

    def test_unreachable_running_freecad_does_not_launch_duplicate(self):
        with mock.patch.object(backend, "activate_open_freecad_document",
                               return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
                mock.patch.object(backend, "_editors", return_value={321: 1}), \
                mock.patch.object(backend, "_launch") as launch:
            with self.assertRaisesRegex(RuntimeError, "node is unavailable"):
                backend._open_new("/models/part.FCStd", "freecad", False, None)
        launch.assert_not_called()

    def test_freecad_launch_is_not_success_until_document_is_identified(self):
        with mock.patch.object(backend, "activate_open_freecad_document",
                               return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
                mock.patch.object(backend, "_editors", return_value={}), \
                mock.patch.object(backend, "_launch", return_value=321), \
                mock.patch.object(backend, "bind_freecad_source",
                                  return_value=False) as bind:
            with self.assertRaisesRegex(RuntimeError, "did not report"):
                backend._open_new("/models/part.step", "freecad", False, None)
        bind.assert_called_once_with(321, "/models/part.step")

    def test_sockets_fall_back_to_oldest_unmapped_editor(self):
        with mock.patch.object(backend.os, "listdir", return_value=["api.sock", "api-222.sock"]), \
                mock.patch.object(backend, "_editors", return_value={111: 1, 222: 2}), \
                mock.patch.object(
                    backend, "_unix_socket_owner", return_value=None
                ) as owner:
            sockets = backend._sockets()
        self.assertEqual(dict(sockets)[111].split("/")[-1], "api.sock")
        self.assertEqual(dict(sockets)[222].split("/")[-1], "api-222.sock")
        owner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
