"""Aktualizacje obrazów aplikacji: ustawienia, lista, sprawdzanie na żądanie, aktualizacja jednej albo wszystkich."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request

from deployra_common.jobs import Job, JobFailed
from deployra_common.runtimes import RUNTIMES

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import app_updates, deploy
from ..web import redirect, refresh, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
SETTINGS = Depends(require(Permission.ALLOW_SETTINGS))
DEPLOY = Depends(require(Permission.ALLOW_DEPLOYMENT))


def _image(panel: Panel, app: dict) -> str:
    try:
        spec, _ = deploy.load(panel, app)
    except Exception:  # noqa: BLE001 — uszkodzona specyfikacja nie psuje listy
        return ""
    if spec.source.kind == "image":
        return spec.source.image or ""
    if spec.source.kind == "runtime" and spec.source.runtime in RUNTIMES:
        return RUNTIMES[spec.source.runtime].image
    return "docker-compose"


def _busy(panel: Panel, app: dict) -> bool:
    current = panel.jobs.get(app.get("last_job") or "")
    return app["status"] == "deploying" and bool(current) and not current.done


@router.get("/settings/app-updates")
def page(request: Request, job: str = "", panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    config = app_updates.settings(panel)
    apps = [a for a in panel.db.fetchall("SELECT * FROM apps ORDER BY name") if a["kind"] in app_updates.UPDATABLE]
    rows = []
    for app in apps:
        row = panel.db.get("app_updates", app["id"], key="app_id")
        ignored = app_updates.is_ignored(row)
        items = app_updates.available(panel, app["id"], with_ignored=True)
        image = _image(panel, app)
        rows.append({"app": app, "image": image, "found": [] if ignored else items, "ignored": items if ignored else [],
                     "policy": app.get("update_policy") or "",
                     "lines": [(app_updates.short(i), "" if i["image"] == image else i["image"]) for i in items]})  # fmt: skip
    actionable = sum(1 for r in rows if r["found"])
    return render(
        request, "settings/app_updates.html", title=t("settings.tab_app_updates"), tab="app_updates", config=config, rows=rows,
        status=app_updates.status(panel), running=app_updates.running(panel), job=panel.jobs.get(job) if job else None,
        intervals=app_updates.INTERVALS, actionable=actionable,
    )  # fmt: skip


@router.post("/settings/app-updates")
def save(mode: str = Form("notify"), interval_hours: int = Form(12), auto_patch: str = Form(""), panel: Panel = Depends(get_panel), user: dict = SETTINGS):
    app_updates.save_settings(panel, mode, interval_hours, bool(auto_patch))
    return toast(t("msg.settings_saved"))


@router.post("/settings/app-updates/check")
async def check(request: Request, panel: Panel = Depends(get_panel), user: dict = DEPLOY):
    if app_updates.running(panel):
        return toast(t("appupd.already_running"), "warn", status=409)
    job = panel.jobs.start("updates", t("appupd.check_job"), lambda job: app_updates.check(panel, job))
    return redirect(request, f"/settings/app-updates?job={job.id}")


def _picked(form: Any) -> list[int]:
    return [int(v) for v in form.getlist("app_ids") if str(v).isdigit()]


def _todo(panel: Panel, only: list[int] | None = None) -> list[tuple[dict, dict[str, str]]]:
    """Aplikacje do aktualizacji (bez zignorowanych, trybu „off” i trwających wdrożeń) z planem zmian tagów."""
    config = app_updates.settings(panel)
    todo = []
    for row in panel.db.fetchall("SELECT * FROM app_updates ORDER BY app_id"):
        app = panel.db.get("apps", row["app_id"])
        if not app or (only is not None and app["id"] not in only) or _busy(panel, app) or app_updates.is_ignored(row):
            continue
        if only is None and app_updates.policy(panel, app, config) == "off":
            continue
        changes = app_updates.plan(panel, app, row.get("items") or [], config, auto=False)
        if changes is not None:
            todo.append((app, changes))
    return todo


@router.post("/settings/app-updates/all")
async def update_all(request: Request, panel: Panel = Depends(get_panel), user: dict = DEPLOY):
    """Wszystkie (albo zaznaczone) aplikacje z aktualizacją, po kolei w jednym zadaniu (nowe wersje — z tej samej linii)."""
    form = await request.form()
    picked = _picked(form)
    todo = _todo(panel, picked if "app_ids" in form else None)
    if not todo:
        return toast(t("appupd.nothing"), "warn")

    async def work(job: Job) -> dict[str, Any]:
        done = []
        for app, changes in todo:
            job.log(t("appupd.log_auto", name=app["name"]))
            try:
                await app_updates.update_app(panel, job, app["id"], changes)
                done.append(app["name"])
            except (JobFailed, LookupError) as exc:
                job.log(f"✖ {app['name']}: {exc}")
        return {"updated": done}

    job = panel.jobs.start("deploy", t("appupd.all_job", count=len(todo)), work)
    return redirect(request, f"/settings/app-updates?job={job.id}")


@router.post("/settings/app-updates/ignore")
async def ignore(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):
    form = await request.form()
    picked = _picked(form) or [int(form.get("app_id") or 0)]
    count = app_updates.ignore(panel, picked)
    return refresh(t("appupd.ignored_msg", count=count))


@router.post("/settings/app-updates/{app_id}/unignore")
def unignore(app_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):
    app_updates.unignore(panel, app_id)
    return refresh()


@router.post("/apps/{app_id}/update")
async def update_one(request: Request, app_id: int, panel: Panel = Depends(get_panel), user: dict = DEPLOY):
    app = panel.db.get("apps", app_id)
    if not app:
        raise LookupError(t("msg.no_app"))
    if _busy(panel, app):
        return toast(t("msg.deploy_running"), "warn", status=409)
    changes = app_updates.plan(panel, app, app_updates.available(panel, app_id), app_updates.settings(panel), auto=False)
    if changes is None:
        return toast(t("appupd.nothing"), "warn")
    job = panel.jobs.start("deploy", t("appupd.one_job", name=app["name"]), lambda job: app_updates.update_app(panel, job, app_id, changes))
    return redirect(request, f"/apps/{app_id}?job={job.id}")


@router.post("/apps/{app_id}/update-policy")
def update_policy(app_id: int, policy: str = Form(""), panel: Panel = Depends(get_panel),
                  user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    if not panel.db.get("apps", app_id):
        raise LookupError(t("msg.no_app"))
    app_updates.set_policy(panel, app_id, policy)
    return toast(t("msg.settings_saved"))
