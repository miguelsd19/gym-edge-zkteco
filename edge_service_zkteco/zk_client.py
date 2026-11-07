from __future__ import annotations

import json
import queue
import socket
from struct import pack
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from edge_service_zkteco.config import EdgeConfig
from edge_service_zkteco.event_parser import event_to_dict
from edge_service_zkteco.models import USER_DISABLED_FLAG, ZkUserInfo
from edge_service_zkteco.zk_capture import capture_window
from edge_service_zkteco.zk_fingerprint import enroll_fingerprint


class ZkUnavailableError(Exception):
    """Raised when the ZKTeco device cannot be reached or a command fails."""


class ZkCommandTimeoutError(ZkUnavailableError):
    """Raised when the device thread does not answer a command in time."""


class ZkCommandRejectedError(ZkUnavailableError):
    """Raised when the device answered but refused the command."""


def _is_network_error(exc: Exception) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, BrokenPipeError, OSError)):
        return True
    return "network" in type(exc).__name__.lower()


def _write_user_access(conn: Any, user: ZkUserInfo, group_id: str, privilege: int) -> None:
    """Write and verify the effective access group on SpeedFace terminals.

    Legacy 28-byte terminals retain the full-record disabled-bit update.
    """
    if conn.user_packet_size == 72:
        # SpeedFace 6.60: the group_id bytes in CMD_USER_WRQ are not the
        # effective access group. Use the dedicated command, without rewriting
        # the user record (which also contains personal access schedules).
        response = conn._ZK__send_command(22, pack('<II', user.uid, int(group_id)), 1024)
        if not response.get('status'):
            raise ZkCommandRejectedError('Device refused access group update.')
        conn.refresh_data()
        saved_group = _read_access_group(conn, user.uid)
        if saved_group != group_id:
            raise ZkCommandRejectedError(
                f'Access group readback mismatch for user_id={user.user_id}: '
                f'expected={group_id} actual={saved_group}.'
            )
        return
    encoding = conn.encoding
    if conn.user_packet_size == 28:
        payload = pack('<HB5s8sIxBHI', user.uid, privilege,
                       user.password.encode(encoding, errors='ignore'),
                       user.name.encode(encoding, errors='ignore'), user.card,
                       int(group_id or 0), 0, int(user.user_id))
    else:
        raise ZkCommandRejectedError('Unsupported ZK user packet size.')
    response = conn._ZK__send_command(8, payload, 1024)
    if not response.get('status'):
        raise ZkCommandRejectedError('Device refused user access update.')
    conn.refresh_data()
    saved = next((item for item in conn.get_users() if int(item.uid) == user.uid), None)
    if (saved is None or str(saved.user_id) != user.user_id
            or int(saved.privilege) != privilege
            or str(saved.group_id).strip() != group_id):
        raise ZkCommandRejectedError(
            f'User access readback mismatch for user_id={user.user_id}; blocking not verified.'
        )


def _read_access_group(conn: Any, uid: int) -> str:
    response = conn._ZK__send_command(21, pack('<I', uid), 1024)
    data = conn._ZK__data
    if not response.get('status') or len(data) not in (1, 2, 4):
        raise ZkCommandRejectedError('Cannot read effective access group.')
    group = int.from_bytes(data, 'little')
    if not 1 <= group <= 99:
        raise ZkCommandRejectedError(f'Invalid effective access group: {group}.')
    return str(group)


def _to_user_info(user: Any) -> ZkUserInfo:
    return ZkUserInfo(
        uid=int(getattr(user, "uid", 0) or 0),
        user_id=str(getattr(user, "user_id", "") or ""),
        name=str(getattr(user, "name", "") or ""),
        privilege=int(getattr(user, "privilege", 0) or 0),
        password=str(getattr(user, "password", "") or ""),
        group_id=str(getattr(user, "group_id", "") or ""),
        card=int(getattr(user, "card", 0) or 0),
    )


def create_device_user(conn: Any, user_id: str, name: str, group_id: str) -> tuple[ZkUserInfo, bool]:
    """Crea el usuario en el lector (si no existe) y lo deja en `group_id`.

    Devuelve (usuario, creado). Si el user_id ya existe no lo modifica. El
    usuario nuevo se escribe con un UID libre y despues se le asigna el grupo
    con la misma escritura verificada que usa el sync.
    """
    users = conn.get_users()
    existing = next((item for item in users if str(item.user_id).strip() == user_id), None)
    if existing is not None:
        return _to_user_info(existing), False
    uid = max((int(item.uid) for item in users), default=0) + 1
    if uid > 65535:
        raise ZkCommandRejectedError("No hay UID libre en el lector.")
    conn.set_user(uid=uid, name=name[:24], privilege=0, password="", group_id="",
                  user_id=user_id, card=0)
    saved = next((item for item in conn.get_users() if int(item.uid) == uid), None)
    if saved is None or str(saved.user_id).strip() != user_id:
        raise ZkCommandRejectedError(f"El lector no guardo al usuario {user_id}.")
    user = _to_user_info(saved)
    _write_user_access(conn, user, group_id, user.privilege)
    user.group_id = group_id
    return user, True


def enroll_device_fingerprint(conn: Any, user: ZkUserInfo, finger_index: int, timeout: float = 60) -> bool:
    """Pone el lector en modo enrolamiento: la persona pone el dedo 3 veces.

    Usa CMD_STARTENROLL y consume tramas completas del socket de pyzk.
    Devuelve True solo si se puede leer una plantilla nueva o modificada.
    """
    return enroll_fingerprint(conn, user, finger_index, timeout)


# Indice de "dedo" que el SDK de ZKTeco (StartEnrollEx) usa para pedir la cara.
FACE_ENROLL_INDEX = 111
CMD_STARTENROLL = 61
# Resultados de enroll_device_face.
FACE_SAVED = "saved"
FACE_NO_CAPTURE = "no_capture"
FACE_NOT_SAVED = "not_saved"


def _face_count(conn: Any) -> int:
    conn.read_sizes()
    return int(getattr(conn, "faces", 0) or 0)


def enroll_device_face(conn: Any, user: ZkUserInfo, timeout: float, *, quiet_seconds: float = 8.0,
                       on_packet: Callable[[bytes], None] | None = None) -> str:
    """Pone el lector en modo "registro de cara" para el usuario y verifica que quedo.

    pyzk no trae registro de cara: se manda CMD_STARTENROLL con el indice 111,
    como StartEnrollEx del SDK de ZKTeco. El lector avisa con eventos mientras
    la persona se pone enfrente; se espera hasta que deja de mandarlos
    (`quiet_seconds`) o hasta `timeout`. El resultado se confirma con el
    conteo de caras del lector (sube si la guardo). Si el usuario ya tenia cara
    el lector la reemplaza y el conteo no sube: se reporta como no confirmada.

    Devuelve FACE_SAVED, FACE_NO_CAPTURE (el lector no mando ningun evento: nadie
    se puso enfrente) o FACE_NOT_SAVED (hubo captura pero el conteo no subio).
    """
    before = _face_count(conn)
    sock = conn._ZK__sock
    old_timeout = sock.gettimeout()
    events = 0
    try:
        conn.cancel_capture()
        if conn.tcp:
            payload = pack("<24sbb", str(user.user_id).encode(), FACE_ENROLL_INDEX, 1)
        else:
            payload = pack("<Ib", int(user.user_id), FACE_ENROLL_INDEX)
        response = conn._ZK__send_command(CMD_STARTENROLL, payload)
        if not response.get("status"):
            raise ZkCommandRejectedError("El lector no acepto el registro de cara.")
        deadline = time.monotonic() + timeout
        last_event = None
        sock.settimeout(1.0)
        while time.monotonic() < deadline:
            if last_event is not None and time.monotonic() - last_event >= quiet_seconds:
                break
            try:
                data = sock.recv(1032)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("El lector cerro la conexion durante el registro de cara.")
            conn._ZK__ack_ok()
            events += 1
            last_event = time.monotonic()
            if on_packet is not None:
                on_packet(data)
    finally:
        sock.settimeout(old_timeout)
        if events == 0:
            # Nadie se puso enfrente: se saca al lector del modo registro.
            try:
                conn.cancel_capture()
            except Exception:  # noqa: BLE001
                pass
    if events == 0:
        return FACE_NO_CAPTURE
    conn.refresh_data()
    return FACE_SAVED if _face_count(conn) > before else FACE_NOT_SAVED


class _PendingCommand:
    def __init__(self, name: str, func: Callable[[Any], Any]):
        self.name = name
        self.func = func
        self.result: Any = None
        self.error: Exception | None = None
        self.done = threading.Event()


class ZkClient:
    """
    Cliente del terminal ZKTeco sobre pyzk.

    El protocolo ZK usa un unico socket para captura en vivo y comandos, y los
    terminales no toleran conexiones concurrentes, asi que toda la comunicacion
    vive en un unico "device thread" que alterna entre live_capture y ventanas
    de comandos encolados desde otros threads.
    """

    def __init__(
        self,
        config: EdgeConfig,
        logger,
        on_event: Callable[[Any], None],
        zk_factory: Callable[..., Any] | None = None,
    ):
        self.config = config
        self.logger = logger
        self.on_event = on_event
        self._zk_factory = zk_factory
        self._commands: "queue.Queue[_PendingCommand]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Any = None
        self._conn_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._started_at = time.monotonic()
        self._last_success_monotonic: float | None = None
        self._last_success_at = ""
        self._consecutive_failures = 0
        self._event_log_lock = threading.Lock()
        # Grupo efectivo confirmado por UID: {uid: (user_id, grupo, epoch)}.
        # Evita leer del lector el grupo de cada socio en cada sincronizacion;
        # se guarda en disco para que un reinicio de la PC no obligue a releer
        # a todos. Usa hora de reloj (no monotonic) porque sobrevive reinicios.
        self._access_group_cache_lock = threading.Lock()
        self._access_group_cache_dirty = False
        self._access_group_cache_saved_at = time.monotonic()
        self._access_group_cache: dict[int, tuple[str, str, float]] = self._load_access_group_cache()
        self.config.zk_event_log_file.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ API

    # The SpeedFace does not enforce the privilege disabled bit. A confirmed
    # effective access group, rather than that bit, determines sync state.
    verifies_access_group = False

    def get_access_group(self, user: ZkUserInfo) -> str:
        cached = self._cached_access_group(user)
        if cached is not None:
            return cached
        group = self._submit('get_access_group', lambda conn: _read_access_group(conn, user.uid))
        self._remember_access_group(user, group)
        return group

    def save_access_group_cache(self) -> None:
        """Escribe la cache en disco si cambio (al terminar cada sincronizacion)."""
        path = self._access_group_cache_path()
        if path is None:
            return
        with self._access_group_cache_lock:
            if not self._access_group_cache_dirty:
                return
            payload = {
                str(uid): {"user_id": user_id, "group": group, "confirmed_at": confirmed_at}
                for uid, (user_id, group, confirmed_at) in self._access_group_cache.items()
            }
            self._access_group_cache_dirty = False
            self._access_group_cache_saved_at = time.monotonic()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            self.logger.warning("Could not save ZK access group cache: %s", exc)

    def _access_group_cache_path(self):
        return getattr(self.config, "zk_access_group_cache_file", None)

    def _access_group_cache_ttl(self) -> int:
        return getattr(self.config, "zk_access_group_cache_seconds", 21600)

    def _load_access_group_cache(self) -> dict[int, tuple[str, str, float]]:
        path = self._access_group_cache_path()
        if path is None or self._access_group_cache_ttl() <= 0 or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            now = time.time()
            cache = {}
            for uid, entry in raw.items():
                confirmed_at = float(entry["confirmed_at"])
                if 0 <= now - confirmed_at < self._access_group_cache_ttl():
                    cache[int(uid)] = (str(entry["user_id"]), str(entry["group"]), confirmed_at)
            return cache
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # Cache corrupta: se ignora y el edge vuelve a leer del lector.
            self.logger.warning("Ignoring unreadable ZK access group cache: %s", exc)
            return {}

    def _cached_access_group(self, user: ZkUserInfo) -> str | None:
        ttl = self._access_group_cache_ttl()
        with self._access_group_cache_lock:
            entry = self._access_group_cache.get(user.uid)
        if entry is None or ttl <= 0:
            return None
        user_id, group, confirmed_at = entry
        # Si el UID ahora es de otra persona (re-enrolamiento), no vale.
        if user_id != user.user_id or not 0 <= time.time() - confirmed_at < ttl:
            return None
        return group

    def _remember_access_group(self, user: ZkUserInfo, group: str | None) -> None:
        with self._access_group_cache_lock:
            if group is None:
                if self._access_group_cache.pop(user.uid, None) is not None:
                    self._access_group_cache_dirty = True
            else:
                self._access_group_cache[user.uid] = (user.user_id, group, time.time())
                self._access_group_cache_dirty = True
            save_due = time.monotonic() - self._access_group_cache_saved_at >= 60
        # Guardado periodico durante un sync largo: un apagon a la mitad no
        # obliga a releer a todos los ya confirmados.
        if save_due:
            self.save_access_group_cache()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._device_loop,
            daemon=True,
            name="zk-device",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._interrupt_capture()
        if self._thread is not None:
            self._thread.join(timeout=self.config.zk_command_timeout_seconds)
        self._fail_pending_commands("ZK client stopped.")

    def unlock(self, duration_seconds: int | None = None) -> None:
        duration = duration_seconds or self.config.zk_open_door_duration_seconds
        attempts = self.config.zk_open_door_retry_attempts
        backoff = self.config.zk_open_door_retry_backoff_seconds
        last_error: ZkUnavailableError | None = None

        for attempt in range(1, attempts + 1):
            try:
                self._submit("unlock", lambda conn: conn.unlock(time=duration))
                return
            except ZkCommandRejectedError:
                raise
            except ZkUnavailableError as exc:
                last_error = exc
                self.logger.warning(
                    "ZK unlock attempt %s/%s failed: %s",
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts and backoff > 0:
                    time.sleep(backoff * attempt)

        assert last_error is not None
        raise last_error

    def get_users(self) -> list[ZkUserInfo]:
        def read_users(conn: Any) -> Any:
            users = conn.get_users()
            self.verifies_access_group = conn.user_packet_size == 72
            return users

        raw_users = self._submit("get_users", read_users)
        return [_to_user_info(user) for user in raw_users or []]

    def set_user_access(self, user: ZkUserInfo, group_id: str, disabled: bool) -> None:
        # set_user sobreescribe el registro completo del usuario (los templates
        # de cara/huella viven aparte, keyed por uid), por eso se reenvian todos
        # los campos leidos del equipo cambiando solo group_id y el bit de
        # deshabilitado de privilege.
        privilege = user.privilege & ~USER_DISABLED_FLAG
        if disabled:
            privilege |= USER_DISABLED_FLAG
        try:
            self._submit(
                "set_user",
                lambda conn: _write_user_access(conn, user, str(group_id), privilege),
            )
        except Exception:
            # Estado incierto: la siguiente sincronizacion vuelve a leerlo.
            self._remember_access_group(user, None)
            raise
        if self.verifies_access_group:
            # _write_user_access ya verifico el grupo leyendolo del lector.
            self._remember_access_group(user, str(group_id))

    def create_user(self, user_id: str, name: str, group_id: str) -> tuple[ZkUserInfo, bool]:
        user, created = self._submit(
            "create_user", lambda conn: create_device_user(conn, user_id, name, str(group_id))
        )
        if created and self.verifies_access_group:
            self._remember_access_group(user, str(group_id))
        return user, created

    def enroll_fingerprint(self, user: ZkUserInfo, finger_index: int, timeout: float) -> bool:
        # Bloquea el hilo del lector mientras la persona pone el dedo: en ese
        # rato no se capturan entradas en vivo (el lector sigue decidiendo solo).
        return self._submit(
            "enroll_fingerprint",
            lambda conn: enroll_device_fingerprint(conn, user, finger_index, timeout),
            timeout=timeout + 30,
        )

    def enroll_face(self, user: ZkUserInfo, timeout: float) -> str:
        # Igual que la huella: bloquea el hilo del lector mientras dura.
        return self._submit(
            "enroll_face",
            lambda conn: enroll_device_face(conn, user, timeout),
            timeout=timeout + 30,
        )

    def delete_user(self, user: ZkUserInfo) -> None:
        # Borra el usuario y sus templates (cara/huella) del equipo.
        self._submit("delete_user", lambda conn: conn.delete_user(uid=user.uid))
        self._remember_access_group(user, None)
        self.save_access_group_cache()

    def refresh_data(self) -> None:
        self._submit("refresh_data", lambda conn: conn.refresh_data())

    def healthcheck(self) -> bool:
        self._submit("get_time", lambda conn: conn.get_time())
        return True

    # -------------------------------------------------------- passive health

    def note_passive_success(self) -> None:
        self._mark_success()

    def last_success_at_iso(self) -> str:
        with self._state_lock:
            return self._last_success_at

    def seconds_since_last_success_or_started(self) -> float:
        with self._state_lock:
            reference = (
                self._last_success_monotonic
                if self._last_success_monotonic is not None
                else self._started_at
            )
        return time.monotonic() - reference

    def consecutive_failures(self) -> int:
        with self._state_lock:
            return self._consecutive_failures

    # ------------------------------------------------------------- internals

    def _submit(self, name: str, func: Callable[[Any], Any], timeout: float | None = None) -> Any:
        if self._stop.is_set() or self._thread is None or not self._thread.is_alive():
            raise ZkUnavailableError("ZK device thread is not running.")

        command = _PendingCommand(name, func)
        self._commands.put(command)
        self._interrupt_capture()

        wait_seconds = timeout or self.config.zk_command_timeout_seconds
        if not command.done.wait(wait_seconds):
            raise ZkCommandTimeoutError(
                f"Timed out waiting for ZK command '{name}' after {wait_seconds}s."
            )
        if command.error is not None:
            raise command.error
        return command.result

    def _interrupt_capture(self) -> None:
        with self._conn_lock:
            conn = self._conn
        if conn is not None:
            try:
                conn.end_live_capture = True
            except Exception:  # noqa: BLE001
                pass

    def _device_loop(self) -> None:
        initial_backoff = min(1.0, self.config.zk_reconnect_max_backoff_seconds)
        backoff = initial_backoff
        while not self._stop.is_set():
            conn = self._connect()
            if conn is None:
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self.config.zk_reconnect_max_backoff_seconds)
                continue

            backoff = initial_backoff
            try:
                self._run_session(conn)
            except Exception as exc:  # noqa: BLE001
                self._mark_failure()
                self.logger.warning("ZK device session ended with error: %s", exc)
            finally:
                with self._conn_lock:
                    self._conn = None
                try:
                    conn.disconnect()
                except Exception:  # noqa: BLE001
                    pass

        self._fail_pending_commands("ZK client stopped.")

    def _connect(self) -> Any:
        try:
            conn = self._build_zk().connect()
        except Exception as exc:  # noqa: BLE001
            self._mark_failure()
            self.logger.warning(
                "Could not connect to ZKTeco device %s:%s: %s",
                self.config.zk_device_host,
                self.config.zk_device_port,
                exc,
            )
            return None

        with self._conn_lock:
            self._conn = conn
        self._mark_success()
        self.logger.info(
            "Connected to ZKTeco device %s:%s.",
            self.config.zk_device_host,
            self.config.zk_device_port,
        )
        return conn

    def _build_zk(self) -> Any:
        factory = self._zk_factory
        if factory is None:
            # Import lazy: permite correr la suite de tests sin pyzk instalado.
            from zk import ZK

            factory = ZK
        return factory(
            self.config.zk_device_host,
            port=self.config.zk_device_port,
            timeout=self.config.zk_device_timeout_seconds,
            password=self.config.zk_device_password,
            force_udp=self.config.zk_force_udp,
            ommit_ping=self.config.zk_omit_ping,
        )

    def _run_session(self, conn: Any) -> None:
        while not self._stop.is_set():
            self._execute_pending(conn)
            if self._stop.is_set():
                return
            if self._keepalive_due():
                conn.get_time()
                self._mark_success()
            self._capture_until_interrupted(conn)

    def _capture_until_interrupted(self, conn: Any) -> None:
        capture_window(
            conn, on_event=self._handle_event,
            should_yield=lambda: not self._commands.empty() or self._keepalive_due(),
            stopped=self._stop.is_set,
            poll_seconds=self.config.zk_capture_poll_seconds,
            min_seconds=getattr(self.config, 'zk_capture_min_window_seconds', 0.5),
            logger=self.logger,
        )

    def _execute_pending(self, conn: Any) -> None:
        for _ in range(getattr(self.config, 'zk_command_batch_size', 4)):
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return

            try:
                command.result = command.func(conn)
            except Exception as exc:  # noqa: BLE001
                if _is_network_error(exc):
                    command.error = ZkUnavailableError(
                        f"Network error while running ZK command '{command.name}': {exc}"
                    )
                    command.done.set()
                    raise
                command.error = ZkCommandRejectedError(
                    f"ZK device rejected command '{command.name}': {exc}"
                )
                # El equipo respondio (aunque rechazo el comando): la
                # comunicacion sigue viva y la sesion continua.
                self._mark_success()
                command.done.set()
                continue

            self._mark_success()
            command.done.set()

    def _keepalive_due(self) -> bool:
        return (
            self.seconds_since_last_success_or_started()
            >= self.config.zk_keepalive_seconds
        )

    def _handle_event(self, event: Any) -> None:
        self._mark_success()
        self._append_event_log(event)
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001
            self.logger.exception("ZK event handler failed.")

    def _append_event_log(self, event: Any) -> None:
        record = event_to_dict(event)
        record["received_at"] = datetime.now(timezone.utc).isoformat()
        try:
            with self._event_log_lock:
                with self.config.zk_event_log_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=True) + "\n")
        except OSError as exc:
            self.logger.warning("Could not append to ZK event log: %s", exc)

    def _fail_pending_commands(self, message: str) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            command.error = ZkUnavailableError(message)
            command.done.set()

    def _mark_success(self) -> None:
        with self._state_lock:
            self._last_success_monotonic = time.monotonic()
            self._last_success_at = datetime.now(timezone.utc).isoformat()
            self._consecutive_failures = 0

    def _mark_failure(self) -> None:
        with self._state_lock:
            self._consecutive_failures += 1
