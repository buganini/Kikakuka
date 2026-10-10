import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from kikakuka import user_config


class UserConfigTest(unittest.TestCase):
    def test_executable_override_preserves_workspaces_and_auto_removes_only_it(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".kikakuka"
            config.write_text(
                json.dumps({"workspaces": ["/boards/project.kkkk"]}),
                encoding="utf-8",
            )
            with mock.patch.object(user_config, "config_path", return_value=config):
                executable = Path("/opt/FreeCAD.AppImage")
                user_config.set_custom_executable("freecad", executable)
                self.assertEqual(
                    user_config.custom_executable("freecad"), executable
                )
                self.assertEqual(
                    user_config.load_config(config)["workspaces"],
                    ["/boards/project.kkkk"],
                )

                user_config.set_custom_executable("freecad", None)

            self.assertEqual(
                user_config.load_config(config),
                {"workspaces": ["/boards/project.kkkk"]},
            )


if __name__ == "__main__":
    unittest.main()
