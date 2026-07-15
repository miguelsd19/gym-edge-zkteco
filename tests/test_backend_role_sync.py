import json
import unittest
from unittest.mock import patch

from edge_service_zkteco.backend_api import BackendApiClient


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class BackendRoleSyncTests(unittest.TestCase):
    def _build_client(self):
        config = type(
            "Config",
            (),
            {
                "device_token": "token",
                "backend_base_url": "http://backend.test",
                "zk_user_role_sync_backend_path": "/api/access/hikvision-user-roles/",
                "request_timeout_seconds": 3.0,
            },
        )()
        return BackendApiClient(config)

    @patch("edge_service_zkteco.backend_api.urllib.request.urlopen")
    def test_fetch_user_roles_parses_assignments(self, urlopen):
        urlopen.return_value = _FakeResponse(
            {
                "results": [
                    {
                        "member_id": "1",
                        "member_name": "Ana Activa",
                        "rfid_code": "RFID001",
                        "device_user_id": "1001",
                        "target_user_type": "normal",
                        "reason": "Access granted",
                    },
                    {
                        "member_id": "2",
                        "member_name": "Beto Vencido",
                        "rfid_code": "RFID002",
                        "device_user_id": "1002",
                        "target_user_type": "blackList",
                        "reason": "No active subscription",
                    },
                ]
            }
        )
        client = self._build_client()

        assignments = client.fetch_user_roles()

        self.assertEqual(len(assignments), 2)
        self.assertEqual(assignments[0].device_user_id, "1001")
        self.assertEqual(assignments[0].target_user_type, "normal")
        self.assertEqual(assignments[1].target_user_type, "blackList")

    @patch("edge_service_zkteco.backend_api.urllib.request.urlopen")
    def test_fetch_user_roles_supports_member_id_filter(self, urlopen):
        urlopen.return_value = _FakeResponse({"results": []})
        client = self._build_client()

        client.fetch_user_roles(member_ids=["m1", "m2"])

        request = urlopen.call_args.args[0]
        self.assertIn("member_ids=m1", request.full_url)
        self.assertIn("member_ids=m2", request.full_url)
