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

        def _run(self, callback):
            callback()


class _DocumentObserver:
    """Publish FreeCAD document lifecycle without periodic process polling."""

    def __init__(self, node):
        self.node = node
        self.paths = {}
        self.events = queue.Queue()
        threading.Thread(target=self._broadcast, daemon=True).start()

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
        path = getattr(document, "FileName", "") or ""
        if not name or not path:
            return
        path = os.path.realpath(os.path.abspath(path))
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
        path = self.paths.pop(getattr(document, "Name", None), None)
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
            for document in getattr(FreeCAD, "listDocuments", lambda: {})().values():
                _document_observer._record(document)
        return node


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
