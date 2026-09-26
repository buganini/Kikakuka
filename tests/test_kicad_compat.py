import tempfile
import unittest
from pathlib import Path

from kicad_compat import (
    KiCad10Compatibility,
    UnsupportedKiCadVersion,
    get_kicad_compat,
)


class KiCadCompatibilityTest(unittest.TestCase):
    def test_factory_selects_kicad_10_from_string_and_version_object(self):
        self.assertIsInstance(
            get_kicad_compat("10.0.3-1"), KiCad10Compatibility)
        version = type("Version", (), {"major": 10})()
        self.assertIsInstance(
            get_kicad_compat(version), KiCad10Compatibility)

    def test_factory_rejects_unimplemented_major(self):
        with self.assertRaisesRegex(
                UnsupportedKiCadVersion, "major version 11"):
            get_kicad_compat("11.0")

    def test_kicad_10_package_uris_are_version_scoped(self):
        compatibility = get_kicad_compat("10.0")
        self.assertEqual(
            compatibility.placeholder_model_uri,
            "${KICAD10_3RD_PARTY}/3dmodels/"
            "com_github_buganini_kikakuka-footprints/"
            "Kikakuka.3dshapes/unit-cube.step",
        )
        self.assertEqual(
            compatibility.footprint_library_uri("com_example_library"),
            "${KICAD10_3RD_PARTY}/footprints/"
            "com_example_library/Kikakuka.pretty",
        )

    def test_kicad_10_library_table_contract(self):
        compatibility = get_kicad_compat("10.0")
        table = {
            "version": "7",
            "lib": [{
                "name": "Example",
                "type": "KiCad",
                "uri": "${KIPRJMOD}/Example.pretty",
                "options": "",
                "descr": "Example library",
            }],
        }
        self.assertEqual(
            compatibility.render_library_table("fp_lib_table", table),
            '(fp_lib_table\n'
            '  (version 7)\n'
            '  (lib (name "Example")(type "KiCad")'
            '(uri "${KIPRJMOD}/Example.pretty")(options "")'
            '(descr "Example library"))\n'
            ')\n',
        )

    def test_kicad_10_converts_both_library_tables(self):
        compatibility = get_kicad_compat("10.0")
        with tempfile.TemporaryDirectory() as temporary:
            project_dir = Path(temporary) / "project"
            library = Path(temporary) / "libraries" / "Example.kicad_sym"
            project_dir.mkdir()
            library.parent.mkdir()
            library.touch()
            project = {
                "sym_lib_table": {"lib": [{"uri": str(library)}]},
                "fp_lib_table": {"lib": [{"uri": "${KIPRJMOD}/local"}]},
            }

            def relative(path, base, allow_outside=False):
                self.assertTrue(allow_outside)
                return Path(path).relative_to(Path(base).parent).as_posix()

            changes = compatibility.convert_library_paths_to_project_relative(
                project, str(project_dir), relative)

        self.assertEqual(len(changes), 1)
        self.assertEqual(
            project["sym_lib_table"]["lib"][0]["uri"],
            "${KIPRJMOD}/libraries/Example.kicad_sym",
        )

    def test_kicad_10_pose_workaround_restores_definition_items(self):
        original_items = [object(), object()]

        class Footprint:
            definition = type("Definition", (), {"items": original_items})()

            @property
            def orientation(self):
                return self._orientation

            @orientation.setter
            def orientation(self, value):
                self._orientation = value
                self.definition.items = ["setter dropped models"]

        footprint = Footprint()
        get_kicad_compat("10.0").set_footprint_pose(
            footprint, "position", "orientation")
        self.assertEqual(footprint.position, "position")
        self.assertEqual(footprint.orientation, "orientation")
        self.assertEqual(footprint.definition.items, original_items)


if __name__ == "__main__":
    unittest.main()
