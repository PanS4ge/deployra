"""Przeglądarka baz MySQL/MariaDB (jak phpMyAdmin): źródła, połączenia i wywołania węzła.

Źródło to zapisane połączenie (`db_connections`: zewnętrzny serwer albo kontener na węźle, opcjonalnie przypięte
do aplikacji) albo wykryty kontener bazy na węźle (klucz `n<węzeł>~<endpoint>~<kontener>`). Z bazą łączy się
węzeł (`/api/db/*`), bo to on ma sieć aplikacji; panel trzyma tylko konfigurację (hasło zaszyfrowane kluczem
panelu). Dla aplikacji podpowiada połączenie ze zmiennych (DB_HOST, WORDPRESS_DB_*, DATABASE_URL…) — hasło
czyta wtedy ze zmiennych przy zapisie, nie przez formularz.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

from deployra_common.client import RemoteError

from ..db import now
from ..i18n import t

KEY_CONN = re.compile(r"^c(\d{1,9})$")
KEY_LOCAL = re.compile(r"^n(\d{1,9})~(local|ct-\d{1,9})~([A-Za-z0-9][A-Za-z0-9_.-]{0,190})$")
CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,190}$")
HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9_]([A-Za-z0-9_.-]*[A-Za-z0-9_])?$|^\[?[0-9A-Fa-f:.]{2,45}\]?$")
EP = re.compile(r"^(local|ct-\d{1,9})$")
SSL_MODES = ("", "require", "verify")
MYSQL_REPOS = {"mysql", "mariadb", "percona", "percona-server", "mysql-server", "mariadb-galera"}
SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}


class DbError(Exception):
    """Błąd do pokazania w miejscu wyników (węzeł niedostępny, logowanie, zapytanie)."""


@dataclass
class Source:
    key: str
    title: str
    node: dict[str, Any]
    target: dict[str, Any]  # ciało zapytań /api/db/* (bez parametrów operacji)
    kind: str  # "container" | "remote"
    conn: dict[str, Any] | None = None
    app_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def address(self) -> str:
        if self.kind == "container":
            return self.target["container"]
        return f"{self.target['host']}:{self.target['port']}"


def is_mysql_image(image: str | None) -> bool:
    ref = (image or "").split("@", 1)[0]
    repo = ref.rsplit(":", 1)[0] if ":" in ref.rsplit("/", 1)[-1] else ref
    last = repo.rsplit("/", 1)[-1].lower()
    return last in MYSQL_REPOS or last.endswith(("-mysql", "-mariadb"))


def local_key(node_id: int, ep: str, container: str) -> str:
    return f"n{node_id}~{ep or 'local'}~{container}"


# --- połączenia (CRUD) -------------------------------------------------------------------------------


def connections(panel: Any, app_id: int | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT c.*, n.name AS node_name, n.status AS node_status, a.name AS app_name FROM db_connections c "
        "LEFT JOIN nodes n ON n.id = c.node_id LEFT JOIN apps a ON a.id = c.app_id"
    )
    if app_id is None:
        return panel.db.fetchall(sql + " ORDER BY c.name, c.id")
    return panel.db.fetchall(sql + " WHERE c.app_id = ? ORDER BY c.name, c.id", [app_id])


def get(panel: Any, conn_id: int) -> dict[str, Any]:
    row = panel.db.get("db_connections", conn_id)
    if not row:
        raise LookupError(t("db.err_missing"))
    return row


def _text(form: Any, key: str, limit: int = 190) -> str:
    return str(form.get(key) or "").strip()[:limit]


def parse_form(panel: Any, form: Any, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pola połączenia z formularza po sprawdzeniu. Puste hasło przy edycji = bez zmiany."""
    kind = _text(form, "kind") or "remote"
    try:
        node_id = int(form.get("node_id") or 0)
    except (TypeError, ValueError):
        node_id = 0
    node = panel.db.get("nodes", node_id) if node_id else None
    if not node:
        raise ValueError(t("db.err_node"))
    app_id = None
    if str(form.get("app_id") or "").isdigit() and int(form.get("app_id")):
        app = panel.db.get("apps", int(form.get("app_id")))
        if not app:
            raise ValueError(t("msg.no_app"))
        app_id = app["id"]
    values: dict[str, Any] = {"node_id": node["id"], "app_id": app_id, "username": _text(form, "username", 128) or None,
                              "dbname": _text(form, "dbname", 128) or None}  # fmt: skip
    if kind == "container":
        container, ep = _text(form, "container"), _text(form, "endpoint") or "local"
        if not CONTAINER.match(container):
            raise ValueError(t("db.err_container"))
        if not EP.match(ep):
            raise ValueError(t("db.err_container"))
        values.update(container=container, endpoint=ep, host=None, port=None, ssl="")
        default_name = container
    else:
        host = _text(form, "host", 253)
        if not HOST.match(host):
            raise ValueError(t("db.err_host"))
        try:
            port = int(form.get("port") or 3306)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            raise ValueError(t("db.err_port"))
        ssl = _text(form, "ssl")
        if ssl not in SSL_MODES:
            ssl = ""
        if not values["username"]:
            raise ValueError(t("db.err_user"))
        values.update(container=None, endpoint="local", host=host, port=port, ssl=ssl)
        default_name = host
    values["name"] = _text(form, "name", 120) or default_name
    password = str(form.get("password") or "")
    if password:
        values["password"] = panel.sealer.seal(password)
    elif not existing or bool(form.get("clear_password")):
        values["password"] = None
    return values


def create(panel: Any, values: dict[str, Any]) -> int:
    return panel.db.insert("db_connections", **values, created_at=now())


def update(panel: Any, conn_id: int, values: dict[str, Any]) -> None:
    get(panel, conn_id)
    panel.db.update("db_connections", conn_id, **values)


def delete(panel: Any, conn_id: int) -> None:
    get(panel, conn_id)
    panel.db.execute("DELETE FROM db_connections WHERE id = ?", [conn_id])


def forget_app(panel: Any, app_id: int) -> None:
    """Usunięta aplikacja: połączenia zostają na liście ogólnej, bez przypięcia."""
    panel.db.execute("UPDATE db_connections SET app_id = NULL WHERE app_id = ?", [app_id])


# --- źródła ------------------------------------------------------------------------------------------


def connection_source(panel: Any, row: dict[str, Any]) -> Source:
    node = panel.db.get("nodes", row["node_id"])
    if not node:
        raise LookupError(t("db.err_node"))
    target: dict[str, Any] = {"user": row.get("username") or None, "password": panel.sealer.unseal(row.get("password")) or None,
                              "database": row.get("dbname") or None}  # fmt: skip
    if row.get("container"):
        target.update(container=row["container"], ep=row.get("endpoint") or "local")
        kind = "container"
    else:
        target.update(host=row["host"], port=int(row.get("port") or 3306), ssl=row.get("ssl") or "")
        kind = "remote"
    return Source(key=f"c{row['id']}", title=row["name"], node=node, target=target, kind=kind, conn=row, app_id=row.get("app_id"))


def resolve(panel: Any, key: str) -> Source:
    match = KEY_CONN.match(key or "")
    if match:
        return connection_source(panel, get(panel, int(match.group(1))))
    match = KEY_LOCAL.match(key or "")
    if not match:
        raise LookupError(t("db.err_missing"))
    node = panel.db.get("nodes", int(match.group(1)))
    if not node:
        raise LookupError(t("db.err_node"))
    ep, container = match.group(2), match.group(3)
    return Source(key=key, title=container, node=node, target={"container": container, "ep": ep}, kind="container")


async def call(panel: Any, source: Source, op: str, timeout: float = 40, **payload: Any) -> dict[str, Any]:
    """Operacja przeglądarki na węźle; błędy (logowanie, sieć, stary węzeł) jako DbError z czytelną treścią."""
    try:
        client = panel.node_client(source.node)
    except LookupError as exc:
        raise DbError(str(exc)) from exc
    body = {k: v for k, v in {**source.target, **payload}.items() if v is not None}
    try:
        return await client.post(f"/api/db/{op}", json=body, timeout=timeout) or {}
    except RemoteError as exc:
        if exc.status == 404 and exc.detail == "Not Found":
            raise DbError(t("db.err_node_outdated", node=source.node["name"])) from exc
        raise DbError(exc.detail) from exc
    except Exception as exc:  # noqa: BLE001 — węzeł offline / przekroczony czas
        raise DbError(t("db.err_node_offline", node=source.node["name"], error=type(exc).__name__)) from exc


# --- wykrywanie kontenerów baz -----------------------------------------------------------------------


async def _node_containers(panel: Any, node: dict[str, Any], ep: str = "local") -> list[dict[str, Any]]:
    path = "/api/containers" if ep == "local" else f"/api/containers?ep={ep}"
    try:
        return await asyncio.wait_for(panel.node_client(node).get(path), timeout=10) or []
    except Exception:  # noqa: BLE001 — węzeł offline nie blokuje listy
        return []


def _container_item(node: dict[str, Any], ep: str, c: dict[str, Any]) -> dict[str, Any]:
    return {"key": local_key(node["id"], ep, c["name"]), "node_id": node["id"], "node": node["name"], "ep": ep, "container": c["name"],
            "image": c.get("image") or "", "app": c.get("app"), "stack": c.get("stack")}  # fmt: skip


async def discovered(panel: Any) -> list[dict[str, Any]]:
    """Działające kontenery MySQL/MariaDB na sparowanych węzłach (rozpoznane po obrazie)."""
    nodes = panel.db.fetchall("SELECT * FROM nodes WHERE status = 'paired' ORDER BY name")
    lists = await asyncio.gather(*(_node_containers(panel, node) for node in nodes))
    return [
        _container_item(node, "local", c)
        for node, items in zip(nodes, lists)
        for c in items
        if c.get("state") == "running" and is_mysql_image(c.get("image"))
    ]


async def _app_context(panel: Any, app: dict[str, Any]) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]], list[dict[str, Any]]]:
    """(węzeł, endpoint, kontenery węzła, kontenery aplikacji)."""
    from . import deploy
    from .uptime import app_containers

    node = panel.db.get("nodes", app["node_id"])
    if not node or node.get("status") != "paired":
        return node, "local", [], []
    try:
        ep = deploy.load(panel, app)[0].endpoint or "local"
    except Exception:  # noqa: BLE001 — uszkodzony spec: endpoint hosta
        ep = "local"
    items = await _node_containers(panel, node, ep)
    return node, ep, items, app_containers(items, app)


async def app_sources(panel: Any, app: dict[str, Any]) -> tuple[list[Source], dict[str, Any]]:
    """Źródła aplikacji: przypięte połączenia i jej własne kontenery MySQL/MariaDB (np. usługa `db` w compose).

    Zwraca też kontekst do formularza (węzeł, endpoint, nazwy kontenerów i podpowiedź ze zmiennych).
    """
    sources = [connection_source(panel, row) for row in connections(panel, app["id"]) if panel.db.get("nodes", row["node_id"])]
    node, ep, items, mine = await _app_context(panel, app)
    linked = {(s.node["id"], s.target.get("container")) for s in sources if s.kind == "container"}
    for c in mine:
        if c.get("state") == "running" and is_mysql_image(c.get("image")) and (node["id"], c["name"]) not in linked:
            sources.append(Source(key=local_key(node["id"], ep, c["name"]), title=c["name"], node=node,
                                  target={"container": c["name"], "ep": ep}, kind="container", app_id=app["id"], extra={"detected": True}))  # fmt: skip
    env = await app_env(panel, app, node, ep, mine)
    context = {"node": node, "ep": ep, "containers": [c["name"] for c in items if c.get("state") == "running"],
               "suggestion": env_suggestion(env, _aliases(items, mine))}  # fmt: skip
    return sources, context


async def app_env(panel: Any, app: dict[str, Any], node: dict[str, Any] | None = None, ep: str | None = None,
                  mine: list[dict[str, Any]] | None = None) -> dict[str, str]:  # fmt: skip
    """Zmienne aplikacji: z działającego kontenera (to, czego używa naprawdę), inaczej ze specyfikacji z zestawami."""
    from . import deploy, env_groups

    if node is None:
        node, ep, _, mine = await _app_context(panel, app)
    for c in [c for c in mine or [] if not is_mysql_image(c.get("image"))][:1]:
        try:
            path = f"/api/containers/{c['id']}" + ("" if ep == "local" else f"?ep={ep}")
            info = await asyncio.wait_for(panel.node_client(node).get(path), timeout=10)
            env = dict(item.split("=", 1) for item in (info.get("Config") or {}).get("Env") or [] if "=" in item)
            if env:
                return env
        except Exception:  # noqa: BLE001 — kontener niedostępny: zmienne ze specyfikacji
            pass
    try:
        spec, _ = deploy.load(panel, app)
        return dict(env_groups.effective(panel, app["id"], spec).env)
    except Exception:  # noqa: BLE001
        return {}


def _aliases(items: list[dict[str, Any]], mine: list[dict[str, Any]]) -> dict[str, str]:
    """Nazwa hosta w zmiennych → kontener: nazwy kontenerów i usługi compose z projektu aplikacji (`db` → `wp-db-1`)."""
    out = {c["name"]: c["name"] for c in items if c.get("state") == "running"}
    projects = {c.get("stack") for c in mine if c.get("stack")}
    for c in items:
        if c.get("stack") in projects and c.get("service") and c.get("state") == "running":
            out.setdefault(c["service"], c["name"])
    return out


# --- podpowiedź ze zmiennych aplikacji ------------------------------------------------------------------

URL_KEYS = ("DATABASE_URL", "DB_URL", "MYSQL_URL", "MARIADB_URL", "DB_DSN")
ENV_GROUPS = (  # (host, port, użytkownik, hasło, baza)
    ("DB_HOST", "DB_PORT", ("DB_USER", "DB_USERNAME"), ("DB_PASSWORD", "DB_PASS"), ("DB_NAME", "DB_DATABASE")),
    ("WORDPRESS_DB_HOST", None, ("WORDPRESS_DB_USER",), ("WORDPRESS_DB_PASSWORD",), ("WORDPRESS_DB_NAME",)),
    ("MYSQL_HOST", "MYSQL_PORT", ("MYSQL_USER",), ("MYSQL_PASSWORD",), ("MYSQL_DATABASE",)),
    ("MARIADB_HOST", "MARIADB_PORT", ("MARIADB_USER",), ("MARIADB_PASSWORD",), ("MARIADB_DATABASE",)),
    ("DATABASE_HOST", "DATABASE_PORT", ("DATABASE_USER", "DATABASE_USERNAME"), ("DATABASE_PASSWORD",), ("DATABASE_NAME",)),
    ("database__connection__host", "database__connection__port", ("database__connection__user",),
     ("database__connection__password",), ("database__connection__database",)),  # Ghost
)
NOT_MYSQL = ("pgsql", "postgres", "sqlite", "mongo", "mssql", "sqlsrv", "oracle", "redis")


def _first(env: dict[str, str], keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    for key in keys:
        if env.get(key):
            return key, env[key]
    return None, None


def _split_host(value: str, port: str | None) -> tuple[str, int]:
    host, sep, tail = value.rpartition(":")
    if sep and tail.isdigit() and "]" not in tail:
        return host.strip("[]"), int(tail)
    return value, int(port) if (port or "").isdigit() else 3306


def env_suggestion(env: dict[str, str], aliases: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Połączenie MySQL odczytane ze zmiennych aplikacji; `password_key` — zmienna z hasłem (samo hasło nie wychodzi)."""
    aliases = aliases or {}
    driver = " ".join(str(env.get(k) or "") for k in ("DB_CONNECTION", "DB_TYPE", "DB_DRIVER", "DB_ENGINE", "database__client")).lower()
    found: dict[str, Any] | None = None
    for key in URL_KEYS:
        url = env.get(key) or ""
        scheme = url.split(":", 1)[0].lower()
        if not scheme.startswith(("mysql", "mariadb")):
            continue
        parsed = urlparse(url)
        if parsed.hostname:
            found = {"host": parsed.hostname, "port": parsed.port or 3306, "username": unquote(parsed.username or ""),
                     "dbname": parsed.path.lstrip("/").split("?")[0], "password_key": key if parsed.password else None, "url": True}  # fmt: skip
            break
    if found is None and not any(word in driver for word in NOT_MYSQL):
        for host_key, port_key, user_keys, pass_keys, db_keys in ENV_GROUPS:
            if not env.get(host_key):
                continue
            host, port = _split_host(env[host_key], env.get(port_key) if port_key else None)
            if port == 5432 or port == 27017:
                continue
            _, user = _first(env, user_keys)
            pass_key, _ = _first(env, pass_keys)
            _, dbname = _first(env, db_keys)
            found = {"host": host, "port": port, "username": user or "", "dbname": dbname or "", "password_key": pass_key, "url": False}
            break
    if found is None:
        return None
    container = aliases.get(found["host"])
    found["kind"] = "container" if container else "remote"
    found["container"] = container or ""
    return found


def env_password(env: dict[str, str], key: str) -> str | None:
    """Hasło ze zmiennej wskazanej przez podpowiedź (także z adresu DATABASE_URL)."""
    value = env.get(key)
    if not value:
        return None
    if key in URL_KEYS:
        parsed = urlparse(value)
        return unquote(parsed.password) if parsed.password else None
    return value
