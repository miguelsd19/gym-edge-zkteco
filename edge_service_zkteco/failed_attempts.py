from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path

from edge_service_zkteco.models import FailedAttempt


class FailedAttemptStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, attempt: FailedAttempt) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(attempt), ensure_ascii=True) + "\n")
