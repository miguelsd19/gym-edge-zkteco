import queue
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from edge_service_zkteco.zk_client import (
    ZkClient,
    ZkCommandRejectedError,
    ZkCommandTimeoutError,
    ZkUnavailableError,
)


class _FakeRejectedResponse(Exception):
    """Simula ZKErrorResponse: el equipo responde pero rechaza el comando."""


class FakeConn:
    def __init__(self):
        self.end_live_capture = False
        self.pending_events = queue.Queue()
        self.unlock_calls = []
        self.unlock_errors = []
        self.get_time_calls = 0
        self.get_time_delay = 0.0
        self.users = []
        self.set_user_calls = []
        self.refresh_calls = 0
        self.disconnected = False

    def live_capture(self, new_timeout=1.0):
        while not self.end_live_capture:
            try:
                event = self.pending_events.get(timeout=0.005)
            except queue.Empty:
                event = None
            yield event

    def unlock(self, time=3):
        self.unlock_calls.append(time)
        if self.unlock_errors:
            raise self.unlock_errors.pop(0)

    def get_time(self):
        if self.get_time_delay:
            time.sleep(self.get_time_delay)
        self.get_time_calls += 1
        return "now"

    def get_users(self):
        return self.users

    def set_user(self, **kwargs):
        self.set_user_calls.append(kwargs)

    def refresh_data(self):
        self.refresh_calls += 1

    def disconnect(self):
        self.disconnected = True


class FakeZkFactory:
    def __init__(self, conns, connect_errors=0):
        self.conns = list(conns)
        self.connect_errors = connect_errors
        self.connect_attempts = 0
        self.init_kwargs = None

    def __call__(self, host, **kwargs):
        self.host = host
        self.init_kwargs = kwargs
        return self

    def connect(self):
        self.connect_attempts += 1
        if self.connect_errors > 0:
            self.connect_errors -= 1
            raise ConnectionError("connection refused")
        if len(self.conns) > 1:
            return self.conns.pop(0)
        return self.conns[0]


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class _SilentLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class ZkClientTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.received_events = queue.Queue()
        self.client = None

    def tearDown(self):
        if self.client is not None:
            self.client.stop()

    def _build_config(self, **overrides):
        values = {
            "zk_device_host": "192.168.1.201",
            "zk_device_port": 4370,
            "zk_device_password": 0,
            "zk_device_timeout_seconds": 1.0,
            "zk_force_udp": False,
            "zk_omit_ping": True,
            "zk_capture_poll_seconds": 0.01,
            "zk_keepalive_seconds": 999.0,
            "zk_reconnect_max_backoff_seconds": 0.05,
            "zk_command_timeout_seconds": 2.0,
            "zk_open_door_duration_seconds": 3,
            "zk_open_door_retry_attempts": 2,
            "zk_open_door_retry_backoff_seconds": 0.0,
            "zk_event_log_file": Path(self._tmpdir.name) / "zk_events.jsonl",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _start_client(self, factory, **config_overrides):
        config = self._build_config(**config_overrides)
        self.client = ZkClient(
            config,
            _SilentLogger(),
            on_event=self.received_events.put,
            zk_factory=factory,
        )
        self.client.start()
        return self.client

    def test_events_reach_on_event_and_are_logged(self):
        conn = FakeConn()
        client = self._start_client(FakeZkFactory([conn]))

        event = SimpleNamespace(uid=1, user_id="1001", timestamp=None, status=0, punch=1)
        conn.pending_events.put(event)

        received = self.received_events.get(timeout=3.0)
        self.assertEqual(received.user_id, "1001")

        self.assertTrue(
            _wait_until(lambda: client.config.zk_event_log_file.exists())
        )
        content = client.config.zk_event_log_file.read_text(encoding="utf-8")
        self.assertIn('"user_id": "1001"', content)
        self.assertEqual(client.consecutive_failures(), 0)
        self.assertTrue(client.last_success_at_iso())

    def test_command_interrupts_capture_and_capture_resumes(self):
        conn = FakeConn()
        client = self._start_client(FakeZkFactory([conn]))

        self.assertTrue(client.healthcheck())
        self.assertEqual(conn.get_time_calls, 1)

        conn.pending_events.put(
            SimpleNamespace(uid=2, user_id="2002", timestamp=None, status=0, punch=1)
        )
        received = self.received_events.get(timeout=3.0)
        self.assertEqual(received.user_id, "2002")

    def test_unlock_retries_on_network_error(self):
        conn = FakeConn()
        conn.unlock_errors = [ConnectionError("socket died")]
        client = self._start_client(FakeZkFactory([conn]))

        client.unlock()

        self.assertEqual(len(conn.unlock_calls), 2)
        self.assertEqual(conn.unlock_calls[-1], 3)

    def test_unlock_does_not_retry_when_device_rejects(self):
        conn = FakeConn()
        conn.unlock_errors = [_FakeRejectedResponse("can not unlock")]
        client = self._start_client(FakeZkFactory([conn]))

        with self.assertRaises(ZkCommandRejectedError):
            client.unlock()

        self.assertEqual(len(conn.unlock_calls), 1)

    def test_reconnects_after_failed_connection_attempts(self):
        conn = FakeConn()
        factory = FakeZkFactory([conn], connect_errors=2)
        client = self._start_client(factory)

        conn.pending_events.put(
            SimpleNamespace(uid=3, user_id="3003", timestamp=None, status=0, punch=1)
        )
        received = self.received_events.get(timeout=3.0)

        self.assertEqual(received.user_id, "3003")
        self.assertGreaterEqual(factory.connect_attempts, 3)

    def test_keepalive_refreshes_last_success(self):
        conn = FakeConn()
        client = self._start_client(
            FakeZkFactory([conn]),
            zk_keepalive_seconds=0.05,
        )

        self.assertTrue(_wait_until(lambda: conn.get_time_calls >= 1))
        self.assertLess(client.seconds_since_last_success_or_started(), 5.0)

    def test_command_timeout_raises(self):
        conn = FakeConn()
        conn.get_time_delay = 1.0
        client = self._start_client(
            FakeZkFactory([conn]),
            zk_command_timeout_seconds=0.2,
        )

        with self.assertRaises(ZkCommandTimeoutError):
            client.healthcheck()

    def test_stop_shuts_down_thread_and_rejects_commands(self):
        conn = FakeConn()
        client = self._start_client(FakeZkFactory([conn]))
        self.assertTrue(_wait_until(lambda: client._conn is not None))

        client.stop()

        self.assertTrue(_wait_until(lambda: not client._thread.is_alive()))
        self.assertTrue(conn.disconnected)
        with self.assertRaises(ZkUnavailableError):
            client.healthcheck()

    def test_set_user_access_resends_full_user_record(self):
        conn = FakeConn()
        conn.users = [
            SimpleNamespace(
                uid=7,
                user_id="1001",
                name="Ana Activa",
                privilege=0,
                password="pw",
                group_id="1",
                card=123,
            )
        ]
        client = self._start_client(FakeZkFactory([conn]))

        users = client.get_users()
        self.assertEqual(len(users), 1)
        client.set_user_access(users[0], "99", disabled=True)

        self.assertEqual(len(conn.set_user_calls), 1)
        call = conn.set_user_calls[0]
        self.assertEqual(
            call,
            {
                "uid": 7,
                "name": "Ana Activa",
                "privilege": 1,
                "password": "pw",
                "group_id": "99",
                "user_id": "1001",
                "card": 123,
            },
        )

    def test_factory_receives_connection_settings(self):
        conn = FakeConn()
        factory = FakeZkFactory([conn])
        client = self._start_client(factory)
        self.assertTrue(_wait_until(lambda: factory.init_kwargs is not None))

        self.assertEqual(factory.host, "192.168.1.201")
        self.assertEqual(factory.init_kwargs["port"], 4370)
        self.assertEqual(factory.init_kwargs["password"], 0)
        self.assertTrue(factory.init_kwargs["ommit_ping"])
        self.assertFalse(factory.init_kwargs["force_udp"])
