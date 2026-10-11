"""Ruch i bezpieczeństwo: topka adresów IP (kraj, ISP), podejrzane adresy i bany na Deployra-WG."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Depends, Form, Request

from deployra_common.client import RemoteError

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..web import render, toast

router = APIRouter(dependencies=[Depends(current_user)])
PROXY = Depends(require(Permission.ALLOW_PROXY))
CHANGED = {"bans-changed": True}


def _client(panel: Panel):
    client = panel.gateway_client()
    if client is None:
        raise LookupError(t("traffic.no_gateway"))
    return client


async def _get(panel: Panel, path: str, **params) -> tuple[dict | None, str | None]:
    """Odczyt z bramki dla fragmentów strony — błąd pokazujemy w miejscu listy, nie jako stronę błędu."""
    client = panel.gateway_client()
    if client is None:
        return None, t("traffic.no_gateway")
    try:
        return await asyncio.wait_for(client.get(path, params=params or None), 10), None
    except RemoteError as exc:
        return None, exc.detail
    except (httpx.HTTPError, asyncio.TimeoutError):
        return None, t("msg.gateway_not_responding")


@router.get("/traffic")
async def page(request: Request, panel: Panel = Depends(get_panel), user: dict = PROXY):
    data, error = await _get(panel, "/api/bans")
    domains = sorted({d for site in panel.db.all("sites") for d in site.get("domains") or []})
    return render(request, "traffic.html", title=t("traffic.title"), gateway=panel.gateway(), data=data, error=error, domains=domains)


@router.get("/traffic/top")
async def top(request: Request, days: int = 1, domain: str = "", panel: Panel = Depends(get_panel), user: dict = PROXY):
    data, error = await _get(panel, "/api/traffic/top", days=days, domain=domain, limit=50)
    return render(request, "partials/traffic_top.html", data=data, error=error, days=days, domain=domain)


@router.get("/traffic/suspicious")
async def suspicious(request: Request, panel: Panel = Depends(get_panel), user: dict = PROXY):
    data, error = await _get(panel, "/api/traffic/suspicious")
    return render(request, "partials/traffic_suspicious.html", data=data, error=error)


@router.get("/traffic/bans")
async def bans(request: Request, panel: Panel = Depends(get_panel), user: dict = PROXY):
    data, error = await _get(panel, "/api/bans")
    return render(request, "partials/traffic_bans.html", data=data, error=error)


@router.post("/traffic/ban")
async def ban(target: str = Form(...), minutes: int = Form(0), reason: str = Form(""), panel: Panel = Depends(get_panel), user: dict = PROXY):
    result = await _client(panel).post("/api/bans", json={"target": target.strip(), "minutes": minutes or None, "reason": reason.strip()})
    level = "ok" if result["nginx"]["ok"] or result["nginx"]["status"] == "written" else "warn"
    return toast(t("traffic.banned", target=result["ban"]["target"]), level, extra=CHANGED)


@router.post("/traffic/unban")
async def unban(target: str = Form(...), panel: Panel = Depends(get_panel), user: dict = PROXY):
    await _client(panel).post("/api/bans/remove", json={"target": target})
    return toast(t("traffic.unbanned", target=target), extra=CHANGED)


@router.post("/traffic/settings")
async def settings(
    auto: str = Form(""),
    rate: int = Form(600),
    minutes: int = Form(60),
    allowlist: str = Form(""),
    panel: Panel = Depends(get_panel),
    user: dict = PROXY,
):
    entries = [line.strip() for line in allowlist.replace(",", "\n").splitlines() if line.strip()]
    await _client(panel).put("/api/bans/settings", json={"auto": bool(auto), "rate": rate, "minutes": minutes, "allowlist": entries})
    return toast(t("msg.settings_saved"))


@router.post("/traffic/geoip")
async def geoip(panel: Panel = Depends(get_panel), user: dict = PROXY):
    status = await _client(panel).post("/api/geoip/refresh", timeout=300)
    return toast(t("traffic.geoip_updated") if status.get("updated") else t("traffic.geoip_current"), extra=CHANGED)
