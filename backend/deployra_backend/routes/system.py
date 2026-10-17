"""Informacje o węźle, zasoby, parowanie, zadania, RPS."""

from __future__ import annotations

import ipaddress
import platform
import random
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
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


_CACHED_IP: tuple[float, str | None] = (0.0, None)


def detect_host_ip() -> str | None:
    global _CACHED_IP
    now = time.monotonic()
    if now - _CACHED_IP[0] < 300.0 and _CACHED_IP[1]:
        return _CACHED_IP[1]
    ip = None
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with httpx.Client(timeout=1.5) as client:
                res = client.get(url)
                if res.status_code == 200:
                    cand = res.text.strip()
                    ipaddress.ip_address(cand)
                    ip = cand
                    break
        except Exception:
            pass
    if not ip:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("1.1.1.1", 80))
                cand = s.getsockname()[0]
                if cand and not cand.startswith("127."):
                    ip = cand
        except Exception:
            pass
    if not ip:
        try:
            cand = socket.gethostbyname(platform.node())
            if cand and not cand.startswith("127."):
                ip = cand
        except Exception:
            pass
    _CACHED_IP = (now, ip)
    return ip


@router.get("/info")
def info(refresh: bool = False, ctx: Context = Depends(get_ctx)) -> dict:
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
        "ip": detect_host_ip(),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "docker": {k: docker_version.get(k) for k in ("Version", "ApiVersion", "Os", "Arch", "KernelVersion", "error")},
        "compose": _tool_version("docker", "compose", "version", "--short"),
        "git": _tool_version("git", "--version"),
        "tunnel": {"address": wg_conf.get("address"), "lan_subnets": wg_conf.get("lan_subnets", []), "peer": wg_spoke.status(ctx.config.wg_iface)}
        if wg_conf
        else None,
        "proxmox": ctx.config.proxmox,
        "proxmox_url": ctx.state.get("proxmox_url") or ctx.config.proxmox_url or (f"https://{detect_host_ip() or '127.0.0.1'}:8006" if ctx.config.proxmox else None),
        "endpoints": [ep.public() for ep in ctx.endpoints.list(refresh=refresh)],
        "port_pool": list(ctx.config.port_pool),
        "static_port": ctx.state.get("static_port"),
        "sftp": {"port": ctx.state.get("sftp_port") or ctx.config.sftp_port, "fingerprint": ctx.sftp.fingerprint() if ctx.sftp else None}
        if ctx.config.sftp_enabled
        else None,
        **VitalsSampler.static_info(),
    }


class ProxmoxConfig(BaseModel):
    url: str


@router.get("/proxmox/entities")
def proxmox_entities_route(ctx: Context = Depends(get_ctx)) -> list[dict]:
    if not ctx.config.proxmox:
        return []
    from ..services import proxmox
    return proxmox.discover_proxmox_entities(
        ctx.config.proc_dir, ctx.config.pve_dir, getattr(ctx.config, "pve_qemu_dir", Path("/etc/pve/qemu-server"))
    )


@router.post("/proxmox/{kind}/{vmid}/action/{action}")
def proxmox_action_route(kind: str, vmid: int, action: str, ctx: Context = Depends(get_ctx)) -> dict:
    from ..services import proxmox
    proxmox.execute_proxmox_action(kind, vmid, action)
    return {"ok": True}


@router.post("/proxmox/config")
def proxmox_config_route(body: ProxmoxConfig, ctx: Context = Depends(get_ctx)) -> dict:
    ctx.state.update(proxmox_url=body.url.strip())
    return {"ok": True, "proxmox_url": body.url.strip()}


def collect_node_ports(ctx: Context) -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    if ctx.config.sftp_enabled:
        sftp_port = ctx.state.get("sftp_port") or ctx.config.sftp_port
        ports.append({"port": sftp_port, "protocol": "tcp", "owner": "SFTP", "kind": "system"})
    if ctx.config.wg_enabled:
        wg_conf = ctx.state.get("wg") or {}
        wg_port = ctx.state.get("wg_port") or (wg_conf.get("listen_port") if isinstance(wg_conf, dict) else None) or 51820
        ports.append({"port": wg_port, "protocol": "udp", "owner": "WireGuard", "kind": "system"})
    try:
        for ep in ctx.endpoints.list():
            try:
                dk = ctx.docker_for(ep.id)
                for c in dk.containers.list(all=True):
                    c_name = c.name
                    net_settings = getattr(c, "attrs", {}).get("NetworkSettings", {})
                    port_bindings = net_settings.get("Ports") or {}
                    for cont_port, bindings in port_bindings.items():
                        proto = "udp" if "/udp" in cont_port else "tcp"
                        if bindings:
                            for b in bindings:
                                host_p = b.get("HostPort")
                                if host_p and str(host_p).isdigit():
                                    ports.append({
                                        "port": int(host_p),
                                        "protocol": proto,
                                        "owner": c_name,
                                        "kind": "container",
                                        "endpoint": ep.id,
                                        "container_port": cont_port,
                                    })
            except Exception:
                pass
    except Exception:
        pass
    ports.sort(key=lambda p: (p["port"], p["protocol"]))
    return ports


@router.get("/ports")
def node_ports(ctx: Context = Depends(get_ctx)) -> dict:
    return {
        "pool": list(ctx.config.port_pool),
        "used": collect_node_ports(ctx),
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


class PeerUpdateRequest(BaseModel):
    container: str
    version: str


@router.get("/update/peers")
def update_peers(log: str = "", ctx: Context = Depends(get_ctx)) -> dict:
    """Panel i bramka na tym hoście Dockera, które ten węzeł umie podmienić (aktualizacje bez sidecara)."""
    peers = ctx.updates.peers() if hasattr(ctx.updates, "peers") else []
    if log:
        for peer in peers:
            if peer["name"] == log:
                peer["log"] = ctx.updates.peer_log(log)
    return {"service": NAME, "version": __version__, "peers": peers}


@router.post("/update/peer")
def update_peer(body: PeerUpdateRequest, ctx: Context = Depends(get_ctx)) -> dict:
    if not hasattr(ctx.updates, "request_peer"):
        raise HTTPException(409, tr("srv.upd.no_updater"))
    try:
        result = ctx.updates.request_peer(body.container, body.version)
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    logger.info(tr("node.log.update_requested", version=result["target"], mode=f"peer:{result['name']}"))
    return result


@router.get("/endpoints")
def endpoints(refresh: bool = False, ctx: Context = Depends(get_ctx)) -> list[dict]:
    """Docker hosta i (na Proxmoxie) kontenery LXC z własnym Dockerem."""
    return [ep.public() for ep in ctx.endpoints.list(refresh=refresh)]


@router.get("/vitals")
def vitals(ctx: Context = Depends(get_ctx)) -> dict:
    return ctx.vitals.latest()


@router.get("/usage")
def usage(ctx: Context = Depends(get_ctx)) -> dict:
    """Zasoby hosta i kontenerów aplikacji — panel zbiera je co minutę do historii i alertów."""
    return ctx.usage.collect(ctx)


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
