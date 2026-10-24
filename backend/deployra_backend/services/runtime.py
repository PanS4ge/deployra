"""Aplikacje „z plików”: oficjalny obraz języka + pliki z katalogu aplikacji (ZIP / SFTP).

Zależności instaluje jednorazowy kontener (`app-<slug>-install`) w tym samym katalogu, z logiem
w zadaniu wdrożenia. Kontener aplikacji dostaje katalog jako wolumen, więc PHP widzi zmiany od razu,
a pozostałe środowiska — po ponownym wdrożeniu (panel robi je sam po wgraniu ZIP-a).
"""

from __future__ import annotations

import fnmatch
import io
from pathlib import Path
from typing import Any

from deployra_common.i18n import tr
from deployra_common.jobs import Job, JobFailed
from deployra_common.runtimes import KEEP_DIRS, RUNTIMES, Runtime, env_for
from deployra_common.schemas import AppSpec, VolumeMount

from . import docker_svc, static_sites

INSTALL_TIMEOUT = 30 * 60


def runtime_of(spec: AppSpec) -> Runtime:
    return RUNTIMES[spec.source.runtime or ""]


def app_dir(cfg: Any, slug: str) -> Path:
    return static_sites.site_dir(cfg.sites_dir, slug)


def ensure_files(cfg: Any, job: Job, spec: AppSpec) -> Path:
    """Katalog aplikacji; pusty dostaje pliki startowe środowiska."""
    path = app_dir(cfg, spec.slug)
    if path.is_dir() and any(p.name not in KEEP_DIRS for p in path.iterdir()):
        return path
    path.mkdir(parents=True, exist_ok=True)
    starter = runtime_of(spec).starter
    for name, body in starter.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="\n")
    if starter:
        job.log(tr("node.runtime.starter", files=", ".join(starter)))
    return path


def check_required(runtime: Runtime, path: Path) -> None:
    if runtime.requires and not any(p.is_file() and fnmatch.fnmatch(p.name, runtime.requires) for p in path.iterdir()):
        raise JobFailed(tr("node.runtime.need_files", pattern=runtime.requires))


def ensure_image(client: Any, job: Job, image: str, refresh: bool = False) -> None:
    """Pobiera obraz tylko wtedy, gdy go nie ma — kolejne wdrożenia nie czekają na rejestr. `refresh` — zawsze od nowa
    (naprawa podatności: ten sam tag, np. php:8.3-apache, w rejestrze ma już poprawioną wersję)."""
    if not refresh:
        try:
            client.api.inspect_image(image)
            return
        except Exception:  # noqa: BLE001 — brak obrazu lokalnie
            pass
    docker_svc.pull_image(client, job, image)


def ensure_base(client: Any, job: Job, runtime: Runtime, refresh: bool = False) -> str:
    """Obraz bazowy budowany raz na węźle (np. PHP z rozszerzeniami mysqli i mod_rewrite); `refresh` buduje go od nowa
    na świeżo pobranym obrazie języka."""
    tag = runtime.run_image()
    if not refresh:
        try:
            client.api.inspect_image(tag)
            return tag
        except Exception:  # noqa: BLE001
            pass
    job.log(tr("node.runtime.building_base", image=tag))
    docker_svc.pull_image(client, job, runtime.image)  # świeży obraz języka; jego digest w etykiecie — panel sprawdzi, czy jest nowszy
    pulled = client.api.inspect_image(runtime.image).get("RepoDigests") or []
    labels = {"deployra.base": runtime.image}
    if pulled and "@" in pulled[0]:
        labels["deployra.base-digest"] = pulled[0].split("@", 1)[1]
    for event in client.api.build(fileobj=io.BytesIO(runtime.base.encode()), tag=tag, rm=True, pull=False, labels=labels, decode=True):
        if "error" in event:
            raise JobFailed(str(event["error"]).strip())
        line = str(event.get("stream") or "").rstrip()
        if line:
            job.log("  " + line)
    return tag


def _mount(cfg: Any, spec: AppSpec, runtime: Runtime) -> VolumeMount:
    # ścieżka węzła (nie użytkownika) — bez walidacji, żeby tryb deweloperski działał też na Windowsie
    return VolumeMount.model_construct(source=str(app_dir(cfg, spec.slug)), target=runtime.mount, read_only=False)


def install(ctx: Any, job: Job, spec: AppSpec, client: Any) -> None:
    runtime = runtime_of(spec)
    command = (spec.source.install_command or runtime.install).strip()
    if not command:
        return
    image = runtime.build_image or runtime.run_image()
    if runtime.build_image:
        ensure_image(client, job, runtime.build_image, refresh=spec.refresh_image)
    name = f"app-{spec.slug}-install"
    leftover = docker_svc.find_container(client, name)
    if leftover:
        client.api.remove_container(leftover["Id"], force=True)
    network = ctx.config.docker_network
    docker_svc.ensure_network(client, network)
    mount = _mount(ctx.config, spec, runtime)
    host_config = client.api.create_host_config(
        binds={mount.source: {"bind": mount.target, "mode": "rw"}},
        network_mode=network,
        log_config={"Type": "json-file", "Config": {"max-size": "10m", "max-file": "1"}},
    )
    job.log(tr("node.runtime.installing", image=image))
    job.log(f"$ {command}")
    # instalacja dostaje wszystkie zmienne (build i both — jako zmienne budowania, runtime — jak dotąd)
    cid = client.api.create_container(
        image, command=["sh", "-c", command], name=name, environment=env_for(runtime, spec.container_port, spec.env),
        working_dir=runtime.mount, labels={docker_svc.SYSTEM_LABEL: "install", docker_svc.APP_LABEL: spec.slug}, host_config=host_config,
    )["Id"]  # fmt: skip
    try:
        client.api.start(cid)
        pending = ""
        for chunk in client.api.logs(cid, stream=True, follow=True):
            pending += chunk.decode(errors="replace") if isinstance(chunk, bytes) else str(chunk)
            *lines, pending = pending.split("\n")
            for line in lines:
                job.log("  " + line.rstrip("\r"))
        if pending.strip():
            job.log("  " + pending.rstrip())
        code = client.api.wait(cid, timeout=INSTALL_TIMEOUT).get("StatusCode", 1)
    finally:
        client.api.remove_container(cid, force=True)
    if code != 0:
        raise JobFailed(tr("node.runtime.install_failed", code=code))


def prepare(ctx: Any, job: Job, spec: AppSpec, client: Any) -> str:
    """Pliki, obraz i zależności — zwraca obraz, z którego wystartuje aplikacja."""
    runtime = runtime_of(spec)
    path = ensure_files(ctx.config, job, spec)
    check_required(runtime, path)
    if runtime.base:
        image = ensure_base(client, job, runtime, refresh=spec.refresh_image)
    else:
        image = runtime.image
        ensure_image(client, job, image, refresh=spec.refresh_image)
    install(ctx, job, spec, client)
    return image


def container_extras(cfg: Any, spec: AppSpec) -> dict[str, Any]:
    """Polecenie, katalog roboczy, zmienne i wolumeny kontenera aplikacji."""
    runtime = runtime_of(spec)
    return {
        "command": ["sh", "-c", (spec.source.start_command or runtime.start).strip()],
        "working_dir": runtime.mount,
        "env": env_for(runtime, spec.container_port, spec.runtime_env()),
        "volumes": [_mount(cfg, spec, runtime), *spec.volumes],
    }
