from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class AccessDecision:
    granted: bool
    reason: str
    member_name: str
    event_id: str
    should_open_door: bool


@dataclass
class UserRoleAssignment:
    member_id: str
    member_name: str
    rfid_code: str
    device_user_id: str
    target_user_type: str
    reason: str


@dataclass
class ZkUserInfo:
    uid: int
    user_id: str
    name: str
    privilege: int
    password: str
    group_id: str
    card: int


@dataclass
class FailedAttempt:
    identifier: str
    error: str
    happened_at: str

    @classmethod
    def create(cls, *, identifier: str, error: str) -> "FailedAttempt":
        return cls(
            identifier=identifier,
            error=error,
            happened_at=datetime.now(timezone.utc).isoformat(),
        )


@dataclass
class ZkEventCandidate:
    event_key: str
    identifier: str
    status: int | None
    punch: int | None
    happened_at: str
