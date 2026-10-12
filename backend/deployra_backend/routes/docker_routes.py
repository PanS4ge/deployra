"""Kontenery, obrazy, wolumeny i sieci (zastępuje Portainera)."""

from __future__ import annotations

import asyncio
import json
import socket

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from deployra_common.i18n import tr
from deployra_common.schemas import ContainerSpec, NetworkSpec, VolumeSpec

from ..context import Context, get_ctx
from ..services import docker_svc
from . import job_response, signed

router = APIRouter(prefix="/api", dependencies=[Depends(signed)])


class LogQuery(BaseModel):
    must: list[str] = []
    exclude: list[str] = []
    since: float
    until: float
    containers: list[str] = []
    apps: list[str] = []
    limit: int = 200


@router.post("/logs/search")
def logs_search(body: LogQuery, ep: str = "local", ctx: Context = Depends(get_ctx)) -> dict:
    """Mini-ELK: przeszukanie logów kontenerów (wzorce, wykluczenia, okres) — na żądanie, bez indeksu."""
    from ..services import logsearch

    must, exclude = logsearch.compile_all(body.must), logsearch.compile_all(body.exclude)
    if body.until <= body.since:
        raise HTTPException(400, tr("node.logs.bad_range"))
    return logsearch.search(ctx.docker_for(ep), must, exclude, body.since, body.until, set(body.containers) or None,
                            set(body.apps) or None, limit=max(1, min(body.limit, 1000)))  # fmt: skip
ws_router = APIRouter(prefix="/api")

SHELLS = {
    "auto": ["/bin/sh", "-c", "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"],
    "sh": ["/bin/sh"],
    "bash": ["/bin/bash"],
    "ash": ["/bin/ash"],
}


def dk(request: Request, ep: str = "local") -> Any:
    """Klient Dockera wybranego endpointu (host albo CT na Proxmoxie)."""
    return request.app.state.ctx.docker_for(ep)


def _container(cid: str, client: Any = Depends(dk)) -> dict:
    """Sprawdza istnienie kontenera przed otwarciem strumienia."""
    return client.api.inspect_container(cid)


# --- kontenery -----------------------------------------------------------------


@router.get("/containers")
def containers(client: Any = Depends(dk)) -> list[dict]:
    return docker_svc.list_containers(client)


@router.post("/containers")
async def create_container(spec: ContainerSpec, client: Any = Depends(dk), ctx: Context = Depends(get_ctx)) -> dict:
    def work(job):
        if spec.pull:
            docker_svc.pull_image(client, job, spec.image)
        cid = docker_svc.run_container(client, spec)
        job.log(tr("node.container_started", name=spec.name, id=cid[:12]))
        return {"id": cid}

    return job_response(ctx.jobs.start("container", f"Kontener {spec.name}", work))


@router.get("/containers/{cid}")
def container(info: dict = Depends(_container)) -> dict:
    return info


@router.post("/containers/{cid}/action/{action}")
def container_action(cid: str, action: str, client: Any = Depends(dk)) -> dict:
    docker_svc.container_action(client, cid, action)
    return {"ok": True}


@router.delete("/containers/{cid}")
def remove_container(cid: str, force: bool = False, volumes: bool = False, client: Any = Depends(dk)) -> dict:
    docker_svc.remove_container(client, cid, force=force, volumes=volumes)
    return {"ok": True}


@router.get("/containers/{cid}/top")
def container_top(cid: str, client: Any = Depends(dk)) -> dict:
    return docker_svc.container_top(client, cid)


@router.get("/containers/{cid}/stats")
def container_stats(cid: str, client: Any = Depends(dk)) -> dict:
    return docker_svc.stats_once(client, cid)


@router.get("/containers/{cid}/stats/stream", response_class=EventSourceResponse)
async def container_stats_stream(cid: str, info: dict = Depends(_container), client: Any = Depends(dk)):
    async for sample in docker_svc.stats_stream(client, cid):
        yield ServerSentEvent(data=sample, event="stats")


@router.get("/containers/{cid}/logs")
def container_logs(cid: str, tail: int = 300, timestamps: bool = False, client: Any = Depends(dk)) -> dict:
    raw = client.api.logs(cid, tail=max(1, min(tail, 5000)), timestamps=timestamps)
    return {"lines": raw.decode(errors="replace").splitlines()}


@router.get("/containers/{cid}/logs/stream", response_class=EventSourceResponse)
async def container_logs_stream(
    cid: str, tail: int = 200, timestamps: bool = False, info: dict = Depends(_container), client: Any = Depends(dk)
):
    async for line in docker_svc.follow_logs(client, cid, tail=max(0, min(tail, 5000)), timestamps=timestamps):
        yield ServerSentEvent(raw_data=line, event="line")


@ws_router.websocket("/containers/{cid}/exec")
async def container_exec(
    websocket: WebSocket, cid: str, shell: str = "auto", cols: int = 120, rows: int = 32, ep: str = "local"
) -> None:
    """Terminal: bajty binarne = wejście/wyjście PTY, tekst JSON = sterowanie (resize)."""
    ctx: Context = websocket.app.state.ctx
    try:
        ctx.guard.check_websocket(websocket)
        client = ctx.docker_for(ep)
    except (PermissionError, HTTPException, LookupError, ValueError):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    api = client.api
    try:
        exec_id = (
            await asyncio.to_thread(
                api.exec_create, cid, SHELLS.get(shell, SHELLS["auto"]), stdin=True, tty=True,
                environment={"TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)},
            )
        )["Id"]  # fmt: skip
        sock = await asyncio.to_thread(api.exec_start, exec_id, tty=True, socket=True)
    except Exception as exc:  # noqa: BLE001 — błąd trafia do terminala
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close()
        return
    raw: socket.socket = getattr(sock, "_sock", sock)
    loop = asyncio.get_running_loop()

    async def resize(c: int, r: int) -> None:
        try:
            await asyncio.to_thread(api.exec_resize, exec_id, height=max(r, 2), width=max(c, 10))
        except Exception:  # noqa: BLE001
            pass

    async def pump_out() -> None:
        while True:
            data = await loop.run_in_executor(None, raw.recv, 65536)
            if not data:
                return
            await websocket.send_bytes(data)

    async def pump_in() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes"):
                await loop.run_in_executor(None, raw.sendall, message["bytes"])
            elif message.get("text"):
                try:
                    control = json.loads(message["text"])
                except ValueError:
                    continue
                if control.get("type") == "resize":
                    await resize(int(control.get("cols", 80)), int(control.get("rows", 24)))
                elif control.get("type") == "input":
                    await loop.run_in_executor(None, raw.sendall, str(control.get("data", "")).encode())

    await resize(cols, rows)
    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        try:
            raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        raw.close()
        try:
            await websocket.send_text(json.dumps({"type": "exit"}))
            await websocket.close()
        except Exception:  # noqa: BLE001 — klient już rozłączony
            pass


# --- obrazy --------------------------------------------------------------------


class PullRequest(BaseModel):
    image: str


class PruneRequest(BaseModel):
    all: bool = False


@router.get("/images")
def images(client: Any = Depends(dk)) -> list[dict]:
    return docker_svc.list_images(client)


@router.post("/images/pull")
async def pull(body: PullRequest, client: Any = Depends(dk), ctx: Context = Depends(get_ctx)) -> dict:
    from deployra_common.schemas import check_image

    image = check_image(body.image)
    return job_response(ctx.jobs.start("pull", f"Pobieranie {image}", lambda job: docker_svc.pull_image(client, job, image)))


@router.delete("/images/{image_id:path}")
def remove_image(image_id: str, force: bool = False, client: Any = Depends(dk)) -> dict:
    docker_svc.remove_image(client, image_id, force)
    return {"ok": True}


@router.post("/images/prune")
def prune_images(body: PruneRequest, client: Any = Depends(dk)) -> dict:
    return docker_svc.prune_images(client, body.all)


# --- wolumeny ------------------------------------------------------------------


@router.get("/volumes")
def volumes(client: Any = Depends(dk)) -> list[dict]:
    return docker_svc.list_volumes(client)


@router.post("/volumes")
def create_volume(spec: VolumeSpec, client: Any = Depends(dk)) -> dict:
    return docker_svc.create_volume(client, spec)


@router.get("/volumes/{name}")
def volume(name: str, client: Any = Depends(dk)) -> dict:
    return client.api.inspect_volume(name)


@router.delete("/volumes/{name}")
def remove_volume(name: str, force: bool = False, client: Any = Depends(dk)) -> dict:
    client.api.remove_volume(name, force=force)
    return {"ok": True}


@router.post("/volumes/prune")
def prune_volumes(client: Any = Depends(dk)) -> dict:
    return client.api.prune_volumes()


# --- sieci ---------------------------------------------------------------------


@router.get("/networks")
def networks(client: Any = Depends(dk)) -> list[dict]:
    return docker_svc.list_networks(client)


@router.post("/networks")
def create_network(spec: NetworkSpec, client: Any = Depends(dk)) -> dict:
    return docker_svc.create_network(client, spec)


@router.get("/networks/{network_id}")
def network(network_id: str, client: Any = Depends(dk)) -> dict:
    return client.api.inspect_network(network_id)


@router.delete("/networks/{network_id}")
def remove_network(network_id: str, client: Any = Depends(dk)) -> dict:
    docker_svc.remove_network(client, network_id)
    return {"ok": True}


@router.post("/networks/prune")
def prune_networks(client: Any = Depends(dk)) -> dict:
    return client.api.prune_networks()
