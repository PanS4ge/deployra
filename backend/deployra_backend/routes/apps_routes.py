"""Stacki compose, aplikacje, upload stron i konta SFTP."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from deployra_common.jobs import JobFailed
from deployra_common.schemas import AppSpec, StackSpec, check_slug
from deployra_common.server import receive_verified_upload

from ..context import Context, get_ctx
from ..services import apps, compose, sftp_users, static_sites
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
        raise HTTPException(404, "ten stack nie jest zarządzany przez Deployrę (brak pliku compose)") from None


@router.put("/stacks/{name}")
async def save_stack(name: str, spec: StackSpec, pull: bool = False, ep: str = "local", ctx: Context = Depends(get_ctx)) -> dict:
    compose.check_name(name)
    compose.parse_compose(spec.compose)
    _, root, env = _stack_target(ctx, ep)

    def work(job):
        compose.write_stack(root, name, spec.compose, spec.env)
        compose.up(job, root, name, pull=pull, env=env)

    return job_response(ctx.jobs.start("stack", f"Stack {name}: wdrożenie", work))


@router.post("/stacks/{name}/{verb}")
async def stack_action(name: str, verb: str, ep: str = "local", ctx: Context = Depends(get_ctx)) -> dict:
    compose.check_name(name)
    _, root, env = _stack_target(ctx, ep)
    return job_response(ctx.jobs.start("stack", f"Stack {name}: {verb}", lambda job: compose.action(job, root, name, verb, env)))


@router.delete("/stacks/{name}")
async def remove_stack(name: str, volumes: bool = False, ep: str = "local", ctx: Context = Depends(get_ctx)) -> dict:
    compose.check_name(name)
    _, root, env = _stack_target(ctx, ep)
    return job_response(ctx.jobs.start("stack", f"Stack {name}: usuwanie", lambda job: compose.remove(job, root, name, volumes, env)))


# --- aplikacje -----------------------------------------------------------------


class RemoveRequest(BaseModel):
    delete_data: bool = False


@router.get("/apps")
def list_apps(ctx: Context = Depends(get_ctx)) -> dict:
    return ctx.state.get("apps", {})


@router.post("/apps/deploy")
async def deploy(spec: AppSpec, ctx: Context = Depends(get_ctx)) -> dict:
    return job_response(ctx.jobs.start("deploy", f"Wdrożenie {spec.slug}", lambda job: apps.deploy(ctx, job, spec)))


@router.post("/apps/{slug}/rollback")
async def rollback(slug: str, spec: AppSpec, ctx: Context = Depends(get_ctx)) -> dict:
    check_slug(slug)
    return job_response(ctx.jobs.start("rollback", f"Rollback {slug}", lambda job: apps.rollback(ctx, job, slug, spec)))


@router.post("/apps/{slug}/remove")
async def remove_app(slug: str, body: RemoveRequest, ctx: Context = Depends(get_ctx)) -> dict:
    check_slug(slug)
    return job_response(ctx.jobs.start("remove", f"Usuwanie {slug}", lambda job: apps.remove(ctx, job, slug, body.delete_data)))


@upload_router.post("/apps/{slug}/upload")
async def upload_site(slug: str, request: Request, ctx: Context = Depends(get_ctx)) -> dict:
    """Wgranie strony jako ZIP (podmiana atomowa katalogu)."""
    check_slug(slug)
    max_bytes = ctx.config.max_upload_mb * 1024 * 1024
    with tempfile.TemporaryDirectory(dir=ctx.config.srv_dir) as tmp:
        archive = Path(tmp) / "upload.zip"
        await receive_verified_upload(request, str(archive), max_bytes)
        try:
            count = static_sites.publish_zip(ctx.config.sites_dir, slug, archive, max_bytes * 4)
        except JobFailed as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # zipfile.BadZipFile itd.
            raise HTTPException(400, f"nie udało się rozpakować archiwum: {exc}") from None
    return {"files": count}


# --- SFTP ----------------------------------------------------------------------


class SftpUser(BaseModel):
    slug: str
    password: str | None = None
    keys: list[str] | None = None


@router.get("/sftp")
def sftp_users(ctx: Context = Depends(get_ctx)) -> dict:
    return {
        "enabled": ctx.config.sftp_enabled,
        "port": ctx.config.sftp_port,
        "fingerprint": ctx.sftp.fingerprint() if ctx.sftp else None,
        "users": sftp_users.list_users(ctx.state),
    }


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
