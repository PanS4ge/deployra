"""Aktualizacje komponentów: porównywanie wersji i rozmowa z aktualizatorem.

Komponent (panel, bramka, backend) sam się nie podmienia — nie ma do tego
uprawnień ani nie przeżyłby własnego restartu. Robi to mały kontener
`deployra-updater` (sidecar z docker.sock, ~1 MB RAM w spoczynku), z którym
komponent dzieli katalog `/run/deployra-update`:

    request   ← komponent: `version=1.2.0`             (prośba o aktualizację)
    status    → aktualizator: state/version/message/updated (key=value, po linii)
    agent     → aktualizator: tryb i znacznik życia, odświeżany co minutę
    log       → aktualizator: pełny przebieg ostatniej aktualizacji

Na hoście bez Dockera (backend jako usługa systemd) tę samą rolę pełni
`install-native.sh --update`, uruchamiany przez `systemd-run`.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

VERSION = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-([0-9A-Za-z][0-9A-Za-z.-]{0,30}))?$")
HEARTBEAT_MAX = 180  # s — starszy znacznik życia = aktualizator nie działa
STATES = ("idle", "queued", "running", "done", "error")


def parse_version(value: Any) -> tuple | None:
    """`1.2.3` / `v1.2.3-rc1` → krotka do porównań (wydanie > rc tej samej wersji)."""
    match = VERSION.match(str(value or "").strip())
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def normalize(value: Any) -> str:
    """Wersja bez prefiksu `v` (tak tagujemy obrazy)."""
    if parse_version(value) is None:
        raise ValueError(f"nieprawidłowy numer wersji: {value!r}")
    return str(value).strip().removeprefix("v")


def is_newer(candidate: Any, current: Any) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def same_version(a: Any, b: Any) -> bool:
    pa, pb = parse_version(a), parse_version(b)
    return pa is not None and pa == pb


def _read_kv(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def _write_kv(path: Path, values: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    lines = [f"{key}={str(value).replace(chr(10), ' ').replace(chr(13), ' ')}" for key, value in values.items()]
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class UpdateAgent:
    """Strona komponentu: czy jest aktualizator, w jakim stanie, i zlecenie aktualizacji.

    `launcher` (tryb natywny) zamiast czekać na sidecar uruchamia aktualizację od razu.
    """

    def __init__(self, directory: Path, launcher: Callable[[str], None] | None = None, native_ready: bool = False) -> None:
        self.dir = Path(directory)
        self.launcher = launcher
        self.native_ready = native_ready

    def _agent(self) -> dict[str, str] | None:
        info = _read_kv(self.dir / "agent")
        if not info:
            return None
        try:
            fresh = time.time() - float(info.get("updated", 0)) < HEARTBEAT_MAX
        except ValueError:
            fresh = False
        return info if fresh else None

    @property
    def mode(self) -> str | None:
        if self.launcher:
            return "native" if self.native_ready else None
        agent = self._agent()
        return agent.get("mode", "image") if agent else None

    def info(self, log_lines: int = 0) -> dict[str, Any]:
        status = _read_kv(self.dir / "status")
        state = status.get("state", "idle")
        try:
            updated = float(status.get("updated", 0)) or None
        except ValueError:
            updated = None
        out: dict[str, Any] = {
            "available": self.mode is not None,
            "mode": self.mode,
            "state": state if state in STATES else "idle",
            "target": status.get("version") or None,
            "message": status.get("message", ""),
            "updated": updated,
        }
        if log_lines:
            try:
                lines = (self.dir / "log").read_text(encoding="utf-8", errors="replace").splitlines()
                out["log"] = lines[-log_lines:]
            except OSError:
                out["log"] = []
        return out

    def request(self, version: str) -> dict[str, Any]:
        target = normalize(version)
        if self.mode is None:
            raise LookupError(
                "brak aktualizatora — uruchom komponent z pliku compose z katalogu deploy/ (usługa *-updater) "
                "albo zaktualizuj ręcznie"
            )
        current = self.info()
        if current["state"] in ("queued", "running") and current["updated"] and time.time() - current["updated"] < 1800:
            raise LookupError(f"aktualizacja do {current['target']} już trwa")
        self.dir.mkdir(parents=True, exist_ok=True)
        _write_kv(self.dir / "status", {"state": "queued", "version": target, "message": "czeka na aktualizator", "updated": int(time.time())})
        if self.launcher:
            try:
                self.launcher(target)
            except Exception as exc:
                _write_kv(self.dir / "status", {"state": "error", "version": target, "message": str(exc), "updated": int(time.time())})
                raise
        else:
            _write_kv(self.dir / "request", {"version": target, "requested": int(time.time())})
        return self.info()
