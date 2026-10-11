"""Aktualizacje: ustawienia, sprawdzanie wydań, baner, uruchamianie aktualizacji w tle."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request

from .. import __version__
from ..context import Panel, get_panel
from ..deps import current_user, require
from ..permissions import Permission
from ..i18n import t
from ..services import updates
from ..web import redirect, refresh, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
SETTINGS = Depends(require(Permission.ALLOW_SETTINGS))


@router.get("/settings/updates")
def page(request: Request, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    latest = updates.latest(panel)
    return render(
        request, "settings/updates.html", title=t("settings.tab_updates"), tab="updates", latest=latest, current=__version__,
        newer=bool(latest.get("version")) and updates.is_newer(latest["version"], __version__),
        notes=updates.notes_html(latest.get("notes", "")), target=updates.target_version(panel), pending=updates.pending(panel),
        source=updates.source(panel), env_source=panel.config.update_repo, s=panel.db.settings(),
        skipped=panel.db.setting("update_skip"),
    )  # fmt: skip


@router.get("/settings/updates/components")
async def components(request: Request, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    items = await updates.components(panel)
    return render(request, "partials/update_components.html", items=items, target=updates.target_version(panel),
                  pending=updates.pending(panel), describe=updates.describe)  # fmt: skip


@router.post("/settings/updates")
def save(
    update_repo: str = Form(""),
    update_check: str = Form(""),
    update_auto: str = Form(""),
    panel: Panel = Depends(get_panel),
    user: dict = SETTINGS,
):
    repo = updates.validate_source(update_repo)
    changed = repo != (panel.db.setting("update_repo") or "")
    panel.db.set_setting("update_repo", repo)
    panel.db.set_setting("update_check", bool(update_check))
    panel.db.set_setting("update_auto", bool(update_auto))
    if changed:
        panel.db.set_setting("update_latest", None)  # nowe źródło — stara informacja o wydaniu nieaktualna
        return refresh(t("upd.saved_check"))
    return toast(t("upd.saved"))


@router.post("/settings/updates/check")
async def check_now(panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    info = await updates.check(panel)
    if info.get("error"):
        return refresh(info["error"], "err")
    if updates.is_newer(info["version"], __version__):
        return refresh(t("upd.available_toast", version=info["version"]))
    return refresh(t("upd.up_to_date_toast", version=__version__))


@router.post("/settings/updates/start")
async def start(request: Request, target: str = Form(""), only: str = Form(""), panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    keys = {k for k in only.split(",") if k} or None
    job = updates.start(panel, target or None, keys)
    panel.db.set_setting("update_snooze", None)
    return redirect(request, f"/jobs/{job.id}", t("upd.started"))


@router.post("/updates/snooze")
def snooze(panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    panel.db.set_setting("update_snooze", time.time() + updates.SNOOZE)
    return toast(t("upd.snoozed"), "info")


@router.post("/updates/skip")
def skip(version: str = Form(...), panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    panel.db.set_setting("update_skip", updates.normalize(version))
    return toast(t("upd.skipped_toast", version=version), "info")


@router.post("/updates/unskip")
def unskip(panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    panel.db.set_setting("update_skip", None)
    return refresh(t("upd.unskipped"))
