import importlib.util
import os
import queue
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADLESS_EXPORT_PATH = os.path.join(
    REPOSITORY_ROOT, "FreekiCAD", "freecad", "FreekiCAD",
    "HeadlessExport.py"
)
IM_CLIENT_PATH = os.path.join(
    REPOSITORY_ROOT, "FreekiCAD", "freecad", "FreekiCAD",
    "im_client.py"
)
STEP_LOADER_PATH = os.path.join(
    REPOSITORY_ROOT, "FreekiCAD", "freecad", "FreekiCAD",
    "StepLoader.py"
)


class FakeShape:
    def __init__(self, faces=1):
        self.Faces = [object()] * faces
        self.Placement = None

    def isNull(self):
        return False

    def copy(self):
        copied = FakeShape(len(self.Faces))
        copied.Placement = self.Placement
        return copied


class FakeDocument:
    def __init__(self, name="FreekiCADExport"):
        self.Name = name
        self.recompute_count = 0
        self.objects = []

    def recompute(self):
        self.recompute_count += 1

    def addObject(self, _type_name, name):
        obj = types.SimpleNamespace(Name=name, Label=name, Shape=None)
        self.objects.append(obj)
        return obj


def _linked_object(object_type, label, shape=None, group=None):
    proxy = types.SimpleNamespace(Type=object_type)
    return types.SimpleNamespace(
        Name=label.replace(" ", "_"),
        Label=label,
        Proxy=proxy,
        Shape=shape,
        Group=list(group or []),
    )


def load_headless_export_module():
    fake_freecad = types.ModuleType("FreeCAD")
    fake_freecad.Console = types.SimpleNamespace(
        PrintMessage=mock.Mock(), PrintError=mock.Mock())
    fake_import = types.ModuleType("Import")
    fake_import.export = mock.Mock()
    fake_assembly = types.ModuleType(
        "FreekiCAD.freecad.FreekiCAD.Assembly")
    module_name = "FreekiCAD.freecad.FreekiCAD.HeadlessExport"
    spec = importlib.util.spec_from_file_location(module_name, HEADLESS_EXPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        "FreeCAD": fake_freecad,
        "Import": fake_import,
        "FreekiCAD.freecad.FreekiCAD.Assembly": fake_assembly,
    }):
        spec.loader.exec_module(module)
    return module, fake_freecad, fake_import, fake_assembly


class HeadlessExportTests(unittest.TestCase):
    def setUp(self):
        (self.module, self.freecad, self.part,
         self.assembly) = load_headless_export_module()

    def test_load_all_waits_for_each_object_before_repositioning(self):
        events = []
        step = _linked_object("StepObject", "Enclosure", FakeShape())
        step.Proxy.reload = mock.Mock(
            side_effect=lambda obj, force: events.append("step") or True)
        pcb = _linked_object("PcbObject", "Board")
        pcb.Proxy.reload_sync = mock.Mock(
            side_effect=lambda obj, reposition: events.append("pcb"))
        pcb.Proxy._reposition_all_coupled_objects = mock.Mock(
            side_effect=lambda doc: events.append("position"))
        document = FakeDocument()

        self.module._load_all_objects([step, pcb], document)

        self.assertEqual(events, ["step", "pcb", "position"])
        step.Proxy.reload.assert_called_once_with(step, force=True)
        pcb.Proxy.reload_sync.assert_called_once_with(
            pcb, reposition=False)
        self.assertEqual(document.recompute_count, 1)

    def test_collect_export_objects_excludes_editor_and_debug_geometry(self):
        board = _linked_object(None, "Board_Board", FakeShape())
        component = _linked_object(None, "Board_U1", FakeShape())
        bend = _linked_object("BendLine", "Board_Bend", FakeShape())
        marker = _linked_object("CouplerMarker", "Board_Coupler", FakeShape())
        marker.CouplerType = "CouplerFixed"
        debug = _linked_object(None, "Board_DebugArrows", FakeShape())
        pcb = _linked_object(
            "PcbObject", "Board", group=[board, component, bend, marker, debug])
        step = _linked_object("StepObject", "Enclosure", FakeShape())

        result = self.module._collect_export_objects([pcb, step])

        self.assertEqual(result, [board, component, step])

    def test_uniform_pcb_color_expands_after_bending_changes_faces(self):
        board = _linked_object(None, "Board_Board", FakeShape(faces=3))
        pcb = _linked_object("PcbObject", "Board", group=[board])
        color = (0.1, 0.6, 0.2)
        pcb.Proxy._export_face_colors = {board.Name: [color]}

        result = self.module._collect_export_sources([pcb])

        self.assertEqual(result, [(board, [color, color, color])])

    def test_export_defers_recompute_until_synchronous_loading(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "assembly.kkkk_asm")
            target = os.path.join(root, "assembly.step")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write('{"objects": []}')
            document = FakeDocument()
            self.freecad.newDocument = mock.Mock(return_value=document)
            self.freecad.closeDocument = mock.Mock()
            step = _linked_object("StepObject", "Enclosure", FakeShape())
            colors = [(1.0, 0.0, 0.0)]
            step.Proxy._export_face_colors = colors
            global_placement = object()
            step.getGlobalPlacement = mock.Mock(
                return_value=global_placement)
            self.assembly.insert = mock.Mock(return_value=[step])

            with mock.patch.object(self.module, "_load_all_objects") as load_all:
                self.module.export_assembly(source, target)

            self.assembly.insert.assert_called_once_with(
                source, document.Name, recompute=False)
            load_all.assert_called_once_with([step], document)
            flat = document.objects[0]
            self.assertIs(flat.Shape.Placement, global_placement)
            self.part.export.assert_called_once_with(
                [(flat, colors)], target,
                legacy=False, keepPlacement=True)
            self.freecad.closeDocument.assert_called_once_with(document.Name)

    def test_export_accepts_kicad_pcb_as_single_linked_object(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "board.kicad_pcb")
            target = os.path.join(root, "board.step")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write("(kicad_pcb)")
            document = FakeDocument()
            self.freecad.newDocument = mock.Mock(return_value=document)
            self.freecad.closeDocument = mock.Mock()
            board = _linked_object(None, "Board_Board", FakeShape())
            placement = object()
            board.getGlobalPlacement = mock.Mock(return_value=placement)
            pcb = _linked_object("PcbObject", "Board", group=[board])
            create_pcb_object = mock.Mock(return_value=pcb)
            fake_pcb_module = types.ModuleType(
                "FreekiCAD.freecad.FreekiCAD.PcbObject")
            fake_pcb_module.create_pcb_object = create_pcb_object

            with mock.patch.dict(sys.modules, {
                    "FreekiCAD.freecad.FreekiCAD.PcbObject":
                    fake_pcb_module}):
                with mock.patch.object(
                        self.module, "_load_all_objects") as load_all:
                    self.module.export_assembly(source, target)

            create_pcb_object.assert_called_once_with(
                filename=source, document=document, recompute=False)
            load_all.assert_called_once_with([pcb], document)
            flat = document.objects[0]
            self.assertIs(flat.Shape.Placement, placement)
            self.part.export.assert_called_once_with(
                [flat], target, legacy=False, keepPlacement=True)
            self.freecad.closeDocument.assert_called_once_with(document.Name)

    def test_export_rejects_unsupported_input_extension(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "board.FCStd")
            target = os.path.join(root, "board.step")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write("")

            with self.assertRaisesRegex(
                    ValueError, r"\.kkkk_asm or \.kicad_pcb"):
                self.module.export_assembly(source, target)


class FakeSignal:
    def connect(self, callback):
        self.callback = callback

    def emit(self, value):
        self.callback(value)


def load_im_client_module(*, with_qt=True):
    fake_freecad = types.ModuleType("FreeCAD")
    fake_freecad.GuiUp = with_qt
    fake_freecad.Console = types.SimpleNamespace(
        PrintMessage=mock.Mock(), PrintError=mock.Mock())
    qt_core = types.SimpleNamespace(
        QObject=object,
        Signal=lambda *args: FakeSignal(),
        Slot=lambda *args: lambda callback: callback,
    )
    fake_pyside = types.ModuleType("PySide")
    fake_pyside.QtCore = qt_core
    module_name = "FreekiCAD.freecad.FreekiCAD.im_client_sync_test"
    spec = importlib.util.spec_from_file_location(module_name, IM_CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
        "FreeCAD": fake_freecad,
        "PySide": fake_pyside if with_qt else None,
    }):
        spec.loader.exec_module(module)
    return module


class InstanceClientSyncTests(unittest.TestCase):
    def test_headless_node_does_not_publish_its_documents_or_require_pyside(self):
        module = load_im_client_module(with_qt=False)
        node = mock.Mock()
        module.FreeCAD.addDocumentObserver = mock.Mock()
        module.FreeCAD.listDocuments = mock.Mock(return_value={
            "Board": types.SimpleNamespace(
                Name="Board", FileName="/boards/board.FCStd")})
        with mock.patch.object(module.im_mesh, "start_node", return_value=node):
            self.assertIs(module.ensure_node(), node)
        self.assertIsNone(module.QtCore)
        module.FreeCAD.addDocumentObserver.assert_not_called()
        node.set_document_provider.assert_not_called()
        node.publish.assert_not_called()

    def test_gui_startup_can_attach_observer_after_node_was_started(self):
        module = load_im_client_module()
        module.FreeCAD.GuiUp = False
        module.FreeCAD.addDocumentObserver = mock.Mock()
        module.FreeCAD.listDocuments = mock.Mock(return_value={
            "Board": types.SimpleNamespace(
                Name="Board", FileName="/boards/board.FCStd")})
        node = mock.Mock()
        with mock.patch.object(module.im_mesh, "start_node", return_value=node):
            module.ensure_node()
            module.FreeCAD.addDocumentObserver.assert_not_called()
            module.ensure_node(observe_documents=True)
        module.FreeCAD.addDocumentObserver.assert_called_once()
        deadline = module.time.monotonic() + 2
        while node.publish.call_count < 1 and module.time.monotonic() < deadline:
            module.time.sleep(0.01)
        node.publish.assert_called_once_with("/boards/board.FCStd", os.getpid())

    def test_existing_freecad_documents_are_published_when_node_starts(self):
        module = load_im_client_module()
        node = mock.Mock()
        module.FreeCAD.addDocumentObserver = mock.Mock()
        module.FreeCAD.listDocuments = mock.Mock(return_value={
            "Board": types.SimpleNamespace(
                Name="Board", FileName="/boards/board.FCStd")})
        with mock.patch.object(module.im_mesh, "start_node", return_value=node):
            self.assertIs(module.ensure_node(), node)
        module.FreeCAD.addDocumentObserver.assert_called_once()
        node.set_document_provider.assert_called_once()
        deadline = module.time.monotonic() + 2
        while node.publish.call_count < 1 and module.time.monotonic() < deadline:
            module.time.sleep(0.01)
        node.publish.assert_called_once_with("/boards/board.FCStd", os.getpid())

    def test_document_scan_repairs_missed_open_and_close_events(self):
        module = load_im_client_module()
        node = mock.Mock()
        observer = module._DocumentObserver(node)
        observer.paths = {"Old": "/models/old.FCStd"}
        module.FreeCAD.listDocuments = mock.Mock(return_value={
            "New": types.SimpleNamespace(
                Name="New", FileName="/models/new.FCStd"),
            "Unsaved": types.SimpleNamespace(Name="Unsaved", FileName=""),
        })

        self.assertEqual(observer.list_documents(), ["/models/new.FCStd"])
        deadline = module.time.monotonic() + 2
        while node.publish.call_count < 2 and module.time.monotonic() < deadline:
            module.time.sleep(0.01)
        self.assertEqual(node.publish.call_args_list, [
            mock.call("/models/old.FCStd", None),
            mock.call("/models/new.FCStd", os.getpid()),
        ])

    def test_imported_assembly_source_is_reported_without_document_filename(self):
        module = load_im_client_module()
        node = mock.Mock()
        observer = module._DocumentObserver(node)
        document = types.SimpleNamespace(
            Name="Assembly", Label="assembly", FileName="")
        module.FreeCAD.listDocuments = mock.Mock(return_value={"Assembly": document})

        observer.register_source(document, "/models/assembly.kkkk_asm")
        self.assertEqual(observer.list_documents(), ["/models/assembly.kkkk_asm"])
        deadline = module.time.monotonic() + 2
        while node.publish.call_count < 1 and module.time.monotonic() < deadline:
            module.time.sleep(0.01)
        node.publish.assert_called_once_with(
            "/models/assembly.kkkk_asm", os.getpid())

        observer.slotDeletedDocument(document)
        self.assertEqual(observer.list_documents(), [])

    def test_activate_imported_assembly_selects_matching_mdi_tab(self):
        module = load_im_client_module()
        document = types.SimpleNamespace(
            Name="Assembly", Label="assembly", FileName="")
        module.FreeCAD.listDocuments = mock.Mock(return_value={"Assembly": document})
        module.FreeCAD.setActiveDocument = mock.Mock()
        observer = module._DocumentObserver(mock.Mock())
        observer.register_source(document, "/models/assembly.kkkk_asm")
        class FakeMdiSubWindow:
            def windowTitle(self):
                return "assembly : 1[*]"

        matching_window = FakeMdiSubWindow()
        other_window = types.SimpleNamespace(
            windowTitle=lambda: "assembly : 1[*]")
        graphics = types.SimpleNamespace(parentWidget=lambda: matching_window)
        mdi = types.SimpleNamespace(
            subWindowList=lambda: [other_window, matching_window],
            setActiveSubWindow=mock.Mock())
        fake_document = types.SimpleNamespace(activeView=lambda: types.SimpleNamespace(
            graphicsView=lambda: graphics))
        fake_gui = types.SimpleNamespace(getMainWindow=lambda: types.SimpleNamespace(
            findChild=lambda _type: mdi), getDocument=lambda _name: fake_document)
        fake_pyside = types.ModuleType("PySide")
        fake_pyside.QtWidgets = types.SimpleNamespace(
            QMdiArea=object(), QMdiSubWindow=FakeMdiSubWindow)

        with mock.patch.dict(sys.modules, {"FreeCADGui": fake_gui,
                                            "PySide": fake_pyside}):
            self.assertTrue(observer.activate_document(
                "/models/assembly.kkkk_asm"))
            self.assertFalse(observer.activate_document(
                "/models/missing.kkkk_asm"))
        mdi.setActiveSubWindow.assert_called_once_with(matching_window)
        module.FreeCAD.setActiveDocument.assert_called_once_with("Assembly")

    def test_open_step_uses_nonmodal_import_and_registers_source(self):
        module = load_im_client_module()
        observer = module._DocumentObserver(mock.Mock())
        document = types.SimpleNamespace(Name="Imported", FileName="", Objects=[object()])
        documents = {}
        module.FreeCAD.listDocuments = lambda: documents
        fake_import = types.ModuleType("Import")
        fake_import.open = mock.Mock(side_effect=lambda _: documents.update(
            {"Imported": document}))
        with mock.patch.dict(sys.modules, {"Import": fake_import}), \
                mock.patch.object(observer, "activate_document",
                                  side_effect=[False, True]) as activate:
            self.assertTrue(observer.open_document("/models/part.step"))
        fake_import.open.assert_called_once_with("/models/part.step")
        self.assertEqual(observer.source_paths["Imported"], "/models/part.step")
        self.assertEqual(activate.call_count, 2)

    def test_open_native_freecad_document_in_same_process(self):
        module = load_im_client_module()
        observer = module._DocumentObserver(mock.Mock())
        document = types.SimpleNamespace(Name="Part", FileName="/models/part.FCStd")
        documents = {}
        module.FreeCAD.listDocuments = lambda: documents
        module.FreeCAD.openDocument = mock.Mock(side_effect=lambda _: documents.update(
            {"Part": document}))
        with mock.patch.object(observer, "activate_document",
                               side_effect=[False, True]):
            self.assertTrue(observer.open_document("/models/part.FCStd"))
        module.FreeCAD.openDocument.assert_called_once_with("/models/part.FCStd")

    def test_bind_launched_step_to_its_unsaved_import_document(self):
        module = load_im_client_module()
        observer = module._DocumentObserver(mock.Mock())
        document = types.SimpleNamespace(Name="Imported", FileName="", Objects=[object()])
        module.FreeCAD.listDocuments = lambda: {"Imported": document}
        self.assertTrue(observer.bind_launched_source("/models/part.step"))
        self.assertEqual(observer.list_documents(), ["/models/part.step"])
        self.assertTrue(observer.bind_launched_source("/models/part.step"))

    def test_document_scan_runs_on_gui_thread(self):
        module = load_im_client_module()
        observer = module._DocumentObserver(mock.Mock())
        gui_thread = threading.current_thread()
        module.QtCore.QThread = types.SimpleNamespace(
            currentThread=threading.current_thread)
        callbacks = queue.Queue()
        observer.dispatcher = types.SimpleNamespace(
            thread=lambda: gui_thread,
            dispatch=types.SimpleNamespace(emit=callbacks.put),
        )
        observed_threads = []
        module.FreeCAD.listDocuments = lambda: observed_threads.append(
            threading.current_thread()) or {}
        results = []
        worker = threading.Thread(target=lambda: results.append(observer.list_documents()))
        worker.start()
        callbacks.get(timeout=2)()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [[]])
        self.assertEqual(observed_threads, [gui_thread])

    def test_created_document_is_published_after_filename_is_assigned(self):
        module = load_im_client_module()
        callbacks = []
        module.QtCore.QTimer = types.SimpleNamespace(
            singleShot=lambda _delay, callback: callbacks.append(callback))
        node = mock.Mock()
        observer = module._DocumentObserver(node)
        document = types.SimpleNamespace(Name="Board", FileName="")
        observer.slotCreatedDocument(document)
        document.FileName = "/boards/board.FCStd"
        callbacks.pop()()
        deadline = module.time.monotonic() + 2
        while node.publish.call_count < 1 and module.time.monotonic() < deadline:
            module.time.sleep(0.01)
        node.publish.assert_called_once_with("/boards/board.FCStd", os.getpid())

    def test_activation_publishes_document_loaded_after_create(self):
        module = load_im_client_module()
        node = mock.Mock()
        observer = module._DocumentObserver(node)
        document = types.SimpleNamespace(Name="Board", FileName="")
        observer._record(document)
        document.FileName = "/boards/board.FCStd"
        observer.slotActivateDocument(document)
        deadline = module.time.monotonic() + 2
        while node.publish.call_count < 1 and module.time.monotonic() < deadline:
            module.time.sleep(0.01)
        node.publish.assert_called_once_with("/boards/board.FCStd", os.getpid())

    def test_freecad_document_events_publish_open_and_close(self):
        module = load_im_client_module()
        node = mock.Mock()
        observer = module._DocumentObserver(node)
        document = types.SimpleNamespace(Name="Board", FileName="/boards/board.FCStd")
        observer._record(document)
        observer.slotDeletedDocument(document)
        deadline = module.time.monotonic() + 2
        while node.publish.call_count < 2 and module.time.monotonic() < deadline:
            module.time.sleep(0.01)
        self.assertEqual(node.publish.call_args_list, [
            mock.call("/boards/board.FCStd", os.getpid()),
            mock.call("/boards/board.FCStd", None),
        ])

    def test_save_as_replaces_the_published_path(self):
        module = load_im_client_module()
        node = mock.Mock()
        observer = module._DocumentObserver(node)
        document = types.SimpleNamespace(Name="Board", FileName="/boards/old.FCStd")
        observer._record(document)
        document.FileName = "/boards/new.FCStd"
        observer.slotFinishSaveDocument(document, document.FileName)
        deadline = module.time.monotonic() + 2
        while node.publish.call_count < 3 and module.time.monotonic() < deadline:
            module.time.sleep(0.01)
        self.assertEqual(node.publish.call_args_list, [
            mock.call("/boards/old.FCStd", os.getpid()),
            mock.call("/boards/old.FCStd", None),
            mock.call("/boards/new.FCStd", os.getpid()),
        ])

    def test_request_sync_uses_mesh_without_workspace(self):
        module = load_im_client_module()
        with mock.patch.object(module, "_request", return_value={
                "status": "ok", "action": "reload",
                "socket": "/tmp/kicad/api-123.sock"}) as request:
            result = module.request_sync(
                "reload", "/board/main.kicad_pcb")

        self.assertEqual(result["socket"], "/tmp/kicad/api-123.sock")
        request.assert_called_once_with({
            "action": "reload", "filepath": "/board/main.kicad_pcb", "object": ""})

    def test_request_sync_raises_instance_error(self):
        module = load_im_client_module()
        module._request = mock.Mock(return_value={
            "status": "error", "message": "KiCad API was not ready"})

        with self.assertRaisesRegex(RuntimeError, "not ready"):
            module.request_sync("reload", "/board/main.kicad_pcb")


class HeadlessStepLoaderTests(unittest.TestCase):
    def test_command_line_import_retains_per_face_colors(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_freecad.GuiUp = False
        fake_freecad.Console = types.SimpleNamespace(
            PrintWarning=mock.Mock())
        document = FakeDocument("__FreekiCAD_tmp__")
        fake_freecad.newDocument = mock.Mock(return_value=document)
        fake_freecad.closeDocument = mock.Mock()
        shape = FakeShape(faces=2)
        colors = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
        imported_obj = types.SimpleNamespace(Shape=shape)
        fake_import = types.ModuleType("Import")
        fake_import.insert = mock.Mock(return_value=[
            (imported_obj, colors),
        ])
        fake_part = types.ModuleType("Part")
        module_name = "FreekiCAD.freecad.FreekiCAD.StepLoader_headless_test"
        spec = importlib.util.spec_from_file_location(
            module_name, STEP_LOADER_PATH)
        module = importlib.util.module_from_spec(spec)

        with mock.patch.dict(sys.modules, {
                "FreeCAD": fake_freecad,
                "Import": fake_import,
                "Part": fake_part}):
            spec.loader.exec_module(module)
            result = module._load_step("/models/part.step", document)

        self.assertEqual(result[0][1], colors)
        self.assertEqual(len(result[0][0].Faces), 2)
        fake_import.insert.assert_called_once_with(
            name="/models/part.step",
            docName=document.Name,
            merge=True,
            useLinkGroup=False)
        fake_freecad.closeDocument.assert_called_once_with(document.Name)


if __name__ == "__main__":
    unittest.main()
