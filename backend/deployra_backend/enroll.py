"""Dołączenie węzła do Deployra-WG oraz parowanie z panelem."""

from __future__ import annotations

import base64
import secrets
import time
from typing import Any

import httpx
from loguru import logger

from deployra_common import pairing, wg
from deployra_common.schemas import TunnelConfig

PAIR_CONTEXT = "panel-node"
MAX_FAILURES = 10
LOCKOUT_SECONDS = 600


def bootstrap_url(gateway: str) -> str:
    gateway = gateway.strip().rstrip("/")
    return gateway if gateway.startswith(("http://", "https://")) else f"http://{gateway}"


def join_gateway(ctx: Any) -> None:
    """Rejestruje klucz WG węzła na bramce i zapisuje konfigurację tunelu.

    Klucz prywatny powstaje lokalnie i nigdy nie opuszcza węzła. Ponowienie
    z tym samym tokenem i kluczem jest bezpieczne (bramka odda tę samą konfigurację).
    """
    cfg, state = ctx.config, ctx.state
    token = pairing.JoinToken.parse(cfg.join_token)
    pending = state.get("wg_pending_private")
    if not pending:
        pending, _ = wg.genkey()
        state.update(wg_pending_private=pending)
    public = wg.pubkey(pending)
    request = pairing.seal(
        token.join_secret,
        {"kind": "join", "token_id": token.id, "wg_public_key": public, "name": cfg.name, "nonce": secrets.token_hex(12)},
    )
    response = httpx.post(bootstrap_url(cfg.gateway) + "/join", json=request, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"bramka odrzuciła dołączenie ({response.status_code}): {response.text[:300]}")
    data = pairing.unseal(token.join_secret, response.json(), "join-ok")
    if data.get("nonce") != request["nonce"]:
        raise RuntimeError("odpowiedź bramki nie pasuje do żądania")
    tunnel = TunnelConfig(**data["tunnel"])
    lan = [str(s) for s in data.get("lan_subnets") or []]
    state.update(
        wg={
            "private_key": pending,
            "address": tunnel.address,
            "subnet": tunnel.subnet,
            "hub_public_key": tunnel.hub_public_key,
            "hub_endpoint": tunnel.hub_endpoint,
            "hub_address": tunnel.hub_address,
            "keepalive": tunnel.keepalive,
            "lan_subnets": lan,
        },
        join={"id": token.id, "pair_secret": token.pair_secret},
        wg_pending_private=None,
    )
    logger.success(f"Dołączono do Deployra-WG: adres w tunelu {tunnel.address}")


def join_with_retry(ctx: Any) -> None:
    delay = 5
    while True:
        try:
            join_gateway(ctx)
            return
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            logger.warning(f"Dołączenie do bramki nie powiodło się: {exc}. Ponawiam za {delay} s")
            time.sleep(delay)
            delay = min(delay * 2, 60)


def ensure_pairing_code(ctx: Any) -> str | None:
    """W trybie bez tokenu (lub po resecie) węzeł paruje się kodem z logów."""
    state = ctx.state
    if state.paired or state.get("join"):
        return None
    code = state.get("pairing_code")
    if not code:
        code = pairing.new_code()
        state.update(pairing_code=code)
    return code


def reset_pairing(ctx: Any) -> str:
    code = pairing.new_code()
    ctx.state.update(api_secret=None, pairing_code=code, join=None)
    return code


class PairingGate:
    """Ogranicza zgadywanie kodu parowania."""

    def __init__(self) -> None:
        self.failures = 0
        self.locked_until = 0.0

    def check(self) -> None:
        if time.time() < self.locked_until:
            raise PermissionError("zbyt wiele nieudanych prób parowania — spróbuj za kilka minut")

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            self.failures = 0
            self.locked_until = time.time() + LOCKOUT_SECONDS


def pair(ctx: Any, gate: PairingGate, body: dict) -> dict:
    state = ctx.state
    if state.paired:
        raise LookupError("węzeł jest już sparowany — zresetuj parowanie na węźle")
    gate.check()
    join = state.get("join")
    psk = join["pair_secret"] if join else state.get("pairing_code")
    if not psk:
        raise LookupError("węzeł nie czeka na parowanie")
    try:
        request = pairing.unseal(psk, body, "pair-node")
        if not wg.valid_key(request.get("x25519", "")):
            raise ValueError("zły klucz")
    except ValueError:
        gate.fail()
        raise PermissionError("zły kod parowania") from None
    private, public = pairing.new_keypair()
    secret = pairing.derive_secret(private, request["x25519"], psk, PAIR_CONTEXT)
    state.update(api_secret=base64.b64encode(secret).decode(), pairing_code=None, join=None)
    logger.success("Sparowano z panelem Deployry")
    return pairing.seal(
        psk,
        {
            "kind": "pair-node-ok",
            "x25519": public,
            "nonce": request.get("nonce"),
            "node_id": state.get("node_id"),
            "name": ctx.config.name,
        },
    )
