"""Przeglądarka baz MySQL/MariaDB (jak phpMyAdmin): kontener bazy na węźle albo zewnętrzny serwer.

Łączy się węzeł, nie panel — to on ma sieć aplikacji. Kontener bazy: po adresie IP w sieci Dockera (backend
działa w `network_mode: host`) albo przez opublikowany port; dane logowania ze zmiennych kontenera
(MYSQL_ROOT_PASSWORD, MARIADB_USER…, także warianty `_FILE`), o ile panel nie podał własnych. Zewnętrzny
serwer: host:port jak w aplikacji, opcjonalnie z SSL.

Komórki wyników idą jako JSON: tekst, liczba albo None; binaria jako `{"bin": rozmiar, "hex": początek}`,
długie teksty jako `{"text": początek, "len": długość}`. W trybie atrapy Dockera (dev) zamiast serwera
odpowiada demonstracyjna baza SQLite z tym samym API.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import ssl as ssllib
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from deployra_common.i18n import tr

SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}
CONNECT_TIMEOUT = 5
CANDIDATE_TIMEOUT = 3  # kontener: kolejne adresy (sieci Dockera, opublikowany port) próbujemy szybciej
MAX_ROWS = 1000
MAX_PER_PAGE = 200
EXACT_COUNT_LIMIT = 200_000  # większe tabele bez filtra: liczba wierszy z information_schema (szacunek)
DRAIN_LIMIT = 200_000  # konsola: tyle wierszy ponad pokazane liczymy; dalej wynik jest przerywany
_PLACEHOLDER = re.compile(r"%(s|%)")


class DbError(Exception):
    """Błąd połączenia albo zapytania — treść idzie do panelu jako komunikat."""


@dataclass
class Target:
    container: str | None = None
    ep: str = "local"
    host: str | None = None
    port: int = 3306
    user: str | None = None
    password: str | None = None
    database: str | None = None
    ssl: str = ""  # "" | "require" (szyfrowanie bez weryfikacji) | "verify" (certyfikat i nazwa hosta)


def ident(name: str) -> str:
    """Nazwa w odwrotnych apostrofach; `%` podwojony, bo wewnętrzne zapytania zawsze idą z parametrami (%s)."""
    return "`" + str(name).replace("`", "``").replace("%", "%%") + "`"


def _s(value: Any) -> str:
    """Tekst z katalogu serwera — część wersji MySQL/MariaDB zwraca kolumny information_schema jako bajty."""
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)


def like_pattern(text: str) -> str:
    """Wzorzec „zawiera” z ucieczką `!` (ESCAPE '!' działa tak samo w MySQL i SQLite)."""
    return "%" + text.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def _printable(text: str) -> bool:
    return all(ch.isprintable() or ch in "\t\n\r" for ch in text)


def cell(value: Any, limit: int) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None or not _printable(text):
            return {"bin": len(raw), "hex": raw[:16].hex()}
    elif isinstance(value, (set, frozenset)):  # kolumna SET
        text = ",".join(sorted(value))
    elif isinstance(value, (Decimal, dt.date, dt.time, dt.timedelta)):
        text = str(value)
    else:
        text = str(value)
    if limit and len(text) > limit:
        return {"text": text[:limit], "len": len(text)}
    return text


# --- dane logowania i adres kontenera bazy -------------------------------------------------------


def _env(info: dict) -> dict[str, str]:
    out = {}
    for item in (info.get("Config") or {}).get("Env") or []:
        key, sep, value = item.partition("=")
        if sep:
            out[key] = value
    return out


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "yes", "true", "on")


def container_login(env: dict[str, str], read_file: Any = None) -> tuple[str, str]:
    """(użytkownik, hasło) z konwencji obrazów mysql, mariadb i bitnami — root, gdy jego hasło jest znane."""

    def value(*keys: str) -> str | None:
        for key in keys:
            if env.get(key):
                return env[key]
            path = env.get(f"{key}_FILE")
            if path and read_file:
                secret = read_file(path)
                if secret:
                    return secret.strip()
        return None

    root = value("MYSQL_ROOT_PASSWORD", "MARIADB_ROOT_PASSWORD")
    if root:
        return "root", root
    if any(_truthy(env.get(k)) for k in ("MYSQL_ALLOW_EMPTY_PASSWORD", "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD", "ALLOW_EMPTY_PASSWORD")):
        return "root", ""
    user = value("MYSQL_USER", "MARIADB_USER")
    if user:
        return user, value("MYSQL_PASSWORD", "MARIADB_PASSWORD") or ""
    return "root", ""


def container_database(env: dict[str, str]) -> str | None:
    return env.get("MYSQL_DATABASE") or env.get("MARIADB_DATABASE") or None


def container_addresses(info: dict, endpoint_ip: str | None = None) -> list[tuple[str, int]]:
    """Adresy, pod którymi backend może dosięgnąć bazy w kontenerze, w kolejności prób."""
    env = _env(info)
    try:
        port = int(env.get("MYSQL_TCP_PORT") or env.get("MARIADB_PORT_NUMBER") or env.get("MYSQL_PORT_NUMBER") or 3306)
    except ValueError:
        port = 3306
    settings = info.get("NetworkSettings") or {}
    internal: list[tuple[str, int]] = []
    if (info.get("HostConfig") or {}).get("NetworkMode") == "host":
        internal.append(("127.0.0.1", port))
    for net in (settings.get("Networks") or {}).values():
        if (net or {}).get("IPAddress"):
            internal.append((net["IPAddress"], port))
    if settings.get("IPAddress"):
        internal.append((settings["IPAddress"], port))
    published = []
    for binding in (settings.get("Ports") or {}).get(f"{port}/tcp") or []:
        host_port = str((binding or {}).get("HostPort") or "")
        if host_port.isdigit():
            host_ip = binding.get("HostIp") or ""
            if endpoint_ip:
                host_ip = endpoint_ip
            elif host_ip in ("", "0.0.0.0", "::"):
                host_ip = "127.0.0.1"
            published.append((host_ip, int(host_port)))
    # CT na Proxmoxie: sieci Dockera są w namespace CT, z hosta widać tylko opublikowane porty pod adresem CT
    ordered = published + internal if endpoint_ip else internal + published
    seen, out = set(), []
    for item in ordered:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --- połączenie ------------------------------------------------------------------------------------


def _ssl_context(mode: str) -> ssllib.SSLContext | None:
    if mode == "verify":
        return ssllib.create_default_context()
    if mode == "require":
        context = ssllib.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssllib.CERT_NONE
        return context
    return None


def _mysql_error(exc: Exception) -> DbError:
    args = getattr(exc, "args", ())
    if len(args) >= 2 and isinstance(args[0], int):
        return DbError(f"({args[0]}) {args[1]}")
    return DbError(str(exc) or type(exc).__name__)


def _connect(host: str, port: int, user: str, password: str, database: str | None, ssl_mode: str, timeout: int,
             connect_timeout: int, multi: bool) -> Any:  # fmt: skip
    import pymysql
    from pymysql.constants import CLIENT

    return pymysql.connect(
        host=host, port=port, user=user, password=password, database=database or None, charset="utf8mb4",
        connect_timeout=connect_timeout, read_timeout=timeout, write_timeout=timeout, autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS if multi else 0, ssl=_ssl_context(ssl_mode),
        cursorclass=pymysql.cursors.SSCursor,  # bez buforowania: `SELECT *` z dużej tabeli nie ładuje jej całej do RAM
    )  # fmt: skip


@contextmanager
def open_session(ctx: Any, target: Target, database: str | None = None, timeout: int = 30, multi: bool = False) -> Iterator[Browser]:
    """Sesja przeglądarki dla celu (kontener albo zewnętrzny serwer); połączenie zamykane po wyjściu."""
    if getattr(ctx.config, "fake_docker", False):
        with demo_session() as session:
            yield session
        return
    database = database or target.database
    if target.container:
        candidates, user, password, default_db = _container_target(ctx, target)
        database = database or default_db
        connect_timeout = CANDIDATE_TIMEOUT if len(candidates) > 1 else CONNECT_TIMEOUT
    elif target.host:
        candidates, user, password = [(target.host, target.port)], target.user or "", target.password or ""
        connect_timeout = CONNECT_TIMEOUT
    else:
        raise DbError(tr("node.db.no_target"))
    conn, last = None, None
    for host, port in candidates:
        try:
            conn = _connect(host, port, user, password, database, target.ssl, timeout, connect_timeout, multi)
            break
        except Exception as exc:  # noqa: BLE001 — następny adres kontenera; zostaje ostatni błąd
            last = exc
            args = getattr(exc, "args", ())
            if args and args[0] in (1044, 1045, 1049):  # zły login/baza — inne adresy nic nie zmienią
                break
    if conn is None:
        if last is None:
            raise DbError(tr("node.db.no_address", name=target.container or ""))
        raise _mysql_error(last)
    try:
        yield Browser(conn, default_db=database or "")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — połączenie mogło paść w trakcie
            pass


def _container_target(ctx: Any, target: Target) -> tuple[list[tuple[str, int]], str, str, str | None]:
    from . import docker_svc

    client = ctx.docker_for(target.ep)
    try:
        info = client.api.inspect_container(target.container)
    except Exception as exc:  # noqa: BLE001 — brak kontenera albo Dockera
        raise DbError(tr("node.db.no_container", name=target.container)) from exc
    if not (info.get("State") or {}).get("Running"):
        raise DbError(tr("node.db.not_running", name=target.container))
    env = _env(info)

    def read_file(path: str) -> str | None:
        code, output = docker_svc._exec(client, info["Id"], ["cat", path])
        return output if code == 0 else None

    if target.user:
        user, password = target.user, target.password or ""
    else:
        user, password = container_login(env, read_file)
    endpoint_ip = None
    if target.ep and target.ep != "local":
        try:
            endpoint_ip = ctx.endpoints.get(target.ep).ip
        except Exception:  # noqa: BLE001 — bez adresu CT zostają adresy z sieci Dockera
            endpoint_ip = None
    return container_addresses(info, endpoint_ip), user, password, container_database(env)


# --- operacje ----------------------------------------------------------------------------------------


class Browser:
    """Operacje przeglądarki na otwartym połączeniu (MySQL/MariaDB przez PyMySQL)."""

    dialect = "mysql"

    def __init__(self, conn: Any, default_db: str = "") -> None:
        self.conn = conn
        self.default_db = default_db

    # niskopoziomowe: wewnętrzne zapytania zawsze z krotką parametrów (także pustą)
    def _all(self, sql: str, args: tuple | list = ()) -> list[tuple]:
        try:
            cur = self.conn.cursor()
            try:
                cur.execute(sql, tuple(args))
                return list(cur.fetchall())
            finally:
                cur.close()
        except Exception as exc:  # noqa: BLE001
            raise self._error(exc) from exc

    def _error(self, exc: Exception) -> DbError:
        return exc if isinstance(exc, DbError) else _mysql_error(exc)

    def ref(self, db: str, table: str) -> str:
        return f"{ident(db)}.{ident(table)}"

    def server(self) -> dict[str, Any]:
        version, user = self._all("SELECT VERSION(), CURRENT_USER()")[0]
        return {"version": _s(version), "user": _s(user), "dialect": self.dialect}

    def databases(self) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT s.SCHEMA_NAME, COUNT(t.TABLE_NAME), COALESCE(SUM(t.DATA_LENGTH + t.INDEX_LENGTH), 0) "
            "FROM information_schema.SCHEMATA s LEFT JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = s.SCHEMA_NAME "
            "GROUP BY s.SCHEMA_NAME ORDER BY s.SCHEMA_NAME"
        )
        return [{"name": _s(n), "tables": int(c or 0), "size": int(s or 0), "system": _s(n).lower() in SYSTEM_DATABASES} for n, c, s in rows]

    def overview(self) -> dict[str, Any]:
        return {**self.server(), "default_db": self.default_db, "databases": self.databases()}

    def tables(self, db: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, TABLE_COLLATION, TABLE_COMMENT "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
            [db],
        )
        return [
            {"name": _s(name), "view": "VIEW" in _s(kind).upper(), "engine": _s(engine), "rows": rows_ if rows_ is None else int(rows_),
             "size": int(data or 0) + int(index or 0), "collation": _s(collation), "comment": _s(comment)}
            for name, kind, engine, rows_, data, index, collation, comment in rows
        ]  # fmt: skip

    def columns(self, db: str, table: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            [db, table],
        )
        if not rows:
            raise DbError(tr("node.db.no_table", name=table))
        return [
            {"name": _s(name), "type": _s(kind), "null": _s(nullable).upper() == "YES", "key": _s(key),
             "default": None if default is None else _s(default), "extra": _s(extra), "comment": _s(comment)}
            for name, kind, nullable, key, default, extra, comment in rows
        ]  # fmt: skip

    def indexes(self, db: str, table: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME, INDEX_TYPE FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME, SEQ_IN_INDEX",
            [db, table],
        )
        out: dict[str, dict[str, Any]] = {}
        for name, non_unique, column, kind in rows:
            item = out.setdefault(_s(name), {"name": _s(name), "unique": not int(non_unique or 0), "type": _s(kind), "columns": []})
            item["columns"].append(_s(column))
        return list(out.values())

    def create_sql(self, db: str, table: str) -> str:
        try:
            row = self._all(f"SHOW CREATE TABLE {self.ref(db, table)}")
        except DbError:
            return ""
        return _s(row[0][1]) if row and len(row[0]) > 1 else ""

    def structure(self, db: str, table: str) -> dict[str, Any]:
        return {"columns": self.columns(db, table), "indexes": self.indexes(db, table), "create": self.create_sql(db, table)}

    def _estimate(self, db: str, table: str) -> int | None:
        rows = self._all("SELECT TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", [db, table])
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def rows(self, db: str, table: str, page: int = 1, per_page: int = 50, order: str | None = None, desc: bool = False,
             column: str | None = None, search: str | None = None, cell_limit: int = 300) -> dict[str, Any]:  # fmt: skip
        columns = self.columns(db, table)
        names = [c["name"] for c in columns]
        per_page = max(1, min(int(per_page or 50), MAX_PER_PAGE))
        page = max(1, int(page or 1))
        where, args = "", []
        if search:
            targets = [column] if column in names else names
            where = " WHERE " + " OR ".join(f"CAST({ident(c)} AS CHAR) LIKE %s ESCAPE '!'" for c in targets)
            args = [like_pattern(search)] * len(targets)
        approx = False
        estimate = None if search else self._estimate(db, table)
        if estimate is not None and estimate > EXACT_COUNT_LIMIT:
            total, approx = estimate, True
        else:
            total = int(self._all(f"SELECT COUNT(*) FROM {self.ref(db, table)}{where}", args)[0][0])
        order_sql = f" ORDER BY {ident(order)} {'DESC' if desc else 'ASC'}" if order in names else ""
        offset = (page - 1) * per_page
        data = self._all(f"SELECT * FROM {self.ref(db, table)}{where}{order_sql} LIMIT {per_page} OFFSET {offset}", args)
        primary = [c["name"] for c in columns if c["key"] == "PRI"]
        return {
            "columns": columns, "rows": [[cell(v, cell_limit) for v in row] for row in data], "total": total, "approx": approx,
            "page": page, "per_page": per_page, "primary": primary,
        }  # fmt: skip

    # konsola SQL: tekst użytkownika bez formatowania parametrów, wiele instrukcji naraz
    def query(self, sql: str, max_rows: int = 500, cell_limit: int = 2000) -> dict[str, Any]:
        max_rows = max(1, min(int(max_rows or 500), MAX_ROWS))
        started = time.monotonic()
        results: list[dict[str, Any]] = []
        error, aborted = None, False
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            while True:
                result = self._result(cur, max_rows, cell_limit)
                results.append(result)
                if result["truncated"] and result["total"] is None:
                    aborted = True  # ogromny wynik: dalszych instrukcji nie czytamy
                    break
                if not cur.nextset():
                    break
        except Exception as exc:  # noqa: BLE001 — błąd instrukcji: wyniki poprzednich zostają
            error = str(self._error(exc))
        finally:
            if aborted:  # zamknięcie kursora czytałoby resztę wyniku do końca — zrywamy połączenie
                (getattr(self.conn, "_force_close", None) or self.conn.close)()
            else:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001 — nieodczytana reszta wyniku po błędzie
                    pass
        return {"results": results, "error": error, "aborted": aborted, "elapsed": round(time.monotonic() - started, 3)}

    @staticmethod
    def _result(cur: Any, max_rows: int, cell_limit: int) -> dict[str, Any]:
        if not cur.description:
            return {"columns": [], "rows": [], "affected": max(cur.rowcount or 0, 0), "truncated": False, "total": None}
        fetched = list(cur.fetchmany(max_rows + 1))
        total: int | None = len(fetched)
        if total > max_rows:  # reszta wyniku tylko do policzenia (kursor bez bufora — bez trzymania w pamięci)
            while total < DRAIN_LIMIT:
                chunk = cur.fetchmany(5000)
                if not chunk:
                    break
                total += len(chunk)
            else:
                total = None
        return {
            "columns": [str(d[0]) for d in cur.description], "rows": [[cell(v, cell_limit) for v in row] for row in fetched[:max_rows]],
            "affected": None, "truncated": len(fetched) > max_rows, "total": total,
        }  # fmt: skip


# --- demonstracja (tryb atrapy Dockera) ----------------------------------------------------------


class _DemoCursor:
    """Kursor SQLite z parametrami w stylu PyMySQL (%s, %%)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._cur = conn.cursor()
        self._pending: list[str] = []

    @property
    def description(self) -> Any:
        return self._cur.description

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def execute(self, sql: str, args: tuple | None = None) -> None:
        if args is None:  # konsola: kilka instrukcji — kolejne wykonuje nextset()
            self._pending = _split_sqlite(sql)
            if not self._pending:
                raise DbError("Pusty tekst zapytania")
            self._cur.execute(self._pending.pop(0))
            return
        self._cur.execute(_PLACEHOLDER.sub(lambda m: "?" if m.group(1) == "s" else "%", sql), args)

    def nextset(self) -> bool:
        if not self._pending:
            return False
        self._cur.execute(self._pending.pop(0))
        return True

    def fetchall(self) -> list[tuple]:
        return self._cur.fetchall()

    def fetchmany(self, size: int) -> list[tuple]:
        return self._cur.fetchmany(size)

    def close(self) -> None:
        self._cur.close()


def _split_sqlite(sql: str) -> list[str]:
    """Instrukcje rozdzielone średnikami; średnik w cudzysłowie nie dzieli (sqlite3.complete_statement)."""
    out, buf = [], ""
    for part in sql.split(";"):
        buf = f"{buf};{part}" if buf else part
        if sqlite3.complete_statement(buf + ";"):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


class _DemoConn:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def cursor(self) -> _DemoCursor:
        return _DemoCursor(self._conn)

    def close(self) -> None:  # wspólna baza demo zostaje otwarta
        pass


class DemoBrowser(Browser):
    """Te same operacje na SQLite: dwie „bazy” jako dołączone schematy, katalog z sqlite_master i PRAGMA."""

    dialect = "sqlite"

    def server(self) -> dict[str, Any]:
        return {"version": f"SQLite {sqlite3.sqlite_version} (demo)", "user": "demo@localhost", "dialect": self.dialect}

    def databases(self) -> list[dict[str, Any]]:
        out = []
        for name in DEMO_SCHEMAS:
            count = self._all(f"SELECT COUNT(*) FROM {ident(name)}.sqlite_master WHERE type IN ('table', 'view')")[0][0]
            out.append({"name": name, "tables": int(count), "size": 0, "system": False})
        return out

    def _check_db(self, db: str) -> None:
        if db not in DEMO_SCHEMAS:
            raise DbError(f"(1049) Unknown database '{db}'")

    def tables(self, db: str) -> list[dict[str, Any]]:
        self._check_db(db)
        out = []
        for name, kind in self._all(f"SELECT name, type FROM {ident(db)}.sqlite_master WHERE type IN ('table', 'view') ORDER BY name"):
            count = self._all(f"SELECT COUNT(*) FROM {self.ref(db, name)}")[0][0]
            out.append({"name": name, "view": kind == "view", "engine": "SQLite", "rows": int(count), "size": 0, "collation": "", "comment": ""})
        return out

    def columns(self, db: str, table: str) -> list[dict[str, Any]]:
        self._check_db(db)
        rows = self._all(f"PRAGMA {ident(db)}.table_info({ident(table)})")
        if not rows:
            raise DbError(tr("node.db.no_table", name=table))
        return [{"name": name, "type": kind.lower(), "null": not notnull, "key": "PRI" if pk else "", "default": default,
                 "extra": "", "comment": ""} for _, name, kind, notnull, default, pk in rows]  # fmt: skip

    def indexes(self, db: str, table: str) -> list[dict[str, Any]]:
        out = []
        primary = [c["name"] for c in self.columns(db, table) if c["key"] == "PRI"]
        if primary:
            out.append({"name": "PRIMARY", "unique": True, "type": "BTREE", "columns": primary})
        for _, name, unique, *_ in self._all(f"PRAGMA {ident(db)}.index_list({ident(table)})"):
            if name.startswith("sqlite_autoindex"):
                continue
            cols = [row[2] for row in self._all(f"PRAGMA {ident(db)}.index_info({ident(name)})")]
            out.append({"name": name, "unique": bool(unique), "type": "BTREE", "columns": cols})
        return out

    def create_sql(self, db: str, table: str) -> str:
        rows = self._all(f"SELECT sql FROM {ident(db)}.sqlite_master WHERE name = %s", [table])
        return rows[0][0] if rows else ""

    def _estimate(self, db: str, table: str) -> int | None:
        return None


DEMO_SCHEMAS = ("nextcloud", "wordpress")
_demo_lock = threading.Lock()
_demo: sqlite3.Connection | None = None


def _seed_demo() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    for name in DEMO_SCHEMAS:
        conn.execute(f"ATTACH DATABASE ':memory:' AS {name}")
    conn.executescript(
        """
        CREATE TABLE nextcloud.oc_users (uid VARCHAR(64) PRIMARY KEY, displayname VARCHAR(64), password VARCHAR(255) NOT NULL, quota VARCHAR(16));
        CREATE TABLE nextcloud.oc_filecache (fileid INTEGER PRIMARY KEY, storage INTEGER NOT NULL, path VARCHAR(4000), mimetype VARCHAR(64),
                                             size BIGINT NOT NULL DEFAULT 0, mtime BIGINT NOT NULL, etag VARCHAR(40));
        CREATE INDEX nextcloud.fs_storage_path ON oc_filecache (storage, path);
        CREATE TABLE nextcloud.oc_appconfig (appid VARCHAR(32) NOT NULL, configkey VARCHAR(64) NOT NULL, configvalue TEXT,
                                             PRIMARY KEY (appid, configkey));
        CREATE TABLE wordpress.wp_posts (ID INTEGER PRIMARY KEY, post_author INTEGER NOT NULL DEFAULT 0, post_date DATETIME NOT NULL,
                                         post_title TEXT NOT NULL, post_content TEXT, post_status VARCHAR(20) NOT NULL DEFAULT 'publish', thumbnail BLOB);
        CREATE INDEX wordpress.type_status_date ON wp_posts (post_status, post_date);
        CREATE TABLE wordpress.wp_options (option_id INTEGER PRIMARY KEY, option_name VARCHAR(191) NOT NULL UNIQUE, option_value TEXT,
                                           autoload VARCHAR(20) NOT NULL DEFAULT 'yes');
        CREATE TABLE wordpress.wp_users (ID INTEGER PRIMARY KEY, user_login VARCHAR(60) NOT NULL, user_email VARCHAR(100), user_registered DATETIME);
        CREATE VIEW wordpress.wp_published AS SELECT ID, post_title, post_date FROM wp_posts WHERE post_status = 'publish';
        """
    )
    users = [("admin", "Administrator", "5GB"), ("ola", "Ola Nowak", "1GB"), ("marek", "Marek Kowalski", None), ("gosc", None, "none")]
    conn.executemany("INSERT INTO nextcloud.oc_users VALUES (?, ?, ?, ?)", [(u, d, "3|$argon2id$v=19$m=65536,t=4,p=1$" + u * 4, q) for u, d, q in users])
    mimes = ["httpd/unix-directory", "image/jpeg", "application/pdf", "text/plain", "video/mp4"]
    conn.executemany(
        "INSERT INTO nextcloud.oc_filecache VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(i, 1 + i % 3, f"files/{['Zdjęcia', 'Dokumenty', 'Muzyka'][i % 3]}/plik-{i:03d}.{['', 'jpg', 'pdf', 'txt', 'mp4'][i % 5]}",
          mimes[i % 5], (i * 7919) % 50_000_000, 1_760_000_000 + i * 3600, f"{i:08x}") for i in range(1, 238)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO nextcloud.oc_appconfig VALUES (?, ?, ?)",
        [("core", "installedat", "1726000000.1"), ("core", "vendor", "nextcloud"), ("files", "enabled", "yes"),
         ("theming", "name", "Chmura"), ("theming", "color", "#0082c9"), ("activity", "enabled", "yes")],
    )  # fmt: skip
    long_text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 30
    conn.executemany(
        "INSERT INTO wordpress.wp_posts VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(i, 1 + i % 2, f"2026-{1 + i % 10:02d}-{1 + i % 27:02d} 1{i % 10}:00:00", f"Wpis numer {i}", long_text if i % 4 == 0 else f"Treść wpisu {i}",
          "draft" if i % 7 == 0 else "publish", bytes([0x89, 0x50, 0x4E, 0x47, i % 256]) if i % 3 == 0 else None) for i in range(1, 64)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO wordpress.wp_options (option_name, option_value, autoload) VALUES (?, ?, ?)",
        [("siteurl", "https://blog.example.com", "yes"), ("blogname", "Blog demo", "yes"), ("admin_email", "admin@example.com", "yes"),
         ("active_plugins", 'a:2:{i:0;s:19:"akismet/akismet.php";i:1;s:9:"hello.php";}', "yes"), ("cron", None, "no")],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO wordpress.wp_users VALUES (?, ?, ?, ?)",
        [(1, "admin", "admin@example.com", "2026-01-10 08:00:00"), (2, "redaktor", None, "2026-03-02 12:30:00")],
    )
    return conn


@contextmanager
def demo_session() -> Iterator[Browser]:
    global _demo
    with _demo_lock:
        if _demo is None:
            _demo = _seed_demo()
        yield DemoBrowser(_DemoConn(_demo))
