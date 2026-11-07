"""Ochrona przed SSRF: adresy, pod które panel nie wyśle żądania z adresu podanego przez użytkownika.

Blokujemy usługi metadanych chmury i link-local (np. 169.254.169.254 na Oracle/AWS/GCP — tam leżą poświadczenia
maszyny). Sieci LAN i localhost zostają dozwolone: to homelab, a powiadomienia (ntfy, Gotify) i monitory często
celują w usługi w tej samej sieci. API węzłów i bramek i tak wymagają podpisu HMAC.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from ..i18n import t

BLOCKED = [
    ipaddress.ip_network(net)
    for net in ("169.254.0.0/16", "0.0.0.0/8", "100.100.100.200/32", "fe80::/10", "fd00:ec2::254/128", "::/128")
]


def _bad(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in BLOCKED)


def _resolve(host: str) -> list[str]:
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None)]
    except (socket.gaierror, UnicodeError, OSError):
        return []  # nie rozwiązuje się — samo żądanie i tak się nie uda


def host_blocked(host: str | None) -> bool:
    host = (host or "").strip("[]").strip().lower()
    if not host:
        return False
    if _bad(host):
        return True
    return any(_bad(a) for a in _resolve(host))


async def check_host(host: str | None) -> None:
    if await asyncio.to_thread(host_blocked, host):
        raise ValueError(t("net.blocked", host=host))


async def check_url(url: str) -> None:
    await check_host(urlsplit(url or "").hostname)


async def httpx_guard(request) -> None:  # noqa: ANN001 — hak zdarzeń httpx (sprawdza też każde przekierowanie)
    await check_host(request.url.host)
