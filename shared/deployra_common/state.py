"""Trwały stan komponentu w pliku JSON (uprawnienia 600, zapis atomowy, blokada)."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any


class JsonState:
    def __init__(self, path: Path, defaults: dict[str, Any]) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = deepcopy(defaults)
        if path.exists():
            self._data.update(json.loads(path.read_text(encoding="utf-8")))

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return deepcopy(self._data[key])

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return deepcopy(self._data.get(key, default))

    def update(self, **changes: Any) -> None:
        with self._lock:
            self._data.update(deepcopy(changes))
            self.save()

    def mutate(self, key: str, fn: Callable[[Any], Any]) -> Any:
        """Atomowa modyfikacja: fn(kopia wartości) -> nowa wartość."""
        with self._lock:
            self._data[key] = fn(deepcopy(self._data.get(key)))
            self.save()
            return deepcopy(self._data[key])

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    @staticmethod
    def b64(value: str | None) -> bytes | None:
        return base64.b64decode(value) if value else None
