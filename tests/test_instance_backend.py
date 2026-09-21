"""Tests for the editor logic executed by each mesh node."""

import unittest
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from FreekiCAD.freecad.FreekiCAD import instance_backend as backend
from FreekiCAD.freecad.FreekiCAD import im_mesh
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

        with mock.patch("FreekiCAD.freecad.FreekiCAD.kicad_api_retry.time.sleep"):
            self.assertEqual(retry_kicad_call(func, max_retries=5), "ok")
        self.assertEqual(attempts["count"], 3)


class InstanceBackendTests(unittest.TestCase):
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
                    mock.patch.object(backend, "_find_board", return_value=(None, None)), \
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
                    mock.patch.object(backend, "_find_board", return_value=(None, None)), \
                    mock.patch.object(backend, "_launch", return_value=111), \
                    mock.patch.object(backend, "_wait_for_board", return_value=(222, "/ipc/api-222.sock")) as wait:
                pid, socket_path = backend._open_new(
                    "/boards/main.kicad_pcb", "kicad", True, None)
        self.assertEqual((pid, socket_path), (222, "/ipc/api-222.sock"))
        wait.assert_called_once_with("/boards/main.kicad_pcb")

    def test_board_reuses_only_verified_matching_socket(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/boards/main.kicad_pcb": 111}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "_find_board", return_value=(111, "/ipc/api.sock")), \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus"):
            reply = backend.handle({"action": "open-file", "filepath": "/boards/main.kicad_pcb"})
        self.assertEqual(reply["pid"], 111)
        launch.assert_not_called()
        node.publish.assert_not_called()

    def test_mismatched_board_mapping_is_invalidated(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/boards/main.kicad_pcb": 111}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "_find_board", return_value=(222, "/ipc/api-222.sock")), \
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
                mock.patch.object(backend, "_find_board", return_value=(None, None)), \
                mock.patch.object(backend, "_launch") as launch:
            reply = backend.handle({"action": "monitor-couplers", "filepath": "/boards/main.kicad_pcb"})
        self.assertEqual(reply["status"], "error")
        launch.assert_not_called()

    def test_resolve_includes_caller_fields_and_verified_socket(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "_find_board", return_value=(123, "/ipc/api.sock")):
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
                mock.patch.object(backend, "_launch", return_value=321) as launch, \
                mock.patch.object(backend, "_focus") as focus:
            reply = backend.handle({"action": "open-file", "filepath": "/models/part.FCStd"})
        self.assertEqual(reply["pid"], 321)
        launch.assert_called_once_with("/models/part.FCStd", "freecad")
        focus.assert_called_once_with(321)

    def test_sockets_do_not_use_privileged_connections(self):
        with mock.patch.object(backend.os, "listdir", return_value=["api.sock", "api-222.sock"]), \
                mock.patch.object(backend, "_editors", return_value={111: 1, 222: 2}), \
                mock.patch.object(backend.psutil, "net_connections") as privileged:
            sockets = backend._sockets()
        self.assertEqual(dict(sockets)[111].split("/")[-1], "api.sock")
        self.assertEqual(dict(sockets)[222].split("/")[-1], "api-222.sock")
        privileged.assert_not_called()


if __name__ == "__main__":
    unittest.main()
