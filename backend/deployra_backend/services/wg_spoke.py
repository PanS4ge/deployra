"""Tunel węzła (szprycha) do Deployra-WG."""

from __future__ import annotations

import ipaddress
import subprocess
from pathlib import Path

from loguru import logger

from deployra_common import wg
from deployra_common.i18n import tr


def default_iface() -> str:
    try:
        out = subprocess.run(["ip", "-o", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout
        parts = out.split()
        return parts[parts.index("dev") + 1]
    except (ValueError, IndexError, OSError, subprocess.SubprocessError):
        return "eth0"


def nat_rule(conf: dict, subnet: str) -> list[str]:
    """MASQUERADE ruchu z tunelu do sieci za węzłem — bez sztywnego interfejsu wyjściowego, więc działa też dla sieci
    za innym interfejsem niż domyślny (np. `wg0` serwera WireGuard na węźle albo most Dockera)."""
    net = str(ipaddress.ip_network(subnet, strict=False))
    return ["-s", str(conf["subnet"]), "-d", net, "-j", "MASQUERADE"]


def build_config(conf: dict, lan_iface: str | None = None) -> str:
    """Konfiguracja wg-quick węzła. `conf` to zapis `state['wg']`. (`lan_iface` zostaje dla zgodności — już nieużywany.)"""
    prefix = ipaddress.ip_network(conf["subnet"], strict=False).prefixlen
    post_up, post_down = [], []
    lan = conf.get("lan_subnets") or []
    if lan:
        post_up += [
            "sysctl -w net.ipv4.ip_forward=1 || true",  # w kontenerze /proc/sys bywa tylko do odczytu
            "iptables -A FORWARD -i %i -j ACCEPT",
            "iptables -A FORWARD -o %i -j ACCEPT",
        ]
        post_down += ["iptables -D FORWARD -i %i -j ACCEPT", "iptables -D FORWARD -o %i -j ACCEPT"]
        for subnet in lan:
            rule = " ".join(nat_rule(conf, subnet))
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
        logger.error(tr("node.log.no_wg_tools"))
        return False
    control = wg.WgControl(iface_name)
    control.write_conf(build_config(conf))
    try:
        control.up()
    except RuntimeError as exc:
        logger.error(str(exc))
        return False
    logger.success(tr("node.log.tunnel_up", iface=iface_name, address=conf["address"], hub=conf["hub_endpoint"]))
    return True


def check_lan(conf: dict, subnets: list[str]) -> list[str]:
    """Podsieci za węzłem: IPv4, nie mniejsze niż /8 i nie nachodzące na sieć tunelu."""
    tunnel = ipaddress.ip_network(conf["subnet"], strict=False)
    out = []
    for value in subnets:
        net = ipaddress.ip_network(str(value).strip(), strict=False)
        if net.version != 4 or net.prefixlen < 8 or net.overlaps(tunnel):
            raise ValueError(tr("node.lan.bad_subnet", subnet=value))
        out.append(str(net))
    return sorted(set(out))


def _ipt(runner, *args: str) -> int:
    try:
        return runner(["iptables", *args], capture_output=True, text=True, timeout=15, check=False).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def apply_lan(old: dict, new: dict, iface_name: str = "wg0", runner=subprocess.run) -> bool:
    """Nowe podsieci za węzłem od razu (bez restartu tunelu): reguły NAT/FORWARD na żywo + plik wg-quick na przyszłość.
    Zwraca True, gdy reguły weszły (False: brak iptables / tunel nie działa — zadziałają po starcie tunelu)."""
    before, after = set(old.get("lan_subnets") or []), set(new.get("lan_subnets") or [])
    if wg.WgControl.tools_available():
        wg.WgControl(iface_name).write_conf(build_config(new))
    if _ipt(runner, "-L", "-n") != 0:
        return False
    if after:
        for direction in ("-i", "-o"):
            if _ipt(runner, "-C", "FORWARD", direction, iface_name, "-j", "ACCEPT") != 0:
                _ipt(runner, "-A", "FORWARD", direction, iface_name, "-j", "ACCEPT")
        try:
            Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
        except OSError:
            pass
    for subnet in sorted(before - after):
        _ipt(runner, "-t", "nat", "-D", "POSTROUTING", *nat_rule(old, subnet))
    ok = True
    for subnet in sorted(after):
        rule = nat_rule(new, subnet)
        if _ipt(runner, "-t", "nat", "-C", "POSTROUTING", *rule) != 0:
            ok &= _ipt(runner, "-t", "nat", "-A", "POSTROUTING", *rule) == 0
    logger.info(tr("node.lan.applied", subnets=", ".join(sorted(after)) or "—"))
    return ok


def status(iface_name: str = "wg0") -> dict | None:
    if not wg.WgControl.tools_available():
        return None
    peers = wg.WgControl(iface_name).dump()
    return peers[0] if peers else None
