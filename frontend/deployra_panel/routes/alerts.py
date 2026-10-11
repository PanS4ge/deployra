"""Ustawienia → Alerty: kanały powiadomień, reguły z progami (co gdzie trafia) i historia alertów."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import alerts, notify
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
SETTINGS = Depends(require(Permission.ALLOW_SETTINGS))


@router.get("/settings/alerts")
def page(request: Request, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    channels = notify.channels(panel)
    for channel in channels:
        config = notify.channel_config(panel, channel)
        # sekrety nie wracają do przeglądarki — w formularzu tylko informacja, że są ustawione
        channel["values"] = {f.name: ("" if f.kind == "secret" else config.get(f.name, "")) for f in notify.KINDS[channel["kind"]]["fields"]}
        channel["secrets"] = [f.name for f in notify.KINDS[channel["kind"]]["fields"] if f.kind == "secret" and config.get(f.name)]
    return render(request, "settings/alerts.html", title=t("settings.tab_alerts"), tab="alerts", channels=channels, kinds=notify.KINDS,
                  rules=alerts.RULES, config=alerts.settings(panel), history=alerts.recent(panel))  # fmt: skip


def _values(form) -> dict[str, str]:
    return {key[2:]: str(value) for key, value in form.multi_items() if key.startswith("f_")}


@router.post("/settings/alerts/channels")
async def add_channel(request: Request, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    form = await request.form()
    channel_id = notify.save_channel(panel, str(form.get("name") or ""), str(form.get("kind") or ""), _values(form))
    alerts.add_channel_everywhere(panel, channel_id)
    return redirect(request, "/settings/alerts", t("notify.added"))


@router.post("/settings/alerts/channels/{channel_id}")
async def edit_channel(request: Request, channel_id: int, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    form = await request.form()
    row = panel.db.get("notify_channels", channel_id)
    if not row:
        raise LookupError(t("notify.not_found"))
    notify.save_channel(panel, str(form.get("name") or ""), row["kind"], _values(form), channel_id)
    return redirect(request, "/settings/alerts", t("notify.saved"))


@router.post("/settings/alerts/channels/{channel_id}/toggle")
def toggle_channel(request: Request, channel_id: int, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    row = panel.db.get("notify_channels", channel_id)
    if not row:
        raise LookupError(t("notify.not_found"))
    panel.db.update("notify_channels", channel_id, enabled=0 if row["enabled"] else 1)
    return redirect(request, "/settings/alerts")


@router.post("/settings/alerts/channels/{channel_id}/delete")
def delete_channel(request: Request, channel_id: int, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    panel.db.delete("notify_channels", channel_id)
    alerts.remove_channel(panel, channel_id)
    notify.STATUS.pop(channel_id, None)
    return redirect(request, "/settings/alerts", t("notify.removed"))


@router.post("/settings/alerts/channels/{channel_id}/test")
async def test_channel(channel_id: int, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    message = notify.Message(t("notify.test_title"), t("notify.test_text", user=user["email"]), "info")
    error = await notify.send_to_channel(panel, channel_id, message)
    return toast(t("notify.test_failed", error=error), "err", status=502) if error else toast(t("notify.test_sent"))


@router.post("/settings/alerts/rules")
async def save_rules(request: Request, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    form = await request.form()
    values = {
        rule_id: {
            "enabled": form.get(f"{rule_id}.enabled"),
            "threshold": form.get(f"{rule_id}.threshold"),
            "minutes": form.get(f"{rule_id}.minutes"),
            "channels": form.getlist(f"{rule_id}.channels"),
            "auto_move": form.get(f"{rule_id}.auto_move"),
        }
        for rule_id in alerts.RULES
    }
    alerts.save_settings(panel, values)
    alerts.forget_settings_cache()
    return toast(t("alerts.saved"))


@router.post("/alerts/{alert_id}/close")
def close_alert(request: Request, alert_id: int, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    panel.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE id = ? AND status = 'firing'", [time.time(), alert_id])
    back = urlsplit(request.headers.get("HX-Current-URL") or "").path  # tylko ścieżka — bez przekierowań na zewnątrz
    return redirect(request, back if back.startswith("/") else "/settings/alerts")
