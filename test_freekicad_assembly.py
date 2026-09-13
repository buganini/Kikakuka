import importlib.util
import json
import math
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


ASSEMBLY_PATH = os.path.join(
    os.path.dirname(__file__), "FreekiCAD", "FreekiCAD", "Assembly.py"
)


class Vector:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z


class Rotation:
    def __init__(self, axis, angle_degrees):
        self.Axis = axis
        self.Angle = math.radians(angle_degrees)


class Placement:
    def __init__(self, base, rotation):
        self.Base = base
        self.Rotation = rotation


class Document:
    def __init__(self, name="Assembly", filename=""):
        self.Name = name
        self.Label = name
        self.FileName = filename
        self.recompute_count = 0

    def recompute(self):
        self.recompute_count += 1


class PcbObjectStub:
    def __init__(self, document, filename=""):
        self.Document = document
        self.Name = "Board001"
        self.Label = "控制板"
        self.FileName = filename
        self.Proxy = types.SimpleNamespace(Type="PcbObject")
        self.Placement = Placement(Vector(1, 2, 3), Rotation(Vector(0, 1, 0), 45))
        self.AutoReload = False
        self.SnapToCoupler = True
        self.EnableBending = False
        self.ImportOuterCopper = True
        self.ImportInnerCopper = True
        self.ImportSolderMask = True
        self.BuildDebugObjects = True
        self.DebugBoard = False
        self.WedgeMode = "Wireframe"
        self.CouplerPoses = "[]"


class StepObjectStub:
    def __init__(self, document, filename=""):
        self.Document = document
        self.Name = "StepObject"
        self.Label = "Enclosure"
        self.FileName = filename
        self.Proxy = types.SimpleNamespace(Type="StepObject")
        self.Placement = Placement(Vector(7, 8, 9), Rotation(Vector(1, 0, 0), 30))
        self.AutoReload = False


def load_assembly_module(fake_freecad):
    module_name = "_freekicad_assembly_test"
    spec = importlib.util.spec_from_file_location(module_name, ASSEMBLY_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"FreeCAD": fake_freecad}):
        spec.loader.exec_module(module)
    return module


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.documents = {}
        self.fake_freecad = types.ModuleType("FreeCAD")
        self.fake_freecad.Vector = Vector
        self.fake_freecad.Rotation = Rotation
        self.fake_freecad.Placement = Placement
        self.fake_freecad.getDocument = lambda name: self.documents[name]

        def new_document(name):
            document = Document(name)
            self.documents[name] = document
            return document

        self.fake_freecad.newDocument = new_document
        self.assembly = load_assembly_module(self.fake_freecad)

    def test_export_uses_relative_paths_and_saves_settings_and_placement(self):
        with tempfile.TemporaryDirectory() as root:
            assembly_dir = os.path.join(root, "assembly")
            board_dir = os.path.join(root, "boards")
            os.makedirs(assembly_dir)
            os.makedirs(board_dir)
            board_path = os.path.join(board_dir, "main.kicad_pcb")
            manifest_path = os.path.join(assembly_dir, "main.kkkk_asm")
            document = Document("Assembly", os.path.join(root, "source.FCStd"))
            obj = PcbObjectStub(document, board_path)

            self.assembly.export([obj], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                data = json.load(stream)
            self.assertEqual(set(data), {"objects"})
            saved = data["objects"][0]
            self.assertEqual(saved["type"], "PcbObject")
            self.assertEqual(saved["file"], os.path.join("..", "boards", "main.kicad_pcb"))
            self.assertNotIn("name", saved)
            self.assertNotIn("label", saved)
            self.assertEqual(saved["settings"]["WedgeMode"], "Wireframe")
            self.assertFalse(saved["settings"]["EnableBending"])
            self.assertEqual(saved["placement"]["base"], [1.0, 2.0, 3.0])
            self.assertAlmostEqual(
                saved["placement"]["rotation"]["angle_degrees"], 45.0
            )

    def test_export_omits_placement_for_couplermoving_positioned_object(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            document = Document("Assembly")
            fixed = PcbObjectStub(document, os.path.join(root, "fixed.kicad_pcb"))
            fixed.Name = "Fixed"
            fixed.CouplerPoses = json.dumps(
                [{"type": "CouplerFixed", "ref": "J1"}]
            )
            moving = PcbObjectStub(document, os.path.join(root, "moving.kicad_pcb"))
            moving.Name = "Moving"
            moving.CouplerPoses = json.dumps(
                [{"type": "CouplerMoving", "ref": "J1"}]
            )

            self.assembly.export([fixed, moving], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                objects = json.load(stream)["objects"]
            self.assertIn("placement", objects[0])
            self.assertNotIn("placement", objects[1])

    def test_export_keeps_placement_when_moving_coupler_target_is_not_exported(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            document = Document("Assembly")
            moving = PcbObjectStub(document, os.path.join(root, "moving.kicad_pcb"))
            moving.CouplerPoses = json.dumps(
                [{"type": "CouplerMoving", "ref": "J1"}]
            )

            self.assembly.export([moving], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                saved = json.load(stream)["objects"][0]
            self.assertIn("placement", saved)

    def test_export_saves_step_path_autoreload_and_placement(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "assembly", "main.kkkk_asm")
            os.makedirs(os.path.dirname(manifest_path))
            document = Document("Assembly")
            step = StepObjectStub(
                document, os.path.join(root, "models", "enclosure.step")
            )

            self.assembly.export([step], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                saved = json.load(stream)["objects"][0]
            self.assertEqual(saved["type"], "StepObject")
            self.assertEqual(
                saved["file"], os.path.join("..", "models", "enclosure.step")
            )
            self.assertEqual(saved["settings"], {"AutoReload": False})
            self.assertEqual(saved["placement"]["base"], [7.0, 8.0, 9.0])

    def test_insert_resolves_relative_path_and_restores_values(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "assembly", "main.kkkk_asm")
            os.makedirs(os.path.dirname(manifest_path))
            data = {
                "objects": [
                    {
                        "type": "LinkedObject",
                        # Old manifests may still contain these fields.  They
                        # are intentionally ignored on import.
                        "name": "Board001",
                        "label": "Main board",
                        "file": os.path.join("..", "boards", "main.kicad_pcb"),
                        "settings": {
                            "AutoReload": False,
                            "ImportOuterCopper": True,
                            "WedgeMode": "Wireframe",
                        },
                        "placement": {
                            "base": [4, 5, 6],
                            "rotation": {"axis": [0, 0, 1], "angle_degrees": 90},
                        },
                    }
                ],
            }
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump(data, stream)

            document = Document("Target")
            self.documents[document.Name] = document
            created = PcbObjectStub(document)
            pcb_module = types.ModuleType("FreekiCAD.PcbObject")
            pcb_module.create_pcb_object = mock.Mock(return_value=created)

            with mock.patch.dict(sys.modules, {"FreekiCAD.PcbObject": pcb_module}):
                result = self.assembly.insert(manifest_path, document.Name)

            self.assertEqual(result, [created])
            pcb_module.create_pcb_object.assert_called_once_with(
                document=document
            )
            self.assertEqual(
                created.FileName,
                os.path.join(root, "boards", "main.kicad_pcb"),
            )
            self.assertFalse(created.AutoReload)
            self.assertTrue(created.ImportOuterCopper)
            self.assertEqual(created.WedgeMode, "Wireframe")
            self.assertEqual(created.Placement.Base.x, 4)
            self.assertAlmostEqual(created.Placement.Rotation.Angle, math.pi / 2)
            self.assertEqual(document.recompute_count, 1)

    def test_insert_creates_step_object_and_restores_placement(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump({"objects": [{
                    "type": "StepObject",
                    "file": "enclosure.step",
                    "settings": {"AutoReload": False},
                    "placement": {
                        "base": [10, 20, 30],
                        "rotation": {
                            "axis": [0, 1, 0],
                            "angle_degrees": 60,
                        },
                    },
                }]}, stream)

            document = Document("Target")
            self.documents[document.Name] = document
            created = StepObjectStub(document)
            step_module = types.ModuleType("FreekiCAD.StepObject")
            step_module.create_step_object = mock.Mock(return_value=created)

            with mock.patch.dict(sys.modules, {"FreekiCAD.StepObject": step_module}):
                result = self.assembly.insert(manifest_path, document.Name)

            self.assertEqual(result, [created])
            step_module.create_step_object.assert_called_once_with(
                document=document
            )
            self.assertEqual(created.FileName, os.path.join(root, "enclosure.step"))
            self.assertFalse(created.AutoReload)
            self.assertEqual(created.Placement.Base.x, 10)
            self.assertAlmostEqual(created.Placement.Rotation.Angle, math.pi / 3)

    def test_rejects_non_object_manifest(self):
        with tempfile.NamedTemporaryFile("w", suffix=".kkkk_asm") as stream:
            json.dump([], stream)
            stream.flush()
            with self.assertRaisesRegex(ValueError, "JSON object"):
                self.assembly._read(stream.name)


if __name__ == "__main__":
    unittest.main()
