"""Aplikacje: kreator (obraz / compose / statyczna / Git / z plików), szczegóły, redeploy, rollback, upload, webhooki."""

from __future__ import annotations

import os
import re
import secrets
import shlex
import tempfile
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import ValidationError

from deployra_common import envfile
from deployra_common.jobs import Job
from deployra_common.runtimes import RUNTIMES
from deployra_common.schemas import AppSpec, VolumeMount

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission, has
from ..services import charts, deploy, env_groups, migrate, relay, resources, scheduled_tasks
from ..web import redirect, render, templates, toast

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


ENV_EDITOR_ID = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


def _form_rows(form: Any) -> list[dict[str, str]]:
    """Wiersze edytora KLUCZ / wartość / zakres z formularza (w kolejności wyświetlania)."""
    keys, values, scopes = form.getlist("env_key"), form.getlist("env_value"), form.getlist("env_scope")
    return [{
        "key": str(key), "value": str(values[i]) if i < len(values) else "",
        "scope": str(scopes[i]) if i < len(scopes) and scopes[i] in envfile.SCOPES else "runtime",
    } for i, key in enumerate(keys)]  # fmt: skip


def _env_from_rows(rows: list[dict[str, str]]) -> tuple[dict[str, str], dict[str, str]]:
    env: dict[str, str] = {}
    scopes: dict[str, str] = {}
    for row in rows:
        key = row["key"].strip()
        if not key:  # pusty wiersz (np. dopisany i niewypełniony) — pomijamy
            continue
        if not envfile.KEY.match(key):
            raise ValueError(t("msg.bad_env", line=key))
        env[key] = row["value"]
        if row["scope"] in ("build", "both"):
            scopes[key] = row["scope"]
        else:
            scopes.pop(key, None)
    return env, scopes


def env_from_form(form: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Zmienne aplikacji z edytora: w trybie „.env” z tekstu, w trybie pól z wierszy.
    Bez `env_mode` przyjmujemy stare pole `env` (tekst .env), żeby stare formularze i skrypty dalej działały."""
    if "env_mode" not in form and "env" in form:
        return envfile.parse(str(form.get("env") or ""))
    if str(form.get("env_mode") or "form") == "raw":
        return envfile.parse(str(form.get("env_raw") or ""))
    return _env_from_rows(_form_rows(form))


def _rows(env: dict[str, str], scopes: dict[str, str]) -> list[dict[str, str]]:
    return [{"key": k, "value": v, "scope": scopes.get(k, "runtime")} for k, v in env.items()]


def editor_state(eid: str, env: dict[str, str], scopes: dict[str, str], mode: str = "form", text: str | None = None) -> dict[str, Any]:
    """Stan edytora zmiennych dla szablonu `apps/_env_editor.html`."""
    return {"id": eid, "mode": mode, "rows": _rows(env, scopes), "text": envfile.dump(env, scopes) if text is None else text}


def group_picker(panel: Panel, attached: list[int]) -> list[dict[str, Any]]:
    """Zestawy do wyboru: najpierw dołączone (w kolejności), potem reszta po nazwie."""
    groups = {g["id"]: g for g in env_groups.list_groups(panel)}
    order = [groups[i] for i in attached if i in groups]
    return order + [g for g in groups.values() if g["id"] not in attached]


def _group_ids(form: Any, user: dict, current: list[int]) -> list[int]:
    """Zestaw zmiennych może zawierać sekrety — dołączać go może tylko ktoś, kto widzi go w ustawieniach."""
    if not has(user, Permission.ALLOW_SETTINGS):
        return current
    return [int(x) for x in form.getlist("env_group") if str(x).isdigit()]


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
    with_container = kind in ("image", "git", "runtime")  # bez kontenera (compose, strona) nie ma wdrożenia bez przestojów
    zero_downtime = bool(form.get("zero_downtime")) and with_container
    env, env_scopes = env_from_form(form)
    spec = AppSpec(
        slug=slug,
        source=source,
        endpoint=get("endpoint", "local"),
        container_port=int(port) if port else None,
        env=env,
        env_scopes=env_scopes,
        volumes=_volumes(str(form.get("volumes") or "")),
        memory_mb=int(get("memory_mb")) if get("memory_mb") else None,
        cpus=float(get("cpus")) if get("cpus") else None,
        domains=domains,
        host_port=existing.host_port if existing else None,  # stały port hosta (np. przejęty kontener) — nie ma go w formularzu
        zero_downtime=zero_downtime,
        health_path=get("health_path") if with_container else None,
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
    from ..services import app_icons, uptime

    rows = panel.db.fetchall("SELECT a.*, n.name AS node_name FROM apps a LEFT JOIN nodes n ON n.id = a.node_id ORDER BY a.name")
    stats = uptime.stats_for(panel, [("app", a["id"]) for a in rows])
    items = []
    for a in rows:
        a["usage"] = resources.app_usage(a)
        s = stats.get(("app", a["id"]))
        items.append({
            "id": a["id"], "name": a["name"].lower(), "node": (a["node_name"] or "").lower(), "status": a["status"],
            "cpu": (a["usage"] or {}).get("cpu", -1), "mem": (a["usage"] or {}).get("mem", -1),
            "uptime": s["uptime_24h"] if s and s.get("uptime_24h") is not None else -1,
            "text": " ".join([a["name"], a["slug"], a["node_name"] or "", *(a.get("domains") or []), app_icons.app_image(panel, a) or ""]).lower(),
        })  # fmt: skip
    return render(request, "apps/list.html", title=t("nav.apps"), apps=rows, stats=stats, items=items)


COMMON_PORTS = {
    "nginx": 80, "httpd": 80, "apache2": 80, "caddy": 80, "redis": 6379, "postgres": 5432, "mysql": 3306,
    "mariadb": 3306, "mongo": 27017, "mongodb": 27017, "node": 3000, "python": 8000, "traefik": 80,
    "vaultwarden/server": 80, "uptime-kuma": 3001, "louislam/uptime-kuma": 3001, "gitea/gitea": 3000,
    "gitlab/gitlab-ce": 80, "grafana/grafana": 3000, "prom/prometheus": 9090, "portainer/portainer-ce": 9000,
    "it-tools": 80, "adguard/adguardhome": 3000, "pihole/pihole": 80, "nextcloud": 80, "wordpress": 80,
    "ghost": 2368, "rabbitmq": 5672, "memcached": 11211,
}


@router.get("/apps/docker-search")
async def docker_hub_search(q: str = "") -> list[dict]:
    query = q.strip().lower()
    if not query:
        return []
    results = []
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                f"https://hub.docker.com/v2/search/repositories/?query={query}&page_size=8",
                headers={"Accept": "application/json", "User-Agent": "Deployra/1.2"},
            )
            if resp.status_code == 200:
                data = resp.json().get("results", [])
                for item in data:
                    repo = item.get("repo_name", "")
                    short_name = repo.split("/")[-1] if "/" in repo else repo
                    port = COMMON_PORTS.get(repo) or COMMON_PORTS.get(short_name) or 80
                    results.append({
                        "name": repo,
                        "description": item.get("short_description", "")[:120],
                        "stars": item.get("star_count", 0),
                        "is_official": item.get("is_official", False),
                        "port": port,
                    })
    except Exception:
        pass

    if not results:
        for name, port in COMMON_PORTS.items():
            if query in name:
                results.append({
                    "name": name,
                    "description": f"Standardowy obraz {name}",
                    "stars": 1000,
                    "is_official": "/" not in name,
                    "port": port,
                })
    return results[:8]


@router.get("/apps/new")
def new_app(request: Request, template: str = "", panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):
    settings = panel.db.settings()
    prefill = panel.plugins.app_template(template) if template else None
    nodes = _nodes(panel)
    free = {n["id"]: resources.node_free(n["id"]) for n in nodes}
    try:  # zmienne z szablonu pokazujemy w edytorze jak zwykłe pola
        env, scopes = envfile.parse((prefill or {}).get("env") or "")
    except ValueError:
        env, scopes = {}, {}
    return render(
        request, "apps/new.html", title=t("apps.new"), nodes=nodes, free=free, base_domain=settings.get("base_domain", ""),
        has_cf=bool(settings.get("cloudflare_token")), has_gateway=bool(panel.gateway()), prefill=prefill,
        app_templates=panel.plugins.app_templates(), template=template,
        enved=editor_state("env", env, scopes), groups=group_picker(panel, []), attached=[],
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
        group_ids = env_groups.resolve_ids(panel, _group_ids(form, user, []))
        upload = await _save_upload(panel, form.get("file")) if spec.source.kind in ("static", "runtime") else None
        if upload:  # pliki trafiają na węzeł przed pierwszym wdrożeniem — zamiast plików startowych
            await _publish(panel, node, spec.slug, upload)
        app_id = deploy.create_app_row(panel, name or slug, node["id"], spec, options)
        env_groups.set_attached(panel, app_id, group_ids)
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc), "err", status=400)
    if spec.env.get("DEPLOYRA_PAIR_CODE") == "auto":  # szablon Minecrafta: plugin sparuje się sam przy pierwszym starcie
        from ..services import minecraft
        from .minecraft import panel_url

        _, code = minecraft.create(panel, name or slug, app_id)
        spec.env.update(DEPLOYRA_PAIR_CODE=code, DEPLOYRA_PANEL_URL=panel_url(request, panel))
        panel.db.update("apps", app_id, spec=deploy.store(panel, spec, options))
    app = panel.db.get("apps", app_id)
    job = start_deploy(panel, app)
    panel.notify(t("msg.notify_deploying", user=user["email"], slug=slug, node=node["name"]))
    return redirect(request, f"/apps/{app_id}?job={job.id}")


# --- szczegóły ---------------------------------------------------------------------------


def move_targets(panel: Panel, app: dict, spec: AppSpec) -> list[dict]:
    """Kafelki „Przenieś”: bieżący węzeł + pozostałe z RAM-em, dyskiem, CPU i oceną, czy aplikacja się zmieści."""
    import time

    MB = 1024 * 1024
    usage = resources.app_usage(app) or {}
    need = round(usage["mem"] / MB) if usage.get("mem") else spec.memory_mb
    out = []
    for n in panel.db.fetchall("SELECT * FROM nodes ORDER BY name"):
        latest = resources.LATEST.get(n["id"]) or {}
        host = latest.get("host") or {}
        online = n["status"] == "paired" and time.time() - latest.get("ts", 0) < 180
        free = resources.free_mb(host) if online else None
        disk_free = round((host.get("disk_total") - host.get("disk_used")) / MB) if online and host.get("disk_total") else None
        if n["id"] == app["node_id"]:
            fit = "here"
        elif n["status"] != "paired":
            fit = "offline"
        elif free is None or not need:
            fit = "unknown"
        else:
            fit = "ok" if free >= need * 1.2 + 64 else ("tight" if free >= need else "no")
        out.append({"node": n, "online": online, "free": free, "total": round(host["ram_total"] / MB) if host.get("ram_total") else None,
                    "ram": host.get("ram"), "cpu": host.get("cpu"), "disk_free": disk_free, "fit": fit})  # fmt: skip
    out.sort(key=lambda x: (x["fit"] != "here", x["fit"] in ("no", "offline"), x["node"]["name"]))
    return [{**x, "need": need} for x in out]


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
    from deployra_common.container_profiles import detect_profile

    monitor = uptime.monitor_for(panel, "app", app["id"])
    others = move_targets(panel, app, spec)
    img = spec.source.image if spec.source.kind == "image" else ""
    app_profile = detect_profile(img, spec.env, [{"container": spec.container_port}] if spec.container_port else [])
    return render(
        request, "apps/detail.html", title=app["name"], app=app, spec=spec, options=options, node=node, site=site, cert=cert,
        job=live_job, hook_url=hook_url, hook_secret=panel.sealer.unseal(app.get("webhook_secret")),
        monitor=monitor, monitor_stats=uptime.stats_for(panel, [("app", app["id"])]).get(("app", app["id"])),
        settings=panel.db.settings(), other_nodes=others, auto_capable=migrate.auto_capable(spec),
        acls=panel.db.fetchall("SELECT id, name FROM access_lists ORDER BY name"),
        app_profile=app_profile, mc_server=panel.db.fetchone("SELECT id FROM minecraft_servers WHERE app_id = ?", [app["id"]]),
    )  # fmt: skip


@router.get("/apps/{app_id}/icon")
async def app_icon(app: dict = Depends(_app), panel: Panel = Depends(get_panel)):
    from fastapi import Response

    from ..services import app_icons

    found = await app_icons.get(panel, app)
    if not found:
        return Response(status_code=404, headers={"Cache-Control": "private, max-age=3600"})
    data, kind = found
    # SVG z cudzej strony: bez skryptów nawet po otwarciu adresu wprost
    headers = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"}  # fmt: skip
    return Response(content=data, media_type=kind, headers=headers)


@router.post("/apps/{app_id}/icon")
async def set_app_icon(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    from ..services import app_icons

    form = await request.form()
    mode = str(form.get("mode") or "auto")
    value = app.get("icon")
    if mode == "auto":
        value = None
    elif mode == "url":
        url = str(form.get("url") or "").strip()
        if not re.match(r"^https?://\S{4,1000}$", url):
            return toast(t("apps.icon_bad_url"), "err", status=400)
        value = f"url:{url}"
    elif mode == "upload":
        upload = form.get("file")
        if not hasattr(upload, "read"):
            return toast(t("apps.icon_bad_file"), "err", status=400)
        data = await upload.read(app_icons.MAX_BYTES + 1)
        try:
            value = app_icons.save_upload(panel, app["id"], data, getattr(upload, "content_type", "") or "")
        except ValueError:
            return toast(t("apps.icon_bad_file"), "err", status=400)
    if mode != "upload":  # wgrany plik nie jest już potrzebny (nowy wgrany zastępuje stary sam)
        for old in app_icons.icon_dir(panel).glob(f"custom-{app['id']}-*"):
            old.unlink(missing_ok=True)
    panel.db.update("apps", app["id"], icon=value)
    app_icons.forget(panel, app["id"])
    return redirect(request, f"/apps/{app['id']}/edit", t("apps.icon_saved"))


@router.get("/apps/{app_id}/badge")
def app_badge(request: Request, app: dict = Depends(_app)):
    return templates.TemplateResponse(request, "partials/app_badge.html", {"app": app})


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


async def app_container(panel: Panel, app: dict) -> tuple[dict | None, str, str | None]:
    """(węzeł, endpoint, ID kontenera aplikacji) — ten sam dobór co przy akcjach zasilania (etykieta, app-<slug>, port, nazwa)."""
    from ..services.uptime import app_containers
    from .docker import _p

    node = panel.node(app["node_id"])
    if not node:
        return None, "local", None
    try:
        spec, _ = deploy.load(panel, app)
        ep = spec.endpoint or "local"
    except Exception:  # noqa: BLE001 — uszkodzony spec nie blokuje podglądu
        ep = "local"
    try:
        mine = app_containers(await panel.node_client(node).get(_p("/api/containers", ep)) or [], app)
    except Exception:  # noqa: BLE001 — węzeł offline
        return node, ep, None
    mine.sort(key=lambda c: c.get("state") != "running")  # najpierw działający
    return node, ep, (mine[0]["id"] if mine else None)


async def _to_container(request: Request, panel: Panel, app: dict, suffix: str = "", anchor: str = ""):
    from .docker import _q

    node, ep, cid = await app_container(panel, app)
    if not node:
        return redirect(request, f"/apps/{app['id']}", t("apps.node_missing"), "err")
    if not cid:
        return redirect(request, f"/apps/{app['id']}", t("apps.no_container", name=app["name"]), "err")
    return redirect(request, f"/nodes/{node['id']}/containers/{cid}{suffix}{_q(ep)}{anchor}")


@router.get("/apps/{app_id}/logs")
async def app_logs(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                   user: dict = Depends(require(Permission.ALLOW_SEEKING_CONTAINER))):  # fmt: skip
    return await _to_container(request, panel, app, anchor="#logs")


@router.get("/apps/{app_id}/files")
async def app_files(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                    user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES))):  # fmt: skip
    return await _to_container(request, panel, app, suffix="/files")


async def _app_target_or_toast(panel: Panel, app: dict):
    node, ep, cid = await app_container(panel, app)
    if not node or not cid:
        return None, toast(t("apps.no_container", name=app["name"]), "err", status=404)
    return (node, ep, cid), None


@router.post("/apps/{app_id}/quick/{action_id}")
async def app_quick_action(action_id: str, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                           user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):  # fmt: skip
    from .docker import quick_action

    target, error = await _app_target_or_toast(panel, app)
    return error or await quick_action(panel, *target, action_id)


@router.get("/apps/{app_id}/dump")
async def app_dump(app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                   user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):  # fmt: skip
    from .docker import dump_download

    target, error = await _app_target_or_toast(panel, app)
    if error:
        raise HTTPException(404, t("apps.no_container", name=app["name"]))
    return await dump_download(panel, *target)


@router.post("/apps/{app_id}/backup-dump")
async def app_backup_dump(app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                          user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):  # fmt: skip
    from .docker import dump_to_storage

    target, error = await _app_target_or_toast(panel, app)
    return error or await dump_to_storage(panel, *target)


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
    spec = env_groups.effective(panel, app["id"], spec)
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
    scheduled_tasks.remove_for_app(panel, app["id"])
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


@router.post("/apps/env-editor")
async def env_editor(request: Request):
    """Edytor zmiennych bez zapisu: dodanie / usunięcie wiersza albo przejście między polami a plikiem .env."""
    query = request.query_params
    eid = query.get("id") or "env"
    if not ENV_EDITOR_ID.match(eid):
        return toast(t("msg.bad_data"), "err", status=400)
    form = await request.form()
    mode = str(form.get("env_mode") or "form")
    text = str(form.get("env_raw") or "")
    target = query.get("to") if query.get("to") in ("form", "raw") else mode
    try:
        if target == "raw":
            if mode == "form":  # pola → tekst; w trybie pliku tekst zostaje dokładnie taki, jaki był
                env, scopes = _env_from_rows(_form_rows(form))
                text = envfile.dump(env, scopes)
            state = editor_state(eid, {}, {}, "raw", text)
        elif mode == "raw":  # tekst → pola
            env, scopes = envfile.parse(text)
            state = editor_state(eid, env, scopes, "form")
        else:
            rows = _form_rows(form)
            if query.get("drop", "").isdigit() and int(query["drop"]) < len(rows):
                del rows[int(query["drop"])]
            if query.get("add"):
                rows.append({"key": "", "value": "", "scope": "runtime"})
            state = {"id": eid, "mode": "form", "rows": rows, "text": ""}
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    return templates.TemplateResponse(request, "apps/_env_editor.html", {"enved": state})


@router.get("/apps/{app_id}/edit")
def edit_page(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
              user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    spec, options = deploy.load(panel, app)
    settings = panel.db.settings()
    attached = env_groups.attached_ids(panel, app["id"])
    return render(request, "apps/edit.html", title=t("msg.title_app_settings", name=app["name"]), app=app, spec=spec, options=options,
                  node=panel.node(app["node_id"]), has_cf=bool(settings.get("cloudflare_token")),
                  enved=editor_state("env", spec.env, spec.env_scopes), groups=group_picker(panel, attached), attached=attached)  # fmt: skip


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
        group_ids = env_groups.resolve_ids(panel, _group_ids(form, user, env_groups.attached_ids(panel, app["id"])))
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
                    domains=spec.domains, **deploy.strategy_columns(spec))  # fmt: skip
    env_groups.set_attached(panel, app["id"], group_ids)  # zestawy wchodzą przy następnym wdrożeniu, bez automatycznego
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
    """Wszystko, co trafia do kontenera (bez domen); puste polecenie [] i brak polecenia to to samo.

    Sposób wdrażania (bez przestojów, test zdrowia) działa dopiero przy następnym wdrożeniu — nie pyta „postawić od nowa?”.
    """
    data = spec.model_dump(exclude={"domains", "zero_downtime", "health_path"})
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


# --- zadania cykliczne (cron w kontenerze aplikacji) ----------------------------------------------

TASKS_CHANGED = {"tasks-changed": True}


def _task_presets() -> list[tuple[str, str, str]]:
    return [
        ("*/5 * * * *", t("tasks.every.5m"), "*/5 * * * *"),
        ("0 * * * *", t("tasks.every.hourly"), "@hourly"),
        ("0 0 * * *", t("tasks.every.daily"), "@daily"),
        ("0 0 * * 0", t("tasks.every.weekly"), "@weekly"),
        ("0 0 1 * *", t("tasks.every.monthly"), "@monthly"),
    ]


def _task(panel: Panel, app: dict, task_id: int) -> dict:
    task = scheduled_tasks.get_task(panel, app["id"], task_id)
    if not task:
        raise HTTPException(404, t("tasks.not_found"))
    return task


def _task_list(panel: Panel, app: dict) -> dict:
    tasks = [{**task, "running": task["id"] in panel.task_runs} for task in scheduled_tasks.list_tasks(panel, app["id"])]
    return {"tasks": tasks, "running": any(task["running"] for task in tasks)}


async def _task_form(request: Request) -> dict:
    form = await request.form()
    return scheduled_tasks.clean(str(form.get("name", "")), str(form.get("command", "")), str(form.get("schedule", "")),
                                 str(form.get("timeout", "")), bool(form.get("enabled")))  # fmt: skip


@router.get("/apps/{app_id}/tasks")
def app_tasks(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
              user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):  # fmt: skip
    return render(request, "apps/_tasks.html", app=app, presets=_task_presets(), default_timeout=scheduled_tasks.TIMEOUT_DEFAULT,
                  **_task_list(panel, app))  # fmt: skip


@router.get("/apps/{app_id}/tasks/list")
def app_tasks_list(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                   user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):  # fmt: skip
    return render(request, "apps/_task_list.html", app=app, presets=_task_presets(), **_task_list(panel, app))


# zadanie to dowolne polecenie w kontenerze — te same uprawnienia co terminal
@router.post("/apps/{app_id}/tasks")
async def create_task(request: Request, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    data = await _task_form(request)
    scheduled_tasks.save(panel, app["id"], data)
    return toast(t("tasks.created", name=data["name"]), extra=TASKS_CHANGED)


@router.post("/apps/{app_id}/tasks/{task_id}/update")
async def update_task(request: Request, task_id: int, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    _task(panel, app, task_id)
    data = await _task_form(request)
    scheduled_tasks.save(panel, app["id"], data, task_id)
    return toast(t("tasks.updated", name=data["name"]), extra=TASKS_CHANGED)


@router.post("/apps/{app_id}/tasks/{task_id}/toggle")
async def toggle_task(task_id: int, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    task = _task(panel, app, task_id)
    enabled = 0 if task["enabled"] else 1
    panel.db.update("scheduled_tasks", task_id, enabled=enabled)
    return toast(t("tasks.turned_on" if enabled else "tasks.turned_off", name=task["name"]), extra=TASKS_CHANGED)


@router.post("/apps/{app_id}/tasks/{task_id}/run")
async def run_task(task_id: int, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                   user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    task = _task(panel, app, task_id)
    if not scheduled_tasks.start(panel, task_id):
        return toast(t("tasks.already_running"), "warn", extra=TASKS_CHANGED)
    return toast(t("tasks.run_started", name=task["name"]), extra=TASKS_CHANGED)


@router.post("/apps/{app_id}/tasks/{task_id}/delete")
async def delete_task(task_id: int, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_CONTAINER_TERMINAL))):  # fmt: skip
    task = _task(panel, app, task_id)
    scheduled_tasks.remove(panel, task_id)
    return toast(t("tasks.deleted", name=task["name"]), "warn", extra=TASKS_CHANGED)


@router.get("/apps/{app_id}/tasks/{task_id}/runs")
def task_runs(request: Request, task_id: int, app: dict = Depends(_app), panel: Panel = Depends(get_panel),
              user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):  # fmt: skip
    _task(panel, app, task_id)
    return render(request, "apps/_task_runs.html", runs=scheduled_tasks.runs(panel, task_id))
