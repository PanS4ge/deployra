"""Rejestracja routerów panelu."""

from __future__ import annotations

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from . import alerts, analytics, apps, auth, certs, dashboard, db_backups, dns, docker, hooks, import_npm, jobs, logs, mcp, minecraft, nodes, plugins, proxy, servers, settings, terminal, traffic, tunnel, updates, uptime

    for module in (auth, dashboard, analytics, traffic, jobs, logs, tunnel, uptime, nodes, docker, terminal, apps, hooks, proxy, certs, dns, db_backups, import_npm, updates, plugins, alerts, servers, settings, mcp):
        app.include_router(module.router)
    app.include_router(minecraft.public)  # pobieranie pluginu i kanał pluginu (bez sesji, podpis HMAC)
    app.include_router(minecraft.router)
