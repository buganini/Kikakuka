"""FreekiCAD's asynchronous KiCad request client for the instance mesh."""

import threading
import time
import os
import queue

import FreeCAD
try:
    from PySide import QtCore
except ImportError:
    QtCore = None

from . import im_mesh
from .instance_backend import handle


_response_handler = None
_dispatcher = None
_node_lock = threading.Lock()
_document_observer = None


if QtCore is not None:
    class _Dispatcher(QtCore.QObject):
        dispatch = QtCore.Signal(object)

        def __init__(self):
            super().__init__()
            self.dispatch.connect(self._run)

        @QtCore.Slot(object)
        def _run(self, callback):
            callback()


class _DocumentObserver:
    """Publish FreeCAD document lifecycle without periodic process polling."""

    def __init__(self, node):
        self.node = node
        self.paths = {}
        self.source_paths = {}
        self.events = queue.Queue()
        self.dispatcher = _Dispatcher() if QtCore is not None else None
        threading.Thread(target=self._broadcast, daemon=True).start()

    def _scan_paths(self):
        current = {}
        documents = FreeCAD.listDocuments()
        for document in documents.values():
            name = getattr(document, "Name", None)
            path = self._document_path(document)
            if name and path:
                current[name] = path
        self.source_paths = {name: path for name, path in self.source_paths.items()
                             if name in documents}
        previous_paths = set(self.paths.values())
        current_paths = set(current.values())
        self.paths = current
        for path in sorted(previous_paths - current_paths):
            self.events.put((path, None))
        for path in sorted(current_paths - previous_paths):
            self.events.put((path, os.getpid()))
        return sorted(current_paths)

    def _document_path(self, document):
        name = getattr(document, "Name", None)
        path = getattr(document, "FileName", "") or self.source_paths.get(name, "")
        return os.path.normcase(os.path.realpath(os.path.abspath(path))) if path else ""

    def _on_gui_thread(self, callback, timeout=2):
        if (self.dispatcher is None or not hasattr(QtCore, "QThread") or
                QtCore.QThread.currentThread() == self.dispatcher.thread()):
            return callback()
        result = queue.Queue(maxsize=1)

        def run():
            try:
                result.put((True, callback()))
            except Exception as exc:
                result.put((False, exc))

        self.dispatcher.dispatch.emit(run)
        try:
            success, value = result.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("FreeCAD GUI did not answer document request") from exc
        if not success:
            raise value
        return value

    def list_documents(self):
        """Read FreeCAD's live documents on its GUI thread."""
        return self._on_gui_thread(self._scan_paths)

    def register_source(self, document, filepath):
        """Record the path of an imported file whose Document.FileName is empty."""
        self.source_paths[document.Name] = os.path.normcase(
            os.path.realpath(os.path.abspath(filepath)))
        self._record(document)

    def activate_document(self, filepath):
        """Select a matching FreeCAD document and its visible MDI tab."""
        filepath = os.path.normcase(os.path.realpath(os.path.abspath(filepath)))

        def activate():
            self._scan_paths()
            document = next((document for document in FreeCAD.listDocuments().values()
                             if self._document_path(document) == filepath), None)
            if document is None:
                return False
            import FreeCADGui
            from PySide import QtWidgets
            mdi = FreeCADGui.getMainWindow().findChild(QtWidgets.QMdiArea)
            if mdi is not None:
                view = FreeCADGui.getDocument(document.Name).activeView()
                graphics = view.graphicsView() if view is not None and hasattr(
                    view, "graphicsView") else None
                window = graphics
                while window is not None and not isinstance(window, QtWidgets.QMdiSubWindow):
                    window = window.parentWidget()
                if window is None:
                    # Some document types have no graphicsView; their tab
                    # title is usable only when its label is unambiguous.
                    label = str(getattr(document, "Label", ""))
                    matching = [candidate for candidate in mdi.subWindowList()
                                if candidate.windowTitle().startswith(label + " : ")]
                    window = matching[0] if len(matching) == 1 else None
                if window is not None and window in mdi.subWindowList():
                    mdi.setActiveSubWindow(window)
            FreeCAD.setActiveDocument(document.Name)
            return True

        return self._on_gui_thread(activate)

    def open_document(self, filepath):
        """Open a new file in this GUI process and identify its document."""
        filepath = os.path.normcase(os.path.realpath(os.path.abspath(filepath)))

        def open_on_gui():
            if self.activate_document(filepath):
                return True
            before = set(FreeCAD.listDocuments())
            suffix = os.path.splitext(filepath)[1].lower()
            if suffix == ".fcstd":
                FreeCAD.openDocument(filepath)
            elif suffix in (".step", ".stp"):
                # ImportGui.open opens a modal STEP-settings dialog, which
                # cannot be answered by an IPC request. Import.open uses the
                # current importer settings without blocking the GUI thread.
                import Import
                Import.open(filepath)
            elif suffix == ".kkkk_asm":
                from . import Assembly
                Assembly.open(filepath)
            else:
                raise ValueError(f"unsupported FreeCAD file: {filepath}")
            created = set(FreeCAD.listDocuments()) - before
            if len(created) != 1:
                raise RuntimeError(f"FreeCAD did not create one document for: {filepath}")
            document = FreeCAD.listDocuments()[created.pop()]
            if not getattr(document, "FileName", ""):
                self.register_source(document, filepath)
            else:
                self._record(document)
            return self.activate_document(filepath)

        return self._on_gui_thread(open_on_gui, timeout=90)

    def bind_launched_source(self, filepath):
        """Identify an import opened by FreeCAD's command-line startup."""
        filepath = os.path.normcase(os.path.realpath(os.path.abspath(filepath)))

        def bind():
            if filepath in self._scan_paths():
                return True
            documents = list(FreeCAD.listDocuments().values())
            unbound = [document for document in documents
                       if not getattr(document, "FileName", "") and
                       document.Name not in self.source_paths]
            if len(unbound) != 1:
                return False
            if (os.path.splitext(filepath)[1].lower() in (".step", ".stp") and
                    not getattr(unbound[0], "Objects", ())):
                return False
            self.register_source(unbound[0], filepath)
            return True

        return self._on_gui_thread(bind)

    def _broadcast(self):
        while True:
            path, pid = self.events.get()
            try:
                self.node.publish(path, pid)
            except Exception as exc:
                try:
                    _log(f"could not publish FreeCAD document state: {exc}", error=True)
                except Exception:
                    pass

    def _record(self, document):
        name = getattr(document, "Name", None)
        path = self._document_path(document)
        if not name or not path:
            return
        previous = self.paths.get(name)
        if previous == path:
            return
        self.paths[name] = path
        if previous and previous not in self.paths.values():
            self.events.put((previous, None))
        self.events.put((path, os.getpid()))

    def slotCreatedDocument(self, document):
        # FileName is often assigned only after the creation callback. Check
        # again when the GUI event loop finishes loading the document.
        if QtCore is not None and hasattr(QtCore, "QTimer"):
            QtCore.QTimer.singleShot(0, lambda: self._record(document))
        else:
            self._record(document)

    def slotActivateDocument(self, document):
        self._record(document)

    def slotChangedDocument(self, document, _property):
        self._record(document)

    def slotFinishSaveDocument(self, document, _filename):
        self._record(document)

    def slotDeletedDocument(self, document):
        name = getattr(document, "Name", None)
        self.source_paths.pop(name, None)
        path = self.paths.pop(name, None)
        if path and path not in self.paths.values():
            self.events.put((path, None))


def _log(message, error=False):
    method = FreeCAD.Console.PrintError if error else FreeCAD.Console.PrintMessage
    method(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] FreekiCAD: {message}\n")


def ensure_node(observe_documents=None):
    """Start a node in any FreeCAD process; observe documents only in the GUI."""
    global _document_observer
    if observe_documents is None:
        observe_documents = bool(getattr(FreeCAD, "GuiUp", False))
    with _node_lock:
        node = im_mesh.start_node(handle)
        if (observe_documents and _document_observer is None and
                hasattr(FreeCAD, "addDocumentObserver")):
            _document_observer = _DocumentObserver(node)
            FreeCAD.addDocumentObserver(_document_observer)
            node.set_document_provider(_document_observer.list_documents)
            node.set_document_activator(_document_observer.activate_document)
            node.set_document_opener(_document_observer.open_document)
            node.set_source_registrar(_document_observer.bind_launched_source)
            _document_observer._scan_paths()
        return node


def register_document_source(document, filepath):
    """Associate a FreeCAD import-created document with its source file."""
    if not getattr(FreeCAD, "GuiUp", False):
        return
    ensure_node(observe_documents=True)
    if _document_observer is not None:
        _document_observer.register_source(document, filepath)


def set_response_handler(handler):
    global _response_handler, _dispatcher
    _response_handler = handler
    if _dispatcher is None and QtCore is not None:
        _dispatcher = _Dispatcher()


def dispatch_to_main_thread(callback):
    if _dispatcher is not None:
        _dispatcher.dispatch.emit(callback)
    else:
        callback()


def _message(action, filepath, object_label, component):
    message = {"action": action, "filepath": filepath, "object": object_label}
    if component:
        message["component"] = component
    return message


def _request(message):
    ensure_node()
    return im_mesh.request(message)


def send_request(action, filepath, object_label="", component=""):
    """Run a KiCad request off the GUI thread, then dispatch the reply."""
    message = _message(action, filepath, object_label, component)

    def run():
        try:
            reply = _request(message)
        except Exception as exc:
            reply = {"status": "error", "message": str(exc)}
        reply.setdefault("action", action)
        reply.setdefault("object", object_label)
        reply.setdefault("component", component)
        if reply.get("status") == "error":
            _log(reply.get("message", "unknown instance error"), error=True)
        if _response_handler is not None:
            dispatch_to_main_thread(lambda: _response_handler(reply))

    threading.Thread(target=run, daemon=True).start()


def request_sync(action, filepath, object_label="", component="", **_unused):
    """Resolve an editor/socket without requiring a Workspace Manager."""
    reply = _request(_message(action, filepath, object_label, component))
    if reply.get("status") == "error":
        raise RuntimeError(reply.get("message", "unknown instance error"))
    if not reply.get("socket"):
        raise RuntimeError(f"instance manager returned no KiCad socket: {reply}")
    return reply


def report_error(socket_path, error):
    _log(f"Failed to connect to KiCad API socket: {socket_path}: {error}", error=True)
