"""Aplikacja FastAPI panelu."""

from __future__ import annotations

import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from deployra_common.client import RemoteError

from .config import Config
from deployra_common import i18n as i18n_core

from . import i18n
from .context import Panel
from .deps import Forbidden, LoginRequired
from .security import SESSION_COOKIE
from .services import access
from .web import is_htmx, render, toast

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PREFIXES = ("/static/", "/hooks/", "/healthz", "/login", "/setup", "/language")


def create_app(config: Config | None = None, panel: Panel | None = None) -> FastAPI:
    panel = panel or Panel(config or Config())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        from .services import notify, resources, updates, uptime, wg_peer

        await run_in_threadpool(wg_peer.ensure_up, panel)
        await run_in_threadpool(panel.sessions.cleanup)
        # zadania żyją w pamięci — to, co trwało w chwili restartu, zostało przerwane
        panel.db.execute("UPDATE apps SET status = 'error', last_error = ? WHERE status = 'deploying'", [i18n.t("msg.deploy_interrupted")])
        panel.db.execute("UPDATE certificates SET status = 'error', error = ? WHERE status = 'issuing'", [i18n.t("msg.cert_interrupted")])
        await run_in_threadpool(updates.finish_pending, panel)
        await run_in_threadpool(notify.migrate_legacy_webhook, panel)
        panel.plugins.start(app)
        tasks = []
        if panel.config.local_node:
            from .services import local_node

            tasks.append(asyncio.create_task(local_node.loop(panel)))
        if panel.config.monitoring:
            tasks.append(asyncio.create_task(uptime.loop(panel)))
            tasks.append(asyncio.create_task(resources.loop(panel)))
        if panel.config.update_checks:
            tasks.append(asyncio.create_task(updates.loop(panel)))
        yield
        for task in tasks:
            task.cancel()
        await panel.aclose()

    app = FastAPI(title="Deployra", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.panel = panel
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.middleware("http")
    async def session_middleware(request: Request, call_next):
        request.state.user = None
        request.state.csrf = ""
        path = request.url.path
        cookie_lang, accept = request.cookies.get(i18n.LANG_COOKIE), request.headers.get("accept-language")
        i18n_core.current.set(i18n.resolve(None, cookie_lang, panel.default_language, accept))
        if path.startswith("/static/") or path.startswith("/hooks/") or path == "/healthz":
            return await call_next(request)

        has_users = await run_in_threadpool(panel.has_users)
        if not has_users and not path.startswith("/setup"):
            return _redirect(request, "/setup")

        sid = request.cookies.get(SESSION_COOKIE)
        session = await run_in_threadpool(panel.sessions.load, sid) if sid else None
        if session:
            request.state.user = session
            i18n_core.current.set(i18n.resolve(session, cookie_lang, panel.default_language, accept))
            request.state.csrf = session["csrf"]
            if request.method not in SAFE_METHODS:
                token = request.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(token, session["csrf"]):
                    return JSONResponse({"detail": i18n.t("msg.csrf")}, status_code=403)
            if session.get("scoped") and not access.path_allowed(path):
                # konto „tylko przypisane serwery”: cała reszta panelu jest zamknięta, nie tylko ukryta w menu
                if request.method == "GET" and "text/event-stream" not in request.headers.get("accept", ""):
                    return _redirect(request, "/servers")
                return JSONResponse({"detail": i18n.t("servers.denied")}, status_code=403)
        elif not any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return _redirect(request, f"/login?next={path}" if request.method == "GET" else "/login")
        return await call_next(request)

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired) -> Response:
        return _redirect(request, "/login")

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden) -> Response:
        if is_htmx(request):
            return toast(exc.message, "err", status=403)
        return render(request, "error.html", status_code=403, title=i18n.t("msg.title_forbidden"), message=exc.message, icon="shield-x")

    @app.exception_handler(RemoteError)
    async def _remote(request: Request, exc: RemoteError) -> Response:
        message = i18n.t("msg.remote_error", detail=exc.detail)
        if is_htmx(request):
            return toast(message, "err", status=502)
        return render(request, "error.html", status_code=502, title=i18n.t("msg.title_node_error"), message=message, icon="hdd-network")

    @app.exception_handler(httpx.HTTPError)
    async def _offline(request: Request, exc: httpx.HTTPError) -> Response:
        message = i18n.t("msg.offline")
        logger.warning(f"{request.url.path}: {type(exc).__name__}: {exc}")
        if is_htmx(request):
            return toast(message, "err", status=504)
        return render(request, "error.html", status_code=504, title=i18n.t("msg.title_offline"), message=message, icon="wifi-off")

    @app.exception_handler(LookupError)
    async def _lookup(request: Request, exc: LookupError) -> Response:
        if is_htmx(request):
            return toast(str(exc), "err", status=409)
        return render(request, "error.html", status_code=409, title=i18n.t("msg.title_conflict"), message=str(exc), icon="exclamation-diamond")

    @app.exception_handler(ValueError)
    async def _value(request: Request, exc: ValueError) -> Response:
        if is_htmx(request):
            return toast(str(exc), "err", status=400)
        return render(request, "error.html", status_code=400, title=i18n.t("msg.title_bad_data"), message=str(exc), icon="exclamation-diamond")

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> Response:
        if is_htmx(request) or request.url.path.startswith("/hooks/"):
            if request.url.path.startswith("/hooks/"):
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            return toast(str(exc.detail), "err", status=exc.status_code)
        return render(
            request, "error.html", status_code=exc.status_code, title=i18n.t("msg.title_error", code=exc.status_code), message=str(exc.detail),
            icon="question-octagon" if exc.status_code == 404 else "exclamation-octagon",
        )  # fmt: skip

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    from .plugins import static_route
    from .routes import register
    from .web import plugin_templates

    plugin_templates.manager = panel.plugins
    register(app)
    app.include_router(static_route(panel.plugins))
    return app


def _redirect(request: Request, url: str) -> Response:
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)
