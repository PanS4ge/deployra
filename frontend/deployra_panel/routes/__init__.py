"""Rejestracja routerów panelu."""

from __future__ import annotations

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from . import alerts, analytics, apps, auth, certs, dashboard, dns, docker, hooks, import_npm, jobs, logs, nodes, plugins, proxy, servers, settings, terminal, traffic, tunnel, updates, uptime

    for module in (auth, dashboard, analytics, traffic, jobs, logs, tunnel, uptime, nodes, docker, terminal, apps, hooks, proxy, certs, dns, import_npm, updates, plugins, alerts, servers, settings):
        app.include_router(module.router)
