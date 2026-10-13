"""Uptime (w stylu Uptime Kuma): monitory aplikacji, kontenerów, węzłów i dowolnych URL.

Panel co kilkanaście sekund sprawdza monitory, którym minął interwał. Każde
sprawdzenie trafia do `monitor_checks` (trzymane 48 h — pasek ostatnich
sprawdzeń, opóźnienia) i do agregatu `monitor_hourly` (90 dni — uptime 24 h /
7 dni / 30 dni). Zmiana stanu to alert `monitor_down` — trafia do kanałów
wybranych w Ustawienia → Alerty (panel, webhook, Discord, Telegram, e-mail…).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx
from loguru import logger

from deployra_common.client import RemoteError

from ..db import now
from deployra_common import i18n as i18n_core

from ..i18n import t

TICK = 15
RAW_KEEP = 48 * 3600
HOURLY_KEEP = 90 * 86400


# --- zarządzanie monitorami ---------------------------------------------------------------


def ensure_for(panel: Any, kind: str, ref_id: int, name: str, node_id: int | None = None, target: dict | None = None) -> int:
    row = panel.db.fetchone("SELECT id FROM monitors WHERE kind = ? AND ref_id = ?", [kind, ref_id])
    if row:
        return row["id"]
    return panel.db.insert("monitors", name=name[:120], kind=kind, ref_id=ref_id, node_id=node_id, target=target or {},
                           interval_s=60, enabled=1, status="unknown", created_at=now())  # fmt: skip


def monitor_for(panel: Any, kind: str, ref_id: int) -> dict | None:
    return panel.db.fetchone("SELECT * FROM monitors WHERE kind = ? AND ref_id = ?", [kind, ref_id])


def remove(panel: Any, monitor_id: int) -> None:
    panel.db.execute("DELETE FROM monitor_checks WHERE monitor_id = ?", [monitor_id])
    panel.db.execute("DELETE FROM monitor_hourly WHERE monitor_id = ?", [monitor_id])
    panel.db.delete("monitors", monitor_id)


def remove_for(panel: Any, kind: str, ref_id: int) -> None:
    row = monitor_for(panel, kind, ref_id)
    if row:
        remove(panel, row["id"])


def container_key(node_id: int, ep: str, name: str) -> dict:
    return {"node_id": node_id, "ep": ep, "name": name}


def toggle_container(panel: Any, node: dict, ep: str, name: str) -> bool:
    """Włącza/wyłącza monitorowanie kontenera. Zwraca True, jeśli teraz jest monitorowany."""
    for row in panel.db.fetchall("SELECT * FROM monitors WHERE kind = 'container' AND node_id = ?", [node["id"]]):
        target = row.get("target") or {}
        if target.get("name") == name and target.get("ep", "local") == ep:
            remove(panel, row["id"])
            return False
    label = name if ep == "local" else f"{name} ({ep})"
    panel.db.insert("monitors", name=f"{label} @ {node['name']}"[:120], kind="container", ref_id=None, node_id=node["id"],
                    target=container_key(node["id"], ep, name), interval_s=60, enabled=1, status="unknown",
                    created_at=now())  # fmt: skip
    return True


# --- statystyki --------------------------------------------------------------------------------


def _uptime(panel: Any, monitor_id: int, seconds: int) -> float | None:
    since = int((time.time() - seconds) // 3600)
    row = panel.db.fetchone(
        "SELECT SUM(checks) AS c, SUM(ups) AS u, SUM(latency_sum) AS ls, SUM(latency_n) AS ln FROM monitor_hourly "
        "WHERE monitor_id = ? AND hour >= ?",
        [monitor_id, since],
    )
    if not row or not row["c"]:
        return None
    return round(100.0 * row["u"] / row["c"], 3)


def stats(panel: Any, monitor: dict, beats: int = 40) -> dict:
    mid = monitor["id"]
    recent = panel.db.fetchall(
        "SELECT ts, ok, latency_ms, message FROM monitor_checks WHERE monitor_id = ? ORDER BY ts DESC LIMIT ?", [mid, beats]
    )[::-1]
    latency = panel.db.fetchone(
        "SELECT SUM(latency_sum) AS s, SUM(latency_n) AS n FROM monitor_hourly WHERE monitor_id = ? AND hour >= ?",
        [mid, int((time.time() - 86400) // 3600)],
    )
    return {
        "monitor": monitor,
        "status": monitor["status"],
        "uptime_24h": _uptime(panel, mid, 86400),
        "uptime_7d": _uptime(panel, mid, 7 * 86400),
        "uptime_30d": _uptime(panel, mid, 30 * 86400),
        "latency": round(latency["s"] / latency["n"], 1) if latency and latency["n"] else None,
        "beats": recent,
    }


def stats_for(panel: Any, keys: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    out = {}
    for kind, ref_id in keys:
        monitor = monitor_for(panel, kind, ref_id)
        if monitor:
            out[(kind, ref_id)] = stats(panel, monitor, beats=30)
    return out


def container_stats(panel: Any, node_id: int, ep: str) -> dict[str, dict]:
    out = {}
    for row in panel.db.fetchall("SELECT * FROM monitors WHERE kind = 'container' AND node_id = ?", [node_id]):
        target = row.get("target") or {}
        if target.get("ep", "local") == ep:
            out[target.get("name")] = stats(panel, row, beats=20)
    return out


def all_stats(panel: Any) -> list[dict]:
    return [stats(panel, row) for row in panel.db.fetchall("SELECT * FROM monitors ORDER BY kind, name")]


# --- sprawdzanie ----------------------------------------------------------------------------


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def app_containers(items: list[dict], app: dict) -> list[dict]:
    """Kontenery aplikacji: etykieta Deployry / app-<slug> / projekt compose, a dla dopisanych ręcznie —
    opublikowany port aplikacji albo nazwa kontenera równa nazwie aplikacji (bez myślników, np. robal_bot ↔ Robal Bot)."""
    slug = app["slug"]
    exact = [c for c in items if c.get("app") == slug or c["name"] == f"app-{slug}" or c.get("stack") == slug]
    if exact:
        return exact
    port = app.get("host_port")
    by_port = [c for c in items if port and any(p.get("host") == port for p in c.get("ports") or [])]
    if by_port:
        return by_port
    keys = {k for k in (_norm(slug), _norm(app.get("name"))) if k}
    return [c for c in items if _norm(re.sub(r"[-_]\d+$", "", c["name"])) in keys]


class Checker:
    def __init__(self, panel: Any) -> None:
        self.panel = panel
        self._containers: dict[tuple[int, str], tuple[float, list[dict]]] = {}
        self._http = httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": "Deployra-Uptime/1.0"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _containers_of(self, node: dict, ep: str) -> list[dict]:
        key = (node["id"], ep)
        cached = self._containers.get(key)
        if cached and time.time() - cached[0] < TICK:
            return cached[1]
        path = "/api/containers" + ("" if ep == "local" else f"?ep={ep}")
        items = await self.panel.node_client(node).get(path)
        self._containers[key] = (time.time(), items)
        return items

    async def check(self, monitor: dict) -> tuple[bool | None, float | None, str]:
        """(ok, opóźnienie ms, opis). ok=None = pomijamy (np. trwa deploy)."""
        kind = monitor["kind"]
        started = time.perf_counter()
        elapsed = lambda: round((time.perf_counter() - started) * 1000, 1)  # noqa: E731
        try:
            if kind == "node":
                node = self.panel.node(monitor["ref_id"])
                if not node or node["status"] != "paired":
                    return None, None, t("uptime.msg.node_unpaired")
                await self.panel.node_client(node).get("/api/vitals")
                return True, elapsed(), t("uptime.msg.responds")
            if kind == "container":
                target = monitor.get("target") or {}
                node = self.panel.node(target.get("node_id") or monitor.get("node_id"))
                if not node:
                    return False, None, t("uptime.msg.node_missing")
                for c in await self._containers_of(node, target.get("ep", "local")):
                    if c["name"] == target.get("name"):
                        ok = c["state"] == "running" and "unhealthy" not in (c.get("status") or "")
                        return ok, None, c.get("status") or c["state"]
                return False, None, t("uptime.msg.container_missing")
            if kind == "app":
                return await self._check_app(monitor, elapsed)
            if kind == "url":
                target = monitor.get("target") or {}
                response = await self._http.get(target["url"])
                ok = response.status_code < 400
                keyword = target.get("keyword")
                if ok and keyword and keyword not in response.text:
                    return False, elapsed(), t("uptime.msg.keyword_missing", status=response.status_code, keyword=keyword)
                return ok, elapsed(), f"HTTP {response.status_code}"
        except (RemoteError, httpx.HTTPError, LookupError, asyncio.TimeoutError) as exc:
            return False, None, (getattr(exc, "detail", None) or str(exc) or type(exc).__name__)[:300]
        return None, None, t("uptime.msg.unknown_kind")

    async def _check_app(self, monitor: dict, elapsed) -> tuple[bool | None, float | None, str]:
        app = self.panel.db.get("apps", monitor["ref_id"])
        if not app:
            return False, None, t("uptime.msg.app_missing")
        if app["status"] in ("deploying", "new"):
            return None, None, t("uptime.msg.deploying")
        node = self.panel.node(app["node_id"])
        if node and not app.get("domains"):  # bez domeny HTTP nic nie powie (bot, baza, port tylko w LAN) — liczy się stan kontenerów
            return await self._check_containers(node, app)
        if not node:
            return False, None, t("uptime.msg.not_deployed")
        site = self.panel.db.get("sites", app["site_id"]) if app.get("site_id") else None
        domain = (app.get("domains") or [None])[0]
        if app.get("host_port"):
            host = ((site or {}).get("target") or {}).get("host") or self.panel.node_upstream_host(node)
            url = f"http://{host}:{app['host_port']}/"
        else:  # aplikacja dopisana bez portu (np. przejęta ręcznie) — sprawdzamy cel jej hosta proxy, a bez niego kontenery
            from .proxy_health import probe_url

            probe = probe_url(self.panel, site, {n["id"]: n for n in self.panel.db.all("nodes")}) if site and site.get("target") else None
            if not probe:
                return await self._check_containers(node, app)
            url, domain = probe[0], probe[1] or domain
        gateway = self.panel.gateway_client()
        if gateway:
            result = await gateway.post("/api/check", json={"url": url, "host": domain})
            if result.get("status") is None:
                return False, None, result.get("error") or t("uptime.msg.no_response")
            return bool(result.get("ok")), result.get("ms"), f"HTTP {result['status']}"
        response = await self._http.get(url, headers={"Host": domain} if domain else None)
        return response.status_code < 500, elapsed(), f"HTTP {response.status_code}"

    async def _check_containers(self, node: dict, app: dict) -> tuple[bool | None, float | None, str]:
        from .deploy import load

        try:
            endpoint = load(self.panel, app)[0].endpoint
        except (ValueError, KeyError):
            endpoint = "local"
        items = await self._containers_of(node, endpoint)
        mine = app_containers(items, app)
        if not mine and app["kind"] == "static":  # strony statyczne serwuje wspólny nginx węzła
            mine = [c for c in items if c.get("system") == "static"]
        if not mine:
            return False, None, t("uptime.msg.container_missing")
        for c in mine:
            if c["state"] != "running" or "unhealthy" in (c.get("status") or ""):
                return False, None, f"{c['name']}: {c.get('status') or c['state']}"
        message = (mine[0].get("status") or mine[0]["state"]) if len(mine) == 1 else t("uptime.msg.containers_up", count=len(mine))
        return True, None, message


def record(panel: Any, monitor: dict, ok: bool, latency: float | None, message: str) -> str | None:
    """Zapisuje wynik; zwraca nowy stan, jeśli się zmienił."""
    ts = time.time()
    db = panel.db
    db.insert("monitor_checks", monitor_id=monitor["id"], ts=ts, ok=int(ok), latency_ms=latency, message=message[:300])
    hour = int(ts // 3600)
    lat_sum, lat_n = (latency or 0.0), (1 if latency is not None else 0)
    if db.dialect == "sqlite":
        db.execute(
            "INSERT INTO monitor_hourly (monitor_id, hour, checks, ups, latency_sum, latency_n) VALUES (?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(monitor_id, hour) DO UPDATE SET checks = checks + 1, ups = ups + excluded.ups, "
            "latency_sum = latency_sum + excluded.latency_sum, latency_n = latency_n + excluded.latency_n",
            [monitor["id"], hour, int(ok), lat_sum, lat_n],
        )
    else:
        db.execute(
            "INSERT INTO monitor_hourly (monitor_id, hour, checks, ups, latency_sum, latency_n) VALUES (?, ?, 1, ?, ?, ?) "
            "ON DUPLICATE KEY UPDATE checks = checks + 1, ups = ups + VALUES(ups), "
            "latency_sum = latency_sum + VALUES(latency_sum), latency_n = latency_n + VALUES(latency_n)",
            [monitor["id"], hour, int(ok), lat_sum, lat_n],
        )
    status = "up" if ok else "down"
    changed = status != monitor["status"]
    db.update("monitors", monitor["id"], status=status, last_check=ts, last_message=message[:300],
              **({"last_change": ts} if changed else {}))  # fmt: skip
    if not changed or (monitor["status"] == "unknown" and status == "up"):
        return None  # pierwsze udane sprawdzenie nie jest „zmianą”, o której warto pisać
    return status


async def notify_change(panel: Any, monitor: dict, status: str, message: str) -> None:
    """Zmiana stanu monitora → alert (kanały wybiera się w Ustawienia → Alerty) i zdarzenie dla pluginów."""
    from . import alerts

    panel.plugins.emit("monitor.up" if status == "up" else "monitor.down", monitor=monitor, message=message)
    await alerts.monitor_change(panel, monitor, status, message)


async def run_due(panel: Any, checker: Checker) -> int:
    nowts = time.time()
    due = [
        m for m in panel.db.fetchall("SELECT * FROM monitors WHERE enabled = 1")
        if not m.get("last_check") or nowts - m["last_check"] >= (m.get("interval_s") or 60)
    ]  # fmt: skip
    semaphore = asyncio.Semaphore(8)

    async def one(monitor: dict) -> None:
        async with semaphore:
            try:
                ok, latency, message = await asyncio.wait_for(checker.check(monitor), timeout=30)
            except asyncio.TimeoutError:
                ok, latency, message = False, None, t("uptime.msg.timeout")
            if ok is None:
                panel.db.update("monitors", monitor["id"], last_check=time.time(), last_message=message)
                return
            changed = record(panel, monitor, ok, latency, message)
            if changed:
                await notify_change(panel, monitor, changed, message)

    await asyncio.gather(*(one(m) for m in due))
    return len(due)


def cleanup(panel: Any) -> None:
    panel.db.execute("DELETE FROM monitor_checks WHERE ts < ?", [time.time() - RAW_KEEP])
    panel.db.execute("DELETE FROM monitor_hourly WHERE hour < ?", [int((time.time() - HOURLY_KEEP) // 3600)])


async def loop(panel: Any) -> None:
    i18n_core.current.set(panel.default_language or i18n_core.DEFAULT)  # powiadomienia z tła — w języku domyślnym panelu
    checker = Checker(panel)
    last_cleanup = 0.0
    try:
        while True:
            try:
                await run_due(panel, checker)
                if time.time() - last_cleanup > 3600:
                    await asyncio.to_thread(cleanup, panel)
                    last_cleanup = time.time()
            except Exception as exc:  # noqa: BLE001 — pętla monitorów nie może paść
                logger.error(f"Uptime: {exc}")
            await asyncio.sleep(TICK)
    finally:
        await checker.aclose()
