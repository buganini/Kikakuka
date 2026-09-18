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
            step_loader_module = types.ModuleType(
                "FreekiCAD.freecad.FreekiCAD.StepLoader")
            step_loader_module._load_step = mock.Mock(
                return_value=[(shape, colors)])
            step_loader_module._write_face_colors = mock.Mock()
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
                    {"FreekiCAD.freecad.FreekiCAD.StepLoader":
                     step_loader_module,
                     "FreekiCAD.freecad.FreekiCAD.PcbObject": None}):
                loaded = proxy.reload(obj, force=True)

            self.assertTrue(loaded)
            self.assertIs(obj.Shape, shape)
            self.assertEqual(proxy._export_face_colors, colors)
            step_loader_module._write_face_colors.assert_called_once_with(
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

    def test_recompute_does_not_reload_external_file(self):
        proxy = self.module.StepObject.__new__(self.module.StepObject)
        proxy._reloading = False
        proxy.reload = mock.Mock()
        proxy._check_file_changed = mock.Mock(return_value=True)
        obj = types.SimpleNamespace(
            FileName="case.step", FileMtime="", AutoReload=True)

        proxy.execute(obj)

        proxy.reload.assert_not_called()
        proxy._check_file_changed.assert_not_called()

    def test_watcher_loads_new_object_even_when_autoreload_is_disabled(self):
        proxy = types.SimpleNamespace(
            _check_file_changed=mock.Mock(return_value=True),
            reload=mock.Mock(),
        )
        obj = types.SimpleNamespace(
            FileName="case.step", FileMtime="", AutoReload=False,
            Proxy=proxy, Document=types.SimpleNamespace(Restoring=False))
        view_object = types.SimpleNamespace(Object=obj)
        view_proxy = self.module.StepObjectViewProvider.__new__(
            self.module.StepObjectViewProvider)

        view_proxy._auto_reload(view_object)

        proxy.reload.assert_called_once_with(obj, force=True)

    def test_repeated_filename_event_does_not_clear_mtime(self):
        document = types.SimpleNamespace(
            Restoring=False, FileName="/project/assembly.FCStd")
        obj = types.SimpleNamespace(
            FileName="models/case.step", FileMtime="123",
            Label="case", Document=document)
        proxy = self.module.StepObject.__new__(self.module.StepObject)
        proxy._reloading = False
        proxy._last_filename = "/project/models/case.step"

        proxy.onChanged(obj, "FileName")

        self.assertEqual(obj.FileMtime, "123")

    def test_create_object_skips_view_provider_in_headless_mode(self):
        class HeadlessObject:
            def __init__(self):
                self.ViewObject = None
                self.Name = "StepObject"
                self.Label = "StepObject"

            def addProperty(self, *args):
                pass

            def setPropertyStatus(self, *args):
                pass

        obj = HeadlessObject()
        document = types.SimpleNamespace(
            addObject=mock.Mock(return_value=obj),
            recompute=mock.Mock(),
        )
        self.module.FreeCAD.GuiUp = False

        with mock.patch.object(
                self.module, "StepObjectViewProvider") as view_provider:
            result = self.module.create_step_object(document=document)

        self.assertIs(result, obj)
        view_provider.assert_not_called()
        document.recompute.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
