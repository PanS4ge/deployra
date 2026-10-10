"""Rejestracja routerów panelu."""

from __future__ import annotations

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from . import apps, auth, certs, dashboard, docker, hooks, import_npm, jobs, nodes, proxy, settings, terminal, tunnel

    for module in (auth, dashboard, jobs, tunnel, nodes, docker, terminal, apps, hooks, proxy, certs, import_npm, settings):
        app.include_router(module.router)
