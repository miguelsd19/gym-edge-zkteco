"""Alta en el lector y registro remoto de huella pedidos por el backend.

Mismo contrato que el edge Hikvision (gym-System, edge_service_hikvision):
el backend inicia un trabajo con `{member_id, biometrics, finger_index}`.

- biometrics = [] solo da de alta al usuario (al inscribir; en planes de
  pareja, a cada persona una tras otra).
- biometrics = ["fingerprint"], ["face"] o ambos: primero la huella y luego la
  cara. La cara usa StartEnrollEx con el indice 111 (sin soporte en pyzk) y se
  confirma con el conteo de caras del lector.

El edge pregunta al backend el ID biometrico del miembro, crea al usuario si
no existe (grupo bloqueado), registra lo pedido y al final sincroniza al
miembro para dejarlo en el grupo que le toca. Corre en un hilo y el backend
consulta el estado. Un registro a la vez. Los errores llevan un codigo RL-xxx
(docs/CODIGOS_ERROR_LECTOR.md en gym-System); los de la peticion misma (400)
tambien lo mandan en `error_code` para que el backend no tenga que adivinarlo.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from edge_service_zkteco.backend_api import BackendUnavailableError
from edge_service_zkteco.models import USER_DISABLED_FLAG
from edge_service_zkteco.zk_client import FACE_NO_CAPTURE, FACE_SAVED, ZkUnavailableError
from edge_service_zkteco.zk_enroll import FingerprintAlreadyExistsError

MAX_FINGER_INDEX = 9
KEEP_FINISHED_JOBS = 20
KNOWN_BIOMETRICS = ("fingerprint", "face")

# Estados que ve el backend.
PREPARING = "preparing"
CREATING_USER = "creating_user"
WAITING_FINGER = "waiting_finger"
WAITING_FACE = "waiting_face"
SYNCING = "syncing"
DONE = "done"
FAILED = "failed"


class EnrollRequestError(ValueError):
    """Peticion invalida o lector ocupado: el control server responde 400 con el codigo."""

    def __init__(self, code: str, message: str, *, active_job_id: str = ""):
        super().__init__(message)
        self.code = code
        self.active_job_id = active_job_id


class EnrollFailure(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


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
            raise EnrollRequestError("RL-004", "member_id es obligatorio.")
        raw = payload.get("biometrics", ["fingerprint"])
        if not isinstance(raw, list) or any(item not in KNOWN_BIOMETRICS for item in raw):
            raise EnrollRequestError("RL-004", "biometrics debe ser una lista con fingerprint y/o face.")
        biometrics = [item for item in KNOWN_BIOMETRICS if item in raw]
        try:
            finger_index = int(payload.get("finger_index", 6))
        except (TypeError, ValueError) as exc:
            raise EnrollRequestError("RL-004", "finger_index debe ser un numero de 0 a 9.") from exc
        if not 0 <= finger_index <= MAX_FINGER_INDEX:
            raise EnrollRequestError("RL-004", "finger_index debe ser un numero de 0 a 9.")

        with self._lock:
            if self._active_job_id is not None:
                # Con el id el backend puede retomar el registro si es del mismo
                # miembro (p. ej. el inicio anterior se agoto en la red).
                raise EnrollRequestError(
                    "RL-012", "Ya hay un registro en curso en este lector. Espera a que termine.",
                    active_job_id=self._active_job_id,
                )
            job_id = uuid.uuid4().hex
            self._jobs[job_id] = {
                "job_id": job_id,
                "member_id": member_id,
                "biometrics": biometrics,
                "finger_index": finger_index,
                "status": PREPARING,
                "message": "Preparando el lector…",
                "user_id": "",
                "created_user": False,
                "results": {"user": "pending", **{item: "pending" for item in biometrics}},
                "error_code": "",
                "error_detail": "",
                "warning_code": "",
                "warning_detail": "",
                "started_at": _now_iso(),
                "finished_at": "",
            }
            self._active_job_id = job_id
            self._forget_old_jobs()

        try:
            threading.Thread(
                target=self._run, args=(job_id,), daemon=True, name=f"zk-enroll-{job_id[:8]}"
            ).start()
        except Exception:
            # Sin hilo nadie liberaria el lector: quedaria "en curso" para siempre.
            with self._lock:
                self._active_job_id = None
                self._jobs.pop(job_id, None)
            raise
        return {"success": True, **self._snapshot(job_id)}

    def handle_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("job_id") or "").strip()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise EnrollRequestError("RL-014", "No existe ese registro (el edge pudo reiniciarse).")
            snapshot = dict(job)
            snapshot["results"] = dict(job["results"])
        return {"success": True, **snapshot}

    # ------------------------------------------------------------- worker

    def _run(self, job_id: str) -> None:
        job = self._jobs[job_id]
        failure: EnrollFailure | None = None
        try:
            self._enroll(job_id, job)
        except EnrollFailure as exc:
            failure = exc
        except ZkUnavailableError as exc:
            failure = EnrollFailure("RL-101", "El lector no responde.", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("ZK remote enroll crashed. job=%s", job_id)
            failure = EnrollFailure("RL-901", "Error inesperado del servicio del lector.",
                                    f"{type(exc).__name__}: {exc}")
        try:
            # Con el usuario en el lector (aunque falle la huella) se aplica su
            # acceso segun la membresia: lo nuevo nace bloqueado.
            if job["results"].get("user") in ("created", "existing"):
                self._sync(job_id, job, show_status=failure is None)
            if failure is None:
                self._update(job_id, status=DONE, message=self._done_message(job))
            else:
                self.logger.warning("ZK remote enroll failed. job=%s code=%s %s (%s)",
                                    job_id, failure.code, failure.message, failure.detail)
                self._update(job_id, status=FAILED, message=failure.message,
                             error_code=failure.code, error_detail=failure.detail)
        except Exception as exc:  # noqa: BLE001
            # El trabajo nunca debe quedar a medias: el backend consultaria sin fin.
            self.logger.exception("ZK remote enroll could not finish. job=%s", job_id)
            self._update(job_id, status=FAILED, message="Error inesperado del servicio del lector.",
                         error_code="RL-901", error_detail=f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None
                job["finished_at"] = job["finished_at"] or _now_iso()

    def _enroll(self, job_id: str, job: dict[str, Any]) -> None:
        member_id = job["member_id"]
        try:
            assignments = self.backend_api.fetch_user_roles(member_ids=[member_id])
        except BackendUnavailableError as exc:
            raise EnrollFailure("RL-104", "El servicio del lector no pudo consultar al sistema.", str(exc)) from exc
        assignment = next((item for item in assignments if item.member_id == member_id), None)
        identifier = (self.identifier_for(assignment) or "").strip() if assignment else ""
        if not identifier:
            raise EnrollFailure("RL-003", "El miembro no tiene ID biometrico en el sistema.")
        self._update(job_id, user_id=identifier, status=CREATING_USER, message="Dando de alta en el lector…")
        if identifier in self.config.zk_protected_user_ids:
            raise EnrollFailure("RL-201", f"El ID {identifier} esta protegido en este lector.")

        existing = next(
            (user for user in self.device.get_users() if (user.user_id or "").strip() == identifier),
            None,
        )
        if existing is not None and existing.privilege & ~USER_DISABLED_FLAG:
            raise EnrollFailure("RL-201", f"El ID {identifier} es administrador del lector.")
        if existing is None:
            try:
                existing, created = self.device.create_user(
                    identifier, assignment.member_name or identifier, self.config.zk_blocked_group_id
                )
            except ZkUnavailableError as exc:
                raise EnrollFailure("RL-202", "El lector no acepto dar de alta al usuario.", str(exc)) from exc
            self._update(job_id, created_user=created)
            self._set_result(job_id, "user", "created" if created else "existing")
            self.logger.info("ZK user created for enroll. user_id=%s uid=%s", identifier, existing.uid)
        else:
            self._set_result(job_id, "user", "existing")

        # Cada biometrico se intenta aunque falle el anterior (la persona ya esta
        # frente al lector). Si alguno falla, el trabajo termina en failed con el
        # codigo del primero que fallo; lo guardado queda en results.
        failures: list[EnrollFailure] = []
        steps = (("fingerprint", self._enroll_fingerprint), ("face", self._enroll_face))
        for key, step in steps:
            if key not in job["biometrics"]:
                continue
            try:
                step(job_id, job, existing, identifier)
            except EnrollFailure as failure:
                self._set_result(job_id, key, "failed")
                failures.append(failure)
        if failures:
            saved = [label for key, label in (("fingerprint", "huella"), ("face", "cara"))
                     if job["results"].get(key) == "saved"]
            first = failures[0]
            message = " ".join(failure.message for failure in failures)
            if saved:
                message += f" La {' y la '.join(saved)} si quedo guardada."
            raise EnrollFailure(
                first.code, message,
                " | ".join(f"{failure.code}: {failure.detail or failure.message}" for failure in failures)
                if len(failures) > 1 else first.detail,
            )

    def _enroll_fingerprint(self, job_id: str, job: dict[str, Any], existing: Any, identifier: str) -> None:
        self._update(job_id, status=WAITING_FINGER,
                     message="Pide a la persona que ponga el mismo dedo 3 veces en el lector.")
        try:
            enrolled = self.device.enroll_fingerprint(
                existing, job["finger_index"], timeout=self.config.zk_enroll_timeout_seconds
            )
        except FingerprintAlreadyExistsError as exc:
            raise EnrollFailure("RL-302", str(exc), "La plantilla de ese dedo ya existe en el lector.") from exc
        except ZkUnavailableError as exc:
            raise EnrollFailure(
                "RL-301", "No se capturo la huella: nadie puso el dedo a tiempo o el lector no respondio.",
                str(exc),
            ) from exc
        if not enrolled:
            raise EnrollFailure(
                "RL-302", "El lector no guardo la huella (dedo ya registrado, tiempo agotado o "
                "el lector no acepta el registro remoto).",
            )
        self._set_result(job_id, "fingerprint", "saved")
        self.logger.info("ZK fingerprint enrolled. user_id=%s finger=%s", identifier, job["finger_index"])

    def _enroll_face(self, job_id: str, job: dict[str, Any], existing: Any, identifier: str) -> None:
        self._update(job_id, status=WAITING_FACE,
                     message="Pide a la persona que se ponga frente al lector para registrar su cara.")
        try:
            result = self.device.enroll_face(existing, timeout=self.config.zk_enroll_timeout_seconds)
        except ZkUnavailableError as exc:
            raise EnrollFailure(
                "RL-401", "No se capturo la cara: el lector no respondio durante el registro.", str(exc),
            ) from exc
        if result == FACE_NO_CAPTURE:
            raise EnrollFailure("RL-401", "No se capturo la cara: nadie se puso frente al lector a tiempo.")
        if result != FACE_SAVED:
            raise EnrollFailure(
                "RL-402", "El lector no confirmo que guardo la cara (mala captura, o la persona ya "
                "tenia una cara registrada y se reemplazo).",
            )
        self._set_result(job_id, "face", "saved")
        self.logger.info("ZK face enrolled. user_id=%s", identifier)

    def _sync(self, job_id: str, job: dict[str, Any], show_status: bool) -> None:
        if show_status:
            self._update(job_id, status=SYNCING, message="Aplicando el acceso segun la membresia…")
        try:
            counters = self.sync_member(job["member_id"])
        except Exception as exc:  # noqa: BLE001
            # Lo registrado si quedo; el sync programado ajusta el grupo despues.
            self.logger.warning("ZK sync after enroll failed. member=%s error=%s", job["member_id"], exc)
            self._update(job_id, warning_code="RL-501", warning_detail=str(exc))
            return
        # Sin excepcion tambien puede no haberse aplicado (sync apagado, o el
        # usuario recien creado no aparecio): quedaria bloqueado sin aviso.
        if isinstance(counters, dict) and counters.get("updated", 0) + counters.get("unchanged", 0) == 0:
            self.logger.warning("ZK sync after enroll did not apply. member=%s counters=%s",
                                job["member_id"], counters)
            self._update(job_id, warning_code="RL-501", warning_detail=f"No se aplico el acceso: {counters}")

    def _done_message(self, job: dict[str, Any]) -> str:
        user = "Alta en el lector lista" if job["created_user"] else "Usuario ya estaba en el lector"
        saved = [label for key, label in (("fingerprint", "huella"), ("face", "cara"))
                 if job["results"].get(key) == "saved"]
        if saved:
            return f"{user}: {' y '.join(saved)} guardada{'s' if len(saved) > 1 else ''}."
        return f"{user}."

    # ------------------------------------------------------------ helpers

    def _set_result(self, job_id: str, key: str, value: str) -> None:
        with self._lock:
            self._jobs[job_id]["results"][key] = value

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.update(changes)
            if changes.get("status") in (DONE, FAILED):
                job["finished_at"] = _now_iso()

    def _snapshot(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = dict(self._jobs[job_id])
            job["results"] = dict(job["results"])
            return job

    def _forget_old_jobs(self) -> None:
        finished = [
            job_id for job_id, job in self._jobs.items()
            if job["status"] in (DONE, FAILED) and job_id != self._active_job_id
        ]
        for job_id in finished[: max(0, len(finished) - KEEP_FINISHED_JOBS)]:
            self._jobs.pop(job_id, None)
