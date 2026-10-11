"""Ustawienia → Pluginy: lista, włączanie, wgrywanie ZIP i usuwanie."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from ..context import Panel, get_panel
from ..deps import Forbidden, current_user, require
from ..i18n import t
from ..permissions import Permission, has
from ..plugins import MAX_ZIP
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
SETTINGS = Depends(require(Permission.ALLOW_SETTINGS))
RISKY = Depends(require(Permission.ALLOW_RISKY_SETTINGS))


@router.get("/settings/plugins")
def page(request: Request, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    plugins = sorted(panel.plugins.plugins.values(), key=lambda p: (not p.builtin, p.name.lower()))
    return render(request, "settings/plugins.html", title=t("settings.tab_plugins"), tab="plugins", plugins=plugins,
                  plugins_dir=panel.config.plugins_dir)  # fmt: skip


@router.post("/settings/plugins/rescan")
def rescan(request: Request, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    panel.plugins.discover()
    return redirect(request, "/settings/plugins", t("plugins.rescanned", count=len(panel.plugins.plugins)))


@router.post("/settings/plugins/{plugin_id}/enable")
def enable(request: Request, plugin_id: str, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    plugin = panel.plugins.plugins.get(plugin_id)
    # kod z zewnątrz działa z pełnymi uprawnieniami panelu — włącza go tylko ktoś z ryzykownymi ustawieniami
    if plugin and "code" in plugin.capabilities and not plugin.builtin and not has(user, Permission.ALLOW_RISKY_SETTINGS):
        raise Forbidden(t("plugins.code_needs_risky"))
    plugin = panel.plugins.enable(plugin_id)
    if plugin.error:
        return redirect(request, "/settings/plugins", t("plugins.failed", name=plugin.name, error=plugin.error), "err")
    return redirect(request, "/settings/plugins", t("plugins.enabled", name=plugin.name))


@router.post("/settings/plugins/{plugin_id}/disable")
def disable(request: Request, plugin_id: str, panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    plugin = panel.plugins.disable(plugin_id)
    return redirect(request, "/settings/plugins", t("plugins.disabled", name=plugin.name))


@router.post("/settings/plugins/upload")
async def upload(request: Request, file: UploadFile, panel: Panel = Depends(get_panel), user: dict = RISKY):
    if not (file.filename or "").lower().endswith(".zip"):
        return toast(t("plugins.zip_only"), "err", status=400)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plugin.zip"
        size = 0
        with path.open("wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_ZIP:
                    return toast(t("plugins.zip_too_big"), "err", status=413)
                out.write(chunk)
        plugin = await run_in_threadpool(panel.plugins.install_zip, path)
    return redirect(request, "/settings/plugins", t("plugins.installed", name=plugin.name))


@router.post("/settings/plugins/{plugin_id}/delete")
def delete(request: Request, plugin_id: str, panel: Panel = Depends(get_panel), user: dict = RISKY):
    name = panel.plugins._get(plugin_id).name
    panel.plugins.remove(plugin_id)
    return redirect(request, "/settings/plugins", t("plugins.removed", name=name))
