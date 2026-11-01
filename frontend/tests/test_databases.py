"""Przeglądarka baz: podpowiedź ze zmiennych aplikacji, połączenia (walidacja, szyfrowane hasło), strony i konsola SQL.

Węzeł zastępuje atrapa (`FakeDbNode`): odpowiada na /api/containers i /api/db/* jak backend.
"""

import json

import pytest

from deployra_common.client import RemoteError
from deployra_common.schemas import AppSpec
from deployra_panel.db import now
from deployra_panel.permissions import Permission
from deployra_panel.security import SESSION_COOKIE
from deployra_panel.services import databases, deploy

VIEW = {
    "overview": {"version": "11.4.3-MariaDB", "user": "root@%", "dialect": "mysql", "default_db": "wp",
                 "databases": [{"name": "information_schema", "tables": 80, "size": 0, "system": True},
                               {"name": "wp", "tables": 2, "size": 65536, "system": False}]},
    "tables": [{"name": "wp_posts", "view": False, "engine": "InnoDB", "rows": 3, "size": 32768, "collation": "utf8mb4_general_ci", "comment": ""},
               {"name": "wp_users", "view": False, "engine": "InnoDB", "rows": 1, "size": 16384, "collation": "utf8mb4_general_ci", "comment": ""}],
    "rows": {"columns": [{"name": "ID", "type": "bigint(20) unsigned", "null": False, "key": "PRI", "default": None, "extra": "auto_increment", "comment": ""},
                         {"name": "post_title", "type": "text", "null": False, "key": "", "default": None, "extra": "", "comment": ""},
                         {"name": "thumb", "type": "blob", "null": True, "key": "", "default": None, "extra": "", "comment": ""}],
             "rows": [[1, "Witaj <świecie>", None], [2, {"text": "Długi", "len": 9000}, {"bin": 2048, "hex": "89504e47"}]],
             "total": 2, "approx": False, "page": 1, "per_page": 50, "primary": ["ID"]},
}  # fmt: skip


class FakeDbNode:
    def __init__(self, containers=None, view=VIEW, fail=None, info=None):
        self.containers, self.view, self.fail, self.info = containers or [], view, fail, info or {}
        self.posts = []

    async def get(self, path, **kwargs):
        if path.startswith("/api/containers/"):
            return self.info
        return self.containers

    async def post(self, path, json=None, **kwargs):
        self.posts.append((path, json))
        if self.fail:
            raise self.fail
        op = path.rsplit("/", 1)[-1]
        if op == "view":
            return {k: v for k, v in self.view.items() if k == "overview" or json.get("db")}
        if op == "overview":
            return self.view["overview"]
        if op == "query":
            return {"results": [{"columns": ["n"], "rows": [[1]], "affected": None, "truncated": False, "total": 1},
                                {"columns": [], "rows": [], "affected": 4, "truncated": False, "total": None}],
                    "error": "(1146) Table 'wp.nie_ma' doesn't exist", "aborted": False, "elapsed": 0.012}  # fmt: skip
        raise AssertionError(path)


def add_node(panel, name="dom"):
    return panel.db.insert("nodes", name=name, transport="http", status="paired", created_at=now())


def add_app(panel, node_id, env=None, slug="blog"):
    spec = AppSpec.model_validate({"slug": slug, "source": {"kind": "image", "image": "wordpress:6"}, "container_port": 80, "env": env or {}})
    return deploy.create_app_row(panel, slug.title(), node_id, spec, {})


def toast_of(response):
    return json.loads(response.headers["HX-Trigger"])["toast"]


# --- podpowiedź ze zmiennych ------------------------------------------------------------------------


def test_env_suggestion_reads_common_conventions():
    wp = databases.env_suggestion({"WORDPRESS_DB_HOST": "db.example.com:3307", "WORDPRESS_DB_USER": "wp", "WORDPRESS_DB_PASSWORD": "x",
                                   "WORDPRESS_DB_NAME": "blog"})  # fmt: skip
    assert wp == {"host": "db.example.com", "port": 3307, "username": "wp", "dbname": "blog", "password_key": "WORDPRESS_DB_PASSWORD",
                  "url": False, "kind": "remote", "container": ""}  # fmt: skip

    laravel = databases.env_suggestion({"DB_CONNECTION": "mysql", "DB_HOST": "mysql", "DB_USERNAME": "app", "DB_PASS": "p", "DB_DATABASE": "shop"},
                                       aliases={"mysql": "shop-mysql-1"})  # fmt: skip
    assert laravel["kind"] == "container" and laravel["container"] == "shop-mysql-1" and laravel["password_key"] == "DB_PASS"

    url = databases.env_suggestion({"DATABASE_URL": "mysql+pymysql://user:p%40ss@10.0.0.5/app?charset=utf8mb4"})
    assert (url["host"], url["port"], url["username"], url["dbname"], url["password_key"]) == ("10.0.0.5", 3306, "user", "app", "DATABASE_URL")
    assert databases.env_password({"DATABASE_URL": "mysql://user:p%40ss@10.0.0.5/app"}, "DATABASE_URL") == "p@ss"


def test_env_suggestion_skips_other_databases():
    assert databases.env_suggestion({"DB_CONNECTION": "pgsql", "DB_HOST": "pg"}) is None
    assert databases.env_suggestion({"DB_HOST": "pg", "DB_PORT": "5432"}) is None
    assert databases.env_suggestion({"DATABASE_URL": "postgres://u:p@h/db"}) is None
    assert databases.env_suggestion({"PATH": "/usr/bin"}) is None


@pytest.mark.parametrize(
    "image, expected",
    [("mysql:8", True), ("mariadb:11.4", True), ("bitnami/mariadb:latest", True), ("mysql/mysql-server:8.0", True),
     ("yobasystems/alpine-mariadb", True), ("prom/mysqld-exporter", False), ("phpmyadmin:5", False), ("mysql/mysql-router", False),
     ("registry:5000/mariadb:11", True), ("nginx", False)],
)  # fmt: skip
def test_mysql_images(image, expected):
    assert databases.is_mysql_image(image) is expected


# --- połączenia ---------------------------------------------------------------------------------------


def test_create_remote_connection_seals_password_and_keeps_it_on_edit(admin):
    panel = admin.app.state.panel
    node_id = add_node(panel)
    response = admin.post("/databases/connections", data={"kind": "remote", "node_id": node_id, "host": "db.example.com", "port": "3306",
                                                         "username": "shop", "password": "tajne", "dbname": "shop", "ssl": "require"})  # fmt: skip
    assert response.status_code == 204 and response.headers["HX-Redirect"] == "/databases/c1"
    row = panel.db.get("db_connections", 1)
    assert row["name"] == "db.example.com" and row["ssl"] == "require" and "tajne" not in row["password"]
    assert panel.sealer.unseal(row["password"]) == "tajne"

    admin.post("/databases/connections/1", data={"kind": "remote", "node_id": node_id, "host": "db2.example.com", "port": "3306",
                                                 "username": "shop", "password": "", "name": "Sklep"})  # fmt: skip
    row = panel.db.get("db_connections", 1)
    assert row["host"] == "db2.example.com" and row["name"] == "Sklep" and panel.sealer.unseal(row["password"]) == "tajne"


@pytest.mark.parametrize(
    "form, error",
    [({"kind": "remote", "host": "bad host!", "username": "u"}, "db.err_host"),
     ({"kind": "remote", "host": "db", "port": "70000", "username": "u"}, "db.err_port"),
     ({"kind": "remote", "host": "db"}, "db.err_user"),
     ({"kind": "container", "container": "../etc"}, "db.err_container")],
)  # fmt: skip
def test_connection_validation(admin, form, error):
    from deployra_panel.i18n import t

    panel = admin.app.state.panel
    node_id = add_node(panel)
    response = admin.post("/databases/connections", data={"node_id": node_id, **form})
    assert response.status_code == 400 and toast_of(response)["message"] == t(error)
    assert not panel.db.fetchall("SELECT * FROM db_connections")


def test_test_button_uses_stored_password(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = add_node(panel)
    conn_id = databases.create(panel, {"name": "x", "node_id": node_id, "host": "db", "port": 3306, "username": "u",
                                       "password": panel.sealer.seal("zapisane"), "endpoint": "local", "ssl": ""})  # fmt: skip
    fake = FakeDbNode()
    monkeypatch.setattr(panel, "node_client", lambda node: fake)
    response = admin.post("/databases/connections/test", data={"conn_id": conn_id, "kind": "remote", "node_id": node_id, "host": "db",
                                                              "username": "u", "password": ""})  # fmt: skip
    assert toast_of(response)["level"] == "ok" and "11.4.3-MariaDB" in toast_of(response)["message"]
    assert fake.posts[0] == ("/api/db/overview", {"host": "db", "port": 3306, "ssl": "", "user": "u", "password": "zapisane"})


# --- przeglądarka ---------------------------------------------------------------------------------------


def test_browser_shows_rows_and_escapes_values(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = add_node(panel)
    fake = FakeDbNode()
    monkeypatch.setattr(panel, "node_client", lambda node: fake)
    page = admin.get(f"/databases/n{node_id}~local~wp-db?db=wp&table=wp_posts&order=ID&dir=desc&q=Witaj")
    assert page.status_code == 200
    assert "Witaj &lt;świecie&gt;" in page.text and "0x89504e47" in page.text and "db-null" in page.text
    assert "wp_users" in page.text and "information_schema" not in page.text  # bazy systemowe schowane
    path, body = fake.posts[0]
    assert path == "/api/db/view" and body == {"container": "wp-db", "ep": "local", "db": "wp", "table": "wp_posts", "mode": "rows", "page": 1,
                                               "per_page": 50, "order": "ID", "desc": True, "search": "Witaj"}  # fmt: skip


def test_browser_shows_node_errors_inline(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = add_node(panel)
    monkeypatch.setattr(panel, "node_client", lambda node: FakeDbNode(fail=RemoteError(400, "(1045) Access denied for user 'root'")))
    page = admin.get(f"/databases/n{node_id}~local~wp-db")
    assert page.status_code == 200 and "Access denied" in page.text

    monkeypatch.setattr(panel, "node_client", lambda node: FakeDbNode(fail=RemoteError(404, "Not Found")))
    from deployra_panel.i18n import t

    assert t("db.err_node_outdated", node="dom") in admin.get(f"/databases/n{node_id}~local~wp-db").text
    assert admin.get("/databases/zly-klucz").status_code == 404


def test_sql_console_renders_all_results(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = add_node(panel)
    fake = FakeDbNode()
    monkeypatch.setattr(panel, "node_client", lambda node: fake)
    response = admin.post(f"/databases/n{node_id}~local~wp-db/sql", data={"sql": "SELECT 1; UPDATE x SET y=1; SELECT * FROM nie_ma", "db": "wp"})
    assert response.status_code == 200
    assert "nie_ma" in response.text and "#2" in response.text
    assert fake.posts[0][1]["sql"].startswith("SELECT 1;") and fake.posts[0][1]["db"] == "wp"


def test_permissions(client, panel, monkeypatch):
    node_id = add_node(panel)
    monkeypatch.setattr(panel, "node_client", lambda node: FakeDbNode())
    user_id = panel.db.insert("users", email="v@example.com", password_hash="x", name="V", permissions=Permission.ALLOW_DATABASES.value)
    sid, csrf = panel.sessions.create(user_id, "127.0.0.1", "pytest")
    client.cookies.set(SESSION_COOKIE, sid)
    client.headers.update({"X-CSRF-Token": csrf, "HX-Request": "true"})
    assert client.get(f"/databases/n{node_id}~local~wp-db?db=wp").status_code == 200
    assert client.post(f"/databases/n{node_id}~local~wp-db/sql", data={"sql": "DROP TABLE wp_users"}).status_code == 403
    assert client.post("/databases/connections", data={"kind": "remote", "node_id": node_id, "host": "h", "username": "u"}).status_code == 403
    panel.db.update("users", user_id, permissions="")
    assert client.get("/databases").status_code == 403


# --- baza aplikacji -------------------------------------------------------------------------------------


def test_app_page_detects_compose_database_and_redirects(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = add_node(panel)
    app_id = add_app(panel, node_id, slug="blog")
    containers = [
        {"id": "a1", "name": "blog-app-1", "image": "wordpress:6", "state": "running", "stack": "blog", "service": "app", "ports": []},
        {"id": "d1", "name": "blog-db-1", "image": "mariadb:11", "state": "running", "stack": "blog", "service": "db", "ports": []},
    ]
    monkeypatch.setattr(panel, "node_client", lambda node: FakeDbNode(containers=containers))
    response = admin.get(f"/apps/{app_id}/database")
    assert response.headers["HX-Redirect"] == f"/databases/n{node_id}~local~blog-db-1?app={app_id}"


def test_app_page_suggests_external_database_and_links_it_with_env_password(admin, monkeypatch):
    panel = admin.app.state.panel
    node_id = add_node(panel)
    app_id = add_app(panel, node_id)
    env = ["WORDPRESS_DB_HOST=mysql.hosting.pl", "WORDPRESS_DB_USER=blog", "WORDPRESS_DB_PASSWORD=z-env", "WORDPRESS_DB_NAME=blog"]
    containers = [{"id": "a1", "name": "app-blog", "image": "wordpress:6", "state": "running", "app": "blog", "ports": []}]
    monkeypatch.setattr(panel, "node_client", lambda node: FakeDbNode(containers=containers, info={"Config": {"Env": env}}))

    page = admin.get(f"/apps/{app_id}/database")
    assert page.status_code == 200
    assert 'value="mysql.hosting.pl"' in page.text and 'name="env_password" value="WORDPRESS_DB_PASSWORD"' in page.text
    assert "z-env" not in page.text  # hasło nie trafia do przeglądarki

    response = admin.post("/databases/connections", data={"kind": "remote", "node_id": node_id, "host": "mysql.hosting.pl", "port": "3306",
                                                         "username": "blog", "password": "", "dbname": "blog", "app_id": app_id,
                                                         "env_password": "WORDPRESS_DB_PASSWORD"})  # fmt: skip
    assert response.status_code == 204
    row = panel.db.fetchone("SELECT * FROM db_connections WHERE app_id = ?", [app_id])
    assert panel.sealer.unseal(row["password"]) == "z-env"
    # jedno źródło → przycisk „Baza danych” prowadzi prosto do przeglądarki
    assert admin.get(f"/apps/{app_id}/database").headers["HX-Redirect"] == f"/databases/c{row['id']}?app={app_id}"


def test_removed_app_unlinks_connections(admin):
    panel = admin.app.state.panel
    node_id = add_node(panel)
    app_id = add_app(panel, node_id)
    conn_id = databases.create(panel, {"name": "x", "node_id": node_id, "app_id": app_id, "host": "db", "port": 3306, "username": "u",
                                       "endpoint": "local", "ssl": ""})  # fmt: skip
    databases.forget_app(panel, app_id)
    assert panel.db.get("db_connections", conn_id)["app_id"] is None
