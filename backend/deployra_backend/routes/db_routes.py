"""Przeglądarka baz MySQL/MariaDB: panel podaje cel (kontener na węźle albo host), węzeł łączy się i zwraca dane."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..context import Context, get_ctx
from ..services import dbbrowse
from . import signed

router = APIRouter(prefix="/api/db", dependencies=[Depends(signed)])

NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,190}$"


class DbTarget(BaseModel):
    container: str | None = Field(default=None, pattern=NAME)
    ep: str = Field(default="local", pattern=r"^(local|ct-\d{1,9})$")
    host: str | None = Field(default=None, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    user: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=1024)
    database: str | None = Field(default=None, max_length=128)
    ssl: str = Field(default="", pattern=r"^(|require|verify)$")

    def target(self) -> dbbrowse.Target:
        return dbbrowse.Target(**self.model_dump(include=set(DbTarget.model_fields)))


class TablesRequest(DbTarget):
    db: str = Field(min_length=1, max_length=128)


class TableRequest(TablesRequest):
    table: str = Field(min_length=1, max_length=128)


class RowsRequest(TableRequest):
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=50, ge=1, le=dbbrowse.MAX_PER_PAGE)
    order: str | None = Field(default=None, max_length=128)
    desc: bool = False
    column: str | None = Field(default=None, max_length=128)
    search: str | None = Field(default=None, max_length=500)


class ViewRequest(DbTarget):
    db: str | None = Field(default=None, max_length=128)
    table: str | None = Field(default=None, max_length=128)
    mode: str = Field(default="rows", pattern=r"^(rows|structure|none)$")
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=50, ge=1, le=dbbrowse.MAX_PER_PAGE)
    order: str | None = Field(default=None, max_length=128)
    desc: bool = False
    column: str | None = Field(default=None, max_length=128)
    search: str | None = Field(default=None, max_length=500)


class QueryRequest(DbTarget):
    db: str | None = Field(default=None, max_length=128)
    sql: str = Field(min_length=1, max_length=1_000_000)
    max_rows: int = Field(default=500, ge=1, le=dbbrowse.MAX_ROWS)


@contextmanager
def _session(ctx: Context, body: DbTarget, **kwargs: Any) -> Iterator[dbbrowse.Browser]:
    try:
        with dbbrowse.open_session(ctx, body.target(), **kwargs) as session:
            yield session
    except dbbrowse.DbError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/overview")
def overview(body: DbTarget, ctx: Context = Depends(get_ctx)) -> dict:
    with _session(ctx, body) as session:
        return session.overview()


@router.post("/tables")
def tables(body: TablesRequest, ctx: Context = Depends(get_ctx)) -> dict:
    with _session(ctx, body) as session:
        return {"tables": session.tables(body.db)}


@router.post("/structure")
def structure(body: TableRequest, ctx: Context = Depends(get_ctx)) -> dict:
    with _session(ctx, body) as session:
        return session.structure(body.db, body.table)


@router.post("/rows")
def rows(body: RowsRequest, ctx: Context = Depends(get_ctx)) -> dict:
    with _session(ctx, body) as session:
        return session.rows(body.db, body.table, body.page, body.per_page, body.order, body.desc, body.column, body.search)


@router.post("/view")
def view(body: ViewRequest, ctx: Context = Depends(get_ctx)) -> dict:
    """Strona przeglądarki jednym połączeniem: serwer i bazy, tabele wybranej bazy, wiersze albo struktura tabeli.

    Błąd w części bazy/tabeli (np. tabela zniknęła) wraca jako `error` obok listy baz — panel pokazuje go w treści.
    """
    with _session(ctx, body) as session:
        out: dict[str, Any] = {"overview": session.overview()}
        if body.db:
            try:
                out["tables"] = session.tables(body.db)
                if body.table and body.mode == "rows":
                    out["rows"] = session.rows(body.db, body.table, body.page, body.per_page, body.order, body.desc, body.column, body.search)
                elif body.table and body.mode == "structure":
                    out["structure"] = session.structure(body.db, body.table)
            except dbbrowse.DbError as exc:
                out["error"] = str(exc)
        return out


@router.post("/query")
def query(body: QueryRequest, ctx: Context = Depends(get_ctx)) -> dict:
    """Konsola SQL: kilka instrukcji naraz; błąd jednej zwracany obok wyników poprzednich (bez kodu 400)."""
    with _session(ctx, body, database=body.db, timeout=120, multi=True) as session:
        return session.query(body.sql, body.max_rows)
