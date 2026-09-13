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

    def test_new_linked_object_does_not_import_copper_by_default(self):
        linked_object = self._import_linked_object()

        class FakeObject:
            def addExtension(self, _extension):
                pass

            def addProperty(self, _kind, name, _group, _description):
                setattr(self, name, None)

            def setPropertyStatus(self, _name, _status):
                pass

        obj = FakeObject()
        linked_object.LinkedObject(obj)

        self.assertFalse(hasattr(obj, "ImportCopper"))
        self.assertIs(obj.ImportOuterCopper, False)
        self.assertIs(obj.ImportInnerCopper, False)
        self.assertIs(obj.ImportSolderMask, False)

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

    def test_board_color_keeps_normalized_kipy_channels(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock())
        board_layer_module = types.ModuleType(
            "kipy.proto.board.board_types_pb2")
        board_layer_module.BoardLayer = types.SimpleNamespace(BL_F_Mask=42)
        layer = types.SimpleNamespace(
            layer=42,
            color=types.SimpleNamespace(
                red=0.761, green=0.765, blue=0.0, alpha=1.0))
        board = types.SimpleNamespace(
            get_stackup=lambda: types.SimpleNamespace(layers=[layer]))

        with mock.patch.dict(sys.modules, {
                "kipy.proto.board.board_types_pb2": board_layer_module}), \
                mock.patch.object(
                    linked_object, "_kipy_retry",
                    side_effect=lambda func: func()), \
                mock.patch.object(
                    linked_object, "_get_board_color_from_file") as fallback:
            color = linked_object._get_board_color(
                board, "samples/fpc.kicad_pcb")

        self.assertEqual(color, (0.761, 0.765, 0.0))
        fallback.assert_not_called()

    def test_board_color_normalizes_legacy_byte_channels(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock())
        board_layer_module = types.ModuleType(
            "kipy.proto.board.board_types_pb2")
        board_layer_module.BoardLayer = types.SimpleNamespace(BL_F_Mask=42)
        layer = types.SimpleNamespace(
            layer=42,
            color=types.SimpleNamespace(
                red=194, green=195, blue=0, alpha=255))
        board = types.SimpleNamespace(
            get_stackup=lambda: types.SimpleNamespace(layers=[layer]))

        with mock.patch.dict(sys.modules, {
                "kipy.proto.board.board_types_pb2": board_layer_module}), \
                mock.patch.object(
                    linked_object, "_kipy_retry",
                    side_effect=lambda func: func()):
            color = linked_object._get_board_color(board, None)

        self.assertAlmostEqual(color[0], 194 / 255.0)
        self.assertAlmostEqual(color[1], 195 / 255.0)
        self.assertEqual(color[2], 0.0)

    def test_coupler_type_prefers_library_entry_name(self):
        linked_object = self._import_linked_object()

        self.assertEqual(
            linked_object._footprint_coupler_type(
                _NamedFootprint("CouplerMoving", "renamed value")),
            "CouplerMoving",
        )
        self.assertEqual(
            linked_object._footprint_coupler_type(
                _NamedFootprint("CouplerOrigin", "renamed value")),
            "CouplerOrigin",
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

    def test_coupler_tilt_reads_custom_field_in_degrees(self):
        linked_object = self._import_linked_object()
        footprint = _NamedFootprint("CouplerMoving")
        footprint.texts_and_fields = [_CustomField("Tilt", "-12.5 deg")]

        value = linked_object._footprint_field_value(footprint, "Tilt")

        self.assertEqual(linked_object._parse_coupler_tilt(value), -12.5)

    def test_live_coupler_pose_contains_all_positioning_properties(self):
        linked_object = self._import_linked_object()
        from kipy.proto.board.board_types_pb2 import BoardLayer

        footprint = _NamedFootprint("CouplerMoving")
        footprint.reference_field = types.SimpleNamespace(
            text=types.SimpleNamespace(value="pair"))
        footprint.position = types.SimpleNamespace(
            x=12_500_000, y=-7_250_000)
        footprint.layer = BoardLayer.BL_F_Cu
        footprint.orientation = _Angle(27)
        footprint.texts_and_fields = [
            _CustomField("Z", "2.4 mm"),
            _CustomField("Tilt", "-12.5 deg"),
        ]

        pose = linked_object._coupler_pose_from_footprint(footprint, 1.6)

        self.assertEqual(pose, {
            "ref": "pair", "type": "CouplerMoving",
            "x": 12.5, "y": 7.25, "board_z": 1.6,
            "is_back": False, "z": 2.4, "tilt": -12.5,
            "rotation": 27,
        })

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

    def test_live_coupler_poll_updates_only_persisted_list(self):
        linked_object = self._import_linked_object()
        monitored = [
            {"ref": "pair", "type": "CouplerFixed", "x": 1},
            {"ref": "origin", "type": "CouplerOrigin", "x": 2},
        ]
        live = [
            {"ref": "new", "type": "CouplerFixed", "x": 30},
            {"ref": "origin", "type": "CouplerOrigin", "x": 20},
            {"ref": "pair", "type": "CouplerFixed", "x": 10},
        ]

        selected = linked_object._select_monitored_coupler_poses(
            monitored, live)

        self.assertEqual([pose["ref"] for pose in selected], [
            "pair", "origin"])
        self.assertEqual([pose["x"] for pose in selected], [10, 20])

    def test_live_coupler_poll_rejects_partial_result(self):
        linked_object = self._import_linked_object()
        monitored = [
            {"ref": "pair", "type": "CouplerFixed"},
            {"ref": "pair", "type": "CouplerMoving"},
        ]
        live = [{"ref": "pair", "type": "CouplerFixed"}]

        self.assertIsNone(
            linked_object._select_monitored_coupler_poses(monitored, live))

    def test_changed_live_couplers_are_applied(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy._coupler_poll_in_flight = True
        proxy._coupler_monitor_generation = 7
        proxy._coupler_poll_retry_after = 0.0
        proxy._apply_live_coupler_poses = mock.Mock()
        old_pose = {
            "ref": "pair", "type": "CouplerFixed", "x": 1,
        }
        new_pose = {
            "ref": "pair", "type": "CouplerFixed", "x": 2,
        }
        obj = types.SimpleNamespace(
            Label="board", CouplerPoses=json.dumps([old_pose]))

        proxy._finish_coupler_poll(obj, 7, [new_pose], None)

        self.assertFalse(proxy._coupler_poll_in_flight)
        proxy._apply_live_coupler_poses.assert_called_once_with(
            obj, [new_pose])

    def test_stale_live_coupler_result_is_ignored_after_reload(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy._coupler_poll_in_flight = True
        proxy._coupler_monitor_generation = 8
        proxy._coupler_poll_retry_after = 0.0
        proxy._apply_live_coupler_poses = mock.Mock()
        obj = types.SimpleNamespace(CouplerPoses="[]")

        proxy._finish_coupler_poll(obj, 7, [], None)

        proxy._apply_live_coupler_poses.assert_not_called()

    def test_applying_live_coupler_updates_marker_and_repositions(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock())

        class Marker:
            Name = "Board_Coupler_pair"
            CouplerType = "CouplerFixed"
            Reference = "pair"
            Z = 0
            Tilt = 0
            Placement = None
            FreekiCAD_InitPlacement = None

            def setPropertyStatus(self, _prop, _status):
                pass

        marker = Marker()
        document = object()
        obj = types.SimpleNamespace(
            Label="board", CouplerPoses="[]", Group=[marker],
            Document=document)
        placement = object()
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy._coupler_placement = mock.Mock(return_value=placement)
        proxy._reposition_all_coupled_objects = mock.Mock()
        pose = {
            "ref": "pair", "type": "CouplerFixed", "x": 10,
            "y": 20, "z": 3, "tilt": 12, "rotation": 30,
        }

        proxy._apply_live_coupler_poses(obj, [pose])

        self.assertEqual(json.loads(obj.CouplerPoses), [pose])
        self.assertEqual(marker.Z, 3)
        self.assertEqual(marker.Tilt, 12)
        self.assertIs(marker.Placement, placement)
        self.assertIs(marker.FreekiCAD_InitPlacement, placement)
        proxy._reposition_all_coupled_objects.assert_called_once_with(document)

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

    def test_coupler_snap_uses_bent_marker_placements(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)

        moving_marker = types.SimpleNamespace(
            CouplerType="CouplerMoving", Reference="J1",
            Placement=_Placement2D(
                _Vector2D(8, 9, 4), _Rotation2D(None, 70)))
        fixed_marker = types.SimpleNamespace(
            CouplerType="CouplerFixed", Reference="J1",
            Placement=_Placement2D(
                _Vector2D(-3, 6, 5), _Rotation2D(None, -20)))
        moving = types.SimpleNamespace(
            Label="moving", Group=[moving_marker],
            Placement=_Placement2D())
        fixed = types.SimpleNamespace(
            Label="fixed", Group=[fixed_marker],
            Placement=_Placement2D(
                _Vector2D(40, 30, 2), _Rotation2D(None, 15)))

        # These serialized poses deliberately differ from the marker
        # placements, which represent their positions after bending.
        moving_pose = {
            "ref": "J1", "type": "CouplerMoving",
            "x": 100, "y": 100, "rotation": 0,
        }
        fixed_pose = {
            "ref": "J1", "type": "CouplerFixed",
            "x": -100, "y": -100, "rotation": 0,
        }

        self.assertTrue(proxy._snap_moving_object(
            moving, moving_pose, fixed, fixed_pose))

        moving_world = moving.Placement.multiply(moving_marker.Placement)
        fixed_world = fixed.Placement.multiply(fixed_marker.Placement)
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

        proxy._reposition_all_coupled_objects(document)

        self.assertEqual(
            [call.args[0].Name
             for call in proxy._snap_moving_object.call_args_list],
            ["Parent", "Child"],
        )

    def test_fixed_and_moving_pair_still_snaps_through_dependency_builder(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy.Type = "LinkedObject"
        fixed_pose = {
            "ref": "pair", "type": "CouplerFixed",
            "x": 4, "y": 5, "rotation": 15,
        }
        moving_pose = {
            "ref": "pair", "type": "CouplerMoving",
            "x": 10, "y": 20, "rotation": 30,
        }
        fixed = types.SimpleNamespace(
            Name="Fixed", Label="fixed", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([fixed_pose]),
            Placement=_Placement2D(
                _Vector2D(100, 200, 0), _Rotation2D(None, 45)),
            Group=[])
        moving = types.SimpleNamespace(
            Name="Moving", Label="moving", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([moving_pose]),
            Placement=_Placement2D(), Group=[])
        document = types.SimpleNamespace(Objects=[moving, fixed])
        moving.Document = document
        fixed.Document = document

        proxy._reposition_all_coupled_objects(document)

        moving_world = moving.Placement.multiply(
            proxy._coupler_placement(moving_pose))
        fixed_world = fixed.Placement.multiply(
            proxy._coupler_placement(fixed_pose))
        self.assertAlmostEqual(moving_world.Base.x, fixed_world.Base.x)
        self.assertAlmostEqual(moving_world.Base.y, fixed_world.Base.y)
        self.assertAlmostEqual(moving_world.Base.z, fixed_world.Base.z)
        self.assertAlmostEqual(
            (moving_world.angle - fixed_world.angle) % 360, 180)

    def test_reposition_all_respects_snap_to_coupler_option(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy.Type = "LinkedObject"
        proxy._snap_moving_object = mock.Mock(return_value=True)

        fixed = types.SimpleNamespace(
            Name="Fixed", Label="fixed", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([
                {"ref": "pair", "type": "CouplerFixed"},
            ]))
        disabled = types.SimpleNamespace(
            Name="Disabled", Label="disabled", Proxy=proxy,
            SnapToCoupler=False, CouplerPoses=json.dumps([
                {"ref": "pair", "type": "CouplerMoving"},
            ]))
        document = types.SimpleNamespace(Objects=[fixed, disabled])
        fixed.Document = document
        disabled.Document = document

        proxy._reposition_all_coupled_objects(document)

        proxy._snap_moving_object.assert_not_called()

    def test_successful_reload_repositions_entire_document_after_cleanup(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy.Type = "LinkedObject"
        proxy._reloading = True
        proxy._remove_board_children = mock.Mock(return_value=({}, {}))
        proxy._do_execute = mock.Mock()
        proxy._suspend_component_move_sync = mock.Mock()
        proxy._resume_component_move_sync = mock.Mock()
        document = types.SimpleNamespace(
            FileName="/project/assembly.FCStd")
        obj = types.SimpleNamespace(
            Name="Board", Label="board", Proxy=proxy,
            FileName="/project/board.kicad_pcb", FileMtime="123",
            Document=document)

        def assert_reload_is_finished(actual_document):
            self.assertIs(actual_document, document)
            self.assertFalse(proxy._reloading)

        proxy._reposition_all_coupled_objects = mock.Mock(
            side_effect=assert_reload_is_finished)

        proxy._handle_reload_response(obj, "/tmp/kicad.sock")

        proxy._reposition_all_coupled_objects.assert_called_once_with(document)

    def test_reposition_skips_only_board_actively_rebuilding(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        fixed_proxy = linked_object.LinkedObject.__new__(
            linked_object.LinkedObject)
        fixed_proxy.Type = "LinkedObject"
        fixed_proxy._reloading = False
        fixed_proxy._snap_moving_object = mock.Mock(return_value=True)
        fixed_proxy._snap_origin_object = mock.Mock(return_value=True)
        moving_proxy = linked_object.LinkedObject.__new__(
            linked_object.LinkedObject)
        moving_proxy.Type = "LinkedObject"
        moving_proxy._reloading = True
        moving_proxy._in_execute = True
        moving_proxy._snap_moving_object = mock.Mock(return_value=True)

        fixed = types.SimpleNamespace(
            Name="Fixed", Label="fixed", Proxy=fixed_proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([
                {"ref": "pair", "type": "CouplerFixed"},
                {"ref": "origin", "type": "CouplerOrigin"},
            ]))
        moving = types.SimpleNamespace(
            Name="Moving", Label="moving", Proxy=moving_proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([
                {"ref": "pair", "type": "CouplerMoving"},
            ]))
        document = types.SimpleNamespace(Objects=[fixed, moving])
        fixed.Document = document
        moving.Document = document

        fixed_proxy._reposition_all_coupled_objects(document)

        fixed_proxy._snap_origin_object.assert_called_once()
        fixed_proxy._snap_moving_object.assert_not_called()
        message = linked_object.FreeCAD.Console.PrintMessage.call_args_list[0]
        self.assertIn("actively rebuilding", message.args[0])
        self.assertIn("moving", message.args[0])

        # Waiting for a response leaves the previous board data intact, so it
        # must follow a changed root even though _reloading remains true.
        moving_proxy._in_execute = False
        moving_proxy._snap_origin_object = mock.Mock(return_value=True)
        moving_proxy._reposition_all_coupled_objects(document)

        moving_proxy._snap_origin_object.assert_called_once()
        moving_proxy._snap_moving_object.assert_called_once()

    def test_reload_error_releases_request_and_excludes_stale_board(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy.Type = "LinkedObject"
        proxy._reloading = True
        proxy._reposition_all_coupled_objects = mock.Mock()
        document = types.SimpleNamespace()
        board = types.SimpleNamespace(
            Label="board", Proxy=proxy, Document=document)

        proxy._handle_reload_error(board, "timed out")

        self.assertFalse(proxy._reloading)
        self.assertTrue(proxy._reload_failed)
        self.assertEqual(proxy._reload_failure_count, 1)
        self.assertGreater(proxy._reload_retry_after, 0)
        proxy._reposition_all_coupled_objects.assert_called_once_with(document)
        error = linked_object.FreeCAD.Console.PrintError.call_args.args[0]
        self.assertIn("board", error)
        self.assertIn("timed out", error)
        self.assertIn("retry in 5s", error)

    def test_automatic_reload_obeys_failure_backoff(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy._reloading = False
        proxy._reload_retry_after = 200.0
        proxy._check_file_changed = mock.Mock(return_value=True)
        obj = types.SimpleNamespace(Name="Board")

        with mock.patch.object(linked_object.time, "monotonic", return_value=100.0):
            proxy.reload(obj)

        proxy._check_file_changed.assert_not_called()

    def test_manual_reload_bypasses_failure_backoff(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy._reloading = False
        proxy._reload_retry_after = 200.0
        proxy._ensure_properties = mock.Mock()
        obj = types.SimpleNamespace(
            Name="Board", Label="board", FileName="/boards/board.kicad_pcb",
            Document=types.SimpleNamespace(FileName=""))
        send_request = mock.Mock()

        with mock.patch.object(linked_object.time, "monotonic", return_value=100.0), \
                mock.patch.dict(sys.modules, {
                    "FreekiCAD.workspace_bus": types.SimpleNamespace(
                        send_request=send_request),
                }):
            proxy.reload(obj, force=True)

        self.assertTrue(proxy._reloading)
        send_request.assert_called_once()

    def test_restored_linked_object_positions_from_saved_coupler_poses(self):
        linked_object = self._import_linked_object()
        document = types.SimpleNamespace(Restoring=False)
        proxy = types.SimpleNamespace(
            _check_file_changed=mock.Mock(return_value=False),
            _reposition_all_coupled_objects=mock.Mock(),
            reload=mock.Mock(),
        )
        obj = types.SimpleNamespace(
            Document=document, FileName="/boards/origin.kicad_pcb",
            FileMtime="unchanged", AutoReload=True, Proxy=proxy)
        vobj = types.SimpleNamespace(Object=obj)
        view_proxy = linked_object.LinkedObjectViewProvider.__new__(
            linked_object.LinkedObjectViewProvider)
        view_proxy._initial_positioning_pending = True

        view_proxy._auto_reload(vobj)
        view_proxy._auto_reload(vobj)

        proxy.reload.assert_not_called()
        proxy._reposition_all_coupled_objects.assert_called_once_with(document)
        self.assertFalse(view_proxy._initial_positioning_pending)

    def test_changed_restored_board_waits_for_reload_before_positioning(self):
        linked_object = self._import_linked_object()
        document = types.SimpleNamespace(Restoring=False)
        proxy = types.SimpleNamespace(
            _check_file_changed=mock.Mock(return_value=True),
            _reposition_all_coupled_objects=mock.Mock(),
            reload=mock.Mock(),
        )
        obj = types.SimpleNamespace(
            Document=document, FileName="/boards/origin.kicad_pcb",
            FileMtime="old", AutoReload=True, Proxy=proxy)
        view_proxy = linked_object.LinkedObjectViewProvider.__new__(
            linked_object.LinkedObjectViewProvider)
        view_proxy._initial_positioning_pending = True

        view_proxy._auto_reload(types.SimpleNamespace(Object=obj))

        proxy.reload.assert_called_once_with(obj)
        proxy._reposition_all_coupled_objects.assert_not_called()
        self.assertFalse(view_proxy._initial_positioning_pending)

    def test_restored_board_positions_when_auto_reload_is_disabled(self):
        linked_object = self._import_linked_object()
        document = types.SimpleNamespace(Restoring=False)
        proxy = types.SimpleNamespace(
            _check_file_changed=mock.Mock(),
            _reposition_all_coupled_objects=mock.Mock(),
            reload=mock.Mock(),
        )
        obj = types.SimpleNamespace(
            Document=document, FileName="/boards/origin.kicad_pcb",
            FileMtime="saved", AutoReload=False, Proxy=proxy)
        view_proxy = linked_object.LinkedObjectViewProvider.__new__(
            linked_object.LinkedObjectViewProvider)
        view_proxy._initial_positioning_pending = True

        view_proxy._auto_reload(types.SimpleNamespace(Object=obj))

        proxy._check_file_changed.assert_not_called()
        proxy.reload.assert_not_called()
        proxy._reposition_all_coupled_objects.assert_called_once_with(document)

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

        proxy._reposition_all_coupled_objects(document)

        proxy._snap_moving_object.assert_not_called()
        warning = linked_object.FreeCAD.Console.PrintWarning.call_args.args[0]
        self.assertIn("dependency cycle", warning)
        self.assertIn("board A", warning)
        self.assertIn("board B", warning)

    def test_multiple_moving_couplers_report_error_and_skip_positioning(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy.Type = "LinkedObject"
        proxy._snap_moving_object = mock.Mock(return_value=True)

        fixed = types.SimpleNamespace(
            Name="Fixed", Label="fixed", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([
                {"ref": "first", "type": "CouplerFixed"},
                {"ref": "second", "type": "CouplerFixed"},
            ]))
        ambiguous = types.SimpleNamespace(
            Name="Ambiguous", Label="ambiguous board", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([
                {"ref": "first", "type": "CouplerMoving"},
                {"ref": "second", "type": "CouplerMoving"},
            ]))
        document = types.SimpleNamespace(Objects=[fixed, ambiguous])
        fixed.Document = document
        ambiguous.Document = document

        proxy._reposition_all_coupled_objects(document)

        proxy._snap_moving_object.assert_not_called()
        error = linked_object.FreeCAD.Console.PrintError.call_args.args[0]
        self.assertIn("ambiguous board", error)
        self.assertIn("2 CouplerMoving and 0 CouplerOrigin", error)
        self.assertIn("skipping", error)

    def test_origin_coupler_snaps_to_virtual_fixed_coupler_at_world_origin(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy.Type = "LinkedObject"
        origin_pose = {
            "ref": "origin", "type": "CouplerOrigin",
            "x": 10, "y": 20, "rotation": 30,
        }
        board = types.SimpleNamespace(
            Name="Board", Label="origin board", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([origin_pose]),
            Placement=_Placement2D(
                _Vector2D(50, 60, 0), _Rotation2D(None, 45)),
            Group=[])
        document = types.SimpleNamespace(Objects=[board])
        board.Document = document

        proxy._reposition_all_coupled_objects(document)

        origin_world = board.Placement.multiply(
            proxy._coupler_placement(origin_pose))
        self.assertAlmostEqual(origin_world.Base.x, 0)
        self.assertAlmostEqual(origin_world.Base.y, 0)
        self.assertAlmostEqual(origin_world.Base.z, 0)
        self.assertAlmostEqual(origin_world.angle % 360, 180)
        message = linked_object.FreeCAD.Console.PrintMessage.call_args.args[0]
        self.assertIn("world origin", message)

    def test_origin_and_moving_couplers_report_error_and_skip_positioning(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        proxy = linked_object.LinkedObject.__new__(linked_object.LinkedObject)
        proxy.Type = "LinkedObject"
        proxy._snap_moving_object = mock.Mock(return_value=True)

        board = types.SimpleNamespace(
            Name="Board", Label="conflicting board", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([
                {"ref": "moving", "type": "CouplerMoving"},
                {"ref": "origin", "type": "CouplerOrigin"},
            ]))
        document = types.SimpleNamespace(Objects=[board])
        board.Document = document

        proxy._reposition_all_coupled_objects(document)

        proxy._snap_moving_object.assert_not_called()
        error = linked_object.FreeCAD.Console.PrintError.call_args.args[0]
        self.assertIn("1 CouplerMoving and 1 CouplerOrigin", error)
        self.assertIn("skipping", error)

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
        self.assertEqual(children[0].Tilt, 10)
        self.assertEqual(children[0].Placement.Base.z, 6.1)
        self.assertIs(
            children[0].FreekiCAD_InitPlacement, children[0].Placement)
        self.assertFalse(children[0].ViewObject.Visibility)
        self.assertEqual(children[0].Shape[0], "face")
        polygon_points = children[0].Shape[1][1]
        self.assertEqual(polygon_points[0].y, 1)
        self.assertEqual(polygon_points[2].y, 0)


if __name__ == "__main__":
    unittest.main()
