"""Integration checks for the on-demand instance mesh."""

import os
from pathlib import Path
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
                mock.patch.object(im_mesh, "runtime_dir",
                                  return_value=Path(self.directory.name)), \
                mock.patch.object(im_mesh.psutil, "process_iter", return_value=[process]):
            self.assertEqual(im_mesh._candidate_endpoints(),
                             [im_mesh._endpoint(123, 456789)])

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
