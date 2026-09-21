import unittest
from unittest import mock

import psutil

from workspace_monitor import program_for_process, snapshot_editor_processes


class FakeProcess:
    def __init__(self, pid, name, arguments=(), cwd="/boards"):
        self.info = {"pid": pid, "name": name}
        self.arguments = arguments
        self.directory = cwd

    def cmdline(self):
        return self.arguments

    def cwd(self):
        return self.directory


class WorkspaceMonitorTests(unittest.TestCase):
    def test_classifies_gui_editors_but_not_cli_or_unrelated_processes(self):
        self.assertEqual(program_for_process("pcbnew.exe"), "KiCad")
        self.assertEqual(program_for_process("PCB Editor"), "KiCad")
        self.assertEqual(program_for_process("eeschema"), "KiCad")
        self.assertEqual(program_for_process("FreeCAD.exe"), "FreeCAD")
        self.assertIsNone(program_for_process("kicad-cli"))
        self.assertIsNone(program_for_process("FreeCADCmd"))
        self.assertIsNone(program_for_process("python"))

    def test_lists_running_processes_and_prefers_tracked_paths(self):
        processes = [
            FakeProcess(12, "pcbnew", ("pcbnew", "/boards/other.kicad_pcb")),
            FakeProcess(30, "FreeCAD", ("FreeCAD",)),
            FakeProcess(50, "kicad-cli", ("kicad-cli",)),
            FakeProcess(60, "python", ("python", "panelizer.py")),
        ]
        pidmap = {
            "/boards/main.kicad_pcb": 12,
            "/models/assembly.FCStd": 30,
            "/boards/dead.kicad_pcb": 99,
            "/boards/panel.kkkk_fab": 60,
        }

        with mock.patch("workspace_monitor.psutil.process_iter", return_value=processes):
            rows = snapshot_editor_processes(pidmap)

        self.assertEqual(rows, (
            (12, "KiCad", "/boards/main.kicad_pcb"),
            (30, "FreeCAD", "/models/assembly.FCStd"),
        ))

    def test_uses_command_line_for_untracked_editor_and_marks_unknown(self):
        processes = [
            FakeProcess(8, "eeschema", ("eeschema", "main.kicad_sch")),
            FakeProcess(9, "FreeCAD", ("FreeCAD",)),
        ]

        with mock.patch("workspace_monitor.psutil.process_iter", return_value=processes):
            rows = snapshot_editor_processes({})

        self.assertEqual(rows, (
            (8, "KiCad", "/boards/main.kicad_sch"),
            (9, "FreeCAD", ""),
        ))

    def test_access_denied_keeps_process_with_unknown_path(self):
        process = FakeProcess(42, "pcbnew")
        process.cmdline = mock.Mock(side_effect=psutil.AccessDenied(pid=42))

        with mock.patch("workspace_monitor.psutil.process_iter", return_value=[process]):
            rows = snapshot_editor_processes({})

        self.assertEqual(rows, ((42, "KiCad", ""),))


if __name__ == "__main__":
    unittest.main()
