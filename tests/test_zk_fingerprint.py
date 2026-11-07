import socket
import unittest
from struct import pack, unpack
from types import SimpleNamespace

from edge_service_zkteco.zk_fingerprint import enroll_fingerprint


def frame(command, payload=b'', session=1):
    data = pack('<4H', command, 0, session, 7) + payload
    return pack('<HHI', 0x5050, 0x7d82, len(data)) + data


class Socket:
    def __init__(self, conn):
        self.conn = conn
        self.timeout = 5
        self.chunks = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        command = unpack('<H', data[8:10])[0]
        self.conn.commands.append(command)
        if command == 61:
            # Observed one-byte progress frames, coalesced and fragmented.
            data = frame(2000) + frame(500, b'\x2b') + frame(500, b'\x24')
            data += frame(500, bytes.fromhex('00004e03393930303031000000000000000000'))
            self.chunks.extend([data[:5], data[5:19], data[19:]])
            if self.conn.saves:
                self.conn.template = b'new-template'
        else:
            # Late event immediately ahead of command ACK.
            self.chunks.append(frame(500, b'\x38') + frame(2000))

    def recv(self, size):
        if self.chunks:
            return self.chunks.pop(0)
        if self.conn.partial:
            self.conn.partial = False
            return frame(500, b'\x2b')[:10]
        raise socket.timeout()


class Conn:
    tcp = True
    _ZK__session_id = 1
    _ZK__reply_id = 0

    def __init__(self, saves=True):
        self.commands = []
        self.saves = saves
        self.template = None
        self.partial = False
        self.acks = 0
        self._ZK__sock = Socket(self)

    def _ZK__create_header(self, command, payload, session, reply):
        return pack('<4H', command, 0, session, reply + 1) + payload

    def _ZK__create_tcp_top(self, data):
        return pack('<HHI', 0x5050, 0x7d82, len(data)) + data

    def _ZK__ack_ok(self):
        self.acks += 1

    def get_user_template(self, **kwargs):
        return self.template

    def refresh_data(self):
        pass


class FingerprintTests(unittest.TestCase):
    user = SimpleNamespace(uid=2429, user_id='990001')

    def test_observed_progress_split_frames_and_late_events(self):
        conn = Conn()
        self.assertTrue(enroll_fingerprint(conn, self.user, 6))
        self.assertEqual(conn.commands, [62, 61, 62, 500, 60])
        self.assertEqual(conn.acks, 7)
        self.assertEqual(conn._ZK__sock.timeout, 5)
        self.assertFalse(conn._ZK__sock.chunks)

    def test_progress_without_template_is_failure(self):
        self.assertFalse(enroll_fingerprint(Conn(saves=False), self.user, 6))

    def test_existing_unchanged_template_is_not_success(self):
        conn = Conn(saves=False)
        conn.template = b'previous-template'
        self.assertFalse(enroll_fingerprint(conn, self.user, 6))

    def test_partial_frame_does_not_report_success_and_restores_timeout(self):
        conn = Conn()
        conn.partial = True
        with self.assertRaises((ConnectionError, ValueError)):
            enroll_fingerprint(conn, self.user, 6)
        self.assertEqual(conn._ZK__sock.timeout, 5)


if __name__ == '__main__':
    unittest.main()
