"""Tunel: synchronizacja panelu z Deployra-WG i status peerów."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Form, Request

from deployra_common.client import RemoteError

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import sync, wg_peer
from ..web import redirect, render, toast

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


# --- zewnętrzny WireGuard (bramka jako klient wg-easy itp.) ---------------------------------


@router.get("/tunnel/external")
async def external_status(request: Request, panel: Panel = Depends(get_panel)):
    client, tunnels, error = panel.gateway_client(), [], None
    if client:
        try:
            tunnels = (await asyncio.wait_for(client.get("/api/external-wg"), 10))["tunnels"]
        except RemoteError as exc:
            error = t("extwg.old_gateway") if exc.status == 404 else exc.detail
        except (httpx.HTTPError, asyncio.TimeoutError):
            error = t("msg.gateway_down_hint")
    return render(request, "partials/external_wg.html", tunnels=tunnels, error=error)


@router.post("/tunnel/external")
async def external_save(request: Request, name: str = Form(...), config: str = Form(...), routes: str = Form(""), label: str = Form(""),
                        panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_TUNNEL))):  # fmt: skip
    client = panel.gateway_client()
    if not client:
        return toast(t("extwg.no_gateway"), "err", status=400)
    nets = [r for r in re.split(r"[,\s]+", routes) if r]
    name = name.strip()
    try:
        result = await client.put(f"/api/external-wg/{quote(name, safe='')}", json={"config": config, "routes": nets or None, "label": label.strip() or None})
    except RemoteError as exc:
        return toast(t("extwg.old_gateway") if exc.status == 404 else exc.detail, "err", status=400)
    except httpx.HTTPError:
        return toast(t("msg.gateway_down_hint"), "err", status=502)
    panel.notify(t("extwg.notify", user=user["email"], name=name, routes=", ".join(result["routes"])))
    if result.get("error"):
        return redirect(request, "/tunnel", t("extwg.saved_down", name=name, error=result["error"]), "warn")
    message = t("extwg.saved", name=name, routes=", ".join(result["routes"]))
    if result.get("ignored"):
        message += " " + t("extwg.ignored", fields=", ".join(f.upper() for f in result["ignored"]))
    return redirect(request, "/tunnel", message)


@router.post("/tunnel/external/{name}/delete")
async def external_delete(request: Request, name: str, panel: Panel = Depends(get_panel),
                          user: dict = Depends(require(Permission.ALLOW_TUNNEL))):  # fmt: skip
    client = panel.gateway_client()
    if not client:
        return toast(t("extwg.no_gateway"), "err", status=400)
    try:
        await client.delete(f"/api/external-wg/{quote(name, safe='')}")
    except RemoteError as exc:
        return toast(exc.detail, "err", status=400)
    return redirect(request, "/tunnel", t("extwg.removed", name=name), "warn")


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


@router.post("/tunnel/gateway/config")
async def gateway_config(
    request: Request,
    public_host: str = Form(...),
    wg_port: int = Form(51820),
    bootstrap_port: int = Form(51821),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_TUNNEL)),
):
    gw = panel.gateway()
    if not gw:
        return toast(t("msg.no_gateway"), "err", status=400)
    host = public_host.strip().lower()
    if not host or any(c.isspace() or c in "/:@" for c in host.strip("[]")):
        return toast(t("edge.bad_host"), "err", status=400)
    bootstrap = f"http://{host}:{bootstrap_port}"
    panel.db.update("gateway", gw["id"], public_host=host, wg_port=max(1, min(wg_port, 65535)), bootstrap_url=bootstrap)
    return redirect(request, "/tunnel", "Zapisano adres publiczny bramki oraz porty.")


@router.post("/tunnel/gateway/quick-host")
async def quick_gateway_host(
    request: Request,
    host: str = Form(...),
    port: int = Form(51821),
    wg_port: int = Form(51820),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_TUNNEL)),
):
    gw = panel.gateway()
    if not gw:
        return toast(t("msg.no_gateway"), "err", status=400)
    clean_host = host.strip().lower()
    if not clean_host or any(c.isspace() or c in "/:@" for c in clean_host.strip("[]")):
        return toast("Niepoprawny adres IP / domena", "err", status=400)
    bootstrap = f"http://{clean_host}:{port}"
    fields = {"public_host": clean_host, "bootstrap_url": bootstrap}
    if wg_port and 1 <= wg_port <= 65535:
        fields["wg_port"] = wg_port
    panel.db.update("gateway", gw["id"], **fields)
    return toast(f"Zapisano nowy adres bramki: {clean_host}:{port} (WireGuard: {fields.get('wg_port', 51820)})")
