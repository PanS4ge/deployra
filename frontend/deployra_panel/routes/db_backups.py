"""Kopie baz danych w ustawieniach kopii: harmonogramy, zrzut teraz, lista kopii, pobieranie i przywracanie."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import db_backups
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
ADMIN = Depends(require(Permission.ALLOW_RISKY_SETTINGS))


@router.get("/settings/backup/db/picker")
async def picker(request: Request, panel: Panel = Depends(get_panel), user: dict = ADMIN):
    containers = await db_backups.database_containers(panel)
    options = [(f"{c['node_id']}:{c['container']}", f"{c['container']} · {c['node']}", c["title"]) for c in containers]
    return render(request, "settings/_db_picker.html", options=options)


@router.post("/settings/backup/db")
async def create_schedule(request: Request, panel: Panel = Depends(get_panel), user: dict = ADMIN):
    form = await request.form()
    node_id, _, container = str(form.get("target") or "").partition(":")
    if not node_id.isdigit():
        raise ValueError(t("backup.db_err_container"))
    db_backups.create(panel, int(node_id), container, str(form.get("cron") or ""), form.get("retention"), bool(form.get("enabled")))
    return redirect(request, "/settings/backup", t("backup.db_saved"))


@router.post("/settings/backup/db/{sid}")
async def update_schedule(sid: int, request: Request, panel: Panel = Depends(get_panel), user: dict = ADMIN):
    form = await request.form()
    db_backups.update(panel, sid, str(form.get("cron") or ""), form.get("retention"), bool(form.get("enabled")))
    return redirect(request, "/settings/backup", t("backup.db_saved"))


@router.post("/settings/backup/db/{sid}/delete")
async def delete_schedule(sid: int, request: Request, panel: Panel = Depends(get_panel), user: dict = ADMIN):
    db_backups.delete(panel, sid)
    return redirect(request, "/settings/backup", t("backup.db_deleted"))


@router.post("/settings/backup/db/{sid}/run")
async def run_now(sid: int, panel: Panel = Depends(get_panel), user: dict = ADMIN):
    if not db_backups.start(panel, sid):
        return toast(t("backup.db_already_running"), "warn", status=409)
    return toast(t("backup.db_started"))


@router.get("/settings/backup/db/{sid}/files")
async def files(sid: int, request: Request, panel: Panel = Depends(get_panel), user: dict = ADMIN):
    sched = db_backups.get(panel, sid)
    try:
        dumps = await run_in_threadpool(db_backups.stored_dumps, panel, sid)
        error = ""
    except Exception as exc:  # noqa: BLE001 — niedostępny magazyn pokazujemy w miejscu listy
        dumps, error = [], db_backups.error_text(exc)
    return render(request, "settings/_db_files.html", sid=sid, dumps=dumps, error=error, container=sched["container"])


@router.get("/settings/backup/db/{sid}/download/{name}")
async def download(sid: int, name: str, panel: Panel = Depends(get_panel), user: dict = ADMIN):
    content = await run_in_threadpool(db_backups.read_dump, panel, sid, name)
    return Response(content=content, media_type="application/gzip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/settings/backup/db/{sid}/restore")
async def restore(sid: int, request: Request, panel: Panel = Depends(get_panel), user: dict = ADMIN):
    form = await request.form()
    container = await db_backups.restore(panel, sid, str(form.get("name") or ""))
    return toast(t("backup.db_restored", container=container))
