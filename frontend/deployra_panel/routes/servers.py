"""„Moje serwery”: panel dla kont z przypisanymi aplikacjami/kontenerami (w stylu Pterodactyla).

Każda akcja sprawdza przypisanie zalogowanego użytkownika i jego uprawnienie (zasilanie, logi, konsola,
pliki, wdrożenie, zmienne). Administrator (konto bez ograniczeń) widzi tu swoje przypisania tak samo.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.sse import EventSourceResponse, ServerSentEvent

from deployra_common import envfile
from deployra_common.client import RemoteError
from deployra_common.runtimes import RUNTIMES

from ..context import Panel, get_panel
from ..deps import current_user
from ..i18n import t
from ..security import SESSION_COOKIE
from ..services import access, deploy, relay, resources
from ..web import redirect, render, templates, toast
from .jobs import job_sse, line_html

router = APIRouter()
POWER_ACTIONS = ("start", "stop", "restart", "kill")


def _p(path: str, ep: str) -> str:
    return path if ep == "local" else f"{path}{'&' if '?' in path else '?'}ep={ep}"


def _grant(grant_id: int, request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(current_user)) -> dict[str, Any]:
    try:
        return access.grant(panel, user, grant_id)
    except LookupError:
        raise HTTPException(404, t("servers.not_found")) from None


def _need(row: dict[str, Any], perm: str) -> None:
    if not access.can(row, perm):
        raise HTTPException(403, t("servers.no_perm", what=t(f"servers.perm.{access.NAMES[perm]}")))


async def _state(panel: Panel, target: access.Target) -> dict[str, Any] | None:
    try:
        items = await panel.node_client(target.node).get(_p("/api/containers", target.ep))
    except (RemoteError, Exception):  # noqa: BLE001 — węzeł offline: pokażemy „nie odpowiada”
        return None
    if target.stack:
        mine = [c for c in items if c.get("stack") == target.stack]
        running = sum(1 for c in mine if c["state"] == "running")
        return {"state": "running" if mine and running == len(mine) else ("exited" if not running else "partial"), "containers": mine}
    return next((c for c in items if c["name"] == target.container), {"state": "missing"})


@router.get("/servers")
async def servers(request: Request, panel: Panel = Depends(get_panel), user: dict = Depends(current_user)):
    rows = access.grants_for(panel, user["id"])
    for row in rows:
        app = row.get("app")
        latest = resources.LATEST.get(row["node"]["id"]) if row.get("node") else None
        if app:
            row["usage"] = ((latest or {}).get("apps") or {}).get(app["slug"])
        elif latest:
            row["usage"] = next((c for c in latest.get("containers") or [] if c.get("name") == row["container"]), None)
    return render(request, "servers/list.html", title=t("servers.my_servers"), rows=rows)


@router.get("/servers/{grant_id}")
async def server(request: Request, row: dict = Depends(_grant), panel: Panel = Depends(get_panel)):
    target = access.target(panel, row)
    state = await _state(panel, target)
    spec = deploy.load(panel, row["app"])[0] if row["app"] else None
    job = panel.jobs.get(row["app"].get("last_job") or "") if row["app"] else None
    envtext = envfile.dump(spec.env, spec.env_scopes) if spec else ""
    return render(request, "servers/detail.html", title=row["label"], row=row, target=target, state=state, spec=spec, envtext=envtext,
                  job=job if job and not job.done else None, runtime=RUNTIMES.get((spec.source.runtime if spec else None) or ""))  # fmt: skip


@router.get("/servers/{grant_id}/resources")
def server_resources(request: Request, row: dict = Depends(_grant), panel: Panel = Depends(get_panel)):
    if row["app"]:
        from .apps import app_resources

        return app_resources(request, row["app"], panel)
    latest = resources.LATEST.get(row["node"]["id"]) or {}
    current = next((c for c in latest.get("containers") or [] if c.get("name") == row["container"]), None)
    return render(request, "partials/app_resources.html", app=None, current=current, mem_chart=None, cpu_chart=None)


@router.post("/servers/{grant_id}/power/{action}")
async def power(action: str, row: dict = Depends(_grant), panel: Panel = Depends(get_panel)):
    _need(row, access.POWER)
    if action not in POWER_ACTIONS:
        raise HTTPException(400, t("servers.bad_action"))
    target = access.target(panel, row)
    client = panel.node_client(target.node)
    if target.stack:  # aplikacja compose — cały stack (zadanie na węźle)
        await client.post(_p(f"/api/stacks/{target.stack}/{'stop' if action == 'kill' else action}", target.ep))
    else:
        await client.post(_p(f"/api/containers/{target.container}/action/{action}", target.ep))
    return toast(t("msg.container_" + action), extra={"server-changed": True})


@router.get("/servers/{grant_id}/logs/stream", response_class=EventSourceResponse)
async def logs(row: dict = Depends(_grant), panel: Panel = Depends(get_panel)):
    _need(row, access.LOGS)
    target = access.target(panel, row)
    client = panel.node_client(target.node)
    try:
        async with client.stream("GET", _p(f"/api/containers/{target.container}/logs/stream", target.ep), params={"tail": 300}) as response:
            async for event, data in relay.iter_sse(response):
                if event == "line":
                    yield ServerSentEvent(raw_data=line_html(data), event="line")
    except RemoteError as exc:
        err_msg = line_html(f"⚠️ Nie można pobrać strumienia logów: {exc.detail}")
        yield ServerSentEvent(raw_data=err_msg, event="line")
    except (httpx.HTTPError, asyncio.TimeoutError):
        err_msg = line_html(f"⚠️ Węzeł {target.node.get('name', 'zdalny')} nie odpowiada")
        yield ServerSentEvent(raw_data=err_msg, event="line")
    except Exception as exc:
        err_msg = line_html(f"⚠️ Błąd strumienia logów: {exc}")
        yield ServerSentEvent(raw_data=err_msg, event="line")


@router.get("/servers/{grant_id}/stats/stream", response_class=EventSourceResponse)
async def stats(row: dict = Depends(_grant), panel: Panel = Depends(get_panel)):
    target = access.target(panel, row)
    client = panel.node_client(target.node)
    template = templates.get_template("partials/container_stats.html")
    try:
        async with client.stream("GET", _p(f"/api/containers/{target.container}/stats/stream", target.ep)) as response:
            async for event, data in relay.iter_sse(response):
                if event == "stats":
                    yield ServerSentEvent(raw_data=" ".join(template.render(s=json.loads(data)).split()), event="stats")
    except (RemoteError, httpx.HTTPError, asyncio.TimeoutError, Exception):
        pass


@router.get("/servers/{grant_id}/console")
def console_page(request: Request, row: dict = Depends(_grant), panel: Panel = Depends(get_panel)):
    _need(row, access.CONSOLE)
    target = access.target(panel, row)
    return render(request, "nodes/terminal.html", title=f"{t('servers.perm.console')} · {row['label']}", node=target.node,
                  cid=target.container, ws_path=f"/servers/{row['id']}/console/ws", back=f"/servers/{row['id']}")  # fmt: skip


@router.websocket("/servers/{grant_id}/console/ws")
async def console_ws(websocket: WebSocket, grant_id: int, shell: str = "auto", cols: int = 120, rows: int = 32) -> None:
    from .terminal import relay_terminal

    panel: Panel = websocket.app.state.panel
    session = await run_in_threadpool(panel.sessions.load, websocket.cookies.get(SESSION_COOKIE))
    try:
        row = access.grant(panel, session, grant_id) if session else None
    except LookupError:
        row = None
    if not row or not access.can(row, access.CONSOLE):
        await websocket.close(code=4401)
        return
    target = access.target(panel, row)
    await relay_terminal(websocket, panel, session, target.node, target.container, target.ep, shell, cols, rows)


@router.post("/servers/{grant_id}/upload")
async def upload(request: Request, file: UploadFile, row: dict = Depends(_grant), panel: Panel = Depends(get_panel)):
    _need(row, access.FILES)
    app = row["app"]
    if not app or app["kind"] not in ("static", "runtime"):
        raise HTTPException(400, t("msg.upload_static_only"))
    from .apps import _publish, _save_upload, start_deploy

    try:
        tmp = await _save_upload(panel, file)
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    if not tmp:
        return toast(t("msg.upload_zip"), "err", status=400)
    count = await _publish(panel, panel.node(app["node_id"]), app["slug"], tmp)
    spec, _ = deploy.load(panel, app)
    if app["kind"] == "runtime" and not RUNTIMES[spec.source.runtime or ""].live and access.can(row, access.DEPLOY):
        job = start_deploy(panel, app)
        panel.db.update("apps", app["id"], last_job=job.id)
        return redirect(request, f"/servers/{row['id']}", t("msg.published_redeploy", count=count))
    return toast(t("msg.published_files", count=count))


@router.post("/servers/{grant_id}/deploy")
async def redeploy(request: Request, row: dict = Depends(_grant), panel: Panel = Depends(get_panel)):
    _need(row, access.DEPLOY)
    app = row["app"]
    if not app:
        raise HTTPException(400, t("servers.app_only"))
    running = panel.jobs.get(app.get("last_job") or "")
    if running and not running.done:
        return toast(t("msg.deploy_running"), "warn", status=409)
    from .apps import start_deploy

    job = start_deploy(panel, app)
    panel.db.update("apps", app["id"], last_job=job.id)
    return redirect(request, f"/servers/{row['id']}")


@router.get("/servers/{grant_id}/job/stream", response_class=EventSourceResponse)
async def job_stream(row: dict = Depends(_grant), panel: Panel = Depends(get_panel)):
    """Log wdrożenia — tylko ostatniego zadania tej aplikacji, nie dowolnego zadania panelu."""
    job = panel.jobs.get((row["app"] or {}).get("last_job") or "")
    if not job:
        raise HTTPException(404, t("msg.job_expired"))
    async for event in job_sse(job):
        yield event


@router.post("/servers/{grant_id}/env")
async def save_env(request: Request, row: dict = Depends(_grant), panel: Panel = Depends(get_panel)):
    _need(row, access.ENV)
    app = row["app"]
    if not app:
        raise HTTPException(400, t("servers.app_only"))
    form = await request.form()
    spec, options = deploy.load(panel, app)
    try:
        env, scopes = envfile.parse(str(form.get("env") or ""))
        spec = spec.model_copy(update={"env": env, "env_scopes": scopes})
        type(spec).model_validate(spec.model_dump())
    except ValueError as exc:
        return toast(str(exc), "err", status=400)
    panel.db.update("apps", app["id"], spec=deploy.store(panel, spec, options))
    if form.get("deploy") and access.can(row, access.DEPLOY):
        from .apps import start_deploy

        job = start_deploy(panel, panel.db.get("apps", app["id"]))
        panel.db.update("apps", app["id"], last_job=job.id)
    return redirect(request, f"/servers/{row['id']}", t("msg.saved_redeploy"))
