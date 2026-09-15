import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEP_OBJECT_PATH = os.path.join(
    REPOSITORY_ROOT, "FreekiCAD", "freecad", "FreekiCAD",
    "StepObject.py"
)


def load_step_object_module(fake_freecad):
    spec = importlib.util.spec_from_file_location(
        "FreekiCAD.freecad.FreekiCAD.StepObject", STEP_OBJECT_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"FreeCAD": fake_freecad}):
        spec.loader.exec_module(module)
    return module


class StepObjectTests(unittest.TestCase):
    def setUp(self):
        self.fake_freecad = types.ModuleType("FreeCAD")
        self.fake_freecad.Console = types.SimpleNamespace(
            PrintWarning=mock.Mock(), PrintMessage=mock.Mock())
        self.module = load_step_object_module(self.fake_freecad)

    def test_reload_replaces_shape_and_colors_without_changing_placement(self):
        with tempfile.NamedTemporaryFile(suffix=".step") as stream:
            shape = types.SimpleNamespace(Faces=[object(), object()])
            colors = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
            pcb_module = types.ModuleType(
                "FreekiCAD.freecad.FreekiCAD.PcbObject")
            pcb_module._load_step = mock.Mock(return_value=[(shape, colors)])
            pcb_module._write_face_colors = mock.Mock()
            placement = object()
            obj = types.SimpleNamespace(
                FileName=stream.name,
                FileMtime="",
                Document=object(),
                Label="Enclosure",
                Placement=placement,
                Shape=None,
                # FreeCAD 1.1's Gui.ViewProviderGeometryObject does not expose
                # the legacy DiffuseColor property.
                ViewObject=types.SimpleNamespace(),
            )
            proxy = self.module.StepObject.__new__(self.module.StepObject)
            proxy._reloading = False

            with mock.patch.dict(
                    sys.modules,
                    {"FreekiCAD.freecad.FreekiCAD.PcbObject": pcb_module}):
                loaded = proxy.reload(obj, force=True)

            self.assertTrue(loaded)
            self.assertIs(obj.Shape, shape)
            pcb_module._write_face_colors.assert_called_once_with(
                obj.ViewObject, colors)
            self.assertIs(obj.Placement, placement)
            self.assertEqual(obj.FileMtime, str(os.path.getmtime(stream.name)))

    def test_relative_path_is_resolved_from_freecad_document(self):
        obj = types.SimpleNamespace(
            FileName=os.path.join("models", "case.stp"),
            Document=types.SimpleNamespace(FileName="/project/assembly.FCStd"),
        )
        self.assertEqual(
            self.module._resolved_filename(obj),
            os.path.normpath("/project/models/case.stp"),
        )

    def test_execute_respects_disabled_autoreload_after_first_load(self):
        proxy = self.module.StepObject.__new__(self.module.StepObject)
        proxy._reloading = False
        proxy.reload = mock.Mock()
        proxy._check_file_changed = mock.Mock(return_value=True)
        obj = types.SimpleNamespace(
            FileName="case.step", FileMtime="123", AutoReload=False)

        proxy.execute(obj)

        proxy.reload.assert_not_called()

        obj.FileMtime = ""
        proxy.execute(obj)
        proxy.reload.assert_called_once_with(obj, force=True)


if __name__ == "__main__":
    unittest.main()
