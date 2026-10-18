"""Węzeł za zewnętrznym WireGuardem (np. wg-easy): Deployra generuje konfigurację i sprawdza połączenia, sieć zestawia użytkownik.

Węzeł działa jak bezpośredni (API pod adresem w sieci WG, parowanie kodem), ale panel zna jego sieć: podpowiada compose
z `network_mode`, sprawdza, czy panel widzi API i czy bramka ma trasę do tej sieci (Tunel → Zewnętrzny WireGuard).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Any

import httpx

from deployra_common import wg
from deployra_common.client import RemoteError

from ..db import now
from ..i18n import lang, t
from .sync import DEFAULT_IMAGES

NODE_PORT = 2750
_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def clean(address: str, subnet: str, container: str, proxy_address: str) -> dict[str, str]:
    """Adres węzła w sieci WG, podsieć (domyślnie /24 wokół adresu), kontener WG dla `network_mode` i adres dla bramki."""
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        raise ValueError(t("extnode.bad_address")) from None
    try:
        net = ipaddress.ip_network(subnet.strip() or f"{ip}/{24 if ip.version == 4 else 64}", strict=False)
    except ValueError:
        raise ValueError(t("extnode.bad_subnet")) from None
    if ip not in net:
        raise ValueError(t("extnode.outside_subnet", address=ip, subnet=net))
    container = container.strip()
    if container and not _CONTAINER.match(container):
        raise ValueError(t("extnode.bad_container"))
    proxy = proxy_address.strip()
    if proxy:
        try:
            proxy = str(ipaddress.ip_address(proxy))
        except ValueError:
            raise ValueError(t("extnode.bad_proxy")) from None
    return {"address": str(ip), "subnet": str(net), "container": container, "proxy_address": proxy or str(ip)}


def create(panel: Any, name: str, address: str, subnet: str = "", container: str = "", proxy_address: str = "",
           gateway_id: int | None = None) -> int:  # fmt: skip
    conf = clean(address, subnet, container, proxy_address)
    host = f"[{conf['address']}]" if ":" in conf["address"] else conf["address"]
    return panel.db.insert(
        "nodes", name=name.strip()[:60] or conf["address"], transport="direct", api_url=f"http://{host}:{NODE_PORT}", status="pending",
        proxy_address=conf["proxy_address"], gateway_id=gateway_id or None, external_wg=conf, lan_subnets=[conf["subnet"]], created_at=now(),
    )  # fmt: skip


def compose_snippet(node: dict, images: str = DEFAULT_IMAGES) -> str:
    """Compose węzła w sieci zewnętrznego WireGuarda: wspólny stos sieciowy z kontenerem WG albo sieć hosta."""
    conf = node.get("external_wg") or {}
    images = (images or DEFAULT_IMAGES).strip().rstrip("-")
    if conf.get("container"):
        network = f'network_mode: "container:{conf["container"]}"   # {t("extnode.c_container", address=conf["address"])}'
        bind = ""
    else:
        network = f"network_mode: host                     # {t('extnode.c_host')}"
        bind = f'      DEPLOYRA_API_BIND: "{conf.get("address", "")}"   # {t("extnode.c_bind")}\n'
    return f"""services:
  deployra-backend:
    image: {images}-backend:latest
    container_name: deployra-backend
    {network}
    labels:
      deployra.system: backend
    environment:
      DEPLOYRA_NAME: "{node["name"]}"
      DEPLOYRA_LANG: "{lang()}"
{bind}    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /var/lib/deployra:/var/lib/deployra
      - /srv/deployra:/srv/deployra
    restart: unless-stopped
    mem_limit: 192m
    logging: {{ driver: json-file, options: {{ max-size: 5m, max-file: "2" }} }}
"""


def client_conf(node: dict, text: str) -> str:
    """Plik klienta z wg-easy → wg0.conf węzła: tunelem idzie tylko podsieć WG (reszta ruchu serwera bez zmian), bez DNS i PostUp,
    z keepalive — serwer WG musi móc dojść do węzła za NAT-em, bo to panel i bramka łączą się z węzłem."""
    conf = node.get("external_wg") or {}
    parsed = wg.parse_client_conf(text)
    mine = {str(ipaddress.ip_interface(a).ip) for a in parsed["addresses"] if _is_interface(a)}
    if conf.get("address") and conf["address"] not in mine:
        raise ValueError(t("extnode.conf_other_address", found=", ".join(sorted(mine)) or "—", address=conf["address"]))
    peer = parsed["peer"]
    lines = ["[Interface]", f"PrivateKey = {parsed['private_key']}", f"Address = {', '.join(parsed['addresses'])}"]
    if parsed["mtu"]:
        lines.append(f"MTU = {parsed['mtu']}")
    lines += ["", "[Peer]", f"PublicKey = {peer['public_key']}"]
    if peer["preshared_key"]:
        lines.append(f"PresharedKey = {peer['preshared_key']}")
    lines += [f"AllowedIPs = {conf.get('subnet')}", f"Endpoint = {peer['endpoint']}", f"PersistentKeepalive = {peer['keepalive']}"]
    return "\n".join(lines) + "\n"


def install_snippet(wg0: str) -> str:
    """Polecenia dla hosta węzła (Debian/Ubuntu/Proxmox): plik z prawami 600 i usługa wg-quick startująca z systemem."""
    return (
        "apt-get install -y wireguard-tools\n"
        "install -m 600 /dev/stdin /etc/wireguard/wg0.conf <<'EOF'\n"
        f"{wg0}EOF\n"
        "systemctl enable --now wg-quick@wg0\n"
        "wg show wg0 latest-handshakes\n"
    )


def _is_interface(value: str) -> bool:
    try:
        ipaddress.ip_interface(value)
        return True
    except ValueError:
        return False


def _covered(routes: list[str], address: str) -> bool:
    ip = ipaddress.ip_address(address)
    for route in routes:
        try:
            if ip in ipaddress.ip_network(route, strict=False):
                return True
        except ValueError:
            continue
    return False


async def check(panel: Any, node: dict) -> dict[str, Any]:
    """Czy panel widzi API węzła i czy bramka węzła ma tunel z trasą do jego adresu (dla ruchu do aplikacji)."""
    conf = node.get("external_wg") or {}
    result: dict[str, Any] = {"panel": False, "panel_error": None, "gateway": None}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{panel.node_url(node)}/health")
        result["panel"] = response.status_code < 500
        if not result["panel"]:
            result["panel_error"] = f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        result["panel_error"] = type(exc).__name__
    gateway = panel.gateway_by_id(node.get("gateway_id")) if node.get("gateway_id") else panel.edge()
    client = panel.client_for_gateway(gateway) if gateway else None
    if client:
        # bramka sama sprawdza, czy dochodzi do węzła — działa też, gdy stoi obok serwera WG i nie potrzebuje własnego tunelu
        info: dict[str, Any] = {"name": gateway.get("name") or gateway.get("public_host"), "ok": False, "error": None, "tunnel": None,
                                "online": False}  # fmt: skip
        try:
            probe = await asyncio.wait_for(client.post("/api/check", json={"url": f"{panel.node_url(node)}/health"}), 15)
            info["ok"] = bool(probe.get("ok"))
            info["error"] = None if info["ok"] else probe.get("error") or (f"HTTP {probe['status']}" if probe.get("status") else None)
        except RemoteError as exc:
            info["error"] = exc.detail
        except (httpx.HTTPError, asyncio.TimeoutError):
            info["error"] = t("msg.gateway_down_hint")
        if not info["ok"]:  # podpowiedź: czy bramka ma w ogóle tunel z trasą do tej sieci
            try:
                tunnels = (await asyncio.wait_for(client.get("/api/external-wg"), 10))["tunnels"]
                target = conf.get("proxy_address") or conf.get("address")
                match = next((tun for tun in tunnels if target and _covered(tun["routes"], target)), None)
                if match:
                    info.update(tunnel=match["name"], online=bool(match.get("online")))
            except (RemoteError, httpx.HTTPError, asyncio.TimeoutError):
                pass  # starsza bramka bez zewnętrznego WG — zostaje sam wynik testu
        result["gateway"] = info
    return result
