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
    def test_mac_freecad_launch_uses_explicit_app_and_file_argument(self):
        with mock.patch.object(backend.platform, "system", return_value="Darwin"), \
                mock.patch.object(backend, "_editors", side_effect=[{}, {321: 1}]), \
                mock.patch.object(backend.subprocess, "Popen") as popen:
            self.assertEqual(backend._launch("/models/part.FCStd", "freecad"), 321)
        popen.assert_called_once_with([
            "open", "-a", "FreeCAD", "-n", "-W", "--args", "/models/part.FCStd"])

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
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
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
                mock.patch.object(backend.psutil, "pid_exists", return_value=True), \
                mock.patch.object(backend, "activate_open_freecad_document",
                                  return_value=222) as activate, \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus") as focus:
            reply = backend.handle({"action": "open-file", "filepath": "/models/part.FCStd"})
        self.assertEqual(reply["pid"], 222)
        activate.assert_called_once_with("/models/part.FCStd")
        launch.assert_not_called()
        focus.assert_called_once_with(222)

    def test_freecad_closed_document_is_not_reused_from_pidmap(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/models/part.FCStd": 111}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend.psutil, "pid_exists", return_value=True), \
                mock.patch.object(backend, "activate_open_freecad_document",
                                  return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
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
        open_in_node.assert_called_once_with("/models/new.step")
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

    def test_freecad_launch_is_not_success_until_document_is_identified(self):
        with mock.patch.object(backend, "activate_open_freecad_document",
                               return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
                mock.patch.object(backend, "_launch", return_value=321), \
                mock.patch.object(backend, "bind_freecad_source",
                                  return_value=False) as bind:
            with self.assertRaisesRegex(RuntimeError, "did not report"):
                backend._open_new("/models/part.step", "freecad", False, None)
        bind.assert_called_once_with(321, "/models/part.step")

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
