"""Tunel węzła (szprycha) do Deployra-WG."""

from __future__ import annotations

import ipaddress
import subprocess

from loguru import logger

from deployra_common import wg


def default_iface() -> str:
    try:
        out = subprocess.run(["ip", "-o", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout
        parts = out.split()
        return parts[parts.index("dev") + 1]
    except (ValueError, IndexError, OSError, subprocess.SubprocessError):
        return "eth0"


def build_config(conf: dict, lan_iface: str | None = None) -> str:
    """Konfiguracja wg-quick węzła. `conf` to zapis `state['wg']`."""
    prefix = ipaddress.ip_network(conf["subnet"], strict=False).prefixlen
    post_up, post_down = [], []
    lan = conf.get("lan_subnets") or []
    if lan:
        out_iface = lan_iface or default_iface()
        post_up += ["sysctl -w net.ipv4.ip_forward=1", "iptables -A FORWARD -i %i -j ACCEPT", "iptables -A FORWARD -o %i -j ACCEPT"]
        post_down += ["iptables -D FORWARD -i %i -j ACCEPT", "iptables -D FORWARD -o %i -j ACCEPT"]
        for subnet in lan:
            net = str(ipaddress.ip_network(subnet, strict=False))
            rule = f"-s {conf['subnet']} -d {net} -o {out_iface} -j MASQUERADE"
            post_up.append(f"iptables -t nat -A POSTROUTING {rule}")
            post_down.append(f"iptables -t nat -D POSTROUTING {rule}")
    iface = wg.WgInterface(
        private_key=conf["private_key"],
        addresses=[f"{conf['address']}/{prefix}"],
        post_up=post_up,
        post_down=post_down,
    )
    hub = wg.WgPeer(
        public_key=conf["hub_public_key"],
        allowed_ips=[conf["subnet"]],
        endpoint=conf["hub_endpoint"],
        keepalive=conf.get("keepalive", 25),
        comment="Deployra-WG",
    )
    return wg.render_quick(iface, [hub])


def bring_up(conf: dict, iface_name: str = "wg0") -> bool:
    if not wg.WgControl.tools_available():
        logger.error("Brak wireguard-tools (wg, wg-quick) — tunel nie wystartuje")
        return False
    control = wg.WgControl(iface_name)
    control.write_conf(build_config(conf))
    try:
        control.up()
    except RuntimeError as exc:
        logger.error(str(exc))
        return False
    logger.success(f"Tunel {iface_name} działa: {conf['address']} ↔ {conf['hub_endpoint']}")
    return True


def status(iface_name: str = "wg0") -> dict | None:
    if not wg.WgControl.tools_available():
        return None
    peers = wg.WgControl(iface_name).dump()
    return peers[0] if peers else None
