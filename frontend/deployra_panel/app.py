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
from .context import Panel
from .deps import Forbidden, LoginRequired
from .security import SESSION_COOKIE
from .web import is_htmx, render, toast

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PREFIXES = ("/static/", "/hooks/", "/healthz", "/login", "/setup")


def create_app(config: Config | None = None, panel: Panel | None = None) -> FastAPI:
    panel = panel or Panel(config or Config())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        from .services import updates, uptime, wg_peer

        await run_in_threadpool(wg_peer.ensure_up, panel)
        await run_in_threadpool(panel.sessions.cleanup)
        # zadania żyją w pamięci — to, co trwało w chwili restartu, zostało przerwane
        panel.db.execute("UPDATE apps SET status = 'error', last_error = ? WHERE status = 'deploying'",
                         ["Wdrożenie przerwane restartem panelu — wdroż ponownie"])
        panel.db.execute("UPDATE certificates SET status = 'error', error = ? WHERE status = 'issuing'",
                         ["Wystawianie przerwane restartem panelu — ponów"])
        await run_in_threadpool(updates.finish_pending, panel)
        tasks = []
        if panel.config.monitoring:
            tasks.append(asyncio.create_task(uptime.loop(panel)))
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
        if path.startswith("/static/") or path.startswith("/hooks/") or path == "/healthz":
            return await call_next(request)

        has_users = await run_in_threadpool(panel.has_users)
        if not has_users and not path.startswith("/setup"):
            return _redirect(request, "/setup")

        sid = request.cookies.get(SESSION_COOKIE)
        session = await run_in_threadpool(panel.sessions.load, sid) if sid else None
        if session:
            request.state.user = session
            request.state.csrf = session["csrf"]
            if request.method not in SAFE_METHODS:
                token = request.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(token, session["csrf"]):
                    return JSONResponse({"detail": "Nieprawidłowy token CSRF — odśwież stronę"}, status_code=403)
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
        return render(request, "error.html", status_code=403, title="Brak dostępu", message=exc.message, icon="shield-x")

    @app.exception_handler(RemoteError)
    async def _remote(request: Request, exc: RemoteError) -> Response:
        message = f"Zdalny komponent: {exc.detail}"
        if is_htmx(request):
            return toast(message, "err", status=502)
        return render(request, "error.html", status_code=502, title="Błąd węzła", message=message, icon="hdd-network")

    @app.exception_handler(httpx.HTTPError)
    async def _offline(request: Request, exc: httpx.HTTPError) -> Response:
        message = "Nie można połączyć się z węzłem/bramką — sprawdź tunel i czy usługa działa"
        logger.warning(f"{request.url.path}: {type(exc).__name__}: {exc}")
        if is_htmx(request):
            return toast(message, "err", status=504)
        return render(request, "error.html", status_code=504, title="Brak połączenia", message=message, icon="wifi-off")

    @app.exception_handler(LookupError)
    async def _lookup(request: Request, exc: LookupError) -> Response:
        if is_htmx(request):
            return toast(str(exc), "err", status=409)
        return render(request, "error.html", status_code=409, title="Nie da się", message=str(exc), icon="exclamation-diamond")

    @app.exception_handler(ValueError)
    async def _value(request: Request, exc: ValueError) -> Response:
        if is_htmx(request):
            return toast(str(exc), "err", status=400)
        return render(request, "error.html", status_code=400, title="Błędne dane", message=str(exc), icon="exclamation-diamond")

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> Response:
        if is_htmx(request) or request.url.path.startswith("/hooks/"):
            if request.url.path.startswith("/hooks/"):
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            return toast(str(exc.detail), "err", status=exc.status_code)
        return render(
            request, "error.html", status_code=exc.status_code, title=f"Błąd {exc.status_code}", message=str(exc.detail),
            icon="question-octagon" if exc.status_code == 404 else "exclamation-octagon",
        )  # fmt: skip

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    from .routes import register

    register(app)
    return app


def _redirect(request: Request, url: str) -> Response:
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)
