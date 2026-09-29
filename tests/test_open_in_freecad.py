"""Open in FreeCAD orchestration, including cross-process request transport."""

import os
import importlib.util
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from im import freecad_open, im_mesh


class OpenInFreeCADTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.filepath = Path(self.directory.name, 'board.kicad_pcb')
        self.filepath.touch()
        self.runtime = mock.patch.object(im_mesh, 'runtime_dir',
                                         return_value=Path(self.directory.name, 'mesh'))
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.peers = [dict(pid=1, endpoint='first', freecad_pcb=2),
                      dict(pid=2, endpoint='second', freecad_pcb=2)]
        self.discovery = mock.patch.object(freecad_open, '_gui_peers', return_value=self.peers)
        self.discovery.start()
        self.addCleanup(self.discovery.stop)
        self.focus = mock.patch.object(freecad_open, '_focus').start()
        self.addCleanup(mock.patch.stopall)

    def test_search_selects_target_before_focus_and_update(self):
        order = []
        def open_pcb(peer, path, socket, **kwargs):
            self.assertEqual(socket, '/tmp/editor.sock')
            if kwargs.get('probe'):
                order.append(('probe', peer['pid']))
                return 'Chosen' if peer['pid'] == 2 else None
            order.append(('update', peer['pid'], kwargs['document_name']))
            return True
        self.focus.side_effect = lambda pid: order.append(('focus', pid))
        with mock.patch.object(freecad_open, '_open_pcb', side_effect=open_pcb):
            self.assertEqual(freecad_open.open_board(str(self.filepath), 'ipc:///tmp/editor.sock'), 2)
        self.assertEqual(order, [('probe', 1), ('probe', 2), ('focus', 2), ('update', 2, 'Chosen')])

    def test_creates_in_first_instance_only_after_no_matches(self):
        def create_after_search(*args, **kwargs):
            if kwargs.get('create'):
                self.focus.assert_called_once_with(1)
                return True
            self.focus.assert_not_called()
            return None
        with mock.patch.object(freecad_open, '_open_pcb', side_effect=create_after_search) as op:
            freecad_open.open_board(str(self.filepath), '/tmp/editor.sock')
        self.assertEqual([call.args[0]['pid'] for call in op.call_args_list], [1, 2, 1, 2, 1])
        self.assertEqual(op.call_args.kwargs, {'create': True})

    def test_active_document_in_later_instance_precedes_inactive_match(self):
        calls = []
        def open_pcb(peer, path, socket, **kwargs):
            calls.append((peer['pid'], kwargs.get('active_only')))
            return 'Active' if peer['pid'] == 2 or not kwargs.get('active_only') else None
        with mock.patch.object(freecad_open, '_open_pcb', side_effect=open_pcb):
            self.assertEqual(freecad_open.open_board(str(self.filepath), '/tmp/live.sock'), 2)
        self.assertEqual(calls, [(1, True), (2, True), (2, None)])

    def test_does_not_create_elsewhere_after_update_failure(self):
        with mock.patch.object(freecad_open, '_open_pcb', side_effect=RuntimeError('import failed')) as op:
            with self.assertRaisesRegex(RuntimeError, 'import failed'):
                freecad_open.open_board(str(self.filepath), '/tmp/editor.sock')
        self.assertEqual(op.call_count, 1)
        self.focus.assert_not_called()

    def test_launches_empty_freecad_and_waits_for_its_node(self):
        with mock.patch.object(freecad_open, '_gui_peers', side_effect=[[], self.peers]), \
                mock.patch.object(freecad_open, '_editors', return_value={}), \
                mock.patch.object(freecad_open, '_launch', return_value=1) as launch, \
                mock.patch.object(freecad_open.time, 'sleep'), \
                mock.patch.object(freecad_open, '_open_pcb', return_value=True):
            freecad_open.open_board(str(self.filepath), '/tmp/editor.sock')
        launch.assert_called_once_with(None, 'freecad')

    def test_waits_for_existing_process_without_launching_another(self):
        def wait_for_node(_delay):
            self.focus.assert_not_called()

        with mock.patch.object(freecad_open, '_gui_peers', side_effect=[[], self.peers]), \
                mock.patch.object(freecad_open, '_editors', return_value={1: 0}), \
                mock.patch.object(freecad_open, '_launch') as launch, \
                mock.patch.object(freecad_open.time, 'sleep', side_effect=wait_for_node), \
                mock.patch.object(freecad_open, '_open_pcb', return_value=True):
            freecad_open.open_board(str(self.filepath), '/tmp/editor.sock')
        launch.assert_not_called()

    def test_repeated_click_is_coalesced_while_request_is_running(self):
        path = os.path.normcase(os.path.realpath(str(self.filepath)))
        with im_mesh._file_lock('open-in-freecad:' + path), \
                mock.patch.object(freecad_open, '_open_pcb') as op:
            self.assertIsNone(freecad_open.open_board(str(self.filepath), '/tmp/editor.sock'))
        op.assert_not_called()

    def test_unsaved_board_fails_before_discovery(self):
        with mock.patch.object(freecad_open, '_gui_peers') as peers:
            with self.assertRaisesRegex(ValueError, 'Save the PCB'):
                freecad_open.open_board(None, '/tmp/editor.sock')
        peers.assert_not_called()

    def test_mesh_waits_for_completion_and_propagates_gui_failure(self):
        node = im_mesh.InstanceNode(lambda _: {})
        self.addCleanup(node.close)
        opener = mock.Mock(return_value=True)
        node.set_pcb_opener(opener)
        peer = dict(pid=node.pid, endpoint=node.endpoint, freecad_pcb=2)
        self.assertTrue(freecad_open._open_pcb(peer, str(self.filepath), '/tmp/editor.sock', create=True))
        opener.assert_called_once_with(str(self.filepath), '/tmp/editor.sock', create=True, active_only=False, probe=False, document_name=None)
        opener.return_value = 'Assembly'
        self.assertEqual(freecad_open._open_pcb(
            peer, str(self.filepath), '/tmp/editor.sock', probe=True), 'Assembly')
        self.assertTrue(opener.call_args.kwargs['probe'])
        opener.side_effect = RuntimeError('geometry failed')
        with self.assertRaisesRegex(RuntimeError, 'geometry failed'):
            freecad_open._open_pcb(peer, str(self.filepath), '/tmp/editor.sock')

    def test_old_pcb_capability_is_rejected_before_request(self):
        with mock.patch.object(im_mesh, '_exchange') as exchange:
            with self.assertRaisesRegex(RuntimeError, 'Update FreekiCAD'):
                freecad_open._open_pcb(dict(freecad_pcb=True), str(self.filepath), '/tmp/live.sock', probe=True)
        exchange.assert_not_called()

    def test_nonblocking_lock_is_reusable_after_release(self):
        with im_mesh._file_lock('button', blocking=False) as acquired:
            self.assertTrue(acquired)
            with im_mesh._file_lock('button', blocking=False) as duplicate:
                self.assertFalse(duplicate)
        with im_mesh._file_lock('button', blocking=False) as acquired:
            self.assertTrue(acquired)


class PluginEntrypointTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'kicad-addon/plugin/plugins/open_in_freecad.py'
        spec = importlib.util.spec_from_file_location('open_in_freecad_plugin_test', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_uses_current_editor_socket_without_saving_board(self):
        board = mock.Mock()
        board.name = '/boards/current.kicad_pcb'
        kicad = mock.Mock()
        kicad.get_board.return_value = board
        kicad.get_version.return_value = '10.0.0'
        with mock.patch.object(self.module, 'KiCad', return_value=kicad), \
                mock.patch.object(self.module, 'open_board', return_value=123) as open_board, \
                mock.patch.dict(os.environ, {'KICAD_API_SOCKET': 'ipc:///tmp/current.sock'}), \
                mock.patch('builtins.print'):
            self.assertEqual(self.module.main(), 0)
        open_board.assert_called_once_with('/boards/current.kicad_pcb', 'ipc:///tmp/current.sock')
        board.save.assert_not_called()
        board.revert.assert_not_called()

    def test_reports_freecad_error_as_failed_action(self):
        kicad = mock.Mock()
        kicad.get_board.return_value.name = '/boards/current.kicad_pcb'
        kicad.get_version.return_value = '10.0.0'
        with mock.patch.object(self.module, 'KiCad', return_value=kicad), \
                mock.patch.object(self.module, 'open_board', side_effect=RuntimeError('import failed')), \
                mock.patch('builtins.print') as report:
            self.assertEqual(self.module.main(), 1)
        report.assert_called_once_with('Kikakuka Open in FreeCAD: import failed', file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
