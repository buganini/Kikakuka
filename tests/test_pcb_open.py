import json
import os
import unittest
from unittest import mock

import pcb_open


class FakeConnection:
    def __init__(self, reply):
        payload = json.dumps(reply).encode("utf-8")
        self.remaining = len(payload).to_bytes(4, "big") + payload
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        # Exercise the client's partial-read handling.
        chunk = self.remaining[:min(size, 3)]
        self.remaining = self.remaining[len(chunk):]
        return chunk


class PcbOpenTests(unittest.TestCase):
    def test_workspace_request_uses_length_prefixed_open_file_protocol(self):
        path = "/boards/panel.kicad_pcb"
        connection = FakeConnection({
            "status": "ok", "action": "open-file", "filepath": path,
            "pid": 123,
        })
        with mock.patch("pcb_open._connect_workspace", return_value=connection):
            self.assertTrue(pcb_open.request_workspace_open(path))

        request_size = int.from_bytes(connection.sent[:4], "big")
        self.assertEqual(request_size, len(connection.sent) - 4)
        self.assertEqual(
            json.loads(connection.sent[4:].decode("utf-8")),
            {"action": "open-file", "filepath": path},
        )

    def test_workspace_request_rejects_response_for_another_file(self):
        connection = FakeConnection({
            "status": "ok", "action": "open-file",
            "filepath": "/boards/other.kicad_pcb", "pid": 123,
        })
        with mock.patch("pcb_open._connect_workspace", return_value=connection):
            self.assertFalse(
                pcb_open.request_workspace_open("/boards/panel.kicad_pcb")
            )

    def test_windows_workspace_connection_uses_local_tcp(self):
        connection = object()
        with mock.patch("pcb_open.platform.system", return_value="Windows"):
            with mock.patch(
                "pcb_open.socket.create_connection", return_value=connection
            ) as connect:
                self.assertIs(pcb_open._connect_workspace(3.0), connection)
        connect.assert_called_once_with(
            ("127.0.0.1", pcb_open.WORKSPACE_PORT), 3.0
        )

    def test_manager_success_does_not_open_file_again(self):
        with mock.patch("pcb_open.os.path.isfile", return_value=True):
            with mock.patch("pcb_open.request_workspace_open", return_value=True):
                with mock.patch("pcb_open.open_with_system") as fallback:
                    result = pcb_open.open_pcb_file("/boards/panel.kicad_pcb")
        self.assertEqual(result, "workspace")
        fallback.assert_not_called()

    def test_schematic_uses_the_same_workspace_first_tool(self):
        path = "/boards/main.kicad_sch"
        with mock.patch("pcb_open.os.path.isfile", return_value=True):
            with mock.patch("pcb_open.request_workspace_open", return_value=True) as request:
                with mock.patch("pcb_open.open_with_system") as fallback:
                    result = pcb_open.open_kicad_file(path)
        self.assertEqual(result, "workspace")
        request.assert_called_once_with(path)
        fallback.assert_not_called()

    def test_pcb_only_entry_point_rejects_schematic(self):
        with self.assertRaises(ValueError):
            pcb_open.open_pcb_file("/boards/main.kicad_sch")

    def test_manager_error_falls_back_to_system(self):
        with mock.patch("pcb_open.os.path.isfile", return_value=True):
            with mock.patch("pcb_open.request_workspace_open", return_value=False):
                with mock.patch("pcb_open.open_with_system") as fallback:
                    result = pcb_open.open_pcb_file("/boards/panel.kicad_pcb")
        self.assertEqual(result, "system")
        fallback.assert_called_once_with("/boards/panel.kicad_pcb")

    def test_unavailable_manager_falls_back_to_system(self):
        with mock.patch("pcb_open.os.path.isfile", return_value=True):
            with mock.patch(
                "pcb_open.request_workspace_open",
                side_effect=ConnectionRefusedError("offline"),
            ):
                with mock.patch("pcb_open.open_with_system") as fallback:
                    result = pcb_open.open_pcb_file("/boards/panel.kicad_pcb")
        self.assertEqual(result, "system")
        fallback.assert_called_once_with("/boards/panel.kicad_pcb")

    def test_system_fallback_uses_platform_file_association(self):
        path = "/boards/panel.kicad_pcb"
        for platform_name, command in (
            ("Darwin", ["open", "-n", path]),
            ("Linux", ["xdg-open", path]),
        ):
            with self.subTest(platform=platform_name):
                with mock.patch("pcb_open.platform.system", return_value=platform_name):
                    with mock.patch("pcb_open.subprocess.Popen") as popen:
                        pcb_open.open_with_system(path)
                popen.assert_called_once_with(command)

        with mock.patch("pcb_open.platform.system", return_value="Windows"):
            with mock.patch.object(os, "startfile", create=True) as startfile:
                pcb_open.open_with_system(path)
        startfile.assert_called_once_with(path)

    def test_missing_file_is_not_sent_to_workspace_or_system(self):
        with mock.patch("pcb_open.os.path.isfile", return_value=False):
            with mock.patch("pcb_open.request_workspace_open") as request:
                with self.assertRaises(FileNotFoundError):
                    pcb_open.open_pcb_file("/boards/missing.kicad_pcb")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
