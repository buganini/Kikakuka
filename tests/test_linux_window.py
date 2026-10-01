import types
import unittest
from unittest import mock

from im import linux_window


class _Property:
    def __init__(self, value):
        self.value = value


class _Window:
    def __init__(self, pid):
        self.pid = pid

    def get_full_property(self, _atom, _property_type):
        return _Property([self.pid])


class _Root:
    def __init__(self, window_ids):
        self.window_ids = window_ids
        self.events = []

    def get_full_property(self, _atom, _property_type):
        return _Property(self.window_ids)

    def send_event(self, event, event_mask):
        self.events.append((event, event_mask))


class _Connection:
    def __init__(self, windows):
        self.root = _Root(list(windows))
        self.windows = windows
        self.flushed = False
        self.closed = False

    def screen(self):
        return types.SimpleNamespace(root=self.root)

    def intern_atom(self, name, only_if_exists=False):
        return name

    def create_resource_object(self, _kind, window_id):
        return _Window(self.windows[window_id])

    def flush(self):
        self.flushed = True

    def close(self):
        self.closed = True


class LinuxWindowActivationTests(unittest.TestCase):
    def _xlib(self, windows):
        connection = _Connection(windows)
        X = types.SimpleNamespace(
            AnyPropertyType=0,
            CurrentTime=0,
            SubstructureRedirectMask=1,
            SubstructureNotifyMask=2,
        )
        display = types.SimpleNamespace(Display=mock.Mock(
            return_value=connection))
        protocol = types.SimpleNamespace(event=types.SimpleNamespace(
            ClientMessage=lambda **kwargs: kwargs))
        return connection, (X, display, protocol)

    def test_xwayland_window_uses_net_active_window(self):
        connection, xlib = self._xlib({10: 1234, 11: 5678})
        environ = {
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
        }

        with mock.patch.object(linux_window, "_load_xlib",
                               return_value=xlib):
            activated = linux_window.bring_pid_to_front(
                5678, environ=environ)

        self.assertTrue(activated)
        self.assertTrue(connection.flushed)
        self.assertTrue(connection.closed)
        event, mask = connection.root.events[0]
        self.assertEqual(event["client_type"], "_NET_ACTIVE_WINDOW")
        self.assertEqual(event["window"].pid, 5678)
        self.assertEqual(event["data"], (32, [1, 0, 0, 0, 0]))
        self.assertEqual(mask, 3)

    def test_native_wayland_without_x11_display_reports_error(self):
        with self.assertRaisesRegex(
                linux_window.WindowActivationError, "native Wayland"):
            linux_window.bring_pid_to_front(
                1234,
                environ={"XDG_SESSION_TYPE": "wayland",
                         "WAYLAND_DISPLAY": "wayland-0"},
            )

    def test_native_wayland_window_not_in_xwayland_reports_error(self):
        _connection, xlib = self._xlib({10: 1234})
        environ = {
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
        }

        with mock.patch.object(linux_window, "_load_xlib",
                               return_value=xlib):
            with self.assertRaisesRegex(
                    linux_window.WindowActivationError,
                    "does not expose an XWayland window"):
                linux_window.bring_pid_to_front(5678, environ=environ)

    def test_x11_missing_window_returns_false(self):
        _connection, xlib = self._xlib({10: 1234})
        with mock.patch.object(linux_window, "_load_xlib",
                               return_value=xlib):
            self.assertFalse(linux_window.bring_pid_to_front(
                5678,
                environ={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
            ))


if __name__ == "__main__":
    unittest.main()
