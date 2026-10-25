"""Podatności: lista paczek ze znanymi CVE, skan na żądanie, ignorowanie, naprawa przez wdrożenie ze świeżym obrazem."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Form, Request

from deployra_common.jobs import Job, JobFailed

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import app_updates, deploy, vulns
from ..web import redirect, refresh, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
FIXABLE = ("image", "compose", "git", "runtime")


def _finding(panel: Panel, finding_id: int) -> dict:
    row = panel.db.get("vuln_findings", finding_id)
    if not row:
        raise LookupError(t("vuln.not_found"))
    return row


@router.get("/security")
def page(request: Request, job: str = "", show: str = "open", panel: Panel = Depends(get_panel)):
    show = show if show in ("open", "ignored", "resolved") else "open"
    rows = vulns.listing(panel, show)
    apps = {a["id"]: a for a in panel.db.fetchall("SELECT id, name, slug, kind, node_id, icon, domains FROM apps")}
    nodes = {n["id"]: n for n in panel.db.fetchall("SELECT id, name FROM nodes")}
    live = panel.jobs.get(job) if job else None
    return render(
        request, "security.html", title=t("vuln.title"), groups=vulns.group(rows), counts=vulns.counts(vulns.listing(panel)),
        tabs={state: len(vulns.listing(panel, state)) for state in ("ignored", "resolved")}, show=show, apps=apps, nodes=nodes,
        status=vulns.status(panel), config=vulns.settings(panel), running=vulns.running(panel), job=live, fixable=FIXABLE,
        intervals=vulns.INTERVALS, severities=vulns.SEVERITIES[:4],
    )  # fmt: skip


@router.post("/security/scan")
async def scan(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):
    if vulns.running(panel):
        return toast(t("vuln.already_running"), "warn", status=409)
    job = panel.jobs.start("security", t("vuln.job_title"), lambda job: vulns.scan(panel, job))
    return redirect(request, f"/security?job={job.id}")


@router.get("/settings/security")
def settings_page(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_SETTINGS))):
    return render(request, "settings/security.html", title=t("settings.tab_security"), tab="security", config=vulns.settings(panel),
                  status=vulns.status(panel), intervals=vulns.INTERVALS, severities=vulns.SEVERITIES[:4])  # fmt: skip


@router.post("/security/settings")
def save_settings(enabled: str = Form(""), interval_hours: int = Form(6), alert_severity: str = Form("high"), panel: Panel = Depends(get_panel),
                  user: dict = Depends(require(Permission.ALLOW_SETTINGS))):  # fmt: skip
    vulns.save_settings(panel, bool(enabled), interval_hours, alert_severity)
    return toast(t("msg.settings_saved"))


@router.post("/security/snooze")
def snooze(panel: Panel = Depends(get_panel)):
    panel.db.set_setting("vuln_banner_snooze", time.time() + 24 * 3600)
    return toast(t("vuln.snoozed"))


def _ignore(panel: Panel, row: dict) -> None:
    panel.db.update("vuln_findings", row["id"], status="ignored", ignored_ids=[e["id"] for e in row.get("vulns") or []])
    vulns.silence(panel, row["id"])


def _picked(form: Any) -> list[int]:
    """Zaznaczone grupy (aplikacja albo kontener) — każda to lista ID pozycji, np. "3,7,9"."""
    return [int(part) for value in form.getlist("ids") for part in str(value).split(",") if part.strip().isdigit()]


@router.post("/security/ignore-many")
async def ignore_many(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):
    rows = [r for r in (panel.db.get("vuln_findings", i) for i in _picked(await request.form())) if r and r["status"] == "open"]
    for row in rows:
        _ignore(panel, row)
    return refresh(t("vuln.ignored_many", count=len(rows)))


@router.post("/security/{finding_id}/ignore")
def ignore(finding_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):
    row = _finding(panel, finding_id)
    _ignore(panel, row)
    return refresh(t("vuln.ignored_msg", package=row["package"]))


@router.post("/security/{finding_id}/restore")
def restore(finding_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):
    row = _finding(panel, finding_id)
    if row["status"] == "ignored":
        panel.db.update("vuln_findings", row["id"], status="open", ignored_ids=[])
    return refresh()


def _busy(panel: Panel, app: dict) -> bool:
    current = panel.jobs.get(app.get("last_job") or "")
    return app["status"] == "deploying" and bool(current) and not current.done


def _image_changes(panel: Panel, app: dict) -> dict[str, str]:
    """Obraz z poprawką znaleziony przy skanie (poprawka dopiero w nowszej gałęzi): {obecny obraz: nowy}."""
    if app["kind"] != "image":
        return {}
    current = deploy.load(panel, app)[0].source.image or ""
    targets = {r["suggest"] for r in vulns.listing(panel, "open", app["id"]) if r.get("suggest")}
    return {current: sorted(targets, key=lambda image: vulns.vkey(image.rsplit(":", 1)[-1]))[-1]} if targets and current else {}


def _fix_job(panel: Panel, apps: list[dict]):
    """Aplikacje po kolei: wdrożenie ze świeżo pobranym obrazem (środowisko „z plików” budowane od nowa), na końcu
    jeden ponowny skan. Nieudane wdrożenie jednej aplikacji nie zatrzymuje reszty."""

    async def work(job: Job) -> dict:
        done, failed = [], []
        for index, app in enumerate(apps, 1):
            if len(apps) > 1:
                job.log(t("vuln.fix_step", index=index, count=len(apps), name=app["name"]))
            changes = _image_changes(panel, app)
            for old, new in changes.items():
                job.log(t("vuln.switching_image", old=old, new=new))
            app_updates.apply_versions(panel, panel.db.get("apps", app["id"]), changes)
            try:
                await deploy.run(panel, job, app["id"], refresh=True, verify=bool(changes), cleanup=True)
                done.append(app["name"])
            except (JobFailed, LookupError) as exc:
                if changes:  # nowa wersja nie wstała albo nie odpowiada — w ustawieniach aplikacji wraca stary obraz
                    app_updates.apply_versions(panel, panel.db.get("apps", app["id"]), {new: old for old, new in changes.items()})
                    job.log(t("vuln.switch_reverted", image=", ".join(changes)))
                failed.append(app["name"])
                if len(apps) == 1:
                    raise
                job.log(f"✖ {app['name']}: {exc}")
        job.log(t("vuln.rescan"))
        try:
            await vulns.scan(panel, job)  # błąd OSV skan zapisuje w logu sam
        except LookupError as exc:  # skan już trwa — wdrożenia i tak się udały
            job.log(str(exc))
        except JobFailed:
            pass
        for app in apps:  # świeży obraz nie pomógł — poprawka jest dopiero w nowszej gałęzi (np. PHP 8.0 → 8.2)
            for row in vulns.listing(panel, "open", app["id"]):
                if vulns.new_branch(row["version"], row["fixed"]):
                    job.log("⚠ " + t("vuln.needs_branch", name=app["name"], package=row["package"], version=row["version"], fixed=row["fixed"]))
        if failed:
            raise JobFailed(t("vuln.fix_partial", failed=", ".join(failed), done=len(done)))
        return {"fixed": done}

    return work


@router.post("/security/fix-many")
async def fix_many(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):
    """Zaznaczone aplikacje naprawiane po kolei w jednym zadaniu (kontenery spoza aplikacji pomijamy)."""
    apps: dict[int, dict] = {}
    for finding_id in _picked(await request.form()):
        row = panel.db.get("vuln_findings", finding_id)
        app = panel.db.get("apps", row["app_id"]) if row and row["app_id"] else None
        if app and app["kind"] in FIXABLE and not _busy(panel, app):
            apps.setdefault(app["id"], app)
    if not apps:
        return toast(t("vuln.cant_fix"), "warn", status=400)
    job = panel.jobs.start("deploy", t("vuln.fix_many_job", count=len(apps)), _fix_job(panel, list(apps.values())))
    return redirect(request, f"/security?job={job.id}")


@router.post("/security/{finding_id}/fix")
async def fix(request: Request, finding_id: int, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):
    """Wdrożenie aplikacji ze świeżo pobranym obrazem (środowisko „z plików” budowane od nowa), potem ponowny skan."""
    row = _finding(panel, finding_id)
    app = panel.db.get("apps", row["app_id"]) if row["app_id"] else None
    if not app or app["kind"] not in FIXABLE:
        return toast(t("vuln.cant_fix"), "warn", status=400)
    if _busy(panel, app):
        return toast(t("msg.deploy_running"), "warn", status=409)
    job = panel.jobs.start("deploy", t("vuln.fix_job", name=app["name"]), _fix_job(panel, [app]))
    return redirect(request, f"/apps/{app['id']}?job={job.id}")
