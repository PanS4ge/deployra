"""Aktualizacje: ustawienia, sprawdzanie wydań, baner, uruchamianie aktualizacji w tle."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request

from .. import __version__
from ..context import Panel, get_panel
from ..deps import current_user, require
from ..permissions import Permission
from ..services import updates
from ..web import redirect, refresh, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
SETTINGS = Depends(require(Permission.ALLOW_SETTINGS))


@router.get("/settings/updates")
def page(request: Request, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    latest = updates.latest(panel)
    return render(
        request, "settings/updates.html", title="Aktualizacje", tab="updates", latest=latest, current=__version__,
        newer=bool(latest.get("version")) and updates.is_newer(latest["version"], __version__),
        notes=updates.notes_html(latest.get("notes", "")), target=updates.target_version(panel), pending=updates.pending(panel),
        source=updates.source(panel), env_source=panel.config.update_repo, s=panel.db.settings(),
        skipped=panel.db.setting("update_skip"),
    )  # fmt: skip


@router.get("/settings/updates/components")
async def components(request: Request, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    items = await updates.components(panel)
    return render(request, "partials/update_components.html", items=items, target=updates.target_version(panel),
                  modes=updates.MODES, pending=updates.pending(panel))  # fmt: skip


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
        return refresh("Zapisano — sprawdź teraz, czy jest nowa wersja")
    return toast("Zapisano ustawienia aktualizacji")


@router.post("/settings/updates/check")
async def check_now(panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    info = await updates.check(panel)
    if info.get("error"):
        return refresh(info["error"], "err")
    if updates.is_newer(info["version"], __version__):
        return refresh(f"Dostępna wersja {info['version']} ✦")
    return refresh(f"Masz najnowszą wersję ({__version__})")


@router.post("/settings/updates/start")
async def start(request: Request, target: str = Form(""), only: str = Form(""), panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    keys = {k for k in only.split(",") if k} or None
    job = updates.start(panel, target or None, keys)
    panel.db.set_setting("update_snooze", None)
    return redirect(request, f"/jobs/{job.id}", "Aktualizacja ruszyła w tle — możesz zamknąć tę stronę")


@router.post("/updates/snooze")
def snooze(panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    panel.db.set_setting("update_snooze", time.time() + updates.SNOOZE)
    return toast("Przypomnę jutro", "info")


@router.post("/updates/skip")
def skip(version: str = Form(...), panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    panel.db.set_setting("update_skip", updates.normalize(version))
    return toast(f"Pominięto wersję {version} — następną pokażę normalnie", "info")


@router.post("/updates/unskip")
def unskip(panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    panel.db.set_setting("update_skip", None)
    return refresh("Pominięta wersja znów będzie proponowana")
