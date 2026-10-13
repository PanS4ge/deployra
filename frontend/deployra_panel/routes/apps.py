"""Aplikacje: kreator (obraz / compose / statyczna / Git / z plików), szczegóły, redeploy, rollback, upload, webhooki."""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import tempfile
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import ValidationError

from deployra_common.jobs import Job
from deployra_common.runtimes import RUNTIMES
from deployra_common.schemas import AppSpec, VolumeMount

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import charts, deploy, migrate, relay, resources
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
        raise HTTPException(404, t("msg.no_app"))
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
            raise ValueError(t("msg.bad_env", line=line))
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] == '"':  # "…" jak w .env — tak pokazujemy wartości wieloliniowe
            try:
                value = str(json.loads(value))
            except ValueError:
                pass
        out[key.strip()] = value
    return out


def _volumes(text: str) -> list[VolumeMount]:
    out = []
    for line in _lines(text):
        source, sep, target = line.rpartition(":")
        ro = False
        if target in ("ro", "rw") and ":" in source:
            ro, (source, _, target) = target == "ro", source.rpartition(":")
        if not sep:
            raise ValueError(t("msg.bad_volume", line=line))
        out.append(VolumeMount(source=source, target=target, read_only=ro))
    return out


def spec_from_form(form: Any, slug: str, existing: AppSpec | None = None) -> tuple[AppSpec, dict]:
    kind = str(form.get("kind") or (existing.source.kind if existing else "image"))
    get = lambda key, default="": str(form.get(key) or default).strip()  # noqa: E731
    domains = [d.lower() for d in _lines(get("domains").replace(",", "\n"))]
    source: dict[str, Any] = {"kind": kind}
    if kind == "image":
        source["image"] = get("image")
        command = str(form.get("command") or "").strip()
        if "command" in form:
            source["command"] = shlex.split(command) if command else None
        elif existing and existing.source.kind == "image":
            source["command"] = existing.source.command
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
    elif kind == "runtime":
        source.update(runtime=get("runtime"), start_command=get("start_command") or None, install_command=get("install_command") or None)
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
        host_port=existing.host_port if existing else None,  # stały port hosta (np. przejęty kontener) — nie ma go w formularzu
    )
    if spec.source.kind == "runtime" and not spec.container_port:  # proces w tle nie ma czego wystawiać pod domeną
        spec.domains = []
    options = {"https": bool(form.get("https")), "force_ssl": bool(form.get("force_ssl")), "dns": bool(form.get("dns"))}
    return spec, options


async def _save_upload(panel: Panel, file: Any) -> str | None:
    """ZIP z formularza → plik tymczasowy (None, gdy nic nie wybrano)."""
    if file is None or not getattr(file, "filename", ""):
        return None
    if not file.filename.lower().endswith(".zip"):
        raise ValueError(t("msg.upload_zip"))
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=panel.config.data_dir / "uploads")
    size = 0
    with os.fdopen(fd, "wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                out.close()
                os.unlink(tmp)
                raise ValueError(t("msg.file_too_big_gb"))
            out.write(chunk)
    return tmp


async def _publish(panel: Panel, node: dict, slug: str, tmp: str) -> int:
    try:
        result = await panel.node_client(node).upload_file(f"/api/apps/{slug}/upload", tmp)
    finally:
        os.unlink(tmp)
    return (result or {}).get("files", 0)


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
    return render(request, "apps/list.html", title=t("nav.apps"), apps=rows, stats=stats)


@router.get("/apps/new")
def new_app(request: Request, template: str = "", panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):
    settings = panel.db.settings()
    prefill = panel.plugins.app_template(template) if template else None
    nodes = _nodes(panel)
    free = {n["id"]: resources.node_free(n["id"]) for n in nodes}
    return render(
        request, "apps/new.html", title=t("apps.new"), nodes=nodes, free=free, base_domain=settings.get("base_domain", ""),
        has_cf=bool(settings.get("cloudflare_token")), has_gateway=bool(panel.gateway()), prefill=prefill,
        app_templates=panel.plugins.app_templates(), template=template,
    )  # fmt: skip


@router.post("/apps/new")
async def create_app(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):
    form = await request.form()
    name = str(form.get("name") or "").strip()[:60]
    slug = slugify(str(form.get("slug") or name))
    node = panel.node(int(form.get("node_id") or 0))
    if not node or node["status"] != "paired":
        return toast(t("msg.pick_paired_node"), "err", status=400)
    try:
        spec, options = spec_from_form(form, slug)
        if panel.db.get("apps", spec.slug, key="slug"):
            raise ValueError(t("msg.app_exists", slug=spec.slug))
        upload = await _save_upload(panel, form.get("file")) if spec.source.kind in ("static", "runtime") else None
        if upload:  # pliki trafiają na węzeł przed pierwszym wdrożeniem — zamiast plików startowych
            await _publish(panel, node, spec.slug, upload)
        app_id = deploy.create_app_row(panel, name or slug, node["id"], spec, options)
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)
    app = panel.db.get("apps", app_id)
    job = start_deploy(panel, app)
    panel.notify(t("msg.notify_deploying", user=user["email"], slug=slug, node=node["name"]))
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
    others = [(n, resources.node_free(n["id"])) for n in _nodes(panel) if n["id"] != app["node_id"]]
    return render(
        request, "apps/detail.html", title=app["name"], app=app, spec=spec, options=options, node=node, site=site, cert=cert,
        job=live_job, hook_url=hook_url, hook_secret=panel.sealer.unseal(app.get("webhook_secret")),
        monitor=monitor, monitor_stats=uptime.stats_for(panel, [("app", app["id"])]).get(("app", app["id"])),
        settings=panel.db.settings(), other_nodes=others, auto_capable=migrate.auto_capable(spec),
        acls=panel.db.fetchall("SELECT id, name FROM access_lists ORDER BY name"),
    )  # fmt: skip


@router.post("/apps/{app_id}/access")
async def set_access(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                     user: dict = Depends(require(Permission.ALLOW_PROXY))):  # fmt: skip
    """Lista dostępu (hasło / adresy IP) na hoście proxy aplikacji — jak „Access List” w NPM."""
    from ..services import proxy_state

    form = await request.form()
    if not app.get("site_id"):
        return toast(t("apps.access_no_domain"), "err", status=400)
    acl_id = int(form.get("access_list_id") or 0) or None
    if acl_id and not panel.db.get("access_lists", acl_id):
        return toast(t("apps.access_missing"), "err", status=400)
    panel.db.update("sites", app["site_id"], access_list_id=acl_id)
    message, level = proxy_state.push_message(await proxy_state.push(panel))
    return toast(message, level if level != "ok" else "ok")


# --- przejęcie kontenera spoza Deployry -----------------------------------------------------------


@router.get("/nodes/{node_id}/containers/{cid}/adopt")
async def adopt_page(request: Request, cid: str, node_id: int, panel: Panel = Depends(get_panel),
                     user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):  # fmt: skip
    node = panel.node(node_id)
    if not node or node["status"] != "paired":
        raise HTTPException(404, t("msg.pick_paired_node"))
    plan = await panel.node_client(node).get(f"/api/containers/{cid}/adopt-plan")
    settings = panel.db.settings()
    slug = slugify(plan["name"])
    return render(request, "apps/adopt.html", title=t("adopt.title", name=plan["name"]), node=node, cid=cid, plan=plan, slug=slug,
                  base_domain=settings.get("base_domain", ""), has_cf=bool(settings.get("cloudflare_token")),
                  has_gateway=bool(panel.gateway()))  # fmt: skip


@router.post("/nodes/{node_id}/containers/{cid}/adopt")
async def adopt(request: Request, cid: str, node_id: int, panel: Panel = Depends(get_panel),
                user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):  # fmt: skip
    node = panel.node(node_id)
    if not node or node["status"] != "paired":
        return toast(t("msg.pick_paired_node"), "err", status=400)
    form = await request.form()
    plan = await panel.node_client(node).get(f"/api/containers/{cid}/adopt-plan")  # specyfikacja z węzła, nie z formularza
    if plan["problems"]:
        return toast(plan["problems"][0], "err", status=400)
    name = str(form.get("name") or plan["name"]).strip()[:60]
    slug = slugify(str(form.get("slug") or name))
    port = next((p for p in plan["ports"] if str(p["container"]) == str(form.get("port"))), (plan["ports"] or [None])[0])
    if not port:
        return toast(t("adopt.no_port"), "err", status=400)
    try:
        spec = AppSpec(
            slug=slug, source={"kind": "image", "image": plan["image"], "command": plan["command"]}, container_port=port["container"],
            host_port=port.get("host"), env=plan["env"], volumes=[VolumeMount(**v) for v in plan["volumes"]],
            memory_mb=plan["memory_mb"], cpus=plan["cpus"],
            domains=[d.lower() for d in _lines(str(form.get("domains") or "").replace(",", "\n"))],
        )  # fmt: skip
        options = {"https": bool(form.get("https")), "force_ssl": bool(form.get("force_ssl")), "dns": bool(form.get("dns"))}
        app_id = deploy.create_app_row(panel, name or slug, node["id"], spec, options)
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)
    title = t("adopt.job", name=plan["name"], slug=slug)
    job = panel.jobs.start("deploy", title, lambda job: deploy.run(panel, job, app_id, adopt_container=cid))
    panel.db.update("apps", app_id, last_job=job.id)
    panel.notify(t("adopt.notify", user=user["email"], name=plan["name"], slug=slug))
    return redirect(request, f"/apps/{app_id}?job={job.id}")


@router.get("/apps/{app_id}/resources")
def app_resources(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel)):
    from .nodes import history_labels

    data = resources.app_history(panel, app["id"])
    mem_chart = cpu_chart = None
    cut = resources.trimmed(data, ("mem", "limit", "cpu"))
    if cut:
        starts, v = cut
        labels = history_labels(starts, data["step"])
        series = [("RAM MB", v["mem"], "#22d3ee", True)]
        if any(v["limit"]):
            series.append((t("res.limit") + " MB", v["limit"], "#f472b6", False))
        mem_chart = charts.area_chart(labels, series, height=160)
        cpu_chart = charts.area_chart(labels, [("CPU %", v["cpu"], "#8b5cf6", True)], height=160)
    current = ((resources.LATEST.get(app["node_id"]) or {}).get("apps") or {}).get(app["slug"])
    return render(request, "partials/app_resources.html", app=app, current=current, mem_chart=mem_chart, cpu_chart=cpu_chart)


@router.post("/apps/{app_id}/move")
async def move(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
               user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    form = await request.form()
    target = panel.node(int(form.get("node_id") or 0))
    if not target or target["status"] != "paired" or target["id"] == app["node_id"]:
        return toast(t("msg.pick_paired_node"), "err", status=400)
    running = panel.jobs.get(app.get("last_job") or "")
    if app["status"] == "deploying" and running and not running.done:
        return toast(t("msg.deploy_running"), "warn", status=409)
    delete_old = bool(form.get("delete_old"))
    job = panel.jobs.start("move", t("move.job_title", app=app["slug"], target=target["name"]),
                           lambda job: migrate.run(panel, job, app["id"], target["id"], delete_old))  # fmt: skip
    panel.db.update("apps", app["id"], last_job=job.id)
    panel.notify(t("move.notify_started", user=user["email"], app=app["name"], target=target["name"]))
    return redirect(request, f"/apps/{app['id']}?job={job.id}")


@router.post("/apps/{app_id}/auto-move")
async def auto_move(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                    user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    form = await request.form()
    spec, options = deploy.load(panel, app)
    enabled = bool(form.get("auto_move")) and migrate.auto_capable(spec)
    panel.db.update("apps", app["id"], spec=deploy.store(panel, spec, {**options, "auto_move": enabled}))
    return toast(t("move.auto_on") if enabled else t("move.auto_off"))


@router.post("/apps/{app_id}/deploy")
async def redeploy(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
             user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):  # fmt: skip
    if app["status"] == "deploying" and panel.jobs.get(app.get("last_job") or "") and not panel.jobs.get(app["last_job"]).done:
        return toast(t("msg.deploy_running"), "warn", status=409)
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
    job = panel.jobs.start("remove", t("jobs.title_remove", name=app["slug"]), lambda job: deploy.remove(panel, job, app["id"], delete_data))
    from ..services import uptime

    uptime.remove_for(panel, "app", app["id"])
    return redirect(request, f"/jobs/{job.id}", t("msg.removing", slug=app["slug"]))


@router.post("/apps/{app_id}/upload")
async def upload(request: Request, file: UploadFile, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                 user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    if app["kind"] not in ("static", "git", "runtime"):
        return toast(t("msg.upload_static_only"), "err", status=400)
    try:
        tmp = await _save_upload(panel, file)
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    if not tmp:
        return toast(t("msg.upload_zip"), "err", status=400)
    count = await _publish(panel, panel.node(app["node_id"]), app["slug"], tmp)
    spec, _ = deploy.load(panel, app)
    if spec.source.kind == "runtime" and not RUNTIMES[spec.source.runtime or ""].live:  # nowy kod → zależności i restart
        job = start_deploy(panel, app)
        return redirect(request, f"/apps/{app['id']}?job={job.id}", t("msg.published_redeploy", count=count))
    return toast(t("msg.published_files", count=count))


@router.get("/apps/{app_id}/edit")
def edit_page(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
              user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    spec, options = deploy.load(panel, app)
    settings = panel.db.settings()
    return render(request, "apps/edit.html", title=t("msg.title_app_settings", name=app["name"]), app=app, spec=spec, options=options,
                  node=panel.node(app["node_id"]), has_cf=bool(settings.get("cloudflare_token")))  # fmt: skip


@router.post("/apps/{app_id}/edit")
async def edit(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
               user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    form = await request.form()
    old_spec, old_options = deploy.load(panel, app)
    try:
        spec, options = spec_from_form(form, app["slug"], old_spec)
        options["auto_move"] = bool(old_options.get("auto_move")) and migrate.auto_capable(spec)
        if not panel.db.setting("cloudflare_token"):  # pole DNS jest wtedy wyłączone i nie przychodzi z formularza
            options["dns"] = bool(old_options.get("dns"))
        if spec.endpoint != old_spec.endpoint:
            raise ValueError(t("msg.endpoint_change"))
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)
    changes = _changes(old_spec, spec)
    decision = str(form.get("decision") or "")
    if changes and decision not in ("deploy", "save"):
        # zmiana w kontenerze → najpierw pytamy: postawić od nowa teraz czy tylko zapisać (nic jeszcze nie zapisujemy)
        response = render(request, "partials/edit_confirm.html", changes=changes, running=bool(app.get("host_port")))
        response.headers.update({"HX-Retarget": "#edit-confirm", "HX-Reswap": "innerHTML"})
        return response
    panel.db.update("apps", app["id"], name=str(form.get("name") or app["name"])[:60], spec=deploy.store(panel, spec, options),
                    domains=spec.domains)  # fmt: skip
    if changes:
        if decision == "deploy":
            job = start_deploy(panel, panel.db.get("apps", app["id"]))
            return redirect(request, f"/apps/{app['id']}?job={job.id}", t("msg.saved_deploying"))
        return redirect(request, f"/apps/{app['id']}", t("msg.saved_redeploy"))
    exposure = spec.domains != old_spec.domains or any(bool(options.get(k)) != bool(old_options.get(k)) for k in ("https", "force_ssl", "dns"))
    if exposure and app.get("host_port"):
        if deploy.static_kind(spec):  # strona statyczna: domeny są w nginx węzła, wdrożenie to tylko przeładowanie
            job = start_deploy(panel, panel.db.get("apps", app["id"]))
            return redirect(request, f"/apps/{app['id']}?job={job.id}", t("msg.saved_deploying"))
        # tylko domeny / HTTPS / DNS → bramka i DNS, kontener działa dalej
        job = panel.jobs.start("deploy", t("deploy.domains_job", slug=app["slug"]), lambda job: deploy.expose_only(panel, job, app["id"]))
        panel.db.update("apps", app["id"], last_job=job.id)
        return redirect(request, f"/apps/{app['id']}?job={job.id}", t("msg.saved_domains"))
    return redirect(request, f"/apps/{app['id']}", t("msg.saved_plain"))


def _container_part(spec: AppSpec) -> dict:
    """Wszystko, co trafia do kontenera (bez domen); puste polecenie [] i brak polecenia to to samo."""
    data = spec.model_dump(exclude={"domains"})
    data["source"]["command"] = data["source"].get("command") or None
    return data


def _changes(old: AppSpec, new: AppSpec) -> list[str]:
    """Co zmieniło się w kontenerze — nazwy pól do pytania „postawić od nowa?” (puste = tylko domeny / nic)."""
    a, b = _container_part(old), _container_part(new)
    keys = [k for k in a["source"] if a["source"][k] != b["source"].get(k)] + [k for k in a if k != "source" and a[k] != b.get(k)]
    return list(dict.fromkeys(t(f"apps.change.f.{k}") for k in keys if k != "kind"))


@router.post("/apps/{app_id}/webhook/rotate")
def rotate_webhook(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                   user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    panel.db.update("apps", app["id"], webhook_secret=panel.sealer.seal(secrets.token_urlsafe(24)))
    return redirect(request, f"/apps/{app['id']}", t("msg.webhook_rotated"))


@router.post("/apps/{app_id}/action/{action}")
async def app_container_action(request: Request, action: str, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                               user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):
    if action not in ("start", "stop", "restart", "kill", "pause", "unpause"):
        return toast(t("msg.bad_data"), "err", status=400)
    node = panel.node(app["node_id"])
    if not node or node["status"] != "paired":
        return toast(t("msg.node_not_responding"), "err", status=400)
    spec, _ = deploy.load(panel, app)
    ep = spec.endpoint or "local"
    client = panel.node_client(node)

    from .docker import _p
    from ..services.uptime import app_containers

    try:
        items = await client.get(_p("/api/containers", ep))
        mine = app_containers(items, app)
        target_ids = [c["id"] for c in mine] if mine else [f"app-{app['slug']}"]
        for cid in target_ids:
            await client.post(_p(f"/api/containers/{cid}/action/{action}", ep))
    except Exception as exc:
        return toast(getattr(exc, "detail", None) or str(exc), "err", status=502)

    known = ("start", "stop", "restart", "kill", "pause", "unpause")
    from ..services import alerts, uptime

    monitor = uptime.monitor_for(panel, "app", app["id"])
    if action == "start":
        panel.db.update("apps", app["id"], status="running")
        if monitor and monitor.get("status") == "paused":
            panel.db.update("monitors", monitor["id"], status="unknown")
    elif action in ("stop", "kill"):
        panel.db.update("apps", app["id"], status="stopped")
        if monitor:
            panel.db.update("monitors", monitor["id"], status="paused", last_message=t("uptime.msg.stopped"))
            active = alerts._active(panel).get(("monitor_down", "monitor", monitor["id"]))
            if active:
                await alerts.resolve(panel, active, t("uptime.msg.stopped"))
    elif action == "pause":
        panel.db.update("apps", app["id"], status="paused")
        if monitor:
            panel.db.update("monitors", monitor["id"], status="paused", last_message=t("uptime.msg.stopped"))
            active = alerts._active(panel).get(("monitor_down", "monitor", monitor["id"]))
            if active:
                await alerts.resolve(panel, active, t("uptime.msg.stopped"))
    elif action == "unpause":
        panel.db.update("apps", app["id"], status="running")
        if monitor and monitor.get("status") == "paused":
            panel.db.update("monitors", monitor["id"], status="unknown")

    return toast(t("msg.container_" + action) if action in known else action, extra={"app-changed": True, "containers-changed": True})
