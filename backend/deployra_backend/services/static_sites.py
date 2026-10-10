"""Strony statyczne: jeden współdzielony `nginx:alpine` na węzeł serwuje wszystkie strony po nagłówku Host."""

from __future__ import annotations

import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from deployra_common.jobs import Job, JobFailed
from deployra_common.schemas import check_domain, check_slug

from . import docker_svc, ports

CONTAINER = "deployra-static"
STATIC_KEY = "__static__"
MAX_FILES = 50_000

PLACEHOLDER = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{slug} — gotowe</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; font-family:system-ui,sans-serif;
         color:#e6e8ff; background:radial-gradient(ellipse at 20% 10%,#2a1b5c 0,transparent 55%),
         radial-gradient(ellipse at 80% 90%,#0e3a4f 0,transparent 50%),#05060f; }}
  main {{ text-align:center; padding:2rem; }}
  h1 {{ font-size:clamp(1.6rem,4vw,2.6rem); margin:0 0 .5rem; }}
  code {{ color:#22d3ee; }}
  footer {{ position:fixed; bottom:1rem; width:100%; text-align:center; color:#8b90b8; font-size:.85rem; }}
</style>
</head>
<body>
<main>
  <h1>Twoja strona jest gotowa 🚀</h1>
  <p>Wgraj pliki przez SFTP albo panel. Identyfikator strony: <code>{slug}</code></p>
</main>
<footer>Powered by Deployra</footer>
</body>
</html>
"""

DEFAULT_CONF = """# Deployra — domyślny serwer (nieznany host)
server {
    listen 80 default_server;
    server_name _;
    return 404;
}
"""


def site_dir(sites_dir: Path, slug: str) -> Path:
    return sites_dir / check_slug(slug)


def ensure_site_dir(sites_dir: Path, slug: str) -> Path:
    path = site_dir(sites_dir, slug)
    if not path.exists():
        path.mkdir(parents=True)
        (path / "index.html").write_text(PLACEHOLDER.format(slug=slug), encoding="utf-8")
    return path


def _safe_members(archive: zipfile.ZipFile, max_total: int) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    infos = archive.infolist()
    if len(infos) > MAX_FILES:
        raise JobFailed(f"za dużo plików w archiwum (> {MAX_FILES})")
    total, members = 0, []
    for info in infos:
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise JobFailed(f"archiwum zawiera dowiązanie symboliczne: {info.filename}")
        name = PurePosixPath(info.filename.replace("\\", "/"))
        if name.is_absolute() or ".." in name.parts or (name.parts and ":" in name.parts[0]):
            raise JobFailed(f"niebezpieczna ścieżka w archiwum: {info.filename}")
        total += info.file_size
        if total > max_total:
            raise JobFailed("archiwum po rozpakowaniu jest za duże")
        members.append((info, name))
    return members


def extract_zip(zip_path: Path, dest: Path, max_total: int) -> int:
    """Bezpieczne rozpakowanie; jeśli całość siedzi w jednym katalogu, spłaszcza go."""
    with zipfile.ZipFile(zip_path) as archive:
        members = _safe_members(archive, max_total)
        files = [(i, n) for i, n in members if not i.is_dir()]
        tops = {n.parts[0] for _, n in files if n.parts}
        strip = len(tops) == 1 and all(len(n.parts) > 1 for _, n in files)
        count = 0
        for info, name in files:
            parts = name.parts[1:] if strip else name.parts
            if not parts:
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            count += 1
    return count


def swap_dir(target: Path, new: Path) -> None:
    """Podmienia katalog strony na nowy (stary usuwany po zamianie)."""
    old = target.with_name(f".old-{target.name}-{uuid.uuid4().hex[:6]}")
    if target.exists():
        target.rename(old)
    new.rename(target)
    shutil.rmtree(old, ignore_errors=True)


def publish_zip(sites_dir: Path, slug: str, zip_path: Path, max_total: int) -> int:
    tmp = sites_dir / f".new-{slug}-{uuid.uuid4().hex[:6]}"
    tmp.mkdir(parents=True)
    try:
        count = extract_zip(zip_path, tmp, max_total)
        if count == 0:
            raise JobFailed("archiwum jest puste")
        swap_dir(site_dir(sites_dir, slug), tmp)
        return count
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def publish_dir(sites_dir: Path, slug: str, source: Path) -> None:
    tmp = sites_dir / f".new-{slug}-{uuid.uuid4().hex[:6]}"
    shutil.copytree(source, tmp, symlinks=False, ignore=shutil.ignore_patterns(".git"))
    swap_dir(site_dir(sites_dir, slug), tmp)


def render_site_conf(slug: str, domains: list[str]) -> str:
    names = " ".join([check_domain(d) for d in domains] + [f"{check_slug(slug)}.deployra.internal"])
    return f"""# Deployra — strona statyczna {slug}
server {{
    listen 80;
    server_name {names};
    root /srv/sites/{slug};
    index index.html index.htm;
    charset utf-8;
    location / {{
        try_files $uri $uri/ $uri.html =404;
    }}
    location ~ /\\. {{
        deny all;
    }}
}}
"""


def _exec(client: Any, cid: str, cmd: list[str]) -> tuple[int, str]:
    exec_id = client.api.exec_create(cid, cmd)["Id"]
    output = client.api.exec_start(exec_id)
    code = client.api.exec_inspect(exec_id).get("ExitCode", 1)
    return code, output.decode(errors="replace") if isinstance(output, bytes) else str(output)


def ensure_container(ctx: Any, job: Job | None = None) -> int:
    """Uruchamia współdzielony kontener nginx (jeśli go nie ma) i zwraca jego port."""
    cfg, client = ctx.config, ctx.docker
    port = ports.allocate(ctx.state, client, STATIC_KEY, cfg.port_pool, ctx.state.get("static_port"))
    ctx.state.update(static_port=port)
    conf_dir = cfg.static_conf_dir
    conf_dir.mkdir(parents=True, exist_ok=True)
    default = conf_dir / "00-default.conf"
    if not default.exists():
        default.write_text(DEFAULT_CONF, encoding="utf-8")
    existing = docker_svc.find_container(client, CONTAINER)
    if existing:
        if existing.get("State") != "running":
            client.api.start(existing["Id"])
        return port
    if job:
        job.log(f"Uruchamiam współdzielony serwer stron statycznych ({cfg.static_image}) na porcie {port}")
    try:
        client.api.inspect_image(cfg.static_image)
    except Exception:  # noqa: BLE001 — brak obrazu lokalnie
        repo, _, tag = cfg.static_image.partition(":")
        client.api.pull(repo, tag=tag or "latest")
    from deployra_common.schemas import ContainerSpec, PortMap, VolumeMount

    docker_svc.run_container(
        client,
        ContainerSpec(
            name=CONTAINER,
            image=cfg.static_image,
            ports=[PortMap(container=80, host=port)],
            volumes=[
                VolumeMount(source=str(cfg.sites_dir), target="/srv/sites", read_only=True),
                VolumeMount(source=str(conf_dir), target="/etc/nginx/conf.d", read_only=True),
            ],
            labels={docker_svc.SYSTEM_LABEL: "static"},
            network=cfg.docker_network,
            memory_mb=64,
            pull=False,
        ),
    )
    time.sleep(1)
    return port


def apply_site(ctx: Any, slug: str, domains: list[str], job: Job | None = None) -> int:
    """Zapisuje konfigurację strony, testuje `nginx -t`, przeładowuje; przy błędzie przywraca poprzednią."""
    port = ensure_container(ctx, job)
    conf_path = ctx.config.static_conf_dir / f"site-{check_slug(slug)}.conf"
    previous = conf_path.read_text(encoding="utf-8") if conf_path.exists() else None
    conf_path.write_text(render_site_conf(slug, domains), encoding="utf-8")
    reload_or_rollback(ctx, conf_path, previous)
    return port


def remove_site(ctx: Any, slug: str, delete_files: bool) -> None:
    conf_path = ctx.config.static_conf_dir / f"site-{check_slug(slug)}.conf"
    if conf_path.exists():
        previous = conf_path.read_text(encoding="utf-8")
        conf_path.unlink()
        reload_or_rollback(ctx, conf_path, previous)
    if delete_files:
        shutil.rmtree(site_dir(ctx.config.sites_dir, slug), ignore_errors=True)


def reload_or_rollback(ctx: Any, conf_path: Path, previous: str | None) -> None:
    container = docker_svc.find_container(ctx.docker, CONTAINER)
    if not container:
        return
    code, output = _exec(ctx.docker, container["Id"], ["nginx", "-t"])
    if code != 0:
        if previous is None:
            conf_path.unlink(missing_ok=True)
        else:
            conf_path.write_text(previous, encoding="utf-8")
        raise JobFailed(f"nginx odrzucił konfigurację strony:\n{output.strip()}")
    _exec(ctx.docker, container["Id"], ["nginx", "-s", "reload"])
