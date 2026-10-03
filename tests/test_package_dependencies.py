import unittest
from unittest.mock import patch

from tools.package_dependencies import check_dependencies


class PackageDependencyTests(unittest.TestCase):
    def test_cli_and_python_bindings_must_match(self):
        with patch("tools.package_dependencies.platform.system", return_value="Linux"), \
                patch("tools.package_dependencies.platform.machine", return_value="x86_64"), \
                patch("tools.package_dependencies.importlib.util.find_spec", return_value=object()), \
                patch("tools.package_dependencies.os.path.isfile", return_value=True), \
                patch("tools.package_dependencies.os.access", return_value=True), \
                patch("tools.package_dependencies.shutil.which", return_value="/bin/tool"), \
                patch("tools.package_dependencies.subprocess.check_output", side_effect=["10.0.6\n", "9.0.8\n"]):
            with self.assertRaisesRegex(RuntimeError, "version mismatch"):
                check_dependencies("/bin/kicad-cli")

    def test_missing_requirements_are_reported_together(self):
        with patch("tools.package_dependencies.platform.system", return_value="Linux"), \
                patch("tools.package_dependencies.platform.machine", return_value="x86_64"), \
                patch("tools.package_dependencies.importlib.util.find_spec", return_value=None), \
                patch("tools.package_dependencies.os.path.isfile", return_value=False), \
                patch("tools.package_dependencies.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                check_dependencies("/missing/kicad-cli")
        message = str(caught.exception)
        for expected in ("PyInstaller", "pcbnew", "/missing/kicad-cli", "patchelf", "sudo apt install"):
            self.assertIn(expected, message)

    def test_unsupported_linux_architecture_fails_before_build(self):
        with patch("tools.package_dependencies.platform.system", return_value="Linux"), \
                patch("tools.package_dependencies.platform.machine", return_value="aarch64"), \
                patch("tools.package_dependencies.importlib.util.find_spec", return_value=object()), \
                patch("tools.package_dependencies.os.path.isfile", return_value=True), \
                patch("tools.package_dependencies.os.access", return_value=True), \
                patch("tools.package_dependencies.shutil.which", return_value="/bin/tool"):
            with self.assertRaisesRegex(RuntimeError, "native x86_64"):
                check_dependencies("/bin/kicad-cli")


if __name__ == "__main__":
    unittest.main()
