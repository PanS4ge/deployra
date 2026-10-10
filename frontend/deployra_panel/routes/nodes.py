"""Węzły: dodawanie (tunel / bezpośrednio), przegląd, zasoby na żywo, RPS."""

from __future__ import annotations

import asyncio
import random
import time

import httpx
from fastapi import APIRouter, Depends, Form, Request

from deployra_common.client import RemoteError

from ..context import Panel, get_panel
from ..deps import current_user, node_or_404, require
from ..permissions import Permission
from ..services import sync
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])

RPS = {"rock": "🪨", "paper": "📜", "scissors": "✂️"}
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


@router.get("/nodes")
def nodes(request: Request, panel: Panel = Depends(get_panel)):
    rows = panel.db.all("nodes")
    pending = any(n["status"] in ("pending", "joined") for n in rows)
    return render(request, "nodes/list.html", title="Węzły", nodes=rows, pending=pending)


@router.post("/nodes/refresh")
async def refresh(request: Request, panel: Panel = Depends(get_panel)):
    done = await sync.refresh_pending(panel)
    if done:
        return redirect(request, "/nodes", f"Sparowano węzły: {done}")
    return toast("Węzeł jeszcze nie dołączył", "info")


@router.get("/nodes/add")
def add_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_ADD_DOCKERS))):
    return render(request, "nodes/add.html", title="Dodaj węzeł", gateway=panel.gateway())


@router.post("/nodes/invite")
async def invite(
    request: Request,
    name: str = Form(...),
    lan_subnets: str = Form(""),
    ttl: int = Form(60),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_ADD_DOCKERS)),
):
    subnets = [s for s in lan_subnets.replace(",", "\n").splitlines() if s.strip()]
    node_id, token = await sync.invite_node(panel, name, subnets, max(5, min(ttl, 7 * 24 * 60)))
    gateway = panel.gateway()
    node = panel.node(node_id)
    snippet = sync.compose_snippet(gateway, token, node["name"], panel.db.setting("backend_image", "deployra-backend:latest"))
    return render(request, "nodes/invite.html", title=f"Dołącz węzeł {node['name']}", node=node, token=token, snippet=snippet, gateway=gateway)


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
    return redirect(request, f"/nodes/{node['id']}", f"Sparowano węzeł {node['name']} ✔")


@router.get("/nodes/{node_id}")
def node_page(request: Request, node: dict = Depends(node_or_404)):
    return render(request, "nodes/overview.html", title=node["name"], node=node, tab="overview")


@router.get("/nodes/{node_id}/vitals")
async def vitals(request: Request, node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel)):
    data, error = None, None
    if node["status"] != "paired":
        error = "Węzeł nie jest jeszcze sparowany"
    else:
        try:
            data = await asyncio.wait_for(panel.node_client(node).get("/api/vitals"), timeout=8)
        except (RemoteError, httpx.HTTPError, asyncio.TimeoutError) as exc:
            error = getattr(exc, "detail", None) or "węzeł nie odpowiada"
    compact = request.query_params.get("compact") == "1"
    return render(request, "partials/vitals.html", node=node, v=data, error=error, compact=compact)


@router.post("/nodes/{node_id}/info")
async def refresh_info(request: Request, node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel)):
    info = await panel.node_client(node).get("/api/info")
    panel.db.update("nodes", node["id"], info=info)
    return redirect(request, f"/nodes/{node['id']}", "Odświeżono informacje o węźle")


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
    return redirect(request, f"/nodes/{node['id']}", "Zmieniono nazwę")


@router.post("/nodes/{node_id}/subnets")
async def subnets(request: Request, lan_subnets: str = Form(""), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                  user: dict = Depends(require(Permission.ALLOW_TUNNEL))):  # fmt: skip
    values = sync._check_subnets([s for s in lan_subnets.replace(",", "\n").splitlines()])
    client = panel.gateway_client()
    if node["transport"] != "tunnel" or not client:
        raise LookupError("routing podsieci LAN działa tylko dla węzłów w tunelu")
    await client.request("PATCH", f"/api/peers/{node['peer_id']}", json={"lan_subnets": values})
    panel.db.update("nodes", node["id"], lan_subnets=values)
    return toast("Zapisano podsieci. Na węźle zrestartuj kontener backendu, żeby włączyć NAT dla LAN.", "ok")


@router.post("/nodes/{node_id}/repair")
async def repair(request: Request, code: str = Form(...), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(Permission.ALLOW_ADD_DOCKERS))):  # fmt: skip
    await sync.pair_node(panel, node, code)
    return redirect(request, f"/nodes/{node['id']}", "Sparowano ponownie ✔")


@router.post("/nodes/{node_id}/delete")
async def delete(request: Request, node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(Permission.ALLOW_DELETE_DOCKERS))):  # fmt: skip
    apps = panel.db.fetchall("SELECT slug FROM apps WHERE node_id = ?", [node["id"]])
    if apps:
        raise LookupError("Na węźle są aplikacje: " + ", ".join(a["slug"] for a in apps) + " — usuń je najpierw")
    await sync.remove_node(panel, node)
    return redirect(request, "/nodes", f"Usunięto węzeł {node['name']}")
