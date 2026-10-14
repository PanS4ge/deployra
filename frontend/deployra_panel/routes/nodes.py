"""Węzły: dodawanie (tunel / bezpośrednio), przegląd, zasoby na żywo, RPS."""

from __future__ import annotations

import asyncio
import random
import time

import httpx
from fastapi import APIRouter, Depends, Form, Request, Response

from deployra_common.client import RemoteError

from ..context import LOCAL_UPSTREAM, Panel, get_panel
from ..deps import current_user, node_or_404, require
from ..i18n import t
from ..permissions import Permission
from ..services import charts, resources, sync
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])

RPS = {"rock": "🪨", "paper": "📜", "scissors": "✂️"}
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


@router.get("/nodes")
def nodes(request: Request, panel: Panel = Depends(get_panel)):
    rows = panel.db.all("nodes")
    pending = any(n["status"] in ("pending", "joined") for n in rows)
    return render(request, "nodes/list.html", title=t("nav.nodes"), nodes=rows, pending=pending)


@router.post("/nodes/refresh")
async def refresh(request: Request, quiet: bool = False, panel: Panel = Depends(get_panel)):
    done = await sync.refresh_pending(panel)
    if done:
        return redirect(request, "/nodes", t("msg.nodes_paired", count=done))
    if quiet:  # odpytywanie w tle ze strony dołączania — bez dymka co 5 s
        return Response(status_code=204)
    return toast(t("msg.node_not_joined"), "info")


@router.get("/nodes/add")
def add_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_ADD_DOCKERS))):
    wg_gateways = panel.wg_gateways()
    has_wg = bool(wg_gateways)
    gateway = wg_gateways[0] if wg_gateways else panel.gateway()
    return render(
        request, "nodes/add.html",
        title=t("nodes.add"),
        gateway=gateway,
        wg_gateways=wg_gateways,
        has_wg=has_wg,
        target=sync.node_target(gateway) if (gateway and has_wg) else None,
    )


@router.post("/nodes/invite")
async def invite(
    request: Request,
    name: str = Form(...),
    lan_subnets: str = Form(""),
    ttl: int = Form(60),
    gateway_id: int | None = Form(None),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_ADD_DOCKERS)),
):
    subnets = [s for s in lan_subnets.replace(",", "\n").splitlines() if s.strip()]
    node_id, token = await sync.invite_node(panel, name, subnets, max(5, min(ttl, 7 * 24 * 60)), gateway_id=gateway_id)
    from ..services import updates

    node = panel.node(node_id)
    gateway = panel.gateway_by_id(node.get("gateway_id")) or panel.gateway()
    images = panel.db.setting("image_prefix") or sync.DEFAULT_IMAGES
    args = (gateway, token, node["name"])
    snippets = {"compose": sync.compose_snippet(*args, images), "shell": sync.shell_snippet(*args, images),
                "run": sync.docker_run_snippet(*args, images), "native": sync.native_snippet(*args, updates.source(panel) or "PanS4ge/deployra")}  # fmt: skip
    return render(request, "nodes/invite.html", title=t("msg.title_join_node", name=node["name"]), node=node, token=token, snippets=snippets,
                  target=sync.node_target(gateway))  # fmt: skip


@router.post("/nodes/direct")
async def add_direct(
    request: Request,
    name: str = Form(""),
    url: str = Form(...),
    code: str = Form(...),
    proxy_address: str = Form(""),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_ADD_DOCKERS)),
):
    node = await sync.add_direct_node(panel, name, url, code, proxy_address)
    return redirect(request, f"/nodes/{node['id']}", t("msg.node_paired", name=node["name"]))


@router.get("/nodes/{node_id}")
async def node_page(request: Request, node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel)):
    gateway = panel.gateway_by_id(node.get("gateway_id")) or panel.gateway()
    has_wg = bool(gateway and gateway.get("wg_enabled", 1) and gateway.get("role") != "proxy")
    target = sync.node_target(gateway) if (gateway and has_wg) else None
    token = request.query_params.get("token")
    snippets = None
    if node["status"] != "paired" and node.get("transport") == "tunnel" and gateway and has_wg:
        if token:
            from ..services import updates
            images = panel.db.setting("image_prefix") or sync.DEFAULT_IMAGES
            args = (gateway, token, node["name"])
            snippets = {
                "compose": sync.compose_snippet(*args, images),
                "shell": sync.shell_snippet(*args, images),
                "run": sync.docker_run_snippet(*args, images),
                "native": sync.native_snippet(*args, updates.source(panel) or "PanS4ge/deployra"),
            }
        else:
            expires = float(node.get("token_expires") or 0)
            if expires < time.time() or request.query_params.get("reinvite") == "1":
                try:
                    token, snippets = await sync.reinvite_node(panel, node)
                except Exception:
                    pass
    return render(request, "nodes/overview.html", title=node["name"], node=node, tab="overview",
                  gateway=gateway, has_wg=has_wg, target=target, snippets=snippets, token=token)


@router.post("/nodes/{node_id}/reinvite")
async def reinvite(
    request: Request,
    node: dict = Depends(node_or_404),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_ADD_DOCKERS)),
):
    if node["status"] == "paired":
        return redirect(request, f"/nodes/{node['id']}", t("msg.node_already_paired", default="Węzeł jest już sparowany."))
    token, _ = await sync.reinvite_node(panel, node)
    return redirect(request, f"/nodes/{node['id']}?token={token}")


@router.get("/nodes/{node_id}/vitals")
async def vitals(request: Request, node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel)):
    data, error = None, None
    if node["status"] != "paired":
        error = t("nodes.not_paired")
    else:
        try:
            data = await asyncio.wait_for(panel.node_client(node).get("/api/vitals"), timeout=8)
        except (RemoteError, httpx.HTTPError, asyncio.TimeoutError) as exc:
            error = getattr(exc, "detail", None) or t("msg.node_not_responding")
    compact = request.query_params.get("compact") == "1"
    return render(request, "partials/vitals.html", node=node, v=data, error=error, compact=compact)


def history_labels(starts: list[float], step: float) -> list[str]:
    fmt = "%H:%M" if step < 3600 else "%d.%m %H:00" if step == 3600 else "%d.%m"
    return [time.strftime(fmt, time.localtime(ts)) for ts in starts]


@router.get("/nodes/{node_id}/resources")
def node_resources(request: Request, range: str = "24h", node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel)):
    span = range if range in resources.RANGES else "24h"
    data = resources.node_history(panel, node["id"], span)
    chart, cut = None, resources.trimmed(data, ("cpu", "ram", "disk"))
    if cut:
        starts, v = cut
        chart = charts.area_chart(history_labels(starts, data["step"]), [
            ("CPU %", v["cpu"], "#8b5cf6", True), ("RAM %", v["ram"], "#22d3ee", False), (t("res.disk") + " %", v["disk"], "#f472b6", False),
        ], height=200, scale_max=100)  # fmt: skip
    latest = resources.LATEST.get(node["id"]) or {}
    apps = {a["slug"]: a for a in panel.db.fetchall("SELECT id, slug, name FROM apps WHERE node_id = ?", [node["id"]])}
    rows = []
    for c in latest.get("containers") or []:
        if c.get("state") != "running":
            continue
        rows.append({**c, "app_row": apps.get(c.get("app") or "")})
    rows.sort(key=lambda c: -(c.get("mem") or 0))
    return render(request, "partials/node_resources.html", node=node, chart=chart, span=span, ranges=list(resources.RANGES),
                  rows=rows[:12], host=latest.get("host") or {}, updated=latest.get("ts"))  # fmt: skip


@router.post("/nodes/{node_id}/info")
async def refresh_info(request: Request, node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel)):
    info = await panel.node_client(node).get("/api/info", params={"refresh": "true"})
    panel.db.update("nodes", node["id"], info=info)
    target = request.headers.get("referer") or f"/nodes/{node['id']}"
    return redirect(request, target, t("msg.node_refreshed"))


@router.post("/nodes/{node_id}/rps")
async def rps(request: Request, choice: str = Form(...), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel)):
    if choice not in RPS:
        choice = random.choice(list(RPS))
    started = time.perf_counter()
    try:
        answer = (await panel.node_client(node).post("/api/rps"))["chosen"]
    except (RemoteError, httpx.HTTPError):
        return render(request, "partials/rps.html", walkover=True)
    elapsed = (time.perf_counter() - started) * 1000
    result = "draw" if answer == choice else ("win" if BEATS[choice] == answer else "lose")
    return render(request, "partials/rps.html", you=RPS[choice], node=RPS[answer], result=result, ms=round(elapsed, 1), walkover=False)


@router.post("/nodes/{node_id}/rename")
def rename(request: Request, name: str = Form(...), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
           user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):  # fmt: skip
    panel.db.update("nodes", node["id"], name=name.strip()[:60] or node["name"])
    return redirect(request, f"/nodes/{node['id']}", t("msg.renamed"))


@router.post("/nodes/{node_id}/address")
def address(request: Request, proxy_address: str = Form(""), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
            user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):  # fmt: skip
    panel.db.update("nodes", node["id"], proxy_address=proxy_address.strip() or LOCAL_UPSTREAM)
    return redirect(request, f"/nodes/{node['id']}/settings", t("msg.address_saved"))


@router.post("/nodes/{node_id}/subnets")
async def subnets(request: Request, lan_subnets: str = Form(""), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                  user: dict = Depends(require(Permission.ALLOW_TUNNEL))):  # fmt: skip
    values = sync._check_subnets([s for s in lan_subnets.replace(",", "\n").splitlines()])
    client = panel.hub_client()
    if node["transport"] != "tunnel" or not client:
        raise LookupError(t("msg.lan_tunnel_only"))
    await client.request("PATCH", f"/api/peers/{node['peer_id']}", json={"lan_subnets": values})
    panel.db.update("nodes", node["id"], lan_subnets=values)
    await sync.push_edge_routes(panel)
    return toast(t("msg.subnets_saved"), "ok")


@router.post("/nodes/{node_id}/repair")
async def repair(request: Request, code: str = Form(...), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(Permission.ALLOW_ADD_DOCKERS))):  # fmt: skip
    await sync.pair_node(panel, node, code)
    return redirect(request, f"/nodes/{node['id']}", t("msg.repaired"))


@router.get("/nodes/{node_id}/ports")
async def node_ports(request: Request, node: dict = Depends(node_or_404), ep: str = "local", panel: Panel = Depends(get_panel)):
    try:
        ports_data = await panel.node_client(node).get("/api/ports")
    except Exception:
        ports_data = {"pool": [20000, 29999], "used": []}
    return render(request, "nodes/ports.html", title=f"{node['name']} · Porty", node=node, tab="ports", ports_data=ports_data)


@router.get("/nodes/{node_id}/proxmox")
async def node_proxmox(request: Request, node: dict = Depends(node_or_404), ep: str = "local", panel: Panel = Depends(get_panel)):
    client = panel.node_client(node)
    try:
        entities = await client.get("/api/proxmox/entities")
    except Exception:
        entities = []
    pve_url = (node.get("info") or {}).get("proxmox_url")
    return render(request, "nodes/proxmox.html", title=f"{node['name']} · Proxmox VE", node=node, tab="proxmox", entities=entities, proxmox_url=pve_url)


@router.post("/nodes/{node_id}/proxmox/{kind}/{vmid}/action/{action}")
async def proxmox_action(request: Request, node_id: int, kind: str, vmid: int, action: str, node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                         user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):
    client = panel.node_client(node)
    await client.post(f"/api/proxmox/{kind}/{vmid}/action/{action}")
    return toast(f"Wykonano: {action} na #{vmid}")


@router.post("/nodes/{node_id}/proxmox/config")
async def proxmox_config(request: Request, node_id: int, url: str = Form(...), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                         user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):
    client = panel.node_client(node)
    await client.post("/api/proxmox/config", json={"url": url.strip()})
    info = node.get("info") or {}
    info["proxmox_url"] = url.strip()
    panel.db.update("nodes", node["id"], info=info)
    return toast("Zapisano adres Proxmox VE")


@router.post("/nodes/{node_id}/delete")
async def delete(request: Request, node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(Permission.ALLOW_DELETE_DOCKERS))):  # fmt: skip
    apps = panel.db.fetchall("SELECT slug FROM apps WHERE node_id = ?", [node["id"]])
    if apps:
        raise LookupError(t("msg.node_has_apps", apps=", ".join(a["slug"] for a in apps)))
    await sync.remove_node(panel, node)
    return redirect(request, "/nodes", t("msg.node_deleted", name=node["name"]))
