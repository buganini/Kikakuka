import os
import unittest
from unittest import mock

import pcb_open


class PcbOpenTests(unittest.TestCase):
    def test_workspace_request_uses_instance_mesh(self):
        self.assertGreater(pcb_open.WORKSPACE_OPEN_TIMEOUT, 120)
        path = "/boards/panel.kicad_pcb"
        reply = {
            "status": "ok", "action": "open-file", "filepath": path,
            "pid": 123,
        }
        with mock.patch("pcb_open.im_mesh.request", return_value=reply) as request:
            self.assertTrue(pcb_open.request_workspace_open(path))
        request.assert_called_once_with(
            {"action": "open-file", "filepath": path}, timeout=pcb_open.WORKSPACE_OPEN_TIMEOUT)

    def test_workspace_request_rejects_response_for_another_file(self):
        reply = {
            "status": "ok", "action": "open-file",
            "filepath": "/boards/other.kicad_pcb", "pid": 123,
        }
        with mock.patch("pcb_open.im_mesh.request", return_value=reply):
            with self.assertRaisesRegex(RuntimeError, "mismatched"):
                pcb_open.request_workspace_open("/boards/panel.kicad_pcb")

    def test_manager_error_does_not_duplicate_open(self):
        with mock.patch("pcb_open.os.path.isfile", return_value=True):
            with mock.patch("pcb_open.request_workspace_open", side_effect=RuntimeError("KiCad busy")):
                with mock.patch("pcb_open.open_with_system") as fallback:
                    with self.assertRaisesRegex(RuntimeError, "KiCad busy"):
                        pcb_open.open_pcb_file("/boards/panel.kicad_pcb")
        fallback.assert_not_called()

    def test_ambiguous_timeout_does_not_duplicate_open(self):
        with mock.patch("pcb_open.os.path.isfile", return_value=True):
            with mock.patch("pcb_open.request_workspace_open", side_effect=TimeoutError("waiting")):
                with mock.patch("pcb_open.open_with_system") as fallback:
                    with self.assertRaises(TimeoutError):
                        pcb_open.open_pcb_file("/boards/panel.kicad_pcb")
        fallback.assert_not_called()

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
