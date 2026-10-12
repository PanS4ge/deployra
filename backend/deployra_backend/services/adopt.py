"""„Przejmij kontener”: kontener postawiony poza Deployrą (ręcznie, Portainerem…) staje się aplikacją.

Plan przejęcia czytamy z `docker inspect`: obraz, polecenie, porty, zmienne (bez domyślnych z obrazu),
wolumeny i limity. Przejęcie: stary kontener staje i dostaje nazwę `<nazwa>-pre-deployra` (zostaje jako
kopia do ręcznego usunięcia), a Deployra stawia `app-<slug>` z tymi samymi wolumenami i portem hosta.
Jeśli nowy kontener nie wstanie, stary wraca pod swoją nazwą i rusza jak wcześniej.
"""

from __future__ import annotations

from typing import Any

from deployra_common.i18n import tr
from deployra_common.jobs import Job
from deployra_common.schemas import AppSpec

from . import apps, docker_svc

BACKUP_SUFFIX = "-pre-deployra"


def plan(client: Any, cid: str) -> dict[str, Any]:
    info = client.api.inspect_container(cid)
    config, host = info.get("Config") or {}, info.get("HostConfig") or {}
    labels = config.get("Labels") or {}
    name = (info.get("Name") or "").lstrip("/")
    problems = []
    if labels.get(docker_svc.SYSTEM_LABEL):
        problems.append(tr("node.adopt.system"))
    if labels.get(docker_svc.APP_LABEL):
        problems.append(tr("node.adopt.already"))
    if labels.get("com.docker.compose.project"):
        problems.append(tr("node.adopt.compose", project=labels["com.docker.compose.project"]))
    image_env: set[str] = set()
    image_cmd = None
    try:
        image_info = client.api.inspect_image(config.get("Image") or "")
        image_env = set((image_info.get("Config") or {}).get("Env") or [])
        image_cmd = (image_info.get("Config") or {}).get("Cmd")
    except Exception:  # noqa: BLE001 — obrazu już nie ma lokalnie: bierzemy wszystkie zmienne
        pass
    env = {}
    for item in config.get("Env") or []:
        if item in image_env or "=" not in item:
            continue
        key, _, value = item.partition("=")
        env[key] = value
    ports = []
    for key, bindings in sorted((host.get("PortBindings") or {}).items()):
        port, _, proto = key.partition("/")
        if proto != "tcp":
            continue
        for binding in bindings or []:
            if binding.get("HostPort"):
                ports.append({"container": int(port), "host": int(binding["HostPort"])})
    if not ports:  # nic nie wystawione — proponujemy pierwszy port z EXPOSE obrazu/kontenera
        exposed = sorted(int(k.split("/")[0]) for k in (config.get("ExposedPorts") or {}) if k.endswith("/tcp"))
        ports = [{"container": p, "host": None} for p in exposed[:1]]
    volumes = []
    for mount in info.get("Mounts") or []:
        if mount.get("Type") == "volume" and mount.get("Name"):
            volumes.append({"source": mount["Name"], "target": mount["Destination"], "read_only": not mount.get("RW", True)})
        elif mount.get("Type") == "bind" and mount.get("Source"):
            volumes.append({"source": mount["Source"], "target": mount["Destination"], "read_only": not mount.get("RW", True)})
    command = config.get("Cmd")
    return {
        "name": name,
        "image": config.get("Image"),
        "command": command if command and command != image_cmd else None,
        "env": env,
        "ports": ports,
        "volumes": volumes,
        "memory_mb": int(host["Memory"] / 1024 / 1024) if host.get("Memory") else None,
        "cpus": round(host["NanoCpus"] / 1e9, 2) if host.get("NanoCpus") else None,
        "networks": sorted((info.get("NetworkSettings") or {}).get("Networks") or {}),
        "state": (info.get("State") or {}).get("Status"),
        "problems": problems,
    }


def adopt(ctx: Any, job: Job, cid: str, spec: AppSpec) -> dict[str, Any]:
    client = ctx.docker
    current = plan(client, cid)
    if current["problems"]:
        raise ValueError(current["problems"][0])
    info = client.api.inspect_container(cid)
    old_name = info["Name"].lstrip("/")
    backup = f"{old_name}{BACKUP_SUFFIX}"[:120]
    restart = (info.get("HostConfig") or {}).get("RestartPolicy") or {"Name": "no"}
    was_running = bool((info.get("State") or {}).get("Running"))
    job.log(tr("node.adopt.stopping", name=old_name, backup=backup))
    if was_running:
        client.api.stop(cid)
    client.api.rename(cid, backup)
    client.api.update_container(cid, restart_policy={"Name": "no"})  # kopia nie wstaje sama po restarcie serwera
    try:
        result = apps.deploy(ctx, job, spec)
    except BaseException:
        job.log(tr("node.adopt.restoring", name=old_name))
        try:
            client.api.rename(cid, old_name)
            client.api.update_container(cid, restart_policy={k: v for k, v in restart.items() if k in ("Name", "MaximumRetryCount")})
            if was_running:
                client.api.start(cid)
        except Exception as exc:  # noqa: BLE001 — zgłaszamy pierwotny błąd, ale zostawiamy ślad
            job.log(tr("node.adopt.restore_failed", error=exc))
        raise
    job.log(tr("node.adopt.done", backup=backup))
    return {**result, "backup": backup}
