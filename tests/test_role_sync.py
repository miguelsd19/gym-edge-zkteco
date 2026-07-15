import logging
import unittest
from types import SimpleNamespace

from edge_service_zkteco.main import _handle_user_role_sync, _sync_user_roles
from edge_service_zkteco.models import UserRoleAssignment, ZkUserInfo


def _assignment(member_id="1", device_user_id="1001", target="normal", rfid=""):
    return UserRoleAssignment(
        member_id=member_id,
        member_name=f"Member {member_id}",
        rfid_code=rfid,
        device_user_id=device_user_id,
        target_user_type=target,
        reason="test",
    )


def _user(uid=7, user_id="1001", group_id="1"):
    return ZkUserInfo(
        uid=uid,
        user_id=user_id,
        name="Test User",
        privilege=0,
        password="",
        group_id=group_id,
        card=0,
    )


class _FakeBackend:
    def __init__(self, assignments):
        self.assignments = assignments
        self.member_ids_calls = []

    def fetch_user_roles(self, *, member_ids=None):
        self.member_ids_calls.append(member_ids)
        return self.assignments


class _FakeDevice:
    def __init__(self, users):
        self.users = users
        self.set_group_calls = []
        self.refresh_calls = 0

    def get_users(self):
        return self.users

    def set_user_group(self, user, group_id):
        self.set_group_calls.append((user.user_id, group_id))

    def refresh_data(self):
        self.refresh_calls += 1


def _config(**overrides):
    values = {
        "zk_user_role_sync_enabled": True,
        "access_mode": "biometric",
        "zk_allowed_group_id": "1",
        "zk_blocked_group_id": "99",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _logger():
    logger = logging.getLogger("test-role-sync")
    logger.addHandler(logging.NullHandler())
    return logger


class SyncUserRolesTests(unittest.TestCase):
    def test_blocks_expired_member_by_moving_to_blocked_group(self):
        backend = _FakeBackend([_assignment(target="blackList")])
        device = _FakeDevice([_user(group_id="1")])

        counters = _sync_user_roles(
            logger=_logger(),
            config=_config(),
            backend_api=backend,
            device=device,
            reason="test",
        )

        self.assertEqual(device.set_group_calls, [("1001", "99")])
        self.assertEqual(device.refresh_calls, 1)
        self.assertEqual(counters["updated"], 1)

    def test_restores_active_member_to_allowed_group(self):
        backend = _FakeBackend([_assignment(target="normal")])
        device = _FakeDevice([_user(group_id="99")])

        counters = _sync_user_roles(
            logger=_logger(),
            config=_config(),
            backend_api=backend,
            device=device,
            reason="test",
        )

        self.assertEqual(device.set_group_calls, [("1001", "1")])
        self.assertEqual(counters["updated"], 1)

    def test_skips_user_already_in_target_group_without_refresh(self):
        backend = _FakeBackend([_assignment(target="normal")])
        device = _FakeDevice([_user(group_id="1")])

        counters = _sync_user_roles(
            logger=_logger(),
            config=_config(),
            backend_api=backend,
            device=device,
            reason="test",
        )

        self.assertEqual(device.set_group_calls, [])
        self.assertEqual(device.refresh_calls, 0)
        self.assertEqual(counters["unchanged"], 1)

    def test_counts_missing_and_invalid_assignments(self):
        backend = _FakeBackend(
            [
                _assignment(member_id="1", device_user_id="404", target="blackList"),
                _assignment(member_id="2", device_user_id="1001", target="weird"),
                _assignment(member_id="3", device_user_id="", target="normal"),
            ]
        )
        device = _FakeDevice([_user()])

        counters = _sync_user_roles(
            logger=_logger(),
            config=_config(),
            backend_api=backend,
            device=device,
            reason="test",
        )

        self.assertEqual(counters["missing"], 1)
        self.assertEqual(counters["skipped"], 2)
        self.assertEqual(device.set_group_calls, [])

    def test_matches_user_by_uid_fallback(self):
        backend = _FakeBackend([_assignment(device_user_id="7", target="blackList")])
        device = _FakeDevice([_user(uid=7, user_id="", group_id="1")])

        counters = _sync_user_roles(
            logger=_logger(),
            config=_config(),
            backend_api=backend,
            device=device,
            reason="test",
        )

        self.assertEqual(counters["updated"], 1)
        self.assertEqual(device.set_group_calls, [("", "99")])

    def test_disabled_sync_does_nothing(self):
        backend = _FakeBackend([_assignment(target="blackList")])
        device = _FakeDevice([_user()])

        counters = _sync_user_roles(
            logger=_logger(),
            config=_config(zk_user_role_sync_enabled=False),
            backend_api=backend,
            device=device,
            reason="test",
        )

        self.assertEqual(counters["updated"], 0)
        self.assertEqual(backend.member_ids_calls, [])
        self.assertEqual(device.set_group_calls, [])

    def test_rfid_mode_prefers_rfid_code_as_identifier(self):
        backend = _FakeBackend(
            [_assignment(device_user_id="1001", rfid="RF01", target="blackList")]
        )
        device = _FakeDevice([_user(user_id="RF01", group_id="1")])

        counters = _sync_user_roles(
            logger=_logger(),
            config=_config(access_mode="rfid"),
            backend_api=backend,
            device=device,
            reason="test",
        )

        self.assertEqual(counters["updated"], 1)
        self.assertEqual(device.set_group_calls, [("RF01", "99")])


class HandleUserRoleSyncTests(unittest.TestCase):
    def test_parses_member_ids_from_list_and_csv(self):
        backend = _FakeBackend([])
        device = _FakeDevice([])

        response = _handle_user_role_sync(
            payload={"member_ids": ["1", " 2 ", ""], "reason": "renewal"},
            logger=_logger(),
            config=_config(),
            backend_api=backend,
            device=device,
        )
        self.assertTrue(response["success"])
        self.assertEqual(response["member_ids"], ["1", "2"])
        self.assertEqual(response["reason"], "renewal")
        self.assertEqual(backend.member_ids_calls, [["1", "2"]])

        _handle_user_role_sync(
            payload={"member_ids": "3,4"},
            logger=_logger(),
            config=_config(),
            backend_api=backend,
            device=device,
        )
        self.assertEqual(backend.member_ids_calls[-1], ["3", "4"])

    def test_rejects_invalid_member_ids_type(self):
        with self.assertRaises(ValueError):
            _handle_user_role_sync(
                payload={"member_ids": 42},
                logger=_logger(),
                config=_config(),
                backend_api=_FakeBackend([]),
                device=_FakeDevice([]),
            )
