import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kicad_paths


class FakeBoard:
    def __init__(self, project_path=""):
        project = type("Project", (), {"path": project_path})()
        self.document = type("Document", (), {"project": project})()
        self.expand_calls = []

    def expand_text_variables(self, value, **kwargs):
        self.expand_calls.append((value, kwargs))
        return value


class LegacyBoard(FakeBoard):
    def expand_text_variables(self, value):
        self.expand_calls.append((value, {}))
        return value


class FakeKiCad:
    def __init__(self, binary=None):
        self.binary = binary

    def get_kicad_binary_path(self, _name):
        if self.binary is None:
            raise FileNotFoundError
        return str(self.binary)


class KiCadPathsTest(unittest.TestCase):
    def test_resolve_nested_variables_relative_to_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            model = project / "models" / "part.step"
            model.parent.mkdir()
            model.write_text("STEP", encoding="ascii")
            board = FakeBoard(str(project))

            resolved = kicad_paths.resolve_model_path(
                "${MODEL_ROOT}/${MODEL_FILE}",
                board,
                {
                    "KIPRJMOD": project,
                    "MODEL_ROOT": "models",
                    "MODEL_FILE": "part.step",
                },
            )

            self.assertEqual(resolved, model)
            self.assertEqual(
                board.expand_calls,
                [("${MODEL_ROOT}/${MODEL_FILE}", {"expand_env_vars": True})],
            )

    def test_resolve_supports_legacy_expand_signature(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model = Path(temp_dir) / "part.step"
            model.write_text("STEP", encoding="ascii")
            board = LegacyBoard()

            resolved = kicad_paths.resolve_model_path(
                "${MODEL}", board, {"MODEL": str(model)})

            self.assertEqual(resolved, model)
            self.assertEqual(board.expand_calls, [("${MODEL}", {})])

    def test_unresolved_variable_is_not_treated_as_a_path(self):
        self.assertIsNone(kicad_paths.resolve_model_path(
            "${MISSING}/part.step", FakeBoard(), {}))

    def test_prefer_step_selects_sibling_for_vrml(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            vrml = Path(temp_dir) / "part.wrl"
            step = Path(temp_dir) / "part.step"
            vrml.write_text("VRML", encoding="ascii")
            step.write_text("STEP", encoding="ascii")

            self.assertEqual(
                kicad_paths.resolve_model_path(
                    str(vrml), FakeBoard(), {}, prefer_step=True),
                step,
            )
            self.assertEqual(
                kicad_paths.resolve_model_path(
                    str(vrml), FakeBoard(), {}, prefer_step=False),
                vrml,
            )

    def test_path_variables_collect_project_pcm_config_and_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            config_file = config / "10.0" / "kicad_common.json"
            config_file.parent.mkdir(parents=True)
            config_file.write_text(
                '{"environment":{"vars":{"MODEL_BASE":"/models"}}}',
                encoding="utf-8",
            )
            source = (
                root / "data" / "10.0" / "3rdparty" / "package"
                / "plugins" / "kicad_paths.py"
            )
            source.parent.mkdir(parents=True)
            binary = root / "KiCad.app" / "Contents" / "MacOS" / "kicad-cli"
            binary.parent.mkdir(parents=True)
            binary.touch()
            model_dir = (
                root / "KiCad.app" / "Contents" / "SharedSupport"
                / "3dmodels"
            )
            model_dir.mkdir(parents=True)

            with mock.patch.object(
                    kicad_paths, "_config_directories", return_value=[config]), \
                    mock.patch.object(
                        kicad_paths, "_data_directories", return_value=[]), \
                    mock.patch.dict(os.environ, {}, clear=True):
                variables = kicad_paths.path_variables(
                    FakeKiCad(binary),
                    FakeBoard("/project"),
                    source_path=source,
                )

            self.assertEqual(variables["MODEL_BASE"], "/models")
            self.assertEqual(variables["KIPRJMOD"], "/project")
            self.assertEqual(
                variables["KICAD10_3RD_PARTY"],
                str((root / "data" / "10.0" / "3rdparty").resolve()),
            )
            self.assertEqual(
                variables["KICAD10_3DMODEL_DIR"], str(model_dir.resolve()))


if __name__ == "__main__":
    unittest.main()
