"""Aktualizacje backendu: sam backend (Docker), `install-native.sh --update` (systemd) albo dawny sidecar."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deployra_common.updates import UpdateAgent

from ..config import Config
from .self_update import SelfUpdater


class BackendUpdates:
    """Backend w Dockerze aktualizuje się sam; sidecar `deployra-updater` zostaje dla starszych instalacji."""

    def __init__(self, updater: SelfUpdater, sidecar: UpdateAgent) -> None:
        self.updater = updater
        self.own = UpdateAgent(updater.dir, launcher=updater.launch, launch_mode=updater.mode)
        self.sidecar = sidecar

    def _pick(self) -> UpdateAgent:
        return self.sidecar if self.own.mode is None and self.sidecar.mode is not None else self.own

    def info(self, log_lines: int = 0) -> dict[str, Any]:
        out = self._pick().info(log_lines)
        if not out["available"]:
            out["reason"] = self.updater.reason or "no_updater"  # panel pokaże, czego brakuje (socket, wolumen, obraz…)
        return out

    def request(self, version: str) -> dict[str, Any]:
        return self._pick().request(version)

    def peers(self) -> list[dict[str, Any]]:
        return self.updater.peers()

    def peer_log(self, name: str, lines: int = 30) -> list[str]:
        return self.updater.peer_log(name, lines)

    def request_peer(self, container: str, version: str) -> dict[str, Any]:
        return self.updater.launch_peer(container, version)


def update_agent(config: Config, docker: Callable[[], Any] | None = None) -> UpdateAgent | BackendUpdates:
    if not config.native_repo:
        sidecar = UpdateAgent(config.update_dir)
        return BackendUpdates(SelfUpdater(config, docker), sidecar) if docker else sidecar
    script = Path(config.native_repo) / "deploy" / "install-native.sh"

    def launch(version: str) -> None:
        # systemd-run: aktualizacja żyje w osobnej jednostce, więc przeżyje restart deployra-backend
        cmd = ["systemd-run", f"--unit=deployra-update-{int(time.time())}", "--collect", "--quiet",
               "/bin/bash", str(script), "--update", version]  # fmt: skip
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise LookupError(f"systemd-run: {(result.stderr or result.stdout).strip()[:300]}")

    ready = script.is_file() and shutil.which("systemd-run") is not None
    return UpdateAgent(config.update_dir, launcher=launch, native_ready=ready)
