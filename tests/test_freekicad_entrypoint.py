"""FreeCAD process registration must not depend on workbench activation."""

import importlib.util
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "FreekiCAD" / "freecad" / "FreekiCAD"
PACKAGE_NAME = "FreekiCAD.freecad.FreekiCAD"
EXPORT_SCRIPT = Path(__file__).resolve().parents[1] / "FreekiCAD" / "scripts" / "kkkk_export.py"


class FreekiCADEntrypointTests(unittest.TestCase):
    def test_export_script_adds_its_freekicad_root_to_sys_path(self):
        freecad_package = types.ModuleType("freecad")
        freecad_package.__path__ = []
        addon_package = types.ModuleType("freecad.FreekiCAD")
        addon_package.__path__ = []
        headless_export = types.ModuleType("freecad.FreekiCAD.HeadlessExport")
        headless_export.main = mock.Mock(return_value=29)
        original_path = list(sys.path)
        try:
            with mock.patch.dict(sys.modules, {
                "freecad": freecad_package,
                "freecad.FreekiCAD": addon_package,
                "freecad.FreekiCAD.HeadlessExport": headless_export,
            }):
                with self.assertRaises(SystemExit) as exited:
                    runpy.run_path(str(EXPORT_SCRIPT), run_name="__main__")
        finally:
            sys.path[:] = original_path

        self.assertEqual(exited.exception.code, 29)
        headless_export.main.assert_called_once_with()

    def test_shared_instance_backend_has_no_kikakuka_root_dependency(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PACKAGE_DIR.parent)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", "import FreekiCAD.instance_backend"],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_package_import_starts_instance_node_without_document_observer_api(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_freecad.addImportType = mock.Mock()
        fake_freecad.addExportType = mock.Mock()
        fake_client = types.ModuleType(f"{PACKAGE_NAME}.im_client")
        fake_client.ensure_node = mock.Mock()
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME, PACKAGE_DIR / "__init__.py",
            submodule_search_locations=[str(PACKAGE_DIR)])
        package = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
            "FreeCAD": fake_freecad,
            PACKAGE_NAME: package,
            f"{PACKAGE_NAME}.im_client": fake_client,
        }):
            spec.loader.exec_module(package)
        fake_client.ensure_node.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
