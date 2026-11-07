"""Fingerprint capture using complete ZK frames and template readback."""
from __future__ import annotations

import socket
import time
from collections import deque
from struct import pack, unpack
from typing import Any

from edge_service_zkteco.zk_capture import EventPacketBuffer


class _EnrollmentStream:
    def __init__(self, conn: Any):
        self.conn = conn
        self.sock = conn._ZK__sock
        self.reader = EventPacketBuffer(conn.tcp)
        self.pending: deque[bytes] = deque()

    def packet(self, deadline: float) -> bytes:
        while not self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Fingerprint response deadline expired")
            self.sock.settimeout(remaining)
            self.pending.extend(self.reader.feed(self.sock.recv(65536)))
        return self.pending.popleft()

    def event(self, packet: bytes) -> bool:
        if unpack('<H', packet[:2])[0] != 500:
            return False
        if getattr(self.conn, 'verbose', False):
            print(f'[HUELLA PAQUETE] {packet.hex()}', flush=True)
        self.conn._ZK__ack_ok()
        return True

    def command(self, command: int, payload: bytes = b'') -> None:
        conn = self.conn
        data = conn._ZK__create_header(command, payload, conn._ZK__session_id, conn._ZK__reply_id)
        if conn.tcp:
            self.sock.sendall(conn._ZK__create_tcp_top(data))
        else:
            self.sock.sendto(data, conn._ZK__address)
        deadline = time.monotonic() + 5
        while True:
            packet = self.packet(deadline)
            if self.event(packet):
                continue
            code, _, session, reply = unpack('<4H', packet[:8])
            conn._ZK__reply_id = reply
            if code != 2000 or session != conn._ZK__session_id:
                raise ConnectionError(f'Fingerprint command {command} rejected: {code}')
            return


def _template_data(template: Any) -> Any:
    return getattr(template, 'template', template)


def enroll_fingerprint(conn: Any, user: Any, finger_index: int, timeout: float = 60) -> bool:
    """Ignore undocumented progress codes; only a new saved template is success.

    A complete event frame is consumed before another command is sent. Cleanup
    consumes late events too, so they cannot become the next command response.
    """
    sock = conn._ZK__sock
    old_timeout = sock.gettimeout()
    before = _template_data(conn.get_user_template(uid=user.uid, temp_id=finger_index))
    stream = _EnrollmentStream(conn)
    try:
        stream.command(62)  # CMD_CANCELCAPTURE
        payload = (pack('<24sbb', str(user.user_id).encode(), finger_index, 1)
                   if conn.tcp else pack('<Ib', int(user.user_id), finger_index))
        stream.command(61, payload)  # CMD_STARTENROLL
        deadline = time.monotonic() + timeout
        while True:
            try:
                packet = stream.packet(deadline)
            except (socket.timeout, TimeoutError):
                if stream.reader.buffer:
                    raise ConnectionError('Incomplete fingerprint event frame')
                break
            if not stream.event(packet):
                raise ConnectionError('Unexpected response during fingerprint capture')
    finally:
        try:
            # Cancel first, then unsubscribe and restore normal verification.
            # Each exchange drains late events before accepting its ACK.
            stream.command(62)
            stream.command(500, pack('<I', 0))
            stream.command(60)
            if stream.reader.buffer or stream.pending:
                raise ConnectionError('Unread fingerprint packets after cleanup')
        finally:
            sock.settimeout(old_timeout)
    conn.refresh_data()
    after = _template_data(conn.get_user_template(uid=user.uid, temp_id=finger_index))
    return bool(after) and (not before or after != before)
