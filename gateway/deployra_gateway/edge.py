"""Bramka brzegowa („widelec”): ruch publiczny przyjmuje ta bramka, a WireGuard trzyma inna — hub.

Np. Oracle (publiczne 80/443, aplikacje w chmurze) + mikr.us (hub WireGuard, do którego dołączają
domowe serwery). Bramka brzegowa dołącza do huba tokenem z panelu — tak jak węzeł — więc jej nginx
widzi węzły pod adresami 10.66.0.x. Ruch idzie: internet → bramka brzegowa → tunel przez hub → węzeł,
bez drugiego nginx po drodze. Podsieci LAN węzłów (np. 192.168.1.0/24) panel dopisuje przez /api/routes.

Panel paruje się z bramką brzegową sam: sekret parowania jest w tokenie (część, której hub nie zna).
"""

from __future__ import annotations

import ipaddress
import secrets
from typing import Any

from loguru import logger

from deployra_common import pairing, wg
from deployra_common.i18n import tr
from deployra_common.schemas import TunnelConfig


def bootstrap_url(address: str) -> str:
    address = address.strip().rstrip("/")
    return address if address.startswith(("http://", "https://")) else f"http://{address}"


class EdgeTunnel:
    """Tunel bramki brzegowej do huba (szprycha WireGuard) + trasy do podsieci LAN węzłów."""

    def __init__(self, config: Any, state: Any, control: wg.WgControl | None = None) -> None:
        self.config = config
        self.state = state
        self.control = control or wg.WgControl(config.wg_iface)

    @property
    def conf(self) -> dict[str, Any] | None:
        return self.state.get("edge")

    @property
    def address(self) -> str | None:
        return (self.conf or {}).get("address")

    def join(self, post: Any = None) -> dict[str, Any]:
        """Rejestruje klucz WG na hubie. Klucz prywatny powstaje lokalnie; ponowienie z tym samym tokenem jest bezpieczne."""
        token = pairing.JoinToken.parse(self.config.join_token)
        pending = self.state.get("edge_pending_private")
        if not pending:
            pending, _ = wg.genkey()
            self.state.update(edge_pending_private=pending)
        nonce = secrets.token_hex(12)
        request = pairing.seal(token.join_secret, {"kind": "join", "token_id": token.id, "wg_public_key": wg.pubkey(pending),
                                                   "name": self.config.public_host or "edge", "nonce": nonce})  # fmt: skip
        if post is None:
            import httpx

            post = httpx.post
        response = post(bootstrap_url(self.config.hub) + "/join", json=request, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(tr("gw.edge.join_rejected", code=response.status_code, error=response.text[:300]))
        data = pairing.unseal(token.join_secret, response.json(), "join-ok")
        if data.get("nonce") != nonce:
            raise RuntimeError(tr("gw.edge.hub_mismatch"))
        tunnel = TunnelConfig(**data["tunnel"])
        conf = {
            "private_key": pending, "address": tunnel.address, "subnet": tunnel.subnet, "hub_public_key": tunnel.hub_public_key,
            "hub_endpoint": tunnel.hub_endpoint, "hub_address": tunnel.hub_address, "keepalive": tunnel.keepalive, "routes": [],
        }  # fmt: skip
        # kod parowania = sekret z tokenu: panel go zna, hub nie — panel sparuje się sam przez tunel
        self.state.update(edge=conf, edge_pending_private=None, edge_token=token.id, pairing_code=token.pair_secret, panel_secret=None)
        logger.success(tr("gw.edge.joined", address=tunnel.address, hub=tunnel.hub_endpoint))
        return conf

    def render(self) -> str:
        conf = self.conf or {}
        prefix = ipaddress.ip_network(conf["subnet"], strict=False).prefixlen
        iface = wg.WgInterface(private_key=conf["private_key"], addresses=[f"{conf['address']}/{prefix}"])
        hub = wg.WgPeer(public_key=conf["hub_public_key"], allowed_ips=[conf["subnet"], *conf.get("routes", [])],
                        endpoint=conf["hub_endpoint"], keepalive=conf.get("keepalive") or 25, comment="Deployra-WG hub")  # fmt: skip
        return wg.render_quick(iface, [hub])

    def up(self) -> bool:
        if not self.conf:
            return False
        if not wg.WgControl.tools_available():
            logger.error(tr("gw.log.no_wg_tools"))
            return False
        self.control.write_conf(self.render())
        try:
            self.control.up()
        except RuntimeError as exc:
            logger.error(str(exc))
            return False
        for route in (self.conf or {}).get("routes", []):
            self.control.add_route(route)
        logger.success(tr("gw.edge.tunnel_up", address=self.address, hub=(self.conf or {}).get("hub_endpoint")))
        return True

    def set_routes(self, subnets: list[str]) -> list[str]:
        """Podsieci LAN za węzłami — ruch do nich idzie tunelem do huba, a hub kieruje go do właściwego węzła."""
        conf = self.conf
        if not conf:
            raise LookupError(tr("gw.edge.not_joined"))
        tunnel = ipaddress.ip_network(conf["subnet"], strict=False)
        routes = []
        for subnet in subnets:
            network = ipaddress.ip_network(subnet, strict=False)
            if network.version != 4 or network.overlaps(tunnel) or network.prefixlen < 8:
                raise ValueError(tr("gw.edge.bad_route", subnet=subnet))
            routes.append(str(network))
        routes = sorted(set(routes))
        if routes == conf.get("routes", []):
            return routes
        self.state.update(edge={**conf, "routes": routes})
        if wg.WgControl.tools_available() and self.control.is_up():
            self.control.write_conf(self.render())
            self.control.syncconf(wg.render_sync(wg.WgInterface(private_key=conf["private_key"], addresses=[]), [
                wg.WgPeer(public_key=conf["hub_public_key"], allowed_ips=[conf["subnet"], *routes], endpoint=conf["hub_endpoint"],
                          keepalive=conf.get("keepalive") or 25),
            ]))  # fmt: skip
            for route in routes:
                self.control.add_route(route)
        return routes

    def status(self) -> dict[str, Any]:
        conf = self.conf or {}
        live = self.control.dump() if conf and self.config.wg_enabled and wg.WgControl.tools_available() else []
        peer = live[0] if live else {}
        return {"address": conf.get("address"), "hub_endpoint": conf.get("hub_endpoint"), "routes": conf.get("routes", []),
                "latest_handshake": peer.get("latest_handshake") or None, "rx_bytes": peer.get("rx_bytes", 0),
                "tx_bytes": peer.get("tx_bytes", 0)}  # fmt: skip
