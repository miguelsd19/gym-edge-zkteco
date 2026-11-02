from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _get_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _get_csv_ints(name: str) -> tuple[int, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return ()
    return tuple(int(value.strip()) for value in raw.split(",") if value.strip())


def _get_id_set(name: str) -> frozenset[str]:
    """IDs de usuario del lector separados por coma; acepta rangos (7001-7026)."""
    ids: set[str] = set()
    for chunk in os.getenv(name, "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        start, sep, end = chunk.partition("-")
        if sep and start.strip().isdigit() and end.strip().isdigit():
            low, high = int(start), int(end)
            if high < low or high - low > 100_000:
                raise ValueError(f"{name}: rango invalido '{chunk}'.")
            ids.update(str(value) for value in range(low, high + 1))
        else:
            ids.add(chunk)
    return frozenset(ids)


def _get_optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass
class EdgeConfig:
    base_dir: Path
    backend_base_url: str
    device_token: str
    access_mode: str
    request_timeout_seconds: float
    poll_interval_seconds: float
    granted_identifier_cooldown_seconds: float
    log_file: Path
    failed_attempts_file: Path
    input_file: Path
    zk_event_log_file: Path
    zk_state_file: Path
    zk_event_source: str
    zk_device_host: str
    zk_device_port: int
    zk_device_password: int
    zk_device_timeout_seconds: float
    zk_force_udp: bool
    zk_omit_ping: bool
    zk_capture_poll_seconds: float
    zk_capture_min_window_seconds: float
    zk_command_batch_size: int
    zk_keepalive_seconds: float
    zk_reconnect_max_backoff_seconds: float
    zk_command_timeout_seconds: float
    zk_max_event_age_seconds: int
    zk_allowed_punch_codes: tuple[int, ...]
    zk_allowed_status_codes: tuple[int, ...]
    zk_open_door_on_grant: bool
    zk_open_door_duration_seconds: int
    zk_open_door_retry_attempts: int
    zk_open_door_retry_backoff_seconds: float
    zk_allowed_group_id: str
    zk_blocked_group_id: str
    edge_control_enabled: bool
    edge_control_bind_host: str
    edge_control_port: int
    edge_control_path: str
    edge_role_sync_path: str
    edge_delete_users_path: str
    zk_protected_user_ids: frozenset[str]
    zk_remote_enroll_enabled: bool
    edge_enroll_path: str
    edge_enroll_status_path: str
    zk_enroll_timeout_seconds: float
    zk_user_role_sync_enabled: bool
    zk_access_group_cache_seconds: int
    zk_access_group_cache_file: Path
    zk_user_role_sync_interval_seconds: int
    zk_user_role_sync_backend_path: str
    zk_recovery_restart_seconds: int
    edge_heartbeat_log_seconds: int
    edge_health_report_interval_seconds: int
    watchdog_enabled: bool
    watchdog_timeout_seconds: int

    @classmethod
    def load(cls) -> "EdgeConfig":
        base_dir = _resolve_base_dir()
        dotenv_name = os.getenv("EDGE_ENV_FILE", ".env")
        _load_dotenv(base_dir / dotenv_name)

        access_mode = os.getenv("ACCESS_MODE", "biometric").strip().lower()
        if access_mode not in {"rfid", "biometric"}:
            raise ValueError("ACCESS_MODE must be either 'rfid' or 'biometric'.")

        event_source = os.getenv("ZK_EVENT_SOURCE", "device").strip().lower()
        if event_source not in {"device", "file"}:
            raise ValueError("ZK_EVENT_SOURCE must be either 'device' or 'file'.")

        zk_device_host = _get_optional("ZK_DEVICE_HOST")
        if event_source == "device" and not zk_device_host:
            raise ValueError("ZK_DEVICE_HOST is required when ZK_EVENT_SOURCE=device.")

        return cls(
            base_dir=base_dir,
            backend_base_url=_get_required("BACKEND_BASE_URL").rstrip("/"),
            device_token=_get_required("DEVICE_TOKEN"),
            access_mode=access_mode,
            request_timeout_seconds=_get_float("REQUEST_TIMEOUT_SECONDS", 3.0),
            poll_interval_seconds=_get_float("POLL_INTERVAL_SECONDS", 0.5),
            granted_identifier_cooldown_seconds=_get_float(
                "GRANTED_IDENTIFIER_COOLDOWN_SECONDS",
                3.0,
            ),
            log_file=base_dir / os.getenv("LOG_FILE", "runtime/edge_zkteco.log"),
            failed_attempts_file=base_dir / os.getenv(
                "FAILED_ATTEMPTS_FILE",
                "runtime/failed_attempts_zkteco.jsonl",
            ),
            input_file=base_dir / os.getenv("INPUT_FILE", "runtime/input.txt"),
            zk_event_log_file=base_dir / os.getenv(
                "ZK_EVENT_LOG_FILE",
                "runtime/zk_events.jsonl",
            ),
            zk_state_file=base_dir / os.getenv(
                "ZK_STATE_FILE",
                "runtime/zk_state.json",
            ),
            zk_event_source=event_source,
            zk_device_host=zk_device_host,
            zk_device_port=_get_int("ZK_DEVICE_PORT", 4370),
            zk_device_password=_get_int("ZK_DEVICE_PASSWORD", 0),
            zk_device_timeout_seconds=_get_float("ZK_DEVICE_TIMEOUT_SECONDS", 5.0),
            zk_force_udp=_get_bool("ZK_FORCE_UDP", False),
            zk_omit_ping=_get_bool("ZK_OMIT_PING", True),
            zk_capture_poll_seconds=max(0.2, _get_float("ZK_CAPTURE_POLL_SECONDS", 1.0)),
            zk_capture_min_window_seconds=max(0.1, _get_float("ZK_CAPTURE_MIN_WINDOW_SECONDS", 0.5)),
            zk_command_batch_size=max(1, _get_int("ZK_COMMAND_BATCH_SIZE", 4)),
            zk_keepalive_seconds=max(5.0, _get_float("ZK_KEEPALIVE_SECONDS", 30.0)),
            zk_reconnect_max_backoff_seconds=max(
                1.0,
                _get_float("ZK_RECONNECT_MAX_BACKOFF_SECONDS", 30.0),
            ),
            zk_command_timeout_seconds=max(
                1.0,
                _get_float("ZK_COMMAND_TIMEOUT_SECONDS", 10.0),
            ),
            zk_max_event_age_seconds=_get_int("ZK_MAX_EVENT_AGE_SECONDS", 86400),
            zk_allowed_punch_codes=_get_csv_ints("ZK_ALLOWED_PUNCH_CODES"),
            zk_allowed_status_codes=_get_csv_ints("ZK_ALLOWED_STATUS_CODES"),
            zk_open_door_on_grant=_get_bool("ZK_OPEN_DOOR_ON_GRANT", True),
            zk_open_door_duration_seconds=max(
                1,
                _get_int("ZK_OPEN_DOOR_DURATION_SECONDS", 3),
            ),
            zk_open_door_retry_attempts=max(
                1,
                _get_int("ZK_OPEN_DOOR_RETRY_ATTEMPTS", 2),
            ),
            zk_open_door_retry_backoff_seconds=max(
                0.0,
                _get_float("ZK_OPEN_DOOR_RETRY_BACKOFF_SECONDS", 0.5),
            ),
            zk_allowed_group_id=_get_optional("ZK_ALLOWED_GROUP_ID", "1") or "1",
            zk_blocked_group_id=_get_optional("ZK_BLOCKED_GROUP_ID", "99") or "99",
            edge_control_enabled=_get_bool("EDGE_CONTROL_ENABLED", True),
            edge_control_bind_host=os.getenv("EDGE_CONTROL_BIND_HOST", "0.0.0.0").strip(),
            edge_control_port=_get_int("EDGE_CONTROL_PORT", 9877),
            edge_control_path=os.getenv("EDGE_CONTROL_PATH", "/door/open").strip() or "/door/open",
            edge_role_sync_path=os.getenv(
                "EDGE_ROLE_SYNC_PATH",
                "/hikvision/user-role-sync",
            ).strip()
            or "/hikvision/user-role-sync",
            edge_delete_users_path=os.getenv(
                "EDGE_DELETE_USERS_PATH",
                "/zkteco/users/delete",
            ).strip()
            or "/zkteco/users/delete",
            zk_protected_user_ids=_get_id_set("ZK_PROTECTED_USER_IDS"),
            # Apagado por defecto: encenderlo solo despues de que enroll_probe
            # confirme que el firmware del lector acepta el enrolamiento remoto.
            zk_remote_enroll_enabled=_get_bool("ZK_REMOTE_ENROLL_ENABLED", False),
            edge_enroll_path=os.getenv("EDGE_ENROLL_PATH", "/zkteco/enroll").strip()
            or "/zkteco/enroll",
            edge_enroll_status_path=os.getenv(
                "EDGE_ENROLL_STATUS_PATH", "/zkteco/enroll/status"
            ).strip()
            or "/zkteco/enroll/status",
            zk_enroll_timeout_seconds=max(30.0, _get_float("ZK_ENROLL_TIMEOUT_SECONDS", 240.0)),
            zk_user_role_sync_enabled=_get_bool("ZK_USER_ROLE_SYNC_ENABLED", True),
            zk_access_group_cache_seconds=max(
                0,
                _get_int("ZK_ACCESS_GROUP_CACHE_SECONDS", 21600),
            ),
            zk_access_group_cache_file=base_dir / os.getenv(
                "ZK_ACCESS_GROUP_CACHE_FILE",
                "runtime/zk_access_groups.json",
            ),
            zk_user_role_sync_interval_seconds=max(
                300,
                _get_int("ZK_USER_ROLE_SYNC_INTERVAL_SECONDS", 86400),
            ),
            zk_user_role_sync_backend_path=os.getenv(
                "ZK_USER_ROLE_SYNC_BACKEND_PATH",
                "/api/access/hikvision-user-roles/",
            ).strip()
            or "/api/access/hikvision-user-roles/",
            zk_recovery_restart_seconds=_get_int("ZK_RECOVERY_RESTART_SECONDS", 300),
            edge_heartbeat_log_seconds=_get_int("EDGE_HEARTBEAT_LOG_SECONDS", 60),
            edge_health_report_interval_seconds=_get_int(
                "EDGE_HEALTH_REPORT_INTERVAL_SECONDS",
                30,
            ),
            watchdog_enabled=_get_bool("EDGE_WATCHDOG_ENABLED", True),
            watchdog_timeout_seconds=_get_int("EDGE_WATCHDOG_TIMEOUT_SECONDS", 30),
        )

    @property
    def validation_path(self) -> str:
        if self.access_mode == "rfid":
            return "/api/access/validate/"
        return "/api/access/biometric-events/"


def _resolve_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]
