import importlib
import json
import math
import os
import sys
import tempfile
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

    def getYawPitchRoll(self):
        return self.angle, 0.0, 0.0


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

    def copy(self):
        return _Placement2D(
            _Vector2D(self.Base.x, self.Base.y, self.Base.z),
            _Rotation2D(self.axis, self.angle))

    def toMatrix(self):
        radians = math.radians(self.angle)
        values = (
            (math.cos(radians), -math.sin(radians), 0.0, self.Base.x),
            (math.sin(radians), math.cos(radians), 0.0, self.Base.y),
            (0.0, 0.0, 1.0, self.Base.z),
            (0.0, 0.0, 0.0, 1.0),
        )
        matrix = types.SimpleNamespace()
        for row in range(4):
            for column in range(4):
                setattr(
                    matrix, f"A{row + 1}{column + 1}",
                    values[row][column])
        return matrix


class OutlineWireOrderTests(unittest.TestCase):
    def _import_linked_object(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_part = types.ModuleType("Part")
        module_name = "FreekiCAD.freecad.FreekiCAD.PcbObject"
        self.addCleanup(sys.modules.pop, module_name, None)
        with mock.patch.dict(
            sys.modules,
            {"FreeCAD": fake_freecad, "Part": fake_part},
        ):
            sys.modules.pop(module_name, None)
            return importlib.import_module(module_name)

    def test_create_pcb_object_skips_view_provider_in_headless_mode(self):
        linked_object = self._import_linked_object()

        class HeadlessObject:
            def __init__(self):
                self.ViewObject = None
                self.Name = "PcbObject"
                self.Label = "PcbObject"

            def addExtension(self, *args):
                pass

            def addProperty(self, *args):
                pass

            def setPropertyStatus(self, *args):
                pass

        obj = HeadlessObject()
        document = types.SimpleNamespace(
            addObject=mock.Mock(return_value=obj),
            recompute=mock.Mock(),
        )
        linked_object.FreeCAD.GuiUp = False

        with mock.patch.object(
                linked_object, "PcbObjectViewProvider") as view_provider:
            result = linked_object.create_pcb_object(document=document)

        self.assertIs(result, obj)
        view_provider.assert_not_called()
        document.recompute.assert_called_once_with()

    def test_nearest_bend_piece_prefers_rigid_piece_over_strip(self):
        linked_object = self._import_linked_object()
        linked_object.Part.Vertex = lambda point: point

        class Piece:
            def __init__(self, distance):
                self.distance = distance

            def distToShape(self, point):
                return self.distance, [], []

        index, distance = linked_object._nearest_bend_piece(
            [Piece(0.1), Piece(0.4), Piece(0.8)],
            _Vector2D(), excluded={0})

        self.assertEqual(index, 1)
        self.assertAlmostEqual(distance, 0.4)

    def test_nearest_bend_piece_uses_strip_if_no_rigid_shape_is_valid(self):
        linked_object = self._import_linked_object()
        linked_object.Part.Vertex = lambda point: point

        class BrokenPiece:
            def distToShape(self, point):
                raise RuntimeError("invalid shape")

        class StripPiece:
            def distToShape(self, point):
                return 0.25, [], []

        index, distance = linked_object._nearest_bend_piece(
            [StripPiece(), BrokenPiece()], _Vector2D(), excluded={0})

        self.assertEqual(index, 0)
        self.assertAlmostEqual(distance, 0.25)

    def test_active_bend_does_not_permanently_block_component_sync(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        bend = types.SimpleNamespace(
            Proxy=types.SimpleNamespace(Type="BendLine"),
            Active=True, Angle=types.SimpleNamespace(Value=45.0))
        obj = types.SimpleNamespace(EnableBending=True, Group=[bend])

        self.assertFalse(proxy._is_component_move_blocked(obj))

        proxy._component_sync_suspended = True
        self.assertTrue(proxy._is_component_move_blocked(obj))
        proxy._component_sync_suspended = False
        proxy._bending = True
        self.assertTrue(proxy._is_component_move_blocked(obj))

    def test_bent_component_placement_maps_back_to_flat_pcb_frame(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.GuiUp = False
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        observer = linked_object._OutlineSketchObserver()

        initial = _Placement2D(
            _Vector2D(2, 3, 0), _Rotation2D(None, 10))
        bend = _Placement2D(
            _Vector2D(20, -5, 4), _Rotation2D(None, 65))
        moved_flat = _Placement2D(
            _Vector2D(7, 11, 0), _Rotation2D(None, 25))
        component = types.SimpleNamespace(
            FreekiCAD_InitPlacement=initial,
            FreekiCAD_BendPlacement=bend.multiply(initial),
            Placement=bend.multiply(moved_flat))
        parent = types.SimpleNamespace()

        flat = observer._flat_component_placement(component, parent)

        self.assertAlmostEqual(flat.Base.x, moved_flat.Base.x)
        self.assertAlmostEqual(flat.Base.y, moved_flat.Base.y)
        self.assertAlmostEqual(flat.Base.z, moved_flat.Base.z)
        self.assertAlmostEqual(flat.angle, moved_flat.angle)

    def test_component_sync_requires_component_focus(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.GuiUp = True
        component = types.SimpleNamespace(Name="Board_J1")
        parent = object()
        observer = linked_object._OutlineSketchObserver()
        selection = types.SimpleNamespace(
            Object=parent, SubElementNames=("Board_J1.Edge1",))
        fake_gui = types.ModuleType("FreeCADGui")
        fake_gui.Selection = types.SimpleNamespace(
            getSelectionEx=mock.Mock(return_value=[selection]))

        with mock.patch.dict(sys.modules, {"FreeCADGui": fake_gui}):
            self.assertTrue(observer._is_component_focused(
                component, parent))

        fake_gui.Selection.getSelectionEx.return_value = []
        with mock.patch.dict(sys.modules, {"FreeCADGui": fake_gui}):
            self.assertFalse(observer._is_component_focused(
                component, parent))

    def test_unfocused_placement_change_does_not_schedule_kicad_move(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        parent = types.SimpleNamespace(
            Name="Board", Proxy=proxy, EnableBending=False, Group=[])
        proxy.Type = "PcbObject"
        component = types.SimpleNamespace(
            Name="Board_J1", Label="Board_J1", TypeId="Part::Feature",
            Document=types.SimpleNamespace(Restoring=False), InList=[parent])
        observer = linked_object._OutlineSketchObserver()
        observer._is_component_focused = mock.Mock(return_value=False)
        observer._constrain_placement = mock.Mock()
        observer._schedule_move_component = mock.Mock()

        observer.slotChangedObject(component, "Placement")

        observer._constrain_placement.assert_not_called()
        observer._schedule_move_component.assert_not_called()

        observer._is_component_focused.return_value = True
        observer.slotChangedObject(component, "Placement")

        observer._constrain_placement.assert_called_once_with(
            component, parent)
        observer._schedule_move_component.assert_called_once_with(
            component, parent)

    def test_pcb_retains_uniform_face_colors_for_headless_export(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._export_face_colors = {}
        child = types.SimpleNamespace(
            Name="Board_Mask_F_Mask",
            Shape=types.SimpleNamespace(Faces=[object(), object()]))

        proxy._remember_export_colors(
            child, (0.1, 0.6, 0.2), transparency=40)

        self.assertEqual(proxy._export_face_colors[child.Name], [
            (0.1, 0.6, 0.2, 0.6),
        ])

    def test_body_is_full_when_no_copper_or_mask_is_imported(self):
        linked_object = self._import_linked_object()
        bounds = linked_object._outer_body_bounds(1.6, ([], []))

        self.assertEqual(bounds, (0.0, 1.6))

    def test_missing_kipy_is_reported_as_error(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(),
            PrintWarning=mock.Mock(),
            PrintError=mock.Mock(),
        )
        workspace_bus = types.ModuleType(
            "FreekiCAD.freecad.FreekiCAD.workspace_bus")
        workspace_bus.report_error = mock.Mock()
        blocked_kipy_modules = {
            name: None
            for name in sys.modules
            if name == "kipy" or name.startswith("kipy.")
        }
        blocked_kipy_modules["kipy"] = None
        blocked_kipy_modules[
            "FreekiCAD.freecad.FreekiCAD.workspace_bus"] = workspace_bus

        with mock.patch.dict(sys.modules, blocked_kipy_modules):
            result = linked_object.load_board("board.kicad_pcb", "/tmp/kicad")

        error = linked_object.FreeCAD.Console.PrintError.call_args.args[0]
        self.assertIn("Could not import kipy", error)
        self.assertIn("Install kicad-python", error)
        workspace_bus.report_error.assert_called_once()
        self.assertIsNone(result[0])

    def test_single_planar_face_unwraps_boolean_shell(self):
        linked_object = self._import_linked_object()
        face = types.SimpleNamespace(OuterWire=object())
        shell = types.SimpleNamespace(Faces=[face])

        self.assertIs(linked_object._single_planar_face(shell), face)

    def test_imported_outer_layers_set_single_body_bounds(self):
        linked_object = self._import_linked_object()
        front_copper = {
            "name": "F.Cu", "z": 1.555, "direction": 0.035,
            "body_cut_full": True,
        }
        front_mask = {
            "name": "F.Mask", "z": 1.59, "direction": 0.010,
            "body_cut_full": True,
        }
        inner_copper = {
            "name": "In1.Cu", "z": 0.700, "direction": 0.018,
            "body_cut_full": False,
        }
        back_copper = {
            "name": "B.Cu", "z": 0.045, "direction": -0.035,
            "body_cut_full": True,
        }
        back_mask = {
            "name": "B.Mask", "z": 0.010, "direction": -0.010,
            "body_cut_full": True,
        }

        bounds = linked_object._outer_body_bounds(
            1.6,
            ([front_copper, inner_copper, back_copper],
             [front_mask, back_mask]))

        self.assertEqual(bounds, (0.045, 1.555))

    def test_surface_clipping_uses_largest_dielectric_gap(self):
        linked_object = self._import_linked_object()

        result = linked_object._largest_dielectric_gap_midpoint(
            1.6,
            ([{"z": 1.555, "direction": 0.035},
              {"z": 0.045, "direction": -0.035},
              {"z": 0.700, "direction": 0.018}],
             [{"z": 1.59, "direction": 0.010},
              {"z": 0.010, "direction": -0.010}]))

        self.assertAlmostEqual(result, (0.718 + 1.555) / 2.0)

    def test_stiffener_bend_overlap_uses_full_bend_span_area(self):
        linked_object = self._import_linked_object()

        class Area:
            def common(self, span):
                return types.SimpleNamespace(Area=span.overlap_area)

        spans = [
            types.SimpleNamespace(overlap_area=0.25),
            types.SimpleNamespace(overlap_area=0.00001),
        ]
        overlaps = linked_object._stiffener_bend_overlaps(
            {"F_Stiffener_Tail reinforcement": Area()},
            [object(), object()], spans,
            area_tolerance=0.0001,
        )

        self.assertEqual(
            overlaps, [("F_Stiffener_Tail reinforcement", 0, 0.25)])
        warning = linked_object._stiffener_bend_warning(
            "F.Stiffener Polyimide 1", "Bend 1", 0.25)
        self.assertIn("F.Stiffener Polyimide 1", warning)
        self.assertIn("Bend 1", warning)
        self.assertIn("not deformed", warning)

    def test_stiffener_object_name_has_side_prefix(self):
        linked_object = self._import_linked_object()

        self.assertEqual(
            linked_object._stiffener_object_name(
                True, "Tail reinforcement", "Polyimide", 1),
            "F_Stiffener_Tail reinforcement")
        self.assertEqual(
            linked_object._stiffener_object_name(False, "", "FR4", 2),
            "B_Stiffener_FR4_2")

    def test_bend_layer_is_found_by_case_insensitive_board_name(self):
        linked_object = self._import_linked_object()
        board = types.SimpleNamespace(
            get_layer_name=mock.Mock(
                side_effect=lambda layer: {1: "Edge.Cuts", 7: "FrEeKiCaD"}[layer]))
        items = [
            types.SimpleNamespace(layer=1),
            types.SimpleNamespace(layer=7),
            types.SimpleNamespace(layer=7),
        ]

        with mock.patch.object(
            linked_object, "_kipy_retry", side_effect=lambda call: call()
        ):
            layer, name = linked_object._find_named_board_layer(
                board, items, "freekicad")

        self.assertEqual(layer, 7)
        self.assertEqual(name, "FrEeKiCaD")
        self.assertEqual(board.get_layer_name.call_count, 2)

    def test_user4_enum_without_freekicad_name_is_not_a_bend_layer(self):
        linked_object = self._import_linked_object()
        board = types.SimpleNamespace(
            get_layer_name=mock.Mock(return_value="User.4"))

        with mock.patch.object(
            linked_object, "_kipy_retry", side_effect=lambda call: call()
        ):
            layer, name = linked_object._find_named_board_layer(
                board, [types.SimpleNamespace(layer=7)], "freekicad")

        self.assertIsNone(layer)
        self.assertIsNone(name)

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

    def test_repeated_pcb_filename_event_does_not_trigger_reload(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._last_filename = "/project/boards/power.kicad_pcb"
        proxy._remove_children = mock.Mock()
        obj = types.SimpleNamespace(
            FileName="boards/power.kicad_pcb", FileMtime="123",
            Label="power",
            Document=types.SimpleNamespace(
                Restoring=False, FileName="/project/assembly.FCStd"),
        )

        proxy.onChanged(obj, "FileName")

        self.assertEqual(obj.FileMtime, "123")
        proxy._remove_children.assert_not_called()

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
        linked_object.PcbObject(obj)

        self.assertFalse(hasattr(obj, "ImportCopper"))
        self.assertIs(obj.ImportOuterCopper, False)
        self.assertIs(obj.ImportInnerCopper, False)
        self.assertIs(obj.ImportSolderMask, False)
        self.assertIs(obj.ImportSilkscreen, False)

    def test_surface_property_change_is_debounced_before_clearing_mtime(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._schedule_surface_reload = mock.Mock()
        obj = types.SimpleNamespace(
            Document=types.SimpleNamespace(Restoring=False),
            FileMtime="loaded",
        )

        proxy.onChanged(obj, "ImportSilkscreen")

        self.assertEqual(obj.FileMtime, "loaded")
        proxy._schedule_surface_reload.assert_called_once_with(
            obj, property_name="ImportSilkscreen")

    def test_duplicate_surface_property_events_do_not_reload(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._schedule_surface_reload = mock.Mock()
        properties = {
            "ImportOuterCopper": True,
            "ImportInnerCopper": False,
            "ImportSolderMask": True,
            "ImportSilkscreen": False,
        }
        proxy._rebuild_setting_values = dict(properties)
        obj = types.SimpleNamespace(
            Document=types.SimpleNamespace(Restoring=False), **properties)

        for prop in properties:
            proxy.onChanged(obj, prop)

        proxy._schedule_surface_reload.assert_not_called()

    def test_changed_surface_property_still_reloads(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._schedule_surface_reload = mock.Mock()
        proxy._rebuild_setting_values = {"ImportSilkscreen": False}
        obj = types.SimpleNamespace(
            Document=types.SimpleNamespace(Restoring=False),
            ImportSilkscreen=True)

        proxy.onChanged(obj, "ImportSilkscreen")

        proxy._schedule_surface_reload.assert_called_once_with(
            obj, property_name="ImportSilkscreen")

    def test_disabling_snap_to_coupler_does_not_reload(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._schedule_surface_reload = mock.Mock()
        proxy._schedule_rebend = mock.Mock()
        obj = types.SimpleNamespace(
            Document=types.SimpleNamespace(Restoring=False),
            SnapToCoupler=False)

        proxy.onChanged(obj, "SnapToCoupler")

        proxy._schedule_surface_reload.assert_not_called()
        proxy._schedule_rebend.assert_not_called()

    def test_surface_reload_restarts_pending_timer_and_reloads_immediately(self):
        linked_object = self._import_linked_object()

        class Signal:
            def connect(self, callback):
                self.callback = callback

        class Timer:
            def __init__(self):
                self.timeout = Signal()
                self.active = False
                self.stop_count = 0
                self.delays = []

            def setSingleShot(self, _single_shot):
                pass

            def isActive(self):
                return self.active

            def stop(self):
                self.stop_count += 1
                self.active = False

            def start(self, delay):
                self.delays.append(delay)
                self.active = True

        timer = Timer()
        fake_pyside = types.ModuleType("PySide")
        fake_pyside.QtCore = types.SimpleNamespace(QTimer=lambda: timer)
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._ensure_surface_reload_timer_state()
        proxy.reload = mock.Mock()
        first = types.SimpleNamespace(FileMtime="first")
        latest = types.SimpleNamespace(FileMtime="latest")

        with mock.patch.dict(sys.modules, {"PySide": fake_pyside}):
            proxy._schedule_surface_reload(first)
            proxy._schedule_surface_reload(latest)
            timer.timeout.callback()

        self.assertEqual(timer.delays, [2000, 2000])
        self.assertEqual(timer.stop_count, 1)
        self.assertEqual(first.FileMtime, "first")
        self.assertEqual(latest.FileMtime, "")
        proxy.reload.assert_called_once_with(latest)

    def test_auto_reload_does_not_bypass_pending_surface_debounce(self):
        linked_object = self._import_linked_object()
        proxy = types.SimpleNamespace(
            _check_file_changed=mock.Mock(return_value=True),
            _surface_reload_is_pending=mock.Mock(return_value=True),
            reload=mock.Mock(),
        )
        obj = types.SimpleNamespace(
            Document=types.SimpleNamespace(Restoring=False),
            FileName="/project/board.kicad_pcb",
            FileMtime="",
            AutoReload=True,
            Proxy=proxy,
        )
        vobj = types.SimpleNamespace(Object=obj)
        provider = linked_object.PcbObjectViewProvider.__new__(
            linked_object.PcbObjectViewProvider)

        provider._auto_reload(vobj)

        proxy._check_file_changed.assert_not_called()
        proxy.reload.assert_not_called()

    def test_outline_observer_does_not_open_kicad_during_surface_debounce(self):
        linked_object = self._import_linked_object()
        proxy = types.SimpleNamespace(
            _on_outline_changed=mock.Mock(),
            _on_outline_edit_start=mock.Mock(),
            _surface_reload_is_pending=mock.Mock(return_value=True),
        )
        parent = types.SimpleNamespace(Name="Board", Proxy=proxy)
        sketch = types.SimpleNamespace(
            Name="Board_Outline",
            TypeId="Sketcher::SketchObject",
            Document=types.SimpleNamespace(Restoring=False),
            InList=[parent],
        )
        observer = linked_object._OutlineSketchObserver()

        observer.slotInEdit(types.SimpleNamespace(Object=sketch))
        observer.slotChangedObject(sketch, "Shape")

        proxy._on_outline_edit_start.assert_not_called()
        proxy._on_outline_changed.assert_not_called()

    def test_outline_observer_ignores_assembly_view_provider(self):
        linked_object = self._import_linked_object()
        observer = linked_object._OutlineSketchObserver()
        assembly_view_provider = types.SimpleNamespace()

        observer.slotInEdit(assembly_view_provider)
        observer.slotResetEdit(assembly_view_provider)

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

        module_name = "FreekiCAD.freecad.FreekiCAD.PcbObject"
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
        module_name = "FreekiCAD.freecad.FreekiCAD.PcbObject"
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

    def test_board_color_file_does_not_borrow_silkscreen_color(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock())
        board_text = '''(kicad_pcb
  (setup
    (stackup
      (layer "F.Mask" (type "Top Solder Mask"))
      (layer "B.Silkscreen"
        (type "Bottom Silk Screen")
        (color "#808080FF")))))'''

        with tempfile.TemporaryDirectory() as directory:
            filepath = os.path.join(directory, "board.kicad_pcb")
            with open(filepath, "w", encoding="utf-8") as board_file:
                board_file.write(board_text)

            color = linked_object._get_board_color_from_file(filepath)

        self.assertIsNone(color)

    def test_board_color_file_reads_color_from_its_mask_layer_only(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock())
        board_text = '''(kicad_pcb
  (setup
    (stackup
      (layer "F.Mask"
        (type "Top Solder Mask")
        (material "Mask (green)")
        (color "#123456"))
      (layer "B.Silkscreen" (color "#808080FF")))))'''

        with tempfile.TemporaryDirectory() as directory:
            filepath = os.path.join(directory, "board.kicad_pcb")
            with open(filepath, "w", encoding="utf-8") as board_file:
                board_file.write(board_text)

            color = linked_object._get_board_color_from_file(filepath)

        self.assertEqual(color, (0x12 / 255.0, 0x34 / 255.0,
                                 0x56 / 255.0))

    def test_coupler_type_prefers_library_entry_name(self):
        linked_object = self._import_linked_object()

        self.assertEqual(
            linked_object._footprint_coupler_type(
                _NamedFootprint("CouplerMoving", "renamed value")),
            "CouplerMoving",
        )
        self.assertEqual(
            linked_object._footprint_coupler_type(
                _NamedFootprint("CouplerAt", "renamed value")),
            "CouplerAt",
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
        self.assertEqual(linked_object._parse_coupler_z("10 mil"), 0.254)
        self.assertEqual(linked_object._parse_coupler_z("250 um"), 0.25)
        self.assertEqual(linked_object._parse_coupler_z("250 µm"), 0.25)

    def test_coupler_offset_uses_length_units(self):
        linked_object = self._import_linked_object()

        self.assertEqual(linked_object._parse_coupler_offset("2.4"), 2.4)
        self.assertEqual(linked_object._parse_coupler_offset("1 in"), 25.4)
        self.assertEqual(linked_object._parse_coupler_offset("10 mil"), 0.254)
        self.assertEqual(linked_object._parse_coupler_offset("250 um"), 0.25)

    def test_coupler_tilt_reads_custom_field_in_degrees(self):
        linked_object = self._import_linked_object()
        footprint = _NamedFootprint("CouplerMoving")
        footprint.texts_and_fields = [_CustomField("Tilt", "-12.5 deg")]

        value = linked_object._footprint_field_value(footprint, "Tilt")

        self.assertEqual(linked_object._parse_coupler_tilt(value), -12.5)

    def test_coupler_at_coordinates_default_to_mm(self):
        linked_object = self._import_linked_object()

        self.assertEqual(
            linked_object._parse_coupler_at_coordinate("12.5", "X"), 12.5)
        self.assertEqual(
            linked_object._parse_coupler_at_coordinate("1 in", "Y"), 25.4)

    def test_coupler_custom_field_can_be_updated(self):
        linked_object = self._import_linked_object()
        footprint = _NamedFootprint("CouplerMoving")
        field = _CustomField("Z", "0 mm")
        footprint.texts_and_fields = [field]

        changed = linked_object._set_footprint_field_value(
            footprint, "Z", "2.5 mm")

        self.assertTrue(changed)
        self.assertEqual(field.text.text.value, "2.5 mm")

    def test_missing_coupler_offset_field_can_be_created(self):
        linked_object = self._import_linked_object()
        from kipy.board_types import Field, FootprintInstance

        footprint = FootprintInstance()
        z_field = Field()
        z_field.name = "Z"
        z_field.proto.id.id = 4
        z_field.proto.text.id.value = "source-text-id"
        z_field.text.value = "0 mm"
        footprint.definition.add_item(z_field)

        changed = linked_object._set_footprint_field_value(
            footprint, "Offset", "2.5 mm", create=True)

        self.assertTrue(changed)
        offset = next(field for field in footprint.definition.items
                      if isinstance(field, Field)
                      and field.name == "Offset")
        self.assertEqual(offset.text.value, "2.5 mm")
        self.assertEqual(offset.field_id, 5)
        self.assertNotEqual(
            offset.proto.text.id.value, z_field.proto.text.id.value)
        self.assertFalse(offset.visible)

        packed = FootprintInstance(proto=footprint.proto)
        packed_offset = next(
            field for field in packed.definition.items
            if isinstance(field, Field) and field.name == "Offset")
        self.assertEqual(packed_offset.text.value, "2.5 mm")

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
            _CustomField("Offset", "1.25 mm"),
            _CustomField("Tilt", "-12.5 deg"),
        ]

        pose = linked_object._coupler_pose_from_footprint(footprint, 1.6)

        self.assertEqual(pose, {
            "ref": "pair", "type": "CouplerMoving",
            "x": 12.5, "y": 7.25, "board_z": 1.6,
            "is_back": False, "z": 2.4, "offset": 1.25,
            "tilt": -12.5,
            "rotation": 27,
        })

    def test_live_coupler_at_pose_contains_world_target(self):
        linked_object = self._import_linked_object()
        from kipy.proto.board.board_types_pb2 import BoardLayer

        footprint = _NamedFootprint("CouplerAt")
        footprint.reference_field = types.SimpleNamespace(
            text=types.SimpleNamespace(value="absolute"))
        footprint.position = types.SimpleNamespace(x=5_000_000, y=-6_000_000)
        footprint.layer = BoardLayer.BL_F_Cu
        footprint.orientation = _Angle(10)
        footprint.texts_and_fields = [
            _CustomField("TargetX", "12.5 mm"),
            _CustomField("TargetY", "1 in"),
            _CustomField("TargetZ", "250 mil"),
            _CustomField("Tilt", "5 deg"),
        ]

        pose = linked_object._coupler_pose_from_footprint(footprint, 1.6)

        self.assertEqual(pose["target_x"], 12.5)
        self.assertEqual(pose["target_y"], 25.4)
        self.assertEqual(pose["target_z"], 6.35)
        self.assertEqual(pose["z"], 0)
        self.assertEqual(pose["offset"], 0)

    def test_stored_coupler_poses_are_loaded_from_json(self):
        linked_object = self._import_linked_object()
        obj = types.SimpleNamespace(CouplerPoses=json.dumps([
            {"ref": "mcu", "type": "CouplerFixed", "z": 2.4},
            {"ref": "other", "type": "CouplerMoving", "z": 0},
        ]))

        poses = linked_object.PcbObject._coupler_poses(
            obj, linked_object.COUPLER_FIXED)

        self.assertEqual(poses, [
            {"ref": "mcu", "type": "CouplerFixed", "z": 2.4},
        ])

    def test_live_coupler_poll_updates_only_persisted_list(self):
        linked_object = self._import_linked_object()
        monitored = [
            {"ref": "pair", "type": "CouplerFixed", "x": 1},
            {"ref": "absolute", "type": "CouplerAt", "x": 2},
        ]
        live = [
            {"ref": "new", "type": "CouplerFixed", "x": 30},
            {"ref": "absolute", "type": "CouplerAt", "x": 20},
            {"ref": "pair", "type": "CouplerFixed", "x": 10},
        ]

        selected = linked_object._select_monitored_coupler_poses(
            monitored, live)

        self.assertEqual([pose["ref"] for pose in selected], [
            "pair", "absolute"])
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

    def test_coupler_xy_move_uses_destination_partition_transform(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D

        class Partition:
            def __init__(self, min_x, max_x):
                self.min_x = min_x
                self.max_x = max_x

            def isInside(self, point, _tolerance, _include_boundary):
                return self.min_x <= point.x < self.max_x

        old_pose = {
            "ref": "pair", "type": "CouplerFixed", "x": 2, "y": 3,
            "board_z": 1.6, "rotation": 0, "z": 1, "offset": 0,
            "tilt": 0,
        }
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        old_flat = proxy._coupler_placement(old_pose)
        old_transform = _Placement2D(
            _Vector2D(10, 0, 0), _Rotation2D(None, 10))
        new_transform = _Placement2D(
            _Vector2D(30, 5, 4), _Rotation2D(None, 70))
        marker = types.SimpleNamespace(
            Name="Board_Coupler_pair", CouplerType="CouplerFixed",
            Reference="pair", X=12, Y=3, Z=1, Offset=0, Tilt=0,
            Placement=old_transform.multiply(old_flat),
            FreekiCAD_InitPlacement=old_flat)
        document = object()
        obj = types.SimpleNamespace(
            Label="board", CouplerPoses=json.dumps([old_pose]),
            Document=document)
        proxy._unbent_board_shape = object()
        proxy._unbent_placements = {}
        proxy._bend_partition_pieces = [
            Partition(0, 10), Partition(10, 20)]
        proxy._bend_partition_strip_pieces = set()
        proxy._bend_partition_half_t = 0.8
        proxy._bend_child_piece_idx = {marker.Name: 0}
        proxy._bend_piece_placements = [old_transform, new_transform]
        proxy._schedule_coupler_update = mock.Mock()
        proxy._schedule_rebend = mock.Mock()
        proxy._reposition_all_coupled_objects = mock.Mock()

        proxy._coupler_marker_changed(obj, marker, "X")

        new_pose = json.loads(obj.CouplerPoses)[0]
        expected = new_transform.multiply(proxy._coupler_placement(new_pose))
        self.assertAlmostEqual(marker.Placement.Base.x, expected.Base.x)
        self.assertAlmostEqual(marker.Placement.Base.y, expected.Base.y)
        self.assertAlmostEqual(marker.Placement.Base.z, expected.Base.z)
        self.assertAlmostEqual(marker.Placement.angle, expected.angle)
        self.assertEqual(proxy._bend_child_piece_idx[marker.Name], 1)
        proxy._schedule_rebend.assert_not_called()
        proxy._reposition_all_coupled_objects.assert_called_once_with(document)

    def test_bend_piece_placement_signature_uses_final_matrix(self):
        linked_object = self._import_linked_object()
        first = _Placement2D(
            _Vector2D(10, 20, 30), _Rotation2D(None, 45))
        within_tolerance = _Placement2D(
            _Vector2D(10 + 1e-12, 20, 30), _Rotation2D(None, 45))
        moved = _Placement2D(
            _Vector2D(10.001, 20, 30), _Rotation2D(None, 45))

        signature = linked_object._placement_matrix_signature(first)

        self.assertEqual(
            signature,
            linked_object._placement_matrix_signature(within_tolerance))
        self.assertNotEqual(
            signature,
            linked_object._placement_matrix_signature(moved))

    def test_bend_partition_signature_depends_on_cut_geometry(self):
        linked_object = self._import_linked_object()
        base = [
            (_Vector2D(1, 2), _Vector2D(3, 4), "A", 0, 0.5, 1.0),
            (_Vector2D(5, 6), _Vector2D(7, 8), "B", 0, 0.5, 1.0),
        ]
        same_cuts_new_angle = [
            (*entry[:4], -entry[4], entry[5]) for entry in base]
        moved_cut = list(base)
        moved_cut[1] = (
            _Vector2D(5, 6), _Vector2D(7.1, 8),
            *base[1][2:])

        signature = linked_object._bend_partition_signature(base, 1.6)

        self.assertEqual(
            signature,
            linked_object._bend_partition_signature(
                same_cuts_new_angle, 1.6))
        self.assertNotEqual(
            signature,
            linked_object._bend_partition_signature(moved_cut, 1.6))

    def test_changed_live_couplers_are_applied(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
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
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._coupler_poll_in_flight = True
        proxy._coupler_monitor_generation = 8
        proxy._coupler_poll_retry_after = 0.0
        proxy._apply_live_coupler_poses = mock.Mock()
        obj = types.SimpleNamespace(CouplerPoses="[]")

        proxy._finish_coupler_poll(obj, 7, [], None)

        proxy._apply_live_coupler_poses.assert_not_called()

    def test_live_coupler_result_for_deleted_object_is_ignored(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._coupler_poll_in_flight = True
        proxy._coupler_monitor_generation = 7
        proxy._coupler_poll_retry_after = 0.0
        proxy._apply_live_coupler_poses = mock.Mock()

        class DeletedObject:
            @property
            def Name(self):
                raise ReferenceError("deleted object")

        proxy._finish_coupler_poll(DeletedObject(), 7, [], None)

        self.assertFalse(proxy._coupler_poll_in_flight)
        proxy._apply_live_coupler_poses.assert_not_called()

    def test_applying_live_coupler_updates_marker_and_repositions(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock())

        class Marker:
            Name = "Board_Coupler_pair"
            CouplerType = "CouplerFixed"
            Reference = "pair"
            X = 0
            Y = 0
            Z = 0
            Offset = 0
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
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._coupler_placement = mock.Mock(return_value=placement)
        proxy._reposition_all_coupled_objects = mock.Mock()
        pose = {
            "ref": "pair", "type": "CouplerFixed", "x": 10,
            "y": 20, "z": 3, "offset": 1.5,
            "tilt": 12, "rotation": 30,
        }

        proxy._apply_live_coupler_poses(obj, [pose])

        self.assertEqual(json.loads(obj.CouplerPoses), [pose])
        self.assertEqual(marker.X, 10)
        self.assertEqual(marker.Y, 20)
        self.assertEqual(marker.Z, 3)
        self.assertEqual(marker.Offset, 1.5)
        self.assertEqual(marker.Tilt, 12)
        self.assertIs(marker.Placement, placement)
        self.assertIs(marker.FreekiCAD_InitPlacement, placement)
        proxy._reposition_all_coupled_objects.assert_called_once_with(document)

    def test_live_tilt_only_update_does_not_rebend_board(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock())
        old_pose = {
            "ref": "pair", "type": "CouplerFixed", "x": 4, "y": 6,
            "board_z": 1.6, "rotation": 10, "z": 1, "offset": 0.5,
            "tilt": 0,
        }
        new_pose = {**old_pose, "tilt": 22}
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        old_flat = proxy._coupler_placement(old_pose)
        bend_transform = _Placement2D(
            _Vector2D(-8, 13, 2), _Rotation2D(None, -25))
        marker = types.SimpleNamespace(
            Name="Board_Coupler_pair", CouplerType="CouplerFixed",
            Reference="pair", X=4, Y=6, Z=1, Offset=0.5, Tilt=0,
            Placement=bend_transform.multiply(old_flat),
            FreekiCAD_InitPlacement=old_flat)
        document = object()
        obj = types.SimpleNamespace(
            Label="board", CouplerPoses=json.dumps([old_pose]),
            Group=[marker], Document=document)
        proxy._unbent_board_shape = object()
        proxy._unbent_placements = {}
        proxy._rebend = mock.Mock()
        proxy._reposition_all_coupled_objects = mock.Mock()

        proxy._apply_live_coupler_poses(obj, [new_pose])

        expected = bend_transform.multiply(proxy._coupler_placement(new_pose))
        self.assertAlmostEqual(marker.Placement.Base.x, expected.Base.x)
        self.assertAlmostEqual(marker.Placement.Base.y, expected.Base.y)
        self.assertAlmostEqual(marker.Placement.Base.z, expected.Base.z)
        self.assertAlmostEqual(marker.Placement.angle, expected.angle)
        self.assertEqual(marker.Tilt, 22)
        proxy._rebend.assert_not_called()
        proxy._reposition_all_coupled_objects.assert_called_once_with(document)

    def test_editing_coupler_fields_updates_pose_and_schedules_kicad_sync(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        document = object()
        marker = types.SimpleNamespace(
            Name="Board_Coupler_pair", CouplerType="CouplerFixed",
            Reference="pair", X=12.5, Y=7.25, Z=2.4, Offset=1.25,
            Tilt=-12.5,
            Placement=None, FreekiCAD_InitPlacement=None)
        obj = types.SimpleNamespace(
            Label="board", CouplerPoses=json.dumps([{
                "ref": "pair", "type": "CouplerFixed", "x": 1,
                "y": 2, "board_z": 1.6, "rotation": 27,
            }]), Document=document)
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._schedule_coupler_update = mock.Mock()
        proxy._reposition_all_coupled_objects = mock.Mock()

        proxy._coupler_marker_changed(obj, marker)

        pose = json.loads(obj.CouplerPoses)[0]
        self.assertEqual(
            (pose["x"], pose["y"], pose["z"], pose["offset"],
             pose["tilt"]),
            (12.5, 7.25, 2.4, 1.25, -12.5))
        self.assertEqual(proxy._pending_coupler_updates["pair"], {
            "ref": "pair", "type": "CouplerFixed",
            "x": 12.5, "y": 7.25, "z": 2.4, "offset": 1.25,
            "tilt": -12.5,
        })
        proxy._schedule_coupler_update.assert_called_once_with(obj, "pair")
        proxy._reposition_all_coupled_objects.assert_called_once_with(document)

    def test_editing_only_coupler_tilt_reuses_existing_bend_transform(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        document = object()
        old_pose = {
            "ref": "pair", "type": "CouplerFixed", "x": 12.5,
            "y": 7.25, "board_z": 1.6, "rotation": 27,
            "z": 2.4, "offset": 1.25, "tilt": 0,
        }
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        old_flat = proxy._coupler_placement(old_pose)
        bend_transform = _Placement2D(
            _Vector2D(20, -5, 3), _Rotation2D(None, 35))
        old_displayed = bend_transform.multiply(old_flat)
        marker = types.SimpleNamespace(
            Name="Board_Coupler_pair", CouplerType="CouplerFixed",
            Reference="pair", X=12.5, Y=7.25, Z=2.4, Offset=1.25,
            Tilt=18, Placement=old_displayed,
            FreekiCAD_InitPlacement=old_flat)
        obj = types.SimpleNamespace(
            Label="board", CouplerPoses=json.dumps([old_pose]),
            Document=document)
        proxy._unbent_board_shape = object()
        proxy._unbent_placements = {}
        proxy._schedule_coupler_update = mock.Mock()
        proxy._schedule_rebend = mock.Mock()
        proxy._reposition_all_coupled_objects = mock.Mock()

        proxy._coupler_marker_changed(obj, marker, "Tilt")

        new_pose = json.loads(obj.CouplerPoses)[0]
        new_flat = proxy._coupler_placement(new_pose)
        expected = bend_transform.multiply(new_flat)
        self.assertAlmostEqual(marker.Placement.Base.x, expected.Base.x)
        self.assertAlmostEqual(marker.Placement.Base.y, expected.Base.y)
        self.assertAlmostEqual(marker.Placement.Base.z, expected.Base.z)
        self.assertAlmostEqual(marker.Placement.angle, expected.angle)
        proxy._schedule_rebend.assert_not_called()
        proxy._reposition_all_coupled_objects.assert_called_once_with(document)

    def test_coupler_update_writes_xy_z_and_tilt_to_kicad(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        footprint = _NamedFootprint("CouplerFixed")
        footprint.reference_field = types.SimpleNamespace(
            text=types.SimpleNamespace(value="pair"))
        footprint.position = None
        z_field = _CustomField("Z", "0 mm")
        offset_field = _CustomField("Offset", "0 mm")
        tilt_field = _CustomField("Tilt", "0 deg")
        footprint.texts_and_fields = [z_field, offset_field, tilt_field]
        board = types.SimpleNamespace(
            get_footprints=mock.Mock(return_value=[footprint]),
            begin_commit=mock.Mock(return_value="commit"),
            update_items=mock.Mock(), push_commit=mock.Mock())
        linked_object._kipy_ready_board = mock.Mock(return_value=board)
        linked_object._kipy_retry = lambda func: func()
        vector2 = types.SimpleNamespace(
            from_xy_mm=mock.Mock(return_value=(12.5, -7.25)))
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._coupler_updates_in_flight = {"pair": {
            "ref": "pair", "type": "CouplerFixed",
            "x": 12.5, "y": 7.25, "z": 2.4, "offset": 1.25,
            "tilt": -12.5,
        }}
        proxy._pending_coupler_updates = {}
        proxy._coupler_update_timers = {}
        fake_kicad = types.ModuleType("kipy.kicad")
        fake_kicad.KiCad = mock.Mock(return_value=object())
        fake_geometry = types.ModuleType("kipy.geometry")
        fake_geometry.Vector2 = vector2

        with mock.patch.dict(sys.modules, {
                "kipy.kicad": fake_kicad,
                "kipy.geometry": fake_geometry}):
            proxy._write_coupler_update_to_kicad(
                "/tmp/api.sock", "pair",
                proxy._coupler_updates_in_flight["pair"])

        self.assertEqual(footprint.position, (12.5, -7.25))
        self.assertEqual(z_field.text.text.value, "2.4 mm")
        self.assertEqual(offset_field.text.text.value, "1.25 mm")
        self.assertEqual(tilt_field.text.text.value, "-12.5 deg")
        board.update_items.assert_called_once_with([footprint])
        board.push_commit.assert_called_once_with(
            "commit", "Update coupler pair from FreeCAD")

    def test_coupler_at_update_does_not_require_or_write_local_z(self):
        linked_object = self._import_linked_object()
        footprint = _NamedFootprint("CouplerAt")
        footprint.reference_field = types.SimpleNamespace(
            text=types.SimpleNamespace(value="absolute"))
        footprint.position = None
        tilt_field = _CustomField("Tilt", "0 deg")
        footprint.texts_and_fields = [
            _CustomField("TargetX", "0 mm"),
            _CustomField("TargetY", "0 mm"),
            _CustomField("TargetZ", "0 mm"),
            tilt_field,
        ]
        board = types.SimpleNamespace(
            get_footprints=mock.Mock(return_value=[footprint]),
            begin_commit=mock.Mock(return_value="commit"),
            update_items=mock.Mock(), push_commit=mock.Mock())
        linked_object._kipy_ready_board = mock.Mock(return_value=board)
        linked_object._kipy_retry = lambda func: func()
        vector2 = types.SimpleNamespace(
            from_xy_mm=mock.Mock(return_value=(1.0, -2.0)))
        update = {
            "ref": "absolute", "type": "CouplerAt",
            "x": 1.0, "y": 2.0, "z": 0.0, "tilt": 15.0,
        }
        fake_kicad = types.ModuleType("kipy.kicad")
        fake_kicad.KiCad = mock.Mock(return_value=object())
        fake_geometry = types.ModuleType("kipy.geometry")
        fake_geometry.Vector2 = vector2

        with mock.patch.dict(sys.modules, {
                "kipy.kicad": fake_kicad,
                "kipy.geometry": fake_geometry}):
            linked_object.PcbObject._write_coupler_update_to_kicad(
                "/tmp/api.sock", "absolute", update)

        self.assertEqual(footprint.position, (1.0, -2.0))
        self.assertEqual(tilt_field.text.text.value, "15 deg")
        self.assertIsNone(
            linked_object._footprint_field_value(footprint, "Z", None))

    def test_coupler_update_response_does_not_write_on_main_thread(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        update = {"ref": "pair", "type": "CouplerFixed"}
        proxy._coupler_updates_in_flight = {"pair": update}
        proxy._pending_coupler_updates = {}
        proxy._coupler_update_timers = {}
        proxy._write_coupler_update_to_kicad = mock.Mock()
        started = []

        class FakeThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                started.append(self)

        with mock.patch("threading.Thread", FakeThread):
            proxy._handle_update_coupler_response(
                types.SimpleNamespace(Label="board"),
                "/tmp/api.sock", "pair")

        self.assertEqual(len(started), 1)
        self.assertTrue(started[0].daemon)
        proxy._write_coupler_update_to_kicad.assert_not_called()

    def test_coupler_snap_makes_planes_coincide_face_to_face(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock())
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
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
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)

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
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy.Type = "PcbObject"
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
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy.Type = "PcbObject"
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
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy.Type = "PcbObject"
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
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy.Type = "PcbObject"
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

    def test_synchronous_reload_waits_for_workspace_and_board_geometry(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy.Type = "PcbObject"
        proxy._reloading = False
        proxy._coupler_monitor_generation = 0
        proxy._ensure_coupler_monitor_state = mock.Mock()
        proxy._ensure_properties = mock.Mock()
        shape = types.SimpleNamespace(isNull=lambda: False)
        workspace_bus = types.ModuleType(
            "FreekiCAD.freecad.FreekiCAD.workspace_bus")
        workspace_bus.request_sync = mock.Mock(return_value={
            "socket": "/tmp/kicad.sock",
        })

        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb") as board_file:
            obj = types.SimpleNamespace(
                Name="Board", Label="board", FileName=board_file.name,
                Group=[])

            def finish_reload(actual_obj, socket_path, reposition=True):
                self.assertEqual(socket_path, "/tmp/kicad.sock")
                self.assertFalse(reposition)
                actual_obj.Group.append(types.SimpleNamespace(
                    Name="Board_Board", Shape=shape))
                proxy._reloading = False

            proxy._handle_reload_response = mock.Mock(
                side_effect=finish_reload)
            with mock.patch.dict(sys.modules, {
                    "FreekiCAD.freecad.FreekiCAD.workspace_bus":
                    workspace_bus}):
                result = proxy.reload_sync(obj, reposition=False)

        self.assertTrue(result)
        workspace_bus.request_sync.assert_called_once_with(
            "reload", board_file.name, object_label="board")
        proxy._handle_reload_response.assert_called_once_with(
            obj, "/tmp/kicad.sock", reposition=False)

    def test_reposition_skips_only_board_actively_rebuilding(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        fixed_proxy = linked_object.PcbObject.__new__(
            linked_object.PcbObject)
        fixed_proxy.Type = "PcbObject"
        fixed_proxy._reloading = False
        fixed_proxy._snap_moving_object = mock.Mock(return_value=True)
        fixed_proxy._snap_at_object = mock.Mock(return_value=True)
        moving_proxy = linked_object.PcbObject.__new__(
            linked_object.PcbObject)
        moving_proxy.Type = "PcbObject"
        moving_proxy._reloading = True
        moving_proxy._in_execute = True
        moving_proxy._snap_moving_object = mock.Mock(return_value=True)

        fixed = types.SimpleNamespace(
            Name="Fixed", Label="fixed", Proxy=fixed_proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([
                {"ref": "pair", "type": "CouplerFixed"},
                {"ref": "absolute", "type": "CouplerAt"},
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

        fixed_proxy._snap_at_object.assert_called_once()
        fixed_proxy._snap_moving_object.assert_not_called()
        message = linked_object.FreeCAD.Console.PrintMessage.call_args_list[0]
        self.assertIn("actively rebuilding", message.args[0])
        self.assertIn("moving", message.args[0])

        # Waiting for a response leaves the previous board data intact, so it
        # must follow a changed root even though _reloading remains true.
        moving_proxy._in_execute = False
        moving_proxy._snap_at_object = mock.Mock(return_value=True)
        moving_proxy._reposition_all_coupled_objects(document)

        moving_proxy._snap_at_object.assert_called_once()
        moving_proxy._snap_moving_object.assert_called_once()

    def test_reload_error_releases_request_and_excludes_stale_board(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy.Type = "PcbObject"
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
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._reloading = False
        proxy._reload_retry_after = 200.0
        proxy._check_file_changed = mock.Mock(return_value=True)
        obj = types.SimpleNamespace(Name="Board")

        with mock.patch.object(linked_object.time, "monotonic", return_value=100.0):
            proxy.reload(obj)

        proxy._check_file_changed.assert_not_called()

    def test_automatic_reload_cannot_bypass_surface_deadline(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._reloading = False
        proxy._surface_reload_deadline = 102.0
        proxy._check_file_changed = mock.Mock(return_value=True)
        obj = types.SimpleNamespace(Name="Board")

        with mock.patch.object(linked_object.time, "monotonic", return_value=100.0):
            proxy.reload(obj)

        proxy._check_file_changed.assert_not_called()

    def test_manual_reload_bypasses_failure_backoff(self):
        linked_object = self._import_linked_object()
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy._reloading = False
        proxy._reload_retry_after = 200.0
        proxy._ensure_properties = mock.Mock()
        obj = types.SimpleNamespace(
            Name="Board", Label="board", FileName="/boards/board.kicad_pcb",
            Document=types.SimpleNamespace(FileName=""))
        send_request = mock.Mock()

        with mock.patch.object(linked_object.time, "monotonic", return_value=100.0), \
                mock.patch.dict(sys.modules, {
                    "FreekiCAD.freecad.FreekiCAD.workspace_bus": types.SimpleNamespace(
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
        view_proxy = linked_object.PcbObjectViewProvider.__new__(
            linked_object.PcbObjectViewProvider)
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
        view_proxy = linked_object.PcbObjectViewProvider.__new__(
            linked_object.PcbObjectViewProvider)
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
        view_proxy = linked_object.PcbObjectViewProvider.__new__(
            linked_object.PcbObjectViewProvider)
        view_proxy._initial_positioning_pending = True

        view_proxy._auto_reload(types.SimpleNamespace(Object=obj))

        proxy._check_file_changed.assert_not_called()
        proxy.reload.assert_not_called()
        proxy._reposition_all_coupled_objects.assert_called_once_with(document)

    def test_coupler_dependency_cycle_is_not_applied(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock())
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy.Type = "PcbObject"
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
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy.Type = "PcbObject"
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
        self.assertIn("2 CouplerMoving and 0 CouplerAt", error)
        self.assertIn("skipping", error)

    def test_coupler_at_snaps_to_absolute_world_coordinates(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy.Type = "PcbObject"
        at_pose = {
            "ref": "absolute", "type": "CouplerAt",
            "x": 10, "y": 20, "rotation": 30,
            "target_x": 12.5, "target_y": -4.25, "target_z": 3.5,
        }
        board = types.SimpleNamespace(
            Name="Board", Label="absolute board", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([at_pose]),
            Placement=_Placement2D(
                _Vector2D(50, 60, 0), _Rotation2D(None, 45)),
            Group=[])
        document = types.SimpleNamespace(Objects=[board])
        board.Document = document

        proxy._reposition_all_coupled_objects(document)

        target_world = board.Placement.multiply(
            proxy._coupler_placement(at_pose))
        self.assertAlmostEqual(target_world.Base.x, 12.5)
        self.assertAlmostEqual(target_world.Base.y, -4.25)
        self.assertAlmostEqual(target_world.Base.z, 3.5)
        self.assertAlmostEqual(target_world.angle % 360, 180)
        message = linked_object.FreeCAD.Console.PrintMessage.call_args.args[0]
        self.assertIn("world (12.5, -4.25, 3.5)", message)

    def test_coupler_at_and_moving_report_error_and_skip_positioning(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Console = types.SimpleNamespace(
            PrintMessage=mock.Mock(), PrintWarning=mock.Mock(),
            PrintError=mock.Mock())
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)
        proxy.Type = "PcbObject"
        proxy._snap_moving_object = mock.Mock(return_value=True)

        board = types.SimpleNamespace(
            Name="Board", Label="conflicting board", Proxy=proxy,
            SnapToCoupler=True, CouplerPoses=json.dumps([
                {"ref": "moving", "type": "CouplerMoving"},
                {"ref": "absolute", "type": "CouplerAt"},
            ]))
        document = types.SimpleNamespace(Objects=[board])
        board.Document = document

        proxy._reposition_all_coupled_objects(document)

        proxy._snap_moving_object.assert_not_called()
        error = linked_object.FreeCAD.Console.PrintError.call_args.args[0]
        self.assertIn("1 CouplerMoving and 1 CouplerAt", error)
        self.assertIn("skipping", error)

    def test_coupler_mating_rotates_around_local_y(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D

        mating = linked_object.PcbObject._coupler_mating_placement()

        self.assertEqual(
            (mating.axis.x, mating.axis.y, mating.axis.z), (0, 1, 0))
        self.assertEqual(mating.angle, 180)

    def test_coupler_t_uses_positive_footprint_x_direction(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D

        placement = linked_object.PcbObject._coupler_placement({
            "x": 0, "y": 0, "rotation": 30, "tilt": 10,
        })

        self.assertEqual(placement.angle, 40)

    def test_coupler_z_stays_along_board_normal_when_tilted(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D

        class TrackingPlacement:
            created = 0

            def __init__(self, vector=None, rotation=None, steps=None):
                if steps is not None:
                    self.steps = steps
                else:
                    labels = (
                        "surface", "in-plane-offset", "z-offset", "tilt")
                    self.steps = [labels[TrackingPlacement.created]]
                    TrackingPlacement.created += 1

            def multiply(self, other):
                return TrackingPlacement(steps=self.steps + other.steps)

        linked_object.FreeCAD.Placement = TrackingPlacement

        placement = linked_object.PcbObject._coupler_placement({
            "x": 0, "y": 0, "rotation": 0,
            "board_z": 1.6, "z": 2.4, "offset": 3.2, "tilt": 10,
        })

        self.assertEqual(
            placement.steps,
            ["surface", "in-plane-offset", "z-offset", "tilt"])

    def test_positive_coupler_offset_follows_triangle_direction(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D

        placement = linked_object.PcbObject._coupler_placement({
            "x": 0, "y": 0, "rotation": 0,
            "board_z": 1.6, "z": 2.4, "offset": 3.2, "tilt": 0,
        })

        self.assertAlmostEqual(placement.Base.x, 0)
        self.assertAlmostEqual(placement.Base.y, -3.2)
        self.assertAlmostEqual(placement.Base.z, 4.0)

    def test_back_coupler_flips_the_footprint_frame(self):
        linked_object = self._import_linked_object()
        linked_object.FreeCAD.Vector = _Vector2D
        linked_object.FreeCAD.Rotation = _Rotation2D
        linked_object.FreeCAD.Placement = _Placement2D

        placement = linked_object.PcbObject._coupler_placement({
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
        proxy = linked_object.PcbObject.__new__(linked_object.PcbObject)

        proxy._build_coupler_children(obj, [{
            "ref": "mcu", "type": "CouplerFixed",
            "x": 10, "y": 20, "board_z": 1.6,
            "z": 4.5, "offset": 2.5, "rotation": 30, "tilt": 10,
        }])

        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].Label, "CouplerFixed mcu")
        self.assertEqual(children[0].Z, 4.5)
        self.assertEqual(children[0].Offset, 2.5)
        self.assertEqual(children[0].Tilt, 10)
        self.assertEqual(children[0].Placement.Base.z, 6.1)
        self.assertIs(
            children[0].FreekiCAD_InitPlacement, children[0].Placement)
        self.assertFalse(children[0].ViewObject.Visibility)
        self.assertEqual(children[0].ViewObject.Proxy, 0)
        self.assertEqual(children[0].Shape[0], "face")
        polygon_points = children[0].Shape[1][1]
        self.assertEqual(polygon_points[0].y, 1)
        self.assertEqual(polygon_points[2].y, 0)


if __name__ == "__main__":
    unittest.main()
