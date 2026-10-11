"""Uptime: monitory aplikacji, kontenerów, węzłów i własnych URL (w stylu Uptime Kuma)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ..context import Panel, get_panel
from ..db import now
from ..deps import current_user, node_or_404, require
from ..i18n import t
from ..permissions import Permission
from ..services import uptime
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])


def _monitor(monitor_id: int, panel: Panel = Depends(get_panel)) -> dict:
    row = panel.db.get("monitors", monitor_id)
    if not row:
        raise HTTPException(404, t("msg.no_monitor"))
    return row


@router.get("/uptime")
def uptime_page(request: Request, panel: Panel = Depends(get_panel)):
    items = uptime.all_stats(panel)
    template = "partials/uptime_list.html" if request.headers.get("HX-Target") == "uptime-list" else "uptime.html"
    return render(request, template, title="Uptime", items=items)


@router.post("/uptime/url")
def add_url(request: Request, name: str = Form(...), url: str = Form(...), keyword: str = Form(""), interval: int = Form(60),
            panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):  # fmt: skip
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return toast(t("msg.url_scheme"), "err", status=400)
    panel.db.insert("monitors", name=name.strip()[:120] or url, kind="url", target={"url": url, "keyword": keyword.strip() or None},
                    interval_s=max(20, min(interval, 3600)), enabled=1, status="unknown", created_at=now())  # fmt: skip
    return redirect(request, "/uptime", t("msg.monitor_added"))


@router.post("/uptime/{monitor_id}/toggle")
def toggle(request: Request, monitor: dict = Depends(_monitor), panel: Panel = Depends(get_panel),
           user: dict = Depends(require(Permission.ALLOW_SETTINGS))):  # fmt: skip
    panel.db.update("monitors", monitor["id"], enabled=0 if monitor["enabled"] else 1, status="unknown")
    return redirect(request, "/uptime", "Wstrzymano monitor" if monitor["enabled"] else "Wznowiono monitor")


@router.post("/uptime/{monitor_id}/interval")
def interval(request: Request, seconds: int = Form(...), monitor: dict = Depends(_monitor), panel: Panel = Depends(get_panel),
             user: dict = Depends(require(Permission.ALLOW_SETTINGS))):  # fmt: skip
    panel.db.update("monitors", monitor["id"], interval_s=max(20, min(seconds, 3600)))
    return toast(t("msg.interval_changed"))


@router.post("/uptime/{monitor_id}/delete")
def delete(request: Request, monitor: dict = Depends(_monitor), panel: Panel = Depends(get_panel),
           user: dict = Depends(require(Permission.ALLOW_SETTINGS))):  # fmt: skip
    uptime.remove(panel, monitor["id"])
    return redirect(request, "/uptime", t("msg.monitor_deleted"))


@router.post("/nodes/{node_id}/containers/monitor")
def toggle_container(name: str = Form(...), ep: str = Form("local"), node: dict = Depends(node_or_404), panel: Panel = Depends(get_panel),
                     user: dict = Depends(require(Permission.ALLOW_SETTINGS))):  # fmt: skip
    on = uptime.toggle_container(panel, node, ep, name)
    return toast(t("msg.monitoring_on" if on else "msg.monitoring_off", name=name), extra={"containers-changed": True})
