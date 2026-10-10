"""Instance Manager controls without importing the desktop UI runtime."""

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


class WorkspaceInstanceManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_path = (
            Path(__file__).resolve().parents[1] / "kikakuka" / "workspace.py"
        )
        cls.source = cls.source_path.read_text(encoding="utf-8")
        tree = ast.parse(cls.source)
        main_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MainUI"
        )
        cls.methods = {
            node.name: node for node in main_class.body
            if isinstance(node, ast.FunctionDef)
        }

    def _method(self, name, namespace):
        module = ast.Module(body=[self.methods[name]], type_ignores=[])
        exec(compile(module, str(self.source_path), "exec"), namespace)
        return namespace[name]

    def test_kill_all_requires_confirmation_before_starting_worker(self):
        thread = mock.Mock()
        confirm = mock.Mock(return_value=False)
        handler = self._method("kill_all_instances", {
            "Confirm": confirm,
            "Thread": thread,
        })
        owner = SimpleNamespace(_kill_all_instances=mock.sentinel.worker)

        handler(owner)

        confirm.assert_called_once()
        thread.assert_not_called()

    def test_kill_all_runs_in_background_after_confirmation(self):
        thread = mock.Mock()
        handler = self._method("kill_all_instances", {
            "Confirm": mock.Mock(return_value=True),
            "Thread": thread,
        })
        owner = SimpleNamespace(_kill_all_instances=mock.sentinel.worker)

        handler(owner)

        thread.assert_called_once_with(
            target=mock.sentinel.worker, daemon=True)
        thread.return_value.start.assert_called_once_with()

    def test_kill_all_worker_refreshes_the_instance_list(self):
        process_control = SimpleNamespace(
            kill_all_cad_instances=mock.Mock(return_value=0))
        worker = self._method("_kill_all_instances", {})
        owner = SimpleNamespace(refresh_monitor=mock.Mock())

        with mock.patch.dict(
                sys.modules,
                {"kikakuka.process_control": process_control}):
            worker(owner)

        process_control.kill_all_cad_instances.assert_called_once_with()
        owner.refresh_monitor.assert_called_once_with()

    def test_kill_all_button_is_right_aligned_after_refresh(self):
        content = ast.get_source_segment(
            self.source, self.methods["content"])
        manager = content.split('with Tab("Instance Manager"):', 1)[1]
        manager = manager.split('with Tab("Integration"):', 1)[0]

        refresh = manager.index(
            'Button("Refresh").click(self.refresh_monitor)')
        spacer = manager.index("Spacer()", refresh)
        kill_all = manager.index(
            'Button("Close All").click(self.kill_all_instances)')
        self.assertLess(refresh, spacer)
        self.assertLess(spacer, kill_all)


if __name__ == "__main__":
    unittest.main()
