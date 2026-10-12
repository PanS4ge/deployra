"""Węzeł na tej samej maszynie co panel (deploy/panel-node.compose.yml) — panel paruje go sam.

Backend zapisuje swój kod parowania do wspólnego wolumenu (DEPLOYRA_PAIR_FILE), panel czyta go stamtąd
(DEPLOYRA_LOCAL_NODE_CODE) i dodaje węzeł bezpośredni pod DEPLOYRA_LOCAL_NODE — bez przepisywania kodu
z logów. Plik znika po sparowaniu. Bramka widzi aplikacje tego węzła przez tunel WireGuard panelu
(albo pod 127.0.0.1, gdy panel stoi przy bramce) — adres ustala `Panel.node_upstream_host`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

from deployra_common.client import RemoteError

from ..context import LOCAL_UPSTREAM
from . import sync

RETRY = 10  # s — backend może wstać później niż panel
ATTEMPTS = 180


async def adopt(panel: Any) -> dict[str, Any] | None:
    """Paruje lokalny węzeł, jeśli jeszcze nie jest sparowany i jest kod; zwraca węzeł albo None."""
    config = panel.config
    if not config.local_node:
        return None
    base = sync._base_url(config.local_node, 2750)
    existing = panel.db.fetchone("SELECT * FROM nodes WHERE api_url = ?", [base])
    if existing and existing["status"] == "paired":
        return existing
    try:
        code = config.local_node_code.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not code:
        return None
    if existing:  # nieudana próba sprzed restartu
        panel.db.delete("nodes", existing["id"])
    node = await sync.add_direct_node(panel, config.local_node_name, base, code, "")
    panel.db.update("nodes", node["id"], proxy_address=LOCAL_UPSTREAM)
    logger.success(f"Węzeł {node['name']} z tej maszyny sparowany automatycznie")
    return panel.node(node["id"])


async def loop(panel: Any) -> None:
    for _ in range(ATTEMPTS):
        try:
            if await adopt(panel):
                return
        except (RemoteError, httpx.HTTPError, PermissionError, LookupError, ValueError, OSError) as exc:
            logger.warning(f"Lokalny węzeł: {getattr(exc, 'detail', None) or exc}")
        await asyncio.sleep(RETRY)
