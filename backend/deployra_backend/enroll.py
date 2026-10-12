"""Dołączenie węzła do Deployra-WG oraz parowanie z panelem."""

from __future__ import annotations

import base64
import os
import secrets
import time
from pathlib import Path
from typing import Any

from loguru import logger

from deployra_common import pairing, wg
from deployra_common.i18n import tr
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
    import httpx  # tylko przy dołączaniu — nie trzymamy go w pamięci na stałe

    response = httpx.post(bootstrap_url(cfg.gateway) + "/join", json=request, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(tr("node.join_rejected", code=response.status_code, error=response.text[:300]))
    data = pairing.unseal(token.join_secret, response.json(), "join-ok")
    if data.get("nonce") != request["nonce"]:
        raise RuntimeError(tr("node.gateway_mismatch"))
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
    logger.success(tr("node.log.joined", address=tunnel.address))


def join_with_retry(ctx: Any) -> None:
    delay = 5
    while True:
        try:
            join_gateway(ctx)
            return
        except Exception as exc:  # noqa: BLE001 — bramka niedostępna, zły token, sieć: ponawiamy z narastającym odstępem
            logger.warning(tr("node.log.join_retry", error=exc, delay=delay))
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
    publish_code(ctx, code)
    return code


def reset_pairing(ctx: Any) -> str:
    code = pairing.new_code()
    ctx.state.update(api_secret=None, pairing_code=code, join=None)
    publish_code(ctx, code)
    return code


def publish_code(ctx: Any, code: str | None) -> None:
    """Węzeł obok panelu (all-in-one): kod parowania trafia do wspólnego wolumenu, panel paruje się sam.

    Plik istnieje tylko do sparowania — potem znika, a kod i tak przestaje działać.
    """
    path = getattr(ctx.config, "pair_file", "")
    if not path:
        return
    target = Path(path)
    try:
        if code:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
            os.chmod(target, 0o600)
        else:
            target.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"{path}: {exc}")


class PairingGate:
    """Ogranicza zgadywanie kodu parowania."""

    def __init__(self) -> None:
        self.failures = 0
        self.locked_until = 0.0

    def check(self) -> None:
        if time.time() < self.locked_until:
            raise PermissionError(tr("node.pair_locked"))

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            self.failures = 0
            self.locked_until = time.time() + LOCKOUT_SECONDS


def pair(ctx: Any, gate: PairingGate, body: dict) -> dict:
    state = ctx.state
    if state.paired:
        raise LookupError(tr("node.already_paired"))
    gate.check()
    join = state.get("join")
    psk = join["pair_secret"] if join else state.get("pairing_code")
    if not psk:
        raise LookupError(tr("node.not_waiting"))
    try:
        request = pairing.unseal(psk, body, "pair-node")
        if not wg.valid_key(request.get("x25519", "")):
            raise ValueError(tr("node.bad_key"))
    except ValueError:
        gate.fail()
        raise PermissionError(tr("node.bad_code")) from None
    private, public = pairing.new_keypair()
    secret = pairing.derive_secret(private, request["x25519"], psk, PAIR_CONTEXT)
    state.update(api_secret=base64.b64encode(secret).decode(), pairing_code=None, join=None)
    publish_code(ctx, None)
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
