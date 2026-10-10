"""Aplikacje FastAPI bramki: publiczny bootstrap i API dla panelu (tylko przez tunel)."""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from loguru import logger
from pydantic import BaseModel, Field

from deployra_common.jobs import Job
from deployra_common.schemas import CertRequest, CertUpload, ProxyState

from . import NAME, __version__, certs
from .context import Context, get_ctx

RENEW_INTERVAL = 12 * 3600


def _errors(app: FastAPI) -> None:
    @app.exception_handler(ValueError)
    async def _value(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PermissionError)
    async def _perm(_: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(LookupError)
    async def _lookup(_: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)


# --- bootstrap (publiczny, port 51821) ------------------------------------------------


def create_bootstrap_app(ctx: Context) -> FastAPI:
    app = FastAPI(title="Deployra-WG bootstrap", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    _errors(app)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "service": NAME, "version": __version__, "paired": bool(ctx.state.panel_secret)}

    @app.post("/pair")
    def pair(body: dict = Body(...)) -> dict:
        return ctx.pair_panel(body)

    @app.post("/join")
    def join(body: dict = Body(...)) -> dict:
        return ctx.join_node(body)

    return app


# --- API dla panelu (podpisane, przez tunel) ----------------------------------------------


async def _signed(request: Request) -> str:
    return await request.app.state.ctx.guard(request)


class TokenRequest(BaseModel):
    id: str
    join_secret: str
    name: str = ""
    lan_subnets: list[str] = Field(default_factory=list)
    ttl_minutes: int = Field(default=60, ge=5, le=7 * 24 * 60)


class PeerUpdate(BaseModel):
    lan_subnets: list[str]


class CheckRequest(BaseModel):
    url: str
    host: str | None = None


def _job_response(job: Job) -> dict:
    return {"job": job.snapshot()}


def _cert_job(ctx: Context, req: CertRequest):
    def work(job: Job) -> dict:
        certs.run_lego(job, ctx.config, req, "run")
        certs.remember(ctx.state, req)
        job.log("Certyfikat zainstalowany — przeładowuję nginx")
        result = ctx.apply_proxy()
        job.log(f"nginx: {result.status} {result.message}".rstrip())
        return certs.read_info(ctx.config.certs_dir, req.id) or {}

    return work


def create_api_app(ctx: Context, *, start_scheduler: bool = True) -> FastAPI:
    async def renew_loop() -> None:
        await asyncio.sleep(300)
        while True:
            try:
                await renew_due(ctx)
            except Exception as exc:  # noqa: BLE001 — pętla odnowień nie może paść
                logger.error(f"Odnawianie certyfikatów: {exc}")
            await asyncio.sleep(RENEW_INTERVAL)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(renew_loop()) if start_scheduler else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Deployra-WG API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx
    _errors(app)
    router = APIRouter(prefix="/api", dependencies=[Depends(_signed)])

    @router.get("/status")
    def status(ctx: Context = Depends(get_ctx)) -> dict:
        tokens = ctx.state.get("join_tokens", {})
        return {
            **ctx.public_info(),
            "service": NAME,
            "wg_up": ctx.hub.control.is_up() if ctx.config.wg_enabled else False,
            "peers": ctx.hub.status(),
            "tokens": [
                {"id": tid, "name": t["name"], "expires": t["expires"], "used": bool(t["used_by"])}
                for tid, t in tokens.items()
            ],
            "nginx": ctx.last_apply,
            "certs": certs_list(ctx),
        }

    @router.post("/join-tokens")
    def add_token(body: TokenRequest, ctx: Context = Depends(get_ctx)) -> dict:
        return ctx.register_token(body.id, body.join_secret, body.name, body.lan_subnets, body.ttl_minutes)

    @router.delete("/join-tokens/{token_id}")
    def drop_token(token_id: str, ctx: Context = Depends(get_ctx)) -> dict:
        ctx.drop_token(token_id)
        return {"ok": True}

    @router.patch("/peers/{peer_id}")
    def update_peer(peer_id: str, body: PeerUpdate, ctx: Context = Depends(get_ctx)) -> dict:
        try:
            ctx.hub.update_subnets(peer_id, body.lan_subnets)
        except KeyError:
            raise HTTPException(404, "nie ma takiego peera") from None
        return {"ok": True}

    @router.delete("/peers/{peer_id}")
    def remove_peer(peer_id: str, ctx: Context = Depends(get_ctx)) -> dict:
        if peer_id == "panel":
            raise HTTPException(400, "nie można usunąć panelu z tunelu")
        ctx.hub.remove_peer(peer_id)
        ctx.drop_token(peer_id)
        return {"ok": True}

    @router.put("/proxy")
    def apply_proxy(body: ProxyState, ctx: Context = Depends(get_ctx)) -> dict:
        result = ctx.apply_proxy(body)
        return {"ok": result.ok, "status": result.status, "message": result.message}

    @router.get("/certs")
    def list_certs(ctx: Context = Depends(get_ctx)) -> list[dict]:
        return certs_list(ctx)

    @router.post("/certs/issue")
    async def issue(body: CertRequest, ctx: Context = Depends(get_ctx)) -> dict:
        return _job_response(ctx.jobs.start("cert", f"Certyfikat {', '.join(body.domains)}", _cert_job(ctx, body)))

    @router.post("/certs/upload")
    def upload(body: CertUpload, ctx: Context = Depends(get_ctx)) -> dict:
        info = certs.install_upload(ctx.config.certs_dir, body)
        result = ctx.apply_proxy()
        return {"cert": info, "nginx": {"ok": result.ok, "status": result.status, "message": result.message}}

    @router.delete("/certs/{cert_id}")
    def delete_cert(cert_id: int, ctx: Context = Depends(get_ctx)) -> dict:
        certs.forget(ctx.state, ctx.config.certs_dir, cert_id)
        ctx.apply_proxy()
        return {"ok": True}

    @router.post("/check")
    async def check(body: CheckRequest) -> dict:
        if not body.url.startswith(("http://", "https://")):
            raise HTTPException(400, "adres musi zaczynać się od http:// lub https://")
        started = time.perf_counter()
        headers = {"Host": body.host} if body.host else {}
        try:
            async with httpx.AsyncClient(timeout=10, verify=False, follow_redirects=False) as client:
                response = await client.get(body.url, headers=headers)
            return {"ok": response.status_code < 500, "status": response.status_code, "ms": round((time.perf_counter() - started) * 1000)}
        except httpx.HTTPError as exc:
            return {"ok": False, "status": None, "error": str(exc) or type(exc).__name__}

    def _job(job_id: str, ctx: Context = Depends(get_ctx)) -> Job:
        found = ctx.jobs.get(job_id)
        if not found:
            raise HTTPException(404, "nie ma takiego zadania")
        return found

    @router.get("/jobs/{job_id}")
    def job(found: Job = Depends(_job)) -> dict:
        return {**_job_response(found), "lines": found.lines[-500:]}

    @router.get("/jobs/{job_id}/stream", response_class=EventSourceResponse)
    async def job_stream(found: Job = Depends(_job), start: int = 0):
        async for kind, payload in found.follow(start):
            yield ServerSentEvent(raw_data=payload, event="line") if kind == "line" else ServerSentEvent(data=payload, event="end")

    app.include_router(router)
    return app


def certs_list(ctx: Context) -> list[dict]:
    known = ctx.state.get("certs", {})
    out = []
    if ctx.config.certs_dir.exists():
        for path in sorted(ctx.config.certs_dir.iterdir(), key=lambda p: p.name):
            if path.name.isdigit():
                info = certs.read_info(ctx.config.certs_dir, int(path.name))
                if info:
                    meta = known.get(path.name, {})
                    info.update(challenge=meta.get("challenge", "custom"), managed=bool(meta))
                    out.append(info)
    return out


async def renew_due(ctx: Context) -> None:
    for cert_id, meta in ctx.state.get("certs", {}).items():
        info = certs.read_info(ctx.config.certs_dir, int(cert_id))
        if info and info["days_left"] > certs.RENEW_DAYS:
            continue
        req = CertRequest.model_validate(meta)

        def work(job: Job, req: CertRequest = req) -> bool:
            changed = certs.run_lego(job, ctx.config, req, "renew")
            if changed:
                ctx.apply_proxy()
                job.log("Odnowiono i przeładowano nginx")
            return changed

        job = ctx.jobs.start("renew", f"Odnowienie {', '.join(req.domains)}", work)
        async for _ in job.follow():
            pass
        if job.status == "error":
            logger.error(f"Nie udało się odnowić certyfikatu {cert_id}: {job.error}")
