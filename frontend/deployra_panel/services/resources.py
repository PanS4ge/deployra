"""Zasoby węzłów i aplikacji: zbieranie co minutę, historia, alerty i ochrona przed przepełnieniem.

Panel pyta każdy węzeł o `/api/usage` (host + kontenery aplikacji). Próbki trafiają do `node_samples`
i `app_samples` (48 h, co minutę) oraz do agregatów godzinowych (30 dni, średnia i maksimum).
Ostatnie odczyty trzymamy w pamięci — z nich korzystają kreator (wolny RAM przy wyborze węzła),
sprawdzenie przed wdrożeniem i automatyczne przenoszenie aplikacji z przeciążonego węzła.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from loguru import logger

from deployra_common import i18n as i18n_core
from deployra_common.client import RemoteError

from ..i18n import t
from . import alerts

TICK = 60
RAW_KEEP = 48 * 3600
HOURLY_KEEP = 30 * 86400
MIN_FREE_MB = 64  # tyle wolnej pamięci musi zostać na węźle dla aplikacji bez limitu
DISK_FULL = 95

LATEST: dict[int, dict[str, Any]] = {}  # węzeł → ostatni odczyt (z polem "ts")


def _mb(value: float | None) -> int:
    return int((value or 0) / 1024 / 1024)


def free_mb(host: dict[str, Any]) -> int | None:
    if not host or not host.get("ram_total"):
        return None
    return _mb(host["ram_total"] - (host.get("ram_used") or 0))


async def fetch(panel: Any, node: dict[str, Any]) -> dict[str, Any] | None:
    client = panel.node_client(node)
    try:
        return await asyncio.wait_for(client.get("/api/usage"), timeout=20)
    except RemoteError as exc:
        if exc.status != 404:
            return None
    except (httpx.HTTPError, asyncio.TimeoutError, OSError):
        return None
    try:  # starszy węzeł bez /api/usage — same zasoby hosta
        return {"host": await asyncio.wait_for(client.get("/api/vitals"), timeout=10), "apps": {}, "containers": []}
    except (RemoteError, httpx.HTTPError, asyncio.TimeoutError, OSError):
        return None


def _upsert_hourly(panel: Any, table: str, key: str, key_id: int, hour: int, values: dict[str, float]) -> None:
    sums = {k: v for k, v in values.items() if k.endswith("_sum")}
    maxes = {k: v for k, v in values.items() if k.endswith("_max")}
    cols = [key, "hour", "n", *sums, *maxes]
    params = [key_id, hour, 1, *sums.values(), *maxes.values()]
    marks = ", ".join("?" for _ in cols)
    if panel.db.dialect == "sqlite":
        updates = ["n = n + 1"] + [f"{k} = {k} + excluded.{k}" for k in sums] + [f"{k} = MAX({k}, excluded.{k})" for k in maxes]
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks}) ON CONFLICT({key}, hour) DO UPDATE SET {', '.join(updates)}"
    else:
        updates = ["n = n + 1"] + [f"{k} = {k} + VALUES({k})" for k in sums] + [f"{k} = GREATEST({k}, VALUES({k}))" for k in maxes]
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks}) ON DUPLICATE KEY UPDATE {', '.join(updates)}"
    panel.db.execute(sql, params)


def store(panel: Any, node: dict[str, Any], usage: dict[str, Any], apps: dict[str, dict], ts: float) -> None:
    host = usage.get("host") or {}
    hour = int(ts // 3600)
    load = host.get("load") or [None]
    panel.db.insert("node_samples", node_id=node["id"], ts=ts, cpu=host.get("cpu"), ram=host.get("ram"), swap=host.get("swap"),
                    disk=host.get("disk"), load1=load[0], ram_used=host.get("ram_used"), ram_total=host.get("ram_total"),
                    disk_used=host.get("disk_used"), disk_total=host.get("disk_total"))  # fmt: skip
    cpu, ram, disk = host.get("cpu") or 0.0, host.get("ram") or 0.0, host.get("disk") or 0.0
    _upsert_hourly(panel, "node_hourly", "node_id", node["id"], hour,
                   {"cpu_sum": cpu, "ram_sum": ram, "cpu_max": cpu, "ram_max": ram, "disk_max": disk})  # fmt: skip
    for slug, stats in (usage.get("apps") or {}).items():
        app = apps.get(slug)
        if not app or stats.get("mem") is None:
            continue
        cpu, mem = stats.get("cpu") or 0.0, float(stats.get("mem") or 0)
        panel.db.insert("app_samples", app_id=app["id"], ts=ts, cpu=stats.get("cpu"), mem=mem, mem_limit=stats.get("limit"))
        _upsert_hourly(panel, "app_hourly", "app_id", app["id"], hour, {"cpu_sum": cpu, "mem_sum": mem, "cpu_max": cpu, "mem_max": mem})


def cleanup(panel: Any) -> None:
    edge = time.time() - RAW_KEEP
    panel.db.execute("DELETE FROM node_samples WHERE ts < ?", [edge])
    panel.db.execute("DELETE FROM app_samples WHERE ts < ?", [edge])
    hour = int((time.time() - HOURLY_KEEP) // 3600)
    panel.db.execute("DELETE FROM node_hourly WHERE hour < ?", [hour])
    panel.db.execute("DELETE FROM app_hourly WHERE hour < ?", [hour])
    alerts.cleanup(panel)


async def tick(panel: Any, tracker: alerts.Tracker) -> None:
    nodes = panel.db.fetchall("SELECT * FROM nodes WHERE status = 'paired'")
    results = await asyncio.gather(*(fetch(panel, node) for node in nodes))
    ts = time.time()
    alerts.forget_settings_cache()
    for node, usage in zip(nodes, results):
        apps = {a["slug"]: a for a in panel.db.fetchall("SELECT * FROM apps WHERE node_id = ?", [node["id"]])}
        if usage is not None:
            usage["ts"] = ts
            LATEST[node["id"]] = usage
            await asyncio.to_thread(store, panel, node, usage, apps, ts)
        fired = await alerts.evaluate_node(panel, tracker, node, usage, apps, ts)
        if "node_ram" in fired and alerts.settings(panel)["node_ram"]["auto_move"]:
            from . import migrate

            await migrate.relieve(panel, node)


async def loop(panel: Any) -> None:
    i18n_core.current.set(panel.default_language or i18n_core.DEFAULT)  # alerty z tła — w języku domyślnym panelu
    tracker = alerts.Tracker()
    last_cleanup = 0.0
    await asyncio.sleep(5)
    while True:
        started = time.time()
        try:
            await tick(panel, tracker)
            if time.time() - last_cleanup > 3600:
                await asyncio.to_thread(cleanup, panel)
                last_cleanup = time.time()
        except Exception as exc:  # noqa: BLE001 — pętla zasobów nie może paść
            logger.error(f"Zasoby: {type(exc).__name__}: {exc}")
        await asyncio.sleep(max(5.0, TICK - (time.time() - started)))


# --- ochrona przed przepełnieniem ---------------------------------------------------------------


async def current(panel: Any, node: dict[str, Any], max_age: float = 90) -> dict[str, Any] | None:
    cached = LATEST.get(node["id"])
    if cached and time.time() - cached.get("ts", 0) < max_age:
        return cached
    usage = await fetch(panel, node)
    if usage is not None:
        usage["ts"] = time.time()
        LATEST[node["id"]] = usage
    return usage


def node_free(node_id: int) -> int | None:
    """Wolny RAM węzła (MB) z ostatniego odczytu — do kreatora."""
    cached = LATEST.get(node_id)
    return free_mb(cached.get("host") or {}) if cached else None


async def better_nodes(panel: Any, exclude: int, need_mb: int) -> list[tuple[dict[str, Any], int]]:
    """Inne węzły z wystarczającą ilością wolnej pamięci, od najluźniejszego."""
    out = []
    for node in panel.db.fetchall("SELECT * FROM nodes WHERE status = 'paired' AND id != ?", [exclude]):
        usage = await current(panel, node)
        free = free_mb((usage or {}).get("host") or {})
        disk = ((usage or {}).get("host") or {}).get("disk") or 0
        if free is not None and free - need_mb >= MIN_FREE_MB and disk < DISK_FULL:
            out.append((node, free))
    return sorted(out, key=lambda item: -item[1])


async def capacity_problem(panel: Any, node: dict[str, Any], memory_mb: int | None) -> str | None:
    """Czy aplikacja zmieści się na węźle? Zwraca opis problemu z podpowiedzią innego węzła albo None."""
    usage = await current(panel, node, max_age=30)
    host = (usage or {}).get("host") or {}
    free = free_mb(host)
    if free is None:
        return None  # nie wiemy — nie blokujemy
    need = memory_mb or MIN_FREE_MB
    problem = None
    if (host.get("disk") or 0) >= DISK_FULL:
        problem = t("res.disk_full", node=node["name"], disk=round(host["disk"]))
    elif free < need:
        problem = t("res.no_memory", node=node["name"], free=free, need=need)
    if not problem:
        return None
    better = await better_nodes(panel, node["id"], need)
    if better:
        problem += " " + t("res.try_node", nodes=", ".join(f"{n['name']} ({free_mb_} MB)" for n, free_mb_ in better[:3]))
    return problem


# --- historia do wykresów -----------------------------------------------------------------------

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def _buckets(rows: list[dict], ts_key: str, start: float, step: float, count: int, fields: dict[str, str]) -> dict[str, list[float]]:
    sums = {k: [0.0] * count for k in fields}
    ns = [0] * count
    for row in rows:
        index = int((row[ts_key] - start) // step)
        if 0 <= index < count:
            ns[index] += 1
            for key, column in fields.items():
                sums[key][index] += float(row.get(column) or 0)
    return {k: [round(v / n, 1) if n else 0.0 for v, n in zip(values, ns)] for k, values in sums.items()} | {"_n": ns}  # type: ignore[dict-item]


def node_history(panel: Any, node_id: int, span: str = "24h") -> dict[str, Any]:
    seconds = RANGES.get(span, 86400)
    now_ts = time.time()
    if span == "24h":
        step, count = 600.0, 144
        start = (now_ts // step + 1) * step - step * count
        rows = panel.db.fetchall("SELECT ts, cpu, ram, disk FROM node_samples WHERE node_id = ? AND ts >= ?", [node_id, start])
        data = _buckets(rows, "ts", start, step, count, {"cpu": "cpu", "ram": "ram", "disk": "disk"})
    else:
        step = 3600.0 if span == "7d" else 4 * 3600.0
        count = int(seconds // step)
        start = (now_ts // step + 1) * step - step * count
        rows = panel.db.fetchall("SELECT hour, n, cpu_sum, ram_sum, disk_max FROM node_hourly WHERE node_id = ? AND hour >= ?",
                                 [node_id, int(start // 3600)])  # fmt: skip
        for row in rows:
            row["ts"], row["cpu"], row["ram"] = row["hour"] * 3600, row["cpu_sum"] / max(row["n"], 1), row["ram_sum"] / max(row["n"], 1)
        data = _buckets(rows, "ts", start, step, count, {"cpu": "cpu", "ram": "ram", "disk": "disk_max"})
    data["start"], data["step"] = start, step
    return data


def trimmed(data: dict[str, Any], keys: tuple[str, ...]) -> tuple[list[float], dict[str, list[float]]] | None:
    """Bez pustych przedziałów sprzed pierwszej próbki (świeży węzeł nie ma „zer” w historii). None = za mało danych."""
    first = next((i for i, n in enumerate(data["_n"]) if n), None)
    if first is None or len(data["_n"]) - first < 2:
        return None
    starts = [data["start"] + i * data["step"] for i in range(first, len(data["_n"]))]
    return starts, {k: data[k][first:] for k in keys}


def app_history(panel: Any, app_id: int) -> dict[str, Any]:
    step, count = 600.0, 144
    start = (time.time() // step + 1) * step - step * count
    rows = panel.db.fetchall("SELECT ts, cpu, mem, mem_limit FROM app_samples WHERE app_id = ? AND ts >= ?", [app_id, start])
    for row in rows:
        row["mem"] = (row.get("mem") or 0) / 1024 / 1024
        row["mem_limit"] = (row.get("mem_limit") or 0) / 1024 / 1024
    data = _buckets(rows, "ts", start, step, count, {"cpu": "cpu", "mem": "mem", "limit": "mem_limit"})
    data["start"], data["step"] = start, step
    return data
