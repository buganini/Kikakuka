"""Run with FreeCAD's Python for real Sketcher/OCC regression coverage."""
import ast
from pathlib import Path
import unittest
from unittest.mock import Mock

try:
    import FreeCAD
    import Part
    import Sketcher
except ImportError:
    FreeCAD = None


@unittest.skipIf(FreeCAD is None, 'requires FreeCAD Python with Sketcher')
class OutlineSketchNativeTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'FreekiCAD/freecad/FreekiCAD/PcbObject.py'
        tree = ast.parse(source.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PcbObject')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_build_outline_sketch')
        self.observer = Mock()
        namespace = {'FreeCAD': FreeCAD, 'Part': Part,
                     '_ensure_sketch_observer': lambda: self.observer}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
        self.build = namespace['_build_outline_sketch']
        self.doc = FreeCAD.newDocument('OutlineRegression')

    def tearDown(self):
        FreeCAD.closeDocument(self.doc.Name)

    def check_edges(self, edges, constraint_count):
        sketch = self.doc.addObject('Sketcher::SketchObject', 'Outline')
        self.build(None, sketch, edges)
        self.doc.recompute()
        self.assertEqual(sketch.ConstraintCount, constraint_count)
        self.assertEqual(sketch.solve(), 0)
        for edge, geo in zip(edges, sketch.Geometry):
            rebuilt = geo.toShape()
            for source, target in ((edge, rebuilt), (rebuilt, edge)):
                for point in source.discretize(Number=17):
                    self.assertLess(Part.Vertex(point).distToShape(target)[0], 1e-6)
            if isinstance(geo, Part.ArcOfCircle):
                mid = geo.value((geo.FirstParameter + geo.LastParameter) / 2)
                self.assertLess(Part.Vertex(mid).distToShape(edge)[0], 1e-6)
        self.observer.unsuppress.assert_called_once_with(sketch.Name)

    def test_reversed_arc_frame_and_unordered_edges(self):
        v = FreeCAD.Vector
        # Original RISC-V top-left arc, whose normal is -Z and local X is -X.
        a, m, b = v(114.3, -70.85, 0), v(114.592893, -70.142893, 0), v(115.3, -69.85, 0)
        c = v(120, -75, 0)
        self.check_edges([Part.makeLine(c, b), Part.Arc(a, m, b).toShape(),
                          Part.makeLine(a, c)], 3)

    def test_disconnected_rings_and_circle(self):
        v = FreeCAD.Vector
        edges = []
        for x in (0, 20):
            a, b, c = v(x, 0, 0), v(x+4, 0, 0), v(x, 4, 0)
            edges.extend([Part.makeLine(a, b), Part.makeLine(c, b), Part.makeLine(a, c)])
        edges.append(Part.makeCircle(2, v(40, 0, 0)))
        self.check_edges(edges, 6)

    def test_open_edges_are_not_forced_closed(self):
        v = FreeCAD.Vector
        self.check_edges([Part.makeLine(v(0,0,0), v(2,0,0)),
                          Part.makeLine(v(4,0,0), v(6,0,0))], 0)

    def test_observer_is_released_on_solver_failure(self):
        sketch = Mock(Name='Broken', Geometry=[])
        sketch.solve.side_effect = RuntimeError('solver failed')
        with self.assertRaises(RuntimeError):
            self.build(None, sketch, [])
        self.observer.unsuppress.assert_called_once_with('Broken')


if __name__ == '__main__':
    unittest.main()
