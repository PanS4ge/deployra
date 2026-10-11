"""Rejestracja routerów panelu."""

from __future__ import annotations

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from . import analytics, apps, auth, certs, dashboard, docker, hooks, import_npm, jobs, nodes, proxy, settings, terminal, traffic, tunnel, updates, uptime

    for module in (auth, dashboard, analytics, traffic, jobs, tunnel, uptime, nodes, docker, terminal, apps, hooks, proxy, certs, import_npm, updates, settings):
        app.include_router(module.router)
