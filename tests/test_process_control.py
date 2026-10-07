import io
import unittest
from unittest import mock

import psutil

import process_control


def fake_process(pid, name):
    process = mock.Mock(pid=pid)
    process.info = {"pid": pid, "name": name}
    return process


class ProcessControlTest(unittest.TestCase):
    def test_kills_same_user_kicad_and_freecad_processes(self):
        pcbnew = fake_process(10, "pcbnew.exe")
        freecad = fake_process(20, "FreeCAD_1.1.0.AppImage")
        freecadcmd = fake_process(30, "FreeCADCmd.exe")
        unrelated = fake_process(40, "python")
        kicad_api = fake_process(50, "kicad-api.exe")

        waits = [
            ([pcbnew], [freecad]),
            ([freecad], []),
        ]
        output = io.StringIO()
        with mock.patch.object(
                process_control, "owned_process_iter",
                return_value=[
                    pcbnew, freecad, freecadcmd, unrelated, kicad_api,
                ]), mock.patch.object(
                    process_control.psutil, "wait_procs", side_effect=waits,
                ) as wait_procs, mock.patch("sys.stdout", output):
            result = process_control.kill_all_cad_instances(timeout=0.25)

        self.assertEqual(result, 0)
        pcbnew.terminate.assert_called_once_with()
        freecad.terminate.assert_called_once_with()
        freecadcmd.terminate.assert_not_called()
        unrelated.terminate.assert_not_called()
        kicad_api.terminate.assert_not_called()
        pcbnew.kill.assert_not_called()
        freecad.kill.assert_called_once_with()
        freecadcmd.kill.assert_not_called()
        self.assertEqual(wait_procs.call_count, 2)
        self.assertIn("1 KiCad and 1 FreeCAD", output.getvalue())

    def test_reports_processes_that_cannot_be_stopped(self):
        freecad = fake_process(20, "FreeCAD")
        freecad.terminate.side_effect = psutil.AccessDenied(pid=20)
        error = io.StringIO()
        output = io.StringIO()
        with mock.patch.object(
                process_control, "owned_process_iter",
                return_value=[freecad]), mock.patch("sys.stderr", error), \
                mock.patch("sys.stdout", output):
            result = process_control.kill_all_cad_instances()

        self.assertEqual(result, 1)
        self.assertIn("Could not stop FreeCAD PID 20", error.getvalue())

    def test_succeeds_when_no_instances_are_running(self):
        output = io.StringIO()
        with mock.patch.object(
                process_control, "owned_process_iter", return_value=[]), \
                mock.patch("sys.stdout", output):
            result = process_control.kill_all_cad_instances()

        self.assertEqual(result, 0)
        self.assertIn("No running KiCad or FreeCAD", output.getvalue())


if __name__ == "__main__":
    unittest.main()
