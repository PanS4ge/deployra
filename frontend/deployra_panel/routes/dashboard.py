"""Pulpit i powiadomienia."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Depends, Request

from deployra_common.client import RemoteError

from ..context import Panel, get_panel
from ..deps import current_user
from ..i18n import t
from ..services import alerts
from ..web import render

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/")
async def dashboard(request: Request, panel: Panel = Depends(get_panel)):
    db = panel.db
    apps = db.all("apps")
    nodes = db.all("nodes")
    certs = db.all("certificates")
    gateway = panel.gateway()
    stats = {
        "apps": len(apps),
        "apps_running": sum(1 for a in apps if a["status"] == "running"),
        "apps_error": sum(1 for a in apps if a["status"] == "error"),
        "nodes": len(nodes),
        "nodes_paired": sum(1 for n in nodes if n["status"] == "paired"),
        "sites": db.scalar("SELECT COUNT(*) FROM sites"),
        "certs": len(certs),
        "certs_warn": sum(1 for c in certs if c["status"] in ("expiring", "expired", "error", "missing")),
    }
    onboarding = [
        {"done": bool(gateway), "title": t("onboarding.gateway"), "text": t("onboarding.gateway_text"), "href": "/tunnel", "icon": "diagram-3"},
        {"done": stats["nodes_paired"] > 0, "title": t("nodes.add"), "text": t("onboarding.node_text"), "href": "/nodes/add", "icon": "hdd-network"},
        {"done": stats["apps"] > 0, "title": t("onboarding.app"), "text": t("onboarding.app_text"), "href": "/apps/new", "icon": "rocket-takeoff"},
    ]
    return render(
        request, "dashboard.html", title=t("nav.dashboard"), stats=stats, nodes=nodes, apps=apps[-6:][::-1], gateway=gateway,
        onboarding=onboarding, jobs=panel.jobs.list()[:6], firing=alerts.firing(panel),
    )  # fmt: skip


@router.get("/dashboard/tunnel")
async def tunnel_card(request: Request, panel: Panel = Depends(get_panel)):
    client = panel.hub_client()
    status, error = None, None
    if client:
        try:
            status = await asyncio.wait_for(client.get("/api/status"), timeout=8)
        except (RemoteError, httpx.HTTPError, asyncio.TimeoutError) as exc:
            error = getattr(exc, "detail", None) or t("msg.gateway_not_responding")
    return render(request, "partials/tunnel_card.html", gw=status, error=error, gateway=panel.gateway())


@router.get("/notifications")
def notifications(request: Request, panel: Panel = Depends(get_panel)):
    items = panel.db.fetchall("SELECT * FROM notifications ORDER BY id DESC LIMIT 30")
    panel.db.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")
    return render(request, "partials/notifications.html", items=items)
