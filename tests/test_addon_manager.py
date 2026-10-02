import json
import builtins
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile

import addon_manager
import build_addon_archives
import freecad_addon_installer
from freecad_addon_installer import dependency_specs


def metadata(identifier, package_type, version="8.0"):
    return {
        "name": identifier,
        "identifier": identifier,
        "type": package_type,
        "versions": [{"version": version, "kicad_version": "10.0"}],
    }


class AddonManagerTest(unittest.TestCase):
    def setUp(self):
        self.custom_executable = mock.patch.object(
            addon_manager, "custom_executable", return_value=None
        )
        self.custom_executable.start()
        self.addCleanup(self.custom_executable.stop)
        addon_manager.clear_kicad_installations_cache()

    def test_kicad_installation_getter_caches_until_cleared(self):
        first = [(Path("/opt/kicad-cli"), "10.0.1")]
        second = [(Path("/opt/kicad-cli"), "10.0.2")]
        with mock.patch.object(
            addon_manager,
            "_detected_kicad_installations",
            side_effect=[first, second],
        ) as detect:
            self.assertEqual(
                addon_manager.get_kicad_installations(), tuple(first)
            )
            self.assertEqual(
                addon_manager.get_kicad_installations(), tuple(first)
            )
            addon_manager.clear_kicad_installations_cache()
            self.assertEqual(
                addon_manager.get_kicad_installations(), tuple(second)
            )

        self.assertEqual(detect.call_count, 2)

    def test_addon_statuses_detects_kicad_installations_once(self):
        installations = [(Path("/opt/kicad-cli"), "10.0.1")]
        statuses = [mock.Mock(), mock.Mock()]
        with (
            mock.patch.object(
                addon_manager,
                "_detected_kicad_installations",
                return_value=installations,
            ) as detect,
            mock.patch.object(
                addon_manager,
                "kicad_addon_status",
                side_effect=statuses,
            ) as kicad_status,
            mock.patch.object(
                addon_manager,
                "freekicad_status",
                return_value=mock.Mock(),
            ),
        ):
            addon_manager.addon_statuses()

        detect.assert_called_once_with()
        self.assertEqual(
            kicad_status.call_args_list,
            [
                mock.call(addon_manager.KICAD_PLUGIN),
                mock.call(addon_manager.KICAD_LIBRARY),
            ],
        )

    def test_kicad_detection_uses_installation_metadata_before_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "kicad-cli"
            executable.touch()
            executable.with_name("kicad").touch()
            with (
                mock.patch.object(
                    addon_manager,
                    "_kicad_cli_candidates",
                    return_value=[executable],
                ),
                mock.patch.object(
                    addon_manager,
                    "_kicad_version_from_installation",
                    return_value="10.0.1",
                ),
                mock.patch.object(
                    addon_manager,
                    "_command_version",
                ) as command_version,
            ):
                installations = addon_manager._detected_kicad_installations()

        self.assertEqual(installations, [(executable, "10.0.1")])
        command_version.assert_not_called()

    def test_kicad_detection_rejects_orphaned_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "kicad-cli"
            executable.touch()
            with mock.patch.object(
                    addon_manager,
                    "_kicad_cli_candidates",
                    return_value=[executable],
                    ), mock.patch.object(
                        addon_manager, "_command_version"
                    ) as command_version:
                installations = addon_manager._detected_kicad_installations()

        self.assertEqual(installations, [])
        command_version.assert_not_called()

    def test_bundle_archive_uses_cli_runtime_resource_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "build/addons/kicad-plugin.zip"
            expected.parent.mkdir(parents=True)
            expected.write_bytes(b"zip")

            with (
                mock.patch.object(addon_manager, "_source_root", return_value=root),
                mock.patch.object(addon_manager.sys, "_MEIPASS", None, create=True),
            ):
                actual = addon_manager.bundle_archive(addon_manager.KICAD_PLUGIN)

            self.assertEqual(actual, expected)

    def test_bundle_archive_uses_pyinstaller_runtime_resource_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "addons/kicad-plugin.zip"
            expected.parent.mkdir(parents=True)
            expected.write_bytes(b"zip")

            with mock.patch.object(
                addon_manager.sys, "_MEIPASS", str(root), create=True
            ):
                actual = addon_manager.bundle_archive(addon_manager.KICAD_PLUGIN)

            self.assertEqual(actual, expected)

    def test_resolve_kicad_paths_honors_configured_third_party(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            settings = home / "Library/Preferences/kicad/10.0"
            settings.mkdir(parents=True)
            custom = home / "custom-pcm"
            (settings / "kicad_common.json").write_text(
                json.dumps({
                    "environment": {
                        "vars": {"KICAD10_3RD_PARTY": str(custom)}
                    }
                }),
                encoding="utf-8",
            )

            paths = addon_manager.resolve_kicad_paths(
                "10.0", system="Darwin", home=home, environ={}
            )

            self.assertEqual(paths.settings_dir, settings)
            self.assertEqual(paths.third_party_dir, custom)

    def test_install_plugin_uses_pcm_layout_and_records_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "plugin.zip"
            package = metadata("com.example.tools", "plugin")
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("metadata.json", json.dumps(package))
                output.writestr("plugins/plugin.json", "{}")
                output.writestr("plugins/run.py", "print('ok')")
            paths = addon_manager.KiCadPaths(
                "10.0", root / "settings", root / "3rdparty"
            )

            with (
                mock.patch.object(addon_manager, "_read_json_metadata", return_value=package),
                mock.patch.object(addon_manager, "bundle_archive", return_value=archive),
                mock.patch.object(addon_manager, "resolve_kicad_paths", return_value=paths),
                mock.patch.object(
                    addon_manager,
                    "find_kicad_installation",
                    return_value=root / "kicad-cli",
                ),
            ):
                status = addon_manager.install_kicad_addon(addon_manager.KICAD_PLUGIN)

            installed = root / "3rdparty/plugins/com_example_tools"
            self.assertEqual((installed / "plugin.json").read_text(), "{}")
            self.assertEqual(status.installed_version, "8.0")
            self.assertEqual(status.uninstall_action, "Uninstall")
            self.assertNotIn("Restart", status.status_text)
            recorded = json.loads(paths.installed_packages.read_text())
            self.assertEqual(recorded["packages"][0]["current_version"], "8.0")
            self.assertEqual(
                recorded["packages"][0]["repository_name"], "Local file"
            )

    def test_install_library_adds_global_footprint_table_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "library.zip"
            package = metadata("com.example.footprints", "library")
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("metadata.json", json.dumps(package))
                output.writestr(
                    "footprints/Kikakuka.pretty/Test.kicad_mod", "(footprint)"
                )
                output.writestr("3dmodels/Kikakuka.3dshapes/Test.step", "STEP")
            paths = addon_manager.KiCadPaths(
                "10.0", root / "settings", root / "3rdparty"
            )

            with (
                mock.patch.object(addon_manager, "_read_json_metadata", return_value=package),
                mock.patch.object(addon_manager, "bundle_archive", return_value=archive),
                mock.patch.object(addon_manager, "resolve_kicad_paths", return_value=paths),
                mock.patch.object(
                    addon_manager,
                    "find_kicad_installation",
                    return_value=root / "kicad-cli",
                ),
            ):
                addon_manager.install_kicad_addon(addon_manager.KICAD_LIBRARY)

            table = paths.footprint_table.read_text()
            self.assertIn('(name "PCM_Kikakuka")', table)
            self.assertIn(
                "${KICAD10_3RD_PARTY}/footprints/com_example_footprints/"
                "Kikakuka.pretty",
                table,
            )

    def test_uninstall_plugin_removes_only_matching_pcm_payload_and_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = metadata("com.example.tools", "plugin")
            paths = addon_manager.KiCadPaths(
                "10.0", root / "settings", root / "3rdparty"
            )
            payload = paths.third_party_dir / "plugins/com_example_tools"
            payload.mkdir(parents=True)
            (payload / "plugin.json").write_text("{}")
            other = paths.third_party_dir / "plugins/com_example.other"
            other.mkdir(parents=True)
            paths.installed_packages.parent.mkdir(parents=True)
            paths.installed_packages.write_text(json.dumps({"packages": [
                {"current_version": "8.0", "package": package},
                {
                    "current_version": "1.0",
                    "package": {"identifier": "com.example.other"},
                },
            ]}))

            with (
                mock.patch.object(addon_manager, "_read_json_metadata", return_value=package),
                mock.patch.object(addon_manager, "resolve_kicad_paths", return_value=paths),
                mock.patch.object(addon_manager, "_running_editor_pids", return_value=[]),
                mock.patch.object(
                    addon_manager,
                    "find_kicad_installation",
                    return_value=root / "kicad-cli",
                ),
            ):
                status = addon_manager.uninstall_kicad_addon(
                    addon_manager.KICAD_PLUGIN
                )

            self.assertFalse(payload.exists())
            self.assertTrue(other.exists())
            recorded = json.loads(paths.installed_packages.read_text())
            self.assertEqual(
                [entry["package"]["identifier"] for entry in recorded["packages"]],
                ["com.example.other"],
            )
            self.assertEqual(status.action, "Install")
            self.assertEqual(status.uninstall_action, "")

    def test_uninstall_library_removes_footprint_table_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = metadata("com.example.footprints", "library")
            paths = addon_manager.KiCadPaths(
                "10.0", root / "settings", root / "3rdparty"
            )
            payload = (
                paths.third_party_dir
                / "footprints/com_example_footprints/Kikakuka.pretty"
            )
            payload.mkdir(parents=True)
            paths.installed_packages.parent.mkdir(parents=True)
            paths.installed_packages.write_text(json.dumps({"packages": [
                {"current_version": "8.0", "package": package},
            ]}))
            paths.footprint_table.write_text(
                '(fp_lib_table\n'
                '  (lib (name "Keep") (type "KiCad") (uri "/keep"))\n'
                '  (lib (name "PCM_Kikakuka") (type "KiCad") '
                '(uri "${KICAD10_3RD_PARTY}/footprints/'
                'com_example_footprints/Kikakuka.pretty"))\n'
                ')\n'
            )

            with (
                mock.patch.object(addon_manager, "_read_json_metadata", return_value=package),
                mock.patch.object(addon_manager, "resolve_kicad_paths", return_value=paths),
                mock.patch.object(addon_manager, "_running_editor_pids", return_value=[]),
                mock.patch.object(
                    addon_manager,
                    "find_kicad_installation",
                    return_value=root / "kicad-cli",
                ),
            ):
                addon_manager.uninstall_kicad_addon(addon_manager.KICAD_LIBRARY)

            table = paths.footprint_table.read_text()
            self.assertNotIn("PCM_Kikakuka", table)
            self.assertIn('(name "Keep")', table)

    def test_missing_installation_has_no_action(self):
        package = metadata("com.example.tools", "plugin")
        with (
            mock.patch.object(addon_manager, "_read_json_metadata", return_value=package),
            mock.patch.object(addon_manager, "find_kicad_installation", return_value=None),
            mock.patch.object(addon_manager, "find_any_kicad_version", return_value=None),
        ):
            status = addon_manager.kicad_addon_status(addon_manager.KICAD_PLUGIN)

        self.assertEqual(status.status_text, "Cannot find KiCad installation")
        self.assertEqual(status.action, "")
        self.assertEqual(status.uninstall_action, "")

    def test_unsupported_kicad_major_has_no_action(self):
        package = metadata("com.example.tools", "plugin")
        with (
            mock.patch.object(addon_manager, "_read_json_metadata", return_value=package),
            mock.patch.object(addon_manager, "find_kicad_installation", return_value=None),
            mock.patch.object(addon_manager, "find_any_kicad_version", return_value="9.0.6"),
        ):
            status = addon_manager.kicad_addon_status(addon_manager.KICAD_PLUGIN)

        self.assertEqual(
            status.status_text,
            "Unsupported KiCad version 9.0.6; requires 10.x",
        )
        self.assertEqual(status.action, "")
        self.assertEqual(status.uninstall_action, "")

    def test_invalid_pcm_record_is_repaired_from_payload_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = metadata("com.example.tools", "plugin")
            package["name"] = "Kikakuka Tools"
            paths = addon_manager.KiCadPaths(
                "10.0", root / "settings", root / "3rdparty"
            )
            payload = paths.third_party_dir / "plugins/com_example_tools"
            payload.mkdir(parents=True)
            (payload / ".kikakuka-version").write_text("8.0\n")
            paths.installed_packages.parent.mkdir(parents=True)
            paths.installed_packages.write_text(json.dumps({"packages": [{
                "current_version": "0.0",
                "package": {
                    "identifier": "com.example.tools",
                    "name": "com_example_tools",
                    "type": "invalid",
                },
                "repository_name": "<unknown>",
            }]}))

            with (
                mock.patch.object(addon_manager, "_read_json_metadata", return_value=package),
                mock.patch.object(addon_manager, "resolve_kicad_paths", return_value=paths),
                mock.patch.object(addon_manager, "_running_editor_pids", return_value=[]),
                mock.patch.object(
                    addon_manager,
                    "find_kicad_installation",
                    return_value=root / "kicad-cli",
                ),
            ):
                status = addon_manager.kicad_addon_status(
                    addon_manager.KICAD_PLUGIN
                )

            self.assertEqual(status.installed_version, "8.0")
            recorded = json.loads(paths.installed_packages.read_text())["packages"][0]
            self.assertEqual(recorded["current_version"], "8.0")
            self.assertEqual(recorded["package"]["name"], "Kikakuka Tools")
            self.assertEqual(recorded["package"]["type"], "plugin")
            self.assertEqual(recorded["repository_name"], "Local file")

    def test_addon_archive_can_include_payload_version_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "run.py"
            source.write_text("pass\n")
            output = root / "addon.zip"

            build_addon_archives._build_archive(
                output,
                [(source, Path("plugins/run.py"))],
                {Path("plugins/.kikakuka-version"): "8.0"},
            )

            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    archive.read("plugins/.kikakuka-version"), b"8.0\n"
                )

    def test_required_freecad_dependencies_include_version_constraints(self):
        xml = b"""<package xmlns="https://wiki.freecad.org/Package_Metadata">
          <depend type="python" version_gte="7.2.2">psutil</depend>
          <depend type="python" optional="true" version_gte="2.0">shapely</depend>
          <depend type="python" version_gte="1.0" version_lt="2.0">example</depend>
        </package>"""

        self.assertEqual(
            dependency_specs(xml),
            ["psutil>=7.2.2", "example>=1.0,<2.0"],
        )

    def test_freecad_dependencies_fall_back_without_headless_pyside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "freecad/bin/python3"
            python.parent.mkdir(parents=True)
            python.touch()
            user_data = root / "user"
            fake_freecad = types.SimpleNamespace(
                getHomePath=lambda: str(root / "freecad"),
                ConfigGet=lambda key: str(user_data) if key == "UserAppData" else "",
            )
            real_import = builtins.__import__

            def import_without_pyside(name, *args, **kwargs):
                if name == "addonmanager_utilities":
                    raise ImportError("No viable version of PySide was found")
                return real_import(name, *args, **kwargs)

            completed = mock.Mock(stdout="", stderr="", returncode=0)
            with (
                mock.patch.dict(sys.modules, {"FreeCAD": fake_freecad}),
                mock.patch.object(
                    builtins, "__import__", side_effect=import_without_pyside
                ),
                mock.patch.object(
                    freecad_addon_installer.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                freecad_addon_installer.install_dependencies(
                    ["psutil>=7.2.2"]
                )

            command = run.call_args.args[0]
            self.assertEqual(command[:4], [str(python), "-m", "pip", "--disable-pip-version-check"])
            self.assertIn("psutil>=7.2.2", command)
            self.assertIn(
                str(
                    user_data
                    / "AdditionalPythonPackages"
                    / f"py{sys.version_info.major}{sys.version_info.minor}"
                ),
                command,
            )

    def test_freecad_install_falls_back_when_headless_pyside_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "freekicad.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(
                    "package.xml",
                    '<package xmlns="https://wiki.freecad.org/Package_Metadata">'
                    '<version>8.0</version>'
                    '<depend type="python" version_gte="7.2.2">psutil</depend>'
                    "</package>",
                )
                output.writestr("freecad/FreekiCAD/module.py", "VALUE = 1\n")
            package_xml = root / "user/Mod/FreekiCAD/package.xml"
            package_xml.parent.mkdir(parents=True)
            (package_xml.parent / "old.py").write_text("old\n")
            real_import = builtins.__import__

            def import_without_pyside(name, *args, **kwargs):
                if name == "addonmanager_installer":
                    raise ImportError("No viable version of PySide was found")
                return real_import(name, *args, **kwargs)

            with (
                mock.patch.dict(
                    sys.modules,
                    {"Addon": types.SimpleNamespace(Addon=lambda *args, **kwargs: None)},
                ),
                mock.patch.object(
                    freecad_addon_installer,
                    "installed_package_xml",
                    return_value=package_xml,
                ),
                mock.patch.object(freecad_addon_installer, "install_dependencies"),
                mock.patch.object(
                    builtins, "__import__", side_effect=import_without_pyside
                ),
                mock.patch.object(freecad_addon_installer, "emit") as emit,
            ):
                freecad_addon_installer.install(archive)

            self.assertEqual(
                (package_xml.parent / "freecad/FreekiCAD/module.py").read_text(),
                "VALUE = 1\n",
            )
            self.assertFalse((package_xml.parent / "old.py").exists())
            emit.assert_called_once_with(
                ok=True,
                version="8.0",
                dependencies=["psutil>=7.2.2"],
            )

    def test_direct_freecad_install_rejects_archive_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "freekicad.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("package.xml", "<package/>")
                output.writestr("../escaped.py", "bad\n")

            with self.assertRaisesRegex(ValueError, "Unsafe archive path"):
                freecad_addon_installer._install_archive_direct(
                    archive, root / "Mod/FreekiCAD"
                )

            self.assertFalse((root / "escaped.py").exists())

    def test_freecad_helper_inputs_use_environment_not_cli_arguments(self):
        completed = mock.Mock(
            stdout=(
                addon_manager._RESULT_PREFIX
                + '{"ok": true, "version": "8.0"}\n'
            ),
            stderr="",
            returncode=0,
        )
        helper = Path("/bundle/freecad_addon_installer.py")
        archive = Path("/bundle/freekicad.zip")
        with (
            mock.patch.object(
                addon_manager, "freecad_commands", return_value=[["freecadcmd"]]
            ),
            mock.patch.object(
                addon_manager, "freecad_helper_path", return_value=helper
            ),
            mock.patch.object(
                addon_manager.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = addon_manager.run_freecad_helper("install", archive)

        self.assertEqual(result["version"], "8.0")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["freecadcmd", str(helper)])
        self.assertEqual(kwargs["env"]["KIKAKUKA_ADDON_ACTION"], "install")
        self.assertEqual(
            kwargs["env"]["KIKAKUKA_ADDON_ARCHIVE"], str(archive)
        )

    def test_freecad_helper_preserves_result_with_non_utf8_logs(self):
        for ok in (True, False):
            with self.subTest(ok=ok):
                payload = {"ok": ok, "version": "8.1", "error": "無法安裝"}
                output = (
                    b"native log: \xa6\x77\n"
                    + (addon_manager._RESULT_PREFIX + json.dumps(payload) + "\n").encode("ascii")
                )
                script = (
                    f"import sys; sys.stdout.buffer.write({output!r}); "
                    "sys.stderr.buffer.write(b'error log: \\xa6\\x77\\n')"
                )
                with mock.patch.object(
                    addon_manager, "freecad_commands",
                    return_value=[[sys.executable, "-c", script]],
                ):
                    if ok:
                        self.assertEqual(addon_manager.run_freecad_helper("status"), payload)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "無法安裝"):
                            addon_manager.run_freecad_helper("install", Path("addon.zip"))

    def test_freecad_pip_non_utf8_logs_do_not_hide_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            for exit_code in (0, 1):
                with self.subTest(exit_code=exit_code):
                    script = (
                        "import sys; sys.stdout.buffer.write(b'pip: \\xa6\\x77\\n'); "
                        "sys.stderr.buffer.write(b'pip error: \\xa6\\x77\\n'); "
                        f"sys.exit({exit_code})"
                    )
                    def command(args):
                        return [sys.executable, "-c", script]
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        if exit_code:
                            with self.assertRaisesRegex(RuntimeError, "dependency psutil"):
                                freecad_addon_installer._run_pip_specs(
                                    ["psutil"], Path(directory), command,
                                )
                        else:
                            freecad_addon_installer._run_pip_specs(
                                ["psutil"], Path(directory), command,
                            )

    def test_freecad_result_protocol_is_ascii_and_preserves_unicode(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            freecad_addon_installer.emit(ok=False, error="無法讀取使用者資料夾")
        output.getvalue().encode("ascii")
        self.assertEqual(
            addon_manager._parse_helper_result(output.getvalue()),
            {"ok": False, "error": "無法讀取使用者資料夾"},
        )

    def test_freecad_commands_do_not_fall_back_to_gui_console_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            console = root / "freecadcmd"
            gui = root / "freecad"
            console.touch()
            gui.touch()

            def which(name):
                if name == "freecadcmd":
                    return str(console)
                if name == "freecad":
                    return str(gui)
                return None

            with (
                mock.patch.object(
                    addon_manager, "_owned_freecad_executables", return_value=[]
                ),
                mock.patch.object(addon_manager.shutil, "which", side_effect=which),
            ):
                commands = addon_manager.freecad_commands(system="Linux")

            self.assertEqual(commands, [[str(console)]])

    def test_freecad_commands_ignore_transient_appimage_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            console = (
                Path(directory)
                / ".mount_FreeCAD123/usr/bin/freecadcmd"
            )
            console.parent.mkdir(parents=True)
            console.touch()
            with mock.patch.object(
                    addon_manager,
                    "_owned_freecad_executables",
                    return_value=[console],
                    ), mock.patch.object(
                        addon_manager.shutil, "which", return_value=None
                    ):
                commands = addon_manager.freecad_commands(system="Linux")

        self.assertEqual(commands, [])

    def test_manual_freecad_gui_uses_snap_console_companion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gui = root / "freecad"
            console = root / "freecad.cmd"
            gui.touch()
            console.touch()
            with mock.patch.object(
                addon_manager,
                "custom_executable",
                return_value=gui,
            ):
                commands = addon_manager.freecad_commands(system="Linux")

        self.assertEqual(commands, [[str(console)]])

    def test_manual_freecad_appimage_uses_console_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            appimage = Path(directory) / "FreeCAD.AppImage"
            appimage.touch()
            with mock.patch.object(
                addon_manager,
                "custom_executable",
                return_value=appimage,
            ):
                commands = addon_manager.freecad_commands(system="Linux")

        self.assertEqual(commands, [[str(appimage), "--console"]])

    def test_manual_kicad_gui_prefers_cli_companion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gui = root / "kicad"
            cli = root / "kicad-cli"
            gui.touch()
            cli.touch()
            with mock.patch.object(
                addon_manager,
                "custom_executable",
                return_value=gui,
            ):
                candidates = addon_manager._kicad_cli_candidates()

        self.assertEqual(candidates, [cli])

    def test_manual_macos_app_bundles_use_internal_console_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kicad_app = root / "KiCad.app"
            kicad_cli = kicad_app / "Contents/MacOS/kicad-cli"
            kicad_cli.parent.mkdir(parents=True)
            kicad_cli.touch()
            freecad_app = root / "FreeCAD.app"
            freecad_cmd = freecad_app / "Contents/Resources/bin/freecadcmd"
            freecad_cmd.parent.mkdir(parents=True)
            freecad_cmd.touch()

            with mock.patch.object(
                    addon_manager.platform, "system", return_value="Darwin"), \
                    mock.patch.object(
                        addon_manager,
                        "custom_executable",
                        return_value=kicad_app,
                    ):
                self.assertEqual(
                    addon_manager._kicad_cli_candidates(), [kicad_cli]
                )
            with mock.patch.object(
                    addon_manager,
                    "custom_executable",
                    return_value=freecad_app,
                    ):
                self.assertEqual(
                    addon_manager.freecad_commands(system="Darwin"),
                    [[str(freecad_cmd)]],
                )

    def test_addon_diagnostics_lists_installations_and_socket_folder(self):
        with (
            mock.patch.object(
                addon_manager,
                "get_kicad_installations",
                return_value=((Path("/opt/kicad-cli"), "10.0.2"),),
            ),
            mock.patch.object(
                addon_manager,
                "freecad_commands",
                return_value=[["/snap/bin/freecad.cmd"]],
            ),
            mock.patch.object(
                addon_manager,
                "_freecad_version_from_installation",
                return_value=None,
            ),
            mock.patch.object(
                addon_manager,
                "_command_version",
                return_value="1.1.4",
            ),
            mock.patch(
                "im.im_mesh.runtime_dir",
                return_value=Path("/tmp/kikakuka-1000"),
            ),
        ):
            diagnostics = addon_manager.addon_diagnostics()

        self.assertEqual(
            diagnostics,
            {
                "kicad": "/opt/kicad-cli (10.0.2)",
                "freecad": "/snap/bin/freecad.cmd (1.1.4)",
                "kicad_manual": False,
                "freecad_manual": False,
                "socket_folder": "/tmp/kikakuka-1000",
            },
        )

    def test_freecad_helper_main_reads_environment_action(self):
        with (
            mock.patch.dict(
                freecad_addon_installer.os.environ,
                {"KIKAKUKA_ADDON_ACTION": "status"},
            ),
            mock.patch.object(
                freecad_addon_installer,
                "installed_package_xml",
                return_value=Path("/tmp/FreekiCAD/package.xml"),
            ),
            mock.patch.object(
                freecad_addon_installer, "package_version", return_value="8.0"
            ),
            mock.patch.object(freecad_addon_installer, "emit") as emit,
        ):
            result = freecad_addon_installer.main()

        self.assertEqual(result, 0)
        emit.assert_called_once_with(
            ok=True,
            version="8.0",
            path=str(Path("/tmp/FreekiCAD/package.xml")),
        )

    def test_freecad_uninstall_uses_addon_manager_uninstaller(self):
        with tempfile.TemporaryDirectory() as directory:
            package_xml = Path(directory) / "FreekiCAD/package.xml"
            package_xml.parent.mkdir(parents=True)
            package_xml.write_text("<package/>")
            seen = []

            class FakeAddon:
                def __init__(self, name):
                    self.name = name

            class FakeUninstaller:
                def __init__(self, addon):
                    seen.append(addon.name)

                def run(self):
                    return True

            modules = {
                "Addon": types.SimpleNamespace(Addon=FakeAddon),
                "addonmanager_uninstaller": types.SimpleNamespace(
                    AddonUninstaller=FakeUninstaller
                ),
            }
            with (
                mock.patch.dict(sys.modules, modules),
                mock.patch.object(
                    freecad_addon_installer,
                    "installed_package_xml",
                    return_value=package_xml,
                ),
                mock.patch.object(freecad_addon_installer, "emit") as emit,
            ):
                freecad_addon_installer.uninstall()

            self.assertEqual(seen, ["FreekiCAD"])
            emit.assert_called_once_with(ok=True, removed=True)

    def test_freecad_uninstall_falls_back_when_headless_pyside_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_xml = root / "Mod/FreekiCAD/package.xml"
            package_xml.parent.mkdir(parents=True)
            package_xml.write_text("<package/>")
            sibling = root / "Mod/Keep/file.txt"
            sibling.parent.mkdir(parents=True)
            sibling.write_text("keep")
            real_import = builtins.__import__

            def import_without_pyside(name, *args, **kwargs):
                if name == "addonmanager_uninstaller":
                    raise ImportError(
                        "No viable version of PySide was found "
                        "(tried the FreeCAD PySide wrapper, PySide6 and PySide2)"
                    )
                return real_import(name, *args, **kwargs)

            fake_addon = types.SimpleNamespace(Addon=lambda name: name)
            with (
                mock.patch.dict(sys.modules, {"Addon": fake_addon}),
                mock.patch.object(
                    freecad_addon_installer,
                    "installed_package_xml",
                    return_value=package_xml,
                ),
                mock.patch.object(
                    builtins, "__import__", side_effect=import_without_pyside
                ),
                mock.patch.object(freecad_addon_installer, "emit") as emit,
            ):
                freecad_addon_installer.uninstall()

            self.assertFalse(package_xml.parent.exists())
            self.assertEqual(sibling.read_text(), "keep")
            emit.assert_called_once_with(ok=True, removed=True)

    def test_freecad_uninstall_does_not_hide_unrelated_import_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            package_xml = Path(directory) / "FreekiCAD/package.xml"
            package_xml.parent.mkdir(parents=True)
            package_xml.write_text("<package/>")
            real_import = builtins.__import__

            def broken_import(name, *args, **kwargs):
                if name == "addonmanager_uninstaller":
                    raise ImportError("unexpected addon manager failure")
                return real_import(name, *args, **kwargs)

            with (
                mock.patch.dict(
                    sys.modules,
                    {"Addon": types.SimpleNamespace(Addon=lambda name: name)},
                ),
                mock.patch.object(
                    freecad_addon_installer,
                    "installed_package_xml",
                    return_value=package_xml,
                ),
                mock.patch.object(
                    builtins, "__import__", side_effect=broken_import
                ),
            ):
                with self.assertRaisesRegex(
                    ImportError, "unexpected addon manager failure"
                ):
                    freecad_addon_installer.uninstall()

            self.assertTrue(package_xml.parent.exists())

    def test_freecad_install_status_does_not_prompt_for_restart(self):
        with (
            mock.patch.object(
                addon_manager,
                "run_freecad_helper",
                return_value={"ok": True, "version": "8.0"},
            ),
            mock.patch.object(
                addon_manager, "bundle_archive", return_value=Path("freekicad.zip")
            ),
            mock.patch.object(addon_manager, "bundled_version", return_value="8.0"),
        ):
            status = addon_manager.install_freekicad()

        self.assertNotIn("Restart", status.status_text)

    def test_freecad_uninstall_status_does_not_prompt_for_restart(self):
        with (
            mock.patch.object(addon_manager, "run_freecad_helper"),
            mock.patch.object(addon_manager, "bundled_version", return_value="8.0"),
        ):
            status = addon_manager.uninstall_freekicad()

        self.assertNotIn("Restart", status.status_text)

    def test_install_requires_all_kicad_instances_to_be_closed(self):
        with (
            mock.patch.object(
                addon_manager, "_running_editor_pids", return_value=[101, 202]
            ),
            mock.patch.object(addon_manager, "install_kicad_addon") as install,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Close all KiCad instances before install$",
            ):
                addon_manager.install_addon(addon_manager.KICAD_PLUGIN)

        install.assert_not_called()

    def test_running_instance_scan_uses_editor_process_names(self):
        from im import im_mesh

        def process(pid, name):
            value = mock.Mock(pid=pid)
            value.info = {"pid": pid, "name": name}
            return value

        processes = [
            process(10, "pcbnew"),
            process(20, "FreeCAD"),
            process(30, "FreeCADCmd.exe"),
            process(40, "python"),
        ]
        with mock.patch.object(
            im_mesh, "owned_process_iter", return_value=processes
        ):
            self.assertEqual(
                addon_manager._running_editor_pids(addon_manager.KICAD_PLUGIN),
                [10],
            )
            self.assertEqual(
                addon_manager._running_editor_pids(addon_manager.FREEKICAD),
                [20, 30],
            )

    def test_uninstall_requires_all_freecad_instances_to_be_closed(self):
        with (
            mock.patch.object(
                addon_manager, "_running_editor_pids", return_value=[303]
            ),
            mock.patch.object(addon_manager, "uninstall_freekicad") as uninstall,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"^Close all FreeCAD instances before uninstall$",
            ):
                addon_manager.uninstall_addon(addon_manager.FREEKICAD)

        uninstall.assert_not_called()

    def test_missing_freecad_installation_has_no_action(self):
        with mock.patch.object(addon_manager, "freecad_commands", return_value=[]):
            status = addon_manager.freekicad_status()

        self.assertEqual(status.status_text, "Cannot find FreeCAD installation")
        self.assertEqual(status.action, "")
        self.assertEqual(status.uninstall_action, "")

    def test_unsupported_freecad_version_has_no_action(self):
        with (
            mock.patch.object(
                addon_manager, "freecad_commands", return_value=[["freecadcmd"]]
            ),
            mock.patch.object(
                addon_manager,
                "freecad_installation_version",
                return_value="0.21.2",
            ),
            mock.patch.object(addon_manager, "bundled_version", return_value="8.0"),
        ):
            status = addon_manager.freekicad_status()

        self.assertEqual(
            status.status_text,
            "Unsupported FreeCAD version 0.21.2; requires 1.0 or newer",
        )
        self.assertEqual(status.action, "")
        self.assertEqual(status.uninstall_action, "")

    def test_freecad_1_0_is_supported(self):
        with (
            mock.patch.object(
                addon_manager, "freecad_commands", return_value=[["freecadcmd"]]
            ),
            mock.patch.object(
                addon_manager,
                "freecad_installation_version",
                return_value="1.0.2",
            ),
            mock.patch.object(
                addon_manager, "_heuristic_freekicad_version", return_value=None
            ),
            mock.patch.object(addon_manager, "bundled_version", return_value="8.0"),
        ):
            status = addon_manager.freekicad_status()

        self.assertTrue(status.available)
        self.assertEqual(status.action, "Install")

    def test_freecad_1_1_is_supported(self):
        with (
            mock.patch.object(
                addon_manager, "freecad_commands", return_value=[["freecadcmd"]]
            ),
            mock.patch.object(
                addon_manager,
                "freecad_installation_version",
                return_value="1.1.0",
            ),
            mock.patch.object(addon_manager, "_heuristic_freekicad_version", return_value=None),
            mock.patch.object(addon_manager, "bundled_version", return_value="8.0"),
        ):
            status = addon_manager.freekicad_status()

        self.assertTrue(status.available)
        self.assertEqual(status.action, "Install")

    def test_freecad_version_falls_back_to_macos_bundle_metadata(self):
        import plistlib

        with tempfile.TemporaryDirectory() as directory:
            executable = (
                Path(directory)
                / "FreeCAD.app/Contents/Resources/bin/freecadcmd"
            )
            executable.parent.mkdir(parents=True)
            executable.touch()
            info = executable.parents[2] / "Info.plist"
            with info.open("wb") as destination:
                plistlib.dump({"CFBundleVersion": "1.1.3"}, destination)

            with (
                mock.patch.object(addon_manager.platform, "system", return_value="Darwin"),
                mock.patch.object(addon_manager, "_command_version", return_value=None),
            ):
                version = addon_manager.freecad_installation_version(
                    [[str(executable)]]
                )

            self.assertEqual(version, "1.1.3")


class SymlinkInstallTests(unittest.TestCase):
    def test_status_marks_symlink_without_displaying_path_after_refresh(self):
        with mock.patch.object(addon_manager, "freecad_commands", return_value=[["freecadcmd"]]), \
                mock.patch.object(addon_manager, "freecad_installation_version", return_value="1.1.3"), \
                mock.patch.object(addon_manager, "bundled_version", return_value="8.2"), \
                mock.patch.object(addon_manager, "run_freecad_helper", return_value={
                    "version": "8.2", "symlink_target": "/source/FreekiCAD"}):
            status = addon_manager.freekicad_status(query_freecad=True)
        self.assertEqual(status.status_text, "Installed 8.2 — Symlink")

    def test_symlink_install_uses_existing_process_guard(self):
        with mock.patch.object(addon_manager, "freekicad_source_directory", return_value=Path("/source/FreekiCAD")), \
                mock.patch.object(addon_manager, "_require_no_running_instances", side_effect=RuntimeError("Close FreeCAD")), \
                mock.patch.object(addon_manager, "run_freecad_helper") as helper:
            with self.assertRaisesRegex(RuntimeError, "Close FreeCAD"):
                addon_manager.install_freekicad_symlink()
        helper.assert_not_called()

    def test_source_only_button(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "FreekiCAD"
            (source / "freecad/FreekiCAD").mkdir(parents=True)
            (source / "package.xml").write_text("<package><version>8.2</version></package>")
            with mock.patch.object(addon_manager, "_source_root", return_value=root), \
                    mock.patch.object(sys, "frozen", False, create=True), \
                    mock.patch.object(sys, "_MEIPASS", None, create=True):
                status = addon_manager.AddonStatus(addon_manager.FREEKICAD, "FreekiCAD", "8.2", None)
                self.assertEqual(status.as_row()["symlink"], "Install Symlink")
                for installed_version in ("8.2", "8.1"):
                    for detail in ("", "Symlink"):
                        installed = addon_manager.AddonStatus(
                            addon_manager.FREEKICAD, "FreekiCAD", "8.2",
                            installed_version, detail)
                        self.assertEqual(installed.as_row()["symlink"], "")
                with mock.patch.object(sys, "frozen", True):
                    self.assertEqual(status.as_row()["symlink"], "")
                with mock.patch.object(sys, "_MEIPASS", str(root)):
                    self.assertEqual(status.as_row()["symlink"], "")
                with mock.patch.object(
                    addon_manager.platform, "system", return_value="Windows"
                ):
                    self.assertEqual(status.as_row()["symlink"], "")
                unavailable = addon_manager.AddonStatus(addon_manager.FREEKICAD, "FreekiCAD", "8.2", None, available=False)
                self.assertEqual(unavailable.as_row()["symlink"], "")

    def test_install_reinstall_and_uninstall_preserve_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "checkout/FreekiCAD"
            (source / "freecad/FreekiCAD").mkdir(parents=True)
            metadata = "<package><version>8.2</version></package>"
            (source / "package.xml").write_text(metadata)
            destination = root / "user/Mod/FreekiCAD"
            destination.mkdir(parents=True)
            (destination / "old.txt").write_text("old")
            with mock.patch.object(freecad_addon_installer, "installed_package_xml", return_value=destination / "package.xml"), \
                    mock.patch.object(freecad_addon_installer, "install_dependencies"), \
                    mock.patch.object(freecad_addon_installer, "emit"):
                for _ in range(2):
                    freecad_addon_installer.install_symlink(source)
                    self.assertTrue(destination.is_symlink())
                    self.assertEqual(destination.readlink(), source.resolve())
                with mock.patch.object(freecad_addon_installer, "emit") as emit:
                    self.assertEqual(freecad_addon_installer.main(["status"]), 0)
                    self.assertEqual(emit.call_args.kwargs["symlink_target"], str(source.resolve()))
                (source / "live.py").write_text("changed")
                self.assertEqual((destination / "live.py").read_text(), "changed")
                freecad_addon_installer.uninstall()
                self.assertFalse(destination.is_symlink())
                self.assertEqual((source / "package.xml").read_text(), metadata)
                self.assertTrue((source / "live.py").is_file())

    def test_failed_symlink_creation_preserves_existing_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            (source / "freecad/FreekiCAD").mkdir(parents=True)
            (source / "package.xml").write_text("<package><version>8.2</version></package>")
            destination = root / "Mod/FreekiCAD"
            destination.mkdir(parents=True)
            (destination / "old.txt").write_text("old")
            with mock.patch.object(freecad_addon_installer, "installed_package_xml", return_value=destination / "package.xml"), \
                    mock.patch.object(freecad_addon_installer, "install_dependencies"), \
                    mock.patch.object(Path, "symlink_to", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    freecad_addon_installer.install_symlink(source)
            self.assertEqual((destination / "old.txt").read_text(), "old")

    def test_archive_install_replaces_link_without_writing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "package.xml").write_text("original")
            destination = root / "Mod/FreekiCAD"
            destination.parent.mkdir()
            destination.symlink_to(source, target_is_directory=True)
            archive = root / "addon.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("package.xml", "<package><version>8.2</version></package>")
            with mock.patch.object(freecad_addon_installer, "installed_package_xml", return_value=destination / "package.xml"), \
                    mock.patch.object(freecad_addon_installer, "install_dependencies"), \
                    mock.patch.object(freecad_addon_installer, "emit"):
                freecad_addon_installer.install(archive)
            self.assertFalse(destination.is_symlink())
            self.assertEqual((source / "package.xml").read_text(), "original")
            self.assertEqual(freecad_addon_installer.package_version(destination / "package.xml"), "8.2")


if __name__ == "__main__":
    unittest.main()
