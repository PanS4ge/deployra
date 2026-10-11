"""Przenoszenie aplikacji między węzłami: paczka z plikami, stackiem compose, wolumenami i kontami SFTP.

Paczka to zwykły tar w `/srv/deployra/transfer/<id>.tar`, który panel pobiera ze starego węzła
i wysyła na nowy. Wolumeny pakuje i rozpakowuje jednorazowy kontener (`alpine` + tar), bo backend
nie widzi /var/lib/docker — katalog transferu ma tę samą ścieżkę na hoście i w kontenerze backendu.
Ścieżki hosta montowane w aplikacji (np. /mnt/media) nie są przenoszone — trafiają do logu.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any

from deployra_common.i18n import tr
from deployra_common.jobs import Job, JobFailed
from deployra_common.schemas import AppSpec, check_slug

from . import apps, compose, docker_svc, runtime, static_sites
from .vitals import format_bytes

HELPER_IMAGE = "alpine:3.20"
SKIP_FILES = (".deployra", "node_modules")  # odbudowywane przy wdrożeniu na nowym węźle
_ID = re.compile(r"^[0-9a-f]{16}$")


def transfer_dir(cfg: Any) -> Path:
    return cfg.srv_dir / "transfer"


def bundle_path(cfg: Any, transfer_id: str) -> Path:
    if not _ID.match(transfer_id):
        raise ValueError(tr("node.transfer.bad_id"))
    return transfer_dir(cfg) / f"{transfer_id}.tar"


def new_id() -> str:
    return secrets.token_hex(8)


def _helper(ctx: Any, job: Job, binds: dict[str, dict[str, str]], command: list[str]) -> None:
    client = ctx.docker
    runtime.ensure_image(client, job, HELPER_IMAGE)
    host_config = client.api.create_host_config(binds=binds, network_mode="none")
    cid = client.api.create_container(HELPER_IMAGE, command=command, labels={docker_svc.SYSTEM_LABEL: "transfer"},
                                      host_config=host_config)["Id"]  # fmt: skip
    try:
        client.api.start(cid)
        code = client.api.wait(cid, timeout=6 * 3600).get("StatusCode", 1)
        if code != 0:
            logs = client.api.logs(cid, tail=20)
            text = logs.decode(errors="replace") if isinstance(logs, bytes) else str(logs)
            raise JobFailed(tr("node.transfer.helper_failed", code=code, logs=text.strip()))
    finally:
        client.api.remove_container(cid, force=True)


def _tar_dir(source: Path, target: Path, skip: tuple[str, ...] = ()) -> None:
    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = info.name.split("/")
        return None if len(parts) > 1 and parts[1] in skip else info

    with tarfile.open(target, "w:gz") as tar:
        tar.add(source, arcname=".", filter=keep)


def _untar(archive: Path, target: Path) -> None:
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="data")


def _set_aside(path: Path, job: Job) -> None:
    """Coś już tu leży (np. stara kopia po wcześniejszych przenosinach) — odkładamy zamiast nadpisywać."""
    if path.exists():
        backup = path.with_name(f".bak-{path.name}-{int(time.time())}")
        path.rename(backup)
        job.log(tr("node.transfer.set_aside", path=str(path), backup=backup.name))


def app_volumes(ctx: Any, slug: str, spec: AppSpec) -> tuple[list[dict[str, Any]], list[str]]:
    """(wolumeny Dockera do przeniesienia, pominięte ścieżki hosta)."""
    if spec.source.kind == "compose":
        found = ctx.docker.api.volumes(filters={"label": f"com.docker.compose.project={slug}"}).get("Volumes") or []
        return [{"name": v["Name"], "labels": v.get("Labels") or {}} for v in found], []
    named = [v.source for v in spec.volumes if not v.source.startswith("/")]
    skipped = [v.source for v in spec.volumes if v.source.startswith("/")]
    return [{"name": name, "labels": {}} for name in dict.fromkeys(named)], skipped


def set_running(ctx: Any, job: Job, slug: str, kind: str, running: bool) -> None:
    """Zatrzymanie aplikacji na czas pakowania (spójne dane) i ponowne uruchomienie, gdy coś pójdzie nie tak."""
    check_slug(slug)
    if kind == "compose":
        root = compose.stacks_root(ctx.config.stacks_dir, "local")
        if (root / slug / compose.COMPOSE_FILE).exists():
            compose.action(job, root, slug, "start" if running else "stop")
        return
    existing = docker_svc.find_container(ctx.docker, apps.container_name(slug))
    if existing:
        (ctx.docker.api.start if running else ctx.docker.api.stop)(existing["Id"])
        job.log(tr("node.transfer.started" if running else "node.transfer.stopped", name=apps.container_name(slug)))


def export(ctx: Any, job: Job, slug: str, spec: AppSpec, stop: bool) -> dict[str, Any]:
    cfg, kind = ctx.config, spec.source.kind
    check_slug(slug)
    transfer_id = new_id()
    root = transfer_dir(cfg)
    work = root / f".out-{transfer_id}"
    (work / "volumes").mkdir(parents=True)
    stopped = False
    try:
        if stop and kind != "static":
            set_running(ctx, job, slug, kind, False)
            stopped = True
        manifest: dict[str, Any] = {"version": 1, "slug": slug, "kind": kind, "files": False, "stack": False, "volumes": [], "sftp": {}}
        site = static_sites.site_dir(cfg.sites_dir, slug)
        if (kind in ("static", "runtime") or (kind == "git" and spec.source.git_static_dir)) and site.is_dir():
            job.log(tr("node.transfer.packing_files"))
            _tar_dir(site, work / "files.tar.gz", SKIP_FILES if kind == "runtime" else ())
            manifest["files"] = True
        stack = compose.stacks_root(cfg.stacks_dir, "local") / slug
        if kind == "compose" and stack.is_dir():
            _tar_dir(stack, work / "stack.tar.gz")
            manifest["stack"] = True
        volumes, skipped = app_volumes(ctx, slug, spec)
        for volume in volumes:
            job.log(tr("node.transfer.packing_volume", name=volume["name"]))
            _helper(ctx, job, {volume["name"]: {"bind": "/v", "mode": "ro"}, str(work / "volumes"): {"bind": "/out", "mode": "rw"}},
                    ["tar", "czf", f"/out/{volume['name']}.tar.gz", "-C", "/v", "."])  # fmt: skip
            manifest["volumes"].append(volume)
        for path in skipped:
            job.log(tr("node.transfer.skipped_bind", path=path))
        manifest["sftp"] = {name: info for name, info in (ctx.state.get("sftp_users") or {}).items() if info.get("slug") == slug}
        (work / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        bundle = root / f"{transfer_id}.tar"
        with tarfile.open(bundle, "w") as tar:
            tar.add(work, arcname=".")
        size = bundle.stat().st_size
        job.log(tr("node.transfer.packed", size=format_bytes(size)))
        return {"transfer": transfer_id, "size": size, "volumes": [v["name"] for v in volumes], "skipped": skipped}
    except BaseException:
        if stopped:
            set_running(ctx, job, slug, kind, True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def import_bundle(ctx: Any, job: Job, slug: str, transfer_id: str) -> dict[str, Any]:
    cfg = ctx.config
    check_slug(slug)
    bundle = bundle_path(cfg, transfer_id)
    if not bundle.is_file():
        raise JobFailed(tr("node.transfer.missing"))
    free = shutil.disk_usage(cfg.srv_dir).free
    if free < bundle.stat().st_size * 2:
        raise JobFailed(tr("node.transfer.no_space", free=format_bytes(free), need=format_bytes(bundle.stat().st_size * 2)))
    work = transfer_dir(cfg) / f".in-{transfer_id}"
    try:
        _untar(bundle, work)
        manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("slug") != slug:
            raise JobFailed(tr("node.transfer.wrong_app", slug=manifest.get("slug")))
        if manifest.get("files"):
            job.log(tr("node.transfer.unpacking_files"))
            site = static_sites.site_dir(cfg.sites_dir, slug)
            _set_aside(site, job)
            site.mkdir(parents=True)
            _untar(work / "files.tar.gz", site)
        if manifest.get("stack"):
            stack = compose.stacks_root(cfg.stacks_dir, "local") / slug
            _set_aside(stack, job)
            stack.mkdir(parents=True)
            _untar(work / "stack.tar.gz", stack)
        client = ctx.docker
        for volume in manifest.get("volumes") or []:
            name = volume["name"]
            try:
                client.api.inspect_volume(name)
                job.log(tr("node.transfer.volume_overwrite", name=name))
            except Exception:  # noqa: BLE001 — wolumenu nie ma, tworzymy (z etykietami compose, żeby compose go przyjął)
                client.api.create_volume(name, labels=volume.get("labels") or None)
            job.log(tr("node.transfer.unpacking_volume", name=name))
            _helper(ctx, job, {name: {"bind": "/v", "mode": "rw"}, str(work / "volumes"): {"bind": "/in", "mode": "ro"}},
                    ["sh", "-c", f"find /v -mindepth 1 -delete && tar xzf /in/{name}.tar.gz -C /v"])  # fmt: skip
        imported = _import_sftp(ctx, job, manifest.get("sftp") or {}, slug)
        return {"files": bool(manifest.get("files")), "volumes": [v["name"] for v in manifest.get("volumes") or []], "sftp": imported}
    finally:
        shutil.rmtree(work, ignore_errors=True)
        bundle.unlink(missing_ok=True)


def _import_sftp(ctx: Any, job: Job, users: dict[str, dict], slug: str) -> list[str]:
    imported: list[str] = []

    def change(current: dict) -> dict:
        for name, info in users.items():
            if name in current and current[name].get("slug") != slug:
                job.log(tr("node.transfer.sftp_taken", name=name))
                continue
            current[name] = {"slug": slug, "hash": info.get("hash"), "keys": info.get("keys") or []}
            imported.append(name)
        return current

    if users:
        ctx.state.mutate("sftp_users", change)
    return imported


def remove_bundle(cfg: Any, transfer_id: str) -> None:
    bundle_path(cfg, transfer_id).unlink(missing_ok=True)


def cleanup(cfg: Any, max_age: float = 24 * 3600) -> None:
    """Paczki, których panel nie odebrał (np. przerwane przenosiny)."""
    root = transfer_dir(cfg)
    if not root.is_dir():
        return
    for path in root.iterdir():
        if time.time() - path.stat().st_mtime > max_age:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
