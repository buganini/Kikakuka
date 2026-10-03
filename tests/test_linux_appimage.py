import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

from im.instance_backend import external_process_environment, freecad_process_environment
from tools.linux_appimage import ADDONS, download_tool, external_dependencies, validate_addons


class LinuxAppImageTests(unittest.TestCase):
    def test_dependency_scan_rejects_unresolved_libraries(self):
        result = SimpleNamespace(returncode=0, stdout="libmissing.so => not found\n", stderr="")
        with tempfile.TemporaryDirectory() as directory, \
                patch("tools.linux_appimage.subprocess.run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "libmissing.so"):
                external_dependencies([Path("module.so")], Path(directory), Path(directory), {})

    def test_dependency_scan_skips_libraries_already_bundled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            internal = root / "_internal"
            internal.mkdir()
            bundled = root / "AppDir/shared/lib"
            bundled.mkdir(parents=True)
            (bundled / "libc.so.6").touch()
            result = SimpleNamespace(returncode=0, stderr="", stdout=(
                f"libpython.so => {internal}/libpython.so (0x1234)\n"
                "libc.so.6 => /usr/lib/libc.so.6 (0x1234)\n"
                "libextra.so => /usr/lib/libextra.so (0x1234)\n"))
            with patch("tools.linux_appimage.subprocess.run", return_value=result):
                self.assertEqual(external_dependencies(
                    [Path("module.so")], internal, root / "AppDir", {}),
                    [Path("/usr/lib/libextra.so").resolve()])

    def test_cached_tool_is_verified_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            tool_directory = cache / "sharun-v0.8.1-x86_64"
            tool_directory.mkdir()
            (tool_directory / "sharun").write_bytes(b"corrupted")
            with patch("tools.linux_appimage.urllib.request.urlopen") as request:
                with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                    download_tool("sharun", cache)
                request.assert_not_called()

    def test_all_three_addon_archives_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            internal = Path(directory)
            addons = internal / "addons"
            addons.mkdir()
            for name in ADDONS:
                with zipfile.ZipFile(addons / name, "w") as archive:
                    archive.writestr("payload", "content")
            self.assertEqual(set(validate_addons(internal)), set(ADDONS))
            (addons / ADDONS[0]).unlink()
            with self.assertRaises(FileNotFoundError):
                validate_addons(internal)

    def test_external_apps_receive_original_host_environment(self):
        host = {
            "PATH": "/usr/bin:/bin", "HOME": "/home/user",
            "XDG_DATA_DIRS": "/usr/share", "DISPLAY": ":0",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "APPDIR": "/tmp/mounted-appimage", "APPIMAGE": "/home/user/Kikakuka.AppImage",
            "PYTHONPATH": "/host/venv",
        }
        snapshot = base64.b64encode(b"\0".join(
            os.fsencode(f"{key}={value}") for key, value in host.items())).decode()
        contaminated = {
            "PATH": "/tmp/AppDir/bin", "QT_PLUGIN_PATH": "/tmp/AppDir/qt",
            "LD_LIBRARY_PATH": "/tmp/AppDir/lib", "SHARUN_DIR": "/tmp/AppDir",
            "KIKAKUKA_HOST_ENV": snapshot,
        }
        with patch.dict(os.environ, contaminated, clear=True):
            external = external_process_environment()
            freecad = freecad_process_environment()
            self.assertEqual(dict(os.environ), contaminated)
        self.assertEqual(external["PATH"], host["PATH"])
        self.assertEqual(external["XDG_DATA_DIRS"], host["XDG_DATA_DIRS"])
        self.assertEqual(external["DBUS_SESSION_BUS_ADDRESS"], host["DBUS_SESSION_BUS_ADDRESS"])
        for name in ("APPDIR", "APPIMAGE", "SHARUN_DIR", "KIKAKUKA_HOST_ENV", "LD_LIBRARY_PATH"):
            self.assertNotIn(name, external)
        self.assertNotIn("PYTHONPATH", freecad)
        self.assertEqual(freecad["HOME"], host["HOME"])

    def test_invalid_host_snapshot_does_not_silently_launch_with_bundled_libraries(self):
        with patch.dict(os.environ, {"KIKAKUKA_HOST_ENV": "invalid!"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "host environment"):
                external_process_environment()


if __name__ == "__main__":
    unittest.main()
