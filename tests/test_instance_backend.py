"""Tests for the editor logic executed by each mesh node."""

import os
import subprocess
import unittest
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from im import im_mesh
from im import instance_backend as backend
from im.kicad_api_retry import retry_kicad_call
from kipy.errors import ApiError
from kipy.proto.common import ApiStatusCode


class RetryKicadCallTests(unittest.TestCase):
    def test_reuses_busy_retry_policy(self):
        attempts = {"count": 0}

        def func():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ApiError("busy", code=ApiStatusCode.AS_BUSY)
            return "ok"

        with mock.patch("im.kicad_api_retry.time.sleep"):
            self.assertEqual(retry_kicad_call(func, max_retries=5), "ok")
        self.assertEqual(attempts["count"], 3)


class InstanceBackendTests(unittest.TestCase):
    def setUp(self):
        self.read_custom_executable = backend._custom_executable
        self.custom_executable = mock.patch.object(
            backend, "_custom_executable", return_value=None
        )
        self.custom_executable.start()
        self.addCleanup(self.custom_executable.stop)

    def test_each_node_can_read_manual_executable_from_user_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".kikakuka").write_text(
                '{"freecad_executable": "/opt/FreeCAD.AppImage"}',
                encoding="utf-8",
            )
            with mock.patch.object(Path, "home", return_value=home):
                self.assertEqual(
                    self.read_custom_executable("freecad"),
                    Path("/opt/FreeCAD.AppImage"),
                )

    def test_linux_focus_uses_x11_backend(self):
        with mock.patch.object(backend, "owned_pid_exists", return_value=True), \
                mock.patch.object(
                    backend.platform, "system", return_value="Linux"), \
                mock.patch(
                    "im.linux_window.bring_pid_to_front",
                    return_value=True,
                ) as activate:
            self.assertIsNone(backend._focus(123))
        activate.assert_called_once_with(123)

    def test_linux_focus_returns_wayland_error_without_qt(self):
        from im.linux_window import WindowActivationError

        with mock.patch.object(backend, "owned_pid_exists", return_value=True), \
                mock.patch.object(
                    backend.platform, "system", return_value="Linux"), \
                mock.patch(
                    "im.linux_window.bring_pid_to_front",
                    side_effect=WindowActivationError("native Wayland"),
                ):
            self.assertEqual(backend._focus(123), "native Wayland")

    def test_launch_empty_freecad_uses_application_not_pcb_association(self):
        for system, executable, expected in (
                ("Darwin", None, ["open", "-a", "FreeCAD", "-n", "-W", "--args"]),
                ("Linux", "/usr/bin/freecad", ["/usr/bin/freecad"]),
                ("Windows", "C:/FreeCAD/bin/FreeCAD.exe", ["C:/FreeCAD/bin/FreeCAD.exe"])):
            with self.subTest(system=system), \
                    mock.patch.object(backend.platform, "system", return_value=system), \
                    mock.patch.object(backend, "_editors", side_effect=[{}, {123: 1.0}]), \
                    mock.patch.object(backend, "_windows_freecad_executable", return_value=executable), \
                    mock.patch.object(backend.shutil, "which", return_value=executable), \
                    mock.patch.object(backend, "freecad_process_environment", return_value={}), \
                    mock.patch.object(backend.subprocess, "Popen") as popen:
                self.assertEqual(backend._launch(None, "freecad"), 123)
                self.assertEqual(popen.call_args.args[0], expected)

    def test_ready_board_does_not_use_inherited_kicad_token(self):
        with mock.patch.dict(os.environ, {"KICAD_API_TOKEN": "stale"}), \
                mock.patch("kipy.kicad.KiCad") as client, \
                mock.patch("im.kicad_api_retry.get_ready_kicad_board") as ready, \
                mock.patch.object(backend, "get_kicad_compat"):
            backend._ready_board("/tmp/selected.sock")
        client.assert_called_once_with(socket_path="ipc:///tmp/selected.sock",
                                       kicad_token="", timeout_ms=1000)
        self.assertIs(ready.call_args.args[0], client.return_value)

    def test_linux_file_launch_uses_isolated_freecad_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "freecad-startup.log"
            with mock.patch.object(backend.platform, "system", return_value="Linux"), \
                    mock.patch.object(backend, "_systemd_run_freecad", return_value=None), \
                    mock.patch.object(backend, "_editors", side_effect=[{}, {321: 1}]), \
                    mock.patch.object(backend.shutil, "which", return_value="/usr/bin/freecad"), \
                    mock.patch.object(backend, "freecad_launch_log_path", return_value=log_path), \
                    mock.patch.object(backend.subprocess, "Popen") as popen:
                self.assertEqual(backend._launch("/models/part.FCStd", "freecad"), 321)
            args, kwargs = popen.call_args
            self.assertEqual(args[0], ["/usr/bin/freecad", "/models/part.FCStd"])
            self.assertEqual(kwargs["env"],
                             backend.freecad_process_environment("/usr/bin/freecad"))
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["cwd"], Path.home())
            self.assertEqual(Path(kwargs["stdout"].name), log_path)

    def test_linux_prefers_systemd_user_service_for_freecad(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "freecad-startup.log"
            environment = {
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1002/bus",
                "DISPLAY": ":10.0",
                "PATH": "/usr/bin",
                "PYTHONNOUSERSITE": "1",
                "XDG_RUNTIME_DIR": "/run/user/1002",
                "KICAD_API_TOKEN": "must-not-be-forwarded",
                "UNRELATED_SECRET": "must-not-be-forwarded",
            }
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(
                    backend.platform, "system", return_value="Linux"), \
                    mock.patch.object(
                        backend, "freecad_process_environment",
                        return_value=environment), \
                    mock.patch.object(
                        backend, "freecad_launch_log_path",
                        return_value=log_path), \
                    mock.patch.object(
                        backend.shutil, "which",
                        return_value="/usr/bin/systemd-run"), \
                    mock.patch.object(
                        Path, "home", return_value=Path("/home/tester")), \
                    mock.patch.object(
                        backend.subprocess, "run",
                        return_value=completed) as run, \
                    mock.patch.object(backend.subprocess, "Popen") as popen:
                result = backend._popen_freecad(
                    ["/opt/FreeCAD.AppImage", "/models/part.FCStd"],
                    "/opt/FreeCAD.AppImage",
                )

            self.assertIs(result, completed)
            popen.assert_not_called()
            invocation = run.call_args.args[0]
            self.assertEqual(invocation[0], "/usr/bin/systemd-run")
            self.assertIn("--user", invocation)
            self.assertIn("--collect", invocation)
            self.assertNotIn("--scope", invocation)
            self.assertIn("--service-type=exec", invocation)
            self.assertIn("--working-directory=/home/tester", invocation)
            self.assertIn("--property=KillMode=process", invocation)
            self.assertIn("--property=PrivateTmp=no", invocation)
            self.assertIn(
                f"--property=StandardOutput=append:{log_path}", invocation
            )
            self.assertIn("--setenv=DISPLAY=:10.0", invocation)
            self.assertIn("--setenv=PYTHONNOUSERSITE=1", invocation)
            self.assertFalse(any(
                argument.startswith("--setenv=KICAD_API_TOKEN=")
                for argument in invocation
            ))
            self.assertFalse(any(
                argument.startswith("--setenv=UNRELATED_SECRET=")
                for argument in invocation
            ))
            self.assertEqual(
                invocation[-2:],
                ["/opt/FreeCAD.AppImage", "/models/part.FCStd"],
            )
            self.assertEqual(run.call_args.kwargs["env"], environment)

    def test_linux_falls_back_when_systemd_launch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "freecad-startup.log"
            environment = {
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1002/bus",
                "XDG_RUNTIME_DIR": "/run/user/1002",
            }
            failed = subprocess.CompletedProcess([], 1, "", "no user manager")
            with mock.patch.object(
                    backend.platform, "system", return_value="Linux"), \
                    mock.patch.object(
                        backend, "freecad_process_environment",
                        return_value=environment), \
                    mock.patch.object(
                        backend, "freecad_launch_log_path",
                        return_value=log_path), \
                    mock.patch.object(
                        backend.shutil, "which",
                        return_value="/usr/bin/systemd-run"), \
                    mock.patch.object(
                        Path, "home", return_value=Path("/home/tester")), \
                    mock.patch.object(
                        backend.subprocess, "run", return_value=failed), \
                    mock.patch.object(backend.subprocess, "Popen") as popen:
                result = backend._popen_freecad(
                    ["/opt/FreeCAD.AppImage"], "/opt/FreeCAD.AppImage"
                )

            self.assertIs(result, popen.return_value)
            args, kwargs = popen.call_args
            self.assertEqual(args[0], ["/opt/FreeCAD.AppImage"])
            self.assertEqual(kwargs["cwd"], Path("/home/tester"))
            self.assertEqual(kwargs["env"], environment)
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(Path(kwargs["stdout"].name), log_path)

    def test_manual_snap_console_launches_gui_companion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "freecad-startup.log"
            console = root / "freecad.cmd"
            gui = root / "freecad"
            console.touch()
            gui.touch()
            with mock.patch.object(
                    backend, "_custom_executable", return_value=console), \
                    mock.patch.object(
                        backend.platform, "system", return_value="Linux"), \
                    mock.patch.object(
                        backend, "_editors", side_effect=[{}, {321: 1}]), \
                    mock.patch.object(
                        backend, "freecad_process_environment", return_value={}), \
                    mock.patch.object(
                        backend, "freecad_launch_log_path", return_value=log_path), \
                    mock.patch.object(backend.subprocess, "Popen") as popen:
                self.assertEqual(
                    backend._launch("/models/part.FCStd", "freecad"), 321
                )
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [str(gui), "/models/part.FCStd"])
            self.assertEqual(kwargs["env"], {})
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(Path(kwargs["stdout"].name), log_path)

    def test_manual_macos_app_bundle_is_launched_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "FreeCAD.app"
            app.mkdir()
            with mock.patch.object(
                    backend, "_custom_executable", return_value=app), \
                    mock.patch.object(
                        backend.platform, "system", return_value="Darwin"), \
                    mock.patch.object(
                        backend, "_editors", side_effect=[{}, {321: 1}]), \
                    mock.patch.object(
                        backend, "freecad_process_environment", return_value={}), \
                    mock.patch.object(backend.subprocess, "Popen") as popen:
                self.assertEqual(
                    backend._launch("/models/part.FCStd", "freecad"), 321
                )

        popen.assert_called_once_with(
            [
                "open", "-a", str(app), "-n", "-W", "--args",
                "/models/part.FCStd",
            ],
            env={},
        )

    def test_manual_macos_console_command_launches_containing_app(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "FreeCAD.app"
            command = app / "Contents/Resources/bin/freecadcmd"
            command.parent.mkdir(parents=True)
            command.touch()
            with mock.patch.object(
                    backend, "_custom_executable", return_value=command), \
                    mock.patch.object(
                        backend.platform, "system", return_value="Darwin"), \
                    mock.patch.object(
                        backend, "_editors", side_effect=[{}, {321: 1}]), \
                    mock.patch.object(
                        backend, "freecad_process_environment", return_value={}), \
                    mock.patch.object(backend.subprocess, "Popen") as popen:
                self.assertEqual(
                    backend._launch("/models/part.FCStd", "freecad"), 321
                )

        popen.assert_called_once_with(
            [
                "open", "-a", str(app), "-n", "-W", "--args",
                "/models/part.FCStd",
            ],
            env={},
        )

    def test_manual_macos_kicad_binary_launches_containing_app(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "KiCad.app"
            executable = app / "Contents/MacOS/kicad"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with mock.patch.object(
                    backend, "_custom_executable", return_value=executable), \
                    mock.patch.object(
                        backend.platform, "system", return_value="Darwin"), \
                    mock.patch.object(
                        backend, "_editors", side_effect=[{}, {321: 1}]), \
                    mock.patch.object(backend.subprocess, "Popen") as popen:
                self.assertEqual(
                    backend._launch("/boards/main.kicad_pcb", "kicad"), 321
                )

        popen.assert_called_once_with([
            "open", "-a", str(app), "-n", "-g", "/boards/main.kicad_pcb",
        ])

    def test_orphaned_manual_kicad_cli_is_not_used_as_gui(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "kicad-cli"
            cli.touch()
            with mock.patch.object(
                backend, "_custom_executable", return_value=cli
            ):
                self.assertIsNone(
                    backend._configured_gui_executable("kicad")
                )

    def test_linux_kicad_executable_matches_file_type(self):
        paths = {
            "pcbnew": "/usr/bin/pcbnew",
            "eeschema": "/usr/bin/eeschema",
            "kicad": "/usr/bin/kicad",
        }
        with mock.patch.object(
                backend.shutil, "which", side_effect=paths.get):
            self.assertEqual(
                backend._linux_kicad_executable("/boards/main.kicad_pcb"),
                "/usr/bin/pcbnew",
            )
            self.assertEqual(
                backend._linux_kicad_executable("/boards/main.kicad_sch"),
                "/usr/bin/eeschema",
            )
            self.assertEqual(
                backend._linux_kicad_executable("/boards/main.kicad_pro"),
                "/usr/bin/kicad",
            )

    def test_linux_manual_kicad_cli_resolves_matching_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "kicad-cli"
            pcbnew = root / "pcbnew"
            cli.touch()
            pcbnew.touch()
            with mock.patch.object(
                    backend, "_custom_executable", return_value=cli):
                self.assertEqual(
                    backend._linux_kicad_executable(
                        "/boards/main.kicad_pcb"),
                    str(pcbnew),
                )

    def test_linux_manual_kicad_appimage_opens_every_file_type(self):
        with tempfile.TemporaryDirectory() as directory:
            appimage = Path(directory) / "KiCad.AppImage"
            appimage.touch()
            with mock.patch.object(
                    backend, "_custom_executable", return_value=appimage):
                for suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro"):
                    with self.subTest(suffix=suffix):
                        self.assertEqual(
                            backend._linux_kicad_executable(
                                "/boards/main" + suffix),
                            str(appimage),
                        )

    def test_linux_kicad_environment_uses_x11_only_with_xwayland(self):
        original = {
            "XDG_SESSION_TYPE": "wayland",
            "DISPLAY": ":0",
            "GDK_BACKEND": "wayland",
        }
        with mock.patch(
                "im.linux_window.xwayland_available",
                return_value=True):
            environment = backend._linux_kicad_environment(original)
        self.assertEqual(environment["GDK_BACKEND"], "x11")
        self.assertEqual(original["GDK_BACKEND"], "wayland")

        with mock.patch(
                "im.linux_window.xwayland_available",
                return_value=False):
            environment = backend._linux_kicad_environment({
                "XDG_SESSION_TYPE": "wayland",
                "DISPLAY": ":0",
            })
        self.assertNotIn("GDK_BACKEND", environment)

    def test_linux_kicad_launch_does_not_use_xdg_open(self):
        environment = {"GDK_BACKEND": "x11"}
        with mock.patch.object(
                backend.platform, "system", return_value="Linux"), \
                mock.patch.object(
                    backend, "_editors", side_effect=[{}, {321: 1}]), \
                mock.patch.object(
                    backend, "_linux_kicad_executable",
                    return_value="/usr/bin/pcbnew"), \
                mock.patch.object(
                    backend, "_linux_kicad_environment",
                    return_value=environment), \
                mock.patch.object(backend.subprocess, "Popen") as popen:
            self.assertEqual(
                backend._launch("/boards/main.kicad_pcb", "kicad"), 321
            )
        popen.assert_called_once_with(
            ["/usr/bin/pcbnew", "/boards/main.kicad_pcb"],
            env=environment,
        )

    def test_kicad_lock_path_matches_kicad_convention(self):
        self.assertEqual(
            backend._kicad_lock_path("/boards/main.kicad_pcb"),
            Path("/boards/~main.kicad_pcb.lck"),
        )

    def test_foreign_kicad_lock_may_prompt_open_anyway(self):
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / "main.kicad_pcb"
            backend._kicad_lock_path(board).write_text(
                '{"username":"someone-else","hostname":"another-host"}',
                encoding="utf-8",
            )
            self.assertTrue(
                backend._kicad_file_may_prompt_open_anyway(board))

    def test_stale_owned_kicad_lock_does_not_require_foreground(self):
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / "main.kicad_pcb"
            backend._kicad_lock_path(board).write_text(
                '{"username":"current-user","hostname":"current-host"}',
                encoding="utf-8",
            )
            with mock.patch.object(
                    backend.getpass, "getuser", return_value="current-user"), \
                    mock.patch.object(
                        backend.socket, "gethostname", return_value="current-host"), \
                    mock.patch.object(backend, "_editors", return_value={}):
                self.assertFalse(
                    backend._kicad_file_may_prompt_open_anyway(board))

    def test_owned_kicad_lock_prompts_when_another_kicad_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / "main.kicad_pcb"
            backend._kicad_lock_path(board).write_text(
                '{"username":"current-user","hostname":"current-host"}',
                encoding="utf-8",
            )
            with mock.patch.object(
                    backend.getpass, "getuser", return_value="current-user"), \
                    mock.patch.object(
                        backend.socket, "gethostname", return_value="current-host"), \
                    mock.patch.object(backend, "_editors", return_value={123: 1.0}):
                self.assertTrue(
                    backend._kicad_file_may_prompt_open_anyway(board))

    def test_editors_excludes_processes_owned_by_other_users(self):
        own = mock.Mock(
            pid=111,
            info={
                "pid": 111,
                "name": "pcbnew",
                "create_time": 1,
                "uids": mock.Mock(effective=1000),
            },
        )
        other = mock.Mock(
            pid=222,
            info={
                "pid": 222,
                "name": "pcbnew",
                "create_time": 2,
                "uids": mock.Mock(effective=2000),
            },
        )

        with mock.patch.object(
                    backend, "owned_process_iter", return_value=[own]
                ) as process_iter:
            self.assertEqual(backend._editors(), {111: 1})

        process_iter.assert_called_once_with(
            ["pid", "name", "create_time"]
        )

    def test_windows_editors_excludes_processes_owned_by_other_users(self):
        own = mock.Mock(
            pid=111,
            info={
                "pid": 111,
                "name": "pcbnew.exe",
                "create_time": 1,
                "username": r"WORKSTATION\alice",
            },
        )
        other = mock.Mock(
            pid=222,
            info={
                "pid": 222,
                "name": "pcbnew.exe",
                "create_time": 2,
                "username": r"WORKSTATION\bob",
            },
        )
        with mock.patch.object(
                    backend, "owned_process_iter", return_value=[own]
                ) as process_iter:
            self.assertEqual(backend._editors(), {111: 1})

        process_iter.assert_called_once_with(
            ["pid", "name", "create_time"]
        )

    def test_editors_excludes_non_editor_kicad_processes(self):
        processes = [
            mock.Mock(
                pid=pid,
                info={"pid": pid, "name": name, "create_time": pid},
            )
            for pid, name in enumerate(
                ("kicad", "pcbnew", "eeschema", "PCB Editor",
                 "kicad-api", "kicad-api.exe", "kicad-cli"),
                start=1,
            )
        ]
        with mock.patch.object(
            backend, "owned_process_iter", return_value=processes
        ):
            self.assertEqual(
                backend._editors(),
                {pid: pid for pid in range(1, 5)},
            )

    def test_windows_kicad_api_sentinel_is_created_once(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_kicad_socket_directory", return_value=directory
                ):
            sentinel = backend.ensure_windows_kicad_api_sentinel()
            self.assertEqual(sentinel, str(Path(directory) / "api.sock"))
            self.assertEqual(
                Path(sentinel).read_bytes(),
                backend.WINDOWS_KICAD_API_SENTINEL,
            )
            Path(sentinel).write_bytes(b"existing")
            self.assertEqual(
                backend.ensure_windows_kicad_api_sentinel(), sentinel
            )
            self.assertEqual(Path(sentinel).read_bytes(), b"existing")

    def test_windows_kicad_api_sentinel_does_not_replace_directory(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_kicad_socket_directory", return_value=directory
                ):
            sentinel = Path(directory) / "api.sock"
            sentinel.mkdir()
            self.assertIsNone(
                backend.ensure_windows_kicad_api_sentinel()
            )
            self.assertTrue(sentinel.is_dir())

    def test_windows_socket_candidates_come_from_named_pipe_enumeration(self):
        directory = r"C:\Temp\kicad"
        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_kicad_socket_directory", return_value=directory
                ), \
                mock.patch.object(
                    backend, "ensure_windows_kicad_api_sentinel"
                ), \
                mock.patch.object(
                    backend.os, "listdir", side_effect=[
                        ["api.sock"],
                        [r"C:\Temp\kicad\api-222.sock"],
                    ]
                ), \
                mock.patch.object(
                    backend, "_editors", return_value={111: 1, 222: 2}
                ), \
                mock.patch.object(
                    backend, "_windows_named_pipe_owner"
                ) as owner:
            self.assertEqual(backend._sockets(), [
                (222, os.path.join(directory, "api-222.sock")),
                (111, os.path.join(directory, "api.sock")),
            ])
        owner.assert_not_called()

    def test_windows_generic_pipe_uses_server_pid(self):
        directory = r"C:\Temp\kicad"
        editors = {111: 1, 222: 2}
        pipe_name = r"C:\Temp\kicad\api.sock"
        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_kicad_socket_directory", return_value=directory
                ), \
                mock.patch.object(
                    backend, "ensure_windows_kicad_api_sentinel"
                ), \
                mock.patch.object(
                    backend.os, "listdir", side_effect=[
                        ["api.sock"],
                        [pipe_name],
                    ]
                ), \
                mock.patch.object(backend, "_editors", return_value=editors), \
                mock.patch.object(
                    backend, "_windows_named_pipe_owner", return_value=222
                ) as owner:
            self.assertEqual(backend._sockets(), [
                (222, os.path.join(directory, "api.sock")),
            ])

        owner.assert_called_once_with(
            rf"\\.\pipe\{pipe_name}", editors, set()
        )

    def test_windows_named_pipe_owner_validates_live_editor(self):
        process = mock.Mock()
        process.create_time.return_value = 2
        pipe_path = r"\\.\pipe\C:\Temp\kicad\api.sock"

        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_windows_named_pipe_server_pid",
                    return_value=222,
                ), \
                mock.patch.object(
                    backend, "owned_process", return_value=process
                ):
            self.assertEqual(
                backend._windows_named_pipe_owner(
                    pipe_path, {111: 1, 222: 2}, excluded_pids={111}
                ),
                222,
            )

    def test_windows_named_pipe_server_pid_closes_handle(self):
        create_file = mock.Mock(return_value=123)
        get_server_pid = mock.Mock()

        def return_pid(_handle, pid_pointer):
            pid_pointer._obj.value = 222
            return True

        get_server_pid.side_effect = return_pid
        close_handle = mock.Mock(return_value=True)
        kernel32 = mock.Mock(
            CreateFileW=create_file,
            GetNamedPipeServerProcessId=get_server_pid,
            CloseHandle=close_handle,
        )
        pipe_path = r"\\.\pipe\C:\Temp\kicad\api.sock"

        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch(
                    "ctypes.WinDLL", return_value=kernel32, create=True
                ):
            self.assertEqual(
                backend._windows_named_pipe_server_pid(pipe_path), 222
            )

        create_file.assert_called_once_with(
            pipe_path, 0, 0, None, 3, 0, None
        )
        get_server_pid.assert_called_once()
        close_handle.assert_called_once_with(123)

    def test_windows_named_pipe_owner_ignores_unknown_or_reused_pid(self):
        pipe_path = r"\\.\pipe\C:\Temp\kicad\api.sock"

        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_windows_named_pipe_server_pid",
                    side_effect=[333, 222],
                ), \
                mock.patch.object(
                    backend, "owned_process", return_value=None
                ) as owned_process:
            self.assertIsNone(
                backend._windows_named_pipe_owner(pipe_path, {222: 2})
            )
            owned_process.assert_not_called()
            self.assertIsNone(
                backend._windows_named_pipe_owner(pipe_path, {222: 2})
            )
            owned_process.assert_called_once_with(222, 2)

    def test_unix_socket_owner_scans_only_same_user_unmapped_editors(self):
        owner = mock.Mock()
        owner.net_connections.return_value = [
            mock.Mock(laddr="/tmp/kicad/api.sock"),
        ]
        processes = {111: None, 333: owner}

        with mock.patch.object(backend.platform, "system", return_value="Linux"), \
                mock.patch.object(
                    backend, "owned_process",
                    side_effect=lambda pid, _created: processes[pid],
                ) as process:
            self.assertEqual(
                backend._unix_socket_owner(
                    "/tmp/kicad/api.sock",
                    {111: 1, 222: 2, 333: 3},
                    excluded_pids={222},
                ),
                333,
            )

        self.assertEqual(process.call_args_list, [mock.call(111, 1),
                                                  mock.call(333, 3)])
        owner.net_connections.assert_called_once_with(kind="unix")

    def test_unix_socket_owner_continues_after_process_error(self):
        inaccessible = mock.Mock()
        inaccessible.uids.return_value = mock.Mock(effective=1000)
        inaccessible.create_time.return_value = 1
        inaccessible.net_connections.side_effect = backend.psutil.AccessDenied(
            pid=111
        )
        owner = mock.Mock()
        owner.net_connections.return_value = [
            mock.Mock(laddr="/tmp/kicad/api.sock"),
        ]

        with mock.patch.object(backend.platform, "system", return_value="Darwin"), \
                mock.patch.object(
                    backend, "owned_process",
                    side_effect=lambda pid, _created: {
                        111: inaccessible, 222: owner
                    }[pid],
                ):
            self.assertEqual(
                backend._unix_socket_owner(
                    "/tmp/kicad/api.sock", {111: 1, 222: 2}
                ),
                222,
            )

    def test_unix_socket_owner_retries_before_fallback(self):
        owner = mock.Mock()
        owner.net_connections.side_effect = [
            [],
            [mock.Mock(laddr="/tmp/kicad/api.sock")],
        ]

        with mock.patch.object(backend.platform, "system", return_value="Linux"), \
                mock.patch.object(
                    backend, "owned_process", return_value=owner
                ) as owned_process, \
                mock.patch.object(backend.time, "sleep") as sleep:
            self.assertEqual(
                backend._unix_socket_owner(
                    "/tmp/kicad/api.sock",
                    {111: 1},
                    max_retries=2,
                    delay_s=0.05,
                ),
                111,
            )

        self.assertEqual(owned_process.call_count, 2)
        self.assertEqual(owner.net_connections.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_scan_open_boards_reads_each_reachable_kicad_endpoint(self):
        with mock.patch.object(backend, "_sockets", return_value=[
                (111, "/tmp/kicad/api.sock"),
                (222, "/tmp/kicad/api-222.sock"),
                (333, "/tmp/kicad/api-333.sock"),
        ]), mock.patch.object(backend, "_board_path", side_effect=[
                "/boards/one.kicad_pcb", RuntimeError("busy"),
                "/boards/three.kicad_pcb",
        ]):
            self.assertEqual(backend.scan_open_kicad_boards(), [
                (111, "/boards/one.kicad_pcb", "/tmp/kicad/api.sock"),
                (333, "/boards/three.kicad_pcb", "/tmp/kicad/api-333.sock"),
            ])

    def test_mac_freecad_launch_uses_explicit_app_and_file_argument(self):
        with mock.patch.object(backend.platform, "system", return_value="Darwin"), \
                mock.patch.object(backend, "_editors", side_effect=[{}, {321: 1}]), \
                mock.patch.object(backend.subprocess, "Popen") as popen:
            self.assertEqual(backend._launch("/models/part.FCStd", "freecad"), 321)
        popen.assert_called_once_with([
            "open", "-a", "FreeCAD", "-n", "-W", "--args", "/models/part.FCStd"],
            env=backend.freecad_process_environment())

    def test_windows_freecad_launch_does_not_require_kkkk_asm_association(self):
        filepath = "C:/models/assembly.kkkk_asm"
        executable = "C:/Program Files/FreeCAD 1.1/bin/FreeCAD.exe"
        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "_windows_freecad_executable",
                    return_value=executable
                ), \
                mock.patch.object(
                    backend, "_editors", side_effect=[{}, {321: 1}]
                ), \
                mock.patch.object(backend.subprocess, "Popen") as popen, \
                mock.patch.object(
                    backend.os, "startfile", create=True
                ) as startfile:
            self.assertEqual(backend._launch(filepath, "freecad"), 321)
        popen.assert_called_once_with(
            [executable, filepath], env=backend.freecad_process_environment(executable))
        startfile.assert_not_called()

    def test_freecad_environment_does_not_inherit_kicad_python_and_qt(self):
        contaminated = {
            "PYTHONUSERBASE": "C:/KiCad/3rdparty", "PYTHONPATH": "C:/KiCad/python",
            "PYTHONHOME": "C:/KiCad", "QT_PLUGIN_PATH": "C:/KiCad/Qt/plugins",
            "PYTHONEXECUTABLE": "C:/KiCad/python.exe",
            "VIRTUAL_ENV": "C:/KiCad/plugin-venv",
            "VIRTUAL_ENV_PROMPT": "kicad-plugin",
            "__PYVENV_LAUNCHER__": "C:/KiCad/python.exe",
            "QT_QPA_PLATFORM_PLUGIN_PATH": "C:/KiCad/Qt/platforms",
            "QML2_IMPORT_PATH": "C:/KiCad/qml", "PATH": "C:/Windows",
            "APPDATA": "C:/Users/test/AppData/Roaming",
            "KICAD_API_TOKEN": "stale", "KICAD_API_SOCKET": "ipc://old.sock",
            "DYLD_LIBRARY_PATH": "/KiCad/lib", "DYLD_FRAMEWORK_PATH": "/KiCad/Frameworks",
            "DYLD_FALLBACK_LIBRARY_PATH": "/KiCad/lib", "DYLD_FALLBACK_FRAMEWORK_PATH": "/KiCad/Frameworks",
            "DYLD_INSERT_LIBRARIES": "/KiCad/inject.dylib",
            "LD_LIBRARY_PATH": "/KiCad/lib", "LD_PRELOAD": "/KiCad/inject.so",
            "QT_QPA_FONTDIR": "/KiCad/fonts",
        }
        with mock.patch.dict(backend.os.environ, contaminated, clear=True):
            environment = backend.freecad_process_environment("C:/FreeCAD/bin/FreeCAD.exe")
            self.assertEqual(dict(backend.os.environ), contaminated)
        for name in contaminated.keys() - {"PATH", "APPDATA"}:
            self.assertNotIn(name, environment)
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(environment["APPDATA"], contaminated["APPDATA"])
        self.assertEqual(environment["PATH"], str(Path("C:/FreeCAD/bin")) + os.pathsep + "C:/Windows")

    def test_windows_kicad_launch_ensures_api_sentinel(self):
        with mock.patch.object(
                    backend.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    backend, "ensure_windows_kicad_api_sentinel"
                ) as ensure, \
                mock.patch.object(
                    backend, "_editors", side_effect=[{}, {321: 1}]
                ), \
                mock.patch.object(
                    backend.os, "startfile", create=True
                ) as startfile:
            self.assertEqual(
                backend._launch("C:/boards/test.kicad_pcb"), 321
            )
        ensure.assert_called_once_with()
        startfile.assert_called_once_with(
            "C:/boards/test.kicad_pcb", show_cmd=4)

    def test_different_board_files_do_not_overlap_editor_launch(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def launch(_filepath, _program):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.1)
            with guard:
                active -= 1
            return 123

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(backend, "_find_board", return_value=(None, None, None)), \
                    mock.patch.object(backend, "_launch", side_effect=launch), \
                    mock.patch.object(backend, "_wait_for_board", return_value=(123, "/ipc/api.sock")):
                threads = [threading.Thread(target=backend._open_new,
                                            args=(f"/boards/{name}.kicad_pcb", "kicad", True, None))
                           for name in ("first", "second")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(maximum, 1)

    def test_board_launch_waits_for_verified_path_before_releasing_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(backend, "_find_board", return_value=(None, None, None)), \
                    mock.patch.object(backend, "_launch", return_value=111), \
                    mock.patch.object(backend, "_wait_for_board", return_value=(222, "/ipc/api-222.sock")) as wait:
                pid, socket_path = backend._open_new(
                    "/boards/main.kicad_pcb", "kicad", True, None)
        self.assertEqual((pid, socket_path), (222, "/ipc/api-222.sock"))
        wait.assert_called_once_with("/boards/main.kicad_pcb")

    def test_locked_board_is_focused_before_waiting_for_ipc(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(backend, "_find_board", return_value=(None, None, None)), \
                    mock.patch.object(
                        backend, "_kicad_file_may_prompt_open_anyway",
                        side_effect=lambda _path: events.append("lock") or True,
                    ), \
                    mock.patch.object(
                        backend, "_launch",
                        side_effect=lambda _path, _program: events.append("launch") or 111,
                    ), \
                    mock.patch.object(
                        backend, "_focus",
                        side_effect=lambda _pid: events.append("focus"),
                    ) as focus, \
                    mock.patch.object(
                        backend, "_wait_for_board",
                        side_effect=lambda _path: events.append("wait") or
                        (111, "/ipc/api.sock"),
                    ):
                result = backend._open_new(
                    "/boards/main.kicad_pcb", "kicad", True, None)
        self.assertEqual(result, (111, "/ipc/api.sock"))
        self.assertEqual(events, ["lock", "launch", "focus", "wait"])
        focus.assert_called_once_with(111)

    def test_unlocked_linked_board_launch_stays_in_background(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(backend, "_find_board", return_value=(None, None, None)), \
                    mock.patch.object(
                        backend, "_kicad_file_may_prompt_open_anyway",
                        return_value=False,
                    ), \
                    mock.patch.object(backend, "_launch", return_value=111), \
                    mock.patch.object(
                        backend, "_wait_for_board",
                        return_value=(111, "/ipc/api.sock"),
                    ), \
                    mock.patch.object(backend, "_focus") as focus:
                result = backend._open_new(
                    "/boards/main.kicad_pcb", "kicad", True, None)
        self.assertEqual(result, (111, "/ipc/api.sock"))
        focus.assert_not_called()

    def test_macos_linked_board_restores_requesting_freecad_focus(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(
                        backend, "_find_board", return_value=(None, None, None)), \
                    mock.patch.object(
                        backend, "_kicad_file_may_prompt_open_anyway",
                        return_value=False,
                    ), \
                    mock.patch.object(
                        backend, "_launch",
                        side_effect=lambda _path, _program:
                        events.append(("launch", 111)) or 111,
                    ), \
                    mock.patch.object(
                        backend, "_wait_for_board",
                        side_effect=lambda _path:
                        events.append(("ready", 111)) or
                        (111, "/ipc/api.sock"),
                    ), \
                    mock.patch.object(
                        backend, "_focus",
                        side_effect=lambda pid: events.append(("focus", pid)),
                    ):
                result = backend._open_new(
                    "/boards/main.kicad_pcb", "kicad", True, None,
                    restore_focus_pid=222)

        self.assertEqual(result, (111, "/ipc/api.sock"))
        self.assertEqual(events, [
            ("launch", 111),
            ("focus", 222),
            ("ready", 111),
            ("focus", 222),
        ])

    def test_macos_lock_prompt_stays_front_until_board_is_ready(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(
                        backend, "_find_board", return_value=(None, None, None)), \
                    mock.patch.object(
                        backend, "_kicad_file_may_prompt_open_anyway",
                        return_value=True,
                    ), \
                    mock.patch.object(
                        backend, "_launch", return_value=111), \
                    mock.patch.object(
                        backend, "_wait_for_board",
                        side_effect=lambda _path:
                        events.append(("ready", 111)) or
                        (111, "/ipc/api.sock"),
                    ), \
                    mock.patch.object(
                        backend, "_focus",
                        side_effect=lambda pid: events.append(("focus", pid)),
                    ):
                result = backend._open_new(
                    "/boards/main.kicad_pcb", "kicad", True, None,
                    restore_focus_pid=222)

        self.assertEqual(result, (111, "/ipc/api.sock"))
        self.assertEqual(events, [
            ("focus", 111),
            ("ready", 111),
            ("focus", 222),
        ])

    def test_unresolved_macos_lock_prompt_remains_in_front(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(
                        backend, "_find_board", return_value=(None, None, None)), \
                    mock.patch.object(
                        backend, "_kicad_file_may_prompt_open_anyway",
                        return_value=True,
                    ), \
                    mock.patch.object(
                        backend, "_launch", return_value=111), \
                    mock.patch.object(
                        backend, "_wait_for_board", return_value=(None, None)), \
                    mock.patch.object(backend, "_focus") as focus:
                result = backend._open_new(
                    "/boards/main.kicad_pcb", "kicad", True, None,
                    restore_focus_pid=222)

        self.assertEqual(result, (None, None))
        focus.assert_called_once_with(111)

    def test_freekicad_linked_action_does_not_focus_ready_board(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_find_board", return_value=(None, None, None)), \
                mock.patch.object(
                    backend, "_open_new",
                    return_value=(111, "/ipc/api.sock"),
                ), \
                mock.patch.object(backend, "_focus") as focus:
            reply = backend.handle({
                "action": "reload",
                "filepath": "/boards/main.kicad_pcb",
            })
        self.assertEqual(reply["status"], "ok")
        self.assertEqual(reply["pid"], 111)
        focus.assert_not_called()

    def test_macos_reload_passes_caller_to_new_board_launch(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(
                    backend.platform, "system", return_value="Darwin"), \
                mock.patch.object(
                    backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_find_board", return_value=(None, None, None)), \
                mock.patch.object(
                    backend, "_open_new",
                    return_value=(111, "/ipc/api.sock"),
                ) as open_new:
            reply = backend.handle({
                "action": "reload",
                "filepath": "/boards/main.kicad_pcb",
                "caller_pid": 222,
            })

        self.assertEqual(reply["status"], "ok")
        open_new.assert_called_once_with(
            "/boards/main.kicad_pcb", "kicad", True, node,
            ensure_fresh=False, restore_focus_pid=222)

    def test_rejects_invalid_caller_pid(self):
        reply = backend.handle({
            "action": "reload",
            "filepath": "/boards/main.kicad_pcb",
            "caller_pid": True,
        })

        self.assertEqual(reply, {
            "status": "error", "message": "invalid caller PID"})

    def test_board_reuses_only_verified_matching_socket(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/boards/main.kicad_pcb": 111}
        board = mock.Mock()
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_find_board",
                    return_value=(111, "/ipc/api.sock", board),
                ), \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus") as focus:
            reply = backend.handle({"action": "open-file", "filepath": "/boards/main.kicad_pcb"})
        self.assertEqual(reply["pid"], 111)
        self.assertEqual(reply["socket"], "/ipc/api.sock")
        board.revert.assert_not_called()
        launch.assert_not_called()
        node.publish.assert_not_called()
        focus.assert_called_once_with(111)

    def test_open_reply_includes_window_activation_error(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/boards/main.kicad_pcb": 111}
        board = mock.Mock()
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_find_board",
                    return_value=(111, "/ipc/api.sock", board),
                ), \
                mock.patch.object(
                    backend, "_focus", return_value="native Wayland"):
            reply = backend.handle({
                "action": "open-file",
                "filepath": "/boards/main.kicad_pcb",
            })

        self.assertEqual(reply["status"], "ok")
        self.assertEqual(reply["activation_error"], "native Wayland")
        self.assertEqual(reply["message"], "native Wayland")

    def test_ensure_fresh_reuses_ready_probe_connection_for_revert(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/boards/main.kicad_pcb": 111}
        board = mock.Mock()
        board.name = "/boards/main.kicad_pcb"
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_sockets",
                    return_value=[(111, "/ipc/api.sock")],
                ), \
                mock.patch.object(
                    backend, "_ready_board", return_value=board,
                ) as ready, \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus"):
            reply = backend.handle({
                "action": "open-file",
                "filepath": "/boards/main.kicad_pcb",
                "ensure_fresh": True,
            })
        self.assertEqual(reply["pid"], 111)
        ready.assert_called_once_with(
            "/ipc/api.sock",
            max_retries=backend.FRESH_READY_RETRIES,
            delay_s=backend.FRESH_READY_DELAY_S,
        )
        board.revert.assert_called_once_with()
        board.get_shapes.assert_called_once_with()
        launch.assert_not_called()

    def test_ensure_fresh_handles_board_opened_while_waiting_for_launch_lock(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        board = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(im_mesh, "runtime_dir", return_value=Path(directory)), \
                    mock.patch.object(backend, "local_node", return_value=node), \
                    mock.patch.object(backend.os.path, "isfile", return_value=True), \
                    mock.patch.object(backend, "_find_board", side_effect=[
                        (None, None, None),
                        (111, "/ipc/api.sock", board),
                    ]), \
                    mock.patch.object(backend, "_launch") as launch, \
                    mock.patch.object(backend, "_focus"):
                reply = backend.handle({
                    "action": "open-file",
                    "filepath": "/boards/main.kicad_pcb",
                    "ensure_fresh": True,
                })
        self.assertEqual(reply["pid"], 111)
        board.revert.assert_called_once_with()
        board.get_shapes.assert_called_once_with()
        launch.assert_not_called()

    def test_mismatched_board_mapping_is_invalidated(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/boards/main.kicad_pcb": 111}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_find_board",
                    return_value=(222, "/ipc/api-222.sock", mock.Mock()),
                ), \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus"):
            reply = backend.handle({"action": "open-file", "filepath": "/boards/main.kicad_pcb"})
        self.assertEqual(reply["pid"], 222)
        node.publish.assert_called_once_with("/boards/main.kicad_pcb", None)
        launch.assert_not_called()

    def test_monitor_does_not_launch_unopened_board(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "_find_board", return_value=(None, None, None)), \
                mock.patch.object(backend, "_launch") as launch:
            reply = backend.handle({"action": "monitor-couplers", "filepath": "/boards/main.kicad_pcb"})
        self.assertEqual(reply["status"], "error")
        launch.assert_not_called()

    def test_resolve_includes_caller_fields_and_verified_socket(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(
                    backend, "_find_board",
                    return_value=(123, "/ipc/api.sock", mock.Mock()),
                ):
            reply = backend.handle({"action": "move-component", "filepath": "/boards/main.kicad_pcb",
                                    "object": "Board", "component": "U1"})
        self.assertEqual(reply["socket"], "/ipc/api.sock")
        self.assertEqual(reply["object"], "Board")
        self.assertEqual(reply["component"], "U1")

    def test_freecad_file_uses_same_open_and_focus_path(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
                mock.patch.object(backend, "_editors", return_value={}), \
                mock.patch.object(backend, "bind_freecad_source", return_value=True), \
                mock.patch.object(backend, "_launch", return_value=321) as launch, \
                mock.patch.object(backend, "_focus") as focus:
            reply = backend.handle({"action": "open-file", "filepath": "/models/part.FCStd"})
        self.assertEqual(reply["pid"], 321)
        launch.assert_called_once_with("/models/part.FCStd", "freecad")
        focus.assert_called_once_with(321)

    def test_freecad_live_document_overrides_stale_pidmap(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/models/part.FCStd": 111}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "owned_pid_exists", return_value=True), \
                mock.patch.object(backend, "activate_open_freecad_document",
                                  return_value=222) as activate, \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus") as focus:
            reply = backend.handle({"action": "open-file", "filepath": "/models/part.FCStd"})
        self.assertEqual(reply["pid"], 222)
        activate.assert_called_once_with(
            "/models/part.FCStd", before_activate=focus)
        launch.assert_not_called()
        focus.assert_called_once_with(222)

    def test_freecad_closed_document_is_not_reused_from_pidmap(self):
        node = mock.Mock()
        node.snapshot.return_value = {"/models/part.FCStd": 111}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "owned_pid_exists", return_value=True), \
                mock.patch.object(backend, "activate_open_freecad_document",
                                  return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
                mock.patch.object(backend, "_editors", return_value={}), \
                mock.patch.object(backend, "bind_freecad_source", return_value=True), \
                mock.patch.object(backend, "_launch", return_value=321) as launch, \
                mock.patch.object(backend, "_focus"):
            reply = backend.handle({"action": "open-file", "filepath": "/models/part.FCStd"})
        self.assertEqual(reply["pid"], 321)
        launch.assert_called_once_with("/models/part.FCStd", "freecad")

    def test_new_freecad_file_opens_in_existing_gui_node(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "activate_open_freecad_document",
                                  return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node",
                                  return_value=222) as open_in_node, \
                mock.patch.object(backend, "_launch") as launch, \
                mock.patch.object(backend, "_focus") as focus:
            reply = backend.handle({"action": "open-file", "filepath": "/models/new.step"})
        self.assertEqual(reply["pid"], 222)
        open_in_node.assert_called_once_with(
            "/models/new.step", before_open=focus)
        launch.assert_not_called()
        focus.assert_called_once_with(222)

    def test_freecad_node_import_failure_does_not_launch_duplicate(self):
        node = mock.Mock()
        node.snapshot.return_value = {}
        with mock.patch.object(backend, "local_node", return_value=node), \
                mock.patch.object(backend.os.path, "isfile", return_value=True), \
                mock.patch.object(backend, "activate_open_freecad_document",
                                  return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node",
                                  side_effect=RuntimeError("import failed")), \
                mock.patch.object(backend, "_launch") as launch:
            with self.assertRaisesRegex(RuntimeError, "import failed"):
                backend.handle({"action": "open-file", "filepath": "/models/new.step"})
        launch.assert_not_called()

    def test_unreachable_running_freecad_does_not_launch_duplicate(self):
        with mock.patch.object(backend, "activate_open_freecad_document",
                               return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
                mock.patch.object(backend, "_editors", return_value={321: 1}), \
                mock.patch.object(backend, "_launch") as launch:
            with self.assertRaisesRegex(RuntimeError, "node is unavailable"):
                backend._open_new("/models/part.FCStd", "freecad", False, None)
        launch.assert_not_called()

    def test_freecad_launch_is_not_success_until_document_is_identified(self):
        with mock.patch.object(backend, "activate_open_freecad_document",
                               return_value=None), \
                mock.patch.object(backend, "open_in_freecad_node", return_value=None), \
                mock.patch.object(backend, "_editors", return_value={}), \
                mock.patch.object(backend, "_launch", return_value=321), \
                mock.patch.object(backend, "bind_freecad_source",
                                  return_value=False) as bind:
            with self.assertRaisesRegex(RuntimeError, "did not report"):
                backend._open_new("/models/part.step", "freecad", False, None)
        bind.assert_called_once_with(321, "/models/part.step")

    def test_sockets_fall_back_to_oldest_unmapped_editor(self):
        with mock.patch.object(backend.os, "listdir", return_value=["api.sock", "api-222.sock"]), \
                mock.patch.object(backend, "_editors", return_value={111: 1, 222: 2}), \
                mock.patch.object(
                    backend, "_unix_socket_owner", return_value=None
                ) as owner:
            sockets = backend._sockets()
        self.assertEqual(dict(sockets)[111].split("/")[-1], "api.sock")
        self.assertEqual(dict(sockets)[222].split("/")[-1], "api-222.sock")
        owner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
