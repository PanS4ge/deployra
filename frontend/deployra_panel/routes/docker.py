"""Docker na węźle (zamiennik Portainera): kontenery, logi, statystyki, obrazy, wolumeny, sieci, stacki, SFTP."""

from __future__ import annotations

import json
from typing import Any

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import ValidationError

from deployra_common import container_profiles
from deployra_common.client import SignedClient
from deployra_common.jobs import Job
from deployra_common.schemas import ContainerSpec, NetworkSpec, PortMap, StackSpec, VolumeMount, VolumeSpec

from ..context import Panel, get_panel
from ..deps import current_user, node_or_404, require
from ..i18n import t
from ..permissions import Permission
from ..services import relay
from ..web import redirect, render, templates, toast
from .jobs import line_html

router = APIRouter(dependencies=[Depends(current_user)])


def _client(panel: Panel, node: dict) -> SignedClient:
    return panel.node_client(node)


def relay_job(panel: Panel, node: dict, title: str, method: str, path: str, body: Any = None) -> Job:
    """Uruchamia zadanie na węźle i przepisuje jego log do zadania panelu."""
    client = _client(panel, node)

    async def work(job: Job) -> Any:
        if method == "DELETE":
            started = await client.delete(path)
        elif method == "PUT":
            started = await client.put(path, json=body)
        else:
            started = await client.post(path, json=body)
        return await relay.follow_remote_job(client, started["job"]["id"], job)

    return panel.jobs.start("node", title, work)


def ep_param(ep: str = "local") -> str:
    """Endpoint Dockera: `local` (host) albo `ct-<vmid>` (kontener LXC na Proxmoxie)."""
    if not re.match(r"^(local|ct-\d{1,9})$", ep):
        raise HTTPException(400, t("msg.bad_endpoint"))
    return ep


def _p(path: str, ep: str) -> str:
    return f"{path}{'&' if '?' in path else '?'}ep={ep}"


def _q(ep: str) -> str:
    return "" if ep == "local" else f"?ep={ep}"


def _errors(exc: ValidationError) -> str:
    return "\n".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())


# --- kontenery -------------------------------------------------------------------------


@router.get("/nodes/{node_id}/containers")
async def containers(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel)):
    items = await _client(panel, node).get(_p("/api/containers", ep))
    for c in items or []:
        c["profile"] = container_profiles.detect_profile(c.get("image", ""), ports=c.get("ports"))
    from ..services import uptime

    monitors = uptime.container_stats(panel, node["id"], ep)
    template = "nodes/_containers_table.html" if request.headers.get("HX-Target") == "containers" else "nodes/containers.html"
    return render(request, template, title=f"{node['name']} · {t('containers.crumb')}", node=node, tab="containers", items=items, monitors=monitors)


@router.post("/nodes/{node_id}/containers/{cid}/action/{action}")
async def container_action(request: Request, cid: str, action: str, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                           user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):  # fmt: skip
    await _client(panel, node).post(_p(f"/api/containers/{cid}/action/{action}", ep))
    known = ("start", "stop", "restart", "kill", "pause", "unpause")
    if action in ("stop", "kill", "pause"):
        from ..services import alerts, uptime

        for row in panel.db.fetchall("SELECT * FROM monitors WHERE kind = 'container' AND node_id = ?", [node["id"]]):
            target = row.get("target") or {}
            if target.get("name") == cid and target.get("ep", "local") == ep:
                panel.db.update("monitors", row["id"], status="paused", last_message=t("uptime.msg.stopped"))
                active = alerts._active(panel).get(("monitor_down", "monitor", row["id"]))
                if active:
                    await alerts.resolve(panel, active, t("uptime.msg.stopped"))

        for app in panel.db.fetchall("SELECT * FROM apps WHERE node_id = ?", [node["id"]]):
            if cid in (app["slug"], f"app-{app['slug']}") or cid == app.get("name"):
                panel.db.update("apps", app["id"], status="stopped" if action in ("stop", "kill") else "paused")
                monitor = uptime.monitor_for(panel, "app", app["id"])
                if monitor:
                    panel.db.update("monitors", monitor["id"], status="paused", last_message=t("uptime.msg.stopped"))
                    active = alerts._active(panel).get(("monitor_down", "monitor", monitor["id"]))
                    if active:
                        await alerts.resolve(panel, active, t("uptime.msg.stopped"))
    elif action in ("start", "unpause"):
        from ..services import uptime

        for row in panel.db.fetchall("SELECT * FROM monitors WHERE kind = 'container' AND node_id = ?", [node["id"]]):
            target = row.get("target") or {}
            if target.get("name") == cid and target.get("ep", "local") == ep and row.get("status") == "paused":
                panel.db.update("monitors", row["id"], status="unknown")
        for app in panel.db.fetchall("SELECT * FROM apps WHERE node_id = ?", [node["id"]]):
            if cid in (app["slug"], f"app-{app['slug']}") or cid == app.get("name"):
                panel.db.update("apps", app["id"], status="running")
                monitor = uptime.monitor_for(panel, "app", app["id"])
                if monitor and monitor.get("status") == "paused":
                    panel.db.update("monitors", monitor["id"], status="unknown")

    return toast(t("msg.container_" + action) if action in known else action, extra={"containers-changed": True})


@router.post("/nodes/{node_id}/containers/{cid}/remove")
async def container_remove(request: Request, cid: str, volumes: str = Form(""), node: dict = Depends(node_or_404), ep: str = Depends(ep_param),
                           panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DELETE_DOCKERS))):  # fmt: skip
    await _client(panel, node).delete(_p(f"/api/containers/{cid}", ep), params={"force": "true", "volumes": "true" if volumes else "false"})
    return redirect(request, f"/nodes/{node['id']}/containers{_q(ep)}", t("msg.container_removed"))


@router.get("/nodes/{node_id}/containers/new")
def container_new(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):
    return render(request, "nodes/container_new.html", title=t("containers.new"), node=node, tab="containers")


@router.post("/nodes/{node_id}/containers/new")
async def container_create(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                           user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):  # fmt: skip
    form = await request.form()
    try:
        spec = ContainerSpec(
            name=str(form.get("name", "")).strip(),
            image=str(form.get("image", "")).strip(),
            command=str(form.get("command", "")).split() or None,
            env=_kv_lines(str(form.get("env", ""))),
            ports=[PortMap(host=int(h) if h else None, container=int(c)) for h, c in _pairs(str(form.get("ports", "")))],
            volumes=[VolumeMount(source=s, target=t) for s, t in _pairs(str(form.get("volumes", "")))],
            restart=str(form.get("restart", "unless-stopped")),
            memory_mb=int(form["memory_mb"]) if form.get("memory_mb") else None,
            network=str(form.get("network") or "deployra"),
        )
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc) if isinstance(exc, ValidationError) else str(exc), "err", status=400)

    # Sprawdzenie kolizji portów hosta
    host_ports = [p.host for p in spec.ports if p.host is not None]
    if host_ports:
        try:
            ports_resp = await _client(panel, node).get("/api/ports")
            occupied = ports_resp.get("ports", [])
            for hp in host_ports:
                conflict = next((u for u in occupied if u.get("port") == hp), None)
                if conflict:
                    owner = conflict.get("owner", "system")
                    return toast(t("msg.port_collision", port=hp, owner=owner), "err", status=409)
        except Exception:
            pass

    job = relay_job(panel, node, t("jobs.title_container", name=spec.name, node=node["name"]), "POST", _p("/api/containers", ep), spec.model_dump(mode="json"))
    return redirect(request, f"/jobs/{job.id}")


@router.get("/nodes/{node_id}/containers/{cid}")
async def container(request: Request, cid: str, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                    user: dict = Depends(require(Permission.ALLOW_SEEKING_CONTAINER))):  # fmt: skip
    info = await _client(panel, node).get(_p(f"/api/containers/{cid}", ep))
    name = info.get("Name", "").lstrip("/")
    cfg = info.get("Config") or {}
    env = cfg.get("Env") or []
    ports = []
    for p in (cfg.get("ExposedPorts") or {}).keys():
        try:
            ports.append({"container": int(p.split("/")[0])})
        except (ValueError, IndexError):
            pass
    for p in ((info.get("NetworkSettings") or {}).get("Ports") or {}).keys():
        try:
            ports.append({"container": int(p.split("/")[0])})
        except (ValueError, IndexError):
            pass
    profile = container_profiles.detect_profile(cfg.get("Image", ""), env, ports)
    initial_logs = ""
    try:
        logs_res = await _client(panel, node).get(_p(f"/api/containers/{cid}/logs", ep), params={"tail": 100})
        initial_logs = "\n".join(logs_res.get("lines", []))
    except Exception:
        pass
    return render(request, "nodes/container.html", title=name, node=node, tab="containers", c=info, name=name,
                  profile=profile, initial_logs=initial_logs, raw=json.dumps(info, indent=2, ensure_ascii=False))  # fmt: skip


@router.post("/nodes/{node_id}/containers/{cid}/action/quick/{action_id}")
async def container_quick_action(request: Request, node_id: int, cid: str, action_id: str,
                                 node: dict = Depends(node_or_404), ep: str = Depends(ep_param),
                                 panel: Panel = Depends(get_panel),
                                 user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):
    res = await _client(panel, node).post(_p(f"/api/containers/{cid}/action/quick/{action_id}", ep))
    output = (res or {}).get("output", "")
    code = (res or {}).get("exit_code", 0)
    if code != 0:
        return toast(f"Błąd ({code}): {output[:120]}", kind="err")
    summary = output.strip().splitlines()[0] if output.strip() else "Wykonano pomyślnie"
    return toast(f"Sukces: {summary[:120]}")


@router.get("/nodes/{node_id}/containers/{cid}/dump")
async def container_dump_download(request: Request, node_id: int, cid: str,
                                  node: dict = Depends(node_or_404), ep: str = Depends(ep_param),
                                  panel: Panel = Depends(get_panel),
                                  user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):
    client = _client(panel, node)
    res = await client.client.get(
        client._url(_p(f"/api/containers/{cid}/action/quick/dump_sql", ep)),
        headers=client._headers("GET", f"/api/containers/{cid}/action/quick/dump_sql"),
    )
    if res.status_code != 200:
        raise HTTPException(res.status_code, "Nie udało się wykonać zrzutu bazy")
    fn = res.headers.get("content-disposition", "").split("filename=")[-1].strip('"') or f"dump_{cid[:12]}.sql.gz"
    return Response(
        content=res.content,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


@router.post("/nodes/{node_id}/containers/{cid}/backup-dump")
async def container_backup_to_storage(request: Request, node_id: int, cid: str,
                                      node: dict = Depends(node_or_404), ep: str = Depends(ep_param),
                                      panel: Panel = Depends(get_panel),
                                      user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):
    from ..services import backup_storage

    driver = backup_storage.get_driver(panel)
    if not driver:
        return toast("Brak skonfigurowanego magazynu kopii w Ustawienia -> Kopie zapasowe", kind="err")
    client = _client(panel, node)
    res = await client.client.get(
        client._url(_p(f"/api/containers/{cid}/action/quick/dump_sql", ep)),
        headers=client._headers("GET", f"/api/containers/{cid}/action/quick/dump_sql"),
    )
    if res.status_code != 200:
        return toast(f"Błąd zrzutu bazy: {res.text[:100]}", kind="err")
    fn = res.headers.get("content-disposition", "").split("filename=")[-1].strip('"') or f"db_dump_{cid[:12]}.sql.gz"
    target_name = f"database/{node['name']}/{fn}"
    driver.upload(target_name, res.content)
    return toast(f"Zapisano zrzut bazy w magazynie kopii: {fn}")


@router.get("/nodes/{node_id}/containers/{cid}/logs/stream", response_class=EventSourceResponse)
async def logs_stream(cid: str, tail: int = 300, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_SEEKING_CONTAINER))):  # fmt: skip
    client = _client(panel, node)
    async with client.stream("GET", _p(f"/api/containers/{cid}/logs/stream", ep), params={"tail": tail}) as response:
        async for event, data in relay.iter_sse(response):
            if event == "line":
                yield ServerSentEvent(raw_data=line_html(data), event="line")


@router.get("/nodes/{node_id}/containers/{cid}/stats/stream", response_class=EventSourceResponse)
async def stats_stream(cid: str, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_SEEKING_CONTAINER))):  # fmt: skip
    client = _client(panel, node)
    template = templates.get_template("partials/container_stats.html")
    async with client.stream("GET", _p(f"/api/containers/{cid}/stats/stream", ep)) as response:
        async for event, data in relay.iter_sse(response):
            if event == "stats":
                yield ServerSentEvent(raw_data=" ".join(template.render(s=json.loads(data)).split()), event="stats")


# --- obrazy -------------------------------------------------------------------------------


@router.get("/nodes/{node_id}/images")
async def images(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel)):
    items = await _client(panel, node).get(_p("/api/images", ep))
    return render(request, "nodes/images.html", title=f"{node['name']} · {t('nodes.tab_images').lower()}", node=node, tab="images", items=items)


@router.post("/nodes/{node_id}/images/pull")
async def image_pull(request: Request, image: str = Form(...), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                     user: dict = Depends(require(Permission.ALLOW_PULL_IMAGES))):  # fmt: skip
    from ..services import registries

    image_name = image.strip()
    auth = registries.auth_for_image(panel, image_name)
    body: dict[str, Any] = {"image": image_name}
    if auth:
        body["auth"] = auth
    job = relay_job(panel, node, t("jobs.title_pull", image=image_name, node=node["name"]), "POST", _p("/api/images/pull", ep), body)
    return redirect(request, f"/jobs/{job.id}")


@router.post("/nodes/{node_id}/images/remove")
async def image_remove(request: Request, image_id: str = Form(...), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_DELETE_DOCKERS))):  # fmt: skip
    await _client(panel, node).delete(_p(f"/api/images/{image_id}", ep), params={"force": "false"})
    return redirect(request, f"/nodes/{node['id']}/images{_q(ep)}", t("msg.image_removed"))


@router.post("/nodes/{node_id}/images/prune")
async def image_prune(request: Request, all: str = Form(""), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_DELETE_DOCKERS))):  # fmt: skip
    result = await _client(panel, node).post(_p("/api/images/prune", ep), json={"all": bool(all)}, timeout=300.0)
    from ..web import human_bytes

    return redirect(request, f"/nodes/{node['id']}/images{_q(ep)}", f"Odzyskano {human_bytes(result.get('SpaceReclaimed', 0))}")


# --- wolumeny i sieci ---------------------------------------------------------------------


@router.get("/nodes/{node_id}/volumes")
async def volumes(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel)):
    items = await _client(panel, node).get(_p("/api/volumes", ep))
    return render(request, "nodes/volumes.html", title=f"{node['name']} · {t('nodes.tab_volumes').lower()}", node=node, tab="volumes", items=items)


@router.post("/nodes/{node_id}/volumes")
async def volume_create(request: Request, name: str = Form(...), driver: str = Form("local"), node: dict = Depends(node_or_404), ep: str = Depends(ep_param),
                        panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_CREATING_VOLUMES))):  # fmt: skip
    spec = VolumeSpec(name=name.strip(), driver=driver.strip() or "local")
    await _client(panel, node).post(_p("/api/volumes", ep), json=spec.model_dump())
    return redirect(request, f"/nodes/{node['id']}/volumes{_q(ep)}", t("msg.volume_created", name=spec.name))


@router.post("/nodes/{node_id}/volumes/remove")
async def volume_remove(request: Request, name: str = Form(...), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                        user: dict = Depends(require(Permission.ALLOW_DELETING_VOLUMES))):  # fmt: skip
    await _client(panel, node).delete(_p(f"/api/volumes/{name}", ep))
    return redirect(request, f"/nodes/{node['id']}/volumes{_q(ep)}", t("msg.volume_removed", name=name))


@router.get("/nodes/{node_id}/networks")
async def networks(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel)):
    items = await _client(panel, node).get(_p("/api/networks", ep))
    return render(request, "nodes/networks.html", title=f"{node['name']} · {t('nodes.tab_networks').lower()}", node=node, tab="networks", items=items)


@router.post("/nodes/{node_id}/networks")
async def network_create(request: Request, name: str = Form(...), driver: str = Form("bridge"), subnet: str = Form(""),
                         gateway: str = Form(""), internal: str = Form(""), node: dict = Depends(node_or_404), ep: str = Depends(ep_param),
                         panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_CREATING_NETWORKS))):  # fmt: skip
    try:
        spec = NetworkSpec(name=name.strip(), driver=driver.strip() or "bridge", subnet=subnet.strip() or None,
                           gateway=gateway.strip() or None, internal=bool(internal))  # fmt: skip
    except ValidationError as exc:
        return toast(_errors(exc), "err", status=400)
    await _client(panel, node).post(_p("/api/networks", ep), json=spec.model_dump())
    return redirect(request, f"/nodes/{node['id']}/networks{_q(ep)}", t("msg.network_created", name=spec.name))


@router.post("/nodes/{node_id}/networks/remove")
async def network_remove(request: Request, network_id: str = Form(...), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                         user: dict = Depends(require(Permission.ALLOW_DELETING_NETWORKS))):  # fmt: skip
    await _client(panel, node).delete(_p(f"/api/networks/{network_id}", ep))
    return redirect(request, f"/nodes/{node['id']}/networks{_q(ep)}", t("msg.network_removed"))


# --- stacki --------------------------------------------------------------------------------


@router.get("/nodes/{node_id}/stacks")
async def stacks(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel)):
    items = await _client(panel, node).get(_p("/api/stacks", ep))
    apps = {a["slug"] for a in panel.db.fetchall("SELECT slug FROM apps WHERE node_id = ?", [node["id"]])}
    return render(request, "nodes/stacks.html", title=f"{node['name']} · {t('stacks.crumb')}", node=node, tab="stacks", items=items, apps=apps)


@router.get("/nodes/{node_id}/stacks/new")
def stack_new(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param)):
    sample = "services:\n  web:\n    image: nginx:alpine\n    restart: unless-stopped\n    ports:\n      - \"20080:80\"\n"
    return render(request, "nodes/stack_edit.html", title=t("stacks.new"), node=node, tab="stacks", stack={"name": "", "compose": sample, "env": {}}, new=True)


@router.get("/nodes/{node_id}/stacks/{name}")
async def stack_edit(request: Request, name: str, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel)):
    stack = await _client(panel, node).get(_p(f"/api/stacks/{name}", ep))
    return render(request, "nodes/stack_edit.html", title=f"Stack {name}", node=node, tab="stacks", stack=stack, new=False)


@router.post("/nodes/{node_id}/stacks/save")
async def stack_save(request: Request, name: str = Form(...), compose: str = Form(...), env: str = Form(""), pull: str = Form(""),
                     node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                     user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):  # fmt: skip
    name = name.strip().lower()
    try:
        spec = StackSpec(compose=compose.replace("\r\n", "\n"), env=_kv_lines(env))
    except (ValidationError, ValueError) as exc:
        return toast(_errors(exc) if isinstance(exc, ValidationError) else str(exc), "err", status=400)

    # Sprawdzenie kolizji portów hosta w compose
    compose_ports = extract_compose_host_ports(compose)
    if compose_ports:
        try:
            ports_resp = await _client(panel, node).get("/api/ports")
            occupied = ports_resp.get("ports", [])
            for hp in compose_ports:
                conflict = next(
                    (u for u in occupied if u.get("port") == hp and not (
                        u.get("owner", "").startswith(f"{name}_") or
                        u.get("owner", "").startswith(f"{name}-") or
                        u.get("owner") == name
                    )),
                    None,
                )
                if conflict:
                    owner = conflict.get("owner", "system")
                    return toast(t("msg.port_collision_compose", port=hp, owner=owner), "err", status=409)
        except Exception:
            pass

    job = relay_job(panel, node, t("jobs.title_stack", name=name, node=node["name"]), "PUT", _p(f"/api/stacks/{name}?pull={'true' if pull else 'false'}", ep),
                    spec.model_dump())  # fmt: skip
    return redirect(request, f"/jobs/{job.id}")


@router.post("/nodes/{node_id}/stacks/{name}/{verb}")
async def stack_action(request: Request, name: str, verb: str, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):  # fmt: skip
    if verb == "remove":
        if not has_perm(user, Permission.ALLOW_DEPLOYMENT_DELETE):
            return toast(t("msg.no_perm_delete"), "err", status=403)
        job = relay_job(panel, node, t("jobs.title_stack_remove", name=name), "DELETE", _p(f"/api/stacks/{name}", ep))
    else:
        job = relay_job(panel, node, f"Stack {name}: {verb}", "POST", _p(f"/api/stacks/{name}/{verb}", ep))
    return redirect(request, f"/jobs/{job.id}")


# --- SFTP ---------------------------------------------------------------------------------


@router.get("/nodes/{node_id}/sftp")
async def sftp(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel)):
    data = await _client(panel, node).get(_p("/api/sftp", ep))
    apps = panel.db.fetchall("SELECT slug, name FROM apps WHERE node_id = ? AND kind IN ('static', 'git') ORDER BY slug", [node["id"]])
    return render(request, "nodes/sftp.html", title=f"{node['name']} · SFTP", node=node, tab="sftp", data=data, apps=apps)


@router.post("/nodes/{node_id}/sftp")
async def sftp_save(request: Request, username: str = Form(...), slug: str = Form(...), password: str = Form(""), keys: str = Form(""),
                    node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                    user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    body = {"slug": slug, "password": password or None, "keys": [k.strip() for k in keys.splitlines() if k.strip()] or None}
    await _client(panel, node).put(_p(f"/api/sftp/{username.strip().lower()}", ep), json=body)
    return redirect(request, f"/nodes/{node['id']}/sftp{_q(ep)}", t("msg.sftp_saved", name=username))


@router.post("/nodes/{node_id}/sftp/{username}/delete")
async def sftp_delete(request: Request, username: str, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    await _client(panel, node).delete(_p(f"/api/sftp/{username}", ep))
    return redirect(request, f"/nodes/{node['id']}/sftp{_q(ep)}", t("msg.sftp_deleted", name=username))


@router.post("/nodes/{node_id}/sftp/port")
async def sftp_port_save(request: Request, port: int = Form(...), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                         user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    if not (1 <= port <= 65535):
        raise HTTPException(400, "Port musi być z zakresu 1-65535")
    try:
        ports_resp = await _client(panel, node).get("/api/ports")
        occupied = ports_resp.get("ports", [])
        conflict = next((u for u in occupied if u.get("port") == port and u.get("owner") != "SFTP"), None)
        if conflict:
            owner = conflict.get("owner", "system")
            return toast(t("msg.port_collision", port=port, owner=owner), "err", status=409)
    except Exception:
        pass
    await _client(panel, node).post(_p("/api/sftp/config", ep), json={"port": port})
    return redirect(request, f"/nodes/{node['id']}/sftp{_q(ep)}", t("msg.sftp_port_saved", port=port))


# --- ustawienia węzła ------------------------------------------------------------------------


@router.get("/nodes/{node_id}/settings")
def node_settings(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param)):
    return render(request, "nodes/settings.html", title=f"{node['name']} · {t('nav.settings').lower()}", node=node, tab="settings")


# --- pomocnicze -------------------------------------------------------------------------------


def has_perm(user: dict, perm: Permission) -> bool:
    from ..permissions import has

    return has(user, perm)


def _kv_lines(text: str) -> dict[str, str]:
    out = {}
    for line in text.replace("\r\n", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(t("msg.bad_env", line=line))
        out[key.strip()] = value.strip()
    return out


def _pairs(text: str) -> list[tuple[str, str]]:
    """Linie „a:b” (porty host:kontener, wolumeny źródło:cel)."""
    out = []
    for line in text.replace("\r\n", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        left, sep, right = line.rpartition(":")
        if not sep:
            left, right = "", line
        out.append((left.strip(), right.strip()))
    return out


def extract_compose_host_ports(compose_text: str) -> list[int]:
    """Wyciąga porty hosta zdefiniowane w docker-compose."""
    try:
        import yaml
        data = yaml.safe_load(compose_text)
        if not isinstance(data, dict):
            return []
        services = data.get("services")
        if not isinstance(services, dict):
            return []
        host_ports: list[int] = []
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            ports = svc.get("ports") or []
            for p in ports:
                if isinstance(p, dict):
                    pub = p.get("published")
                    if pub and str(pub).isdigit():
                        host_ports.append(int(pub))
                elif isinstance(p, str):
                    p = p.split("/")[0]
                    parts = p.split(":")
                    if len(parts) >= 2:
                        host_p = parts[-2]
                        if host_p.isdigit():
                            host_ports.append(int(host_p))
        return host_ports
    except Exception:
        return []


# --- Menedżer plików kontenera ------------------------------------------------


@router.get("/nodes/{node_id}/containers/{cid}/files")
async def container_files(
    request: Request,
    node_id: int,
    cid: str,
    path: str = "/",
    node: dict = Depends(node_or_404),
    ep: str = Depends(ep_param),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES)),
):
    client = _client(panel, node)
    info = await client.get(_p(f"/api/containers/{cid}", ep))
    c_name = info.get("Name", "").lstrip("/") or cid[:12]

    try:
        data = await client.get(_p(f"/api/containers/{cid}/files", ep), params={"path": path})
    except Exception as exc:
        err = getattr(exc, "detail", str(exc))
        data = {"path": path, "items": [], "error": err}

    cur_path = data.get("path") or path
    parts = [p for p in cur_path.strip("/").split("/") if p]
    crumbs = []
    acc = ""
    for p in parts:
        acc += "/" + p
        crumbs.append({"name": p, "path": acc})

    ctx = {
        "node": node,
        "cid": cid,
        "c_name": c_name,
        "ep": ep,
        "epa": "" if ep == "local" else f"?ep={ep}",
        "cur_path": cur_path,
        "crumbs": crumbs,
        "items": data.get("items", []),
        "error": data.get("error"),
        "P": Permission,
    }

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return render(request, "nodes/_container_files_table.html", **ctx)

    return render(
        request,
        "nodes/container_files.html",
        title=f"{t('containers.files')}: {c_name}",
        tab="containers",
        **ctx,
    )


@router.get("/nodes/{node_id}/containers/{cid}/files/read")
async def container_file_read(
    request: Request,
    node_id: int,
    cid: str,
    path: str,
    node: dict = Depends(node_or_404),
    ep: str = Depends(ep_param),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES)),
):
    client = _client(panel, node)
    try:
        data = await client.get(_p(f"/api/containers/{cid}/files/read", ep), params={"path": path})
        return Response(content=json.dumps(data, ensure_ascii=False), media_type="application/json")
    except Exception as exc:
        raise HTTPException(400, getattr(exc, "detail", str(exc)))


@router.get("/nodes/{node_id}/containers/{cid}/files/download")
async def container_file_download(
    request: Request,
    node_id: int,
    cid: str,
    path: str,
    node: dict = Depends(node_or_404),
    ep: str = Depends(ep_param),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES)),
):
    import os

    client = _client(panel, node)
    res = await client.request("GET", _p(f"/api/containers/{cid}/files/download", ep), params={"path": path})
    if res.status_code != 200:
        raise HTTPException(res.status_code, "Nie udało się pobrać pliku")
    fn = res.headers.get("content-disposition", "").split("filename=")[-1].strip('"') or os.path.basename(path)
    return Response(
        content=res.content,
        media_type=res.headers.get("content-type", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


@router.post("/nodes/{node_id}/containers/{cid}/files/write")
async def container_file_write(
    request: Request,
    node_id: int,
    cid: str,
    node: dict = Depends(node_or_404),
    ep: str = Depends(ep_param),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES_WRITE)),
):
    form = await request.form()
    path = str(form.get("path") or "").strip()
    content = str(form.get("content") or "")
    if not path:
        return toast(t("common.required_field"), "err", status=400)

    client = _client(panel, node)
    try:
        await client.post(_p(f"/api/containers/{cid}/files/write", ep), json={"path": path, "content": content})
        return toast(t("containers.file_saved"))
    except Exception as exc:
        return toast(getattr(exc, "detail", str(exc)), "err", status=400)


@router.post("/nodes/{node_id}/containers/{cid}/files/upload")
async def container_file_upload(
    request: Request,
    node_id: int,
    cid: str,
    node: dict = Depends(node_or_404),
    ep: str = Depends(ep_param),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES_WRITE)),
):
    form = await request.form()
    dir_path = str(form.get("dir_path") or "/").strip()
    files = form.getlist("files")
    client = _client(panel, node)

    file_payload = []
    for f in files:
        if not hasattr(f, "filename") or not f.filename:
            continue
        raw = await f.read()
        file_payload.append(("files", (f.filename, raw, getattr(f, "content_type", "application/octet-stream"))))

    if not file_payload:
        return toast(t("common.required_field"), "err", status=400)

    try:
        await client.request(
            "POST",
            _p(f"/api/containers/{cid}/files/upload", ep),
            data={"dir_path": dir_path},
            files=file_payload,
        )
        epa = "" if ep == "local" else f"&ep={ep}"
        return redirect(
            request,
            f"/nodes/{node['id']}/containers/{cid}/files?path={dir_path}{epa}",
            t("containers.files_uploaded", count=len(file_payload)),
        )
    except Exception as exc:
        return toast(getattr(exc, "detail", str(exc)), "err", status=400)


@router.post("/nodes/{node_id}/containers/{cid}/files/delete")
async def container_file_delete(
    request: Request,
    node_id: int,
    cid: str,
    node: dict = Depends(node_or_404),
    ep: str = Depends(ep_param),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES_WRITE)),
):
    form = await request.form()
    path = str(form.get("path") or "").strip()
    dir_path = str(form.get("dir_path") or "/").strip()
    if not path:
        return toast(t("common.required_field"), "err", status=400)

    client = _client(panel, node)
    try:
        await client.delete(_p(f"/api/containers/{cid}/files", ep), params={"path": path})
        epa = "" if ep == "local" else f"&ep={ep}"
        return redirect(
            request,
            f"/nodes/{node['id']}/containers/{cid}/files?path={dir_path}{epa}",
            t("containers.file_deleted", path=path),
        )
    except Exception as exc:
        return toast(getattr(exc, "detail", str(exc)), "err", status=400)


@router.post("/nodes/{node_id}/containers/{cid}/files/mkdir")
async def container_file_mkdir(
    request: Request,
    node_id: int,
    cid: str,
    node: dict = Depends(node_or_404),
    ep: str = Depends(ep_param),
    panel: Panel = Depends(get_panel),
    user: dict = Depends(require(Permission.ALLOW_CONTAINER_FILES_WRITE)),
):
    form = await request.form()
    name = str(form.get("name") or "").strip()
    dir_path = str(form.get("dir_path") or "/").strip()
    if not name:
        return toast(t("common.required_field"), "err", status=400)

    target_path = f"{dir_path.rstrip('/')}/{name.strip('/')}"
    client = _client(panel, node)
    try:
        await client.post(_p(f"/api/containers/{cid}/files/mkdir", ep), json={"path": target_path})
        epa = "" if ep == "local" else f"&ep={ep}"
        return redirect(
            request,
            f"/nodes/{node['id']}/containers/{cid}/files?path={dir_path}{epa}",
            t("containers.folder_created"),
        )
    except Exception as exc:
        return toast(getattr(exc, "detail", str(exc)), "err", status=400)

