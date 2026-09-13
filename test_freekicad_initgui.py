import importlib
import sys
import types
import unittest
from unittest import mock


class ReloadAllObjectsCommandTests(unittest.TestCase):
    def test_reload_all_forces_reload_even_when_file_mtime_is_unchanged(self):
        linked = types.SimpleNamespace(
            FileName="/boards/main.kicad_pcb",
            Proxy=types.SimpleNamespace(reload=mock.Mock()),
        )
        document = types.SimpleNamespace(Objects=[linked])

        fake_freecad = types.ModuleType("FreeCAD")
        fake_freecad.ActiveDocument = document
        fake_freecad_gui = types.ModuleType("FreeCADGui")
        fake_freecad_gui.Workbench = object
        fake_freecad_gui.addWorkbench = mock.Mock()
        fake_freecad_gui.addCommand = mock.Mock()

        module_name = "FreekiCAD.InitGui"
        self.addCleanup(sys.modules.pop, module_name, None)
        with mock.patch.dict(
            sys.modules,
            {"FreeCAD": fake_freecad, "FreeCADGui": fake_freecad_gui},
        ):
            sys.modules.pop(module_name, None)
            init_gui = importlib.import_module(module_name)
            init_gui.ReloadAllObjectsCommand().Activated()

        linked.Proxy.reload.assert_called_once_with(linked, force=True)
        fake_freecad_gui.addCommand.assert_any_call(
            "CreateStepObject", mock.ANY)
        fake_freecad_gui.addCommand.assert_any_call(
            "CreatePcbObject", mock.ANY)


if __name__ == "__main__":
    unittest.main()
