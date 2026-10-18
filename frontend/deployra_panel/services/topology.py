"""Mapa infrastruktury: panel → bramki → węzły → (CT) → aplikacje → domeny, jako graf dla force-graph."""

from __future__ import annotations

from typing import Any

from ..context import LOCAL_UPSTREAM


def _label(domains: Any) -> str:
    items = [d for d in (domains or []) if d]
    if not items:
        return "—"
    return items[0] + (f" +{len(items) - 1}" if len(items) > 1 else "")


def build(panel: Any) -> dict[str, list[dict[str, Any]]]:
    """Węzły grafu ({id, type, label, status, url}) i połączenia ({source, target, kind}); kind: manage | tunnel | host | traffic."""
    from . import deploy

    nodes: list[dict[str, Any]] = [{"id": "panel", "type": "panel", "label": "Deployra", "status": "ok", "url": "/"}]
    links: list[dict[str, Any]] = []
    known: set[str] = {"panel"}

    def add(node: dict[str, Any]) -> None:
        if node["id"] not in known:
            known.add(node["id"])
            nodes.append(node)

    def link(source: str, target: str, kind: str) -> None:
        if source in known and target in known:
            links.append({"source": source, "target": target, "kind": kind})

    # bramki: hub łączy się z panelem, brzegowa z hubem (ruch publiczny → WireGuard → hub)
    gateways = panel.gateways()
    hub = panel.gateway()
    for gw in gateways:
        role = gw.get("role") or "hub"
        add({"id": f"gw-{gw['id']}", "type": "gateway", "role": role, "label": gw.get("name") or gw.get("public_host") or f"#{gw['id']}",
             "host": gw.get("public_host"), "status": gw.get("status") or "paired", "url": "/tunnel"})  # fmt: skip
    for gw in gateways:
        if gw.get("role") == "edge" and hub and hub["id"] != gw["id"]:
            link(f"gw-{hub['id']}", f"gw-{gw['id']}", "tunnel")
        else:
            link("panel", f"gw-{gw['id']}", "manage")

    def public_gateway(gw_id: Any) -> str | None:
        """Bramka, przez którą wchodzi ruch: przypisana, inaczej brzegowa, inaczej hub."""
        if gw_id and f"gw-{gw_id}" in known:
            return f"gw-{gw_id}"
        edge = panel.edge()
        return f"gw-{edge['id']}" if edge else None

    # węzły: przez tunel do swojej bramki (albo huba), bezpośrednie i węzeł obok panelu — wprost z panelu
    for node in panel.db.all("nodes"):
        nid = f"node-{node['id']}"
        add({"id": nid, "type": "node", "label": node["name"], "status": node["status"], "transport": node["transport"],
             "url": f"/nodes/{node['id']}"})  # fmt: skip
        if node["transport"] == "tunnel" and node.get("proxy_address") != LOCAL_UPSTREAM:
            parent = f"gw-{node['gateway_id']}" if node.get("gateway_id") and f"gw-{node['gateway_id']}" in known else (f"gw-{hub['id']}" if hub else "panel")
            link(parent, nid, "tunnel" if parent != "panel" else "manage")
        elif node.get("external_wg"):  # zewnętrzny WireGuard: bramka sięga węzła tunelem wg-easy
            link(public_gateway(node.get("gateway_id")) or "panel", nid, "tunnel")
        else:
            link("panel", nid, "manage")

    # aplikacje: na węźle albo w kontenerze LXC węzła (Proxmox)
    for app in panel.db.all("apps"):
        aid, nid = f"app-{app['id']}", f"node-{app['node_id']}"
        try:
            endpoint = deploy.load(panel, app)[0].endpoint
        except Exception:  # noqa: BLE001 — uszkodzona specyfikacja nie psuje całej mapy
            endpoint = "local"
        parent = nid
        if endpoint.startswith("ct-"):
            parent = f"ct-{app['node_id']}-{endpoint[3:]}"
            add({"id": parent, "type": "ct", "label": f"CT {endpoint[3:]}", "status": "ok", "url": f"/nodes/{app['node_id']}"})
            link(nid, parent, "host")
        add({"id": aid, "type": "app", "label": app["name"], "status": app["status"], "kind": app["kind"], "app_id": app["id"],
             "url": f"/apps/{app['id']}"})  # fmt: skip
        link(parent, aid, "host")

    # hosty proxy: przy aplikacji, a ręczne — przy bramce, która je obsługuje
    for site in panel.db.all("sites"):
        if site.get("archived"):
            continue
        sid = f"site-{site['id']}"
        add({"id": sid, "type": "site", "label": _label(site.get("domains")), "status": "ok" if site.get("enabled") else "off",
             "kind": site.get("kind"), "url": f"/proxy/hosts/{site['id']}"})  # fmt: skip
        owner = f"app-{site['app_id']}" if site.get("app_id") and f"app-{site['app_id']}" in known else public_gateway(site.get("gateway_id"))
        if owner:
            link(owner, sid, "traffic")
    return {"nodes": nodes, "links": links}
