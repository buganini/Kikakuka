import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import kicad_paths


def load_module():
    path = Path(__file__).resolve().parents[1] / 'FreekiCAD/freecad/FreekiCAD/ImportFingerprint.py'
    spec = importlib.util.spec_from_file_location('fingerprint_unit.ImportFingerprint', path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {'fingerprint_unit.kicad_paths': kicad_paths}):
        spec.loader.exec_module(module)
    return module


class ImportFingerprintTests(unittest.TestCase):
    def test_live_content_external_models_and_settings_invalidate(self):
        module = load_module()
        board = mock.Mock()
        board.get_as_string.return_value = '(kicad_pcb unsaved-content)'
        variables = types.SimpleNamespace(variables={'TITLE': 'before'})
        board.get_project.return_value.get_text_variables.return_value = variables
        model = types.SimpleNamespace(filename='model.step', visible=True)
        board.get_footprints.return_value = [types.SimpleNamespace(
            definition=types.SimpleNamespace(models=[model]))]
        with tempfile.TemporaryDirectory() as directory:
            filename = str(Path(directory) / 'board.kicad_pcb')
            path = Path(directory) / 'model.step'
            settings = {'ImportOuterCopper': True}
            with mock.patch.object(module, 'path_variables', return_value={}), \
                    mock.patch.object(module, 'resolve_model_path',
                                      side_effect=lambda *a, **k: path if path.exists() else None):
                def read():
                    return module.read_fingerprint(None, board, filename, settings, None)
                before = read()
                self.assertEqual(before, read())
                board.get_as_string.return_value += ' changed-without-saving'
                changed = read()
                self.assertNotEqual(before, changed)
                path.write_text('newly installed model')
                installed = read()
                self.assertNotEqual(changed, installed)
                path.write_text('replaced model with new contents')
                replaced = read()
                self.assertNotEqual(installed, replaced)
                path.unlink()
                self.assertEqual(changed, read())
                settings['ImportOuterCopper'] = False
                configured = read()
                self.assertNotEqual(changed, configured)
                variables.variables['TITLE'] = 'after'
                self.assertNotEqual(configured, read())
                board.get_as_string.side_effect = RuntimeError('unsupported API')
                with self.assertRaises(RuntimeError):
                    read()
        board.save.assert_not_called()
