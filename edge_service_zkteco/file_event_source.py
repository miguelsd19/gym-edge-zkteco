from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


class FileEventSource:
    """
    Fuente de eventos para pruebas sin hardware.

    Hace tail de un archivo de texto: cada linea nueva es un evento. Acepta un
    `device_user_id` plano o un objeto JSON con las claves de un evento ZK
    ({"user_id": ..., "timestamp": ..., "status": ..., "punch": ...}).
    """

    def __init__(
        self,
        *,
        input_file: Path,
        logger,
        on_event: Callable[[dict[str, Any]], None],
        poll_interval_seconds: float = 0.1,
    ):
        self.input_file = input_file
        self.logger = logger
        self.on_event = on_event
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

        self.input_file.parent.mkdir(parents=True, exist_ok=True)
        if self.input_file.exists():
            self._offset = self.input_file.stat().st_size
        else:
            self.input_file.touch()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._tail_loop,
            daemon=True,
            name="zk-file-events",
        )
        self._thread.start()
        self.logger.info(
            "File event source started. Watching %s for identifiers.",
            self.input_file,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _tail_loop(self) -> None:
        while not self._stop.is_set():
            try:
                lines = self._read_new_lines()
            except OSError as exc:
                self.logger.warning("Could not read input file: %s", exc)
                lines = []

            for line in lines:
                event = self._parse_line(line)
                if event is None:
                    continue
                try:
                    self.on_event(event)
                except Exception:  # noqa: BLE001
                    self.logger.exception("File event handler failed.")

            self._stop.wait(self.poll_interval_seconds)

    def _read_new_lines(self) -> list[str]:
        if not self.input_file.exists():
            return []

        size = self.input_file.stat().st_size
        if size < self._offset:
            self._offset = 0

        if size == self._offset:
            return []

        with self.input_file.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()

        return [line.strip() for line in chunk.splitlines() if line.strip()]

    def _parse_line(self, line: str) -> dict[str, Any] | None:
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                self.logger.warning("Ignoring invalid JSON line in input file: %s", line)
                return None
            if not isinstance(parsed, dict):
                return None
            parsed.setdefault(
                "timestamp",
                datetime.now().isoformat(timespec="seconds"),
            )
            return parsed

        return {
            "user_id": line,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
