"""Cloudflare Tunnel (cloudflared) jako alternatywne wejście ruchu na Deployra-WG.

Panel przez API Cloudflare tworzy zdalnie zarządzany tunel, ustawia jego
ingress (hosty z `ingress = cloudflare` → http://localhost:80, czyli nginx
bramki) i rekordy CNAME `<host> → <tunnel>.cfargotunnel.com`. Bramka uruchamia
`cloudflared tunnel run` z tokenem tunelu. Nie trzeba publicznego IP ani
otwartych portów 80/443 — HTTPS kończy się na krawędzi Cloudflare.

Token API potrzebuje dodatkowo uprawnienia „Account · Cloudflare Tunnel · Edit”.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..db import now
from . import dns

SERVICE = "http://localhost:80"


class TunnelError(Exception):
    pass


def state(panel: Any) -> dict[str, Any]:
    s = panel.db.settings()
    return {
        "enabled": bool(s.get("cf_tunnel_id")),
        "tunnel_id": s.get("cf_tunnel_id"),
        "account_id": s.get("cf_account_id"),
        "synced_at": s.get("cf_tunnel_synced_at"),
        "hosts": [d for site in panel.db.all("sites") if site.get("ingress") == "cloudflare" and site["enabled"] for d in site["domains"]],
    }


async def _account_id(cf: dns.Cloudflare) -> str:
    zones = await cf.zones()
    if not zones:
        raise TunnelError("token nie widzi żadnej strefy — nie da się ustalić konta Cloudflare")
    return zones[0]["account"]["id"]


async def enable(panel: Any) -> dict:
    cf = dns.client(panel)
    if not cf:
        raise TunnelError("najpierw dodaj token API Cloudflare")
    gateway = panel.gateway_client()
    if not gateway:
        raise TunnelError("cloudflared uruchamia Deployra-WG — najpierw połącz bramkę")
    try:
        account = panel.db.setting("cf_account_id") or await _account_id(cf)
        tunnel_id = panel.db.setting("cf_tunnel_id")
        if not tunnel_id:
            created = await cf._call("POST", f"/accounts/{account}/cfd_tunnel", json={"name": "deployra", "config_src": "cloudflare"})
            tunnel_id = created["id"]
        token = await cf._call("GET", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/token")
    finally:
        await cf.aclose()
    panel.db.set_setting("cf_account_id", account)
    panel.db.set_setting("cf_tunnel_id", tunnel_id)
    await gateway.put("/api/cloudflared", json={"token": token})
    await sync(panel)
    panel.notify("Cloudflare Tunnel uruchomiony na Deployra-WG", "ok")
    return state(panel)


async def disable(panel: Any) -> None:
    gateway = panel.gateway_client()
    if gateway:
        try:
            await gateway.put("/api/cloudflared", json={"token": None})
        except Exception:  # noqa: BLE001 — bramka może być offline, ustawienia i tak czyścimy
            pass
    panel.db.set_setting("cf_tunnel_id", None)


async def sync(panel: Any) -> list[str]:
    """Ustawia ingress tunelu i rekordy CNAME dla hostów z wejściem przez Cloudflare."""
    info = state(panel)
    if not info["enabled"]:
        return []
    cf = dns.client(panel)
    if not cf:
        raise TunnelError("brak tokenu Cloudflare")
    rules = [{"hostname": host, "service": SERVICE, "originRequest": {"httpHostHeader": host}} for host in info["hosts"]]
    rules.append({"service": "http_status:404"})
    done = []
    try:
        await cf._call(
            "PUT",
            f"/accounts/{info['account_id']}/cfd_tunnel/{info['tunnel_id']}/configurations",
            json={"config": {"ingress": rules}},
        )
        target = f"{info['tunnel_id']}.cfargotunnel.com"
        for site in panel.db.all("sites"):
            if site.get("ingress") != "cloudflare" or not site["enabled"]:
                continue
            for host in site["domains"]:
                zone = await cf.zone_for(host.removeprefix("*."))
                existing = await cf._call("GET", f"/zones/{zone['id']}/dns_records", params={"name": host})
                payload = {"type": "CNAME", "name": host, "content": target, "proxied": True, "ttl": 1, "comment": "Deployra tunnel"}
                conflicting = [r for r in existing if r["type"] in ("A", "AAAA", "CNAME")]
                if conflicting:
                    record = await cf._call("PUT", f"/zones/{zone['id']}/dns_records/{conflicting[0]['id']}", json=payload)
                else:
                    record = await cf._call("POST", f"/zones/{zone['id']}/dns_records", json=payload)
                if not panel.db.fetchone("SELECT id FROM dns_records WHERE site_id = ? AND domain = ?", [site["id"], host]):
                    panel.db.insert("dns_records", site_id=site["id"], domain=host, zone_id=zone["id"], record_id=record["id"], created_at=now())
                done.append(host)
    except (dns.DnsError, httpx.HTTPError) as exc:
        raise TunnelError(str(exc)) from exc
    finally:
        await cf.aclose()
    panel.db.set_setting("cf_tunnel_synced_at", now())
    return done
