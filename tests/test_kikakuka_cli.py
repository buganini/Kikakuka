import runpy
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "kikakuka.py"


class KikakukaCliTest(unittest.TestCase):
    def test_open_mode_is_dispatched_before_ui_imports(self):
        pcb_open = types.ModuleType("pcb_open")
        pcb_open.open_requested_kicad_files = mock.Mock(return_value=True)
        arguments = ["--open", "--fresh", "/boards/main.kicad_pcb"]

        blocked_ui_modules = {
            "differ": None,
            "workspace": None,
            "panelizer": None,
            "gerber": None,
        }
        with mock.patch.object(sys, "argv", [str(SCRIPT), *arguments]), \
                mock.patch.dict(
                    sys.modules,
                    {"pcb_open": pcb_open, **blocked_ui_modules},
                ):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(SCRIPT), run_name="__main__")

        self.assertEqual(exited.exception.code, 0)
        pcb_open.open_requested_kicad_files.assert_called_once_with(arguments)


if __name__ == "__main__":
    unittest.main()
