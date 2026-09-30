import importlib.util
import os
import sys
import types
import unittest
from unittest import mock


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_PATH = os.path.join(
    REPOSITORY_ROOT, "FreekiCAD", "freecad", "FreekiCAD"
)
IMPORT_PATH = os.path.join(PACKAGE_PATH, "PcbImport.py")
INIT_PATH = os.path.join(PACKAGE_PATH, "__init__.py")


class Document:
    def __init__(self, name):
        self.Name = name


def load_module(path, name, fake_freecad):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"FreeCAD": fake_freecad}):
        spec.loader.exec_module(module)
    return module


class PcbImportTests(unittest.TestCase):
    def setUp(self):
        self.document = Document("Assembly")
        self.documents = {self.document.Name: self.document}
        self.fake_freecad = types.ModuleType("FreeCAD")
        self.fake_freecad.getDocument = mock.Mock(
            side_effect=lambda name: self.documents[name])

        def new_document(name):
            document = Document(name)
            self.documents[name] = document
            return document

        self.fake_freecad.newDocument = mock.Mock(side_effect=new_document)
        self.create_pcb_object = mock.Mock(return_value=object())
        self.pcb_object_module = types.ModuleType(
            "FreekiCAD.freecad.FreekiCAD.PcbObject")
        self.pcb_object_module.create_pcb_object = self.create_pcb_object

        module_name = "FreekiCAD.freecad.FreekiCAD.PcbImport"
        self.addCleanup(sys.modules.pop, module_name, None)
        with mock.patch.dict(
            sys.modules,
            {
                "FreeCAD": self.fake_freecad,
                "FreekiCAD.freecad.FreekiCAD.PcbObject": (
                    self.pcb_object_module),
            },
        ):
            self.pcb_import = load_module(
                IMPORT_PATH, module_name, self.fake_freecad)

    def test_insert_creates_linked_pcb_in_named_document(self):
        result = self.pcb_import.insert("board.kicad_pcb", "Assembly")

        self.fake_freecad.getDocument.assert_called_once_with("Assembly")
        self.create_pcb_object.assert_called_once_with(
            "board.kicad_pcb", document=self.document)
        self.assertIs(result, self.create_pcb_object.return_value)

    def test_open_creates_document_named_after_board(self):
        self.pcb_import.open("/boards/control.kicad_pcb")

        self.fake_freecad.newDocument.assert_called_once_with("control")
        self.fake_freecad.getDocument.assert_called_once_with("control")
        created_document = self.create_pcb_object.call_args.kwargs["document"]
        self.assertEqual(created_document.Name, "control")
        self.create_pcb_object.assert_called_once_with(
            "/boards/control.kicad_pcb", document=created_document)

    def test_package_registers_kicad_pcb_importer(self):
        fake_freecad = types.ModuleType("FreeCAD")
        fake_freecad.addImportType = mock.Mock()
        fake_freecad.addExportType = mock.Mock()

        load_module(INIT_PATH, "freekicad_package_init", fake_freecad)

        fake_freecad.addImportType.assert_any_call(
            "FreekiCAD linked KiCad PCB (*.kicad_pcb)",
            "freecad.FreekiCAD.PcbImport",
        )

    def test_gui_import_starts_loading_on_next_event_loop_turn(self):
        for entry in ("open", "insert"):
            with self.subTest(entry=entry):
                self.fake_freecad.GuiUp = True
                provider = mock.Mock()
                view = types.SimpleNamespace(Proxy=provider)
                self.create_pcb_object.return_value = types.SimpleNamespace(
                    ViewObject=view)
                callbacks = []
                timer = types.SimpleNamespace(singleShot=lambda delay, callback:
                    callbacks.append((delay, callback)))
                with mock.patch.dict(sys.modules, {"PySide":
                        types.SimpleNamespace(QtCore=types.SimpleNamespace(QTimer=timer))}):
                    if entry == "open":
                        self.pcb_import.open("board.kicad_pcb")
                    else:
                        self.pcb_import.insert("board.kicad_pcb", "Assembly")
                provider._auto_reload.assert_not_called()
                self.assertEqual(len(callbacks), 1)
                delay, callback = callbacks[0]
                self.assertEqual(delay, 0)
                callback()
                provider._auto_reload.assert_called_once_with(view)


if __name__ == "__main__":
    unittest.main()
