import runpy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import freecad_cli


SCRIPT = Path(__file__).resolve().parents[1] / "kikakuka" / "__main__.py"


class KikakukaCliTest(unittest.TestCase):
    def test_killall_mode_is_dispatched_before_ui_imports(self):
        process_control = types.ModuleType("process_control")
        process_control.kill_all_cad_instances = mock.Mock(return_value=17)

        blocked_ui_modules = {
            "differ": None,
            "workspace": None,
            "panelizer": None,
            "gerber": None,
        }
        with mock.patch.object(
                sys, "argv", [str(SCRIPT), "--killall"]), \
                mock.patch.dict(
                    sys.modules,
                    {"process_control": process_control, **blocked_ui_modules},
                ):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(SCRIPT), run_name="__main__")

        self.assertEqual(exited.exception.code, 17)
        process_control.kill_all_cad_instances.assert_called_once_with()

    def test_killall_rejects_arguments(self):
        process_control = types.ModuleType("process_control")
        process_control.kill_all_cad_instances = mock.Mock(return_value=0)
        with mock.patch.object(
                sys, "argv", [str(SCRIPT), "--killall", "extra"]), \
                mock.patch.dict(sys.modules, {"process_control": process_control}), \
                mock.patch.object(sys, "stderr"):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(SCRIPT), run_name="__main__")

        self.assertEqual(exited.exception.code, 2)
        process_control.kill_all_cad_instances.assert_not_called()

    def test_assembly_export_is_dispatched_before_ui_imports(self):
        for target in ("model.step", "model.STP", "model.stl"):
            with self.subTest(target=target):
                launcher = types.ModuleType("freecad_cli")
                launcher.run_freekicad_export = mock.Mock(return_value=31)
                arguments = ["assembly.KKKK_ASM", target]

                blocked_ui_modules = {
                    "differ": None,
                    "workspace": None,
                    "panelizer": None,
                    "gerber": None,
                }
                with mock.patch.object(
                        sys, "argv", [str(SCRIPT), *arguments]), \
                        mock.patch.dict(
                            sys.modules,
                            {"freecad_cli": launcher, **blocked_ui_modules},
                        ):
                    with self.assertRaises(SystemExit) as exited:
                        runpy.run_path(str(SCRIPT), run_name="__main__")

                self.assertEqual(exited.exception.code, 31)
                launcher.run_freekicad_export.assert_called_once_with(
                    *arguments
                )

    def test_freecadcmd_mode_is_dispatched_before_ui_imports(self):
        launcher = types.ModuleType("freecad_cli")
        launcher.run_freecadcmd = mock.Mock(return_value=23)
        arguments = ["--freecadcmd", "export.py", "input.FCStd", "output.step"]

        blocked_ui_modules = {
            "differ": None,
            "workspace": None,
            "panelizer": None,
            "gerber": None,
        }
        with mock.patch.object(sys, "argv", [str(SCRIPT), *arguments]), \
                mock.patch.dict(
                    sys.modules,
                    {"freecad_cli": launcher, **blocked_ui_modules},
                ):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(SCRIPT), run_name="__main__")

        self.assertEqual(exited.exception.code, 23)
        launcher.run_freecadcmd.assert_called_once_with(arguments[1:])

    def test_open_mode_is_dispatched_before_ui_imports(self):
        pcb_open = types.ModuleType("pcb_open")
        pcb_open.open_requested_kicad_files = mock.Mock(return_value=True)
        arguments = ["--open", "--fresh", "/boards/main.kicad_pcb"]

        blocked_ui_modules = {
            "differ": None,
            "workspace": None,
            "panelizer": None,
            "gerber": None,
        }
        with mock.patch.object(sys, "argv", [str(SCRIPT), *arguments]), \
                mock.patch.dict(
                    sys.modules,
                    {"pcb_open": pcb_open, **blocked_ui_modules},
                ):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(SCRIPT), run_name="__main__")

        self.assertEqual(exited.exception.code, 0)
        pcb_open.open_requested_kicad_files.assert_called_once_with(arguments)

    def test_freecadcmd_passes_arguments_and_exit_status_through(self):
        completed = mock.Mock(returncode=7)
        environment = {"PATH": "/opt"}
        with mock.patch.object(
                freecad_cli, "freecad_commands",
                return_value=[["/opt/freecadcmd"]]), \
                mock.patch.object(
                    freecad_cli, "_freecad_environment",
                    return_value=environment,
                ), \
                mock.patch.object(
                    freecad_cli.subprocess, "run", return_value=completed,
                ) as run:
            result = freecad_cli.run_freecadcmd(
                ["export.py", "input.FCStd", "output.step"]
            )

        self.assertEqual(result, 7)
        run.assert_called_once_with([
            "/opt/freecadcmd", "export.py", "input.FCStd", "output.step",
        ], env=environment)

    def test_freecadcmd_reports_missing_executable(self):
        with mock.patch.object(
                freecad_cli, "freecad_commands", return_value=[]), \
                mock.patch.object(freecad_cli.sys, "stderr") as stderr:
            result = freecad_cli.run_freecadcmd([])

        self.assertEqual(result, 127)
        self.assertTrue(stderr.write.called)

    def test_freecadcmd_uses_isolated_environment(self):
        completed = mock.Mock(returncode=0)
        environment = {"PATH": "C:\\FreeCAD\\bin"}
        with mock.patch.object(
                freecad_cli, "freecad_commands",
                return_value=[["C:\\FreeCAD\\bin\\FreeCADCmd.exe"]]), \
                mock.patch.object(
                    freecad_cli, "_freecad_environment",
                    return_value=environment,
                ) as process_environment, \
                mock.patch.object(
                    freecad_cli.subprocess, "run", return_value=completed,
                ) as run:
            result = freecad_cli.run_freecadcmd(["export.py"])

        self.assertEqual(result, 0)
        process_environment.assert_called_once_with(
            "C:\\FreeCAD\\bin\\FreeCADCmd.exe"
        )
        run.assert_called_once_with([
            "C:\\FreeCAD\\bin\\FreeCADCmd.exe", "export.py",
        ], env=environment)

    def test_freecadcmd_preserves_appimage_console_prefix(self):
        completed = mock.Mock(returncode=0)
        environment = {"PATH": "/usr/bin"}
        appimage = "/opt/FreeCAD.AppImage"
        with mock.patch.object(
                freecad_cli, "freecad_commands",
                return_value=[[appimage, "--console"]]), \
                mock.patch.object(
                    freecad_cli, "_freecad_environment",
                    return_value=environment,
                ) as process_environment, \
                mock.patch.object(
                    freecad_cli.subprocess, "run", return_value=completed,
                ) as run:
            result = freecad_cli.run_freecadcmd(["export.py", "input.FCStd"])

        self.assertEqual(result, 0)
        process_environment.assert_called_once_with(appimage)
        run.assert_called_once_with([
            appimage, "--console", "export.py", "input.FCStd",
        ], env=environment)

    def test_freekicad_export_uses_source_entry_point(self):
        expected = (
            Path(__file__).resolve().parents[1]
            / "FreekiCAD/scripts/kkkk_export.py"
        )
        with mock.patch.object(
                freecad_cli.sys, "_MEIPASS", None, create=True):
            self.assertEqual(freecad_cli.kkkk_export_script_path(), expected)

    def test_freekicad_export_uses_bundled_entry_point(self):
        with tempfile.TemporaryDirectory() as directory:
            script = (
                Path(directory) / "freekicad/scripts/kkkk_export.py"
            )
            script.parent.mkdir(parents=True)
            script.touch()
            with mock.patch.object(
                    freecad_cli.sys, "_MEIPASS", directory, create=True):
                self.assertEqual(
                    freecad_cli.kkkk_export_script_path(), script
                )

    def test_freekicad_export_invokes_bundled_script(self):
        script = Path("/bundle/freekicad/scripts/kkkk_export.py")
        with mock.patch.object(
                freecad_cli, "kkkk_export_script_path",
                return_value=script), \
                mock.patch.object(
                    freecad_cli, "run_freecadcmd", return_value=19,
                ) as run:
            result = freecad_cli.run_freekicad_export(
                "input.kkkk_asm", "output.stl"
            )

        self.assertEqual(result, 19)
        run.assert_called_once_with([
            str(script), "input.kkkk_asm", "output.stl",
        ])


if __name__ == "__main__":
    unittest.main()
