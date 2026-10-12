"""Czy host proxy działa: bramka odpytuje cel każdego hosta (jednym zbiorczym zapytaniem, wynik pamiętany 30 s)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from deployra_common.client import RemoteError

from . import proxy_state

CACHE_SECONDS = 30
_cache: dict[tuple[int, str], tuple[float, dict]] = {}


def probe_url(panel: Any, site: dict[str, Any], nodes: dict[int, dict]) -> tuple[str, str | None] | None:
    """(adres celu widziany przez bramkę, nagłówek Host) albo None, gdy celu nie da się ustalić."""
    upstream = proxy_state.resolve_target(panel, site.get("target"), nodes)
    if upstream is None:
        return None
    host = next((d for d in site.get("domains") or [] if not d.startswith("*.")), None)
    return f"{upstream.scheme}://{upstream.host}:{upstream.port}{upstream.path or '/'}", host


async def check(panel: Any, sites: list[dict[str, Any]]) -> dict[int, dict]:
    client = panel.gateway_client()
    if not client:
        return {}
    nodes = {n["id"]: n for n in panel.db.all("nodes")}
    now = time.time()
    results: dict[int, dict] = {}
    todo: dict[str, tuple[int, str, str | None]] = {}
    for site in sites:
        probe = probe_url(panel, site, nodes)
        if probe is None:
            results[site["id"]] = {"ok": False, "status": None, "error": "?"}
            continue
        cached = _cache.get((site["id"], probe[0]))
        if cached and now - cached[0] < CACHE_SECONDS:
            results[site["id"]] = cached[1]
        else:
            todo[str(site["id"])] = (site["id"], *probe)
    if not todo:
        return results
    items = [{"id": key, "url": url, "host": host} for key, (_, url, host) in todo.items()]
    try:
        answer = (await client.post("/api/check/batch", json={"items": items}, timeout=30))["results"]
    except RemoteError as exc:
        if exc.status != 404:
            return results
        answer = await _one_by_one(client, items)  # starsza bramka bez /api/check/batch
    except (httpx.HTTPError, KeyError, TypeError):
        return results
    for key, (site_id, url, _) in todo.items():
        result = answer.get(key)
        if result is not None:
            _cache[(site_id, url)] = (now, result)
            results[site_id] = result
    return results


async def _one_by_one(client: Any, items: list[dict]) -> dict[str, dict]:
    semaphore = asyncio.Semaphore(8)

    async def one(item: dict) -> tuple[str, dict | None]:
        async with semaphore:
            try:
                return item["id"], await client.post("/api/check", json={"url": item["url"], "host": item["host"]}, timeout=15)
            except (RemoteError, httpx.HTTPError):
                return item["id"], None

    return {key: value for key, value in await asyncio.gather(*(one(i) for i in items)) if value is not None}
