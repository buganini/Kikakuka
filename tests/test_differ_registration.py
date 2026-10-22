import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


class DifferRegistrationTests(unittest.TestCase):
    def test_register_update_and_unregister_follow_ui_lifetime(self):
        source = Path(__file__).resolve().parents[1] / 'kikakuka/differ.py'
        tree = ast.parse(source.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DifferUI')
        methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
        node = mock.Mock()

        class DesktopApplication:
            def run(self):
                node.register_application.assert_called_once_with('Differ', ('/a.kicad_pcb', ''))
                self.state.source_b = '/b.kicad_pcb'
                self._register_instance()
                node.register_application.assert_called_with('Differ', ('/a.kicad_pcb', '/b.kicad_pcb'))
                node.unregister_application.assert_not_called()
                return 0

        cls.body = [methods['run'], methods['_register_instance']]
        namespace = {'DesktopApplication': DesktopApplication}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), 'exec'), namespace)
        ui = namespace['DifferUI']()
        ui.state = SimpleNamespace(source_a='/a.kicad_pcb', source_b='')
        with mock.patch('cadhoc.mesh.start_node', return_value=node):
            self.assertEqual(ui.run(), 0)
        node.unregister_application.assert_called_once()
        self.assertIn('self._register_instance()', ast.unparse(methods['content']))
