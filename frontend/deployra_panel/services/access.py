"""Konta „tylko przypisane serwery” (w stylu Pterodactyla): użytkownik widzi wyłącznie swoje aplikacje i kontenery.

Konto z `scoped = 1` ma w panelu tylko stronę „Moje serwery” — middleware blokuje mu każdy inny adres.
Każde przypisanie (`grants`) wskazuje aplikację Deployry albo kontener na węźle (po nazwie, bo identyfikator
zmienia się przy odtworzeniu) i ma własne uprawnienia: zasilanie, logi, konsola, pliki, wdrożenie, zmienne.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..db import now
from ..i18n import t

POWER, LOGS, CONSOLE, FILES, DEPLOY, ENV = "P", "L", "C", "F", "D", "E"
ALL = (POWER, LOGS, CONSOLE, FILES, DEPLOY, ENV)
APP_ONLY = {FILES, DEPLOY, ENV}  # przy samym kontenerze nie ma plików ani wdrożeń
NAMES = {POWER: "power", LOGS: "logs", CONSOLE: "console", FILES: "files", DEPLOY: "deploy", ENV: "env"}
SCOPED_PREFIXES = ("/servers", "/logout", "/language", "/login", "/static/", "/healthz")


def clean_perms(perms: str, kind: str) -> str:
    allowed = [p for p in ALL if p in (perms or "") and (kind == "app" or p not in APP_ONLY)]
    return "".join(allowed)


def is_scoped(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("scoped"))


def path_allowed(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in SCOPED_PREFIXES)


@dataclass
class Target:
    """Na czym działa przypisanie: węzeł, endpoint Dockera, kontener (albo stack compose aplikacji)."""

    node: dict[str, Any]
    ep: str
    container: str
    app: dict[str, Any] | None = None
    stack: str | None = None


def grants_for(panel: Any, user_id: int) -> list[dict[str, Any]]:
    rows = panel.db.fetchall("SELECT * FROM grants WHERE user_id = ? ORDER BY id", [user_id])
    for row in rows:
        row["app"] = panel.db.get("apps", row["app_id"]) if row["kind"] == "app" and row.get("app_id") else None
        node_id = row["app"]["node_id"] if row["app"] else row.get("node_id")
        row["node"] = panel.node(node_id) if node_id else None
        row["label"] = row["app"]["name"] if row["app"] else row.get("container") or "?"
    return rows


def grant(panel: Any, user: dict[str, Any], grant_id: int) -> dict[str, Any]:
    """Przypisanie zalogowanego użytkownika — cudze albo nieistniejące → LookupError (404 dla użytkownika)."""
    row = next((g for g in grants_for(panel, user["id"]) if g["id"] == grant_id), None)
    if not row or (row["kind"] == "app" and not row["app"]) or not row["node"]:
        raise LookupError(t("servers.not_found"))
    return row


def can(row: dict[str, Any], perm: str) -> bool:
    return perm in (row.get("perms") or "")


def target(panel: Any, row: dict[str, Any]) -> Target:
    node = row["node"]
    if row["kind"] == "app":
        from . import deploy

        app = row["app"]
        spec, _ = deploy.load(panel, app)
        stack = app["slug"] if app["kind"] == "compose" else None
        return Target(node=node, ep=spec.endpoint, container=f"app-{app['slug']}", app=app, stack=stack)
    return Target(node=node, ep="local", container=row["container"])


def add(panel: Any, user_id: int, kind: str, perms: str, app_id: int | None = None, node_id: int | None = None,
        container: str | None = None) -> int:  # fmt: skip
    if kind == "app":
        if not app_id or not panel.db.get("apps", app_id):
            raise ValueError(t("servers.pick_app"))
        exists = panel.db.fetchone("SELECT id FROM grants WHERE user_id = ? AND kind = 'app' AND app_id = ?", [user_id, app_id])
        node_id, container = None, None
    elif kind == "container":
        import re

        if not node_id or not panel.node(node_id) or not container or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$", container):
            raise ValueError(t("servers.pick_container"))
        exists = panel.db.fetchone("SELECT id FROM grants WHERE user_id = ? AND kind = 'container' AND node_id = ? AND container = ?",
                                   [user_id, node_id, container])  # fmt: skip
        app_id = None
    else:
        raise ValueError(t("servers.pick_app"))
    if exists:
        raise ValueError(t("servers.duplicate"))
    return panel.db.insert("grants", user_id=user_id, kind=kind, app_id=app_id, node_id=node_id, container=container,
                           perms=clean_perms(perms, kind), created_at=now())  # fmt: skip


def forget_app(panel: Any, app_id: int) -> None:
    panel.db.execute("DELETE FROM grants WHERE kind = 'app' AND app_id = ?", [app_id])
