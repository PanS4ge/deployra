"""Trwały stan węzła (`state.json`)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from deployra_common.state import JsonState

DEFAULTS: dict[str, Any] = {
    "node_id": None,
    "api_secret": None,  # base64, po sparowaniu z panelem
    "pairing_code": None,  # tryb bez bramki / ponowne parowanie
    "join": None,  # {"id", "pair_secret"} po dołączeniu do bramki, do czasu sparowania
    "wg": None,  # konfiguracja tunelu od bramki
    "wg_pending_private": None,
    "ports": {},  # slug -> port z puli
    "static_port": None,
    "sftp_users": {},  # login -> {"hash", "slug", "keys": []}
    "apps": {},  # slug -> metadane wdrożenia
}


class State(JsonState):
    def __init__(self, path: Path) -> None:
        super().__init__(path, DEFAULTS)
        if not self.get("node_id"):
            self.update(node_id=uuid.uuid4().hex[:12])

    @property
    def secret(self) -> bytes | None:
        return self.b64(self.get("api_secret"))

    @property
    def paired(self) -> bool:
        return bool(self.get("api_secret"))
