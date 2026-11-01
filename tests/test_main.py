import logging
import queue
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from edge_service_zkteco.backend_api import BackendUnavailableError
from edge_service_zkteco.failed_attempts import FailedAttemptStore
from edge_service_zkteco.main import (
    _HealthState,
    _drain_access_events,
    _build_edge_health_payload,
    _handle_remote_open,
    _is_duplicate_recent_grant,
    _is_stale_event,
    _process_event,
    _sync_user_roles,
)
from edge_service_zkteco.models import AccessDecision, UserRoleAssignment, ZkUserInfo
from edge_service_zkteco.state_store import ProcessedEventStore
from edge_service_zkteco.zk_client import ZkUnavailableError


def _logger():
    logger = logging.getLogger("test-main")
    logger.addHandler(logging.NullHandler())
    return logger


def _decision(granted=True, should_open=None):
    return AccessDecision(
        granted=granted,
        reason="",
        member_name="",
        event_id="evt",
        should_open_door=granted if should_open is None else should_open,
    )


class _FakeBackend:
    def __init__(self, decision=None, error=None):
        self.decision = decision or _decision()
        self.error = error
        self.validate_calls = []

    def validate(self, identifier, event_timestamp=""):
        self.validate_calls.append((identifier, event_timestamp))
        if self.error is not None:
            raise self.error
        return self.decision


class _FakeDevice:
    def __init__(self, unlock_error=None):
        self.unlock_calls = []
        self.unlock_error = unlock_error
        self._failures = 0
        self._no_success_seconds = 0.0
        self._last_success_at = "2026-07-02T10:00:00+00:00"

    def unlock(self, duration_seconds=None):
        self.unlock_calls.append(duration_seconds)
        if self.unlock_error is not None:
            raise self.unlock_error

    def consecutive_failures(self):
        return self._failures

    def seconds_since_last_success_or_started(self):
        return self._no_success_seconds

    def last_success_at_iso(self):
        return self._last_success_at


class DuplicateRecentGrantTests(unittest.TestCase):
    def test_detects_duplicate_within_cooldown(self):
        recent = {"1001": time.monotonic()}
        self.assertTrue(_is_duplicate_recent_grant("1001", recent, 3.0))
        self.assertFalse(_is_duplicate_recent_grant("2002", recent, 3.0))

    def test_expires_old_grants(self):
        recent = {"1001": time.monotonic() - 10.0}
        self.assertFalse(_is_duplicate_recent_grant("1001", recent, 3.0))
        self.assertEqual(recent, {})

    def test_disabled_when_cooldown_is_zero(self):
        recent = {"1001": time.monotonic()}
        self.assertFalse(_is_duplicate_recent_grant("1001", recent, 0))


class StaleEventTests(unittest.TestCase):
    def test_naive_timestamp_is_interpreted_as_local_time(self):
        fresh = datetime.now().isoformat(timespec="seconds")
        stale = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")

        self.assertFalse(_is_stale_event(fresh, 86400))
        self.assertTrue(_is_stale_event(stale, 86400))

    def test_empty_or_invalid_timestamp_is_not_stale(self):
        self.assertFalse(_is_stale_event("", 86400))
        self.assertFalse(_is_stale_event("not-a-date", 86400))
        self.assertFalse(_is_stale_event("2000-01-01T00:00:00", 0))


class EdgeHealthPayloadTests(unittest.TestCase):
    def test_file_mode_is_always_healthy(self):
        status, message, last = _build_edge_health_payload(
            device=_FakeDevice(),
            is_file_mode=True,
        )
        self.assertEqual(status, "healthy")
        self.assertIn("file", message.lower())
        self.assertEqual(last, "")

    def test_healthy_when_device_responds(self):
        status, _, last = _build_edge_health_payload(
            device=_FakeDevice(),
            is_file_mode=False,
        )
        self.assertEqual(status, "healthy")
        self.assertEqual(last, "2026-07-02T10:00:00+00:00")

    def test_degraded_after_consecutive_failures(self):
        device = _FakeDevice()
        device._failures = 5
        status, _, _ = _build_edge_health_payload(device=device, is_file_mode=False)
        self.assertEqual(status, "degraded")

    def test_degraded_after_no_success_window(self):
        device = _FakeDevice()
        device._no_success_seconds = 60.0
        device._last_success_at = ""
        status, message, last = _build_edge_health_payload(
            device=device,
            is_file_mode=False,
        )
        self.assertEqual(status, "degraded")
        self.assertEqual(last, "")
        self.assertIn("aun no logra", message)


class ProcessEventTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.processed = ProcessedEventStore(Path(self._tmpdir.name) / "state.json")
        self.failed_attempts = FailedAttemptStore(
            Path(self._tmpdir.name) / "failed.jsonl"
        )
        self.recent_grants = {}

    def _config(self, **overrides):
        values = {
            "zk_allowed_punch_codes": (),
            "zk_allowed_status_codes": (),
            "zk_max_event_age_seconds": 86400,
            "granted_identifier_cooldown_seconds": 3.0,
            "zk_open_door_on_grant": True,
            "zk_open_door_duration_seconds": 3,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _event(self, user_id="1001", timestamp=None):
        return {
            "user_id": user_id,
            "timestamp": timestamp or datetime.now().isoformat(timespec="seconds"),
            "status": 0,
            "punch": 1,
            "uid": 7,
        }

    def _process(self, event, *, config=None, backend=None, device=None):
        backend = backend or _FakeBackend()
        device = device or _FakeDevice()
        _process_event(
            event=event,
            logger=_logger(),
            config=config or self._config(),
            backend_api=backend,
            device=device,
            failed_attempts=self.failed_attempts,
            processed_events=self.processed,
            recent_grants=self.recent_grants,
        )
        return backend, device

    def test_granted_event_validates_and_opens_door(self):
        backend, device = self._process(self._event())

        self.assertEqual(len(backend.validate_calls), 1)
        self.assertEqual(backend.validate_calls[0][0], "1001")
        self.assertEqual(device.unlock_calls, [3])
        self.assertIn("1001", self.recent_grants)

    def test_access_is_validated_between_sync_writes_and_after_final_write(self):
        for granted in (True, False):
            with self.subTest(granted=granted):
                events = queue.Queue()
                order = []
                device = _FakeDevice()
                backend = _FakeBackend(decision=_decision(granted=granted))
                users = [
                    ZkUserInfo(i, str(i), 'Test', 0, '', '', 0)
                    for i in (11, 12)
                ]
                backend.fetch_user_roles = lambda **kw: [
                    UserRoleAssignment(str(i), 'Test', str(i), str(i), 'normal', 'test')
                    for i in (11, 12)
                ]
                device.get_users = lambda: users
                device.refresh_data = lambda: order.append('refresh')

                def write(user, group_id, disabled):
                    order.append('write-' + user.user_id)
                    events.put(self._event(user_id=str(user.uid) + str(granted)))

                def process(event):
                    self._process(event, backend=backend, device=device)
                    order.append('access')

                device.set_user_access = write
                health = _HealthState()
                counters = _sync_user_roles(
                    logger=_logger(),
                    config=self._config(
                        zk_user_role_sync_enabled=True, access_mode='rfid',
                        zk_allowed_group_id='1', zk_blocked_group_id='99',
                    ),
                    backend_api=backend, device=device, reason='test',
                    progress_callback=lambda: _drain_access_events(events, process, health),
                )
                self.assertEqual(order, ['write-11', 'access', 'write-12', 'refresh', 'access'])
                self.assertEqual(counters['updated'], 2)
                self.assertEqual(len(backend.validate_calls), 2)
                self.assertEqual(device.unlock_calls, [3, 3] if granted else [])

    def test_sync_event_drain_is_bounded_under_continuous_events(self):
        events = queue.Queue()
        events.put('event')
        processed = []

        def process(event):
            processed.append(event)
            events.put(event)

        _drain_access_events(events, process, _HealthState())
        self.assertEqual(len(processed), 16)
        self.assertEqual(events.qsize(), 1)

    def test_same_event_is_processed_only_once(self):
        event = self._event()
        backend_one, _ = self._process(event)
        backend_two, device_two = self._process(event)

        self.assertEqual(len(backend_one.validate_calls), 1)
        self.assertEqual(len(backend_two.validate_calls), 0)
        self.assertEqual(device_two.unlock_calls, [])

    def test_duplicate_identifier_within_cooldown_is_skipped(self):
        self._process(self._event(timestamp="2026-07-02T10:00:00"))
        backend, device = self._process(self._event(timestamp="2026-07-02T10:00:01"))

        self.assertEqual(len(backend.validate_calls), 0)
        self.assertEqual(device.unlock_calls, [])

    def test_backend_unavailable_denies_and_records_failed_attempt(self):
        backend, device = self._process(
            self._event(),
            backend=_FakeBackend(error=BackendUnavailableError("down")),
        )

        self.assertEqual(device.unlock_calls, [])
        self.assertNotIn("1001", self.recent_grants)
        content = self.failed_attempts.path.read_text(encoding="utf-8")
        self.assertIn('"identifier": "1001"', content)

    def test_denied_event_does_not_open_door(self):
        backend, device = self._process(
            self._event(),
            backend=_FakeBackend(decision=_decision(granted=False)),
        )

        self.assertEqual(len(backend.validate_calls), 1)
        self.assertEqual(device.unlock_calls, [])

    def test_open_door_on_grant_disabled_skips_unlock_but_records_grant(self):
        backend, device = self._process(
            self._event(),
            config=self._config(zk_open_door_on_grant=False),
        )

        self.assertEqual(len(backend.validate_calls), 1)
        self.assertEqual(device.unlock_calls, [])
        self.assertIn("1001", self.recent_grants)

    def test_unlock_failure_records_failed_attempt(self):
        backend, device = self._process(
            self._event(),
            device=_FakeDevice(unlock_error=ZkUnavailableError("device down")),
        )

        self.assertEqual(device.unlock_calls, [3])
        content = self.failed_attempts.path.read_text(encoding="utf-8")
        self.assertIn("device down", content)

    def test_stale_event_is_skipped(self):
        stale = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        backend, device = self._process(self._event(timestamp=stale))

        self.assertEqual(len(backend.validate_calls), 0)
        self.assertEqual(device.unlock_calls, [])


class HandleRemoteOpenTests(unittest.TestCase):
    def test_remote_open_unlocks_and_reports_metadata(self):
        device = _FakeDevice()
        response = _handle_remote_open(
            payload={"requested_by": "admin", "access_point_id": "5"},
            logger=_logger(),
            config=SimpleNamespace(zk_open_door_duration_seconds=4),
            device=device,
        )

        self.assertTrue(response["success"])
        self.assertEqual(response["requested_by"], "admin")
        self.assertEqual(response["access_point_id"], "5")
        self.assertEqual(device.unlock_calls, [4])

    def test_remote_open_propagates_device_error(self):
        device = _FakeDevice(unlock_error=ZkUnavailableError("unreachable"))

        with self.assertRaises(ZkUnavailableError):
            _handle_remote_open(
                payload={},
                logger=_logger(),
                config=SimpleNamespace(zk_open_door_duration_seconds=3),
                device=device,
            )
