"""Operacje na Dockerze przez niskopoziomowe API (szybkie listy, bez inspect per kontener)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

from deployra_common.i18n import tr
from deployra_common.jobs import Job, JobFailed
from deployra_common.schemas import ContainerSpec, NetworkSpec, VolumeSpec

SYSTEM_LABEL = "deployra.system"
APP_LABEL = "deployra.app"
PROTECTED_NETWORKS = {"bridge", "host", "none"}
CONTAINER_ACTIONS = {"start", "stop", "restart", "kill", "pause", "unpause"}


def _name(summary: dict) -> str:
    names = summary.get("Names") or []
    return names[0].lstrip("/") if names else summary["Id"][:12]


def _ports(raw: list[dict] | None) -> list[dict]:
    seen, out = set(), []
    for p in raw or []:
        key = (p.get("PublicPort"), p.get("PrivatePort"), p.get("Type"))
        if key in seen:
            continue
        seen.add(key)
        entry = {"container": p.get("PrivatePort"), "proto": p.get("Type", "tcp")}
        if p.get("PublicPort"):
            entry.update(host=p["PublicPort"], host_ip=p.get("IP"))
        out.append(entry)
    return sorted(out, key=lambda e: (e.get("host") or 0, e["container"] or 0))


def list_containers(client: Any) -> list[dict]:
    out = []
    for c in client.api.containers(all=True):
        labels = c.get("Labels") or {}
        out.append(
            {
                "id": c["Id"],
                "short_id": c["Id"][:12],
                "name": _name(c),
                "image": c.get("Image"),
                "image_id": c.get("ImageID"),
                "state": c.get("State"),
                "status": c.get("Status"),
                "created": c.get("Created"),
                "ports": _ports(c.get("Ports")),
                "app": labels.get(APP_LABEL),
                "stack": labels.get("com.docker.compose.project"),
                "service": labels.get("com.docker.compose.service"),
                "system": labels.get(SYSTEM_LABEL),
                "mounts": [
                    {"type": m.get("Type"), "name": m.get("Name"), "source": m.get("Source"), "destination": m.get("Destination")}
                    for m in c.get("Mounts") or []
                ],
                "networks": sorted(((c.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
            }
        )
    return sorted(out, key=lambda c: c["name"])


def inspect_container(client: Any, cid: str) -> dict:
    return client.api.inspect_container(cid)


def container_action(client: Any, cid: str, action: str) -> None:
    if action not in CONTAINER_ACTIONS:
        raise ValueError(tr("node.unknown_action", action=action))
    getattr(client.api, action)(cid)


def remove_container(client: Any, cid: str, *, force: bool, volumes: bool) -> None:
    info = client.api.inspect_container(cid)
    if (info.get("Config", {}).get("Labels") or {}).get(SYSTEM_LABEL):
        raise PermissionError(tr("node.system_container"))
    client.api.remove_container(cid, force=force, v=volumes)


def container_top(client: Any, cid: str) -> dict:
    return client.api.top(cid)


# --- statystyki ---------------------------------------------------------------


def compute_stats(cur: dict, prev: dict | None) -> dict:
    cpu = 0.0
    if prev:
        cur_cpu, prev_cpu = cur.get("cpu_stats") or {}, prev.get("cpu_stats") or {}
        cpu_delta = (cur_cpu.get("cpu_usage") or {}).get("total_usage", 0) - (prev_cpu.get("cpu_usage") or {}).get(
            "total_usage", 0
        )
        sys_delta = cur_cpu.get("system_cpu_usage", 0) - prev_cpu.get("system_cpu_usage", 0)
        online = cur_cpu.get("online_cpus") or len((cur_cpu.get("cpu_usage") or {}).get("percpu_usage") or [1])
        if sys_delta > 0 and cpu_delta >= 0:
            cpu = cpu_delta / sys_delta * online * 100
    mem = cur.get("memory_stats") or {}
    details = mem.get("stats") or {}
    usage = max(mem.get("usage", 0) - details.get("inactive_file", details.get("cache", 0)), 0)
    limit = mem.get("limit") or 0
    rx = tx = 0
    for net in (cur.get("networks") or {}).values():
        rx += net.get("rx_bytes", 0)
        tx += net.get("tx_bytes", 0)
    read = write = 0
    for entry in (cur.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []:
        if entry.get("op", "").lower() == "read":
            read += entry.get("value", 0)
        elif entry.get("op", "").lower() == "write":
            write += entry.get("value", 0)
    return {
        "cpu": round(cpu, 2),
        "mem_usage": usage,
        "mem_limit": limit,
        "mem": round(usage / limit * 100, 2) if limit else 0.0,
        "net_rx": rx,
        "net_tx": tx,
        "blk_read": read,
        "blk_write": write,
        "pids": (cur.get("pids_stats") or {}).get("current"),
    }


def stats_once(client: Any, cid: str) -> dict:
    """Jedna próbka z prawdziwym CPU% (daemon zbiera dwie próbki, ~1-2 s)."""
    raw = client.api.stats(cid, stream=False)
    return compute_stats(raw, raw.get("precpu_stats") and {"cpu_stats": raw["precpu_stats"]})


async def stats_stream(client: Any, cid: str, interval: float = 2.0) -> AsyncIterator[dict]:
    prev = None
    while True:
        raw = await asyncio.to_thread(client.api.stats, cid, stream=False, one_shot=True)
        yield compute_stats(raw, prev)
        prev = raw
        await asyncio.sleep(interval)


# --- logi ---------------------------------------------------------------------


async def follow_logs(client: Any, cid: str, *, tail: int = 200, follow: bool = True, timestamps: bool = False) -> AsyncIterator[str]:
    """Linie logów; przy rozłączeniu klienta zamyka strumień Dockera (bez wiszących wątków)."""
    stream = await asyncio.to_thread(
        client.api.logs, cid, stream=True, follow=follow, tail=tail, timestamps=timestamps
    )
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1000)

    def pump() -> None:
        try:
            for chunk in stream:
                asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
        except Exception:  # noqa: BLE001 — zamknięty strumień kończy pompę
            pass
        finally:
            try:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=pump, name=f"logs-{cid[:12]}", daemon=True).start()
    buffer = b""
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                yield line.rstrip(b"\r").decode(errors="replace")
        if buffer:
            yield buffer.decode(errors="replace")
    finally:
        close = getattr(stream, "close", None)
        if close:
            await asyncio.to_thread(close)


# --- obrazy -------------------------------------------------------------------


def list_images(client: Any) -> list[dict]:
    used: dict[str, list[str]] = {}
    for c in client.api.containers(all=True):
        used.setdefault(c.get("ImageID", ""), []).append(_name(c))
    out = []
    for img in client.api.images():
        tags = [t for t in img.get("RepoTags") or [] if t != "<none>:<none>"]
        out.append(
            {
                "id": img["Id"],
                "short_id": img["Id"].split(":")[-1][:12],
                "tags": tags,
                "size": img.get("Size", 0),
                "created": img.get("Created"),
                "used_by": used.get(img["Id"], []),
                "dangling": not tags,
            }
        )
    return sorted(out, key=lambda i: (i["tags"][0] if i["tags"] else "~", i["id"]))


def pull_image(client: Any, job: Job, image: str) -> str:
    """Pobiera obraz, logując zmiany statusu warstw (bez zalewania logu paskiem postępu)."""
    from docker.utils import parse_repository_tag

    repo, tag = parse_repository_tag(image)
    tag = tag or "latest"
    job.log(tr("node.pulling", image=f"{repo}:{tag}"))
    last: dict[str, str] = {}
    for event in client.api.pull(repo, tag=tag, stream=True, decode=True):
        if "error" in event:
            raise JobFailed(event.get("error") or tr("node.pull_error"))
        status, layer = event.get("status", ""), event.get("id", "")
        if not layer:
            job.log(status)
        elif last.get(layer) != status and status not in ("Downloading", "Extracting", "Waiting"):
            job.log(f"{layer}: {status}")
        last[layer] = status
    return f"{repo}:{tag}"


def remove_image(client: Any, image_id: str, force: bool = False) -> None:
    client.api.remove_image(image_id, force=force)


def prune_images(client: Any, all_unused: bool = False) -> dict:
    filters = {"dangling": False} if all_unused else {"dangling": True}
    return client.api.prune_images(filters=filters)


# --- wolumeny i sieci -----------------------------------------------------------


def list_volumes(client: Any) -> list[dict]:
    used: dict[str, list[str]] = {}
    for c in client.api.containers(all=True):
        for m in c.get("Mounts") or []:
            if m.get("Type") == "volume" and m.get("Name"):
                used.setdefault(m["Name"], []).append(_name(c))
    volumes = (client.api.volumes() or {}).get("Volumes") or []
    out = [
        {
            "name": v["Name"],
            "driver": v.get("Driver"),
            "mountpoint": v.get("Mountpoint"),
            "created": v.get("CreatedAt"),
            "labels": v.get("Labels") or {},
            "used_by": used.get(v["Name"], []),
        }
        for v in volumes
    ]
    return sorted(out, key=lambda v: v["name"])


def create_volume(client: Any, spec: VolumeSpec) -> dict:
    return client.api.create_volume(spec.name, driver=spec.driver, driver_opts=spec.driver_opts or None, labels=spec.labels or None)


def list_networks(client: Any) -> list[dict]:
    members: dict[str, list[str]] = {}
    for c in client.api.containers(all=True):
        for net in ((c.get("NetworkSettings") or {}).get("Networks") or {}).values():
            if net.get("NetworkID"):
                members.setdefault(net["NetworkID"], []).append(_name(c))
    out = []
    for n in client.api.networks():
        ipam = [cfg.get("Subnet") for cfg in ((n.get("IPAM") or {}).get("Config") or []) if cfg.get("Subnet")]
        out.append(
            {
                "id": n["Id"],
                "short_id": n["Id"][:12],
                "name": n["Name"],
                "driver": n.get("Driver"),
                "scope": n.get("Scope"),
                "internal": n.get("Internal", False),
                "attachable": n.get("Attachable", False),
                "subnets": ipam,
                "containers": members.get(n["Id"], []),
                "protected": n["Name"] in PROTECTED_NETWORKS,
            }
        )
    return sorted(out, key=lambda n: n["name"])


def create_network(client: Any, spec: NetworkSpec) -> dict:
    from docker.types import IPAMConfig, IPAMPool

    ipam = None
    if spec.subnet:
        ipam = IPAMConfig(pool_configs=[IPAMPool(subnet=spec.subnet, gateway=spec.gateway)])
    return client.api.create_network(
        spec.name,
        driver=spec.driver,
        internal=spec.internal,
        attachable=spec.attachable,
        enable_ipv6=spec.enable_ipv6,
        labels=spec.labels or None,
        options=spec.options or None,
        ipam=ipam,
    )


def remove_network(client: Any, network_id: str) -> None:
    info = client.api.inspect_network(network_id)
    if info.get("Name") in PROTECTED_NETWORKS:
        raise PermissionError(tr("node.network_protected"))
    client.api.remove_network(network_id)


def ensure_network(client: Any, name: str) -> None:
    if not client.api.networks(names=[name]):
        client.api.create_network(name, driver="bridge", attachable=True, labels={SYSTEM_LABEL: "network"})


# --- uruchamianie kontenerów -------------------------------------------------


def run_container(client: Any, spec: ContainerSpec) -> str:
    """Tworzy i uruchamia kontener z bezpiecznej specyfikacji. Zwraca id."""
    if spec.network:
        ensure_network(client, spec.network)
    port_bindings: dict[str, Any] = {}
    exposed = []
    for p in spec.ports:
        key = f"{p.container}/{p.protocol}"
        exposed.append((p.container, p.protocol))
        if p.host:
            port_bindings[key] = (p.host_ip, p.host) if p.host_ip else p.host
    binds = {v.source: {"bind": v.target, "mode": "ro" if v.read_only else "rw"} for v in spec.volumes}
    restart = None if spec.restart == "no" else {"Name": spec.restart, **({"MaximumRetryCount": 5} if spec.restart == "on-failure" else {})}
    host_config = client.api.create_host_config(
        port_bindings=port_bindings or None,
        binds=binds or None,
        restart_policy=restart,
        mem_limit=f"{spec.memory_mb}m" if spec.memory_mb else None,
        nano_cpus=int(spec.cpus * 1e9) if spec.cpus else None,
        network_mode=spec.network or None,
        log_config={"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
    )
    container = client.api.create_container(
        spec.image,
        command=spec.command,
        working_dir=spec.working_dir,
        name=spec.name,
        environment=spec.env or None,
        labels=spec.labels or None,
        ports=[p if proto == "tcp" else (p, proto) for p, proto in exposed] or None,
        host_config=host_config,
    )
    client.api.start(container["Id"])
    return container["Id"]


def find_container(client: Any, name: str) -> dict | None:
    for c in client.api.containers(all=True, filters={"name": f"^/{name}$"}):
        return c
    return None
