from __future__ import annotations

import json
import queue
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from edge_service_zkteco.config import EdgeConfig
from edge_service_zkteco.event_parser import event_to_dict
from edge_service_zkteco.models import USER_DISABLED_FLAG, ZkUserInfo


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
        self.config.zk_event_log_file.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ API

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
        raw_users = self._submit("get_users", lambda conn: conn.get_users())
        users: list[ZkUserInfo] = []
        for user in raw_users or []:
            users.append(
                ZkUserInfo(
                    uid=int(getattr(user, "uid", 0) or 0),
                    user_id=str(getattr(user, "user_id", "") or ""),
                    name=str(getattr(user, "name", "") or ""),
                    privilege=int(getattr(user, "privilege", 0) or 0),
                    password=str(getattr(user, "password", "") or ""),
                    group_id=str(getattr(user, "group_id", "") or ""),
                    card=int(getattr(user, "card", 0) or 0),
                )
            )
        return users

    def set_user_access(self, user: ZkUserInfo, group_id: str, disabled: bool) -> None:
        # set_user sobreescribe el registro completo del usuario (los templates
        # de cara/huella viven aparte, keyed por uid), por eso se reenvian todos
        # los campos leidos del equipo cambiando solo group_id y el bit de
        # deshabilitado de privilege.
        privilege = user.privilege & ~USER_DISABLED_FLAG
        if disabled:
            privilege |= USER_DISABLED_FLAG
        self._submit(
            "set_user",
            lambda conn: conn.set_user(
                uid=user.uid,
                name=user.name,
                privilege=privilege,
                password=user.password,
                group_id=str(group_id),
                user_id=user.user_id,
                card=user.card,
            ),
        )

    def delete_user(self, user: ZkUserInfo) -> None:
        # Borra el usuario y sus templates (cara/huella) del equipo.
        self._submit("delete_user", lambda conn: conn.delete_user(uid=user.uid))

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

    def _submit(self, name: str, func: Callable[[Any], Any]) -> Any:
        if self._stop.is_set() or self._thread is None or not self._thread.is_alive():
            raise ZkUnavailableError("ZK device thread is not running.")

        command = _PendingCommand(name, func)
        self._commands.put(command)
        self._interrupt_capture()

        if not command.done.wait(self.config.zk_command_timeout_seconds):
            raise ZkCommandTimeoutError(
                f"Timed out waiting for ZK command '{name}' "
                f"after {self.config.zk_command_timeout_seconds}s."
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
        conn.end_live_capture = False
        for event in conn.live_capture(new_timeout=self.config.zk_capture_poll_seconds):
            if event is not None:
                self._handle_event(event)
            if self._stop.is_set() or not self._commands.empty() or self._keepalive_due():
                conn.end_live_capture = True

    def _execute_pending(self, conn: Any) -> None:
        while True:
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
