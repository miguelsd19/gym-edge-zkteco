import json
import logging
import unittest
import urllib.error
import urllib.request

from edge_service_zkteco.control_server import EdgeControlServer


def _request(url, *, method="POST", token="token", payload=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=3.0) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


class EdgeControlServerTests(unittest.TestCase):
    def setUp(self):
        self.open_calls = []
        self.sync_calls = []
        self.on_open_error = None

        def on_open(payload):
            if self.on_open_error is not None:
                raise self.on_open_error
            self.open_calls.append(payload)
            return {"success": True, "message": "Torniquete abierto correctamente."}

        def on_sync(payload):
            self.sync_calls.append(payload)
            return {"success": True, "message": "ok"}

        logger = logging.getLogger("test-control-server")
        logger.addHandler(logging.NullHandler())
        self.server = EdgeControlServer(
            bind_host="127.0.0.1",
            port=0,
            path="/door/open",
            sync_user_roles_path="/hikvision/user-role-sync",
            bearer_token="token",
            logger=logger,
            on_open=on_open,
            on_sync_user_roles=on_sync,
        )
        self.port = self.server._server.server_address[1]
        self.server.start()
        self.addCleanup(self.server._server.shutdown)

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_post_door_open_invokes_callback(self):
        status, body = _request(
            self._url("/door/open"),
            payload={"requested_by": "admin"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(body["success"])
        self.assertEqual(self.open_calls, [{"requested_by": "admin"}])

    def test_post_sync_path_and_zk_alias_route_to_same_handler(self):
        _request(self._url("/hikvision/user-role-sync"), payload={"reason": "a"})
        _request(self._url("/zkteco/user-role-sync"), payload={"reason": "b"})

        self.assertEqual(
            [call["reason"] for call in self.sync_calls],
            ["a", "b"],
        )

    def test_rejects_missing_or_wrong_bearer_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _request(self._url("/door/open"), token="wrong")
        self.assertEqual(ctx.exception.code, 401)

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _request(self._url("/door/open"), token=None)
        self.assertEqual(ctx.exception.code, 401)

    def test_unknown_path_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _request(self._url("/other"))
        self.assertEqual(ctx.exception.code, 404)

    def test_device_failure_returns_502(self):
        self.on_open_error = RuntimeError("device unreachable")

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _request(self._url("/door/open"))
        self.assertEqual(ctx.exception.code, 502)

    def test_value_error_returns_400(self):
        self.on_open_error = ValueError("bad request")

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _request(self._url("/door/open"))
        self.assertEqual(ctx.exception.code, 400)

    def test_extra_routes_are_dispatched_and_absent_by_default(self):
        logger = logging.getLogger("test-control-server")
        calls = []
        server = EdgeControlServer(
            bind_host="127.0.0.1",
            port=0,
            path="/door/open",
            sync_user_roles_path="/hikvision/user-role-sync",
            bearer_token="token",
            logger=logger,
            on_open=lambda payload: {"success": True},
            extra_routes={"enroll": lambda payload: calls.append(payload) or {"success": True, "job_id": "j1"}},
        )
        port = server._server.server_address[1]
        server.start()
        self.addCleanup(server._server.shutdown)

        status, body = _request(f"http://127.0.0.1:{port}/enroll", payload={"member_id": "m-1"})

        self.assertEqual((status, body["job_id"]), (200, "j1"))
        self.assertEqual(calls, [{"member_id": "m-1"}])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _request(self._url("/enroll"), payload={})
        self.assertEqual(ctx.exception.code, 404)

    def test_get_healthcheck_reports_zkteco_device(self):
        status, body = _request(self._url("/door/open"), method="GET")

        self.assertEqual(status, 200)
        self.assertTrue(body["success"])
        self.assertEqual(body["device"], "zkteco")
