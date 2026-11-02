"""Aktualizacja „w miejscu”: ten sam kontener (nazwa, porty, sieci, wolumeny, etykiety stacka) na nowym obrazie.

Dla kontenerów postawionych poza Deployrą (ręcznie, Portainerem, stackiem compose), które aplikacja panelu tylko
wskazuje. Konfigurację czytamy z `docker inspect` i pomijamy to, co przyszło ze starego obrazu (zmienne typu
PHP_VERSION, polecenie, katalog roboczy…), żeby nowy obraz dał swoje. Anonimowe wolumeny podpinamy pod nowy kontener
z nazwy — dane zostają. Stary kontener czeka pod nazwą `<nazwa>-pre-update`, aż nowy wstanie (i odpowie przez HTTP,
jeśli panel o to prosi); inaczej wraca na swoje miejsce i rusza jak wcześniej.
"""

from __future__ import annotations

from typing import Any

from deployra_common.errors import CodedError
from deployra_common.i18n import tr
from deployra_common.jobs import Job, JobFailed

from . import apps, docker_svc

BACKUP_SUFFIX = "-pre-update"
DEPLOYRA_IMAGES = ("ghcr.io/pans4ge/deployra-",)  # kontenery Deployry aktualizuje aktualizator
# klucze Config, które obraz ustawia sam — zostawiamy je w nowym kontenerze tylko, gdy ktoś je zmienił
IMAGE_DEFAULTS = ("Cmd", "Entrypoint", "WorkingDir", "User", "StopSignal", "Healthcheck", "Shell", "OnBuild")


def _endpoint(net: dict, short_id: str) -> dict:
    out: dict[str, Any] = {}
    aliases = [a for a in net.get("Aliases") or [] if a != short_id]
    if aliases:
        out["Aliases"] = aliases
    if net.get("IPAMConfig"):
        out["IPAMConfig"] = net["IPAMConfig"]
    return out


def new_config(info: dict, image_config: dict | None, image: str) -> tuple[dict, dict[str, dict]]:
    """(konfiguracja do `POST /containers/create`, pozostałe sieci do podpięcia po utworzeniu kontenera)."""
    old = image_config or {}
    config = {k: v for k, v in (info.get("Config") or {}).items() if k not in ("MacAddress",)}
    config["Image"] = image
    image_env = set(old.get("Env") or [])
    config["Env"] = [item for item in config.get("Env") or [] if item not in image_env]
    for key in IMAGE_DEFAULTS:
        if key in config and config[key] == old.get(key):
            config.pop(key)
    image_labels = old.get("Labels") or {}
    config["Labels"] = {k: v for k, v in (config.get("Labels") or {}).items() if image_labels.get(k) != v}
    image_volumes = old.get("Volumes") or {}
    config["Volumes"] = {k: v for k, v in (config.get("Volumes") or {}).items() if k not in image_volumes} or None
    short_id = info["Id"][:12]
    if config.get("Hostname") == short_id:  # domyślna nazwa hosta = ID kontenera; nowy dostanie swoje
        config.pop("Hostname")

    host = dict(info.get("HostConfig") or {})
    binds = list(host.get("Binds") or [])
    taken = {b.split(":")[1] for b in binds if b.count(":") >= 1} | {m.get("Target") for m in host.get("Mounts") or []}
    for mount in info.get("Mounts") or []:  # anonimowe wolumeny (VOLUME w obrazie) — z nazwy, inaczej dane by przepadły
        if mount.get("Type") == "volume" and mount.get("Name") and mount.get("Destination") not in taken:
            binds.append(f"{mount['Name']}:{mount['Destination']}" + ("" if mount.get("RW", True) else ":ro"))
    host["Binds"] = binds or None
    config["HostConfig"] = host

    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    mode = host.get("NetworkMode") or "default"
    if mode in ("host", "none") or mode.startswith("container:"):
        return config, {}
    primary = "bridge" if mode == "default" else mode
    if primary in networks:
        config["NetworkingConfig"] = {"EndpointsConfig": {primary: _endpoint(networks[primary], short_id)}}
    return config, {name: _endpoint(net, short_id) for name, net in networks.items() if name != primary}


def _host_port(info: dict) -> int | None:
    for bindings in ((info.get("HostConfig") or {}).get("PortBindings") or {}).values():
        for binding in bindings or []:
            if str(binding.get("HostPort") or "").isdigit():
                return int(binding["HostPort"])
    return None


def _settled(ctx: Any, job: Job, client: Any, cid: str, verify_port: int | None, health_path: str) -> bool:
    if not apps._wait_running(client, cid, ctx.wait_seconds):
        job.log(tr("node.app.crashed"))
        return False
    if (client.api.inspect_container(cid)["State"].get("Health") or {}).get("Status") == "unhealthy":
        job.log(tr("node.inplace.unhealthy"))
        return False
    if verify_port:
        url = apps._health_url(ctx.endpoints.get("local"), verify_port, health_path or "/")
        if not apps._wait_healthy(ctx, job, url, lambda: client.api.inspect_container(cid)["State"]):
            job.log(tr("node.app.verify_failed"))
            return False
    return True


def update(ctx: Any, job: Job, container: str, image: str, verify: bool = False, health_path: str = "/", cleanup: bool = True,
           pull: bool = True) -> dict:  # fmt: skip
    client = ctx.docker
    info = client.api.inspect_container(container)
    cid, name = info["Id"], info["Name"].lstrip("/")
    labels = (info.get("Config") or {}).get("Labels") or {}
    old_ref, old_id = (info.get("Config") or {}).get("Image") or "", info.get("Image") or ""
    if labels.get(docker_svc.SYSTEM_LABEL) or old_ref.lower().startswith(DEPLOYRA_IMAGES):  # węzeł nie podmienia sam siebie
        raise CodedError(tr("node.inplace.system", name=name), code="DPL-SYSTEM", container=name)
    try:
        image_config = client.api.inspect_image(old_id or old_ref).get("Config") or {}
    except Exception:  # noqa: BLE001 — starego obrazu już nie ma: zmienne kontenera idą w całości
        image_config = None
        job.log(tr("node.inplace.no_old_image", image=old_ref))
    stack = labels.get("com.docker.compose.project")
    job.log(tr("node.inplace.start", name=name, old=old_ref, new=image))
    new_ref = docker_svc.pull_image(client, job, image) if pull else image
    host_port = _host_port(info)
    try:
        same = bool(old_id) and client.api.inspect_image(new_ref).get("Id") == old_id
    except Exception:  # noqa: BLE001 — nie da się porównać: aktualizujemy
        same = False
    if same:  # kontener już działa na tym obrazie — odtwarzanie nic by nie zmieniło
        job.log(tr("node.inplace.same_image", name=name, image=new_ref))
        return {"container": cid, "name": name, "image": new_ref, "previous_image": old_ref, "host_port": host_port, "backup": None,
                "stack": stack, "unchanged": True}  # fmt: skip
    config, extra = new_config(info, image_config, new_ref)
    was_running = bool((info.get("State") or {}).get("Running"))
    restart = (info.get("HostConfig") or {}).get("RestartPolicy") or {"Name": "no"}
    backup = f"{name}{BACKUP_SUFFIX}"[:120]
    stale = docker_svc.find_container(client, backup)
    if stale:  # kopia z poprzedniej aktualizacji bez sprzątania
        if stale.get("State") == "running":
            raise CodedError(tr("node.inplace.backup_running", backup=backup), code="DPL-INPLACE-BACKUP", container=backup)
        client.api.remove_container(stale["Id"], force=True)

    client.api.rename(cid, backup)
    client.api.update_container(cid, restart_policy={"Name": "no"})  # kopia nie wstaje sama po restarcie serwera
    new_id = None
    try:
        new_id = client.api.create_container_from_config(config, name=name)["Id"]
        for network, endpoint in extra.items():
            client.api.connect_container_to_network(new_id, network, aliases=endpoint.get("Aliases"),
                                                    ipv4_address=(endpoint.get("IPAMConfig") or {}).get("IPv4Address"))  # fmt: skip
        if was_running:
            job.log(tr("node.inplace.swapping", name=name))
            client.api.stop(cid)
            client.api.start(new_id)
            if not _settled(ctx, job, client, new_id, host_port if verify else None, health_path):
                logs = client.api.logs(new_id, tail=40).decode(errors="replace")
                job.log(logs.rstrip() or tr("node.app.no_logs"))
                raise CodedError(tr("node.inplace.failed", name=name, image=new_ref), code="DPL-INPLACE-START", container=name,
                                 image=new_ref, previous=old_ref)  # fmt: skip
        else:
            job.log(tr("node.inplace.left_stopped", name=name))
    except BaseException:
        job.log(tr("node.inplace.restoring", name=name, image=old_ref))
        try:
            if new_id:
                client.api.remove_container(new_id, force=True)
            client.api.rename(cid, name)
            client.api.update_container(cid, restart_policy={k: v for k, v in restart.items() if k in ("Name", "MaximumRetryCount")})
            if was_running:
                client.api.start(cid)
        except Exception as exc:  # noqa: BLE001 — zgłaszamy pierwotny błąd, ale zostawiamy ślad
            job.log(tr("node.adopt.restore_failed", error=exc))
        raise

    kept = backup
    if cleanup:
        try:
            client.api.remove_container(cid, force=True)
            job.log(tr("node.adopt.backup_removed", backup=backup))
            kept = None
        except Exception as exc:  # noqa: BLE001 — zostaje jako kopia
            job.log(tr("node.adopt.restore_failed", error=exc))
        if old_id and old_id != client.api.inspect_container(new_id).get("Image"):
            apps._drop_image(client, job, old_id)
    if kept:
        job.log(tr("node.inplace.backup_kept", backup=kept))
    if stack:
        job.log("⚠ " + tr("node.inplace.stack_note", stack=stack, image=new_ref))
    job.log(tr("node.inplace.done", name=name, image=new_ref))
    return {"container": new_id, "name": name, "image": new_ref, "previous_image": old_ref, "host_port": host_port, "backup": kept,
            "stack": stack}  # fmt: skip


def run(ctx: Any, job: Job, container: str, image: str, **options: Any) -> dict:
    lock = ctx.deploy_lock(f"inplace:{container}")
    if not lock.acquire(blocking=False):
        raise JobFailed(tr("node.app.busy"))
    try:
        return update(ctx, job, container, image, **options)
    finally:
        lock.release()
