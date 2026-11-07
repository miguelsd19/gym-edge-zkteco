import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from edge_service_zkteco.fingerprint_inventory import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNREACHABLE,
    build_parser,
    run_inventory,
)


class FakeConn:
    def __init__(self):
        self.users_list = [
            SimpleNamespace(uid=1, user_id="7904", name="Ana"),
            SimpleNamespace(uid=2, user_id="7611", name="Luis"),
            SimpleNamespace(uid=3, user_id="", name="Sin ID"),
        ]
        self.templates_list = [
            SimpleNamespace(uid=1, fid=0, template=b"\x01"),
            SimpleNamespace(uid=1, fid=5, template=b"\x02"),
        ]
        self.templates_error = None
        self.disconnected = False
        self.writes = []

    def read_sizes(self):
        self.users = 3
        self.fingers = 2
        self.faces = 4

    def get_users(self):
        return self.users_list

    def get_templates(self):
        if self.templates_error:
            raise self.templates_error
        return self.templates_list

    def disconnect(self):
        self.disconnected = True

    def __getattr__(self, name):
        # Cualquier otro metodo (set_user, delete_user...) seria una escritura.
        if name.startswith(("set_", "delete_", "clear_", "enroll")):
            def _write(*args, **kwargs):
                self.writes.append(name)
            return _write
        raise AttributeError(name)


class FakeZK:
    def __init__(self, conn=None, error=None):
        self.conn = conn
        self.error = error

    def __call__(self, *args, **kwargs):
        return self

    def connect(self):
        if self.error:
            raise self.error
        return self.conn


def _run(conn=None, error=None):
    tmp = tempfile.TemporaryDirectory()
    out = Path(tmp.name) / "sub" / "huellas.json"
    args = build_parser().parse_args(["--host", "10.0.0.5", "--out", str(out)])
    with redirect_stdout(io.StringIO()) as stdout:
        code = run_inventory(args, zk_factory=FakeZK(conn, error))
    return tmp, out, code, stdout.getvalue()


class FingerprintInventoryTests(unittest.TestCase):
    def test_inventory_counts_fingerprints_per_user_without_templates_or_names(self):
        conn = FakeConn()
        tmp, out, code, stdout = _run(conn)
        with tmp:
            self.assertEqual(code, EXIT_OK)
            payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["users"],
            [{"user_id": "7611", "fingerprints": 0}, {"user_id": "7904", "fingerprints": 2}],
        )
        self.assertEqual(payload["device_totals"], {"users": 3, "fingers": 2, "faces": 4})
        self.assertIn("read_at", payload)
        raw = json.dumps(payload)
        self.assertNotIn("template", raw)
        self.assertNotIn("Ana", raw)
        self.assertTrue(conn.disconnected)
        self.assertEqual(conn.writes, [])
        self.assertIn("con huella: 1", stdout)

    def test_inventory_unreachable_reader(self):
        tmp, out, code, stdout = _run(error=OSError("timed out"))
        with tmp:
            self.assertEqual(code, EXIT_UNREACHABLE)
            self.assertFalse(out.exists())
        self.assertIn("GymEdgeZkteco", stdout)

    def test_inventory_read_failure_writes_nothing_and_disconnects(self):
        conn = FakeConn()
        conn.templates_error = RuntimeError("can't reach device")
        tmp, out, code, _ = _run(conn)
        with tmp:
            self.assertEqual(code, EXIT_FAILED)
            self.assertFalse(out.exists())
        self.assertTrue(conn.disconnected)


if __name__ == "__main__":
    unittest.main()
