"""Hub WireGuard: peery (panel i węzły), przydział adresów, synchronizacja bez zrywania sesji."""

from __future__ import annotations

import ipaddress
import time
from typing import Any

from loguru import logger

from deployra_common import wg
from deployra_common.schemas import TunnelConfig


class Hub:
    def __init__(self, config: Any, state: Any, control: wg.WgControl | None = None) -> None:
        self.config = config
        self.state = state
        self.control = control or wg.WgControl(config.wg_iface)
        if not state.get("hub_private_key"):
            private, _ = wg.genkey()
            state.update(hub_private_key=private)

    @property
    def public_key(self) -> str:
        return wg.pubkey(self.state.get("hub_private_key"))

    def interface(self) -> wg.WgInterface:
        prefix = ipaddress.ip_network(self.config.wg_subnet).prefixlen
        iface = "%i"
        return wg.WgInterface(
            private_key=self.state.get("hub_private_key"),
            addresses=[f"{self.config.hub_address}/{prefix}"],
            listen_port=self.config.wg_port,
            post_up=[
                "sysctl -w net.ipv4.ip_forward=1",
                f"iptables -I FORWARD -i {iface} -o {iface} -j ACCEPT",
            ],
            post_down=[f"iptables -D FORWARD -i {iface} -o {iface} -j ACCEPT"],
        )

    def peers(self) -> list[wg.WgPeer]:
        out = []
        for peer_id, peer in sorted(self.state.get("peers", {}).items()):
            out.append(
                wg.WgPeer(
                    public_key=peer["public_key"],
                    allowed_ips=[f"{peer['address']}/32", *peer.get("lan_subnets", [])],
                    comment=f"{peer['kind']} {peer.get('name', '')} ({peer_id})",
                )
            )
        return out

    def tunnel_config(self, address: str) -> TunnelConfig:
        return TunnelConfig(
            address=address,
            subnet=self.config.wg_subnet,
            hub_public_key=self.public_key,
            hub_endpoint=self.config.hub_endpoint,
            hub_address=self.config.hub_address,
            gateway_api=self.config.api_url,
        )

    def add_peer(self, peer_id: str, kind: str, name: str, public_key: str, lan_subnets: list[str] | None = None) -> str:
        """Dodaje (lub aktualizuje) peera i zwraca jego adres w tunelu."""
        if not wg.valid_key(public_key):
            raise ValueError("zły klucz publiczny WireGuard")
        lan = [str(ipaddress.ip_network(s, strict=False)) for s in lan_subnets or []]
        tunnel = ipaddress.ip_network(self.config.wg_subnet)
        for subnet in lan:
            if ipaddress.ip_network(subnet).overlaps(tunnel):
                raise ValueError(f"podsieć {subnet} nachodzi na podsieć tunelu")

        def change(peers: dict) -> dict:
            for other_id, other in peers.items():
                if other_id != peer_id and other["public_key"] == public_key:
                    raise ValueError("ten klucz jest już przypisany do innego peera")
                if other_id != peer_id:
                    for subnet in lan:
                        for existing in other.get("lan_subnets", []):
                            if ipaddress.ip_network(subnet).overlaps(ipaddress.ip_network(existing)):
                                raise ValueError(f"podsieć {subnet} jest już routowana przez {other.get('name')}")
            current = peers.get(peer_id)
            address = current["address"] if current else wg.allocate_ip(
                self.config.wg_subnet, [p["address"] for p in peers.values()]
            )
            peers[peer_id] = {
                "kind": kind,
                "name": name,
                "public_key": public_key,
                "address": address,
                "lan_subnets": lan,
                "created": current["created"] if current else int(time.time()),
            }
            return peers

        peers = self.state.mutate("peers", change)
        self.sync()
        return peers[peer_id]["address"]

    def update_subnets(self, peer_id: str, lan_subnets: list[str]) -> None:
        peer = self.state.get("peers", {}).get(peer_id)
        if not peer:
            raise KeyError(peer_id)
        self.add_peer(peer_id, peer["kind"], peer["name"], peer["public_key"], lan_subnets)

    def remove_peer(self, peer_id: str) -> None:
        self.state.mutate("peers", lambda peers: {k: v for k, v in peers.items() if k != peer_id})
        self.sync()

    def up(self) -> bool:
        if not self.config.wg_enabled:
            logger.warning("WireGuard wyłączony (DEPLOYRA_WG=off) — tryb deweloperski")
            return False
        if not wg.WgControl.tools_available():
            logger.error("Brak wireguard-tools — hub nie wystartuje")
            return False
        self.control.write_conf(wg.render_quick(self.interface(), self.peers()))
        try:
            self.control.up()
        except RuntimeError as exc:
            logger.error(str(exc))
            return False
        logger.success(f"Hub WireGuard działa: {self.config.hub_address} (UDP {self.config.wg_port})")
        return True

    def sync(self) -> None:
        if not self.config.wg_enabled or not wg.WgControl.tools_available():
            return
        iface, peers = self.interface(), self.peers()
        self.control.write_conf(wg.render_quick(iface, peers))
        if not self.control.is_up():
            self.up()
            return
        self.control.syncconf(wg.render_sync(iface, peers))
        for peer in peers:
            for network in peer.allowed_ips:
                if not ipaddress.ip_network(network).subnet_of(ipaddress.ip_network(self.config.wg_subnet)):
                    self.control.add_route(network)

    def status(self) -> list[dict]:
        live = {p["public_key"]: p for p in self.control.dump()} if self.config.wg_enabled else {}
        now = int(time.time())
        out = []
        for peer_id, peer in sorted(self.state.get("peers", {}).items()):
            info = live.get(peer["public_key"], {})
            handshake = info.get("latest_handshake") or 0
            out.append(
                {
                    "id": peer_id,
                    **{k: peer[k] for k in ("kind", "name", "address", "lan_subnets", "created")},
                    "public_key": peer["public_key"],
                    "endpoint": info.get("endpoint"),
                    "latest_handshake": handshake or None,
                    "handshake_age": now - handshake if handshake else None,
                    "online": bool(handshake and now - handshake < 180),
                    "rx_bytes": info.get("rx_bytes", 0),
                    "tx_bytes": info.get("tx_bytes", 0),
                }
            )
        return out
