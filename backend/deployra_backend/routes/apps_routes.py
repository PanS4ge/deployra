"""Stacki compose, aplikacje, upload stron, konta SFTP i przenoszenie aplikacji między węzłami."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from deployra_common.i18n import tr
from deployra_common.jobs import JobFailed
from deployra_common.schemas import AppSpec, StackSpec, check_slug
from deployra_common.server import receive_verified_upload

from ..context import Context, get_ctx
from deployra_common.runtimes import KEEP_DIRS

from ..services import adopt, apps, compose, sftp_users, static_sites, transfer
from . import job_response, signed, signed_upload

router = APIRouter(prefix="/api", dependencies=[Depends(signed)])
upload_router = APIRouter(prefix="/api", dependencies=[Depends(signed_upload)])


# --- stacki --------------------------------------------------------------------------------


def _stack_target(ctx: Context, ep: str) -> tuple[object, "Path", dict[str, str]]:
    """(klient Dockera, katalog stacków, env z DOCKER_HOST) dla endpointu."""
    return ctx.docker_for(ep), compose.stacks_root(ctx.config.stacks_dir, ep), ctx.endpoints.docker_host_env(ep)


@router.get("/stacks")
def stacks(ep: str = "local", ctx: Context = Depends(get_ctx)) -> list[dict]:
    client, root, _ = _stack_target(ctx, ep)
    root.mkdir(parents=True, exist_ok=True)
    return compose.list_stacks(client, root)


@router.get("/stacks/{name}")
def stack(name: str, ep: str = "local", ctx: Context = Depends(get_ctx)) -> dict:
    _, root, _ = _stack_target(ctx, ep)
    try:
        return compose.read_stack(root, name)
    except FileNotFoundError:
        raise HTTPException(404, tr("node.stack_unmanaged")) from None


@router.put("/stacks/{name}")
async def save_stack(name: str, spec: StackSpec, pull: bool = False, ep: str = "local", ctx: Context = Depends(get_ctx)) -> dict:
    compose.check_name(name)
    compose.parse_compose(spec.compose)
    _, root, env = _stack_target(ctx, ep)

    def work(job):
        compose.write_stack(root, name, spec.compose, spec.env)
        compose.up(job, root, name, pull=pull, env=env)

    return job_response(ctx.jobs.start("stack", tr("node.job.stack_deploy", name=name), work))


@router.post("/stacks/{name}/{verb}")
async def stack_action(name: str, verb: str, ep: str = "local", ctx: Context = Depends(get_ctx)) -> dict:
    compose.check_name(name)
    _, root, env = _stack_target(ctx, ep)
    return job_response(ctx.jobs.start("stack", tr("node.job.stack_action", name=name, action=verb), lambda job: compose.action(job, root, name, verb, env)))


@router.delete("/stacks/{name}")
async def remove_stack(name: str, volumes: bool = False, ep: str = "local", ctx: Context = Depends(get_ctx)) -> dict:
    compose.check_name(name)
    _, root, env = _stack_target(ctx, ep)
    return job_response(ctx.jobs.start("stack", tr("node.job.stack_remove", name=name), lambda job: compose.remove(job, root, name, volumes, env)))


# --- aplikacje -----------------------------------------------------------------


class RemoveRequest(BaseModel):
    delete_data: bool = False


@router.get("/apps")
def list_apps(ctx: Context = Depends(get_ctx)) -> dict:
    return ctx.state.get("apps", {})


@router.post("/apps/deploy")
async def deploy(spec: AppSpec, ctx: Context = Depends(get_ctx)) -> dict:
    return job_response(ctx.jobs.start("deploy", tr("node.job.deploy", name=spec.slug), lambda job: apps.deploy(ctx, job, spec)))


@router.post("/apps/{slug}/rollback")
async def rollback(slug: str, spec: AppSpec, ctx: Context = Depends(get_ctx)) -> dict:
    check_slug(slug)
    return job_response(ctx.jobs.start("rollback", f"Rollback {slug}", lambda job: apps.rollback(ctx, job, slug, spec)))


@router.post("/apps/{slug}/remove")
async def remove_app(slug: str, body: RemoveRequest, ctx: Context = Depends(get_ctx)) -> dict:
    check_slug(slug)
    return job_response(ctx.jobs.start("remove", tr("node.job.remove", name=slug), lambda job: apps.remove(ctx, job, slug, body.delete_data)))


@upload_router.post("/apps/{slug}/upload")
async def upload_site(slug: str, request: Request, ctx: Context = Depends(get_ctx)) -> dict:
    """Wgranie strony jako ZIP (podmiana atomowa katalogu)."""
    check_slug(slug)
    max_bytes = ctx.config.max_upload_mb * 1024 * 1024
    with tempfile.TemporaryDirectory(dir=ctx.config.srv_dir) as tmp:
        archive = Path(tmp) / "upload.zip"
        await receive_verified_upload(request, str(archive), max_bytes)
        try:
            keep = KEEP_DIRS if ctx.state.get("apps", {}).get(slug, {}).get("kind") == "runtime" else ()
            count = static_sites.publish_zip(ctx.config.sites_dir, slug, archive, max_bytes * 4, keep)
        except JobFailed as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # zipfile.BadZipFile itd.
            raise HTTPException(400, tr("node.unpack_failed", error=exc)) from None
    return {"files": count}


# --- przejęcie kontenera spoza Deployry ------------------------------------------------------


class AdoptRequest(BaseModel):
    container: str
    spec: AppSpec


@router.get("/containers/{cid}/adopt-plan")
def adopt_plan(cid: str, ctx: Context = Depends(get_ctx)) -> dict:
    return adopt.plan(ctx.docker, cid)


@router.post("/apps/{slug}/adopt")
async def adopt_container(slug: str, body: AdoptRequest, ctx: Context = Depends(get_ctx)) -> dict:
    check_slug(slug)
    if body.spec.slug != slug or body.spec.endpoint != "local":
        raise HTTPException(400, tr("node.adopt.bad_spec"))
    problems = adopt.plan(ctx.docker, body.container)["problems"]
    if problems:
        raise HTTPException(400, problems[0])
    title = tr("node.job.adopt", name=slug)
    return job_response(ctx.jobs.start("deploy", title, lambda job: adopt.adopt(ctx, job, body.container, body.spec)))


# --- przenoszenie między węzłami ---------------------------------------------------------


class ExportRequest(BaseModel):
    spec: AppSpec
    stop: bool = True


class PowerRequest(BaseModel):
    kind: str
    running: bool


class ImportRequest(BaseModel):
    transfer: str


@router.post("/apps/{slug}/export")
async def export_app(slug: str, body: ExportRequest, ctx: Context = Depends(get_ctx)) -> dict:
    check_slug(slug)
    title = tr("node.job.export", name=slug)
    return job_response(ctx.jobs.start("export", title, lambda job: transfer.export(ctx, job, slug, body.spec, body.stop)))


@router.post("/apps/{slug}/power")
async def power(slug: str, body: PowerRequest, ctx: Context = Depends(get_ctx)) -> dict:
    check_slug(slug)
    title = tr("node.job.start" if body.running else "node.job.stop", name=slug)
    return job_response(ctx.jobs.start("power", title, lambda job: transfer.set_running(ctx, job, slug, body.kind, body.running)))


@router.get("/transfers/{transfer_id}")
def download_transfer(transfer_id: str, ctx: Context = Depends(get_ctx)) -> FileResponse:
    path = transfer.bundle_path(ctx.config, transfer_id)
    if not path.is_file():
        raise HTTPException(404, tr("node.transfer.missing"))
    return FileResponse(path, media_type="application/x-tar")


@router.delete("/transfers/{transfer_id}")
def delete_transfer(transfer_id: str, ctx: Context = Depends(get_ctx)) -> dict:
    transfer.remove_bundle(ctx.config, transfer_id)
    return {"ok": True}


@upload_router.post("/transfers")
async def upload_transfer(request: Request, ctx: Context = Depends(get_ctx)) -> dict:
    transfer_id = transfer.new_id()
    path = transfer.bundle_path(ctx.config, transfer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = await receive_verified_upload(request, str(path), ctx.config.max_transfer_mb * 1024 * 1024)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"transfer": transfer_id, "size": size}


@router.post("/apps/{slug}/import")
async def import_app(slug: str, body: ImportRequest, ctx: Context = Depends(get_ctx)) -> dict:
    check_slug(slug)
    transfer.bundle_path(ctx.config, body.transfer)
    loop = asyncio.get_running_loop()

    def work(job):
        result = transfer.import_bundle(ctx, job, slug, body.transfer)
        if result["sftp"]:
            from ..app import ensure_sftp

            asyncio.run_coroutine_threadsafe(ensure_sftp(ctx), loop)
        return result

    return job_response(ctx.jobs.start("import", tr("node.job.import", name=slug), work))


# --- SFTP ----------------------------------------------------------------------


class SftpUser(BaseModel):
    slug: str
    password: str | None = None
    keys: list[str] | None = None


class SftpPortConfig(BaseModel):
    port: int


@router.get("/sftp")
def list_sftp(ctx: Context = Depends(get_ctx)) -> dict:
    return {
        "enabled": ctx.config.sftp_enabled,
        "port": ctx.state.get("sftp_port") or ctx.config.sftp_port,
        "fingerprint": ctx.sftp.fingerprint() if ctx.sftp else None,
        "users": sftp_users.list_users(ctx.state),
    }


@router.post("/sftp/config")
async def update_sftp_config(body: SftpPortConfig, ctx: Context = Depends(get_ctx)) -> dict:
    if not (1 <= body.port <= 65535):
        raise HTTPException(400, "Port musi być z zakresu 1-65535")
    ctx.state.update(sftp_port=body.port)
    if ctx.sftp:
        await ctx.sftp.stop()
        ctx.sftp = None
        from ..app import ensure_sftp

        await ensure_sftp(ctx)
    return {"ok": True, "port": body.port}


@router.put("/sftp/{username}")
async def set_sftp_user(username: str, body: SftpUser, ctx: Context = Depends(get_ctx)) -> dict:
    static_sites.ensure_site_dir(ctx.config.sites_dir, body.slug)
    result = sftp_users.set_user(ctx.state, username, body.slug, body.password, body.keys)
    from ..app import ensure_sftp

    await ensure_sftp(ctx)
    return result


@router.delete("/sftp/{username}")
def delete_sftp_user(username: str, ctx: Context = Depends(get_ctx)) -> dict:
    sftp_users.delete_user(ctx.state, username)
    return {"ok": True}
