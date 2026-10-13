"""Framebuffer regressions; execute this module inside a FreeCAD GUI session.

For example, from the GUI Python console (with the repository as cwd)::

    import runpy
    suite = runpy.run_path('tests/test_freekicad_surface_render_native.py')
    suite['run_tests']()

Ordinary Python/FreeCADCmd discovery skips these tests. They create and close
their own documents and never load, save, or change the user's PCB. Set
FREEKICAD_RENDER_TEST_OUTPUT to a directory to retain the PNG captures.
"""

import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import types
import unittest
import uuid

try:
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    from pivy import coin
    from PySide import QtGui
except ImportError:
    App = None


@unittest.skipUnless(App is not None and getattr(App, 'GuiUp', False),
                     'requires a running FreeCAD GUI and OpenGL viewport')
class SurfaceRenderNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load the checkout, without replacing the user's installed workbench
        # modules or starting an integration/IPC connection.
        cls.package_name = '_freekicad_surface_render_' + uuid.uuid4().hex
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(Path(__file__).resolve().parents[1]
                                / 'FreekiCAD/freecad/FreekiCAD')]
        sys.modules[cls.package_name] = package
        cls.pcb = importlib.import_module(cls.package_name + '.PcbObject')

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name == cls.package_name or name.startswith(cls.package_name + '.'):
                sys.modules.pop(name, None)

    def setUp(self):
        previous = App.ActiveDocument
        self.previous_document = previous.Name if previous else None
        self.doc = App.newDocument('SurfaceRender_' + uuid.uuid4().hex[:8])
        self.addCleanup(self._close_document)
        self.view = Gui.activeDocument().activeView()
        manager = self.view.getViewer().getSoRenderManager()
        manager.setAutoClipping(manager.NO_AUTO_CLIPPING)
        self.capture_index = 0
        self.capture_records = {}
        output = os.environ.get('FREEKICAD_RENDER_TEST_OUTPUT')
        if output:
            self.output = Path(output)
            self.output.mkdir(parents=True, exist_ok=True)
        else:
            temporary = tempfile.TemporaryDirectory(prefix='freekicad-render-')
            self.addCleanup(temporary.cleanup)
            self.output = Path(temporary.name)

        self.board = self._feature(
            'Substrate', Part.makeBox(20, 20, 0.972, App.Vector(-10, -10, 0)),
            (0.75, 0.75, 0.75), transparency=37, substrate=True)
        self.copper = self._feature(
            'Copper', self._plane(-8, -8, 7, 16, z=0.99), (0.85, 0.25, 0.05))
        self.mask = self._feature(
            'Mask', self._plane(-10, -10, 20, 20), (0.05, 0.60, 0.10),
            transparency=50)
        self.silk = self._feature(
            'Silk', self._plane(2, 2, 6, 6), (0.95, 0.95, 0.95))
        # This overlaps the silk in XY. A too-large depth range bias makes
        # the lettering paint over this component with a tight near plane.
        self.component = self._feature(
            'Component', Part.makeBox(3, 3, 1, App.Vector(4, 4, 1)),
            (0.10, 0.15, 0.85))
        self.doc.recompute()
        self.original_cad_state = self._cad_state()
        self.assertTrue(self.pcb._apply_surface_render_state(
            self.copper.ViewObject, 'F.Cu'))
        self.assertTrue(self.pcb._apply_surface_render_state(
            self.mask.ViewObject, 'F.Mask'))
        self.assertTrue(self.pcb._apply_surface_render_state(
            self.silk.ViewObject, 'F.SilkS', silkscreen=True))

        # Scope deterministic light/transparency settings to this document.
        # BASE_COLOR removes lighting differences between opaque controls and
        # transparent paths; the real FreeCAD Part geometry renderer remains.
        scene = self.view.getSceneGraph()
        light = coin.SoLightModel()
        light.model.setValue(coin.SoLightModel.BASE_COLOR)
        light.setOverride(True)
        scene.insertChild(light, 0)
        transparency = coin.SoTransparencyType()
        transparency.value.setValue(
            coin.SoTransparencyType.SORTED_OBJECT_SORTED_TRIANGLE_BLEND)
        scene.insertChild(transparency, 1)
        self._camera(False, 0)

    def _close_document(self):
        if self.doc.Name in App.listDocuments():
            App.closeDocument(self.doc.Name)
        if self.previous_document in App.listDocuments():
            App.setActiveDocument(self.previous_document)
            Gui.activeDocument().activeView().redraw()

    @staticmethod
    def _plane(x, y, width, height, z=1):
        return Part.makePlane(width, height, App.Vector(x, y, z))

    def _feature(self, name, shape, color, transparency=0, substrate=False):
        obj = self.doc.addObject('Part::Feature', name)
        obj.Shape = shape
        obj.ViewObject.DisplayMode = 'Shaded'
        obj.ViewObject.ShapeColor = color
        obj.ViewObject.LineColor = color
        obj.ViewObject.Transparency = transparency
        if substrate:
            cad_state = self._cad_state()
            self.assertTrue(self.pcb._apply_board_polygon_offset(obj.ViewObject))
            self.assertEqual(self._cad_state(), cad_state,
                             'substrate render state changed CAD geometry or placement')
        return obj

    def _cad_state(self):
        # A viewer update can change OCC's cached mesh and topology bookkeeping
        # without changing the CAD model. Copy and clean independently, so the
        # comparison includes exact geometry/topology/locations but not those
        # display caches, and never cleans the document's actual shape.
        shapes = {}
        placements = {}
        for obj in self.doc.Objects:
            if hasattr(obj, 'Shape'):
                shape = obj.Shape.copy().cleaned()
                shapes[obj.Name] = hashlib.sha256(
                    shape.exportBrepToString().encode('utf-8')).hexdigest()
            if hasattr(obj, 'Placement'):
                # App::Part has a placement but no shape. Check it explicitly
                # rather than relying on child-local BREP to detect movement.
                placements[obj.Name] = tuple(obj.Placement.toMatrix().A)
        return {'shapes': shapes, 'placements': placements}

    def _camera(self, perspective, tilt, near=0.02):
        self.near_distance = near
        self.view.setCameraType('Perspective' if perspective else 'Orthographic')
        camera = self.view.getCameraNode()
        rotation = coin.SbRotation(coin.SbVec3f(1, 0, 0), math.radians(tilt))
        position = rotation.multVec(coin.SbVec3f(0, 0, 35))
        camera.orientation.setValue(rotation)
        camera.position.setValue(position)
        camera.aspectRatio.setValue(1.0)
        camera.focalDistance.setValue(34.0)
        camera.nearDistance.setValue(near)
        camera.farDistance.setValue(100.0)
        if perspective:
            camera.heightAngle.setValue(math.radians(45))
        else:
            camera.height.setValue(28.0)
        self.view.redraw()
        Gui.updateGui()

    def _capture(self, label, size=(512, 512)):
        # Restore the deliberately stressful clipping range after any viewer
        # auto-clipping caused by a camera/visibility update.
        self.view.redraw()
        Gui.updateGui()
        camera = self.view.getCameraNode()
        camera.nearDistance.setValue(self.near_distance)
        camera.farDistance.setValue(getattr(self, 'far_distance', 100.0))
        path = self.output / '{}-{:02d}-{}.png'.format(
            self._testMethodName, self.capture_index, label)
        self.capture_index += 1
        before = self.view.getCamera()
        # Do not pump viewer events between setting the clip planes and the
        # synchronous image render: automatic clipping would replace them.
        self.view.saveImage(str(path), size[0], size[1], 'White')
        image = QtGui.QImage(str(path))
        self.assertFalse(image.isNull(), str(path))
        self.assertEqual((image.width(), image.height()), size)
        record = {
            'image': str(path),
            'camera_before': before,
            'camera_after': self.view.getCamera(),
            'near_after': float(camera.nearDistance.getValue()),
            'far_after': float(camera.farDistance.getValue()),
            'image_size': size,
            'samples': [],
        }
        self.capture_records[int(image.cacheKey())] = record
        path.with_suffix('.json').write_text(json.dumps(record, indent=2))
        self.assertAlmostEqual(record['near_after'], self.near_distance, places=7,
                               msg='viewer replaced stressful near plane')
        return image

    def _sample(self, image, point):
        screen = self.view.getCameraNode().getViewVolume(
            image.width() / image.height()).projectToScreen(coin.SbVec3f(*point))
        x = int(round(screen[0] * (image.width() - 1)))
        y = int(round((1 - screen[1]) * (image.height() - 1)))
        self.assertTrue(3 <= x < image.width() - 3
                        and 3 <= y < image.height() - 3, (point, x, y))
        pixels = [image.pixelColor(x + dx, y + dy)
                  for dx in range(-2, 3) for dy in range(-2, 3)]
        color = tuple(statistics.median(getattr(pixel, channel)()
                                       for pixel in pixels)
                      for channel in ('red', 'green', 'blue'))
        record = self.capture_records[int(image.cacheKey())]
        record['samples'].append({'point': point, 'pixel': (x, y), 'rgb': color})
        Path(record['image']).with_suffix('.json').write_text(
            json.dumps(record, indent=2))
        return color

    def _assert_rgb_close(self, actual, expected, label, tolerance=12):
        self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, expected)),
                             tolerance, '{}: {} vs {}'.format(
                                 label, actual, expected))

    @staticmethod
    def _native_triangle_counts(obj):
        """Inspect the actual viewer mesh without retessellating the Shape."""
        counts = []
        seen = set()

        def visit(node):
            node_id = int(node.getNodeId())
            if node_id in seen:
                return
            seen.add(node_id)
            type_name = node.getTypeId().getName().getString()
            if 'BrepFaceSet' in type_name:
                indices = node.getField('coordIndex')
                if indices is not None:
                    counts.append(indices.getNum() // 4)
            if hasattr(node, 'getNumChildren'):
                for index in range(node.getNumChildren()):
                    visit(node.getChild(index))

        visit(obj.ViewObject.RootNode)
        return counts

    def _grazing_camera(self, center, normal, tangent, width, angle, camera=None):
        if camera is None:
            self.view.setCameraType('Perspective')
            camera = self.view.getCameraNode()
        # Match the close user camera's focal distance, not a square image's
        # framing distance. Its wide viewport is about 4.33--5.2 mm across.
        distance = 2.8
        aspect = width / (2 * distance * math.tan(math.radians(45) / 2))
        image_size = (int(round(512 * aspect)), 512)
        outward = (normal * math.sin(math.radians(angle))
                   + tangent * math.cos(math.radians(angle)))
        position = center + outward * distance
        rotation = App.Rotation(App.Vector(0, 0, 1), outward)
        camera.orientation.setValue(coin.SbRotation(*rotation.Q))
        camera.position.setValue(position.x, position.y, position.z)
        camera.aspectRatio.setValue(image_size[0] / image_size[1])
        camera.focalDistance.setValue(distance)
        camera.heightAngle.setValue(math.radians(45))
        self.near_distance = 0.017
        camera.nearDistance.setValue(self.near_distance)
        camera.farDistance.setValue(100.0)
        self.view.redraw()
        Gui.updateGui()
        return image_size

    def _stress_surfaces(self, curved):
        """Independent meshes of coincident surfaces at real PCB coordinates."""
        for obj in self.doc.Objects:
            obj.ViewObject.Visibility = False
        if curved:
            radius, span, length = 2.0, 100.0, 4.4
            mid = math.radians(span / 2)
            normal = App.Vector(math.cos(mid), math.sin(mid), 0)
            tangent = App.Vector(-math.sin(mid), math.cos(mid), 0)
            center = App.Vector(135.69, -75.63, -2.70)
            base = center - normal * radius - App.Vector(0, 0, length / 2)

            def cylinder_patch(start, width):
                cylinder = Part.makeCylinder(
                    radius, width, base + App.Vector(0, 0, start),
                    App.Vector(0, 0, 1), span)
                return next(face for face in cylinder.Faces
                            if isinstance(face.Surface, Part.Cylinder)).copy()

            mask_shape = cylinder_patch(0, length)
            silk_shape = Part.makeCompound([
                cylinder_patch(start, 0.18)
                for start in (0.4, 1.2, 2.0, 2.8, 3.6)])
            viewport_width = 4.33
        else:
            center = App.Vector(175, -77, 17)
            rotation = App.Rotation(App.Vector(1, 2, 1), 47)
            normal = rotation.multVec(App.Vector(0, 0, 1))
            tangent = rotation.multVec(App.Vector(0, 1, 0))

            def rectangle(x0, y0, x1, y1):
                # Bake double-precision global vertices into each face; the
                # native viewer then independently converts these to floats.
                points = [center + rotation.multVec(App.Vector(x, y, 0))
                          for x, y in ((x0, y0), (x1, y0),
                                       (x1, y1), (x0, y1), (x0, y0))]
                return Part.Face(Part.makePolygon(points))

            mask_shape = rectangle(-3, -3, 3, 3)
            cuts = (-2.2, -1.87, -1.05, -0.12, 0.08, 0.8, 1.47, 2.2)
            silk_shape = Part.makeCompound([
                rectangle(x, y0, x + 0.18, y1)
                for x in (-1.8, -0.9, 0, 0.9, 1.8)
                for y0, y1 in zip(cuts, cuts[1:])])
            viewport_width = 5.2

        mask = self._feature('StressMask', mask_shape, (0.05, 0.60, 0.10),
                             transparency=50)
        # Magenta distinguishes silk from the white screenshot background.
        # This changes only the material, not the depth/rendering path.
        silk = self._feature('StressSilk', silk_shape, (0.95, 0.05, 0.95))
        if curved:
            # At radius 2 mm, 1-/2-degree chords have approximately 0.076/
            # 0.305 micrometre sag. Keep the mismatch physically small and
            # give the mask the finer mesh, which can cover coarse silk.
            mask.ViewObject.Deviation = 0.01
            mask.ViewObject.AngularDeflection = 1.0
            silk.ViewObject.Deviation = 0.02
            silk.ViewObject.AngularDeflection = 2.0
        self.doc.recompute()
        self.assertTrue(self.pcb._apply_surface_render_state(
            mask.ViewObject, 'F.Mask'))
        self.assertTrue(self.pcb._apply_surface_render_state(
            silk.ViewObject, 'F.SilkS', silkscreen=True))
        return mask, silk, center, normal, tangent, viewport_width

    def _assert_silk_survives_coincident_mask(self, reference, composited,
                                             mask, silk, label):
        def is_silk(image, x, y):
            pixel = image.pixel(x, y)
            return (((pixel >> 16) & 255) > 180
                    and ((pixel >> 8) & 255) < 70
                    and (pixel & 255) > 180)

        interior_count = 0
        missing = []
        for y in range(2, reference.height() - 2):
            for x in range(2, reference.width() - 2):
                if not all(is_silk(reference, x + dx, y + dy)
                           for dx, dy in ((0, 0), (-1, 0), (1, 0),
                                          (0, -1), (0, 1))):
                    continue
                interior_count += 1
                if not is_silk(composited, x, y):
                    missing.append((x, y))
        record = self.capture_records[int(composited.cacheKey())]
        record['silk_coverage'] = {
            'fixture': label,
            'interior_pixels': interior_count,
            'missing_pixels': len(missing),
            'missing_fraction': len(missing) / max(interior_count, 1),
            'first_missing_pixels': missing[:30],
            'native_triangle_counts': {
                'mask': self._native_triangle_counts(mask),
                'silk': self._native_triangle_counts(silk),
            },
            'deviation': {
                'mask': float(mask.ViewObject.Deviation),
                'silk': float(silk.ViewObject.Deviation),
            },
            'angular_deflection': {
                'mask': float(mask.ViewObject.AngularDeflection),
                'silk': float(silk.ViewObject.AngularDeflection),
            },
        }
        Path(record['image']).with_suffix('.json').write_text(
            json.dumps(record, indent=2))
        self.assertGreater(interior_count, 100,
                           '{} must contain visible silk interiors'.format(label))
        self.assertLessEqual(
            len(missing), max(8, int(interior_count * 0.001)),
            '{} lost {}/{} interior silk pixels; {}'.format(
                label, len(missing), interior_count, record['image']))

    def _check_grazing_surfaces(self, curved):
        mask, silk, center, normal, tangent, width = self._stress_surfaces(curved)
        cad_state = self._cad_state()
        label = 'curved' if curved else 'planar'
        for angle in (6, 12):
            with self.subTest(fixture=label, grazing_degrees=angle):
                image_size = self._grazing_camera(
                    center, normal, tangent, width, angle)
                mask.ViewObject.Visibility = False
                reference = self._capture(
                    '{}-{}deg-silk-only'.format(label, angle), size=image_size)
                mask.ViewObject.Visibility = True
                composited = self._capture(
                    '{}-{}deg-masked'.format(label, angle), size=image_size)
                self._assert_silk_survives_coincident_mask(
                    reference, composited, mask, silk, label)
        self.assertEqual(self._cad_state(), cad_state)

    def test_global_tilted_plane_preserves_silk_at_grazing_angles(self):
        self._check_grazing_surfaces(curved=False)

    def test_independent_cylinder_meshes_preserve_silk_at_grazing_angles(self):
        self._check_grazing_surfaces(curved=True)

    def test_camera_only_orbit_does_not_reuse_stale_surface_bias(self):
        mask, silk, center, normal, tangent, width = self._stress_surfaces(False)
        cad_state = self._cad_state()
        self.view.setCameraType('Perspective')
        camera = self.view.getCameraNode()
        # Looking from opposite tangential directions makes a cached camera-
        # facing displacement point away from the next view, not merely vary
        # its magnitude slightly. Both views still face the front surface.
        poses = ((6, 1), (12, -1), (30, 1))
        references = {}
        mask.ViewObject.Visibility = False
        for angle, side in poses:
            image_size = self._grazing_camera(
                center, normal, tangent * side, width, angle, camera=camera)
            references[(angle, side)] = self._capture(
                'orbit-{}deg-side{}-reference'.format(angle, side), size=image_size)

        mask.ViewObject.Visibility = True
        # From here to the end, only fields of this same camera node change.
        # Do not toggle visibility, touch materials, or rebuild scene nodes.
        for angle, side in (*poses, poses[0]):
            with self.subTest(grazing_degrees=angle, viewing_side=side):
                image_size = self._grazing_camera(
                    center, normal, tangent * side, width, angle, camera=camera)
                composited = self._capture(
                    'orbit-{}deg-side{}-masked'.format(angle, side), size=image_size)
                self._assert_silk_survives_coincident_mask(
                    references[(angle, side)], composited, mask, silk,
                    'camera-only orbit {}deg side{}'.format(angle, side))
        self.assertEqual(self._cad_state(), cad_state)

    def test_surface_bias_respects_rotated_and_translated_parent(self):
        mask, silk, center, normal, tangent, width = self._stress_surfaces(False)
        parent = self.doc.addObject('App::Part', 'SurfaceParent')
        parent.addObject(mask)
        parent.addObject(silk)
        # A large rotation makes a world-space vector incorrectly applied in
        # model space move the coating behind the visible surface.
        placement = App.Placement(
            App.Vector(13, -8, 4), App.Rotation(normal, 143))
        parent.Placement = placement
        self.doc.recompute()
        center = placement.multVec(center)
        normal = placement.Rotation.multVec(normal)
        tangent = placement.Rotation.multVec(tangent)
        cad_state = self._cad_state()
        self.view.setCameraType('Perspective')
        camera = self.view.getCameraNode()
        for angle in (6, 12):
            with self.subTest(grazing_degrees=angle):
                image_size = self._grazing_camera(
                    center, normal, tangent, width, angle, camera=camera)
                mask.ViewObject.Visibility = False
                reference = self._capture(
                    'parent-{}deg-reference'.format(angle), size=image_size)
                mask.ViewObject.Visibility = True
                composited = self._capture(
                    'parent-{}deg-masked'.format(angle), size=image_size)
                self._assert_silk_survives_coincident_mask(
                    reference, composited, mask, silk,
                    'transformed parent {}deg'.format(angle))
        self.assertEqual(self._cad_state(), cad_state)

    def test_global_flat_mask_composes_over_copper_with_tight_clipping(self):
        mask, silk, center, normal, tangent, width = self._stress_surfaces(False)
        silk.ViewObject.Visibility = False
        horizontal = tangent.cross(normal)

        def rectangle(x0, y0, x1, y1, depth):
            points = [center + horizontal * x + tangent * y - normal * depth
                      for x, y in ((x0, y0), (x1, y0), (x1, y1),
                                   (x0, y1), (x0, y0))]
            return Part.Face(Part.makePolygon(points))

        # Match the actual flat PCB's separations, while independently
        # tessellating its copper into differently positioned triangles.
        copper = self._feature('StressCopper', Part.makeCompound([
            rectangle(x, y, x + 2, y + 2, 0.010)
            for x in (-3, -1, 1) for y in (-3, -1, 1)]),
            (0.85, 0.25, 0.05))
        substrate = self._feature(
            'StressSubstrate', rectangle(-3, -3, 3, 3, 0.028).extrude(
                normal * -0.3), (0.75, 0.75, 0.75), transparency=37,
            substrate=True)
        self.doc.recompute()
        self.assertTrue(self.pcb._apply_surface_render_state(
            copper.ViewObject, 'F.Cu'))
        cad_state = self._cad_state()

        def rgb(image, x, y):
            pixel = image.pixel(x, y)
            return ((pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255)

        def is_copper(image, x, y):
            red, green, blue = rgb(image, x, y)
            return red > 170 and green < 100 and blue < 45

        def is_mask(image, x, y):
            red, green, blue = rgb(image, x, y)
            return red < 50 and green > 100 and blue < 60

        for distance, near, angle in ((2.8, 0.002, 12), (30, 0.002, 60),
                                      (75, 0.0001, 12), (75, 0.02, 60)):
            with self.subTest(eye_distance=distance, near=near, tilt=angle):
                size = self._grazing_camera(center, normal, tangent, width, angle)
                camera = self.view.getCameraNode()
                outward = (normal * math.sin(math.radians(angle))
                           + tangent * math.cos(math.radians(angle)))
                position = center + outward * distance
                camera.position.setValue(position.x, position.y, position.z)
                camera.focalDistance.setValue(distance)
                # Keep the same close framing while changing the eye distance,
                # so a precision failure is not hidden by subpixel geometry.
                camera.heightAngle.setValue(2 * math.atan(
                    math.tan(math.radians(45) / 2) * 2.8 / distance))
                self.near_distance = near
                label = 'flat-eye{}-near{}-{}deg'.format(distance, near, angle)

                substrate.ViewObject.Visibility = False
                mask.ViewObject.Visibility = False
                copper.ViewObject.Visibility = True
                copper_only = self._capture(label + '-copper-only', size=size)
                substrate.ViewObject.Visibility = True
                copper_with_body = self._capture(label + '-copper-with-body', size=size)
                substrate.ViewObject.Visibility = False
                copper.ViewObject.Visibility = False
                mask.ViewObject.Visibility = True
                mask.ViewObject.Transparency = 0
                mask_only = self._capture(label + '-opaque-mask-only', size=size)
                copper.ViewObject.Visibility = True
                # Diagnose the two interfaces separately: substrate showing
                # through opaque copper also changes the eventual mask color.
                substrate.ViewObject.Visibility = False
                mask.ViewObject.Transparency = 50
                composited = self._capture(label + '-mask-over-copper', size=size)

                with self.subTest(interface='copper/substrate'):
                    self._assert_copper_occludes_substrate(
                        copper_only, copper_with_body, label)

                checked = 0
                mismatches = []
                for y in range(3, size[1] - 3):
                    for x in range(3, size[0] - 3):
                        # Exclude silhouette/antialiasing pixels in both
                        # controls. Test interiors across all native triangles,
                        # rather than sampling one triangle's median color.
                        if not all(is_copper(copper_only, x + dx, y + dy)
                                   and is_mask(mask_only, x + dx, y + dy)
                                   for dx, dy in ((0, 0), (-3, 0), (3, 0),
                                                  (0, -3), (0, 3))):
                            continue
                        checked += 1
                        expected = tuple((a + b) / 2 for a, b in zip(
                            rgb(copper_only, x, y), rgb(mask_only, x, y)))
                        actual = rgb(composited, x, y)
                        if max(abs(a - b) for a, b in zip(actual, expected)) > 12:
                            mismatches.append((x, y))
                record = self.capture_records[int(composited.cacheKey())]
                record['mask_copper_composition'] = {
                    'eye_distance_mm': distance,
                    'mask_to_copper_mm': 0.010,
                    'mask_to_substrate_mm': 0.028,
                    'checked_pixels': checked,
                    'incorrect_pixels': len(mismatches),
                    'first_incorrect_pixels': mismatches[:30],
                    # saveImage may use a framebuffer with a different depth
                    # format from the live viewport. Preserve this limitation.
                    'capture_method': 'FreeCAD activeView.saveImage',
                }
                Path(record['image']).with_suffix('.json').write_text(
                    json.dumps(record, indent=2))
                self.assertGreater(checked, 200, label + ' lacks control overlap')
                self.assertLessEqual(
                    len(mismatches), max(8, int(checked * 0.001)),
                    '{}: {}/{} mask/copper pixels have incorrect color; {}'.format(
                        label, len(mismatches), checked, record['image']))
        self.assertEqual(self._cad_state(), cad_state)

    def _assert_copper_occludes_substrate(self, reference, composited, label):
        def rgb(image, x, y):
            pixel = image.pixel(x, y)
            return ((pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255)

        def is_copper(x, y):
            red, green, blue = rgb(reference, x, y)
            return red > 170 and green < 100 and blue < 45

        checked = 0
        incorrect = []
        colors = {}
        for y in range(3, reference.height() - 3):
            for x in range(3, reference.width() - 3):
                if not all(is_copper(x + dx, y + dy)
                           for dx, dy in ((0, 0), (-3, 0), (3, 0),
                                          (0, -3), (0, 3))):
                    continue
                checked += 1
                expected = rgb(reference, x, y)
                actual = rgb(composited, x, y)
                if max(abs(a - b) for a, b in zip(actual, expected)) > 12:
                    incorrect.append((x, y))
                    colors[actual] = colors.get(actual, 0) + 1
        record = self.capture_records[int(composited.cacheKey())]
        record['copper_substrate_occlusion'] = {
            'copper_to_substrate_mm': 0.018,
            'checked_pixels': checked,
            'incorrect_pixels': len(incorrect),
            'first_incorrect_pixels': incorrect[:30],
            'incorrect_colors': [
                {'rgb': color, 'count': count}
                for color, count in sorted(colors.items(),
                                           key=lambda item: -item[1])[:8]],
            'capture_method': 'FreeCAD activeView.saveImage',
        }
        Path(record['image']).with_suffix('.json').write_text(
            json.dumps(record, indent=2))
        self.assertGreater(checked, 200, label + ' lacks visible copper interiors')
        self.assertLessEqual(
            len(incorrect), max(8, int(checked * 0.001)),
            '{}: substrate changed {}/{} opaque copper pixels; {}'.format(
                label, len(incorrect), checked, record['image']))

    def test_global_flat_copper_occludes_substrate_around_drill_holes(self):
        for obj in self.doc.Objects:
            obj.ViewObject.Visibility = False
        center = App.Vector(175, -77, 17)
        # Measured normal of the user's affected flat board region. The eye
        # is nearly normal to this plane, rather than grazing a bent surface.
        normal = App.Vector(-0.856722146929626, -0.12659843190297582, 0.5)
        rotation = App.Rotation(App.Vector(0, 0, 1), normal)
        tangent = rotation.multVec(App.Vector(0, 1, 0))
        top = self._plane(-3, -3, 6, 6, z=0)
        pads = []
        for x, y, radius in ((-1.5, -1.3, 0.23), (1.2, -1.4, 0.31),
                             (-1.1, 1.4, 0.27), (1.5, 1.2, 0.20)):
            axis = App.Vector(x, y, 0)
            hole = Part.Face(Part.Wire(Part.makeCircle(radius, axis)))
            pad = Part.Face(Part.Wire(Part.makeCircle(0.78, axis)))
            pads.append(pad.cut(hole))
            top = top.cut(hole)

        def positioned(shape, depth):
            # Bake the transform into the BREP's geometry, so native float
            # vertices retain the real board's large global coordinates.
            return shape.transformGeometry(App.Placement(
                center - normal * depth, rotation).toMatrix())

        copper = self._feature('DrilledCopper', positioned(
            Part.makeCompound(pads), 0), (0.85, 0.25, 0.05))
        substrate = self._feature('DrilledSubstrate', positioned(
            top.extrude(App.Vector(0, 0, -0.3)), 0.018),
            (0.75, 0.75, 0.75), transparency=37, substrate=True)
        # Separate circular boundaries produce different native triangles in
        # the annular pads and the larger board face surrounding the drills.
        copper.ViewObject.Deviation = 0.02
        copper.ViewObject.AngularDeflection = 5.0
        substrate.ViewObject.Deviation = 0.08
        substrate.ViewObject.AngularDeflection = 20.0
        self.doc.recompute()
        self.assertTrue(self.pcb._apply_surface_render_state(
            copper.ViewObject, 'F.Cu'))
        cad_state = self._cad_state()
        distance = 19.340759
        self.far_distance = 50.101582
        for near in (0.012207392, 1.0):
            with self.subTest(eye_distance=distance, near=near):
                angle = 86.85831
                size = self._grazing_camera(center, normal, tangent, 5.2, angle)
                camera = self.view.getCameraNode()
                outward = (normal * math.sin(math.radians(angle))
                           + tangent * math.cos(math.radians(angle)))
                position = center + outward * distance
                camera.position.setValue(position.x, position.y, position.z)
                camera.focalDistance.setValue(distance)
                camera.heightAngle.setValue(2 * math.atan(
                    math.tan(math.radians(45) / 2) * 2.8 / distance))
                self.near_distance = near
                label = 'drilled-eye{}-near{}'.format(distance, near)
                substrate.ViewObject.Visibility = False
                reference = self._capture(label + '-copper-only', size=size)
                substrate.ViewObject.Visibility = True
                composited = self._capture(label + '-copper-with-body', size=size)
                self._assert_copper_occludes_substrate(reference, composited, label)
        self.assertEqual(self._cad_state(), cad_state)

    def test_substrate_still_occludes_buried_patch_except_through_hole(self):
        for obj in self.doc.Objects:
            obj.ViewObject.Visibility = False
        center = App.Vector(175, -77, 17)
        normal = App.Vector(-0.856722146929626, -0.12659843190297582, 0.5)
        rotation = App.Rotation(App.Vector(0, 0, 1), normal)
        tangent = rotation.multVec(App.Vector(0, 1, 0))
        horizontal = rotation.multVec(App.Vector(1, 0, 0))

        def positioned(shape, depth):
            return shape.transformGeometry(App.Placement(
                center - normal * depth, rotation).toMatrix())

        top = self._plane(-3, -3, 6, 6, z=0)
        hole = Part.Face(Part.Wire(Part.makeCircle(0.45)))
        substrate = self._feature('OccludingSubstrate', positioned(
            top.cut(hole).extrude(App.Vector(0, 0, -0.3)), 0),
            (0.75, 0.75, 0.75), substrate=True)
        patches = []
        for name, depth in (('FrontBuriedPatch', 0.1), ('BackBuriedPatch', 0.2)):
            shape = positioned(self._plane(-2, -2, 4, 4, z=0), depth)
            if name.startswith('Back'):
                shape.reverse()
            patches.append(self._feature(name, shape, (0.10, 0.15, 0.85)))
        self.doc.recompute()
        cad_state = self._cad_state()
        distance = 19.340759
        self.far_distance = 50.101582

        for side in (1, -1):
            patch = patches[0 if side == 1 else 1]
            patches[1 if side == 1 else 0].ViewObject.Visibility = False
            cap_center = center if side == 1 else center - normal * 0.3
            buried_center = cap_center - normal * (side * 0.1)
            sample_points = {
                'covered': tuple(buried_center + horizontal * 1.0),
                'through_hole': tuple(buried_center),
            }
            # The real camera and a well-resolved near plane are asserted.
            # An extreme .0001 near plane is captured for diagnosis only: at
            # this eye distance the .1 mm gap can fall below depth precision.
            near_values = (0.012207392, 1.0, 0.0001) if side == 1 else (0.012207392, 1.0)
            for near in near_values:
                diagnostic_only = near == 0.0001
                with self.subTest(side=side, near=near,
                                  diagnostic_only=diagnostic_only):
                    angle = 86.85831
                    size = self._grazing_camera(
                        cap_center, normal * side, tangent, 5.2, angle)
                    camera = self.view.getCameraNode()
                    outward = (normal * side * math.sin(math.radians(angle))
                               + tangent * math.cos(math.radians(angle)))
                    position = cap_center + outward * distance
                    camera.position.setValue(position.x, position.y, position.z)
                    camera.focalDistance.setValue(distance)
                    camera.heightAngle.setValue(2 * math.atan(
                        math.tan(math.radians(45) / 2) * 2.8 / distance))
                    self.near_distance = near
                    label = 'buried-side{}-near{}'.format(side, near)
                    patch.ViewObject.Visibility = True
                    substrate.ViewObject.Visibility = False
                    reference = self._capture(label + '-patch-only', size=size)
                    patch.ViewObject.Visibility = False
                    substrate.ViewObject.Visibility = True
                    substrate.ViewObject.Transparency = 0
                    body_only = self._capture(label + '-body-only', size=size)
                    patch.ViewObject.Visibility = True
                    opaque = self._capture(label + '-opaque-body', size=size)
                    substrate.ViewObject.Transparency = 37
                    transparent = self._capture(label + '-transparent-body', size=size)

                    patch_color = self._sample(reference, sample_points['covered'])
                    body_color = self._sample(body_only, sample_points['covered'])
                    opaque_color = self._sample(opaque, sample_points['covered'])
                    transparent_color = self._sample(
                        transparent, sample_points['covered'])
                    expected_tint = tuple(0.63 * b + 0.37 * p
                                          for b, p in zip(body_color, patch_color))
                    hole_reference = self._sample(reference, sample_points['through_hole'])
                    opaque_hole = self._sample(opaque, sample_points['through_hole'])
                    transparent_hole = self._sample(
                        transparent, sample_points['through_hole'])
                    record = self.capture_records[int(transparent.cacheKey())]
                    record['buried_patch_occlusion'] = {
                        'buried_depth_mm': 0.1,
                        'side': 'front' if side == 1 else 'back',
                        'diagnostic_only': diagnostic_only,
                        'patch_rgb': patch_color,
                        'body_rgb': body_color,
                        'opaque_rgb': opaque_color,
                        'transparent_rgb': transparent_color,
                        'expected_transparent_rgb': expected_tint,
                        'hole_reference_rgb': hole_reference,
                        'opaque_hole_rgb': opaque_hole,
                        'transparent_hole_rgb': transparent_hole,
                    }
                    Path(record['image']).with_suffix('.json').write_text(
                        json.dumps(record, indent=2))
                    if diagnostic_only:
                        continue
                    self.assertGreater(max(abs(a - b) for a, b in zip(
                        patch_color, body_color)), 40,
                        'buried patch and body controls must differ')
                    self._assert_rgb_close(opaque_color, body_color,
                                           label + ' opaque cap hides buried patch')
                    self._assert_rgb_close(transparent_color, expected_tint,
                                           label + ' transparent cap tints buried patch')
                    self._assert_rgb_close(opaque_hole, hole_reference,
                                           label + ' opaque body preserves hole')
                    self._assert_rgb_close(transparent_hole, hole_reference,
                                           label + ' transparent body preserves hole')
        self.assertEqual(self._cad_state(), cad_state)

    def test_mask_preserves_alpha_over_substrate_and_copper(self):
        for perspective, tilt, near in (
                (False, 0, 1.0), (True, 30, 1.0), (True, 60, 0.02)):
            with self.subTest(perspective=perspective, tilt=tilt, near=near):
                self._camera(perspective, tilt, near)
                self.mask.ViewObject.Visibility = False
                under = self._capture('under')
                self.mask.ViewObject.Visibility = True
                self.mask.ViewObject.Transparency = 0
                opaque = self._capture('opaque')
                self.mask.ViewObject.Transparency = 50
                blended = self._capture('blended')
                for label, point in (
                        ('substrate', (5, -5, 1)),
                        ('copper', (-5, -5, 1))):
                    background = self._sample(under, point)
                    foreground = self._sample(opaque, point)
                    result = self._sample(blended, point)
                    self.assertGreater(
                        max(abs(a - b) for a, b in zip(background, foreground)),
                        40, '{} controls must differ: {} vs {}'.format(
                            label, background, foreground))
                    expected = tuple((a + b) / 2 for a, b
                                     in zip(background, foreground))
                    self._assert_rgb_close(result, expected, label)
        self.assertEqual(self._cad_state(), self.original_cad_state)

    def test_tight_near_plane_preserves_silk_and_component_occlusion(self):
        # Return to the first view at the end to catch stale Coin render state.
        for tilt, near in ((0, 0.002), (30, 0.02), (60, 1.0), (0, 0.002)):
            with self.subTest(tilt=tilt, near=near):
                self._camera(True, tilt, near)
                self.board.ViewObject.Visibility = False
                self.mask.ViewObject.Visibility = False
                self.silk.ViewObject.Visibility = False
                component_reference = self._capture('component-reference')
                self.silk.ViewObject.Visibility = True
                reference = self._capture('opaque-reference')
                self.board.ViewObject.Visibility = True
                self.mask.ViewObject.Visibility = True
                composited = self._capture('all-layers')
                for label, point, control in (
                        ('silk', (3, 3, 1), reference),
                        ('raised component', (5.5, 5.5, 2), component_reference)):
                    expected = self._sample(control, point)
                    self._assert_rgb_close(
                        self._sample(composited, point), expected, label)
                # The translucent substrate just below must not wash out copper.
                self.mask.ViewObject.Visibility = False
                copper = self._capture('copper-with-substrate')
                self._assert_rgb_close(
                    self._sample(copper, (-5, -5, 1)),
                    self._sample(reference, (-5, -5, 1)), 'outer copper')
        self.assertEqual(self._cad_state(), self.original_cad_state)

    def test_transparent_component_still_tints_underlying_silk(self):
        self.component.ViewObject.Transparency = 50
        point = (5.5, 5.5, 2)
        for tilt, near in ((0, 0.002), (30, 0.02)):
            with self.subTest(tilt=tilt, near=near):
                self._camera(True, tilt, near)
                self.board.ViewObject.Visibility = False
                self.mask.ViewObject.Visibility = False
                reference = self._capture('transparent-component-reference')
                self.board.ViewObject.Visibility = True
                self.mask.ViewObject.Visibility = True
                composited = self._capture('transparent-component-all-layers')
                expected = self._sample(reference, point)
                actual = self._sample(composited, point)
                # A silk-only delayed overlay can pass opaque occlusion tests
                # yet overwrite this transparent component with white.
                self.assertGreater(expected[2] - expected[0], 40,
                                   'reference must show blue tint over silk')
                self.assertGreater(actual[2] - actual[0], 40,
                                   'silk must remain below transparent component')
                self._assert_rgb_close(actual, expected, 'transparent component')
        self.assertEqual(self._cad_state(), self.original_cad_state)


def run_tests(stream=None):
    """Run safely from a macro/GUI console without consuming FreeCAD argv."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SurfaceRenderNativeTests)
    return unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)


if __name__ == '__main__':
    run_tests()
