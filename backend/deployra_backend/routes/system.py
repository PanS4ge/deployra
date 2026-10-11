"""Informacje o węźle, zasoby, parowanie, zadania, RPS."""

from __future__ import annotations

import platform
import random
import shutil
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.sse import EventSourceResponse
from loguru import logger
from pydantic import BaseModel

from deployra_common.i18n import tr
from deployra_common.jobs import Job

from .. import NAME, __version__, enroll
from ..context import Context, get_ctx
from ..services import wg_spoke
from ..services.vitals import VitalsSampler
from . import job_events, job_response, signed

public = APIRouter()
router = APIRouter(prefix="/api", dependencies=[Depends(signed)])


@public.get("/health")
def health(ctx: Context = Depends(get_ctx)) -> dict:
    state = ctx.state
    return {
        "ok": True,
        "service": NAME,
        "version": __version__,
        "name": ctx.config.name,
        "paired": state.paired,
        "awaiting": "token" if state.get("join") else ("code" if state.get("pairing_code") else None),
    }


@public.post("/pair")
async def pair(request: Request, ctx: Context = Depends(get_ctx)) -> dict:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, tr("node.expected_json")) from None
    try:
        return enroll.pair(ctx, ctx.pair_gate, body)
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None


def _tool_version(*cmd: str) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        out = subprocess.run(list(cmd), capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


@router.get("/info")
def info(ctx: Context = Depends(get_ctx)) -> dict:
    try:
        docker_version = ctx.docker.api.version()
    except Exception as exc:  # noqa: BLE001 — pokazujemy, że Docker nie działa
        docker_version = {"error": str(exc)}
    wg_conf = ctx.state.get("wg") or {}
    return {
        "service": NAME,
        "version": __version__,
        "node_id": ctx.state.get("node_id"),
        "name": ctx.config.name,
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "docker": {k: docker_version.get(k) for k in ("Version", "ApiVersion", "Os", "Arch", "KernelVersion", "error")},
        "compose": _tool_version("docker", "compose", "version", "--short"),
        "git": _tool_version("git", "--version"),
        "tunnel": {"address": wg_conf.get("address"), "lan_subnets": wg_conf.get("lan_subnets", []), "peer": wg_spoke.status(ctx.config.wg_iface)}
        if wg_conf
        else None,
        "proxmox": ctx.config.proxmox,
        "endpoints": [ep.public() for ep in ctx.endpoints.list()],
        "port_pool": list(ctx.config.port_pool),
        "static_port": ctx.state.get("static_port"),
        "sftp": {"port": ctx.config.sftp_port, "fingerprint": ctx.sftp.fingerprint() if ctx.sftp else None}
        if ctx.config.sftp_enabled
        else None,
        **VitalsSampler.static_info(),
    }


class UpdateRequest(BaseModel):
    version: str


@router.get("/update")
def update_status(log: int = 0, ctx: Context = Depends(get_ctx)) -> dict:
    return {"service": NAME, "version": __version__, **ctx.updates.info(max(0, min(log, 400)))}


@router.post("/update")
def update_start(body: UpdateRequest, ctx: Context = Depends(get_ctx)) -> dict:
    try:
        info = ctx.updates.request(body.version)
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    logger.info(tr("node.log.update_requested", version=info["target"], mode=info["mode"]))
    return {"service": NAME, "version": __version__, **info}


@router.get("/endpoints")
def endpoints(refresh: bool = False, ctx: Context = Depends(get_ctx)) -> list[dict]:
    """Docker hosta i (na Proxmoxie) kontenery LXC z własnym Dockerem."""
    return [ep.public() for ep in ctx.endpoints.list(refresh=refresh)]


@router.get("/vitals")
def vitals(ctx: Context = Depends(get_ctx)) -> dict:
    return ctx.vitals.latest()


@router.post("/rps")
def rps() -> dict:
    """Kamień-papier-nożyce: zabawny test opóźnienia węzła."""
    return {"chosen": random.choice(["rock", "paper", "scissors"])}


@router.get("/jobs")
def jobs(ctx: Context = Depends(get_ctx)) -> list[dict]:
    return [job.snapshot() for job in ctx.jobs.list()]


def _job(job_id: str, ctx: Context = Depends(get_ctx)) -> Job:
    found = ctx.jobs.get(job_id)
    if not found:
        raise HTTPException(404, tr("srv.no_job"))
    return found


@router.get("/jobs/{job_id}")
def job(found: Job = Depends(_job)) -> dict:
    return {**job_response(found), "lines": found.lines[-500:]}


@router.get("/jobs/{job_id}/stream", response_class=EventSourceResponse)
async def job_stream(found: Job = Depends(_job), start: int = 0):
    async for event in job_events(found, start):
        yield event
