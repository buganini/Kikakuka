import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


class PanelizerOpenFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_path = Path(__file__).resolve().parents[1] / "panelizer.py"
        cls.tree = ast.parse(cls.source_path.read_text(encoding="utf-8"))
        panelizer_class = next(
            node for node in cls.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "PanelizerUI"
        )
        cls.open_method = next(
            node for node in panelizer_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "open_file"
        )
        cls.content_method = next(
            node for node in panelizer_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "content"
        )

    def _open_file(self, pcb):
        thread = mock.Mock()
        namespace = {
            "Thread": thread,
            "open_pcb_file": mock.sentinel.open_pcb_file,
        }
        module = ast.Module(body=[self.open_method], type_ignores=[])
        exec(compile(module, str(self.source_path), "exec"), namespace)
        namespace["open_file"](object(), None, pcb)
        return thread

    def test_opens_kicad_pcb_in_a_background_thread(self):
        path = "/boards/source.kicad_pcb"

        thread = self._open_file(SimpleNamespace(
            file_type="kicad", file=path))

        thread.assert_called_once_with(
            target=mock.sentinel.open_pcb_file,
            args=[path],
            daemon=True,
        )
        thread.return_value.start.assert_called_once_with()

    def test_does_not_open_gerber_or_non_pcb_sources(self):
        for pcb in (
            SimpleNamespace(file_type="gerber", file="/boards/gerbers"),
            SimpleNamespace(file_type="kicad", file="/boards/source.kicad_sch"),
        ):
            with self.subTest(pcb=pcb):
                thread = self._open_file(pcb)
                thread.assert_not_called()

    def test_open_file_button_precedes_duplicate_and_is_kicad_only(self):
        source = ast.get_source_segment(
            self.source_path.read_text(encoding="utf-8"),
            self.content_method,
        )

        open_button = 'Button("Open in KiCad").click(self.open_file, self.state.focus)'
        duplicate_button = 'Button("Duplicate").click(self.duplicate, self.state.focus)'
        condition = 'if self.state.focus.file_type == "kicad":'
        self.assertIn(condition, source)
        self.assertIn(open_button, source)
        self.assertLess(source.index(open_button), source.index(duplicate_button))


if __name__ == "__main__":
    unittest.main()
