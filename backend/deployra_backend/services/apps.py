"""Wdrażanie aplikacji na węźle (obraz / compose / statyczna / Git / z plików) z rollbackiem.

Aplikacja może trafić na Docker hosta (`local`) albo — na Proxmoxie — do
wybranego kontenera LXC (`ct-<vmid>`). Wtedy bramka łączy się z IP tego CT.
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone
from typing import Any

from deployra_common.i18n import tr
from deployra_common.jobs import Job, JobFailed
from deployra_common.schemas import AppSpec, ContainerSpec, PortMap, check_slug

from . import compose, docker_svc, git_build, ports, runtime, static_sites

KEEP_BUILDS = 3


def container_name(slug: str) -> str:
    return f"app-{check_slug(slug)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(ctx: Any, slug: str, **info: Any) -> dict:
    def change(apps: dict) -> dict:
        apps[slug] = {**apps.get(slug, {}), **info, "deployed_at": _now()}
        return apps

    return ctx.state.mutate("apps", change)[slug]


def _wait_running(client: Any, cid: str, seconds: float = 6.0) -> bool:
    """Czeka kilka sekund: kontener, który padł albo się restartuje, uznajemy za nieudany."""
    deadline = time.time() + seconds
    while True:
        state = client.api.inspect_container(cid)["State"]
        if state.get("Status") in ("exited", "dead") or state.get("Restarting"):
            return False
        if time.time() >= deadline:
            return bool(state.get("Running"))
        time.sleep(0.5)


def _container_spec(ctx: Any, spec: AppSpec, image: str, host_port: int | None) -> ContainerSpec:
    extra = runtime.container_extras(ctx.config, spec) if spec.source.kind == "runtime" else {}
    return ContainerSpec(
        name=container_name(spec.slug),
        image=image,
        command=extra.get("command") or (spec.source.command if spec.source.kind == "image" else None),
        working_dir=extra.get("working_dir"),
        env=extra.get("env", spec.env),
        ports=[PortMap(container=spec.container_port, host=host_port)] if host_port and spec.container_port else [],
        volumes=extra.get("volumes", spec.volumes),
        labels={docker_svc.APP_LABEL: spec.slug},
        memory_mb=spec.memory_mb,
        cpus=spec.cpus,
        network=ctx.config.docker_network,
    )


def replace_container(ctx: Any, job: Job, spec: AppSpec, image: str, host_port: int | None) -> str:
    """Zastępuje kontener aplikacji nowym; jeśli nowy nie wstanie, wraca do poprzedniego obrazu."""
    client = ctx.docker_for(spec.endpoint)
    name = container_name(spec.slug)
    previous_image = None
    existing = docker_svc.find_container(client, name)
    if existing:
        previous_image = client.api.inspect_container(existing["Id"])["Config"]["Image"]
        job.log(tr("node.app.stopping_previous", image=previous_image))
        client.api.remove_container(existing["Id"], force=True)
    if host_port:
        job.log(tr("node.app.starting", name=name, image=image, host_port=host_port, port=spec.container_port))
    else:
        job.log(tr("node.app.starting_worker", name=name, image=image))
    cid = docker_svc.run_container(client, _container_spec(ctx, spec, image, host_port))
    if _wait_running(client, cid, ctx.wait_seconds):
        job.log(tr("node.app.running"))
        return cid
    logs = client.api.logs(cid, tail=40).decode(errors="replace")
    job.log(tr("node.app.crashed"))
    job.log(logs.rstrip() or tr("node.app.no_logs"))
    client.api.remove_container(cid, force=True)
    if previous_image and previous_image != image:
        job.log(tr("node.app.restoring", image=previous_image))
        docker_svc.run_container(client, _container_spec(ctx, spec, previous_image, host_port))
    raise JobFailed(tr("node.app.failed"))


def deploy(ctx: Any, job: Job, spec: AppSpec) -> dict:
    lock = ctx.deploy_lock(spec.slug)
    if not lock.acquire(blocking=False):
        raise JobFailed(tr("node.app.busy"))
    try:
        return _deploy(ctx, job, spec)
    finally:
        lock.release()


def _deploy(ctx: Any, job: Job, spec: AppSpec) -> dict:
    cfg, kind = ctx.config, spec.source.kind
    endpoint = ctx.endpoints.get(spec.endpoint)
    client = ctx.docker_for(spec.endpoint)
    docker_env = ctx.endpoints.docker_host_env(spec.endpoint)
    previous = ctx.state.get("apps", {}).get(spec.slug, {})
    if previous.get("endpoint", "local") != spec.endpoint and previous.get("host_port"):
        raise JobFailed(tr("node.app.other_endpoint"))
    base = {"kind": kind, "endpoint": spec.endpoint, "upstream_host": endpoint.ip if endpoint.kind == "lxc" else None}
    if endpoint.kind == "lxc":
        job.log(tr("node.app.target", name=endpoint.name, vmid=endpoint.vmid, ip=endpoint.ip or "?"))

    if kind == "static" or (kind == "git" and spec.source.git_static_dir):
        static_sites.ensure_site_dir(cfg.sites_dir, spec.slug)
        sha = None
        if kind == "git":
            repo, sha = git_build.checkout(
                job, cfg.builds_dir, cfg.data_dir, spec.slug, spec.source.git_url, spec.source.git_branch,
                spec.source.git_token, spec.source.git_ssh_key,
            )  # fmt: skip
            source = git_build.inside(repo, spec.source.git_static_dir or ".")
            static_sites.publish_dir(cfg.sites_dir, spec.slug, source)
            job.log(tr("node.app.site_updated"))
        port = static_sites.apply_site(ctx, spec.slug, spec.domains, job)
        job.log(tr("node.app.static_ready", port=port))
        return _record(ctx, spec.slug, **base, host_port=port, sha=sha, domains=spec.domains)

    if kind == "runtime" and not spec.container_port:  # proces w tle (bot, skrypt) — bez portu i domeny
        ports.release(ctx.state, spec.slug)
        image = runtime.prepare(ctx, job, spec, client)
        cid = replace_container(ctx, job, spec, image, None)
        return _record(ctx, spec.slug, **base, host_port=None, image=image, runtime=spec.source.runtime, container=cid)

    host_port = ports.allocate(
        ctx.state, client, spec.slug, cfg.port_pool, spec.host_port or previous.get("host_port"), check_host=endpoint.kind == "host"
    )

    if kind == "runtime":
        image = runtime.prepare(ctx, job, spec, client)
        cid = replace_container(ctx, job, spec, image, host_port)
        return _record(ctx, spec.slug, **base, host_port=host_port, image=image, runtime=spec.source.runtime, container=cid)

    if kind == "image":
        image = docker_svc.pull_image(client, job, spec.source.image)
        cid = replace_container(ctx, job, spec, image, host_port)
        return _record(ctx, spec.slug, **base, host_port=host_port, image=image, previous_image=previous.get("image"), container=cid)

    if kind == "compose":
        service = spec.source.expose_service or next(iter(compose.parse_compose(spec.source.compose)["services"]))
        compose.parse_compose(spec.source.compose, service)
        override = compose.expose_override(service, host_port, spec.container_port, spec.slug)
        root = compose.stacks_root(cfg.stacks_dir, spec.endpoint)
        compose.write_stack(root, spec.slug, spec.source.compose, spec.env, override)
        compose.up(job, root, spec.slug, pull=True, env=docker_env)
        return _record(ctx, spec.slug, **base, host_port=host_port, service=service)

    if kind == "git":
        repo, sha = git_build.checkout(
            job, cfg.builds_dir, cfg.data_dir, spec.slug, spec.source.git_url, spec.source.git_branch,
            spec.source.git_token, spec.source.git_ssh_key,
        )  # fmt: skip
        tag = f"deployra/{spec.slug}:{sha[:12]}"
        git_build.build(job, repo, spec.source.dockerfile, spec.source.build_context, tag, env=docker_env)
        cid = replace_container(ctx, job, spec, tag, host_port)
        _prune_builds(client, spec.slug, keep={tag, previous.get("image")}, job=job)
        return _record(
            ctx, spec.slug, **base, host_port=host_port, image=tag, previous_image=previous.get("image"), sha=sha, container=cid
        )

    raise JobFailed(tr("node.app.unknown_kind", kind=kind))


def _prune_builds(client: Any, slug: str, keep: set, job: Job) -> None:
    images = [i for i in client.api.images(name=f"deployra/{slug}") if i.get("RepoTags")]
    images.sort(key=lambda i: i.get("Created", 0), reverse=True)
    for img in images[KEEP_BUILDS:]:
        if not keep.intersection(img["RepoTags"]):
            try:
                client.api.remove_image(img["Id"], force=False)
                job.log(tr("node.app.old_build_removed", tags=", ".join(img["RepoTags"])))
            except Exception:  # noqa: BLE001 — obraz w użyciu, zostawiamy
                pass


def rollback(ctx: Any, job: Job, slug: str, spec: AppSpec) -> dict:
    info = ctx.state.get("apps", {}).get(slug) or {}
    previous = info.get("previous_image")
    if info.get("kind") not in ("image", "git") or not previous:
        raise JobFailed(tr("node.app.no_previous"))
    cid = replace_container(ctx, job, spec, previous, info["host_port"])
    return _record(ctx, slug, image=previous, previous_image=info.get("image"), container=cid)


def remove(ctx: Any, job: Job, slug: str, delete_data: bool) -> None:
    cfg = ctx.config
    info = ctx.state.get("apps", {}).get(slug) or {}
    endpoint = info.get("endpoint", "local")
    kind = info.get("kind")
    try:
        client = ctx.docker_for(endpoint)
        docker_env = ctx.endpoints.docker_host_env(endpoint)
    except LookupError:
        job.log(tr("node.app.endpoint_down", endpoint=endpoint))
        client, docker_env = None, {}
    root = compose.stacks_root(cfg.stacks_dir, endpoint)
    if client and (kind == "compose" or (root / slug / compose.COMPOSE_FILE).exists()):
        compose.remove(job, root, slug, volumes=delete_data, env=docker_env)
    existing = docker_svc.find_container(client, container_name(slug)) if client else None
    if existing:
        client.api.remove_container(existing["Id"], force=True, v=delete_data)
        job.log(tr("node.app.container_removed", name=container_name(slug)))
    if kind == "static" or (kind == "git" and info.get("host_port") == ctx.state.get("static_port")):
        static_sites.remove_site(ctx, slug, delete_files=delete_data)
        job.log(tr("node.app.site_conf_removed"))
    if kind == "runtime" and delete_data:
        shutil.rmtree(static_sites.site_dir(cfg.sites_dir, slug), ignore_errors=True)
    if delete_data:
        shutil.rmtree(cfg.builds_dir / slug, ignore_errors=True)
    ports.release(ctx.state, slug)
    ctx.state.mutate("apps", lambda apps: {k: v for k, v in apps.items() if k != slug})
    job.log(tr("node.app.removed", slug=slug))
