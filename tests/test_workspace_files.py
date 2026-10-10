"""Workspace project-path normalization and file enumeration."""

import tempfile
import unittest
from pathlib import Path

from kikakuka.common import (findFiles, kicad_project_path, workspace_entry_path,
                    workspace_filename)


class WorkspaceFileTests(unittest.TestCase):
    def test_board_and_schematic_represent_the_sibling_project(self):
        for selected in (
                "/boards/main.kicad_pro",
                "/boards/main.kicad_pcb",
                "/boards/main.kicad_sch",
                "/boards/main.kicad_prl"):
            with self.subTest(selected=selected):
                self.assertEqual(
                    kicad_project_path(selected),
                    "/boards/main.kicad_pro",
                )

    def test_project_tree_label_hides_suffix_only_when_project_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "main.kicad_pro"
            self.assertEqual(workspace_filename(project), "main")
            project.touch()
            self.assertEqual(workspace_filename(project), "main.kicad_pro")

    def test_nonproject_tree_label_keeps_its_suffix(self):
        self.assertEqual(
            workspace_filename("/boards/main.kicad_pcb"), "main.kicad_pcb"
        )

    def test_existing_board_can_add_a_missing_project_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / "main.kicad_pcb"
            board.touch()

            self.assertEqual(
                workspace_entry_path(board),
                str(Path(directory) / "main.kicad_pro"),
            )
            self.assertFalse(Path(directory, "main.kicad_pro").exists())

    def test_missing_selected_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(
                workspace_entry_path(Path(directory) / "missing.kicad_sch")
            )

    def test_missing_board_and_schematic_are_not_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "main.kicad_pro"
            board = root / "main.kicad_pcb"
            board.touch()
            workspace = {"projects": [{"path": str(project)}]}

            findFiles(workspace, str(root))

            self.assertEqual(
                [entry["path"] for entry in workspace["projects"][0]["files"]],
                [str(board)],
            )


if __name__ == "__main__":
    unittest.main()
