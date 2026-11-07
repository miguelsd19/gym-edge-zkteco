"""Registro remoto de huella y cara en el lector ZKTeco (SpeedFace 6.60).

pyzk 0.9 lee las notificaciones del registro con posiciones fijas y se
desfasa: la respuesta de un comando terminaba consumiendo un evento pendiente
("TCP packet invalid"). Aqui se leen tramas TCP completas, se confirma cada
evento y, al cerrar, se consumen los eventos tardios antes de aceptar el ACK.

Fin del registro: el lector manda un evento final con
`resultado (H) + tamano (H) + user_id`. En cuanto llega se sale; ya no se
espera a que se agote el tiempo (antes recepcion esperaba 4 minutos y el edge
no recibia entradas en vivo mientras tanto). Las marcas de acceso que llegan
mientras la persona sigue frente al lector traen el user_id al inicio y no
cuentan como avance del registro.

Huella: exito solo si despues se lee una plantilla nueva o distinta.
Cara: exito si el lector confirma con resultado 0 en el evento final o si sube
el conteo de caras.
"""
from __future__ import annotations

import socket
import time
from collections import deque
from struct import pack, unpack
from typing import Any, Callable

from edge_service_zkteco.zk_capture import EventPacketBuffer

CMD_STARTVERIFY = 60
CMD_STARTENROLL = 61
CMD_CANCELCAPTURE = 62
CMD_REG_EVENT = 500
CMD_ACK_OK = 2000

# Indice de "dedo" que el SDK de ZKTeco (StartEnrollEx) usa para pedir la cara.
FACE_ENROLL_INDEX = 111
FACE_SAVED = "saved"
FACE_NO_CAPTURE = "no_capture"
FACE_NOT_SAVED = "not_saved"

# Tamanos de las marcas de acceso que conoce pyzk (decode_attendance).
_ATTENDANCE_SIZES = (12, 32, 36, 52)


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
                raise TimeoutError("Enrollment response deadline expired")
            self.sock.settimeout(remaining)
            self.pending.extend(self.reader.feed(self.sock.recv(65536)))
        return self.pending.popleft()

    def event(self, packet: bytes) -> bool:
        if unpack('<H', packet[:2])[0] != CMD_REG_EVENT:
            return False
        if getattr(self.conn, 'verbose', False):
            print(f'[REGISTRO PAQUETE] {packet.hex()}', flush=True)
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
            if code != CMD_ACK_OK or session != conn._ZK__session_id:
                raise ConnectionError(f'Enrollment command {command} rejected: {code}')
            return


def enrollment_result(payload: bytes, user_id: str) -> int | None:
    """Resultado del evento final del registro, o None si es otro evento.

    Formato observado en Norias: resultado (H), tamano (H) y el user_id
    rellenado con ceros (p. ej. 0000 4e03 "990001"...). 0 = guardado.
    """
    uid = str(user_id).encode()
    if len(payload) < 4 + len(uid) or payload[4:4 + len(uid)] != uid:
        return None
    return unpack('<H', payload[:2])[0]


def _is_attendance(payload: bytes, user_id: str) -> bool:
    uid = str(user_id).encode()
    return len(payload) in _ATTENDANCE_SIZES or (len(payload) >= len(uid) and payload.startswith(uid))


def _template_data(template: Any) -> Any:
    return getattr(template, 'template', template)


def _capture(conn: Any, user: Any, index: int, timeout: float, *, quiet_seconds: float | None = None,
             on_packet: Callable[[bytes], None] | None = None) -> tuple[int, int | None]:
    """Pide el registro y espera el evento final.

    Devuelve (eventos de avance, resultado final o None). Sale al llegar el
    evento final; si no llega, a los `quiet_seconds` sin eventos de avance
    (si se pide) o al agotar `timeout`. Siempre cancela, quita la suscripcion
    y regresa al lector a verificar, consumiendo los eventos tardios.
    """
    sock = conn._ZK__sock
    old_timeout = sock.gettimeout()
    stream = _EnrollmentStream(conn)
    progress = 0
    final: int | None = None
    try:
        stream.command(CMD_CANCELCAPTURE)
        payload = (pack('<24sbb', str(user.user_id).encode(), index, 1)
                   if conn.tcp else pack('<Ib', int(user.user_id), index))
        stream.command(CMD_STARTENROLL, payload)
        deadline = time.monotonic() + timeout
        last_progress: float | None = None
        while True:
            wait_until = deadline
            if quiet_seconds is not None and last_progress is not None:
                wait_until = min(deadline, last_progress + quiet_seconds)
            try:
                packet = stream.packet(wait_until)
            except (socket.timeout, TimeoutError):
                if stream.reader.buffer:
                    raise ConnectionError('Incomplete enrollment event frame')
                if time.monotonic() < deadline and wait_until < deadline:
                    break  # silencio tras el ultimo avance
                if time.monotonic() >= deadline:
                    break
                continue
            if not stream.event(packet):
                raise ConnectionError('Unexpected response during enrollment')
            if on_packet is not None:
                on_packet(packet)
            event_payload = packet[8:]
            final = enrollment_result(event_payload, user.user_id)
            if final is not None:
                break
            if _is_attendance(event_payload, user.user_id):
                continue  # la persona ya marca: no es avance del registro
            progress += 1
            last_progress = time.monotonic()
    finally:
        try:
            stream.command(CMD_CANCELCAPTURE)
            stream.command(CMD_REG_EVENT, pack('<I', 0))
            stream.command(CMD_STARTVERIFY)
            if stream.reader.buffer or stream.pending:
                raise ConnectionError('Unread enrollment packets after cleanup')
        finally:
            sock.settimeout(old_timeout)
    return progress, final


def enroll_fingerprint(conn: Any, user: Any, finger_index: int, timeout: float = 60) -> bool:
    """Exito solo si se lee una plantilla nueva o distinta de la que habia."""
    before = _template_data(conn.get_user_template(uid=user.uid, temp_id=finger_index))
    _capture(conn, user, finger_index, timeout)
    conn.refresh_data()
    after = _template_data(conn.get_user_template(uid=user.uid, temp_id=finger_index))
    return bool(after) and (not before or after != before)


def _face_count(conn: Any) -> int:
    conn.read_sizes()
    return int(getattr(conn, "faces", 0) or 0)


def enroll_face(conn: Any, user: Any, timeout: float, *, quiet_seconds: float = 8.0,
                on_packet: Callable[[bytes], None] | None = None) -> str:
    """FACE_SAVED, FACE_NO_CAPTURE (nadie se puso enfrente) o FACE_NOT_SAVED.

    Si el usuario ya tenia cara, el lector la reemplaza y el conteo no sube;
    entonces solo cuenta como guardada si el evento final trae resultado 0.
    """
    before = _face_count(conn)
    progress, final = _capture(conn, user, FACE_ENROLL_INDEX, timeout,
                               quiet_seconds=quiet_seconds, on_packet=on_packet)
    if final is None and progress == 0:
        return FACE_NO_CAPTURE
    if final == 0:
        return FACE_SAVED
    conn.refresh_data()
    return FACE_SAVED if _face_count(conn) > before else FACE_NOT_SAVED
