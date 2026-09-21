"""Integration checks for the on-demand instance mesh."""

import os
from pathlib import Path
import hashlib
import socket
import tempfile
import threading
import time
import multiprocessing
import types
import unittest
from unittest import mock

from FreekiCAD.freecad.FreekiCAD import im_mesh


def _child_node(runtime_path, ready, stop):
    im_mesh.runtime_dir = lambda: Path(runtime_path)
    node = im_mesh.InstanceNode(lambda _: {"status": "ok", "owner": os.getpid()})
    try:
        ready.put(os.getpid())
        stop.wait(10)
    finally:
        node.close()


class InstanceMeshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runtime_patch = mock.patch.object(
            im_mesh, "runtime_dir", return_value=Path(self.directory.name))
        self.runtime_patch.start()
        self.nodes = []

    def tearDown(self):
        for node in self.nodes:
            node.close()
        self.runtime_patch.stop()
        self.directory.cleanup()

    def node(self, handle, on_change=None, kicad_api=None):
        node = im_mesh.InstanceNode(handle, on_change, kicad_api)
        self.nodes.append(node)
        return node

    def test_elects_live_node_and_fails_over_after_close(self):
        first = self.node(lambda _: {"status": "ok", "owner": "first"})
        second = self.node(lambda _: {"status": "ok", "owner": "second"})
        elected = min((first, second), key=lambda node: node.id)
        self.assertEqual(im_mesh.request({"action": "probe"})["owner"],
                         "first" if elected is first else "second")
        elected.close()
        self.nodes.remove(elected)
        self.assertEqual(im_mesh.request({"action": "probe"})["owner"],
                         "second" if elected is first else "first")

    def test_singleton_can_restart_after_close(self):
        first = im_mesh.start_node(lambda _: {"status": "ok"})
        self.nodes.append(first)
        first.close()
        self.nodes.remove(first)
        second = im_mesh.start_node(lambda _: {"status": "ok"})
        self.nodes.append(second)
        self.assertIsNot(first, second)

    @unittest.skipIf(os.name == "nt", "Unix socket cleanup")
    def test_endpoint_contains_pid_and_start_time_without_registration_json(self):
        node = self.node(lambda _: {"status": "ok"})
        self.assertEqual(im_mesh._parse_endpoint(node.endpoint),
                         (os.getpid(), node.started_ms))
        self.assertEqual(list(Path(self.directory.name).glob("*.json")), [])

    @unittest.skipIf(os.name == "nt", "Unix socket cleanup")
    def test_discovery_removes_stale_socket_after_pid_reuse(self):
        stale = im_mesh._endpoint(os.getpid(), im_mesh._started_ms(os.getpid()) - 1000)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(stale)
        self.assertTrue(Path(stale).exists())
        self.assertEqual(im_mesh.discover(), [])
        self.assertFalse(Path(stale).exists())

    @unittest.skipIf(os.name == "nt", "Unix socket cleanup")
    def test_discovery_does_not_remove_a_busy_live_socket(self):
        endpoint = im_mesh._endpoint(os.getpid(), im_mesh._started_ms(os.getpid()))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(endpoint)
            listener.listen(16)
            self.assertEqual(im_mesh.discover(), [])
            self.assertTrue(Path(endpoint).exists())
        Path(endpoint).unlink()

    def test_windows_pipe_name_can_be_discovered_by_enumeration(self):
        fake_os = types.SimpleNamespace(name="nt", path=os.path)
        with mock.patch.object(im_mesh, "os", fake_os), \
                mock.patch.object(im_mesh, "_windows_user_sid",
                                  return_value="S-1-5-21-123-456"), \
                mock.patch.object(im_mesh, "runtime_dir",
                                  return_value=Path(self.directory.name)):
            endpoint = im_mesh._endpoint(123, 456789)
            fake_os.listdir = mock.Mock(return_value=["unrelated", endpoint.split("\\")[-1]])
            self.assertEqual(im_mesh._candidate_endpoints(), [endpoint])
            self.assertEqual(im_mesh._parse_endpoint(endpoint), (123, 456789))

    def test_windows_pipe_discovery_falls_back_to_process_list(self):
        fake_os = types.SimpleNamespace(name="nt", path=os.path,
                                        listdir=mock.Mock(side_effect=OSError))
        process = types.SimpleNamespace(pid=123, info={"create_time": 456.789})
        with mock.patch.object(im_mesh, "os", fake_os), \
                mock.patch.object(im_mesh, "_windows_user_sid",
                                  return_value="S-1-5-21-123-456"), \
                mock.patch.object(im_mesh, "runtime_dir",
                                  return_value=Path(self.directory.name)), \
                mock.patch.object(im_mesh.psutil, "process_iter", return_value=[process]):
            self.assertEqual(im_mesh._candidate_endpoints(),
                             [im_mesh._endpoint(123, 456789)])

    def test_windows_pipe_scope_uses_sid_not_runtime_directory(self):
        fake_os = types.SimpleNamespace(name="nt", path=os.path)
        sid = "S-1-5-21-123-456"
        scope = hashlib.sha256(sid.encode("ascii")).hexdigest()[:16]
        with mock.patch.object(im_mesh, "os", fake_os), \
                mock.patch.object(im_mesh, "_windows_user_sid", return_value=sid):
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path("A")):
                endpoint = im_mesh._endpoint(123, 456789)
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path("B")):
                self.assertEqual(im_mesh._endpoint(123, 456789), endpoint)
                self.assertEqual(im_mesh._parse_endpoint(endpoint), (123, 456789))
            self.assertEqual(endpoint,
                             rf"\\.\pipe\kikakuka-{scope}-123-456789")
            with mock.patch.object(im_mesh, "_windows_user_sid",
                                   return_value="S-1-5-21-999"):
                self.assertIsNone(im_mesh._parse_endpoint(endpoint))

    def test_windows_runtime_directory_fallback_uses_sid(self):
        fake_os = types.SimpleNamespace(name="nt", environ={})
        with mock.patch.object(im_mesh, "os", fake_os), \
                mock.patch.object(im_mesh, "_windows_user_sid",
                                  return_value="S-1-5-21-123-456"):
            self.runtime_patch.stop()
            try:
                self.assertEqual(im_mesh.runtime_dir().name,
                                 f"kikakuka-{im_mesh._windows_user_scope()}")
            finally:
                self.runtime_patch.start()

    @unittest.skipUnless(os.name == "nt", "requires Windows access token")
    def test_windows_sid_is_read_from_process_token(self):
        self.assertRegex(im_mesh._windows_user_sid(), r"^S-\d+(?:-\d+)+$")

    def test_process_exit_triggers_lower_pid_failover(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        stops = [context.Event(), context.Event()]
        children = [context.Process(target=_child_node,
                                    args=(self.directory.name, ready, stop))
                    for stop in stops]
        try:
            for child in children:
                child.start()
                ready.get(timeout=8)
            lowest = min(children, key=lambda child: child.pid)
            self.assertEqual(im_mesh.request({"action": "probe"})["owner"], lowest.pid)
            stops[children.index(lowest)].set()
            lowest.join(5)
            self.assertFalse(lowest.is_alive())
            survivor = next(child for child in children if child is not lowest)
            self.assertEqual(im_mesh.request({"action": "probe"})["owner"], survivor.pid)
        finally:
            for stop in stops:
                stop.set()
            for child in children:
                child.join(5)
                if child.is_alive():
                    child.terminate()
                    child.join(5)

    def test_broadcast_and_snapshot_for_late_joiner(self):
        one = self.node(lambda _: {"status": "ok"})
        path = os.path.realpath(os.path.join(self.directory.name, "board.kicad_pcb"))
        one.publish(path, os.getpid())
        two = self.node(lambda _: {"status": "ok"})
        self.assertEqual(two.snapshot()[path], os.getpid())
        one.publish(path, None)
        self.assertNotIn(path, two.snapshot())

    def test_freecad_document_scan_queries_each_gui_node(self):
        first = self.node(lambda _: {"status": "ok"})
        second = self.node(lambda _: {"status": "ok"})
        without_provider = self.node(lambda _: {"status": "ok"})
        first.set_document_provider(lambda: ["/models/one.FCStd"])
        second.set_document_provider(lambda: ["/models/two.FCStd"])

        self.assertEqual(im_mesh.scan_freecad_documents(), [
            (os.getpid(), ("/models/one.FCStd", "/models/two.FCStd")),
        ])
        self.assertEqual(im_mesh._exchange(first.endpoint, {
            "mesh_action": "freecad-list-documents"}, token=first.token), {
            "status": "ok", "pid": os.getpid(),
            "documents": ["/models/one.FCStd"],
        })
        self.assertEqual(im_mesh._exchange(without_provider.endpoint, {
            "mesh_action": "freecad-list-documents"},
            token=without_provider.token)["status"], "error")

    def test_freecad_activation_checks_live_nodes(self):
        first = self.node(lambda _: {"status": "ok"})
        second = self.node(lambda _: {"status": "ok"})
        first.set_document_provider(lambda: [])
        second.set_document_provider(lambda: ["/models/part.FCStd"])
        first.set_document_activator(lambda _: False)
        activated = []
        second.set_document_activator(
            lambda path: activated.append(path) or path == "/models/part.FCStd")

        self.assertEqual(im_mesh.activate_open_freecad_document(
            "/models/part.FCStd"), os.getpid())
        self.assertEqual(activated, ["/models/part.FCStd"])
        self.assertIsNone(im_mesh.activate_open_freecad_document(
            "/models/missing.FCStd"))
        self.assertEqual(im_mesh._exchange(first.endpoint, {
            "mesh_action": "freecad-activate-document",
            "filepath": "relative.FCStd"}, token=first.token)["status"], "error")

    def test_freecad_open_targets_gui_node_and_returns_after_open(self):
        nongui = self.node(lambda _: {"status": "ok"})
        gui = self.node(lambda _: {"status": "ok"})
        opened = []
        gui.set_document_provider(lambda: [])
        gui.set_document_opener(lambda path: opened.append(path) or True)
        self.assertEqual(im_mesh.open_in_freecad_node(
            "/models/new.FCStd"), os.getpid())
        self.assertEqual(opened, ["/models/new.FCStd"])
        self.assertIsNone(nongui.document_opener)

    def test_freecad_open_reports_import_error(self):
        gui = self.node(lambda _: {"status": "ok"})
        gui.set_document_provider(lambda: [])
        gui.set_document_opener(lambda _: (_ for _ in ()).throw(
            ValueError("bad STEP")))
        with self.assertRaisesRegex(RuntimeError, "bad STEP"):
            im_mesh.open_in_freecad_node("/models/bad.step")

    def test_existing_gui_without_open_handler_does_not_trigger_new_launch(self):
        gui = self.node(lambda _: {"status": "ok"})
        gui.set_document_provider(lambda: [])
        with self.assertRaisesRegex(RuntimeError, "cannot open"):
            im_mesh.open_in_freecad_node("/models/new.step")

    def test_bind_source_reports_imported_document_to_launch_caller(self):
        gui = self.node(lambda _: {"status": "ok"})
        gui.set_document_provider(lambda: [])
        gui.set_source_registrar(lambda path: path == "/models/part.step")
        self.assertTrue(im_mesh.bind_freecad_source(os.getpid(),
                                                    "/models/part.step"))

    def test_request_id_is_idempotent_on_one_node(self):
        calls = []
        node = self.node(lambda _: calls.append(1) or {"status": "ok"})
        request = {"mesh_action": "dispatch", "id": "same-id",
                   "request": {"action": "probe"}}
        self.assertEqual(im_mesh._exchange(node.endpoint, request, token=node.token)["status"], "accepted")
        self.assertEqual(im_mesh._exchange(node.endpoint, request, token=node.token)["status"], "accepted")
        deadline = im_mesh.time.monotonic() + 2
        while im_mesh.time.monotonic() < deadline:
            if im_mesh._exchange(node.endpoint, {"mesh_action": "result", "id": "same-id"}, token=node.token)["status"] != "pending":
                break
        self.assertEqual(calls, [1])

    def test_node_rejects_request_without_shared_token(self):
        node = self.node(lambda _: {"status": "ok"})
        self.assertEqual(im_mesh._exchange(
            node.endpoint, {"mesh_action": "snapshot"}, token="wrong")["status"],
            "error")

    def test_pcb_request_skips_node_without_kicad_dependency(self):
        self.node(lambda _: {"status": "ok", "owner": "without-kipy"}, kicad_api=False)
        self.node(lambda _: {"status": "ok", "owner": "with-kipy"}, kicad_api=True)
        reply = im_mesh.request({"action": "open-file", "filepath": "/boards/main.kicad_pcb"})
        self.assertEqual(reply["owner"], "with-kipy")

    def test_concurrent_nodes_serialize_the_same_file_open(self):
        path = os.path.realpath(os.path.join(self.directory.name, "part.FCStd"))
        launched = []
        nodes = {}

        def make_handler(name):
            def handle(request):
                if request["filepath"] not in nodes[name].snapshot():
                    launched.append(name)
                    time.sleep(0.1)
                return {"status": "ok", "action": "open-file",
                        "filepath": request["filepath"], "pid": os.getpid()}
            return handle

        nodes["a"] = self.node(make_handler("a"))
        nodes["b"] = self.node(make_handler("b"))
        replies = []

        def send(name):
            node = nodes[name]
            request_id = f"request-{name}"
            replies.append(im_mesh._exchange(node.endpoint, {
                "mesh_action": "dispatch", "id": request_id,
                "request": {"action": "open-file", "filepath": path}},
                token=node.token))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = im_mesh._exchange(node.endpoint, {
                    "mesh_action": "result", "id": request_id}, token=node.token)
                if result["status"] != "pending":
                    replies.append(result)
                    return
                time.sleep(0.01)
            self.fail("mesh request did not complete")

        threads = [threading.Thread(target=send, args=(name,)) for name in nodes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(launched), 1)
        self.assertEqual(sum(reply["status"] == "ok" for reply in replies), 2)


if __name__ == "__main__":
    unittest.main()
