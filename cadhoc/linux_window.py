import ctypes
import ctypes.util
import os


class WindowActivationError(RuntimeError):
    """Raised when Linux cannot activate a window through X11."""


def _is_wayland_session(environ):
    return (environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or
            bool(environ.get("WAYLAND_DISPLAY")))


class _XClientMessageData(ctypes.Union):
    _fields_ = [
        ("b", ctypes.c_char * 20),
        ("s", ctypes.c_short * 10),
        ("l", ctypes.c_long * 5),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _XClientMessageData),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_int),
        ("xclient", _XClientMessageEvent),
        ("pad", ctypes.c_long * 24),
    ]


class _X11Connection:
    _ANY_PROPERTY_TYPE = 0
    _CLIENT_MESSAGE = 33
    _SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
    _SUBSTRUCTURE_REDIRECT_MASK = 1 << 20

    def __init__(self, library, display, display_name):
        self.library = library
        self.display = display
        self.display_name = display_name
        self.root = library.XDefaultRootWindow(display)

    def close(self):
        if self.display:
            self.library.XCloseDisplay(self.display)
            self.display = None

    def _atom(self, name, only_if_exists=False):
        return self.library.XInternAtom(
            self.display, name.encode("ascii"), int(only_if_exists))

    def _property_values(self, window, atom):
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        item_count = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.library.XGetWindowProperty(
            self.display,
            ctypes.c_ulong(window),
            ctypes.c_ulong(atom),
            0,
            1 << 20,
            0,
            self._ANY_PROPERTY_TYPE,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(item_count),
            ctypes.byref(bytes_after),
            ctypes.byref(data),
        )
        try:
            if status != 0 or actual_format.value != 32 or not data:
                return ()
            values = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
            return tuple(int(values[index])
                         for index in range(item_count.value))
        finally:
            if data:
                self.library.XFree(data)

    def client_window_ids(self):
        for name in ("_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST"):
            atom = self._atom(name, only_if_exists=True)
            if atom:
                values = self._property_values(self.root, atom)
                if values:
                    return values
        return ()

    def window_pid(self, window):
        atom = self._atom("_NET_WM_PID", only_if_exists=True)
        if not atom:
            return None
        values = self._property_values(window, atom)
        return values[0] if values else None

    def activate(self, window):
        active_atom = self._atom("_NET_ACTIVE_WINDOW")
        if not active_atom:
            return False
        event = _XEvent()
        event.xclient.type = self._CLIENT_MESSAGE
        event.xclient.serial = 0
        event.xclient.send_event = 1
        event.xclient.display = self.display
        event.xclient.window = int(window)
        event.xclient.message_type = active_atom
        event.xclient.format = 32
        event.xclient.data.l[:] = (1, 0, 0, 0, 0)
        sent = self.library.XSendEvent(
            self.display,
            self.root,
            0,
            self._SUBSTRUCTURE_REDIRECT_MASK |
            self._SUBSTRUCTURE_NOTIFY_MASK,
            ctypes.byref(event),
        )
        self.library.XFlush(self.display)
        return bool(sent)


def _load_x11():
    library_name = ctypes.util.find_library("X11")
    if not library_name:
        raise WindowActivationError(
            "Linux window activation requires the system libX11 library."
        )
    try:
        library = ctypes.CDLL(library_name)
    except OSError as exc:
        raise WindowActivationError(
            f"Cannot load the system libX11 library: {exc}"
        ) from exc

    library.XOpenDisplay.argtypes = (ctypes.c_char_p,)
    library.XOpenDisplay.restype = ctypes.c_void_p
    library.XDefaultRootWindow.argtypes = (ctypes.c_void_p,)
    library.XDefaultRootWindow.restype = ctypes.c_ulong
    library.XInternAtom.argtypes = (
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int)
    library.XInternAtom.restype = ctypes.c_ulong
    library.XGetWindowProperty.argtypes = (
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
        ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    )
    library.XGetWindowProperty.restype = ctypes.c_int
    library.XSendEvent.argtypes = (
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
        ctypes.POINTER(_XEvent),
    )
    library.XSendEvent.restype = ctypes.c_int
    library.XFlush.argtypes = (ctypes.c_void_p,)
    library.XFlush.restype = ctypes.c_int
    library.XFree.argtypes = (ctypes.c_void_p,)
    library.XFree.restype = ctypes.c_int
    library.XCloseDisplay.argtypes = (ctypes.c_void_p,)
    library.XCloseDisplay.restype = ctypes.c_int
    return library


def _open_x11_display(display_name):
    library = _load_x11()
    display = library.XOpenDisplay(os.fsencode(display_name))
    if not display:
        raise WindowActivationError(
            f"Cannot connect to X11 display {display_name!r}."
        )
    return _X11Connection(library, display, display_name)


def x11_display_available(environ=None):
    """Return whether DISPLAY names a reachable X11/XWayland server."""
    environ = os.environ if environ is None else environ
    display_name = environ.get("DISPLAY")
    if not display_name:
        return False
    try:
        connection = _open_x11_display(display_name)
    except WindowActivationError:
        return False
    connection.close()
    return True


def xwayland_available(environ=None):
    """Return whether a Wayland session also exposes a usable X display."""
    environ = os.environ if environ is None else environ
    return _is_wayland_session(environ) and x11_display_available(environ)


def _activate_x11_window(pid, display_name):
    connection = None
    try:
        connection = _open_x11_display(display_name)
        for window in connection.client_window_ids():
            try:
                window_pid = connection.window_pid(window)
            except Exception:
                continue
            if window_pid == int(pid):
                return connection.activate(window)
        return False
    except WindowActivationError:
        raise
    except Exception as exc:
        raise WindowActivationError(
            f"Cannot connect to X11 display {display_name!r}: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def bring_pid_to_front(pid, environ=None):
    """Activate an X11 or XWayland top-level window through EWMH."""
    environ = os.environ if environ is None else environ
    display_name = environ.get("DISPLAY")
    wayland = _is_wayland_session(environ)
    if not display_name:
        if wayland:
            raise WindowActivationError(
                "Cannot bring native Wayland windows to the front. "
                "Kikakuka supports external activation only for X11 and "
                "XWayland windows."
            )
        raise WindowActivationError(
            "Cannot bring the window to the front because DISPLAY is not set."
        )

    activated = _activate_x11_window(pid, display_name)
    if not activated and wayland:
        raise WindowActivationError(
            "This process does not expose an XWayland window. Native Wayland "
            "windows cannot be brought to the front by another application."
        )
    return activated
