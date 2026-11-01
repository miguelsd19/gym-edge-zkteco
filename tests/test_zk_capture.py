import socket
import unittest
from datetime import datetime
from struct import pack

from edge_service_zkteco.zk_capture import EventPacketBuffer, capture_window, decode_attendance


def frame(payload, command=500):
    packet = pack('<4H', command, 0, 0, 0) + payload
    return pack('<HHI', 0x5050, 0x7d82, len(packet)) + packet


def attendance(user='1001', size=32):
    if size == 12:
        return pack('<IBB6B', int(user), 1, 0, 26, 10, 31, 20, 0, 0)
    return pack('<24sBB6B', user.encode(), 1, 0, 26, 10, 31, 20, 0, 0) + bytes(size-32)


class CaptureProtocolTests(unittest.TestCase):
    def test_fragmented_and_coalesced_tcp_frames(self):
        first, second = frame(attendance()), frame(attendance('2002'))
        reader = EventPacketBuffer(True)
        self.assertEqual(reader.feed(first[:5]), [])
        self.assertEqual(reader.feed(first[5:13]), [])
        packets = reader.feed(first[13:] + second)
        self.assertEqual([decode_attendance(p[8:])[0].user_id for p in packets], ['1001', '2002'])
        self.assertEqual(reader.buffer, b'')

    def test_known_attendance_layouts_and_multiple_records(self):
        for size in (12, 32, 36, 52):
            with self.subTest(size=size):
                event = decode_attendance(attendance(size=size))[0]
                self.assertEqual(event.user_id, '1001')
                self.assertEqual(event.timestamp, datetime(2026, 10, 31, 20, 0, 0))
        self.assertEqual(len(decode_attendance(attendance(size=12)*2)), 2)

    def test_invalid_frames_and_unknown_events_are_rejected(self):
        for chunk in (b'', pack('<HHI', 0, 0, 10), pack('<HHI', 0x5050, 0x7d82, 999999)):
            with self.assertRaises((ConnectionError, ValueError)):
                EventPacketBuffer(True).feed(chunk)
        with self.assertRaises(ValueError):
            decode_attendance(b'\0'*13)
        payload = pack('<4H', 500, 0, 0, 0) + attendance()
        self.assertEqual(EventPacketBuffer(False).feed(payload), [payload])

    def test_callback_failure_restores_timeout_subscription_and_disabled_state(self):
        class Sock:
            timeout = 7
            def gettimeout(self): return self.timeout
            def settimeout(self, value): self.timeout = value
            def recv(self, size): return frame(attendance())
        class Conn:
            _ZK__sock = Sock()
            tcp = True
            is_enabled = False
            flags = []
            def cancel_capture(self): pass
            def verify_user(self): pass
            def enable_device(self): self.is_enabled = True
            def disable_device(self): self.is_enabled = False
            def reg_event(self, flags): self.flags.append(flags)
            def _ZK__ack_ok(self): pass
        conn = Conn()
        def fail(event): raise RuntimeError('consumer failed')
        with self.assertRaisesRegex(RuntimeError, 'consumer failed'):
            capture_window(conn, on_event=fail, should_yield=lambda: False,
                           stopped=lambda: False, poll_seconds=0.01, min_seconds=0.01, logger=None)
        self.assertEqual(conn._ZK__sock.timeout, 7)
        self.assertEqual(conn.flags, [1, 0])
        self.assertFalse(conn.is_enabled)
