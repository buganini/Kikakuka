"""Checks for the JSON framing used by the local instance transport."""

import socket
import unittest

from FreekiCAD.freecad.FreekiCAD import im_transport


class InstanceTransportTests(unittest.TestCase):
    def test_json_round_trip_over_stream_socket(self):
        left, right = socket.socketpair()
        with im_transport.Connection(left) as sender, \
                im_transport.Connection(right) as receiver:
            message = {"mesh_action": "dispatch", "request": {"filepath": "板子.kicad_pcb"}}
            sender.send(message)
            self.assertEqual(receiver.receive(1000), message)

    def test_rejects_oversized_frame_before_reading_body(self):
        left, right = socket.socketpair()
        with im_transport.Connection(left) as receiver, right:
            right.sendall((im_transport.MAX_MESSAGE_BYTES + 1).to_bytes(4, "big"))
            with self.assertRaisesRegex(ValueError, "too large"):
                receiver.receive(1000)

    def test_unresponsive_connection_times_out(self):
        left, right = socket.socketpair()
        with im_transport.Connection(left) as receiver, right:
            with self.assertRaises(TimeoutError):
                receiver.receive(20)


if __name__ == "__main__":
    unittest.main()
