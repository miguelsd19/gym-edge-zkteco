import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from edge_service_zkteco.backup_terminal import (
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_UNREACHABLE,
    build_parser,
    run_backup,
)


class FakeConn:
    def __init__(self):
        self.user_records = [
            SimpleNamespace(uid=1, user_id="100", name="Ana Pérez", privilege=0, password="", group_id="1", card=0),
            SimpleNamespace(uid=2, user_id="101", name="Luis", privilege=14, password="1234", group_id="1", card=55),
            SimpleNamespace(uid=3, user_id="100", name="Duplicado", privilege=0, password="", group_id="1", card=0),
        ]
        self.templates = [
            SimpleNamespace(uid=1, fid=0, valid=1, template=b"\x01\x02"),
            SimpleNamespace(uid=1, fid=1, valid=1, template=b"\x03"),
        ]
        self.attendance = [
            SimpleNamespace(uid=1, user_id="100", timestamp=datetime(2026, 10, 1, 8, 30), status=1, punch=0),
        ]
        self.face_count = 2
        self.templates_error = None
        self.calls = []

    # Info del equipo
    def get_firmware_version(self):
        return "Ver 6.60"

    def get_serialnumber(self):
        return "ABC123"

    def get_platform(self):
        return "ZMM220_TFT"

    def get_device_name(self):
        return "SpeedFace-V5L"

    def get_mac(self):
        raise RuntimeError("not supported")

    def get_face_version(self):
        return 7

    def get_fp_version(self):
        return 10

    def get_time(self):
        return datetime(2026, 10, 11, 9, 0)

    def read_sizes(self):
        # pyzk guarda los conteos como atributos enteros tras read_sizes().
        self.users = len(self.user_records)
        self.faces = self.face_count
        self.users_cap = 3000
        self.fingers = len(self.templates)
        self.fingers_cap = 3000
        self.faces_cap = 3000
        self.records = len(self.attendance)
        self.rec_cap = 100000

    # Datos
    def get_users(self):
        return self.user_records

    def get_templates(self):
        if self.templates_error:
            raise self.templates_error
        return self.templates

    def get_attendance(self):
        return self.attendance

    def disable_device(self):
        self.calls.append("disable")

    def enable_device(self):
        self.calls.append("enable")

    def disconnect(self):
        self.calls.append("disconnect")


class FakeZK:
    def __init__(self, conn=None, connect_error=None):
        self.conn = conn
        self.connect_error = connect_error
        self.init_kwargs = None

    def __call__(self, host, **kwargs):
        self.init_kwargs = {"host": host, **kwargs}
        return self

    def connect(self):
        if self.connect_error:
            raise self.connect_error
        return self.conn


def _run(argv, factory):
    with redirect_stdout(io.StringIO()):
        return run_backup(build_parser().parse_args(argv), zk_factory=factory)


class BackupTerminalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "backup"

    def tearDown(self):
        self._tmp.cleanup()

    def test_backup_writes_all_files(self):
        conn = FakeConn()
        factory = FakeZK(conn)

        code = _run(["--host", "10.0.0.5", "--out", str(self.out), "--comm-key", "77"], factory)

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(factory.init_kwargs["password"], 77)
        self.assertEqual(factory.init_kwargs["port"], 4370)

        users = json.loads((self.out / "users.json").read_text(encoding="utf-8"))
        self.assertEqual([u["user_id"] for u in users], ["100", "101", "100"])
        self.assertEqual(users[0]["name"], "Ana Pérez")
        self.assertTrue(users[1]["is_admin"])

        fingers = json.loads((self.out / "fingerprints.json").read_text(encoding="utf-8"))
        self.assertEqual(fingers[0]["template_b64"], "AQI=")
        self.assertEqual(fingers[0]["size"], 2)

        attendance = json.loads((self.out / "attendance.json").read_text(encoding="utf-8"))
        self.assertEqual(attendance[0]["timestamp"], "2026-10-01T08:30:00")

        info = json.loads((self.out / "device_info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["serial_number"], "ABC123")
        self.assertIsNone(info["mac"])
        self.assertEqual(info["sizes"]["faces"], 2)

        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["counts"], {"users": 3, "fingerprints": 2, "attendance": 1})
        self.assertEqual(manifest["failures"], {})
        warnings = " ".join(manifest["warnings"])
        self.assertIn("2 caras", warnings)
        self.assertIn("repetidos", warnings)
        self.assertIn("get_mac", warnings)

        self.assertEqual(conn.calls, ["disconnect"])

    def test_backup_users_csv_counts_fingerprints(self):
        _run(["--host", "10.0.0.5", "--out", str(self.out)], FakeZK(FakeConn()))

        with (self.out / "users.csv").open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["name"], "Ana Pérez")
        self.assertEqual(rows[0]["fingerprints"], "2")
        self.assertEqual(rows[1]["fingerprints"], "0")
        self.assertNotIn("password", rows[0])

    def test_backup_unreachable_device(self):
        code = _run(
            ["--host", "10.0.0.5", "--out", str(self.out)],
            FakeZK(connect_error=OSError("timed out")),
        )

        self.assertEqual(code, EXIT_UNREACHABLE)
        self.assertFalse((self.out / "manifest.json").exists())

    def test_backup_partial_failure_keeps_other_sections(self):
        conn = FakeConn()
        conn.templates_error = RuntimeError("can't read templates")

        code = _run(["--host", "10.0.0.5", "--out", str(self.out)], FakeZK(conn))

        self.assertEqual(code, EXIT_PARTIAL)
        self.assertTrue((self.out / "users.json").exists())
        self.assertTrue((self.out / "users.csv").exists())
        self.assertTrue((self.out / "attendance.json").exists())
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("fingerprints", manifest["failures"])

    def test_backup_disable_device_always_reenables(self):
        conn = FakeConn()

        def boom():
            raise KeyboardInterrupt

        conn.get_users = boom

        with self.assertRaises(KeyboardInterrupt):
            _run(["--host", "10.0.0.5", "--out", str(self.out), "--disable-device"], FakeZK(conn))

        self.assertEqual(conn.calls, ["disable", "enable", "disconnect"])

    def test_backup_skip_flags(self):
        _run(
            ["--host", "10.0.0.5", "--out", str(self.out), "--skip-fingerprints", "--skip-attendance"],
            FakeZK(FakeConn()),
        )

        self.assertFalse((self.out / "fingerprints.json").exists())
        self.assertFalse((self.out / "attendance.json").exists())
        self.assertTrue((self.out / "users.csv").exists())


if __name__ == "__main__":
    unittest.main()
