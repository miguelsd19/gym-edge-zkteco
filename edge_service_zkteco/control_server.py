from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

# Alias local del path de sync: el backend usa el sufijo configurado en
# ACCESS_EDGE_ROLE_SYNC_PATH (default /hikvision/user-role-sync); este alias
# permite migrar a un nombre neutral sin romper compatibilidad.
ZK_ROLE_SYNC_ALIAS_PATH = "/zkteco/user-role-sync"


class EdgeControlServer:
    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        path: str,
        sync_user_roles_path: str,
        bearer_token: str,
        logger: logging.Logger,
        on_open: Callable[[dict[str, Any]], dict[str, Any]],
        on_sync_user_roles: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.bind_host = bind_host
        self.port = port
        self.path = path if path.startswith("/") else f"/{path}"
        self.sync_user_roles_path = (
            sync_user_roles_path
            if sync_user_roles_path.startswith("/")
            else f"/{sync_user_roles_path}"
        )
        self.sync_paths = {self.sync_user_roles_path, ZK_ROLE_SYNC_ALIAS_PATH}
        self.known_paths = {self.path} | self.sync_paths
        self.bearer_token = bearer_token
        self.logger = logger
        self.on_open = on_open
        self.on_sync_user_roles = on_sync_user_roles
        self._server = ThreadingHTTPServer((bind_host, port), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()
        self.logger.info(
            "Edge control listener started on http://%s:%s%s and http://%s:%s%s.",
            self.bind_host,
            self.port,
            self.path,
            self.bind_host,
            self.port,
            self.sync_user_roles_path,
        )

    def _build_handler(self):
        outer = self

        class ControlHandler(BaseHTTPRequestHandler):
            server_version = "EdgeControlServer/1.0"

            def do_POST(self) -> None:  # noqa: N802
                if self.path not in outer.known_paths:
                    self._send_json(404, {"success": False, "message": "Not found."})
                    return

                if self.headers.get("Authorization", "") != f"Bearer {outer.bearer_token}":
                    self._send_json(401, {"success": False, "message": "Unauthorized."})
                    return

                length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                    if not isinstance(payload, dict):
                        raise ValueError("payload must be an object")
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    self._send_json(400, {"success": False, "message": "Invalid JSON payload."})
                    return

                try:
                    if self.path == outer.path:
                        response_payload = outer.on_open(payload)
                    elif outer.on_sync_user_roles is not None:
                        response_payload = outer.on_sync_user_roles(payload)
                    else:
                        self._send_json(404, {"success": False, "message": "Not found."})
                        return
                except ValueError as exc:
                    self._send_json(
                        400,
                        {"success": False, "message": str(exc) or "Invalid request."},
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    outer.logger.exception("Edge control command failed: %s", exc)
                    self._send_json(
                        502,
                        {
                            "success": False,
                            "message": f"No se pudo completar la operacion del edge: {exc}",
                        },
                    )
                    return

                self._send_json(200, response_payload)

            def do_GET(self) -> None:  # noqa: N802
                if self.path in outer.known_paths:
                    self._send_json(
                        200,
                        {"success": True, "message": "ok", "device": "zkteco"},
                    )
                    return
                self._send_json(404, {"success": False, "message": "Not found."})

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                outer.logger.debug("Edge control server: " + format, *args)

            def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return ControlHandler
