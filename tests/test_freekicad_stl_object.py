import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STL_OBJECT_PATH = os.path.join(
    REPOSITORY_ROOT, "FreekiCAD", "freecad", "FreekiCAD", "StlObject.py")


class QuantityStub:
    def __init__(self, millimetres):
        self.millimetres = millimetres

    def getValueAs(self, unit):
        if unit != "mm":
            raise AssertionError(unit)
        return types.SimpleNamespace(Value=self.millimetres)


def load_stl_object_module(fake_freecad):
    step_module = types.ModuleType("FreekiCAD.freecad.FreekiCAD.StepObject")

    def resolved_filename(obj):
        path = obj.FileName
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(
            os.path.dirname(obj.Document.FileName), path))

    step_module._resolved_filename = resolved_filename
    spec = importlib.util.spec_from_file_location(
        "FreekiCAD.freecad.FreekiCAD.StlObject", STL_OBJECT_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
            "FreeCAD": fake_freecad,
            "FreekiCAD.freecad.FreekiCAD.StepObject": step_module}):
        spec.loader.exec_module(module)
    return module


class StlObjectTests(unittest.TestCase):
    def setUp(self):
        self.fake_freecad = types.ModuleType("FreeCAD")
        self.fake_freecad.Console = types.SimpleNamespace(
            PrintWarning=mock.Mock(), PrintMessage=mock.Mock())
        self.module = load_stl_object_module(self.fake_freecad)

    def test_reload_replaces_mesh_with_requested_unit_scale(self):
        with tempfile.NamedTemporaryFile(suffix=".stl") as stream:
            mesh = object()
            loader = types.ModuleType(
                "FreekiCAD.freecad.FreekiCAD.StlLoader")
            loader._load_stl_mesh = mock.Mock(return_value=mesh)
            placement = mock.Mock()
            placement.copy.return_value = "saved placement"

            class MeshObject:
                FileName = stream.name
                FileMtime = ""
                LengthPerUnit = QuantityStub(25.4)
                Document = types.SimpleNamespace(FileName="")
                Label = "Printed case"
                Placement = placement

                @property
                def Mesh(self):
                    return self._mesh

                @Mesh.setter
                def Mesh(self, value):
                    self._mesh = value
                    self.Placement = "identity placement"

            obj = MeshObject()
            proxy = self.module.StlObject.__new__(self.module.StlObject)
            proxy._reloading = False

            with mock.patch.dict(sys.modules, {
                    "FreekiCAD.freecad.FreekiCAD.StlLoader": loader}):
                loaded = proxy.reload(obj, force=True)

            self.assertTrue(loaded)
            self.assertIs(obj.Mesh, mesh)
            self.assertEqual(obj.Placement, "saved placement")
            placement.copy.assert_called_once_with()
            loader._load_stl_mesh.assert_called_once_with(stream.name, 25.4)
            self.assertEqual(obj.FileMtime, str(os.path.getmtime(stream.name)))

    def test_length_per_unit_change_invalidates_loaded_mesh(self):
        obj = types.SimpleNamespace(
            FileName="case.stl",
            FileMtime="123",
            LengthPerUnit=QuantityStub(0.001),
            Document=types.SimpleNamespace(
                Restoring=False, FileName="/project/assembly.FCStd"),
        )
        proxy = self.module.StlObject.__new__(self.module.StlObject)
        proxy._reloading = False
        proxy._last_filename = "/project/case.stl"

        proxy.onChanged(obj, "LengthPerUnit")

        self.assertEqual(obj.FileMtime, "")

    def test_create_object_uses_native_mesh_feature(self):
        class HeadlessObject:
            def __init__(self):
                self.ViewObject = None
                self.Name = "StlObject"
                self.Label = "StlObject"
                self.properties = []

            def addProperty(self, *args):
                self.properties.append(args)

            def setPropertyStatus(self, *args):
                pass

        obj = HeadlessObject()
        document = types.SimpleNamespace(
            addObject=mock.Mock(return_value=obj), recompute=mock.Mock())
        self.module.FreeCAD.GuiUp = False

        result = self.module.create_stl_object(document=document)

        self.assertIs(result, obj)
        document.addObject.assert_called_once_with(
            "Mesh::FeaturePython", "StlObject")
        self.assertEqual(result.LengthPerUnit, "1 mm")
        self.assertIn(
            ("App::PropertyLength", "LengthPerUnit", "LinkedFile",
             "Physical length represented by one STL coordinate unit"),
            result.properties,
        )


if __name__ == "__main__":
    unittest.main()
