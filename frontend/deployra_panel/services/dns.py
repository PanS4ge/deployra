"""Rekordy DNS w Cloudflare (A/AAAA per host zamiast wildcarda)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any

import httpx

from ..db import now

API = "https://api.cloudflare.com/client/v4"


class DnsError(Exception):
    pass


class Cloudflare:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=20, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, path, **kwargs)
        try:
            data = response.json()
        except ValueError:
            raise DnsError(f"Cloudflare: {response.status_code}") from None
        if not data.get("success"):
            errors = "; ".join(e.get("message", "?") for e in data.get("errors", [])) or response.reason_phrase
            raise DnsError(f"Cloudflare: {errors}")
        return data.get("result")

    async def verify(self) -> bool:
        result = await self._call("GET", "/user/tokens/verify")
        return result.get("status") == "active"

    async def zones(self) -> list[dict]:
        return await self._call("GET", "/zones", params={"per_page": 50})

    async def zone_for(self, domain: str) -> dict:
        matches = [z for z in await self.zones() if domain == z["name"] or domain.endswith("." + z["name"])]
        if not matches:
            raise DnsError(f"Brak strefy Cloudflare dla {domain}")
        return max(matches, key=lambda z: len(z["name"]))

    async def upsert(self, domain: str, ip: str, proxied: bool) -> tuple[str, str]:
        zone = await self.zone_for(domain)
        kind = "AAAA" if ipaddress.ip_address(ip).version == 6 else "A"
        existing = await self._call("GET", f"/zones/{zone['id']}/dns_records", params={"name": domain, "type": kind})
        payload = {"type": kind, "name": domain, "content": ip, "ttl": 1, "proxied": proxied, "comment": "Deployra"}
        if existing:
            record = await self._call("PUT", f"/zones/{zone['id']}/dns_records/{existing[0]['id']}", json=payload)
        else:
            record = await self._call("POST", f"/zones/{zone['id']}/dns_records", json=payload)
        return zone["id"], record["id"]

    async def delete(self, zone_id: str, record_id: str) -> None:
        await self._call("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")


def client(panel: Any) -> Cloudflare | None:
    token = panel.sealer.unseal(panel.db.setting("cloudflare_token"))
    return Cloudflare(token) if token else None


async def public_ip(panel: Any) -> str:
    """Publiczny adres bramki dla rekordów DNS (ustawienie lub rozwiązany host bramki)."""
    configured = (panel.db.setting("public_ip") or "").strip()
    if configured:
        return str(ipaddress.ip_address(configured))
    gateway = panel.gateway()
    if not gateway:
        raise DnsError("Brak bramki — ustaw publiczny IP w ustawieniach DNS")
    host = gateway["public_host"].strip("[]")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, socket.AF_INET)
        return infos[0][4][0]


async def ensure_records(panel: Any, site: dict) -> list[str]:
    """Tworzy/aktualizuje rekordy dla domen hosta. Pomija wildcardy. Zwraca opis zmian."""
    cf = client(panel)
    if not cf:
        return []
    try:
        ip = await public_ip(panel)
        proxied = bool(panel.db.setting("cloudflare_proxied", False))
        done = []
        for domain in site["domains"]:
            if domain.startswith("*."):
                continue
            zone_id, record_id = await cf.upsert(domain, ip, proxied)
            if not panel.db.fetchone("SELECT id FROM dns_records WHERE site_id = ? AND domain = ?", [site["id"], domain]):
                panel.db.insert("dns_records", site_id=site["id"], domain=domain, zone_id=zone_id, record_id=record_id, created_at=now())
            done.append(f"{domain} → {ip}")
        return done
    finally:
        await cf.aclose()


async def remove_records(panel: Any, site_id: int) -> None:
    rows = panel.db.fetchall("SELECT * FROM dns_records WHERE site_id = ?", [site_id])
    cf = client(panel)
    try:
        for row in rows:
            if cf:
                try:
                    await cf.delete(row["zone_id"], row["record_id"])
                except (DnsError, httpx.HTTPError):
                    pass
            panel.db.delete("dns_records", row["id"])
    finally:
        if cf:
            await cf.aclose()
