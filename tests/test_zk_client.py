import queue
import tempfile
import time
import unittest
from struct import unpack, pack
import socket
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


class FakeEventSocket:
    def __init__(self, conn):
        self.conn = conn
        self.timeout = 1.0

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        try:
            event = self.conn.pending_events.get(timeout=min(self.timeout, 0.005))
        except queue.Empty:
            raise socket.timeout()
        if isinstance(event, bytes):
            return event
        payload = pack('<24sBB6B', event.user_id.encode(), event.status, event.punch, 26, 10, 31, 20, 0, 0)
        packet = pack('<4H', 500, 0, 0, 0) + payload
        return pack('<HHI', 0x5050, 0x7d82, len(packet)) + packet


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
        self.encoding = 'utf-8'
        self.user_packet_size = 72
        self.ignore_user_write = False
        self.access_groups = {}
        self.access_commands = []
        self._ZK__sock = FakeEventSocket(self)
        self.tcp = True
        self.is_enabled = True
        self.event_flags = []

    def cancel_capture(self):
        pass

    def verify_user(self):
        pass

    def reg_event(self, flags):
        self.event_flags.append(flags)

    def enable_device(self):
        self.is_enabled = True

    def disable_device(self):
        self.is_enabled = False

    def _ZK__ack_ok(self):
        pass

    def live_capture(self, new_timeout=1.0):
        raise AssertionError('The client must not use pyzk.live_capture')

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

    def _ZK__send_command(self, command, payload, response_size):
        self.access_commands.append((command, payload))
        if command == 21:
            uid = unpack('<I', payload)[0]
            self._ZK__data = bytes([self.access_groups.get(uid, 1)])
            return {'status': True}
        if command == 22:
            uid, group = unpack('<II', payload)
            if not self.ignore_user_write:
                self.access_groups[uid] = group
            return {'status': True}
        assert command == 8
        if self.user_packet_size == 28:
            uid, privilege, password, name, card, group, _, user_id = unpack('<HB5s8sIxBHI', payload)
            group, user_id = str(group), str(user_id)
        else:
            uid, privilege, password, name, card_bytes, group, user_id = unpack('<HB8s24s4sx7sx24s', payload)
            card = unpack('<I', card_bytes)[0]
            group, user_id = group.rstrip(b'\0').decode(), user_id.rstrip(b'\0').decode()
        values = dict(uid=uid, privilege=privilege, password=password.rstrip(b'\0').decode(),
                      name=name.rstrip(b'\0').decode(), card=card, group_id=group, user_id=user_id)
        self.set_user_calls.append(values)
        if not self.ignore_user_write:
            self.users = [SimpleNamespace(**values) if u.uid == uid else u for u in self.users]
        return {'status': True}

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
    def test_existing_finger_error_survives_queue_without_reconnect(self):
        from edge_service_zkteco.zk_enroll import FingerprintAlreadyExistsError
        conn = FakeConn()
        factory = FakeZkFactory([conn])
        client = self._start_client(factory)

        def already_present(conn):
            raise FingerprintAlreadyExistsError('Este dedo ya tiene una huella registrada.')

        with self.assertRaises(FingerprintAlreadyExistsError):
            client._submit('enroll_fingerprint', already_present)
        self.assertEqual(client._submit('next-command', lambda c: 'ok'), 'ok')
        self.assertEqual(factory.connect_attempts, 1)

    def test_command_batch_and_listen_window_prevent_capture_starvation(self):
        from edge_service_zkteco.zk_client import _PendingCommand
        from tests.test_zk_capture import frame, attendance
        config = self._build_config(zk_capture_min_window_seconds=0.04)
        client = ZkClient(config, _SilentLogger(), self.received_events.put)
        conn = FakeConn()
        calls = []
        conn.get_users = lambda: self.fail('Capture must not download users')
        for n in range(7):
            client._commands.put(_PendingCommand('test', lambda c, n=n: calls.append(n)))
        client._execute_pending(conn)
        self.assertEqual(calls, [0, 1, 2, 3])
        self.assertEqual(client._commands.qsize(), 3)
        conn.pending_events.put(frame(attendance('1001')) + frame(attendance('2002')))
        began = time.monotonic()
        client._capture_until_interrupted(conn)
        self.assertGreaterEqual(time.monotonic()-began, 0.04)
        self.assertEqual([self.received_events.get_nowait().user_id for _ in range(2)], ['1001', '2002'])
        client._execute_pending(conn)
        self.assertEqual(calls, list(range(7)))
        self.assertEqual(conn.event_flags[-1], 0)

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
            "zk_capture_min_window_seconds": 0.01,
            "zk_command_batch_size": 4,
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
        conn.user_packet_size = 28
        conn.users = [
            SimpleNamespace(
                uid=7,
                user_id="1001",
                name="Ana",
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
                "name": "Ana",
                "privilege": 1,
                "password": "pw",
                "group_id": "99",
                "user_id": "1001",
                "card": 123,
            },
        )

    def test_access_wire_formats_preserve_admin_and_verify_readback(self):
        from edge_service_zkteco.models import ZkUserInfo
        from edge_service_zkteco.zk_client import _write_user_access
        for size in (28, 72):
            with self.subTest(size=size):
                conn = FakeConn()
                conn.user_packet_size = size
                user = ZkUserInfo(uid=7, user_id='1001', name='Ana', privilege=14,
                                  password='pw', group_id='1', card=123)
                conn.users = [user]
                _write_user_access(conn, user, '99', 15)
                self.assertEqual(conn.users[0].privilege, 15 if size == 28 else 14)
                if size == 72:
                    self.assertEqual(conn.access_groups[7], 99)
                    self.assertEqual(conn.set_user_calls, [])
                _write_user_access(conn, user, '1', 14)
                self.assertEqual(conn.users[0].privilege, 14)
                if size == 72:
                    self.assertEqual(conn.access_groups[7], 1)
                conn.ignore_user_write = True
                with self.assertRaisesRegex(ZkCommandRejectedError, 'readback mismatch'):
                    _write_user_access(conn, user, '99', 15)

    def _speedface_client(self, **config_overrides):
        from edge_service_zkteco.models import ZkUserInfo
        conn = FakeConn()
        conn.user_packet_size = 72
        conn.users = [SimpleNamespace(uid=7, user_id="1001", name="Ana", privilege=0,
                                      password="", group_id="", card=0)]
        conn.access_groups = {7: 1}
        client = self._start_client(FakeZkFactory([conn]), **config_overrides)
        client.get_users()  # detecta el formato de 72 bytes
        user = ZkUserInfo(uid=7, user_id="1001", name="Ana", privilege=0,
                          password="", group_id="", card=0)
        return conn, client, user

    def _group_reads(self, conn):
        return sum(1 for command, _ in conn.access_commands if command == 21)

    def test_access_group_is_cached_between_syncs(self):
        conn, client, user = self._speedface_client()

        self.assertEqual(client.get_access_group(user), "1")
        self.assertEqual(client.get_access_group(user), "1")

        self.assertEqual(self._group_reads(conn), 1)

    def test_verified_write_updates_cache_without_extra_read(self):
        conn, client, user = self._speedface_client()
        client.set_user_access(user, "99", True)
        reads_after_write = self._group_reads(conn)  # la verificacion de la escritura

        self.assertEqual(client.get_access_group(user), "99")
        self.assertEqual(self._group_reads(conn), reads_after_write)

    def test_failed_write_forgets_cached_group(self):
        conn, client, user = self._speedface_client()
        client.get_access_group(user)
        conn.ignore_user_write = True

        with self.assertRaises(ZkCommandRejectedError):
            client.set_user_access(user, "99", True)
        reads_before = self._group_reads(conn)
        client.get_access_group(user)

        self.assertEqual(self._group_reads(conn), reads_before + 1)

    def test_access_group_cache_disabled_with_zero_ttl(self):
        conn, client, user = self._speedface_client(zk_access_group_cache_seconds=0)

        client.get_access_group(user)
        client.get_access_group(user)

        self.assertEqual(self._group_reads(conn), 2)

    def test_access_group_cache_survives_restart(self):
        cache_file = Path(self._tmpdir.name) / "zk_access_groups.json"
        conn, client, user = self._speedface_client(zk_access_group_cache_file=cache_file)
        client.get_access_group(user)
        client.save_access_group_cache()
        client.stop()

        conn2, client2, user2 = self._speedface_client(zk_access_group_cache_file=cache_file)
        self.assertEqual(client2.get_access_group(user2), "1")
        self.assertEqual(self._group_reads(conn2), 0)

    def test_persisted_cache_ignored_for_other_person_or_expired(self):
        import json as _json
        cache_file = Path(self._tmpdir.name) / "zk_access_groups.json"
        cache_file.write_text(_json.dumps({
            "7": {"user_id": "9999", "group": "99", "confirmed_at": time.time()},
        }), encoding="utf-8")
        conn, client, user = self._speedface_client(zk_access_group_cache_file=cache_file)
        self.assertEqual(client.get_access_group(user), "1")  # UID reasignado: se relee
        self.assertEqual(self._group_reads(conn), 1)
        client.stop()

        cache_file.write_text(_json.dumps({
            "7": {"user_id": "1001", "group": "99", "confirmed_at": time.time() - 7 * 3600},
        }), encoding="utf-8")
        conn2, client2, user2 = self._speedface_client(zk_access_group_cache_file=cache_file)
        self.assertEqual(client2.get_access_group(user2), "1")  # vencida: se relee
        self.assertEqual(self._group_reads(conn2), 1)

    def test_corrupt_cache_file_is_ignored(self):
        cache_file = Path(self._tmpdir.name) / "zk_access_groups.json"
        cache_file.write_text("{no es json", encoding="utf-8")
        conn, client, user = self._speedface_client(zk_access_group_cache_file=cache_file)

        self.assertEqual(client.get_access_group(user), "1")
        self.assertEqual(self._group_reads(conn), 1)

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
