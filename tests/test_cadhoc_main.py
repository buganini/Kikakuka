"""Tests for the standalone CADhoc command line entry point."""

import contextlib
import io
import unittest
from unittest import mock

from cadhoc import __main__ as cadhoc_main


class CADhocMainTests(unittest.TestCase):
    def test_request_logs_headless_window_activation_error(self):
        reply = {
            "status": "ok",
            "pid": 123,
            "activation_error": "native Wayland cannot be activated",
            "message": "native Wayland cannot be activated",
        }
        with mock.patch.object(
                cadhoc_main, "instance_handle", return_value=reply), \
                self.assertLogs("cadhoc", level="INFO") as logs:
            self.assertIs(cadhoc_main._handle({
                "action": "open-file",
                "filepath": "/boards/main.kicad_pcb",
            }), reply)

        self.assertTrue(any(
            "message=native Wayland cannot be activated" in line
            for line in logs.output
        ))

    def test_no_subcommand_keeps_run_as_the_default(self):
        self.assertEqual(cadhoc_main._parse_arguments([]).command, "run")

    def test_test_subcommand_prints_both_discovery_methods(self):
        output = io.StringIO()
        with mock.patch.object(
                    cadhoc_main, "enumerated_kicad_sockets",
                    return_value=[(None, "/tmp/kicad/api.sock")],
                ), \
                mock.patch.object(
                    cadhoc_main, "owner_kicad_sockets",
                    return_value=[(111, "/tmp/kicad/api.sock")],
                ), \
                mock.patch.object(cadhoc_main, "start_node") as start_node, \
                mock.patch.object(cadhoc_main.sys, "platform", "darwin"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cadhoc_main.main(["test"]), 0)

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
                    cadhoc_main, "enumerated_kicad_sockets", return_value=[]
                ), \
                mock.patch.object(
                    cadhoc_main, "owner_kicad_sockets", return_value=[]
                ), \
                mock.patch.object(cadhoc_main.sys, "platform", "win32"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cadhoc_main.main(["test"]), 0)

        self.assertIn("GetNamedPipeServerProcessId:\n  (none)\n",
                      output.getvalue())


if __name__ == "__main__":
    unittest.main()
