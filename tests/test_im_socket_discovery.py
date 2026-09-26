"""Tests for the standalone cross-platform socket discovery probes."""

import os
import unittest
from unittest import mock

from im import socket_discovery_test as discovery


class SocketDiscoveryProbeTests(unittest.TestCase):
    def test_enumeration_does_not_guess_generic_socket_pid(self):
        with mock.patch.object(
                    discovery, "_same_user_editors", return_value={222: mock.Mock()}
                ), \
                mock.patch.object(discovery, "_endpoint_inventory", return_value=[
                    ("api.sock", "/tmp/kicad/api.sock", None),
                    ("api-222.sock", "/tmp/kicad/api-222.sock", None),
                    ("api-333.sock", "/tmp/kicad/api-333.sock", None),
                ]):
            self.assertEqual(discovery.enumerated_kicad_sockets(), [
                (None, "/tmp/kicad/api.sock"),
                (222, "/tmp/kicad/api-222.sock"),
            ])

    def test_unix_owner_probe_uses_per_process_connections(self):
        owner = mock.Mock()
        owner.net_connections.return_value = [
            mock.Mock(laddr="/tmp/kicad/api.sock"),
            mock.Mock(laddr="/tmp/not-kicad.sock"),
        ]
        inaccessible = mock.Mock()
        inaccessible.net_connections.side_effect = discovery.psutil.AccessDenied(
            pid=222
        )
        with mock.patch.object(
                    discovery.platform, "system", return_value="Darwin"
                ), \
                mock.patch.object(
                    discovery, "_same_user_editors",
                    return_value={111: owner, 222: inaccessible},
                ):
            self.assertEqual(discovery.owner_kicad_sockets(), [
                (111, "/tmp/kicad/api.sock"),
            ])

        owner.net_connections.assert_called_once_with(kind="unix")
        inaccessible.net_connections.assert_called_once_with(kind="unix")

    def test_windows_owner_probe_uses_named_pipe_server_pid(self):
        pipe = r"\\.\pipe\C:\Temp\kicad\api.sock"
        canonical = os.path.join(r"C:\Temp\kicad", "api.sock")
        with mock.patch.object(
                    discovery.platform, "system", return_value="Windows"
                ), \
                mock.patch.object(
                    discovery, "_same_user_editors", return_value={222: mock.Mock()}
                ), \
                mock.patch.object(
                    discovery, "_endpoint_inventory",
                    return_value=[("api.sock", canonical, pipe)],
                ), \
                mock.patch.object(
                    discovery, "_windows_server_pid", return_value=222
                ) as server_pid:
            self.assertEqual(discovery.owner_kicad_sockets(), [
                (222, canonical),
            ])

        server_pid.assert_called_once_with(pipe)

    def test_windows_server_pid_probe_closes_handle(self):
        create_file = mock.Mock(return_value=123)
        get_server_pid = mock.Mock()

        def return_pid(_handle, pid_pointer):
            pid_pointer._obj.value = 222
            return True

        get_server_pid.side_effect = return_pid
        close_handle = mock.Mock(return_value=True)
        kernel32 = mock.Mock(
            CreateFileW=create_file,
            GetNamedPipeServerProcessId=get_server_pid,
            CloseHandle=close_handle,
        )
        pipe = r"\\.\pipe\C:\Temp\kicad\api.sock"
        with mock.patch(
                    "ctypes.WinDLL", return_value=kernel32, create=True
                ):
            self.assertEqual(discovery._windows_server_pid(pipe), 222)

        create_file.assert_called_once_with(pipe, 0, 0, None, 3, 0, None)
        close_handle.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
