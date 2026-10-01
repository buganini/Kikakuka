"""Tests for the standalone Instance Manager command line entry point."""

import contextlib
import io
import unittest
from unittest import mock

from im import __main__ as im_main


class InstanceManagerMainTests(unittest.TestCase):
    def test_request_logs_headless_window_activation_error(self):
        reply = {
            "status": "ok",
            "pid": 123,
            "activation_error": "native Wayland cannot be activated",
            "message": "native Wayland cannot be activated",
        }
        with mock.patch.object(
                im_main, "instance_handle", return_value=reply), \
                self.assertLogs("im", level="INFO") as logs:
            self.assertIs(im_main._handle({
                "action": "open-file",
                "filepath": "/boards/main.kicad_pcb",
            }), reply)

        self.assertTrue(any(
            "message=native Wayland cannot be activated" in line
            for line in logs.output
        ))

    def test_no_subcommand_keeps_run_as_the_default(self):
        self.assertEqual(im_main._parse_arguments([]).command, "run")

    def test_test_subcommand_prints_both_discovery_methods(self):
        output = io.StringIO()
        with mock.patch.object(
                    im_main, "enumerated_kicad_sockets",
                    return_value=[(None, "/tmp/kicad/api.sock")],
                ), \
                mock.patch.object(
                    im_main, "owner_kicad_sockets",
                    return_value=[(111, "/tmp/kicad/api.sock")],
                ), \
                mock.patch.object(im_main, "start_node") as start_node, \
                mock.patch.object(im_main.sys, "platform", "darwin"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(im_main.main(["test"]), 0)

        start_node.assert_not_called()
        self.assertEqual(
            output.getvalue(),
            "enumerate:\n"
            "  ?\t/tmp/kicad/api.sock\n"
            "psutil.Process.net_connections(kind=\"unix\"):\n"
            "  111\t/tmp/kicad/api.sock\n",
        )

    def test_test_subcommand_names_windows_owner_api(self):
        output = io.StringIO()
        with mock.patch.object(
                    im_main, "enumerated_kicad_sockets", return_value=[]
                ), \
                mock.patch.object(
                    im_main, "owner_kicad_sockets", return_value=[]
                ), \
                mock.patch.object(im_main.sys, "platform", "win32"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(im_main.main(["test"]), 0)

        self.assertIn("GetNamedPipeServerProcessId:\n  (none)\n",
                      output.getvalue())


if __name__ == "__main__":
    unittest.main()
