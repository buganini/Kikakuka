import os
import unittest
from unittest import mock

import pcb_open


class PcbOpenTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("pcb_open._ensure_instance_node")
        self.ensure_node = patcher.start()
        self.addCleanup(patcher.stop)

    def test_workspace_request_uses_instance_mesh(self):
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
        self.ensure_node.assert_called_once_with()
        fallback.assert_not_called()

    def test_schematic_uses_the_same_workspace_first_tool(self):
        path = "/boards/main.kicad_sch"
        with mock.patch("pcb_open.os.path.isfile", return_value=True):
            with mock.patch("pcb_open.request_workspace_open", return_value=True) as request:
                with mock.patch("pcb_open.open_with_system") as fallback:
                    result = pcb_open.open_kicad_file(path)
        self.assertEqual(result, "workspace")
        request.assert_called_once_with(path, ensure_fresh=False)
        fallback.assert_not_called()

    def test_ensure_fresh_is_part_of_the_open_request(self):
        path = "/boards/panel.kicad_pcb"
        reply = {
            "status": "ok", "action": "open-file", "filepath": path,
            "pid": 123,
        }
        with mock.patch("pcb_open.os.path.isfile", return_value=True), \
                mock.patch("pcb_open.im_mesh.request", return_value=reply) as request:
            result = pcb_open.open_pcb_file(path, ensure_fresh=True)
        self.assertEqual(result, "workspace")
        request.assert_called_once_with({
            "action": "open-file", "filepath": path, "ensure_fresh": True,
        }, timeout=pcb_open.WORKSPACE_OPEN_TIMEOUT)

    def test_ensure_fresh_rejects_non_pcb_files(self):
        with self.assertRaisesRegex(ValueError, "ensure_fresh"):
            pcb_open.open_kicad_file(
                "/boards/main.kicad_sch", ensure_fresh=True)

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

    def test_open_cli_accepts_fresh_after_open(self):
        for arguments in (
            ["--open", "--fresh", "/boards/main.kicad_pcb"],
            ["--open", "/boards/main.kicad_pcb", "--fresh"],
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    pcb_open.parse_open_arguments(arguments),
                    (["/boards/main.kicad_pcb"], True),
                )

    def test_open_cli_requires_open_as_first_argument(self):
        with self.assertRaisesRegex(ValueError, "--open must be the first"):
            pcb_open.parse_open_arguments([
                "--fresh", "--open", "/boards/main.kicad_pcb",
            ])

    def test_open_cli_forwards_all_files_to_instance_open(self):
        paths = ["/boards/a.kicad_pcb", "/boards/b.kicad_pcb"]
        with mock.patch("pcb_open.open_kicad_file") as open_file:
            handled = pcb_open.open_requested_kicad_files(
                ["--open", "--fresh", *paths])

        self.assertTrue(handled)
        self.assertEqual(open_file.call_args_list, [
            mock.call(paths[0], ensure_fresh=True),
            mock.call(paths[1], ensure_fresh=True),
        ])

    def test_open_cli_applies_fresh_only_to_pcbs_in_mixed_list(self):
        paths = [
            "/boards/a.kicad_sch",
            "/boards/a.kicad_pcb",
            "/boards/a.kicad_pro",
        ]
        with mock.patch("pcb_open.open_kicad_file") as open_file:
            handled = pcb_open.open_requested_kicad_files(
                ["--open", *paths, "--fresh"])

        self.assertTrue(handled)
        self.assertEqual(open_file.call_args_list, [
            mock.call(paths[0], ensure_fresh=False),
            mock.call(paths[1], ensure_fresh=True),
            mock.call(paths[2], ensure_fresh=False),
        ])

    def test_open_cli_fresh_requires_a_pcb_before_opening_anything(self):
        with mock.patch("pcb_open.open_kicad_file") as open_file:
            with self.assertRaisesRegex(ValueError, "at least one .kicad_pcb"):
                pcb_open.open_requested_kicad_files([
                    "--open", "--fresh", "/boards/a.kicad_sch",
                    "/boards/a.kicad_pro",
                ])
        open_file.assert_not_called()

    def test_open_cli_is_not_selected_without_open_flag(self):
        with mock.patch("pcb_open.open_kicad_file") as open_file:
            self.assertFalse(pcb_open.open_requested_kicad_files([
                "/boards/main.kicad_pcb",
            ]))
        open_file.assert_not_called()

    def test_open_cli_rejects_fresh_without_open(self):
        with self.assertRaisesRegex(ValueError, "--fresh requires --open"):
            pcb_open.parse_open_arguments([
                "--fresh", "/boards/main.kicad_pcb",
            ])

    def test_open_cli_requires_a_path(self):
        with self.assertRaisesRegex(ValueError, "at least one KiCad file"):
            pcb_open.parse_open_arguments(["--open", "--fresh"])


if __name__ == "__main__":
    unittest.main()
