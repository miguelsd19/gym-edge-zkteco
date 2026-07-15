from __future__ import annotations

import json
import socket
import urllib.parse
import urllib.error
import urllib.request

from edge_service_zkteco.config import EdgeConfig
from edge_service_zkteco.models import AccessDecision, UserRoleAssignment


class BackendUnavailableError(Exception):
    """Raised when the backend cannot be reached or answers unexpectedly."""


class BackendApiClient:
    def __init__(self, config: EdgeConfig):
        self.config = config

    def _coerce_bool(self, value, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)

        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "f", "no", "n", "off", ""}:
            return False
        return default

    def validate(self, identifier: str, event_timestamp: str = "") -> AccessDecision:
        if self.config.access_mode == "rfid":
            payload = {"rfid_code": identifier}
        else:
            payload = {"device_user_id": identifier}
        if event_timestamp:
            payload["event_timestamp"] = event_timestamp
        request = urllib.request.Request(
            url=f"{self.config.backend_base_url}{self.config.validation_path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.device_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.config.request_timeout_seconds,
            ) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise BackendUnavailableError(
                f"HTTP {exc.code} while calling backend: {body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise BackendUnavailableError(f"Network error while calling backend: {exc}") from exc

        try:
            parsed = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError("Backend returned invalid JSON.") from exc

        granted = self._coerce_bool(parsed.get("granted"), default=False)
        should_open_value = parsed.get("should_open_door") if "should_open_door" in parsed else granted
        return AccessDecision(
            granted=granted,
            reason=str(parsed.get("reason", "")),
            member_name=str(parsed.get("member_name", "")),
            event_id=str(parsed.get("event_id", "")),
            should_open_door=self._coerce_bool(should_open_value, default=granted),
        )

    def report_edge_health(
        self,
        *,
        status: str,
        message: str = "",
        device_last_success_at: str = "",
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": status,
            "message": message,
        }
        if device_last_success_at:
            # El serializer del backend expone este campo con nombre "hikvision_*"
            # pero es generico: guarda el ultimo exito de comunicacion con el
            # dispositivo del edge, sea Hikvision o ZKTeco.
            payload["hikvision_last_success_at"] = device_last_success_at

        request = urllib.request.Request(
            url=f"{self.config.backend_base_url}/api/access/edge-health/",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.device_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.config.request_timeout_seconds,
            ) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise BackendUnavailableError(
                f"HTTP {exc.code} while calling backend: {body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise BackendUnavailableError(f"Network error while calling backend: {exc}") from exc

        try:
            parsed = json.loads(raw_body or "{}")
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError("Backend returned invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise BackendUnavailableError("Backend returned invalid JSON.")
        return parsed

    def fetch_user_roles(
        self,
        *,
        member_ids: list[str] | None = None,
    ) -> list[UserRoleAssignment]:
        query_params = []
        if member_ids:
            for member_id in member_ids:
                cleaned = str(member_id).strip()
                if cleaned:
                    query_params.append(("member_ids", cleaned))

        request_url = f"{self.config.backend_base_url}{self.config.zk_user_role_sync_backend_path}"
        if query_params:
            request_url = f"{request_url}?{urllib.parse.urlencode(query_params)}"

        request = urllib.request.Request(
            url=request_url,
            headers={
                "Authorization": f"Bearer {self.config.device_token}",
                "Content-Type": "application/json",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.config.request_timeout_seconds,
            ) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise BackendUnavailableError(
                f"HTTP {exc.code} while calling backend: {body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise BackendUnavailableError(f"Network error while calling backend: {exc}") from exc

        try:
            parsed = json.loads(raw_body or "{}")
        except json.JSONDecodeError as exc:
            raise BackendUnavailableError("Backend returned invalid JSON.") from exc

        if not isinstance(parsed, dict):
            raise BackendUnavailableError("Backend returned invalid JSON.")
        results = parsed.get("results", [])
        if not isinstance(results, list):
            raise BackendUnavailableError("Backend returned invalid JSON.")

        assignments: list[UserRoleAssignment] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            assignments.append(
                UserRoleAssignment(
                    member_id=str(item.get("member_id", "")),
                    member_name=str(item.get("member_name", "")),
                    rfid_code=str(item.get("rfid_code", "")),
                    device_user_id=str(item.get("device_user_id", "")),
                    target_user_type=str(item.get("target_user_type", "")),
                    reason=str(item.get("reason", "")),
                )
            )
        return assignments
