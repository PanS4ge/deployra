"""Bany adresów IP: lista w stanie bramki, egzekwowana w nginx i (gdy jest ipset) w firewallu.

nginx: zmienna `geo $deployra_ban` → strona 403 w każdym serwerze (`return 418` → `@deployra_banned`) (działa też dla ruchu z Cloudflare,
bo realip podmienia adres na prawdziwy), `deny` w streamach. Firewall: zestawy ipset `deployra-ban`
i `deployra-ban6` z regułą DROP w INPUT — odcina zalew połączeń, zanim dotknie nginx.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
import time
from typing import Any

from loguru import logger

from deployra_common.i18n import tr

from .state import State

DEFAULT_SETTINGS = {
    "auto": False,
    "rate": 600,
    "minutes": 60,
    "err404_threshold": 15,
    "probe_threshold": 5,
    "ban_404": True,
    "ban_probes": True,
    "allowlist": [],
}
SETS = (("deployra-ban", "inet", "iptables", 4), ("deployra-ban6", "inet6", "ip6tables", 6))

CLOUDFLARE_NETWORKS: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)


def normalize(target: str, protected: list[str] | None = None) -> str:
    """Adres albo podsieć do zbanowania; odrzuca adresy prywatne, tunel i zbyt szerokie zakresy."""
    try:
        net = ipaddress.ip_network(str(target).strip(), strict=False)
    except ValueError:
        raise ValueError(tr("gw.ban.bad_target", target=target)) from None
    if not net.is_global:
        raise ValueError(tr("gw.ban.private", target=net))
    if net.prefixlen < (16 if net.version == 4 else 32):
        raise ValueError(tr("gw.ban.too_wide", target=net))
    for value in protected or []:
        other = ipaddress.ip_network(value, strict=False)
        if other.version == net.version and net.overlaps(other):
            raise ValueError(tr("gw.ban.protected", target=net))
    return str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net)


class Bans:
    def __init__(self, state: State, protected: list[str] | None = None) -> None:
        self.state = state
        self.protected = list(protected or []) + list(CLOUDFLARE_NETWORKS)

    @property
    def settings(self) -> dict[str, Any]:
        return {**DEFAULT_SETTINGS, **(self.state.get("ban_settings") or {})}

    def update_settings(
        self,
        auto: bool,
        rate: int,
        minutes: int,
        allowlist: list[str],
        err404_threshold: int = 15,
        probe_threshold: int = 5,
        ban_404: bool = True,
        ban_probes: bool = True,
    ) -> dict[str, Any]:
        clean = []
        for item in allowlist:
            try:
                clean.append(str(ipaddress.ip_network(item.strip(), strict=False)))
            except ValueError:
                raise ValueError(tr("gw.ban.bad_target", target=item)) from None
        settings = {
            "auto": bool(auto),
            "rate": max(60, min(int(rate), 100_000)),
            "minutes": max(1, min(int(minutes), 43_200)),
            "err404_threshold": max(3, min(int(err404_threshold), 10_000)),
            "probe_threshold": max(1, min(int(probe_threshold), 1_000)),
            "ban_404": bool(ban_404),
            "ban_probes": bool(ban_probes),
            "allowlist": clean,
        }
        self.state.update(ban_settings=settings)
        return settings

    def list(self) -> list[dict[str, Any]]:
        now = time.time()
        items = [{"target": target, **entry} for target, entry in (self.state.get("bans") or {}).items()]
        return sorted((i for i in items if not i.get("expires") or i["expires"] > now), key=lambda i: i.get("created", 0), reverse=True)

    def nets(self) -> list[str]:
        return [item["target"] for item in self.list()]

    def is_banned(self, ip: str) -> bool:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(address in ipaddress.ip_network(net, strict=False) for net in self.nets())

    def allowed(self, ip: str) -> bool:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if any(address in ipaddress.ip_network(net, strict=False) for net in CLOUDFLARE_NETWORKS):
            return True
        return any(address in ipaddress.ip_network(net, strict=False) for net in self.settings["allowlist"])

    def add(self, target: str, minutes: int | None = None, reason: str = "", auto: bool = False) -> dict[str, Any]:
        net = normalize(target, self.protected)
        now = time.time()
        entry = {"reason": reason[:200], "created": now, "expires": now + minutes * 60 if minutes else None, "auto": auto}
        self.state.mutate("bans", lambda bans: {**(bans or {}), net: entry})
        return {"target": net, **entry}

    def remove(self, target: str) -> bool:
        try:
            net = normalize(target)
        except ValueError:
            net = target.strip()
        existed = net in (self.state.get("bans") or {})
        self.state.mutate("bans", lambda bans: {k: v for k, v in (bans or {}).items() if k != net})
        return existed

    def expire(self, now: float | None = None) -> bool:
        now = now or time.time()
        bans = self.state.get("bans") or {}
        alive = {k: v for k, v in bans.items() if not v.get("expires") or v["expires"] > now}
        if len(alive) != len(bans):
            self.state.update(bans=alive)
            return True
        return False


class BanWall:
    """Zestawy ipset + reguła DROP. Bez ipset (dev, Windows) — nic nie robi, zostaje warstwa nginx."""

    def __init__(self, enabled: bool = True) -> None:
        self.available = enabled and shutil.which("ipset") is not None and shutil.which("iptables") is not None
        self._rules_ok = False

    def sync(self, nets: list[str]) -> bool:
        if not self.available:
            return False
        try:
            for name, family, iptables, version in SETS:
                members = [n for n in nets if ipaddress.ip_network(n, strict=False).version == version]
                tmp = f"{name}-new"
                script = [f"create {name} hash:net family {family} -exist", f"create {tmp} hash:net family {family} -exist", f"flush {tmp}"]
                script += [f"add {tmp} {member} -exist" for member in members]
                script += [f"swap {tmp} {name}", f"destroy {tmp}"]
                subprocess.run(["ipset", "restore"], input="\n".join(script) + "\n", text=True, check=True, capture_output=True, timeout=30)
                if not self._rules_ok and shutil.which(iptables):
                    rule = ["INPUT", "-m", "set", "--match-set", name, "src", "-j", "DROP"]
                    if subprocess.run([iptables, "-C", *rule], capture_output=True, timeout=10).returncode != 0:
                        subprocess.run([iptables, "-I", *rule], check=True, capture_output=True, timeout=10)
            self._rules_ok = True
            return True
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning(tr("gw.log.banwall_failed", error=getattr(exc, "stderr", None) or exc))
            return False
