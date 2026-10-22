"""Shared FreeCAD routing must not launch duplicates after ambiguous failures."""
import unittest
from unittest import mock
from cadhoc import editor_open


class FreeCADOpenTests(unittest.TestCase):
    def setUp(self):
        for target in ('_ensure_instance_node',):
            patcher = mock.patch.object(editor_open, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(editor_open.os.path, 'isfile', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mesh_success_does_not_launch(self):
        with mock.patch.object(editor_open, 'request_workspace_open', return_value=True), \
                mock.patch.object(editor_open, 'open_with_system') as launch:
            self.assertEqual(editor_open.open_freecad_file('/tmp/a.kkkk_asm'), 'workspace')
        launch.assert_not_called()

    def test_offline_launches_only_when_no_freecad_is_running(self):
        for running in ([], [123]):
            with self.subTest(running=running), \
                    mock.patch.object(editor_open, 'request_workspace_open', side_effect=ConnectionError), \
                    mock.patch('cadhoc.instance_backend._editors', return_value=running), \
                    mock.patch.object(editor_open, 'open_with_system') as launch:
                if running:
                    with self.assertRaises(ConnectionError):
                        editor_open.open_freecad_file('/tmp/a.kkkk_asm')
                    launch.assert_not_called()
                else:
                    self.assertEqual(editor_open.open_freecad_file('/tmp/a.kkkk_asm'), 'system')
                    launch.assert_called_once_with('/tmp/a.kkkk_asm', program='freecad')

    def test_ambiguous_errors_never_launch_again(self):
        for error in (TimeoutError, RuntimeError):
            with self.subTest(error=error), \
                    mock.patch.object(editor_open, 'request_workspace_open', side_effect=error), \
                    mock.patch.object(editor_open, 'open_with_system') as launch:
                with self.assertRaises(error):
                    editor_open.open_freecad_file('/tmp/a.FCStd')
                launch.assert_not_called()

    def test_rejects_kikakuka_documents(self):
        for path in ('/tmp/a.kkkk', '/tmp/a.kkkk_fab'):
            with self.assertRaises(ValueError):
                editor_open.open_freecad_file(path)
