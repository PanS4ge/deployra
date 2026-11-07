"""Główny obiekt panelu: baza, sesje, sekrety, zadania, klienci bramki i węzłów."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from fastapi import Request

from deployra_common.client import SignedClient
from deployra_common.updates import UpdateAgent

from . import __version__
from .config import Config
from .db import Database, now
from .i18n import t
from .security import LoginLimiter, Sealer, Sessions
from .services.persistent_jobs import PersistentJobRegistry

NODE_API_PORT = 2750
LOCAL_UPSTREAM = "local"  # proxy_address węzła obok panelu: adres ustalany na bieżąco (tunel panelu albo 127.0.0.1)


class Panel:
    def __init__(self, config: Config) -> None:
        self.config = config
        config.ensure_dirs()
        self.db = Database(config.db_url)
        self.db.migrate()
        self.sealer = Sealer(config.data_dir / "secret.key")
        self.sessions = Sessions(self.db, config.session_hours)
        self.limiter = LoginLimiter(self.db)
        # logowanie kontami list dostępu: 20 błędów na godzinę z adresu = ban na bramce, rosnące opóźnienie per konto
        self.fwd_limiter = LoginLimiter(self.db, limit=20, window=3600, free=5, scope="fwd")
        self._trusted: tuple[float, set[str]] = (0.0, set())
        self.jobs = PersistentJobRegistry(self.db, origin=f"panel {__version__}")
        self.updates = UpdateAgent(config.update_dir)
        self.default_language: str | None = self.db.setting("language") or config.language or None  # odświeżane przy zapisie ustawień
        from .plugins import PluginManager

        self.plugins = PluginManager(self, config.plugins_dir)
        self._clients: dict[str, tuple[str, SignedClient]] = {}
        self.task_runs: dict[int, asyncio.Task] = {}  # przebiegi zadań cyklicznych w toku (ID zadania → zadanie asyncio)

    # --- użytkownicy ----------------------------------------------------------------

    def has_users(self) -> bool:
        return bool(self.db.scalar("SELECT COUNT(*) FROM users"))

    def setup_code(self, create: bool = True) -> str | None:
        """Kod do pierwszej konfiguracji (/setup): bez niego obcy nie założy konta administratora na świeżej instalacji
        ani po resecie. Leży w katalogu danych (0600) i jest w logach kontenera."""
        import os
        import secrets

        from loguru import logger

        path = self.config.data_dir / "setup.code"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        if not create:
            return None
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # bez 0/O i 1/I
        code = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
        path.write_text(code, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.warning(t("setup.code_log", code=code))
        return code

    def drop_setup_code(self) -> None:
        (self.config.data_dir / "setup.code").unlink(missing_ok=True)

    def trusted_proxies(self) -> set[str]:
        """Zaufane proxy: z konfiguracji (DEPLOYRA_TRUSTED_PROXIES) + adresy tunelowe bramek (hub i brzegowa), które
        przekazują ruch do panelu — bez tego wszyscy za bramką brzegową mieliby jeden adres (i wspólny limit logowań)."""
        import time
        from urllib.parse import urlsplit

        at, cached = self._trusted
        if time.time() - at < 60:
            return cached
        found = {p.strip() for p in self.config.trusted_proxies.split(",") if p.strip() and p.strip() != "*"}
        for gw in self.db.fetchall("SELECT hub_address, api_url FROM gateway"):
            if gw.get("hub_address"):
                found.add(str(gw["hub_address"]).split("/")[0])
            host = urlsplit(str(gw.get("api_url") or "")).hostname
            if host:
                found.add(host)
        self._trusted = (time.time(), found)
        return found

    # --- powiadomienia ----------------------------------------------------------------

    def notify(self, message: str, level: str = "info") -> None:
        self.db.insert("notifications", message=message[:1000], level=level, created_at=now(), is_read=0)
        self.db.execute(
            "DELETE FROM notifications WHERE id NOT IN (SELECT id FROM (SELECT id FROM notifications ORDER BY id DESC LIMIT 100) AS keep)"
        )
        self.plugins.emit("notification", message=message, level=level)

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
        """Bramka z hubem WireGuard (do niej dołączają węzły i panel)."""
        return (
            self.db.fetchone("SELECT * FROM gateway WHERE role = 'hub' AND wg_enabled = 1 ORDER BY id LIMIT 1")
            or self.db.fetchone("SELECT * FROM gateway WHERE role = 'hub' ORDER BY id LIMIT 1")
            or self.db.fetchone("SELECT * FROM gateway ORDER BY id LIMIT 1")
        )

    def gateways(self) -> list[dict[str, Any]]:
        """Wszystkie skonfigurowane bramki."""
        return self.db.fetchall("SELECT * FROM gateway ORDER BY id")

    def wg_gateways(self) -> list[dict[str, Any]]:
        """Bramki obsługujące WireGuard (z hubem VPN)."""
        return [g for g in self.gateways() if g.get("wg_enabled", 1) and g.get("secret") and g.get("role") != "proxy"]

    def gateway_by_id(self, gw_id: int) -> dict[str, Any] | None:
        return self.db.get("gateway", gw_id)

    def client_for_gateway(self, gw: dict[str, Any]) -> SignedClient | None:
        if not gw or not gw.get("secret"):
            return None
        key = gw.get("role") if gw.get("role") in ("hub", "edge") else f"gw-{gw.get('id', 'default')}"
        return self._client(key, gw["api_url"], self.sealer.unseal_bytes(gw["secret"]))

    def edge_row(self) -> dict[str, Any] | None:
        """Bramka brzegowa („widelec”), także jeszcze niesparowana."""
        return self.db.fetchone("SELECT * FROM gateway WHERE role = 'edge' ORDER BY id LIMIT 1")

    def edge(self) -> dict[str, Any] | None:
        """Bramka z ruchem publicznym: brzegowa, jeśli jest sparowana, inaczej hub."""
        row = self.edge_row()
        return row if row and row.get("secret") else self.gateway()

    def gateway_client(self) -> SignedClient | None:
        """Bramka z ruchem publicznym: proxy, certyfikaty, statystyki, bany, test HTTP."""
        gw = self.edge()
        if not gw or not gw.get("secret"):
            return None
        return self.client_for_gateway(gw)

    def hub_client(self, gw: dict[str, Any] | None = None) -> SignedClient | None:
        """Hub WireGuard: peery, tokeny dołączenia, status tunelu."""
        target = gw or self.gateway()
        if not target or not target.get("secret"):
            return None
        return self.client_for_gateway(target)

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
        if node.get("proxy_address") == LOCAL_UPSTREAM:  # węzeł obok panelu — bramka widzi go przez tunel panelu
            hub = self.gateway() or {}
            if hub.get("panel_address"):
                return str(hub["panel_address"]).split("/")[0]
            edge = self.edge_row()
            if edge and edge.get("secret"):  # panel przy hubie, ruch wchodzi bramką brzegową — przez adres huba w tunelu
                return str(hub.get("hub_address") or "127.0.0.1")
            return "127.0.0.1"
        return node.get("proxy_address") or None

    async def aclose(self) -> None:
        for _, client in self._clients.values():
            await client.aclose()


def get_panel(request: Request) -> Panel:
    return request.app.state.panel
