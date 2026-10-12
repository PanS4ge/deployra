"""Tunel: synchronizacja panelu z Deployra-WG i status peerów."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Depends, Form, Request

from deployra_common.client import RemoteError

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import sync, wg_peer
from ..web import redirect, render

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/tunnel")
def tunnel(request: Request, panel: Panel = Depends(get_panel)):
    gateway = panel.gateway()
    return render(
        request, "tunnel.html", title=t("tunnel.card_title"), gateway=gateway, wg_available=wg_peer.available(panel),
        colocated=bool(gateway and not gateway.get("panel_address")), edge=panel.edge_row(),
    )  # fmt: skip


@router.get("/tunnel/status")
async def tunnel_status(request: Request, panel: Panel = Depends(get_panel)):
    status, error = None, None
    try:
        await asyncio.wait_for(sync.refresh_pending(panel), timeout=15)  # dokańcza parowanie węzłów i bramki brzegowej
        status = await asyncio.wait_for(sync.gateway_status(panel), timeout=10)
    except (RemoteError, httpx.HTTPError, asyncio.TimeoutError) as exc:
        error = getattr(exc, "detail", None) or t("msg.gateway_down_hint")
    nodes = {n["peer_id"]: n for n in panel.db.all("nodes") if n.get("peer_id")}
    return render(request, "partials/tunnel_status.html", gw=status, error=error, nodes=nodes, gateway=panel.gateway())


@router.post("/tunnel/pair")
async def pair(
    request: Request,
    address: str = Form(...),
    code: str = Form(...),
    colocated: str = Form(""),
    api_override: str = Form(""),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_TUNNEL)),
):
    await sync.pair_gateway(panel, address, code, bool(colocated), api_override)
    return redirect(request, "/tunnel", t("msg.gateway_synced"))


@router.post("/tunnel/edge")
async def edge_invite(request: Request, public_host: str = Form(...), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_TUNNEL))):  # fmt: skip
    _, token = await sync.invite_edge(panel, public_host)
    snippet = sync.edge_snippet(panel.gateway(), public_host.strip().lower(), token)
    return render(request, "partials/edge_invite.html", snippet=snippet, host=public_host.strip().lower())


@router.post("/tunnel/edge/unlink")
async def edge_unlink(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_TUNNEL))):
    await sync.unlink_edge(panel)
    return redirect(request, "/tunnel", t("edge.unlinked"), "warn")


@router.post("/tunnel/unlink")
async def unlink(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_TUNNEL))):
    await sync.unlink_gateway(panel)
    return redirect(request, "/tunnel", t("msg.gateway_unlinked"), "warn")
