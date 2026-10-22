"""Verify the poster is captured from the main Window after reconciliation."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class WorkspacePosterTests(unittest.TestCase):
    def test_window_ref_is_resolved_after_sync_and_defers_dialog(self):
        source = Path(__file__).resolve().parents[1] / 'kikakuka/workspace.py'
        tree = ast.parse(source.read_text())
        queued = []
        refs = {}
        node = SimpleNamespace(get_poster=Mock(return_value=queued.append))

        class DesktopApplication:
            def sync(self):
                refs['main'] = SimpleNamespace(node=node)

        main = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MainUI')
        sync_method = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == 'sync')
        harness = ast.ClassDef(name='MainUI', bases=[ast.Name(id='DesktopApplication', ctx=ast.Load())],
                               keywords=[], body=[sync_method], decorator_list=[])
        notify = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                      and n.name == '_show_window_activation_error')
        ns = dict(DesktopApplication=DesktopApplication, PUI=SimpleNamespace(ref=refs.__getitem__),
                  Critical=Mock(), _window_activation_error_post=None)
        module = ast.fix_missing_locations(ast.Module(body=[harness, notify], type_ignores=[]))
        exec(compile(module, str(source), 'exec'), ns)
        ui = ns['MainUI']()
        ui._apply_application_events = Mock()
        ui.sync()
        ui._apply_application_events.assert_called_once()
        node.get_poster.assert_called_once()
        ns['_show_window_activation_error']('Test error')
        ns['Critical'].assert_not_called()
        self.assertEqual(len(queued), 1)
        queued.pop()()
        ns['Critical'].assert_called_once_with('Test error', 'Cannot Bring Window to Front')
        self.assertNotIn('self.get_poster()', ast.unparse(main))
        self.assertIn(".ref('main')", ast.unparse(main))


if __name__ == '__main__':
    unittest.main()
