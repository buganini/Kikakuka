import importlib
import json
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


if __name__ == "__main__":
    unittest.main()
