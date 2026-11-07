import io
import logging
import socket
import time
import unittest
from contextlib import redirect_stdout
from struct import pack, unpack
from types import SimpleNamespace

from edge_service_zkteco import enroll_probe
from edge_service_zkteco.models import UserRoleAssignment, ZkUserInfo
from edge_service_zkteco.remote_enroll import DONE, FAILED, RemoteEnrollManager
from edge_service_zkteco.zk_client import (
    FACE_NO_CAPTURE,
    FACE_NOT_SAVED,
    FACE_SAVED,
    ZkCommandRejectedError,
    ZkUnavailableError,
    create_device_user,
    enroll_device_face,
    enroll_device_fingerprint,
)


class _FakeSock:
    def __init__(self):
        self.timeout = 5.0
        self.packets = []

    def recv(self, size):
        if self.packets:
            return self.packets.pop(0)
        raise socket.timeout()

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        command = unpack('<H', data[8:10])[0]
        if command == 61:
            identifier, finger, _ = unpack('<24sbb', data[16:])
            user_id = identifier.split(b'\0')[0].decode()
            user = next(u for u in self.conn.users if u.user_id == user_id)
            if finger != 111:
                self.conn.enroll_calls.append((user.uid, finger, user_id))
                if self.conn.enroll_result and self.conn.store_template:
                    self.conn.templates[(user.uid, finger)] = b'tpl'
        ack = pack('<4H', 2000, 0, 1, 1)
        self.packets.append(pack('<HHI', 0x5050, 0x7d82, len(ack)) + ack)
        if command == 61 and finger == 111:
            # Cara: los eventos que mande el lector, sin evento final.
            for payload in self.conn.face_packets:
                event = pack('<4H', 500, 0, 1, 1) + payload
                self.packets.append(pack('<HHI', 0x5050, 0x7d82, len(event)) + event)
        elif command == 61:
            # Evento final del registro (resultado, tamano, user_id), como el lector.
            final = pack('<4H', 500, 0, 1, 1) + pack('<HH', 0, 3) + user_id.encode().ljust(15, b'\0')
            self.packets.append(pack('<HHI', 0x5050, 0x7d82, len(final)) + final)


class _FakeConn:
    """Lector SpeedFace (registro de 72 bytes) en memoria."""

    user_packet_size = 72
    tcp = True

    def __init__(self, users=(), enroll_result=True, store_template=True):
        self.users = [SimpleNamespace(**user) for user in users]
        self.groups = {}
        self.templates = {}
        self.enroll_result = enroll_result
        self.store_template = store_template
        self.enroll_calls = []
        self.deleted = []
        self._ZK__sock = _FakeSock()
        self._ZK__sock.conn = self
        self._ZK__session_id = 1
        self._ZK__reply_id = 0
        self._ZK__data = b""
        self.disconnected = False
        self.faces = 29
        self.face_packets = []
        self.face_saves = True

    def _ZK__create_header(self, command, payload, session, reply):
        return pack('<4H', command, 0, session, reply + 1) + payload

    def _ZK__create_tcp_top(self, data):
        return pack('<HHI', 0x5050, 0x7d82, len(data)) + data

    # registro de cara
    def read_sizes(self):
        return True

    def cancel_capture(self):
        return True

    def _ZK__ack_ok(self):
        if self.face_saves:
            self.faces += 1

    # pyzk
    def connect(self):
        return self

    def disconnect(self):
        self.disconnected = True

    def get_users(self):
        return list(self.users)

    def set_user(self, uid, name, privilege, password, group_id, user_id, card):
        self.users.append(SimpleNamespace(
            uid=uid, user_id=user_id, name=name, privilege=privilege,
            password=password, group_id=group_id, card=card,
        ))

    def _ZK__send_command(self, command, payload=b"", size=8):
        if command == 22:
            uid, group = unpack("<II", payload)
            self.groups[uid] = group
            return {"status": True}
        if command == 21:
            uid = unpack("<I", payload)[0]
            self._ZK__data = pack("<I", self.groups.get(uid, 1))
            return {"status": True}
        raise AssertionError(command)

    def refresh_data(self):
        return True

    def enroll_user(self, uid, temp_id, user_id):
        self.enroll_calls.append((uid, temp_id, user_id))
        self._ZK__sock.settimeout(60)  # como pyzk
        if self.enroll_result and self.store_template:
            self.templates[(uid, temp_id)] = b"tpl"
        return self.enroll_result

    def get_user_template(self, uid, temp_id):
        return self.templates.get((uid, temp_id))

    def delete_user(self, uid):
        self.deleted.append(uid)
        self.users = [user for user in self.users if user.uid != uid]

    def get_device_name(self):
        return "SpeedFace-V5L"

    def get_firmware_version(self):
        return "Ver 6.60"

    def get_fp_version(self):
        return 10


def _existing(uid, user_id, privilege=0):
    return {"uid": uid, "user_id": user_id, "name": f"User {user_id}", "privilege": privilege,
            "password": "", "group_id": "", "card": 0}


class DeviceHelpersTests(unittest.TestCase):
    def test_create_user_uses_next_uid_and_blocked_group(self):
        conn = _FakeConn(users=[_existing(5, "100"), _existing(9, "101")])

        user, created = create_device_user(conn, "3800", "Ana Lopez", "99")

        self.assertTrue(created)
        self.assertEqual((user.uid, user.user_id, user.group_id), (10, "3800", "99"))
        self.assertEqual(conn.groups[10], 99)

    def test_create_user_returns_existing_without_writing(self):
        conn = _FakeConn(users=[_existing(5, "3800")])

        user, created = create_device_user(conn, "3800", "Ana Lopez", "99")

        self.assertFalse(created)
        self.assertEqual(user.uid, 5)
        self.assertEqual(conn.groups, {})

    def test_enroll_restores_socket_timeout_and_checks_template(self):
        conn = _FakeConn(users=[_existing(3, '3800')])
        user = ZkUserInfo(uid=3, user_id="3800", name="Ana", privilege=0, password="", group_id="99", card=0)

        self.assertTrue(enroll_device_fingerprint(conn, user, 6))
        self.assertEqual(conn.enroll_calls, [(3, 6, "3800")])
        self.assertEqual(conn._ZK__sock.timeout, 5.0)

    def test_enroll_without_saved_template_is_not_success(self):
        conn = _FakeConn(users=[_existing(3, '3800')], store_template=False)
        user = ZkUserInfo(uid=3, user_id="3800", name="Ana", privilege=0, password="", group_id="99", card=0)

        self.assertFalse(enroll_device_fingerprint(conn, user, 6))


class _FakeBackend:
    def __init__(self, assignments):
        self.assignments = assignments

    def fetch_user_roles(self, member_ids=None):
        return [item for item in self.assignments if item.member_id in (member_ids or [])]


class _FakeDevice:
    def __init__(self, users=(), enroll_result=True, enroll_error=None, face_result="saved"):
        self.users = list(users)
        self.enroll_result = enroll_result
        self.enroll_error = enroll_error
        self.face_result = face_result
        self.order = []
        self.created = []
        self.enrolled = []

    def get_users(self):
        return list(self.users)

    def create_user(self, user_id, name, group_id):
        user = ZkUserInfo(uid=len(self.users) + 1, user_id=user_id, name=name, privilege=0,
                          password="", group_id=group_id, card=0)
        self.users.append(user)
        self.created.append((user_id, name, group_id))
        return user, True

    def enroll_fingerprint(self, user, finger_index, timeout):
        if self.enroll_error is not None:
            raise self.enroll_error
        self.enrolled.append((user.user_id, finger_index))
        self.order.append("fingerprint")
        return self.enroll_result

    def enroll_face(self, user, timeout):
        self.order.append("face")
        if isinstance(self.face_result, Exception):
            raise self.face_result
        return self.face_result


def _assignment(member_id="m-1", device_user_id="3800"):
    return UserRoleAssignment(
        member_id=member_id, member_name="Ana Lopez", rfid_code=device_user_id,
        device_user_id=device_user_id, target_user_type="normal", reason="",
    )


def _manager(device, assignments=None, protected=()):
    logger = logging.getLogger("test-remote-enroll")
    logger.addHandler(logging.NullHandler())
    synced = []
    manager = RemoteEnrollManager(
        logger=logger,
        config=SimpleNamespace(
            zk_protected_user_ids=frozenset(protected),
            zk_blocked_group_id="99",
            zk_enroll_timeout_seconds=60,
        ),
        backend_api=_FakeBackend(assignments if assignments is not None else [_assignment()]),
        device=device,
        identifier_for=lambda assignment: assignment.rfid_code or assignment.device_user_id,
        sync_member=synced.append,
    )
    return manager, synced


def _wait(manager, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = manager.handle_status({"job_id": job_id})
        if snapshot["status"] in (DONE, FAILED):
            return snapshot
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class RemoteEnrollManagerTests(unittest.TestCase):
    def test_creates_missing_user_enrolls_and_syncs_member(self):
        device = _FakeDevice()
        manager, synced = _manager(device)

        started = manager.handle_start({"member_id": "m-1", "biometrics": ["fingerprint"], "finger_index": 6})
        result = _wait(manager, started["job_id"])

        self.assertEqual(result["status"], DONE)
        self.assertTrue(result["created_user"])
        self.assertEqual(result["results"], {"user": "created", "fingerprint": "saved"})
        self.assertEqual(result["error_code"], "")
        self.assertEqual(device.created, [("3800", "Ana Lopez", "99")])
        self.assertEqual(device.enrolled, [("3800", 6)])
        self.assertEqual(synced, ["m-1"])

    def test_registration_only_creates_user_without_fingerprint(self):
        device = _FakeDevice()
        manager, synced = _manager(device)

        result = _wait(manager, manager.handle_start({"member_id": "m-1", "biometrics": []})["job_id"])

        self.assertEqual(result["status"], DONE)
        self.assertEqual(result["results"], {"user": "created"})
        self.assertEqual(result["message"], "Alta en el lector lista.")
        self.assertEqual(device.enrolled, [])
        self.assertEqual(synced, ["m-1"])

    def test_default_biometrics_is_fingerprint_for_older_backends(self):
        device = _FakeDevice()
        manager, _ = _manager(device)
        result = _wait(manager, manager.handle_start({"member_id": "m-1"})["job_id"])
        self.assertEqual(result["results"], {"user": "created", "fingerprint": "saved"})

    def test_existing_user_is_enrolled_without_creating(self):
        device = _FakeDevice(users=[ZkUserInfo(uid=1, user_id="3800", name="Ana", privilege=0,
                                               password="", group_id="1", card=0)])
        manager, _ = _manager(device)

        result = _wait(manager, manager.handle_start({"member_id": "m-1", "biometrics": ["fingerprint"]})["job_id"])

        self.assertEqual(result["status"], DONE)
        self.assertFalse(result["created_user"])
        self.assertEqual(result["results"]["user"], "existing")
        self.assertEqual(device.created, [])

    def test_fingerprint_then_face(self):
        device = _FakeDevice()
        manager, synced = _manager(device)
        result = _wait(manager, manager.handle_start(
            {"member_id": "m-1", "biometrics": ["face", "fingerprint"], "finger_index": 6})["job_id"])
        self.assertEqual(result["status"], DONE)
        self.assertEqual(result["results"], {"user": "created", "fingerprint": "saved", "face": "saved"})
        self.assertEqual(result["message"], "Alta en el lector lista: huella y cara guardadas.")
        self.assertEqual(device.order, ["fingerprint", "face"])
        self.assertEqual(synced, ["m-1"])

    def test_face_only(self):
        device = _FakeDevice()
        manager, _ = _manager(device)
        result = _wait(manager, manager.handle_start({"member_id": "m-1", "biometrics": ["face"]})["job_id"])
        self.assertEqual(result["results"], {"user": "created", "face": "saved"})
        self.assertEqual(device.enrolled, [])

    def test_face_is_tried_even_if_fingerprint_fails(self):
        device = _FakeDevice(enroll_result=False)
        manager, synced = _manager(device)
        result = _wait(manager, manager.handle_start(
            {"member_id": "m-1", "biometrics": ["fingerprint", "face"]})["job_id"])
        self.assertEqual((result["status"], result["error_code"]), (FAILED, "RL-302"))
        self.assertEqual(result["results"], {"user": "created", "fingerprint": "failed", "face": "saved"})
        self.assertEqual(device.order, ["fingerprint", "face"])
        self.assertIn("La cara si quedo guardada.", result["message"])
        self.assertEqual(synced, ["m-1"])

    def test_both_failing_reports_both_codes(self):
        device = _FakeDevice(enroll_result=False, face_result="no_capture")
        manager, _ = _manager(device)
        result = _wait(manager, manager.handle_start(
            {"member_id": "m-1", "biometrics": ["fingerprint", "face"]})["job_id"])
        self.assertEqual(result["error_code"], "RL-302")
        self.assertIn("RL-302", result["error_detail"])
        self.assertIn("RL-401", result["error_detail"])
        self.assertEqual(result["results"], {"user": "created", "fingerprint": "failed", "face": "failed"})

    def test_face_failures_have_codes_and_keep_the_fingerprint(self):
        for face_result, code in (
            ("no_capture", "RL-401"),
            ("not_saved", "RL-402"),
            (ZkUnavailableError("timeout"), "RL-401"),
        ):
            device = _FakeDevice(face_result=face_result)
            manager, synced = _manager(device)
            result = _wait(manager, manager.handle_start(
                {"member_id": "m-1", "biometrics": ["fingerprint", "face"]})["job_id"])
            self.assertEqual((result["status"], result["error_code"]), (FAILED, code))
            self.assertEqual(result["results"], {"user": "created", "fingerprint": "saved", "face": "failed"})
            self.assertEqual(synced, ["m-1"])

    def test_refuses_protected_and_admin_users(self):
        manager, _ = _manager(_FakeDevice(), protected={"3800"})
        result = _wait(manager, manager.handle_start({"member_id": "m-1"})["job_id"])
        self.assertEqual((result["status"], result["error_code"]), (FAILED, "RL-201"))
        self.assertIn("protegido", result["message"])

        admin = ZkUserInfo(uid=1, user_id="3800", name="Admin", privilege=14, password="", group_id="1", card=0)
        device = _FakeDevice(users=[admin])
        manager, _ = _manager(device)
        result = _wait(manager, manager.handle_start({"member_id": "m-1"})["job_id"])
        self.assertEqual((result["status"], result["error_code"]), (FAILED, "RL-201"))
        self.assertEqual(device.enrolled, [])

    def test_member_without_biometric_id_fails(self):
        manager, _ = _manager(_FakeDevice(), assignments=[])
        result = _wait(manager, manager.handle_start({"member_id": "m-1"})["job_id"])
        self.assertEqual((result["status"], result["error_code"]), (FAILED, "RL-003"))
        self.assertIn("ID biometrico", result["message"])

    def test_fingerprint_failures_have_codes_and_still_apply_access(self):
        for device, code in (
            (_FakeDevice(enroll_result=False), "RL-302"),
            (_FakeDevice(enroll_error=ZkUnavailableError("timeout")), "RL-301"),
        ):
            manager, synced = _manager(device)
            result = _wait(manager, manager.handle_start({"member_id": "m-1", "biometrics": ["fingerprint"]})["job_id"])
            self.assertEqual((result["status"], result["error_code"]), (FAILED, code))
            self.assertEqual(result["results"], {"user": "created", "fingerprint": "failed"})
            # El usuario ya quedo en el lector: se aplica su acceso igual.
            self.assertEqual(synced, ["m-1"])

    def test_unreachable_reader_is_rl101(self):
        class _Down(_FakeDevice):
            def get_users(self):
                raise ZkUnavailableError("ZK device thread is not running.")

        manager, synced = _manager(_Down())
        result = _wait(manager, manager.handle_start({"member_id": "m-1", "biometrics": []})["job_id"])
        self.assertEqual((result["status"], result["error_code"]), (FAILED, "RL-101"))
        self.assertEqual(synced, [])

    def test_sync_failure_is_a_warning(self):
        device = _FakeDevice()
        manager, _ = _manager(device)

        def failing_sync(member_id):
            raise RuntimeError("backend down")

        manager.sync_member = failing_sync
        result = _wait(manager, manager.handle_start({"member_id": "m-1", "biometrics": []})["job_id"])
        self.assertEqual(result["status"], DONE)
        self.assertEqual(result["warning_code"], "RL-501")

    def test_only_one_enroll_at_a_time_and_validates_input(self):
        manager, _ = _manager(_FakeDevice())
        manager._active_job_id = "busy"
        with self.assertRaises(ValueError):
            manager.handle_start({"member_id": "m-1"})
        manager._active_job_id = None
        for payload in ({}, {"member_id": "m-1", "finger_index": 10}, {"member_id": "m-1", "finger_index": "x"},
                        {"member_id": "m-1", "biometrics": ["iris"]}, {"member_id": "m-1", "biometrics": "fingerprint"}):
            with self.assertRaises(ValueError):
                manager.handle_start(payload)
        with self.assertRaises(ValueError):
            manager.handle_status({"job_id": "nope"})


class EnrollProbeTests(unittest.TestCase):
    def _run(self, conn, *argv):
        args = enroll_probe.build_parser().parse_args(["--host", "1.2.3.4", *argv])
        out = io.StringIO()
        with redirect_stdout(out):
            code = enroll_probe.run_probe(args, zk_factory=lambda *a, **k: conn)
        return code, out.getvalue()

    def test_probe_success_deletes_test_user(self):
        conn = _FakeConn(users=[_existing(1, "100")])

        code, output = self._run(conn)

        self.assertEqual(code, enroll_probe.EXIT_OK)
        self.assertEqual(conn.deleted, [2])
        self.assertEqual([user.user_id for user in conn.users], ["100"])
        self.assertIn("SI acepta", output)

    def test_probe_face_mode(self):
        conn = _FakeConn()
        conn.face_packets = [b"\x01\x02"]
        code, output = self._run(conn, "--modo", "cara", "--face-timeout", "2", "--verbose")
        self.assertEqual(code, enroll_probe.EXIT_OK)
        self.assertIn("[CARA] OK", output)
        self.assertRegex(output, r"paquete 1: f401[0-9a-f]{12}0102\n")
        self.assertEqual(conn.enroll_calls, [])

    def test_probe_both_reports_what_was_not_saved(self):
        conn = _FakeConn()
        conn.face_packets = []  # el lector nunca pide la cara
        code, output = self._run(conn, "--modo", "ambas", "--face-timeout", "0.3")
        self.assertEqual(code, enroll_probe.EXIT_NOT_ENROLLED)
        self.assertIn("[HUELLA] OK", output)
        self.assertIn("No se guardo: cara", output)
        self.assertEqual(conn.deleted, [1])

    def test_probe_refuses_existing_test_id(self):
        conn = _FakeConn(users=[_existing(1, "990001")])

        code, _ = self._run(conn)

        self.assertEqual(code, enroll_probe.EXIT_USER_EXISTS)
        self.assertEqual(conn.deleted, [])
        self.assertEqual(conn.enroll_calls, [])

    def test_probe_reports_unsupported_and_cleans_up(self):
        conn = _FakeConn(enroll_result=False)

        code, output = self._run(conn, "--user-id", "990002")

        self.assertEqual(code, enroll_probe.EXIT_NOT_ENROLLED)
        self.assertEqual(conn.deleted, [1])
        self.assertIn("--verbose", output)

    def test_probe_keep_leaves_test_user(self):
        conn = _FakeConn()
        code, _ = self._run(conn, "--keep")
        self.assertEqual(code, enroll_probe.EXIT_OK)
        self.assertEqual(conn.deleted, [])

    def test_probe_does_not_claim_deletion_if_user_remains(self):
        conn = _FakeConn()
        conn.delete_user = lambda uid: None
        code, output = self._run(conn)
        self.assertEqual(code, enroll_probe.EXIT_ERROR)
        self.assertIn('[AVISO] No se pudo borrar', output)
        self.assertNotIn('990001 borrado del lector', output)


if __name__ == "__main__":
    unittest.main()
