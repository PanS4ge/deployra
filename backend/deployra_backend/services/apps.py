"""Wdrażanie aplikacji na węźle (obraz / compose / statyczna / Git / z plików) z rollbackiem.

Aplikacja może trafić na Docker hosta (`local`) albo — na Proxmoxie — do
wybranego kontenera LXC (`ct-<vmid>`). Wtedy bramka łączy się z IP tego CT.

Wdrożenie bez przestojów (`zero_downtime`): nowy kontener startuje obok starego na
nowym porcie i czeka na test zdrowia. Węzeł zwraca wynik z `handover=True`; panel
przełącza bramkę na nowy port, a dopiero potem wywołuje `commit_handover` (stary
kontener znika po chwili, nowy przejmuje nazwę). Gdy test się nie uda, `abort`/błąd
zostawia stary kontener bez zmian.
"""

from __future__ import annotations

import http.client
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from deployra_common.i18n import tr
from deployra_common.jobs import Job, JobFailed
from deployra_common.schemas import AppSpec, ContainerSpec, PortMap, check_slug

from . import compose, docker_svc, git_build, ports, runtime, static_sites

KEEP_BUILDS = 3


def container_name(slug: str) -> str:
    return f"app-{check_slug(slug)}"


def next_name(slug: str) -> str:
    """Tymczasowa nazwa nowej wersji podczas wdrożenia bez przestojów."""
    return f"{container_name(slug)}-next"


def http_status(url: str) -> int | None:
    """Kod odpowiedzi HTTP spod adresu z węzła; None, gdy nic nie odpowiada."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 — adres budujemy z własnego portu
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(ctx: Any, slug: str, **info: Any) -> dict:
    def change(apps: dict) -> dict:
        kept = {k: v for k, v in apps.get(slug, {}).items() if k != "handover"}  # oczekujące przełączenie już załatwione
        apps[slug] = {**kept, **info, "deployed_at": _now()}
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


def _container_spec(ctx: Any, spec: AppSpec, image: str, host_port: int | None, name: str | None = None) -> ContainerSpec:
    extra = runtime.container_extras(ctx.config, spec) if spec.source.kind == "runtime" else {}
    return ContainerSpec(
        name=name or container_name(spec.slug),
        image=image,
        command=extra.get("command") or (spec.source.command if spec.source.kind == "image" else None),
        working_dir=extra.get("working_dir"),
        env=extra.get("env", spec.runtime_env()),
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


def _health_url(endpoint: Any, host_port: int, path: str) -> str:
    """Adres testu zdrowia od strony węzła (kontener LXC: IP CT, Docker hosta: localhost)."""
    host = (endpoint.ip or "127.0.0.1") if endpoint.kind == "lxc" else "127.0.0.1"
    return f"http://{host}:{host_port}{path}"


def _wait_healthy(ctx: Any, job: Job, url: str, check: Any) -> bool:
    """Co sekundę pyta adres testu zdrowia, aż odpowie kodem < 500 albo minie limit czasu."""
    job.log(tr("node.app.health_wait", url=url))
    deadline = time.time() + ctx.health_timeout
    last: int | None = None
    while True:
        state = check()
        if state.get("Status") in ("exited", "dead") or state.get("Restarting"):
            return False
        if state.get("Running"):
            last = http_status(url)
            if last is not None and last < 500:
                job.log(tr("node.app.health_ok", status=last))
                return True
        if time.time() >= deadline:
            job.log(tr("node.app.health_timeout", url=url, timeout=int(ctx.health_timeout),
                       last=last if last is not None else tr("node.app.health_no_answer")))  # fmt: skip
            return False
        time.sleep(ctx.health_interval)


def _git_image(ctx: Any, job: Job, spec: AppSpec) -> tuple[str, str]:
    """Klon repozytorium i obraz z Dockerfile; zwraca (tag obrazu, sha commita)."""
    cfg = ctx.config
    repo, sha = git_build.checkout(
        job, cfg.builds_dir, cfg.data_dir, spec.slug, spec.source.git_url, spec.source.git_branch,
        spec.source.git_token, spec.source.git_ssh_key,
    )  # fmt: skip
    tag = f"deployra/{spec.slug}:{sha[:12]}"
    git_build.build(job, repo, spec.source.dockerfile, spec.source.build_context, tag, env=ctx.endpoints.docker_host_env(spec.endpoint),
                    build_args=spec.build_env(), pull=spec.refresh_image)  # fmt: skip
    return tag, sha


def _handover_deploy(ctx: Any, job: Job, spec: AppSpec, base: dict, previous: dict) -> dict:
    """Wdrożenie bez przestojów, krok 1: nowa wersja obok starej, na nowym porcie, po teście zdrowia.

    Stara wersja działa dalej bez zmian. Zapisujemy oczekujące przełączenie; bramkę przełącza panel,
    a potem `commit_handover`. Błąd po stronie węzła zostawia stary kontener i zwalnia nowy.
    """
    cfg, kind = ctx.config, spec.source.kind
    client = ctx.docker_for(spec.endpoint)
    endpoint = ctx.endpoints.get(spec.endpoint)
    if kind == "runtime":
        image, extra = runtime.prepare(ctx, job, spec, client), {"runtime": spec.source.runtime}
    elif kind == "image":
        image = docker_svc.pull_image(client, job, spec.source.image)
        extra = {"previous_image": previous.get("image")}
    else:
        image, sha = _git_image(ctx, job, spec)
        extra = {"previous_image": previous.get("image"), "sha": sha}

    stale = docker_svc.find_container(client, next_name(spec.slug))  # pozostałość po przerwanym wdrożeniu
    if stale:
        job.log(tr("node.app.handover_stale", name=next_name(spec.slug)))
        client.api.remove_container(stale["Id"], force=True)
    host_port = ports.allocate_spare(ctx.state, client, spec.slug, cfg.port_pool, check_host=endpoint.kind == "host")
    job.log(tr("node.app.handover_starting", name=next_name(spec.slug), image=image, host_port=host_port, port=spec.container_port))
    try:
        cid = docker_svc.run_container(client, _container_spec(ctx, spec, image, host_port, name=next_name(spec.slug)))
    except Exception:
        ports.release_spare(ctx.state, spec.slug)
        raise

    if not _wait_running(client, cid, ctx.wait_seconds):
        healthy = False
        job.log(tr("node.app.crashed"))
    elif spec.health_path:
        healthy = _wait_healthy(ctx, job, _health_url(endpoint, host_port, spec.health_path),
                                lambda: client.api.inspect_container(cid)["State"])  # fmt: skip
    else:
        healthy = True
        job.log(tr("node.app.health_running"))
    if not healthy:
        job.log(client.api.logs(cid, tail=40).decode(errors="replace").rstrip() or tr("node.app.no_logs"))
        client.api.remove_container(cid, force=True)
        ports.release_spare(ctx.state, spec.slug)
        job.log(tr("node.app.handover_kept"))
        raise JobFailed(tr("node.app.failed"))

    if kind == "git":
        _prune_builds(client, spec.slug, keep={image, previous.get("image")}, job=job)
    record = {**base, **extra, "image": image, "host_port": host_port, "container": cid}
    ctx.state.mutate("apps", lambda apps: {**apps, spec.slug: {**apps.get(spec.slug, {}), "handover": record}})
    job.log(tr("node.app.handover_ready", host_port=host_port))
    return {"host_port": host_port, "upstream_host": base["upstream_host"], "handover": True}


def commit_handover(ctx: Any, job: Job, slug: str) -> dict:
    """Wdrożenie bez przestojów, krok 2: bramka już ma nowy port — stara wersja znika, nowa przejmuje nazwę."""
    lock = ctx.deploy_lock(slug)
    if not lock.acquire(blocking=False):
        raise JobFailed(tr("node.app.busy"))
    try:
        record = (ctx.state.get("apps", {}).get(slug) or {}).get("handover")
        if not record:
            raise JobFailed(tr("node.app.no_handover"))
        client = ctx.docker_for(record["endpoint"])
        name = container_name(slug)
        old = docker_svc.find_container(client, name)
        ports.commit_spare(ctx.state, slug)
        _record(ctx, slug, **record)
        if old:
            job.log(tr("node.app.handover_grace", seconds=int(ctx.handover_grace)))
            time.sleep(ctx.handover_grace)
            client.api.remove_container(old["Id"], force=True)
            job.log(tr("node.app.container_removed", name=name))
        client.api.rename(record["container"], name)
        job.log(tr("node.app.handover_done", host_port=record["host_port"]))
        return {"host_port": record["host_port"]}
    finally:
        lock.release()


def abort_handover(ctx: Any, job: Job, slug: str) -> dict:
    """Wycofanie przed przełączeniem bramki: usuwa nową wersję, stara zostaje nietknięta."""
    lock = ctx.deploy_lock(slug)
    if not lock.acquire(blocking=False):
        raise JobFailed(tr("node.app.busy"))
    try:
        info = ctx.state.get("apps", {}).get(slug) or {}
        record = info.get("handover")
        if record:
            try:
                client = ctx.docker_for(record["endpoint"])
                client.api.remove_container(record["container"], force=True)
            except Exception:  # noqa: BLE001 — kontenera mogło już nie być, a stan i tak sprzątamy
                pass
            job.log(tr("node.app.handover_aborted"))
        ports.release_spare(ctx.state, slug)
        _drop_handover(ctx, slug)
        return {"host_port": info.get("host_port")}
    finally:
        lock.release()


def _drop_handover(ctx: Any, slug: str) -> None:
    def change(apps: dict) -> dict:
        if slug in apps:
            apps[slug] = {k: v for k, v in apps[slug].items() if k != "handover"}
        return apps

    ctx.state.mutate("apps", change)


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

    if spec.zero_downtime and kind in ("image", "git", "runtime"):  # z portem (bez portu — wyżej)
        return _handover_deploy(ctx, job, spec, base, previous)

    wanted = spec.host_port or previous.get("host_port")
    host_port = ports.allocate(ctx.state, client, spec.slug, cfg.port_pool, wanted, check_host=endpoint.kind == "host",
                               fallback=not spec.host_port)  # fmt: skip
    if wanted and host_port != wanted:  # zapamiętany port ktoś zajął — aplikacja dostaje inny, panel przepnie bramkę
        job.log(tr("node.port_moved", old=wanted, port=host_port))

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
        compose.write_stack(root, spec.slug, spec.source.compose, spec.runtime_env(), override)
        compose.up(job, root, spec.slug, pull=True, env=docker_env)
        return _record(ctx, spec.slug, **base, host_port=host_port, service=service)

    if kind == "git":
        tag, sha = _git_image(ctx, job, spec)
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
    leftover = docker_svc.find_container(client, next_name(slug)) if client else None  # wdrożenie bez przestojów w toku
    if leftover:
        client.api.remove_container(leftover["Id"], force=True, v=delete_data)
        job.log(tr("node.app.container_removed", name=next_name(slug)))
    ports.release_spare(ctx.state, slug)
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
