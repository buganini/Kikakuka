"""Legacy FCStd proxy imports after FreekiCAD adopted the freecad namespace."""

import importlib
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ADDON_PACKAGE = (Path(__file__).resolve().parents[1] / "FreekiCAD" /
                 "freecad" / "FreekiCAD")


class LegacyRestoreTests(unittest.TestCase):
    def test_saved_linked_object_module_resolves_to_canonical_classes(self):
        freecad = types.ModuleType("freecad")
        freecad.__path__ = [str(ADDON_PACKAGE.parent)]
        freecad_app = types.ModuleType("FreeCAD")
        im_client = types.ModuleType("freecad.FreekiCAD.im_client")
        im_client.ensure_node = mock.Mock()
        implementation = types.ModuleType("freecad.FreekiCAD.PcbObject")
        implementation.PcbObject = type(
            "PcbObject", (), {"__module__": implementation.__name__})
        implementation.PcbObjectViewProvider = type(
            "PcbObjectViewProvider", (), {"__module__": implementation.__name__})
        implementation.BendLine = type(
            "BendLine", (), {"__module__": implementation.__name__})
        implementation.create_pcb_object = lambda: None

        name = "freecad.FreekiCAD"
        spec = importlib.util.spec_from_file_location(
            name, ADDON_PACKAGE / "__init__.py",
            submodule_search_locations=[str(ADDON_PACKAGE)])
        package = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
                "freecad": freecad,
                "FreeCAD": freecad_app,
                name: package,
                name + ".im_client": im_client,
                name + ".PcbObject": implementation,
        }):
            spec.loader.exec_module(package)
            package.PcbObject = implementation
            self.assertIs(sys.modules["FreekiCAD"], package)
            self.assertEqual(importlib.util.find_spec("FreekiCAD.LinkedObject").origin,
                             str(ADDON_PACKAGE / "LinkedObject.py"))
            legacy = importlib.import_module("FreekiCAD.LinkedObject")
            self.assertIs(legacy.LinkedObject, implementation.PcbObject)
            self.assertIs(legacy.LinkedObjectViewProvider,
                          implementation.PcbObjectViewProvider)
            self.assertIs(legacy.BendLine, implementation.BendLine)
            self.assertEqual(legacy.LinkedObject.__module__,
                             "freecad.FreekiCAD.PcbObject")
            self.assertNotIn("FreekiCAD.PcbObject", sys.modules)


if __name__ == "__main__":
    unittest.main()
