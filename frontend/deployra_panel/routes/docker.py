"""Docker na węźle (zamiennik Portainera): kontenery, logi, statystyki, obrazy, wolumeny, sieci, stacki, SFTP."""

from __future__ import annotations

import json
from typing import Any

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import ValidationError

from deployra_common.client import SignedClient
from deployra_common.jobs import Job
from deployra_common.schemas import ContainerSpec, NetworkSpec, PortMap, StackSpec, VolumeMount, VolumeSpec

from ..context import Panel, get_panel
from ..deps import current_user, node_or_404, require
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
        raise HTTPException(400, "nieprawidłowy endpoint")
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
    from ..services import uptime

    monitors = uptime.container_stats(panel, node["id"], ep)
    template = "nodes/_containers_table.html" if request.headers.get("HX-Target") == "containers" else "nodes/containers.html"
    return render(request, template, title=f"{node['name']} · kontenery", node=node, tab="containers", items=items, monitors=monitors)


@router.post("/nodes/{node_id}/containers/{cid}/action/{action}")
async def container_action(request: Request, cid: str, action: str, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                           user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):  # fmt: skip
    await _client(panel, node).post(_p(f"/api/containers/{cid}/action/{action}", ep))
    names = {"start": "Uruchomiono", "stop": "Zatrzymano", "restart": "Zrestartowano", "kill": "Zabito", "pause": "Wstrzymano", "unpause": "Wznowiono"}
    return toast(f"{names.get(action, action)} kontener", extra={"containers-changed": True})


@router.post("/nodes/{node_id}/containers/{cid}/remove")
async def container_remove(request: Request, cid: str, volumes: str = Form(""), node: dict = Depends(node_or_404), ep: str = Depends(ep_param),
                           panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_DELETE_DOCKERS))):  # fmt: skip
    await _client(panel, node).delete(_p(f"/api/containers/{cid}", ep), params={"force": "true", "volumes": "true" if volumes else "false"})
    return redirect(request, f"/nodes/{node['id']}/containers{_q(ep)}", "Usunięto kontener")


@router.get("/nodes/{node_id}/containers/new")
def container_new(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), user: dict = Depends(require(Permission.ALLOW_MODIFY_DOCKERS))):
    return render(request, "nodes/container_new.html", title="Nowy kontener", node=node, tab="containers")


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
    job = relay_job(panel, node, f"Kontener {spec.name} na {node['name']}", "POST", _p("/api/containers", ep), spec.model_dump(mode="json"))
    return redirect(request, f"/jobs/{job.id}")


@router.get("/nodes/{node_id}/containers/{cid}")
async def container(request: Request, cid: str, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                    user: dict = Depends(require(Permission.ALLOW_SEEKING_CONTAINER))):  # fmt: skip
    info = await _client(panel, node).get(_p(f"/api/containers/{cid}", ep))
    name = info.get("Name", "").lstrip("/")
    return render(request, "nodes/container.html", title=name, node=node, tab="containers", c=info, name=name,
                  raw=json.dumps(info, indent=2, ensure_ascii=False))  # fmt: skip


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
    return render(request, "nodes/images.html", title=f"{node['name']} · obrazy", node=node, tab="images", items=items)


@router.post("/nodes/{node_id}/images/pull")
async def image_pull(request: Request, image: str = Form(...), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                     user: dict = Depends(require(Permission.ALLOW_PULL_IMAGES))):  # fmt: skip
    job = relay_job(panel, node, f"Pobieranie {image} na {node['name']}", "POST", _p("/api/images/pull", ep), {"image": image.strip()})
    return redirect(request, f"/jobs/{job.id}")


@router.post("/nodes/{node_id}/images/remove")
async def image_remove(request: Request, image_id: str = Form(...), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_DELETE_DOCKERS))):  # fmt: skip
    await _client(panel, node).delete(_p(f"/api/images/{image_id}", ep), params={"force": "false"})
    return redirect(request, f"/nodes/{node['id']}/images{_q(ep)}", "Usunięto obraz")


@router.post("/nodes/{node_id}/images/prune")
async def image_prune(request: Request, all: str = Form(""), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_DELETE_DOCKERS))):  # fmt: skip
    result = await _client(panel, node).post(_p("/api/images/prune", ep), json={"all": bool(all)})
    from ..web import human_bytes

    return redirect(request, f"/nodes/{node['id']}/images{_q(ep)}", f"Odzyskano {human_bytes(result.get('SpaceReclaimed', 0))}")


# --- wolumeny i sieci ---------------------------------------------------------------------


@router.get("/nodes/{node_id}/volumes")
async def volumes(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel)):
    items = await _client(panel, node).get(_p("/api/volumes", ep))
    return render(request, "nodes/volumes.html", title=f"{node['name']} · wolumeny", node=node, tab="volumes", items=items)


@router.post("/nodes/{node_id}/volumes")
async def volume_create(request: Request, name: str = Form(...), driver: str = Form("local"), node: dict = Depends(node_or_404), ep: str = Depends(ep_param),
                        panel: Panel = Depends(get_panel), user: dict = Depends(require(Permission.ALLOW_CREATING_VOLUMES))):  # fmt: skip
    spec = VolumeSpec(name=name.strip(), driver=driver.strip() or "local")
    await _client(panel, node).post(_p("/api/volumes", ep), json=spec.model_dump())
    return redirect(request, f"/nodes/{node['id']}/volumes{_q(ep)}", f"Utworzono wolumen {spec.name}")


@router.post("/nodes/{node_id}/volumes/remove")
async def volume_remove(request: Request, name: str = Form(...), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                        user: dict = Depends(require(Permission.ALLOW_DELETING_VOLUMES))):  # fmt: skip
    await _client(panel, node).delete(_p(f"/api/volumes/{name}", ep))
    return redirect(request, f"/nodes/{node['id']}/volumes{_q(ep)}", f"Usunięto wolumen {name}")


@router.get("/nodes/{node_id}/networks")
async def networks(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel)):
    items = await _client(panel, node).get(_p("/api/networks", ep))
    return render(request, "nodes/networks.html", title=f"{node['name']} · sieci", node=node, tab="networks", items=items)


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
    return redirect(request, f"/nodes/{node['id']}/networks{_q(ep)}", f"Utworzono sieć {spec.name}")


@router.post("/nodes/{node_id}/networks/remove")
async def network_remove(request: Request, network_id: str = Form(...), node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                         user: dict = Depends(require(Permission.ALLOW_DELETING_NETWORKS))):  # fmt: skip
    await _client(panel, node).delete(_p(f"/api/networks/{network_id}", ep))
    return redirect(request, f"/nodes/{node['id']}/networks{_q(ep)}", "Usunięto sieć")


# --- stacki --------------------------------------------------------------------------------


@router.get("/nodes/{node_id}/stacks")
async def stacks(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel)):
    items = await _client(panel, node).get(_p("/api/stacks", ep))
    apps = {a["slug"] for a in panel.db.fetchall("SELECT slug FROM apps WHERE node_id = ?", [node["id"]])}
    return render(request, "nodes/stacks.html", title=f"{node['name']} · stacki", node=node, tab="stacks", items=items, apps=apps)


@router.get("/nodes/{node_id}/stacks/new")
def stack_new(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param)):
    sample = "services:\n  web:\n    image: nginx:alpine\n    restart: unless-stopped\n    ports:\n      - \"20080:80\"\n"
    return render(request, "nodes/stack_edit.html", title="Nowy stack", node=node, tab="stacks", stack={"name": "", "compose": sample, "env": {}}, new=True)


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
    job = relay_job(panel, node, f"Stack {name} na {node['name']}", "PUT", _p(f"/api/stacks/{name}?pull={'true' if pull else 'false'}", ep),
                    spec.model_dump())  # fmt: skip
    return redirect(request, f"/jobs/{job.id}")


@router.post("/nodes/{node_id}/stacks/{name}/{verb}")
async def stack_action(request: Request, name: str, verb: str, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                       user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT))):  # fmt: skip
    if verb == "remove":
        if not has_perm(user, Permission.ALLOW_DEPLOYMENT_DELETE):
            return toast("Brak uprawnień do usuwania", "err", status=403)
        job = relay_job(panel, node, f"Usuwanie stacka {name}", "DELETE", _p(f"/api/stacks/{name}", ep))
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
    return redirect(request, f"/nodes/{node['id']}/sftp{_q(ep)}", f"Zapisano konto SFTP {username}")


@router.post("/nodes/{node_id}/sftp/{username}/delete")
async def sftp_delete(request: Request, username: str, node: dict = Depends(node_or_404), ep: str = Depends(ep_param), panel: Panel = Depends(get_panel),
                      user: dict = Depends(require(Permission.ALLOW_DEPLOYMENT_MODIFICATIONS))):  # fmt: skip
    await _client(panel, node).delete(_p(f"/api/sftp/{username}", ep))
    return redirect(request, f"/nodes/{node['id']}/sftp{_q(ep)}", f"Usunięto konto {username}")


# --- ustawienia węzła ------------------------------------------------------------------------


@router.get("/nodes/{node_id}/settings")
def node_settings(request: Request, node: dict = Depends(node_or_404), ep: str = Depends(ep_param)):
    return render(request, "nodes/settings.html", title=f"{node['name']} · ustawienia", node=node, tab="settings")


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
            raise ValueError(f"linia „{line}” — oczekiwano KLUCZ=wartość")
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
