import importlib
import json
import math
import sys
import types
import unittest
from unittest import mock


class _FakeFace:
    def __init__(self, wire):
        self.Area = wire.area


class _FakeWire:
    def __init__(self, area):
        self.area = area


class _CurrentKipyCircle:
    """BoardCircle API shape used by current kicad-python/kipy."""

    def radius(self):
        return 20_000_000


class _DroppingDefinition:
    def __init__(self):
        self.items = ["pad", "3d-model"]


class _DroppingOrientationFootprint:
    """Mimics kicad-python 0.8 dropping models in its angle setter."""

    def __init__(self):
        self.definition = _DroppingDefinition()
        self.position = None
        self._orientation = None

    @property
    def orientation(self):
        return self._orientation

    @orientation.setter
    def orientation(self, value):
        self._orientation = value
        self.definition.items = ["pad"]


class _FootprintAttributes:
    def __init__(self, do_not_populate):
        self.do_not_populate = do_not_populate


class _AttributedFootprint:
    def __init__(self, do_not_populate):
        self.attributes = _FootprintAttributes(do_not_populate)


class _LibraryId:
    def __init__(self, name):
        self.name = name


class _Definition:
    def __init__(self, name):
        self.id = _LibraryId(name)


class _NamedFootprint:
    def __init__(self, library_name, value_name="ignored"):
        self.definition = _Definition(library_name)
        self.value_field = types.SimpleNamespace(
            text=types.SimpleNamespace(value=value_name))


class _Angle:
    def __init__(self, degrees):
        self.degrees = degrees


class _CustomField:
    def __init__(self, name, value):
        self.name = name
        self.text = types.SimpleNamespace(
            text=types.SimpleNamespace(value=value))


class _Vector2D:
    def __init__(self, x=0, y=0, z=0):
        self.x = x
        self.y = y
        self.z = z


class _Rotation2D:
    def __init__(self, axis=None, angle=0):
        self.axis = axis
        self.angle = float(angle)


class _Placement2D:
    def __init__(self, vector=None, rotation=None):
        self.Base = vector or _Vector2D()
        self.angle = rotation.angle if rotation else 0.0
        self.axis = rotation.axis if rotation else None

    def multiply(self, other):
        radians = math.radians(self.angle)
        x = (self.Base.x + math.cos(radians) * other.Base.x
             - math.sin(radians) * other.Base.y)
        y = (self.Base.y + math.sin(radians) * other.Base.x
             + math.cos(radians) * other.Base.y)
        return _Placement2D(
            _Vector2D(x, y, self.Base.z + other.Base.z),
            _Rotation2D(None, self.angle + other.angle))

    def inverse(self):
        radians = math.radians(-self.angle)
        x = -(math.cos(radians) * self.Base.x
              - math.sin(radians) * self.Base.y)
        y = -(math.sin(radians) * self.Base.x
              + math.cos(radians) * self.Base.y)
        return _Placement2D(
            _Vector2D(x, y, -self.Base.z),
            _Rotation2D(None, -self.angle))


class OutlineWireOrderTests(unittest.TestCase):
    def _import_linked_object(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_part = types.ModuleType("Part")
        module_name = "FreekiCAD.FreekiCAD.LinkedObject"
        self.addCleanup(sys.modules.pop, module_name, None)
        with mock.patch.dict(
            sys.modules,
            {"FreeCAD": fake_freecad, "Part": fake_part},
        ):
            sys.modules.pop(module_name, None)
            return importlib.import_module(module_name)

    def test_linked_filename_resolves_relative_to_saved_fcstd(self):
        linked_object = self._import_linked_object()
        obj = types.SimpleNamespace(
            FileName="boards/power.kicad_pcb",
            Document=types.SimpleNamespace(FileName="/project/assembly.FCStd"),
        )

        self.assertEqual(
            linked_object._resolved_linked_filename(obj),
            "/project/boards/power.kicad_pcb",
        )

    def test_linked_filename_becomes_relative_for_document_descendant(self):
        linked_object = self._import_linked_object()
        obj = types.SimpleNamespace(
            FileName="/project/boards/power.kicad_pcb",
            Document=types.SimpleNamespace(FileName="/project/assembly.FCStd"),
        )

        self.assertEqual(
            linked_object._portable_linked_filename(obj, obj.FileName),
            "boards/power.kicad_pcb",
        )

    def test_linked_filename_stays_absolute_outside_document_directory(self):
        linked_object = self._import_linked_object()
        obj = types.SimpleNamespace(
            FileName="/boards/power.kicad_pcb",
            Document=types.SimpleNamespace(FileName="/project/assembly.FCStd"),
        )

        self.assertEqual(
            linked_object._portable_linked_filename(obj, obj.FileName),
            "/boards/power.kicad_pcb",
        )

    def test_unsaved_document_does_not_make_linked_filename_relative(self):
        linked_object = self._import_linked_object()
        obj = types.SimpleNamespace(
            FileName="/project/boards/power.kicad_pcb",
            Document=types.SimpleNamespace(FileName=""),
        )

        self.assertEqual(
            linked_object._portable_linked_filename(obj, obj.FileName),
            "/project/boards/power.kicad_pcb",
        )

    def test_largest_profile_is_selected_before_hole(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_part = types.ModuleType("Part")
        fake_part.Face = _FakeFace

        module_name = "FreekiCAD.FreekiCAD.LinkedObject"
        self.addCleanup(sys.modules.pop, module_name, None)
        with mock.patch.dict(
            sys.modules,
            {"FreeCAD": fake_freecad, "Part": fake_part},
        ):
            sys.modules.pop(module_name, None)
            linked_object = importlib.import_module(module_name)

        # bulb-1ch.kicad_pcb declares its 15 x 4.35 mm cutout before its
        # radius-20 mm circular outline.
        cutout = _FakeWire(15.0 * 4.35)
        outer_circle = _FakeWire(3.141592653589793 * 20.0 * 20.0)

        order = linked_object._outline_wire_order([cutout, outer_circle])

        self.assertEqual(order, [1, 0])

    def test_board_circle_uses_radius_method_without_end_attribute(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_part = types.ModuleType("Part")
        module_name = "FreekiCAD.FreekiCAD.LinkedObject"
        self.addCleanup(sys.modules.pop, module_name, None)

        with mock.patch.dict(
            sys.modules,
            {"FreeCAD": fake_freecad, "Part": fake_part},
        ):
            sys.modules.pop(module_name, None)
            linked_object = importlib.import_module(module_name)

        self.assertEqual(
            linked_object._board_circle_radius_mm(_CurrentKipyCircle()),
            20.0,
        )

    def test_step_import_does_not_use_global_freecad_preferences(self):
        linked_object = self._import_linked_object()
        import_gui = mock.Mock()

        linked_object._insert_step_merged(
            import_gui, "/models/module.step", "temporary"
        )

        import_gui.insert.assert_called_once_with(
            name="/models/module.step",
            docName="temporary",
            merge=True,
            useLinkGroup=False,
        )

    def test_footprint_pose_preserves_3d_model_definition_items(self):
        linked_object = self._import_linked_object()
        footprint = _DroppingOrientationFootprint()

        linked_object._set_footprint_pose_preserving_definition(
            footprint, "new-position", "new-orientation"
        )

        self.assertEqual(footprint.position, "new-position")
        self.assertEqual(footprint.orientation, "new-orientation")
        self.assertEqual(footprint.definition.items, ["pad", "3d-model"])

    def test_dnp_footprints_are_identified_for_model_import_filtering(self):
        linked_object = self._import_linked_object()

        self.assertTrue(
            linked_object._footprint_is_dnp(_AttributedFootprint(True))
        )
        self.assertFalse(
            linked_object._footprint_is_dnp(_AttributedFootprint(False))
        )
        self.assertFalse(linked_object._footprint_is_dnp(object()))

    def test_component_cache_tracks_step_importer_revision(self):
        linked_object = self._import_linked_object()
        value = linked_object._component_transform_cache_value(
            {
                "is_back": False,
                "models": [{
                    "path": "/tmp/model.step",
                    "offset": (0, 0, 0),
                    "rotation": (0, 0, 0),
                    "scale": (1, 1, 1),
                }],
            },
            thickness=1.6,
        )

        self.assertEqual(
            json.loads(value)["step_importer_revision"],
            linked_object.STEP_IMPORTER_REVISION,
        )

    def test_coupler_type_prefers_library_entry_name(self):
        linked_object = self._import_linked_object()

        self.assertEqual(
            linked_object._footprint_coupler_type(
                _NamedFootprint("CouplerMoving", "renamed value")),
            "CouplerMoving",
        )
        self.assertIsNone(
            linked_object._footprint_coupler_type(
                _NamedFootprint("ordinary", "ordinary")))

    def test_coupler_rotation_matches_existing_board_geometry_convention(self):
        linked_object = self._import_linked_object()
        footprint = _NamedFootprint("CouplerMoving")
        footprint.orientation = _Angle(27)

        self.assertEqual(
            linked_object._coupler_rotation_degrees(footprint), 27)

    def test_coupler_z_reads_custom_field_in_millimetres(self):
        linked_object = self._import_linked_object()
        footprint = _NamedFootprint("CouplerMoving")
        footprint.texts_and_fields = [_CustomField("Z", "2.4 mm")]

        value = linked_object._footprint_field_value(footprint, "Z")

        self.assertEqual(linked_object._parse_coupler_z(value), 2.4)
        self.assertEqual(linked_object._parse_coupler_z("2.4"), 2.4)
        self.assertEqual(linked_object._parse_coupler_z("1 in"), 25.4)

    def test_coupler_t_reads_custom_field_in_degrees(self):
        linked_object = self._import_linked_object()
        footprint = _NamedFootprint("CouplerMoving")
        footprint.texts_and_fields = [_CustomField("T", "-12.5 deg")]

        value = linked_object._footprint_field_value(footprint, "T")

        self.assertEqual(linked_object._parse_coupler_t(value), -12.5)

    def test_stored_coupler_poses_are_loaded_from_json(self):
        linked_object = self._import_linked_object()
        obj = types.SimpleNamespace(CouplerPoses=json.dumps([
            {"ref": "mcu", "type": "CouplerFixed", "z": 2.4},
            {"ref": "other", "type": "CouplerMoving", "z": 0},
        ]))

        poses = linked_object.LinkedObject._coupler_poses(
            obj, linked_object.COUPLER_FIXED)

        self.assertEqual(poses, [
            {"ref": "mcu", "type": "CouplerFixed", "z": 2.4},
        ])

    def test_coupler_snap_makes_planes_coincide_face_to_face(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        moving = types.SimpleNamespace(
            Label="moving",
            Placement=_Placement2D(
                _Vector2D(100, -20, 3), _Rotation2D(None, 25)))
        fixed = types.SimpleNamespace(
            Label="fixed",
            Placement=_Placement2D(
                _Vector2D(10, 20, 3), _Rotation2D(None, 30)))
        moving_pose = {
            "ref": "J1", "type": "CouplerMoving",
            "x": -2, "y": 7, "board_z": 1.6,
            "z": 2.4, "rotation": -40, "is_back": True,
        }
        fixed_pose = {
            "ref": "J1", "type": "CouplerFixed",
            "x": 4, "y": 5, "board_z": 1.2,
            "z": 2.4, "rotation": 15,
        }

        self.assertTrue(proxy._snap_moving_object(
            moving, moving_pose, fixed, fixed_pose))

        moving_world = moving.Placement.multiply(
            proxy._coupler_placement(moving_pose))
        fixed_world = fixed.Placement.multiply(
            proxy._coupler_placement(fixed_pose))
        self.assertAlmostEqual(moving_world.Base.x, fixed_world.Base.x)
        self.assertAlmostEqual(moving_world.Base.y, fixed_world.Base.y)
        self.assertAlmostEqual(moving_world.Base.z, fixed_world.Base.z)
        self.assertAlmostEqual(
            (moving_world.angle - fixed_world.angle) % 360, 180)

    def test_coupler_chain_snaps_parent_before_child(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy.Type = "LinkedObject"
        proxy._snap_moving_object = mock.Mock(return_value=True)

        root = types.SimpleNamespace(
            Name="Root", Label="root", Proxy=proxy, SnapToCoupler=True,
            CouplerPoses=json.dumps([
                {"ref": "root-parent", "type": "CouplerFixed"},
            ]))
        parent = types.SimpleNamespace(
            Name="Parent", Label="parent", Proxy=proxy, SnapToCoupler=True,
            CouplerPoses=json.dumps([
                {"ref": "root-parent", "type": "CouplerMoving"},
                {"ref": "parent-child", "type": "CouplerFixed"},
            ]))
        child = types.SimpleNamespace(
            Name="Child", Label="child", Proxy=proxy, SnapToCoupler=True,
            CouplerPoses=json.dumps([
                {"ref": "parent-child", "type": "CouplerMoving"},
            ]))
        document = types.SimpleNamespace(Objects=[child, parent, root])
        for board in document.Objects:
            board.Document = document

        proxy._snap_couplers_after_reload(root)

        self.assertEqual(
            [call.args[0].Name
             for call in proxy._snap_moving_object.call_args_list],
            ["Parent", "Child"],
        )

    def test_coupler_dependency_cycle_is_not_applied(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy.Type = "LinkedObject"
        proxy._snap_moving_object = mock.Mock(return_value=True)

        board_a = types.SimpleNamespace(
            Name="BoardA", Label="board A", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([
                {"ref": "a", "type": "CouplerFixed"},
                {"ref": "b", "type": "CouplerMoving"},
            ]))
        board_b = types.SimpleNamespace(
            Name="BoardB", Label="board B", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([
                {"ref": "b", "type": "CouplerFixed"},
                {"ref": "a", "type": "CouplerMoving"},
            ]))
        document = types.SimpleNamespace(Objects=[board_a, board_b])
        board_a.Document = document
        board_b.Document = document

        proxy._snap_couplers_after_reload(board_a)

        proxy._snap_moving_object.assert_not_called()
        warning = linked_object.FreeCAD.Console.PrintWarning.call_args.args[0]
        self.assertIn("dependency cycle", warning)
        self.assertIn("board A", warning)
        self.assertIn("board B", warning)

    def test_coupler_mating_rotates_around_local_y(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D

        mating = linked_object.LinkedObject._coupler_mating_placement()

        self.assertEqual(
            (mating.axis.x, mating.axis.y, mating.axis.z), (0, 1, 0))
        self.assertEqual(mating.angle, 180)

    def test_coupler_t_uses_positive_footprint_x_direction(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D

        placement = linked_object.LinkedObject._coupler_placement({
            "x": 0, "y": 0, "rotation": 30, "tilt": 10,
        })

        self.assertEqual(placement.angle, 40)

    def test_back_coupler_flips_the_footprint_frame(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D

        placement = linked_object.LinkedObject._coupler_placement({
            "x": 0, "y": 0, "rotation": 30, "tilt": 10,
            "is_back": True,
        })

        self.assertEqual(placement.angle, 220)

    def test_couplers_are_created_as_hidden_group_children(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        linked_object.Part.makePolygon = lambda points: ("polygon", points)
        linked_object.Part.Face = lambda wire: ("face", wire)
        linked_object.Part.makeLine = lambda start, end: ("line", start, end)
        linked_object.Part.makeCompound = lambda shapes: ("compound", shapes)

        class Marker:
            def __init__(self, name):
                self.Name = name
                self.Label = name
                self.ViewObject = types.SimpleNamespace()

            def addProperty(self, _property_type, name, _group, _description):
                setattr(self, name, None)

            def setPropertyStatus(self, _name, _status):
                pass

        class Document:
            def addObject(self, _type_name, name):
                return Marker(name)

        children = []
        obj = types.SimpleNamespace(
            Name="Board", Document=Document(), addObject=children.append)
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)

        proxy._build_coupler_children(obj, [{
            "ref": "mcu", "type": "CouplerFixed",
            "x": 10, "y": 20, "board_z": 1.6,
            "z": 4.5, "rotation": 30, "tilt": 10,
        }])

        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].Label, "CouplerFixed mcu")
        self.assertEqual(children[0].Z, 4.5)
        self.assertEqual(children[0].Placement.Base.z, 6.1)
        self.assertFalse(children[0].ViewObject.Visibility)
        self.assertEqual(children[0].Shape[0], "face")
        polygon_points = children[0].Shape[1][1]
        self.assertEqual(polygon_points[0].y, 1)
        self.assertEqual(polygon_points[2].y, 0)


if __name__ == "__main__":
    unittest.main()
