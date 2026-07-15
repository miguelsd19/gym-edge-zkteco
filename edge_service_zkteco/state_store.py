from __future__ import annotations

import json
import threading
from pathlib import Path


class ProcessedEventStore:
    def __init__(self, path: Path, max_keys: int = 500):
        self.path = path
        self.max_keys = max_keys
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._keys = self._load()

    def contains(self, event_key: str) -> bool:
        with self._lock:
            return event_key in self._keys

    def remember(self, event_key: str) -> None:
        with self._lock:
            if event_key in self._keys:
                return

            self._keys.append(event_key)
            if len(self._keys) > self.max_keys:
                self._keys = self._keys[-self.max_keys :]
            self._persist()

    def _load(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [str(value) for value in data][-self.max_keys :]

    def _persist(self) -> None:
        self.path.write_text(
            json.dumps(self._keys, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
