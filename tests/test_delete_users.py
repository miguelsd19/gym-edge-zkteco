import json
import logging
import os
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

from edge_service_zkteco.config import _get_id_set
from edge_service_zkteco.control_server import EdgeControlServer
from edge_service_zkteco.main import _handle_delete_users
from edge_service_zkteco.models import ZkUserInfo


def _user(uid, user_id, privilege=0):
    return ZkUserInfo(
        uid=uid, user_id=user_id, name=f"User {user_id}", privilege=privilege,
        password="", group_id="1", card=0,
    )


class _FakeDevice:
    def __init__(self, users, fail_ids=()):
        self.users = users
        self.fail_ids = set(fail_ids)
        self.deleted = []
        self.refresh_calls = 0

    def get_users(self):
        return self.users

    def delete_user(self, user):
        if user.user_id in self.fail_ids:
            raise RuntimeError("device error")
        self.deleted.append(user.user_id)

    def refresh_data(self):
        self.refresh_calls += 1


def _logger():
    logger = logging.getLogger("test-delete-users")
    logger.addHandler(logging.NullHandler())
    return logger


def _config(protected=()):
    return SimpleNamespace(zk_protected_user_ids=frozenset(protected))


class HandleDeleteUsersTests(unittest.TestCase):
    def test_deletes_only_requested_normal_users(self):
        device = _FakeDevice([
            _user(1, "100"),
            _user(2, "101"),
            _user(3, "7001"),               # protegido por configuracion
            _user(4, "99999", privilege=14),  # admin del lector
            _user(5, "102", privilege=1),     # deshabilitado, no admin
        ])

        body = _handle_delete_users(
            payload={"user_ids": ["100", "102", "7001", "99999", "555"], "reason": "inactivos"},
            logger=_logger(),
            config=_config(protected={"7001"}),
            device=device,
        )

        self.assertEqual(device.deleted, ["100", "102"])
        self.assertEqual(body["results"], {
            "100": "deleted", "102": "deleted", "7001": "protected",
            "99999": "protected", "555": "not_found",
        })
        self.assertEqual((body["deleted"], body["protected"], body["not_found"], body["failed"]), (2, 2, 1, 0))
        self.assertEqual(device.refresh_calls, 1)

    def test_reports_failed_deletes_and_skips_refresh_when_nothing_deleted(self):
        device = _FakeDevice([_user(1, "100")], fail_ids={"100"})

        body = _handle_delete_users(
            payload={"user_ids": ["100"]}, logger=_logger(), config=_config(), device=device
        )

        self.assertEqual(body["results"], {"100": "failed"})
        self.assertEqual(device.refresh_calls, 0)

    def test_rejects_invalid_payloads(self):
        device = _FakeDevice([])
        for payload in ({}, {"user_ids": "100"}, {"user_ids": []}, {"user_ids": [str(i) for i in range(201)]}):
            with self.assertRaises(ValueError):
                _handle_delete_users(payload=payload, logger=_logger(), config=_config(), device=device)
        self.assertEqual(device.deleted, [])


class ProtectedIdsConfigTests(unittest.TestCase):
    def test_parses_ids_and_ranges(self):
        with mock.patch.dict(os.environ, {"ZK_PROTECTED_USER_IDS": "7001-7003, 98989,99999"}):
            self.assertEqual(_get_id_set("ZK_PROTECTED_USER_IDS"), {"7001", "7002", "7003", "98989", "99999"})

    def test_empty_means_no_extra_protection(self):
        with mock.patch.dict(os.environ, {"ZK_PROTECTED_USER_IDS": ""}):
            self.assertEqual(_get_id_set("ZK_PROTECTED_USER_IDS"), frozenset())


class DeleteUsersRouteTests(unittest.TestCase):
    def _start(self, with_delete=True):
        self.calls = []
        kwargs = {}
        if with_delete:
            kwargs = {
                "delete_users_path": "/zkteco/users/delete",
                "on_delete_users": lambda payload: self.calls.append(payload) or {"success": True},
            }
        server = EdgeControlServer(
            bind_host="127.0.0.1", port=0, path="/door/open",
            sync_user_roles_path="/hikvision/user-role-sync", bearer_token="token",
            logger=_logger(), on_open=lambda payload: {"success": True},
            on_sync_user_roles=lambda payload: {"success": True}, **kwargs,
        )
        server.start()
        self.addCleanup(server._server.shutdown)
        return f"http://127.0.0.1:{server._server.server_address[1]}/zkteco/users/delete"

    def _post(self, url, token="token"):
        request = urllib.request.Request(
            url, data=json.dumps({"user_ids": ["100"]}).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=3.0) as response:
            return response.status

    def test_route_calls_delete_handler(self):
        self.assertEqual(self._post(self._start()), 200)
        self.assertEqual(self.calls, [{"user_ids": ["100"]}])

    def test_route_requires_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(self._start(), token="otro")
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(self.calls, [])

    def test_route_absent_without_handler(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(self._start(with_delete=False))
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
