"""Kontekst bramki i logika synchronizacji / dołączania."""

from __future__ import annotations

import base64
import time
from typing import Any

from fastapi import Request
from loguru import logger

from deployra_common import pairing, wg
from deployra_common.hmac_auth import NonceCache
from deployra_common.jobs import JobRegistry
from deployra_common.schemas import ProxyState
from deployra_common.server import SignatureGuard

from . import __version__, certs, nginx_driver
from .config import Config
from .nginx_ctl import ApplyResult, NginxControl
from .state import State
from .wg_hub import Hub

PANEL_CONTEXT = "panel-gateway"


class Gate:
    """Ogranicza zgadywanie kodów i tokenów."""

    def __init__(self, limit: int = 10, lockout: int = 600) -> None:
        self.limit, self.lockout = limit, lockout
        self.failures, self.locked_until = 0, 0.0

    def check(self) -> None:
        if time.time() < self.locked_until:
            raise PermissionError("zbyt wiele nieudanych prób — spróbuj za kilka minut")

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= self.limit:
            self.failures, self.locked_until = 0, time.time() + self.lockout


class Context:
    def __init__(self, config: Config, wg_control: wg.WgControl | None = None) -> None:
        self.config = config
        config.ensure_dirs()
        self.state = State(config.data_dir / "state.json")
        self.hub = Hub(config, self.state, wg_control)
        self.nginx = NginxControl(config.nginx_dir, config.nginx_reload, config.reload_timeout)
        self.jobs = JobRegistry()
        self.guard = SignatureGuard(lambda key: self.state.panel_secret if key == "panel" else None, NonceCache())
        self.gate = Gate()
        self.last_apply: dict[str, Any] | None = None
        from .analytics import Analytics
        from .cloudflared import Cloudflared

        self.analytics = Analytics(config.data_dir, bytes.fromhex(self.state.get("analytics_secret")))
        self.cloudflared = Cloudflared(self.state, config.cloudflared_bin)

    # --- synchronizacja z panelem -------------------------------------------------

    def ensure_pairing_code(self) -> str | None:
        if self.state.panel_secret:
            return None
        code = self.state.get("pairing_code")
        if not code:
            code = pairing.new_code()
            self.state.update(pairing_code=code)
        return code

    def reset_pairing(self) -> str:
        code = pairing.new_code()
        self.state.update(panel_secret=None, pairing_code=code)
        return code

    def pair_panel(self, body: dict) -> dict:
        if self.state.panel_secret:
            raise LookupError("bramka jest już zsynchronizowana z panelem (reset: python -m deployra_gateway reset-pairing)")
        code = self.state.get("pairing_code")
        if not code:
            raise LookupError("bramka nie czeka na synchronizację")
        self.gate.check()
        try:
            request = pairing.unseal(code, body, "pair-gateway")
            if not wg.valid_key(request.get("x25519", "")):
                raise ValueError
        except ValueError:
            self.gate.fail()
            raise PermissionError("zły kod synchronizacji") from None
        tunnel = None
        if request.get("wg_public_key"):
            address = self.hub.add_peer("panel", "panel", str(request.get("name") or "Deployra")[:40], request["wg_public_key"])
            tunnel = self.hub.tunnel_config(address).model_dump()
        private, public = pairing.new_keypair()
        secret = pairing.derive_secret(private, request["x25519"], code, PANEL_CONTEXT)
        self.state.update(panel_secret=base64.b64encode(secret).decode(), pairing_code=None)
        logger.success("Zsynchronizowano z panelem Deployry")
        return pairing.seal(
            code,
            {
                "kind": "pair-gateway-ok",
                "nonce": request.get("nonce"),
                "x25519": public,
                "tunnel": tunnel,
                "gateway": self.public_info(),
            },
        )

    def public_info(self) -> dict:
        return {
            "version": __version__,
            "public_host": self.config.public_host,
            "wg_port": self.config.wg_port,
            "bootstrap_port": self.config.bootstrap_port,
            "subnet": self.config.wg_subnet,
            "hub_address": self.config.hub_address,
            "hub_public_key": self.hub.public_key,
            "api_url": self.config.api_url,
        }

    # --- dołączanie węzłów ----------------------------------------------------------

    def register_token(self, token_id: str, join_secret: str, name: str, lan_subnets: list[str], ttl_minutes: int) -> dict:
        if not token_id.isalnum() or len(token_id) > 32 or len(join_secret) < 16:
            raise ValueError("nieprawidłowy token")
        for subnet in lan_subnets:
            wg.WgPeer(self.hub.public_key, [subnet])  # walidacja podsieci
        entry = {
            "join_secret": join_secret,
            "name": name[:60],
            "lan_subnets": lan_subnets,
            "expires": time.time() + ttl_minutes * 60,
            "used_by": None,
        }
        self.state.mutate("join_tokens", lambda tokens: {**(tokens or {}), token_id: entry})
        return {"id": token_id, "expires": entry["expires"]}

    def drop_token(self, token_id: str) -> None:
        self.state.mutate("join_tokens", lambda tokens: {k: v for k, v in (tokens or {}).items() if k != token_id})

    def join_node(self, body: dict) -> dict:
        self.gate.check()
        token_id = str(body.get("token_id", ""))
        token = self.state.get("join_tokens", {}).get(token_id)
        if not token:
            self.gate.fail()
            raise PermissionError("nieznany token dołączenia")
        try:
            request = pairing.unseal(token["join_secret"], body, "join")
        except ValueError:
            self.gate.fail()
            raise PermissionError("zły token dołączenia") from None
        public_key = request.get("wg_public_key", "")
        if token["used_by"] and token["used_by"] != public_key:
            raise LookupError("ten token został już użyty przez inny węzeł")
        if not token["used_by"] and time.time() > token["expires"]:
            raise PermissionError("token wygasł — wygeneruj nowy w panelu")
        address = self.hub.add_peer(token_id, "node", token["name"] or str(request.get("name", ""))[:60], public_key, token["lan_subnets"])

        def mark(tokens: dict) -> dict:
            tokens[token_id] = {**tokens[token_id], "used_by": public_key, "joined_at": time.time()}
            return tokens

        self.state.mutate("join_tokens", mark)
        logger.success(f"Węzeł {token['name']} dołączył ({address})")
        return pairing.seal(
            token["join_secret"],
            {
                "kind": "join-ok",
                "nonce": request.get("nonce"),
                "tunnel": self.hub.tunnel_config(address).model_dump(),
                "lan_subnets": token["lan_subnets"],
            },
        )

    # --- proxy ------------------------------------------------------------------------

    def apply_proxy(self, proxy: ProxyState | None = None) -> ApplyResult:
        if proxy is None:
            stored = self.state.get("proxy_state")
            proxy = ProxyState.model_validate(stored) if stored else ProxyState()
        files = nginx_driver.render(proxy, lambda cid: certs.is_ready(self.config.certs_dir, cid), self.config.ipv6)
        result = self.nginx.apply(files)
        if result.status != "rejected":
            self.state.update(proxy_state=proxy.model_dump(mode="json"))
        self.last_apply = {"ok": result.ok, "status": result.status, "message": result.message, "at": time.time()}
        return result


def get_ctx(request: Request) -> Context:
    return request.app.state.ctx
