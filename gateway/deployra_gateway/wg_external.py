"""Zewnętrzny WireGuard: bramka jako klient cudzego serwera (wg-easy, MikroTik, router w domu).

Przykład: maszyny domowe w 10.8.0.0/24 za wg-easy na innym VPS-ie — bramka łączy się tam jak zwykły
klient i nginx może kierować ruch prosto na 10.8.0.x, bez Deployry na tych maszynach.

Wklejony plik .conf klienta nie idzie do wg-quick wprost. Bierzemy z niego klucze, adres i serwer, a ruch
przez tunel ograniczamy do podanych podsieci — domyślne `AllowedIPs = 0.0.0.0/0` z wg-easy puściłoby tunelem
cały ruch bramki. DNS, PostUp/PostDown i podobne pola pomijamy (polecenia z cudzego pliku nie wykonujemy).
Konfiguracja leży w stanie bramki (wolumen), więc tunel wstaje sam po restarcie i podmianie kontenera.
"""

from __future__ import annotations

import ipaddress
import re
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from deployra_common import wg
from deployra_common.i18n import tr

NAME = re.compile(r"^wg[a-z0-9_-]{1,13}$")
DEFAULT_ROUTES = ("0.0.0.0/0", "::/0")
KEEPALIVE, IGNORED, parse_client_conf = wg.CLIENT_KEEPALIVE, wg.CLIENT_IGNORED, wg.parse_client_conf  # wspólne z panelem


def default_routes(parsed: dict[str, Any]) -> list[str]:
    """Podsieć z adresu klienta (10.8.0.4/24 → 10.8.0.0/24) + AllowedIPs serwera bez trasy domyślnej."""
    nets = []
    for address in parsed["addresses"]:
        iface = ipaddress.ip_interface(address)
        if iface.network.prefixlen < iface.max_prefixlen:
            nets.append(str(iface.network))
    nets += [str(ipaddress.ip_network(a, strict=False)) for a in parsed["peer"]["allowed_ips"] if a not in DEFAULT_ROUTES]
    return list(dict.fromkeys(nets))


class ExternalTunnels:
    def __init__(self, config: Any, state: Any, control_factory: Callable[[str], wg.WgControl] | None = None) -> None:
        self.config = config
        self.state = state
        self.control_factory = control_factory or (lambda name: wg.WgControl(name))

    def _all(self) -> dict[str, dict]:
        return self.state.get("external_wg", {}) or {}

    def _active(self) -> bool:
        return self.config.wg_enabled and wg.WgControl.tools_available()

    def _check_routes(self, name: str, routes: list[str]) -> list[str]:
        if not routes:
            raise ValueError(tr("gw.ext.no_routes"))
        tunnel = ipaddress.ip_network(self.config.wg_subnet)
        out = []
        for raw in routes:
            net = ipaddress.ip_network(raw.strip(), strict=False)
            if net.prefixlen == 0:
                raise ValueError(tr("gw.ext.default_route", net=net))
            if net.version == tunnel.version and net.overlaps(tunnel):
                raise ValueError(tr("gw.ext.overlap_hub", net=net, subnet=tunnel))
            for peer in (self.state.get("peers", {}) or {}).values():
                for subnet in peer.get("lan_subnets", []):
                    other = ipaddress.ip_network(subnet)
                    if other.version == net.version and other.overlaps(net):
                        raise ValueError(tr("gw.ext.overlap_peer", net=net, name=peer.get("name")))
            for other_name, other in self._all().items():
                if other_name == name:
                    continue
                for subnet in other["routes"]:
                    other_net = ipaddress.ip_network(subnet)
                    if other_net.version == net.version and other_net.overlaps(net):
                        raise ValueError(tr("gw.ext.overlap_ext", net=net, name=other_name))
            out.append(str(net))
        return list(dict.fromkeys(out))

    def put(self, name: str, conf_text: str, routes: list[str] | None = None, label: str | None = None) -> dict[str, Any]:
        """Zapisuje (albo podmienia) tunel i od razu go podnosi. Zwraca wpis stanu z ewentualnym błędem."""
        if not NAME.match(name or "") or name == self.config.wg_iface:
            raise ValueError(tr("gw.ext.bad_name", iface=self.config.wg_iface))
        parsed = parse_client_conf(conf_text)
        # walidacja kluczy i adresów tak, jak zrobi to render
        self._interface(parsed)
        self._peer(name, parsed, ["10.255.255.255/32"])
        chosen = self._check_routes(name, routes if routes else default_routes(parsed))
        label = " ".join(str(label or "").split())[:40] or None  # nazwa serwera po drugiej stronie, np. „Cezary”
        entry = {**parsed, "routes": chosen, "label": label, "created": int(time.time()), "error": None}
        self.state.mutate("external_wg", lambda tunnels: {**(tunnels or {}), name: entry})
        self.apply(name)
        return self._all()[name]

    def remove(self, name: str) -> None:
        if name not in self._all():
            raise KeyError(name)
        if self._active():
            control = self.control_factory(name)
            control.down()
            control.conf_path.unlink(missing_ok=True)
        self.state.mutate("external_wg", lambda tunnels: {k: v for k, v in (tunnels or {}).items() if k != name})

    @staticmethod
    def _interface(entry: dict) -> wg.WgInterface:
        return wg.WgInterface(private_key=entry["private_key"], addresses=entry["addresses"], mtu=entry.get("mtu"), table="off")

    @staticmethod
    def _peer(name: str, entry: dict, routes: list[str]) -> wg.WgPeer:
        peer = entry["peer"]
        return wg.WgPeer(public_key=peer["public_key"], allowed_ips=routes, endpoint=peer["endpoint"], keepalive=peer["keepalive"],
                         preshared_key=peer.get("preshared_key"), comment=f"external {name}")  # fmt: skip

    def apply(self, name: str) -> bool:
        entry = self._all()[name]
        if not self._active():
            return False
        control = self.control_factory(name)
        error = None
        try:
            control.write_conf(wg.render_quick(self._interface(entry), [self._peer(name, entry, entry["routes"])]))
            control.up()  # interfejs o tej nazwie postawiony wcześniej ręcznie zostaje przejęty
            for net in entry["routes"]:
                control.add_route(net)  # `replace` — nadpisuje też stare trasy tej podsieci przez inny interfejs
            logger.success(tr("gw.log.ext_up", name=name, routes=", ".join(entry["routes"])))
        except (RuntimeError, ValueError) as exc:
            error = str(exc)
            logger.error(tr("gw.log.ext_failed", name=name, error=error))
        def remember(tunnels: dict | None) -> dict:
            tunnels = dict(tunnels or {})
            if name in tunnels:
                tunnels[name] = {**tunnels[name], "error": error}
            return tunnels

        self.state.mutate("external_wg", remember)
        return error is None

    def up_all(self) -> None:
        for name in sorted(self._all()):
            self.apply(name)

    def routes(self) -> dict[str, list[str]]:
        return {name: entry["routes"] for name, entry in self._all().items()}

    def status(self) -> list[dict[str, Any]]:
        now = int(time.time())
        active = self._active()
        out = []
        for name, entry in sorted(self._all().items()):
            control = self.control_factory(name)
            live = control.dump() if active else []
            info = live[0] if live else {}
            handshake = info.get("latest_handshake") or 0
            out.append({
                "name": name,
                "label": entry.get("label"),
                "addresses": entry["addresses"],
                "routes": entry["routes"],
                "endpoint": entry["peer"]["endpoint"],
                "live_endpoint": info.get("endpoint"),
                "server_key": entry["peer"]["public_key"],
                "public_key": wg.pubkey(entry["private_key"]),
                "up": bool(live),
                "latest_handshake": handshake or None,
                "handshake_age": now - handshake if handshake else None,
                "online": bool(handshake and now - handshake < 180),
                "rx_bytes": info.get("rx_bytes", 0),
                "tx_bytes": info.get("tx_bytes", 0),
                "ignored": entry.get("ignored", []),
                "error": entry.get("error"),
            })  # fmt: skip
        return out
