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
from ..i18n import lang as i18n_lang
from ..i18n import t

NODE_CONTEXT = "panel-node"
GATEWAY_CONTEXT = "panel-gateway"


def _base_url(address: str, default_port: int) -> str:
    address = address.strip().rstrip("/")
    if not address.startswith(("http://", "https://")):
        address = f"http://{address}"
    parsed = urlparse(address)
    if not parsed.hostname:
        raise ValueError(t("sync.need_address"))
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


async def pair_gateway(
    panel: Any, address: str, code: str, colocated: bool, api_override: str = "", name_override: str = ""
) -> dict:
    bootstrap = _base_url(address, 51821)
    for existing in panel.gateways():
        if existing.get("bootstrap_url") == bootstrap:
            raise LookupError(t("sync.already_linked"))
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
        raise PermissionError(t("sync.gateway_bad_reply")) from None
    if data.get("nonce") != nonce:
        raise PermissionError(t("sync.gateway_mismatch"))
    secret = pairing.derive_secret(x_private, data["x25519"], code, GATEWAY_CONTEXT)
    info = data["gateway"]
    tunnel = data.get("tunnel") or {}
    api_url = api_override.strip().rstrip("/") or info["api_url"]
    wg_enabled = 1 if info.get("wg_enabled", True) else 0
    role = info.get("role", "hub" if wg_enabled else "proxy")
    if name_override:
        name = name_override
    elif not panel.gateways():
        name = "Deployra-WG" if wg_enabled else "Deployra-Proxy"
    else:
        name = f"Deployra-Proxy ({info.get('public_host') or len(panel.gateways()) + 1})"
    row = {
        "name": name,
        "role": role,
        "bootstrap_url": bootstrap,
        "api_url": api_url,
        "secret": panel.sealer.seal(secret),
        "public_host": info["public_host"],
        "wg_port": info["wg_port"],
        "subnet": info["subnet"],
        "hub_address": info["hub_address"],
        "hub_public_key": info["hub_public_key"],
        "panel_address": tunnel.get("address"),
        "panel_wg_private": panel.sealer.seal(wg_private) if (wg_private and wg_enabled) else None,
        "wg_enabled": wg_enabled,
        "info": info,
        "created_at": now(),
    }
    gateway_id = panel.db.insert("gateway", **row)
    gateway = panel.db.get("gateway", gateway_id)
    if wg_private:
        await asyncio.to_thread(wg_peer.bring_up, panel, gateway)
    panel.notify(t("sync.gateway_linked"), "ok")
    return gateway


async def gateway_status(panel: Any) -> dict | None:
    client = panel.hub_client()
    if not client:
        return None
    return await client.get("/api/status")


async def unlink_gateway(panel: Any, gateway_id: int | None = None) -> None:
    if gateway_id:
        panel.db.execute("DELETE FROM gateway WHERE id = ?", [gateway_id])
        panel._clients.pop(f"gw-{gateway_id}", None)
    else:
        panel.db.execute("DELETE FROM gateway")
        for key in list(panel._clients.keys()):
            if key in ("hub", "edge") or key.startswith("gw-"):
                panel._clients.pop(key, None)


# --- bramka brzegowa („widelec”) -----------------------------------------------------------
# Ruch publiczny przyjmuje inna maszyna niż hub WireGuard (np. Oracle + mikr.us). Bramka brzegowa
# dołącza do huba tokenem jak węzeł, a panel paruje się z nią przez tunel sekretem z tokenu.


def edge_snippet(hub: dict, public_host: str, token: str) -> str:
    """Polecenie dla maszyny z bramką brzegową (deploy/gateway.compose.yml z DEPLOYRA_HUB i DEPLOYRA_JOIN)."""
    bootstrap = urlparse(hub["bootstrap_url"]).port or 51821
    return (f'DEPLOYRA_PUBLIC_HOST="{public_host}" DEPLOYRA_HUB="{hub["public_host"]}:{bootstrap}" \\\n'
            f'DEPLOYRA_JOIN="{token}" docker compose -f deploy/gateway.compose.yml up -d')  # fmt: skip


async def invite_edge(panel: Any, public_host: str, ttl_minutes: int = 24 * 60) -> tuple[int, str]:
    hub, client = panel.gateway(), panel.hub_client()
    if not hub or not client:
        raise LookupError(t("sync.link_gateway_first"))
    public_host = public_host.strip().lower()
    if not public_host or any(c.isspace() or c in "/:@" for c in public_host.strip("[]")):
        raise ValueError(t("edge.bad_host"))
    old = panel.edge_row()
    if old and old.get("secret"):
        raise LookupError(t("edge.already"))
    if old:  # poprzednie, niedokończone zaproszenie
        await _drop_edge_peer(client, old)
        panel.db.delete("gateway", old["id"])
    token = pairing.JoinToken.generate()
    await client.post("/api/join-tokens", json={"id": token.id, "join_secret": token.join_secret, "name": public_host,
                                                "ttl_minutes": ttl_minutes, "kind": "edge"})  # fmt: skip
    row_id = panel.db.insert("gateway", name="Deployra-WG (edge)", role="edge", status="pending", public_host=public_host,
                             token_id=token.id, pair_secret=panel.sealer.seal(token.pair_secret), created_at=now())  # fmt: skip
    return row_id, token.encode()


async def _drop_edge_peer(client: Any, row: dict) -> None:
    try:
        await client.delete(f"/api/peers/{row['token_id']}")
    except (RemoteError, httpx.HTTPError):
        pass


async def pair_edge(panel: Any, row: dict, address: str) -> dict:
    """Parowanie z bramką brzegową przez tunel (jej bootstrap na adresie w tunelu, kod = sekret z tokenu)."""
    psk = panel.sealer.unseal(row.get("pair_secret"))
    if not psk:
        raise LookupError(t("sync.no_pair_secret"))
    x_private, x_public = pairing.new_keypair()
    nonce = secrets.token_hex(12)
    bootstrap = f"http://{address}:51821"
    response = await _post_unsigned(f"{bootstrap}/pair", pairing.seal(psk, {"kind": "pair-gateway", "x25519": x_public, "nonce": nonce,
                                                                             "name": "Deployra"}))  # fmt: skip
    try:
        data = pairing.unseal(psk, response, "pair-gateway-ok")
    except ValueError:
        raise PermissionError(t("sync.gateway_bad_reply")) from None
    if data.get("nonce") != nonce:
        raise PermissionError(t("sync.gateway_mismatch"))
    info = data["gateway"]
    if info.get("role") != "edge":
        raise PermissionError(t("edge.not_edge"))
    secret = pairing.derive_secret(x_private, data["x25519"], psk, GATEWAY_CONTEXT)
    panel.db.update("gateway", row["id"], secret=panel.sealer.seal(secret), status="paired", pair_secret=None, bootstrap_url=bootstrap,
                    api_url=info["api_url"], public_host=info.get("public_host") or row["public_host"], wg_port=info.get("wg_port"),
                    subnet=info.get("subnet"), hub_address=info.get("hub_address"), hub_public_key=info.get("hub_public_key"),
                    panel_address=info.get("tunnel_address"), info=info)  # fmt: skip
    panel._clients.pop("edge", None)
    panel.notify(t("edge.linked", host=info.get("public_host") or row["public_host"]), "ok")
    await _switch_ingress(panel)
    return panel.edge_row()


async def unlink_edge(panel: Any) -> None:
    row = panel.edge_row()
    if not row:
        return
    client = panel.hub_client()
    if client and row.get("token_id"):
        await _drop_edge_peer(client, row)
    panel.db.delete("gateway", row["id"])
    panel._clients.pop("edge", None)
    await _switch_ingress(panel)


async def _switch_ingress(panel: Any) -> None:
    """Hosty proxy idą do bramki z ruchem publicznym; hub dostaje pusty stan, gdy ruch wchodzi bramką brzegową."""
    from deployra_common.schemas import ProxyState

    from . import proxy_state

    try:
        await proxy_state.push(panel)
        edge, hub = panel.edge_row(), panel.hub_client()
        if edge and edge.get("secret") and hub:
            await hub.put("/api/proxy", json=ProxyState().model_dump(mode="json"))
    except (RemoteError, httpx.HTTPError):
        pass  # następny zapis hostów proxy wyśle stan jeszcze raz


async def push_edge_routes(panel: Any) -> None:
    """Podsieci LAN węzłów → bramka brzegowa (żeby ruch do np. 192.168.1.0/24 szedł tunelem przez hub)."""
    edge = panel.edge_row()
    if not edge or not edge.get("secret"):
        return
    subnets = sorted({s for node in panel.db.all("nodes") for s in (node.get("lan_subnets") or [])})
    try:
        await panel.gateway_client().put("/api/routes", json={"subnets": subnets})
    except (RemoteError, httpx.HTTPError):
        pass


# --- węzły -------------------------------------------------------------------------------


def _check_subnets(values: list[str]) -> list[str]:
    out = []
    for value in values:
        value = value.strip()
        if value:
            out.append(str(ipaddress.ip_network(value, strict=False)))
    return out


async def invite_node(panel: Any, name: str, lan_subnets: list[str], ttl_minutes: int = 60, gateway_id: int | None = None) -> tuple[int, str]:
    """Tworzy węzeł „oczekujący” i token dołączenia. Zwraca (id węzła, token)."""
    gw = panel.gateway_by_id(gateway_id) if gateway_id else None
    if not gw:
        wg_gws = panel.wg_gateways()
        gw = wg_gws[0] if wg_gws else panel.gateway()
    if not gw:
        raise LookupError(t("sync.link_gateway_first"))
    if not gw.get("wg_enabled", 1) or gw.get("role") == "proxy":
        raise LookupError(t("sync.no_wg_gateway"))
    client = panel.hub_client(gw)
    if not client:
        raise LookupError(t("sync.link_gateway_first"))
    name = name.strip()[:60] or t("tunnel.node")
    subnets = _check_subnets(lan_subnets)
    token = pairing.JoinToken.generate()
    await client.post(
        "/api/join-tokens",
        json={"id": token.id, "join_secret": token.join_secret, "name": name, "lan_subnets": subnets, "ttl_minutes": ttl_minutes},
    )
    node_id = panel.db.insert(
        "nodes", name=name, transport="tunnel", status="pending", peer_id=token.id, pair_secret=panel.sealer.seal(token.pair_secret),
        token_expires=time.time() + ttl_minutes * 60, lan_subnets=subnets, gateway_id=gw["id"], created_at=now(),
    )
    return node_id, token.encode()


async def reinvite_node(panel: Any, node: dict, ttl_minutes: int = 120) -> tuple[str, dict[str, str]]:
    """Generuje nowy token dla istniejącego, niesparowanego węzła i zwraca (token, snippets)."""
    gw = panel.gateway_by_id(node.get("gateway_id")) if node.get("gateway_id") else None
    if not gw:
        wg_gws = panel.wg_gateways()
        gw = wg_gws[0] if wg_gws else panel.gateway()
    if not gw:
        raise LookupError(t("sync.link_gateway_first"))
    if not gw.get("wg_enabled", 1) or gw.get("role") == "proxy":
        raise LookupError(t("sync.no_wg_gateway"))
    client = panel.hub_client(gw)
    if not client:
        raise LookupError(t("sync.link_gateway_first"))
    token = pairing.JoinToken.generate()
    subnets = node.get("lan_subnets") or []
    if node.get("peer_id"):
        try:
            await client.delete(f"/api/join-tokens/{node['peer_id']}")
        except Exception:
            pass
    await client.post(
        "/api/join-tokens",
        json={"id": token.id, "join_secret": token.join_secret, "name": node["name"], "lan_subnets": subnets, "ttl_minutes": ttl_minutes},
    )
    panel.db.update(
        "nodes", node["id"],
        peer_id=token.id,
        pair_secret=panel.sealer.seal(token.pair_secret),
        token_expires=time.time() + ttl_minutes * 60,
        gateway_id=gw["id"],
    )
    gateway = gw
    images = panel.db.setting("image_prefix") or DEFAULT_IMAGES
    encoded = token.encode()
    from ..services import updates
    args = (gateway, encoded, node["name"])
    snippets = {
        "compose": compose_snippet(*args, images),
        "shell": shell_snippet(*args, images),
        "run": docker_run_snippet(*args, images),
        "native": native_snippet(*args, updates.source(panel) or "PanS4ge/deployra"),
    }
    return encoded, snippets


async def refresh_pending(panel: Any) -> int:
    """Sprawdza na bramce, które oczekujące węzły dołączyły, i dokańcza ich parowanie."""
    pending = panel.db.fetchall("SELECT * FROM nodes WHERE transport = 'tunnel' AND status IN ('pending', 'joined')")
    edge = panel.edge_row()
    edge_pending = edge if edge and not edge.get("secret") else None
    if not pending and not edge_pending:
        return 0
    status = await gateway_status(panel)
    peers = {p["id"]: p for p in (status or {}).get("peers", [])}
    done = 0
    if edge_pending and peers.get(edge_pending["token_id"]):
        try:
            await pair_edge(panel, edge_pending, peers[edge_pending["token_id"]]["address"])
            done += 1
        except (httpx.HTTPError, RemoteError, PermissionError):
            pass  # bramka brzegowa jeszcze nie słucha w tunelu
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
        raise LookupError(t("sync.no_pair_secret"))
    x_private, x_public = pairing.new_keypair()
    nonce = secrets.token_hex(12)
    url = panel.node_url(node) + "/pair"
    response = await _post_unsigned(url, pairing.seal(psk, {"kind": "pair-node", "x25519": x_public, "nonce": nonce}))
    try:
        data = pairing.unseal(psk, response, "pair-node-ok")
    except ValueError:
        raise PermissionError(t("sync.node_bad_reply")) from None
    if data.get("nonce") != nonce:
        raise PermissionError(t("sync.node_mismatch"))
    secret = pairing.derive_secret(x_private, data["x25519"], psk, NODE_CONTEXT)
    panel.db.update(
        "nodes", node["id"], secret=panel.sealer.seal(secret), status="paired", pair_secret=None, paired_at=now(),
        info={"node_id": data.get("node_id"), "name": data.get("name")},
    )  # fmt: skip
    panel.notify(t("msg.node_paired", name=node["name"]), "ok")
    panel.plugins.emit("node.paired", node={k: v for k, v in node.items() if k != "secret"})
    from . import uptime

    uptime.ensure_for(panel, "node", node["id"], node["name"], node["id"])
    node = panel.node(node["id"])
    try:
        info = await panel.node_client(node).get("/api/info")
        panel.db.update("nodes", node["id"], info=info)
    except (httpx.HTTPError, RemoteError):
        pass
    return panel.node(node["id"])


async def add_direct_node(panel: Any, name: str, url: str, code: str, proxy_address: str, gateway_id: int | None = None) -> dict:
    base = _base_url(url, 2750)
    node_id = panel.db.insert(
        "nodes", name=name.strip()[:60] or urlparse(base).hostname, transport="direct", api_url=base, status="pending",
        proxy_address=proxy_address.strip() or urlparse(base).hostname, gateway_id=gateway_id, created_at=now(),
    )  # fmt: skip
    try:
        return await pair_node(panel, panel.node(node_id), code)
    except Exception:
        panel.db.delete("nodes", node_id)
        raise


async def remove_node(panel: Any, node: dict) -> None:
    client = panel.hub_client()
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


DEFAULT_IMAGES = "ghcr.io/pans4ge/deployra"


def node_target(gateway: dict) -> str:
    """Adres dołączania bramki (host:port), z którym łączy się nowy węzeł."""
    return f"{gateway['public_host']}:{urlparse(gateway['bootstrap_url']).port or 51821}"


def compose_snippet(gateway: dict, token: str, name: str, images: str = DEFAULT_IMAGES) -> str:
    """Gotowy compose węzła — do wklejenia jako stack w Portainerze albo zapisania jako compose.yml."""
    images = (images or DEFAULT_IMAGES).strip().rstrip("-")
    return f"""services:
  deployra-backend:
    image: {images}-backend:latest
    container_name: deployra-backend
    network_mode: host                     # {t('invite.c_host')}
    cap_add: [NET_ADMIN]
    devices: [/dev/net/tun]
    labels:
      deployra.system: backend
    environment:
      DEPLOYRA_NAME: "{name}"
      DEPLOYRA_GATEWAY: "{node_target(gateway)}"
      DEPLOYRA_JOIN: "{token}"
      DEPLOYRA_LANG: "{i18n_lang()}"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /var/lib/deployra:/var/lib/deployra   # {t('invite.c_state')}
      - /srv/deployra:/srv/deployra           # {t('invite.c_files')}
    restart: unless-stopped
    mem_limit: 192m
    logging: {{ driver: json-file, options: {{ max-size: 5m, max-file: "2" }} }}
"""


def shell_snippet(gateway: dict, token: str, name: str, images: str = DEFAULT_IMAGES) -> str:
    """Jedno wklejenie do terminala: zapisuje compose.yml w ~/deployra-node i uruchamia węzeł."""
    compose = compose_snippet(gateway, token, name, images)
    return f"mkdir -p ~/deployra-node && cd ~/deployra-node && cat > compose.yml <<'EOF'\n{compose}EOF\ndocker compose up -d\n"


def docker_run_snippet(gateway: dict, token: str, name: str, images: str = DEFAULT_IMAGES) -> str:
    images = (images or DEFAULT_IMAGES).strip().rstrip("-")
    lines = [
        "docker run -d --name deployra-backend --restart unless-stopped",
        "  --network host --cap-add NET_ADMIN --device /dev/net/tun --memory 192m",
        "  --label deployra.system=backend --log-opt max-size=5m --log-opt max-file=2",
        f'  -e DEPLOYRA_NAME="{name}" -e DEPLOYRA_GATEWAY="{node_target(gateway)}" -e DEPLOYRA_LANG="{i18n_lang()}"',
        f'  -e DEPLOYRA_JOIN="{token}"',
        "  -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/deployra:/var/lib/deployra -v /srv/deployra:/srv/deployra",
        f"  {images}-backend:latest",
    ]
    return " \\\n".join(lines) + "\n"


def direct_docker_run_snippet(name: str = "vps-2", images: str = DEFAULT_IMAGES, host_network: bool = False) -> str:
    """Gotowe polecenie docker run dla węzła w trybie bezpośrednim / z własnym IP (np. w WireGuard VPN)."""
    images = (images or DEFAULT_IMAGES).strip().rstrip("-")
    net_arg = "--network host" if host_network else "-p 2750:2750"
    lines = [
        "docker run -d --name deployra-backend --restart unless-stopped",
        f"  {net_arg} --memory 192m",
        "  --label deployra.system=backend --log-opt max-size=5m --log-opt max-file=2",
        f'  -e DEPLOYRA_NAME="{name}" -e DEPLOYRA_LANG="{i18n_lang()}"',
        "  -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/deployra:/var/lib/deployra -v /srv/deployra:/srv/deployra",
        f"  {images}-backend:latest",
    ]
    return " \\\n".join(lines) + "\n"


def direct_compose_snippet(name: str = "vps-2", images: str = DEFAULT_IMAGES) -> str:
    """Gotowy compose węzła w trybie bezpośrednim — dla Portainera lub compose.yml."""
    images = (images or DEFAULT_IMAGES).strip().rstrip("-")
    return f"""services:
  deployra-backend:
    image: {images}-backend:latest
    container_name: deployra-backend
    ports:
      - "2750:2750"
    environment:
      DEPLOYRA_NAME: "{name}"
      DEPLOYRA_LANG: "{i18n_lang()}"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /var/lib/deployra:/var/lib/deployra
      - /srv/deployra:/srv/deployra
    restart: unless-stopped
    mem_limit: 192m
    logging: {{ driver: json-file, options: {{ max-size: 5m, max-file: "2" }} }}
"""


def native_snippet(gateway: dict, token: str, name: str, repo: str = "PanS4ge/deployra") -> str:
    """Host Proxmoxa bez Dockera: węzeł jako usługa systemd (widzi kontenery LXC z Dockerem w środku)."""
    url = repo if repo.startswith(("https://", "http://")) else f"https://github.com/{repo}.git"
    return (
        "apt-get update && apt-get install -y git\n"
        f"git clone {url} /opt/deployra\n"
        f'DEPLOYRA_NAME="{name}" DEPLOYRA_GATEWAY="{node_target(gateway)}" \\\n'
        f'  DEPLOYRA_JOIN="{token}" \\\n'
        "  bash /opt/deployra/deploy/install-native.sh\n"
    )
