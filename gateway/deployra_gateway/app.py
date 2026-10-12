"""Aplikacje FastAPI bramki: publiczny bootstrap i API dla panelu (tylko przez tunel)."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from loguru import logger
from pydantic import BaseModel, Field

from deployra_common import i18n
from deployra_common.i18n import tr
from deployra_common.jobs import Job
from deployra_common.schemas import CertRequest, CertUpload, ProxyState

from . import NAME, __version__, certs
from .context import Context, get_ctx

RENEW_INTERVAL = 12 * 3600
SECURITY_TICK = 10
GEOIP_CHECK = 24 * 3600


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
    i18n.language_middleware(app)
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
    kind: str = "node"


class RoutesRequest(BaseModel):
    subnets: list[str] = Field(default_factory=list)


class PeerUpdate(BaseModel):
    lan_subnets: list[str]


class TunnelToken(BaseModel):
    token: str | None = None


class UpdateRequest(BaseModel):
    version: str


class BanRequest(BaseModel):
    target: str
    minutes: int | None = Field(default=None, ge=1, le=525_600)
    reason: str = ""


class UnbanRequest(BaseModel):
    target: str


class BanSettings(BaseModel):
    auto: bool = False
    rate: int = 600
    minutes: int = 60
    allowlist: list[str] = Field(default_factory=list)


class CheckRequest(BaseModel):
    url: str
    host: str | None = None


def _job_response(job: Job) -> dict:
    return {"job": job.snapshot()}


def _cert_job(ctx: Context, req: CertRequest):
    def work(job: Job) -> dict:
        certs.run_lego(job, ctx.config, req, "run")
        certs.remember(ctx.state, req)
        job.log(tr("gw.cert_installed"))
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
                logger.error(tr("gw.log.renew_error", error=exc))
            await asyncio.sleep(RENEW_INTERVAL)

    async def security_loop() -> None:
        """Wygasanie banów i auto-ban adresów, które zalewają bramkę zapytaniami."""
        while True:
            await asyncio.sleep(SECURITY_TICK)
            try:
                if await asyncio.to_thread(security_tick, ctx):
                    await asyncio.to_thread(ctx.apply_bans)
            except Exception as exc:  # noqa: BLE001 — pętla nie może paść
                logger.error(tr("gw.log.security_error", error=exc))

    async def geoip_loop() -> None:
        await asyncio.sleep(20)
        while True:
            if ctx.geoip.enabled and ctx.geoip.stale():
                try:
                    await ctx.geoip.refresh()
                except Exception as exc:  # noqa: BLE001 — brak sieci nie może wywrócić bramki
                    logger.warning(tr("gw.log.geoip_download_failed", error=exc))
            await asyncio.sleep(GEOIP_CHECK)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from . import analytics

        tasks = []
        if start_scheduler:
            tasks.append(asyncio.create_task(renew_loop()))
            tasks.append(asyncio.create_task(ctx.cloudflared.run()))
            if ctx.config.analytics:
                tasks.append(asyncio.create_task(analytics.run(ctx.analytics, "127.0.0.1", ctx.config.analytics_port, ipwatch=ctx.ipwatch)))
            tasks.append(asyncio.create_task(security_loop()))
            tasks.append(asyncio.create_task(geoip_loop()))
        yield
        for task in tasks:
            task.cancel()
        await ctx.cloudflared._stop()

    app = FastAPI(title="Deployra-WG API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx
    i18n.language_middleware(app)
    _errors(app)
    router = APIRouter(prefix="/api", dependencies=[Depends(_signed)])

    @router.get("/status")
    def status(ctx: Context = Depends(get_ctx)) -> dict:
        tokens = ctx.state.get("join_tokens", {})
        return {
            **ctx.public_info(),
            "service": NAME,
            "wg_up": (ctx.edge or ctx.hub).control.is_up() if ctx.config.wg_enabled else False,
            "peers": [] if ctx.edge else ctx.hub.status(),
            "edge": ctx.edge.status() if ctx.edge else None,
            "tokens": [
                {"id": tid, "name": t["name"], "expires": t["expires"], "used": bool(t["used_by"])}
                for tid, t in tokens.items()
            ],
            "nginx": ctx.last_apply,
            "certs": certs_list(ctx),
            "cloudflared": ctx.cloudflared.status(),
            "analytics": {"enabled": ctx.config.analytics, "received": ctx.analytics.received},
        }

    @router.get("/update")
    def update_status(log: int = 0, ctx: Context = Depends(get_ctx)) -> dict:
        return {"service": NAME, "version": __version__, **ctx.updates.info(max(0, min(log, 400)))}

    @router.post("/update")
    def update_start(body: UpdateRequest, ctx: Context = Depends(get_ctx)) -> dict:
        info = ctx.updates.request(body.version)
        logger.info(tr("gw.log.update_requested", version=info["target"]))
        return {"service": NAME, "version": __version__, **info}

    @router.get("/analytics")
    def analytics_daily(days: int = 30, domains: str = "", ctx: Context = Depends(get_ctx)) -> dict:
        names = [d.strip().lower() for d in domains.split(",") if d.strip()] or None
        return {"days": max(1, min(days, 400)), "domains": ctx.analytics.daily(max(1, min(days, 400)), names)}

    @router.get("/analytics/hourly")
    def analytics_hourly(hours: int = 24, domains: str = "", ctx: Context = Depends(get_ctx)) -> dict:
        names = [d.strip().lower() for d in domains.split(",") if d.strip()] or None
        return {"hours": max(1, min(hours, 72)), "domains": ctx.analytics.hourly(max(1, min(hours, 72)), names)}

    def _enrich(items: list[dict]) -> list[dict]:
        lang = i18n.current.get()
        for item in items:
            item["geo"] = ctx.geoip.lookup(item.get("ip") or item.get("target", "").split("/")[0], lang)
            item["banned"] = ctx.bans.is_banned(item["ip"]) if "ip" in item else True
            item["allowlisted"] = ctx.bans.allowed(item["ip"]) if "ip" in item else False
        return items

    @router.get("/traffic/top")
    def traffic_top(days: int = 1, domain: str = "", limit: int = 50) -> dict:
        items = ctx.ipwatch.top(max(1, min(days, 31)), domain.strip().lower() or None, max(1, min(limit, 200)))
        return {"items": _enrich(items), "geoip": ctx.geoip.status(), "retention_days": ctx.config.ip_retention_days}

    @router.get("/traffic/suspicious")
    def traffic_suspicious() -> dict:
        settings = ctx.bans.settings
        return {"items": _enrich(ctx.ipwatch.suspicious(settings["rate"])), "settings": settings}

    @router.get("/bans")
    def bans_list() -> dict:
        return {"items": _enrich(ctx.bans.list()), "settings": ctx.bans.settings, "firewall": ctx.banwall.available,
                "geoip": ctx.geoip.status()}  # fmt: skip

    @router.post("/bans")
    def bans_add(body: BanRequest) -> dict:
        entry = ctx.bans.add(body.target, body.minutes, body.reason)
        result = ctx.apply_bans()
        logger.info(tr("gw.log.banned", target=entry["target"]))
        return {"ban": entry, "nginx": {"ok": result.ok, "status": result.status, "message": result.message}}

    @router.post("/bans/remove")
    def bans_remove(body: UnbanRequest) -> dict:
        if not ctx.bans.remove(body.target):
            raise HTTPException(404, tr("gw.ban.not_found"))
        result = ctx.apply_bans()
        return {"ok": True, "nginx": {"ok": result.ok, "status": result.status, "message": result.message}}

    @router.put("/bans/settings")
    def bans_settings(body: BanSettings) -> dict:
        return ctx.bans.update_settings(body.auto, body.rate, body.minutes, body.allowlist)

    @router.post("/geoip/refresh")
    async def geoip_refresh() -> dict:
        import httpx

        try:
            updated = await ctx.geoip.refresh(force=True)
        except httpx.HTTPError as exc:
            raise HTTPException(502, tr("gw.log.geoip_download_failed", error=exc)) from None
        return {"updated": updated, **ctx.geoip.status()}

    @router.put("/cloudflared")
    def set_cloudflared(body: TunnelToken, ctx: Context = Depends(get_ctx)) -> dict:
        if body.token is not None and (len(body.token) < 20 or any(c.isspace() for c in body.token)):
            raise HTTPException(400, tr("gw.bad_tunnel_token"))
        ctx.cloudflared.set_token(body.token)
        return ctx.cloudflared.status()

    @router.post("/join-tokens")
    def add_token(body: TokenRequest, ctx: Context = Depends(get_ctx)) -> dict:
        return ctx.register_token(body.id, body.join_secret, body.name, body.lan_subnets, body.ttl_minutes, body.kind)

    @router.put("/routes")
    def set_routes(body: RoutesRequest, ctx: Context = Depends(get_ctx)) -> dict:
        """Bramka brzegowa: podsieci LAN węzłów, do których ruch idzie tunelem przez hub."""
        if not ctx.edge:
            return {"routes": [], "edge": False}
        return {"routes": ctx.edge.set_routes(body.subnets), "edge": True}

    @router.delete("/join-tokens/{token_id}")
    def drop_token(token_id: str, ctx: Context = Depends(get_ctx)) -> dict:
        ctx.drop_token(token_id)
        return {"ok": True}

    @router.patch("/peers/{peer_id}")
    def update_peer(peer_id: str, body: PeerUpdate, ctx: Context = Depends(get_ctx)) -> dict:
        try:
            ctx.hub.update_subnets(peer_id, body.lan_subnets)
        except KeyError:
            raise HTTPException(404, tr("gw.no_peer")) from None
        return {"ok": True}

    @router.delete("/peers/{peer_id}")
    def remove_peer(peer_id: str, ctx: Context = Depends(get_ctx)) -> dict:
        if peer_id == "panel":
            raise HTTPException(400, tr("gw.cant_remove_panel"))
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
        return _job_response(ctx.jobs.start("cert", tr("gw.job.cert", domains=", ".join(body.domains)), _cert_job(ctx, body)))

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
            raise HTTPException(400, tr("gw.url_scheme"))
        import httpx  # leniwie — bramka potrzebuje go tylko do testów HTTP

        started = time.perf_counter()
        headers = {"Host": body.host} if body.host else {}
        try:
            async with httpx.AsyncClient(timeout=10, verify=False, follow_redirects=False) as client:
                response = await client.get(body.url, headers=headers)
            return {"ok": response.status_code < 500, "status": response.status_code, "ms": round((time.perf_counter() - started) * 1000)}
        except httpx.ConnectError:
            return {"ok": False, "status": None, "error": tr("gw.check_refused")}
        except httpx.TimeoutException:
            return {"ok": False, "status": None, "error": tr("gw.check_timeout")}
        except httpx.HTTPError as exc:
            return {"ok": False, "status": None, "error": str(exc) or type(exc).__name__}

    def _job(job_id: str, ctx: Context = Depends(get_ctx)) -> Job:
        found = ctx.jobs.get(job_id)
        if not found:
            raise HTTPException(404, tr("srv.no_job"))
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


def security_tick(ctx: Context) -> bool:
    """Jeden obrót pętli bezpieczeństwa; zwraca True, gdy lista banów się zmieniła."""
    changed = ctx.bans.expire()
    settings = ctx.bans.settings
    if settings["auto"]:
        banned = set(ctx.bans.nets())
        for item in ctx.ipwatch.suspicious(settings["rate"]):
            if item["severity"] != "high" or item["ip"] in banned or ctx.bans.allowed(item["ip"]):
                continue
            try:
                ctx.bans.add(item["ip"], settings["minutes"], f"auto · {item['peak']}/min", auto=True)
            except ValueError:
                continue  # adres prywatny / chroniony — nie banujemy
            logger.warning(tr("gw.log.auto_banned", target=item["ip"], peak=item["peak"]))
            changed = True
    return changed


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
                job.log(tr("gw.renewed"))
            return changed

        job = ctx.jobs.start("renew", tr("gw.job.renew", domains=", ".join(req.domains)), work)
        async for _ in job.follow():
            pass
        if job.status == "error":
            logger.error(tr("gw.log.renew_failed", id=cert_id, error=job.error))
