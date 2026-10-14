"""Logi: przeszukiwanie logów kontenerów na wszystkich węzłach naraz (mini-ELK bez indeksu)."""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, Depends, Request
from markupsafe import Markup

from deployra_common.client import RemoteError

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import logquery
from ..web import render

router = APIRouter(dependencies=[Depends(current_user)])
SEE = Depends(require(Permission.ALLOW_SEEKING_CONTAINER))
RANGES = {"15m": 900, "1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}


@router.get("/logs")
def logs_page(request: Request, q: str = "", range: str = "1h", source: str = "all", user: dict = SEE):
    return render(request, "logs.html", title=t("nav.logs"), q=q, span=range if range in RANGES else "1h", ranges=list(RANGES), source=source)


@router.get("/logs/search")
async def logs_search(request: Request, q: str = "", range: str = "1h", source: str = "all", panel: Panel = Depends(get_panel), user: dict = SEE):
    try:
        query = logquery.parse(q)
    except ValueError as exc:
        return render(request, "partials/logs_results.html", error=str(exc))
    until = time.time()
    since = until - RANGES.get(range, 3600)
    nodes = [n for n in panel.db.fetchall("SELECT * FROM nodes WHERE status = 'paired' ORDER BY name")
             if not query.nodes or n["name"] in query.nodes]  # fmt: skip
    containers = list(query.containers)
    if source == "nginx" and not containers:
        containers = ["nginx", "deployra-nginx"]
    body = {"must": query.must, "exclude": query.exclude, "since": since, "until": until, "containers": containers,
            "apps": query.apps, "limit": 200}  # fmt: skip

    async def one(node: dict) -> tuple[dict, dict | None, str | None]:
        try:
            return node, await asyncio.wait_for(panel.node_client(node).post("/api/logs/search", json=body), 30), None
        except RemoteError as exc:
            return node, None, exc.detail
        except (httpx.HTTPError, asyncio.TimeoutError):
            return node, None, t("msg.node_not_responding")

    results = await asyncio.gather(*(one(n) for n in nodes))
    hits, buckets, total, scanned, errors = [], [0] * 40, 0, 0, []
    for node, data, error in results:
        if error or not data:
            errors.append(f"{node['name']}: {error}")
            continue
        total += data["total"]
        scanned += data["scanned"]
        buckets = [a + b for a, b in zip(buckets, data["buckets"])]
        for hit in data["hits"]:
            cname = (hit.get("container") or "").lower()
            if source == "nginx" and "nginx" not in cname:
                continue
            if source == "containers" and "nginx" in cname:
                continue
            hit["node"] = node
            hit["html"] = Markup(logquery.highlight(hit["line"], query.must))
            hits.append(hit)
    hits.sort(key=lambda h: h["ts"] or 0, reverse=True)
    step = (until - since) / len(buckets)
    return render(request, "partials/logs_results.html", hits=hits[:300], total=total, scanned=scanned, nodes=len(nodes),
                  errors=errors, buckets=buckets, peak=max(buckets) or 1, since=since, step=step, error=None)  # fmt: skip
