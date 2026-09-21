import hashlib
import os
import unittest

from differ_source import source_paths


class DifferSourcePathTests(unittest.TestCase):
    def test_working_selection_opens_original_file(self):
        source = os.path.join(os.sep, "repo", "boards", "main.kicad_pcb")
        cache = hashlib.sha256(source.encode("utf-8")).hexdigest()

        self.assertEqual(
            source_paths(source, None, "", os.path.join(os.sep, "tmp", "diff")),
            (os.path.join(os.sep, "tmp", "diff", cache), source, source),
        )

    def test_revision_selection_opens_the_matching_checkout_file(self):
        source = os.path.join(os.sep, "repo", "boards", "main.kicad_pcb")
        revision = "abc123"
        temp_dir = os.path.join(os.sep, "tmp", "diff")
        cache = hashlib.sha256(source.encode("utf-8")).hexdigest()
        export_dir = os.path.join(temp_dir, f"{cache}_{revision}")

        self.assertEqual(
            source_paths(source, os.path.join(os.sep, "repo"), revision, temp_dir),
            (
                export_dir,
                os.path.join(export_dir, "workdir", "boards", "main.kicad_pcb"),
                "boards/main.kicad_pcb",
            ),
        )

    def test_each_revision_has_its_own_checkout(self):
        source = os.path.join(os.sep, "repo", "main.kicad_sch")
        first = source_paths(source, os.path.join(os.sep, "repo"), "rev-a", "/tmp/diff")
        second = source_paths(source, os.path.join(os.sep, "repo"), "rev-b", "/tmp/diff")

        self.assertNotEqual(first[1], second[1])

    def test_revision_requires_repository(self):
        with self.assertRaisesRegex(ValueError, "repository not found"):
            source_paths("/repo/main.kicad_sch", None, "abc123", "/tmp/diff")


if __name__ == "__main__":
    unittest.main()
