import logging
import unittest
from types import SimpleNamespace

from edge_service_zkteco.backend_api import BackendUnavailableError
from edge_service_zkteco.main import _ReaderSyncEventLog, _sync_user_roles
from edge_service_zkteco.models import UserRoleAssignment, ZkUserInfo
from edge_service_zkteco.zk_client import ZkCommandRejectedError


def _logger():
    logger = logging.getLogger("test-reader-sync-events")
    logger.addHandler(logging.NullHandler())
    return logger


def _assignment(member_id, user_id, target):
    return UserRoleAssignment(
        member_id=member_id, member_name=f"Socio {member_id}", rfid_code="", device_user_id=user_id,
        target_user_type=target, reason="Membership expired" if target == "blackList" else "Active",
    )


def _user(uid, user_id, group_id, privilege=0):
    return ZkUserInfo(uid=uid, user_id=user_id, name="U", privilege=privilege, password="", group_id=group_id, card=0)


class _Backend:
    def __init__(self, assignments, fail_reports=False):
        self.assignments = assignments
        self.fail_reports = fail_reports
        self.reported = []

    def fetch_user_roles(self, *, member_ids=None):
        return self.assignments

    def report_reader_sync_events(self, events):
        if self.fail_reports:
            raise BackendUnavailableError("sin red")
        self.reported.extend(events)


class _Device:
    def __init__(self, users, reject=()):
        self.users = users
        self.reject = set(reject)

    def get_users(self):
        return self.users

    def set_user_access(self, user, group_id, disabled):
        if user.user_id in self.reject:
            raise ZkCommandRejectedError("readback mismatch")

    def refresh_data(self):
        pass


CONFIG = SimpleNamespace(
    zk_user_role_sync_enabled=True, access_mode="biometric", zk_allowed_group_id="1", zk_blocked_group_id="99",
)


def _sync(backend, device, log):
    return _sync_user_roles(
        logger=_logger(), config=CONFIG, backend_api=backend, device=device, reason="test", event_log=log,
    )


class ReaderSyncEventsTests(unittest.TestCase):
    def test_reports_blocked_unblocked_missing_and_failed(self):
        backend = _Backend([
            _assignment("a", "101", "blackList"),
            _assignment("b", "102", "normal"),
            _assignment("c", "103", "blackList"),
            _assignment("d", "104", "blackList"),
            _assignment("e", "105", "blackList"),
        ])
        device = _Device(
            [_user(1, "101", "1"), _user(2, "102", "99"), _user(4, "104", "1"), _user(5, "105", "99", privilege=1)],
            reject={"104"},
        )

        _sync(backend, device, _ReaderSyncEventLog())

        actions = {event["device_user_id"]: event["action"] for event in backend.reported}
        self.assertEqual(actions, {"101": "blocked", "102": "unblocked", "103": "missing", "104": "failed"})
        blocked = next(event for event in backend.reported if event["device_user_id"] == "101")
        self.assertEqual(blocked["member_id"], "a")
        self.assertEqual(blocked["reason"], "Membership expired")
        self.assertTrue(blocked["occurred_at"])

    def test_missing_and_failed_reported_once_per_day(self):
        backend = _Backend([_assignment("c", "103", "blackList")])
        log = _ReaderSyncEventLog()

        _sync(backend, _Device([]), log)
        _sync(backend, _Device([]), log)

        self.assertEqual([event["action"] for event in backend.reported], ["missing"])

    def test_keeps_events_when_backend_is_down_and_retries(self):
        backend = _Backend([_assignment("a", "101", "blackList")], fail_reports=True)
        log = _ReaderSyncEventLog()

        _sync(backend, _Device([_user(1, "101", "1")]), log)
        self.assertEqual(log.pending_count(), 1)

        backend.fail_reports = False
        backend.assignments = []
        _sync(backend, _Device([]), log)
        self.assertEqual([event["device_user_id"] for event in backend.reported], ["101"])
        self.assertEqual(log.pending_count(), 0)

    def test_sync_without_event_log_still_works(self):
        backend = _Backend([_assignment("a", "101", "blackList")])

        counters = _sync_user_roles(
            logger=_logger(), config=CONFIG, backend_api=backend, device=_Device([_user(1, "101", "1")]),
            reason="test",
        )

        self.assertEqual(counters["updated"], 1)
        self.assertEqual(backend.reported, [])


if __name__ == "__main__":
    unittest.main()
