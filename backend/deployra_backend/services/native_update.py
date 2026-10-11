"""Aktualizacje backendu: sidecar `deployra-updater` albo (instalacja systemd) `install-native.sh --update`."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from deployra_common.updates import UpdateAgent

from ..config import Config


def update_agent(config: Config) -> UpdateAgent:
    if not config.native_repo:
        return UpdateAgent(config.update_dir)
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
