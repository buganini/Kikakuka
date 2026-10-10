import os
import tempfile
import unittest
from unittest.mock import patch

from kikakuka.file_location import file_location, open_file_location


class DifferFileLocationTests(unittest.TestCase):
    def test_file_location_uses_parent_for_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            filepath = os.path.join(temp_dir, "board.kicad_pcb")

            self.assertEqual(file_location(filepath), temp_dir)

    def test_file_location_keeps_directory_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(file_location(temp_dir), temp_dir)

    @patch("kikakuka.file_location.subprocess.run")
    @patch("kikakuka.file_location.platform.system", return_value="Darwin")
    def test_open_file_location_uses_native_folder_opener(
            self, _system, run):
        with tempfile.TemporaryDirectory() as temp_dir:
            open_file_location(os.path.join(temp_dir, "board.kicad_pcb"))

        run.assert_called_once_with(["open", temp_dir])


if __name__ == "__main__":
    unittest.main()
