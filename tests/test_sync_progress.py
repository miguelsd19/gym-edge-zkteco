import json
import logging
import unittest
from types import SimpleNamespace
from unittest import mock

from edge_service_zkteco.backend_api import BackendApiClient
from edge_service_zkteco.main import _SyncProgress, _report_edge_health, _sync_user_roles
from edge_service_zkteco.models import UserRoleAssignment, ZkUserInfo
from edge_service_zkteco.zk_client import ZkUnavailableError


def _logger():
    logger = logging.getLogger("test-sync-progress")
    logger.addHandler(logging.NullHandler())
    return logger


def _assignment(member_id, user_id, target="normal"):
    return UserRoleAssignment(
        member_id=member_id, member_name=f"M{member_id}", rfid_code="",
        device_user_id=user_id, target_user_type=target, reason="test",
    )


def _user(uid, user_id, group_id="1"):
    return ZkUserInfo(uid=uid, user_id=user_id, name="U", privilege=0, password="", group_id=group_id, card=0)


class _Backend:
    def __init__(self, assignments):
        self.assignments = assignments

    def fetch_user_roles(self, *, member_ids=None):
        return self.assignments


class _Device:
    def __init__(self, users, progress=None):
        self.users = users
        self.progress = progress
        self.seen_during_sync = []

    def get_users(self):
        return self.users

    def set_user_access(self, user, group_id, disabled):
        if self.progress is not None:
            self.seen_during_sync.append(self.progress.snapshot())

    def refresh_data(self):
        pass


def _config():
    return SimpleNamespace(
        zk_user_role_sync_enabled=True, access_mode="biometric",
        zk_allowed_group_id="1", zk_blocked_group_id="99",
    )


class SyncProgressTests(unittest.TestCase):
    def test_full_sync_reports_progress_and_last_counters(self):
        progress = _SyncProgress()
        device = _Device([_user(1, "1001"), _user(2, "1002")], progress)

        _sync_user_roles(
            logger=_logger(), config=_config(), device=device, reason="startup",
            backend_api=_Backend([_assignment("a", "1001", "blackList"), _assignment("b", "1002")]),
            sync_progress=progress,
        )

        during = device.seen_during_sync[0]
        self.assertTrue(during["running"])
        self.assertEqual((during["processed"], during["total"], during["reason"]), (1, 2, "startup"))
        after = progress.snapshot()
        self.assertFalse(after["running"])
        self.assertEqual(after["processed"], 2)
        self.assertEqual(after["last_counters"]["updated"], 1)
        self.assertEqual(after["last_counters"]["unchanged"], 1)
        self.assertTrue(after["last_completed_at"])
        self.assertEqual(after["last_error"], "")

    def test_targeted_sync_does_not_touch_progress(self):
        progress = _SyncProgress()

        _sync_user_roles(
            logger=_logger(), config=_config(), device=_Device([_user(1, "1001")]), reason="renew",
            backend_api=_Backend([_assignment("a", "1001", "blackList")]),
            member_ids=["a"], sync_progress=progress,
        )

        self.assertEqual(progress.snapshot()["started_at"], "")
        self.assertEqual(progress.snapshot()["last_completed_at"], "")

    def test_failed_sync_records_error_and_stops_running(self):
        progress = _SyncProgress()

        class _Down(_Device):
            def get_users(self):
                raise ZkUnavailableError("lector sin red")

        with self.assertRaises(ZkUnavailableError):
            _sync_user_roles(
                logger=_logger(), config=_config(), device=_Down([]), reason="scheduled",
                backend_api=_Backend([_assignment("a", "1001")]), sync_progress=progress,
            )

        snapshot = progress.snapshot()
        self.assertFalse(snapshot["running"])
        self.assertIn("lector sin red", snapshot["last_error"])


class HealthPayloadTests(unittest.TestCase):
    def test_health_report_includes_sync_snapshot(self):
        progress = _SyncProgress()
        progress.start("scheduled")
        progress.set_total(10)
        progress.step()
        backend = mock.Mock()
        device = mock.Mock()
        device.consecutive_failures.return_value = 0
        device.seconds_since_last_success_or_started.return_value = 1.0
        device.last_success_at_iso.return_value = "2026-10-31T00:00:00+00:00"

        _report_edge_health(logger=_logger(), backend_api=backend, device=device,
                            is_file_mode=False, sync_progress=progress)

        sync = backend.report_edge_health.call_args.kwargs["sync"]
        self.assertEqual((sync["running"], sync["processed"], sync["total"]), (True, 1, 10))

    def test_backend_client_sends_sync_key(self):
        client = BackendApiClient(SimpleNamespace(
            backend_base_url="https://backend", device_token="tok", request_timeout_seconds=1.0,
        ))
        captured = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"success": true}'

        def fake_urlopen(request, timeout):
            captured.update(json.loads(request.data.decode()))
            return _Response()

        with mock.patch("edge_service_zkteco.backend_api.urllib.request.urlopen", side_effect=fake_urlopen):
            client.report_edge_health(status="healthy", sync={"running": False, "total": 0})

        self.assertEqual(captured["sync"], {"running": False, "total": 0})


if __name__ == "__main__":
    unittest.main()
