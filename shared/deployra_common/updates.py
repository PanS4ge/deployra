"""Aktualizacje komponentów: porównywanie wersji i rozmowa z aktualizatorem.

Komponent (panel, bramka, backend) sam się nie podmienia — nie ma do tego
uprawnień ani nie przeżyłby własnego restartu. Robi to mały kontener
`deployra-updater` (sidecar z docker.sock, ~1 MB RAM w spoczynku), z którym
komponent dzieli katalog `/run/deployra-update`:

    request   ← komponent: `version=1.2.0`             (prośba o aktualizację)
    status    → aktualizator: state/version/step/message/updated (key=value, po linii;
                step to kod kroku tłumaczony w panelu, message — szczegóły)
    agent     → aktualizator: tryb i znacznik życia, odświeżany co minutę
    log       → aktualizator: pełny przebieg ostatniej aktualizacji

Backend sidecara nie potrzebuje: ma docker.sock, więc na czas aktualizacji
uruchamia krótko żyjącą kopię siebie, która podmienia jego kontener i znika
(deployra_backend.services.self_update) — ten sam protokół plików, tylko w
katalogu danych backendu. Na hoście bez Dockera (backend jako usługa systemd)
tę rolę pełni `install-native.sh --update`, uruchamiany przez `systemd-run`.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .i18n import tr

MOUNTINFO = re.compile(r"/containers/([0-9a-f]{64})/")
_own: list[str | None] = []


def own_container(proc: Path = Path("/proc")) -> str | None:
    """ID kontenera, w którym działa ten proces (DEPLOYRA_CONTAINER albo /proc/self/mountinfo) — po nim węzeł
    na tym samym hoście rozpoznaje panel i bramkę, które może zaktualizować."""
    if not _own:
        found = os.environ.get("DEPLOYRA_CONTAINER") or None
        if not found:
            try:
                match = MOUNTINFO.search((proc / "self" / "mountinfo").read_text(encoding="utf-8", errors="replace"))
                found = match.group(1) if match else None
            except OSError:
                found = None
        _own.append(found)
    return _own[0]


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
        raise ValueError(tr("srv.upd.bad_version", value=repr(value)))
    return str(value).strip().removeprefix("v")


def is_newer(candidate: Any, current: Any) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def same_version(a: Any, b: Any) -> bool:
    pa, pb = parse_version(a), parse_version(b)
    return pa is not None and pa == pb


def read_kv(path: Path) -> dict[str, str]:
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


def write_kv(path: Path, values: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    lines = [f"{key}={str(value).replace(chr(10), ' ').replace(chr(13), ' ')}" for key, value in values.items()]
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_status(directory: Path, state: str, version: str, step: str, message: str = "") -> None:
    write_kv(Path(directory) / "status", {"state": state, "version": version, "step": step, "message": message, "updated": int(time.time())})


def append_log(directory: Path, line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(Path(directory) / "log", "a", encoding="utf-8") as out:
        out.write(f"{stamp} {line}\n")


class UpdateAgent:
    """Strona komponentu: czy jest aktualizator, w jakim stanie, i zlecenie aktualizacji.

    `launcher` (systemd albo backend sam dla siebie) zamiast czekać na sidecar uruchamia aktualizację
    od razu; `launch_mode` mówi, czy i w jakim trybie da się to teraz zrobić.
    """

    def __init__(self, directory: Path, launcher: Callable[[str], None] | None = None, native_ready: bool = False,
                 launch_mode: Callable[[], str | None] | None = None) -> None:  # fmt: skip
        self.dir = Path(directory)
        self.launcher = launcher
        self.native_ready = native_ready
        self.launch_mode = launch_mode

    def _agent(self) -> dict[str, str] | None:
        info = read_kv(self.dir / "agent")
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
            if self.launch_mode:
                return self.launch_mode()
            return "native" if self.native_ready else None
        agent = self._agent()
        return agent.get("mode", "image") if agent else None

    def info(self, log_lines: int = 0) -> dict[str, Any]:
        status = read_kv(self.dir / "status")
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
            "step": status.get("step") or None,
            "message": status.get("message", ""),
            "updated": updated,
            "container": own_container(),
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
            raise LookupError(tr("srv.upd.no_updater"))
        current = self.info()
        if current["state"] in ("queued", "running") and current["updated"] and time.time() - current["updated"] < 1800:
            raise LookupError(tr("srv.upd.running", version=current["target"]))
        self.dir.mkdir(parents=True, exist_ok=True)
        write_status(self.dir, "queued", target, "queued")
        if self.launcher:
            try:
                self.launcher(target)
            except Exception as exc:
                write_status(self.dir, "error", target, "launch_failed", str(exc))
                raise
        else:
            write_kv(self.dir / "request", {"version": target, "requested": int(time.time())})
        return self.info()
