"""Rekordy DNS z Cloudflare z powiązaniem: rekord → host proxy → aplikacja → kontener na węźle."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Any

from ..i18n import t
from .dns import DnsError

TYPES = ("A", "AAAA", "CNAME", "TXT", "MX")
PROXIABLE = ("A", "AAAA", "CNAME")
TTLS = (1, 60, 300, 3600, 86400)  # 1 = automatycznie (Cloudflare)
CONTAINER_KINDS = ("image", "git", "runtime")  # te mają jeden kontener `app-<slug>`


def ttl_text(seconds: int) -> str:
    if seconds == 1:
        return t("dnsrec.auto")
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds % size == 0:
            return f"{seconds // size} {unit}"
    return f"{seconds} s"


def full_name(name: str, zone: str) -> str:
    """`@` / `app` / `app.example.com` → pełna nazwa w strefie."""
    name = name.strip().lower().rstrip(".")
    if name in ("", "@"):
        return zone
    return name if name == zone or name.endswith("." + zone) else f"{name}.{zone}"


def payload(form: Any, zone: str, kind: str | None = None) -> dict[str, Any]:
    """Dane z formularza → treść rekordu dla Cloudflare (z podstawową walidacją)."""
    kind = (kind or str(form.get("type") or "")).upper()
    if kind not in TYPES:
        raise ValueError(t("dnsrec.bad_type"))
    content = str(form.get("content") or "").strip()
    if not content:
        raise ValueError(t("dnsrec.need_content"))
    if kind in ("A", "AAAA"):
        try:
            address = ipaddress.ip_address(content)
        except ValueError:
            raise ValueError(t("dnsrec.bad_ip", value=content)) from None
        if (address.version == 6) != (kind == "AAAA"):
            raise ValueError(t("dnsrec.bad_ip", value=content))
    ttl = int(form.get("ttl") or 1)
    data: dict[str, Any] = {"type": kind, "content": content, "ttl": ttl if ttl in TTLS else 1}
    if kind in PROXIABLE:
        data["proxied"] = bool(form.get("proxied"))
    if kind == "MX":
        data["priority"] = int(form.get("priority") or 10)
    if "name" in form:
        data["name"] = full_name(str(form.get("name") or ""), zone)
    return data


def _target_text(target: dict | None, nodes: dict[int, dict]) -> str:
    if not target:
        return ""
    if target.get("type") == "node":
        node = nodes.get(int(target.get("node_id") or 0))
        return f"{node['name'] if node else '?'}:{target.get('port')}"
    return f"{target.get('host')}:{target.get('port')}"


def linker(panel: Any) -> Callable[[str, dict], dict[str, Any]]:
    """Funkcja (strefa, rekord) → z czym jest połączony: host proxy, aplikacja, węzeł, kontener."""
    sites = {s["id"]: s for s in panel.db.all("sites")}
    apps = {a["id"]: a for a in panel.db.all("apps")}
    nodes = {n["id"]: n for n in panel.db.all("nodes")}
    by_port = {(a["node_id"], a["host_port"]): a for a in apps.values() if a.get("host_port")}
    managed = {(r["zone_id"], r["record_id"]): r["site_id"] for r in panel.db.all("dns_records")}
    exact: dict[str, dict] = {}
    wildcard: list[tuple[str, dict]] = []
    for site in sites.values():
        for domain in site.get("domains") or []:
            domain = domain.lower()
            if domain.startswith("*."):
                wildcard.append((domain[2:], site))
            else:
                exact.setdefault(domain, site)

    def link(zone_id: str, record: dict) -> dict[str, Any]:
        name = str(record.get("name") or "").lower()
        managed_site = managed.get((zone_id, record.get("id")))
        site = exact.get(name) or next((s for suffix, s in wildcard if name.endswith("." + suffix) and "." not in name[: -len(suffix) - 1]), None)
        site = site or sites.get(managed_site)
        out: dict[str, Any] = {"managed": managed_site is not None or record.get("comment") == "Deployra", "site": site, "app": None,
                               "node": None, "container": None, "target": ""}  # fmt: skip
        if not site:
            return out
        target = site.get("target") or {}
        app = apps.get(site.get("app_id")) or (by_port.get((target.get("node_id"), target.get("port"))) if target.get("type") == "node" else None)
        if site["kind"] == "proxy":
            out["target"] = "→ " + _target_text(target, nodes)
        elif site["kind"] == "redirect":
            out["target"] = "↪ " + str((site.get("redirect") or {}).get("domain") or "")
        else:
            out["target"] = "404"
        if app:
            out["app"], out["node"] = app, nodes.get(app["node_id"])
            if app["kind"] in CONTAINER_KINDS:
                out["container"] = f"app-{app['slug']}"
        elif target.get("type") == "node":
            out["node"] = nodes.get(int(target.get("node_id") or 0))
        return out

    return link


async def gateway_ip(panel: Any) -> str | None:
    from .dns import public_ip

    try:
        return await public_ip(panel)
    except (DnsError, OSError):
        return None


async def known_ips(panel: Any) -> dict[str, list[str]]:
    """IP → nazwy serwerów Deployry (bramka, bramka brzegowa, węzły) — do plakietki przy wartości rekordu."""
    import asyncio
    import socket
    from urllib.parse import urlparse

    out: dict[str, list[str]] = {}

    def add(ip: str | None, label: str) -> None:
        if ip and label not in out.setdefault(ip, []):
            out[ip].append(label)

    async def resolve(host: str | None) -> list[str]:
        host = (host or "").strip().strip("[]")
        if not host:
            return []
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        try:
            infos = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, None), 3)
        except (OSError, asyncio.TimeoutError):
            return []
        return sorted({i[4][0] for i in infos})

    nodes = panel.db.all("nodes")
    # węzły w tunelu: publiczny adres widać jako endpoint WireGuarda na hubie
    try:
        from .sync import gateway_status

        status = await asyncio.wait_for(gateway_status(panel), 5) or {}
    except Exception:  # noqa: BLE001 — bez huba zostają adresy z konfiguracji
        status = {}
    by_peer = {n.get("peer_id"): n for n in nodes if n.get("peer_id")}
    for peer in status.get("peers") or []:
        node = by_peer.get(peer.get("id"))
        endpoint = str(peer.get("endpoint") or "")
        if node and endpoint:
            try:
                ip = str(ipaddress.ip_address(endpoint.rsplit(":", 1)[0].strip("[]")))
            except ValueError:
                continue
            if not ipaddress.ip_address(ip).is_loopback:
                add(ip, node["name"])
    for node in nodes:
        for host in (urlparse(node.get("api_url") or "").hostname, node.get("proxy_address")):
            for ip in await resolve(host):
                add(ip, node["name"])
    gateway, edge = panel.gateway(), panel.edge_row()
    for ip in await resolve((gateway or {}).get("public_host")):
        add(ip, t("dnsrec.gateway"))
    if edge and edge.get("secret"):
        for ip in await resolve(edge.get("public_host")):
            add(ip, t("dnsrec.edge"))
    configured = (panel.db.setting("public_ip") or "").strip()
    if configured:
        add(configured, t("dnsrec.gateway"))
    return out
