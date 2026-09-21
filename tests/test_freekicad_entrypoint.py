"""FreeCAD process registration must not depend on workbench activation."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "FreekiCAD" / "freecad" / "FreekiCAD"
PACKAGE_NAME = "FreekiCAD.freecad.FreekiCAD"


class FreekiCADEntrypointTests(unittest.TestCase):
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
