import unittest
from unittest import mock

from cadhoc import linux_window


class _Connection:
    def __init__(self, windows):
        self.windows = windows
        self.activated = []
        self.closed = False

    def client_window_ids(self):
        return tuple(self.windows)

    def window_pid(self, window):
        return self.windows[window]

    def activate(self, window):
        self.activated.append(window)
        return True

    def close(self):
        self.closed = True


class LinuxWindowActivationTests(unittest.TestCase):
    def test_xwayland_window_uses_net_active_window(self):
        connection = _Connection({10: 1234, 11: 5678})
        environ = {
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
        }

        with mock.patch.object(
                linux_window, "_open_x11_display",
                return_value=connection):
            activated = linux_window.bring_pid_to_front(
                5678, environ=environ)

        self.assertTrue(activated)
        self.assertEqual(connection.activated, [11])
        self.assertTrue(connection.closed)

    def test_native_wayland_without_x11_display_reports_error(self):
        with self.assertRaisesRegex(
                linux_window.WindowActivationError, "native Wayland"):
            linux_window.bring_pid_to_front(
                1234,
                environ={"XDG_SESSION_TYPE": "wayland",
                         "WAYLAND_DISPLAY": "wayland-0"},
            )

    def test_native_wayland_window_not_in_xwayland_reports_error(self):
        connection = _Connection({10: 1234})
        environ = {
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
        }

        with mock.patch.object(
                linux_window, "_open_x11_display",
                return_value=connection):
            with self.assertRaisesRegex(
                    linux_window.WindowActivationError,
                    "does not expose an XWayland window"):
                linux_window.bring_pid_to_front(5678, environ=environ)

    def test_x11_missing_window_returns_false(self):
        connection = _Connection({10: 1234})
        with mock.patch.object(
                linux_window, "_open_x11_display",
                return_value=connection):
            self.assertFalse(linux_window.bring_pid_to_front(
                5678,
                environ={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
            ))

    def test_x11_display_available_opens_and_closes_connection(self):
        connection = _Connection({})
        with mock.patch.object(
                linux_window, "_open_x11_display",
                return_value=connection) as open_display:
            self.assertTrue(linux_window.x11_display_available(
                {"DISPLAY": ":1"}))
        open_display.assert_called_once_with(":1")
        self.assertTrue(connection.closed)

    def test_x11_display_unavailable_when_connection_fails(self):
        with mock.patch.object(
                linux_window, "_open_x11_display",
                side_effect=linux_window.WindowActivationError("offline")):
            self.assertFalse(linux_window.x11_display_available(
                {"DISPLAY": ":1"}))

    def test_xwayland_requires_wayland_session_and_x11_connection(self):
        with mock.patch.object(
                linux_window, "x11_display_available",
                return_value=True) as available:
            self.assertTrue(linux_window.xwayland_available({
                "XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0",
            }))
            self.assertFalse(linux_window.xwayland_available({
                "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0",
            }))
        available.assert_called_once()


if __name__ == "__main__":
    unittest.main()
