import os


class WindowActivationError(RuntimeError):
    """Raised when Linux cannot activate a window through X11."""


def _is_wayland_session(environ):
    return (environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or
            bool(environ.get("WAYLAND_DISPLAY")))


def _load_xlib():
    try:
        from Xlib import X, display, protocol
    except ImportError as exc:
        raise WindowActivationError(
            "Linux window activation requires the python-xlib package."
        ) from exc
    return X, display, protocol


def _client_window_ids(root, display_connection, X):
    for atom_name in ("_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST"):
        atom = display_connection.intern_atom(atom_name, only_if_exists=True)
        if not atom:
            continue
        prop = root.get_full_property(atom, X.AnyPropertyType)
        if prop is not None and prop.value is not None:
            return prop.value
    return ()


def _activate_x11_window(pid, display_name, X, display, protocol):
    connection = None
    try:
        connection = display.Display(display_name)
        root = connection.screen().root
        pid_atom = connection.intern_atom("_NET_WM_PID", only_if_exists=True)
        active_atom = connection.intern_atom("_NET_ACTIVE_WINDOW")
        if not pid_atom:
            return False

        for window_id in _client_window_ids(root, connection, X):
            window = connection.create_resource_object(
                "window", int(window_id))
            try:
                prop = window.get_full_property(pid_atom, X.AnyPropertyType)
            except Exception:
                continue
            if (prop is None or prop.value is None or not len(prop.value) or
                    int(prop.value[0]) != int(pid)):
                continue

            event = protocol.event.ClientMessage(
                window=window,
                client_type=active_atom,
                data=(32, [1, X.CurrentTime, 0, 0, 0]),
            )
            root.send_event(
                event,
                event_mask=(X.SubstructureRedirectMask |
                            X.SubstructureNotifyMask),
            )
            connection.flush()
            return True
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

    X, display, protocol = _load_xlib()
    activated = _activate_x11_window(
        pid, display_name, X, display, protocol)
    if not activated and wayland:
        raise WindowActivationError(
            "This process does not expose an XWayland window. Native Wayland "
            "windows cannot be brought to the front by another application."
        )
    return activated
