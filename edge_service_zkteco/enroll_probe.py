"""Prueba si el lector acepta el enrolamiento remoto de huella.

Crea un usuario de PRUEBA (por defecto ID 990001) en el grupo bloqueado, pone el
lector en modo "ponga su dedo", revisa si la huella quedo guardada y al final
borra ese usuario de prueba. Nunca toca a otro usuario: si el ID de prueba ya
existe en el lector, se detiene sin hacer nada.

Antes de correrlo hay que DETENER el servicio del edge (el lector acepta una
sola conexion) y tener a alguien junto al lector para poner el dedo.

    python -m edge_service_zkteco.enroll_probe --host 192.168.1.201

Codigos de salida: 0 la huella quedo guardada (el firmware lo soporta),
1 no se pudo conectar, 2 el lector no guardo la huella, 3 el ID de prueba ya
existe, 4 error inesperado.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Callable

from edge_service_zkteco.zk_client import create_device_user, enroll_device_fingerprint

EXIT_OK = 0
EXIT_UNREACHABLE = 1
EXIT_NOT_ENROLLED = 2
EXIT_USER_EXISTS = 3
EXIT_ERROR = 4

TEST_USER_NAME = "PRUEBA HUELLA"


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
        verbose=args.verbose,
    )


def _device_label(conn: Any) -> str:
    parts = []
    for getter in ("get_device_name", "get_firmware_version", "get_fp_version"):
        try:
            parts.append(f"{getter.removeprefix('get_')}={getattr(conn, getter)()}")
        except Exception as exc:  # noqa: BLE001
            parts.append(f"{getter.removeprefix('get_')}=? ({exc})")
    return ", ".join(parts)


def run_probe(args: argparse.Namespace, zk_factory: Callable[..., Any] | None = None) -> int:
    user_id = str(args.user_id).strip()
    _log(f"Conectando a {args.host}:{args.port} ...")
    try:
        conn = _build_zk(args, zk_factory).connect()
    except Exception as exc:  # noqa: BLE001
        _log(f"[ERROR] No se pudo conectar al lector: {exc}")
        _log("Detén el servicio del edge (y ProGym) antes de correr la prueba: el lector acepta una sola conexion.")
        return EXIT_UNREACHABLE

    created_user = None
    result = EXIT_ERROR
    try:
        _log(f"Conectado. {_device_label(conn)}")
        users = conn.get_users()
        _log(f"Usuarios en el lector: {len(users)} (tamano de registro {conn.user_packet_size} bytes).")
        if any(str(item.user_id).strip() == user_id for item in users):
            _log(f"[ALTO] El ID {user_id} ya existe en el lector. No se toca. Usa otro con --user-id.")
            return EXIT_USER_EXISTS

        created_user, _created = create_device_user(conn, user_id, TEST_USER_NAME, args.blocked_group)
        _log(
            f"Usuario de prueba creado: ID {user_id}, uid {created_user.uid}, "
            f"grupo {args.blocked_group} (bloqueado, no abre el torniquete)."
        )
        _log("")
        _log(">>> Ve al lector AHORA: debe pedir la huella. Pon el MISMO dedo 3 veces.")
        _log(">>> Si el lector no cambia de pantalla en ~1 minuto, el firmware no acepta el registro remoto.")
        try:
            enrolled = enroll_device_fingerprint(conn, created_user, args.finger)
        except Exception as exc:  # noqa: BLE001
            _log(f"[RESULTADO] El lector no completo el registro: {type(exc).__name__}: {exc}")
            result = EXIT_NOT_ENROLLED
        else:
            if enrolled:
                _log("[RESULTADO] OK: la huella quedo guardada. El firmware SI acepta el registro remoto.")
                result = EXIT_OK
            else:
                _log("[RESULTADO] El lector no guardo la huella (rechazo, dedo duplicado o tiempo agotado).")
                result = EXIT_NOT_ENROLLED
    except Exception as exc:  # noqa: BLE001
        _log(f"[ERROR] {type(exc).__name__}: {exc}")
        result = EXIT_ERROR
    finally:
        if created_user is not None and not args.keep:
            try:
                conn.delete_user(uid=created_user.uid)
                conn.refresh_data()
                _log(f"Usuario de prueba {user_id} borrado del lector.")
            except Exception as exc:  # noqa: BLE001
                _log(
                    f"[AVISO] No se pudo borrar el usuario de prueba {user_id} (uid {created_user.uid}): "
                    f"{exc}. Borralo a mano en el lector: Menu > Usuarios > {user_id}."
                )
        elif created_user is not None:
            _log(f"Usuario de prueba {user_id} conservado (--keep).")
        try:
            conn.disconnect()
        except Exception:  # noqa: BLE001
            pass
    if result == EXIT_NOT_ENROLLED and not args.verbose:
        _log("Para el diagnostico, repite con --verbose y comparte la salida.")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m edge_service_zkteco.enroll_probe",
        description="Prueba el registro remoto de huella con un usuario de prueba que despues se borra.",
    )
    parser.add_argument("--host", required=True, help="IP del lector, p. ej. 192.168.1.201")
    parser.add_argument("--port", type=int, default=4370)
    parser.add_argument("--comm-key", type=int, default=0, help="Clave de comunicacion del lector; 0 si no tiene")
    parser.add_argument("--timeout", type=float, default=15.0, help="Timeout por comando, en segundos")
    parser.add_argument("--force-udp", action="store_true")
    parser.add_argument("--omit-ping", action="store_true")
    parser.add_argument("--user-id", default="990001", help="ID del usuario de prueba (no debe existir)")
    parser.add_argument("--finger", type=int, default=6, choices=range(10), help="Dedo 0-9 (6 = indice derecho)")
    parser.add_argument("--blocked-group", default="99", help="Grupo bloqueado del lector (ZK_BLOCKED_GROUP_ID)")
    parser.add_argument("--keep", action="store_true", help="No borrar el usuario de prueba al terminar")
    parser.add_argument("--verbose", action="store_true", help="Mostrar los paquetes del protocolo (diagnostico)")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_probe(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
