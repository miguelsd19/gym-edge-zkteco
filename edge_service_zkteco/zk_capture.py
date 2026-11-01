"""Bounded event windows over the existing pyzk socket, without user downloads."""
from __future__ import annotations

import socket
import time
from datetime import datetime
from struct import unpack
from types import SimpleNamespace
from typing import Any, Callable


class EventPacketBuffer:
    def __init__(self, tcp: bool):
        self.tcp = tcp
        self.buffer = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            raise ConnectionError("ZK event socket closed")
        if not self.tcp:
            if len(chunk) < 8:
                raise ValueError("Short ZK UDP event header")
            return [chunk]
        self.buffer += chunk
        packets = []
        while len(self.buffer) >= 8:
            magic1, magic2, size = unpack("<HHI", self.buffer[:8])
            if (magic1, magic2) != (0x5050, 0x7D82) or not 8 <= size <= 65536:
                raise ValueError("Invalid ZK TCP event frame")
            if len(self.buffer) < 8 + size:
                break
            packets.append(self.buffer[8:8 + size])
            self.buffer = self.buffer[8 + size:]
        return packets


def decode_attendance(data: bytes) -> list[Any]:
    """Known pyzk 0.9 attendance layouts; never guess verification failures."""
    if len(data) in (12, 32, 36, 52):
        size = len(data)
    else:
        size = next((n for n in (52, 36, 32, 12) if data and len(data) % n == 0), 0)
    if not size:
        raise ValueError(f"Unsupported ZK attendance payload length: {len(data)}")
    events = []
    for offset in range(0, len(data), size):
        record = data[offset:offset + size]
        if size == 12:
            user_id, status, punch, stamp = unpack("<IBB6s", record)
            user_id = str(user_id)
        else:
            identifier, status, punch, stamp = unpack("<24sBB6s", record[:32])
            user_id = identifier.split(b"\0", 1)[0].decode("utf-8", errors="strict")
        year, month, day, hour, minute, second = unpack("6B", stamp)
        events.append(SimpleNamespace(
            uid=None, user_id=user_id, status=status, punch=punch,
            timestamp=datetime(2000 + year, month, day, hour, minute, second),
        ))
    return events


def capture_window(
    conn: Any, *, on_event: Callable[[Any], None], should_yield: Callable[[], bool],
    stopped: Callable[[], bool], poll_seconds: float, min_seconds: float, logger: Any,
) -> None:
    sock = conn._ZK__sock
    old_timeout = sock.gettimeout()
    was_enabled = conn.is_enabled
    reader = EventPacketBuffer(conn.tcp)
    try:
        conn.cancel_capture()
        conn.verify_user()
        if not conn.is_enabled:
            conn.enable_device()
        conn.reg_event(1)  # EF_ATTLOG: no unconfirmed failure-event parsing.
        began = time.monotonic()
        partial_since = None
        while not stopped():
            if not reader.buffer and time.monotonic() - began >= min_seconds and should_yield():
                break
            timeout = min(poll_seconds, max(0.01, min_seconds - (time.monotonic() - began)))
            if time.monotonic() - began >= min_seconds:
                timeout = poll_seconds
            sock.settimeout(timeout)
            try:
                packets = reader.feed(sock.recv(65536))
            except socket.timeout:
                if partial_since is not None and time.monotonic() - partial_since >= 5:
                    raise ConnectionError("Incomplete ZK event frame timed out")
                continue
            if reader.buffer:
                partial_since = partial_since or time.monotonic()
            else:
                partial_since = None
            # Deliver every complete frame from a recv before yielding to commands.
            for packet in packets:
                command = unpack("<4H", packet[:8])[0]
                conn._ZK__ack_ok()
                if command != 500:
                    logger.debug("Ignored ZK event packet command=%s.", command)
                    continue
                if len(packet) == 8:
                    continue
                try:
                    events = decode_attendance(packet[8:])
                except (ValueError, UnicodeError) as exc:
                    logger.warning("Ignored malformed ZK attendance event: %s", exc)
                    continue
                for event in events:
                    on_event(event)
    finally:
        # Restore command timeout before pyzk's synchronous unregistration.
        sock.settimeout(old_timeout)
        try:
            conn.reg_event(0)
        finally:
            if not was_enabled:
                conn.disable_device()
