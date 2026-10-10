"""Synchronizacja: parowanie panelu z Deployra-WG i z węzłami (X25519 + HKDF)."""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from deployra_common import pairing, wg
from deployra_common.client import RemoteError

from ..db import now
from . import wg_peer

NODE_CONTEXT = "panel-node"
GATEWAY_CONTEXT = "panel-gateway"


def _base_url(address: str, default_port: int) -> str:
    address = address.strip().rstrip("/")
    if not address.startswith(("http://", "https://")):
        address = f"http://{address}"
    parsed = urlparse(address)
    if not parsed.hostname:
        raise ValueError("podaj adres, np. vps.example.com:51821")
    port = parsed.port or default_port
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}:{port}"


async def _post_unsigned(url: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(url, json=body)
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RemoteError(response.status_code, str(detail))
    return response.json()


# --- bramka ----------------------------------------------------------------------------


async def pair_gateway(panel: Any, address: str, code: str, colocated: bool, api_override: str = "") -> dict:
    if panel.gateway():
        raise LookupError("panel jest już połączony z bramką — najpierw ją odłącz")
    bootstrap = _base_url(address, 51821)
    code = pairing.normalize_code(code)
    x_private, x_public = pairing.new_keypair()
    use_wg = not colocated and wg_peer.available(panel)
    wg_private, wg_public = wg.genkey() if use_wg else (None, None)
    nonce = secrets.token_hex(12)
    request = {"kind": "pair-gateway", "x25519": x_public, "nonce": nonce, "name": "Deployra"}
    if wg_public:
        request["wg_public_key"] = wg_public
    response = await _post_unsigned(f"{bootstrap}/pair", pairing.seal(code, request))
    try:
        data = pairing.unseal(code, response, "pair-gateway-ok")
    except ValueError:
        raise PermissionError("odpowiedź bramki nie przeszła weryfikacji — ktoś podmienia ruch?") from None
    if data.get("nonce") != nonce:
        raise PermissionError("odpowiedź bramki nie pasuje do żądania")
    secret = pairing.derive_secret(x_private, data["x25519"], code, GATEWAY_CONTEXT)
    info = data["gateway"]
    tunnel = data.get("tunnel") or {}
    api_url = api_override.strip().rstrip("/") or info["api_url"]
    row = {
        "name": "Deployra-WG",
        "bootstrap_url": bootstrap,
        "api_url": api_url,
        "secret": panel.sealer.seal(secret),
        "public_host": info["public_host"],
        "wg_port": info["wg_port"],
        "subnet": info["subnet"],
        "hub_address": info["hub_address"],
        "hub_public_key": info["hub_public_key"],
        "panel_address": tunnel.get("address"),
        "panel_wg_private": panel.sealer.seal(wg_private) if wg_private else None,
        "info": info,
        "created_at": now(),
    }
    gateway_id = panel.db.insert("gateway", **row)
    gateway = panel.db.get("gateway", gateway_id)
    if wg_private:
        await asyncio.to_thread(wg_peer.bring_up, panel, gateway)
    panel.notify("Połączono z Deployra-WG", "ok")
    return gateway


async def gateway_status(panel: Any) -> dict | None:
    client = panel.gateway_client()
    if not client:
        return None
    return await client.get("/api/status")


async def unlink_gateway(panel: Any) -> None:
    panel.db.execute("DELETE FROM gateway")
    panel._clients.pop("gateway", None)


# --- węzły -------------------------------------------------------------------------------


def _check_subnets(values: list[str]) -> list[str]:
    out = []
    for value in values:
        value = value.strip()
        if value:
            out.append(str(ipaddress.ip_network(value, strict=False)))
    return out


async def invite_node(panel: Any, name: str, lan_subnets: list[str], ttl_minutes: int = 60) -> tuple[int, str]:
    """Tworzy węzeł „oczekujący” i token dołączenia. Zwraca (id węzła, token)."""
    client = panel.gateway_client()
    if not client:
        raise LookupError("najpierw połącz panel z Deployra-WG (zakładka Tunel)")
    name = name.strip()[:60] or "węzeł"
    subnets = _check_subnets(lan_subnets)
    token = pairing.JoinToken.generate()
    await client.post(
        "/api/join-tokens",
        json={"id": token.id, "join_secret": token.join_secret, "name": name, "lan_subnets": subnets, "ttl_minutes": ttl_minutes},
    )
    node_id = panel.db.insert(
        "nodes", name=name, transport="tunnel", status="pending", peer_id=token.id, pair_secret=panel.sealer.seal(token.pair_secret),
        token_expires=time.time() + ttl_minutes * 60, lan_subnets=subnets, created_at=now(),
    )  # fmt: skip
    return node_id, token.encode()


async def refresh_pending(panel: Any) -> int:
    """Sprawdza na bramce, które oczekujące węzły dołączyły, i dokańcza ich parowanie."""
    pending = panel.db.fetchall("SELECT * FROM nodes WHERE transport = 'tunnel' AND status IN ('pending', 'joined')")
    if not pending:
        return 0
    status = await gateway_status(panel)
    peers = {p["id"]: p for p in (status or {}).get("peers", [])}
    done = 0
    for node in pending:
        peer = peers.get(node["peer_id"])
        if not peer:
            continue
        if node["status"] == "pending":
            panel.db.update("nodes", node["id"], tunnel_ip=peer["address"], status="joined")
            node = panel.node(node["id"])
        try:
            await pair_node(panel, node)
            done += 1
        except (httpx.HTTPError, RemoteError, PermissionError):
            continue  # węzeł jeszcze nie słucha w tunelu — spróbujemy przy następnym odświeżeniu
    return done


async def pair_node(panel: Any, node: dict, code: str | None = None) -> dict:
    """Parowanie z węzłem przez tunel (sekret z tokenu) albo bezpośrednio (kod z logów węzła)."""
    psk = pairing.normalize_code(code) if code else panel.sealer.unseal(node.get("pair_secret"))
    if not psk:
        raise LookupError("brak sekretu parowania dla tego węzła")
    x_private, x_public = pairing.new_keypair()
    nonce = secrets.token_hex(12)
    url = panel.node_url(node) + "/pair"
    response = await _post_unsigned(url, pairing.seal(psk, {"kind": "pair-node", "x25519": x_public, "nonce": nonce}))
    try:
        data = pairing.unseal(psk, response, "pair-node-ok")
    except ValueError:
        raise PermissionError("odpowiedź węzła nie przeszła weryfikacji") from None
    if data.get("nonce") != nonce:
        raise PermissionError("odpowiedź węzła nie pasuje do żądania")
    secret = pairing.derive_secret(x_private, data["x25519"], psk, NODE_CONTEXT)
    panel.db.update(
        "nodes", node["id"], secret=panel.sealer.seal(secret), status="paired", pair_secret=None, paired_at=now(),
        info={"node_id": data.get("node_id"), "name": data.get("name")},
    )  # fmt: skip
    panel.notify(f"Sparowano węzeł {node['name']}", "ok")
    node = panel.node(node["id"])
    try:
        info = await panel.node_client(node).get("/api/info")
        panel.db.update("nodes", node["id"], info=info)
    except (httpx.HTTPError, RemoteError):
        pass
    return panel.node(node["id"])


async def add_direct_node(panel: Any, name: str, url: str, code: str, proxy_address: str) -> dict:
    base = _base_url(url, 2750)
    node_id = panel.db.insert(
        "nodes", name=name.strip()[:60] or urlparse(base).hostname, transport="direct", api_url=base, status="pending",
        proxy_address=proxy_address.strip() or urlparse(base).hostname, created_at=now(),
    )  # fmt: skip
    try:
        return await pair_node(panel, panel.node(node_id), code)
    except Exception:
        panel.db.delete("nodes", node_id)
        raise


async def remove_node(panel: Any, node: dict) -> None:
    client = panel.gateway_client()
    if node["transport"] == "tunnel" and client and node.get("peer_id"):
        try:
            await client.delete(f"/api/peers/{node['peer_id']}")
        except RemoteError:
            pass
    for site in panel.db.all("sites"):
        if (site.get("target") or {}).get("node_id") == node["id"]:
            panel.db.update("sites", site["id"], enabled=0)
    panel.db.delete("nodes", node["id"])
    panel._clients.pop(f"node-{node['id']}", None)


def compose_snippet(gateway: dict, token: str, name: str, image: str = "deployra-backend:latest") -> str:
    host = gateway["public_host"]
    bootstrap = urlparse(gateway["bootstrap_url"]).port or 51821
    return f"""# node.compose.yml — Deployra Backend ({name})
# Obraz zbudujesz z repo: docker build -f backend/Dockerfile -t deployra-backend .
services:
  deployra-backend:
    image: {image}
    container_name: deployra-backend
    network_mode: host
    cap_add: [NET_ADMIN]
    devices: [/dev/net/tun]
    restart: unless-stopped
    labels:
      deployra.system: backend
    environment:
      DEPLOYRA_NAME: "{name}"
      DEPLOYRA_GATEWAY: "{host}:{bootstrap}"
      DEPLOYRA_JOIN: "{token}"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /var/lib/deployra:/var/lib/deployra
      - /srv/deployra:/srv/deployra
"""
