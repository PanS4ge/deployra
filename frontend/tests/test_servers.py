import re

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from test_resources import job_of, make_app, nodes, wait_job  # noqa: F401 — fixture `nodes`

from deployra_panel.permissions import Permission, parse


@pytest.fixture
def scoped(admin, nodes):
    """Konto „tylko przypisane serwery” z aplikacją web (zasilanie + logi) i kontenerem minecraft (zasilanie)."""
    panel = admin.app.state.panel
    app = make_app(admin, nodes)
    containers = [{"id": "a1", "name": "app-web", "state": "running", "status": "Up"}, {"id": "b2", "name": "minecraft", "state": "exited"}]
    original_get = nodes[1].get

    async def get(path, **kwargs):
        if path.startswith("/api/containers") and "adopt" not in path:
            return containers
        return await original_get(path, **kwargs)

    nodes[1].get = get
    response = admin.post("/settings/users/save", data={"email": "gracz@example.com", "password": "haslo-gracza-1", "scoped": "1",
                                                        "perm_FULL_ACCESS": "1"})  # fmt: skip
    assert response.status_code == 204
    user = panel.db.fetchone("SELECT * FROM users WHERE email = 'gracz@example.com'")
    assert user["scoped"] == 1 and Permission.FULL_ACCESS not in parse(user["permissions"])
    assert admin.post(f"/settings/users/{user['id']}/servers", data={"kind": "app", "app_id": app["id"], "perm_P": "1", "perm_L": "1"}).status_code == 204
    assert admin.post(f"/settings/users/{user['id']}/servers", data={"kind": "container", "node_id": "1", "container": "minecraft",
                                                                      "perm_P": "1", "perm_F": "1"}).status_code == 204  # fmt: skip
    grants = panel.db.fetchall("SELECT * FROM grants WHERE user_id = ? ORDER BY id", [user["id"]])
    assert [g["perms"] for g in grants] == ["PL", "P"]  # pliki nie mają sensu dla gołego kontenera

    client = TestClient(admin.app, follow_redirects=False)
    login = client.post("/login", data={"email": "gracz@example.com", "password": "haslo-gracza-1"}, headers={"HX-Request": "true"})
    assert login.status_code == 204
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/servers").text).group(1)
    client.headers.update({"X-CSRF-Token": csrf, "HX-Request": "true"})
    return client, grants, app


def test_scoped_user_sees_only_assigned(scoped, nodes):
    client, grants, app = scoped
    for path in ("/", "/apps", f"/apps/{app['id']}", "/nodes/1/containers", "/settings", "/settings/users", "/proxy", "/jobs"):
        response = client.get(path, headers={"HX-Request": ""})
        assert response.status_code in (302, 303) and response.headers["location"] == "/servers", path
    assert client.post("/settings", data={"base_domain": "x.pl"}).status_code == 403
    assert client.post(f"/apps/{app['id']}/deploy").status_code == 403
    notifications = client.get("/notifications")  # htmx: przekierowanie nagłówkiem
    assert notifications.headers.get("HX-Redirect") == "/servers" or notifications.headers.get("location") == "/servers"

    page = client.get("/servers", headers={"HX-Request": ""}).text
    assert "Web" in page and "minecraft" in page and "/settings" not in page.split("<main")[0].split("sidebar")[1]
    detail = client.get(f"/servers/{grants[0]['id']}", headers={"HX-Request": ""}).text
    assert "power/stop" in detail and "logs/stream" in detail and "/console" not in detail and "/deploy" not in detail


def test_scoped_actions_follow_permissions(scoped, nodes, admin):
    client, grants, app = scoped
    web, minecraft = grants
    assert client.post(f"/servers/{web['id']}/power/stop").status_code == 200
    assert ("POST", "/api/containers/app-web/action/stop", None) in nodes[1].calls
    assert client.post(f"/servers/{minecraft['id']}/power/start").status_code == 200
    assert ("POST", "/api/containers/minecraft/action/start", None) in nodes[1].calls
    assert client.post(f"/servers/{web['id']}/power/rm -rf").status_code in (400, 404)
    assert client.post(f"/servers/{web['id']}/deploy").status_code == 403  # bez wdrażania
    assert client.post(f"/servers/{web['id']}/env", data={"env": "A=1"}).status_code == 403
    assert client.get(f"/servers/{minecraft['id']}/console").status_code == 403

    # cudze przypisanie = jak nieistniejące
    other = admin.app.state.panel.db.insert("grants", user_id=1, kind="container", node_id=1, container="tajny", perms="PLC")
    assert client.get(f"/servers/{other}", headers={"HX-Request": ""}).status_code == 404
    assert client.post(f"/servers/{other}/power/stop").status_code == 404

    # konsola przez WebSocket: bez uprawnienia odmowa, terminal węzła zamknięty dla takiego konta
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/servers/{web['id']}/console/ws") as ws:
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/nodes/1/containers/app-web/terminal/ws") as ws:
            ws.receive_text()


def test_deploy_and_env_with_permissions(scoped, nodes, admin):
    client, grants, app = scoped
    panel = admin.app.state.panel
    panel.db.update("grants", grants[0]["id"], perms="PLDE")
    response = client.post(f"/servers/{grants[0]['id']}/env", data={"env": "TZ=Europe/Warsaw\nWORKERS=2", "deploy": "1"})
    assert response.status_code == 204
    job_id = panel.db.get("apps", app["id"])["last_job"]
    wait_job(panel, job_id)
    from deployra_panel.services import deploy

    assert deploy.load(panel, panel.db.get("apps", app["id"]))[0].env == {"TZ": "Europe/Warsaw", "WORKERS": "2"}
    deploys = [c for c in nodes[1].calls if c[0] == "JOB" and c[1] == "/api/apps/deploy"]
    assert deploys[-1][2]["env"] == {"TZ": "Europe/Warsaw", "WORKERS": "2"}
    assert client.post(f"/servers/{grants[0]['id']}/deploy").status_code == 204

    # usunięcie aplikacji kasuje przypisanie
    assert admin.post(f"/apps/{app['id']}/remove").status_code == 204
    wait_job(panel, panel.jobs.list()[0].id)
    assert not panel.db.fetchall("SELECT * FROM grants WHERE kind = 'app' AND app_id = ?", [app["id"]])
