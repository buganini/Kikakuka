import importlib.util
import json
import math
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSEMBLY_PATH = os.path.join(
    REPOSITORY_ROOT, "FreekiCAD", "freecad", "FreekiCAD",
    "Assembly.py"
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

    def multiply(self, other):
        # These hierarchy tests use translation-only placements.  Rotation is
        # still carried so manifest serialization exercises the real fields.
        return Placement(
            Vector(
                self.Base.x + other.Base.x,
                self.Base.y + other.Base.y,
                self.Base.z + other.Base.z,
            ),
            self.Rotation,
        )

    def inverse(self):
        return Placement(
            Vector(-self.Base.x, -self.Base.y, -self.Base.z),
            self.Rotation,
        )


class CoinValue:
    def __init__(self, value):
        self.value = tuple(value)

    def getValue(self):
        return self.value


class CameraField:
    def __init__(self, value):
        self.value = value

    def getValue(self):
        if isinstance(self.value, (tuple, list)):
            return CoinValue(self.value)
        return self.value

    def setValue(self, *value):
        self.value = value if len(value) != 1 else value[0]


class Camera:
    def __init__(self):
        self.position = CameraField((12.0, 23.0, 34.0))
        self.orientation = CameraField((0.1, 0.2, 0.3, 0.9))
        self.nearDistance = CameraField(1.5)
        self.farDistance = CameraField(1500.0)
        self.aspectRatio = CameraField(1.6)
        self.focalDistance = CameraField(250.0)
        self.height = CameraField(90.0)
        self.heightAngle = CameraField(0.75)


class View:
    def __init__(self, camera_type="Orthographic"):
        self.camera_type = camera_type
        self.camera = Camera()
        self.redraw_count = 0

    def getCameraType(self):
        return self.camera_type

    def setCameraType(self, value):
        self.camera_type = value

    def getCameraNode(self):
        return self.camera

    def redraw(self):
        self.redraw_count += 1


def placement(x=0, y=0, z=0, angle=0):
    return Placement(Vector(x, y, z), Rotation(Vector(0, 0, 1), angle))


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
        self.TypeId = "Part::FeaturePython"
        self.Placement = Placement(Vector(1, 2, 3), Rotation(Vector(0, 1, 0), 45))
        self.AutoReload = False
        self.SnapToCoupler = True
        self.EnableBending = False
        self.ImportOuterCopper = True
        self.ImportInnerCopper = True
        self.ImportSolderMask = True
        self.ImportSilkscreen = True
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
        self.TypeId = "Part::FeaturePython"
        self.Placement = Placement(Vector(7, 8, 9), Rotation(Vector(1, 0, 0), 30))
        self.AutoReload = False


class StlObjectStub(StepObjectStub):
    def __init__(self, document, filename=""):
        super().__init__(document, filename)
        self.Name = "StlObject"
        self.Label = "Printed enclosure"
        self.Proxy = types.SimpleNamespace(Type="StlObject")
        self.TypeId = "Mesh::FeaturePython"
        self.LengthPerUnit = types.SimpleNamespace(
            getValueAs=lambda unit: types.SimpleNamespace(Value=25.4))


class LinkStub:
    TypeId = "App::Link"

    def __init__(self, target, global_placement, name="Link"):
        self.LinkedObject = target
        # App::Link may expose properties from its target.  Export must still
        # classify the selected object as an instance before inspecting Proxy.
        self.Proxy = getattr(target, "Proxy", None)
        self.Name = name
        self.Label = name
        self.Placement = global_placement
        self._global_placement = global_placement

    def getGlobalPlacement(self):
        return self._global_placement


class ContainerStub:
    def __init__(self, children, global_placement=None,
                 type_id="Assembly::AssemblyObject", name="Assembly"):
        self.Group = list(children)
        self.TypeId = type_id
        self.Name = name
        self.Label = name
        self.Placement = global_placement or placement()
        self._global_placement = self.Placement

    def getGlobalPlacement(self):
        return self._global_placement


def load_assembly_module(fake_freecad):
    module_name = "FreekiCAD.freecad.FreekiCAD.Assembly"
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
            self.assertTrue(saved["settings"]["ImportSilkscreen"])
            self.assertEqual(saved["placement"]["base"], [1.0, 2.0, 3.0])
            self.assertAlmostEqual(
                saved["placement"]["rotation"]["angle_degrees"], 45.0
            )

    def test_export_saves_active_viewport_camera(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            document = Document("Assembly")
            obj = PcbObjectStub(
                document, os.path.join(root, "main.kicad_pcb"))
            view = View("Orthographic")
            gui_document = types.SimpleNamespace(activeView=lambda: view)
            freecad_gui = types.ModuleType("FreeCADGui")
            freecad_gui.activeDocument = lambda: gui_document

            with mock.patch.object(
                    self.fake_freecad, "GuiUp", True, create=True), \
                    mock.patch.dict(sys.modules, {"FreeCADGui": freecad_gui}):
                self.assembly.export([obj], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                viewport = json.load(stream)["viewport"]
            self.assertEqual(viewport, {
                "camera_type": "Orthographic",
                "position": [12.0, 23.0, 34.0],
                "orientation": [0.1, 0.2, 0.3, 0.9],
                "near_distance": 1.5,
                "far_distance": 1500.0,
                "aspect_ratio": 1.6,
                "focal_distance": 250.0,
                "height": 90.0,
            })

    def test_restore_viewport_camera(self):
        document = Document("Target")
        view = View()
        gui_document = types.SimpleNamespace(activeView=lambda: view)
        freecad_gui = types.ModuleType("FreeCADGui")
        freecad_gui.getDocument = lambda name: gui_document
        viewport = {
            "camera_type": "Perspective",
            "position": [1, 2, 3],
            "orientation": [0, 0, 0, 1],
            "near_distance": 2,
            "far_distance": 2000,
            "aspect_ratio": 1.25,
            "focal_distance": 300,
            "height_angle": 0.5,
        }

        with mock.patch.object(
                self.fake_freecad, "GuiUp", True, create=True), \
                mock.patch.dict(sys.modules, {"FreeCADGui": freecad_gui}):
            restored = self.assembly._restore_viewport(document, viewport)

        self.assertTrue(restored)
        self.assertEqual(view.camera_type, "Perspective")
        self.assertEqual(view.camera.position.value, (1.0, 2.0, 3.0))
        self.assertEqual(view.camera.orientation.value, (0.0, 0.0, 0.0, 1.0))
        self.assertEqual(view.camera.nearDistance.value, 2.0)
        self.assertEqual(view.camera.farDistance.value, 2000.0)
        self.assertEqual(view.camera.aspectRatio.value, 1.25)
        self.assertEqual(view.camera.focalDistance.value, 300.0)
        self.assertEqual(view.camera.heightAngle.value, 0.5)
        self.assertEqual(view.redraw_count, 1)

    def test_insert_restores_viewport_after_recompute(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            viewport = {
                "camera_type": "Orthographic",
                "position": [4, 5, 6],
                "orientation": [0, 0, 0, 1],
                "near_distance": 1,
                "far_distance": 1000,
                "aspect_ratio": 1.5,
                "focal_distance": 100,
                "height": 75,
            }
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump({
                    "objects": [{
                        "type": "StepObject",
                        "file": "enclosure.step",
                    }],
                    "viewport": viewport,
                }, stream)

            document = Document("Target")
            self.documents[document.Name] = document
            created = StepObjectStub(document)
            step_module = types.ModuleType(
                "FreekiCAD.freecad.FreekiCAD.StepObject")
            step_module.create_step_object = mock.Mock(return_value=created)
            view = View()
            gui_document = types.SimpleNamespace(activeView=lambda: view)
            freecad_gui = types.ModuleType("FreeCADGui")
            freecad_gui.getDocument = lambda name: gui_document

            with mock.patch.object(
                    self.fake_freecad, "GuiUp", True, create=True), \
                    mock.patch.dict(sys.modules, {
                        "FreeCADGui": freecad_gui,
                        "FreekiCAD.freecad.FreekiCAD.StepObject": step_module,
                    }):
                self.assembly.insert(manifest_path, document.Name)

            self.assertEqual(document.recompute_count, 1)
            self.assertEqual(view.camera.position.value, (4.0, 5.0, 6.0))
            self.assertEqual(view.camera.height.value, 75.0)
            self.assertEqual(view.redraw_count, 1)

    def test_invalid_viewport_property_is_skipped(self):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".kkkk_asm") as stream:
            json.dump({
                "objects": [],
                "viewport": {
                    "camera_type": "Orthographic",
                    "position": [0, 0, 1],
                    "orientation": [0, 0, 0, 0],
                    "near_distance": 1,
                    "far_distance": 10,
                    "aspect_ratio": 1,
                    "focal_distance": 5,
                    "height": 2,
                },
            }, stream)
            stream.flush()
            viewport = self.assembly._read(stream.name)["viewport"]

        document = Document("Target")
        view = View()
        original_orientation = view.camera.orientation.value
        gui_document = types.SimpleNamespace(activeView=lambda: view)
        freecad_gui = types.ModuleType("FreeCADGui")
        freecad_gui.getDocument = lambda name: gui_document

        with mock.patch.object(
                self.fake_freecad, "GuiUp", True, create=True), \
                mock.patch.dict(sys.modules, {"FreeCADGui": freecad_gui}):
            restored = self.assembly._restore_viewport(document, viewport)

        self.assertTrue(restored)
        self.assertEqual(view.camera.position.value, (0.0, 0.0, 1.0))
        self.assertEqual(view.camera.orientation.value, original_orientation)
        self.assertEqual(view.camera.focalDistance.value, 5.0)
        self.assertEqual(view.camera.height.value, 2.0)
        self.assertEqual(view.redraw_count, 1)

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

    def test_export_saves_stl_length_per_unit_mm_and_placement(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "assembly", "main.kkkk_asm")
            os.makedirs(os.path.dirname(manifest_path))
            document = Document("Assembly")
            stl = StlObjectStub(
                document, os.path.join(root, "models", "enclosure.stl"))

            self.assembly.export([stl], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                saved = json.load(stream)["objects"][0]
            self.assertEqual(saved["type"], "StlObject")
            self.assertEqual(saved["settings"], {
                "AutoReload": False,
                "LengthPerUnitMM": 25.4,
            })
            self.assertEqual(saved["placement"]["base"], [7.0, 8.0, 9.0])

    def test_export_link_uses_instance_global_placement(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            document = Document("Assembly")
            step = StepObjectStub(
                document, os.path.join(root, "models", "enclosure.step"))
            link = LinkStub(step, placement(40, 50, 60, 25))

            self.assembly.export([link], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                saved = json.load(stream)["objects"][0]
            self.assertEqual(saved["type"], "StepObject")
            self.assertEqual(saved["placement"]["base"], [40.0, 50.0, 60.0])
            self.assertAlmostEqual(
                saved["placement"]["rotation"]["angle_degrees"], 25.0)

    def test_exporting_source_ignores_its_other_link_instances(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            document = Document("Assembly")
            step = StepObjectStub(
                document, os.path.join(root, "models", "enclosure.step"))
            step.InList = [LinkStub(step, placement(40, 50, 60))]

            self.assembly.export([step], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                saved = json.load(stream)["objects"][0]
            self.assertEqual(saved["placement"]["base"], [7.0, 8.0, 9.0])

    def test_export_assembly_keeps_multiple_links_to_same_source(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            document = Document("Assembly")
            step = StepObjectStub(
                document, os.path.join(root, "models", "fastener.step"))
            assembly = ContainerStub([
                LinkStub(step, placement(10, 0, 0), "Fastener001"),
                LinkStub(step, placement(20, 0, 0), "Fastener002"),
            ])

            self.assembly.export([assembly], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                objects = json.load(stream)["objects"]
            self.assertEqual(len(objects), 2)
            self.assertEqual(
                [item["placement"]["base"] for item in objects],
                [[10.0, 0.0, 0.0], [20.0, 0.0, 0.0]],
            )
            self.assertEqual(objects[0]["file"], objects[1]["file"])

    def test_export_nested_assembly_rebases_leaf_global_placement(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            document = Document("Assembly")
            step = StepObjectStub(
                document, os.path.join(root, "models", "nested.step"))
            # In its source hierarchy the subassembly begins at X=10 and its
            # leaf instance is at X=15.  Linking that subassembly at X=100
            # must therefore place the leaf at X=105.
            leaf = LinkStub(step, placement(15, 0, 0), "NestedPart")
            subassembly = ContainerStub(
                [leaf], global_placement=placement(10, 0, 0),
                name="Subassembly")
            outer_link = LinkStub(
                subassembly, placement(100, 0, 0), "SubassemblyLink")

            self.assembly.export([outer_link], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                saved = json.load(stream)["objects"][0]
            self.assertEqual(saved["placement"]["base"], [105.0, 0.0, 0.0])

    def test_export_linked_pcb_saves_pose_and_disables_coupler_snap(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            document = Document("Assembly")
            pcb = PcbObjectStub(
                document, os.path.join(root, "boards", "main.kicad_pcb"))
            pcb.CouplerPoses = json.dumps(
                [{"type": "CouplerMoving", "ref": "J1"}])
            link = LinkStub(pcb, placement(70, 80, 90, 15))

            self.assembly.export([link], manifest_path)

            with open(manifest_path, encoding="utf-8") as stream:
                saved = json.load(stream)["objects"][0]
            self.assertFalse(saved["settings"]["SnapToCoupler"])
            self.assertEqual(saved["placement"]["base"], [70.0, 80.0, 90.0])
            self.assertTrue(pcb.SnapToCoupler)

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
            pcb_module = types.ModuleType(
                "FreekiCAD.freecad.FreekiCAD.PcbObject")
            pcb_module.create_pcb_object = mock.Mock(return_value=created)

            with mock.patch.dict(
                    sys.modules,
                    {"FreekiCAD.freecad.FreekiCAD.PcbObject": pcb_module}):
                result = self.assembly.insert(manifest_path, document.Name)

            self.assertEqual(result, [created])
            pcb_module.create_pcb_object.assert_called_once_with(
                document=document, recompute=False
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
            step_module = types.ModuleType(
                "FreekiCAD.freecad.FreekiCAD.StepObject")
            step_module.create_step_object = mock.Mock(return_value=created)

            with mock.patch.dict(
                    sys.modules,
                    {"FreekiCAD.freecad.FreekiCAD.StepObject": step_module}):
                result = self.assembly.insert(manifest_path, document.Name)

            self.assertEqual(result, [created])
            step_module.create_step_object.assert_called_once_with(
                document=document, recompute=False
            )
            self.assertEqual(created.FileName, os.path.join(root, "enclosure.step"))
            self.assertFalse(created.AutoReload)
            self.assertEqual(created.Placement.Base.x, 10)
            self.assertAlmostEqual(created.Placement.Rotation.Angle, math.pi / 3)

    def test_insert_creates_stl_object_and_restores_length_per_unit(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump({"objects": [{
                    "type": "StlObject",
                    "file": "enclosure.stl",
                    "settings": {
                        "AutoReload": False,
                        "LengthPerUnitMM": 0.001,
                    },
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
            created = StlObjectStub(document)
            stl_module = types.ModuleType(
                "FreekiCAD.freecad.FreekiCAD.StlObject")
            stl_module.create_stl_object = mock.Mock(return_value=created)

            with mock.patch.dict(sys.modules, {
                    "FreekiCAD.freecad.FreekiCAD.StlObject": stl_module}):
                result = self.assembly.insert(manifest_path, document.Name)

            self.assertEqual(result, [created])
            stl_module.create_stl_object.assert_called_once_with(
                document=document, recompute=False)
            self.assertEqual(created.FileName, os.path.join(root, "enclosure.stl"))
            self.assertFalse(created.AutoReload)
            self.assertEqual(created.LengthPerUnit, 0.001)
            self.assertEqual(created.Placement.Base.x, 10)

    def test_insert_rejects_obsolete_stl_unit_scale(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump({"objects": [{
                    "type": "StlObject",
                    "file": "enclosure.stl",
                    "settings": {"UnitScale": 25.4},
                }]}, stream)

            document = Document("Target")
            self.documents[document.Name] = document
            stl_module = types.ModuleType(
                "FreekiCAD.freecad.FreekiCAD.StlObject")
            stl_module.create_stl_object = mock.Mock(
                return_value=StlObjectStub(document))

            with mock.patch.dict(sys.modules, {
                    "FreekiCAD.freecad.FreekiCAD.StlObject": stl_module}):
                with self.assertRaisesRegex(
                        ValueError, "obsolete UnitScale"):
                    self.assembly.insert(manifest_path, document.Name)

    def test_insert_can_defer_all_recomputation_for_headless_loading(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_path = os.path.join(root, "main.kkkk_asm")
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump({"objects": [{
                    "type": "StepObject",
                    "file": "enclosure.step",
                }]}, stream)

            document = Document("Target")
            self.documents[document.Name] = document
            created = StepObjectStub(document)
            step_module = types.ModuleType(
                "FreekiCAD.freecad.FreekiCAD.StepObject")
            step_module.create_step_object = mock.Mock(return_value=created)

            with mock.patch.dict(
                    sys.modules,
                    {"FreekiCAD.freecad.FreekiCAD.StepObject": step_module}):
                self.assembly.insert(
                    manifest_path, document.Name, recompute=False)

            step_module.create_step_object.assert_called_once_with(
                document=document, recompute=False)
            self.assertEqual(document.recompute_count, 0)

    def test_rejects_non_object_manifest(self):
        with tempfile.NamedTemporaryFile("w", suffix=".kkkk_asm") as stream:
            json.dump([], stream)
            stream.flush()
            with self.assertRaisesRegex(ValueError, "JSON object"):
                self.assembly._read(stream.name)

    def test_open_registers_manifest_path_for_instance_manager(self):
        fake_client = types.ModuleType("FreekiCAD.freecad.FreekiCAD.cadhoc_client")
        fake_client.register_document_source = mock.Mock()
        with mock.patch.object(self.assembly, "insert") as insert, \
                mock.patch.dict(sys.modules, {
                    "FreekiCAD.freecad.FreekiCAD.cadhoc_client": fake_client}):
            document = self.assembly.open("/models/assembly.kkkk_asm")
        insert.assert_called_once_with("/models/assembly.kkkk_asm", document.Name)
        fake_client.register_document_source.assert_called_once_with(
            document, "/models/assembly.kkkk_asm")

    def test_open_activates_imported_document_only_in_gui(self):
        fake_client = types.ModuleType("FreekiCAD.freecad.FreekiCAD.cadhoc_client")
        fake_client.register_document_source = mock.Mock()
        fake_client.activate_gui_document = mock.Mock()
        for gui_up in (False, True):
            with self.subTest(gui_up=gui_up), \
                    mock.patch.object(self.fake_freecad, "GuiUp", gui_up, create=True), \
                    mock.patch.object(self.assembly, "insert"), \
                    mock.patch.dict(sys.modules, {
                        "FreekiCAD.freecad.FreekiCAD.cadhoc_client": fake_client}):
                fake_client.activate_gui_document.reset_mock()
                document = self.assembly.open("/models/assembly.kkkk_asm")
                if gui_up:
                    fake_client.activate_gui_document.assert_called_once_with(document)
                else:
                    fake_client.activate_gui_document.assert_not_called()


if __name__ == "__main__":
    unittest.main()
