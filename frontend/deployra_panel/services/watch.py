"""Kontrole w tle, których brakowało alertom: certyfikaty, bramki i tunel, automatyczna kopia stanu panelu.

Wywoływane z pętli zasobów (co kilka minut / co godzinę); każda kontrola sama łapie błędy i nie zatrzymuje pętli.
Stan alertu (aktywny / rozwiązany) trzyma tabela `alerts`, więc ten sam problem nie przychodzi co kilka minut.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from ..i18n import t
from . import alerts

GATEWAY_GRACE = 180  # s bez odpowiedzi API bramki, zanim alert
HANDSHAKE_STALE = 300  # s bez handshake'u WireGuard peera bramki brzegowej


def _active(panel: Any, rule: str, kind: str, target_id: int) -> dict | None:
    return alerts._active(panel).get((rule, kind, target_id))


async def _set(panel: Any, rule: str, kind: str, target_id: int, name: str, bad: bool, text: str, ok_text: str, value: float | None = None) -> None:
    config = alerts.settings(panel).get(rule) or {}
    active = _active(panel, rule, kind, target_id)
    if bad and not active and config.get("enabled", True):
        await alerts.fire(panel, rule, kind, target_id, name, value, text)
    elif active and (not bad or not config.get("enabled", True)):
        await alerts.resolve(panel, active, ok_text)


# --- certyfikaty -----------------------------------------------------------------------------------


def _days_left(expires_at: Any) -> float | None:
    if not expires_at:
        return None
    try:
        when = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        try:
            return (float(expires_at) - time.time()) / 86400
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds() / 86400


async def certificates(panel: Any) -> None:
    """Certyfikat wygasa za mniej niż próg (domyślnie 14 dni) — zwykle znaczy, że odnowienie nie działa."""
    from . import certs

    try:
        await certs.sync_from_gateway(panel)
    except Exception as exc:  # noqa: BLE001 — bez bramki porównujemy z tym, co w bazie
        logger.debug(f"certyfikaty: {exc}")
    threshold = float(alerts.settings(panel).get("cert_expiring", {}).get("threshold") or 14)
    for cert in panel.db.all("certificates"):
        days = _days_left(cert.get("expires_at"))
        if days is None or cert.get("challenge") == "custom" and days > threshold:
            continue
        name = ", ".join((cert.get("domains") or [])[:2]) or f"#{cert['id']}"
        await _set(panel, "cert_expiring", "cert", cert["id"], name, days < threshold,
                   t("alerts.text.cert_expiring", days=max(0, int(days)), threshold=int(threshold)), t("alerts.ok.cert_expiring"), round(days, 1))  # fmt: skip


async def cert_failed(panel: Any, cert: dict, error: str) -> None:
    from .notify import Message

    name = ", ".join((cert.get("domains") or [])[:2]) or f"#{cert['id']}"
    await alerts.event(panel, "cert_renew_failed", Message(t("alerts.title.cert_renew_failed", name=name), error[:1500], "err"))


# --- bramki i tunel ----------------------------------------------------------------------------------

_DOWN_SINCE: dict[int, float] = {}


async def gateways(panel: Any) -> None:
    """API bramki nie odpowiada dłużej niż chwilę (gateway_offline); peer bramki brzegowej bez handshake'u (tunnel_peer_offline)."""
    for gw in panel.gateways():
        client = panel.client_for_gateway(gw)
        if not client:
            continue
        name = gw.get("name") or gw.get("public_host") or f"#{gw['id']}"
        try:
            status = await asyncio.wait_for(client.get("/api/status"), 15)
            _DOWN_SINCE.pop(gw["id"], None)
            down = False
        except Exception:  # noqa: BLE001
            status = None
            since = _DOWN_SINCE.setdefault(gw["id"], time.time())
            down = time.time() - since >= GATEWAY_GRACE
        if status is not None or down:
            await _set(panel, "gateway_offline", "gateway", gw["id"], name, down, t("alerts.text.gateway_offline", minutes=GATEWAY_GRACE // 60),
                       t("alerts.ok.gateway_offline"))  # fmt: skip
        if gw.get("role") == "hub" and status:
            await _edge_peer(panel, status)


async def _edge_peer(panel: Any, status: dict) -> None:
    edge = panel.edge_row()
    if not edge or not edge.get("secret"):
        return
    for peer in status.get("peers") or []:
        if peer.get("kind") != "edge":
            continue
        shake = float(peer.get("latest_handshake") or 0)
        stale = not shake or time.time() - shake > HANDSHAKE_STALE
        await _set(panel, "tunnel_peer_offline", "peer", int(edge["id"]), edge.get("name") or "edge", stale,
                   t("alerts.text.tunnel_peer_offline", minutes=HANDSHAKE_STALE // 60), t("alerts.ok.tunnel_peer_offline"))  # fmt: skip


# --- automatyczna kopia stanu panelu --------------------------------------------------------------------


async def panel_backup(panel: Any) -> None:
    """Kopia stanu panelu co `auto_hours` godzin (0 = tylko ręcznie). Błąd = alert panel_backup_failed."""
    from . import backup_storage

    conf = panel.db.setting("backup_config") or {}
    hours = int(conf.get("auto_hours") or 0)
    if hours <= 0:
        return
    last = conf.get("last_auto_at") or 0
    if time.time() - float(last) < hours * 3600:
        return
    conf = {**conf, "last_auto_at": time.time()}
    panel.db.set_setting("backup_config", conf)
    result = await asyncio.to_thread(backup_storage.run_backup, panel)
    if not result.get("ok"):
        await backup_failed(panel, result.get("error") or "?")


async def backup_failed(panel: Any, error: str) -> None:
    from .notify import Message

    await alerts.event(panel, "panel_backup_failed", Message(t("alerts.title.panel_backup_failed", name="Deployra"), error[:1500], "err"))


async def task_failed(panel: Any, task: dict, status: str, output: str) -> None:
    from .notify import Message

    app = panel.db.get("apps", task.get("app_id") or 0) or {}
    name = f"{app.get('name') or '?'} · {task.get('name') or task.get('command') or task['id']}"
    await alerts.event(panel, "task_failed", Message(t("alerts.title.task_failed", name=name), t("alerts.text.task_failed", status=status,
                       output=(output or "")[-800:]), "err"))  # fmt: skip


async def tick(panel: Any, hourly: bool) -> None:
    for check in ([gateways, panel_backup] + ([certificates] if hourly else [])):
        try:
            await check(panel)
        except Exception as exc:  # noqa: BLE001 — jedna kontrola nie zatrzymuje pozostałych
            logger.warning(f"kontrola {check.__name__}: {type(exc).__name__}: {exc}")
