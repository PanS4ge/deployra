"""Mapa infrastruktury (force-graph): panel, bramki, węzły, kontenery LXC, aplikacje i domeny."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..context import Panel, get_panel
from ..deps import current_user
from ..i18n import t
from ..services import topology
from ..web import render

router = APIRouter(dependencies=[Depends(current_user)])

TYPES = ("panel", "gateway", "node", "ct", "app", "site")
STATUSES = ("running", "deploying", "error", "stopped", "pending", "paired", "joined", "new", "ok")


@router.get("/map")
def map_page(request: Request):
    labels = {
        "type": {k: t(f"map.type.{k}") for k in TYPES},
        "role": {k: t(f"map.role.{k}") for k in ("hub", "edge", "proxy")},
        "status": {**{k: t(f"status.{k}") for k in STATUSES}, "off": t("map.off")},
    }
    return render(request, "map.html", title=t("map.title"), types=TYPES[1:], labels=labels)


@router.get("/map/data")
def map_data(panel: Panel = Depends(get_panel)) -> JSONResponse:
    return JSONResponse(topology.build(panel), headers={"Cache-Control": "no-store"})
