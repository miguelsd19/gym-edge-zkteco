"""Enrolamiento remoto de huella pedido por el backend (boton "Registrar huella").

El backend pide enrolar a un miembro; el edge:
1. Pregunta al backend el ID biometrico del miembro (mismo endpoint del sync).
2. Si el usuario no existe en el lector, lo crea en el grupo bloqueado.
3. Pone el lector en modo enrolamiento: la persona pone el dedo 3 veces.
4. Corre el sync de ese miembro para dejarlo en el grupo que le toca.

Tarda hasta minutos, asi que corre en un hilo aparte: el backend inicia el
trabajo y despues consulta su estado. Solo hay un enrolamiento a la vez.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from edge_service_zkteco.models import USER_DISABLED_FLAG
from edge_service_zkteco.zk_client import ZkUnavailableError

MAX_FINGER_INDEX = 9
KEEP_FINISHED_JOBS = 20

# Estados que ve el backend.
PREPARING = "preparing"
WAITING_FINGER = "waiting_finger"
DONE = "done"
FAILED = "failed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RemoteEnrollManager:
    def __init__(
        self,
        *,
        logger: logging.Logger,
        config: Any,
        backend_api: Any,
        device: Any,
        identifier_for: Callable[[Any], str],
        sync_member: Callable[[str], Any],
    ) -> None:
        self.logger = logger
        self.config = config
        self.backend_api = backend_api
        self.device = device
        self.identifier_for = identifier_for
        self.sync_member = sync_member
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_job_id: str | None = None

    # ------------------------------------------------------------ handlers

    def handle_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        member_id = str(payload.get("member_id") or "").strip()
        if not member_id:
            raise ValueError("member_id es obligatorio.")
        try:
            finger_index = int(payload.get("finger_index", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("finger_index debe ser un numero de 0 a 9.") from exc
        if not 0 <= finger_index <= MAX_FINGER_INDEX:
            raise ValueError("finger_index debe ser un numero de 0 a 9.")

        with self._lock:
            if self._active_job_id is not None:
                raise ValueError("Ya hay un registro de huella en curso en este lector. Espera a que termine.")
            job_id = uuid.uuid4().hex
            job = {
                "job_id": job_id,
                "member_id": member_id,
                "finger_index": finger_index,
                "status": PREPARING,
                "message": "Preparando el lector…",
                "user_id": "",
                "created_user": False,
                "started_at": _now_iso(),
                "finished_at": "",
            }
            self._jobs[job_id] = job
            self._active_job_id = job_id
            self._forget_old_jobs()

        threading.Thread(
            target=self._run, args=(job_id,), daemon=True, name=f"zk-enroll-{job_id[:8]}"
        ).start()
        return {"success": True, **self._snapshot(job_id)}

    def handle_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("job_id") or "").strip()
        with self._lock:
            if job_id not in self._jobs:
                raise ValueError("No existe ese registro de huella (el edge pudo reiniciarse).")
        return {"success": True, **self._snapshot(job_id)}

    # ------------------------------------------------------------- worker

    def _run(self, job_id: str) -> None:
        job = self._jobs[job_id]
        try:
            self._enroll(job_id, job["member_id"], job["finger_index"])
        except ZkUnavailableError as exc:
            self.logger.warning("ZK remote enroll failed (device). job=%s error=%s", job_id, exc)
            self._update(job_id, status=FAILED, message=(
                "El lector no confirmo el registro de la huella (no respondio o se agoto el tiempo)."
            ))
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("ZK remote enroll failed. job=%s", job_id)
            self._update(job_id, status=FAILED, message=f"No se pudo registrar la huella: {exc}")
        finally:
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None
                job["finished_at"] = job["finished_at"] or _now_iso()

    def _enroll(self, job_id: str, member_id: str, finger_index: int) -> None:
        assignments = self.backend_api.fetch_user_roles(member_ids=[member_id])
        assignment = next((item for item in assignments if item.member_id == member_id), None)
        if assignment is None:
            self._update(job_id, status=FAILED, message="El miembro no tiene ID biometrico en el sistema.")
            return
        identifier = (self.identifier_for(assignment) or "").strip()
        if not identifier:
            self._update(job_id, status=FAILED, message="El miembro no tiene ID biometrico en el sistema.")
            return
        self._update(job_id, user_id=identifier)
        if identifier in self.config.zk_protected_user_ids:
            self._update(job_id, status=FAILED, message=f"El ID {identifier} esta protegido en este lector.")
            return

        existing = next(
            (user for user in self.device.get_users() if (user.user_id or "").strip() == identifier),
            None,
        )
        if existing is not None and existing.privilege & ~USER_DISABLED_FLAG:
            self._update(job_id, status=FAILED, message=f"El ID {identifier} es administrador del lector.")
            return
        if existing is None:
            # Se crea bloqueado; el sync de abajo lo deja en el grupo correcto.
            existing, created = self.device.create_user(
                identifier, assignment.member_name or identifier, self.config.zk_blocked_group_id
            )
            self._update(job_id, created_user=created)
            self.logger.info("ZK user created for enroll. user_id=%s uid=%s", identifier, existing.uid)

        self._update(
            job_id,
            status=WAITING_FINGER,
            message="Pide a la persona que ponga el mismo dedo 3 veces en el lector.",
        )
        enrolled = self.device.enroll_fingerprint(
            existing, finger_index, timeout=self.config.zk_enroll_timeout_seconds
        )
        if not enrolled:
            self._update(job_id, status=FAILED, message=(
                "El lector no guardo la huella. Puede ser un dedo ya registrado, que se acabo el "
                "tiempo o que el lector no acepta el registro remoto."
            ))
            return

        try:
            self.sync_member(member_id)
        except Exception as exc:  # noqa: BLE001
            # La huella si quedo; el sync programado ajusta el grupo despues.
            self.logger.warning("ZK sync after enroll failed. member=%s error=%s", member_id, exc)
        self.logger.info("ZK fingerprint enrolled. user_id=%s finger=%s", identifier, finger_index)
        self._update(job_id, status=DONE, message="Huella registrada en el lector.")

    # ------------------------------------------------------------ helpers

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.update(changes)
            if changes.get("status") in (DONE, FAILED):
                job["finished_at"] = _now_iso()

    def _snapshot(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._jobs[job_id])

    def _forget_old_jobs(self) -> None:
        finished = [
            job_id for job_id, job in self._jobs.items()
            if job["status"] in (DONE, FAILED) and job_id != self._active_job_id
        ]
        for job_id in finished[: max(0, len(finished) - KEEP_FINISHED_JOBS)]:
            self._jobs.pop(job_id, None)

