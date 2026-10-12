import pytest

PAGES = [
    "/", "/apps", "/apps/new", "/nodes", "/nodes/add", "/uptime", "/proxy/hosts", "/proxy/redirects", "/proxy/dead",
    "/proxy/streams", "/proxy/access", "/proxy/access/new", "/proxy/hosts/new", "/proxy/hosts/new?kind=redirect",
    "/proxy/hosts/new?kind=dead", "/certs", "/tunnel", "/import/npm", "/jobs", "/settings", "/settings/dns",
    "/settings/users", "/settings/backup", "/settings/risky", "/notifications", "/analytics/spark?domains=a.example.com",
    "/analytics/chart?all=1", "/analytics/chart?domains=a.example.com&days=1", "/dashboard/tunnel",
    "/settings/updates", "/settings/updates/components", "/traffic", "/traffic/top", "/traffic/suspicious", "/traffic/bans",
    "/settings/alerts", "/settings/plugins", "/servers", "/settings/users/1/servers", "/logs",
]  # fmt: skip


def test_setup_redirect_and_login_flow(client):
    assert client.get("/").headers["location"] == "/setup"
    assert client.get("/setup").status_code == 200
    client.post("/setup", data={"email": "a@b.pl", "password": "bardzo-tajne", "password_repeat": "bardzo-tajne"})
    client.cookies.clear()
    assert client.get("/apps").headers["location"].startswith("/login")
    assert client.get("/setup").status_code in (303, 204)  # po konfiguracji setup jest zamknięty
    bad = client.post("/login", data={"email": "a@b.pl", "password": "zle"}, headers={"HX-Request": "true"})
    assert bad.status_code == 401
    ok = client.post("/login", data={"email": "a@b.pl", "password": "bardzo-tajne"}, headers={"HX-Request": "true"})
    assert ok.status_code == 204 and "dpl_session" in ok.headers["set-cookie"] and "HttpOnly" in ok.headers["set-cookie"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(admin, path):
    response = admin.get(path, headers={"HX-Request": ""})
    assert response.status_code == 200, response.text[:2000]
    assert "Traceback" not in response.text


def test_csrf_required(admin):
    response = admin.post("/settings", data={"base_domain": "x.pl"}, headers={"X-CSRF-Token": "zly"})
    assert response.status_code == 403
    assert admin.post("/settings", data={"base_domain": "x.pl"}).status_code == 200


def test_npm_import_can_be_hidden(admin):
    assert 'href="/import/npm"' in admin.get("/", headers={"HX-Request": ""}).text
    assert admin.post("/settings", data={"base_domain": "example.com", "hide_npm_import": "1"}).status_code == 200
    assert 'href="/import/npm"' not in admin.get("/", headers={"HX-Request": ""}).text
    assert admin.get("/import/npm", headers={"HX-Request": ""}).status_code == 200  # strona dalej działa pod adresem
    admin.post("/settings", data={"base_domain": "example.com"})
    assert 'href="/import/npm"' in admin.get("/", headers={"HX-Request": ""}).text
