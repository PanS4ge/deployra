"""Przeglądarka baz: dane logowania i adresy kontenera, komórki wyników, operacje (na bazie demo SQLite), konsola SQL."""

import pytest

from deployra_backend.services import dbbrowse


# --- kontener bazy -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"MYSQL_ROOT_PASSWORD": "r00t", "MYSQL_USER": "app", "MYSQL_PASSWORD": "x"}, ("root", "r00t")),
        ({"MARIADB_ROOT_PASSWORD": "m"}, ("root", "m")),
        ({"MYSQL_RANDOM_ROOT_PASSWORD": "yes", "MYSQL_USER": "app", "MYSQL_PASSWORD": "tajne"}, ("app", "tajne")),
        ({"MARIADB_USER": "wp", "MARIADB_PASSWORD": "pw"}, ("wp", "pw")),
        ({"MYSQL_ALLOW_EMPTY_PASSWORD": "yes"}, ("root", "")),
        ({"ALLOW_EMPTY_PASSWORD": "yes"}, ("root", "")),  # bitnami
        ({}, ("root", "")),
    ],
)
def test_container_login_follows_image_conventions(env, expected):
    assert dbbrowse.container_login(env) == expected


def test_container_login_reads_secret_files():
    files = {"/run/secrets/db_root": "z-pliku\n"}
    assert dbbrowse.container_login({"MYSQL_ROOT_PASSWORD_FILE": "/run/secrets/db_root"}, files.get) == ("root", "z-pliku")


def test_container_addresses_prefers_docker_networks_then_published_port():
    info = {
        "Config": {"Env": ["MYSQL_ROOT_PASSWORD=x"]},
        "NetworkSettings": {"Networks": {"deployra": {"IPAddress": "172.30.0.5"}, "wp": {"IPAddress": "172.31.0.2"}},
                            "Ports": {"3306/tcp": [{"HostIp": "0.0.0.0", "HostPort": "33060"}]}},
    }  # fmt: skip
    assert dbbrowse.container_addresses(info) == [("172.30.0.5", 3306), ("172.31.0.2", 3306), ("127.0.0.1", 33060)]
    # CT na Proxmoxie: najpierw opublikowany port pod adresem CT
    assert dbbrowse.container_addresses(info, "10.0.0.9")[0] == ("10.0.0.9", 33060)


def test_container_addresses_custom_port_and_host_network():
    info = {"Config": {"Env": ["MARIADB_PORT_NUMBER=3307"]}, "HostConfig": {"NetworkMode": "host"}, "NetworkSettings": {"Networks": {"host": {}}}}
    assert dbbrowse.container_addresses(info) == [("127.0.0.1", 3307)]


# --- komórki i identyfikatory --------------------------------------------------------------------------


def test_cells_are_json_safe():
    from datetime import datetime
    from decimal import Decimal

    assert dbbrowse.cell(None, 10) is None
    assert dbbrowse.cell(42, 10) == 42
    assert dbbrowse.cell(Decimal("12.50"), 10) == "12.50"
    assert dbbrowse.cell(datetime(2026, 11, 1, 12, 0), 0) == "2026-11-01 12:00:00"
    assert dbbrowse.cell(b"tekst", 10) == "tekst"
    assert dbbrowse.cell(b"\x89PNG\x00\x01", 10) == {"bin": 6, "hex": "89504e470001"}
    assert dbbrowse.cell(b"\x01", 10) == {"bin": 1, "hex": "01"}  # BIT(1)
    assert dbbrowse.cell("x" * 20, 5) == {"text": "xxxxx", "len": 20}
    assert dbbrowse.cell({"b", "a"}, 10) == "a,b"  # SET


def test_ident_and_like_escape():
    assert dbbrowse.ident("a`b%c") == "`a``b%%c`"
    assert dbbrowse.like_pattern("50%_!") == "%50!%!_!!%"


# --- operacje na bazie demo (to samo SQL co dla MySQL: odwrotne apostrofy, LIMIT/OFFSET, CAST AS CHAR) ---


@pytest.fixture
def demo():
    browser = dbbrowse.DemoBrowser(dbbrowse._DemoConn(dbbrowse._seed_demo()))
    return browser


def test_overview_lists_demo_databases(demo):
    data = demo.overview()
    assert data["dialect"] == "sqlite"
    assert [d["name"] for d in data["databases"]] == ["nextcloud", "wordpress"]


def test_tables_and_structure(demo):
    tables = {t["name"]: t for t in demo.tables("wordpress")}
    assert tables["wp_posts"]["rows"] == 63 and tables["wp_published"]["view"]
    structure = demo.structure("wordpress", "wp_posts")
    assert structure["columns"][0] == {"name": "ID", "type": "integer", "null": True, "key": "PRI", "default": None, "extra": "", "comment": ""}
    assert [i["name"] for i in structure["indexes"]] == ["PRIMARY", "type_status_date"]
    assert "CREATE TABLE" in structure["create"]


def test_rows_paginate_sort_and_filter(demo):
    page = demo.rows("nextcloud", "oc_filecache", page=2, per_page=50, order="fileid", desc=True)
    assert page["total"] == 237 and not page["approx"] and page["primary"] == ["fileid"]
    assert [r[0] for r in page["rows"][:2]] == [187, 186]

    found = demo.rows("nextcloud", "oc_filecache", column="path", search="plik-01")
    assert found["total"] == 10  # plik-010 … plik-019

    everywhere = demo.rows("nextcloud", "oc_users", search="Nowak")
    assert [r[0] for r in everywhere["rows"]] == ["ola"]

    assert demo.rows("nextcloud", "oc_users", search="100%")["total"] == 0  # % w szukanym tekście nie jest wzorcem


def test_rows_unknown_order_column_is_ignored_and_binary_is_marked(demo):
    data = demo.rows("wordpress", "wp_posts", order="nie_ma; DROP TABLE wp_posts", per_page=3)
    assert len(data["rows"]) == 3
    thumb = data["columns"].index(next(c for c in data["columns"] if c["name"] == "thumbnail"))
    assert data["rows"][2][thumb] == {"bin": 5, "hex": "89504e4703"}
    with pytest.raises(dbbrowse.DbError):
        demo.rows("wordpress", "nie_ma")


def test_query_runs_several_statements_and_keeps_results_before_error(demo):
    out = demo.query("UPDATE wp_users SET user_email = 'a@b.c' WHERE ID = 2; SELECT 'x;y' AS t; SELECT * FROM nie_ma; SELECT 1")
    assert out["results"][0]["affected"] == 1
    assert out["results"][1]["rows"] == [["x;y"]]
    assert "nie_ma" in out["error"] and len(out["results"]) == 2


def test_query_truncates_and_counts_large_results(demo):
    out = demo.query("SELECT * FROM nextcloud.oc_filecache", max_rows=100)
    result = out["results"][0]
    assert result["truncated"] and len(result["rows"]) == 100 and result["total"] == 237 and not out["aborted"]


# --- konsola SQL na kursorze w stylu PyMySQL (wiele wyników, przerwanie ogromnego wyniku) -------------------


class ScriptedCursor:
    def __init__(self, sets):
        self.sets, self.index, self.pos = sets, 0, 0
        self.closed = False

    @property
    def description(self):
        cols = self.sets[self.index].get("columns")
        return [(c,) for c in cols] if cols else None

    @property
    def rowcount(self):
        return self.sets[self.index].get("affected", -1)

    def execute(self, sql, args=None):
        self.sql = sql

    def fetchmany(self, size):
        rows = self.sets[self.index].get("rows", [])
        chunk = rows[self.pos : self.pos + size]
        self.pos += len(chunk)
        return chunk

    def nextset(self):
        if self.index + 1 >= len(self.sets):
            return None
        if "error" in self.sets[self.index + 1]:
            raise Exception(1146, self.sets[self.index + 1]["error"])
        self.index, self.pos = self.index + 1, 0
        return True

    def close(self):
        self.closed = True


class ScriptedConn:
    def __init__(self, sets):
        self.cur = ScriptedCursor(sets)
        self.forced = False

    def cursor(self):
        return self.cur

    def _force_close(self):
        self.forced = True


def test_mysql_query_reports_affected_rows_and_mysql_errors():
    conn = ScriptedConn([{"affected": 3}, {"columns": ["n"], "rows": [(1,), (2,)]}, {"error": "Table 'x' doesn't exist"}])
    out = dbbrowse.Browser(conn).query("...")
    assert out["results"][0]["affected"] == 3
    assert out["results"][1]["rows"] == [[1], [2]] and out["results"][1]["total"] == 2
    assert out["error"] == "(1146) Table 'x' doesn't exist"
    assert conn.cur.closed and not conn.forced


def test_mysql_query_aborts_huge_result(monkeypatch):
    monkeypatch.setattr(dbbrowse, "DRAIN_LIMIT", 20)
    conn = ScriptedConn([{"columns": ["n"], "rows": [(i,) for i in range(50_000)]}, {"affected": 1}])
    out = dbbrowse.Browser(conn).query("...", max_rows=5)
    assert out["aborted"] and out["results"][0]["total"] is None and len(out["results"]) == 1
    assert conn.forced and not conn.cur.closed  # bez czytania reszty wyniku do końca


# --- API węzła ---------------------------------------------------------------------------------------


def test_api_uses_demo_database_in_fake_mode(api, ctx, monkeypatch):
    monkeypatch.setattr(ctx.config, "fake_docker", True)
    overview = api.request("POST", "/api/db/overview", json={"container": "nextcloud-db-1"})
    assert overview.status_code == 200 and overview.json()["databases"][0]["name"] == "nextcloud"
    rows = api.request("POST", "/api/db/rows", json={"host": "db.example.com", "user": "u", "db": "wordpress", "table": "wp_users"})
    assert rows.json()["total"] == 2
    missing = api.request("POST", "/api/db/structure", json={"container": "x", "db": "wordpress", "table": "nie_ma"})
    assert missing.status_code == 400 and "nie_ma" in missing.json()["detail"]


def test_api_reports_stopped_or_missing_container(api, ctx):
    fake = ctx.docker_for("local")
    fake.api.create_container("mysql:8", name="db-off", environment={"MYSQL_ROOT_PASSWORD": "x"})
    stopped = api.request("POST", "/api/db/overview", json={"container": "db-off"})
    assert stopped.status_code == 400 and "db-off" in stopped.json()["detail"]
    assert api.request("POST", "/api/db/overview", json={"container": "nie-ma"}).status_code == 400
    assert api.request("POST", "/api/db/overview", json={"container": "../etc"}).status_code == 422


def test_api_connects_to_container_address_with_env_login(api, ctx, monkeypatch):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("mariadb:11", name="wp-db", environment={"MARIADB_ROOT_PASSWORD": "sekret", "MARIADB_DATABASE": "wp"})["Id"]
    fake.api.start(cid)
    calls = []

    def connect(host, port, user, password, database, ssl_mode, timeout, connect_timeout, multi):
        calls.append((host, port, user, password, database))
        return dbbrowse._DemoConn(dbbrowse._seed_demo())

    monkeypatch.setattr(dbbrowse, "_connect", connect)
    monkeypatch.setattr(dbbrowse, "Browser", dbbrowse.DemoBrowser)
    response = api.request("POST", "/api/db/overview", json={"container": "wp-db"})
    assert response.status_code == 200, response.text
    assert calls == [("172.30.0.5", 3306, "root", "sekret", "wp")] and response.json()["default_db"] == "wp"


class CatalogCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, args=None):
        self.sql, self.args = sql, args

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class CatalogConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return CatalogCursor(self.rows)


def test_catalog_bytes_are_decoded():
    rows = [(b"wp_posts", b"BASE TABLE", b"InnoDB", 10, 16384, 0, b"utf8mb4_general_ci", b"")]
    assert dbbrowse.Browser(CatalogConn(rows)).tables("wp")[0]["name"] == "wp_posts"
    cols = [(b"ID", b"bigint(20)", b"NO", b"PRI", None, b"auto_increment", b"")]
    assert dbbrowse.Browser(CatalogConn(cols)).columns("wp", "wp_posts")[0]["type"] == "bigint(20)"
