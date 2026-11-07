"""
Inventario de solo lectura: cuantas huellas tiene cada usuario del lector.

Sirve para que Gym System sepa que miembros ya tienen huella en el lector (los
que se registraron antes del registro remoto) y lo muestre en "Registrar en el
lector". Nunca escribe en el equipo y no guarda las plantillas: solo el conteo.

Uso (con el servicio GymEdgeZkteco detenido; el lector acepta una conexion):
    python -m edge_service_zkteco.fingerprint_inventory --host 192.168.1.201 --out pruebas/huellas.json

El archivo tiene `read_at`, el conteo total del lector y una fila por usuario con
`user_id` (el ID biometrico del miembro) y `fingerprints`. Sin nombres ni
plantillas. Las caras no se pueden leer por usuario con este protocolo; solo va
el total del lector.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

EXIT_OK = 0
EXIT_UNREACHABLE = 1
EXIT_FAILED = 2


def _log(message: str) -> None:
    print(message, flush=True)


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


def build_inventory(users: list[Any], templates: list[Any]) -> list[dict[str, Any]]:
    """Una fila por usuario con user_id; las huellas se cuentan por uid interno."""
    fingers_by_uid: dict[int, int] = {}
    for template in templates:
        uid = int(getattr(template, "uid", 0) or 0)
        fingers_by_uid[uid] = fingers_by_uid.get(uid, 0) + 1
    rows = []
    for user in users:
        user_id = str(getattr(user, "user_id", "") or "").strip()
        if not user_id:
            continue
        uid = int(getattr(user, "uid", 0) or 0)
        rows.append({"user_id": user_id, "fingerprints": fingers_by_uid.get(uid, 0)})
    rows.sort(key=lambda row: row["user_id"])
    return rows


def run_inventory(args: argparse.Namespace, zk_factory: Callable[..., Any] | None = None) -> int:
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _log(f"Conectando a {args.host}:{args.port} ...")
    try:
        conn = _build_zk(args, zk_factory).connect()
    except Exception as exc:  # noqa: BLE001
        _log(f"[ERROR] No se pudo conectar al lector: {exc}")
        _log("Revisa que el servicio GymEdgeZkteco este detenido (el lector acepta una sola conexion).")
        return EXIT_UNREACHABLE

    try:
        read_at = datetime.now(timezone.utc).isoformat()
        sizes: dict[str, int] = {}
        try:
            conn.read_sizes()
            sizes = {
                attr: int(getattr(conn, attr, 0) or 0) for attr in ("users", "fingers", "faces")
            }
        except Exception as exc:  # noqa: BLE001
            _log(f"[AVISO] No se pudieron leer los conteos del lector: {exc}")
        users = list(conn.get_users() or [])
        templates = list(conn.get_templates() or [])
    except Exception as exc:  # noqa: BLE001
        _log(f"[ERROR] No se pudo leer el lector: {exc}. Vuelve a correrlo; si sigue, prueba --timeout 30.")
        return EXIT_FAILED
    finally:
        try:
            conn.disconnect()
        except Exception:  # noqa: BLE001
            pass

    rows = build_inventory(users, templates)
    payload = {
        "read_at": read_at,
        "host": args.host,
        "device_totals": sizes,
        "users": rows,
    }
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)

    with_fingers = sum(1 for row in rows if row["fingerprints"])
    _log(f"Usuarios: {len(rows)} | con huella: {with_fingers} | sin huella: {len(rows) - with_fingers}")
    if sizes.get("faces"):
        _log(f"Caras en el lector: {sizes['faces']} (no se pueden leer por usuario).")
    _log(f"Inventario guardado en {out_path}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m edge_service_zkteco.fingerprint_inventory",
        description="Cuenta las huellas de cada usuario del lector (solo lectura, sin plantillas).",
    )
    parser.add_argument("--host", required=True, help="IP del lector, p. ej. 192.168.1.201")
    parser.add_argument("--port", type=int, default=4370)
    parser.add_argument("--out", required=True, help="Archivo JSON de salida")
    parser.add_argument("--comm-key", type=int, default=0, help="Clave de comunicacion del lector; 0 si no tiene")
    parser.add_argument("--timeout", type=float, default=15.0, help="Timeout por comando, en segundos")
    parser.add_argument("--force-udp", action="store_true")
    parser.add_argument("--omit-ping", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_inventory(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
