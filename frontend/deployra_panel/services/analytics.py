"""Statystyki odwiedzin z Deployra-WG (z pamięcią podręczną 60 s — jedno zapytanie na wszystkie domeny)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from deployra_common.client import RemoteError

CACHE_SECONDS = 60
_cache: dict[tuple, tuple[float, Any]] = {}
_locks: dict[tuple, asyncio.Lock] = {}


async def _get(panel: Any, path: str) -> dict | None:
    key = (id(panel), path)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    async with _locks.setdefault(key, asyncio.Lock()):  # wiele wykresów naraz = jedno zapytanie do bramki
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        client = panel.gateway_client()
        if not client:
            return None
        try:
            data = await client.get(path, timeout=8)
        except (RemoteError, httpx.HTTPError):
            return cached[1] if cached else None
        _cache[key] = (time.time(), data)
        return data


def _days(count: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=count - 1 - i)).isoformat() for i in range(count)]


async def daily(panel: Any, domains: list[str] | None, days: int = 30) -> dict[str, Any] | None:
    """Suma po domenach: serie dzienne + podsumowanie. None = brak bramki/danych."""
    data = await _get(panel, f"/api/analytics?days={days}")
    if data is None:
        return None
    per_domain = data.get("domains", {})
    wanted = set(domains) if domains else set(per_domain)
    labels = _days(days)
    totals = {day: {"requests": 0, "uniques": 0, "returning": 0, "bytes": 0} for day in labels}
    for domain in wanted:
        for row in per_domain.get(domain, []):
            if row["day"] in totals:
                for k in ("requests", "uniques", "returning", "bytes"):
                    totals[row["day"]][k] += row[k]
    series = [totals[d] for d in labels]
    summary = {k: sum(row[k] for row in series) for k in ("requests", "uniques", "returning", "bytes")}
    summary["returning_pct"] = round(100 * summary["returning"] / summary["uniques"], 1) if summary["uniques"] else None
    return {"labels": labels, "series": series, "summary": summary}


async def hourly(panel: Any, domains: list[str] | None) -> dict[str, Any] | None:
    data = await _get(panel, "/api/analytics/hourly?hours=24")
    if data is None:
        return None
    per_domain = data.get("domains", {})
    wanted = set(domains) if domains else set(per_domain)
    current = int(time.time() // 3600)
    hours = list(range(current - 23, current + 1))
    totals = {h: {"requests": 0, "uniques": 0} for h in hours}
    for domain in wanted:
        for row in per_domain.get(domain, []):
            if row["hour"] in totals:
                totals[row["hour"]]["requests"] += row["requests"]
                totals[row["hour"]]["uniques"] += row["uniques"]
    labels = [datetime.fromtimestamp(h * 3600, tz=timezone.utc).astimezone().strftime("%H:00") for h in hours]
    series = [totals[h] for h in hours]
    return {"labels": labels, "series": series, "summary": {k: sum(r[k] for r in series) for k in ("requests", "uniques")}}
