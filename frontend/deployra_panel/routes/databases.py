"""Przeglądarka baz MySQL/MariaDB (jak phpMyAdmin): lista połączeń, przeglądanie tabel, konsola SQL, bazy aplikacji."""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..i18n import t
from ..permissions import Permission
from ..services import databases
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user)])
VIEW = Depends(require(Permission.ALLOW_DATABASES))
WRITE = Depends(require(Permission.ALLOW_DATABASES_WRITE))
PER_PAGE = (25, 50, 100, 200)
TABS = ("rows", "structure", "sql")


def _nodes(panel: Panel) -> list[dict]:
    return panel.db.fetchall("SELECT id, name FROM nodes WHERE status = 'paired' ORDER BY name")


def _apps(panel: Panel) -> list[dict]:
    return panel.db.fetchall("SELECT id, name, node_id FROM apps ORDER BY name")


@router.get("/databases")
def databases_page(request: Request, panel: Panel = Depends(get_panel), user: dict = VIEW):
    return render(request, "databases/list.html", title=t("db.title"), connections=databases.connections(panel), nodes=_nodes(panel),
                  apps=_apps(panel), preset={})  # fmt: skip


@router.get("/databases/discovered")
async def discovered(request: Request, panel: Panel = Depends(get_panel), user: dict = VIEW):
    return render(request, "databases/_discovered.html", items=await databases.discovered(panel))


# --- połączenia ----------------------------------------------------------------------------------------


async def _values(panel: Panel, form: Any, existing: dict | None = None) -> dict:
    values = databases.parse_form(panel, form, existing)
    env_key = str(form.get("env_password") or "")
    if "password" not in values or values["password"] is None:
        if env_key and values.get("app_id") and not form.get("password"):  # hasło z podpowiedzi: ze zmiennych aplikacji, nie z formularza
            app = panel.db.get("apps", values["app_id"])
            secret = databases.env_password(await databases.app_env(panel, app), env_key)
            if secret:
                values["password"] = panel.sealer.seal(secret)
    return values


def _next(form: Any, fallback: str) -> str:
    target = str(form.get("next") or "")
    return target if target.startswith("/") and not target.startswith("//") else fallback


@router.post("/databases/connections")
async def create_connection(request: Request, panel: Panel = Depends(get_panel), user: dict = WRITE):
    form = await request.form()
    conn_id = databases.create(panel, await _values(panel, form))
    return redirect(request, _next(form, f"/databases/c{conn_id}"), t("db.saved"))


@router.post("/databases/connections/test")
async def test_connection(request: Request, panel: Panel = Depends(get_panel), user: dict = WRITE):
    form = await request.form()
    existing = databases.get(panel, int(form["conn_id"])) if str(form.get("conn_id") or "").isdigit() else None
    values = await _values(panel, form, existing)
    if existing and "password" not in values:
        values["password"] = existing.get("password")
    source = databases.connection_source(panel, {"id": 0, **values})
    try:
        data = await databases.call(panel, source, "overview", timeout=30)
    except databases.DbError as exc:
        return toast(t("db.test_failed", error=str(exc)), "err")
    return toast(t("db.test_ok", version=data.get("version", "?"), user=data.get("user", "?")))


@router.post("/databases/connections/{conn_id}")
async def update_connection(conn_id: int, request: Request, panel: Panel = Depends(get_panel), user: dict = WRITE):
    form = await request.form()
    existing = databases.get(panel, conn_id)
    databases.update(panel, conn_id, await _values(panel, form, existing))
    return redirect(request, _next(form, f"/databases/c{conn_id}"), t("db.saved"))


@router.post("/databases/connections/{conn_id}/delete")
async def delete_connection(conn_id: int, request: Request, panel: Panel = Depends(get_panel), user: dict = WRITE):
    form = await request.form()
    databases.delete(panel, conn_id)
    return redirect(request, _next(form, "/databases"), t("db.deleted"))


# --- przeglądarka ------------------------------------------------------------------------------------------


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _source(panel: Panel, key: str) -> databases.Source:
    try:
        return databases.resolve(panel, key)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


def _app_context(panel: Panel, source: databases.Source, app_param: str) -> dict | None:
    app_id = _int(app_param, 0) or source.app_id
    return panel.db.get("apps", app_id) if app_id else None


@router.get("/databases/{key}")
async def browser(key: str, request: Request, panel: Panel = Depends(get_panel), user: dict = VIEW):
    source = _source(panel, key)
    q = request.query_params
    db, table = q.get("db") or "", q.get("table") or ""
    tab = q.get("tab") if q.get("tab") in TABS else "rows"
    per_page = _int(q.get("per"), 50) if _int(q.get("per"), 50) in PER_PAGE else 50
    state = {"db": db, "table": table, "tab": tab if table or tab == "sql" else "", "page": max(1, _int(q.get("page"), 1)), "per": per_page,
             "order": q.get("order") or "", "dir": "desc" if q.get("dir") == "desc" else "", "col": q.get("col") or "", "q": q.get("q") or "",
             "sys": "1" if q.get("sys") else "", "app": q.get("app") or ""}  # fmt: skip
    base = f"/databases/{source.key}"

    def link(**changes: Any) -> str:
        merged = {**state, **changes}
        if merged.get("page") == 1:
            merged["page"] = ""
        if merged.get("per") == 50:
            merged["per"] = ""
        params = {k: v for k, v in merged.items() if v not in ("", None)}
        return base + ("?" + urlencode(params) if params else "")

    mode = "none" if tab == "sql" else ("structure" if tab == "structure" else "rows")
    data: dict[str, Any] = {}
    error = ""
    try:
        data = await databases.call(
            panel, source, "view", db=db or None, table=table or None, mode=mode, page=state["page"], per_page=per_page,
            order=state["order"] or None, desc=state["dir"] == "desc", column=state["col"] or None, search=state["q"] or None,
        )  # fmt: skip
        error = data.get("error") or ""
    except databases.DbError as exc:
        error = str(exc)
    overview = data.get("overview") or {}
    rows = data.get("rows")
    pages = max(1, math.ceil((rows or {}).get("total", 0) / per_page)) if rows else 1
    default_sql = f"SELECT * FROM `{table.replace('`', '``')}` LIMIT 100;" if table else ""
    return render(
        request, "databases/browser.html", title=table or db or source.title, source=source, base=base, state=state, link=link,
        overview=overview, databases=overview.get("databases") or [], tables=data.get("tables"), rows=rows, structure=data.get("structure"),
        pages=pages, per_options=PER_PAGE, error=error, app=_app_context(panel, source, state["app"]), default_sql=default_sql,
        system_names=databases.SYSTEM_DATABASES,
    )  # fmt: skip


@router.post("/databases/{key}/sql")
async def run_sql(key: str, request: Request, panel: Panel = Depends(get_panel), user: dict = WRITE):
    source = _source(panel, key)
    form = await request.form()
    sql = str(form.get("sql") or "").strip()
    if not sql:
        return toast(t("db.sql_empty"), "warn", status=400)
    max_rows = _int(form.get("max_rows"), 500)
    try:
        result = await databases.call(panel, source, "query", timeout=150, db=str(form.get("db") or "") or None, sql=sql,
                                      max_rows=max(1, min(max_rows, 1000)))  # fmt: skip
    except databases.DbError as exc:
        result = {"results": [], "error": str(exc), "elapsed": None}
    return render(request, "databases/_sql_result.html", result=result)


# --- baza aplikacji -------------------------------------------------------------------------------------


@router.get("/apps/{app_id}/database")
async def app_database(app_id: int, request: Request, manage: str = "", panel: Panel = Depends(get_panel), user: dict = VIEW):
    app = panel.db.get("apps", app_id)
    if not app:
        raise HTTPException(404, t("msg.no_app"))
    sources, context = await databases.app_sources(panel, app)
    if len(sources) == 1 and not manage:
        return redirect(request, f"/databases/{sources[0].key}?app={app['id']}")
    suggestion = context["suggestion"] or {}
    preset = {"kind": suggestion.get("kind") or "remote", "node_id": app["node_id"], "endpoint": context["ep"], **suggestion} if suggestion else \
        {"kind": "remote", "node_id": app["node_id"], "endpoint": context["ep"]}  # fmt: skip
    return render(
        request, "databases/app.html", title=t("db.app_title"), app=app, sources=sources, context=context, suggestion=suggestion,
        preset=preset, nodes=_nodes(panel), apps=_apps(panel),
    )  # fmt: skip
