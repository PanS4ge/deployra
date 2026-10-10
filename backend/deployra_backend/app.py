"""Aplikacja FastAPI backendu."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from .context import Context
from .routes import apps_routes, docker_routes, system


def _docker_errors() -> tuple[type, type]:
    try:
        from docker import errors

        return errors.NotFound, errors.APIError
    except ImportError:  # pragma: no cover
        return LookupError, RuntimeError


def create_app(ctx: Context, *, start_services: bool = True, bind_host: str = "") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_services:
            ctx.vitals.start()
            if ctx.config.sftp_enabled:
                from .services.sftp_server import SftpService

                ctx.sftp = SftpService(ctx.state, ctx.config.sites_dir, ctx.config.data_dir / "ssh_host_ed25519_key")
                try:
                    await ctx.sftp.start(bind_host, ctx.config.sftp_port)
                except OSError as exc:
                    logger.error(f"SFTP nie wystartował: {exc}")
        yield
        ctx.vitals.stop()
        if ctx.sftp:
            await ctx.sftp.stop()

    app = FastAPI(title="Deployra Backend", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx

    not_found, api_error = _docker_errors()

    @app.exception_handler(not_found)
    async def _not_found(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": f"nie znaleziono: {getattr(exc, 'explanation', None) or exc}"}, status_code=404)

    @app.exception_handler(api_error)
    async def _api_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": getattr(exc, "explanation", None) or str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def _value(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PermissionError)
    async def _perm(_: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(RuntimeError)
    async def _runtime(_: Request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    app.include_router(system.public)
    app.include_router(system.router)
    app.include_router(docker_routes.router)
    app.include_router(docker_routes.ws_router)
    app.include_router(apps_routes.router)
    app.include_router(apps_routes.upload_router)
    return app
