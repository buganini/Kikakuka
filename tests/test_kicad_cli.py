from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kicad_cli import resolve_kicad_cli


class KiCadCliTests(unittest.TestCase):
    def test_bundled_cli_takes_precedence_on_all_platforms(self):
        for system in ("Darwin", "Windows", "Linux"):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as directory:
                base = Path(directory) / "Contents" / "Frameworks"
                if system == "Darwin":
                    cli = base.parent / "MacOS" / "kicad-cli"
                else:
                    name = "kicad-cli.exe" if system == "Windows" else "kicad-cli"
                    cli = base / "KiCad" / "bin" / name
                base.mkdir(parents=True)
                cli.parent.mkdir(parents=True, exist_ok=True)
                cli.touch()
                with patch("kicad_cli.platform.system", return_value=system), \
                        patch("kicad_cli.sys._MEIPASS", str(base), create=True):
                    self.assertEqual(resolve_kicad_cli(), (str(cli), "Bundled"))

    def test_missing_bundle_falls_back_to_system_on_all_platforms(self):
        expected = {
            "Darwin": "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
            "Windows": "C:/Program Files/KiCad/10.0/bin/kicad-cli.exe",
            "Linux": "/opt/kicad/bin/kicad-cli",
        }
        with tempfile.TemporaryDirectory() as directory:
            for system, cli in expected.items():
                with self.subTest(system=system), \
                        patch("kicad_cli.platform.system", return_value=system), \
                        patch("kicad_cli.sys._MEIPASS", directory, create=True), \
                        patch("kicad_cli.glob.glob", return_value=[expected["Windows"]]), \
                        patch("kicad_cli.shutil.which", return_value=expected["Linux"]):
                    self.assertEqual(resolve_kicad_cli(), (cli, "System"))

    def test_linux_without_path_entry_keeps_default(self):
        with patch("kicad_cli.platform.system", return_value="Linux"), \
                patch("kicad_cli.sys._MEIPASS", None, create=True), \
                patch("kicad_cli.shutil.which", return_value=None):
            self.assertEqual(resolve_kicad_cli(), ("/usr/bin/kicad-cli", "System"))


if __name__ == "__main__":
    unittest.main()
