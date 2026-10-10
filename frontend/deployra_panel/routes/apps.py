"""Aplikacje: kreator (obraz / compose / statyczna / Git), szczegóły, redeploy, rollback, upload, webhooki."""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import ValidationError

from deployra_common.jobs import Job
from deployra_common.schemas import AppSpec, VolumeMount

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..permissions import Permission
from ..services import deploy, relay
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
MAX_UPLOAD = 1024 * 1024 * 1024  # 1 GB


def slugify(text: str) -> str:
    table = str.maketrans("ąćęłńóśźż", "acelnoszz")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(table)).strip("-")
    return slug[:40].strip("-") or "app"


def _app(app_id: int, panel: Panel = Depends(get_panel)) -> dict:
    app = panel.db.get("apps", app_id)
    if not app:
        raise HTTPException(404, "Nie ma takiej aplikacji")
    return app


def _nodes(panel: Panel) -> list[dict]:
    return panel.db.fetchall("SELECT * FROM nodes WHERE status = 'paired' ORDER BY name")


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r\n", "\n").splitlines() if line.strip() and not line.strip().startswith("#")]


def _env(text: str) -> dict[str, str]:
    out = {}
    for line in _lines(text):
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"zmienna „{line}” — oczekiwano KLUCZ=wartość")
        out[key.strip()] = value.strip()
    return out


def _volumes(text: str) -> list[VolumeMount]:
    out = []
    for line in _lines(text):
        source, sep, target = line.rpartition(":")
        ro = False
        if target in ("ro", "rw") and ":" in source:
            ro, (source, _, target) = target == "ro", source.rpartition(":")
        if not sep:
            raise ValueError(f"wolumen „{line}” — oczekiwano źródło:/ścieżka")
        out.append(VolumeMount(source=source, target=target, read_only=ro))
    return out


def spec_from_form(form: Any, slug: str, existing: AppSpec | None = None) -> tuple[AppSpec, dict]:
    kind = str(form.get("kind") or (existing.source.kind if existing else "image"))
    get = lambda key, default="": str(form.get(key) or default).strip()  # noqa: E731
    domains = [d.lower() for d in _lines(get("domains").replace(",", "\n"))]
    source: dict[str, Any] = {"kind": kind}
    if kind == "image":
        source["image"] = get("image")
    elif kind == "compose":
        source["compose"] = str(form.get("compose") or "").replace("\r\n", "\n")
        source["expose_service"] = get("expose_service") or None
    elif kind == "git":
        source.update(
            git_url=get("git_url"), git_branch=get("git_branch", "main"), dockerfile=get("dockerfile", "Dockerfile"),
            build_context=get("build_context", "."), git_static_dir=get("git_static_dir") or None,
        )  # fmt: skip
        token, key = get("git_token"), str(form.get("git_ssh_key") or "").strip()
        keep = existing.source if existing else None
        source["git_token"] = token or (keep.git_token if keep else None)
        source["git_ssh_key"] = key or (keep.git_ssh_key if keep else None)
    port = get("container_port")
    spec = AppSpec(
        slug=slug,
        source=source,
        endpoint=get("endpoint", "local"),
        container_port=int(port) if port else None,
        env=_env(str(form.get("env") or "")),
        volumes=_volumes(str(form.get("volumes") or "")),
        memory_mb=int(get("memory_mb")) if get("memory_mb") else None,
        cpus=float(get("cpus")) if get("cpus") else None,
        domains=domains,
    )
    options = {"https": bool(form.get("https")), "force_ssl": bool(form.get("force_ssl")), "dns": bool(form.get("dns"))}
    return spec, options


def start_deploy(panel: Panel, app: dict) -> Job:
    return panel.jobs.start("deploy", f"Deploy {app['slug']}", lambda job: deploy.run(panel, job, app["id"]))


def _errors(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "\n".join(f"{'.'.join(str(p) for p in e['loc'] if p != 'source')}: {e['msg'].removeprefix('Value error, ')}" for e in exc.errors())
    return str(exc)


# --- lista i kreator ---------------------------------------------------------------------


@router.get("/apps")
def apps(request: Request, panel: Panel = Depends(get_panel)):
    rows = panel.db.fetchall("SELECT a.*, n.name AS node_name FROM apps a LEFT JOIN nodes n ON n.id = a.node_id ORDER BY a.name")
    from ..services import uptime

    stats = uptime.stats_for(panel, [("app", a["id"]) for a in rows])
    return render(request, "apps/list.html", title="Aplikacje", apps=rows, stats=stats)


@router.get("/apps/new")
def new_app(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):
    settings = panel.db.settings()
    return render(
        request, "apps/new.html", title="Nowa aplikacja", nodes=_nodes(panel), base_domain=settings.get("base_domain", ""),
        has_cf=bool(settings.get("cloudflare_token")), has_gateway=bool(panel.gateway()),
    )  # fmt: skip


@router.post("/apps/new")
async def create_app(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):
    form = await request.form()
    name = str(form.get("name") or "").strip()[:60]
    slug = slugify(str(form.get("slug") or name))
    node = panel.node(int(form.get("node_id") or 0))
    if not node or node["status"] != "paired":
        return toast("Wybierz sparowany węzeł", "err", status=400)
    try:
        spec, options = spec_from_form(form, slug)
        app_id = deploy.create_app_row(panel, name or slug, node["id"], spec, options)
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)
    app = panel.db.get("apps", app_id)
    job = start_deploy(panel, app)
    panel.notify(f"{user['email']} wdraża aplikację {slug} na {node['name']}")
    return redirect(request, f"/apps/{app_id}?job={job.id}")


# --- szczegóły ---------------------------------------------------------------------------


@router.get("/apps/{app_id}")
def app_page(request: Request, job: str = "", app: dict = Depends(_app), panel: Panel = Depends(get_panel)):
    spec, options = deploy.load(panel, app)
    node = panel.node(app["node_id"])
    site = panel.db.get("sites", app["site_id"]) if app.get("site_id") else None
    cert = panel.db.get("certificates", app["cert_id"]) if app.get("cert_id") else None
    job_id = job or app.get("last_job") or ""
    live_job = panel.jobs.get(job_id) if job_id else None
    hook_url = None
    if spec.source.kind == "git":
        domain = panel.db.setting("panel_domain") or request.url.hostname
        hook_url = f"https://{domain}/hooks/git/{app['id']}"
    from ..services import uptime

    monitor = uptime.monitor_for(panel, "app", app["id"])
    return render(
        request, "apps/detail.html", title=app["name"], app=app, spec=spec, options=options, node=node, site=site, cert=cert,
        job=live_job, hook_url=hook_url, hook_secret=panel.sealer.unseal(app.get("webhook_secret")),
        monitor=monitor, monitor_stats=uptime.stats_for(panel, [("app", app["id"])]).get(("app", app["id"])),
        settings=panel.db.settings(),
    )  # fmt: skip


@router.post("/apps/{app_id}/deploy")
async def redeploy(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
             user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):  # fmt: skip
    if app["status"] == "deploying" and panel.jobs.get(app.get("last_job") or "") and not panel.jobs.get(app["last_job"]).done:
        return toast("Wdrożenie już trwa", "warn", status=409)
    job = start_deploy(panel, app)
    return redirect(request, f"/apps/{app['id']}?job={job.id}")


@router.post("/apps/{app_id}/rollback")
async def rollback(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
             user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    spec, _ = deploy.load(panel, app)
    node = panel.node(app["node_id"])

    async def work(job: Job) -> Any:
        client = panel.node_client(node)
        result = await relay.start_and_follow(client, f"/api/apps/{app['slug']}/rollback", spec.model_dump(mode="json"), job)
        panel.db.update("apps", app["id"], status="running")
        return result

    job = panel.jobs.start("rollback", f"Rollback {app['slug']}", work)
    panel.db.update("apps", app["id"], last_job=job.id)
    return redirect(request, f"/apps/{app['id']}?job={job.id}")


@router.post("/apps/{app_id}/remove")
async def remove(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_DELETE))):  # fmt: skip
    form = await request.form()
    delete_data = bool(form.get("delete_data"))
    job = panel.jobs.start("remove", f"Usuwanie {app['slug']}", lambda job: deploy.remove(panel, job, app["id"], delete_data))
    from ..services import uptime

    uptime.remove_for(panel, "app", app["id"])
    return redirect(request, f"/jobs/{job.id}", f"Usuwam {app['slug']}…")


@router.post("/apps/{app_id}/upload")
async def upload(request: Request, file: UploadFile, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    if app["kind"] not in ("static", "git"):
        return toast("Upload plików działa dla stron statycznych", "err", status=400)
    if not (file.filename or "").lower().endswith(".zip"):
        return toast("Wgraj archiwum .zip", "err", status=400)
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=panel.config.data_dir / "uploads")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    return toast("Plik jest za duży (max 1 GB)", "err", status=413)
                out.write(chunk)
        node = panel.node(app["node_id"])
        result = await panel.node_client(node).upload_file(f"/api/apps/{app['slug']}/upload", tmp)
    finally:
        os.unlink(tmp)
    return toast(f"Opublikowano {result.get('files', 0)} plików ✔")


@router.get("/apps/{app_id}/edit")
def edit_page(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
              user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    spec, options = deploy.load(panel, app)
    settings = panel.db.settings()
    return render(request, "apps/edit.html", title=f"Ustawienia · {app['name']}", app=app, spec=spec, options=options,
                  node=panel.node(app["node_id"]), has_cf=bool(settings.get("cloudflare_token")))  # fmt: skip


@router.post("/apps/{app_id}/edit")
async def edit(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
               user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    form = await request.form()
    old_spec, _ = deploy.load(panel, app)
    try:
        spec, options = spec_from_form(form, app["slug"], old_spec)
        if spec.endpoint != old_spec.endpoint:
            raise ValueError("zmiana endpointu wymaga usunięcia i ponownego utworzenia aplikacji")
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)
    panel.db.update("apps", app["id"], name=str(form.get("name") or app["name"])[:60], spec=deploy.store(panel, spec, options),
                    domains=spec.domains)  # fmt: skip
    if form.get("deploy"):
        job = start_deploy(panel, panel.db.get("apps", app["id"]))
        return redirect(request, f"/apps/{app['id']}?job={job.id}", "Zapisano — wdrażam")
    return redirect(request, f"/apps/{app['id']}", "Zapisano. Zmiany zadziałają po ponownym wdrożeniu.")


@router.post("/apps/{app_id}/webhook/rotate")
def rotate_webhook(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                   user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    panel.db.update("apps", app["id"], webhook_secret=panel.sealer.seal(secrets.token_urlsafe(24)))
    return redirect(request, f"/apps/{app['id']}", "Wygenerowano nowy sekret webhooka — zaktualizuj go w repozytorium")
