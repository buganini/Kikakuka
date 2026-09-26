import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from im import __main__ as entrypoint


ROOT = Path(__file__).resolve().parents[1]


class ImEntrypointTests(unittest.TestCase):
    def test_main_starts_and_closes_foreground_node(self):
        node = mock.Mock(pid=123, endpoint="/tmp/im.sock", kicad_api=True)
        with mock.patch.object(entrypoint, "start_node", return_value=node) as start, \
                mock.patch.object(entrypoint, "_wait_for_shutdown") as wait:
            self.assertEqual(entrypoint.main([]), 0)

        start.assert_called_once_with(
            entrypoint._handle, entrypoint._mapping_changed
        )
        wait.assert_called_once_with()
        node.close.assert_called_once_with()

    def test_both_command_forms_expose_the_same_cli(self):
        for command in (
            [sys.executable, "-m", "im", "--help"],
            [sys.executable, "im", "--help"],
        ):
            with self.subTest(command=command):
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("foreground Kikakuka Instance Manager node",
                              result.stdout)


if __name__ == "__main__":
    unittest.main()
