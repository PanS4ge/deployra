"""Przenoszenie aplikacji na inny węzeł.

1. stary węzeł pakuje pliki, wolumeny, stack compose i konta SFTP (aplikacja stoi na czas pakowania — spójne dane),
2. panel pobiera paczkę i wysyła ją na nowy węzeł, który ją rozpakowuje,
3. zwykłe wdrożenie na nowym węźle — host proxy na bramce dostaje nowy adres; domena, DNS i certyfikat zostają,
4. ze starego węzła znika kontener (dane zostają jako kopia, chyba że zaznaczysz ich usunięcie).

Jeśli coś się nie uda przed przełączeniem ruchu, aplikacja wraca do pracy na starym węźle.
Automatycznie (gdy węzłowi kończy się RAM) przenosimy tylko aplikacje bez wolumenów i bez compose,
i tylko te, którym włączono to w ustawieniach — przerzucanie baz danych w ciemno grozi ich utratą.
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any

import httpx

from deployra_common.client import RemoteError
from deployra_common.jobs import Job, JobFailed
from deployra_common.schemas import AppSpec

from ..i18n import t
from . import alerts, deploy, proxy_state, relay, resources
from .notify import Message

COOLDOWN = 3600  # automatycznie najwyżej jedna aplikacja na godzinę z jednego węzła
_last_auto: dict[int, float] = {}


def auto_capable(spec: AppSpec) -> bool:
    """Bezstanowe: strona, aplikacja z plików albo obraz/Git bez wolumenów (pliki z data/ też jadą w paczce)."""
    if spec.endpoint != "local" or spec.source.kind == "compose":
        return False
    return not spec.volumes


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size} B"


async def _carry(source: Any, target: Any, transfer_id: str, folder: Any, job: Job) -> str:
    """Pobiera paczkę ze starego węzła i wysyła na nowy (przez dysk panelu); zwraca id paczki na nowym węźle."""
    fd, tmp = tempfile.mkstemp(suffix=".tar", dir=folder)
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            async with source.stream("GET", f"/api/transfers/{transfer_id}", timeout=httpx.Timeout(30.0, read=600.0)) as response:
                async for chunk in response.aiter_bytes(1 << 20):
                    out.write(chunk)
                    size += len(chunk)
        job.log(t("move.downloaded", size=_human(size)))
        uploaded = await target.upload_file("/api/transfers", tmp, timeout=6 * 3600)
        job.log(t("move.uploaded"))
        return uploaded["transfer"]
    finally:
        os.unlink(tmp)
        try:
            await source.delete(f"/api/transfers/{transfer_id}")
        except (RemoteError, httpx.HTTPError):
            pass


async def run(panel: Any, job: Job, app_id: int, target_id: int, delete_old: bool = False, auto: bool = False) -> dict[str, Any]:
    app = panel.db.get("apps", app_id)
    if not app:
        raise JobFailed(t("msg.no_app"))
    spec, options = deploy.load(panel, app)
    source, target = panel.node(app["node_id"]), panel.node(target_id)
    if not source or source["status"] != "paired":
        raise JobFailed(t("move.source_down"))
    if not target or target["status"] != "paired":
        raise JobFailed(t("msg.pick_paired_node"))
    if target["id"] == source["id"]:
        raise JobFailed(t("move.same_node"))
    if spec.endpoint != "local":
        raise JobFailed(t("move.ct_only"))
    problem = await resources.capacity_problem(panel, target, spec.memory_mb)
    if problem:
        raise JobFailed(problem)

    slug, spec_json = spec.slug, spec.model_dump(mode="json")
    old_port, old_status = app.get("host_port"), app["status"]
    src, dst = panel.node_client(source), panel.node_client(target)
    job.log(t("move.start", app=app["name"], source=source["name"], target=target["name"]))
    panel.db.update("apps", app_id, status="deploying", last_job=job.id, last_error=None)

    try:  # nieudane pakowanie: węzeł sam uruchamia aplikację z powrotem
        exported = await relay.start_and_follow(src, f"/api/apps/{slug}/export", {"spec": spec_json, "stop": True}, job, "  ")
    except (JobFailed, RemoteError, httpx.HTTPError) as exc:
        panel.db.update("apps", app_id, status=old_status)
        raise JobFailed(t("move.failed", error=getattr(exc, "detail", None) or exc)) from exc
    switched = False
    try:
        remote_id = await _carry(src, dst, exported["result"]["transfer"], panel.config.data_dir / "uploads", job)
        await relay.start_and_follow(dst, f"/api/apps/{slug}/import", {"transfer": remote_id}, job, "  ")
        panel.db.update("apps", app_id, node_id=target["id"], host_port=None)
        switched = True
        await deploy.run(panel, job, app_id)
    except (JobFailed, RemoteError, httpx.HTTPError, OSError, LookupError) as exc:
        reason = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
        job.log(t("move.rolling_back", node=source["name"]))
        await _rollback(panel, job, app_id, source, target, dst, src, spec, options, old_port, old_status, switched)
        panel.db.update("apps", app_id, last_error=t("move.failed", error=reason))
        raise JobFailed(t("move.failed", error=reason)) from exc

    try:
        await relay.start_and_follow(src, f"/api/apps/{slug}/remove", {"delete_data": delete_old}, job, "  ")
    except (JobFailed, RemoteError, httpx.HTTPError) as exc:
        job.log(t("move.old_cleanup_failed", node=source["name"], error=getattr(exc, "detail", None) or exc))
    panel.db.execute("UPDATE monitors SET node_id = ? WHERE kind = 'app' AND ref_id = ?", [target["id"], app_id])
    job.log(t("move.done", app=app["name"], target=target["name"]))
    text = t("move.event_auto" if auto else "move.event", app=app["name"], source=source["name"], target=target["name"])
    await alerts.event(panel, "app_moved", Message(t("alerts.title.app_moved", name=app["name"]), text, "info"))
    return {"node_id": target["id"]}


async def _rollback(panel: Any, job: Job, app_id: int, source: dict, target: dict, dst: Any, src: Any, spec: AppSpec,
                    options: dict, old_port: int | None, old_status: str, switched: bool) -> None:  # fmt: skip
    """Aplikacja wraca na stary węzeł: start kontenera, stary adres w proxy, sprzątanie kopii na nowym węźle."""
    slug = spec.slug
    if switched:
        try:
            await relay.start_and_follow(dst, f"/api/apps/{slug}/remove", {"delete_data": True}, job, "  ")
        except (JobFailed, RemoteError, httpx.HTTPError) as exc:
            job.log(t("move.target_cleanup_failed", error=getattr(exc, "detail", None) or exc))
    panel.db.update("apps", app_id, node_id=source["id"], host_port=old_port, status=old_status)
    try:
        await relay.start_and_follow(src, f"/api/apps/{slug}/power", {"kind": spec.source.kind, "running": True}, job, "  ")
    except (JobFailed, RemoteError, httpx.HTTPError) as exc:
        job.log(t("move.restart_failed", error=getattr(exc, "detail", None) or exc))
    app = panel.db.get("apps", app_id)
    if app.get("site_id") and old_port:
        site = panel.db.get("sites", app["site_id"])
        upstream = ((site or {}).get("target") or {}).get("host") if ((site or {}).get("target") or {}).get("type") == "address" else None
        deploy._upsert_site(panel, app, source, old_port, options, upstream)
        result = await proxy_state.push(panel)
        message, _ = proxy_state.push_message(result)
        job.log("  " + message)


# --- automatyczne odciążanie ---------------------------------------------------------------------


async def relieve(panel: Any, node: dict[str, Any]) -> Any:
    """Węzłowi kończy się RAM: przenosi największą aplikację z włączonym „przenoś automatycznie” na najluźniejszy węzeł."""
    if time.time() - _last_auto.get(node["id"], 0) < COOLDOWN:
        return None
    usage = (resources.LATEST.get(node["id"]) or {}).get("apps") or {}
    candidates = []
    for app in panel.db.fetchall("SELECT * FROM apps WHERE node_id = ? AND status = 'running'", [node["id"]]):
        spec, options = deploy.load(panel, app)
        if options.get("auto_move") and auto_capable(spec):
            mem = (usage.get(app["slug"]) or {}).get("mem") or 0
            candidates.append((mem, app, spec))
    if not candidates:
        return None
    mem, app, spec = max(candidates, key=lambda c: c[0])
    need = max(int(mem / 1024 / 1024), spec.memory_mb or 0, resources.MIN_FREE_MB)
    targets = await resources.better_nodes(panel, node["id"], need)
    if not targets:
        await alerts.event(panel, "app_moved", Message(t("alerts.title.app_moved", name=app["name"]),
                                                       t("move.no_target", app=app["name"], node=node["name"]), "warn"))  # fmt: skip
        _last_auto[node["id"]] = time.time()
        return None
    target = targets[0][0]
    _last_auto[node["id"]] = time.time()
    title = t("move.job_title", app=app["slug"], target=target["name"])
    return panel.jobs.start("move", title, lambda job: run(panel, job, app["id"], target["id"], auto=True))
