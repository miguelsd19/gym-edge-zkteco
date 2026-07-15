import json
import unittest
from unittest.mock import patch

from edge_service_zkteco.backend_api import BackendApiClient, BackendUnavailableError


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class BackendApiClientTests(unittest.TestCase):
    def _build_client(self, access_mode="biometric"):
        config = type(
            "Config",
            (),
            {
                "access_mode": access_mode,
                "device_token": "token",
                "backend_base_url": "http://backend.test",
                "validation_path": (
                    "/api/access/validate/"
                    if access_mode == "rfid"
                    else "/api/access/biometric-events/"
                ),
                "request_timeout_seconds": 3.0,
            },
        )()
        return BackendApiClient(config)

    @patch("edge_service_zkteco.backend_api.urllib.request.urlopen")
    def test_validate_defaults_should_open_to_granted_when_field_missing(self, urlopen):
        urlopen.return_value = _FakeResponse(
            {
                "granted": True,
                "reason": "Access granted",
                "member_name": "Juan Perez",
                "event_id": "evt-1",
            }
        )
        client = self._build_client(access_mode="rfid")

        result = client.validate("AABBCCDD")

        self.assertTrue(result.granted)
        self.assertTrue(result.should_open_door)

    @patch("edge_service_zkteco.backend_api.urllib.request.urlopen")
    def test_validate_respects_explicit_false_string_for_should_open(self, urlopen):
        urlopen.return_value = _FakeResponse(
            {
                "granted": True,
                "should_open_door": "false",
                "reason": "Allowed but should not open",
                "member_name": "Juan Perez",
                "event_id": "evt-2",
            }
        )
        client = self._build_client(access_mode="biometric")

        result = client.validate("1001")

        self.assertTrue(result.granted)
        self.assertFalse(result.should_open_door)

    @patch("edge_service_zkteco.backend_api.urllib.request.urlopen")
    def test_validate_sends_device_user_id_and_timestamp_in_biometric_mode(self, urlopen):
        urlopen.return_value = _FakeResponse({"granted": True})
        client = self._build_client(access_mode="biometric")

        client.validate("1001", event_timestamp="2026-07-02T10:00:00")

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["device_user_id"], "1001")
        self.assertEqual(payload["event_timestamp"], "2026-07-02T10:00:00")
        self.assertIn("/api/access/biometric-events/", request.full_url)
        self.assertEqual(request.headers.get("Authorization"), "Bearer token")

    @patch("edge_service_zkteco.backend_api.urllib.request.urlopen")
    def test_validate_raises_on_invalid_json(self, urlopen):
        class _BrokenResponse:
            def read(self):
                return b"not-json"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        urlopen.return_value = _BrokenResponse()
        client = self._build_client()

        with self.assertRaises(BackendUnavailableError):
            client.validate("1001")

    @patch("edge_service_zkteco.backend_api.urllib.request.urlopen")
    def test_report_edge_health_serializes_device_last_success_at(self, urlopen):
        urlopen.return_value = _FakeResponse({"status": "healthy"})
        client = self._build_client()

        client.report_edge_health(
            status="healthy",
            message="Conectado al ZKTeco.",
            device_last_success_at="2026-07-02T10:00:00+00:00",
        )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(
            payload["hikvision_last_success_at"],
            "2026-07-02T10:00:00+00:00",
        )
        self.assertIn("/api/access/edge-health/", request.full_url)

    @patch("edge_service_zkteco.backend_api.urllib.request.urlopen")
    def test_report_edge_health_omits_last_success_when_empty(self, urlopen):
        urlopen.return_value = _FakeResponse({"status": "degraded"})
        client = self._build_client()

        client.report_edge_health(status="degraded", message="Sin respuesta.")

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertNotIn("hikvision_last_success_at", payload)
