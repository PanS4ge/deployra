"""Główny obiekt panelu: baza, sesje, sekrety, zadania, klienci bramki i węzłów."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import Request

from deployra_common.client import SignedClient
from deployra_common.jobs import JobRegistry
from deployra_common.updates import UpdateAgent

from .config import Config
from .db import Database, now
from .i18n import t
from .security import LoginLimiter, Sealer, Sessions

NODE_API_PORT = 2750


class Panel:
    def __init__(self, config: Config) -> None:
        self.config = config
        config.ensure_dirs()
        self.db = Database(config.db_url)
        self.db.migrate()
        self.sealer = Sealer(config.data_dir / "secret.key")
        self.sessions = Sessions(self.db, config.session_hours)
        self.limiter = LoginLimiter()
        self.jobs = JobRegistry()
        self.updates = UpdateAgent(config.update_dir)
        self.default_language: str | None = self.db.setting("language") or config.language or None  # odświeżane przy zapisie ustawień
        self._clients: dict[str, tuple[str, SignedClient]] = {}

    # --- użytkownicy ----------------------------------------------------------------

    def has_users(self) -> bool:
        return bool(self.db.scalar("SELECT COUNT(*) FROM users"))

    # --- powiadomienia ----------------------------------------------------------------

    def notify(self, message: str, level: str = "info") -> None:
        self.db.insert("notifications", message=message[:1000], level=level, created_at=now(), is_read=0)
        self.db.execute(
            "DELETE FROM notifications WHERE id NOT IN (SELECT id FROM (SELECT id FROM notifications ORDER BY id DESC LIMIT 100) AS keep)"
        )

    # --- klienci HTTP -----------------------------------------------------------------

    def _client(self, key: str, base_url: str, secret: bytes) -> SignedClient:
        fingerprint = hashlib.sha256(base_url.encode() + secret).hexdigest()
        cached = self._clients.get(key)
        if cached and cached[0] == fingerprint:
            return cached[1]
        client = SignedClient(base_url, secret, timeout=20)
        self._clients[key] = (fingerprint, client)
        return client

    def gateway(self) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT * FROM gateway ORDER BY id LIMIT 1")

    def gateway_client(self) -> SignedClient | None:
        gw = self.gateway()
        if not gw or not gw.get("secret"):
            return None
        return self._client("gateway", gw["api_url"], self.sealer.unseal_bytes(gw["secret"]))

    @staticmethod
    def node_url(node: dict[str, Any]) -> str:
        if node["transport"] == "tunnel":
            return f"http://{node['tunnel_ip']}:{NODE_API_PORT}"
        return node["api_url"].rstrip("/")

    def node_client(self, node: dict[str, Any]) -> SignedClient:
        if node["status"] != "paired" or not node.get("secret"):
            raise LookupError(t("nodes.not_paired"))
        return self._client(f"node-{node['id']}", self.node_url(node), self.sealer.unseal_bytes(node["secret"]))

    def node(self, node_id: int) -> dict[str, Any] | None:
        return self.db.get("nodes", node_id)

    def node_upstream_host(self, node: dict[str, Any]) -> str | None:
        """Adres, pod którym bramka (nginx) widzi porty węzła."""
        if node["transport"] == "tunnel":
            return node.get("tunnel_ip")
        return node.get("proxy_address") or None

    async def aclose(self) -> None:
        for _, client in self._clients.values():
            await client.aclose()


def get_panel(request: Request) -> Panel:
    return request.app.state.panel
