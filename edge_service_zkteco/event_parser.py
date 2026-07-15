from __future__ import annotations

from datetime import datetime
from typing import Any

from edge_service_zkteco.models import ZkEventCandidate

_MAX_IDENTIFIER_LENGTH = 64


def parse_event_candidate(
    event: Any,
    *,
    allowed_punch_codes: tuple[int, ...] = (),
    allowed_status_codes: tuple[int, ...] = (),
) -> ZkEventCandidate | None:
    """
    Convierte un evento en un candidato de acceso.

    Acepta tanto un `zk.attendance.Attendance` de pyzk (atributos uid, user_id,
    timestamp, status, punch) como un dict con las mismas claves (modo file).
    Devuelve None si el evento no trae identificador o no pasa los filtros.
    """
    identifier = _normalize_identifier(_get_field(event, "user_id"))
    if not identifier:
        return None

    status = _coerce_int(_get_field(event, "status"))
    punch = _coerce_int(_get_field(event, "punch"))

    if allowed_punch_codes and (punch is None or punch not in allowed_punch_codes):
        return None
    if allowed_status_codes and (status is None or status not in allowed_status_codes):
        return None

    happened_at = _normalize_timestamp(_get_field(event, "timestamp"))
    uid = _coerce_int(_get_field(event, "uid"))

    event_key = "|".join(
        [
            identifier,
            happened_at,
            str(status) if status is not None else "-",
            str(punch) if punch is not None else "-",
            str(uid) if uid is not None else "-",
        ]
    )
    return ZkEventCandidate(
        event_key=event_key,
        identifier=identifier,
        status=status,
        punch=punch,
        happened_at=happened_at,
    )


def event_to_dict(event: Any) -> dict[str, Any]:
    """Representacion serializable de un evento para el log crudo JSONL."""
    return {
        "uid": _coerce_int(_get_field(event, "uid")),
        "user_id": str(_get_field(event, "user_id") or ""),
        "timestamp": _normalize_timestamp(_get_field(event, "timestamp")),
        "status": _coerce_int(_get_field(event, "status")),
        "punch": _coerce_int(_get_field(event, "punch")),
    }


def _get_field(event: Any, name: str) -> Any:
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def _normalize_identifier(value: Any) -> str:
    if value is None:
        return ""
    normalized = str(value).strip()
    return normalized[:_MAX_IDENTIFIER_LENGTH]


def _normalize_timestamp(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value).strip()


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
