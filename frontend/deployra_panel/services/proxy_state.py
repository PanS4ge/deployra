"""Budowanie pożądanego stanu proxy z bazy i wysyłka do Deployra-WG."""

from __future__ import annotations

from typing import Any

from loguru import logger
from pydantic import ValidationError

from deployra_common.schemas import AccessClient, AccessList, AccessUser, ProxyLocation, ProxyState, Site, Stream, Upstream


def resolve_target(panel: Any, target: dict | None, nodes: dict[int, dict]) -> Upstream | None:
    """Cel w bazie → Upstream z adresem widzianym przez bramkę (IP węzła w tunelu)."""
    if not target:
        return None
    if target.get("type") == "node":
        node = nodes.get(int(target.get("node_id") or 0))
        if not node:
            return None
        host = panel.node_upstream_host(node)
        if not host:
            return None
    else:
        host = target.get("host")
    return Upstream(scheme=target.get("scheme", "http"), host=host, port=int(target["port"]), path=target.get("path") or "")


def site_model(panel: Any, row: dict, nodes: dict[int, dict]) -> Site | None:
    try:
        upstream = resolve_target(panel, row.get("target"), nodes) if row["kind"] == "proxy" else None
        if row["kind"] == "proxy" and upstream is None:
            return None
        locations = []
        for loc in row.get("locations") or []:
            loc_up = resolve_target(panel, loc.get("target"), nodes)
            if loc_up:
                locations.append(ProxyLocation(path=loc["path"], upstream=loc_up, advanced=loc.get("advanced") or ""))
        tunnel = row.get("ingress") == "cloudflare"  # HTTPS kończy się na krawędzi Cloudflare
        return Site(
            id=row["id"],
            kind=row["kind"],
            domains=row["domains"],
            upstream=upstream,
            redirect=row.get("redirect"),
            locations=locations,
            cert_id=None if tunnel else row.get("cert_id"),
            ssl_forced=bool(row["ssl_forced"]) and not tunnel,
            http2=bool(row["http2"]),
            hsts=bool(row["hsts"]),
            hsts_subdomains=bool(row["hsts_subdomains"]),
            block_exploits=bool(row["block_exploits"]),
            websockets=bool(row["websockets"]),
            caching=bool(row["caching"]),
            trust_forwarded_proto=bool(row["trust_forwarded_proto"]) or tunnel,
            access_list_id=row.get("access_list_id"),
            advanced=row.get("advanced") or "",
            max_body_mb=row.get("max_body_mb") or 100,
            enabled=bool(row["enabled"]),
        )
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        logger.warning(f"Host #{row.get('id')} pominięty: {exc}")
        return None


def build_state(panel: Any) -> ProxyState:
    db = panel.db
    nodes = {n["id"]: n for n in db.all("nodes")}
    sites = [s for s in (site_model(panel, row, nodes) for row in db.all("sites")) if s]
    streams = []
    for row in db.all("streams"):
        host = row["upstream_host"]
        if row.get("node_id"):
            node = nodes.get(row["node_id"])
            host = panel.node_upstream_host(node) if node else None
        if not host:
            continue
        try:
            streams.append(
                Stream(id=row["id"], listen_port=row["listen_port"], upstream_host=host, upstream_port=row["upstream_port"],
                       tcp=bool(row["tcp"]), udp=bool(row["udp"]), enabled=bool(row["enabled"]))
            )  # fmt: skip
        except ValidationError as exc:
            logger.warning(f"Stream #{row['id']} pominięty: {exc}")
    acls = []
    for row in db.all("access_lists"):
        acls.append(
            AccessList(
                id=row["id"], name=row["name"], satisfy_any=bool(row["satisfy_any"]), pass_auth=bool(row["pass_auth"]),
                users=[AccessUser(**u) for u in row.get("users") or []],
                clients=[AccessClient(**c) for c in row.get("clients") or []],
            )
        )  # fmt: skip
    return ProxyState(sites=sites, streams=streams, access_lists=acls, default_page=db.setting("default_page", "deployra"))


async def push(panel: Any) -> dict:
    """Wysyła pełny stan do bramki. Zwraca {ok, status, message}."""
    client = panel.gateway_client()
    if not client:
        return {"ok": False, "status": "no-gateway", "message": "Brak połączenia z Deployra-WG — konfiguracja zapisana w panelu"}
    state = build_state(panel)
    result = await client.put("/api/proxy", json=state.model_dump(mode="json"))
    if result.get("status") != "rejected" and panel.db.setting("cf_tunnel_id"):
        from . import cloudflared

        try:
            await cloudflared.sync(panel)
        except cloudflared.TunnelError as exc:
            result = {**result, "status": "pending", "ok": False, "message": f"Cloudflare Tunnel: {exc}"}
    return result


def push_message(result: dict) -> tuple[str, str]:
    status = result.get("status")
    if status == "applied":
        return "Zapisano i przeładowano nginx ✔", "ok"
    if status == "written":
        return "Zapisano konfigurację (nginx bez przeładowania)", "ok"
    if status == "pending":
        return f"Zapisano, ale nginx nie odpowiada: {result.get('message')}", "warn"
    if status == "no-gateway":
        return result["message"], "warn"
    return f"nginx odrzucił konfigurację:\n{result.get('message')}", "err"
