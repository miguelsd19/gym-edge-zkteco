import socket
import unittest
from struct import pack, unpack
from types import SimpleNamespace
from unittest import mock

from edge_service_zkteco.zk_enroll import (
    FACE_NO_CAPTURE,
    FACE_NOT_SAVED,
    FACE_SAVED,
    FingerprintAlreadyExistsError,
    enroll_face,
    enroll_fingerprint,
    enrollment_result,
)

# Evento final observado en Norias: resultado 0, tamano 0x034e, user_id 990001.
FINAL_OK = bytes.fromhex('00004e03393930303031000000000000000000')
FINAL_FAIL = bytes.fromhex('0400000039393030303100000000000000')
# Marca de acceso (36 bytes, user_id al inicio) mientras la persona sigue enfrente.
ATTENDANCE = b'990001'.ljust(24, b'\x00') + b'\x01\x0f' + b'\x00' * 10


def frame(command, payload=b'', session=1):
    data = pack('<4H', command, 0, session, 7) + payload
    return pack('<HHI', 0x5050, 0x7d82, len(data)) + data


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


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
            data = frame(2000) + b''.join(frame(500, payload) for payload in self.conn.events)
            # Fragmentado y agrupado como en el lector.
            self.chunks.extend(chunk for chunk in (data[:5], data[5:19], data[19:]) if chunk)
            if self.conn.saves:
                self.conn.template = b'new-template'
                self.conn.faces += 1
        else:
            # Evento tardio justo antes del ACK del comando.
            self.chunks.append(frame(500, b'\x38') + frame(2000))

    def recv(self, size):
        if self.chunks:
            return self.chunks.pop(0)
        if self.conn.partial:
            self.conn.partial = False
            return frame(500, b'\x2b')[:10]
        # Nada que leer: pasa el tiempo de espera pedido.
        self.conn.clock.now += self.timeout
        raise socket.timeout()


class Conn:
    tcp = True
    _ZK__session_id = 1
    _ZK__reply_id = 0

    def __init__(self, events=(b'\x2b', b'\x24', FINAL_OK), saves=True):
        self.events = list(events)
        self.commands = []
        self.saves = saves
        self.template = None
        self.faces = 29
        self.partial = False
        self.acks = 0
        self.clock = Clock()
        self._ZK__sock = Socket(self)

    def _ZK__create_header(self, command, payload, session, reply):
        return pack('<4H', command, 0, session, reply + 1) + payload

    def _ZK__create_tcp_top(self, data):
        return pack('<HHI', 0x5050, 0x7d82, len(data)) + data

    def _ZK__ack_ok(self):
        self.acks += 1

    def get_user_template(self, **kwargs):
        return self.template

    def read_sizes(self):
        return True

    def refresh_data(self):
        pass


USER = SimpleNamespace(uid=2429, user_id='990001')


class EnrollTestCase(unittest.TestCase):
    def run_with_clock(self, conn, func, *args, **kwargs):
        with mock.patch('edge_service_zkteco.zk_enroll.time.monotonic', conn.clock):
            start = conn.clock.now
            result = func(conn, USER, *args, **kwargs)
            return result, conn.clock.now - start


class FingerprintTests(EnrollTestCase):
    def test_observed_progress_split_frames_and_late_events(self):
        conn = Conn()
        ok, _ = self.run_with_clock(conn, enroll_fingerprint, 6)
        self.assertTrue(ok)
        self.assertEqual(conn.commands, [62, 61, 62, 500, 60])
        self.assertEqual(conn.acks, 7)
        self.assertEqual(conn._ZK__sock.timeout, 5)
        self.assertFalse(conn._ZK__sock.chunks)

    def test_final_event_ends_capture_without_waiting_timeout(self):
        conn = Conn()
        ok, elapsed = self.run_with_clock(conn, enroll_fingerprint, 6, 240)
        self.assertTrue(ok)
        self.assertLess(elapsed, 1)

    def test_without_final_event_waits_until_timeout(self):
        conn = Conn(events=[b'\x2b'])
        ok, elapsed = self.run_with_clock(conn, enroll_fingerprint, 6, 30)
        self.assertTrue(ok)  # la plantilla nueva sigue mandando
        self.assertGreaterEqual(elapsed, 30)

    def test_progress_without_template_is_failure(self):
        ok, _ = self.run_with_clock(Conn(saves=False), enroll_fingerprint, 6)
        self.assertFalse(ok)

    def test_existing_template_prevents_enroll_without_touching_socket(self):
        conn = Conn(saves=False)
        conn.template = b'previous-template'
        with self.assertRaisesRegex(FingerprintAlreadyExistsError, 'ya tiene una huella'):
            self.run_with_clock(conn, enroll_fingerprint, 6)
        self.assertEqual(conn.commands, [])
        self.assertEqual(conn.template, b'previous-template')
        self.assertEqual(conn._ZK__sock.timeout, 5)

    def test_another_free_finger_can_be_registered(self):
        conn = Conn()
        reads = []

        def template(uid, temp_id):
            reads.append((uid, temp_id))
            return None if len(reads) == 1 else b'new-template'

        conn.get_user_template = template
        ok, _ = self.run_with_clock(conn, enroll_fingerprint, 7)
        self.assertTrue(ok)
        self.assertEqual(reads, [(USER.uid, 7), (USER.uid, 7)])
        self.assertIn(61, conn.commands)

    def test_partial_frame_does_not_report_success_and_restores_timeout(self):
        conn = Conn(events=[b'\x2b'])
        conn.partial = True
        with self.assertRaises((ConnectionError, ValueError)):
            self.run_with_clock(conn, enroll_fingerprint, 6)
        self.assertEqual(conn._ZK__sock.timeout, 5)


class FaceTests(EnrollTestCase):
    def test_face_final_ok_is_saved_without_waiting(self):
        conn = Conn(saves=False)  # el conteo no sube: reemplazo de cara
        result, elapsed = self.run_with_clock(conn, enroll_face, 240)
        self.assertEqual(result, FACE_SAVED)
        self.assertLess(elapsed, 1)
        self.assertEqual(conn.commands, [62, 61, 62, 500, 60])

    def test_face_attendance_events_do_not_keep_it_waiting(self):
        # Sin evento final y la persona sigue frente al lector marcando.
        conn = Conn(events=[b'\x2b', ATTENDANCE, ATTENDANCE, ATTENDANCE])
        result, elapsed = self.run_with_clock(conn, enroll_face, 240, quiet_seconds=8)
        self.assertEqual(result, FACE_SAVED)  # sube el conteo
        self.assertLess(elapsed, 20)

    def test_face_nobody_in_front_is_no_capture(self):
        conn = Conn(events=[], saves=False)
        result, elapsed = self.run_with_clock(conn, enroll_face, 30)
        self.assertEqual(result, FACE_NO_CAPTURE)
        self.assertGreaterEqual(elapsed, 30)
        self.assertEqual(conn.commands[-3:], [62, 500, 60])

    def test_face_final_failure_without_new_face_is_not_saved(self):
        conn = Conn(events=[b'\x2b', FINAL_FAIL], saves=False)
        result, _ = self.run_with_clock(conn, enroll_face, 240)
        self.assertEqual(result, FACE_NOT_SAVED)

    def test_face_rejected_command_raises_and_restores_timeout(self):
        conn = Conn()
        original = conn._ZK__sock.sendall

        def reject(data):
            if unpack('<H', data[8:10])[0] == 61:
                conn.commands.append(61)
                conn._ZK__sock.chunks.append(frame(2001))
                return
            original(data)

        conn._ZK__sock.sendall = reject
        with self.assertRaises(ConnectionError):
            self.run_with_clock(conn, enroll_face, 30)
        self.assertEqual(conn._ZK__sock.timeout, 5)


class EnrollmentResultTests(unittest.TestCase):
    def test_final_event_layout(self):
        self.assertEqual(enrollment_result(FINAL_OK, '990001'), 0)
        self.assertEqual(enrollment_result(FINAL_FAIL, '990001'), 4)

    def test_other_events_are_not_final(self):
        self.assertIsNone(enrollment_result(b'\x2b', '990001'))
        self.assertIsNone(enrollment_result(ATTENDANCE, '990001'))
        self.assertIsNone(enrollment_result(FINAL_OK, '3800'))


if __name__ == '__main__':
    unittest.main()
