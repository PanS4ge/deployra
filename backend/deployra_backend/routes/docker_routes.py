"""Kontenery, obrazy, wolumeny i sieci (zastępuje Portainera)."""

from __future__ import annotations

import asyncio
import json
import socket

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

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


@router.get("/containers/{cid}/profile")
def container_profile(cid: str, client: Any = Depends(dk)) -> dict:
    return docker_svc.get_container_profile(client, cid)


@router.api_route("/containers/{cid}/action/quick/{action_id}", methods=["GET", "POST"])
def container_quick_action(cid: str, action_id: str, client: Any = Depends(dk)):
    code, output, is_dump, filename = docker_svc.run_quick_action(client, cid, action_id)
    if is_dump:
        return Response(
            content=output,
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return {"exit_code": code, "output": output}


class ExecRequest(BaseModel):
    cmd: list[str] = Field(min_length=1, max_length=64)
    timeout: int = Field(default=300, ge=1, le=36000)


@router.post("/containers/{cid}/exec/run")
def container_exec_run(cid: str, body: ExecRequest, client: Any = Depends(dk)) -> dict:
    """Jednorazowe polecenie bez TTY (zadania cykliczne): kod wyjścia, ogon wyniku, czy minął limit."""
    return docker_svc.exec_command(client, cid, body.cmd, body.timeout)


@router.post("/containers/{cid}/action/restore")
async def container_restore(cid: str, request: Request, client: Any = Depends(dk)) -> dict:
    """Przywrócenie zrzutu bazy (surowe body, gzip). Podpis żądania już obejmuje całe body."""
    payload = await request.body()
    return await asyncio.to_thread(docker_svc.restore_dump, client, cid, payload)


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


class FileWriteRequest(BaseModel):
    path: str
    content: str


class MkdirRequest(BaseModel):
    path: str


def _files_call(fn: Any, *args: Any) -> Any:
    """Wspólne mapowanie błędów menedżera plików na kody HTTP."""
    try:
        return fn(*args)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except IsADirectoryError as exc:
        raise HTTPException(400, str(exc)) from exc
    except docker_svc.StoppedContainer as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/containers/{cid}/files")
def container_files_list(cid: str, path: str = "/", ep: str = "local", client: Any = Depends(dk), ctx: Context = Depends(get_ctx)) -> dict:
    srv_dir = ctx.config.srv_dir if ep == "local" else None  # pliki /srv/deployra widzimy bezpośrednio tylko na hoście węzła
    return _files_call(docker_svc.list_container_files, client, cid, path, srv_dir)


@router.get("/containers/{cid}/files/read")
def container_files_read(cid: str, path: str, client: Any = Depends(dk)) -> dict:
    return _files_call(docker_svc.read_container_file, client, cid, path)


@router.get("/containers/{cid}/files/download")
def container_files_download(cid: str, path: str, client: Any = Depends(dk)) -> Response:
    from urllib.parse import quote

    filename, data = _files_call(docker_svc.get_container_file_bytes, client, cid, path)
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"  # polskie znaki nie psują nagłówka
    media = "application/zip" if filename.endswith(".zip") else "application/octet-stream"
    return Response(content=data, media_type=media, headers={"Content-Disposition": disposition})


@router.post("/containers/{cid}/files/write")
def container_files_write(cid: str, body: FileWriteRequest, client: Any = Depends(dk)) -> dict:
    return _files_call(docker_svc.write_container_file, client, cid, body.path, body.content)


@router.post("/containers/{cid}/files/upload")
async def container_files_upload(cid: str, dir_path: str = Form("/"), files: list[UploadFile] = File(...), client: Any = Depends(dk)) -> dict:
    uploaded = []
    clean_dir = "/" + dir_path.strip("/")
    payload = [((f.filename or "").replace("\\", "/").strip("/"), await f.read()) for f in files]
    payload = [(name, content) for name, content in payload if name and name not in (".", "..")]
    if any("/" in name for name, _ in payload):  # wgrany folder: ścieżki względne, jedno archiwum z podfolderami
        uploaded = await asyncio.to_thread(_files_call, docker_svc.upload_container_tree, client, cid, clean_dir, payload)
        return {"ok": True, "dir": clean_dir, "uploaded": uploaded}
    for name, content in payload:  # pojedyncze pliki: zachowują właściciela i tryb nadpisywanych
        await asyncio.to_thread(_files_call, docker_svc.write_container_file, client, cid, f"{clean_dir.rstrip('/')}/{name}", content)
        uploaded.append({"name": name, "size": len(content)})
    return {"ok": True, "dir": clean_dir, "uploaded": uploaded}


@router.delete("/containers/{cid}/files")
def container_files_delete(cid: str, path: str, client: Any = Depends(dk)) -> dict:
    return _files_call(docker_svc.delete_container_file, client, cid, path)


@router.post("/containers/{cid}/files/mkdir")
def container_files_mkdir(cid: str, body: MkdirRequest, client: Any = Depends(dk)) -> dict:
    return _files_call(docker_svc.mkdir_container, client, cid, body.path)


@router.get("/containers/{cid}/logs")
def container_logs(cid: str, tail: int = 300, timestamps: bool = False, client: Any = Depends(dk)) -> dict:
    try:
        raw = client.api.logs(cid, tail=max(1, min(tail, 5000)), timestamps=timestamps)
        return {"lines": raw.decode(errors="replace").splitlines()}
    except Exception as exc:
        return {"lines": [f"⚠️ Błąd odczytu logów kontenera: {exc}"]}


@router.get("/containers/{cid}/logs/stream", response_class=EventSourceResponse)
async def container_logs_stream(
    cid: str, tail: int = 200, timestamps: bool = False, info: dict = Depends(_container), client: Any = Depends(dk)
):
    try:
        async for line in docker_svc.follow_logs(client, cid, tail=max(0, min(tail, 5000)), timestamps=timestamps):
            yield ServerSentEvent(raw_data=line, event="line")
    except Exception as exc:
        yield ServerSentEvent(raw_data=f"⚠️ Błąd strumienia logów: {exc}", event="line")


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
    auth: dict | None = None


class PruneRequest(BaseModel):
    all: bool = False


@router.get("/images")
def images(client: Any = Depends(dk)) -> list[dict]:
    return docker_svc.list_images(client)


@router.post("/images/pull")
async def pull(body: PullRequest, client: Any = Depends(dk), ctx: Context = Depends(get_ctx)) -> dict:
    from deployra_common.schemas import check_image

    image = check_image(body.image)
    return job_response(
        ctx.jobs.start("pull", f"Pobieranie {image}", lambda job: docker_svc.pull_image(client, job, image, auth_config=body.auth))
    )


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
