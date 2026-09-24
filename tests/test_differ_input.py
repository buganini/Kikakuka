import os
import tempfile
import unittest
from unittest import mock

from differ_input import prepare_differ_source


class DifferInputTests(unittest.TestCase):
    def test_kicad_source_is_used_directly(self):
        source = os.path.abspath("board.kicad_pcb")
        with mock.patch("differ_input.is_gerber_dir", return_value=False), \
                mock.patch("differ_input.is_gerber_zip", return_value=False), \
                mock.patch("differ_input.convert_to_kicad") as convert:
            prepared, errors = prepare_differ_source(source, "a", "/tmp")

        self.assertEqual(prepared, source)
        self.assertEqual(errors, [])
        convert.assert_not_called()

    def test_gerber_folder_is_converted_to_temporary_kicad_pcb(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                mock.patch("differ_input.is_gerber_dir", return_value=True), \
                mock.patch("differ_input.is_gerber_zip", return_value=False), \
                mock.patch(
                    "differ_input.convert_to_kicad", return_value=["warning"]
                ) as convert:
            source = os.path.join(temp_dir, "fabrication")
            prepared, errors = prepare_differ_source(
                source, "b", temp_dir)

        self.assertEqual(os.path.basename(prepared), "fabrication.kicad_pcb")
        self.assertIn(f"{os.sep}gerber-b-", prepared)
        self.assertEqual(errors, ["warning"])
        convert.assert_called_once_with(
            os.path.abspath(source), prepared, required_edge_cuts=False)

    def test_gerber_zip_drops_zip_suffix_from_output(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                mock.patch("differ_input.is_gerber_dir", return_value=False), \
                mock.patch("differ_input.is_gerber_zip", return_value=True), \
                mock.patch(
                    "differ_input.convert_to_kicad", return_value=[]
                ) as convert:
            source = os.path.join(temp_dir, "production.zip")
            prepared, errors = prepare_differ_source(
                source, "a", temp_dir)

        self.assertEqual(os.path.basename(prepared), "production.kicad_pcb")
        self.assertEqual(errors, [])
        convert.assert_called_once_with(
            os.path.abspath(source), prepared, required_edge_cuts=False)


if __name__ == "__main__":
    unittest.main()
