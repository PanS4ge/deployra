"""Trwały stan bramki."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deployra_common.state import JsonState

DEFAULTS: dict[str, Any] = {
    "hub_private_key": None,
    "pairing_code": None,  # kod do synchronizacji z panelem
    "panel_secret": None,  # base64, po synchronizacji
    "peers": {},  # id -> {kind, name, public_key, address, lan_subnets, created}
    "join_tokens": {},  # id -> {join_secret, name, lan_subnets, expires, used_by}
    "certs": {},  # id -> {domains, challenge, email, cloudflare_token, staging, not_after}
    "proxy_state": None,  # ostatni zastosowany ProxyState (do odtworzenia po restarcie)
}


class State(JsonState):
    def __init__(self, path: Path) -> None:
        super().__init__(path, DEFAULTS)

    @property
    def panel_secret(self) -> bytes | None:
        return self.b64(self.get("panel_secret"))
