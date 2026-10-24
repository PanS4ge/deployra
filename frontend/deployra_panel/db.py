"""Baza danych panelu: SQLite (domyślnie) lub MySQL/MariaDB (PyMySQL).

Cienka warstwa zamiast ORM: zapytania z `?` (dla MySQL zamieniane na `%s`),
wiersze jako dict, migracje wersjonowane z DDL zależnym od dialektu.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

JSON_COLUMNS = {"domains", "target", "redirect", "locations", "users", "clients", "lan_subnets", "info", "login_users", "external_wg", "vulns", "items",
                "ignored_ids"}  # fmt: skip


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self._lock = threading.RLock()
        parsed = urlparse(url)
        if parsed.scheme == "sqlite":
            self.dialect = "sqlite"
            path = url[len("sqlite:///") :]
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
        elif parsed.scheme in ("mysql", "mariadb"):
            import pymysql

            self.dialect = "mysql"
            self._mysql_args = {
                "host": parsed.hostname or "localhost",
                "port": parsed.port or 3306,
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "database": parsed.path.lstrip("/"),
                "charset": "utf8mb4",
                "autocommit": True,
                "cursorclass": pymysql.cursors.DictCursor,
            }
            self._conn = pymysql.connect(**self._mysql_args)
        else:
            raise ValueError("DEPLOYRA_DB: sqlite:///ścieżka albo mysql://user:hasło@host:3306/baza")
        self._in_tx = False

    # --- niskopoziomowe -------------------------------------------------------------

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "mysql" else sql

    def _cursor(self):
        if self.dialect == "mysql":
            self._conn.ping(reconnect=True)
        return self._conn.cursor()

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        """Wykonuje zapytanie; zwraca lastrowid (INSERT) albo liczbę zmienionych wierszy."""
        with self._lock:
            cur = self._cursor()
            cur.execute(self._sql(sql), tuple(params))
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount

    def fetchall(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._cursor()
            cur.execute(self._sql(sql), tuple(params))
            return [self._decode(dict(row)) for row in cur.fetchall()]

    def fetchone(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        row = self.fetchone(sql, params)
        return next(iter(row.values())) if row else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._in_tx:
                yield
                return
            self._in_tx = True
            self.execute("BEGIN" if self.dialect == "sqlite" else "START TRANSACTION")
            try:
                yield
                self.execute("COMMIT")
            except BaseException:
                self.execute("ROLLBACK")
                raise
            finally:
                self._in_tx = False

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        for key in JSON_COLUMNS & row.keys():
            if isinstance(row[key], str):
                row[key] = json.loads(row[key]) if row[key] else None
        return row

    @staticmethod
    def _encode(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            return int(value)
        return value

    # --- wygodne CRUD -----------------------------------------------------------------

    def insert(self, table: str, **values: Any) -> int:
        _check_ident(table, *values)
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        return self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", [self._encode(v) for v in values.values()])

    def update(self, table: str, row_id: Any, key: str = "id", **values: Any) -> int:
        _check_ident(table, key, *values)
        if not values:
            return 0
        sets = ", ".join(f"{col} = ?" for col in values)
        params = [self._encode(v) for v in values.values()] + [row_id]
        return self.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", params)

    def delete(self, table: str, row_id: Any, key: str = "id") -> int:
        _check_ident(table, key)
        return self.execute(f"DELETE FROM {table} WHERE {key} = ?", [row_id])

    def get(self, table: str, row_id: Any, key: str = "id") -> dict[str, Any] | None:
        _check_ident(table, key)
        return self.fetchone(f"SELECT * FROM {table} WHERE {key} = ?", [row_id])

    def all(self, table: str, order: str = "id") -> list[dict[str, Any]]:
        _check_ident(table, order)
        return self.fetchall(f"SELECT * FROM {table} ORDER BY {order}")

    # --- ustawienia ------------------------------------------------------------------

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.fetchone("SELECT value FROM settings WHERE skey = ?", [key])
        return json.loads(row["value"]) if row else default

    def settings(self) -> dict[str, Any]:
        return {row["skey"]: json.loads(row["value"]) for row in self.fetchall("SELECT skey, value FROM settings")}

    def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        if self.dialect == "sqlite":
            self.execute(
                "INSERT INTO settings (skey, value) VALUES (?, ?) ON CONFLICT(skey) DO UPDATE SET value = excluded.value",
                [key, payload],
            )
        else:
            self.execute("INSERT INTO settings (skey, value) VALUES (?, ?) ON DUPLICATE KEY UPDATE value = VALUES(value)", [key, payload])

    # --- migracje --------------------------------------------------------------------

    def migrate(self) -> int:
        from .migrations import MIGRATIONS

        self.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        current = self.scalar("SELECT MAX(version) FROM schema_version") or 0
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            for statement in statements(self.dialect):
                self.execute(statement)
            self.execute("INSERT INTO schema_version (version) VALUES (?)", [version])
            current = version
        return current


_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _check_ident(*names: str) -> None:
    for name in names:
        if not _IDENT.match(name):
            raise ValueError(f"nieprawidłowy identyfikator SQL: {name!r}")
