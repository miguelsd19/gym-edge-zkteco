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


def _user(uid=7, user_id="1001", group_id="1", privilege=0):
    return ZkUserInfo(
        uid=uid,
        user_id=user_id,
        name="Test User",
        privilege=privilege,
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

    def set_user_access(self, user, group_id, disabled):
        self.set_group_calls.append((user.user_id, group_id, disabled))

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
    def test_speedface_sync_uses_effective_group_not_generic_record(self):
        device = _FakeDevice([_user(group_id='99', privilege=1)])
        device.verifies_access_group = True
        device.get_access_group = lambda user: '1'
        counters = _sync_user_roles(
            logger=_logger(), config=_config(),
            backend_api=_FakeBackend([_assignment(target='blackList')]),
            device=device, reason='test',
        )
        self.assertEqual(counters['updated'], 1)
        self.assertEqual(device.set_group_calls, [('1001', '99', True)])

    def test_speedface_confirmed_group_is_unchanged_regardless_of_privilege_bit(self):
        device = _FakeDevice([_user(group_id='', privilege=0)])
        device.verifies_access_group = True
        device.get_access_group = lambda user: '99'
        counters = _sync_user_roles(
            logger=_logger(), config=_config(),
            backend_api=_FakeBackend([_assignment(target='blackList')]),
            device=device, reason='test',
        )
        self.assertEqual(counters['unchanged'], 1)
        self.assertEqual(device.set_group_calls, [])

    def test_unconfirmed_user_is_counted_failed_and_sync_continues(self):
        from edge_service_zkteco.zk_client import ZkCommandRejectedError

        device = _FakeDevice([_user(uid=1, user_id="1001"), _user(uid=2, user_id="1002")])
        device.verifies_access_group = True
        device.get_access_group = lambda user: "1"
        original_set = device.set_user_access

        def set_user_access(user, group_id, disabled):
            if user.user_id == "1001":
                raise ZkCommandRejectedError("readback mismatch")
            original_set(user, group_id, disabled)

        device.set_user_access = set_user_access
        counters = _sync_user_roles(
            logger=_logger(), config=_config(),
            backend_api=_FakeBackend([
                _assignment(member_id="1", device_user_id="1001", target="blackList"),
                _assignment(member_id="2", device_user_id="1002", target="blackList"),
            ]),
            device=device, reason="test",
        )

        self.assertEqual((counters["failed"], counters["updated"]), (1, 1))
        self.assertEqual(device.set_group_calls, [("1002", "99", True)])

    def test_network_error_still_aborts_sync(self):
        from edge_service_zkteco.zk_client import ZkUnavailableError

        device = _FakeDevice([_user(uid=1, user_id="1001")])
        device.verifies_access_group = True

        def unreachable(user):
            raise ZkUnavailableError("network down")

        device.get_access_group = unreachable
        with self.assertRaises(ZkUnavailableError):
            _sync_user_roles(
                logger=_logger(), config=_config(),
                backend_api=_FakeBackend([_assignment(device_user_id="1001", target="blackList")]),
                device=device, reason="test",
            )

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

        self.assertEqual(device.set_group_calls, [("1001", "99", True)])
        self.assertEqual(device.refresh_calls, 1)
        self.assertEqual(counters["updated"], 1)

    def test_restores_active_member_to_allowed_group(self):
        backend = _FakeBackend([_assignment(target="normal")])
        device = _FakeDevice([_user(group_id="99", privilege=1)])

        counters = _sync_user_roles(
            logger=_logger(),
            config=_config(),
            backend_api=backend,
            device=device,
            reason="test",
        )

        self.assertEqual(device.set_group_calls, [("1001", "1", False)])
        self.assertEqual(counters["updated"], 1)

    def test_enables_member_disabled_by_previous_system(self):
        # ProGym bloquea vencidos con el bit de deshabilitado; al renovar en
        # Gym System hay que habilitarlo aunque ya este en el grupo permitido.
        backend = _FakeBackend([_assignment(target="normal")])
        user = _user(group_id="1", privilege=1)
        device = _FakeDevice([user])

        counters = _sync_user_roles(
            logger=_logger(),
            config=_config(),
            backend_api=backend,
            device=device,
            reason="test",
        )

        self.assertEqual(device.set_group_calls, [("1001", "1", False)])
        self.assertFalse(user.disabled)
        self.assertEqual(counters["updated"], 1)

    def test_skips_blocked_member_already_disabled_in_blocked_group(self):
        backend = _FakeBackend([_assignment(target="blackList")])
        device = _FakeDevice([_user(group_id="99", privilege=1)])

        counters = _sync_user_roles(
            logger=_logger(),
            config=_config(),
            backend_api=backend,
            device=device,
            reason="test",
        )

        self.assertEqual(device.set_group_calls, [])
        self.assertEqual(counters["unchanged"], 1)

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
        self.assertEqual(device.set_group_calls, [("", "99", True)])

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
        self.assertEqual(device.set_group_calls, [("RF01", "99", True)])


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
