from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from edge_service_zkteco.backend_api import BackendApiClient, BackendUnavailableError
from edge_service_zkteco.config import EdgeConfig
from edge_service_zkteco.control_server import EdgeControlServer
from edge_service_zkteco.event_parser import parse_event_candidate
from edge_service_zkteco.failed_attempts import FailedAttemptStore
from edge_service_zkteco.file_event_source import FileEventSource
from edge_service_zkteco.logging_setup import configure_logging
from edge_service_zkteco.models import USER_DISABLED_FLAG, FailedAttempt, ZkUserInfo
from edge_service_zkteco.state_store import ProcessedEventStore
from edge_service_zkteco.zk_client import ZkClient, ZkCommandRejectedError, ZkUnavailableError


_ROLE_SYNC_LOCK = threading.RLock()


class _HealthState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_main_loop_at = time.monotonic()

    def touch(self) -> None:
        with self._lock:
            self._last_main_loop_at = time.monotonic()

    def seconds_since_touch(self) -> float:
        with self._lock:
            last_main_loop_at = self._last_main_loop_at
        return time.monotonic() - last_main_loop_at


def _drain_access_events(
    event_queue: "queue.Queue[Any]",
    process_event: Callable[[Any], None],
    health_state: _HealthState,
    limit: int = 16,
) -> None:
    """Atiende accesos entre comandos del sync, usando el mismo socket del lector."""
    health_state.touch()
    for _ in range(limit):
        try:
            event = event_queue.get_nowait()
        except queue.Empty:
            break
        process_event(event)
        health_state.touch()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _SyncProgress:
    """Avance de la sincronizacion completa de usuarios, para reportarlo al backend.

    Solo lo actualizan los syncs completos (al arrancar, programados o pedidos
    sin member_ids); los inmediatos de un socio no mueven la barra. Lo escribe
    el hilo que sincroniza y lo lee el reporte de salud.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._reason = ""
        self._processed = 0
        self._total = 0
        self._started_at = ""
        self._started_monotonic = 0.0
        self._last_completed_at = ""
        self._last_duration_seconds = 0
        self._last_counters: dict[str, int] = {}
        self._last_error = ""

    def start(self, reason: str) -> None:
        with self._lock:
            self._running = True
            self._reason = reason
            self._processed = 0
            self._total = 0
            self._started_at = _utc_now_iso()
            self._started_monotonic = time.monotonic()

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = max(0, int(total))

    def step(self) -> None:
        with self._lock:
            self._processed += 1

    def finish(self, counters: dict[str, int]) -> None:
        with self._lock:
            self._running = False
            self._processed = self._total
            self._last_completed_at = _utc_now_iso()
            self._last_duration_seconds = int(time.monotonic() - self._started_monotonic)
            self._last_counters = {key: int(value) for key, value in counters.items()}
            self._last_error = ""

    def fail(self, exc: BaseException) -> None:
        with self._lock:
            self._running = False
            self._last_error = str(exc)[:200]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "reason": self._reason,
                "processed": self._processed,
                "total": self._total,
                "started_at": self._started_at,
                "last_completed_at": self._last_completed_at,
                "last_duration_seconds": self._last_duration_seconds,
                "last_counters": dict(self._last_counters),
                "last_error": self._last_error,
            }


class _ReaderSyncEventLog:
    """Cambios aplicados al lector para el reporte diario del backend.

    Registra bloqueos/desbloqueos y los socios que no se pudieron actualizar
    (no estan en el lector, o el lector no confirmo). Estos dos ultimos se
    registran una vez por dia y socio, para no repetirlos en cada sync. Se
    envian al terminar cada sincronizacion; si el backend no responde se
    conservan (hasta MAX_PENDING) y se reintentan en la siguiente.
    """

    MAX_PENDING = 5000
    BATCH_SIZE = 500

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = []
        self._reported_today: set[tuple[str, str, str]] = set()
        self._reported_day = ""

    def record(self, *, action: str, assignment: Any, identifier: str, reason: str = "") -> None:
        now = datetime.now(timezone.utc)
        local_day = datetime.now().date().isoformat()
        key = assignment.member_id or identifier
        with self._lock:
            if self._reported_day != local_day:
                self._reported_day = local_day
                self._reported_today.clear()
            if action in {"missing", "failed"}:
                marker = (local_day, key, action)
                if marker in self._reported_today:
                    return
                self._reported_today.add(marker)
            self._pending.append({
                "member_id": assignment.member_id or None,
                "member_name": (assignment.member_name or "")[:200],
                "device_user_id": identifier[:64],
                "action": action,
                "reason": (reason or "")[:200],
                "occurred_at": now.isoformat(timespec="seconds"),
            })
            if len(self._pending) > self.MAX_PENDING:
                self._pending = self._pending[-self.MAX_PENDING:]

    def flush(self, *, backend_api: Any, logger: logging.Logger) -> None:
        while True:
            with self._lock:
                batch = self._pending[: self.BATCH_SIZE]
                self._pending = self._pending[self.BATCH_SIZE:]
            if not batch:
                return
            try:
                backend_api.report_reader_sync_events(batch)
            except BackendUnavailableError as exc:
                with self._lock:
                    self._pending = (batch + self._pending)[-self.MAX_PENDING:]
                logger.warning("Could not report reader sync events (%s pending): %s", len(self._pending), exc)
                return

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


class _FileDeviceStub:
    """
    Sustituto del ZkClient en modo file (pruebas sin hardware).

    Mantiene el mismo contrato de salud pasiva y comandos; la apertura es un
    no-op logueado y la gestion de usuarios no esta disponible.
    """

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def unlock(self, duration_seconds: int | None = None) -> None:
        self.logger.info(
            "File mode: unlock no-op (duration=%s).",
            duration_seconds if duration_seconds is not None else "-",
        )

    def get_users(self) -> list[ZkUserInfo]:
        raise ZkUnavailableError("User role sync is not available in file mode.")

    def set_user_access(self, user: ZkUserInfo, group_id: str, disabled: bool) -> None:
        raise ZkUnavailableError("User role sync is not available in file mode.")

    def delete_user(self, user: ZkUserInfo) -> None:
        raise ZkUnavailableError("User deletion is not available in file mode.")

    def refresh_data(self) -> None:
        return None

    def healthcheck(self) -> bool:
        return True

    def note_passive_success(self) -> None:
        return None

    def last_success_at_iso(self) -> str:
        return ""

    def seconds_since_last_success_or_started(self) -> float:
        return 0.0

    def consecutive_failures(self) -> int:
        return 0


def _is_duplicate_recent_grant(
    identifier: str,
    recent_grants: dict[str, float],
    cooldown_seconds: float,
) -> bool:
    if cooldown_seconds <= 0:
        return False

    now = time.monotonic()
    expired_identifiers = [
        value
        for value, granted_at in recent_grants.items()
        if now - granted_at > cooldown_seconds
    ]
    for expired_identifier in expired_identifiers:
        recent_grants.pop(expired_identifier, None)

    granted_at = recent_grants.get(identifier)
    return granted_at is not None and now - granted_at <= cooldown_seconds


def _is_stale_event(happened_at: str, max_event_age_seconds: int) -> bool:
    if not happened_at or max_event_age_seconds <= 0:
        return False

    normalized = happened_at.replace("Z", "+00:00")
    try:
        event_dt = datetime.fromisoformat(normalized)
    except ValueError:
        return False

    if event_dt.tzinfo is None:
        # Los timestamps del terminal ZK son hora local naive (reloj del equipo).
        age_seconds = (datetime.now() - event_dt).total_seconds()
    else:
        age_seconds = (
            datetime.now(timezone.utc) - event_dt.astimezone(timezone.utc)
        ).total_seconds()
    return age_seconds > max_event_age_seconds


def _process_event(
    *,
    event: Any,
    logger: logging.Logger,
    config: EdgeConfig,
    backend_api: BackendApiClient,
    device: Any,
    failed_attempts: FailedAttemptStore,
    processed_events: ProcessedEventStore,
    recent_grants: dict[str, float],
) -> None:
    candidate = parse_event_candidate(
        event,
        allowed_punch_codes=config.zk_allowed_punch_codes,
        allowed_status_codes=config.zk_allowed_status_codes,
    )
    if not candidate:
        logger.debug("Skipping ZK event without identifier or filtered out: %s", event)
        return

    if _is_stale_event(candidate.happened_at, config.zk_max_event_age_seconds):
        logger.debug(
            "Skipping stale ZK event %s identifier=%s happened_at=%s",
            candidate.event_key,
            candidate.identifier,
            candidate.happened_at or "-",
        )
        return

    if processed_events.contains(candidate.event_key):
        return
    processed_events.remember(candidate.event_key)

    if _is_duplicate_recent_grant(
        candidate.identifier,
        recent_grants,
        config.granted_identifier_cooldown_seconds,
    ):
        logger.info(
            "Skipping duplicate granted identifier %s from ZK event %s.",
            candidate.identifier,
            candidate.event_key,
        )
        return

    logger.info(
        "Candidate ZK event received: identifier=%s status=%s punch=%s happened_at=%s",
        candidate.identifier,
        candidate.status if candidate.status is not None else "-",
        candidate.punch if candidate.punch is not None else "-",
        candidate.happened_at or "-",
    )

    try:
        decision = backend_api.validate(
            candidate.identifier,
            event_timestamp=candidate.happened_at,
        )
    except BackendUnavailableError as exc:
        failed_attempts.append(
            FailedAttempt.create(identifier=candidate.identifier, error=str(exc))
        )
        logger.warning(
            "Backend unavailable for identifier %s. Access denied by safety rule. Error: %s",
            candidate.identifier,
            exc,
        )
        return

    if decision.should_open_door:
        recent_grants[candidate.identifier] = time.monotonic()
        if config.zk_open_door_on_grant:
            try:
                device.unlock(config.zk_open_door_duration_seconds)
            except ZkUnavailableError as exc:
                failed_attempts.append(
                    FailedAttempt.create(identifier=candidate.identifier, error=str(exc))
                )
                logger.warning(
                    "Access was granted for %s but door open command failed: %s",
                    candidate.identifier,
                    exc,
                )

    logger.info(
        "Access result: granted=%s should_open=%s member=%s reason=%s event_id=%s",
        decision.granted,
        decision.should_open_door,
        decision.member_name or "-",
        decision.reason or "-",
        decision.event_id or "-",
    )


def _handle_remote_open(
    *,
    payload: dict[str, Any],
    logger: logging.Logger,
    config: EdgeConfig,
    device: Any,
) -> dict[str, Any]:
    requested_by = str(payload.get("requested_by") or "").strip() or "unknown"
    access_point_id = str(payload.get("access_point_id") or "").strip() or "-"

    device.unlock(config.zk_open_door_duration_seconds)

    logger.info(
        "Remote open command executed. access_point_id=%s requested_by=%s",
        access_point_id,
        requested_by,
    )
    return {
        "success": True,
        "message": "Torniquete abierto correctamente.",
        "requested_by": requested_by,
        "access_point_id": access_point_id,
    }


def _build_edge_health_payload(*, device: Any, is_file_mode: bool) -> tuple[str, str, str]:
    if is_file_mode:
        return ("healthy", "Modo file activo (sin dispositivo ZKTeco).", "")

    consecutive_failures = device.consecutive_failures()
    no_success_seconds = device.seconds_since_last_success_or_started()
    last_success_at = device.last_success_at_iso()

    if consecutive_failures >= 5 or no_success_seconds >= 45:
        if last_success_at:
            return (
                "degraded",
                f"Sin respuesta del ZKTeco desde hace {int(no_success_seconds)}s.",
                last_success_at,
            )
        return (
            "degraded",
            "El edge aun no logra una comunicacion exitosa con el ZKTeco.",
            "",
        )

    return ("healthy", "Conectado al ZKTeco.", last_success_at)


def _identifier_for_assignment(*, assignment, access_mode: str) -> str:
    if access_mode == "biometric":
        return assignment.device_user_id or assignment.rfid_code
    return assignment.rfid_code or assignment.device_user_id


def _sync_user_roles(
    *,
    logger: logging.Logger,
    config: EdgeConfig,
    backend_api: BackendApiClient,
    device: Any,
    reason: str,
    member_ids: list[str] | None = None,
    progress_callback: Callable[[], None] | None = None,
    sync_progress: _SyncProgress | None = None,
    event_log: _ReaderSyncEventLog | None = None,
) -> dict[str, int]:
    tracking = (
        sync_progress is not None
        and member_ids is None
        and config.zk_user_role_sync_enabled
    )
    if tracking:
        sync_progress.start(reason)
    try:
        counters = _run_user_role_sync(
            logger=logger,
            config=config,
            backend_api=backend_api,
            device=device,
            reason=reason,
            member_ids=member_ids,
            progress_callback=progress_callback,
            sync_progress=sync_progress if tracking else None,
            event_log=event_log,
        )
    except Exception as exc:
        if tracking:
            sync_progress.fail(exc)
        raise
    finally:
        # Lo aplicado antes de un error tambien se reporta.
        if event_log is not None:
            event_log.flush(backend_api=backend_api, logger=logger)
    if tracking:
        sync_progress.finish(counters)
    return counters


def _run_user_role_sync(
    *,
    logger: logging.Logger,
    config: EdgeConfig,
    backend_api: BackendApiClient,
    device: Any,
    reason: str,
    member_ids: list[str] | None,
    progress_callback: Callable[[], None] | None,
    sync_progress: _SyncProgress | None,
    event_log: _ReaderSyncEventLog | None = None,
) -> dict[str, int]:
    counters = {"assignments": 0, "updated": 0, "unchanged": 0, "missing": 0, "skipped": 0, "failed": 0}
    if not config.zk_user_role_sync_enabled:
        return counters

    # A targeted renewal/activation supersedes any older full-sync snapshot.
    # Serialize each user's check/write with revision changes so a stale write
    # cannot land after a newer member update has completed.
    with _ROLE_SYNC_LOCK:
        revision = getattr(device, "_role_sync_revision", 0) + 1
        device._role_sync_revision = revision
        revisions = getattr(device, "_role_sync_revisions", {})
        for member_id in member_ids or ["*"]:
            revisions[member_id] = revision
        device._role_sync_revisions = revisions

    if progress_callback is not None:
        progress_callback()
    assignments = backend_api.fetch_user_roles(member_ids=member_ids)
    counters["assignments"] = len(assignments)
    if sync_progress is not None:
        sync_progress.set_total(len(assignments))
    if progress_callback is not None:
        progress_callback()

    users = device.get_users()
    if progress_callback is not None:
        progress_callback()

    users_by_id: dict[str, ZkUserInfo] = {}
    for user in users:
        user_id = (user.user_id or "").strip()
        if user_id:
            users_by_id.setdefault(user_id, user)
        users_by_id.setdefault(str(user.uid), user)

    for assignment in assignments:
        if sync_progress is not None:
            sync_progress.step()
        if progress_callback is not None:
            progress_callback()
        with _ROLE_SYNC_LOCK:
            if max(revisions.get("*", 0), revisions.get(assignment.member_id, 0)) > revision:
                counters["skipped"] += 1
                logger.info("ZK user role sync skipped superseded assignment. member_id=%s", assignment.member_id)
                continue
            target_user_type = (assignment.target_user_type or "").strip()
            if target_user_type not in {"normal", "blackList"}:
                counters["skipped"] += 1
                continue

            identifier = _identifier_for_assignment(
                assignment=assignment,
                access_mode=config.access_mode,
            ).strip()
            if not identifier:
                counters["skipped"] += 1
                continue

            existing_user = users_by_id.get(identifier)
            if existing_user is None:
                counters["missing"] += 1
                if event_log is not None:
                    event_log.record(
                        action="missing", assignment=assignment, identifier=identifier,
                        reason=assignment.reason or "",
                    )
                logger.warning(
                    "ZK user role sync could not find user_id=%s member=%s target=%s.",
                    identifier,
                    assignment.member_name or assignment.member_id or "-",
                    target_user_type,
                )
                continue

            target_group = (
                config.zk_allowed_group_id
                if target_user_type == "normal"
                else config.zk_blocked_group_id
            )
            # Ademas del grupo se ajusta el bit de deshabilitado: otros sistemas
            # (p. ej. ProGym) bloquean asi a los vencidos, y un miembro que renueva
            # quedaria rechazado por el terminal aunque este en el grupo permitido.
            target_disabled = target_user_type == "blackList"
            verified_group = getattr(device, "verifies_access_group", False)
            try:
                if verified_group:
                    existing_user.group_id = device.get_access_group(existing_user)
                if (
                    (existing_user.group_id or "").strip() == target_group
                    and (verified_group or existing_user.disabled == target_disabled)
                ):
                    counters["unchanged"] += 1
                    continue

                device.set_user_access(existing_user, target_group, target_disabled)
            except ZkCommandRejectedError as exc:
                # El lector respondio pero rechazo o no confirmo el cambio de este
                # socio: se registra y se sigue con los demas. Un error de red
                # (ZkUnavailableError) si detiene la sincronizacion.
                counters["failed"] += 1
                if event_log is not None:
                    event_log.record(
                        action="failed", assignment=assignment, identifier=identifier,
                        reason=str(exc),
                    )
                logger.warning(
                    "ZK user access not confirmed. user_id=%s member=%s target=%s error=%s",
                    identifier,
                    assignment.member_name or assignment.member_id or "-",
                    target_user_type,
                    exc,
                )
                continue
            existing_user.group_id = target_group
            if not verified_group:
                existing_user.privilege = (existing_user.privilege & ~USER_DISABLED_FLAG) | int(target_disabled)
            counters["updated"] += 1
            if event_log is not None:
                event_log.record(
                    action="blocked" if target_disabled else "unblocked",
                    assignment=assignment, identifier=identifier, reason=assignment.reason or "",
                )
            logger.info(
                "ZK user access updated. user_id=%s member=%s group=%s blocked=%s (%s) reason=%s",
                identifier,
                assignment.member_name or assignment.member_id or "-",
                target_group,
                target_disabled,
                target_user_type,
                assignment.reason or "-",
            )

    if counters["updated"]:
        device.refresh_data()

    save_cache = getattr(device, "save_access_group_cache", None)
    if save_cache is not None:
        save_cache()

    if progress_callback is not None:
        progress_callback()

    logger.info(
        "ZK user role sync completed (%s). assignments=%s updated=%s unchanged=%s missing=%s skipped=%s failed=%s",
        reason,
        counters["assignments"],
        counters["updated"],
        counters["unchanged"],
        counters["missing"],
        counters["skipped"],
        counters["failed"],
    )
    return counters


def _handle_user_role_sync(
    *,
    payload: dict[str, Any],
    logger: logging.Logger,
    config: EdgeConfig,
    backend_api: BackendApiClient,
    device: Any,
    sync_progress: _SyncProgress | None = None,
    event_log: _ReaderSyncEventLog | None = None,
) -> dict[str, Any]:
    raw_member_ids = payload.get("member_ids", [])
    member_ids: list[str] = []
    if isinstance(raw_member_ids, list):
        for value in raw_member_ids:
            cleaned = str(value).strip()
            if cleaned:
                member_ids.append(cleaned)
    elif isinstance(raw_member_ids, str):
        member_ids = [value.strip() for value in raw_member_ids.split(",") if value.strip()]
    elif raw_member_ids not in (None, ""):
        raise ValueError("member_ids must be a list or comma-separated string.")

    sync_reason = str(payload.get("reason") or "remote-request").strip() or "remote-request"
    counters = _sync_user_roles(
        logger=logger,
        config=config,
        backend_api=backend_api,
        device=device,
        reason=sync_reason,
        member_ids=member_ids or None,
        sync_progress=sync_progress,
        event_log=event_log,
    )
    return {
        "success": True,
        "message": "Sincronizacion de usuarios aplicada en ZKTeco.",
        "member_ids": member_ids,
        "reason": sync_reason,
        **counters,
    }


MAX_DELETE_USERS_PER_REQUEST = 200


def _is_device_admin(user: ZkUserInfo) -> bool:
    # privilege sin el bit de deshabilitado: 0 = usuario normal, otro = admin.
    return bool(user.privilege & ~USER_DISABLED_FLAG)


def _delete_device_users(
    *,
    logger: logging.Logger,
    config: EdgeConfig,
    device: Any,
    user_ids: list[str],
    reason: str,
) -> dict[str, str]:
    """Borra del lector los usuarios pedidos por el backend.

    Devuelve el resultado por ID: deleted, not_found, protected o failed. Nunca
    borra administradores del lector ni los IDs de ZK_PROTECTED_USER_IDS.
    """
    results: dict[str, str] = {}
    users_by_id: dict[str, ZkUserInfo] = {}
    for user in device.get_users():
        user_id = (user.user_id or "").strip()
        if user_id:
            users_by_id.setdefault(user_id, user)

    deleted_any = False
    for user_id in user_ids:
        user = users_by_id.get(user_id)
        if user is None:
            results[user_id] = "not_found"
            continue
        if user_id in config.zk_protected_user_ids or _is_device_admin(user):
            results[user_id] = "protected"
            logger.warning("ZK user delete refused for protected user_id=%s.", user_id)
            continue
        try:
            device.delete_user(user)
        except Exception as exc:  # noqa: BLE001
            results[user_id] = "failed"
            logger.warning("ZK user delete failed. user_id=%s error=%s", user_id, exc)
            continue
        results[user_id] = "deleted"
        deleted_any = True
        logger.info("ZK user deleted. user_id=%s name=%s reason=%s", user_id, user.name, reason)

    if deleted_any:
        device.refresh_data()
    return results


def _handle_delete_users(
    *,
    payload: dict[str, Any],
    logger: logging.Logger,
    config: EdgeConfig,
    device: Any,
) -> dict[str, Any]:
    raw_user_ids = payload.get("user_ids")
    if not isinstance(raw_user_ids, list):
        raise ValueError("user_ids must be a list.")
    user_ids: list[str] = []
    for value in raw_user_ids:
        cleaned = str(value).strip()
        if cleaned and cleaned not in user_ids:
            user_ids.append(cleaned)
    if not user_ids:
        raise ValueError("user_ids is empty.")
    if len(user_ids) > MAX_DELETE_USERS_PER_REQUEST:
        raise ValueError(f"Maximo {MAX_DELETE_USERS_PER_REQUEST} usuarios por solicitud.")

    reason = str(payload.get("reason") or "remote-request").strip() or "remote-request"
    results = _delete_device_users(
        logger=logger, config=config, device=device, user_ids=user_ids, reason=reason
    )
    counters = {status: list(results.values()).count(status) for status in ("deleted", "not_found", "protected", "failed")}
    logger.info("ZK user delete completed (%s). %s", reason, counters)
    return {
        "success": True,
        "message": "Borrado de usuarios aplicado en ZKTeco.",
        "results": results,
        **counters,
    }


def _report_edge_health(
    *,
    logger: logging.Logger,
    backend_api: BackendApiClient,
    device: Any,
    is_file_mode: bool,
    sync_progress: _SyncProgress | None = None,
) -> None:
    status_value, message, last_success_at = _build_edge_health_payload(
        device=device,
        is_file_mode=is_file_mode,
    )
    try:
        backend_api.report_edge_health(
            status=status_value,
            message=message,
            device_last_success_at=last_success_at,
            sync=sync_progress.snapshot() if sync_progress is not None else None,
        )
    except BackendUnavailableError as exc:
        logger.warning("Failed to report edge health to backend: %s", exc)


def _start_watchdog(
    *,
    logger: logging.Logger,
    config: EdgeConfig,
    health_state: _HealthState,
) -> None:
    if not config.watchdog_enabled:
        return

    def _watchdog_loop() -> None:
        poll_seconds = max(2.0, min(config.watchdog_timeout_seconds / 3, 10.0))
        while True:
            time.sleep(poll_seconds)
            stale_for = health_state.seconds_since_touch()
            if stale_for < config.watchdog_timeout_seconds:
                continue

            logger.critical(
                "Edge watchdog detected main loop stall for %.1fs. Exiting process for recovery.",
                stale_for,
            )
            os._exit(1)

    threading.Thread(target=_watchdog_loop, daemon=True, name="edge-watchdog").start()


def _restart_process_for_recovery(*, logger: logging.Logger, reason: str) -> None:
    logger.critical("Restarting edge process for recovery: %s", reason)
    if getattr(sys, "frozen", False):
        os.execv(sys.executable, [sys.executable])
    os.execv(sys.executable, [sys.executable, "-m", "edge_service_zkteco.main"])


def run() -> None:
    config = EdgeConfig.load()
    logger = configure_logging(config.log_file)
    backend_api = BackendApiClient(config)
    failed_attempts = FailedAttemptStore(config.failed_attempts_file)
    processed_events = ProcessedEventStore(config.zk_state_file)
    recent_grants: dict[str, float] = {}
    health_state = _HealthState()
    sync_progress = _SyncProgress()
    reader_sync_events = _ReaderSyncEventLog()
    _start_watchdog(logger=logger, config=config, health_state=health_state)

    event_queue: "queue.Queue[Any]" = queue.Queue()
    is_file_mode = config.zk_event_source == "file"

    if is_file_mode:
        device: Any = _FileDeviceStub(logger)
        file_source = FileEventSource(
            input_file=config.input_file,
            logger=logger,
            on_event=event_queue.put,
        )
        file_source.start()
    else:
        device = ZkClient(config, logger, on_event=event_queue.put)
        device.start()

    if config.edge_control_enabled:
        control_server = EdgeControlServer(
            bind_host=config.edge_control_bind_host,
            port=config.edge_control_port,
            path=config.edge_control_path,
            sync_user_roles_path=config.edge_role_sync_path,
            bearer_token=config.device_token,
            logger=logger,
            on_open=lambda payload: _handle_remote_open(
                payload=payload,
                logger=logger,
                config=config,
                device=device,
            ),
            on_sync_user_roles=lambda payload: _handle_user_role_sync(
                payload=payload,
                logger=logger,
                config=config,
                backend_api=backend_api,
                device=device,
                sync_progress=sync_progress,
                event_log=reader_sync_events,
            ),
            delete_users_path=config.edge_delete_users_path,
            on_delete_users=lambda payload: _handle_delete_users(
                payload=payload,
                logger=logger,
                config=config,
                device=device,
            ),
        )
        control_server.start()

    logger.info(
        (
            "ZKTeco edge service started. access_mode=%s, event_source=%s, device=%s:%s, "
            "open_door_on_grant=%s, role_sync_enabled=%s, "
            "edge_control_enabled=%s, edge_control_port=%s."
        ),
        config.access_mode,
        config.zk_event_source,
        config.zk_device_host or "-",
        config.zk_device_port,
        config.zk_open_door_on_grant,
        config.zk_user_role_sync_enabled,
        config.edge_control_enabled,
        config.edge_control_port,
    )

    role_sync_supported = not is_file_mode and config.zk_user_role_sync_enabled
    next_heartbeat_log_at = time.monotonic() + max(config.edge_heartbeat_log_seconds, 30)
    next_health_report_at = time.monotonic()
    next_user_role_sync_at = 0.0 if role_sync_supported else float("inf")
    recovery_warned = False

    def service_sync_progress() -> None:
        nonlocal next_heartbeat_log_at, next_health_report_at
        _drain_access_events(
            event_queue,
            lambda event: _process_event(
                event=event,
                logger=logger,
                config=config,
                backend_api=backend_api,
                device=device,
                failed_attempts=failed_attempts,
                processed_events=processed_events,
                recent_grants=recent_grants,
            ),
            health_state,
        )
        now = time.monotonic()
        if now >= next_heartbeat_log_at:
            logger.info(
                "Edge heartbeat during role sync: process alive, zk_last_success=%.1fs ago, "
                "consecutive_failures=%s.",
                device.seconds_since_last_success_or_started(),
                device.consecutive_failures(),
            )
            next_heartbeat_log_at = now + max(config.edge_heartbeat_log_seconds, 30)
        if now >= next_health_report_at:
            _report_edge_health(
                logger=logger, backend_api=backend_api, device=device,
                is_file_mode=is_file_mode, sync_progress=sync_progress,
            )
            next_health_report_at = time.monotonic() + max(
                config.edge_health_report_interval_seconds, 15,
            )
        health_state.touch()

    while True:
        health_state.touch()
        try:
            event = event_queue.get(timeout=0.5)
        except queue.Empty:
            event = None
        health_state.touch()

        if event is not None:
            _process_event(
                event=event,
                logger=logger,
                config=config,
                backend_api=backend_api,
                device=device,
                failed_attempts=failed_attempts,
                processed_events=processed_events,
                recent_grants=recent_grants,
            )
            while True:
                try:
                    extra_event = event_queue.get_nowait()
                except queue.Empty:
                    break
                health_state.touch()
                _process_event(
                    event=extra_event,
                    logger=logger,
                    config=config,
                    backend_api=backend_api,
                    device=device,
                    failed_attempts=failed_attempts,
                    processed_events=processed_events,
                    recent_grants=recent_grants,
                )
            health_state.touch()

        now = time.monotonic()

        recovery_seconds = config.zk_recovery_restart_seconds
        if recovery_seconds > 0 and not is_file_mode:
            no_success_seconds = device.seconds_since_last_success_or_started()
            if no_success_seconds >= recovery_seconds:
                _restart_process_for_recovery(
                    logger=logger,
                    reason=(
                        "no successful ZKTeco communication for "
                        f"{no_success_seconds:.1f}s "
                        f"(consecutive_failures={device.consecutive_failures()})"
                    ),
                )
            elif no_success_seconds >= max(60, recovery_seconds / 2) and not recovery_warned:
                logger.warning(
                    "ZKTeco communication has been degraded for %.1fs "
                    "(consecutive_failures=%s). The edge will restart itself at %ss "
                    "without a successful call.",
                    no_success_seconds,
                    device.consecutive_failures(),
                    recovery_seconds,
                )
                recovery_warned = True
            elif no_success_seconds < max(60, recovery_seconds / 2) and recovery_warned:
                logger.info(
                    "ZKTeco communication recovered after %.1fs without a successful call.",
                    no_success_seconds,
                )
                recovery_warned = False

        if now >= next_heartbeat_log_at:
            logger.info(
                "Edge heartbeat: process alive, zk_last_success=%.1fs ago, "
                "consecutive_failures=%s, event_source=%s.",
                device.seconds_since_last_success_or_started(),
                device.consecutive_failures(),
                config.zk_event_source,
            )
            next_heartbeat_log_at = time.monotonic() + max(
                config.edge_heartbeat_log_seconds,
                30,
            )

        if now >= next_health_report_at:
            _report_edge_health(
                logger=logger,
                backend_api=backend_api,
                device=device,
                is_file_mode=is_file_mode,
                sync_progress=sync_progress,
            )
            next_health_report_at = time.monotonic() + max(
                config.edge_health_report_interval_seconds,
                15,
            )

        if now >= next_user_role_sync_at:
            sync_reason = "startup" if next_user_role_sync_at == 0.0 else "scheduled"
            try:
                _sync_user_roles(
                    logger=logger,
                    config=config,
                    backend_api=backend_api,
                    device=device,
                    reason=sync_reason,
                    progress_callback=service_sync_progress,
                    sync_progress=sync_progress,
                    event_log=reader_sync_events,
                )
            except (BackendUnavailableError, ZkUnavailableError) as exc:
                logger.warning("ZK user role sync failed (%s): %s", sync_reason, exc)
            next_user_role_sync_at = (
                time.monotonic() + config.zk_user_role_sync_interval_seconds
            )


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        time.sleep(0.1)
