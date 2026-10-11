"""
Respaldo de solo lectura de un terminal ZKTeco (SpeedFace) via pyzk.

Pensado para sacar los datos del lector antes de migrarlo desde otro sistema
(p. ej. ProGym) al edge de Gym System. Nunca escribe en el equipo.

Uso:
    python -m edge_service_zkteco.backup_terminal --host 192.168.1.201 --out backups/sucursal

Genera en la carpeta de salida:
    device_info.json   info del equipo y conteos (usuarios, huellas, caras, registros)
    users.json         usuarios completos (uid, user_id, nombre, grupo, tarjeta, ...)
    users.csv          lo mismo para abrir en Excel y cruzar con ProGym
    fingerprints.json  plantillas de huella en base64
    attendance.json    historial de marcas del equipo
    manifest.json      resumen de la corrida y avisos

Las caras del SpeedFace no se pueden descargar por este protocolo: se quedan
en el equipo, por eso el lector NO se debe borrar ni reiniciar de fabrica.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

EXIT_OK = 0
EXIT_UNREACHABLE = 1
EXIT_PARTIAL = 2

DEVICE_INFO_GETTERS = (
    ("firmware_version", "get_firmware_version"),
    ("serial_number", "get_serialnumber"),
    ("platform", "get_platform"),
    ("device_name", "get_device_name"),
    ("mac", "get_mac"),
    ("face_version", "get_face_version"),
    ("fingerprint_version", "get_fp_version"),
    ("device_time", "get_time"),
)

SIZE_ATTRIBUTES = (
    "users",
    "users_cap",
    "fingers",
    "fingers_cap",
    "faces",
    "faces_cap",
    "records",
    "rec_cap",
)

USER_CSV_FIELDS = (
    "uid",
    "user_id",
    "name",
    "card",
    "group_id",
    "privilege",
    "disabled",
    "is_admin",
    "fingerprints",
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _save_json(path: Path, payload: Any) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def serialize_user(user: Any) -> dict[str, Any]:
    privilege = int(getattr(user, "privilege", 0) or 0)
    return {
        "uid": int(getattr(user, "uid", 0) or 0),
        "user_id": str(getattr(user, "user_id", "") or "").strip(),
        "name": str(getattr(user, "name", "") or "").strip(),
        "privilege": privilege,
        # El bit 0 de privilege es "usuario deshabilitado" (asi bloquea ProGym a
        # los vencidos); los bits de rol son 2 = enrolador, 6 = manager, 14 = admin.
        "disabled": bool(privilege & 1),
        "is_admin": bool(privilege & 0b1110),
        "password": str(getattr(user, "password", "") or ""),
        "group_id": str(getattr(user, "group_id", "") or "").strip(),
        "card": int(getattr(user, "card", 0) or 0),
    }


def serialize_fingerprint(finger: Any) -> dict[str, Any]:
    template = getattr(finger, "template", b"") or b""
    if isinstance(template, str):
        template = template.encode("latin-1")
    return {
        "uid": int(getattr(finger, "uid", 0) or 0),
        "fid": int(getattr(finger, "fid", 0) or 0),
        "valid": int(getattr(finger, "valid", 0) or 0),
        "size": len(template),
        "template_b64": base64.b64encode(template).decode("ascii"),
    }


def serialize_attendance(record: Any) -> dict[str, Any]:
    return {
        "uid": int(getattr(record, "uid", 0) or 0),
        "user_id": str(getattr(record, "user_id", "") or "").strip(),
        "timestamp": _jsonable(getattr(record, "timestamp", None)),
        "status": _jsonable(getattr(record, "status", None)),
        "punch": _jsonable(getattr(record, "punch", None)),
    }


def write_users_csv(path: Path, users: list[dict[str, Any]], fingers_by_uid: dict[int, int]) -> None:
    # utf-8-sig para que Excel respete acentos y enies al abrirlo con doble clic.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=USER_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for user in sorted(users, key=lambda item: item["uid"]):
            writer.writerow({**user, "fingerprints": fingers_by_uid.get(user["uid"], 0)})


def _build_zk(args: argparse.Namespace, zk_factory: Callable[..., Any] | None) -> Any:
    factory = zk_factory
    if factory is None:
        from zk import ZK

        factory = ZK
    return factory(
        args.host,
        port=args.port,
        timeout=args.timeout,
        password=args.comm_key,
        force_udp=args.force_udp,
        ommit_ping=args.omit_ping,
    )


def _log(message: str) -> None:
    print(message, flush=True)


def run_backup(args: argparse.Namespace, zk_factory: Callable[..., Any] | None = None) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    failures: dict[str, str] = {}
    warnings: list[str] = []
    counts: dict[str, int] = {}

    _log(f"Conectando a {args.host}:{args.port} ...")
    try:
        conn = _build_zk(args, zk_factory).connect()
    except Exception as exc:  # noqa: BLE001
        _log(f"[ERROR] No se pudo conectar al lector: {exc}")
        _log(
            "Revisa la IP y el puerto, que la PC este en la misma red, que la clave de "
            "comunicacion (--comm-key) sea la correcta, y que ProGym u otro programa NO "
            "este conectado al lector en este momento (el equipo solo acepta una conexion)."
        )
        return EXIT_UNREACHABLE
    _log("Conectado.")

    disabled = False
    try:
        if args.disable_device:
            conn.disable_device()
            disabled = True
            _log("Lector en pausa durante el respaldo (se reactiva al terminar).")

        device_info: dict[str, Any] = {"host": args.host, "port": args.port}
        for key, getter in DEVICE_INFO_GETTERS:
            try:
                device_info[key] = _jsonable(getattr(conn, getter)())
            except Exception as exc:  # noqa: BLE001
                device_info[key] = None
                warnings.append(f"{getter}: {exc}")
        try:
            conn.read_sizes()
            device_info["sizes"] = {
                attr: int(getattr(conn, attr, 0) or 0) for attr in SIZE_ATTRIBUTES
            }
        except Exception as exc:  # noqa: BLE001
            device_info["sizes"] = {}
            warnings.append(f"read_sizes: {exc}")
        _save_json(out_dir / "device_info.json", device_info)
        _log(
            "Equipo: {name} | serie {serial} | firmware {fw}".format(
                name=device_info.get("device_name") or "-",
                serial=device_info.get("serial_number") or "-",
                fw=device_info.get("firmware_version") or "-",
            )
        )

        users: list[dict[str, Any]] = []
        try:
            users = [serialize_user(user) for user in conn.get_users() or []]
            _save_json(out_dir / "users.json", users)
            counts["users"] = len(users)
            _log(f"Usuarios: {len(users)}")
        except Exception as exc:  # noqa: BLE001
            failures["users"] = str(exc)
            _log(f"[ERROR] Usuarios: {exc}")

        fingers_by_uid: dict[int, int] = {}
        if args.skip_fingerprints:
            _log("Huellas: omitidas (--skip-fingerprints)")
        else:
            try:
                fingerprints = [serialize_fingerprint(f) for f in conn.get_templates() or []]
                _save_json(out_dir / "fingerprints.json", fingerprints)
                for finger in fingerprints:
                    fingers_by_uid[finger["uid"]] = fingers_by_uid.get(finger["uid"], 0) + 1
                counts["fingerprints"] = len(fingerprints)
                _log(f"Huellas: {len(fingerprints)}")
            except Exception as exc:  # noqa: BLE001
                failures["fingerprints"] = str(exc)
                _log(f"[ERROR] Huellas: {exc}")

        if users:
            write_users_csv(out_dir / "users.csv", users, fingers_by_uid)

        if args.skip_attendance:
            _log("Historial de marcas: omitido (--skip-attendance)")
        else:
            try:
                restore_decode_time = _tolerate_bad_record_dates(conn)
                try:
                    attendance = [serialize_attendance(r) for r in conn.get_attendance() or []]
                finally:
                    restore_decode_time()
                _save_json(out_dir / "attendance.json", attendance)
                counts["attendance"] = len(attendance)
                _log(f"Historial de marcas: {len(attendance)}")
                bad_dates = sum(1 for record in attendance if record["timestamp"] is None)
                if bad_dates:
                    warnings.append(
                        f"{bad_dates} marcas del historial tienen fecha corrupta en el equipo; "
                        "se guardaron con timestamp vacio."
                    )
            except Exception as exc:  # noqa: BLE001
                failures["attendance"] = str(exc)
                _log(f"[ERROR] Historial de marcas: {exc}")

        faces_on_device = int(device_info.get("sizes", {}).get("faces", 0) or 0)
        if faces_on_device:
            warnings.append(
                f"El equipo tiene {faces_on_device} caras registradas que no se pueden "
                "descargar: se quedan en el lector. No lo borres ni lo reinicies de fabrica."
            )
        duplicated = _duplicated_user_ids(users)
        if duplicated:
            warnings.append(
                "user_id repetidos en el equipo (revisar antes de ligarlos a miembros): "
                + ", ".join(duplicated)
            )
        without_user_id = [str(u["uid"]) for u in users if not u["user_id"]]
        if without_user_id:
            warnings.append(
                "Usuarios sin user_id (uid): " + ", ".join(without_user_id)
            )
    finally:
        if disabled:
            try:
                conn.enable_device()
                _log("Lector reactivado.")
            except Exception as exc:  # noqa: BLE001
                _log(f"[ERROR] No se pudo reactivar el lector: {exc}. Reinicialo a mano.")
        try:
            conn.disconnect()
        except Exception:  # noqa: BLE001
            pass

    _save_json(
        out_dir / "manifest.json",
        {
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "host": args.host,
            "counts": counts,
            "failures": failures,
            "warnings": warnings,
        },
    )

    for warning in warnings:
        _log(f"[AVISO] {warning}")
    if failures:
        _log(
            f"Respaldo INCOMPLETO en {out_dir} (fallo: {', '.join(failures)}). "
            "Vuelve a correr el mismo comando; si sigue fallando prueba --timeout 30."
        )
        return EXIT_PARTIAL
    _log(f"Respaldo completo en {out_dir}")
    return EXIT_OK


def _tolerate_bad_record_dates(conn: Any) -> Callable[[], None]:
    # pyzk decodifica cada marca con ZK.__decode_time y un solo registro con
    # fecha imposible (p. ej. dia 31 en un mes de 30) aborta get_attendance()
    # completo. Durante el respaldo esas fechas se guardan como None.
    original = getattr(conn, "_ZK__decode_time", None)
    if original is None:
        return lambda: None

    def tolerant(raw: bytes) -> Any:
        try:
            return original(raw)
        except ValueError:
            return None

    conn._ZK__decode_time = tolerant
    return lambda: delattr(conn, "_ZK__decode_time")


def _duplicated_user_ids(users: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    duplicated: list[str] = []
    for user in users:
        user_id = user["user_id"]
        if not user_id:
            continue
        if user_id in seen and user_id not in duplicated:
            duplicated.append(user_id)
        seen.add(user_id)
    return duplicated


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m edge_service_zkteco.backup_terminal",
        description="Respaldo de solo lectura de un lector ZKTeco (usuarios, huellas, marcas).",
    )
    parser.add_argument("--host", required=True, help="IP del lector, p. ej. 192.168.1.201")
    parser.add_argument("--port", type=int, default=4370)
    parser.add_argument("--out", required=True, help="Carpeta donde se guarda el respaldo")
    parser.add_argument(
        "--comm-key",
        type=int,
        default=0,
        help="Clave de comunicacion del lector (Menu > Comunicacion); 0 si no tiene",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="Timeout por comando, en segundos")
    parser.add_argument("--force-udp", action="store_true", help="Usar UDP en vez de TCP")
    parser.add_argument("--omit-ping", action="store_true", help="No hacer ping antes de conectar")
    parser.add_argument(
        "--disable-device",
        action="store_true",
        help="Pausar el lector durante el respaldo (nadie puede marcar mientras tanto)",
    )
    parser.add_argument("--skip-fingerprints", action="store_true")
    parser.add_argument("--skip-attendance", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_backup(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
